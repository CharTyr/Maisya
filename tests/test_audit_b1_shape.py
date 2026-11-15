import asyncio
from concurrent.futures import ThreadPoolExecutor
import errno
import http.server
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace
import urllib.request

import pytest

from maibot_sdk.context import PluginPaths
from maisya import (
    mcp_worker as mcp_worker_mod,
    recipes as recipes_mod,
    services as services_mod,
    shaping as shaping_mod,
)
from maisya.recipes import _http_get_safe, _is_forbidden_ip, export_recipe_archive
from maisya.services import Services, StdioSession, validate_endpoint_targets
from maisya.shaping import (
    _LOCKOUT_MESSAGE,
    _SETUP_CHAT_FIELDS,
    build_overview,
    execute_shape,
    handle_recipe,
    handle_setup,
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


class _BytesHTTPHandler(http.server.BaseHTTPRequestHandler):
    payload: bytes = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format, *args):
        return


@pytest.mark.asyncio
async def test_handle_recipe_create_uses_copy_mode_and_forwards_options(tmp_path, monkeypatch):
    src_plugin = await _boot_clean_plugin(tmp_path / "src", subdir="src_app", enabled_chats=["100246809"])
    dst_plugin = await _boot_clean_plugin(tmp_path / "dst", subdir="dst_app", enabled_chats=["100246809"])
    try:
        # Export a real profile recipe with a skill and a workflow routine from src_plugin
        skill_dir = src_plugin.data_root / "abilities/skills/test-b1-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-b1-skill\ndescription: b1 test skill\n---\n\nbody\n",
            encoding="utf-8",
        )
        await shaping_mod.patch_profile(src_plugin, "scout", {"add_skills": ["test-b1-skill"]})
        src_plugin.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:100246809",
            due_at=src_plugin.intents.clock() + 3600,
            reason="每日侦察例行",
            payload={
                "platform": "qq",
                "profile": "scout",
                "workspace": "",
                "prompt": "Run daily scout",
                "every_seconds": 3600,
            },
            bypass_limits=True,
        )
        archive_bytes, export_meta = export_recipe_archive(src_plugin, "scout")
        routines_meta = export_meta["manifest"].get("routines") or []
        assert routines_meta, export_meta
        rt_id = routines_meta[0]["id"]

        class _Handler(_BytesHTTPHandler):
            payload = archive_bytes

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setattr(
                recipes_mod,
                "_resolve_public_endpoint",
                lambda raw_url: (raw_url, "recipes.example.com", "127.0.0.1", port),
            )
            # 1) Default action="create" on already-existing "scout" should auto-use
            #    mode="copy" and pick suggested_copy_id ("scout-2"), forwarding
            #    install_skills=False and mount_routines without crashing on mode="create"
            res = await handle_recipe(
                dst_plugin,
                action="create",
                url=f"http://recipes.example.com:{port}/scout.zip",
                patch={
                    "install_skills": False,
                    "overwrite_skills": True,
                    "mount_routines": [rt_id],
                    "routine_chat_id": "100246809",
                    "routine_platform": "qq",
                },
            )
            imported = res.get("imported") or {}
            assert imported.get("mode") == "copy"
            assert imported.get("id") == res["suggested_copy_id"]
            assert imported.get("id") != "scout"
            assert (dst_plugin.profiles.path / imported["id"] / "profile.toml").is_file()
            # install_skills=False must actually be respected by management/_import_recipe
            assert imported.get("installed_skills") == []
            assert not (dst_plugin.data_root / "abilities/skills/test-b1-skill").exists()
            mounted = imported.get("mounted_routines") or []
            assert len(mounted) == 1
            chat_intents = dst_plugin.intents.list("chat:100246809")
            assert any(
                (row.get("payload") or {}).get("profile") == imported["id"]
                and (row.get("payload") or {}).get("prompt") == "Run daily scout"
                for row in chat_intents
            )
        finally:
            server.shutdown()
            server.server_close()
    finally:
        await dst_plugin.on_unload()
        await src_plugin.on_unload()


