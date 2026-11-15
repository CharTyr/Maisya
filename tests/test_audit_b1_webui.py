import json
import re
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded

WEBUI_DIR = Path(__file__).resolve().parents[1] / "webui"


@asynccontextmanager
async def _open_mgmt_page(plugin, page_name: str, *, viewport=None):
    errors = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport=viewport) if viewport else await browser.new_context()
        page = await context.new_page()
        page.on("pageerror", lambda err: errors.append(f"pageerror:{err}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console:{msg.text}")
            if msg.type == "error"
            else None,
        )

        async def request(mode, arguments):
            args = arguments or {}
            if mode == "read":
                return await plugin.manage_read(
                    args.get("section", ""),
                    args.get("payload", "{}"),
                )
            if mode == "write":
                return await plugin.manage_write(
                    args.get("action", ""),
                    args.get("payload", "{}"),
                )
            raise AssertionError(f"unexpected rpc mode: {mode}")

        await page.expose_function("backend", request)
        await page.add_init_script(
            "window.maibot={ready:Promise.resolve(),context:{theme:'light'},query:(_,args)=>window.backend('read',args),action:(_,args)=>window.backend('write',args),navigate:()=>Promise.resolve()}"
        )
        await page.goto((WEBUI_DIR / f"{page_name}.html").as_uri())
        try:
            yield page, errors
        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
