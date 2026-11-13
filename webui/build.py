"""共用管理页源码构建为宿主所需的自包含 HTML。"""

import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    template = (root / "management.html.in").read_text()
    for page, title in (
        ("abilities", "能力"),
        ("memory", "记忆"),
        ("settings", "设置"),
    ):
        body = (root / "fragments" / f"{page}.html").read_text().rstrip("\n")
        rendered = (
            template.replace("{{PAGE_ID}}", page)
            .replace("{{TITLE}}", title)
            .replace("{{BODY}}", body)
        )
        target = root / f"{page}.html"
        if "--check" in sys.argv:
            if not target.exists() or target.read_text() != rendered:
                raise SystemExit(f"{target.name} 需要重新构建")
        else:
            target.write_text(rendered)


if __name__ == "__main__":
    main()
