import asyncio
import base64
import io
import json
import re
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded

WEBUI_DIR = Path(__file__).resolve().parents[1] / "webui"

HOST_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'none'; img-src data: blob:; font-src data:; media-src data: blob:; "
    'object-src \'none\'; frame-src \'none\'; base-uri \'none\'; form-action \'none\'">'
)


def _wrap_csp(html: str) -> str:
    return html.replace("<head>", f"<head>{HOST_CSP_META}", 1)


@asynccontextmanager
async def _open_page(
    plugin,
    tab_name: str,
    *,
    viewport=None,
    on_rpc=None,
    reject_clipboard: bool = False,
):
    errors: list[str] = []
    rpc_log: list[tuple[str, str, dict]] = []
    active = 0
    peak = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = (
            await browser.new_context(viewport=viewport)
            if viewport
            else await browser.new_context()
        )
        page = await context.new_page()
        page.on("pageerror", lambda err: errors.append(f"pageerror:{err}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console:{msg.text}")
            if msg.type == "error"
            else None,
        )

        async def host_rpc(kind, name, arguments):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                args = dict(arguments or {})
                rpc_log.append((kind, name, args))
                if on_rpc is not None:
                    override = await on_rpc(kind, name, args)
                    if override is not None:
                        return override
                if kind == "query":
                    if name == "read":
                        return await plugin.manage_read(**args)
                    if name == "event":
                        return await plugin.console_event(**args)
                    return await plugin.console_query(**args)
                if name == "write":
                    return await plugin.manage_write(**args)
                if name == "note":
                    return await plugin.console_send(**args)
                return await plugin.console_stop(**args)
            finally:
                active -= 1

        await page.expose_function("hostRpc", host_rpc)
        extra_clipboard = (
            "try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new Error('blocked'))}});}catch(_e){}"
            if reject_clipboard
            else ""
        )
        await page.evaluate(
            f"""() => {{
              {extra_clipboard}
              window.maibot = {{
                ready: Promise.resolve(),
                context: {{ theme: "light", host: {{ plugins: {{}} }} }},
                page: {{ state: {{ tab: {json.dumps(tab_name)} }} }},
                query: (name, args) => window.hostRpc("query", name, args),
                action: (name, args) => window.hostRpc("action", name, args),
                navigate: () => Promise.resolve(),
              }};
            }}"""
        )
        raw_doc = (WEBUI_DIR / "now.html").read_text(encoding="utf-8")
        assert "localStorage" not in raw_doc
        assert "sessionStorage" not in raw_doc
        assert "transition: all" not in raw_doc
        await page.set_content(_wrap_csp(raw_doc), wait_until="domcontentloaded")
        try:
            yield page, errors, rpc_log, lambda: peak
            assert await page.locator("img").count() == 0
            assert await page.locator("iframe").count() == 0
        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
