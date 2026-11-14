"""Intelligent UI shared chart & report visualization spec, validator, and renderer."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
import html
import math
from pathlib import Path
import re
from typing import Any


class SpecError(ValueError):
    """Raised when an Intelligent UI spec violates schema or profile rules."""


COMMON_PROFILES = ("news", "report")
REPORT_ONLY_PROFILES = ("report",)

# Component catalog: allowed profiles & brief purpose.
CATALOG: dict[str, dict[str, Any]] = {
    "stat": {"profiles": COMMON_PROFILES, "desc": "单个核心指标卡"},
    "stats": {"profiles": COMMON_PROFILES, "desc": "多指标网格"},
    "compare": {"profiles": COMMON_PROFILES, "desc": "2~6 项并排对比"},
    "bars": {"profiles": COMMON_PROFILES, "desc": "柱状比较图"},
    "line": {"profiles": COMMON_PROFILES, "desc": "折线趋势图"},
    "share": {"profiles": COMMON_PROFILES, "desc": "占比构成条"},
    "timeline": {"profiles": COMMON_PROFILES, "desc": "时间线事件"},
    "table": {"profiles": COMMON_PROFILES, "desc": "结构化数据表"},
    "list": {"profiles": COMMON_PROFILES, "desc": "要点或排行列表"},
    "takeaway": {"profiles": COMMON_PROFILES, "desc": "关键结论高亮卡"},
    "tabs": {"profiles": COMMON_PROFILES, "desc": "多标签视图容器"},
    "heading": {"profiles": REPORT_ONLY_PROFILES, "desc": "章节标题"},
    "text": {"profiles": REPORT_ONLY_PROFILES, "desc": "正文段落"},
    "note": {"profiles": REPORT_ONLY_PROFILES, "desc": "提示或说明框"},
    "image": {"profiles": REPORT_ONLY_PROFILES, "desc": "工作区内嵌图片"},
    "sources": {"profiles": REPORT_ONLY_PROFILES, "desc": "引用来源列表"},
}

LIMITS: dict[str, dict[str, int]] = {
    "news": {
        "blocks": 6,
        "items": 8,
        "compare": 4,
        "points": 16,
        "rows": 12,
        "cols": 6,
        "tabs": 4,
    },
    "report": {
        "blocks": 64,
        "items": 24,
        "compare": 6,
        "points": 60,
        "rows": 50,
        "cols": 10,
        "tabs": 6,
    },
}

_NUM_RE = re.compile(r"(?<![0-9A-Za-z_.])\d[\d,]*(?:\.\d+)?")
_FORBIDDEN_IMAGE_SCHEMES = ("http:", "https:", "data:", "file:", "javascript:")
_SAFE_MIME_DATA_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/webp;base64,",
    "data:image/gif;base64,",
)


@lru_cache(maxsize=1)
def load_viz_css() -> str:
    """Load the bundled viz.css stylesheet."""
    return Path(__file__).with_name("viz.css").read_text(encoding="utf-8")


CSS = load_viz_css()


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _req_str(obj: dict[str, Any], key: str, ctx: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise SpecError(f"{ctx}: 缺少非空字符串字段 '{key}'")
    return val.strip()


def _opt_str(obj: dict[str, Any], *keys: str, ctx: str = "spec") -> str:
    for key in keys:
        if key in obj and obj[key] is not None:
            val = obj[key]
            if not isinstance(val, str):
                raise SpecError(f"{ctx}: 字段 '{key}' 必须是字符串")
            stripped = val.strip()
            if stripped:
                return stripped
    return ""


def _req_scalar_text(obj: dict[str, Any], key: str, ctx: str) -> str:
    val = obj.get(key)
    if _is_number(val):
        return _fmt_num(float(val))
    if isinstance(val, str) and val.strip():
        return val.strip()
    raise SpecError(f"{ctx}: 缺少非空字段 '{key}'")


def _opt_scalar_text(obj: dict[str, Any], key: str, ctx: str) -> str:
    if key not in obj or obj[key] is None:
        return ""
    val = obj[key]
    if _is_number(val):
        return _fmt_num(float(val))
    if isinstance(val, str):
        return val.strip()
    raise SpecError(f"{ctx}: 字段 '{key}' 必须是字符串或数字")


def _fmt_num(num: float) -> str:
    if num.is_integer():
        return str(int(num))
    return f"{num:.6f}".rstrip("0").rstrip(".")


def _check_safe_rel_image_path(path: str, ctx: str) -> None:
    low = path.strip().lower()
    if not low:
        raise SpecError(f"{ctx}: 图片路径不能为空")
    for prefix in _FORBIDDEN_IMAGE_SCHEMES:
        if low.startswith(prefix):
            raise SpecError(f"{ctx}: 图片禁止使用外部或内联 URL ({prefix})")
    if path.startswith("/") or path.startswith("\\") or "://" in path:
        raise SpecError(f"{ctx}: 图片必须是工作区相对路径")
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        raise SpecError(f"{ctx}: 图片禁止使用盘符路径")
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise SpecError(f"{ctx}: 图片路径含非法段: {path!r}")


def _validate_stat_item(item: Any, ctx: str) -> None:
    if not isinstance(item, dict):
        raise SpecError(f"{ctx}: 必须为对象")
    _req_str(item, "label", ctx)
    _req_scalar_text(item, "value", ctx)
    _opt_scalar_text(item, "delta", ctx)
    _opt_str(item, "note", ctx=ctx)
    trend = item.get("trend")
    if trend is not None and trend not in {"up", "down", "flat", "neutral"}:
        raise SpecError(f"{ctx}: trend 仅支持 up/down/flat")


def _validate_block(block: Any, profile: str, ctx: str, *, depth: int = 0) -> None:
    if not isinstance(block, dict):
        raise SpecError(f"{ctx}: 区块必须为对象")
    btype = block.get("type")
    if not isinstance(btype, str) or btype not in CATALOG:
        raise SpecError(f"{ctx}: 未知组件类型 {btype!r}")
    if profile not in CATALOG[btype]["profiles"]:
        raise SpecError(f"{ctx}: 组件 '{btype}' 不允许用于 '{profile}' 档案")

    lim = LIMITS[profile]
    _opt_str(block, "title", "subtitle", "caption", ctx=ctx)

    if btype == "stat":
        _validate_stat_item(block, ctx)
        return

    if btype == "stats":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise SpecError(f"{ctx}: stats.items 必须为非空数组")
        if len(items) > lim["items"]:
            raise SpecError(f"{ctx}: stats.items 最多 {lim['items']} 项")
        for idx, item in enumerate(items):
            _validate_stat_item(item, f"{ctx}.items[{idx}]")
        return

    if btype == "compare":
        items = block.get("items")
        if items is None and isinstance(block.get("left"), dict) and isinstance(block.get("right"), dict):
            items = [block["left"], block["right"]]
        if not isinstance(items, list) or len(items) < 2:
            raise SpecError(f"{ctx}: compare.items 至少需要 2 项")
        if len(items) > lim["compare"]:
            raise SpecError(f"{ctx}: compare.items 最多 {lim['compare']} 项")
        for idx, item in enumerate(items):
            ictx = f"{ctx}.items[{idx}]"
            if not isinstance(item, dict):
                raise SpecError(f"{ictx}: 必须为对象")
            name = _opt_str(item, "name", "label", ctx=ictx)
            if not name:
                raise SpecError(f"{ictx}: 缺少 name 或 label")
            val = _opt_scalar_text(item, "value", ictx)
            pts = item.get("points")
            if pts is not None:
                if not isinstance(pts, list) or not all(isinstance(p, str) and p.strip() for p in pts):
                    raise SpecError(f"{ictx}: points 必须为非空字符串数组")
            if not val and not pts:
                raise SpecError(f"{ictx}: 需提供 value 或 points")
            _opt_str(item, "badge", ctx=ictx)
        return

    if btype == "bars":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise SpecError(f"{ctx}: bars.items 必须为非空数组")
        if len(items) > lim["items"]:
            raise SpecError(f"{ctx}: bars.items 最多 {lim['items']} 项")
        _opt_str(block, "unit", ctx=ctx)
        for idx, item in enumerate(items):
            ictx = f"{ctx}.items[{idx}]"
            if not isinstance(item, dict):
                raise SpecError(f"{ictx}: 必须为对象")
            _req_str(item, "label", ictx)
            if not _is_number(item.get("value")):
                raise SpecError(f"{ictx}: value 必须为有限数字")
            _opt_str(item, "display", "unit", "note", "tone", ctx=ictx)
        return

    if btype == "line":
        points = block.get("points")
        if not isinstance(points, list) or len(points) < 2:
            raise SpecError(f"{ctx}: line.points 至少需要 2 个点")
        if len(points) > lim["points"]:
            raise SpecError(f"{ctx}: line.points 最多 {lim['points']} 个点")
        _opt_str(block, "unit", "x_label", "y_label", ctx=ctx)
        for idx, pt in enumerate(points):
            pctx = f"{ctx}.points[{idx}]"
            if not isinstance(pt, dict):
                raise SpecError(f"{pctx}: 必须为对象")
            x_val = _opt_scalar_text(pt, "x", pctx) or _opt_scalar_text(pt, "label", pctx)
            if not x_val:
                raise SpecError(f"{pctx}: 缺少 x 或 label")
            y_raw = pt["y"] if "y" in pt else pt.get("value")
            if not _is_number(y_raw):
                raise SpecError(f"{pctx}: y/value 必须为有限数字")
        return

    if btype == "share":
        items = block.get("items")
        if not isinstance(items, list) or len(items) < 2:
            raise SpecError(f"{ctx}: share.items 至少需要 2 项")
        if len(items) > lim["items"]:
            raise SpecError(f"{ctx}: share.items 最多 {lim['items']} 项")
        _opt_str(block, "unit", ctx=ctx)
        total = 0.0
        for idx, item in enumerate(items):
            ictx = f"{ctx}.items[{idx}]"
            if not isinstance(item, dict):
                raise SpecError(f"{ictx}: 必须为对象")
            _req_str(item, "label", ictx)
            val = item.get("value")
            if not _is_number(val) or float(val) < 0:
                raise SpecError(f"{ictx}: value 必须为非负有限数字")
            total += float(val)
            _opt_str(item, "display", ctx=ictx)
        if total <= 0:
            raise SpecError(f"{ctx}: share.items 数值总和必须大于 0")
        return

    if btype == "timeline":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise SpecError(f"{ctx}: timeline.items 必须为非空数组")
        if len(items) > lim["items"]:
            raise SpecError(f"{ctx}: timeline.items 最多 {lim['items']} 项")
        for idx, item in enumerate(items):
            ictx = f"{ctx}.items[{idx}]"
            if not isinstance(item, dict):
                raise SpecError(f"{ictx}: 必须为对象")
            if not (_opt_str(item, "time", "date", ctx=ictx)):
                raise SpecError(f"{ictx}: 缺少 time 或 date")
            if not (_opt_str(item, "title", "label", ctx=ictx)):
                raise SpecError(f"{ictx}: 缺少 title 或 label")
            _opt_str(item, "detail", ctx=ictx)
        return

    if btype == "table":
        cols = block.get("columns") if "columns" in block else block.get("headers")
        if not isinstance(cols, list) or not cols or not all(isinstance(c, str) and c.strip() for c in cols):
            raise SpecError(f"{ctx}: table.columns 必须为非空字符串数组")
        if len(cols) > lim["cols"]:
            raise SpecError(f"{ctx}: table.columns 最多 {lim['cols']} 列")
        rows = block.get("rows")
        if not isinstance(rows, list) or not rows:
            raise SpecError(f"{ctx}: table.rows 必须为非空二维数组")
        if len(rows) > lim["rows"]:
            raise SpecError(f"{ctx}: table.rows 最多 {lim['rows']} 行")
        for r_idx, row in enumerate(rows):
            if not isinstance(row, (list, tuple)) or len(row) != len(cols):
                raise SpecError(f"{ctx}.rows[{r_idx}]: 列数必须等于 {len(cols)}")
            for c_idx, cell in enumerate(row):
                if cell is not None and not isinstance(cell, (str, int, float, bool)):
                    raise SpecError(f"{ctx}.rows[{r_idx}][{c_idx}]: 单元格类型非法")
        align = block.get("align")
        if align is not None:
            if not isinstance(align, list) or len(align) != len(cols) or any(
                a not in {"left", "right", "center"} for a in align
            ):
                raise SpecError(f"{ctx}: table.align 必须与列数一致且仅含 left/right/center")
        return

    if btype == "list":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise SpecError(f"{ctx}: list.items 必须为非空数组")
        if len(items) > lim["items"]:
            raise SpecError(f"{ctx}: list.items 最多 {lim['items']} 项")
        for idx, item in enumerate(items):
            ictx = f"{ctx}.items[{idx}]"
            if isinstance(item, str):
                if not item.strip():
                    raise SpecError(f"{ictx}: 列表项不能为空")
            elif isinstance(item, dict):
                k = _opt_str(item, "title", "label", "k", ctx=ictx)
                v = _opt_scalar_text(item, "text", ictx) or _opt_scalar_text(item, "value", ictx) or _opt_str(
                    item, "detail", "v", ctx=ictx
                )
                if not k and not v:
                    raise SpecError(f"{ictx}: 列表对象需包含 text/detail 或 title/label")
            else:
                raise SpecError(f"{ictx}: 列表项必须为字符串或对象")
        return

    if btype == "takeaway":
        _req_str(block, "text", ctx)
        tone = block.get("tone")
        if tone is not None and tone not in {"info", "good", "warn", "bad", "green", "amber", "red"}:
            raise SpecError(f"{ctx}: takeaway.tone 非法")
        return

    if btype == "tabs":
        if depth > 0:
            raise SpecError(f"{ctx}: tabs 不支持嵌套")
        tabs = block.get("tabs")
        if not isinstance(tabs, list) or len(tabs) < 2:
            raise SpecError(f"{ctx}: tabs.tabs 至少需要 2 个标签页")
        if len(tabs) > lim["tabs"]:
            raise SpecError(f"{ctx}: tabs.tabs 最多 {lim['tabs']} 个标签页")
        for idx, tab in enumerate(tabs):
            tctx = f"{ctx}.tabs[{idx}]"
            if not isinstance(tab, dict):
                raise SpecError(f"{tctx}: 标签页必须为对象")
            if not (_opt_str(tab, "label", "title", ctx=tctx)):
                raise SpecError(f"{tctx}: 标签页缺少 label 或 title")
            sub = tab.get("blocks")
            if not isinstance(sub, list) or not sub:
                raise SpecError(f"{tctx}: 标签页 blocks 必须为非空数组")
            for b_idx, child in enumerate(sub):
                _validate_block(child, profile, f"{tctx}.blocks[{b_idx}]", depth=depth + 1)
        return

    if btype == "heading":
        _req_str(block, "text", ctx)
        level = block.get("level", 2)
        if isinstance(level, bool) or level not in {2, 3}:
            raise SpecError(f"{ctx}: heading.level 仅支持 2 或 3")
        return

    if btype == "text":
        _req_str(block, "text", ctx)
        return

    if btype == "note":
        _req_str(block, "text", ctx)
        tone = block.get("tone", "info")
        if tone not in {"info", "warn"}:
            raise SpecError(f"{ctx}: note.tone 仅支持 info 或 warn")
        return

    if btype == "image":
        path = _req_str(block, "path", ctx)
        _check_safe_rel_image_path(path, ctx)
        _opt_str(block, "alt", "caption", ctx=ctx)
        return

    if btype == "sources":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            raise SpecError(f"{ctx}: sources.items 必须为非空数组")
        if len(items) > 32:
            raise SpecError(f"{ctx}: sources.items 最多 32 条")
        for idx, item in enumerate(items):
            ictx = f"{ctx}.items[{idx}]"
            if isinstance(item, str):
                if not item.strip():
                    raise SpecError(f"{ictx}: 来源项不能为空")
            elif isinstance(item, dict):
                if not (_opt_str(item, "title", "label", ctx=ictx)):
                    raise SpecError(f"{ictx}: 来源对象缺少 title 或 label")
                _opt_str(item, "url", "note", "date", ctx=ictx)
            else:
                raise SpecError(f"{ictx}: 来源项必须为字符串或对象")
        return


def validate(spec: Any, profile: str = "report") -> dict[str, Any]:
    """Validate an Intelligent UI spec against the given profile ('news' or 'report')."""
    if profile not in LIMITS:
        raise SpecError(f"未知档案 '{profile}'，可选: {', '.join(LIMITS)}")
    if not isinstance(spec, dict):
        raise SpecError("spec 必须为对象")
    blocks = spec.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise SpecError("spec.blocks 必须为非空数组")
    max_blocks = LIMITS[profile]["blocks"]
    if len(blocks) > max_blocks:
        raise SpecError(f"spec.blocks 最多 {max_blocks} 个区块（当前 {len(blocks)} 个）")

    _opt_str(spec, "title", "subtitle", "caption", ctx="spec")
    for idx, block in enumerate(blocks):
        _validate_block(block, profile, f"blocks[{idx}]")
    return spec


def _block_texts(block: dict[str, Any], out: list[str]) -> None:
    for key in ("title", "subtitle", "caption", "unit"):
        val = block.get(key)
        if isinstance(val, str) and val.strip():
            out.append(val.strip())

    btype = block.get("type")
    if btype == "stat":
        for key in ("label", "value", "delta", "note"):
            val = block.get(key)
            if _is_number(val) or (isinstance(val, str) and val.strip()):
                out.append(str(val).strip())
    elif btype == "stats":
        for item in block.get("items") or []:
            if isinstance(item, dict):
                for key in ("label", "value", "delta", "note"):
                    val = item.get(key)
                    if _is_number(val) or (isinstance(val, str) and val.strip()):
                        out.append(str(val).strip())
    elif btype == "compare":
        items = block.get("items")
        if items is None and isinstance(block.get("left"), dict) and isinstance(block.get("right"), dict):
            items = [block["left"], block["right"]]
        for item in items or []:
            if isinstance(item, dict):
                for key in ("name", "label", "value", "badge"):
                    val = item.get(key)
                    if _is_number(val) or (isinstance(val, str) and val.strip()):
                        out.append(str(val).strip())
                for pt in item.get("points") or []:
                    if isinstance(pt, str) and pt.strip():
                        out.append(pt.strip())
    elif btype == "bars":
        for item in block.get("items") or []:
            if isinstance(item, dict):
                for key in ("label", "value", "display", "unit", "note"):
                    val = item.get(key)
                    if _is_number(val) or (isinstance(val, str) and val.strip()):
                        out.append(str(val).strip())
    elif btype == "line":
        for key in ("x_label", "y_label"):
            val = block.get(key)
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
        for pt in block.get("points") or []:
            if isinstance(pt, dict):
                for key in ("x", "label", "y", "value"):
                    val = pt.get(key)
                    if _is_number(val) or (isinstance(val, str) and val.strip()):
                        out.append(str(val).strip())
    elif btype == "share":
        for item in block.get("items") or []:
            if isinstance(item, dict):
                for key in ("label", "value", "display"):
                    val = item.get(key)
                    if _is_number(val) or (isinstance(val, str) and val.strip()):
                        out.append(str(val).strip())
    elif btype == "timeline":
        for item in block.get("items") or []:
            if isinstance(item, dict):
                for key in ("time", "date", "title", "label", "detail"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        out.append(val.strip())
    elif btype == "table":
        cols = block.get("columns") if "columns" in block else block.get("headers")
        for col in cols or []:
            if isinstance(col, str) and col.strip():
                out.append(col.strip())
        for row in block.get("rows") or []:
            if isinstance(row, (list, tuple)):
                for cell in row:
                    if cell is not None and not isinstance(cell, bool):
                        s = str(cell).strip()
                        if s:
                            out.append(s)
                    elif isinstance(cell, bool):
                        out.append("true" if cell else "false")
    elif btype == "list":
        for item in block.get("items") or []:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for key in ("title", "label", "k", "text", "value", "detail", "v"):
                    val = item.get(key)
                    if _is_number(val) or (isinstance(val, str) and val.strip()):
                        out.append(str(val).strip())
    elif btype in {"takeaway", "heading", "text", "note"}:
        val = block.get("text")
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
    elif btype == "image":
        for key in ("alt", "caption"):
            val = block.get(key)
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
    elif btype == "sources":
        for item in block.get("items") or []:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for key in ("title", "label", "note", "date"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        out.append(val.strip())
    elif btype == "tabs":
        for tab in block.get("tabs") or []:
            if isinstance(tab, dict):
                lbl = _opt_str(tab, "label", "title")
                if lbl:
                    out.append(lbl)
                for child in tab.get("blocks") or []:
                    if isinstance(child, dict):
                        _block_texts(child, out)


def texts_of(spec: dict[str, Any]) -> list[str]:
    """Collect all user-visible text strings from a spec for auditing/searching."""
    out: list[str] = []
    if not isinstance(spec, dict):
        return out
    for key in ("title", "subtitle", "caption"):
        val = spec.get(key)
        if isinstance(val, str) and val.strip():
            out.append(val.strip())
    for block in spec.get("blocks") or []:
        if isinstance(block, dict):
            _block_texts(block, out)
    return out


def numbers_in(text: str) -> list[float]:
    """Extract numeric magnitudes from mixed Chinese/English text."""
    if not text:
        return []
    out: list[float] = []
    for match in _NUM_RE.findall(text):
        cleaned = match.replace(",", "").strip(".")
        if not cleaned:
            continue
        try:
            val = float(cleaned)
        except ValueError:
            continue
        if math.isfinite(val):
            out.append(val)
    return out


def _allowed_numbers(source_text: str) -> list[float]:
    allowed: list[float] = []
    seen: set[float] = set()

    def add(val: float) -> None:
        if math.isfinite(val) and val not in seen:
            seen.add(val)
            allowed.append(val)

    for raw in _NUM_RE.findall(source_text or ""):
        cleaned = raw.replace(",", "").strip(".")
        if not cleaned:
            continue
        try:
            dec = Decimal(cleaned)
            flt = float(dec)
        except (InvalidOperation, ValueError):
            continue
        add(flt)
        for digits in (0, 1, 2, 3):
            add(round(flt, digits))
            try:
                quant = Decimal("1") if digits == 0 else Decimal(f"1e-{digits}")
                add(float(dec.quantize(quant, rounding=ROUND_HALF_UP)))
            except (InvalidOperation, ValueError):
                pass
    return allowed


def _is_number_allowed(num: float, allowed: list[float]) -> bool:
    if num <= 10.0:
        return True
    for cand in allowed:
        if math.isclose(num, cand, rel_tol=1e-9, abs_tol=1e-9):
            return True
    return False


def check_numbers(texts: Iterable[str] | str, source_text: str) -> tuple[bool, str]:
    """Verify every displayed number > 10 appears (exactly or via rounding) in source_text."""
    items = [texts] if isinstance(texts, str) else list(texts)
    allowed = _allowed_numbers(source_text or "")
    missing: list[str] = []
    seen_missing: set[str] = set()
    for text in items:
        for num in numbers_in(text):
            if not _is_number_allowed(num, allowed):
                disp = _fmt_num(num)
                if disp not in seen_missing:
                    seen_missing.add(disp)
                    missing.append(disp)
    if missing:
        return (
            False,
            f"展示数字超过 10 但未在 source_text 中核实（含四舍五入容差）: {', '.join(missing)}",
        )
    return True, ""


def check_spec_numbers(spec: dict[str, Any], source_text: str) -> tuple[bool, str]:
    """Verify every displayed number > 10 inside spec appears in source_text."""
    return check_numbers(texts_of(spec), source_text)


def _esc(val: Any) -> str:
    return html.escape(str(val), quote=True)


def _render_block_header(block: dict[str, Any]) -> str:
    parts: list[str] = []
    title = _opt_str(block, "title")
    cap = _opt_str(block, "subtitle", "caption")
    if title:
        parts.append(f'<h3 class="vs-h">{_esc(title)}</h3>')
    if cap:
        parts.append(f'<p class="vs-cap">{_esc(cap)}</p>')
    return "".join(parts)


def _render_stat(block: dict[str, Any]) -> str:
    label = _esc(block["label"])
    value = _esc(_req_scalar_text(block, "value", "stat"))
    delta = _opt_scalar_text(block, "delta", "stat")
    trend = block.get("trend") or ""
    if trend == "neutral":
        trend = "flat"
    trend_cls = f" {trend}" if trend in {"up", "down"} else ""
    note = _opt_str(block, "note")
    delta_html = f'<span class="vs-stat-d{trend_cls}">{_esc(delta)}</span>' if delta else ""
    note_html = f'<div class="vs-stat-n">{_esc(note)}</div>' if note else ""
    return (
        '<div class="vs-stat">'
        f'<div class="vs-stat-l">{label}</div>'
        f'<div class="vs-stat-row"><span class="vs-stat-v">{value}</span>{delta_html}</div>'
        f"{note_html}"
        "</div>"
    )


def _render_bars(block: dict[str, Any]) -> str:
    items = block["items"]
    global_unit = _opt_str(block, "unit")
    vals = [float(item["value"]) for item in items]
    max_abs = max((abs(v) for v in vals), default=1.0) or 1.0
    cols: list[str] = []
    for item, val in zip(items, vals):
        pct = max(0.0, min(100.0, (abs(val) / max_abs) * 100.0))
        unit = _opt_str(item, "unit") or global_unit
        disp = _opt_str(item, "display") or f"{_fmt_num(val)}{unit}"
        tone = _opt_str(item, "tone")
        tone_cls = f" tone-{tone}" if tone in {"green", "red", "amber", "purple", "teal", "muted"} else ""
        label = _esc(item["label"])
        cols.append(
            '<div class="vs-bar">'
            f'<div class="vs-bar-v">{_esc(disp)}</div>'
            '<div class="vs-bar-t">'
            f'<div class="vs-bar-b{tone_cls}" style="height:{pct:.1f}%"></div>'
            "</div>"
            f'<div class="vs-bar-l" title="{label}">{label}</div>'
            "</div>"
        )
    return _render_block_header(block) + f'<div class="vs-bars"><div class="vs-bars-row">{"".join(cols)}</div></div>'


def _render_line(block: dict[str, Any]) -> str:
    points = block["points"]
    unit = _opt_str(block, "unit")
    xs = [(_opt_scalar_text(pt, "x", "line") or _opt_scalar_text(pt, "label", "line")) for pt in points]
    ys = [float(pt["y"] if "y" in pt else pt["value"]) for pt in points]

    w, h = 600.0, 210.0
    pad_l, pad_r, pad_t, pad_b = 52.0, 24.0, 22.0, 34.0
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    y_min = min(ys)
    y_max = max(ys)
    span = y_max - y_min

    coords: list[tuple[float, float, str, float]] = []
    n = len(points)
    for idx, (xv, yv) in enumerate(zip(xs, ys)):
        px = pad_l + (plot_w * idx / max(1, n - 1))
        if span == 0:
            py = pad_t + plot_h * 0.5
        else:
            py = pad_t + plot_h * (1.0 - (yv - y_min) / span)
        coords.append((px, py, xv, yv))

    grid_lines: list[str] = []
    for ratio in (0.0, 0.5, 1.0):
        gy = pad_t + plot_h * ratio
        val = y_max - span * ratio if span != 0 else y_max
        grid_lines.append(
            f'<line class="vs-line-grid" x1="{pad_l:.1f}" y1="{gy:.1f}" x2="{w - pad_r:.1f}" y2="{gy:.1f}"/>'
        )
        grid_lines.append(
            f'<text class="vs-line-axis" x="{pad_l - 8:.1f}" y="{gy + 4:.1f}" text-anchor="end">'
            f"{_esc(_fmt_num(val) + unit)}</text>"
        )

    path_d = " ".join(
        f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}" for i, (px, py, _, _) in enumerate(coords)
    )
    base_y = pad_t + plot_h
    area_d = f"{path_d} L{coords[-1][0]:.1f},{base_y:.1f} L{coords[0][0]:.1f},{base_y:.1f} Z"

    step = max(1, (n + 5) // 6)
    x_labels: list[str] = []
    dots: list[str] = []
    for idx, (px, py, xv, yv) in enumerate(coords):
        dots.append(
            f'<circle class="vs-line-dot" cx="{px:.1f}" cy="{py:.1f}" r="3.6">'
            f"<title>{_esc(xv)}: {_esc(_fmt_num(yv) + unit)}</title></circle>"
        )
        if n <= 8:
            dots.append(
                f'<text class="vs-line-axis" x="{px:.1f}" y="{max(12.0, py - 8):.1f}" text-anchor="middle">'
                f"{_esc(_fmt_num(yv))}</text>"
            )
        if idx == 0 or idx == n - 1 or idx % step == 0:
            x_labels.append(
                f'<text class="vs-line-axis" x="{px:.1f}" y="{h - 10:.1f}" text-anchor="middle">{_esc(xv)}</text>'
            )

    svg = (
        f'<svg class="vs-line-svg" viewBox="0 0 {int(w)} {int(h)}" role="img" aria-label="{_esc(_opt_str(block, "title") or "趋势折线图")}">'
        + "".join(grid_lines)
        + f'<path class="vs-line-area" d="{area_d}"/>'
        + f'<path class="vs-line-path" d="{path_d}"/>'
        + "".join(dots)
        + "".join(x_labels)
        + "</svg>"
    )
    meta_parts: list[str] = []
    xl = _opt_str(block, "x_label")
    yl = _opt_str(block, "y_label")
    if xl or yl:
        meta_parts.append(f'<div class="vs-line-meta"><span>{_esc(yl)}</span><span>{_esc(xl)}</span></div>')
    return _render_block_header(block) + f'<div class="vs-line-box">{svg}{"".join(meta_parts)}</div>'


def _render_block(
    block: dict[str, Any],
    *,
    uid: str,
    counter: list[int],
    static: bool,
    image_resolver: Callable[[str], str] | None,
) -> str:
    btype = block["type"]

    if btype == "stat":
        return _render_block_header(block) + f'<div class="vs-card">{_render_stat(block)}</div>'

    if btype == "stats":
        cards = "".join(_render_stat(item) for item in block["items"])
        return _render_block_header(block) + f'<div class="vs-stats">{cards}</div>'

    if btype == "compare":
        items = block.get("items")
        if items is None and isinstance(block.get("left"), dict) and isinstance(block.get("right"), dict):
            items = [block["left"], block["right"]]
        cards: list[str] = []
        for item in items:
            name = _esc(_opt_str(item, "name", "label"))
            val = _opt_scalar_text(item, "value", "compare")
            badge = _opt_str(item, "badge")
            win_cls = " win" if item.get("winner") else ""
            badge_html = f'<span class="vs-cmp-badge">{_esc(badge)}</span>' if badge else ""
            val_html = f'<span class="vs-cmp-val">{_esc(val)}</span>' if val else ""
            pts = item.get("points") or []
            pts_html = (
                f'<ul class="vs-cmp-pts">{"".join(f"<li>{_esc(p)}</li>" for p in pts)}</ul>'
                if pts
                else ""
            )
            cards.append(
                f'<div class="vs-cmp-card{win_cls}">'
                f'<div class="vs-cmp-h"><span class="vs-cmp-name">{name}</span>{badge_html}{val_html}</div>'
                f"{pts_html}"
                "</div>"
            )
        return _render_block_header(block) + f'<div class="vs-cmp">{"".join(cards)}</div>'

    if btype == "bars":
        return _render_bars(block)

    if btype == "line":
        return _render_line(block)

    if btype == "share":
        items = block["items"]
        unit = _opt_str(block, "unit")
        total = sum(float(item["value"]) for item in items) or 1.0
        segs: list[str] = []
        legends: list[str] = []
        for idx, item in enumerate(items):
            val = float(item["value"])
            pct = (val / total) * 100.0
            cls = f"c{(idx % 7) + 1}"
            disp = _opt_str(item, "display") or f"{_fmt_num(val)}{unit}"
            segs.append(
                f'<div class="vs-share-seg {cls}" style="width:{pct:.2f}%" title="{_esc(item["label"])}: {_esc(disp)}"></div>'
            )
            legends.append(
                f'<div class="vs-share-item"><span class="vs-share-swatch {cls}"></span>'
                f'<span>{_esc(item["label"])}</span><strong>{_esc(disp)}</strong>'
                f'<span>({pct:.1f}%)</span></div>'
            )
        return (
            _render_block_header(block)
            + f'<div class="vs-share"><div class="vs-share-track">{"".join(segs)}</div>'
            + f'<div class="vs-share-legend">{"".join(legends)}</div></div>'
        )

    if btype == "timeline":
        rows: list[str] = []
        for item in block["items"]:
            t_str = _opt_str(item, "time", "date")
            title = _opt_str(item, "title", "label")
            detail = _opt_str(item, "detail")
            detail_html = f'<div class="vs-tl-detail">{_esc(detail)}</div>' if detail else ""
            rows.append(
                '<div class="vs-tl-row">'
                f'<div class="vs-tl-date">{_esc(t_str)}</div>'
                '<div class="vs-tl-rail"><span class="vs-tl-dot"></span></div>'
                f'<div class="vs-tl-body"><div class="vs-tl-title">{_esc(title)}</div>{detail_html}</div>'
                "</div>"
            )
        return _render_block_header(block) + f'<div class="vs-tl">{"".join(rows)}</div>'

    if btype == "table":
        cols = block.get("columns") if "columns" in block else block["headers"]
        align = block.get("align") or []
        ths: list[str] = []
        col_cls: list[str] = []
        for idx, col in enumerate(cols):
            cls = ' class="num"' if idx < len(align) and align[idx] == "right" else ""
            col_cls.append(cls)
            ths.append(f"<th{cls}>{_esc(col)}</th>")
        trs: list[str] = []
        for row in block["rows"]:
            tds: list[str] = []
            for idx, cell in enumerate(row):
                cls = col_cls[idx]
                if not cls and isinstance(cell, (int, float)) and not isinstance(cell, bool):
                    cls = ' class="num"'
                txt = "" if cell is None else ("true" if cell is True else ("false" if cell is False else str(cell)))
                tds.append(f"<td{cls}>{_esc(txt)}</td>")
            trs.append(f"<tr>{''.join(tds)}</tr>")
        return (
            _render_block_header(block)
            + '<div class="vs-tbl-wrap"><table class="vs-tbl">'
            + f'<thead><tr>{"".join(ths)}</tr></thead>'
            + f'<tbody>{"".join(trs)}</tbody>'
            + "</table></div>"
        )

    if btype == "list":
        tag = "ol" if block.get("ordered") else "ul"
        lis: list[str] = []
        for item in block["items"]:
            if isinstance(item, str):
                lis.append(f'<li class="vs-list-item">{_esc(item)}</li>')
            else:
                k = _opt_str(item, "title", "label", "k")
                v = (
                    _opt_scalar_text(item, "text", "list")
                    or _opt_scalar_text(item, "value", "list")
                    or _opt_str(item, "detail", "v")
                )
                k_html = f'<span class="vs-list-k">{_esc(k)}</span>' if k else ""
                v_html = f"<span>{_esc(v)}</span>" if v else ""
                lis.append(f'<li class="vs-list-item">{k_html}{v_html}</li>')
        return _render_block_header(block) + f'<{tag} class="vs-list">{"".join(lis)}</{tag}>'

    if btype == "takeaway":
        tone = _opt_str(block, "tone")
        tone_map = {"good": "green", "warn": "amber", "bad": "red", "green": "green", "amber": "amber", "red": "red"}
        tcls = f" tone-{tone_map[tone]}" if tone in tone_map else ""
        return _render_block_header(block) + f'<div class="vs-take{tcls}">{_esc(block["text"])}</div>'

    if btype == "tabs":
        tabs = block["tabs"]
        if static:
            sections: list[str] = []
            for tab in tabs:
                lbl = _opt_str(tab, "label", "title")
                inner = "".join(
                    _render_block(child, uid=uid, counter=counter, static=static, image_resolver=image_resolver)
                    for child in tab["blocks"]
                )
                sections.append(
                    '<section class="vs-tab-section">'
                    f'<h4 class="vs-tab-section-title">{_esc(lbl)}</h4>'
                    f'<div class="vs-stack">{inner}</div>'
                    "</section>"
                )
            return _render_block_header(block) + f'<div class="vs-flat">{"".join(sections)}</div>'

        counter[0] += 1
        group_name = f"{uid}-tab-{counter[0]}"
        radios: list[str] = []
        labels: list[str] = []
        panels: list[str] = []
        for idx, tab in enumerate(tabs):
            lbl = _opt_str(tab, "label", "title")
            rid = f"{group_name}-{idx}"
            checked = " checked" if idx == 0 else ""
            active = " is-active" if idx == 0 else ""
            radios.append(
                f'<input class="vs-tab-radio" type="radio" name="{_esc(group_name)}" id="{_esc(rid)}" data-tab-idx="{idx}"{checked}>'
            )
            labels.append(
                f'<label class="vs-tab-lbl{active}" for="{_esc(rid)}" data-tab-idx="{idx}" role="tab" aria-selected="{"true" if idx == 0 else "false"}">{_esc(lbl)}</label>'
            )
            inner = "".join(
                _render_block(child, uid=uid, counter=counter, static=static, image_resolver=image_resolver)
                for child in tab["blocks"]
            )
            panels.append(
                f'<div class="vs-tab-panel{active}" data-tab-idx="{idx}" role="tabpanel"><div class="vs-stack">{inner}</div></div>'
            )
        return (
            _render_block_header(block)
            + '<div class="vs-tabs">'
            + "".join(radios)
            + f'<div class="vs-tab-bar" role="tablist">{"".join(labels)}</div>'
            + f'<div class="vs-tab-panels">{"".join(panels)}</div>'
            + "</div>"
        )

    if btype == "heading":
        level = int(block.get("level", 2))
        tag = "h3" if level == 3 else "h2"
        return f'<{tag} class="vs-h">{_esc(block["text"])}</{tag}>'

    if btype == "text":
        return f'<p class="vs-p">{_esc(block["text"])}</p>'

    if btype == "note":
        tone = _opt_str(block, "tone") or "info"
        return f'<div class="vs-note tone-{_esc(tone)}">{_esc(block["text"])}</div>'

    if btype == "image":
        if image_resolver is None:
            raise SpecError("渲染 image 块时必须提供 image_resolver")
        data_uri = image_resolver(block["path"].strip())
        if not isinstance(data_uri, str) or not data_uri.startswith(_SAFE_MIME_DATA_PREFIXES):
            raise SpecError("image_resolver 必须返回受支持的 base64 图片 data URI")
        alt = _opt_str(block, "alt") or _opt_str(block, "caption") or Path(block["path"]).name
        cap = _opt_str(block, "caption")
        cap_html = f'<figcaption class="vs-fig-cap">{_esc(cap)}</figcaption>' if cap else ""
        return (
            '<figure class="vs-fig">'
            f'<img class="vs-img" src="{_esc(data_uri)}" alt="{_esc(alt)}">'
            f"{cap_html}"
            "</figure>"
        )

    if btype == "sources":
        title = _opt_str(block, "title") or "参考来源"
        lis: list[str] = []
        for item in block["items"]:
            if isinstance(item, str):
                lis.append(f"<li>{_esc(item)}</li>")
            else:
                stitle = _opt_str(item, "title", "label")
                date = _opt_str(item, "date")
                note = _opt_str(item, "note")
                url = _opt_str(item, "url")
                parts = [f"<span>{_esc(stitle)}</span>"]
                if date:
                    parts.append(f' <span>({_esc(date)})</span>')
                if note:
                    parts.append(f' — <span>{_esc(note)}</span>')
                if url:
                    parts.append(f' <span class="vs-src-url">{_esc(url)}</span>')
                lis.append(f"<li>{''.join(parts)}</li>")
        return f'<section class="vs-src"><div class="vs-src-title">{_esc(title)}</div><ol class="vs-src-list">{"".join(lis)}</ol></section>'

    raise SpecError(f"未实现的组件类型: {btype}")


def render(
    spec: dict[str, Any],
    uid: str = "vs",
    *,
    static: bool = False,
    image_resolver: Callable[[str], str] | None = None,
) -> str:
    """Render an Intelligent UI spec fragment without any <script> tags."""
    validate(spec, profile="report")
    root_cls = "vs vs-stack vs-static" if static else "vs vs-stack"
    parts: list[str] = []
    title = _opt_str(spec, "title")
    subtitle = _opt_str(spec, "subtitle", "caption")
    if title:
        parts.append(f'<h1 class="vs-title">{_esc(title)}</h1>')
    if subtitle:
        parts.append(f'<p class="vs-cap">{_esc(subtitle)}</p>')
    counter = [0]
    for block in spec["blocks"]:
        if not isinstance(block, dict):
            raise SpecError("blocks 元素必须为对象")
        parts.append(
            _render_block(
                block,
                uid=uid,
                counter=counter,
                static=static,
                image_resolver=image_resolver,
            )
        )
    return f'<div class="{root_cls}" id="{_esc(uid)}">{"".join(parts)}</div>'


def catalog_prompt(profile: str = "report") -> str:
    """Build concise component catalog prompt text for LLM guidance."""
    if profile not in LIMITS:
        raise SpecError(f"未知档案 '{profile}'")
    lines = [f"Intelligent UI 组件目录（profile={profile}）："]
    for name, info in CATALOG.items():
        if profile in info["profiles"]:
            lines.append(f"- {name}: {info['desc']}")
    return "\n".join(lines)
