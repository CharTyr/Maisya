import base64
import json
from pathlib import Path
import re

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.mark.asyncio
async def test_management_pages_edit_assets_settings_and_vault_under_host_csp(tmp_path):
    plugin = await loaded(tmp_path)
    profile = plugin.profiles.get("scout")
    row = plugin.threads.create(
        profile,
        "输出周报摘要与海报",
        chat_id="chat",
        platform="qq",
        user_id="qq:Alice",
        workspace="ws_ui",
    )
    with plugin.runtime.files.activity(row) as work:
        work.write(
            "out/summary.html",
            "<h2>周报摘要</h2><p>本周进展顺利。</p><script>window.pwned=true</script>",
        )
        (work.path / "out/poster.png").write_bytes(PNG_1X1)
        work.commit()
        attached = plugin.runtime.files.attach(row, work, ["out/poster.png"])
    with plugin.threads.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='waiting', result=?, completed_revision=1 WHERE thread_id=?",
            (
                json.dumps(
                    {
                        "text": "周报摘要已整理，参考 https://example.org/weekly",
                        "attachments": attached,
                    },
                    ensure_ascii=False,
                ),
                row["thread_id"],
            ),
        )
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
                    ("outputs", "产物"),
                    ("abilities", "能力"),
                    ("memory", "手账"),
                    ("settings", "设置"),
                ]:
                    await page.evaluate(
                        "window.maibot={ready:Promise.resolve(),context:{theme:'light'},query:(_,args)=>window.backend('read',args),action:(_,args)=>window.backend('write',args),navigate:()=>Promise.resolve()}"
                    )
                    document = Path("webui/" + name + ".html").read_text(encoding="utf-8")
                    document = document.replace(
                        "<head>",
                        "<head><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src data: blob:\">",
                    )
                    await page.set_content(document)
                    await expect(page.locator("h1")).to_have_text(title)
                    assert await page.locator(".ms-page-sub").count() == 0
                    if await page.locator(".mil-help").count() > 0:
                        await page.locator(".mil-help").first.click()
                        await expect(page.locator("#mil-help-pop")).to_be_visible()
                        pop_box = await page.locator("#mil-help-pop").bounding_box()
                        vp = page.viewport_size
                        assert pop_box is not None and vp is not None
                        assert pop_box["x"] >= 0 and pop_box["x"] + pop_box["width"] <= vp["width"]
                        await page.keyboard.press("Escape")
                        await expect(page.locator("#mil-help-pop")).to_be_hidden()
                    if name == "outputs":
                        await expect(page.locator("#out-st-total")).not_to_have_text("0")
                        await page.locator('[data-out-idx="0"]').click()
                        await expect(page.locator("#out-stage")).to_be_visible()
                        await expect(page.locator("#out-cur-badge")).not_to_be_empty()
                        await page.locator('[data-out-space="docs"]').click()
                        await expect(page.locator("#out-space-heading")).to_have_text(
                            "文档与代码"
                        )
                        await page.locator('[data-out-layout="grid"]').click()
                        await expect(page.locator("#out-list")).to_have_class(
                            re.compile("out-grid")
                        )
                        await page.locator('[data-out-layout="table"]').click()
                        await page.locator('[data-out-space="all"]').click()
                        await expect(
                            page.locator("#out-recent-list button").first
                        ).to_be_visible()
                        await page.locator("#out-recent-list button").first.click()
                        await page.locator('[data-out-kind="workspace_out"]').click()
                        await page.locator('[data-out-idx="0"]').click()
                        await expect(page.locator("#out-stage")).to_contain_text("周报摘要")
                        await page.locator('[data-out-kind="all"]').click()
                        assert await page.evaluate("Boolean(window.pwned)") is False
                        await page.locator('[data-out-space="workspaces"]').click()
                        await page.locator("#workspaces button").first.click()
                        await page.locator("#workspace-files button").first.click()
                        await expect(page.locator("#workspace-content")).not_to_be_empty()
                        assert await page.locator("img").count() == 0
                        assert await page.locator("iframe").count() == 0
                    elif name == "abilities":
                        import io
                        import zipfile

                        # Default view is the spacious Skills Gallery with editor hidden
                        await expect(
                            page.locator("#skills-gallery-panel")
                        ).to_be_visible()
                        await expect(page.locator("#editor-panel")).to_be_hidden()
                        await expect(
                            page.locator('[data-skill="news-digest"]')
                        ).to_be_visible()
                        await expect(
                            page.locator('[data-skill="news-digest"]')
                        ).to_contain_text("资讯简报")
                        await expect(
                            page.locator('[data-skill="source-checking"]')
                        ).to_be_visible()
                        await page.locator("#skill-filter").fill("资讯")
                        await expect(
                            page.locator('[data-skill="news-digest"]')
                        ).to_be_visible()
                        assert (
                            await page.locator('[data-skill="source-checking"]').count()
                            == 0
                        )
                        await page.locator("#skill-filter").fill("")
                        await page.locator('[data-skill-scope="preset"]').click()
                        await expect(
                            page.locator('[data-skill="source-checking"]')
                        ).to_be_visible()
                        await page.locator('[data-skill-scope="all"]').click()

                        # Clicking a skill card opens SKILL workbench; Back button returns to gallery
                        await page.locator('[data-skill="news-digest"]').click()
                        await expect(page.locator("#editor-panel")).to_be_visible()
                        await expect(page.locator("#editor-silk")).to_have_text(
                            "SKILL · 技能工作台"
                        )
                        await expect(page.locator("#skills-gallery-panel")).to_be_hidden()
                        await page.locator("#back-to-skills").click()
                        await expect(page.locator("#skills-gallery-panel")).to_be_visible()
                        await expect(page.locator("#editor-panel")).to_be_hidden()

                        # Create a custom skill, edit frontmatter, bind role, then delete
                        await page.locator("#open-create-skill").click()
                        await expect(page.locator("#skill-add-drawer")).to_be_visible()
                        await page.locator("#new-skill-id").fill("ui-skill")
                        await page.locator("#create-skill").click()
                        await expect(page.locator("#editor-panel")).to_be_visible()
                        await expect(page.locator("#active-target-tag")).to_have_text(
                            "ui-skill"
                        )
                        await page.locator("#sk-meta-desc").fill("界面创建的核验技能")
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        await page.locator('[data-bind-role="scout"]').check()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已将技能 ui-skill 装配给角色 scout"
                        )
                        assert "ui-skill" in plugin.profiles.get("scout").skills
                        await page.locator("#delete-skill").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已删除技能 ui-skill"
                        )
                        assert "ui-skill" not in plugin.profiles.get("scout").skills
                        await expect(page.locator("#skills-gallery-panel")).to_be_visible()

                        # Upload a real .zip skill package with companion reference file
                        await page.locator("#open-import-skill").click()
                        zbuf = io.BytesIO()
                        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
                            zf.writestr("__MACOSX/._SKILL.md", b"appledouble")
                            zf.writestr(
                                "ui-pack/SKILL.md",
                                "---\nname: ui-pack\ndescription: 压缩包导入演示\n---\n# UI Pack\n",
                            )
                            zf.writestr(
                                "ui-pack/references/rules.md",
                                "# Companion Rules\nCheck reference line.\n",
                            )
                        await page.locator("#skill-file").set_input_files(
                            {
                                "name": "ui-pack.zip",
                                "mimeType": "application/zip",
                                "buffer": zbuf.getvalue(),
                            }
                        )
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已导入技能 ui-pack（含 1 个附属文件）"
                        )
                        await expect(page.locator("#editor-panel")).to_be_visible()
                        await expect(page.locator("#sk-companions-box")).to_be_visible()
                        await page.locator('[data-companion-path="references/rules.md"]').click()
                        await expect(page.locator("#sk-companion-preview")).to_contain_text(
                            "Check reference line."
                        )
                        await page.locator("#delete-skill").click()
                        await expect(page.locator("#skills-gallery-panel")).to_be_visible()

                        await expect(
                            page.locator('[data-profile="digest"]')
                        ).to_contain_text("已停用")
                        await page.locator('[data-profile="digest"]').click()
                        await expect(page.locator("#toggle-profile-enabled")).to_have_text(
                            "开启所选"
                        )
                        await page.locator("#toggle-profile-enabled").click()
                        await expect(
                            page.locator('[data-profile="digest"]')
                        ).to_contain_text("启用")
                        assert plugin.profiles.get("digest").enabled is True
                        await page.locator("#toggle-profile-enabled").click()
                        await expect(
                            page.locator('[data-profile="digest"]')
                        ).to_contain_text("已停用")
                        assert plugin.profiles.get("digest").enabled is False
                        await page.locator('[data-profile="scout"]').click()
                        await page.locator('[data-doc-tab="config"]').click()
                        await expect(page.locator("#content")).to_be_enabled()
                        await expect(page.locator("#feedback")).not_to_contain_text(
                            "未知记忆层"
                        )
                        await expect(page.locator("#cfg-visual")).to_be_visible()
                        await expect(page.locator("#cfg-title")).to_have_value("探索者")
                        await plugin.on_config_update(
                            "model",
                            {
                                "models": [
                                    {
                                        "name": "step-5-preview",
                                        "model_identifier": "stepfun/step-5",
                                        "api_provider": "StepFun",
                                    },
                                    {
                                        "name": "glm-5",
                                        "model_identifier": "zhipu/glm-5",
                                        "api_provider": "Zhipu",
                                    },
                                ],
                                "api_providers": [
                                    {"name": "StepFun", "api_key": "sk-never-show"}
                                ],
                            },
                            3,
                        )
                        await page.locator("#cfg-model-name").fill("step-5-preview")
                        await page.locator("#cfg-model-add").click()
                        await page.locator("#cfg-model-name").fill("glm-5")
                        await page.locator("#cfg-model-add").click()
                        await expect(page.locator("#cfg-model-chain")).to_contain_text(
                            "#1 主选"
                        )
                        await expect(page.locator("#cfg-model-chain")).to_contain_text(
                            "#2 备选"
                        )
                        await page.locator('[data-model-move="down"][data-model-idx="0"]').click()
                        await page.locator('[data-cfg-tool="workspace_file"]').check()
                        await expect(page.locator("#content")).to_have_value(
                            re.compile("workspace_file")
                        )
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert plugin.profiles.get("scout").model_list == (
                            "glm-5",
                            "step-5-preview",
                        )
                        assert (
                            "workspace_file"
                            in plugin.memory.read(
                                "profile", target="scout", document="profile.toml"
                            )["content"]
                        )
                        await page.locator("#cfg-access").select_option("listed")
                        await expect(
                            page.locator("#cfg-allow-scope-wrap")
                        ).to_be_visible()
                        await page.locator("#cfg-chat-id").fill("12345678")
                        await page.locator("#cfg-add-chat-btn").click()
                        await expect(page.locator("#content")).to_have_value(
                            re.compile(r'allow_chats\s*=\s*\["qq:group:12345678"\]')
                        )
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert plugin.profiles.get("scout").access == "listed"
                        assert "qq:group:12345678" in plugin.profiles.get("scout").allow_chats
                        await page.locator('[data-cfg-rm-chat="0"]').click()
                        await page.locator("#cfg-access").select_option("everyone")
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert plugin.profiles.get("scout").access == "everyone"
                        await page.locator('[data-doc-tab="notes"]').click()
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
                        # Test Grok-style Recipe Template Share UI (draft checkup -> export -> preview -> SSRF guard -> copy import)
                        await page.locator("#rcp-draft-btn").click()
                        await expect(page.locator("#rcp-draft-box")).to_be_visible()
                        await expect(page.locator("#rcp-draft-box")).to_contain_text(
                            "隐私体检通过"
                        )
                        await expect(page.locator("#rcp-draft-box")).to_contain_text(
                            "RECIPE.md"
                        )
                        await page.locator("#export").click()
                        await expect(page.locator("#rcp-download-link")).to_be_visible()
                        await expect(page.locator("#copy-package")).to_be_visible()
                        await page.locator("#copy-package").click()
                        assert len(await page.locator("#package-text").input_value()) > 60
                        await page.locator("#rcp-url").fill("http://127.0.0.1/evil.zip")
                        await page.locator("#rcp-fetch-btn").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            re.compile("本地|内网|保留|拒绝")
                        )
                        await page.locator("#rcp-preview-btn").click()
                        await expect(page.locator("#rcp-preview-card")).to_be_visible()
                        await expect(page.locator("#rcp-pv-summary")).to_contain_text(
                            "检测到本地同名角色 scout"
                        )
                        await expect(page.locator("#rcp-pv-alignment")).to_be_visible()
                        await page.locator("#rcp-mode-copy").check()
                        await page.locator("#import-id").fill("ui_rcp_copy")
                        await page.locator("#rcp-confirm-import").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已导入配方副本 ui_rcp_copy"
                        )
                        await expect(
                            page.locator('[data-profile="ui_rcp_copy"]')
                        ).to_be_visible()
                        await page.locator("#delete-profile").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已删除所选角色"
                        )
                        await expect(
                            page.locator('[data-profile="ui_rcp_copy"]')
                        ).to_have_count(0)
                        await page.locator("#nav-discovery").click()
                        await expect(page.locator("#discovery-info")).to_be_visible()
                        await expect(page.locator("#discovery-info")).not_to_be_empty()
                        await page.locator("#new-id").fill("ui_temp")
                        await page.locator("#clone").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "新角色已创建"
                        )
                        await expect(
                            page.locator('[data-profile="ui_temp"]')
                        ).to_be_visible()
                        await page.locator("#delete-profile").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已删除所选角色"
                        )
                        assert await page.locator('[data-profile="ui_temp"]').count() == 0
                    elif name == "memory":
                        await expect(
                            page.locator('[data-kind-tab="memory"]')
                        ).to_contain_text("会话备忘")
                        await expect(page.locator("aside")).to_contain_text("A_Memorix")
                        await expect(page.locator("#intent-list")).to_be_visible()
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
                        await page.locator('[data-kind-tab="heartbeat"]').click()
                        await expect(
                            page.locator("#insert-heartbeat-example")
                        ).to_be_visible()
                        await expect(page.locator("#mem-layer-guide")).to_contain_text(
                            "自主探索方向"
                        )
                        await page.locator("#insert-heartbeat-example").click()
                        await page.locator("#save").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert await page.locator("img").count() == 0
                    else:
                        # Default view opens in General Settings (一般设置) with Millennium style
                        assert (
                            await page.locator("html").get_attribute("data-ui-style")
                            == "millennium"
                        )
                        await expect(
                            page.locator('[data-settings-mode="general"]')
                        ).to_have_attribute("aria-pressed", "true")
                        await expect(page.locator("#ui_style")).to_be_visible()
                        await expect(page.locator("#proactive_level")).to_be_visible()
                        await expect(page.locator("#web_provider")).to_be_visible()
                        await expect(
                            page.locator("#web_provider_custom")
                        ).to_be_hidden()
                        await expect(
                            page.locator("#daily_proactive_limit")
                        ).to_be_visible()
                        await expect(
                            page.locator("#heartbeat_interval_minutes")
                        ).to_be_visible()
                        await expect(page.locator("#heartbeat_profile")).to_be_visible()
                        await expect(page.locator("#health")).to_be_visible()

                        # Advanced panels stay hidden until the user switches to 高级设置
                        await expect(page.locator("#vault-handle")).to_be_hidden()
                        await expect(page.locator("#service-config")).to_be_hidden()
                        await expect(page.locator("#webhook-config")).to_be_hidden()

                        # Progressive disclosure for custom web provider
                        await page.locator("#web_provider").select_option("custom")
                        await expect(
                            page.locator("#web_provider_custom")
                        ).to_be_visible()
                        await page.locator("#web_provider").select_option(
                            "google_search_plugin"
                        )
                        await expect(
                            page.locator("#web_provider_custom")
                        ).to_be_hidden()

                        # Theme switch & persistence: millennium <-> maiwork
                        await page.locator("#ui_style").select_option("maiwork")
                        assert (
                            await page.locator("html").get_attribute("data-ui-style")
                            == "maiwork"
                        )
                        await page.locator("#proactive_level").select_option("quiet")
                        await page.locator("#save-settings").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert plugin.settings.ui_style == "maiwork"
                        assert plugin.settings.proactive_level == "quiet"
                        assert plugin.settings.web_provider == "google_search_plugin"
                        await expect(page.locator("#health")).to_contain_text(
                            "联网来源：google_search_plugin"
                        )

                        await page.locator("#ui_style").select_option("millennium")
                        await page.locator("#save-settings").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert (
                            await page.locator("html").get_attribute("data-ui-style")
                            == "millennium"
                        )
                        assert plugin.settings.ui_style == "millennium"

                        # Interactive entry-list whitelist (platform:type:id) & per-chat level + sleep selects
                        await page.locator("#wl-add-id").fill("12345678")
                        await page.locator("#wl-add-btn").click()
                        await expect(page.locator("#chat-whitelist-box")).to_contain_text(
                            "qq:group:12345678"
                        )
                        added_row = page.locator(".ms-entry-row", has_text="qq:group:12345678")
                        await added_row.locator("select[data-wl-level]").select_option("lively")
                        await page.locator("label.mil-switch", has=page.locator("#sleep-enabled")).click()
                        await page.locator("#sleep-start").select_option("01")
                        await page.locator("#sleep-end").select_option("08")
                        await page.locator("#save-settings").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert "qq:group:12345678" in plugin.settings.enabled_chats
                        assert (
                            plugin.settings.chat_proactive_levels.get("qq:group:12345678")
                            == "lively"
                        )
                        assert plugin.settings.sleep_window == "01-08"
                        await added_row.locator("button[data-wl-remove]").click()
                        await page.locator("#save-settings").click()
                        await expect(page.locator("#feedback")).to_contain_text(
                            "已保存"
                        )
                        assert "qq:group:12345678" not in plugin.settings.enabled_chats

                        # Scheduled preset controls (in General Settings; fine fields expandable)
                        await page.locator('[data-sched-preset="digest"]').click()
                        if not await page.locator("#intent-profile").is_visible():
                            await page.locator(
                                "#sched-advanced-details summary"
                            ).click()
                        await expect(page.locator("#intent-profile")).to_have_value(
                            "digest"
                        )
                        await expect(page.locator("#intent-workspace")).to_have_value(
                            "digest"
                        )
                        await expect(page.locator("#intent-every")).to_have_value(
                            "86400"
                        )
                        await page.locator('[data-sched-preset="once"]').click()
                        await page.locator("#intent-profile").fill("scout")
                        await page.locator("#intent-workspace").fill("")
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

                        # Switch to Advanced Settings (高级设置) for vault & JSON tools
                        await page.locator('[data-settings-mode="advanced"]').click()
                        await expect(page.locator("#vault-handle")).to_be_visible()
                        await expect(page.locator("#service-config")).to_be_visible()
                        await expect(page.locator("#webhook-config")).to_be_visible()
                        await page.locator("#vault-handle").fill("drawing")
                        await page.locator("#vault-value").fill(
                            "ui-private-secret-value"
                        )
                        await page.locator("#save-vault").click()
                        await expect(page.locator("#vault-value")).to_have_value("")
                        assert "drawing" in plugin.vault.handles()
                        assert "ui-private-secret-value" not in await page.content()
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.evaluate(
                        "window.dispatchEvent(new CustomEvent('maibot:context',{detail:{theme:'dark'}}))"
                    )
                    assert await page.locator("html").get_attribute("class") == "dark"
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth<=window.innerWidth"
                    )
                    if name == "settings":
                        await page.locator("#ui-style-toggle").click()
                        assert (
                            await page.locator("html").get_attribute("data-ui-style")
                            == "maiwork"
                        )
                        assert (
                            await page.locator("html").get_attribute("class") == "dark"
                        )
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth<=window.innerWidth"
                        )
                        await page.locator('[data-settings-mode="general"]').click()
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
