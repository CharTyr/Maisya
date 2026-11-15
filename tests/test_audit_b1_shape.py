import asyncio
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from maibot_sdk.context import PluginPaths
from maisya import (
    mcp_worker as mcp_worker_mod,
    services as services_mod,
    shaping as shaping_mod,
)
from maisya.services import Services, validate_endpoint_targets
from maisya.shaping import (
    _LOCKOUT_MESSAGE,
    build_overview,
    execute_shape,
    read_settings_state,
    write_settings,
)
from maisya.vault import Vault, VaultError, private_directory
from plugin import create_plugin
from test_foundation import loaded


async def _boot_clean_plugin(
    tmp_path: Path,
    *,
    permissions=("qq:Alice",),
    enabled_chats=(),
    subdir: str = "host",
):
    async def get(key, default=None):
        return list(permissions) if key == "plugin.permission" else default

    plugin = create_plugin()
    base = tmp_path / subdir
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=base / "data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / f"{subdir}_runtime",
        ),
        logger=logging.getLogger("test.b1.shape"),
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": list(enabled_chats),
        }
    )
    await plugin.on_load()
    return plugin


@pytest.mark.asyncio
async def test_build_overview_offloads_profile_summary_to_thread(tmp_path, monkeypatch):
    plugin = await _boot_clean_plugin(tmp_path)
    try:
        main_tid = threading.get_ident()
        seen_tids: list[int] = []
        orig_summary = shaping_mod._profile_summary

        def wrapped_summary(plg, pid):
            seen_tids.append(threading.get_ident())
            return orig_summary(plg, pid)

        monkeypatch.setattr(shaping_mod, "_profile_summary", wrapped_summary)
        overview = await build_overview(plugin)
        assert overview["profiles"]
        assert seen_tids
        assert all(tid != main_tid for tid in seen_tids)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_settings_anti_lockout_enforced_after_model_validate(tmp_path):
    plugin = await _boot_clean_plugin(tmp_path, enabled_chats=["qq:group:100246809"])
    try:
        state = await read_settings_state(plugin)
        ver = state["version"]

        # 1) Omitting "plugin" key completely in write_settings must be blocked
        with pytest.raises(ValueError, match="plugin\\.enabled=false|锁死"):
            await write_settings(
                plugin,
                {"enabled_chats": ["qq:group:100246809"], "daily_proactive_limit": 2},
                expected_version=ver,
            )
        assert plugin.settings.plugin.enabled is True

        # 2) Passing empty "plugin": {} relies on default enabled=False; must be blocked
        with pytest.raises(ValueError, match="plugin\\.enabled=false|锁死"):
            await write_settings(
                plugin,
                {"plugin": {}, "enabled_chats": ["qq:group:100246809"]},
                expected_version=ver,
            )
        assert plugin.settings.plugin.enabled is True

        # 3) Empty {} via execute_shape write must be blocked
        with pytest.raises(ValueError, match="plugin\\.enabled=false|锁死"):
            await execute_shape(
                plugin,
                action="settings",
                op="write",
                patch={},
                expected_version=ver,
                admin_verified=True,
            )
        assert plugin.settings.plugin.enabled is True

        # 4) Anti-lockout blocks removing all enabled_chats and removing caller's admin entry
        with pytest.raises(ValueError, match="启用会话"):
            await execute_shape(
                plugin,
                action="settings",
                op="patch",
                patch={"enabled_chats": []},
                expected_version=ver,
                admin_verified=True,
            )
        plugin_no_host = await _boot_clean_plugin(
            tmp_path, permissions=(), enabled_chats=["qq:group:100246809"], subdir="no_host"
        )
        try:
            await shaping_mod.patch_settings(plugin_no_host, {"admins": ["qq:Alice"]})
            with pytest.raises(ValueError, match="管理员"):
                await shaping_mod.patch_settings(
                    plugin_no_host,
                    {"admins": []},
                    caller_ident="qq:Alice",
                )
        finally:
            await plugin_no_host.on_unload()

        after = await read_settings_state(plugin)
        assert after["version"] == ver
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_profile_and_settings_patch_do_not_enable_unlisted_private_chat(tmp_path):
    plugin = await _boot_clean_plugin(tmp_path, enabled_chats=[])
    try:
        assert plugin.enabled_for("priv_admin_md5", "qq") is False
        res_prof = await execute_shape(
            plugin,
            action="profile",
            op="patch",
            target="default",
            patch={"max_steps": 16},
            current_chat_id="priv_admin_md5",
            current_platform="qq",
            author="admin",
            admin_verified=True,
        )
        assert res_prof["profile"]["profile_id"] == "default"
        assert plugin.enabled_for("priv_admin_md5", "qq") is False
        assert plugin.settings.enabled_chats == []

        res_global = await execute_shape(
            plugin,
            action="settings",
            op="patch",
            patch={"sleep_window": "01-07", "daily_proactive_limit": 3},
            current_chat_id="priv_admin_md5",
            current_platform="qq",
            author="admin",
            admin_verified=True,
        )
        assert res_global["settings"]["sleep_window"] == "01-07"
        assert plugin.enabled_for("priv_admin_md5", "qq") is False
        assert plugin.settings.enabled_chats == []

        # Explicit add_chats enables only the requested target chat
        res_target = await execute_shape(
            plugin,
            action="settings",
            op="patch",
            patch={"add_chats": ["qq:group:100246809"]},
            current_chat_id="priv_admin_md5",
            current_platform="qq",
            author="admin",
            admin_verified=True,
        )
        assert "qq:group:100246809" in res_target["settings"]["enabled_chats"]
        assert "qq:group:100246809" in plugin.settings.enabled_chats
        assert plugin.enabled_for("priv_admin_md5", "qq") is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_settings_and_file_prevalidate_and_rollback(tmp_path):
    plugin = await _boot_clean_plugin(tmp_path, enabled_chats=["qq:group:100246809"])
    try:
        plugin.vault.set("secret_b1", "top-secret-b1-token-xyz")
        plugin.memory.write("memory", "initial group note\n", chat_id="100246809", author="init")
        base_ver = (await read_settings_state(plugin))["version"]

        # 1) Invalid patch fails before mutating settings or incrementing version
        for bad_patch in (
            {"sleep_window": "99-00"},
            {"daily_proactive_limit": "not-an-int"},
            {"plugin": {"enabled": False}, "daily_proactive_limit": 5},
        ):
            with pytest.raises(ValueError):
                await execute_shape(
                    plugin,
                    action="settings",
                    op="patch",
                    patch=bad_patch,
                    expected_version=base_ver,
                    admin_verified=True,
                )
        assert plugin.settings.sleep_window == ""
        assert plugin.settings.daily_proactive_limit == 1
        assert (await read_settings_state(plugin))["version"] == base_ver

        # 2) Valid patch + version conflict check + rollback restores prior snapshot
        updated = await execute_shape(
            plugin,
            action="settings",
            op="patch",
            patch={"sleep_window": "01-08", "daily_proactive_limit": 4},
            expected_version=base_ver,
            admin_verified=True,
        )
        new_ver = updated["version"]
        assert new_ver == base_ver + 1
        with pytest.raises(ValueError, match="版本冲突"):
            await execute_shape(
                plugin,
                action="settings",
                op="patch",
                patch={"daily_proactive_limit": 2},
                expected_version=base_ver,
                admin_verified=True,
            )
        rolled = await execute_shape(
            plugin,
            action="settings",
            op="rollback",
            version=base_ver,
            expected_version=new_ver,
            admin_verified=True,
        )
        assert rolled["settings"]["sleep_window"] == ""
        assert rolled["settings"]["daily_proactive_limit"] == 1

        # 3) File writes reject plain secrets and support rollback by expected_version
        mem_cur = await execute_shape(
            plugin,
            action="file",
            op="read",
            document="MEMORY.md",
            target_chat="qq:group:100246809",
            admin_verified=True,
        )
        with pytest.raises(ValueError, match="凭据"):
            await execute_shape(
                plugin,
                action="file",
                op="write",
                document="MEMORY.md",
                target_chat="qq:group:100246809",
                content="leak top-secret-b1-token-xyz",
                expected_version=mem_cur["version"],
                admin_verified=True,
            )
        mem_w = await execute_shape(
            plugin,
            action="file",
            op="write",
            document="MEMORY.md",
            target_chat="qq:group:100246809",
            content="updated group note\n",
            expected_version=mem_cur["version"],
            admin_verified=True,
        )
        mem_rb = await execute_shape(
            plugin,
            action="file",
            op="rollback",
            document="MEMORY.md",
            target_chat="qq:group:100246809",
            version=mem_cur["version"],
            expected_version=mem_w["version"],
            admin_verified=True,
        )
        assert mem_rb["content"] == "initial group note\n"
    finally:
        await plugin.on_unload()


