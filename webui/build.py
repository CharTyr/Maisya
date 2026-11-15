"""将 Maisya v0.3 双主题 4 页工作台（现场 / 档案 / 资料库 / 设置）打包为宿主单入口自包含 `webui/now.html`。"""

import json
import sys
from pathlib import Path

_PAGES: tuple[tuple[str, str], ...] = (
    ("now", "现场"),
    ("profiles", "档案"),
    ("library", "资料库"),
    ("settings", "设置"),
)


def _extract_between(text: str, start_marker: str, end_marker: str, label: str) -> str:
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start < 0 or end <= start:
        raise ValueError(f"无法定位 {label} 标记")
    return text[start + len(start_marker) : end].strip("\n").rstrip(" \t")


def _replace_marked(text: str, start_marker: str, end_marker: str, placeholder: str, label: str) -> tuple[str, str]:
    start = text.find(start_marker)
    end = text.find(end_marker)
    if start < 0 or end <= start:
        raise ValueError(f"无法定位 {label} 标记")
    extracted = text[start + len(start_marker) : end].strip("\n")
    replaced = text[:start] + placeholder + text[end + len(end_marker) :]
    return replaced, extracted


def _render_unified(
    template: str,
    *,
    css: str,
    mgmt_css: str,
    icons_js: str,
    fish_js: str,
    fragments_json: str,
    mgmt_boot_js: str,
    fragments: dict[str, str],
    page: str = "now",
    title: str = "现场",
) -> str:
    is_now = page == "now"
    out = template
    replacements = {
        "{{MILLENNIUM_CSS}}": css,
        "{{MGMT_CSS}}": mgmt_css,
        "{{ICONS_JS}}": icons_js,
        "{{FISH_JS}}": fish_js,
        "{{MGMT_FRAGMENTS_JSON}}": fragments_json,
        "{{MGMT_BOOT_JS}}": mgmt_boot_js,
        "{{PAGE_ID}}": page,
        "{{TITLE}}": title,
        "{{NOW_HIDDEN}}": "" if is_now else " hidden",
        "{{MGMT_HIDDEN}}": " hidden" if is_now else "",
        "{{MGMT_FEEDBACK_ID}}": "mgmt-feedback" if is_now else "feedback",
        "{{NOW_BODY}}": fragments["now"] if is_now else "",
        "{{MGMT_DEFAULT_BODY}}": "" if is_now else fragments[page],
        "{{NAV_NOW_CURRENT}}": ' aria-current="page"' if page == "now" else "",
        "{{NAV_PROFILES_CURRENT}}": ' aria-current="page"' if page == "profiles" else "",
        "{{NAV_LIBRARY_CURRENT}}": ' aria-current="page"' if page == "library" else "",
        "{{NAV_SETTINGS_CURRENT}}": ' aria-current="page"' if page == "settings" else "",
    }
    for key, val in replacements.items():
        out = out.replace(key, val)
    return out


def main():
    root = Path(__file__).resolve().parent
    css = (root / "millennium.css").read_text(encoding="utf-8").rstrip("\n")
    icons_js = (root / "icons.js").read_text(encoding="utf-8").rstrip("\n")
    fish_js = (root / "fish.js").read_text(encoding="utf-8").rstrip("\n")
    shell_tpl = (root / "now.html.in").read_text(encoding="utf-8")
    mgmt_tpl = (root / "management.html.in").read_text(encoding="utf-8")

    if "{{ICONS_JS}}" not in shell_tpl:
        raise ValueError("missing placeholder: {{ICONS_JS}}")

    shell_tpl, now_body = _replace_marked(
        shell_tpl, "<!-- ==NOW_BODY_START== -->", "<!-- ==NOW_BODY_END== -->", "{{NOW_BODY}}", "NOW_BODY"
    )
    mgmt_css = _extract_between(
        mgmt_tpl, "/* ==MGMT_CSS_START== */", "/* ==MGMT_CSS_END== */", "MGMT_CSS"
    )
    mgmt_boot_js = _extract_between(
        mgmt_tpl, "/* ==MGMT_BOOT_START== */", "/* ==MGMT_BOOT_END== */", "MGMT_BOOT"
    )
    fragments = {"now": now_body}
    for page, _ in _PAGES:
        if page != "now":
            fragments[page] = (root / "fragments" / f"{page}.html").read_text(encoding="utf-8").rstrip("\n")
    fragments_json = json.dumps(fragments, ensure_ascii=False).replace("</", "<\\/")

    target = root / "now.html"
    rendered = _render_unified(
        shell_tpl,
        css=css,
        mgmt_css=mgmt_css,
        icons_js=icons_js,
        fish_js=fish_js,
        fragments_json=fragments_json,
        mgmt_boot_js=mgmt_boot_js,
        fragments=fragments,
        page="now",
        title="现场",
    )

    check = "--check" in sys.argv
    if "{{ICONS_JS}}" in rendered:
        raise SystemExit(f"{target.name} 未能正确内联 icons.js")
    if len(rendered.encode("utf-8")) >= 2 * 1024 * 1024:
        raise SystemExit(f"{target.name} 超出 2 MiB 体积上限")
    if check:
        if not target.exists() or target.read_text(encoding="utf-8") != rendered:
            raise SystemExit(f"{target.name} 需要重新构建")
    else:
        target.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
