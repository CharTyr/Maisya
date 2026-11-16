"""v0.3.1 全面 UX 与核心产品能力恢复验收测试。"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

from playwright.async_api import async_playwright, expect
import pytest

from maisya.packages import export_package, import_package_bundle, preview_package
from maisya.trust import clean_display_name
from test_foundation import loaded
from test_webui_v26_redesign import HOST_CSP_META, PNG_1X1_BYTES


def _wrap_csp(html: str) -> str:
    return html.replace("<head>", "<head>" + HOST_CSP_META, 1)


def _wire_rpc(plugin, rpc_log: list | None = None):
    active, peak = 0, 0

    async def rpc(kind: str, name: str, arguments: dict | None = None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        args = dict(arguments or {})
        if rpc_log is not None:
            rpc_log.append((kind, name, args))
        try:
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

    return rpc, lambda: peak


@pytest.mark.asyncio
async def test_shared_model_catalog_visual_and_chain_reorder(tmp_path, monkeypatch):
    """共享模型库含 185 个未分配模型 + visual 标识，绝不泄密且支持多模型回退链调序。"""
    shared_catalog = tmp_path / "shared" / "model_catalog.toml"
    shared_catalog.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[[api_providers]]",
        'name = "char"',
        'base_url = "https://secret-proxy.example.invalid/v1"',
        'api_key = "sk-shared-secret-never-leak-999"',
        "",
    ]
    for i in range(185):
        prov = "char" if i % 3 == 0 else ("zako" if i % 3 == 1 else "Bailian")
        vis = "visual = true" if i < 6 else "visual = false"
        lines.extend(
            [
                "[[models]]",
                f'name = "unassigned-model-{i:03d}"',
                f'model_identifier = "{prov}/model-{i:03d}"',
                f'api_provider = "{prov}"',
                vis,
                "",
            ]
        )
    shared_catalog.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setenv("MAIBOT_SHARED_MODEL_CATALOG", str(shared_catalog))

    plugin = await loaded(tmp_path, ["qq:10001"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        prof_res = await plugin.manage_read("profiles")
        assert prof_res["success"]
        cat = prof_res["model_catalog"]
        assert len(cat) >= 185
        m0 = next(m for m in cat if m["name"] == "unassigned-model-000")
        assert m0["provider"] == "char" and m0["visual"] is True
        blob = json.dumps(prof_res, ensure_ascii=False)
        assert "sk-shared-secret-never-leak-999" not in blob
        assert "secret-proxy.example.invalid" not in blob

        rpc, get_peak = _wire_rpc(plugin)
        doc = _wrap_csp(Path("webui/now.html").read_text(encoding="utf-8"))
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 860})
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "profiles" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")
                await page.locator("#pr-adv-toggle").click()
                await page.locator("#pr-model-add-btn").click()
                await page.locator("#pr-model-search").fill("unassigned-model-184")
                await page.locator('#pr-model-groups [data-pick-model="unassigned-model-184"]').click()

                await page.locator("#pr-model-add-btn").click()
                await page.locator("#pr-model-search").fill("unassigned-model-001")
                await page.locator('#pr-model-groups [data-pick-model="unassigned-model-001"]').click()

                await expect(page.locator('#pr-model-chain [data-model-item="unassigned-model-001"]')).to_contain_text("视觉")
                # 把第 2 个模型上移为首选模型
                await page.locator('#pr-model-chain [data-model-item="unassigned-model-001"] [data-pr-model-move="-1"]').click()
                await page.locator("#pr-btn-save").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")

                saved = plugin.profiles.get("default")
                assert saved.model_name == "unassigned-model-001"
                assert "unassigned-model-184" in saved.fallback_models
                html_txt = await page.content()
                assert "sk-shared-secret-never-leak-999" not in html_txt
                assert not errors and get_peak() <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_avatar_data_clean_name_ssrf_closed_state_and_nowrap_ui(tmp_path):
    """头像缓存与 CSS 背景渲染、不可见字符兜底、SSRF/无网防护、closed->已结束 与长标题防挤压。"""
    assert clean_display_name("\u3164\u200b  ", fallback="100246809") == "100246809"

    plugin = await loaded(tmp_path, ["qq:10001"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        # SSRF 内网地址拒绝与无 bot_config.toml 禁用公网抓取
        assert plugin._resolve_global_ip("127.0.0.1", 80) is None
        assert plugin._resolve_global_ip("192.168.1.10", 443) is None
        assert plugin._can_fetch_remote_avatar() is False
        assert plugin._download_remote_avatar_data("http://127.0.0.1:8080/a.jpg") == ""

        # 在宿主 data/avatar 目录写入有效群头像 PNG，验证转码、本地缓存与 CSS background-image 渲染
        from PIL import Image

        img_buf = io.BytesIO()
        Image.new("RGB", (16, 16), (37, 99, 235)).save(img_buf, format="PNG")
        avatar_dir = tmp_path / "host" / "data" / "avatar"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        (avatar_dir / "qq_group_100246809.png").write_bytes(img_buf.getvalue())

        async def capability(name, **kwargs):
            if name == "chat.get_all_streams":
                return {
                    "streams": [
                        {
                            "session_id": "sid_grp1",
                            "platform": "qq",
                            "chat_type": "group",
                            "group_id": "100246809",
                            "group_name": "\u3164",
                        }
                    ]
                }
            if name == "llm.generate_with_tools":
                return {"success": True, "response": "已收到补充要求并继续完成。", "tool_calls": []}
            return {"success": True}

        plugin.ctx.call_capability = capability
        cur_set = await plugin.manage_read("settings")
        set_res = await plugin.manage_write(
            "settings_update",
            json.dumps(
                {
                    "settings": {"enabled_chats": ["qq:group:100246809"]},
                    "expected_version": cur_set["version"],
                }
            ),
        )
        assert set_res["success"], set_res
        plugin.messages.upsert_route("sid_grp1", platform="qq", chat_type="group", group_id="100246809")

        prof = plugin.profiles.get("default")
        row = plugin.threads.create(
            prof,
            "这是一个非常非常长长到原本会把右侧状态标签和恢复按钮挤成竖条变丑的线程标题测试文本",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:10001",
        )
        with plugin.threads.connect() as db, db:
            db.execute("UPDATE agent_threads SET state='closed' WHERE thread_id=?", (row["thread_id"],))

        rpc, get_peak = _wire_rpc(plugin)
        doc = _wrap_csp(Path("webui/now.html").read_text(encoding="utf-8"))
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 860})
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "now" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")
                card = page.locator(f'.now-thread-card[data-thread-id="{row["thread_id"]}"]')
                await expect(card).to_be_visible()
                await expect(card).to_contain_text("已结束")
                assert "未知" not in await card.inner_text()
                await expect(card).to_contain_text("100246809")

                # 验证头像为 CSS background-image 且零 <img>
                av_pic = card.locator(".ms-chat-av-pic")
                await expect(av_pic).to_be_attached()
                bg = await av_pic.evaluate("el => getComputedStyle(el).backgroundImage")
                assert "data:image/" in bg
                assert await page.locator("img").count() == 0
                assert await card.locator(".ms-chat-plat svg").count() >= 1

                # 右侧状态标签不得被长标题挤变形，且已移除多余的「恢复继续」按钮；直接发补充说明自动唤醒已结束线程
                tag_box = await card.locator(".mil-tag").bounding_box()
                assert tag_box is not None and tag_box["height"] <= 30
                assert await card.locator("[data-thread-reopen]").count() == 0
                assert await page.locator("#now-reopen-btn").count() == 0
                await card.click()
                await page.locator("#now-note-input").fill("在已结束线程上直接补充一句继续")
                await page.locator("#now-note-send").click()
                await expect(page.locator('#feedback[data-feedback-slot="now"]')).to_have_text(
                    "补充说明已送入线程"
                )
                await plugin.engine.drain()
                reopened_row = plugin.threads.get(row["thread_id"], "qq:group:100246809")
                assert reopened_row["state"] != "closed"
                assert "在已结束线程上直接补充一句继续" in str(reopened_row["history"])
                assert not errors and get_peak() <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_onboarding_icons_structured_selectors_and_enabled_only_chats(tmp_path):
    """引导页 7 项图标、最近私聊平台与 ID 展示、仅显示已启用聊天流与结构化选择器。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        streams_mock = [
            {"session_id": "sid_main", "platform": "qq", "chat_type": "group", "group_id": "100246809", "group_name": "测试群A"},
            {"session_id": "sid_dm1", "platform": "qq", "chat_type": "private", "user_id": "55001", "user_nickname": "\u3164"},
            {"session_id": "sid_dm2", "platform": "qq", "chat_type": "private", "user_id": "55002", "user_nickname": "AliceDM"},
        ]
        plugin.messages.upsert_route("sid_main", platform="qq", chat_type="group", group_id="100246809")
        for idx in range(1, 35):
            gid = str(2000000 + idx)
            sid = f"sid_g{idx}"
            plugin.messages.upsert_route(sid, platform="qq", chat_type="group", group_id=gid)
            streams_mock.append({"session_id": sid, "platform": "qq", "chat_type": "group", "group_id": gid, "group_name": f"未开启群{idx}"})
        plugin.messages.upsert_route("sid_dm1", platform="qq", chat_type="private", user_id="55001")
        plugin.messages.upsert_route("sid_dm2", platform="qq", chat_type="private", user_id="55002")

        async def capability(name, **kwargs):
            if name == "chat.get_all_streams":
                return {"streams": streams_mock}
            if name == "llm.generate_with_tools":
                return {"success": True, "response": "已收到补充要求并继续完成。", "tool_calls": []}
            return {"success": True}

        plugin.ctx.call_capability = capability
        cur_set = await plugin.manage_read("settings")
        set_res = await plugin.manage_write(
            "settings_update",
            json.dumps(
                {
                    "settings": {"enabled_chats": ["qq:group:100246809"]},
                    "expected_version": cur_set["version"],
                }
            ),
        )
        assert set_res["success"], set_res

        rpc_log: list = []
        rpc, get_peak = _wire_rpc(plugin, rpc_log)
        doc = _wrap_csp(Path("webui/now.html").read_text(encoding="utf-8"))
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 860})
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "now" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")
                await expect(page.locator("#onb-scrim")).to_be_visible()
                assert await page.locator(".onb-item .onb-item-ic svg").count() == 7

                # 第 2 步：最近私聊芯片展示平台 Logo + 名字(含不可见兜底 ID) + 原始 ID，以及结构化录入
                await page.locator("#onb-next").click()
                dm_chip_hidden = page.locator('[data-onb-add-admin="qq:private:55001"]')
                await expect(dm_chip_hidden).to_be_visible()
                await expect(dm_chip_hidden).to_contain_text("55001")
                assert await dm_chip_hidden.locator(".ms-chat-plat svg").count() == 1
                dm_chip_named = page.locator('[data-onb-add-admin="qq:private:55002"]')
                await expect(dm_chip_named).to_contain_text("AliceDM")
                await expect(dm_chip_named).to_contain_text("55002")

                await page.locator("#onb-adm-plat").select_option("discord")
                await page.locator("#onb-adm-type").select_option("private")
                await page.locator("#onb-adm-id").fill("887766")
                await page.locator("#onb-admin-add-btn").click()
                await expect(page.locator("#onb-admin-chips")).to_contain_text("discord:private:887766")

                # 第 3 步：仅展示已启用的 1 条聊天流，不直接倾泻 35 条未启用流
                await page.locator("#onb-next").click()
                await expect(page.locator("#onb-chat-list")).to_be_visible()
                await expect(page.locator("#onb-chat-list .ms-stream-row")).to_have_count(1)
                await expect(page.locator("#onb-chat-list")).to_contain_text("测试群A")
                await page.locator("#onb-open-chat-picker").click()
                await expect(page.locator("#onb-chat-cand-list")).to_contain_text("未开启群1")
                await page.locator("#onb-chat-cand-search").fill("未开启群5")
                await expect(page.locator("#onb-chat-cand-list")).to_contain_text("未开启群5")
                await page.locator('[data-onb-add-chat="qq:group:2000005"]').click()
                await expect(page.locator("#onb-chat-list .ms-stream-row")).to_have_count(2)

                # 第 4 步：使用共享检索模型选择器而非原生单薄下拉框
                await page.locator("#onb-next").click()
                await expect(page.locator("#onb-model-trigger")).to_be_visible()
                await page.locator("#onb-close").click()

                # 设置页：候选流按需懒加载 chat_sources，仅展示已启用列表
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator("#set-chat-list .ms-stream-row")).to_have_count(2)
                await page.locator("#set-open-chat-picker").click()
                await page.wait_for_timeout(80)
                assert any(k == "query" and n == "read" and a.get("section") == "chat_sources" for k, n, a in rpc_log)
                await page.locator('[data-set-del-chat="qq:group:100246809"]').click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:group:100246809" not in plugin.settings.enabled_chats
                assert not errors and get_peak() <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_skills_tab_distillation_and_hidden_empty_connectors(tmp_path):
    """独立技能页支持从近期活动提炼 SKILL.md、分类筛选与档案绑定；角色页无外部服务时不显示连接器。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        prof = plugin.profiles.get("default")
        row = plugin.threads.create(
            prof,
            "自动化日报归档流程",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:10001",
        )
        history = [
            {"role": "user", "content": "帮我把群聊日报按项目分类并检查失效链接"},
            {"role": "assistant", "content": "已按项目汇总并完成链接校验。"},
        ]
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='done', history=?, result=?, completed_revision=1 WHERE thread_id=?",
                (
                    json.dumps(history, ensure_ascii=False),
                    json.dumps({"text": "已输出日报归档模板。"}, ensure_ascii=False),
                    row["thread_id"],
                ),
            )

        rpc, get_peak = _wire_rpc(plugin)
        doc = _wrap_csp(Path("webui/now.html").read_text(encoding="utf-8"))
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 860})
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "skills" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")
                await expect(page.locator("#sk-distill-sec")).to_be_visible()
                await page.locator("#sk-distill-sec > summary").click()
                await expect(page.locator("#sk-distill-list")).to_contain_text("自动化日报归档流程")

                # Hermes 式复盘提炼：点击先进入待审阅提案抽屉（不直接落盘污染技能库），确认保存后才写入并绑定
                await page.locator(f'[data-distill-thread="{row["thread_id"]}"]').click()
                await expect(page.locator("#sk-drawer-scrim")).to_be_visible()
                await expect(page.locator("#sk-review-banner")).to_be_visible()
                distilled_id = await page.locator("#sk-edit-id").input_value()
                assert distilled_id and distilled_id not in plugin.memory.skills()
                await page.locator('#sk-bind-profiles [data-sk-bind-profile="default"]').check()
                await page.locator("#sk-drawer-save").click()
                await expect(page.locator("#sk-feedback")).to_contain_text("技能已保存")
                assert distilled_id in plugin.memory.skills()
                assert distilled_id in plugin.profiles.get("default").skills

                # 分类筛选：系统工具 / 个人
                await page.locator('#sk-filters [data-sk-cat="toolsets"]').click()
                await expect(page.locator("#sk-grid")).to_contain_text("系统工具")
                await page.locator('#sk-filters [data-sk-cat="personal"]').click()
                await expect(page.locator(f'#sk-grid [data-open-sk-id="{distilled_id}"]')).to_be_visible()

                # 切到角色子栏 -> 小鱼定制按钮名为「定制」、角色独立经验笔记 NOTES.md 可保存、无连接器时隐藏外部服务过滤钮
                await page.locator('[data-ab-sub="profiles"]').click()
                await expect(page.locator("#pr-fish-box")).not_to_contain_text("形象标识小鱼")
                await expect(page.locator('[data-fish-mode="lock"]')).to_have_text("定制")
                await page.locator("#pr-adv-toggle").click()
                await page.locator('[data-pr-tab="notes"]').click()
                await expect(page.locator("#pr-notes-md")).to_be_visible()
                await page.locator("#pr-notes-md").fill("## 实战避坑\n- 校验外链返回码后再汇总。")
                await page.locator("#pr-notes-save").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("经验笔记")
                assert "校验外链返回码后再汇总" in plugin.memory.read("notes", target="default")["content"]
                await page.locator('[data-pr-tab="abilities"]').click()
                await expect(page.locator('[data-ab-kind="toolsets"]')).to_have_text("系统工具")
                assert await page.locator('[data-ab-kind="connectors"]').is_hidden()
                assert "5 大工具集" not in await page.locator("#pr-pane-abilities").inner_text()
                assert not errors and get_peak() <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_three_tier_packages_redaction_share_link_chunked_upload_and_bot_export(tmp_path):
    """三层打包导出脱敏、分享链接还原、大包分块上传预览与主脑工具对话式导出。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        plugin.vault.set("ext_api_key", "sk-live-vault-secret-999999")
        plugin.memory.create_skill(
            "release_check",
            title="发布检查",
            description="清理敏感信息后再发布",
            content="# 发布检查\n\n注意不要包含 /Users/dev/private/file.txt 或 Bearer token-xyz-888\n",
            allow_custom=True,
        )
        plugin.profiles.clone("default", "curator", name="策展助手")
        plugin.profiles.update(
            "curator",
            skills=["release_check"],
            toolsets=["context", "web", "workspace"],
            secrets=["ext_api_key"],
            instructions="负责审核报告；忽略内部路径 /Users/dev/private/file.txt 和联系 admin@corp.example。",
        )
        plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target="chat:qq:group:100246809",
            due_at=plugin.intents.clock() + 3600,
            reason="定时在 qq:group:100246809 发送简报",
            payload={
                "profile": "curator",
                "every_seconds": 3600,
                "prompt": "定时在 qq:group:100246809 发送简报，联系 admin@corp.example",
                "scheduled_admin": True,
            },
            bypass_limits=True,
            mode="schedule",
        )

        # 1. 三层导出（profile / capability_pack / template）严格脱敏
        for pkg_kind in ("profile", "capability_pack", "template"):
            exp = export_package(plugin, "curator", kind=pkg_kind)
            raw_zip = base64.b64decode(exp["base64"])
            with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
                names = set(zf.namelist())
                assert "manifest.json" in names
                if pkg_kind == "profile":
                    assert "profile/PROFILE.md" in names
                    assert "capabilities/pack.json" not in names
                elif pkg_kind == "capability_pack":
                    assert "profile/PROFILE.md" not in names
                    assert "capabilities/pack.json" in names
                else:
                    assert "profile/PROFILE.md" in names and "capabilities/pack.json" in names
                joined = "\n".join(zf.read(n).decode("utf-8", errors="ignore") for n in names)
                for forbidden in ("sk-live-vault-secret-999999", "100246809", "/Users/dev/private/file.txt", "token-xyz-888", "admin@corp.example"):
                    assert forbidden not in joined, f"{pkg_kind} leaked {forbidden!r}"

        # 2. template 压缩包预览与跨名导入（已移除虚假 maisya:// 分享链接）
        tpl = export_package(plugin, "curator", kind="template")
        assert not tpl.get("share_link")
        prev = preview_package(plugin, base64_data=tpl["base64"])
        assert prev["ok"] and prev["kind"] == "template"
        assert "ext_api_key" in prev["required_secret_handles"]
        assert prev["schedules_count"] >= 1
        assert prev["schedules"][0]["target_chat"] == "<TARGET_CHAT>"

        imp = import_package_bundle(plugin, base64_data=tpl["base64"], target_profile_id="curator_copy", overwrite=True)
        assert imp["ok"] and imp["profile_id"] == "curator_copy"
        copied = plugin.profiles.get("curator_copy")
        assert "release_check" in copied.skills
        assert "admin@corp.example" not in copied.instructions

        # 3. 对话式导出：maisya_shape(op="export") 生成真实 .maisya.zip 文件与工作区工具 package_export
        from maisya.trust import Message
        from test_foundation import event as make_event

        exp_evt = make_event(text="导出curator档案模板", user="10001", platform="qq", group=False)
        exp_evt["message_id"] = "m-curator-exp"
        exp_evt["session_id"] = "priv_admin_exp"
        plugin.messages.record(Message.from_event(exp_evt))
        shape_exp = await plugin.shape_tool(
            action="patch",
            kind="profile",
            op="export",
            target="curator",
            patch={"package_kind": "template"},
            source_msg_id="m-curator-exp",
            chat_id="priv_admin_exp",
            platform="qq",
        )
        assert shape_exp["desensitized"] is True and "share_link" not in shape_exp
        assert shape_exp["filename"].endswith(".maisya.zip")
        assert Path(shape_exp["export_path"]).is_file()

        prof = plugin.profiles.get("curator")
        th = plugin.threads.create(
            prof,
            "对话式打包",
            chat_id="priv_admin_exp",
            platform="qq",
            user_id="qq:10001",
        )
        tool_res = await plugin.runtime.call(
            "package_export",
            {"profile_id": "curator", "kind": "template"},
            th,
        )
        assert tool_res["ok"] and tool_res["attachment_handle"]
        assert tool_res["filename"].endswith(".maisya.zip")

        # 4. Playwright 大于 24 KiB 的 zip 分块上传、预览与确认导入
        big_buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(tpl["base64"])), "r") as src_zf, zipfile.ZipFile(big_buf, "w", zipfile.ZIP_STORED) as dst_zf:
            for item in src_zf.infolist():
                dst_zf.writestr(item, src_zf.read(item.filename))
            dst_zf.writestr("skills/release_check/assets/padding.bin", b"A" * 28000)
        big_zip_path = tmp_path / "big_bundle.maisya.zip"
        big_zip_path.write_bytes(big_buf.getvalue())

        rpc_log: list = []
        rpc, get_peak = _wire_rpc(plugin, rpc_log)
        doc = _wrap_csp(Path("webui/now.html").read_text(encoding="utf-8"))
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 860})
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "profiles" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")
                await page.locator("#pr-btn-import").click()
                await expect(page.locator("#pkg-scrim")).to_be_visible()
                await page.locator("#pr-import-file").set_input_files(str(big_zip_path))
                await expect(page.locator("#pkg-import-preview")).to_be_visible()
                await expect(page.locator("#pkg-import-preview")).to_contain_text("策展助手")
                chunk_calls = [c for c in rpc_log if c[0] == "action" and c[1] == "write" and c[2].get("action") == "package_upload_chunk"]
                assert len(chunk_calls) >= 2
                await page.locator("#pkg-import-confirm").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("已导入档案包")
                assert not errors and get_peak() <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
