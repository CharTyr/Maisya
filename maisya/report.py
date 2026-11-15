"""Research report artifact builder wrapping Intelligent UI rendering."""

from __future__ import annotations

import base64
import html
import io
import re
from typing import Any

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None
    ImageOps = None

from .pathsafe import split_rel
from .viz_spec import check_spec_numbers, load_viz_css, render, validate
from .workspaces import regular_open

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 8 * 1024 * 1024
MAX_CARD_HTML_BYTES = 524_288
_CARD_SAFE_TARGET_BYTES = 508_000

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


def _flatten_card_rgb(im: Any) -> Any:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    if im.mode != "RGB":
        return im.convert("RGB")
    return im


def _compress_card_image(raw: bytes, *, mime: str, target_bytes: int) -> str:
    raw_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    if len(raw) <= 8192 and len(raw_uri.encode("utf-8")) <= target_bytes:
        return raw_uri
    if Image is None:
        return raw_uri
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            w0, h0 = opened.size
            if w0 * h0 > 40_000_000:
                raise ValueError("内嵌图片像素尺寸过大")
            im = (
                ImageOps.exif_transpose(opened)
                if ImageOps is not None
                else opened.copy()
            )
            rgb = _flatten_card_rgb(im)
    except ValueError:
        raise
    except Exception:
        return raw_uri

    orig_w, orig_h = rgb.size
    resample = getattr(Image, "Resampling", Image).LANCZOS
    best_uri = ""
    best_len = 1 << 60
    max_sides = (1080, 880, 720, 560, 420, 320, 240, 180, 128)
    qualities = (84, 72, 60, 48, 36, 26)
    for max_side in max_sides:
        longest = max(orig_w, orig_h)
        if longest > max_side:
            scale = max_side / longest
            tw = max(1, round(orig_w * scale))
            th = max(1, round(orig_h * scale))
            candidate_img = rgb.resize((tw, th), resample)
        else:
            candidate_img = rgb
        for qual in qualities:
            buf = io.BytesIO()
            candidate_img.save(buf, format="JPEG", quality=qual, optimize=True)
            uri = f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
            uri_len = len(uri.encode("utf-8"))
            if uri_len < best_len:
                best_uri = uri
                best_len = uri_len
            if uri_len <= target_bytes:
                return uri
    return best_uri or raw_uri


def _make_image_resolver(
    work: Any,
    *,
    card_mode: bool = False,
    per_image_target: int = 180_000,
    raw_cache: dict[str, tuple[bytes, str]] | None = None,
):
    work_root = getattr(work, "path", None) or getattr(work, "root", None)
    if work_root is None:
        raise ValueError("工作区对象缺少 path")
    total_bytes = 0
    cache = raw_cache if raw_cache is not None else {}

    def resolve(rel_path: str) -> str:
        nonlocal total_bytes
        parts = split_rel(rel_path.strip())
        if len(parts) < 2 or parts[0] not in _ALLOWED_IMAGE_ROOTS:
            raise ValueError(f"image 路径必须位于 in/、project/ 或 out/ 内: {rel_path!r}")
        normalized = "/".join(parts)
        if normalized in cache:
            raw, mime = cache[normalized]
        else:
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

            mime = _image_mime(raw)
            cache[normalized] = (raw, mime)

        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError("报告内嵌图片累计大小不能超过 8 MiB")

        if card_mode:
            return _compress_card_image(raw, mime=mime, target_bytes=per_image_target)
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
    page_title = spec.get("title") or "调研报告"
    csp = _CSP_CARD if is_card else _CSP_PAGE
    viewport = "width=780, initial-scale=1" if is_card else "width=device-width, initial-scale=1"
    container_cls = "report-card" if is_card else "report-page"
    viz_css = load_viz_css()

    def _assemble_doc(body: str) -> str:
        script_html = ""
        if not is_card and 'class="vs-tabs"' in body:
            script_html = f"\n<script>{_TABS_JS}</script>"
        return (
            "<!doctype html>\n"
            f'<html lang="zh-CN" data-theme="{html.escape(theme, quote=True)}">\n'
            "<head>\n"
            '<meta charset="utf-8">\n'
            f'<meta name="viewport" content="{viewport}">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{csp}">\n'
            f"<title>{html.escape(page_title)}</title>\n"
            "<style>\n"
            f"{_PAGE_THEME_CSS}\n"
            f"{viz_css}\n"
            "</style>\n"
            "</head>\n"
            "<body>\n"
            f'<main class="{container_cls}">\n'
            f"{body}\n"
            "</main>"
            f"{script_html}\n"
            "</body>\n"
            "</html>\n"
        )

    raw_cache: dict[str, tuple[bytes, str]] = {}
    resolver = _make_image_resolver(
        work,
        card_mode=is_card,
        per_image_target=180_000,
        raw_cache=raw_cache,
    )
    body_html = render(spec, static=is_card, image_resolver=resolver)
    doc = _assemble_doc(body_html)

    if is_card and len(doc.encode("utf-8")) >= MAX_CARD_HTML_BYTES and raw_cache:
        uri_matches = re.findall(r"data:image/[^\"'\s>]+", body_html)
        img_count = max(1, len(uri_matches))
        img_total = sum(len(u.encode("utf-8")) for u in uri_matches)
        non_img_bytes = len(doc.encode("utf-8")) - img_total
        avail_budget = max(8_192, _CARD_SAFE_TARGET_BYTES - non_img_bytes)
        base_per_img = max(2_048, avail_budget // img_count)
        for factor in (1.0, 0.7, 0.45, 0.25, 0.12):
            tight_resolver = _make_image_resolver(
                work,
                card_mode=True,
                per_image_target=max(1_536, int(base_per_img * factor)),
                raw_cache=raw_cache,
            )
            body_html = render(spec, static=True, image_resolver=tight_resolver)
            doc = _assemble_doc(body_html)
            if len(doc.encode("utf-8")) < MAX_CARD_HTML_BYTES:
                break

    if is_card and len(doc.encode("utf-8")) >= MAX_CARD_HTML_BYTES:
        raise ValueError("卡片 HTML 体积超过 512 KiB 渲染上限，请精简图片数量或正文")

    written = work.write(out_path, doc)
    work.commit()
    return {
        "path": written["path"],
        "bytes": written["bytes"],
        "block_count": len(blocks),
        "layout": layout,
    }
