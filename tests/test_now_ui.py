import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded


@pytest.mark.asyncio
async def test_now_page_real_backend_actions_drafts_theme_and_two_slot_budget(tmp_path):
    plugin = await loaded(tmp_path)

    async def capability(name, **arguments):
        assert name == "llm.generate_with_tools"
        return {
            "success": True,
            "response": "已核实的结果 <script>window.pwned=true</script>",
            "tool_calls": [],
        }

    plugin.ctx.call_capability = capability
    first = await plugin.agent_tool(
        action="start",
        message="探索 <img src=x onerror=window.pwned=true>",
        chat_id="chat",
        platform="qq",
    )
    second = await plugin.agent_tool(
        action="start", message="另一个探索", chat_id="chat", platform="qq"
    )
    with plugin.threads.connect() as db, db:
        plugin.threads.event(
            db,
            first["thread_id"],
            "tools",
            1,
            {"text": "工具细节 " * 1500 + "原始结尾"},
        )
    gate, entered = asyncio.Event(), asyncio.Event()
    blocking = False
    requests, queries, peak, query_peak = 0, 0, 0, 0

    async def query(name, arguments):
        nonlocal requests, queries, peak, query_peak
        requests += 1
        queries += int(name == "snapshot")
        peak = max(peak, requests)
        query_peak = max(query_peak, queries)
        try:
            if blocking and name == "snapshot":
                entered.set()
                await gate.wait()
            return await (
                plugin.console_event(**arguments)
                if name == "event"
                else plugin.console_query(**arguments)
            )
        finally:
            requests -= 1
            queries -= int(name == "snapshot")

    async def action(name, arguments):
        nonlocal requests, peak
        requests += 1
        peak = max(peak, requests)
        try:
            return await (
                plugin.console_send(**arguments)
                if name == "note"
                else plugin.console_stop(**arguments)
            )
        finally:
            requests -= 1

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1200, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.expose_function("queryBackend", query)
                await page.expose_function("actionBackend", action)
                await page.evaluate(
                    """window.maibot={ready:Promise.resolve(),context:{theme:'light'},query:(name,args)=>window.queryBackend(name,args),action:(name,args)=>window.actionBackend(name,args)}"""
                )
                document = Path("webui/now.html").read_text()
                document = document.replace(
                    "<head>",
                    "<head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src data: blob:\">",
                )
                await page.set_content(document)
                await expect(page.locator("h2")).to_have_text("另一个探索")
                # 验证 Aisya 上下文方块 / 方格视图与弹窗阅读
                await expect(page.locator(".cm-tile").first).to_be_visible()
                await page.locator('[data-ci-idx]').first.click()
                await expect(page.locator("#ctx-scrim")).to_be_visible()
                await expect(page.locator("#ctx-modal-body")).not_to_be_empty()
                await page.locator("#ctx-modal-close").click()
                await expect(page.locator("#ctx-scrim")).to_be_hidden()
                await page.locator('[data-ctx-view="grid"]').click()
                assert await page.locator(".cm-cell").count() == 400
                await page.locator('[data-ctx-view="map"]').click()
                # 验证流向图会话筛选与恢复
                await page.locator('[data-flow-chat="chat"]').click()
                await expect(page.locator("#flow-filter-hint")).to_contain_text("chat")
                await page.locator("#flow-all-chats").click()
                await page.locator(f'[data-thread="{first["thread_id"]}"]').click()
                await expect(page.locator("h2")).to_contain_text("探索 <img")
                await page.locator("#message").fill("只属于第一个线程的草稿")
                await page.locator(f'[data-thread="{second["thread_id"]}"]').click()
                await expect(page.locator("#message")).to_have_value("")
                await page.locator(f'[data-thread="{first["thread_id"]}"]').click()
                await expect(page.locator("#message")).to_have_value(
                    "只属于第一个线程的草稿"
                )
                await expect(page.locator("h2")).to_contain_text("探索 <img")
                assert await page.locator("img").count() == 0
                assert not await page.evaluate("Boolean(window.pwned)")
                large = page.locator("details").filter(
                    has=page.get_by_role(
                        "button", name="读取完整步骤", include_hidden=True
                    )
                )
                event_seq = await large.get_attribute("data-event")
                large = page.locator(f'details[data-event="{event_seq}"]')
                await large.locator("summary").click()
                await large.get_by_role("button", name="读取完整步骤").click()
                await large.get_by_role("button", name="继续读取").click()
                await expect(large.locator("pre")).to_contain_text("原始结尾")
                await large.locator("summary").click()
                blocking = True
                await asyncio.wait_for(entered.wait(), 5)
                await page.get_by_role("button", name="递纸条", exact=True).click()
                await expect(page.locator("#feedback")).to_have_text(
                    "纸条已递入原线程。"
                )
                assert peak == 2 and query_peak == 1
                blocking = False
                gate.set()
                await plugin.engine.drain()
                assert "只属于第一个线程的草稿" in str(
                    plugin.threads.get(first["thread_id"], "chat")["history"]
                )
                await expect(page.locator("#message")).to_have_value("")
                await page.get_by_role("button", name="停止线程", exact=True).click()
                await expect(page.locator("#feedback")).to_have_text("线程已停止。")
                await expect(page.locator("#stop")).to_be_disabled()
                await page.locator("#launch-profile").fill("scout")
                await page.locator("#launch-chat").fill("chat")
                await page.locator("#launch-prompt").fill("从现场直接发起的新工作流")
                await page.locator("#launch-run").click()
                await expect(page.locator("#launch-feedback")).to_have_text(
                    "工作流线程已发起。"
                )
                await expect(page.locator("h2")).to_contain_text(
                    "从现场直接发起的新工作流"
                )
                await plugin.engine.drain()
                assert not errors
                destination = Path(".local-checks")
                destination.mkdir(exist_ok=True)
                await page.screenshot(path=str(destination / "now-desktop.png"))
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.evaluate(
                    "window.dispatchEvent(new CustomEvent('maibot:context',{detail:{theme:'dark'}}))"
                )
                assert await page.locator("html").get_attribute("class") == "dark"
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                await page.screenshot(
                    path=str(destination / "now-mobile.png"), full_page=True
                )
            finally:
                await browser.close()
    finally:
        gate.set()
        await plugin.on_unload()