async def test_b2_settings_runtime_mobile_and_rail(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("#ms-page-title")).to_have_text("设置")
            for block in (
                "#set-block-chats",
                "#set-block-auth",
                "#set-block-vault",
                "#set-block-runtime",
            ):
                await expect(page.locator(block)).to_be_visible()

            await page.locator("#set-daily-limit").fill("5")
            await page.locator("#set-sleep-start").fill("01:00")
            await page.locator("#set-sleep-end").fill("07:30")
            await page.locator("#set-save-runtime").click()
            await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
            assert plugin.settings.daily_proactive_limit == 5
            assert plugin.settings.sleep_window == "01:00-07:30"
            assert not errors and get_peak() <= 2

        async with _open_page(
            plugin, "settings", viewport={"width": 390, "height": 844}
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("#ms-mob-dock")).to_be_visible()
            assert await page.locator("#ms-mob-dock [data-mob-nav]").count() == 5
            for block in (
                "#set-block-chats",
                "#set-block-auth",
                "#set-block-vault",
                "#set-block-runtime",
            ):
                await expect(page.locator(block)).to_be_visible()

            await expect(page.locator("#set-daily-limit")).to_have_value("5")
            await page.locator("#set-daily-limit").fill("6")
            await page.locator("#set-save-runtime").click()
            await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
            assert plugin.settings.daily_proactive_limit == 6
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            assert not errors and get_peak() <= 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_outputs_binary_pagination_and_mobile_inspect(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    chunk1 = (b"\x89BIN1_START\x00\xff" + bytes(range(256)) * 768)[:196608]
    chunk2 = (b"\xfeBIN2_TAIL\x80\x01" + bytes(range(255, -1, -1)) * 220)[:53392]
    raw_bin = chunk1 + chunk2
    assert len(raw_bin) == 250000
    expected_b64 = base64.b64encode(raw_bin).decode("ascii")

    try:
        profile = plugin.profiles.get("default")
        row = plugin.threads.create(
            profile,
            "导出二进制产物",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:1001",
            workspace="binws",
        )
        with plugin.runtime.files.activity(row) as work:
            (work.path / "out").mkdir(parents=True, exist_ok=True)
            (work.path / "out/archive.bin").write_bytes(raw_bin)
            work.commit()
            attached = plugin.runtime.files.attach(row, work, ["out/archive.bin"])

        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='waiting', result=?, completed_revision=1 WHERE thread_id=?",
                (
                    json.dumps(
                        {
                            "text": "二进制包已生成并附上摘要。",
                            "attachments": attached,
                        },
                        ensure_ascii=False,
                    ),
                    row["thread_id"],
                ),
            )

        # 后端 196608 字节分片读取连续拼接校验
        p1 = await plugin.manage_read(
            "artifact",
            json.dumps(
                {
                    "kind": "attachment",
                    "handle": attached[0]["handle"],
                    "offset": 0,
                    "chunk_size": 196608,
                }
            ),
        )
        assert p1["success"] and p1["next_offset"] == 196608
        p2 = await plugin.manage_read(
            "artifact",
            json.dumps(
                {
                    "kind": "attachment",
                    "handle": attached[0]["handle"],
                    "offset": p1["next_offset"],
                    "chunk_size": 196608,
                }
            ),
        )
        assert p2["success"] and not p2.get("next_offset")
        reconstructed = base64.b64decode(p1["base64"]) + base64.b64decode(p2["base64"])
        assert base64.b64encode(reconstructed).decode("ascii") == expected_b64

        async with _open_page(
            plugin,
            "library",
            viewport={"width": 1440, "height": 900},
            reject_clipboard=True,
        ) as (page, errors, _rpc_log, get_peak):
            bin_card = page.locator("#lib-gallery [data-lib-idx]", has_text="archive.bin").first
            await expect(bin_card).to_be_visible()
            await bin_card.click()
            await expect(page.locator("#lib-drawer")).to_be_visible()
            await expect(page.locator("#lib-stage")).to_be_visible()
            await expect(page.locator("#lib-copy")).to_be_visible()
            await page.locator("#lib-copy").click()
            await page.locator("#lib-drawer-close").click()
            await expect(page.locator("#lib-drawer")).to_be_hidden()
            assert not errors and get_peak() <= 2

        async with _open_page(
            plugin, "library", viewport={"width": 390, "height": 844}
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("#ms-mob-dock")).to_be_visible()
            bin_card = page.locator("#lib-gallery [data-lib-idx]", has_text="archive.bin").first
            await expect(bin_card).to_be_visible()
            await bin_card.click()
            await expect(page.locator("#lib-drawer")).to_be_visible()
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            await page.locator("#lib-drawer-close").click()
            await expect(page.locator("#lib-drawer")).to_be_hidden()
            assert not errors and get_peak() <= 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_hidden_shortcuts_and_recipe_include_profile(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        plugin.vault.set("export_guard_token", "never-export-secret-xyz")

        # 1) 帮助气泡支持 Escape 关闭且不触发意外写请求；档案 .zip 导出含 PROFILE.md 且脱敏
        async with _open_page(
            plugin, "profiles", viewport={"width": 1440, "height": 900}
        ) as (page, errors, rpc_log, get_peak):
            await expect(page.locator('#pr-roster [data-pick-profile-id="default"]')).to_be_visible()
            if await page.locator("#ms-mgmt-view .mil-help").count() > 0:
                await page.locator("#ms-mgmt-view .mil-help").first.click()
                await expect(page.locator("#mil-help-pop")).to_be_visible()
                await page.keyboard.press("Escape")
                await expect(page.locator("#mil-help-pop")).to_be_hidden()

            await page.keyboard.press("Control+s")
            await page.wait_for_timeout(120)
            assert not any(
                kind == "action" and name == "write"
                for kind, name, _ in rpc_log
            )

            await page.locator("#pr-btn-export").click()
            await expect(page.locator("#pkg-scrim")).to_be_visible()
            await page.locator("#pkg-export-download").click()
            await expect(page.locator("#pr-feedback")).to_contain_text("已导出档案包")
            assert not errors and get_peak() <= 2

        exp = await plugin.manage_write(
            "profile_export_zip", json.dumps({"profile_id": "default"})
        )
        assert exp["success"], exp
        raw_zip = base64.b64decode(exp["base64"])
        with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
            names = set(zf.namelist())
            assert any(n.endswith("PROFILE.md") for n in names)
            assert any(n.endswith("profile.toml") for n in names)
            joined = "\n".join(zf.read(n).decode("utf-8", errors="ignore") for n in names)
            assert "never-export-secret-xyz" not in joined

        bad_exp = await plugin.manage_write(
            "profile_export_zip", json.dumps({"profile_id": "nonexistent_role"})
        )
        assert bad_exp["success"] is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_services_drawer_rename_creds_settings_guard_and_batch(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        plugin.vault.set("gh_token", "super-secret-gh-token-456")

        # 1. MCP 服务写入、原子改名与档案能力卡片呈现（脱敏）
        up1 = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "name": "old_svc",
                    "item": {
                        "command": ["/usr/bin/npx", "-y", "@org/mcp"],
                        "credentials": {"GH_TOKEN": "gh_token"},
                        "tools": ["search_repo"],
                        "readonly": True,
                    },
                }
            ),
        )
        assert up1["success"], up1
        up2 = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "orig_kind": "mcp",
                    "orig_name": "old_svc",
                    "name": "new_svc",
                    "item": {
                        "command": ["/usr/bin/npx", "-y", "@org/mcp"],
                        "credentials": {"GH_TOKEN": "gh_token"},
                        "tools": ["search_repo"],
                        "readonly": True,
                    },
                }
            ),
        )
        assert up2["success"], up2
        svcs_after = plugin.runtime.services.load()
        assert "old_svc" not in svcs_after.get("servers", {})
        assert "new_svc" in svcs_after.get("servers", {})
        assert svcs_after["servers"]["new_svc"]["credentials"] == {"GH_TOKEN": "gh_token"}

        async with _open_page(
            plugin, "profiles", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await page.locator("#pr-adv-toggle").click()
            await expect(page.locator("#pr-adv-body")).to_be_visible()
            await page.locator('[data-pr-tab="abilities"]').click()
            await expect(page.locator("#pr-ability-cards")).to_contain_text("new_svc")
            assert "old_svc" not in await page.locator("#pr-ability-cards").inner_text()
            assert "super-secret-gh-token-456" not in await page.content()
            assert not errors and get_peak() <= 2

        # 2. 设置读取失败时不静默覆写原配置，且明确显示错误信息
        async def fail_settings_rpc(kind, name, args):
            if kind == "query" and name == "read":
                sec = args.get("section")
                if sec == "settings":
                    return {"success": False, "error": "模拟设置读取故障"}
                if sec == "batch":
                    real = await plugin.manage_read(**args)
                    real["sections"]["settings"] = {
                        "success": False,
                        "error": "模拟设置读取故障",
                    }
                    return real
            return None

        async with _open_page(
            plugin,
            "settings",
            viewport={"width": 1440, "height": 900},
            on_rpc=fail_settings_rpc,
        ) as (page, errors, rpc_log, get_peak):
            await expect(page.locator("#set-feedback")).to_contain_text("模拟设置读取故障")
            assert not any(
                kind == "action" and name == "write"
                for kind, name, _ in rpc_log
            )
            assert not errors and get_peak() <= 2

        # 3. Backend batch partial failure isolation & UI graceful rendering
        orig_load = plugin.runtime.services.load

        def broken_services_load():
            raise RuntimeError("boom-services-io")

        plugin.runtime.services.load = broken_services_load
        try:
            batch_res = await plugin.manage_read(
                "batch",
                json.dumps({"sections": {"settings": {}, "services": {}}}),
            )
            assert batch_res["success"] is True
            assert batch_res["sections"]["settings"]["success"] is True
            assert batch_res["sections"]["services"]["success"] is False
            assert "boom-services-io" in batch_res["sections"]["services"]["error"]

            async with _open_page(
                plugin, "settings", viewport={"width": 1440, "height": 900}
            ) as (page, errors, _rpc_log, get_peak):
                await expect(page.locator("#set-daily-limit")).to_be_visible()
                assert not errors and get_peak() <= 2
        finally:
            plugin.runtime.services.load = orig_load
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_management_backend_p1_fixes(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809", "qq:private:2001"])
    try:
        # 1. undo_learning_note succeeds for user-cloned custom profile (allow_custom=True)
        plugin.profiles.clone("default", "custom_default")
        plugin.memory.seed_profile("custom_default")
        plugin.memory.write(
            "notes",
            "自定义角色初版方法论。",
            target="custom_default",
            author="user",
            allow_custom=True,
        )
        th = plugin.threads.create(
            plugin.profiles.get("custom_default"),
            "沉淀自定义方法",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:1001",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db, th["thread_id"], "input", 1, {"text": "总结两条常用约定"}
            )
            db.execute(
                "INSERT INTO learning_receipts (thread_id, revision, status) VALUES (?, 1, 'applied')",
                (th["thread_id"],),
            )
        v2 = plugin.memory.write(
            "notes",
            "自定义角色初版方法论。\n- 追加自动笔记",
            target="custom_default",
            author=f"thread:{th['thread_id']}",
            allow_custom=True,
        )
        intents_res = await plugin.manage_read("intents", "{}")
        receipt = next(
            item
            for item in intents_res["learning"]
            if item.get("profile") == "custom_default"
        )
        undo_res = await plugin.manage_write(
            "undo_learning_note",
            json.dumps(
                {
                    "profile": "custom_default",
                    "version": v2["version"],
                    "at": receipt["at"],
                }
            ),
        )
        assert undo_res["success"] is True
        assert (
            "追加自动笔记"
            not in plugin.memory.read("notes", target="custom_default")["content"]
        )

        # 2. _proactive_quota 按全局总量计（docs/08 第四节）、按触发日计数并排除 'failed'
        settings_dump = plugin.settings.model_dump()
        settings_dump["daily_proactive_limit"] = 3
        await plugin.on_config_update("self", settings_dump, 0)

        now = datetime.now().astimezone()
        today_mid = now.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        yest_mid = (
            now.replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
        ).timestamp()

        with plugin.threads.connect() as db, db:
            rows = [
                ("i_g_ok", "proactive", "longing", "chat:qq:group:100246809", today_mid, "test", "", "{}", "done", "", today_mid, today_mid),
                ("i_g_fail", "proactive", "longing", "chat:qq:group:100246809", today_mid, "test", "", "{}", "failed", "", today_mid, today_mid),
                ("i_g_old", "proactive", "serendipity", "chat:qq:group:100246809", yest_mid, "test", "", "{}", "done", "", yest_mid, yest_mid),
                ("i_p_ok1", "proactive", "longing", "chat:qq:private:2001", today_mid, "test", "", "{}", "done", "", today_mid, today_mid),
                ("i_p_ok2", "proactive", "afterglow", "chat:qq:private:2001", today_mid, "test", "", "{}", "done", "", yest_mid, today_mid),
            ]
            for row_vals in rows:
                db.execute(
                    "INSERT INTO intents(intent_id,kind,source,target,due_at,reason,thread_id,payload,status,wake_status,created,handled) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    row_vals,
                )

        s_res = await plugin.manage_read("settings", "{}")
        assert s_res["proactive_quota"] == {"used": 3, "limit": 3, "remaining": 0}
        assert plugin.intents.proactive_quota_available("qq:private:2001") is False

        # 3. _collect_host_models finishes well below 8s WebUI budget when host probes stall
        async def slow_cap(name, **_kwargs):
            if name in {"llm.get_available_models", "statistics.local.models"}:
                await asyncio.sleep(9.0)
            return {"success": True, "models": [], "streams": []}

        plugin.ctx.call_capability = slow_cap
        t0 = time.monotonic()
        disc = await plugin.manage_read("discovery", "{}")
        elapsed = time.monotonic() - t0
        assert disc["success"] is True
        assert elapsed < 8.0
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_ui_style_persistence_and_now_empty_profile_poll(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        default_prof = plugin.profiles.get("default")
        row = plugin.threads.create(
            default_prof,
            "探索主线仅属于 default",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:1001",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(db, row["thread_id"], "input", 1, {"text": "default 首条输入"})

        # 1. #ui-style-toggle 切换并持久化 modern（且兼容旧值 maiwork -> modern）
        async with _open_page(
            plugin, "now", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "millennium"
            )
            await page.evaluate(
                "() => { window.__styleSaved = false; window.addEventListener('maisya:ui-style-saved', () => { window.__styleSaved = true; }, { once: true }); }"
            )
            await page.locator("#ui-style-toggle").click()
            await page.wait_for_function("() => window.__styleSaved === true")
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "modern"
            )
            assert plugin.settings.ui_style == "modern"
            saved_json = json.loads(
                (plugin.data_root / "settings.json").read_text(encoding="utf-8")
            )
            assert saved_json["ui_style"] == "modern"
            assert not errors and get_peak() <= 2

        # 模拟旧配置 maiwork -> 打开页面与设置页自动归一化为 modern
        legacy_res = await plugin.manage_write(
            "ui_style", json.dumps({"ui_style": "maiwork"})
        )
        assert legacy_res["success"] and legacy_res["ui_style"] == "modern"

        async with _open_page(
            plugin, "now", viewport={"width": 1440, "height": 900}
        ) as (page, errors, rpc_log, get_peak):
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "modern"
            )
            await expect(page.locator("#now-threads")).to_contain_text("default 首条输入")
            await page.locator("#now-thread-search").fill("不存在的关键字__zero__")
            await page.wait_for_timeout(150)
            await expect(page.locator("#now-threads")).to_contain_text("没有匹配当前筛选的活动线程")
            await page.locator("#now-thread-search").fill("")
            await page.wait_for_timeout(150)
            assert not errors and get_peak() <= 2

        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "modern"
            )
            await expect(
                page.locator('#set-theme-seg [data-theme-choice="modern"]')
            ).to_have_attribute("aria-pressed", "true")
            await page.evaluate(
                "() => { window.__styleSaved = false; window.addEventListener('maisya:ui-style-saved', () => { window.__styleSaved = true; }, { once: true }); }"
            )
            await page.locator('#set-theme-seg [data-theme-choice="millennium"]').click()
            await page.wait_for_function("() => window.__styleSaved === true")
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "millennium"
            )
            saved_json = json.loads(
                (plugin.data_root / "settings.json").read_text(encoding="utf-8")
            )
            assert saved_json["ui_style"] == "millennium"
            assert not errors and get_peak() <= 2

        # Direct console_query __none__ backend check
        q_res = await plugin.console_query(thread_id="__none__")
        assert q_res["selected"] is None
        assert q_res["events"] == []
    finally:
        await plugin.on_unload()
