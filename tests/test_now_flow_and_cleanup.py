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
        # Enable digest so FLOW 03 shows digest, maker, scout
        cur = plugin.memory.read("profile", target="digest", document="profile.toml")
        await plugin.manage_write(
            "toggle_profile",
            json.dumps(
                {"target": "digest", "enabled": True, "expected_version": cur["version"]}
            ),
        )

        p_digest = plugin.profiles.get("digest")

        d1 = plugin.threads.create(
            p_digest, "今日游戏资讯简报 #1", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_d1 = plugin.threads.enqueue(d1["thread_id"], "今日游戏资讯简报 #1", chat_id)
        plugin.threads.finish(d1["thread_id"], chat_id, {"text": "简报 1 内容"}, [], rev_d1)
        _mark_delivered(plugin, d1["thread_id"], chat_id)

        d2 = plugin.threads.create(
            p_digest, "今日游戏资讯简报 #2", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_d2 = plugin.threads.enqueue(d2["thread_id"], "今日游戏资讯简报 #2", chat_id)
        plugin.threads.finish(d2["thread_id"], chat_id, {"text": "简报 2 内容"}, [], rev_d2)
        _mark_delivered(plugin, d2["thread_id"], chat_id)

        j_stop = plugin.threads.create(
            p_digest, "手动停止的旧尝试", chat_id=chat_id, platform="qq", user_id="u1"
        )
        plugin.threads.stop(j_stop["thread_id"], chat_id)

        j_err = plugin.threads.create(
            p_digest, "出错的旧尝试", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_jerr = plugin.threads.enqueue(j_err["thread_id"], "出错的旧尝试", chat_id)
        plugin.threads.finish(
            j_err["thread_id"], chat_id, {"error": "模型未响应"}, [], rev_jerr
        )

        requests, peak = 0, 0

        async def query(name, arguments):
            nonlocal requests, peak
            requests += 1
            peak = max(peak, requests)
            try:
                if name == "read":
                    return await plugin.manage_read(**arguments)
                if name == "event":
                    return await plugin.console_event(**arguments)
                return await plugin.console_query(**arguments)
            finally:
                requests -= 1

        async def action(name, arguments):
            nonlocal requests, peak
            requests += 1
            peak = max(peak, requests)
            try:
                if name == "write":
                    return await plugin.manage_write(**arguments)
                if name == "note":
                    return await plugin.console_send(**arguments)
                return await plugin.console_stop(**arguments)
            finally:
                requests -= 1

        raw_doc = Path("webui/now.html").read_text(encoding="utf-8")
        csp_doc = raw_doc.replace(
            "<head>",
            "<head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src data: blob:\">",
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
                        context:{theme:'light'},
                        page:{state:{}},
                        query:(name,args)=>window.queryBackend(name,args),
                        action:(name,args)=>window.actionBackend(name,args)
                    }"""
                )
                await page.set_content(csp_doc)

                # Initial load: top FLOW 04 hides stopped/errored junk threads (shows only d1 & d2)
                await expect(page.locator("#fl-threads .fl-node[data-flow-thread]")).to_have_count(2)
                # Bottom activity list still shows all 4 + visible prune button for 2 junk threads
                await expect(page.locator("#threads [data-thread]")).to_have_count(4)
                await expect(page.locator("#prune-junk-threads")).to_be_visible()
                await expect(page.locator("#prune-junk-threads")).to_contain_text("2")

                # Click maker role in FLOW 03 (which has 0 threads)
                await page.locator('[data-flow-profile="maker"]').click()
                await expect(page.locator('[data-flow-profile="maker"]')).to_have_class(re.compile(r"\bon\b"))
                await expect(page.locator('[data-flow-profile="maker"]')).to_have_attribute("aria-pressed", "true")
                await expect(page.locator("#flow-filter-hint")).to_be_visible()
                await expect(page.locator("#flow-filter-hint")).to_contain_text("创作者")
                await expect(page.locator("#fl-threads [data-wake-profile='maker']")).to_be_visible()
                await expect(page.locator("#fl-threads [data-config-profile='maker']")).to_be_visible()
                await expect(page.locator("#threads [data-thread]")).to_have_count(0)

                # Click quick wake button
                await page.locator('[data-wake-profile="maker"]').click()
                await expect(page.locator("#launch-panel")).to_have_attribute("open", "")
                await expect(page.locator("#launch-profile")).to_have_value("maker")

                # Click quick config button -> jumps in-place to 能力 -> 角色管理 with maker selected
                await page.locator('[data-config-profile="maker"]').click()
                await expect(page.locator("h1")).to_have_text("能力")
                await expect(page.locator("#profiles")).to_be_visible()
                await expect(page.locator('#profiles [data-profile="maker"]')).to_have_attribute("aria-current", "true")

                # Switch back to 现场 and reset filters
                await page.locator('[data-nav="now"]').click()
                await expect(page.locator("h1")).to_have_text("现场")
                await page.locator("#flow-all-chats").click()
                await expect(page.locator("#fl-threads [data-flow-thread]")).to_have_count(2)
                await expect(page.locator("#threads [data-thread]")).to_have_count(4)

                # Click prune junk button
                await page.locator("#prune-junk-threads").click()
                await expect(page.locator("#prune-junk-threads")).to_be_hidden()
                await expect(page.locator("#threads [data-thread]")).to_have_count(2)
                await expect(page.locator("#feedback")).to_contain_text("已清理 2 条废记录")

                # Click inline delete button on one remaining thread
                await page.locator(f'[data-del-thread="{d1["thread_id"]}"]').click()
                await expect(page.locator("#threads [data-thread]")).to_have_count(1)
                await expect(page.locator("#feedback")).to_contain_text("记录已删除")

                assert not errors
                assert peak <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
