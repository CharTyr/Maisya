from pathlib import Path
import re

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded


@pytest.mark.asyncio
async def test_management_pages_edit_assets_settings_and_vault_under_host_csp(tmp_path):
    plugin = await loaded(tmp_path)
    active, peak = 0, 0

    async def request(mode, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            return await (
                plugin.manage_read(**arguments)
                if mode == "read"
                else plugin.manage_write(**arguments)
            )
        finally:
            active -= 1

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1100, "height": 850})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.expose_function("backend", request)
                for name, title in [
                    ("abilities", "能力"),
                    ("memory", "记忆"),
                    ("settings", "设置"),
                ]:
                    await page.evaluate(
                        "window.maibot={ready:Promise.resolve(),context:{theme:'light'},query:(_,args)=>window.backend('read',args),action:(_,args)=>window.backend('write',args),navigate:()=>Promise.resolve()}"
                    )
                    document = Path("webui/" + name + ".html").read_text()
                    document = document.replace(
                        "<head>",
                        "<head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src data: blob:\">",
                    )
                    await page.set_content(document)
                    await expect(page.locator("h1")).to_have_text(title)
                    if name == "abilities":
                        await page.locator('[data-profile="scout"]').click()
                        await page.locator('[data-doc-tab="config"]').click()
                        await expect(page.locator("#content")).to_be_enabled()
                        await expect(page.locator("#feedback")).not_to_contain_text(
                            "未知记忆层"
                        )
                        await expect(page.locator("#cfg-visual")).to_be_visible()
                        await expect(page.locator("#cfg-title")).to_have_value("探索者")
                        await page.locator('[data-cfg-tool="workspace_file"]').check()
                        await expect(page.locator("#content")).to_have_value(
                            re.compile("workspace_file")
                        )
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert (
                            "workspace_file"
                            in plugin.memory.read(
                                "profile", target="scout", document="profile.toml"
                            )["content"]
                        )
                        await page.locator("#document").select_option("notes")
                        await expect(page.locator("#content")).to_be_enabled()
                        await page.locator("#content").fill("先核对原始来源。")
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert (
                            "核对原始来源"
                            in plugin.memory.read("notes", target="scout")["content"]
                        )
                        plugin.memory.write(
                            "notes",
                            "外部已经更新。",
                            target="scout",
                            expected_version=1,
                        )
                        await page.locator("#content").fill("保留我的草稿。")
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text("版本")
                        await expect(page.locator("#content")).to_have_value(
                            "保留我的草稿。"
                        )
                        await page.locator("#reload").click()
                        await expect(page.locator("#content")).to_have_value(
                            "外部已经更新。"
                        )
                        await expect(page.locator("#local-draft")).to_have_value(
                            "保留我的草稿。"
                        )
                        await expect(page.locator("#discovery-info")).not_to_be_empty()
                        await page.locator("#new-id").fill("ui_temp")
                        await page.locator("#clone").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "新能力已创建"
                        )
                        await expect(
                            page.locator('[data-profile="ui_temp"]')
                        ).to_be_visible()
                        await page.locator("#delete-profile").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已删除所选能力档案"
                        )
                        assert await page.locator('[data-profile="ui_temp"]').count() == 0
                    elif name == "memory":
                        await page.locator("#chat").select_option("chat")
                        await expect(page.locator("#content")).to_be_enabled()
                        await page.locator("#content").fill(
                            "群名：玄武厅 <img src=x onerror=window.pwned=true>"
                        )
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert plugin.memory.context("other")["memory"] == ""
                        assert await page.locator("img").count() == 0
                    else:
                        await expect(page.locator("#proactive_level")).to_be_visible()
                        await expect(page.locator("#web_provider")).to_be_visible()
                        await expect(
                            page.locator("#web_provider_custom")
                        ).to_be_visible()
                        await expect(
                            page.locator("#daily_proactive_limit")
                        ).to_be_visible()
                        await expect(
                            page.locator("#heartbeat_interval_minutes")
                        ).to_be_visible()
                        await expect(page.locator("#heartbeat_profile")).to_be_visible()
                        await page.locator("#proactive_level").select_option("quiet")
                        await page.locator("#web_provider").select_option(
                            "google_search_plugin"
                        )
                        await page.locator("#save-settings").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert plugin.settings.proactive_level == "quiet"
                        assert plugin.settings.web_provider == "google_search_plugin"
                        await expect(page.locator("#health")).to_contain_text(
                            "联网来源：google_search_plugin"
                        )
                        await page.locator("#vault-handle").fill("drawing")
                        await page.locator("#vault-value").fill(
                            "ui-private-secret-value"
                        )
                        await page.locator("#save-vault").click()
                        await expect(page.locator("#vault-value")).to_have_value("")
                        assert "drawing" in plugin.vault.handles()
                        assert "ui-private-secret-value" not in await page.content()
                        await page.locator("#intent-chat").fill("chat")
                        await page.locator("#intent-prompt").fill("每天巡检一次热点")
                        await page.locator("#schedule-intent").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "工作流计划已加入队列"
                        )
                        assert any(
                            row["source"] == "workflow"
                            and row["reason"] == "每天巡检一次热点"
                            for row in plugin.intents.list("chat:chat")
                        )
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.evaluate(
                        "window.dispatchEvent(new CustomEvent('maibot:context',{detail:{theme:'dark'}}))"
                    )
                    assert await page.locator("html").get_attribute("class") == "dark"
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth<=window.innerWidth"
                    )
                    await page.screenshot(
                        path=".local-checks/" + name + "-mobile.png", full_page=True
                    )
                assert not errors and peak <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