@pytest.mark.asyncio
async def test_handle_recipe_payload_compat_and_drops_dead_fields(tmp_path):
    plugin = await _boot_clean_plugin(tmp_path)
    calls: list[tuple[str, dict]] = []

    async def fake_write(action: str, content: str = "", **kwargs):
        payload = json.loads(content)
        calls.append((action, payload))
        if action == "recipe_preview":
            return {
                "preview_token": "tok-abc",
                "suggested_target": "alpha-target",
                "suggested_copy_id": "alpha-copy",
                "suggested_id": "alpha-legacy",
            }
        return {"ok": True, **payload}

    orig_write = plugin.management.write
    plugin.management.write = fake_write
    try:
        # Preview + create: must drop on_conflict & can_import, forward options, map mode="copy"
        res = await handle_recipe(
            plugin,
            action="create",
            url="https://example.com/r.zip",
            patch={
                "install_skills": False,
                "overwrite_skills": True,
                "mount_routines": ["r1"],
                "routine_chat_id": "100246809",
                "routine_platform": "discord",
            },
        )
        assert "imported" in res
        assert calls[-1][0] == "recipe_import"
        imp_payload = calls[-1][1]
        assert imp_payload["mode"] == "copy"
        assert "on_conflict" not in imp_payload
        assert imp_payload["target"] in {"alpha-copy", "alpha-target"}
        assert imp_payload["install_skills"] is False
        assert imp_payload["overwrite_skills"] is True
        assert imp_payload["mount_routines"] == ["r1"]
        assert imp_payload["routine_chat_id"] == "100246809"
        assert imp_payload["routine_platform"] == "discord"

        # Update mode picks suggested_target
        calls.clear()
        await handle_recipe(
            plugin,
            action="create",
            url="https://example.com/r.zip",
            patch={"mode": "update"},
        )
        assert calls[-1][1]["mode"] == "update"
        assert calls[-1][1]["target"] == "alpha-target"

        # Legacy preview returning only suggested_id still resolves target
        async def legacy_preview_write(action: str, content: str = "", **kwargs):
            payload = json.loads(content)
            calls.append((action, payload))
            if action == "recipe_preview":
                return {"preview_token": "tok-leg", "suggested_id": "legacy-only"}
            return {"ok": True, **payload}

        plugin.management.write = legacy_preview_write
        calls.clear()
        await handle_recipe(plugin, action="create", url="https://example.com/leg.zip")
        assert calls[-1][1]["target"] == "legacy-only"

        # Token direct import forwards options and drops on_conflict
        calls.clear()
        await handle_recipe(
            plugin,
            action="patch",
            preview_token="tok-direct",
            target="custom-id",
            patch={"install_skills": False, "mount_routines": ["daily"]},
        )
        tok_payload = calls[-1][1]
        assert tok_payload["mode"] == "copy"
        assert "on_conflict" not in tok_payload
        assert tok_payload["install_skills"] is False
        assert tok_payload["mount_routines"] == ["daily"]
    finally:
        plugin.management.write = orig_write
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_build_overview_offloads_profile_summary_to_thread(tmp_path, monkeypatch):
    plugin = await _boot_clean_plugin(tmp_path)
    try:
        main_tid = threading.get_ident()
        seen_tids: list[int] = []
        orig_summary = shaping_mod.profile_summary

        def wrapped_summary(plg, pid):
            seen_tids.append(threading.get_ident())
            return orig_summary(plg, pid)

        monkeypatch.setattr(shaping_mod, "profile_summary", wrapped_summary)
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
                action="write",
                kind="settings",
                patch={},
                expected_version=ver,
            )
        assert plugin.settings.plugin.enabled is True
        after = await read_settings_state(plugin)
        assert after["version"] == ver
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_setup_global_profile_fields_do_not_enable_unlisted_private_chat(tmp_path):
    assert "enable_profiles" not in _SETUP_CHAT_FIELDS
    assert "profiles" not in _SETUP_CHAT_FIELDS
    assert "profile" not in _SETUP_CHAT_FIELDS

    plugin = await _boot_clean_plugin(tmp_path, enabled_chats=[])
    try:
        assert plugin.enabled_for("priv_admin_md5", "qq") is False
        for patch in (
            {"enable_profiles": ["digest"]},
            {"profiles": {"scout": {"max_steps": 16}}},
            {"profile": "scout", "model_name": "custom-model"},
        ):
            res = await handle_setup(
                plugin,
                patch=patch,
                current_chat_id="priv_admin_md5",
                current_platform="qq",
                author="admin",
            )
            assert res["applied"].get("profiles")
            assert plugin.enabled_for("priv_admin_md5", "qq") is False
            assert plugin.settings.enabled_chats == []

        # Explicit target_chat must still enable that target chat when requested
        res_target = await handle_setup(
            plugin,
            target_chat="qq:group:100246809",
            patch={"enable_profiles": ["digest"]},
            current_chat_id="priv_admin_md5",
            current_platform="qq",
            author="admin",
        )
        assert res_target["applied"]["settings"]["enabled_chat"] == "qq:group:100246809"
        assert "qq:group:100246809" in plugin.settings.enabled_chats
        assert plugin.enabled_for("priv_admin_md5", "qq") is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_setup_prevalidates_and_rolls_back_on_midflight_error(tmp_path, monkeypatch):
    plugin = await _boot_clean_plugin(tmp_path, enabled_chats=[])
    try:
        target_chat = "qq:group:100246809"
        # Seed baseline memory & schedule & disabled digest profile
        plugin.memory.write("memory", "initial group note\n", chat_id="100246809", author="init")
        await shaping_mod.patch_profile(plugin, "digest", {"enabled": False})
        assert plugin.profiles.get("digest").enabled is False

        orig_intent = plugin.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:100246809",
            due_at=plugin.intents.clock() + 3600,
            reason="每日 08:00 digest",
            payload={
                "platform": "qq",
                "every_seconds": 86400,
                "daily_at": "08:00",
                "tz_offset_minutes": 480,
                "profile": "digest",
                "workspace": "digest",
                "scheduled_admin": True,
            },
            bypass_limits=True,
        )

        # Seed persistent digest sources so both pre-validation and mid-flight rollback cover digest_sources
        async def _seed_sources(dw):
            dw._save_sources(
                {
                    "feeds": [
                        {
                            "id": "f1",
                            "url": "https://example.com/f1.xml",
                            "host": "example.com",
                            "title": "Feed 1",
                            "origin": "manual",
                            "status": "active",
                        }
                    ],
                    "rejected": [],
                }
            )
            dw.work.commit()

        await shaping_mod._run_in_digest_workspace(
            plugin, "100246809", "qq", "digest", None, _seed_sources
        )
        before_sources = await shaping_mod.handle_digest_sources(
            plugin,
            action="read",
            target_chat=target_chat,
            current_chat_id="priv_admin_md5",
            current_platform="qq",
        )
        assert [f["id"] for f in before_sources["feeds"]] == ["f1"]
        base_ver = (await read_settings_state(plugin))["version"]

        # 1) Pre-validation: invalid schedules=123 or invalid daily_at or bad deliver_to
        #    must fail BEFORE writing settings, profiles, memory, or intents
        for bad_patch in (
            {
                "enable_chat": True,
                "daily_proactive_limit": 7,
                "enable_profiles": ["digest"],
                "memory": "should not persist",
                "schedules": 123,
            },
            {
                "enable_chat": True,
                "daily_proactive_limit": 7,
                "enable_profiles": ["digest"],
                "memory": "should not persist",
                "schedules": [{"daily_at": "99:99", "profile": "digest"}],
            },
            {
                "enable_chat": True,
                "daily_proactive_limit": 7,
                "enable_profiles": ["digest"],
                "memory": "should not persist",
                "run_now": True,
                "deliver_to": "invalid_dest",
            },
        ):
            with pytest.raises(ValueError):
                await handle_setup(
                    plugin,
                    target_chat=target_chat,
                    patch=bad_patch,
                    current_chat_id="priv_admin_md5",
                    current_platform="qq",
                )
            assert plugin.settings.enabled_chats == []
            assert plugin.settings.daily_proactive_limit == 1
            assert plugin.profiles.get("digest").enabled is False
            assert (
                plugin.memory.read("memory", chat_id="100246809", allow_custom=True)["content"]
                == "initial group note\n"
            )
            assert (await read_settings_state(plugin))["version"] == base_ver

        # 2) Mid-flight error rollback: valid inputs pass pre-validation, write settings,
        #    profiles, memory, cancel old schedule and insert new schedules, then crash
        #    when engine.start is invoked for trial run. Everything must roll back!
        async def boom_start(*args, **kwargs):
            raise RuntimeError("mid-flight engine start failure")

        monkeypatch.setattr(plugin.engine, "start", boom_start)
        with pytest.raises(RuntimeError, match="mid-flight engine start failure"):
            await handle_setup(
                plugin,
                target_chat=target_chat,
                patch={
                    "enable_chat": True,
                    "daily_proactive_limit": 9,
                    "sleep_window": "01:00-08:00",
                    "enable_profiles": ["digest"],
                    "append_memory": "should rollback this line",
                    "unsubscribe_ids": ["f1"],
                    "auto_seed_sources": False,
                    "daily_at": ["09:00", "20:00"],
                    "run_now": True,
                    "deliver_to": "current",
                },
                current_chat_id="priv_admin_md5",
                current_platform="qq",
                source_msg_id="m-admin-1",
            )

        assert plugin.settings.enabled_chats == []
        assert plugin.settings.daily_proactive_limit == 1
        assert plugin.settings.sleep_window == ""
        assert (await read_settings_state(plugin))["version"] == base_ver
        assert plugin.profiles.get("digest").enabled is False
        assert (
            plugin.memory.read("memory", chat_id="100246809", allow_custom=True)["content"]
            == "initial group note\n"
        )
        after_sources = await shaping_mod.handle_digest_sources(
            plugin,
            action="read",
            target_chat=target_chat,
            current_chat_id="priv_admin_md5",
            current_platform="qq",
        )
        assert [f["id"] for f in after_sources["feeds"]] == ["f1"]
        assert after_sources["rejected"] == []
        pending = plugin.intents.list_all(status="pending", limit=20)
        pending_ids = {row["intent_id"] for row in pending}
        assert pending_ids == {orig_intent["intent_id"]}
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
async def test_recipes_export_checks_symlinks_only_within_managed_root(tmp_path):
    real_host = tmp_path / "real_host"
    real_host.mkdir()
    sym_host = tmp_path / "sym_host"
    sym_host.symlink_to(real_host, target_is_directory=True)

    plugin = await _boot_clean_plugin(sym_host, subdir="app")
    try:
        good_dir = plugin.data_root / "abilities/skills/good-skill"
        good_dir.mkdir(parents=True, exist_ok=True)
        (good_dir / "SKILL.md").write_text("---\nname: good-skill\n---\n\nok\n", encoding="utf-8")
        await shaping_mod.patch_profile(plugin, "scout", {"skills": ["good-skill"]})

        raw, meta = export_recipe_archive(plugin, "scout")
        assert raw and meta["manifest"]["profile"]["id"] == "scout"

        # Symlink INSIDE managed skill root must still fail closed
        outside = tmp_path / "outside_skill"
        outside.mkdir()
        (outside / "SKILL.md").write_text("---\nname: bad-skill\n---\n\nbad\n", encoding="utf-8")
        bad_dir = plugin.data_root / "abilities/skills/bad-skill"
        bad_dir.symlink_to(outside, target_is_directory=True)
        await shaping_mod.patch_profile(plugin, "scout", {"skills": ["bad-skill"]})
        with pytest.raises(ValueError, match="符号链接"):
            export_recipe_archive(plugin, "scout")
    finally:
        await plugin.on_unload()


