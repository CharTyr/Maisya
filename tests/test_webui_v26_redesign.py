"""WebUI v2.6 双端双主题重设计验收测试（静态构建不变式 + Chromium CSP 端到端）。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

from playwright.async_api import async_playwright, expect
import pytest

from test_foundation import loaded


REPO_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = REPO_ROOT / "webui"
COMPILED_PAGES = ("now", "outputs", "abilities", "memory", "settings")
MGMT_PAGES = ("outputs", "abilities", "memory", "settings")
MAX_BUNDLE_BYTES = 2 * 1024 * 1024

HOST_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "connect-src 'none'; "
    "img-src data: blob:; "
    "font-src data:; "
    "media-src data: blob:; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
    '">'
)

# 与设计规范 / 图标库保持一致的 Unicode Emoji 范围
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F900-\U0001F9FF"
    "\U0001FA70-\U0001FAFF"
    "\u2600-\u27BF"
    "]",
    flags=re.UNICODE,
)

# 严禁在编译产物或可见界面中出现的内部术语、旧主题名及多动作拼接文案
PURE_BANNED_TERMS = (
    "她",
    "清朗手账风",
    "清朗手账",
    "A_Memorix",
    "非 0 字节",
    "非0字节",
    "CSP 白名单",
    "CSP",
    "SHA-256",
    "SHA256",
    "timeout 10s",
    "只进不出",
    "MODEL FALLBACK CHAIN",
    "UNTOUCHED BY REQUEST",
    "REGISTERED HANDLES",
    "EXPOSURE POLICY",
    "复制内容 · 切换源码原文",
    "浏览工作区 Files",
    "阅读预览 / 源码原文",
    "复制内容 · 前往现场",
    "复制文件内容 · 前往现场",
    "导出 / 导入",
    "导入 / 导出",
    "查看 / 格式化",
    "查看折叠 JSON 源码",
    "调序 / 定制小鱼",
    "外观 · 切换",
    "新增 / 编辑",
)

# 仅允许用于既有 DOM id / 后端 JSON 字段键，严禁泄露到注释、提示属性或可见界面文案
SCHEMA_OR_INTERNAL_TERMS = (
    "daily_proactive_limit",
    "every_seconds",
    "scheduled_admin",
    "default_task_slot",
    "default_slot",
    "inflight",
)

ALL_VISIBLE_BANNED_TERMS = PURE_BANNED_TERMS + SCHEMA_OR_INTERNAL_TERMS

COMPOUND_BUTTON_PATTERNS = (
    "· 切换",
    "· 前往",
    " / ",
    "调序 /",
    "导出 /",
    "导入 /",
    "查看 /",
    "分享 /",
)

DEMO_CHAT_ID = "qq:group:100246809"

PNG_1X1_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x00\x00"
    b"\x02\x00\x01H\xaf\xa4q\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _strip_script_and_style(html: str) -> str:
    """Return HTML markup excluding <script> and <style> blocks for static copy checks."""
    without_scripts = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", " ", html, flags=re.I)
    return re.sub(r"<style\b[^>]*>[\s\S]*?</style>", " ", without_scripts, flags=re.I)


def _extract_comments_and_ui_attrs(html: str) -> list[str]:
    """Collect HTML/CSS comments and user-visible UI attributes from compiled HTML."""
    chunks: list[str] = []
    chunks.extend(re.findall(r"<!--[\s\S]*?-->", html))
    for style_body in re.findall(r"<style\b[^>]*>([\s\S]*?)</style>", html, flags=re.I):
        chunks.extend(re.findall(r"/\*[\s\S]*?\*/", style_body))
    for attr in ("title", "aria-label", "data-help", "placeholder"):
        chunks.extend(
            re.findall(
                rf'{attr}\s*=\s*"([^"]*)"',
                html,
                flags=re.I,
            )
        )
        chunks.extend(
            re.findall(
                rf"{attr}\s*=\s*'([^']*)'",
                html,
                flags=re.I,
            )
        )
    return chunks


def test_webui_v26_static_build_and_bundle_invariants():
    """Verify build consistency, bundle budget, zero emoji/banned copy, and CSP-safe static output."""
    build_check = subprocess.run(
        [sys.executable, str(WEBUI_DIR / "build.py"), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert build_check.returncode == 0, (
        f"webui/build.py --check failed: {build_check.stdout}\n{build_check.stderr}"
    )

    external_patterns = (
        ("Google Fonts", re.compile(r"fonts\.googleapis\.com|fonts\.gstatic\.com", re.I)),
        ("External CDN", re.compile(r"cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com", re.I)),
        ("External <script src>", re.compile(r"<script\b[^>]+src\s*=\s*['\"]?https?://", re.I)),
        ("External <link href>", re.compile(r"<link\b[^>]+href\s*=\s*['\"]?https?://", re.I)),
        ("CSS @import URL", re.compile(r"@import\s+url\(['\"]?https?://", re.I)),
    )

    for page_name in COMPILED_PAGES:
        html_path = WEBUI_DIR / f"{page_name}.html"
        assert html_path.is_file(), f"Missing compiled bundle: {html_path}"
        raw_bytes = html_path.read_bytes()
        assert len(raw_bytes) < MAX_BUNDLE_BYTES, (
            f"{page_name}.html exceeds 2 MiB budget: {len(raw_bytes)} bytes"
        )
        html = raw_bytes.decode("utf-8")

        emojis = EMOJI_RE.findall(html)
        assert not emojis, f"{page_name}.html contains Unicode Emoji: {emojis[:8]}"

        for term in PURE_BANNED_TERMS:
            assert term not in html, f"{page_name}.html contains banned term: {term!r}"

        # Exclude DOM id/for attribute occurrences of legacy daily_proactive_limit while checking static copy/comments
        visible_html = re.sub(
            r'\b(?:id|for)\s*=\s*["\']daily_proactive_limit["\']',
            "",
            _strip_script_and_style(html),
            flags=re.I,
        )
        comment_and_attr_blob = "\n".join(_extract_comments_and_ui_attrs(html))
        for term in SCHEMA_OR_INTERNAL_TERMS:
            assert term not in visible_html, (
                f"{page_name}.html leaks schema term in static markup: {term!r}"
            )
            assert term not in comment_and_attr_blob, (
                f"{page_name}.html leaks schema term in comment/UI attr: {term!r}"
            )

        assert not re.search(r"<img\b", html, flags=re.I), f"{page_name}.html contains <img>"
        assert not re.search(r"<iframe\b", html, flags=re.I), (
            f"{page_name}.html contains <iframe>"
        )
        assert not re.search(r"transition\s*:\s*all\b", html, flags=re.I), (
            f"{page_name}.html contains forbidden 'transition: all'"
        )
        assert not re.search(r"\blocalStorage\b|\bsessionStorage\b", html), (
            f"{page_name}.html touches browser web storage"
        )
        for label, pat in external_patterns:
            assert not pat.search(html), f"{page_name}.html references {label}"

        if page_name in MGMT_PAGES:
            assert "MaisyaIcons" in html, (
                f"{page_name}.html must embed shared MaisyaIcons helper"
            )
            assert "m-ic-soft3d" in html and "m-ic-mil" in html, (
                f"{page_name}.html must support theme-isolated icon tiers"
            )


async def _seed_demo_plugin(tmp_path):
    """Create an isolated Plugin instance seeded with fake demo data for WebUI checks."""
    plugin = await loaded(tmp_path, ["qq:user:100998877"])
    await plugin.on_config_update(
        "plugin",
        {
            "plugin": {"enabled": True},
            "chats": {"enabled_chats": ["chat", DEMO_CHAT_ID]},
        },
        1,
    )
    scout = plugin.profiles.get("scout")
    row = plugin.threads.create(
        scout,
        "整理公开技术动态简报",
        chat_id=DEMO_CHAT_ID,
        platform="qq",
        user_id="qq:user:100998877",
        workspace="demo_ws",
    )
    with plugin.runtime.files.activity(row) as work:
        work.write(
            "out/demo-brief.html",
            "<h2>演示简报</h2><p>这是一份纯本地演示产物摘要。</p>",
        )
        (work.path / "out/demo-card.png").write_bytes(PNG_1X1_BYTES)
        work.commit()
        attached = plugin.runtime.files.attach(row, work, ["out/demo-card.png"])
    with plugin.threads.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='waiting', result=?, completed_revision=1 WHERE thread_id=?",
            (
                json.dumps(
                    {
                        "text": "已整理演示简报与图卡，参考 https://example.org/demo-report",
                        "attachments": attached,
                    },
                    ensure_ascii=False,
                ),
                row["thread_id"],
            ),
        )
    vault_res = await plugin.manage_write(
        "vault",
        json.dumps({"handle": "demo_token", "value": "demo-secret-never-render-001"}),
    )
    assert vault_res["success"], vault_res
    svc_cur = await plugin.manage_read("services")
    svc_res = await plugin.manage_write(
        "services",
        json.dumps(
            {
                "configuration": {
                    "servers": {
                        "demo_mcp": {
                            "command": ["/usr/bin/python3", "/opt/trusted/demo_server.py"],
                            "credentials": {"DEMO_TOKEN": "demo_token"},
                            "tools": ["demo_search"],
                            "readonly": True,
                        }
                    },
                    "templates": {},
                },
                "expected_version": svc_cur["version"],
            }
        ),
    )
    assert svc_res["success"], svc_res
    return plugin, row


def _wrap_csp(html: str) -> str:
    return html.replace("<head>", "<head>" + HOST_CSP_META, 1)


async def _audit_rendered_page(page, page_name: str, style: str, scheme: str, viewport_name: str):
    """Run DOM cleanliness, overflow, theme-isolation, FLOW-isolation, and 1-btn-1-func checks."""
    diag = await page.evaluate(
        """({bannedTerms, compoundPatterns, pageName, style}) => {
          const textParts = [document.body ? (document.body.innerText || '') : ''];
          for (const t of document.querySelectorAll('svg text')) {
            if (t.textContent) textParts.push(t.textContent);
          }
          for (const opt of document.querySelectorAll('select option')) {
            if (opt.textContent) textParts.push(opt.textContent);
          }
          for (const el of document.querySelectorAll('[title],[aria-label],[data-help],[placeholder]')) {
            for (const attr of ['title', 'aria-label', 'data-help', 'placeholder']) {
              const v = el.getAttribute(attr);
              if (v) textParts.push(v);
            }
          }
          const combinedText = textParts.join('\\n');
          const foundBanned = bannedTerms.filter((term) => combinedText.includes(term));

          const compoundButtons = [];
          for (const btn of document.querySelectorAll('button')) {
            if (btn.classList.contains('mob-role-item')) continue;
            const styleObj = getComputedStyle(btn);
            if (btn.hidden || styleObj.display === 'none' || styleObj.visibility === 'hidden') continue;
            const label = (btn.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!label) continue;
            if (compoundPatterns.some((p) => label.includes(p))) {
              compoundButtons.push(label);
            }
          }

          return {
            scrollWidth: document.documentElement.scrollWidth,
            innerWidth: window.innerWidth,
            className: document.documentElement.className,
            uiStyle: document.documentElement.getAttribute('data-ui-style'),
            toggleText: document.getElementById('ui-style-toggle')?.textContent?.trim() || '',
            imgCount: document.querySelectorAll('img').length,
            iframeCount: document.querySelectorAll('iframe').length,
            mobCapsuleCount: document.querySelectorAll('.mob-capsule').length,
            soft3dCount: document.querySelectorAll('.m-ic-soft3d').length,
            soft3dEllipseCount: document.querySelectorAll('.m-ic-soft3d ellipse').length,
            milIconCount: document.querySelectorAll('.m-ic-mil, .m-ic-mil-ui').length,
            flowCount: document.querySelectorAll('.flow-svg, .flow-wire, .fl-svg, #flow-panel, #flow').length,
            nowFlowVisible: Boolean(
              document.getElementById('flow-panel') &&
              document.getElementById('flow') &&
              document.querySelector('#flow .fl-svg')
            ),
            hasMaisyaIcons: typeof window.MaisyaIcons === 'object' && window.MaisyaIcons !== null,
            maisyaIconKeys: window.MaisyaIcons ? Object.keys(window.MaisyaIcons) : [],
            combinedText,
            foundBanned,
            compoundButtons,
          };
        }""",
        {
            "bannedTerms": list(ALL_VISIBLE_BANNED_TERMS),
            "compoundPatterns": list(COMPOUND_BUTTON_PATTERNS),
            "pageName": page_name,
            "style": style,
        },
    )

    ctx = f"{page_name}/{viewport_name}/{style}/{scheme}"
    assert diag["className"] in ("", "dark"), f"[{ctx}] invalid html.className={diag['className']!r}"
    expected_class = "dark" if scheme == "dark" else ""
    assert diag["className"] == expected_class, (
        f"[{ctx}] expected className={expected_class!r}, got {diag['className']!r}"
    )
    assert diag["uiStyle"] == style, f"[{ctx}] expected data-ui-style={style!r}, got {diag['uiStyle']!r}"
    expected_toggle = "千禧机械" if style == "millennium" else "默认主题"
    assert diag["toggleText"] == expected_toggle, (
        f"[{ctx}] expected toggle text {expected_toggle!r}, got {diag['toggleText']!r}"
    )
    assert diag["scrollWidth"] <= diag["innerWidth"], (
        f"[{ctx}] horizontal overflow: {diag['scrollWidth']} > {diag['innerWidth']}"
    )
    assert diag["imgCount"] == 0, f"[{ctx}] rendered forbidden <img>"
    assert diag["iframeCount"] == 0, f"[{ctx}] rendered forbidden <iframe>"
    assert diag["mobCapsuleCount"] == 0, f"[{ctx}] rendered forbidden .mob-capsule"
    assert not diag["compoundButtons"], f"[{ctx}] compound buttons found: {diag['compoundButtons']}"
    assert not diag["foundBanned"], f"[{ctx}] banned visible terms found: {diag['foundBanned']}"
    emojis = EMOJI_RE.findall(diag["combinedText"])
    assert not emojis, f"[{ctx}] visible DOM contains Emoji: {emojis[:8]}"
    assert "demo-secret-never-render-001" not in await page.content(), (
        f"[{ctx}] Vault plaintext secret leaked into DOM!"
    )

    if page_name == "now":
        assert diag["nowFlowVisible"], f"[{ctx}] now.html lost core FLOW layout"
    else:
        assert diag["flowCount"] == 0, f"[{ctx}] non-now page contains FLOW elements: {diag['flowCount']}"
        assert diag["hasMaisyaIcons"], f"[{ctx}] window.MaisyaIcons missing on management page"
        for key in ("ensureDefs", "soft3d", "mil", "brand", "ui", "milUi", "mod", "ctrl"):
            assert key in diag["maisyaIconKeys"], f"[{ctx}] MaisyaIcons missing key {key!r}"
        if style == "millennium":
            assert diag["soft3dCount"] == 0, (
                f"[{ctx}] Millennium view mixed in .m-ic-soft3d ({diag['soft3dCount']})"
            )
            assert diag["milIconCount"] > 0, f"[{ctx}] Millennium view missing .m-ic-mil/.m-ic-mil-ui icons"
        else:
            assert diag["soft3dCount"] > 0, f"[{ctx}] Default theme missing .m-ic-soft3d icons"
            assert diag["soft3dEllipseCount"] == 0, (
                f"[{ctx}] Default theme .m-ic-soft3d must have 0 <ellipse> shadows"
            )


@pytest.mark.asyncio
async def test_webui_v26_theme_toggle_icons_and_flow_isolation(tmp_path):
    """Verify #ui-style-toggle, icon tier isolation, dark mode, FLOW isolation, and zero overflow across all 5 views."""
    plugin, _row = await _seed_demo_plugin(tmp_path)
    active, peak = 0, 0

    async def rpc(kind, name, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            args = arguments or {}
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
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                for viewport_name, viewport in (
                    ("desktop", {"width": 1440, "height": 900}),
                    ("mobile", {"width": 390, "height": 844}),
                ):
                    page = await browser.new_page(viewport=viewport)
                    page.on("pageerror", lambda err: errors.append(f"{viewport_name}: {err}"))
                    await page.expose_function("hostRpc", rpc)
                    for page_name in COMPILED_PAGES:
                        await page.evaluate(
                            """() => {
                              window.maibot = {
                                ready: Promise.resolve(),
                                context: { theme: "light" },
                                page: { state: {} },
                                query: (name, args) => window.hostRpc("query", name, args),
                                action: (name, args) => window.hostRpc("action", name, args),
                                navigate: () => Promise.resolve(),
                              };
                            }"""
                        )
                        doc = _wrap_csp((WEBUI_DIR / f"{page_name}.html").read_text(encoding="utf-8"))
                        await page.set_content(doc)
                        await page.evaluate(
                            """() => {
                              window.__themeEvents = [];
                              window.addEventListener("maisya:themechange", (ev) => {
                                window.__themeEvents.push(ev.detail || {});
                              });
                            }"""
                        )
                        await expect(page.locator("#ui-style-toggle")).to_be_visible()

                        if viewport_name == "mobile" and page_name in MGMT_PAGES:
                            await expect(page.locator("#ms-mob-dock")).to_be_visible()

                        # 1) Millennium + Light
                        await _audit_rendered_page(page, page_name, "millennium", "light", viewport_name)

                        # 2) Toggle -> Default theme (maiwork) + Light, asserting event dispatch
                        await page.evaluate("window.__themeEvents = []")
                        await page.locator("#ui-style-toggle").click()
                        theme_events = await page.evaluate("window.__themeEvents")
                        assert len(theme_events) >= 1, (
                            f"[{page_name}/{viewport_name}] #ui-style-toggle did not dispatch 'maisya:themechange'"
                        )
                        await _audit_rendered_page(page, page_name, "maiwork", "light", viewport_name)

                        # 3) Switch Dark while staying in Default theme (maiwork)
                        await page.evaluate(
                            "window.dispatchEvent(new CustomEvent('maibot:context',{detail:{theme:'dark'}}))"
                        )
                        await _audit_rendered_page(page, page_name, "maiwork", "dark", viewport_name)

                        # 4) Toggle back to Millennium while in Dark OLED
                        await page.evaluate("window.__themeEvents = []")
                        await page.locator("#ui-style-toggle").click()
                        theme_events = await page.evaluate("window.__themeEvents")
                        assert len(theme_events) >= 1, (
                            f"[{page_name}/{viewport_name}] second #ui-style-toggle click did not dispatch 'maisya:themechange'"
                        )
                        await _audit_rendered_page(page, page_name, "millennium", "dark", viewport_name)

                        # Restore Light
                        await page.evaluate(
                            "window.dispatchEvent(new CustomEvent('maibot:context',{detail:{theme:'light'}}))"
                        )
                    await page.close()
                assert not errors, f"Browser runtime errors: {errors}"
                assert peak <= 2, f"Exceeded concurrency ceiling: peak={peak}"
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_webui_v26_outputs_abilities_models_and_mobile_flows(tmp_path):
    """Verify separated outputs actions, ordered role model selector, mobile bare-fish carousel, and settings Hub/sheet."""
    cfg_dir = tmp_path / "host/config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "model_config.toml").write_text(
        """
[[api_providers]]
name = "DemoProvider"
base_url = "https://demo-gateway.example.invalid/v1"
api_key = "sk-demo-config-secret-999"

[[models]]
name = "demo-planner-v1"
model_identifier = "demo/planner-v1"
api_provider = "DemoProvider"

[[models]]
name = "demo-reasoner-v2"
model_identifier = "demo/reasoner-v2"
api_provider = "DemoProvider"

[[models]]
name = "demo-fast-v3"
model_identifier = "demo/fast-v3"
api_provider = "DemoProvider"
""",
        encoding="utf-8",
    )

    plugin, _row = await _seed_demo_plugin(tmp_path)

    async def capability(name, **arguments):
        if name == "llm.get_available_models":
            return {"success": True, "models": ["planner", "utils"]}
        if name == "statistics.local.models":
            return {"success": True, "models": []}
        return {"success": True}

    plugin.ctx.call_capability = capability
    active, peak = 0, 0

    async def rpc(kind, name, arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            args = arguments or {}
            if kind == "query":
                return await plugin.manage_read(**args)
            return await plugin.manage_write(**args)
        finally:
            active -= 1

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                # --- 1) Desktop 1440x900: Outputs separated actions ---
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                page.on("pageerror", lambda err: errors.append(f"desktop: {err}"))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light" },
                      query: (_, args) => window.hostRpc("query", "read", args),
                      action: (_, args) => window.hostRpc("action", "write", args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(_wrap_csp((WEBUI_DIR / "outputs.html").read_text(encoding="utf-8")))
                await page.locator('[data-out-idx="0"]').click()
                for selector in (
                    '[data-out-view="preview"]',
                    '[data-out-view="raw"]',
                    "#out-copy-btn",
                    "#out-goto-now",
                ):
                    btn = page.locator(selector)
                    await expect(btn).to_be_visible()
                    txt = (await btn.inner_text()).strip()
                    assert not any(pat in txt for pat in COMPOUND_BUTTON_PATTERNS), (
                        f"Outputs inspector button {selector} has compound label: {txt!r}"
                    )
                await page.locator('[data-out-view="raw"]').click()
                await expect(page.locator('[data-out-view="raw"]')).to_have_attribute("aria-pressed", "true")
                await page.locator('[data-out-view="preview"]').click()
                await expect(page.locator('[data-out-view="preview"]')).to_have_attribute("aria-pressed", "true")
                await page.locator("#out-copy-btn").click()

                # --- 2) Desktop 1440x900: Abilities ordered role model selector (0 pill buttons) ---
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light" },
                      query: (_, args) => window.hostRpc("query", "read", args),
                      action: (_, args) => window.hostRpc("action", "write", args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(_wrap_csp((WEBUI_DIR / "abilities.html").read_text(encoding="utf-8")))
                await page.locator("#nav-roles").click()
                await page.locator('[data-profile="scout"]').click()
                await page.locator('[data-doc-tab="config"]').click()

                assert await page.locator("[data-add-host-model]").count() == 0
                assert await page.locator("#cfg-host-models-list button").count() == 0
                await expect(page.locator("select#cfg-model-picker")).to_be_visible()
                await expect(page.locator("select#cfg-task")).to_be_visible()
                clear_btn = page.locator("#cfg-model-clear")
                if await clear_btn.count() > 0 and await clear_btn.is_enabled():
                    await clear_btn.click()

                await page.locator("select#cfg-model-picker").select_option("demo-reasoner-v2")
                await expect(page.locator("select#cfg-model-picker")).to_have_value("")
                await page.locator("#cfg-model-name").fill("demo-fast-v3")
                await page.locator("#cfg-model-add").click()

                chain_loc = page.locator("#cfg-model-chain, #cfg-model-list").first
                await expect(chain_loc).to_contain_text("demo-reasoner-v2")
                await expect(chain_loc).to_contain_text("demo-fast-v3")
                rows = chain_loc.locator(".m-model-row, .cfg-model-item")
                assert await rows.count() == 2
                assert await chain_loc.locator('[data-model-move="up"]').count() == 2
                assert await chain_loc.locator('[data-model-move="down"]').count() == 2
                assert await chain_loc.locator('[data-model-rm]').count() == 2
                assert (
                    await chain_loc.locator(
                        "button:not([data-model-move]):not([data-model-rm])"
                    ).count()
                    == 0
                )

                await chain_loc.locator('[data-model-move="down"]').first.click()
                assert "demo-fast-v3" in await rows.nth(0).inner_text()
                await chain_loc.locator('[data-model-move="up"]').nth(1).click()
                assert "demo-reasoner-v2" in await rows.nth(0).inner_text()
                await chain_loc.locator('[data-model-rm]').nth(1).click()
                assert await rows.count() == 1

                await page.locator("select#cfg-task").select_option("planner")
                await page.locator("#save").click()
                await expect(page.locator("#feedback")).to_contain_text("已保存")
                saved_scout = plugin.profiles.get("scout")
                assert saved_scout.model_list == ("demo-reasoner-v2",)
                assert saved_scout.task == "planner"
                assert "sk-demo-config-secret-999" not in await page.content()

                # --- 3) Mobile 390x844: Abilities frameless bare-fish carousel & Card 1 sync ---
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light" },
                      query: (_, args) => window.hostRpc("query", "read", args),
                      action: (_, args) => window.hostRpc("action", "write", args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(_wrap_csp((WEBUI_DIR / "abilities.html").read_text(encoding="utf-8")))
                nav_roles = page.locator("#nav-roles")
                if (
                    not await page.locator("#mob-role-carousel, .m-mob-carousel").first.is_visible()
                    and await nav_roles.count() > 0
                    and await nav_roles.is_visible()
                ):
                    await nav_roles.click()
                for ui_toggle_click in (False, True):
                    if ui_toggle_click:
                        await page.locator("#ui-style-toggle").click()
                    await expect(page.locator("#ms-mob-dock")).to_be_visible()
                    assert await page.locator(".mob-capsule").count() == 0
                    avatars = page.locator(".mob-role-avatar")
                    avatar_count = await avatars.count()
                    assert avatar_count >= 2, f"Expected >=2 mobile carousel role avatars, got {avatar_count}"

                    avatar_styles = await page.evaluate(
                        """() => Array.from(document.querySelectorAll('.mob-role-avatar')).map((el) => {
                          const cs = getComputedStyle(el);
                          return {
                            bg: cs.backgroundColor,
                            bt: cs.borderTopWidth,
                            br: cs.borderRightWidth,
                            bb: cs.borderBottomWidth,
                            bl: cs.borderLeftWidth,
                            shadow: cs.boxShadow,
                            hasSvg: Boolean(el.querySelector('svg')),
                          };
                        })"""
                    )
                    for idx, st in enumerate(avatar_styles):
                        assert st["hasSvg"], f"Avatar #{idx} missing inline SVG"
                        assert st["bg"] in ("rgba(0, 0, 0, 0)", "transparent"), (
                            f"Avatar #{idx} must be frameless transparent, got bg={st['bg']!r}"
                        )
                        assert (
                            st["bt"] == "0px"
                            and st["br"] == "0px"
                            and st["bb"] == "0px"
                            and st["bl"] == "0px"
                        ), f"Avatar #{idx} must have 0px borders, got {st}"
                        assert st["shadow"] == "none", (
                            f"Avatar #{idx} must have no box-shadow, got {st['shadow']!r}"
                        )

                    role_buttons = page.locator("button.mob-role-item[data-mob-role]")
                    assert await role_buttons.count() >= 2
                    target_role = await role_buttons.nth(1).get_attribute("data-mob-role")
                    assert target_role
                    await role_buttons.nth(1).click()
                    active_ok = await page.evaluate(
                        """(roleId) => {
                          const btn = document.querySelector(`button.mob-role-item[data-mob-role="${roleId}"]`);
                          if (!btn) return false;
                          return (
                            btn.getAttribute('aria-pressed') === 'true' ||
                            btn.getAttribute('aria-current') === 'true' ||
                            btn.classList.contains('is-active') ||
                            btn.classList.contains('active')
                          );
                        }""",
                        target_role,
                    )
                    assert active_ok, f"Clicked role {target_role!r} was not marked active"
                    card1 = page.locator("#ab-mob-role-card1, .m-mob-role-card1").first
                    await expect(card1).to_be_visible()
                    await expect(card1).to_contain_text(target_role)
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )

                # --- 4) Mobile 390x844: Settings Hub -> subroute -> sheet without horizontal overflow ---
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light" },
                      query: (_, args) => window.hostRpc("query", "read", args),
                      action: (_, args) => window.hostRpc("action", "write", args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(_wrap_csp((WEBUI_DIR / "settings.html").read_text(encoding="utf-8")))
                await expect(page.locator("#ms-mob-dock")).to_be_visible()
                await expect(page.locator("#settings-mob-hub, .m-mob-hub").first).to_be_visible()
                assert await page.locator(".mob-capsule").count() == 0
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

                for scheme in ("light", "dark"):
                    await page.evaluate(
                        f"window.dispatchEvent(new CustomEvent('maibot:context',{{detail:{{theme:'{scheme}'}}}}))"
                    )
                    await page.locator('button[data-mob-route="services"]').click()
                    sub_view = page.locator("#settings-mob-sub, .m-mob-sub").first
                    await expect(sub_view).to_be_visible()
                    await expect(page.locator("button[data-mob-back]").first).to_be_visible()
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    sheet_trigger = page.locator("button[data-svc-edit], button[data-mob-sheet]").first
                    await expect(sheet_trigger).to_be_visible()
                    await sheet_trigger.click()
                    await expect(page.locator(".m-mob-sheet").first).to_be_visible()
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    close_sheet = page.locator(
                        ".m-mob-sheet [data-mob-sheet-close], .m-mob-sheet-scrim, .m-mob-scrim"
                    ).first
                    if await close_sheet.count() > 0 and await close_sheet.is_visible():
                        await close_sheet.click()
                    await page.locator("button[data-mob-back]").first.click()
                    await expect(page.locator("#settings-mob-hub, .m-mob-hub").first).to_be_visible()

                assert not errors, f"Browser runtime errors: {errors}"
                assert peak <= 2, f"Exceeded concurrency ceiling: peak={peak}"
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
