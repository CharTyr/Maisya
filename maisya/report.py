"""Research report artifact builder wrapping Intelligent UI rendering."""

from __future__ import annotations

import base64
import html
from typing import Any

from .pathsafe import split_rel
from .viz_spec import check_spec_numbers, load_viz_css, render, validate
from .workspaces import regular_open

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 8 * 1024 * 1024

_ALLOWED_IMAGE_ROOTS = frozenset({"in", "project", "out"})
_FORBIDDEN_OUTPUT_ROOTS = frozenset({"in", "project", "cache"})

_CSP_PAGE = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'"
)
_CSP_CARD = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"

_PAGE_THEME_CSS = """
:root,
html[data-theme="light"] {
  --bg: #f8fafc;
  --bg-elev: #f1f5f9;
  --card-bg: #ffffff;
  --ink: #0f172a;
  --ink-2: #334155;
  --muted: #64748b;
  --border: rgba(15, 23, 42, 0.12);
  --accent: #2563eb;
}

html[data-theme="dark"] {
  --bg: #0b1220;
  --bg-elev: #162033;
  --card-bg: #111827;
  --ink: #f1f5f9;
  --ink-2: #cbd5e1;
  --muted: #94a3b8;
  --border: rgba(248, 250, 252, 0.14);
  --accent: #60a5fa;
}

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--ink);
  -webkit-font-smoothing: antialiased;
}

.report-page {
  box-sizing: border-box;
  max-width: 920px;
  margin: 0 auto;
  padding: 28px 20px 48px;
}

.report-card {
  box-sizing: border-box;
  width: 780px;
  max-width: 100%;
  margin: 0 auto;
  padding: 24px 26px;
  background: var(--bg);
}
""".strip()

_TABS_JS = """(function(){
  document.querySelectorAll(".vs-tabs").forEach(function(box){
    var radios = box.querySelectorAll(":scope > .vs-tab-radio");
    var labels = box.querySelectorAll(":scope > .vs-tab-bar .vs-tab-lbl");
    var panels = box.querySelectorAll(":scope > .vs-tab-panels > .vs-tab-panel");
    function sync(idx){
      labels.forEach(function(lbl, i){
        var on = i === idx;
        lbl.classList.toggle("is-active", on);
        lbl.setAttribute("aria-selected", on ? "true" : "false");
      });
      panels.forEach(function(panel, i){
        panel.classList.toggle("is-active", i === idx);
      });
    }
    radios.forEach(function(radio, idx){
      radio.addEventListener("change", function(){
        if (radio.checked) sync(idx);
      });
    });
  });
})();"""


def _normalize_report_path(raw: Any) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "out/report.html"
    if not isinstance(raw, str):
        raise ValueError("path 必须为字符串")
    parts = split_rel(raw.strip())
    if parts[0] in _FORBIDDEN_OUTPUT_ROOTS:
        raise ValueError(f"报告产物仅允许写入 out/*.html，禁止写入 {parts[0]}/")
    if parts[0] != "out":
        parts = ("out", *parts)
    if (
        len(parts) < 2
        or not parts[-1].lower().endswith(".html")
        or parts[-1].lower() == ".html"
    ):
        raise ValueError("报告产物路径必须以 .html 结尾且位于 out/ 下")
    return "/".join(parts)


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("仅支持有效的 PNG/JPEG/WebP/GIF 图片魔数")


def _make_image_resolver(work: Any):
    work_root = getattr(work, "path", None) or getattr(work, "root", None)
    if work_root is None:
        raise ValueError("工作区对象缺少 path")
    total_bytes = 0

    def resolve(rel_path: str) -> str:
        nonlocal total_bytes
        parts = split_rel(rel_path.strip())
        if len(parts) < 2 or parts[0] not in _ALLOWED_IMAGE_ROOTS:
            raise ValueError(f"image 路径必须位于 in/、project/ 或 out/ 内: {rel_path!r}")
        normalized = "/".join(parts)
        try:
            with regular_open(work_root, normalized) as fp:
                raw = fp.read(MAX_IMAGE_BYTES + 1)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError(f"无法读取工作区图片 {normalized}: {exc}") from exc

        if not raw:
            raise ValueError(f"图片文件为空: {normalized}")
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"单张图片不能超过 3 MiB: {normalized}")
        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("报告内嵌图片累计大小不能超过 8 MiB")

        mime = _image_mime(raw)
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    return resolve


def render_report_artifact(work: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate Intelligent UI blocks, inline local images, and write an HTML report to out/."""
    if not isinstance(arguments, dict):
        raise ValueError("render_report 参数必须为对象")

    blocks = arguments.get("blocks")
    spec: dict[str, Any] = {"blocks": blocks}
    title = arguments.get("title")
    subtitle = arguments.get("subtitle")
    caption = arguments.get("caption")
    if isinstance(title, str) and title.strip():
        spec["title"] = title.strip()
    elif title is not None and not isinstance(title, str):
        raise ValueError("title 必须为字符串")
    if isinstance(subtitle, str) and subtitle.strip():
        spec["subtitle"] = subtitle.strip()
    elif subtitle is not None and not isinstance(subtitle, str):
        raise ValueError("subtitle 必须为字符串")
    elif isinstance(caption, str) and caption.strip():
        spec["caption"] = caption.strip()

    validate(spec, profile="report")

    source_text = arguments.get("source_text")
    if source_text is not None and not isinstance(source_text, str):
        raise ValueError("source_text 必须为字符串")
    if isinstance(source_text, str) and source_text.strip():
        ok, reason = check_spec_numbers(spec, source_text)
        if not ok:
            raise ValueError(reason)

    theme = arguments.get("theme") or "light"
    if theme not in {"light", "dark"}:
        raise ValueError("theme 仅支持 light 或 dark")

    layout = arguments.get("layout") or "page"
    if layout not in {"page", "card"}:
        raise ValueError("layout 仅支持 page 或 card")

    out_path = _normalize_report_path(arguments.get("path"))
    is_card = layout == "card"
    resolver = _make_image_resolver(work)
    body_html = render(spec, static=is_card, image_resolver=resolver)

    page_title = spec.get("title") or "调研报告"
    csp = _CSP_CARD if is_card else _CSP_PAGE
    viewport = "width=780, initial-scale=1" if is_card else "width=device-width, initial-scale=1"
    container_cls = "report-card" if is_card else "report-page"
    script_html = ""
    if not is_card and 'class="vs-tabs"' in body_html:
        script_html = f"\n<script>{_TABS_JS}</script>"

    doc = (
        "<!doctype html>\n"
        f'<html lang="zh-CN" data-theme="{html.escape(theme, quote=True)}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f'<meta name="viewport" content="{viewport}">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
        f"<title>{html.escape(page_title)}</title>\n"
        "<style>\n"
        f"{_PAGE_THEME_CSS}\n"
        f"{load_viz_css()}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        f'<main class="{container_cls}">\n'
        f"{body_html}\n"
        "</main>"
        f"{script_html}\n"
        "</body>\n"
        "</html>\n"
    )

    written = work.write(out_path, doc)
    work.commit()
    return {
        "path": written["path"],
        "bytes": written["bytes"],
        "block_count": len(blocks),
        "layout": layout,
    }
