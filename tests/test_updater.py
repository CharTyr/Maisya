"""Tests for Maisya update checker, Pulse admin notification, Management & Shaping wiring."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from maisya.config import Settings
from maisya.pulse import Pulse
from maisya.updater import (
    UpdateChecker,
    _clean_release_notes,
    is_newer_version,
    parse_semver,
    read_manifest_info,
)


def _write_manifest(root: Path, version: str = "0.3.3", repo: str = "https://github.com/CharTyr/Maisya") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "_manifest.json").write_text(
        json.dumps(
            {
                "id": "chartyr.maisya",
                "version": version,
                "urls": {"repository": repo},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_semver_parsing_and_comparison():
    assert parse_semver("v0.3.3") == ((0, 3, 3, 0), (1, ""))
    assert parse_semver("0.4.0-rc.1") == ((0, 4, 0, 0), (0, "rc.1"))
    assert parse_semver("not-a-version") is None

    assert is_newer_version("0.3.4", "0.3.3") is True
    assert is_newer_version("v0.4.0", "0.3.3") is True
    assert is_newer_version("1.0.0", "0.9.9") is True
    assert is_newer_version("0.4.0", "0.4.0-rc.1") is True
    assert is_newer_version("0.4.0-rc.2", "0.4.0-rc.1") is True

    assert is_newer_version("0.3.3", "0.3.3") is False
    assert is_newer_version("v0.3.3", "0.3.3") is False
    assert is_newer_version("0.3.2", "0.3.3") is False
    assert is_newer_version("0.4.0-rc.1", "0.4.0") is False
    assert is_newer_version("bad", "0.3.3") is False
    assert is_newer_version("0.4.0", "bad") is False


def test_read_manifest_info_from_repo_root():
    info = read_manifest_info()
    assert parse_semver(info["current_version"]) is not None
    assert info["owner"] == "CharTyr"
    assert info["repo"] == "Maisya"
    assert info["repository_url"] == "https://github.com/CharTyr/Maisya"


def test_clean_release_notes_strips_html_and_builds_summary():
    raw = "## v0.4.0\n\n<!-- hidden -->\n- **新增更新提醒**：支持 WebUI 与管理员私聊通知。\n- <b>优化</b>启动速度。\n"
    notes, summary = _clean_release_notes(raw)
    assert "<!--" not in notes
    assert "<b>" not in notes
    assert "新增更新提醒" in summary
    assert len(summary) <= 140


@pytest.mark.asyncio
async def test_update_checker_lifecycle_etag_dismiss_and_upgrade(tmp_path: Path):
    _write_manifest(tmp_path / "plugin", version="0.3.3")
    state_path = tmp_path / "data" / "update_state.json"
    now = [1000.0]
    calls: list[dict] = []

    def fake_fetch(owner: str, repo: str, *, etag: str, current_version: str):
        calls.append({"owner": owner, "repo": repo, "etag": etag, "current": current_version})
        if etag == '"etag-40"':
            return {"not_modified": True, "etag": '"etag-40"'}
        return {
            "latest_version": "0.4.0",
            "release_name": "Maisya v0.4.0",
            "release_url": "https://github.com/CharTyr/Maisya/releases/tag/v0.4.0",
            "published_at": "2026-11-20T08:00:00Z",
            "notes": "## 更新亮点\n- 支持版本更新提醒与一键检查\n- 修复若干边界问题",
            "etag": '"etag-40"',
            "source": "github_release",
        }

    enabled = [True]
    notify = [True]
    checker = UpdateChecker(
        state_path,
        plugin_root=tmp_path / "plugin",
        check_enabled=lambda: enabled[0],
        notify_admin=lambda: notify[0],
        interval_seconds=3600.0,
        startup_delay_seconds=0.0,
        fetcher=fake_fetch,
        clock=lambda: now[0],
    )

    st0 = checker.status()
    assert st0["current_version"] == "0.3.3"
    assert st0["has_update"] is False
    assert st0["show_banner"] is False

    # First check discovers 0.4.0
    st1 = await checker.check(force=False)
    assert len(calls) == 1
    assert st1["has_update"] is True
    assert st1["show_banner"] is True
    assert st1["latest_version"] == "0.4.0"
    assert "版本更新提醒" in st1["summary"]
    assert checker.should_notify_admin() is True

    # Within interval, non-forced check skips network
    now[0] += 600.0
    await checker.check(force=False)
    assert len(calls) == 1

    # After interval, check sends ETag and handles 304
    now[0] += 4000.0
    st2 = await checker.check(force=False)
    assert len(calls) == 2
    assert calls[1]["etag"] == '"etag-40"'
    assert st2["latest_version"] == "0.4.0"
    assert st2["checked_at"] == now[0]
    assert st2["last_error"] == ""

    # Dismissing 0.4.0 hides banner and suppresses admin DM
    st_dis = checker.dismiss()
    assert st_dis["dismissed"] is True
    assert st_dis["show_banner"] is False
    assert checker.should_notify_admin() is False

    # Undismissing restores banner
    st_un = checker.undismiss()
    assert st_un["dismissed"] is False
    assert st_un["show_banner"] is True

    # Marking notified suppresses future admin DM for 0.4.0
    checker.mark_notified("0.4.0")
    assert checker.should_notify_admin() is False

    # Disabled auto check skips background check, still allows manual force=True
    enabled[0] = False
    now[0] += 5000.0
    assert checker.due_for_background_check() is False
    await checker.check(force=False)
    assert len(calls) == 2
    await checker.check(force=True)
    assert len(calls) == 3

    # Reloading after local plugin upgrade to 0.4.0 immediately clears has_update
    _write_manifest(tmp_path / "plugin", version="0.4.0")
    checker_upgraded = UpdateChecker(
        state_path,
        plugin_root=tmp_path / "plugin",
        fetcher=fake_fetch,
        clock=lambda: now[0],
    )
    st_up = checker_upgraded.status()
    assert st_up["current_version"] == "0.4.0"
    assert st_up["has_update"] is False
    assert st_up["show_banner"] is False


@pytest.mark.asyncio
async def test_update_checker_handles_errors_without_crashing(tmp_path: Path):
    _write_manifest(tmp_path / "plugin", version="0.3.3")
    state_path = tmp_path / "data" / "update_state.json"

    def broken_fetch(owner: str, repo: str, *, etag: str, current_version: str):
        raise RuntimeError("暂无法连接 GitHub（网络超时或不可达）")

    checker = UpdateChecker(
        state_path,
        plugin_root=tmp_path / "plugin",
        fetcher=broken_fetch,
        clock=lambda: 2000.0,
    )
    st = await checker.check(force=True)
    assert st["has_update"] is False
    assert "GitHub" in st["last_error"]
    assert st["checked_at"] == 2000.0


@pytest.mark.asyncio
async def test_pulse_checks_update_and_notifies_admin_once(tmp_path: Path):
    _write_manifest(tmp_path / "plugin", version="0.3.3")
    state_path = tmp_path / "data" / "update_state.json"
    now = [5000.0]

    def fake_fetch(owner: str, repo: str, *, etag: str, current_version: str):
        return {
            "latest_version": "0.4.0",
            "release_name": "Maisya v0.4.0",
            "release_url": "https://github.com/CharTyr/Maisya/releases/tag/v0.4.0",
            "published_at": "2026-11-20T08:00:00Z",
            "notes": "- 新版本功能亮点",
            "etag": '"e1"',
            "source": "github_release",
        }

    notify_flag = [True]
    checker = UpdateChecker(
        state_path,
        plugin_root=tmp_path / "plugin",
        check_enabled=lambda: True,
        notify_admin=lambda: notify_flag[0],
        interval_seconds=3600.0,
        startup_delay_seconds=0.0,
        fetcher=fake_fetch,
        clock=lambda: now[0],
    )

    wakes: list[dict] = []

    class DummyHost:
        async def call_capability(self, name: str, **kwargs):
            if name == "chat.open_session":
                return {"success": True, "stream_id": "sid_admin_1"}
            if name == "maisaka.proactive.trigger":
                wakes.append(kwargs)
                return {"success": True, "queued": True}
            return {"success": True}

    dummy_bridge = SimpleNamespace(
        claim_ready=lambda *a, **kw: {},
        new_input=lambda cid: None,
        redact=lambda v: v,
        store=None,
    )

    pulse = Pulse(
        dummy_bridge,
        DummyHost(),
        lambda cid, plat="": False,
        admins=lambda: {"qq:10001"},
        plugin_enabled=lambda: True,
        heartbeat_enabled=lambda: False,
        updater=checker,
    )

    await pulse.tick()
    assert len(wakes) == 1
    assert wakes[0]["stream_id"] == "sid_admin_1"
    assert wakes[0]["reason"] == "maisya_update_available"
    assert "v0.4.0" in wakes[0]["intent"]
    assert checker.status()["notified_version"] == "0.4.0"

    # Second tick does NOT re-notify for the same version
    now[0] += 10.0
    await pulse.tick()
    assert len(wakes) == 1


@pytest.mark.asyncio
async def test_pulse_respects_sleep_window_and_notify_toggle(tmp_path: Path):
    _write_manifest(tmp_path / "plugin", version="0.3.3")
    state_path = tmp_path / "data" / "update_state.json"

    def fake_fetch(owner: str, repo: str, *, etag: str, current_version: str):
        return {
            "latest_version": "0.4.0",
            "release_name": "Maisya v0.4.0",
            "release_url": "https://github.com/CharTyr/Maisya/releases/tag/v0.4.0",
            "published_at": "2026-11-20T08:00:00Z",
            "notes": "- 新版本",
            "etag": '"e2"',
            "source": "github_release",
        }

    notify_flag = [False]
    checker = UpdateChecker(
        state_path,
        plugin_root=tmp_path / "plugin",
        check_enabled=lambda: True,
        notify_admin=lambda: notify_flag[0],
        startup_delay_seconds=0.0,
        fetcher=fake_fetch,
        clock=lambda: 5000.0,
    )

    wakes: list[dict] = []

    class DummyHost:
        async def call_capability(self, name: str, **kwargs):
            if name == "chat.open_session":
                return {"success": True, "stream_id": "sid_admin_1"}
            if name == "maisaka.proactive.trigger":
                wakes.append(kwargs)
                return {"success": True, "queued": True}
            return {"success": True}

    dummy_bridge = SimpleNamespace(
        claim_ready=lambda *a, **kw: {},
        new_input=lambda cid: None,
        redact=lambda v: v,
        store=None,
    )
    in_sleep = [True]
    dummy_intents = SimpleNamespace(
        clock=lambda: 5000.0,
        in_sleep_window=lambda sw: in_sleep[0],
        due=lambda *a, **kw: [],
    )

    pulse = Pulse(
        dummy_bridge,
        DummyHost(),
        lambda cid, plat="": False,
        intents=dummy_intents,
        sleep_window="23:00-08:00",
        admins=lambda: {"qq:10001"},
        plugin_enabled=lambda: True,
        heartbeat_enabled=lambda: False,
        updater=checker,
    )

    # notify_admin=False -> checks version, does NOT wake
    await pulse.tick()
    assert checker.status()["has_update"] is True
    assert len(wakes) == 0

    # Turn on notify_admin, still in sleep window -> deferred
    notify_flag[0] = True
    await pulse.tick()
    assert len(wakes) == 0

    # Leave sleep window -> wakes once
    in_sleep[0] = False
    await pulse.tick()
    assert len(wakes) == 1


def test_settings_update_defaults():
    s = Settings()
    assert s.update_check_enabled is True
    assert s.update_notify_admin is True
    s2 = Settings(update_check_enabled=False, update_notify_admin=False)
    assert s2.update_check_enabled is False
    assert s2.update_notify_admin is False


@pytest.mark.asyncio
async def test_plugin_management_and_shaping_update_wiring(tmp_path: Path):
    import logging
    from maibot_sdk.context import PluginPaths
    from maisya.shaping import execute_shape
    from plugin import create_plugin

    async def fake_get(key, default=None):
        if key == "plugin.permission":
            return ["qq:10001"]
        return default

    async def fake_call_cap(name, **kwargs):
        return {"success": True, "models": [], "streams": []}

    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=fake_get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya.updater"),
        call_capability=fake_call_cap,
    )
    plugin.set_plugin_config(
        {"plugin": {"enabled": True, "config_version": "0.1.0"}, "admins": ["qq:10001"]}
    )
    await plugin.on_load()
    try:
        assert plugin.updater is not None
        # Inject deterministic fetcher to avoid real network
        plugin.updater._fetcher = lambda owner, repo, *, etag, current_version: {
            "latest_version": "9.9.0",
            "release_name": "Maisya v9.9.0",
            "release_url": "https://github.com/CharTyr/Maisya/releases/tag/v9.9.0",
            "published_at": "2026-11-20T09:00:00Z",
            "notes": "- 全新测试版本亮点",
            "etag": '"e99"',
            "source": "github_release",
        }

        # 1. maisya_now snapshot returns update block without network
        snap0 = await plugin.console_query()
        assert snap0.get("success", True) is not False
        assert "update" in snap0
        assert snap0["update"]["has_update"] is False

        # 2. manage_write update_check discovers v9.9.0
        chk = await plugin.manage_write("update_check", "{}")
        assert chk["success"] is True
        assert chk["update"]["has_update"] is True
        assert chk["update"]["show_banner"] is True
        assert chk["update"]["latest_version"] == "9.9.0"

        # 3. manage_read settings & maisya_shape overview reflect update state
        s_read = await plugin.manage_read("settings", "{}")
        assert s_read["success"] is True
        assert s_read["update"]["latest_version"] == "9.9.0"
        assert s_read["settings"]["update_check_enabled"] is True

        ov = await execute_shape(plugin, action="overview", admin_verified=True)
        assert ov["update"]["latest_version"] == "9.9.0"
        assert ov["update"]["has_update"] is True

        # 4. dismiss & undismiss via manage_write
        dis = await plugin.manage_write("update_dismiss", json.dumps({"version": "9.9.0"}))
        assert dis["success"] is True
        assert dis["update"]["dismissed"] is True
        assert dis["update"]["show_banner"] is False

        undis = await plugin.manage_write("update_undismiss", "{}")
        assert undis["success"] is True
        assert undis["update"]["dismissed"] is False
        assert undis["update"]["show_banner"] is True

        # 5. patch update toggles via maisya_shape
        patched = await execute_shape(
            plugin,
            action="settings",
            op="patch",
            patch={"update_check_enabled": False, "update_notify_admin": False},
            admin_verified=True,
        )
        assert patched["settings"]["update_check_enabled"] is False
        assert patched["settings"]["update_notify_admin"] is False
        assert plugin.updater.status()["check_enabled"] is False
        assert plugin.updater.status()["notify_admin"] is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_webui_update_bubble_popover():
    from playwright.async_api import async_playwright

    now_html = Path(__file__).resolve().parent.parent / "webui" / "now.html"
    html_text = now_html.read_text(encoding="utf-8")
    assert "ms-upd-chip" in html_text
    assert "ms-upd-pop" in html_text
    assert "ms-update-bar" not in html_text
    assert "set-block-update" not in html_text

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1280, "height": 860})
        try:
            await ctx.add_init_script(
                """
                window.__updState = {
                  current_version: "0.3.3",
                  latest_version: "0.4.0",
                  has_update: true,
                  dismissed: false,
                  show_banner: true,
                  dismissed_version: "",
                  notified_version: "",
                  release_name: "Maisya v0.4.0",
                  release_url: "https://github.com/CharTyr/Maisya/releases/tag/v0.4.0",
                  repository_url: "https://github.com/CharTyr/Maisya",
                  published_at: "2026-11-20T08:00:00Z",
                  summary: "新增自动版本更新提醒与一键检查更新",
                  notes: "## 更新亮点\\n- 顶栏紧凑小气泡提醒新版本\\n- 支持管理员私聊单次温和提醒",
                  checked_at: 1795000000,
                  last_error: "",
                  check_enabled: true,
                  notify_admin: true
                };
                window.__savedPatches = [];
                window.maibot = {
                  ready: Promise.resolve(),
                  context: { theme: "light", dashboardStyle: "millennium" },
                  async query(name, args) {
                    if (name === "snapshot") {
                      return {
                        success: true,
                        threads: [],
                        scheduled_intents: [],
                        watches: [],
                        heartbeat_ticks: [],
                        pending_skill_proposals: [],
                        profiles: ["default"],
                        profile_cards: [{ id: "default", title: "默认助手", enabled: true }],
                        enabled_chats: ["qq:group:100246809"],
                        ui_style: "millennium",
                        plugin_enabled: true,
                        onboarding: { show: false, done: true },
                        update: { ...window.__updState }
                      };
                    }
                    return { success: true };
                  },
                  async action(name, args) {
                    if (name === "write") {
                      const payload = JSON.parse(args.payload || "{}");
                      if (args.action === "update_dismiss") {
                        window.__updState.dismissed = true;
                        window.__updState.show_banner = false;
                        window.__updState.dismissed_version = payload.version || "0.4.0";
                        return { success: true, update: { ...window.__updState } };
                      }
                      if (args.action === "update_undismiss") {
                        window.__updState.dismissed = false;
                        window.__updState.show_banner = true;
                        window.__updState.dismissed_version = "";
                        return { success: true, update: { ...window.__updState } };
                      }
                      if (args.action === "update_check") {
                        window.__updState.checked_at = 1795003600;
                        return { success: true, update: { ...window.__updState } };
                      }
                      if (args.action === "settings_update") {
                        const s = payload.settings || {};
                        window.__savedPatches.push(s);
                        if ("update_check_enabled" in s) window.__updState.check_enabled = Boolean(s.update_check_enabled);
                        if ("update_notify_admin" in s) window.__updState.notify_admin = Boolean(s.update_notify_admin);
                        return { success: true, version: "v2", settings: s, update: { ...window.__updState } };
                      }
                    }
                    return { success: true };
                  }
                };
                """
            )
            page = await ctx.new_page()
            await page.goto(now_html.as_uri(), wait_until="domcontentloaded")
            await page.wait_for_function(
                "() => (document.getElementById('ms-upd-chip-txt')?.textContent || '').includes('0.4.0')",
                timeout=5000,
            )

            # Collapsed by default: zero extra layout space, chip highlighted with dot
            assert await page.locator("#ms-upd-pop").is_hidden()
            assert await page.locator("#ms-upd-dot").is_visible()

            # Click topbar chip -> opens compact speech-bubble popover
            await page.click("#ms-upd-chip")
            await page.wait_for_selector("#ms-upd-pop:not([hidden])", timeout=5000)
            assert "v0.3.3" in (await page.locator("#ms-upd-pop-ver").inner_text())
            assert "可更新 v0.4.0" in (await page.locator("#ms-upd-pop-tag").inner_text())
            assert "顶栏紧凑小气泡" in (await page.locator("#ms-upd-notes").inner_text())

            # Dismiss version -> chip loses dot & hot highlight, tag shows 已忽略
            await page.click("#ms-upd-toggle-dismiss")
            await page.wait_for_function(
                "() => document.getElementById('ms-upd-dot').hidden === true",
                timeout=5000,
            )
            assert "已忽略 v0.4.0" in (await page.locator("#ms-upd-pop-tag").inner_text())
            assert "v0.3.3" in (await page.locator("#ms-upd-chip-txt").inner_text())

            # Undismiss -> dot & hot highlight restored
            await page.click("#ms-upd-toggle-dismiss")
            await page.wait_for_function(
                "() => document.getElementById('ms-upd-dot').hidden === false",
                timeout=5000,
            )

            # Manual check inside bubble
            await page.click("#ms-upd-check-btn")
            await page.wait_for_function(
                "() => !document.getElementById('ms-upd-check-btn').disabled",
                timeout=5000,
            )

            # Toggle admin DM checkbox inside bubble
            await page.locator("#ms-upd-notify").click()
            await page.wait_for_function("() => window.__savedPatches.length >= 1", timeout=5000)
            patches = await page.evaluate("() => window.__savedPatches")
            assert patches[-1] == {"update_notify_admin": False}

            # Escape closes popover
            await page.keyboard.press("Escape")
            await page.wait_for_function(
                "() => document.getElementById('ms-upd-pop').hidden === true",
                timeout=5000,
            )
        finally:
            await browser.close()