def test_vault_private_directory_stop_at_atomic_key_and_no_path_leak(tmp_path):
    # 1) Ancestor symlink above mount/root works both with stop_at and stop_at=None
    real_mount = tmp_path / "real_mount"
    real_mount.mkdir()
    sym_mount = tmp_path / "var_mount"
    sym_mount.symlink_to(real_mount, target_is_directory=True)

    # stop_at=None checks path_is_link_like(path) only -> works under symlinked /var-like parent
    v = Vault(sym_mount / "vault_box")
    v.set("api_key", "super-secret-value")
    assert v.for_execution("api_key") == "super-secret-value"

    # Vault(tmp_path / "link") contract preserved (test_vault.py:58-63)
    direct_link = tmp_path / "link"
    direct_link.symlink_to(real_mount, target_is_directory=True)
    with pytest.raises(VaultError):
        Vault(direct_link)

    # stop_at checks only relative layers beneath stop_at
    leaf = private_directory(sym_mount / "sub" / "leaf", stop_at=sym_mount)
    assert leaf.is_dir()

    outside = tmp_path / "outside"
    outside.mkdir()
    (real_mount / "sub" / "evil_link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(VaultError):
        private_directory(sym_mount / "sub" / "evil_link" / "nested", stop_at=sym_mount)
    with pytest.raises(VaultError):
        private_directory(outside / "nested", stop_at=sym_mount)

    # 2) Concurrent Vault initialization must not observe zero-byte master.key or race crash
    race_dir = tmp_path / "race_vault"
    with ThreadPoolExecutor(max_workers=8) as pool:
        vaults = list(pool.map(lambda _: Vault(race_dir), range(8)))
    vaults[0].set("shared", "race-secret")
    assert all(inst.for_execution("shared") == "race-secret" for inst in vaults)

    # 3) Missing .enc raises VaultError and must NOT leak host absolute path
    with pytest.raises(VaultError) as exc_info:
        v.for_execution("missing_handle")
    assert str(tmp_path) not in str(exc_info.value)
    assert ".enc" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_profile_export_checks_symlinks_only_within_managed_root(tmp_path):
    real_host = tmp_path / "real_host"
    real_host.mkdir()
    sym_host = tmp_path / "sym_host"
    sym_host.symlink_to(real_host, target_is_directory=True)

    plugin = await _boot_clean_plugin(sym_host, subdir="app")
    try:
        good_dir = plugin.data_root / "abilities/skills/good-skill"
        good_dir.mkdir(parents=True, exist_ok=True)
        (good_dir / "SKILL.md").write_text("---\nname: good-skill\n---\n\nok\n", encoding="utf-8")
        plugin.profiles.update("default", skills=["good-skill"])

        raw = plugin.profiles.export("default")
        assert raw

        # Symlink INSIDE managed skill root must still fail closed
        outside = tmp_path / "outside_skill"
        outside.mkdir()
        (outside / "SKILL.md").write_text("---\nname: bad-skill\n---\n\nbad\n", encoding="utf-8")
        bad_dir = plugin.data_root / "abilities/skills/bad-skill"
        bad_dir.symlink_to(outside, target_is_directory=True)
        plugin.profiles.update("default", skills=["bad-skill"])
        with pytest.raises(ValueError, match="链接"):
            plugin.profiles.export("default")
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_services_and_mcp_worker_hardening(tmp_path, monkeypatch):
    # 1) Multicast literal and DNS targets rejected
    with pytest.raises(ValueError):
        validate_endpoint_targets(["224.0.0.1:80"])

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, family=0, type=0: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("224.0.0.251", port))
        ],
    )
    with pytest.raises(ValueError, match="非公网|组播"):
        validate_endpoint_targets(["mcast.example.com:443"])

    # 2) Native OS absolute path enforcement in services.py & legacy templates drop
    vault = Vault(tmp_path / "vault")
    services_cfg = {
        "servers": {
            "badwin": {"command": ["C:\\Windows\\System32\\cmd.exe"], "tools": ["t"]},
        },
        "templates": {
            "legacy_tpl": {"argv": ["/bin/echo"]},
        },
    }
    (tmp_path / "services.json").write_text(json.dumps(services_cfg), encoding="utf-8")
    svc = Services(tmp_path / "services.json", vault)
    loaded_cfg = svc.load()
    assert "templates" not in loaded_cfg
    monkeypatch.setattr(services_mod.os, "name", "posix")
    with pytest.raises(ValueError, match="绝对程序路径|绝对路径"):
        await svc._session(
            "badwin",
            {"chat_id": "c1", "thread_id": "t1"},
            svc.configuration["servers"]["badwin"],
        )

    # 3) _prepare_group cleans up partial cgroup directory on write failure
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()

    def _bad_prepare():
        base = cgroup_root / svc.cgroup_name
        if base.exists():
            return
        # Call real _prepare_group logic redirected to cgroup_root by patching Path("/sys/fs/cgroup")
        return

    real_path_cls = services_mod.Path

    class _PatchedPath:
        def __new__(cls, *args, **kwargs):
            if args and str(args[0]) == "/sys/fs/cgroup":
                return real_path_cls(cgroup_root)
            return real_path_cls(*args, **kwargs)

    monkeypatch.setattr(services_mod, "Path", _PatchedPath)
    orig_write_text = real_path_cls.write_text
    fail_once = {"done": False}

    def flaky_write_text(self, text, *args, **kwargs):
        if self.name in {"memory.max", "pids.max", "cpu.max"}:
            return len(text)
        if self.name == "cgroup.subtree_control" and not fail_once["done"]:
            fail_once["done"] = True
            raise OSError(errno.EOPNOTSUPP, "subtree fail")
        return orig_write_text(self, text, *args, **kwargs)

    monkeypatch.setattr(real_path_cls, "write_text", flaky_write_text)
    with pytest.raises(OSError):
        svc._prepare_group()
    assert not (cgroup_root / svc.cgroup_name).exists()
    # Subsequent retry must succeed now instead of returning early on half-init dir
    svc._prepare_group()
    assert (cgroup_root / svc.cgroup_name / "cgroup.subtree_control").is_file()

    # 4) Root UID dedup avoids collision with active_uids and releases on close
    svc2 = Services(tmp_path / "services2.json", vault)
    echo_srv = tmp_path / "echo_mcp.py"
    echo_srv.write_text(
        """import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    mid = msg.get("id")
    if mid is None:
        continue
    if msg.get("method") == "initialize":
        res = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "s", "version": "1"}}
    elif msg.get("method") == "tools/list":
        res = {"tools": [{"name": "ping", "description": "p", "inputSchema": {"type": "object"}}]}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    svc2.digest = "d2"
    orig_platform = sys.platform
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(services_mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(services_mod.os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(svc2, "_prepare_group", lambda: None)

    orig_urandom = os.urandom
    urandom_vals = iter([(1).to_bytes(3, "big"), (2).to_bytes(3, "big")])
    monkeypatch.setattr(
        services_mod.os,
        "urandom",
        lambda n: next(urandom_vals, (3).to_bytes(3, "big")) if n == 3 else orig_urandom(n),
    )
    svc2.active_uids.add(2000001)

    orig_exec = asyncio.create_subprocess_exec
    launched_args: list[tuple] = []

    async def capture_exec(*args, **kwargs):
        launched_args.append(args)
        # Spawn real Python echo server directly so handshake succeeds without Linux cgroup
        return await orig_exec(sys.executable, str(echo_srv), **{k: v for k, v in kwargs.items() if k in {"stdin", "stdout", "stderr", "env", "cwd", "start_new_session", "limit"}})

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_exec)
    s_uid = await svc2._session(
        "srv",
        {"chat_id": "c1", "thread_id": "t1"},
        {"command": [sys.executable, str(echo_srv)], "tools": ["ping"]},
    )
    worker_payload = json.loads(launched_args[0][3])
    assert worker_payload[2] == 2000002
    assert 2000002 in svc2.active_uids
    await s_uid.close()
    assert 2000002 not in svc2.active_uids

    # 5) Eviction respects locked session & close()/close_sessions() catch per-session errors
    class _FakeSession:
        def __init__(self, name: str, *, fail_close: bool = False):
            self.name = name
            self.fail_close = fail_close
            self.closed = False

        async def close(self):
            self.closed = True
            if self.fail_close:
                raise RuntimeError(f"close failed for {self.name}")

    svc3 = Services(tmp_path / "services3.json", vault)
    svc3.digest = "d3"
    sessions_list = []
    for i in range(16):
        k = (f"s{i}", "d3", "c1", f"t{i}")
        fs = _FakeSession(f"s{i}")
        svc3.sessions[k] = fs
        sessions_list.append(fs)
    # Lock oldest session ("s0", "t0") -> must NOT be evicted or deadlock
    svc3.locks[("s0", "t0")] = asyncio.Lock()
    await svc3.locks[("s0", "t0")].acquire()
    # Replace _start/handshake path by capturing 17th session after eviction check via real echo_srv
    monkeypatch.setattr(sys, "platform", orig_platform)
    monkeypatch.setattr(services_mod.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", orig_exec)

    # Also test direct _session eviction using simple echo server
    s16 = await asyncio.wait_for(
        svc3._session(
            "s16",
            {"chat_id": "c1", "thread_id": "t16"},
            {"command": [sys.executable, str(echo_srv)], "tools": ["ping"]},
        ),
        timeout=3.0,
    )
    assert ("s0", "d3", "c1", "t0") in svc3.sessions
    assert sessions_list[0].closed is False
    assert ("s1", "d3", "c1", "t1") not in svc3.sessions
    assert sessions_list[1].closed is True
    assert s16 in svc3.sessions.values()
    svc3.locks[("s0", "t0")].release()

    # If ALL 16 sessions are locked, _session must fail cleanly without deadlock
    for i in range(16):
        k = (f"s{i}", "d3", "c1", f"t{i}")
        if k not in svc3.sessions:
            svc3.sessions[k] = _FakeSession(f"s{i}")
        lock = svc3.locks.setdefault((f"s{i}", f"t{i}"), asyncio.Lock())
        if not lock.locked():
            await lock.acquire()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(
            svc3._session(
                "s17",
                {"chat_id": "c1", "thread_id": "t17"},
                {"command": [sys.executable, str(echo_srv)], "tools": ["ping"]},
            ),
            timeout=2.0,
        )
    for k_lock in svc3.locks.values():
        if k_lock.locked():
            k_lock.release()

    # close_sessions & close isolate per-session close exceptions and cgroup rmdir EBUSY
    s_err = _FakeSession("bad", fail_close=True)
    s_ok = _FakeSession("good", fail_close=False)
    svc3.sessions = {
        ("bad", "d3", "c1", "ta"): s_err,
        ("good", "d3", "c1", "tb"): s_ok,
    }
    await svc3.close_sessions()
    assert s_err.closed and s_ok.closed and svc3.sessions == {}

    s_err2 = _FakeSession("bad2", fail_close=True)
    s_ok2 = _FakeSession("good2", fail_close=False)
    svc3.sessions = {
        ("bad2", "d3", "c1", "tc"): s_err2,
        ("good2", "d3", "c1", "td"): s_ok2,
    }
    ebusy_group = cgroup_root / svc3.cgroup_name
    ebusy_group.mkdir(exist_ok=True)
    orig_rmdir = real_path_cls.rmdir
    monkeypatch.setattr(
        real_path_cls,
        "rmdir",
        lambda self: (_ for _ in ()).throw(OSError(errno.EBUSY, "busy"))
        if self.name == svc3.cgroup_name
        else orig_rmdir(self),
    )
    await svc3.close()
    assert s_err2.closed and s_ok2.closed and svc3.sessions == {} and svc3.closed is True


def test_mcp_worker_rlimits_and_existing_cgroup_leaf(tmp_path, monkeypatch):
    import resource

    # 1) Non-root on Linux must NOT set RLIMIT_AS or RLIMIT_NPROC
    rlimit_calls: list[tuple[int, tuple]] = []
    exec_calls: list[tuple] = []
    fake_resource = SimpleNamespace(
        RLIMIT_CORE=resource.RLIMIT_CORE,
        RLIMIT_NOFILE=resource.RLIMIT_NOFILE,
        RLIMIT_FSIZE=resource.RLIMIT_FSIZE,
        RLIMIT_AS=resource.RLIMIT_AS,
        RLIMIT_NPROC=resource.RLIMIT_NPROC,
        setrlimit=lambda kind, val: rlimit_calls.append((kind, val)),
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(mcp_worker_mod.sys, "platform", "linux")
    monkeypatch.setattr(mcp_worker_mod.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(mcp_worker_mod.os, "execvpe", lambda *a: exec_calls.append(a))
    monkeypatch.setattr(
        mcp_worker_mod.sys,
        "argv",
        ["mcp_worker.py", json.dumps([["/usr/bin/node"], 256, 2000010, "grp"])],
    )
    mcp_worker_mod.main()
    kinds = {k for k, _ in rlimit_calls}
    assert resource.RLIMIT_AS not in kinds
    assert resource.RLIMIT_NPROC not in kinds
    assert kinds == {resource.RLIMIT_CORE, resource.RLIMIT_NOFILE, resource.RLIMIT_FSIZE}
    assert exec_calls

    # 2) Root with pre-existing leaf dir must succeed (exist_ok=True) and set RLIMIT_NPROC in isolated UID
    rlimit_calls.clear()
    exec_calls.clear()
    cg_base = tmp_path / "sys_cg"
    leaf = cg_base / "grp" / "2000010"
    leaf.mkdir(parents=True)
    real_path_cls = mcp_worker_mod.Path

    class _WorkerPatchedPath:
        def __new__(cls, *args, **kwargs):
            if args and str(args[0]) == "/sys/fs/cgroup":
                return real_path_cls(cg_base)
            return real_path_cls(*args, **kwargs)

    monkeypatch.setattr(mcp_worker_mod, "Path", _WorkerPatchedPath)
    monkeypatch.setattr(mcp_worker_mod.os, "geteuid", lambda: 0)
    monkeypatch.setattr(mcp_worker_mod.os, "setgroups", lambda g: None)
    monkeypatch.setattr(mcp_worker_mod.os, "setgid", lambda gid: None)
    monkeypatch.setattr(mcp_worker_mod.os, "setuid", lambda uid: None)
    mcp_worker_mod.main()
    assert (leaf / "memory.max").read_text(encoding="utf-8") == str(256 * 1024**2)
    assert (leaf / "pids.max").read_text(encoding="utf-8") == "64"
    assert resource.RLIMIT_NPROC in {k for k, _ in rlimit_calls}
