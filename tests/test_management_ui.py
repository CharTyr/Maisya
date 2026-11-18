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

HOST_CSP_META = (
    "<head><meta http-equiv=\"Content-Security-Policy\" content=\""
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'none'; img-src data: blob:;\">"
)


def _with_csp(raw_doc: str) -> str:
    return raw_doc.replace("<head>", HOST_CSP_META, 1)


@pytest.mark.asyncio
async def test_management_pages_edit_assets_settings_and_vault_under_host_csp(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    profile = plugin.profiles.get("default")
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

    async def request(kind, name, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
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
        finally:
            active -= 1

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.expose_function("hostRpc", request)
                raw_doc = Path("webui/now.html").read_text(encoding="utf-8")
                assert "localStorage" not in raw_doc
                assert "sessionStorage" not in raw_doc
                assert "transition: all" not in raw_doc
                csp_doc = _with_csp(raw_doc)

                await page.evaluate(
                    """window.maibot={
                        ready:Promise.resolve(),
                        context:{theme:'light', host:{plugins:{}}},
                        page:{state:{tab:'library'}},
                        query:(name,args)=>window.hostRpc('query',name,args),
                        action:(name,args)=>window.hostRpc('action',name,args),
                        navigate:(t)=>{throw new Error('unexpected navigate:'+t);}
                    }"""
                )
                await page.set_content(csp_doc, wait_until="domcontentloaded")

                # 1) 产物页（分类计数画廊 + HTML 安全富渲染/源码切换 + 图片舞台 + CSP/XSS 隔离 + 帮助气泡）
                await expect(page.locator("#ms-page-title")).to_have_text("产物")
                assert await page.locator(".ms-page-sub").count() == 0
                assert await page.locator("#lib-fs-tree").count() == 0
                if await page.locator("#ms-mgmt-view .mil-help").count() > 0:
                    await page.locator("#ms-mgmt-view .mil-help").first.click()
                    await expect(page.locator("#mil-help-pop")).to_be_visible()
                    pop_box = await page.locator("#mil-help-pop").bounding_box()
                    vp = page.viewport_size
                    assert pop_box is not None and vp is not None
                    assert pop_box["x"] >= 0 and pop_box["x"] + pop_box["width"] <= vp["width"]
                    await page.keyboard.press("Escape")
                    await expect(page.locator("#mil-help-pop")).to_be_hidden()

                await expect(page.locator("#lib-count-tag")).not_to_have_text("0 项")
                await page.locator('#lib-filters [data-lib-filter="html"]').click()
                await expect(page.locator("#lib-gallery [data-lib-idx]").first).to_be_visible()
                await page.locator("#lib-gallery [data-lib-idx]").first.click()
                await expect(page.locator("#lib-drawer")).to_be_visible()
                await expect(page.locator("#lib-stage .lib-html-doc")).to_contain_text("周报摘要")
                assert await page.evaluate("Boolean(window.pwned)") is False
                await page.locator('#lib-view-seg [data-lib-view="raw"]').click()
                await expect(page.locator("#lib-stage pre")).to_contain_text("周报摘要")

                await page.locator('#lib-filters [data-lib-filter="image"]').click()
                await expect(page.locator("#lib-gallery [data-lib-idx]").first).to_be_visible()
                await page.locator("#lib-gallery [data-lib-idx]").first.click()
                await expect(page.locator("#lib-stage .lib-img-stage")).to_be_visible()
                await page.locator("#lib-drawer-close").click()
                await page.locator('#lib-filters [data-lib-filter="all"]').click()
                assert await page.locator("img").count() == 0
                assert await page.locator("iframe").count() == 0

                # 2) 能力页角色子栏（预设角色 + 新建 + 结构化会话白名单 + 高级开关/经验笔记 + 打包分享 + 删除）
                await page.locator('[data-nav="abilities"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("能力")
                await expect(page.locator('#pr-roster [data-pick-profile-id="default"]')).to_be_visible()
                await expect(page.locator("#pr-btn-delete")).to_be_disabled()

                await page.locator("#pr-btn-new").click()
                await page.locator("#pr-id").fill("ui_temp")
                await page.locator("#pr-name").fill("界面临时助手")
                await page.locator("#pr-desc").fill("仅在指定群聊运行")
                await page.locator("#pr-access").select_option("listed")
                await expect(page.locator("#pr-chats-wrap")).to_be_visible()
                await page.locator("#pr-alw-id").fill("100246809")
                await page.locator("#pr-allow-struct-add").click()
                await expect(page.locator("#pr-allow-chat-list")).to_contain_text("100246809")

                await page.locator("#pr-adv-toggle").click()
                await expect(page.locator("#pr-adv-body")).to_be_visible()
                await page.locator('[data-pr-tab="abilities"]').click()
                await expect(page.locator("#pr-ability-cards")).to_be_visible()
                await page.locator('[data-pr-tab="general"]').click()
                await page.locator("#pr-md").fill("先核对原始来源，再输出结论。")
                await page.locator("#pr-btn-save").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
                assert plugin.profiles.get("ui_temp").access == "listed"
                assert "qq:group:100246809" in plugin.profiles.get("ui_temp").allow_chats

                await page.locator("#pr-btn-export").click()
                await page.locator("#pkg-export-download").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("已导出档案包")

                await page.locator("#pr-btn-delete").click()
                await expect(page.locator("#ms-confirm-dialog")).to_be_visible()
                await expect(page.locator("#ms-confirm-dialog")).to_contain_text("ui_temp")
                await expect(page.locator("#ms-confirm-cancel")).to_be_focused()
                await page.locator("#ms-confirm-cancel").click()
                await expect(page.locator("#ms-confirm-dialog")).to_be_hidden()
                assert plugin.profiles.get("ui_temp") is not None
                await expect(page.locator('#pr-roster [data-pick-profile-id="ui_temp"]')).to_be_visible()

                await page.locator("#pr-btn-delete").click()
                await expect(page.locator("#ms-confirm-dialog")).to_be_visible()
                await page.locator("#ms-confirm-ok").click()
                await expect(page.locator("#ms-confirm-dialog")).to_be_hidden()
                await expect(page.locator("#pr-feedback")).to_contain_text("已删除自定义档案")
                assert await page.locator('#pr-roster [data-pick-profile-id="ui_temp"]').count() == 0

                # 3) 设置页（4 模块 + 总开关 + 白名单 + 结构化添加 + 双源管理员防自锁死 + 安全存储 + 双主题）
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("设置")
                assert await page.locator("html").get_attribute("data-ui-style") == "millennium"
                await expect(page.locator("#set-block-chats")).to_be_visible()
                await expect(page.locator("#set-block-auth")).to_be_visible()
                await expect(page.locator("#set-block-vault")).to_be_visible()
                await expect(page.locator("#set-block-runtime")).to_be_visible()
                await expect(page.locator("#ms-mgmt-view")).not_to_contain_text("A_Memorix")

                # 总开关与结构化会话白名单条目控件
                prev_enabled = plugin.settings.plugin.enabled
                await page.locator("label.mil-switch", has=page.locator("#set-plugin-enabled")).click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert plugin.settings.plugin.enabled is (not prev_enabled)
                await page.locator("label.mil-switch", has=page.locator("#set-plugin-enabled")).click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert plugin.settings.plugin.enabled is prev_enabled

                await page.locator("#set-chat-plat").select_option("qq")
                await page.locator("#set-chat-type").select_option("group")
                await page.locator("#set-chat-id").fill("12345678")
                await page.locator("#set-chat-add").click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:group:12345678" in plugin.settings.enabled_chats
                await page.locator('[data-set-del-chat="qq:group:12345678"]').click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:group:12345678" not in plugin.settings.enabled_chats

                # 管理员结构化添加与双源防自锁死提示
                await expect(page.locator("#set-host-admins")).to_contain_text("qq:Alice")
                await page.locator("#set-adm-plat").select_option("qq")
                await page.locator("#set-adm-type").select_option("private")
                await page.locator("#set-adm-id").fill("10001")
                await page.locator("#set-admin-add").click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:10001" in plugin.settings.admins
                await page.locator('[data-set-del-admin="qq:10001"]').click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:10001" not in plugin.settings.admins

                await page.locator("#set-adm-plat").select_option("qq")
                await page.locator("#set-adm-type").select_option("private")
                await page.locator("#set-adm-id").fill("10001")
                await page.locator("#set-admin-add").click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                orig_cfg_get = plugin._ctx.config.get

                async def _no_host_admins(key, default=None):
                    return [] if key == "plugin.permission" else await orig_cfg_get(key, default)

                plugin._ctx.config.get = _no_host_admins
                await plugin._refresh_admins()
                await page.locator('[data-nav="now"]').click()
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator('[data-set-del-admin="qq:10001"]')).to_be_visible()
                await page.locator('[data-set-del-admin="qq:10001"]').click()
                await expect(page.locator("#set-admin-err")).to_contain_text("至少保留")
                assert "qq:10001" in plugin.settings.admins

                # 运行与主题设置保存
                await page.locator("#set-daily-limit").fill("3")
                await page.locator("#set-sleep-start").fill("01:00")
                await page.locator("#set-sleep-end").fill("08:00")
                await page.locator("#set-save-runtime").click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert plugin.settings.daily_proactive_limit == 3
                assert plugin.settings.sleep_window == "01:00-08:00"

                await page.evaluate(
                    "() => { window.__styleDone = false; window.addEventListener('maisya:ui-style-saved', () => { window.__styleDone = true; }, { once: true }); }"
                )
                await page.locator('#set-theme-seg [data-theme-choice="modern"]').click()
                await page.wait_for_function("() => window.__styleDone === true")
                assert await page.locator("html").get_attribute("data-ui-style") == "modern"
                assert plugin.settings.ui_style == "modern"

                await page.evaluate(
                    "() => { window.__styleDone = false; window.addEventListener('maisya:ui-style-saved', () => { window.__styleDone = true; }, { once: true }); }"
                )
                await page.locator('#set-theme-seg [data-theme-choice="millennium"]').click()
                await page.wait_for_function("() => window.__styleDone === true")
                assert await page.locator("html").get_attribute("data-ui-style") == "millennium"
                assert plugin.settings.ui_style == "millennium"

                # 密码保险箱只进不出与页面脱敏
                await page.locator("#set-vault-handle").fill("drawing")
                await page.locator("#set-vault-secret").fill("TOKEN=ui-private-secret-value")
                await page.locator("#set-vault-save").click()
                await expect(page.locator("#set-feedback")).to_contain_text("drawing")
                await expect(page.locator("#set-vault-secret")).to_have_value("")
                assert "drawing" in plugin.vault.handles()
                assert "ui-private-secret-value" not in await page.content()

                # 4) 390x844 窄屏深色切换与无横向溢出校验
                await page.set_viewport_size({"width": 390, "height": 844})
                await expect(page.locator("#ms-mob-dock")).to_be_visible()
                assert await page.locator("#ms-mob-dock [data-mob-nav]").count() == 5
                await page.evaluate(
                    "window.dispatchEvent(new CustomEvent('maibot:context',{detail:{theme:'dark'}}))"
                )
                assert await page.locator("html").get_attribute("class") == "dark"
                assert await page.evaluate(
                    "document.documentElement.scrollWidth<=window.innerWidth"
                )
                await page.locator("#ui-style-toggle").click()
                assert await page.locator("html").get_attribute("data-ui-style") == "modern"
                assert await page.locator("html").get_attribute("class") == "dark"
                assert await page.evaluate(
                    "document.documentElement.scrollWidth<=window.innerWidth"
                )
                assert not errors and peak <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_abilities_model_picker_lists_and_syncs_unassigned_models_from_model_config_toml(
    tmp_path,
):
    cfg_dir = tmp_path / "host/config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    model_cfg = cfg_dir / "model_config.toml"
    initial_toml = """
[[api_providers]]
name = "SecretProvider"
base_url = "https://secret-ui-gateway.internal/v1"
api_key = "sk-ui-secret-key-001"

[[models]]
name = "active-planner-model"
model_identifier = "stepfun/step-5"
api_provider = "StepFun"

[[models]]
name = "deepseek-r1-standby"
model_identifier = "deepseek/deepseek-reasoner"
api_provider = "SecretProvider"

[model_task_config.planner]
model_list = ["active-planner-model"]

[model_task_config.web_search]
model_list = ["tavily"]
"""
    model_cfg.write_text(initial_toml, encoding="utf-8")

    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        async def capability(name, **arguments):
            if name == "llm.get_available_models":
                return {"success": True, "models": ["planner", "utils"]}
            if name == "statistics.local.models":
                return {"success": True, "models": []}
            return {"success": True}

        plugin.ctx.call_capability = capability
        active, peak = 0, 0

        async def request(kind, name, arguments):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                args = dict(arguments or {})
                if kind == "query":
                    if name == "read":
                        return await plugin.manage_read(**args)
                    return await plugin.console_query(**args)
                return await plugin.manage_write(**args)
            finally:
                active -= 1

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors = []
            try:
                page = await browser.new_page(viewport={"width": 1200, "height": 880})
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.expose_function("hostRpc", request)
                await page.evaluate(
                    """window.maibot={
                        ready:Promise.resolve(),
                        context:{theme:'light'},
                        page:{state:{tab:'profiles'}},
                        query:(name,args)=>window.hostRpc('query',name,args),
                        action:(name,args)=>window.hostRpc('action',name,args),
                        navigate:()=>Promise.resolve()
                    }"""
                )
                doc = _with_csp(Path("webui/now.html").read_text(encoding="utf-8"))
                await page.set_content(doc, wait_until="domcontentloaded")
                await expect(page.locator("#pr-roster")).to_be_visible()
                await page.locator("#pr-adv-toggle").click()
                await page.locator("#pr-model-add-btn").click()
                await expect(page.locator("#pr-model-groups")).to_be_visible()

                await expect(page.locator("#pr-model-groups")).to_contain_text("deepseek-r1-standby")
                await expect(page.locator("#pr-model-groups")).not_to_contain_text("tavily")

                # 更新 model_config.toml 并切页重载档案后验证新模型自动同步
                model_cfg.write_text(
                    initial_toml
                    + '\n[[models]]\nname = "qwen-coder-unused"\nmodel_identifier = "qwen/qwen3-coder"\napi_provider = "Bailian"\n',
                    encoding="utf-8",
                )
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator("#set-block-chats")).to_be_visible()
                await page.locator('[data-nav="abilities"]').click()
                await page.locator("#pr-adv-toggle").click()
                await page.locator("#pr-model-add-btn").click()
                await expect(page.locator("#pr-model-groups")).to_contain_text("qwen-coder-unused")

                await page.locator('#pr-model-groups [data-pick-model="deepseek-r1-standby"]').click()
                await page.locator("#pr-btn-save").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
                assert plugin.profiles.get("default").model_name == "deepseek-r1-standby"

                html_text = await page.content()
                assert "sk-ui-secret-key-001" not in html_text
                assert "secret-ui-gateway.internal" not in html_text
                assert not errors and peak <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


def _make_multichunk_png(width: int = 780, height: int = 1200) -> bytes:
    """Build a valid uncompressed-noise RGB PNG larger than 450 KB (> 2 x 196608 B chunks)."""
    import hashlib
    import struct
    import zlib

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack("!I", len(data)) + tag + data + struct.pack("!I", crc)

    row_bytes = 1 + width * 3
    noise = hashlib.shake_256(b"maisya-multichunk-card").digest(520_000)
    raw = bytearray(row_bytes * height)
    for y in range(min(height, len(noise) // (width * 3))):
        off = y * row_bytes
        raw[off] = 0
        start = y * width * 3
        raw[off + 1 : off + 1 + width * 3] = noise[start : start + width * 3]
    ihdr = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=1)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


@pytest.mark.asyncio
async def test_outputs_preview_multichunk_card_and_styled_svg_html_under_concurrency(
    tmp_path,
):
    png_bytes = _make_multichunk_png(780, 1200)
    assert len(png_bytes) > 450_000

    plugin = await loaded(tmp_path)
    await plugin.manage_write("onboarding_skip", "{}")
    profile = plugin.profiles.get("default")
    row = plugin.threads.create(
        profile,
        "生成今日开源工程图解简报",
        chat_id="chat",
        platform="qq",
        user_id="qq:Alice",
        workspace="digest_ws",
    )
    with plugin.runtime.files.activity(row) as work:
        work.write(
            "out/digest-card.html",
            """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
            :root { --bg: #0f172a; --card-bg: #1e293b; --ink: #f8fafc; }
            .digest-card { background: var(--card-bg); color: var(--ink); padding: 20px; }
            </style></head><body data-theme="dark">
            <div class="digest-card"><h1>天光简报</h1><p>精选开源工程条目</p></div>
            <script>window.pwned_card = true;</script>
            </body></html>""",
        )
        (work.path / "out/digest-card.png").write_bytes(png_bytes)
        work.commit()
        attached = plugin.runtime.files.attach(
            row, work, ["out/digest-card.html", "out/digest-card.png"]
        )

    png_att = next(a for a in attached if a["name"] == "digest-card.png")
    # 后端分片读取 >450KB 二进制 PNG 完整校验
    chunk1 = await plugin.manage_read(
        "artifact",
        json.dumps({"kind": "attachment", "handle": png_att["handle"], "offset": 0}),
    )
    assert chunk1["success"] and chunk1["next_offset"] == 196608
    buf = bytearray(base64.b64decode(chunk1["base64"]))
    off = chunk1["next_offset"]
    while off is not None:
        nxt = await plugin.manage_read(
            "artifact",
            json.dumps({"kind": "attachment", "handle": png_att["handle"], "offset": off}),
        )
        assert nxt["success"]
        buf.extend(base64.b64decode(nxt["base64"]))
        off = nxt["next_offset"]
    assert bytes(buf) == png_bytes

    with plugin.threads.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='waiting', result=?, completed_revision=1 WHERE thread_id=?",
            (
                json.dumps(
                    {
                        "text": "**天光简报｜工程与开源**",
                        "links": ["https://example.org/post"],
                        "attachments": attached,
                    },
                    ensure_ascii=False,
                ),
                row["thread_id"],
            ),
        )

    active, peak = 0, 0
    first_library_rejected = False

    async def rpc_bridge(kind, name, arguments):
        nonlocal active, peak, first_library_rejected
        active += 1
        peak = max(peak, active)
        try:
            args = arguments or {}
            if kind == "query":
                if name == "read":
                    if args.get("section") == "library" and not first_library_rejected:
                        first_library_rejected = True
                        return {
                            "__reject__": "HTTP 429: 该插件已有两个进行中的 WebUI 请求"
                        }
                    return await plugin.manage_read(**args)
                if name == "event":
                    return await plugin.console_event(**args)
                return await plugin.console_query(**args)
            if name == "write":
                return await plugin.manage_write(**args)
            if name == "stop":
                return await plugin.console_stop(**args)
            return await plugin.console_send(**args)
        finally:
            active -= 1

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors = []
            try:
                page = await browser.new_page(viewport={"width": 1240, "height": 900})
                page.on("pageerror", lambda err: errors.append(str(err)))
                await page.expose_function("hostBackend", rpc_bridge)
                await page.set_content(
                    '<!doctype html><html><body style="margin:0;padding:0">'
                    '<iframe id="host-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" '
                    'style="width:100%;height:880px;border:0"></iframe></body></html>'
                )
                csp_and_bridge = """<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src data: blob:; font-src data:; media-src data: blob:; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"><script>
                (() => {
                  let seq = 0;
                  const pending = new Map();
                  window.addEventListener("message", (ev) => {
                    const msg = ev.data || {};
                    if (msg.type !== "maibot-rpc-res") return;
                    const slot = pending.get(msg.id);
                    if (!slot) return;
                    pending.delete(msg.id);
                    if (msg.reject) slot.reject(new Error(msg.reject));
                    else slot.resolve(msg.res);
                  });
                  const call = (kind, name, args) => new Promise((resolve, reject) => {
                    const id = ++seq;
                    pending.set(id, { resolve, reject });
                    window.parent.postMessage({ type: "maibot-rpc-req", id, kind, name, args }, "*");
                  });
                  window.maibot = {
                    ready: Promise.resolve(),
                    context: { theme: "light", host: { plugins: {} } },
                    page: { state: {} },
                    query: (name, args) => call("query", name, args),
                    action: (name, args) => call("action", name, args),
                    navigate: () => Promise.resolve(),
                  };
                })();
                </script>"""
                raw_doc = Path("webui/now.html").read_text(encoding="utf-8")
                srcdoc = raw_doc.replace("<head>", "<head>" + csp_and_bridge, 1)
                await page.evaluate(
                    """(doc) => {
                      const frame = document.getElementById("host-frame");
                      window.addEventListener("message", async (ev) => {
                        const msg = ev.data || {};
                        if (msg.type !== "maibot-rpc-req") return;
                        try {
                          const res = await window.hostBackend(msg.kind, msg.name, msg.args || {});
                          if (res && res.__reject__) {
                            frame.contentWindow.postMessage({ type: "maibot-rpc-res", id: msg.id, reject: res.__reject__ }, "*");
                          } else {
                            frame.contentWindow.postMessage({ type: "maibot-rpc-res", id: msg.id, res }, "*");
                          }
                        } catch (err) {
                          frame.contentWindow.postMessage({ type: "maibot-rpc-res", id: msg.id, reject: String(err) }, "*");
                        }
                      });
                      frame.srcdoc = doc;
                    }""",
                    srcdoc,
                )

                frame = page.frame_locator("#host-frame")
                jump_btn = frame.locator(f'[data-jump-lib="{png_att["handle"]}"]')
                await expect(jump_btn).to_be_visible()
                await expect(jump_btn).to_contain_text("digest-card.png")

                # 从现场产物按钮跳转产物页并自动应对首轮 429 重试打开图片抽屉
                await jump_btn.click()
                await expect(frame.locator("#ms-page-title")).to_have_text("产物")
                assert first_library_rejected is True
                img_stage = frame.locator("#lib-stage .lib-img-stage")
                await expect(img_stage).to_be_visible()
                style_attr = await img_stage.get_attribute("style") or ""
                assert "data:image/png;base64," in style_attr
                assert "blob:" not in style_attr

                child_frame = page.frames[1]
                assert await child_frame.locator("img").count() == 0
                assert await child_frame.locator("iframe").count() == 0

                # 切到 HTML 过滤并安全预览 digest-card.html（<pre> 转义且脚本不执行）
                await frame.locator("#lib-drawer-close").click()
                await frame.locator('#lib-filters [data-lib-filter="html"]').click()
                await expect(frame.locator("#lib-gallery [data-lib-idx]").first).to_be_visible()
                await frame.locator("#lib-gallery [data-lib-idx]").first.click()
                await expect(frame.locator("#lib-stage")).to_contain_text("天光简报")
                assert await child_frame.evaluate("Boolean(window.pwned_card)") is False

                await frame.locator("#lib-drawer-close").click()
                await frame.locator('#lib-filters [data-lib-filter="all"]').click()
                await expect(frame.locator("#lib-count-tag")).not_to_have_text("0 项")
                await frame.locator("#lib-refresh").click()
                await expect(frame.locator("#lib-gallery [data-lib-idx]").first).to_be_visible()
                assert not errors and peak <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_unified_danger_confirmation_across_all_delete_surfaces(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    chat_id = "qq:group:100246809"
    plugin.settings.enabled_chats = [chat_id]
    try:
        # 1) 角色附属文件 & 自定义技能/附属文件 & 预设技能修改（供恢复默认测试）
        pf = await plugin.manage_write(
            "save_profile_file",
            json.dumps(
                {
                    "target": "default",
                    "path": "references/temp_note.md",
                    "content": "临时参考资料",
                }
            ),
        )
        assert pf["success"], pf

        sk_c = await plugin.manage_write(
            "create_skill",
            json.dumps(
                {
                    "target": "ui_del_skill",
                    "title": "待删除技能",
                    "description": "测试技能二次确认",
                    "content": "# 待删除技能\n\n测试正文\n",
                }
            ),
        )
        assert sk_c["success"], sk_c
        sk_f = await plugin.manage_write(
            "save_skill_file",
            json.dumps(
                {
                    "target": "ui_del_skill",
                    "path": "references/sk_ref.md",
                    "content": "技能附属参考",
                }
            ),
        )
        assert sk_f["success"], sk_f

        preset_before = await plugin.manage_read(
            "asset", json.dumps({"kind": "skill", "target": "news-digest"})
        )
        assert preset_before["success"], preset_before
        preset_upd = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "skill",
                    "target": "news-digest",
                    "content": preset_before["content"] + "\n<!-- ui-temp-edit -->\n",
                    "expected_version": preset_before["version"],
                }
            ),
        )
        assert preset_upd["success"], preset_upd

        # 2) MCP + API 连接器
        svc0 = await plugin.manage_read("services")
        mcp_res = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "expected_version": svc0["version"],
                    "item": {
                        "name": "mcp_to_del",
                        "command": ["/bin/echo", "hi"],
                        "description": "待移除测试连接",
                        "readonly": True,
                        "access": "everyone",
                    },
                }
            ),
        )
        assert mcp_res["success"], mcp_res
        api_res = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "api",
                    "name": "api_to_del",
                    "expected_version": mcp_res["version"],
                    "item": {
                        "api_name": "demo.read",
                        "description": "待移除测试接口",
                        "readonly": True,
                        "access": "everyone",
                    },
                }
            ),
        )
        assert api_res["success"], api_res

        # 3) Vault + Grant
        plugin.vault.set("vault_to_del", "secret-val-123")
        grant_key = plugin.grants.grant(
            scope="*",
            action_key="exec:demo_grant",
            tier="always",
            actor="qq:Alice",
            note="测试撤销二次确认",
        )

        # 4) Threads (deletable + prunable) & Intent
        p_def = plugin.profiles.get("default")
        th_done = plugin.threads.create(
            p_def, "可删除单条记录", chat_id=chat_id, platform="qq", user_id="u1"
        )
        rev_d = plugin.threads.enqueue(th_done["thread_id"], "完成单条", chat_id)
        plugin.threads.finish(
            th_done["thread_id"], chat_id, {"text": "单条已完成结论"}, [], rev_d
        )

        th_junk = plugin.threads.create(
            p_def, "可清理废记录", chat_id=chat_id, platform="qq", user_id="u1"
        )
        plugin.threads.stop(th_junk["thread_id"], chat_id)

        sched = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": chat_id,
                    "profile": "default",
                    "in_seconds": 3600,
                    "every_seconds": 86400,
                    "prompt": "待取消测试计划",
                }
            ),
        )
        assert sched["success"], sched
        all_intents = (await plugin.manage_read("intents"))["intents"]
        intent_id = next(
            it["intent_id"] for it in all_intents if "待取消测试计划" in it["reason"]
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

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                await page.expose_function("hostRpc", request)
                await page.evaluate(
                    """window.maibot={
                        ready:Promise.resolve(),
                        context:{theme:'light', host:{plugins:{}}},
                        page:{state:{tab:'abilities'}},
                        query:(name,args)=>window.hostRpc('query',name,args),
                        action:(name,args)=>window.hostRpc('action',name,args),
                        navigate:()=>Promise.resolve()
                    }"""
                )
                csp_doc = _with_csp(Path("webui/now.html").read_text(encoding="utf-8"))
                await page.set_content(csp_doc, wait_until="domcontentloaded")

                dlg = page.locator("#ms-confirm-dialog")
                btn_cancel = page.locator("#ms-confirm-cancel")
                btn_ok = page.locator("#ms-confirm-ok")

                # A. 角色附属文件删除：取消保留，确认删除（默认选中 __planner__，先切换到 default 并展开高级区）
                await page.locator('#pr-roster [data-pick-profile-id="default"]').wait_for(state="visible")
                await page.locator('#pr-roster [data-pick-profile-id="default"]').click()
                await page.locator("#pr-adv-toggle").click()
                await expect(page.locator("#pr-adv-body")).to_be_visible()
                await page.locator("#pr-companions-box > summary").click()
                await page.locator('[data-pr-comp-open="references/temp_note.md"]').click()
                await expect(page.locator("#pr-comp-del")).to_be_visible()
                await page.locator("#pr-comp-del").click()
                await expect(dlg).to_be_visible()
                await expect(dlg).to_contain_text("references/temp_note.md")
                await btn_cancel.click()
                await expect(dlg).to_be_hidden()
                assert await page.locator('[data-pr-comp-open="references/temp_note.md"]').count() == 1
                await page.locator("#pr-comp-del").click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#pr-feedback")).to_contain_text("已删除角色附属文件")
                assert await page.locator('[data-pr-comp-open="references/temp_note.md"]').count() == 0

                # B. 技能抽屉：技能附属文件 + 技能删除（Esc 不关抽屉！）+ 预设恢复默认
                await page.locator("#ab-tab-skills").click()
                await expect(page.locator("#sk-grid")).to_contain_text("待删除技能")
                await page.locator('[data-open-sk-kind="skill"][data-open-sk-id="ui_del_skill"]').click()
                await expect(page.locator("#sk-drawer-scrim")).to_be_visible()

                # 技能附属文件删除
                await page.locator("#sk-companions-box > summary").click()
                await page.locator('[data-sk-comp-open="references/sk_ref.md"]').click()
                await expect(page.locator("#sk-comp-del")).to_be_visible()
                await page.locator("#sk-comp-del").click()
                await expect(dlg).to_be_visible()
                await expect(dlg).to_contain_text("references/sk_ref.md")
                await btn_cancel.click()
                await expect(dlg).to_be_hidden()
                assert await page.locator('[data-sk-comp-open="references/sk_ref.md"]').count() == 1
                await page.locator("#sk-comp-del").click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#sk-drawer-fb")).to_contain_text("已删除附属文件")
                assert await page.locator('[data-sk-comp-open="references/sk_ref.md"]').count() == 0

                # 技能删除：Esc 关闭确认弹窗，技能抽屉保持打开！
                await page.locator("#sk-drawer-delete").click()
                await expect(dlg).to_be_visible()
                await page.keyboard.press("Escape")
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#sk-drawer-scrim")).to_be_visible()
                assert any(
                    s["id"] == "ui_del_skill"
                    for s in (await plugin.manage_read("skills"))["skills"]
                )
                await page.locator("#sk-drawer-delete").click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#sk-feedback")).to_contain_text("已删除技能")
                assert all(
                    s["id"] != "ui_del_skill"
                    for s in (await plugin.manage_read("skills"))["skills"]
                )

                # 预设技能恢复默认
                await page.locator('[data-open-sk-kind="skill"][data-open-sk-id="news-digest"]').click()
                await expect(page.locator("#sk-drawer-reset")).to_be_visible()
                await page.locator("#sk-drawer-reset").click()
                await expect(dlg).to_be_visible()
                await btn_cancel.click()
                await expect(dlg).to_be_hidden()
                cur_preset = await plugin.manage_read(
                    "asset", json.dumps({"kind": "skill", "target": "news-digest"})
                )
                assert "ui-temp-edit" in cur_preset["content"]
                await page.locator("#sk-drawer-reset").click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#sk-feedback")).to_contain_text("已恢复预设技能默认内容")
                reset_preset = await plugin.manage_read(
                    "asset", json.dumps({"kind": "skill", "target": "news-digest"})
                )
                assert "ui-temp-edit" not in reset_preset["content"]
                await page.locator("#sk-drawer-close").click()

                # C. 外部连接 MCP & 插件 API 移除
                await page.locator("#ab-tab-connectors").click()
                await expect(page.locator('[data-conn-del="mcp_to_del"]')).to_be_visible()
                await page.locator('[data-conn-del="mcp_to_del"]').click()
                await expect(dlg).to_be_visible()
                await expect(dlg).to_contain_text("mcp_to_del")
                await btn_cancel.click()
                assert "mcp_to_del" in plugin.runtime.services.load()["servers"]
                await page.locator('[data-conn-del="mcp_to_del"]').click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#conn-feedback")).to_contain_text("已移除连接")
                assert "mcp_to_del" not in plugin.runtime.services.load()["servers"]

                await expect(page.locator('[data-conn-del-api="api_to_del"]')).to_be_visible()
                await page.locator('[data-conn-del-api="api_to_del"]').click()
                await expect(dlg).to_be_visible()
                await expect(dlg).to_contain_text("api_to_del")
                await btn_cancel.click()
                assert "api_to_del" in plugin.runtime.services.load().get("apis", {})
                await page.locator('[data-conn-del-api="api_to_del"]').click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#conn-feedback")).to_contain_text("已移除插件接口")
                assert "api_to_del" not in plugin.runtime.services.load().get("apis", {})

                # D. 设置页：授权撤销 & 密码保险箱删除
                await page.locator('[data-nav="settings"]').click()
                rev_btn = page.locator(f'[data-revoke-grant="{grant_key}"]')
                await expect(rev_btn).to_be_visible()
                await rev_btn.click()
                await expect(dlg).to_be_visible()
                await btn_cancel.click()
                assert any(g["grant_key"] == grant_key for g in plugin.grants.list_grants())
                await rev_btn.click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#set-feedback")).to_contain_text("已撤销授权")
                assert all(g["grant_key"] != grant_key for g in plugin.grants.list_grants())

                v_del_btn = page.locator('[data-vault-del="vault_to_del"]')
                await expect(v_del_btn).to_be_visible()
                await v_del_btn.click()
                await expect(dlg).to_be_visible()
                await expect(dlg).to_contain_text("vault_to_del")
                await btn_cancel.click()
                assert "vault_to_del" in plugin.vault.handles()
                await v_del_btn.click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator("#set-feedback")).to_contain_text("已删除密码别名")
                assert "vault_to_del" not in plugin.vault.handles()

                # E. 现场页：单条删除、清理废记录、取消计划（非破坏性的暂停不弹窗）
                await page.locator('[data-nav="now"]').click()
                done_card = page.locator(f'#now-threads [data-thread-id="{th_done["thread_id"]}"]')
                await expect(done_card).to_be_visible()
                await done_card.click()
                await expect(page.locator("#now-del-thread-btn")).to_be_visible()
                await page.locator("#now-del-thread-btn").click()
                await expect(dlg).to_be_visible()
                await btn_cancel.click()
                assert plugin.threads.get(th_done["thread_id"], chat_id) is not None
                await page.locator("#now-del-thread-btn").click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator('[data-feedback-slot="now"]')).to_contain_text("已删除该条活动记录")
                with pytest.raises(ValueError, match="线程不存在"):
                    plugin.threads.get(th_done["thread_id"], chat_id)

                await page.locator("#now-prune-btn").click()
                await expect(dlg).to_be_visible()
                await btn_cancel.click()
                assert plugin.threads.get(th_junk["thread_id"], chat_id) is not None
                await page.locator("#now-prune-btn").click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator('[data-feedback-slot="now"]')).to_contain_text("已清理")
                with pytest.raises(ValueError, match="线程不存在"):
                    plugin.threads.get(th_junk["thread_id"], chat_id)

                # 非破坏性「暂停」直接生效，不弹窗；「取消」弹窗确认
                pause_btn = page.locator(
                    f'#now-intents [data-intent-act="intent_pause"][data-intent-id="{intent_id}"]'
                )
                await expect(pause_btn).to_be_visible()
                await pause_btn.click()
                await expect(dlg).to_be_hidden()
                await expect(page.locator('[data-feedback-slot="now"]')).to_contain_text("状态已更新")
                assert plugin.intents.get(intent_id)["status"] == "paused"

                del_int_btn = page.locator(
                    f'#now-intents [data-intent-act="intent_delete"][data-intent-id="{intent_id}"]'
                )
                await del_int_btn.click()
                await expect(dlg).to_be_visible()
                await expect(dlg).to_contain_text("待取消测试计划")
                await btn_cancel.click()
                assert plugin.intents.get(intent_id)["status"] == "paused"
                await del_int_btn.click()
                await btn_ok.click()
                await expect(dlg).to_be_hidden()
                await expect(del_int_btn).to_have_count(0)
                await expect(page.locator('[data-feedback-slot="now"]')).to_contain_text("状态已更新")
                assert plugin.intents.get(intent_id)["status"] == "cancelled"
                assert errors == []
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
