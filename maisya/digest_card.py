"""离线资讯简报卡片渲染、封面筛选与主色提取模块。

仅由 maisya/digest.py 导入调用，实现：
1. 封面图质量判定与 JPEG data URI 压缩编码（过滤 Logo/图标/文字横幅/异常尺寸）
2. 48x48 8 色量化主导色提取与浅底染底调色板生成
3. 5 档 Apple-Weather 天光主题 600px 离线 HTML 卡片生成（内嵌 viz.css 与分类 SVG 徽章，零外链与脚本）
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from html import escape
import io
import re
from typing import Any
from urllib.parse import urlparse

try:
    from PIL import Image, ImageFilter, ImageOps
except ImportError:  # pragma: no cover - runtime graceful fallback
    Image = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


_SHANGHAI_TZ = timezone(timedelta(hours=8))
_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

_SCRIPT_TAG_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

_URL_BAD_SUBSTRINGS = (
    "logo",
    "favicon",
    "avatar",
    "placeholder",
    "brand",
    "sprite",
    "badge",
    "apple-touch-icon",
    "og-default",
    "share-default",
)
_URL_BAD_TOKENS = (
    "icon",
    "default",
)

# 确定性星点分布：(left%, top%, size_px, opacity)
_STARS: tuple[tuple[int, int, int, float], ...] = (
    (8, 12, 2, 0.72),
    (16, 26, 1, 0.50),
    (24, 9, 2, 0.64),
    (33, 21, 1, 0.45),
    (42, 14, 2, 0.68),
    (51, 8, 1, 0.55),
    (59, 25, 2, 0.60),
    (67, 15, 1, 0.50),
    (74, 29, 2, 0.70),
    (82, 11, 1, 0.52),
    (89, 22, 2, 0.66),
    (93, 35, 1, 0.44),
    (13, 41, 1, 0.38),
    (29, 36, 2, 0.48),
    (47, 33, 1, 0.40),
    (63, 40, 1, 0.36),
)

_SKIES: dict[str, dict[str, Any]] = {
    "dawn": {
        "slot_word": "凌晨",
        "dark": False,
        "sky_bg": "linear-gradient(180deg, #0b1026 0%, #1e2749 48%, #4a3b5c 82%, #7c5268 100%)",
        "glow": "radial-gradient(ellipse at 72% 72%, rgba(232, 148, 132, 0.34) 0%, rgba(232, 148, 132, 0) 62%)",
        "orb": "moon",
        "stars": True,
        "accent": "#7c5268",
        "why_bg": "#f6eff3",
        "why_ink": "#5b2d47",
    },
    "morning": {
        "slot_word": "早上",
        "dark": False,
        "sky_bg": "linear-gradient(180deg, #1b5da3 0%, #3b84cd 52%, #7bb6ea 100%)",
        "glow": "radial-gradient(circle at 82% 24%, rgba(255, 240, 198, 0.42) 0%, rgba(255, 240, 198, 0) 56%)",
        "orb": "sun",
        "stars": False,
        "accent": "#1b5da3",
        "why_bg": "#eef5fc",
        "why_ink": "#18497d",
    },
    "noon": {
        "slot_word": "中午",
        "dark": False,
        "sky_bg": "linear-gradient(180deg, #0b61b8 0%, #2b8be0 54%, #6db9f2 100%)",
        "glow": "radial-gradient(circle at 76% 18%, rgba(255, 249, 219, 0.48) 0%, rgba(255, 249, 219, 0) 54%)",
        "orb": "sun",
        "stars": False,
        "accent": "#0b61b8",
        "why_bg": "#edf6fd",
        "why_ink": "#0f4c8a",
    },
    "afternoon": {
        "slot_word": "下午",
        "dark": False,
        "sky_bg": "linear-gradient(180deg, #27588c 0%, #5c6e8f 46%, #b67a69 80%, #db9b68 100%)",
        "glow": "radial-gradient(circle at 80% 38%, rgba(255, 206, 142, 0.46) 0%, rgba(255, 206, 142, 0) 58%)",
        "orb": "sun",
        "stars": False,
        "accent": "#b8642b",
        "why_bg": "#fbf3ec",
        "why_ink": "#7c4217",
    },
    "night": {
        "slot_word": "晚上",
        "dark": True,
        "sky_bg": "linear-gradient(180deg, #070b18 0%, #101935 54%, #1d2b53 100%)",
        "glow": "radial-gradient(circle at 82% 24%, rgba(164, 192, 255, 0.24) 0%, rgba(164, 192, 255, 0) 58%)",
        "orb": "moon",
        "stars": True,
        "accent": "#3b5bdb",
        "why_bg": "#eff2f9",
        "why_ink": "#1f3161",
    },
}

_CATEGORY_THEMES: dict[str, tuple[str, str, str]] = {
    "ai": (
        "#eef2ff",
        "#4338ca",
        '<path d="M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M18.5 15.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1z" fill="currentColor"/>',
    ),
    "code": (
        "#ecfdf5",
        "#047857",
        '<path d="M8.5 8L4 12l4.5 4M15.5 8L20 12l-4.5 4M13.2 6l-2.4 12" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>',
    ),
    "game": (
        "#fff1f2",
        "#be123c",
        '<rect x="3.5" y="7.5" width="17" height="10" rx="5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8 10.5v4M6 12.5h4M15.2 11.5h.01M17.2 13.5h.01" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    ),
    "hardware": (
        "#fffbeb",
        "#b45309",
        '<rect x="7" y="7" width="10" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    ),
    "guide": (
        "#ecfeff",
        "#0e7490",
        '<path d="M4.5 5.5h6a2.5 2.5 0 0 1 2.5 2.5v11a2 2 0 0 0-2-2h-6.5V5.5zm15 0h-6A2.5 2.5 0 0 0 11 8v11a2 2 0 0 1 2-2h6.5V5.5z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
    ),
    "explore": (
        "#f3e8ff",
        "#7e22ce",
        '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.8"/><polygon points="15.5,8.5 13.6,13.6 8.5,15.5 10.4,10.4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>',
    ),
    "news": (
        "#f0f9ff",
        "#0369a1",
        '<rect x="4" y="5" width="16" height="14" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M8 9.5h8M8 13h8M8 16.2h5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
    ),
}


def _host_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def clean_source_label(
    raw_source: Any, *, url: str = "", feed_title: str = ""
) -> str:
    """清洗来源名称，过滤长评语与内部打分文本，必要时回退到源名或域名。"""
    candidate = str(raw_source or "").strip()
    if candidate:
        try:
            float(candidate)
            candidate = ""
        except ValueError:
            pass
    if candidate:
        too_long = len(candidate) > 28
        prose_like = candidate.count(" ") >= 3 and "." not in candidate
        if not too_long and not prose_like:
            return candidate
    feed_clean = str(feed_title or "").strip()
    if feed_clean and len(feed_clean) <= 28:
        return feed_clean
    host = _host_of(url)
    return host or feed_clean or "未知来源"


def _flatten_rgb(im: Any) -> Any:
    """将含透明通道或调色板的图像展平到白底 RGB。"""
    if im.mode in ("RGBA", "LA") or (
        im.mode == "P" and "transparency" in getattr(im, "info", {})
    ):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[3])
        return bg
    if im.mode != "RGB":
        return im.convert("RGB")
    return im


def _url_rejection_reason(url: str) -> str:
    low = str(url or "").strip().lower()
    if not low:
        return ""
    parsed = urlparse(low)
    path_and_query = parsed.path or low
    if parsed.query:
        path_and_query = f"{path_and_query}?{parsed.query}"
    if path_and_query.split("?", 1)[0].endswith(".svg") or ".svg?" in path_and_query:
        return "SVG 矢量图不作为封面"
    for kw in _URL_BAD_SUBSTRINGS:
        if kw in path_and_query:
            return f"URL 含非封面关键词 {kw}"
    tokens = [t for t in _TOKEN_SPLIT_RE.split(path_and_query) if t]
    for tok in tokens:
        if tok in _URL_BAD_TOKENS:
            return f"URL 含图标或占位标识 {tok}"
    return ""


def judge_cover(raw_bytes: bytes, url: str = "") -> str:
    """判定图片字节与 URL 是否适合作为文章封面；合格返回空串，拒收返回原因。"""
    url_reason = _url_rejection_reason(url)
    if url_reason:
        return url_reason
    if not raw_bytes:
        return "空图片数据"
    head = raw_bytes.lstrip()[:256].lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "SVG 内容不作为封面"
    if Image is None:
        return "缺少 Pillow 图像处理库"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as opened:
            im = (
                ImageOps.exif_transpose(opened)
                if ImageOps is not None
                else opened.copy()
            )
            w, h = im.size
            if w < 160 or h < 90:
                return f"图片尺寸过小 ({w}x{h})"
            if h <= 0 or w <= 0:
                return "无效图片尺寸"
            if (w / h) > 3.2 or (h / w) > 2.2:
                return f"宽高比异常 ({w}x{h})"

            rgb = _flatten_rgb(im)
            resample = getattr(Image, "Resampling", Image).BILINEAR
            small = rgb.resize((128, 128), resample)
            quant_method = getattr(getattr(Image, "Quantize", Image), "MEDIANCUT", 0)
            q = small.quantize(colors=16, method=quant_method)
            counts = q.getcolors(maxcolors=32) or []
            total = 128 * 128
            dominant_share = (
                max((cnt for cnt, _ in counts), default=0) / total if total else 0.0
            )
            if dominant_share >= 0.45:
                return f"纯色或 Logo 图占比过高 ({dominant_share:.2f})"
            if 0.30 <= dominant_share < 0.45:
                edge_share = _edge_pixel_share(small)
                if edge_share >= 0.025:
                    return (
                        f"纯底文字横幅特征 (dominant={dominant_share:.2f}, edge={edge_share:.3f})"
                    )
    except Exception as exc:
        return f"图片解码失败: {exc}"
    return ""


def _edge_pixel_share(rgb_128: Any) -> float:
    """计算 128x128 灰度图中高对比度硬边缘像素占比。"""
    gray = rgb_128.convert("L").tobytes()
    width = 128
    height = 128
    edges = 0
    threshold = 42
    for y in range(height - 1):
        row = y * width
        next_row = (y + 1) * width
        for x in range(width - 1):
            p = gray[row + x]
            if (
                abs(p - gray[row + x + 1]) >= threshold
                or abs(p - gray[next_row + x]) >= threshold
            ):
                edges += 1
    return edges / (width * height)


def tint_palette(rgb: tuple[int, int, int]) -> dict[str, str]:
    """根据提取的 RGB 主色调生成适合白底卡片的染底配色。"""
    r, g, b = (
        max(0, min(255, int(rgb[0]))),
        max(0, min(255, int(rgb[1]))),
        max(0, min(255, int(rgb[2]))),
    )
    # 90% 混白得到的实色浅底 + 半透明 glow，保证文字可读与质感
    bg_r = round(r * 0.11 + 255 * 0.89)
    bg_g = round(g * 0.11 + 255 * 0.89)
    bg_b = round(b * 0.11 + 255 * 0.89)

    lum = 0.299 * r + 0.587 * g + 0.114 * b
    scale = 88.0 / lum if lum > 88.0 else 0.72
    scale = min(scale, 0.78)
    ink_r = max(12, min(140, round(r * scale)))
    ink_g = max(12, min(140, round(g * scale)))
    ink_b = max(12, min(140, round(b * scale)))

    return {
        "why_bg": f"#{bg_r:02x}{bg_g:02x}{bg_b:02x}",
        "why_ink": f"#{ink_r:02x}{ink_g:02x}{ink_b:02x}",
        "glow": f"rgba({r}, {g}, {b}, 0.22)",
    }


def cover_tint(rgb_bytes_or_img: bytes | bytearray | Any) -> dict[str, str] | None:
    """从图片字节或 PIL Image 提取主色并生成染底调色板，纯黑白灰图返回 None。"""
    if Image is None:
        return None
    opened_inside = False
    im: Any = None
    try:
        if isinstance(rgb_bytes_or_img, (bytes, bytearray, memoryview)):
            im = Image.open(io.BytesIO(bytes(rgb_bytes_or_img)))
            opened_inside = True
        elif isinstance(rgb_bytes_or_img, Image.Image):
            im = rgb_bytes_or_img
        else:
            return None

        rgb = _flatten_rgb(im)
        resample = getattr(Image, "Resampling", Image).BILINEAR
        tiny = rgb.resize((48, 48), resample)
        quant_method = getattr(getattr(Image, "Quantize", Image), "MEDIANCUT", 0)
        q = tiny.quantize(colors=8, method=quant_method).convert("RGB")
        counts = q.getcolors(maxcolors=64) or []
        best_score = -1.0
        best_rgb: tuple[int, int, int] | None = None
        for count, color in counts:
            r, g, b = int(color[0]), int(color[1]), int(color[2])
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            sat = max(r, g, b) - min(r, g, b)
            if lum > 232 or lum < 24 or sat < 32:
                continue
            score = float(count) * float(sat)
            if score > best_score:
                best_score = score
                best_rgb = (r, g, b)
        if best_rgb is None:
            return None
        return tint_palette(best_rgb)
    except Exception:
        return None
    finally:
        if opened_inside and im is not None:
            try:
                im.close()
            except Exception:
                pass


def encode_cover_data_uri(
    raw_bytes: bytes,
    url: str = "",
    *,
    max_width: int = 1200,
    quality: int = 84,
) -> tuple[str, bool]:
    """校验并压缩封面为 JPEG data URI；返回 (data_uri, cover_small)。拒收时返回 ("", False)。"""
    if judge_cover(raw_bytes, url=url):
        return "", False
    if Image is None:
        return "", False
    try:
        with Image.open(io.BytesIO(raw_bytes)) as opened:
            im = (
                ImageOps.exif_transpose(opened)
                if ImageOps is not None
                else opened.copy()
            )
            orig_w, orig_h = im.size
            cover_small = orig_w < 480
            rgb = _flatten_rgb(im)
            if orig_w > max_width and max_width > 0:
                target_h = max(1, round(orig_h * (max_width / orig_w)))
                resample = getattr(Image, "Resampling", Image).LANCZOS
                rgb = rgb.resize((max_width, target_h), resample)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=int(quality), optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}", cover_small
    except Exception:
        return "", False


def _recompress_data_uri(data_uri: str, *, max_width: int = 800, quality: int = 68) -> str:
    """在 HTML 体积逼近 512KB 上限时进一步压小单张封面图。"""
    if Image is None or not data_uri.startswith("data:image/"):
        return data_uri
    try:
        _, b64 = data_uri.split(",", 1)
        raw = base64.b64decode(b64)
        with Image.open(io.BytesIO(raw)) as im:
            rgb = _flatten_rgb(im)
            w, h = rgb.size
            if w > max_width:
                resample = getattr(Image, "Resampling", Image).LANCZOS
                rgb = rgb.resize((max_width, max(1, round(h * max_width / w))), resample)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality, optimize=True)
            return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
    except Exception:
        return data_uri


def match_sky(slot_label: str = "", *, now_ts: float | None = None) -> tuple[str, dict[str, Any]]:
    """按 slot_label 关键词或 UTC+8 当前小时匹配 5 档天光主题。"""
    label = str(slot_label or "").strip()
    if label:
        if "凌晨" in label:
            return "dawn", _SKIES["dawn"]
        if "下午" in label or "傍晚" in label or "暮" in label:
            return "afternoon", _SKIES["afternoon"]
        if "早" in label or "晨" in label:
            return "morning", _SKIES["morning"]
        if "午" in label:
            return "noon", _SKIES["noon"]
        if "晚" in label or "夜" in label:
            return "night", _SKIES["night"]

    dt = datetime.fromtimestamp(now_ts, _SHANGHAI_TZ) if now_ts is not None else datetime.now(_SHANGHAI_TZ)
    hour = dt.hour
    if 0 <= hour < 5:
        key = "dawn"
    elif 5 <= hour < 11:
        key = "morning"
    elif 11 <= hour < 14:
        key = "noon"
    elif 14 <= hour < 18:
        key = "afternoon"
    else:
        key = "night"
    return key, _SKIES[key]


def _ago(published_ts: float | None, now_ts: float) -> str:
    if published_ts is None:
        return ""
    try:
        diff = max(0.0, float(now_ts) - float(published_ts))
    except (TypeError, ValueError):
        return ""
    if diff < 60:
        return "刚刚"
    minutes = int(diff // 60)
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = int(diff // 3600)
    if hours < 24:
        return f"{hours}小时前"
    days = int(diff // 86400)
    if days == 1:
        return "昨天"
    if days < 30:
        return f"{days}天前"
    dt = datetime.fromtimestamp(float(published_ts), _SHANGHAI_TZ)
    return f"{dt.month}月{dt.day}日"


def _pick_category_key(item: dict[str, Any]) -> str:
    icon = str(item.get("icon") or "").strip().lower()
    if icon in _CATEGORY_THEMES:
        return icon
    kind = str(item.get("kind") or "").strip().lower()
    if kind == "guide":
        return "guide"
    if kind == "explore":
        return "explore"
    hay = " ".join(
        [
            str(item.get("topic") or ""),
            str(item.get("title") or ""),
            " ".join(str(k) for k in (item.get("keywords") or [])),
        ]
    ).lower()
    if any(k in hay for k in ("ai", "agent", "llm", "模型", "智能体", "推理")):
        return "ai"
    if any(k in hay for k in ("game", "indie", "游戏", "steam", "像素", "主机")):
        return "game"
    if any(k in hay for k in ("hardware", "chip", "gpu", "硬件", "芯片", "显卡", "外设")):
        return "hardware"
    if any(k in hay for k in ("rust", "python", "go", "code", "dev", "开源", "架构", "编程", "工程")):
        return "code"
    if any(k in hay for k in ("教程", "指南", "guide", "实操")):
        return "guide"
    if kind == "bridge" or item.get("bridge"):
        return "explore"
    return "news"


def _category_badge_html(item: dict[str, Any]) -> str:
    key = _pick_category_key(item)
    bg, fg, svg_inner = _CATEGORY_THEMES.get(key, _CATEGORY_THEMES["news"])
    return (
        f'<span class="badge" style="background:{bg};color:{fg};" aria-hidden="true">'
        f'<svg viewBox="0 0 24 24" width="20" height="20">{svg_inner}</svg>'
        f"</span>"
    )


def _item_tags(item: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    kind = str(item.get("kind") or "").strip().lower()
    if item.get("bridge") or kind == "explore":
        tags.append("跨界视角")
    topic = str(item.get("topic") or "").strip()
    if topic and topic not in tags:
        tags.append(topic[:8])
    if len(tags) < 2:
        for kw in item.get("keywords") or ():
            k = str(kw or "").strip()
            if k and k not in tags:
                tags.append(k[:8])
                if len(tags) >= 2:
                    break
    return tags[:2]


def _card_css(viz_css: str, sky: dict[str, Any]) -> str:
    accent = str(sky.get("accent") or "#0a84ff")
    return f"""
