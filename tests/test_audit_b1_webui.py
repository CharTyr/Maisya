import json
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded

WEBUI_DIR = Path(__file__).resolve().parents[1] / "webui"

HOST_CSP_META = (
    "<head><meta http-equiv=\"Content-Security-Policy\" content=\""
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'none'; img-src data: blob:;\">"
)


def _wrap_csp(html: str) -> str:
    return html.replace("<head>", HOST_CSP_META, 1)


@asynccontextmanager
async def _open_spa(plugin, tab: str = "now", *, viewport=None):
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = (
            await browser.new_context(viewport=viewport)
            if viewport
            else await browser.new_context(viewport={"width": 1280, "height": 900})
        )
        page = await context.new_page()
        page.on("pageerror", lambda err: errors.append(f"pageerror:{err}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console:{msg.text}")
            if msg.type == "error"
            else None,
        )

        async def request(kind, name, arguments):
            args = dict(arguments or {})
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

        await page.expose_function("hostRpc", request)
        raw_doc = (WEBUI_DIR / "now.html").read_text(encoding="utf-8")
        assert "localStorage" not in raw_doc
        assert "sessionStorage" not in raw_doc
        assert "transition: all" not in raw_doc
        await page.evaluate(
            f"""() => {{
              window.__msPendingQueries = 0;
              window.maibot = {{
                ready: Promise.resolve(),
                context: {{ theme: 'light', host: {{ plugins: {{}} }} }},
                page: {{ state: {{ tab: {json.dumps(tab)} }} }},
                query: async (name, args) => {{
                  window.__msPendingQueries += 1;
                  try {{
                    return await window.hostRpc('query', name, args);
                  }} finally {{
                    window.__msPendingQueries -= 1;
                  }}
                }},
                action: (name, args) => window.hostRpc('action', name, args),
                navigate: () => Promise.resolve(),
              }};
            }}"""
        )
        await page.set_content(_wrap_csp(raw_doc), wait_until="domcontentloaded")
        try:
            yield page, errors
            assert await page.locator("img").count() == 0
            assert await page.locator("iframe").count() == 0
        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
