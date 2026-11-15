"""跨平台路径安全、无 dir_fd 回退、平台流格式、冷却与 NapCat 兜底回归测试。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import stat
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import event, loaded

from maisya.abilities import Profile, Profiles
from maisya.intents import CHAT_TARGET
from maisya.memory import MemoryLayers, parse_skill_meta
from maisya.pathsafe import (
    file_lock,
    has_dir_fd,
    is_absolute_program_path,
    minimal_subprocess_env,
)
from maisya.scout import Scout, checked
from maisya.services import Services
from maisya.shaping import collect_known_chats, resolve_chat_target, resolve_stream_spec
from maisya.threads import ThreadStore
from maisya.trust import format_stream_spec, normalize_admins, parse_stream_spec
from maisya.vault import Vault
from maisya.workspaces import Workspaces


CSP_META = (
    '<head><meta http-equiv="Content-Security-Policy" '
    'content="default-src \'none\'; script-src \'unsafe-inline\'; '
    'style-src \'unsafe-inline\'; connect-src \'none\'; img-src data: blob:">'
)


@pytest.mark.asyncio
async def test_workspace_lifecycle_and_windows_guards_without_dir_fd(
    tmp_path, monkeypatch, simulate_no_dir_fd
):
    assert has_dir_fd() is False
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        plugin.memory.create_skill(
            "companion-demo",
            content="---\r\nname: companion-demo\r\ndescription: demo\r\n---\r\n# Demo\r\n",
        )
        comp_file = (
            plugin.memory.root
            / "abilities/skills/companion-demo/references/guide.md"
        )
        comp_file.parent.mkdir(parents=True, exist_ok=True)
        comp_file.write_text("指南正文\r\n", encoding="utf-8")

        toml_ver = plugin.memory.read(
            "profile", target="default", document="profile.toml"
        )["version"]
        plugin.memory.write(
            "profile",
            'title = "全能默认助手"\nenvironment = "readonly"\nskills = ["companion-demo"]\n',
            target="default",
            document="profile.toml",
            expected_version=toml_ver,
        )

        row = plugin.threads.create(
            plugin.profiles.get("default"),
            "跨平台工作区周期",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
            workspace="ws_no_dirfd",
        )
        with plugin.runtime.files.activity(row) as work:
            # write / read / overwrite
            work.write("project/notes.txt", "初稿内容")
            assert work.read("project/notes.txt")["text"] == "初稿内容"
            work.write("project/notes.txt", "覆盖更新内容")
            assert work.read("project/notes.txt")["text"] == "覆盖更新内容"
            work.write("out/report.txt", "交付报告")

            # inventory
            listed, total = plugin.runtime.files.inventory(work.path)
            listed_map = dict(listed)
            assert listed_map["project/notes.txt"] == len("覆盖更新内容".encode("utf-8"))
            assert listed_map["out/report.txt"] == len("交付报告".encode("utf-8"))
            assert total == sum(listed_map.values())

            # commit + rollback
            version = work.commit()
            assert version.startswith("v_")
            assert (
                work.durable / version / "project/notes.txt"
            ).read_text(encoding="utf-8") == "覆盖更新内容"
            work.write("project/notes.txt", "未提交脏写")
            work.write("project/transient.txt", "临时文件")
            work.rollback()
            assert work.read("project/notes.txt")["text"] == "覆盖更新内容"
            with pytest.raises(FileNotFoundError):
                work.read("project/transient.txt")

            # attach
            attached = plugin.runtime.files.attach(row, work, ["out/report.txt"])
            assert len(attached) == 1
            assert (
                plugin.runtime.files.resolve(attached[0]["handle"], "chat").read_text(
                    encoding="utf-8"
                )
                == "交付报告"
            )

            # _sync_skill_companions
            plugin.runtime._sync_skill_companions(row, work)
            assert (
                work.read("in/skills/companion-demo/references/guide.md")["text"]
                == "指南正文\r\n"
            )

            # _import_bytes (create, idempotent repeat, reject overwrite=False, allow overwrite=True)
            plugin.runtime._import_bytes(work, "in/ref.txt", b"v1-bytes", overwrite=False)
            assert work.read("in/ref.txt")["text"] == "v1-bytes"
            plugin.runtime._import_bytes(work, "in/ref.txt", b"v1-bytes", overwrite=False)
            with pytest.raises(ValueError, match="变化"):
                plugin.runtime._import_bytes(
                    work, "in/ref.txt", b"v2-bytes", overwrite=False
                )
            plugin.runtime._import_bytes(work, "in/ref.txt", b"v2-bytes", overwrite=True)
            assert work.read("in/ref.txt")["text"] == "v2-bytes"

            # Reject Windows invalid names / trailing dots & spaces / NTFS ADS colon
            for bad_rel in (
                "project/NUL.txt",
                "project/COM1",
                "project/trail.",
                "project/trail ",
                "project/a:stream",
            ):
                with pytest.raises(ValueError):
                    work.write(bad_rel, "bad")

            # Reject simulated Windows junction / reparse point (st_reparse_tag != 0)
            junction = work.path / "project/junction_target"
            junction.write_text("reparse", encoding="utf-8")
            real_lstat = os.lstat

            def fake_lstat(path, *args, **kwargs):
                st = real_lstat(path, *args, **kwargs)
                if str(path) == str(junction):
                    return SimpleNamespace(
                        st_mode=st.st_mode,
                        st_ino=st.st_ino,
                        st_dev=st.st_dev,
                        st_nlink=st.st_nlink,
                        st_size=st.st_size,
                        st_mtime_ns=st.st_mtime_ns,
                        st_reparse_tag=0xA0000003,
                    )
                return st

            monkeypatch.setattr("maisya.pathsafe.os.lstat", fake_lstat)
            with pytest.raises(ValueError, match="符号链接"):
                work.read("project/junction_target")
            with pytest.raises(ValueError, match="符号链接"):
                work.write("project/junction_target", "overwrite")
            with pytest.raises(ValueError, match="符号链接"):
                plugin.runtime.files.inventory(work.path)
    finally:
        await plugin.on_unload()


def test_memory_layers_lock_crlf_and_read_skill_file_without_dir_fd(
    tmp_path, simulate_no_dir_fd
):
    assert has_dir_fd() is False
    vault = Vault(tmp_path / "vault")
    store = ThreadStore(tmp_path / "state.db")
    profiles = Profiles(tmp_path / "profiles", contains_secret=vault.contains_secret)
    memory = MemoryLayers(tmp_path / "data", store, profiles, vault)

    saved = memory.write("journal", "无 dir_fd 环境下写入记录", expected_version=0)
    assert saved["version"] == 1
    assert saved["content"].endswith("无 dir_fd 环境下写入记录")
    assert memory.read("journal")["content"] == saved["content"]
    with pytest.raises(ValueError):
        memory.write("journal", "stale", expected_version=0)

    with memory._lock():
        with pytest.raises(BlockingIOError):
            with file_lock(memory.root / ".memory.lock", nonblocking=True):
                pass

    skill_raw = (
        "---\r\n"
        "name: crlf-skill\r\n"
        'title: "CRLF 技能标题"\r\n'
        'description: "CRLF 技能说明"\r\n'
        "---\r\n\r\n"
        "# 正文\r\n"
    )
    meta = parse_skill_meta("crlf-skill", skill_raw)
    assert meta["has_frontmatter"] is True
    assert meta["name"] == "crlf-skill"
    assert meta["title"] == "CRLF 技能标题"
    assert meta["description"] == "CRLF 技能说明"

    memory.create_skill("crlf-skill", content=skill_raw)
    assert memory.read("skill", target="crlf-skill")["content"] == skill_raw
    briefs = memory.skill_briefs(["crlf-skill"])
    assert briefs == [{"id": "crlf-skill", "summary": "CRLF 技能说明"}]

    hb_raw = (
        "## 气质与分寸\r\n保持克制。\r\n\r\n"
        "## 自主探索方向\r\n- 观察跨平台行为\r\n\r\n"
        "## 尾注\r\n忽略\r\n"
    )
    temperament, topics = MemoryLayers.heartbeat_parts(hb_raw)
    assert temperament == "保持克制。"
    assert topics == "- 观察跨平台行为"
    memory.write("heartbeat", hb_raw, expected_version=0)
    assert memory.read("heartbeat")["content"] == hb_raw

    companion = memory.root / "abilities/skills/crlf-skill/references/spec.md"
    companion.parent.mkdir(parents=True, exist_ok=True)
    companion.write_text("附属文档 CRLF\r\n", encoding="utf-8")
    read_back = memory.read_skill_file("crlf-skill", "references/spec.md")
    assert read_back["target"] == "crlf-skill"
    assert read_back["path"] == "references/spec.md"
    assert read_back["is_binary"] is False
    assert read_back["content"] == "附属文档 CRLF\r\n"
    for invalid_rel in ("references/NUL.txt", "references/bad.", "../escape.md"):
        with pytest.raises(ValueError):
            memory.read_skill_file("crlf-skill", invalid_rel)


def test_file_lock_nonblocking_contention_and_workspace_mutex(tmp_path, monkeypatch):
    lock_path = tmp_path / ".test.lock"
    with file_lock(lock_path, nonblocking=True):
        with pytest.raises(BlockingIOError):
            with file_lock(lock_path, nonblocking=True):
                pass

    # Exercise Windows locking branch (both intra-process thread lock and msvcrt LK_NBLCK contention)
    nt_lock_path = tmp_path / ".nt.lock"
    nt_lock_path.write_bytes(b"\x00")
    calls: list[tuple[int, int]] = []
    block_next = {"raise": False}
    fake_msvcrt = types.ModuleType("msvcrt")
    fake_msvcrt.LK_LOCK = 1
    fake_msvcrt.LK_NBLCK = 2
    fake_msvcrt.LK_UNLCK = 3

    def fake_locking(fd, mode, nbytes):
        calls.append((mode, nbytes))
        if block_next["raise"] and mode == fake_msvcrt.LK_NBLCK:
            raise PermissionError(13, "The process cannot access the file")

    fake_msvcrt.locking = fake_locking
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr("maisya.pathsafe.Path", type(tmp_path))
    monkeypatch.setattr("maisya.pathsafe.os.name", "nt")

    block_next["raise"] = True
    with pytest.raises(BlockingIOError):
        with file_lock(nt_lock_path, nonblocking=True):
            pass

    block_next["raise"] = False
    entered = threading.Event()
    release = threading.Event()
    worker_errors: list[BaseException] = []

    def hold_lock():
        try:
            with file_lock(nt_lock_path, nonblocking=True):
                entered.set()
                release.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover
            worker_errors.append(exc)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(BlockingIOError):
            with file_lock(nt_lock_path, nonblocking=True):
                pass
    finally:
        release.set()
        thread.join(timeout=5)
    assert not worker_errors
    assert (fake_msvcrt.LK_NBLCK, 1) in calls
    assert (fake_msvcrt.LK_UNLCK, 1) in calls
    monkeypatch.undo()

    # Workspace dual-writer exclusion
    store = ThreadStore(tmp_path / "ws.db")
    files = Workspaces(tmp_path / "workspaces", store)
    row = store.create(
        Profile(), "mutex", chat_id="chat", platform="qq", user_id="", workspace="trip"
    )
    with files.activity(row):
        with pytest.raises(RuntimeError, match="工作区正由原线程写入"):
            with files.activity(row):
                pass


@pytest.mark.asyncio
async def test_windows_minimal_subprocess_env_and_program_path_checks(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MAISYA_LEAK_CHECK", "must-not-inherit")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE")
    monkeypatch.setattr("maisya.pathsafe.os.name", "nt")

    win_home = tmp_path / "mcp-home"
    env = minimal_subprocess_env(home=win_home)
    assert "MAISYA_LEAK_CHECK" not in env
    assert env["SystemRoot"] == r"C:\Windows"
    assert env["windir"] == r"C:\Windows"
    assert env["PATHEXT"] == ".COM;.EXE"
    assert env["COMSPEC"].lower().endswith("cmd.exe")
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        assert env[key] == str(win_home)

    assert is_absolute_program_path("/usr/bin/python3") is True
    assert is_absolute_program_path(r"C:\Python313\python.exe") is True
    assert is_absolute_program_path(r"\\evil\share\a.exe") is False
    assert is_absolute_program_path("//evil/share/a.exe") is False
    assert is_absolute_program_path("python.exe") is False
    assert is_absolute_program_path(r".\python.exe") is False

    monkeypatch.undo()

    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        plugin.management._validate_services(
            {
                "servers": {
                    "win_srv": {
                        "command": [r"C:\Python313\python.exe", "-m", "demo"],
                        "tools": ["ping"],
                        "readonly": True,
                    }
                },
            }
        )
        for bad_cmd in (
            [r"\\evil\share\a.exe"],
            ["//evil/share/a.exe"],
            ["python.exe"],
            ["bin/tool"],
        ):
            with pytest.raises(ValueError, match="绝对路径"):
                plugin.management._validate_services(
                    {
                        "servers": {
                            "bad_srv": {
                                "command": bad_cmd,
                                "tools": ["ping"],
                                "readonly": True,
                            }
                        },
                    }
                )

        # Services._session accepts C:\Python313\python.exe under nt and rejects UNC/relative
        services = Services(tmp_path / "services.json", plugin.vault)
        native_path_cls = type(tmp_path)
        monkeypatch.setattr("maisya.services.Path", native_path_cls)
        monkeypatch.setattr("maisya.pathsafe.Path", native_path_cls)
        monkeypatch.setattr("maisya.services.os.name", "nt")
        monkeypatch.setattr("maisya.pathsafe.os.name", "nt")

        for bad_cmd in ([r"\\evil\share\a.exe"], ["python.exe"]):
            with pytest.raises(ValueError, match="绝对程序路径"):
                await services._session(
                    "bad_srv",
                    {"chat_id": "chat", "thread_id": "th_1"},
                    {"command": bad_cmd, "tools": ["ping"], "readonly": True},
                )

        spawned = []

        class FakeStreamWriter:
            def __init__(self, responses):
                self._responses = responses

            def write(self, data):
                req = json.loads(data.decode("utf-8"))
                if "id" not in req:
                    return
                if req["method"] == "initialize":
                    res = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}
                elif req["method"] == "tools/list":
                    res = {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]}
                else:
                    res = {}
                self._responses.put_nowait(
                    (json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}) + "\n").encode("utf-8")
                )

            async def drain(self):
                return None

        class FakeStreamReader:
            def __init__(self, responses):
                self._responses = responses

            async def readline(self):
                return await self._responses.get()

        class FakeProc:
            def __init__(self):
                self.returncode = None
                q = asyncio.Queue()
                self.stdin = FakeStreamWriter(q)
                self.stdout = FakeStreamReader(q)
                self.pid = 43210

            def terminate(self):
                self.returncode = 0

            def kill(self):
                self.returncode = -9

            async def wait(self):
                self.returncode = 0 if self.returncode is None else self.returncode
                return self.returncode

        async def fake_exec(*args, **kwargs):
            spawned.append((args, kwargs))
            return FakeProc()

        monkeypatch.setattr("maisya.services.asyncio.create_subprocess_exec", fake_exec)
        session = await services._session(
            "win_srv",
            {"chat_id": "chat", "thread_id": "th_win"},
            {
                "command": [r"C:\Python313\python.exe", "-m", "demo"],
                "tools": ["ping"],
                "readonly": True,
            },
        )
        assert spawned
        args, kwargs = spawned[0]
        assert args == (r"C:\Python313\python.exe", "-m", "demo")
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        assert "MAISYA_LEAK_CHECK" not in kwargs["env"]
        await session.close()
    finally:
        monkeypatch.undo()
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_telegram_topic_stream_spec_and_webui_whitelist(tmp_path):
    topic_spec = "telegram:group:-1003883593744::tg-topic::mt=587"
    parsed = parse_stream_spec(topic_spec)
    assert parsed == ("telegram", "group", "-1003883593744::tg-topic::mt=587")
    assert (
        format_stream_spec(
            "Telegram", "GROUP", group_id="-1003883593744::tg-topic::mt=587"
        )
        == topic_spec
    )
    assert CHAT_TARGET.fullmatch(f"chat:{topic_spec}") is not None

    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        scheduled = plugin.intents.schedule(
            kind="proactive",
            source="later",
            target=f"chat:{topic_spec}",
            due_at=plugin.intents.clock() + 60,
            reason="Telegram topic follow-up",
        )
        assert scheduled["target"] == f"chat:{topic_spec}"

        assert await resolve_stream_spec(plugin, topic_spec) == topic_spec
        plugin.messages.upsert_route(
            "tg_topic_session_1",
            platform="telegram",
            chat_type="group",
            group_id="-1003883593744::tg-topic::mt=587",
        )
        assert await resolve_chat_target(plugin, topic_spec) == (
            "tg_topic_session_1",
            "telegram",
        )
        known = await collect_known_chats(plugin)
        assert any(item["stream_spec"] == topic_spec for item in known)

        async def request(mode, arguments):
            return await (
                plugin.manage_read(**arguments)
                if mode == "read"
                else plugin.manage_write(**arguments)
            )

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1100, "height": 850})
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                await page.expose_function("backend", request)
                await page.evaluate(
                    "window.maibot={ready:Promise.resolve(),context:{theme:'light'},"
                    "query:(_,args)=>window.backend('read',args),"
                    "action:(_,args)=>window.backend('write',args),"
                    "navigate:()=>Promise.resolve()}"
                )
                doc = Path("webui/settings.html").read_text(encoding="utf-8").replace(
                    "<head>", CSP_META, 1
                )
                await page.set_content(doc)
                await page.locator("#wl-add-platform").select_option("telegram")
                await page.locator("#wl-add-type").select_option("group")
                await page.locator("#wl-add-id").fill("-1003883593744::tg-topic::mt=587")
                await page.locator("#wl-add-btn").click()
                await expect(page.locator("#chat-whitelist-box")).to_contain_text(
                    topic_spec
                )
                await page.locator("#save-settings").click()
                await expect(page.locator("#feedback")).to_contain_text("已保存")
                assert topic_spec in plugin.settings.enabled_chats
                assert not errors
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_qq_official_case_sensitive_openid_and_webui_allow_users(tmp_path):
    openid = "9C887AB60BA9AA5EE31F16B4F430C795"
    assert normalize_admins([f"QQ:private:{openid}"]) == {f"qq:{openid}"}
    assert normalize_admins([f"QQ:{openid}"]) == {f"qq:{openid}"}
    assert f"qq:{openid.lower()}" not in normalize_admins([f"qq:private:{openid}"])

    profiles = Profiles(tmp_path / "profiles")
    prof_dir = profiles.path / "qq_case"
    prof_dir.mkdir(parents=True)
    (prof_dir / "PROFILE.md").write_text("QQ 官方 OpenID 权限测试。", encoding="utf-8")
    (prof_dir / "profile.toml").write_text(
        'environment = "readonly"\n'
        'access = "listed"\n'
        f'allow_users = ["qq:private:{openid}", "qq:LegacyMixedCase1"]\n',
        encoding="utf-8",
    )
    prof = profiles.get("qq_case")
    assert prof.permits("chat", "QQ", user_id=openid) is True
    assert prof.permits("chat", "qq", user_id=openid.lower()) is False
    assert prof.permits("chat", "QQ", user_id="LegacyMixedCase1") is True
    assert prof.permits("chat", "qq", user_id="legacymixedcase1") is False

    (prof_dir / "profile.toml").write_text(
        'environment = "readonly"\naccess = "listed"\nallow_users = ["qq:private:"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="platform:user_id"):
        profiles.get("qq_case")

    plugin = await loaded(tmp_path / "plugin", [f"QQ:private:{openid}"])
    try:
        assert plugin.admins == {f"qq:{openid}"}

        async def request(mode, arguments):
            return await (
                plugin.manage_read(**arguments)
                if mode == "read"
                else plugin.manage_write(**arguments)
            )

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1100, "height": 850})
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                await page.expose_function("backend", request)
                await page.evaluate(
                    "window.maibot={ready:Promise.resolve(),context:{theme:'light'},"
                    "query:(_,args)=>window.backend('read',args),"
                    "action:(_,args)=>window.backend('write',args),"
                    "navigate:()=>Promise.resolve()}"
                )
                doc = Path("webui/abilities.html").read_text(encoding="utf-8").replace(
                    "<head>", CSP_META, 1
                )
                await page.set_content(doc)
                await page.locator('[data-profile="default"]').click()
                await expect(page.locator("#editor-panel")).to_be_visible()
                await page.locator('[data-doc-tab="config"]').click()
                await expect(page.locator("#cfg-visual")).to_be_visible()
                await expect(page.locator("#cfg-title")).to_have_value("全能默认助手")
                await page.locator("#cfg-access").select_option("listed")
                await expect(page.locator("#cfg-allow-scope-wrap")).to_be_visible()
                await page.locator("#cfg-user-plat").select_option("qq")
                await page.locator("#cfg-user-id").fill(f"QQ:private:{openid}")
                await page.locator("#cfg-add-user-btn").click()
                await expect(page.locator("#content")).to_have_value(
                    re.compile(re.escape(f'"qq:private:{openid}"'))
                )
                await page.locator("#save").click()
                await expect(page.locator("#feedback")).to_contain_text("已保存")
                saved_default = plugin.profiles.get("default")
                assert f"qq:private:{openid}" in saved_default.allow_users
                assert saved_default.permits("chat", "qq", user_id=openid) is True
                assert saved_default.permits("chat", "qq", user_id=openid.lower()) is False
                assert not errors
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_checked_redaction_html_render_hint_and_scout_cooldown(
    tmp_path, monkeypatch
):
    secret = "sk-secret-value-for-checked-and-scout"

    def redactor(obj):
        if isinstance(obj, str):
            return obj.replace(secret, "[REDACTED]")
        if isinstance(obj, dict):
            return {k: redactor(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [redactor(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(redactor(v) for v in obj)
        return obj

    with pytest.raises(RuntimeError) as err_info:
        checked(
            {"success": False, "error": f"provider failed: {secret}\n  try again"},
            redact=redactor,
        )
    msg = str(err_info.value)
    assert msg.startswith("宿主只读能力调用失败，请检查该能力配置：")
    assert "[REDACTED] try again" in msg
    assert secret not in msg
    assert "\n" not in msg

    with pytest.raises(RuntimeError) as msg_info:
        checked({"success": False, "message": "gateway timeout"}, redact=redactor)
    assert str(msg_info.value) == "宿主只读能力调用失败，请检查该能力配置：gateway timeout"

    with pytest.raises(RuntimeError) as empty_info:
        checked({"success": False})
    assert str(empty_info.value) == "宿主只读能力调用失败，请检查该能力配置"

    # html_render missing browser executable hint preserves original error
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        async def fake_cap(name, **arguments):
            if name == "render.html2png":
                return {
                    "success": False,
                    "error": "BrowserType.launch: Executable doesn't exist at /cache/chromium",
                }
            return {"success": True}

        plugin.ctx.call_capability = fake_cap
        default_prof = plugin.profiles.get("default")
        row = plugin.threads.create(
            default_prof, "渲染提示测试", chat_id="chat", platform="qq", user_id="Alice"
        )
        with pytest.raises(RuntimeError) as render_err:
            await plugin.runtime.call(
                "html_render",
                {"html": "<h1>hello</h1>"},
                {**row, "_tool_call_id": "render-1"},
            )
        render_msg = str(render_err.value)
        assert "Executable doesn't exist" in render_msg
        assert "playwright install chromium" in render_msg
    finally:
        await plugin.on_unload()

    # Scout.step() 300s model cooldown reorder & clear on success
    clock = [1000.0]
    monkeypatch.setattr("maisya.scout.time.monotonic", lambda: clock[0])

    tried: list[str] = []
    behavior: dict[str, str] = {"primary-m": "unavailable", "backup-m": "ok"}

    class DummyHost:
        async def call_capability(self, name, **arguments):
            model = arguments.get("model_name", "")
            tried.append(model)
            mode = behavior[model]
            if mode == "unavailable":
                return {
                    "success": False,
                    "error": f"Model does not exist: {model} ({secret})",
                }
            if mode == "rate_limit":
                return {"success": False, "error": f"rate_limit {secret}"}
            return {"success": True, "response": f"ok:{model}", "tool_calls": []}

    scout = Scout(DummyHost(), redact=redactor)
    profile = Profile(
        id="scout", model_name="primary-m", fallback_models=("backup-m",)
    )

    # Round 1: primary-m unavailable -> cooled for 300s, backup-m succeeds
    r1 = await scout.step(profile, [{"role": "user", "content": "q1"}])
    assert tried == ["primary-m", "backup-m"]
    assert r1["used_model"] == "backup-m"
    assert scout._model_cooldown["primary-m"] == pytest.approx(1300.0)

    # Round 2 (t=1100.0, within 300s): backup-m tried first, primary-m skipped
    tried.clear()
    clock[0] = 1100.0
    r2 = await scout.step(profile, [{"role": "user", "content": "q2"}])
    assert tried == ["backup-m"]
    assert r2["used_model"] == "backup-m"

    # Round 3 (t=1301.0, past 300s): cooldown expired, primary-m tried first & succeeds
    tried.clear()
    clock[0] = 1301.0
    behavior["primary-m"] = "ok"
    r3 = await scout.step(profile, [{"role": "user", "content": "q3"}])
    assert tried == ["primary-m"]
    assert r3["used_model"] == "primary-m"
    assert "primary-m" not in scout._model_cooldown

    # Round 4: cooled model succeeds when fallback fails -> cooldown cleared immediately
    tried.clear()
    behavior["primary-m"] = "unavailable"
    await scout.step(profile, [{"role": "user", "content": "q4"}])
    assert "primary-m" in scout._model_cooldown
    tried.clear()
    clock[0] = 1350.0
    behavior["backup-m"] = "rate_limit"
    behavior["primary-m"] = "ok"
    r4 = await scout.step(profile, [{"role": "user", "content": "q5"}])
    assert tried == ["backup-m", "primary-m"]
    assert r4["used_model"] == "primary-m"
    assert "primary-m" not in scout._model_cooldown

    # Round 5: all fail -> final error redacted
    behavior["primary-m"] = "unavailable"
    behavior["backup-m"] = "rate_limit"
    with pytest.raises(RuntimeError) as all_failed:
        await scout.step(profile, [{"role": "user", "content": "q6"}])
    assert secret not in str(all_failed.value)
    assert "[REDACTED]" in str(all_failed.value)


async def _prepare_file_share_thread(tmp_path, *, chat_id="qq:group:987654321", group_id="987654321"):
    plugin = await loaded(tmp_path)
    if chat_id not in plugin.settings.enabled_chats:
        plugin.settings.enabled_chats.append(chat_id)
    if not chat_id.startswith("qq:group:"):
        plugin.messages.upsert_route(
            chat_id,
            platform="qq",
            chat_type="group",
            group_id=group_id,
        )
    step_count = {"n": 0}

    async def prepare_cap(name, **arguments):
        if name == "llm.generate_with_tools":
            step_count["n"] += 1
            if step_count["n"] == 1:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "call-w",
                            "function": {
                                "name": "workspace_file",
                                "arguments": json.dumps(
                                    {
                                        "action": "write",
                                        "path": "out/report.txt",
                                        "content": "跨平台群文件测试内容",
                                    }
                                ),
                            },
                        }
                    ],
                }
            if step_count["n"] == 2:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "call-a",
                            "function": {
                                "name": "result_attach",
                                "arguments": json.dumps({"paths": ["out/report.txt"]}),
                            },
                        }
                    ],
                }
            return {
                "success": True,
                "response": "文件报告已准备好：https://example.org/report",
                "tool_calls": [],
            }
        return {"success": True}

    plugin.ctx.call_capability = prepare_cap
    started = await plugin.agent_tool(
        action="start",
        profile="default",
        message="生成报告文件",
        chat_id=chat_id,
        platform="qq",
    )
    assert started["success"], started
    att_handle = started["result"]["attachments"][0]["handle"]
    return plugin, started["thread_id"], att_handle


@pytest.mark.asyncio
async def test_share_napcat_group_file_fallback_cases(tmp_path):
    expected_b64 = "base64://" + base64.b64encode(
        "跨平台群文件测试内容".encode("utf-8")
    ).decode("ascii")

    # 1) Opaque session resolved via Share.routes + params form succeeds through real PluginContext unboxing
    from maibot_sdk.context import PluginContext

    plugin, thread_id, handle = await _prepare_file_share_thread(
        tmp_path / "case_params",
        chat_id="opaque-qq-group-session",
        group_id="987654321",
    )
    try:
        api_calls = []

        async def rpc_params_ok(method, plugin_id, payload, **kwargs):
            assert method == "cap.call"
            name = payload["capability"]
            arguments = payload["args"]
            if name == "maisaka.reply.generate":
                return {"success": True, "text": "报告放在这里。"}
            if name == "send.hybrid":
                if any(seg.get("type") == "file" for seg in arguments.get("segments", [])):
                    return {"success": False, "sent": False, "message_id": None}
                return {"success": True, "sent": True, "message_id": "msg-text-1"}
            if name == "api.call":
                api_calls.append(arguments)
                return {"success": True, "result": None}
            return {"success": True}

        sdk_ctx = PluginContext(
            "chartyr.maisya",
            rpc_call=rpc_params_ok,
            paths=plugin.ctx.paths,
        )
        plugin.ctx.call_capability = sdk_ctx.call_capability
        res = await plugin.agent_tool(
            action="share",
            thread_id=thread_id,
            chat_id="opaque-qq-group-session",
            platform="qq",
            attachment_handles=[handle],
        )
        assert res["status"] == "sent" and res["sent"] is True
        assert "error" not in res
        assert len(api_calls) == 1
        assert api_calls[0]["api_name"] == "adapter.napcat.file.upload_group_file"
        assert api_calls[0]["args"] == {
            "params": {
                "group_id": "987654321",
                "file": expected_b64,
                "name": "report.txt",
            }
        }
    finally:
        await plugin.on_unload()

    # 2) Arg-shape mismatch on params -> retries flat args and succeeds
    plugin, thread_id, handle = await _prepare_file_share_thread(tmp_path / "case_flat")
    try:
        api_calls = []

        async def cap_flat_retry(name, **arguments):
            if name == "maisaka.reply.generate":
                return {"success": True, "text": "报告放在这里。"}
            if name == "send.hybrid":
                if any(seg.get("type") == "file" for seg in arguments.get("segments", [])):
                    return {"success": False, "sent": False, "error": "file send unsupported"}
                return {"success": True, "sent": True, "message_id": "msg-text-2"}
            if name == "api.call":
                api_calls.append(arguments)
                if len(api_calls) == 1:
                    return {
                        "success": False,
                        "error": "unexpected keyword argument 'params'",
                    }
                return {"success": True, "retcode": 0}
            return {"success": True}

        plugin.ctx.call_capability = cap_flat_retry
        res = await plugin.agent_tool(
            action="share",
            thread_id=thread_id,
            chat_id="qq:group:987654321",
            platform="qq",
            attachment_handles=[handle],
        )
        assert res["status"] == "sent" and res["sent"] is True
        assert len(api_calls) == 2
        assert "params" in api_calls[0]["args"]
        assert api_calls[1]["args"] == {
            "group_id": "987654321",
            "file": expected_b64,
            "name": "report.txt",
        }
    finally:
        await plugin.on_unload()

    # 3) Timeout -> does NOT retry flat args, returns failed + error
    plugin, thread_id, handle = await _prepare_file_share_thread(tmp_path / "case_timeout")
    try:
        api_calls = []

        async def cap_timeout(name, **arguments):
            if name == "maisaka.reply.generate":
                return {"success": True, "text": "报告放在这里。"}
            if name == "send.hybrid":
                if any(seg.get("type") == "file" for seg in arguments.get("segments", [])):
                    return {"success": False, "sent": False}
                return {"success": True, "sent": True, "message_id": "msg-text-3"}
            if name == "api.call":
                api_calls.append(arguments)
                raise TimeoutError("napcat rpc timed out")
            return {"success": True}

        plugin.ctx.call_capability = cap_timeout
        res = await plugin.agent_tool(
            action="share",
            thread_id=thread_id,
            chat_id="qq:group:987654321",
            platform="qq",
            attachment_handles=[handle],
        )
        assert len(api_calls) == 1
        assert res["status"] == "failed" and res["sent"] is False
        assert "timed out" in res["error"]
    finally:
        await plugin.on_unload()

    # 4) Fallback failure -> returns failed with error preserved
    plugin, thread_id, handle = await _prepare_file_share_thread(tmp_path / "case_fail")
    try:
        api_calls = []

        async def cap_fail(name, **arguments):
            if name == "maisaka.reply.generate":
                return {"success": True, "text": "报告放在这里。"}
            if name == "send.hybrid":
                if any(seg.get("type") == "file" for seg in arguments.get("segments", [])):
                    return {"success": False, "sent": False, "error": "hybrid file rejected"}
                return {"success": True, "sent": True, "message_id": "msg-text-4"}
            if name == "api.call":
                api_calls.append(arguments)
                return {"success": False, "retcode": 100, "msg": "upload denied"}
            return {"success": True}

        plugin.ctx.call_capability = cap_fail
        res = await plugin.agent_tool(
            action="share",
            thread_id=thread_id,
            chat_id="qq:group:987654321",
            platform="qq",
            attachment_handles=[handle],
        )
        assert len(api_calls) == 1
        assert res["status"] == "failed" and res["sent"] is False
        assert "hybrid file rejected" in res["error"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_result", "expected_status", "expected_sent", "expect_calls"),
    [
        (None, "sent", True, 1),
        ("napcat-file-9001", "sent", True, 1),
        (9002, "sent", True, 1),
        ({"retcode": 100, "msg": "upload denied"}, "failed", False, 1),
    ],
)
async def test_share_napcat_group_file_real_sdk_unboxing(
    tmp_path, raw_result, expected_status, expected_sent, expect_calls
):
    from maibot_sdk.context import PluginContext

    plugin, thread_id, handle = await _prepare_file_share_thread(tmp_path / "sdk_unbox")
    try:
        api_calls = []

        async def rpc_host(method, plugin_id, payload, **kwargs):
            assert method == "cap.call"
            cap = payload["capability"]
            arguments = payload["args"]
            if cap == "maisaka.reply.generate":
                return {"success": True, "text": "文件在这里。"}
            if cap == "send.hybrid":
                if any(seg.get("type") == "file" for seg in arguments.get("segments", [])):
                    return {"success": False, "sent": False, "message_id": None}
                return {"success": True, "sent": True, "message_id": "msg-sdk-main"}
            if cap == "api.call":
                api_calls.append(arguments)
                return {"success": True, "result": raw_result}
            return {"success": True}

        sdk_ctx = PluginContext(
            "chartyr.maisya",
            rpc_call=rpc_host,
            paths=plugin.ctx.paths,
        )
        plugin.ctx.call_capability = sdk_ctx.call_capability
        res = await asyncio.wait_for(
            plugin.agent_tool(
                action="share",
                thread_id=thread_id,
                chat_id="qq:group:987654321",
                platform="qq",
                attachment_handles=[handle],
            ),
            timeout=3.0,
        )
        assert len(api_calls) == expect_calls
        assert res["status"] == expected_status
        assert res["sent"] is expected_sent
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_share_napcat_group_file_real_sdk_unboxing_shape_fallback(tmp_path):
    from maibot_sdk.context import PluginContext

    plugin, thread_id, handle = await _prepare_file_share_thread(tmp_path / "sdk_retry")
    try:
        api_calls = []

        async def rpc_host(method, plugin_id, payload, **kwargs):
            assert method == "cap.call"
            cap = payload["capability"]
            arguments = payload["args"]
            if cap == "maisaka.reply.generate":
                return {"success": True, "text": "文件在这里。"}
            if cap == "send.hybrid":
                if any(seg.get("type") == "file" for seg in arguments.get("segments", [])):
                    return {"success": False, "sent": False, "message_id": None}
                return {"success": True, "sent": True, "message_id": "msg-sdk-retry"}
            if cap == "api.call":
                api_calls.append(arguments)
                if len(api_calls) == 1:
                    return {
                        "success": True,
                        "result": {
                            "retcode": 100,
                            "message": "unexpected keyword argument 'params'",
                        },
                    }
                return {"success": True, "result": "uploaded-file-id"}
            return {"success": True}

        sdk_ctx = PluginContext(
            "chartyr.maisya",
            rpc_call=rpc_host,
            paths=plugin.ctx.paths,
        )
        plugin.ctx.call_capability = sdk_ctx.call_capability
        res = await asyncio.wait_for(
            plugin.agent_tool(
                action="share",
                thread_id=thread_id,
                chat_id="qq:group:987654321",
                platform="qq",
                attachment_handles=[handle],
            ),
            timeout=3.0,
        )
        assert len(api_calls) == 2
        assert "params" in api_calls[0]["args"]
        assert "params" not in api_calls[1]["args"]
        assert res["status"] == "sent" and res["sent"] is True
    finally:
        await plugin.on_unload()
