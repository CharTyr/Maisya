import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded


@pytest.mark.asyncio
async def test_now_page_real_backend_actions_drafts_theme_and_two_slot_budget(tmp_path):
    plugin = await loaded(tmp_path)
    await plugin.manage_write("onboarding_skip", "{}")

    cap_hold = False
    cap_entered, cap_gate = asyncio.Event(), asyncio.Event()

    async def capability(name, **arguments):
        assert name == "llm.generate_with_tools"
        if cap_hold:
            cap_entered.set()
            await cap_gate.wait()
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
            {
                "calls": [
                    {
                        "id": "call_treemap",
                        "function": {
                            "name": "web_search",
                            "arguments": {"query": "treemap"},
                        },
                    }
                ],
                "results": [
                    {
                        "call_id": "call_treemap",
                        "value": {"text": "工具细节 " * 300 + "原始结尾"},
                    }
                ],
            },
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
            args = arguments or {}
            if blocking and name == "snapshot":
                entered.set()
                await gate.wait()
            if name == "read":
                return await plugin.manage_read(**args)
            if name == "event":
                return await plugin.console_event(**args)
            return await plugin.console_query(**args)
        finally:
            requests -= 1
            queries -= int(name == "snapshot")

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
                    """window.maibot={ready:Promise.resolve(),context:{theme:'light', host:{plugins:{}}},query:(name,args)=>window.queryBackend(name,args),action:(name,args)=>window.actionBackend(name,args)}"""
                )
                document = Path("webui/now.html").read_text(encoding="utf-8")
                assert "localStorage" not in document
                assert "sessionStorage" not in document
                assert "transition: all" not in document
                document = document.replace(
                    "<head>",
                    '<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'; img-src data: blob:">',
                )
                await page.set_content(document, wait_until="domcontentloaded")
                await expect(page.locator("#now-sel-title")).to_have_text("另一个探索")
                await expect(
                    page.locator(f'#now-threads [data-thread-id="{second["thread_id"]}"]')
                ).to_contain_text("另一个探索")

                # Token Treemap 方块 / 400 格方格与弹窗阅读
                await expect(page.locator(".cm-tile").first).to_be_visible()
                await page.locator("[data-ci-idx]").first.click()
                await expect(page.locator("#ctx-scrim")).to_be_visible()
                await expect(page.locator("#ctx-modal-body")).not_to_be_empty()
                await page.locator("#ctx-modal-close").click()
                await expect(page.locator("#ctx-scrim")).to_be_hidden()
                await page.locator('[data-ctx-view="grid"]').click()
                assert await page.locator(".cm-cell").count() == 400
                await page.locator('[data-ctx-view="map"]').click()

                # 切换至第一个线程并校验 XSS 转义、Muse 步骤链与单步详情
                await page.locator(f'#now-threads [data-thread-id="{first["thread_id"]}"]').click()
                await expect(page.locator("#now-sel-title")).to_contain_text("探索 <img")
                await expect(
                    page.locator(f'#now-threads [data-thread-id="{first["thread_id"]}"]')
                ).to_contain_text("探索 <img")
                assert await page.locator("img").count() == 0
                assert await page.locator("iframe").count() == 0
                assert not await page.evaluate("Boolean(window.pwned)")

                step_btn = page.locator("#now-step-timeline [data-step-id]").filter(
                    has_text="web_search"
                )
                await expect(step_btn.first).to_be_visible()
                await step_btn.first.click()
                await expect(page.locator("#now-view-step")).to_be_visible()
                await expect(page.locator("#now-step-output")).to_contain_text("工具细节")
                await expect(page.locator("#now-step-output")).to_contain_text("原始结尾")
                await page.locator("#now-back-overview").click()
                await expect(page.locator("#now-view-overview")).to_be_visible()

                # 二并发配额隔离下发送补充说明，并验证新发起线程可执行停止与恢复
                await page.locator("#now-note-input").fill("只属于第一个线程的引导")
                blocking = True
                await asyncio.wait_for(entered.wait(), 5)
                await page.locator("#now-note-send").click()
                await expect(page.locator('#feedback[data-feedback-slot="now"]')).to_have_text(
                    "补充说明已送入线程"
                )
                assert peak == 2 and query_peak == 1
                blocking = False
                gate.set()
                await plugin.engine.drain()
                assert "只属于第一个线程的引导" in str(
                    plugin.threads.get(first["thread_id"], "chat")["history"]
                )
                await expect(page.locator("#now-note-input")).to_have_value("")

                # 从现场富选项框直接发起新线程并在运行中停止，随后直接发补充说明自动唤醒（无多余「恢复继续」按钮）
                cap_hold = True
                await page.locator("#launch-wrap > summary").click()
                await page.locator("#launch-profile-trigger").click()
                await page.locator('[data-launch-pick-profile="default"]').click()
                await page.locator("#launch-chat-trigger").click()
                await page.locator('[data-launch-pick-chat="chat"]').click()
                await page.locator("#launch-prompt").fill("从现场直接发起的新工作流")
                await page.locator("#launch-run").click()
                await expect(page.locator("#launch-feedback")).to_have_text("已提交发起")
                await asyncio.wait_for(cap_entered.wait(), 5)
                launched_card = page.locator("#now-threads [data-thread-id]").filter(
                    has_text="从现场直接发起的新工作流"
                ).first
                launched_tid = await launched_card.get_attribute("data-thread-id")
                await launched_card.click()
                await expect(page.locator("#now-stop-btn")).to_be_visible()
                await page.locator("#now-stop-btn").click()
                await expect(page.locator('#feedback[data-feedback-slot="now"]')).to_contain_text(
                    "已请求停止"
                )
                assert await page.locator("#now-reopen-btn").count() == 0
                assert await page.locator("[data-thread-reopen]").count() == 0
                cap_hold = False
                cap_gate.set()
                await plugin.engine.drain()
                await page.locator("#now-note-input").fill("已停止线程直接发消息自动继续")
                await page.locator("#now-note-send").click()
                await expect(page.locator('#feedback[data-feedback-slot="now"]')).to_have_text(
                    "补充说明已送入线程"
                )
                await plugin.engine.drain()
                assert "已停止线程直接发消息自动继续" in str(
                    plugin.threads.get(launched_tid, "chat")["history"]
                )

                # v0.3 不再存在旧 01->04 FLOW 或页面副标题，帮助气泡可键盘关闭
                assert not errors
                assert await page.locator(".ms-page-sub").count() == 0
                assert await page.locator("#flow-panel").count() == 0
                assert await page.locator("#fl-threads").count() == 0
                await page.locator("#launch-wrap .mil-help").first.click()
                await expect(page.locator("#mil-help-pop")).to_be_visible()
                await page.keyboard.press("Escape")
                await expect(page.locator("#mil-help-pop")).to_be_hidden()

                # 主题切换：千禧机械 -> 现代极简 -> 自动 -> 千禧机械
                assert await page.locator("html").get_attribute("data-ui-style") == "millennium"
                await page.locator("#ui-style-toggle").click()
                assert await page.locator("html").get_attribute("data-ui-style") == "modern"
                await page.locator("#ui-style-toggle").click()
                assert await page.locator("html").get_attribute("data-ui-style") in {
                    "millennium",
                    "modern",
                }
                await page.locator("#ui-style-toggle").click()
                assert await page.locator("html").get_attribute("data-ui-style") == "millennium"

                # 窄屏暗色与 5 列移动底栏
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.evaluate(
                    "window.dispatchEvent(new CustomEvent('maibot:context',{detail:{theme:'dark'}}))"
                )
                assert "dark" in (await page.locator("html").get_attribute("class") or "").split()
                assert await page.locator(".ms-mob-dock [data-mob-nav]").count() == 5
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
            finally:
                await browser.close()
    finally:
        gate.set()
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_now_unified_single_page_in_place_tabs_and_state_boot(tmp_path):
    plugin = await loaded(tmp_path)
    await plugin.manage_write("onboarding_skip", "{}")

    async def capability(name, **arguments):
        return {"success": True, "response": "已完成产物总结", "tool_calls": []}

    plugin.ctx.call_capability = capability
    await plugin.agent_tool(
        action="start",
        message="探索单页四标签页联调",
        chat_id="chat",
        platform="qq",
    )
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

    try:
        raw_doc = Path("webui/now.html").read_text(encoding="utf-8")
        csp_doc = raw_doc.replace(
            "<head>",
            '<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'; img-src data: blob:">',
        )
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.expose_function("queryBackend", query)
                await page.expose_function("actionBackend", action)
                await page.evaluate(
                    """window.maibot={
                        ready:Promise.resolve(),
                        context:{theme:'light', host:{plugins:{}}},
                        page:{state:{}},
                        query:(name,args)=>window.queryBackend(name,args),
                        action:(name,args)=>window.actionBackend(name,args),
                        navigate:(target)=>{throw new Error('should not navigate out of single page: '+target);}
                    }"""
                )
                await page.set_content(csp_doc, wait_until="domcontentloaded")

                # 5 页顶栏导航 + 5 列移动底栏
                assert await page.locator("[data-nav]").count() == 5
                assert await page.locator(".ms-mob-dock [data-mob-nav]").count() == 5
                await expect(page.locator("#ms-page-title")).to_have_text("现场")
                await expect(page.locator("#now-threads")).to_contain_text("探索单页四标签页联调")
                assert await page.locator("html").get_attribute("data-ui-style") == "millennium"
                await page.locator("#ui-style-toggle").click()
                assert await page.locator("html").get_attribute("data-ui-style") == "modern"

                # 现场 -> 能力
                await page.locator('[data-nav="abilities"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("能力")
                await expect(page.locator("#ms-now-view")).to_be_hidden()
                await expect(page.locator("#pr-roster")).to_be_visible()
                assert await page.locator("html").get_attribute("data-ui-style") == "modern"

                # 能力 -> 手账
                await page.locator('[data-nav="memory"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("手账")
                await expect(page.locator("#mem-editor")).to_be_visible()

                # 手账 -> 产物（纯粹产物画廊，不含旧文件系统树）
                await page.locator('[data-nav="library"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("产物")
                await expect(page.locator("#lib-gallery-pane")).to_be_visible()
                await expect(page.locator("#lib-filters")).to_be_visible()
                assert await page.locator("#lib-fs-entry").count() == 0
                assert await page.locator("#pr-roster").count() == 0
                assert await page.locator("html").get_attribute("data-ui-style") == "modern"

                # 产物 -> 设置（4 模块，不含已移除的 A_Memorix/Webhook/自定义服务提示）
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("设置")
                await expect(page.locator("#set-block-chats")).to_be_visible()
                await expect(page.locator("#set-block-auth")).to_be_visible()
                await expect(page.locator("#set-block-vault")).to_be_visible()
                await expect(page.locator("#set-block-runtime")).to_be_visible()
                await expect(page.locator("#ms-mgmt-view")).not_to_contain_text("A_Memorix")
                assert await page.locator("#lib-gallery-pane").count() == 0
                assert await page.locator("html").get_attribute("data-ui-style") == "modern"

                # 设置 -> 现场
                await page.locator('[data-nav="now"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("现场")
                await expect(page.locator("#ms-now-view")).to_be_visible()
                await expect(page.locator("#now-threads")).to_contain_text("探索单页四标签页联调")
                assert await page.locator("#ms-mgmt-view").inner_html() == ""
                assert await page.locator("html").get_attribute("data-ui-style") == "modern"

                # 验证 window.maibot.page.state.tab 启动直达对应标签
                state_page = await browser.new_page(viewport={"width": 1280, "height": 900})
                state_page.on("pageerror", lambda error: errors.append(str(error)))
                await state_page.expose_function("queryBackend", query)
                await state_page.expose_function("actionBackend", action)
                await state_page.evaluate(
                    """window.maibot={
                        ready:Promise.resolve(),
                        context:{theme:'light', host:{plugins:{}}},
                        page:{state:{tab:'library'}},
                        query:(name,args)=>window.queryBackend(name,args),
                        action:(name,args)=>window.actionBackend(name,args),
                        navigate:(target)=>{throw new Error('should not navigate out of single page: '+target);}
                    }"""
                )
                await state_page.set_content(csp_doc, wait_until="domcontentloaded")
                await expect(state_page.locator("#ms-page-title")).to_have_text("产物")
                await expect(state_page.locator("#lib-gallery-pane")).to_be_visible()
                await expect(state_page.locator("#ms-now-view")).to_be_hidden()

                assert not errors
                assert peak <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