async def test_audit_b1_undo_learning_receipts_on_memory_and_settings(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        plugin.memory.write("notes", "基线笔记：保留现有规则。", target="scout", author="user")
        t1 = plugin.threads.create(
            plugin.profiles.get("scout"),
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
            target="scout",
            author=f"thread:{t1['thread_id']}",
        )

        plugin.memory.write("notes", "创作者初稿规范", target="maker", author="user")
        t2 = plugin.threads.create(
            plugin.profiles.get("maker"),
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
            target="maker",
            author=f"thread:{t2['thread_id']}",
        )

        t3 = plugin.threads.create(
            plugin.profiles.get("scout"),
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
        assert rec1["profile"] == "scout"
        assert rec1["chat_id"] == "qq:group:100246809"
        assert "向量检索" in rec1["topic"]
        assert rec1["summary"]
        assert rec1["version"] == v2["version"]
        assert rec1["history_id"]
        assert rec1["at"]
        assert rec1["can_undo"] is True

        rec3 = by_tid[t3["thread_id"]]
        assert rec3["profile"] == "scout"
        assert rec3["can_undo"] is False

        async with _open_mgmt_page(plugin, "memory") as (page, errors):
            await page.locator('[data-kind-tab="heartbeat"]').click()
            undo_btn = page.locator('#learning [data-undo-learning="scout"]')
            await expect(undo_btn).to_be_visible()
            card_text = await page.locator("#learning").inner_text()
            assert "scout" in card_text
            assert "向量检索" in card_text

            await undo_btn.click()
            await expect(page.locator("#feedback")).to_contain_text("撤回")
            fb_class = await page.locator("#feedback").get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_class
            assert (
                plugin.memory.read("notes", target="scout")["content"]
                == "基线笔记：保留现有规则。"
            )
            with plugin.threads.connect() as db:
                row = db.execute(
                    "SELECT status FROM learning_receipts WHERE thread_id=?",
                    (t1["thread_id"],),
                ).fetchone()
            assert row["status"] == "reverted"
            await expect(undo_btn).to_have_count(0)
            assert "已撤回" in await page.locator("#learning").inner_text()
            assert errors == []

        async with _open_mgmt_page(plugin, "settings") as (page, errors):
            await page.locator('[data-settings-mode="advanced"]').click()
            undo_btn = page.locator('#learning [data-undo-learning="maker"]')
            await expect(undo_btn).to_be_visible()
            await undo_btn.click()
            await expect(page.locator("#feedback")).to_contain_text("撤回")
            fb_class = await page.locator("#feedback").get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_class
            assert (
                plugin.memory.read("notes", target="maker")["content"]
                == "创作者初稿规范"
            )
            with plugin.threads.connect() as db:
                row = db.execute(
                    "SELECT status FROM learning_receipts WHERE thread_id=?",
                    (t2["thread_id"],),
                ).fetchone()
            assert row["status"] == "reverted"
            await expect(undo_btn).to_have_count(0)
            assert errors == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_audit_b1_scheduled_admin_cancel_on_memory_and_settings(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        r1 = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": "qq:group:100246809",
                    "profile": "scout",
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
                    "profile": "maker",
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

        async with _open_mgmt_page(plugin, "memory") as (page, errors):
            await page.locator('[data-kind-tab="heartbeat"]').click()
            cancel_btn = page.locator(f'#intent-list [data-cancel-intent="{i1}"]')
            await expect(cancel_btn).to_be_visible()
            assert "受保护" in await page.locator("#intent-list").inner_text()
            await cancel_btn.click()
            await expect(page.locator("#feedback")).to_contain_text("取消")
            fb_class = await page.locator("#feedback").get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_class
            assert plugin.intents.get(i1)["status"] == "cancelled"
            assert errors == []

        async with _open_mgmt_page(plugin, "settings") as (page, errors):
            await page.locator('[data-settings-mode="advanced"]').click()
            cancel_btn = page.locator(f'#intent-list [data-cancel-intent="{i2}"]')
            await expect(cancel_btn).to_be_visible()
            assert "受保护" in await page.locator("#intent-list").inner_text()
            await cancel_btn.click()
            await expect(page.locator("#feedback")).to_contain_text("取消")
            fb_class = await page.locator("#feedback").get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_class
            assert plugin.intents.get(i2)["status"] == "cancelled"
            assert errors == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_audit_b1_webhook_quick_builder_desktop_and_mobile(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        plugin.vault.set("gh_hook_key", "super-secret-webhook-token-123")

        async with _open_mgmt_page(
            plugin, "settings", viewport={"width": 1280, "height": 900}
        ) as (page, errors):
            await page.locator('[data-settings-mode="advanced"]').click()
            await page.locator('[data-set-rail="webhooks"]').click()

            tag_name = await page.locator("#wh-b-profile").evaluate(
                "el => el.tagName"
            )
            assert tag_name == "SELECT"
            await expect(
                page.locator('#wh-b-profile option[value="maker"]')
            ).to_be_attached()
            prof_vals = await page.locator("#wh-b-profile option").evaluate_all(
                "opts => opts.map(o => o.value)"
            )
            assert "scout" in prof_vals and "maker" in prof_vals

            kind_vals = await page.locator("#wh-b-kind option").evaluate_all(
                "opts => opts.map(o => o.value)"
            )
            assert kind_vals == ["proactive", "silent"]

            assert (
                await page.locator("#wh-b-secret").get_attribute("required")
                is not None
            )
            secret_ph = (
                await page.locator("#wh-b-secret").get_attribute("placeholder") or ""
            )
            assert "可选" not in secret_ph

            await page.locator("#wh-b-name").fill("repo_push")
            await page.locator("#wh-b-target").fill("qq:group:100246809")
            await page.locator("#wh-b-secret").fill("")
            await page.locator("#wh-b-add").click()
            await expect(page.locator("#feedback")).to_have_class(
                re.compile(r"mil-feedback--err")
            )
            assert "repo_push" not in (
                await page.locator("#webhook-config").input_value()
            )

            await page.locator("#wh-b-secret").fill("bad handle!")
            await page.locator("#wh-b-add").click()
            await expect(page.locator("#feedback")).to_have_class(
                re.compile(r"mil-feedback--err")
            )
            assert "repo_push" not in (
                await page.locator("#webhook-config").input_value()
            )

            await page.locator("#wh-b-profile").select_option("maker")
            await page.locator("#wh-b-kind").select_option("silent")
            await page.locator("#wh-b-secret").fill("gh_hook_key")
            await page.locator("#wh-b-add").click()
            fb_class = await page.locator("#feedback").get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_class

            await page.locator("#save-webhooks").click()
            await expect(page.locator("#feedback")).to_contain_text("保存")
            fb_class = await page.locator("#feedback").get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_class
            saved_hooks = plugin.incoming.load()
            assert saved_hooks["repo_push"]["kind"] == "silent"
            assert saved_hooks["repo_push"]["profile"] == "maker"
            assert saved_hooks["repo_push"]["secret_handle"] == "gh_hook_key"
            assert saved_hooks["repo_push"]["target"] == "chat:qq:group:100246809"
            assert errors == []

        async with _open_mgmt_page(
            plugin, "settings", viewport={"width": 390, "height": 844}
        ) as (page, errors):
            await page.locator('[data-mob-route="webhooks"]').click()
            await page.locator("#mob-sub-act-add").click()
            await expect(page.locator("#settings-mob-scrim")).to_be_visible()
            await expect(
                page.locator('#wh-b-profile option[value="maker"]')
            ).to_be_attached()

            await page.locator("#wh-b-name").fill("ci_hook")
            await page.locator("#wh-b-target").fill("qq:group:100246809")
            await page.locator("#wh-b-profile").select_option("scout")
            await page.locator("#wh-b-kind").select_option("proactive")
            await page.locator("#wh-b-secret").fill("gh_hook_key")
            await page.locator("#wh-b-add").click()

            await expect(page.locator("#settings-mob-scrim")).to_be_hidden()
            cfg_json = json.loads(await page.locator("#webhook-config").input_value())
            assert cfg_json["ci_hook"]["kind"] == "proactive"
            assert cfg_json["ci_hook"]["secret_handle"] == "gh_hook_key"
            assert errors == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_audit_b1_cfg_task_default_both_save_paths(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        # 1) 后端 set_profile_models 应放行 fallback_slot="" 并从 profile.toml 移除 task 键
        res = await plugin.manage_write(
            "set_profile_models",
            json.dumps(
                {"profile_id": "maker", "models": [], "fallback_slot": ""}
            ),
        )
        assert res["success"], res
        maker_toml = tomllib.loads(
            (plugin.profiles.path / "maker" / "profile.toml").read_text(
                encoding="utf-8"
            )
        )
        assert "task" not in maker_toml

        # 重新给 maker 写入 task = "planner" 供界面路径 A 测试
        res2 = await plugin.manage_write(
            "set_profile_models",
            json.dumps(
                {"profile_id": "maker", "models": [], "fallback_slot": "planner"}
            ),
        )
        assert res2["success"], res2

        async with _open_mgmt_page(plugin, "abilities") as (page, errors):
            await page.locator("#nav-roles").click()

            # 路径 A：通过 #cfg-model-apply 保存 "跟随系统默认" (value="")
            await page.locator('#profiles [data-profile="maker"]').click()
            await page.locator('[data-doc-tab="config"]').click()
            await expect(page.locator("#cfg-task")).to_have_value("planner")
            await page.locator("#cfg-task").select_option("")
            await page.locator("#cfg-model-apply").click()
            await expect(page.locator("#feedback")).to_contain_text("应用")
            fb_class = await page.locator("#feedback").get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_class
            await expect(page.locator("#cfg-task")).to_have_value("")
            maker_after = tomllib.loads(
                (plugin.profiles.path / "maker" / "profile.toml").read_text(
                    encoding="utf-8"
                )
            )
            assert "task" not in maker_after

            # 路径 B：通过 #save 保存 "跟随系统默认" (value="")，不得强写 task = ""
            await page.locator('#profiles [data-profile="scout"]').click()
            await page.locator('[data-doc-tab="config"]').click()
            await expect(page.locator("#cfg-title")).to_have_value("探索者")
            await expect(page.locator("#cfg-task")).to_have_value("")

            await page.locator("#cfg-task").select_option("replyer")
            await expect(page.locator("#dirty-tag")).to_be_visible()
            await page.locator("#save").click()
            await expect(page.locator("#dirty-tag")).to_be_hidden()
            await expect(page.locator("#feedback")).to_contain_text("保存")
            assert (
                tomllib.loads(
                    (plugin.profiles.path / "scout" / "profile.toml").read_text(
                        encoding="utf-8"
                    )
                ).get("task")
                == "replyer"
            )

            await page.locator("#cfg-task").select_option("")
            await expect(page.locator("#dirty-tag")).to_be_visible()
            raw_toml = await page.locator("#content").input_value()
            assert 'task = ""' not in raw_toml
            await page.locator("#save").click()
            await expect(page.locator("#dirty-tag")).to_be_hidden()
            await expect(page.locator("#feedback")).to_contain_text("保存")
            fb_class = await page.locator("#feedback").get_attribute("class") or ""
            assert "mil-feedback--err" not in fb_class
            await expect(page.locator("#cfg-task")).to_have_value("")
            scout_after = tomllib.loads(
                (plugin.profiles.path / "scout" / "profile.toml").read_text(
                    encoding="utf-8"
                )
            )
            assert "task" not in scout_after
            assert errors == []
    finally:
        await plugin.on_unload()
