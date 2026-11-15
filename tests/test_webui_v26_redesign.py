"""WebUI v0.3 双端双主题收敛验收测试（静态构建不变式 + Chromium CSP 端到端）。"""

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
COMPILED_PAGES = ("now",)
ALL_TABS = ("now", "profiles", "library", "settings")
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
    "\u5979",
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
        chunks.extend(re.findall(rf'{attr}\s*=\s*"([^"]*)"', html, flags=re.I))
        chunks.extend(re.findall(rf"{attr}\s*=\s*'([^']*)'", html, flags=re.I))
    return chunks


def test_webui_v26_static_build_and_bundle_invariants():
    """Verify v0.3 build consistency, single-bundle budget, zero emoji/banned copy, and CSP purity."""
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

        visible_html = _strip_script_and_style(html)
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

        assert "MaisyaIcons" in html, f"{page_name}.html must embed shared MaisyaIcons helper"
        assert "m-ic-soft3d" in html and "m-ic-mil" in html, (
            f"{page_name}.html must support theme-isolated icon tiers"
        )


async def _seed_demo_plugin(tmp_path):
    """Create an isolated Plugin instance seeded with v0.3 demo data for WebUI checks."""
    plugin = await loaded(tmp_path, ["qq:user:100998877"])
    await plugin.manage_write("onboarding_skip", "{}")
    await plugin.on_config_update(
        "plugin",
        {
            "plugin": {"enabled": True},
            "chats": {"enabled_chats": ["chat", DEMO_CHAT_ID]},
        },
        1,
    )
    cloned = await plugin.manage_write(
        "profile_clone",
        json.dumps(
            {
                "source_id": "default",
                "target_id": "research_bot",
                "name": "调研助手",
            }
        ),
    )
    assert cloned["success"], cloned

    default_profile = plugin.profiles.get("default")
    row = plugin.threads.create(
        default_profile,
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
                },
                "expected_version": svc_cur["version"],
            }
        ),
    )
    assert svc_res["success"], svc_res
    return plugin, row


def _wrap_csp(html: str) -> str:
    return html.replace("<head>", "<head>" + HOST_CSP_META, 1)