def test_recipes_http_ignores_env_proxy_and_blocks_embedded_ipv4(monkeypatch):
    # 1) Embedded IPv4 in IPv6 transition / NAT64 prefixes must be blocked
    for addr in (
        "2002:7f00:1::1",
        "64:ff9b::127.0.0.1",
        "64:ff9b::10.0.0.1",
        "64:ff9b:1::10.0.0.1",
        "2001:0000:0101:0101:0000:0000:80ff:fffe",  # Teredo -> 127.0.0.1
    ):
        assert _is_forbidden_ip(ipaddress.ip_address(addr)) is True, addr

    # 2) HTTP_PROXY / HTTPS_PROXY env vars must be ignored by _http_get_safe opener
    proxy_hit = False

    class _TrapProxyHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal proxy_hit
            proxy_hit = True
            self.send_response(502)
            self.end_headers()

        def log_message(self, format, *args):
            return

    class _TargetHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            return

    proxy_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TrapProxyHandler)
    target_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _TargetHandler)
    proxy_thread = threading.Thread(target=proxy_srv.serve_forever, daemon=True)
    target_thread = threading.Thread(target=target_srv.serve_forever, daemon=True)
    proxy_thread.start()
    target_thread.start()
    try:
        proxy_port = proxy_srv.server_address[1]
        target_port = target_srv.server_address[1]
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setattr(
            recipes_mod,
            "_resolve_public_endpoint",
            lambda raw_url: (raw_url, "public.example.test", "127.0.0.1", target_port),
        )
        passed_handlers: list[urllib.request.ProxyHandler] = []
        built_handlers: list[urllib.request.ProxyHandler] = []
        orig_build = urllib.request.build_opener

        def spy_build_opener(*handlers):
            passed_handlers.extend(h for h in handlers if isinstance(h, urllib.request.ProxyHandler))
            op = orig_build(*handlers)
            built_handlers.extend(h for h in op.handlers if isinstance(h, urllib.request.ProxyHandler))
            return op

        monkeypatch.setattr(urllib.request, "build_opener", spy_build_opener)
        body, _ = _http_get_safe(
            f"http://public.example.test:{target_port}/r.zip",
            headers={"User-Agent": "test"},
            timeout=5,
        )
        assert body == b"ok"
        assert proxy_hit is False
        assert passed_handlers and all(h.proxies == {} for h in passed_handlers)
        assert not built_handlers
    finally:
        proxy_srv.shutdown()
        proxy_srv.server_close()
        target_srv.shutdown()
        target_srv.server_close()


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

    # 2) Native OS absolute path enforcement in services.py (without mutating pathsafe.py)
    vault = Vault(tmp_path / "vault")
    services_cfg = {
        "servers": {
            "badwin": {"command": ["C:\\Windows\\System32\\cmd.exe"], "tools": ["t"]},
        },
        "templates": {
            "badtpl": {
                "argv": ["C:\\evil.exe"],
                "secrets": ["k1"],
                "targets": ["1.1.1.1:443"],
                "schema": {"type": "object", "properties": {}},
            }
        },
    }
    (tmp_path / "services.json").write_text(json.dumps(services_cfg), encoding="utf-8")
    svc = Services(tmp_path / "services.json", vault)
    svc.configuration = services_cfg
    svc.digest = "d1"
    monkeypatch.setattr(services_mod.os, "name", "posix")
    with pytest.raises(ValueError, match="绝对程序路径|绝对路径"):
        await svc._session(
            "badwin",
            {"chat_id": "c1", "thread_id": "t1"},
            svc.configuration["servers"]["badwin"],
        )

    fake_profile = SimpleNamespace(
        template="badtpl",
        access="admins",
        secrets=["k1"],
        sandbox_targets=["1.1.1.1:443"],
    )
    fake_runtime = SimpleNamespace(authorized=lambda ctx: True)
    with pytest.raises(ValueError, match="绝对程序路径|绝对路径"):
        await svc.credential(fake_profile, {}, {"chat_id": "c1", "thread_id": "t1"}, fake_runtime)

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
