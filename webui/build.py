"""将 Maisya 双主题单页 5 标签工作台样式（清朗手账 + MaiBot 千禧机械主题）与管理片段打包为宿主所需的自包含 HTML。"""

import json
import sys
from pathlib import Path

_PAGES: tuple[tuple[str, str], ...] = (
    ("now", "现场"),
    ("outputs", "产物"),
    ("abilities", "能力"),
    ("memory", "手账"),
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
    fish_js: str,
    fragments_json: str,
    mgmt_boot_js: str,
    fragments: dict[str, str],
    page: str,
    title: str,
) -> str:
    is_now = page == "now"
    out = template
    replacements = {
        "{{MILLENNIUM_CSS}}": css,
        "{{MGMT_CSS}}": mgmt_css,
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
        "{{NAV_OUTPUTS_CURRENT}}": ' aria-current="page"' if page == "outputs" else "",
        "{{NAV_ABILITIES_CURRENT}}": ' aria-current="page"' if page == "abilities" else "",
        "{{NAV_MEMORY_CURRENT}}": ' aria-current="page"' if page == "memory" else "",
        "{{NAV_SETTINGS_CURRENT}}": ' aria-current="page"' if page == "settings" else "",
    }
    for key, val in replacements.items():
        out = out.replace(key, val)
    return out


def main():
    root = Path(__file__).resolve().parent
    css = (root / "millennium.css").read_text(encoding="utf-8").rstrip("\n")
    fish_js = (root / "fish.js").read_text(encoding="utf-8").rstrip("\n")
    shell_tpl = (root / "now.html.in").read_text(encoding="utf-8")
    mgmt_tpl = (root / "management.html.in").read_text(encoding="utf-8")

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

    targets: list[tuple[Path, str]] = [
        (
            root / f"{page}.html",
            _render_unified(
                shell_tpl,
                css=css,
                mgmt_css=mgmt_css,
                fish_js=fish_js,
                fragments_json=fragments_json,
                mgmt_boot_js=mgmt_boot_js,
                fragments=fragments,
                page=page,
                title=title,
            ),
        )
        for page, title in _PAGES
    ]
    check = "--check" in sys.argv
    for target, rendered in targets:
        if check:
            if not target.exists() or target.read_text(encoding="utf-8") != rendered:
                raise SystemExit(f"{target.name} 需要重新构建")
        else:
            target.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