async def _audit_rendered_page(
    page, tab_name: str, style: str, scheme: str, viewport_name: str
):
    """Run DOM cleanliness, overflow, theme-isolation, legacy-FLOW-absence, and 1-btn-1-func checks."""
    diag = await page.evaluate(
        """({bannedTerms, compoundPatterns}) => {
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
            flowCount: document.querySelectorAll('.flow-svg, .flow-wire, .fl-svg, #flow-panel, #fl-threads').length,
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
        },
    )

    ctx = f"{tab_name}/{viewport_name}/{style}/{scheme}"
    assert diag["className"] in ("", "dark"), f"[{ctx}] invalid html.className={diag['className']!r}"
    expected_class = "dark" if scheme == "dark" else ""
    assert diag["className"] == expected_class, (
        f"[{ctx}] expected className={expected_class!r}, got {diag['className']!r}"
    )
    assert diag["uiStyle"] == style, f"[{ctx}] expected data-ui-style={style!r}, got {diag['uiStyle']!r}"
    expected_toggle = "千禧机械" if style == "millennium" else "现代极简"
    assert diag["toggleText"] == expected_toggle, (
        f"[{ctx}] expected toggle text {expected_toggle!r}, got {diag['toggleText']!r}"
    )
    assert diag["scrollWidth"] <= diag["innerWidth"], (
        f"[{ctx}] horizontal overflow: {diag['scrollWidth']} > {diag['innerWidth']}"
    )
    assert diag["imgCount"] == 0, f"[{ctx}] rendered forbidden <img>"
    assert diag["iframeCount"] == 0, f"[{ctx}] rendered forbidden <iframe>"
    assert diag["mobCapsuleCount"] == 0, f"[{ctx}] rendered forbidden .mob-capsule"
    assert diag["flowCount"] == 0, f"[{ctx}] legacy FLOW elements leaked: {diag['flowCount']}"
    assert not diag["compoundButtons"], f"[{ctx}] compound buttons found: {diag['compoundButtons']}"
    assert not diag["foundBanned"], f"[{ctx}] banned visible terms found: {diag['foundBanned']}"
    emojis = EMOJI_RE.findall(diag["combinedText"])
    assert not emojis, f"[{ctx}] visible DOM contains Emoji: {emojis[:8]}"
    assert "demo-secret-never-render-001" not in await page.content(), (
        f"[{ctx}] Vault plaintext secret leaked into DOM!"
    )

    assert diag["hasMaisyaIcons"], f"[{ctx}] window.MaisyaIcons missing"
    for key in ("ensureDefs", "soft3d", "mil", "brand", "ui", "milUi", "mod", "ctrl"):
        assert key in diag["maisyaIconKeys"], f"[{ctx}] MaisyaIcons missing key {key!r}"
    if style == "millennium":
        assert diag["soft3dCount"] == 0, (
            f"[{ctx}] Millennium view mixed in .m-ic-soft3d ({diag['soft3dCount']})"
        )
        assert diag["milIconCount"] > 0, (
            f"[{ctx}] Millennium view missing .m-ic-mil/.m-ic-mil-ui icons"
        )
    else:
        assert diag["soft3dCount"] > 0, f"[{ctx}] Modern theme missing .m-ic-soft3d icons"
        assert diag["soft3dEllipseCount"] == 0, (
            f"[{ctx}] Modern theme .m-ic-soft3d must have 0 <ellipse> shadows"
        )


@pytest.mark.asyncio
async def test_webui_v26_theme_toggle_icons_and_flow_isolation(tmp_path):
    """Verify #ui-style-toggle, icon tier isolation, dark mode, legacy FLOW absence, and zero overflow across all 4 tabs."""
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
                doc = _wrap_csp((WEBUI_DIR / "now.html").read_text(encoding="utf-8"))
                for viewport_name, viewport in (
                    ("desktop", {"width": 1440, "height": 900}),
                    ("mobile", {"width": 390, "height": 844}),
                ):
                    page = await browser.new_page(viewport=viewport)
                    page.on("pageerror", lambda err: errors.append(f"{viewport_name}: {err}"))
                    await page.expose_function("hostRpc", rpc)
                    for tab_name in ALL_TABS:
                        await page.evaluate(
                            f"""() => {{
                              window.maibot = {{
                                ready: Promise.resolve(),
                                context: {{ theme: "light", host: {{ plugins: {{}} }} }},
                                page: {{ state: {{ tab: {json.dumps(tab_name)} }} }},
                                query: (name, args) => window.hostRpc("query", name, args),
                                action: (name, args) => window.hostRpc("action", name, args),
                                navigate: () => Promise.resolve(),
                              }};
                            }}"""
                        )
                        await page.set_content(doc, wait_until="domcontentloaded")
                        await page.evaluate(
                            """() => {
                              window.__themeEvents = [];
                              window.addEventListener("maisya:themechange", (ev) => {
                                window.__themeEvents.push(ev.detail || {});
                              });
                            }"""
                        )
                        await expect(page.locator("#ui-style-toggle")).to_be_visible()

                        if viewport_name == "mobile":
                            await expect(page.locator("#ms-mob-dock")).to_be_visible()
                            assert await page.locator("#ms-mob-dock [data-mob-nav]").count() == 4

                        # 1) Millennium + Light
                        await _audit_rendered_page(page, tab_name, "millennium", "light", viewport_name)

                        # 2) Toggle -> Modern + Light, asserting event dispatch
                        await page.evaluate("window.__themeEvents = []")
                        await page.locator("#ui-style-toggle").click()
                        theme_events = await page.evaluate("window.__themeEvents")
                        assert len(theme_events) >= 1, (
                            f"[{tab_name}/{viewport_name}] #ui-style-toggle did not dispatch 'maisya:themechange'"
                        )
                        await _audit_rendered_page(page, tab_name, "modern", "light", viewport_name)

                        # 3) Switch Dark while staying in Modern
                        await page.evaluate(
                            "window.dispatchEvent(new CustomEvent('maibot:context',{detail:{theme:'dark'}}))"
                        )
                        await _audit_rendered_page(page, tab_name, "modern", "dark", viewport_name)

                        # 4) Toggle -> Auto (自动跟随) -> Millennium while in Dark
                        await page.locator("#ui-style-toggle").click()
                        assert await page.locator("html").get_attribute("data-theme") == "auto"
                        await expect(page.locator("#ui-style-toggle")).to_have_text("自动跟随")

                        await page.evaluate("window.__themeEvents = []")
                        await page.locator("#ui-style-toggle").click()
                        theme_events = await page.evaluate("window.__themeEvents")
                        assert len(theme_events) >= 1, (
                            f"[{tab_name}/{viewport_name}] return #ui-style-toggle click did not dispatch 'maisya:themechange'"
                        )
                        await _audit_rendered_page(page, tab_name, "millennium", "dark", viewport_name)

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
    """Verify library single-action buttons, Hermes profile model selector, mobile bare-fish carousel, and settings 4-block layout."""
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
                if name == "read":
                    return await plugin.manage_read(**args)
                return await plugin.console_query(**args)
            return await plugin.manage_write(**args)
        finally:
            active -= 1

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                doc = _wrap_csp((WEBUI_DIR / "now.html").read_text(encoding="utf-8"))

                # --- 1) Desktop 1440x900: Library separated single-purpose actions ---
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                page.on("pageerror", lambda err: errors.append(f"desktop: {err}"))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "library" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")
                await page.locator('#lib-gallery [data-lib-idx="0"]').click()
                await expect(page.locator("#lib-drawer")).to_be_visible()
                for selector in ("#lib-jump-chat", "#lib-copy", "#lib-drawer-close"):
                    btn = page.locator(selector)
                    await expect(btn).to_be_visible()
                    txt = (await btn.inner_text()).strip()
                    assert not any(pat in txt for pat in COMPOUND_BUTTON_PATTERNS), (
                        f"Library inspector button {selector} has compound label: {txt!r}"
                    )
                await page.locator("#lib-copy").click()
                await page.locator("#lib-drawer-close").click()

                # --- 2) Desktop 1440x900: Profiles Hermes primary & model selector ---
                await page.locator('[data-nav="profiles"]').click()
                await page.locator('#pr-roster [data-pick-profile-id="default"]').click()
                await page.locator("#pr-adv-toggle").click()
                await expect(page.locator("select#pr-model")).to_be_visible()
                await expect(page.locator("select#pr-model")).to_contain_text("demo-reasoner-v2")
                await expect(page.locator("select#pr-model")).to_contain_text("demo-fast-v3")
                await page.locator("select#pr-model").select_option("demo-reasoner-v2")
                await page.locator('[data-pr-tab="abilities"]').click()
                await expect(page.locator("#pr-ability-cards")).to_be_visible()
                await page.locator("#pr-btn-save").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
                assert plugin.profiles.get("default").model_name == "demo-reasoner-v2"
                assert "sk-demo-config-secret-999" not in await page.content()

                # --- 3) Mobile 390x844: Profiles frameless bare-fish carousel & Card 1 + bottom sheet ---
                await page.set_viewport_size({"width": 390, "height": 844})
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
                await expect(page.locator("#mob-role-carousel .mob-role-avatar").first).to_be_visible()
                for ui_toggle_click in (False, True):
                    if ui_toggle_click:
                        await page.locator("#ui-style-toggle").click()
                    await expect(page.locator("#ms-mob-dock")).to_be_visible()
                    assert await page.locator(".mob-capsule").count() == 0
                    avatars = page.locator("#mob-role-carousel .mob-role-avatar")
                    avatar_count = await avatars.count()
                    assert avatar_count >= 2, (
                        f"Expected >=2 mobile carousel role avatars, got {avatar_count}"
                    )

                    avatar_styles = await page.evaluate(
                        """() => Array.from(document.querySelectorAll('#mob-role-carousel .mob-role-avatar')).map((el) => {
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
                            btn.getAttribute('aria-selected') === 'true' ||
                            btn.classList.contains('is-active')
                          );
                        }""",
                        target_role,
                    )
                    assert active_ok, f"Clicked role {target_role!r} was not marked active"
                    card1 = page.locator("#ab-mob-role-card1")
                    await expect(card1).to_be_visible()
                    await expect(card1).to_contain_text(target_role)
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )

                await page.locator('[data-mob-role-jump="overview"]').click()
                await expect(page.locator("#profiles-mob-scrim")).to_be_visible()
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                await page.locator("#profiles-mob-sheet-close").click()
                await expect(page.locator("#profiles-mob-scrim")).to_be_hidden()

                # --- 4) Mobile 390x844: Settings 4-block layout in light & dark without overflow ---
                await page.locator('.ms-mob-dock [data-mob-nav="settings"]').click()
                await expect(page.locator("#ms-mob-dock")).to_be_visible()
                for block_id in (
                    "#set-block-chats",
                    "#set-block-auth",
                    "#set-block-vault",
                    "#set-block-runtime",
                ):
                    await expect(page.locator(block_id)).to_be_visible()
                for scheme in ("light", "dark"):
                    await page.evaluate(
                        f"window.dispatchEvent(new CustomEvent('maibot:context',{{detail:{{theme:'{scheme}'}}}}))"
                    )
                    assert await page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )

                assert not errors, f"Browser runtime errors: {errors}"
                assert peak <= 2, f"Exceeded concurrency ceiling: peak={peak}"
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
