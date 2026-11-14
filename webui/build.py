"""将 Maisya 双主题工作台样式（清朗手账默认 + MaiBot 千禧机械主题）与页面骨架打包为宿主所需的自包含 HTML。"""

import sys
from pathlib import Path


def _render_management(
    root: Path, css: str, fish_js: str, page: str, title: str
) -> str:
    template = (root / "management.html.in").read_text(encoding="utf-8")
    body = (root / "fragments" / f"{page}.html").read_text(encoding="utf-8").rstrip("\n")
    return (
        template.replace("{{MILLENNIUM_CSS}}", css)
        .replace("{{FISH_JS}}", fish_js)
        .replace("{{PAGE_ID}}", page)
        .replace("{{TITLE}}", title)
        .replace("{{BODY}}", body)
    )


def _render_now(root: Path, css: str, fish_js: str) -> str:
    template = (root / "now.html.in").read_text(encoding="utf-8")
    return template.replace("{{MILLENNIUM_CSS}}", css).replace("{{FISH_JS}}", fish_js)


def main():
    root = Path(__file__).resolve().parent
    css = (root / "millennium.css").read_text(encoding="utf-8").rstrip("\n")
    fish_js = (root / "fish.js").read_text(encoding="utf-8").rstrip("\n")
    pages = (
        ("outputs", "产物"),
        ("abilities", "能力"),
        ("memory", "手账"),
        ("settings", "设置"),
    )
    targets: list[tuple[Path, str]] = [
        (root / "now.html", _render_now(root, css, fish_js)),
        *(
            (root / f"{page}.html", _render_management(root, css, fish_js, page, title))
            for page, title in pages
        ),
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
