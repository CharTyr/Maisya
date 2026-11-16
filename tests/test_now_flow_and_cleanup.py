import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded


def _mark_delivered(plugin, thread_id: str, chat_id: str, revision: int = 1) -> None:
    with plugin.threads.connect() as db, db:
        db.execute(
            "INSERT OR REPLACE INTO result_deliveries VALUES (?, ?, ?, 'share', 'sent', 'msg_ok', ?)",
            (thread_id, revision, chat_id, time.time()),
        )


def _create_artifact_for_thread(plugin, thread_id: str, chat_id: str, name: str = "card.png") -> str:
    payload = b"fake-png-content-for-cleanup-test"
    handle = "att_" + hashlib.sha256(f"{thread_id}:{name}".encode()).hexdigest()[:24]
    fpath = plugin.runtime.files.artifacts / handle
    fpath.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    with plugin.threads.connect() as db, db:
        db.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, 1, ?, 'image', ?, ?, ?)",
            (handle, chat_id, thread_id, name, len(payload), digest, time.time()),
        )
    return handle


@pytest.mark.asyncio
async def test_thread_delete_and_prune_safety(tmp_path):
    plugin = await loaded(tmp_path)
    chat_id = "qq:group:100246809"
    try:
        plugin.settings.enabled_chats = [chat_id]

        p_scout = plugin.profiles.get("scout")
        p_maker = plugin.profiles.get("maker")

        th_active = plugin.threads.create(
            p_scout, "运行中活动", chat_id=chat_id, platform="qq", user_id="u1"
        )
        plugin.threads.activate(th_active["thread_id"])
        th_ws = plugin.threads.create(
            p_maker,
            "持久工作区活动",
            chat_id=chat_id,
            platform="qq",
            user_id="u1",
            workspace="ongoing_proj",
        )
        plugin.threads.activate(th_ws["thread_id"])
        rev_ws = plugin.threads.enqueue(th_ws["thread_id"], "工作区首轮输入", chat_id)
        plugin.threads.finish(
            th_ws["thread_id"], chat_id, {"text": "工作区首轮结果"}, [], rev_ws
        )
        plugin.threads.stop(th_ws["thread_id"], chat_id)

        th_att = plugin.threads.create(
            p_maker, "含附件记录", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_att = plugin.threads.enqueue(th_att["thread_id"], "含附件输入", chat_id)
        plugin.threads.finish(
            th_att["thread_id"], chat_id, {"text": "已产出附件"}, [], rev_att
        )
        plugin.threads.stop(th_att["thread_id"], chat_id)
        att_handle = _create_artifact_for_thread(plugin, th_att["thread_id"], chat_id)

        th_delivered = plugin.threads.create(
            p_scout, "已交付正常活动", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_deliv = plugin.threads.enqueue(
            th_delivered["thread_id"], "已交付输入", chat_id
        )
        plugin.threads.finish(
            th_delivered["thread_id"], chat_id, {"text": "正常交付正文"}, [], rev_deliv
        )
        _mark_delivered(plugin, th_delivered["thread_id"], chat_id)

        th_conclusion = plugin.threads.create(
            p_scout, "未交付但有结论", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_conc = plugin.threads.enqueue(
            th_conclusion["thread_id"], "有结论输入", chat_id
        )
        plugin.threads.finish(
            th_conclusion["thread_id"], chat_id, {"text": "保留结论"}, [], rev_conc
        )

        # 3 junk threads: stopped undelivered, errored idle, empty idle
        th_stopped = plugin.threads.create(
            p_scout, "中途停止的废活动", chat_id=chat_id, platform="qq", user_id="u1"
        )
        plugin.threads.stop(th_stopped["thread_id"], chat_id)

        th_err = plugin.threads.create(
            p_scout, "报错结束的废活动", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_err = plugin.threads.enqueue(th_err["thread_id"], "报错输入", chat_id)
        plugin.threads.finish(
            th_err["thread_id"], chat_id, {"error": "中断"}, [], rev_err
        )

        th_empty = plugin.threads.create(
            p_scout, "空结果废活动", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_empty = plugin.threads.enqueue(th_empty["thread_id"], "空结果输入", chat_id)
        plugin.threads.finish(th_empty["thread_id"], chat_id, {}, [], rev_empty)

        # Reject deleting active thread and persistent-workspace thread
        res_active = await plugin.manage_write(
            "delete_thread", json.dumps({"thread_id": th_active["thread_id"]})
        )
        assert res_active["success"] is False

        res_ws = await plugin.manage_write(
            "delete_thread", json.dumps({"thread_id": th_ws["thread_id"]})
        )
        assert res_ws["success"] is False

        # Prune should remove ONLY the 3 junk threads
        pruned = await plugin.manage_write("prune_threads", "{}")
        assert pruned["success"] is True
        assert pruned["count"] == 3
        assert set(pruned["deleted"]) == {
            th_stopped["thread_id"],
            th_err["thread_id"],
            th_empty["thread_id"],
        }

        with plugin.threads.connect() as db:
            remaining = {
                r["thread_id"]
                for r in db.execute("SELECT thread_id FROM agent_threads").fetchall()
            }
            assert remaining == {
                th_active["thread_id"],
                th_ws["thread_id"],
                th_att["thread_id"],
                th_delivered["thread_id"],
                th_conclusion["thread_id"],
            }
            for dead in pruned["deleted"]:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM thread_events WHERE thread_id=?", (dead,)
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM thread_search WHERE thread_id=?", (dead,)
                    ).fetchone()[0]
                    == 0
                )

        # Explicit delete removes th_att while keeping its artifact intact in outputs
        res_att = await plugin.manage_write(
            "delete_thread", json.dumps({"thread_id": th_att["thread_id"]})
        )
        assert res_att["success"] is True
        outputs = await plugin.manage_read("outputs", "{}")
        assert outputs["success"] is True
        assert any(item.get("handle") == att_handle for item in outputs["items"])
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_now_flow_role_filter_clean_top_and_cleanup_ui(tmp_path):
    plugin = await loaded(tmp_path)
    chat_id = "qq:group:100246809"
    try:
        plugin.settings.enabled_chats = [chat_id]
        await plugin.manage_write("onboarding_skip", "{}")
        cloned = await plugin.manage_write(
            "profile_clone",
            json.dumps(
                {
                    "source_id": "default",
                    "target_id": "custom_writer",
                    "name": "记录小助手",
                }
            ),
        )
        assert cloned["success"] is True, cloned

        p_default = plugin.profiles.get("default")
        p_custom = plugin.profiles.get("custom_writer")

        d1 = plugin.threads.create(
            p_custom, "今日游戏资讯简报 #1", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_d1 = plugin.threads.enqueue(d1["thread_id"], "今日游戏资讯简报 #1", chat_id)
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                d1["thread_id"],
                "tools",
                1,
                {
                    "calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "web_search",
                                "arguments": {"query": "今日新闻"},
                            },
                        }
                    ],
                    "results": [
                        {
                            "call_id": "call_1",
                            "value": {"text": "工具步骤正文：已检索完成"},
                        }
                    ],
                },
            )
        plugin.threads.finish(d1["thread_id"], chat_id, {"text": "简报 1 内容"}, [], rev_d1)
        _mark_delivered(plugin, d1["thread_id"], chat_id)

        d2 = plugin.threads.create(
            p_default, "今日游戏资讯简报 #2", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_d2 = plugin.threads.enqueue(d2["thread_id"], "今日游戏资讯简报 #2", chat_id)
        plugin.threads.finish(d2["thread_id"], chat_id, {"text": "简报 2 内容"}, [], rev_d2)
        _mark_delivered(plugin, d2["thread_id"], chat_id)

        j_stop = plugin.threads.create(
            p_default, "手动停止的旧尝试", chat_id=chat_id, platform="qq", user_id="u1"
        )
        plugin.threads.stop(j_stop["thread_id"], chat_id)

        j_err = plugin.threads.create(
            p_default, "出错的旧尝试", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_jerr = plugin.threads.enqueue(j_err["thread_id"], "出错的旧尝试", chat_id)
        plugin.threads.finish(
            j_err["thread_id"], chat_id, {"error": "模型未响应"}, [], rev_jerr
        )

        # 增加一条 watch 盯梢意图验证现场面板渲染
        watch_res = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": chat_id,
                    "kind": "return",
                    "reason": "盯梢新讨论",
                    "prompt": "关注群里提到版本发布的消息",
                    "in_seconds": 120,
                    "mode": "watch",
                    "condition": "出现版本发布关键词时触发",
                }
            ),
        )
        assert watch_res["success"] is True, watch_res

        requests, peak = 0, 0

        async def query(name, arguments):
            nonlocal requests, peak
            requests += 1
            peak = max(peak, requests)
            try:
                args = arguments or {}
                if name == "read":
                    return await plugin.manage_read(**args)
                if name == "event":
                    return await plugin.console_event(**args)
                return await plugin.console_query(**args)
            finally:
                requests -= 1

        async def action(name, arguments):
            nonlocal requests, peak
            requests += 1
            peak = max(peak, requests)
            try:
                args = arguments or {}
                if name == "write":
                    return await plugin.manage_write(**args)
                if name == "note":
                    return await plugin.console_send(**args)
                return await plugin.console_stop(**args)
            finally:
                requests -= 1

        raw_doc = Path("webui/now.html").read_text(encoding="utf-8")
        assert "localStorage" not in raw_doc
        assert "sessionStorage" not in raw_doc
        assert "transition: all" not in raw_doc
        csp_doc = raw_doc.replace(
            "<head>",
            '<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'; img-src data: blob:">',
        )

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                await page.expose_function("queryBackend", query)
                await page.expose_function("actionBackend", action)
                await page.evaluate(
                    """window.maibot={
                        ready:Promise.resolve(),
                        context:{theme:'light', host:{plugins:{}}},
                        page:{state:{}},
                        query:(name,args)=>window.queryBackend(name,args),
                        action:(name,args)=>window.actionBackend(name,args)
                    }"""
                )
                await page.set_content(csp_doc, wait_until="domcontentloaded")

                # v0.3 移除旧 01->04 FLOW 与 TREND，保留 Muse 步骤链与 Token 方块图
                assert await page.locator("#flow-panel").count() == 0
                assert await page.locator("#fl-threads").count() == 0
                assert await page.locator("img").count() == 0
                assert await page.locator("iframe").count() == 0

                # 左侧显示 4 条线程与 watch 盯梢意图
                await expect(page.locator("#now-threads [data-thread-id]")).to_have_count(4)
                await expect(page.locator("#now-intents")).to_contain_text("盯梢")

                # 线程状态筛选与搜索框
                await page.click('#now-thread-filters [data-th-filter="running"]')
                await expect(page.locator("#now-threads [data-thread-id]")).to_have_count(0)
                await page.click('#now-thread-filters [data-th-filter="history"]')
                await expect(page.locator("#now-threads [data-thread-id]")).to_have_count(4)
                await page.click('#now-thread-filters [data-th-filter="all"]')

                await page.fill("#now-thread-search", "custom_writer")
                await expect(page.locator("#now-threads [data-thread-id]")).to_have_count(1)
                await page.fill("#now-thread-search", "")
                await expect(page.locator("#now-threads [data-thread-id]")).to_have_count(4)

                # 点击 d1 验证 Muse 垂直步骤链与单步详情切面
                await page.click(f'#now-threads [data-thread-id="{d1["thread_id"]}"]')
                await page.wait_for_selector("#now-step-timeline [data-step-id]")
                await page.locator("#now-step-timeline [data-step-id]").filter(
                    has_text="web_search"
                ).first.click()
                await expect(page.locator("#now-view-step")).to_be_visible()
                await expect(page.locator("#now-step-output")).to_contain_text("工具步骤正文")
                await page.click("#now-back-overview")
                await expect(page.locator("#now-view-overview")).to_be_visible()

                # 触发清理废记录 + 删除单条后刷新现场页验证计数收敛
                await page.click(f'#now-threads [data-thread-id="{d2["thread_id"]}"]')
                pruned = await plugin.manage_write("prune_threads", "{}")
                assert pruned["success"] is True and pruned["count"] == 2
                await plugin.manage_write(
                    "delete_thread", json.dumps({"thread_id": d1["thread_id"]})
                )
                await page.click('[data-nav="abilities"]')
                await expect(page.locator("#pr-roster")).to_be_visible()
                await page.click('[data-nav="now"]')
                await expect(page.locator("#now-threads [data-thread-id]")).to_have_count(1)

                assert not errors
                assert peak <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