async def test_audit_b1_undo_learning_receipts_on_memory_and_settings(tmp_path):
    plugin = await loaded(tmp_path)
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        for tid_name, label in (("custom_writer", "写作助手"), ("custom_maker", "制作助手")):
            cloned = await plugin.manage_write(
                "profile_clone",
                json.dumps({"source_id": "default", "target_id": tid_name, "name": label}),
            )
            assert cloned["success"], cloned

        plugin.memory.write("notes", "基线笔记：保留现有规则。", target="custom_writer", author="user")
        t1 = plugin.threads.create(
            plugin.profiles.get("custom_writer"),
            "向量检索阈值排查",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="u1",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                t1["thread_id"],
                "input",
                1,
                {"text": "请记住群友约定的向量检索阈值调整为 0.78"},
            )
        v2 = plugin.memory.write(
            "notes",
            "基线笔记：保留现有规则。\n- 向量检索阈值建议调为 0.78",
            target="custom_writer",
            author=f"thread:{t1['thread_id']}",
        )

        plugin.memory.write("notes", "创作者初稿规范", target="custom_maker", author="user")
        t2 = plugin.threads.create(
            plugin.profiles.get("custom_maker"),
            "图表输出规范沉淀",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="u1",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                t2["thread_id"],
                "input",
                1,
                {"text": "沉淀画图输出格式规范"},
            )
        plugin.memory.write(
            "notes",
            "创作者初稿规范\n- 图表默认使用矢量 SVG",
            target="custom_maker",
            author=f"thread:{t2['thread_id']}",
        )

        t3 = plugin.threads.create(
            plugin.profiles.get("default"),
            "无新增结论的检索",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="u1",
        )

        with plugin.threads.connect() as db, db:
            db.execute(
                "INSERT INTO learning_receipts (thread_id, revision, status) VALUES (?, 1, 'applied')",
                (t1["thread_id"],),
            )
            db.execute(
                "INSERT INTO learning_receipts (thread_id, revision, status) VALUES (?, 1, 'applied')",
                (t2["thread_id"],),
            )
            db.execute(
                "INSERT INTO learning_receipts (thread_id, revision, status) VALUES (?, 1, 'empty')",
                (t3["thread_id"],),
            )

        intents_res = await plugin.manage_read("intents")
        assert intents_res["success"], intents_res
        by_tid = {item["thread_id"]: item for item in intents_res["learning"]}
        rec1 = by_tid[t1["thread_id"]]
        assert rec1["profile"] == "custom_writer"
        assert rec1["chat_id"] == "qq:group:100246809"
        assert "向量检索" in rec1["topic"]
        assert rec1["summary"]
        assert rec1["version"] == v2["version"]
        assert rec1["history_id"]
        assert rec1["at"]
        assert rec1["can_undo"] is True

        rec3 = by_tid[t3["thread_id"]]
        assert rec3["profile"] == "default"
        assert rec3["can_undo"] is False

        # 撤回学习笔记写回并更新回执状态
        undo1 = await plugin.manage_write(
            "undo_learning_note",
            json.dumps({"profile": "custom_writer", "history_id": rec1["history_id"]}),
        )
        assert undo1["success"], undo1
        assert (
            plugin.memory.read("notes", target="custom_writer")["content"]
            == "基线笔记：保留现有规则。"
        )
        undo2 = await plugin.manage_write(
            "undo_learning_note", json.dumps({"profile": "custom_maker"})
        )
        assert undo2["success"], undo2
        assert (
            plugin.memory.read("notes", target="custom_maker")["content"]
            == "创作者初稿规范"
        )
        with plugin.threads.connect() as db:
            rows = db.execute(
                "SELECT thread_id, status FROM learning_receipts WHERE thread_id IN (?, ?)",
                (t1["thread_id"], t2["thread_id"]),
            ).fetchall()
        assert {r["status"] for r in rows} == {"reverted"}

        async with _open_spa(plugin, "profiles") as (page, errors):
            await expect(page.locator("#ms-page-title")).to_have_text("能力")
            await expect(
                page.locator('#pr-roster [data-pick-profile-id="custom_writer"]')
            ).to_be_visible()
            await expect(
                page.locator('#pr-roster [data-pick-profile-id="custom_maker"]')
            ).to_be_visible()
            await page.locator('[data-nav="library"]').click()
            await expect(page.locator("#ms-page-title")).to_have_text("产物")
            assert errors == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_audit_b1_scheduled_admin_cancel_on_memory_and_settings(tmp_path):
    plugin = await loaded(tmp_path)
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        cloned = await plugin.manage_write(
            "profile_clone",
            json.dumps({"source_id": "default", "target_id": "custom_maker", "name": "制作助手"}),
        )
        assert cloned["success"], cloned

        r1 = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": "qq:group:100246809",
                    "profile": "default",
                    "in_seconds": 3600,
                    "every_seconds": 86400,
                    "prompt": "每日例行巡检群消息",
                }
            ),
        )
        assert r1["success"], r1
        r2 = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": "qq:group:100246809",
                    "profile": "custom_maker",
                    "in_seconds": 7200,
                    "every_seconds": 604800,
                    "prompt": "每周项目周报汇总",
                }
            ),
        )
        assert r2["success"], r2
        all_intents = (await plugin.manage_read("intents"))["intents"]
        i1 = next(
            it["intent_id"] for it in all_intents if "每日例行巡检群消息" in it["reason"]
        )
        i2 = next(
            it["intent_id"] for it in all_intents if "每周项目周报汇总" in it["reason"]
        )

        # 桌面端 1280x900 现场页取消第一项管理员定时计划
        async with _open_spa(
            plugin, "now", viewport={"width": 1280, "height": 900}
        ) as (page, errors):
            cancel_btn = page.locator(
                f'#now-intents [data-intent-act="intent_delete"][data-intent-id="{i1}"]'
            )
            await expect(cancel_btn).to_be_visible()
            await expect(page.locator("#now-intents")).to_contain_text("每日例行巡检群消息")
            await cancel_btn.click()
            await expect(page.locator("#ms-confirm-dialog")).to_be_visible()
            await page.locator("#ms-confirm-ok").click()
            await expect(page.locator("#ms-confirm-dialog")).to_be_hidden()
            await expect(page.locator('[data-feedback-slot="now"]')).to_contain_text("状态已更新")
            fb_cls = await page.locator('[data-feedback-slot="now"]').get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_cls
            assert plugin.intents.get(i1)["status"] == "cancelled"
            assert errors == []

        # 移动端 390x844 现场页取消第二项管理员定时计划
        async with _open_spa(
            plugin, "now", viewport={"width": 390, "height": 844}
        ) as (page, errors):
            await expect(page.locator("#ms-mob-dock")).to_be_visible()
            assert await page.locator("#ms-mob-dock [data-mob-nav]").count() == 5
            cancel_btn2 = page.locator(
                f'#now-intents [data-intent-act="intent_delete"][data-intent-id="{i2}"]'
            )
            await expect(cancel_btn2).to_be_visible()
            await cancel_btn2.click()
            await expect(page.locator("#ms-confirm-dialog")).to_be_visible()
            await page.locator("#ms-confirm-ok").click()
            await expect(page.locator("#ms-confirm-dialog")).to_be_hidden()
            await expect(page.locator('[data-feedback-slot="now"]')).to_contain_text("状态已更新")
            assert plugin.intents.get(i2)["status"] == "cancelled"
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            assert errors == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_audit_b1_settings_and_vault_controls_desktop_and_mobile(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        plugin.vault.set("gh_hook_key", "super-secret-webhook-token-123")

        async with _open_spa(
            plugin, "settings", viewport={"width": 1280, "height": 900}
        ) as (page, errors):
            await expect(page.locator("#ms-page-title")).to_have_text("设置")
            await expect(page.locator("#set-vault-list")).to_contain_text("gh_hook_key")
            assert "super-secret-webhook-token-123" not in await page.content()

            # 1) 非法免打扰时段应报错且不落盘
            await page.locator("#set-sleep-start").fill("99:99")
            await page.locator("#set-save-runtime").click()
            await expect(page.locator("#set-feedback")).to_have_class(
                __import__("re").compile(r"mil-feedback--err")
            )
            assert plugin.settings.sleep_window != "99:99-08:00"

            # 2) 空凭据句柄保存 Vault 应报错
            await page.locator("#set-vault-handle").fill("")
            await page.locator("#set-vault-secret").fill("TOKEN=secret-abc")
            await page.locator("#set-vault-save").click()
            await expect(page.locator("#set-feedback")).to_have_class(
                __import__("re").compile(r"mil-feedback--err")
            )

            # 3) 合法保存运行时设置与新 Vault 凭据
            await page.locator("#set-daily-limit").fill("4")
            await page.locator("#set-sleep-start").fill("00:30")
            await page.locator("#set-sleep-end").fill("07:30")
            await page.locator("#set-save-runtime").click()
            await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
            assert plugin.settings.daily_proactive_limit == 4
            assert plugin.settings.sleep_window == "00:30-07:30"

            await page.locator("#set-vault-handle").fill("ci_deploy_token")
            await page.locator("#set-vault-secret").fill("TOKEN=secret-xyz-999")
            await page.locator("#set-vault-save").click()
            await expect(page.locator("#set-feedback")).to_contain_text("ci_deploy_token")
            await expect(page.locator("#set-vault-secret")).to_have_value("")
            assert "ci_deploy_token" in plugin.vault.handles()
            assert "secret-xyz-999" not in await page.content()
            assert errors == []

        async with _open_spa(
            plugin, "settings", viewport={"width": 390, "height": 844}
        ) as (page, errors):
            await expect(page.locator("#ms-mob-dock")).to_be_visible()
            assert await page.locator("#ms-mob-dock [data-mob-nav]").count() == 5
            for block in (
                "#set-block-chats",
                "#set-block-auth",
                "#set-block-vault",
                "#set-block-runtime",
            ):
                await expect(page.locator(block)).to_be_visible()
            await page.locator("#set-chat-plat").select_option("qq")
            await page.locator("#set-chat-type").select_option("group")
            await page.locator("#set-chat-id").fill("100246809")
            await page.locator("#set-chat-add").click()
            await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
            assert "qq:group:100246809" in plugin.settings.enabled_chats
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            assert errors == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_audit_b1_cfg_task_default_both_save_paths(tmp_path):
    cfg_dir = tmp_path / "host/config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "model_config.toml").write_text(
        """
[[models]]
name = "demo-planner"
model_identifier = "demo/planner"

[[models]]
name = "demo-replyer"
model_identifier = "demo/replyer"
""",
        encoding="utf-8",
    )
    plugin = await loaded(tmp_path)
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        cloned = await plugin.manage_write(
            "profile_clone",
            json.dumps({"source_id": "default", "target_id": "custom_maker", "name": "制作助手"}),
        )
        assert cloned["success"], cloned

        # 1) 后端 set_profile_models 应放行 fallback_slot="" 并从 profile.toml 移除 task 键
        res = await plugin.manage_write(
            "set_profile_models",
            json.dumps(
                {"profile_id": "custom_maker", "models": [], "fallback_slot": ""}
            ),
        )
        assert res["success"], res
        maker_toml = tomllib.loads(
            (plugin.profiles.path / "custom_maker" / "profile.toml").read_text(
                encoding="utf-8"
            )
        )
        assert "task" not in maker_toml

        res2 = await plugin.manage_write(
            "set_profile_models",
            json.dumps(
                {"profile_id": "custom_maker", "models": [], "fallback_slot": "planner"}
            ),
        )
        assert res2["success"], res2
        maker_with_task = tomllib.loads(
            (plugin.profiles.path / "custom_maker" / "profile.toml").read_text(
                encoding="utf-8"
            )
        )
        assert maker_with_task.get("task") == "planner"

        res3 = await plugin.manage_write(
            "set_profile_models",
            json.dumps(
                {"profile_id": "custom_maker", "models": [], "fallback_slot": ""}
            ),
        )
        assert res3["success"], res3
        assert "task" not in tomllib.loads(
            (plugin.profiles.path / "custom_maker" / "profile.toml").read_text(
                encoding="utf-8"
            )
        )

        # 2) v0.3 档案页 #pr-model 切换与跟随默认模型保存
        async def _wait_reads_settled(page) -> None:
            await page.wait_for_function(
                "() => (window.__msPendingQueries || 0) === 0",
                timeout=5000,
            )
            await page.evaluate("() => new Promise(r => setTimeout(r, 0))")
            await page.wait_for_function(
                "() => (window.__msPendingQueries || 0) === 0",
                timeout=5000,
            )

        async with _open_spa(plugin, "profiles") as (page, errors):
            await _wait_reads_settled(page)
            await page.locator('#pr-roster [data-pick-profile-id="custom_maker"]').click()
            await _wait_reads_settled(page)
            await page.locator("#pr-adv-toggle").click()
            await expect(page.locator("#pr-adv-body")).to_be_visible()
            await page.locator("#pr-model-add-btn").click()
            await expect(page.locator("#pr-model-groups")).to_contain_text("demo-planner")
            await page.locator('#pr-model-groups [data-pick-model="demo-planner"]').click()
            await page.evaluate("document.getElementById('pr-feedback').textContent = ''")
            await page.locator("#pr-btn-save").click()
            await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
            await _wait_reads_settled(page)
            assert plugin.profiles.get("custom_maker").model_name == "demo-planner"

            await page.locator('[data-pr-model-del="demo-planner"]').click()
            await page.evaluate("document.getElementById('pr-feedback').textContent = ''")
            await page.locator("#pr-btn-save").click()
            await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
            await _wait_reads_settled(page)
            assert plugin.profiles.get("custom_maker").model_name == ""

            # 默认档案同样可通过高级切换模型并保存
            await page.locator('#pr-roster [data-pick-profile-id="default"]').click()
            await _wait_reads_settled(page)
            await expect(page.locator("#pr-btn-delete")).to_be_disabled()
            await page.locator("#pr-model-add-btn").click()
            await page.locator('#pr-model-groups [data-pick-model="demo-replyer"]').click()
            await page.evaluate("document.getElementById('pr-feedback').textContent = ''")
            await page.locator("#pr-btn-save").click()
            await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
            await _wait_reads_settled(page)
            assert plugin.profiles.get("default").model_name == "demo-replyer"
            await page.locator('[data-pr-model-del="demo-replyer"]').click()
            await page.evaluate("document.getElementById('pr-feedback').textContent = ''")
            await page.locator("#pr-btn-save").click()
            await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
            await _wait_reads_settled(page)
            assert plugin.profiles.get("default").model_name == ""
            assert errors == []
    finally:
        await plugin.on_unload()
