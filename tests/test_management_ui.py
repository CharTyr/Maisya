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

                # 1) 资料库页（产物画廊 + 系统文件树 + CSP/XSS 隔离 + 帮助气泡）
                await expect(page.locator("#ms-page-title")).to_have_text("资料库")
                assert await page.locator(".ms-page-sub").count() == 0
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
                await expect(page.locator("#lib-stage")).to_contain_text("周报摘要")
                assert await page.evaluate("Boolean(window.pwned)") is False

                await page.locator('#lib-filters [data-lib-filter="image"]').click()
                await expect(page.locator("#lib-gallery [data-lib-idx]").first).to_be_visible()
                await page.locator("#lib-gallery [data-lib-idx]").first.click()
                await expect(page.locator("#lib-stage .lib-img-stage")).to_be_visible()
                await page.locator("#lib-drawer-close").click()
                await page.locator('#lib-filters [data-lib-filter="all"]').click()
                assert await page.locator("img").count() == 0
                assert await page.locator("iframe").count() == 0

                # 系统文件树：打开 JOURNAL.md 并保存
                journal_node = page.locator('#lib-fs-tree [data-fs-path*="JOURNAL.md"]').first
                await expect(journal_node).to_be_visible()
                await journal_node.click()
                await expect(page.locator("#lib-fs-pane")).to_be_visible()
                old_journal = await page.locator("#lib-fs-editor").input_value()
                await page.locator("#lib-fs-editor").fill(
                    (old_journal + "\n\n## 测试补充\n保持简洁。").strip()
                )
                await page.locator("#lib-fs-save").click()
                await expect(page.locator("#lib-fs-feedback")).to_contain_text("已保存")
                await page.locator("#lib-fs-close-pane").click()
                await expect(page.locator("#lib-gallery-pane")).to_be_visible()

                # 2) 档案页（仅 default 预设 + 新建 + Hermes 高级开关/能力卡片 + .zip + 删除）
                await page.locator('[data-nav="profiles"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("档案")
                await expect(page.locator('#pr-roster [data-pick-profile-id="default"]')).to_be_visible()
                await expect(page.locator("#pr-btn-delete")).to_be_disabled()

                await page.locator("#pr-btn-new").click()
                await page.locator("#pr-id").fill("ui_temp")
                await page.locator("#pr-name").fill("界面临时助手")
                await page.locator("#pr-desc").fill("仅在指定群聊运行")
                await page.locator("#pr-access").select_option("listed")
                await expect(page.locator("#pr-chats-wrap")).to_be_visible()
                await page.locator("#pr-allowed-chats").fill("qq:group:100246809")

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
                await expect(page.locator("#pr-feedback")).to_contain_text("已导出档案包")

                await page.locator("#pr-btn-delete").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("已删除自定义档案")
                assert await page.locator('#pr-roster [data-pick-profile-id="ui_temp"]').count() == 0

                # 3) 设置页（4 模块 + 总开关 + 白名单 + 双源管理员防自锁死 + 安全存储 + 双主题）
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("设置")
                assert await page.locator("html").get_attribute("data-ui-style") == "millennium"
                await expect(page.locator("#set-block-chats")).to_be_visible()
                await expect(page.locator("#set-block-auth")).to_be_visible()
                await expect(page.locator("#set-block-vault")).to_be_visible()
                await expect(page.locator("#set-block-runtime")).to_be_visible()
                await expect(page.locator("#ms-mgmt-view")).not_to_contain_text("A_Memorix")

                # 总开关与会话白名单条目控件
                prev_enabled = plugin.settings.plugin.enabled
                await page.locator("label.mil-switch", has=page.locator("#set-plugin-enabled")).click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert plugin.settings.plugin.enabled is (not prev_enabled)
                await page.locator("label.mil-switch", has=page.locator("#set-plugin-enabled")).click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert plugin.settings.plugin.enabled is prev_enabled

                await page.locator("#set-chat-input").fill("qq:group:12345678")
                await page.locator("#set-chat-add").click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:group:12345678" in plugin.settings.enabled_chats
                await page.locator('input[data-set-chat="qq:group:12345678"]').uncheck()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:group:12345678" not in plugin.settings.enabled_chats

                # 管理员添加与双源防自锁死提示
                await expect(page.locator("#set-host-admins")).to_contain_text("qq:Alice")
                await page.locator("#set-admin-input").fill("qq:private:10001")
                await page.locator("#set-admin-add").click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:10001" in plugin.settings.admins
                await page.locator('[data-set-del-admin="qq:10001"]').click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert "qq:10001" not in plugin.settings.admins

                await page.locator("#set-admin-input").fill("qq:private:10001")
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

                # 安全存储只进不出与页面脱敏
                await page.locator("#set-vault-handle").fill("drawing")
                await page.locator("#set-vault-label").fill("画图密钥")
                await page.locator("#set-vault-domain").fill("api.example.com")
                await page.locator("#set-vault-secret").fill("TOKEN=ui-private-secret-value")
                await page.locator("#set-vault-save").click()
                await expect(page.locator("#set-feedback")).to_contain_text("drawing")
                await expect(page.locator("#set-vault-secret")).to_have_value("")
                assert "drawing" in plugin.vault.handles()
                assert "ui-private-secret-value" not in await page.content()

                # 4) 390x844 窄屏深色切换与无横向溢出校验
                await page.set_viewport_size({"width": 390, "height": 844})
                await expect(page.locator("#ms-mob-dock")).to_be_visible()
                assert await page.locator("#ms-mob-dock [data-mob-nav]").count() == 4
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
                await expect(page.locator("#pr-model")).to_be_visible()

                await expect(page.locator("#pr-model")).to_contain_text("deepseek-r1-standby")
                await expect(page.locator("#pr-model")).not_to_contain_text("tavily")

                # 更新 model_config.toml 并切页重载档案后验证新模型自动同步
                model_cfg.write_text(
                    initial_toml
                    + '\n[[models]]\nname = "qwen-coder-unused"\nmodel_identifier = "qwen/qwen3-coder"\napi_provider = "Bailian"\n',
                    encoding="utf-8",
                )
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator("#set-block-chats")).to_be_visible()
                await page.locator('[data-nav="profiles"]').click()
                await page.locator("#pr-adv-toggle").click()
                await expect(page.locator("#pr-model")).to_contain_text("qwen-coder-unused")

                await page.locator("#pr-model").select_option("deepseek-r1-standby")
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

                # 从现场产物按钮跳转资料库并自动应对首轮 429 重试打开图片抽屉
                await jump_btn.click()
                await expect(frame.locator("#ms-page-title")).to_have_text("资料库")
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