:root {{
  --bg: #f2f2f7;
  --bg-elev: #ffffff;
  --card-bg: #f5f6f9;
  --fill: #f0f2f6;
  --fill-2: #e5e7eb;
  --ink: #1c1c1e;
  --ink-2: #48484a;
  --muted: #8e8e93;
  --line: rgba(0, 0, 0, 0.08);
  --accent: {accent};
}}
*, *::before, *::after {{ box-sizing: border-box; }}
html, body {{
  margin: 0;
  padding: 0;
  width: 600px;
  background: #f2f2f7;
  color: #1c1c1e;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  -webkit-font-smoothing: antialiased;
}}
.card {{
  width: 600px;
  margin: 0 auto;
  background: #f2f2f7;
  color: #1c1c1e;
  position: relative;
  overflow: hidden;
  padding-bottom: 20px;
}}
.sky {{
  position: relative;
  min-height: 296px;
  padding: 40px 28px 132px;
  color: #ffffff;
  overflow: hidden;
  background: {sky["sky_bg"]};
}}
.sky .glow {{
  position: absolute;
  inset: 0;
  background: {sky["glow"]};
  pointer-events: none;
}}
.sky .stars {{
  position: absolute;
  inset: 0;
  pointer-events: none;
}}
.sky .star {{
  position: absolute;
  border-radius: 50%;
  background: #ffffff;
}}
.orb {{
  position: absolute;
  top: 34px;
  right: 36px;
  width: 64px;
  height: 64px;
  border-radius: 50%;
  pointer-events: none;
}}
.orb.sun {{
  background: radial-gradient(circle at 35% 35%, #fff9dc 0%, #ffd578 58%, rgba(255, 196, 92, 0.72) 100%);
  box-shadow: 0 0 36px 12px rgba(255, 224, 140, 0.42);
}}
.orb.moon {{
  background: radial-gradient(circle at 36% 34%, #fffdf6 0%, #f3efe2 46%, #d8d2c2 100%);
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.08), 0 0 60px 20px rgba(200, 200, 255, 0.18), inset -10px -12px 22px rgba(120, 110, 150, 0.22);
}}
.orb.moon::before {{
  content: "";
  position: absolute;
  width: 16px;
  height: 16px;
  left: 54%;
  top: 28%;
  border-radius: 50%;
  background: rgba(170, 160, 150, 0.18);
}}
.orb.moon::after {{
  content: "";
  position: absolute;
  width: 10px;
  height: 10px;
  left: 30%;
  top: 60%;
  border-radius: 50%;
  background: rgba(170, 160, 150, 0.16);
}}
.sky-fade {{
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 120px;
  background: linear-gradient(180deg, rgba(242, 242, 247, 0) 0%, #f2f2f7 100%);
  pointer-events: none;
}}
.sky-inner {{
  position: relative;
  z-index: 2;
}}
.eyebrow {{
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: rgba(255, 255, 255, 0.86);
}}
.slot {{
  margin: 0;
  font-size: 78px;
  line-height: 1.02;
  font-weight: 800;
  letter-spacing: -0.03em;
  color: #ffffff;
  text-shadow: 0 2px 14px rgba(0, 0, 0, 0.16);
}}
.lede {{
  margin: 12px 0 0;
  font-size: 15px;
  line-height: 1.45;
  color: rgba(255, 255, 255, 0.92);
}}
.lede b {{
  font-weight: 700;
  color: #ffffff;
}}
.list {{
  position: relative;
  z-index: 3;
  margin-top: -100px;
  padding: 0 18px 8px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}}
.item {{
  background: #ffffff;
  border-radius: 24px;
  padding: 20px 20px 18px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04), 0 8px 24px rgba(18, 24, 40, 0.06);
}}
.list .item:first-child {{
  box-shadow: 0 0 0 0.5px rgba(0, 0, 0, 0.05), 0 4px 14px rgba(10, 10, 40, 0.08), 0 26px 52px -20px var(--card-glow, rgba(18, 24, 40, 0.24));
}}
.item.has-cover {{
  padding: 0;
  overflow: hidden;
}}
.item-body {{
  padding: 18px 20px 18px;
}}
.frame {{
  position: relative;
  width: 100%;
  aspect-ratio: 2 / 1;
  border-radius: 16px;
  overflow: hidden;
  margin-bottom: 14px;
  background: #eef0f4;
}}
.item.has-cover .frame {{
  border-radius: 0;
  margin-bottom: 0;
}}
.item.lead .frame {{
  aspect-ratio: 16 / 9;
}}
.frame > img {{
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}}
.frame.small > img.bg {{
  position: absolute;
  inset: -10%;
  width: 120%;
  height: 120%;
  object-fit: cover;
  filter: blur(18px) brightness(0.92);
  transform: scale(1.06);
}}
.frame.small > img.fg {{
  position: relative;
  z-index: 1;
  width: 100%;
  height: 100%;
  object-fit: contain;
}}
.item-head {{
  display: flex;
  align-items: flex-start;
  gap: 12px;
}}
.badge {{
  flex: 0 0 38px;
  width: 38px;
  height: 38px;
  border-radius: 11px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-top: 2px;
}}
.item-title {{
  margin: 0;
  font-size: 20px;
  line-height: 1.32;
  font-weight: 700;
  color: #16161a;
  letter-spacing: -0.015em;
  word-break: break-word;
}}
.item.lead .item-title {{
  font-size: 25px;
  line-height: 1.28;
}}
.item-summary {{
  margin: 10px 0 0;
  font-size: 15.5px;
  line-height: 1.56;
  color: #48484a;
  word-break: break-word;
}}
.item.lead .item-summary {{
  font-size: 17px;
  line-height: 1.58;
  color: #2c2c2e;
}}
.why {{
  margin: 12px 0 0;
  padding: 10px 13px;
  border-radius: 14px;
  font-size: 14px;
  line-height: 1.5;
  font-weight: 500;
}}
.why.why-sub {{
  background: #f5f6f9;
  color: #3a3a3c;
}}
.why-k {{
  display: inline-block;
  font-size: 11.5px;
  font-weight: 700;
  letter-spacing: 0.03em;
  padding: 2px 7px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.72);
  margin-right: 8px;
  vertical-align: 1px;
}}
.why.why-sub .why-k {{
  background: rgba(0, 0, 0, 0.06);
  color: #48484a;
}}
.card-viz {{
  margin: 14px 0 0;
}}
.meta {{
  margin-top: 13px;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: #8e8e93;
}}
.meta-src {{
  font-weight: 600;
  color: #636366;
}}
.meta-dot {{
  color: #c7c7cc;
}}
.tags {{
  margin-left: auto;
  display: inline-flex;
  gap: 6px;
}}
.tag {{
  padding: 2px 9px;
  border-radius: 999px;
  background: #f2f2f7;
  color: #48484a;
  font-size: 11.5px;
  font-weight: 600;
}}
.empty-state {{
  background: #ffffff;
  border-radius: 24px;
  padding: 28px 22px;
  text-align: center;
  color: #636366;
  font-size: 15px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04), 0 8px 24px rgba(18, 24, 40, 0.06);
}}
.foot {{
  margin-top: 14px;
  padding: 4px 24px 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: #8e8e93;
}}
.foot-brand {{
  font-weight: 800;
  color: #636366;
  letter-spacing: 0.14em;
}}
{viz_css}
/* 保证白卡内的图表组件在夜间天光主题下仍采用高对比浅底配色 */
.item .card-viz .vs {{
  --vs-surface: #f5f6f9;
  --vs-card: #f5f6f9;
  --vs-bg-elev: #f5f6f9;
  --vs-fill: #ebeef4;
  --vs-fill-2: #dde2ec;
  --vs-ink: #16161a;
  --vs-ink-2: #48484a;
  --vs-muted: #8e8e93;
  --vs-line: rgba(0, 0, 0, 0.08);
  --vs-accent: {accent};
  background: transparent;
  color: var(--vs-ink);
  border-radius: 0;
  padding: 0;
  border: none;
}}
"""


def _render_item_html(
    item: dict[str, Any],
    *,
    is_lead: bool,
    sky: dict[str, Any],
    now_ts: float,
) -> str:
    title = escape(str(item.get("title") or "").strip())
    summary = escape(str(item.get("brief") or item.get("summary") or "").strip())
    why_text = escape(
        str(item.get("why") or item.get("note") or item.get("bridge") or "").strip()
    )
    cover_data = str(item.get("cover_data") or "").strip()
    has_cover = cover_data.startswith("data:image/")
    cover_small = bool(item.get("cover_small"))
    viz_frag = str(item.get("viz_frag") or "").strip()

    visual_html = ""
    if has_cover:
        src = escape(cover_data, quote=True)
        if cover_small:
            visual_html = (
                '<div class="frame small">'
                f'<img class="bg" src="{src}" alt="">'
                f'<img class="fg" src="{src}" alt="">'
                "</div>"
            )
        else:
            visual_html = f'<div class="frame"><img src="{src}" alt=""></div>'
    elif viz_frag:
        visual_html = f'<div class="viz card-viz">{viz_frag}</div>'

    badge_html = "" if has_cover else _category_badge_html(item)
    title_html = f'<div class="item-head">{badge_html}<h2 class="item-title">{title}</h2></div>'
    summary_html = f'<p class="item-summary">{summary}</p>' if summary else ""

    pal = item.get("cover_tint") if (has_cover and isinstance(item.get("cover_tint"), dict)) else None
    glow = pal.get("glow") if isinstance(pal, dict) else ""
    card_style = f' style="--card-glow:{escape(str(glow), quote=True)};"' if glow else ""

    why_html = ""
    if why_text:
        if is_lead:
            lead_pal = pal or sky
            why_bg = escape(str(lead_pal.get("why_bg") or sky["why_bg"]), quote=True)
            why_ink = escape(str(lead_pal.get("why_ink") or sky["why_ink"]), quote=True)
            why_html = (
                f'<p class="why" style="background:{why_bg};color:{why_ink};">'
                f'<span class="why-k">值得看</span>{why_text}</p>'
            )
        else:
            why_html = f'<p class="why why-sub"><span class="why-k">值得看</span>{why_text}</p>'

    src_label = clean_source_label(
        item.get("source"),
        url=str(item.get("url") or ""),
        feed_title=str(item.get("feed_title") or ""),
    )
    kind = str(item.get("kind") or "").strip().lower()
    ago_str = "" if kind == "guide" else _ago(item.get("published_ts"), now_ts)
    meta_parts = [f'<span class="meta-src">{escape(src_label)}</span>']
    if ago_str:
        meta_parts.append('<span class="meta-dot">·</span>')
        meta_parts.append(f"<span>{escape(ago_str)}</span>")

    tags = _item_tags(item)
    if tags:
        pills = "".join(f'<span class="tag">{escape(t)}</span>' for t in tags)
        meta_parts.append(f'<span class="tags">{pills}</span>')
    meta_html = f'<div class="meta">{"".join(meta_parts)}</div>'

    cls = "item lead" if is_lead else "item"
    if has_cover:
        return (
            f'<article class="{cls} has-cover"{card_style}>'
            f'{visual_html}<div class="item-body">{title_html}{summary_html}{why_html}{meta_html}</div>'
            "</article>"
        )
    viz_html = f'<div class="viz card-viz">{viz_frag}</div>' if viz_frag else ""
    return f'<article class="{cls}">{title_html}{summary_html}{viz_html}{why_html}{meta_html}</article>'


def _build_html_once(
    items: list[dict[str, Any]],
    *,
    sky_key: str,
    sky: dict[str, Any],
    eyebrow_text: str,
    slot_word: str,
    lede_html: str,
    foot_right: str,
    viz_css: str,
    now_ts: float,
) -> str:
    stars_html = ""
    if sky.get("stars"):
        dots = "".join(
            f'<i class="star" style="left:{lx}%;top:{ty}%;width:{sz}px;height:{sz}px;opacity:{op};"></i>'
            for lx, ty, sz, op in _STARS
        )
        stars_html = f'<div class="stars" aria-hidden="true">{dots}</div>'

    orb_kind = "moon" if sky.get("orb") == "moon" else "sun"
    orb_html = f'<div class="orb {orb_kind}" aria-hidden="true"></div>'
    dark_attr = ' data-theme="dark"' if sky.get("dark") else ""

    if items:
        cards_html = "\n".join(
            _render_item_html(it, is_lead=(idx == 0), sky=sky, now_ts=now_ts)
            for idx, it in enumerate(items)
        )
    else:
        cards_html = '<div class="empty-state">本轮暂无入选资讯条目</div>'

    css = _card_css(viz_css, sky)
    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=600, initial-scale=1.0">\n'
        "<title>Maisya 资讯简报</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n"
        "<body>\n"
        f'<main class="card" data-sky="{escape(sky_key, quote=True)}"{dark_attr}>\n'
        f'<header class="sky" data-sky="{escape(sky_key, quote=True)}"{dark_attr}>\n'
        '<div class="glow" aria-hidden="true"></div>\n'
        f"{stars_html}\n"
        f"{orb_html}\n"
        '<div class="sky-inner">\n'
        f'<p class="eyebrow">{escape(eyebrow_text)}</p>\n'
        f'<h1 class="slot">{escape(slot_word)}</h1>\n'
        f'<p class="lede">{lede_html}</p>\n'
        "</div>\n"
        '<div class="sky-fade" aria-hidden="true"></div>\n'
        "</header>\n"
        '<section class="list">\n'
        f"{cards_html}\n"
        "</section>\n"
        '<footer class="foot">\n'
        '<span class="foot-brand">MAISYA</span>\n'
        f"<span>{escape(foot_right)}</span>\n"
        "</footer>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )
    return _SCRIPT_TAG_RE.sub("", doc)


def build_card_html(
    items: list[dict[str, Any]],
    *,
    slot_label: str = "",
    group_title: str = "",
    total_candidates: int = 0,
    viz_css: str = "",
    now_ts: float | None = None,
) -> str:
    """构建 600px 离线 Apple-Weather 天光风格资讯简报 HTML。"""
    ts = float(now_ts) if now_ts is not None else datetime.now(_SHANGHAI_TZ).timestamp()
    dt = datetime.fromtimestamp(ts, _SHANGHAI_TZ)
    sky_key, sky = match_sky(slot_label, now_ts=ts)
    slot_word = str(sky["slot_word"])

    date_part = f"{dt.month}月{dt.day}日 · 周{_WEEKDAYS[dt.weekday()]}"
    clean_slot_label = str(slot_label or "").strip()
    if clean_slot_label and clean_slot_label != slot_word:
        eyebrow_text = f"{date_part} · {clean_slot_label}"
    else:
        eyebrow_text = date_part

    shown = len(items)
    total = max(shown, int(total_candidates or 0))
    group_label = escape(str(group_title or "").strip() or "今日精选")
    lede_html = f"<b>{group_label}</b> · 本期精选 {shown} 条"
    if total > shown:
        lede_html += f" · 共找到 {total} 条"

    foot_right = (
        f"本期 {shown} 条 · 共扫描 {total} 条" if total > shown else f"本期 {shown} 条"
    )

    working = [dict(it) for it in items]
    html_doc = _build_html_once(
        working,
        sky_key=sky_key,
        sky=sky,
        eyebrow_text=eyebrow_text,
        slot_word=slot_word,
        lede_html=lede_html,
        foot_right=foot_right,
        viz_css=viz_css,
        now_ts=ts,
    )

    # 遵守 html_render 的 512KB 上限：超限时自动再压缩封面图，极端情况移除非首条封面
    if len(html_doc.encode("utf-8")) > 490_000:
        for it in working:
            cd = str(it.get("cover_data") or "")
            if cd.startswith("data:image/"):
                it["cover_data"] = _recompress_data_uri(cd, max_width=760, quality=66)
        html_doc = _build_html_once(
            working,
            sky_key=sky_key,
            sky=sky,
            eyebrow_text=eyebrow_text,
            slot_word=slot_word,
            lede_html=lede_html,
            foot_right=foot_right,
            viz_css=viz_css,
            now_ts=ts,
        )
    if len(html_doc.encode("utf-8")) > 512_000:
        for idx in range(len(working) - 1, -1, -1):
            if working[idx].get("cover_data"):
                working[idx]["cover_data"] = ""
                html_doc = _build_html_once(
                    working,
                    sky_key=sky_key,
                    sky=sky,
                    eyebrow_text=eyebrow_text,
                    slot_word=slot_word,
                    lede_html=lede_html,
                    foot_right=foot_right,
                    viz_css=viz_css,
                    now_ts=ts,
                )
                if len(html_doc.encode("utf-8")) <= 512_000:
                    break
    return html_doc
