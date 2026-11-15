import asyncio
import base64
import json
import re
import time
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
    page_name: str,
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
                context: {{ theme: "light" }},
                page: {{ state: {{}} }},
                query: (name, args) => window.hostRpc("query", name, args),
                action: (name, args) => window.hostRpc("action", name, args),
                navigate: () => Promise.resolve(),
              }};
            }}"""
        )
        doc = _wrap_csp((WEBUI_DIR / f"{page_name}.html").read_text(encoding="utf-8"))
        await page.set_content(doc)
        try:
            yield page, errors, rpc_log, lambda: peak
        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
async def test_b2_settings_runtime_mobile_and_rail(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await page.locator('[data-settings-mode="advanced"]').click()
            rail_runtime = page.locator('[data-set-rail="runtime"]')
            await expect(rail_runtime).to_be_visible()
            await rail_runtime.click()
            await expect(rail_runtime).to_have_class(re.compile(r"\bis-active\b"))
            await expect(page.locator('[data-set-sec="runtime"]')).to_be_visible()
            await expect(page.locator("#set-steps")).to_be_visible()
            await expect(page.locator("#save-settings-adv")).to_be_visible()
            await expect(page.locator("#save-settings-adv")).to_be_enabled()

            await page.locator("#set-steps").fill("22")
            await page.locator("#save-settings-adv").click()
            await expect(page.locator("#feedback")).to_contain_text("已保存")
            assert plugin.settings.agent_max_steps == 22
            assert not errors and get_peak() <= 2

        async with _open_page(
            plugin, "settings", viewport={"width": 390, "height": 844}
        ) as (page, errors, _rpc_log, get_peak):
            mob_runtime = page.locator('#settings-mob-hub [data-mob-route="runtime"]')
            await expect(mob_runtime).to_be_visible()
            await expect(page.locator("#mob-sum-runtime")).to_be_visible()

            await mob_runtime.click()
            await expect(page.locator("#settings-mob-hub")).to_be_hidden()
            await expect(page.locator("#settings-mob-sub")).to_be_visible()
            await expect(page.locator("#set-steps")).to_be_visible()
            await expect(page.locator("#set-timeout")).to_be_visible()
            await expect(page.locator("#set-ctx")).to_be_visible()
            await expect(page.locator("#save-settings-adv")).to_be_visible()
            await expect(page.locator("#save-settings-adv")).to_be_enabled()

            await page.locator("#set-timeout").fill("240")
            await page.locator("#save-settings-adv").click()
            await expect(page.locator("#feedback")).to_contain_text("已保存")
            assert plugin.settings.agent_timeout == 240

            await page.locator("[data-mob-back]").click()
            await expect(page.locator("#mob-sum-runtime")).to_contain_text("240")
            assert not errors and get_peak() <= 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_outputs_binary_pagination_and_mobile_inspect(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    chunk1 = (b"\x89BIN1_START\x00\xff" + bytes(range(256)) * 768)[:196608]
    chunk2 = (b"\xfeBIN2_TAIL\x80\x01" + bytes(range(255, -1, -1)) * 220)[:53392]
    raw_bin = chunk1 + chunk2
    assert len(raw_bin) == 250000
    expected_b64 = base64.b64encode(raw_bin).decode("ascii")

    try:
        profile = plugin.profiles.get("scout")
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

        async with _open_page(
            plugin,
            "outputs",
            viewport={"width": 1440, "height": 900},
            reject_clipboard=True,
        ) as (page, errors, _rpc_log, get_peak):
            bin_item = page.locator("#out-list [data-out-idx]", has_text="archive.bin").first
            await expect(bin_item).to_be_visible()
            await bin_item.click()
            await expect(page.locator("#out-more-btn")).to_be_visible()

            await page.locator("#out-more-btn").click()
            await expect(page.locator("#out-more-btn")).to_be_hidden()
            await expect(page.locator("#out-stage")).to_contain_text("250000")

            await page.locator("#out-copy-btn").click()
            await expect(page.locator("#out-copy-drawer")).to_be_visible()
            copied_b64 = await page.locator("#out-copy-box").input_value()
            assert copied_b64 == expected_b64
            assert not errors and get_peak() <= 2

        async with _open_page(
            plugin, "outputs", viewport={"width": 390, "height": 844}
        ) as (page, errors, _rpc_log, get_peak):
            await page.locator('[data-out-space="workspaces"]').click()
            ws_btn = page.locator("#workspaces [data-ws]").first
            await expect(ws_btn).to_be_visible()
            await ws_btn.click()
            ws_file = page.locator('[data-ws-path="out/archive.bin"]')
            await expect(ws_file).to_be_visible()
            await ws_file.click()
            await expect(page.locator("#out-shell")).to_have_class(
                __import__("re").compile(r"is-mob-inspect")
            )
            await expect(page.locator("#out-inspector")).to_be_visible()

            await page.locator("#out-mob-back").click()
            await expect(page.locator("#out-shell")).not_to_have_class(
                __import__("re").compile(r"is-mob-inspect")
            )

            # Switch to conclusions so attachment handles are absent from outData.items,
            # forcing [data-open-att] through the else branch.
            await page.locator('[data-out-space="conclusions"]').click()
            conc_item = page.locator("#out-list [data-out-idx]").first
            await expect(conc_item).to_be_visible()
            await conc_item.click()
            att_chip = page.locator("[data-open-att]").first
            await expect(att_chip).to_be_visible()

            await page.evaluate(
                "() => document.getElementById('out-shell')?.classList.remove('is-mob-inspect')"
            )
            await att_chip.evaluate("el => el.click()")
            await expect(page.locator("#out-shell")).to_have_class(
                __import__("re").compile(r"is-mob-inspect")
            )
            await expect(page.locator("#out-inspector")).to_be_visible()
            assert not errors and get_peak() <= 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_hidden_shortcuts_and_recipe_include_profile(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        async with _open_page(
            plugin, "abilities", viewport={"width": 1440, "height": 900}
        ) as (page, errors, rpc_log, get_peak):
            await expect(page.locator("#skills-gallery-panel")).to_be_visible()
            assert not await page.locator("#save").is_visible()

            await page.keyboard.press("Control+s")
            await page.wait_for_timeout(120)
            assert not any(
                kind == "action"
                and name == "write"
                and args.get("action") == "asset"
                for kind, name, args in rpc_log
            )

            await page.locator("#nav-discovery").click()
            await expect(page.locator("#discovery-panel")).to_be_visible()
            assert not await page.locator("#save").is_visible()
            await page.keyboard.press("Control+s")
            await page.wait_for_timeout(120)
            assert not any(
                kind == "action"
                and name == "write"
                and args.get("action") == "asset"
                for kind, name, args in rpc_log
            )

            await page.locator("#nav-roles").click()
            await expect(page.locator("#save")).to_be_visible()
            old_val = await page.locator("#content").input_value()
            await page.locator("#content").fill(old_val + "\n\n快捷键测试保存")
            await page.keyboard.press("Control+s")
            await expect(page.locator("#feedback")).to_contain_text("已保存")
            assert any(
                kind == "action"
                and name == "write"
                and args.get("action") == "asset"
                for kind, name, args in rpc_log
            )

            # #rcp-inc-profile unchecked blocks recipe draft/export
            await page.locator("#rcp-draft-btn").click()
            await expect(page.locator("#rcp-draft-box")).to_be_visible()
            await expect(page.locator("#rcp-draft-box")).to_contain_text("隐私体检通过")

            await page.locator("#rcp-inc-profile").uncheck()
            await expect(page.locator("#rcp-draft-box")).to_contain_text(
                __import__("re").compile(r"PROFILE|档案正文|必选")
            )
            await page.locator("#export").click()
            await expect(page.locator("#feedback")).to_contain_text(
                __import__("re").compile(r"PROFILE|档案正文|必选")
            )

            await page.locator("#rcp-inc-profile").check()
            await expect(page.locator("#rcp-draft-box")).to_contain_text("隐私体检通过")
            assert not errors and get_peak() <= 2

        res = await plugin.manage_read(
            "recipe_package",
            json.dumps({"mode": "export", "target": "scout", "include_profile": False}),
        )
        assert res["success"] is False
        assert "PROFILE" in res["error"] or "档案" in res["error"]

        async with _open_page(
            plugin, "settings", viewport={"width": 390, "height": 844}
        ) as (page, errors, rpc_log, get_peak):
            await expect(page.locator("#settings-mob-hub")).to_be_visible()
            assert not await page.locator("#save-settings").is_visible()

            await page.keyboard.press("Control+s")
            await page.wait_for_timeout(120)
            assert not any(
                kind == "action"
                and name == "write"
                and args.get("action") == "settings"
                for kind, name, args in rpc_log
            )

            await page.locator('#settings-mob-hub [data-mob-route="general"]').click()
            await expect(page.locator("#save-settings")).to_be_visible()
            await expect(page.locator("#save-settings")).to_be_enabled()
            await page.keyboard.press("Control+s")
            await expect(page.locator("#feedback")).to_contain_text("已保存")
            assert any(
                kind == "action"
                and name == "write"
                and args.get("action") == "settings"
                for kind, name, args in rpc_log
            )
            assert not errors and get_peak() <= 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_services_drawer_rename_creds_settings_guard_and_batch(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        # 1. Service drawer MCP bare creds + atomic rename & kind switch
        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await page.locator('[data-settings-mode="advanced"]').click()
            await page.locator('[data-set-rail="services"]').click()
            await expect(page.locator("#service-cards")).to_contain_text("MCP 服务")
            await page.locator("#svc-new-btn").click()

            await page.locator("#svc-d-kind").select_option("mcp")
            await page.locator("#svc-d-name").fill("old_svc")
            await page.locator("#svc-d-cmd").fill("/usr/bin/npx -y @org/mcp")
            await page.locator("#svc-d-creds").fill("gh_token\npath")
            await page.locator("#svc-d-save").click()
            await expect(page.locator("#feedback")).to_contain_text("已保存服务 old_svc")

            svcs = plugin.runtime.services.load()
            assert svcs["servers"]["old_svc"]["credentials"] == {
                "GH_TOKEN": "gh_token",
                "SECRET_PATH": "path",
            }

            edit_btn = page.locator("#service-cards button", has_text="编辑").first
            await edit_btn.click()
            await page.locator("#svc-d-kind").select_option("template")
            await page.locator("#svc-d-name").fill("new_svc")
            await page.locator("#svc-d-targets").fill("93.184.216.34:443")
            await page.locator("#svc-d-save").click()
            await expect(page.locator("#feedback")).to_contain_text("已保存服务 new_svc")

            svcs_after = plugin.runtime.services.load()
            assert "old_svc" not in svcs_after["servers"]
            assert "new_svc" in svcs_after["templates"]
            assert svcs_after["templates"]["new_svc"]["secrets"] == ["gh_token", "path"]
            assert not errors and get_peak() <= 2

        # 2. Unhydrated settings guard prevents TypeError / overwriting config
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
            await expect(page.locator("#feedback")).to_contain_text("模拟设置读取故障")
            await expect(page.locator("#save-settings")).to_be_disabled()
            await expect(page.locator("#save-settings-streams")).to_be_disabled()
            await expect(page.locator("#save-settings-adv")).to_be_disabled()
            await page.locator("#save-settings").click(force=True)
            await page.wait_for_timeout(120)
            assert not any(
                kind == "action"
                and name == "write"
                and args.get("action") == "settings"
                for kind, name, args in rpc_log
            )
            assert not errors and get_peak() <= 2

        # 3. Backend batch partial failure isolation & UI explicit error rendering
        orig_load = plugin.runtime.services.load

        def broken_services_load():
            raise RuntimeError("boom-services-io")

        plugin.runtime.services.load = broken_services_load
        try:
            batch_res = await plugin.manage_read(
                "batch",
                json.dumps({"sections": {"settings": {}, "services": {}, "webhooks": {}}}),
            )
            assert batch_res["success"] is True
            assert batch_res["sections"]["settings"]["success"] is True
            assert batch_res["sections"]["webhooks"]["success"] is True
            assert batch_res["sections"]["services"]["success"] is False
            assert "boom-services-io" in batch_res["sections"]["services"]["error"]

            async with _open_page(
                plugin, "settings", viewport={"width": 1440, "height": 900}
            ) as (page, errors, _rpc_log, get_peak):
                await expect(page.locator("#daily_proactive_limit")).to_be_visible()
                await page.locator('[data-settings-mode="advanced"]').click()
                await expect(page.locator("#service-cards")).to_contain_text(
                    "boom-services-io"
                )
                assert not errors and get_peak() <= 2
        finally:
            plugin.runtime.services.load = orig_load

        # Memory batch partial failure renders explicit error on intents panel
        async def fail_intents_batch(kind, name, args):
            if kind == "query" and name == "read" and args.get("section") == "batch":
                real = await plugin.manage_read(**args)
                real["sections"]["intents"] = {
                    "success": False,
                    "error": "模拟计划队列加载失败",
                }
                return real
            return None

        async with _open_page(
            plugin,
            "memory",
            viewport={"width": 1440, "height": 900},
            on_rpc=fail_intents_batch,
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("#content")).to_be_enabled()
            await expect(page.locator("#intent-list")).to_contain_text(
                "模拟计划队列加载失败"
            )
            assert not errors and get_peak() <= 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_management_backend_p1_fixes(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809", "qq:private:2001"])
    try:
        # 1. undo_learning_note succeeds for user-cloned custom profile (allow_custom=True)
        plugin.profiles.clone("scout", "custom_scout")
        plugin.memory.seed_profile("custom_scout")
        plugin.memory.write(
            "notes",
            "自定义角色初版方法论。",
            target="custom_scout",
            author="user",
            allow_custom=True,
        )
        th = plugin.threads.create(
            plugin.profiles.get("custom_scout"),
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
            target="custom_scout",
            author=f"thread:{th['thread_id']}",
            allow_custom=True,
        )
        intents_res = await plugin.manage_read("intents", "{}")
        receipt = next(
            item
            for item in intents_res["learning"]
            if item.get("profile") == "custom_scout"
        )
        undo_res = await plugin.manage_write(
            "undo_learning_note",
            json.dumps(
                {
                    "profile": "custom_scout",
                    "version": v2["version"],
                    "at": receipt["at"],
                }
            ),
        )
        assert undo_res["success"] is True
        assert (
            "追加自动笔记"
            not in plugin.memory.read("notes", target="custom_scout")["content"]
        )

        # 2. _proactive_quota matches dual-arena bucketing, trigger-day & excludes 'failed'
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
                # Group today done -> counts toward group (=1)
                ("i_g_ok", "proactive", "longing", "chat:qq:group:100246809", today_mid, "test", "", "{}", "done", "", today_mid, today_mid),
                # Group today failed -> excluded
                ("i_g_fail", "proactive", "longing", "chat:qq:group:100246809", today_mid, "test", "", "{}", "failed", "", today_mid, today_mid),
                # Group yesterday -> excluded
                ("i_g_old", "proactive", "serendipity", "chat:qq:group:100246809", yest_mid, "test", "", "{}", "done", "", yest_mid, yest_mid),
                # Private today done -> counts toward private (=1)
                ("i_p_ok1", "proactive", "longing", "chat:qq:private:2001", today_mid, "test", "", "{}", "done", "", today_mid, today_mid),
                # Private created yesterday but triggered/handled today -> counts toward private (=2)
                ("i_p_ok2", "proactive", "afterglow", "chat:qq:private:2001", today_mid, "test", "", "{}", "done", "", yest_mid, today_mid),
            ]
            for row_vals in rows:
                db.execute(
                    "INSERT INTO intents(intent_id,kind,source,target,due_at,reason,thread_id,payload,status,wake_status,created,handled) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    row_vals,
                )

        s_res = await plugin.manage_read("settings", "{}")
        assert s_res["proactive_quota"] == {"used": 2, "limit": 3, "remaining": 1}
        assert plugin.intents.proactive_quota_available("qq:private:2001") is True

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
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        scout = plugin.profiles.get("scout")
        row = plugin.threads.create(
            scout,
            "探索主线仅属于 scout",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:1001",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(db, row["thread_id"], "input", 1, {"text": "scout 首条输入"})

        # 1. #ui-style-toggle persists across pages & reloads
        async with _open_page(
            plugin, "now", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "millennium"
            )
            await page.locator("#ui-style-toggle").click()
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "maiwork"
            )
            await page.wait_for_timeout(150)
            assert plugin.settings.ui_style == "maiwork"
            saved_json = json.loads(
                (plugin.data_root / "settings.json").read_text(encoding="utf-8")
            )
            assert saved_json["ui_style"] == "maiwork"
            assert not errors and get_peak() <= 2

        async with _open_page(
            plugin, "now", viewport={"width": 1440, "height": 900}
        ) as (page, errors, rpc_log, get_peak):
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "maiwork"
            )

            # 2. Empty-profile 3s poll suppression on now.html
            await expect(page.locator("#now-subline")).to_contain_text(
                "scout 首条输入"
            )
            rpc_log.clear()
            await page.locator('[data-flow-profile="maker"]').first.click()
            await expect(page.locator("#now-headline")).to_have_text("待命")
            await expect(page.locator("#now-subline")).not_to_contain_text(
                "scout 首条输入"
            )

            # Wait past 3s auto-poll cycle and confirm thread_id="__none__" suppresses fallback
            await page.wait_for_timeout(3600)
            poll_queries = [
                args
                for kind, name, args in rpc_log
                if kind == "query" and name == "snapshot"
            ]
            assert poll_queries, "expected periodic console poll after profile switch"
            assert all(q.get("thread_id") == "__none__" for q in poll_queries)
            await expect(page.locator("#now-headline")).to_have_text("待命")
            await expect(page.locator("#now-subline")).not_to_contain_text(
                "scout 首条输入"
            )
            await expect(page.locator("#events")).not_to_contain_text("scout 首条输入")
            assert not errors and get_peak() <= 2

        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "maiwork"
            )
            await expect(page.locator("#ui_style")).to_have_value("maiwork")
            await page.locator("#ui-style-toggle").click()
            await expect(page.locator("html")).to_have_attribute(
                "data-ui-style", "millennium"
            )
            await expect(page.locator("#ui_style")).to_have_value("millennium")
            await page.wait_for_timeout(150)
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
