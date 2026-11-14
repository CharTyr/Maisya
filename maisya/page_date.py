"""Deterministic publication date extraction for HTML pages and plain text."""

from __future__ import annotations

import calendar
import email.utils
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

_MIN_TS = datetime(2000, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
_MAX_FUTURE_SEC = 2 * 86400.0

# Meta property/name/itemprop keys ordered conceptually by priority (primary vs modified fallback)
_META_PRIMARY_KEYS = (
    "article:published_time",
    "og:article:published_time",
    "og:published_time",
    "published_time",
    "published_at",
    "publish_time",
    "publish_date",
    "publishdate",
    "pubdate",
    "datepublished",
    "datecreated",
    "dc.date.issued",
    "dc.date.created",
    "dcterms.issued",
    "dcterms.created",
    "dc.date",
    "sailthru.date",
    "parsely-pub-date",
    "cxenseparse:recs:publishtime",
    "weibo:article:create_at",
    "bytedance_published_time",
    "release_date",
    "created_at",
    "date",
)

_META_FALLBACK_KEYS = (
    "article:modified_time",
    "og:updated_time",
    "datemodified",
    "dc.date.modified",
    "dcterms.modified",
    "last-modified",
    "lastmod",
)

_ATTR_DATE_KEYS = (
    "datetime",
    "data-published-at",
    "data-published",
    "data-publish-time",
    "data-pubtime",
    "data-pubdate",
    "data-created-at",
    "data-created",
    "data-timestamp",
    "data-time",
    "data-date",
    "pubdate",
)

_IGNORE_CONTAINER_TAGS = {"footer", "nav", "aside"}
_IGNORE_CONTAINER_RE = re.compile(
    r"(?:comment|comments|reply|replies|footer|copyright|sidebar|widget|nav|menu|breadcrumb|related|recommend|promo|ad-|ads|login|signup)",
    re.I,
)
_PUBLISH_HINT_RE = re.compile(
    r"(?:publish|published|pubdate|pub-time|pubtime|post-date|post_date|entry-date|article-date|article-time|create-time|created|byline|posted|dateline|date|time)",
    re.I,
)
_NEGATIVE_HINT_RE = re.compile(
    r"(?:copyright|icp|备案|©|joined|member|registered|注册于|comment|评论|回复|reply|login|sign\s*in)",
    re.I,
)
_TEXT_PUBLISH_CUE_RE = re.compile(
    r"(?:发布时间|发表于|发布于|出版于|更新于|更新时间|时间\s*[:：]|日期\s*[:：]|published(?:\s+on|\s+at)?|posted(?:\s+on|\s+at)?|updated(?:\s+on|\s+at)?|date\s*:)",
    re.I,
)

# Relative date expressions
_CN_REL_RE = re.compile(
    r"(?:^|\b|\s)(?:(刚刚|刚才|片刻前)|"
    r"(半)\s*(?:个)?\s*(小时|钟头|天|日|月)前|"
    r"(\d+)\s*(秒|分钟|分|小时|钟头|天|日|周|星期|个月|月|年)前|"
    r"(今天|今日|昨天|昨日|前天)(?:\s*(\d{1,2}:\d{2}(?::\d{2})?))?)(?=$|\b|\s|[，。,.;；!！?？])"
)

_NUM_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_EN_REL_RE = re.compile(
    r"\b(?:(just\s+now|moments?\s+ago)|"
    r"(today|yesterday)(?:\s+(?:at\s+)?(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?))?|"
    r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*"
    r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|wks?|w|months?|mos?|years?|yrs?|y)\s+ago)\b",
    re.I,
)

_TEXT_DATE_PATTERNS = (
    # ISO 8601 / YYYY-MM-DD HH:MM:SS TZ
    re.compile(
        r"\b((?:19|20)\d{2}[-/](?:1[0-2]|0?[1-9])[-/](?:3[01]|[12]\d|0?[1-9])"
        r"(?:[T\s](?:2[0-3]|1\d|0?\d):[0-5]\d(?::[0-5]\d)?(?:\.\d+)?(?:\s*(?:Z|[+-]\d{2}:?\d{2}))?)?)\b"
    ),
    # Chinese dates: 2026年10月15日 14:30(:00)
    re.compile(
        r"((?:19|20)\d{2}\s*年\s*(?:1[0-2]|0?[1-9])\s*月\s*(?:3[01]|[12]\d|0?[1-9])\s*[日号]?"
        r"(?:\s*(?:2[0-3]|1\d|0?\d):[0-5]\d(?::[0-5]\d)?)?)"
    ),
    # Month DD, YYYY (optionally with time)
    re.compile(
        r"\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+(?:3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?,?\s+(?:19|20)\d{2}"
        r"(?:[,\s]+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?)?)\b",
        re.I,
    ),
    # DD Month YYYY
    re.compile(
        r"\b((?:3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?\s+"
        r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r",?\s+(?:19|20)\d{2}(?:[,\s]+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?)?)\b",
        re.I,
    ),
)

_CN_MD_RE = re.compile(
    r"(?:^|[\s:：(\[])((?:1[0-2]|0?[1-9])\s*月\s*(?:3[01]|[12]\d|0?[1-9])\s*[日号]"
    r"(?:\s*(?:2[0-3]|1\d|0?\d):[0-5]\d(?::[0-5]\d)?)?)"
)

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _resolve_now(now: float | int | datetime | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        dt = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return float(now)


def _in_valid_window(ts: float, now_ts: float) -> bool:
    return _MIN_TS <= ts <= (now_ts + _MAX_FUTURE_SEC)


def _format_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_hhmm(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?\s*$", raw, re.I)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    ss = int(m.group(3) or 0)
    ampm = (m.group(4) or "").lower()
    if ampm == "pm" and hh < 12:
        hh += 12
    elif ampm == "am" and hh == 12:
        hh = 0
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    return hh, mm, ss


def _apply_day_and_hhmm(base_ts: float, day_delta: int, hhmm: str | None) -> float:
    dt = datetime.fromtimestamp(base_ts, tz=timezone.utc) + timedelta(days=day_delta)
    parsed = _parse_hhmm(hhmm)
    if parsed is not None:
        hh, mm, ss = parsed
        dt = dt.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    else:
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return dt.timestamp()


def _parse_relative_raw(text: str, now_ts: float) -> float | None:
    s = re.sub(r"\s+", " ", text).strip()
    if not s or len(s) > 80:
        return None

    m = _CN_REL_RE.search(s)
    if m:
        just, half, half_unit, num_s, unit, day_word, hhmm = m.groups()
        if just:
            return now_ts
        if half and half_unit:
            sec_map = {"小时": 1800, "钟头": 1800, "天": 43200, "日": 43200, "月": 15 * 86400}
            return now_ts - sec_map[half_unit]
        if num_s and unit:
            n = int(num_s)
            if n <= 0 or n > 3650:
                return None
            if unit in {"秒"}:
                return now_ts - n
            if unit in {"分钟", "分"}:
                return now_ts - n * 60
            if unit in {"小时", "钟头"}:
                return now_ts - n * 3600
            if unit in {"天", "日"}:
                return now_ts - n * 86400
            if unit in {"周", "星期"}:
                return now_ts - n * 7 * 86400
            if unit in {"个月", "月"}:
                return now_ts - n * 30 * 86400
            if unit == "年":
                return now_ts - n * 365 * 86400
        if day_word:
            delta = 0 if day_word in {"今天", "今日"} else (-1 if day_word in {"昨天", "昨日"} else -2)
            return _apply_day_and_hhmm(now_ts, delta, hhmm)

    m_en = _EN_REL_RE.search(s)
    if m_en:
        just, day_word, hhmm, qty_s, unit_s = m_en.groups()
        if just:
            return now_ts
        if day_word:
            delta = 0 if day_word.lower() == "today" else -1
            return _apply_day_and_hhmm(now_ts, delta, hhmm)
        if qty_s and unit_s:
            qty_l = qty_s.lower()
            n = int(qty_l) if qty_l.isdigit() else _NUM_WORDS.get(qty_l, 0)
            if n <= 0 or n > 3650:
                return None
            u = unit_s.lower()
            if u.startswith("s"):
                return now_ts - n
            if u.startswith("m") and u not in {"mo", "mos", "month", "months"}:
                return now_ts - n * 60
            if u.startswith("h"):
                return now_ts - n * 3600
            if u.startswith("d"):
                return now_ts - n * 86400
            if u.startswith("w"):
                return now_ts - n * 7 * 86400
            if u.startswith("m"):
                return now_ts - n * 30 * 86400
            if u.startswith("y"):
                return now_ts - n * 365 * 86400
    return None


def _is_relative_label(text: str) -> bool:
    s = re.sub(r"\s+", " ", text or "").strip()
    if not s or len(s) > 48:
        return False
    return bool(_CN_REL_RE.search(s) or _EN_REL_RE.search(s))


def relative_ts(text: str, now: float | int | datetime | None = None) -> float | None:
    """Resolve a relative time string into a bounded Unix timestamp, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    now_ts = _resolve_now(now)
    ts = _parse_relative_raw(text, now_ts)
    if ts is None or not _in_valid_window(ts, now_ts):
        return None
    return ts


def _make_utc_ts(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> float | None:
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    max_day = calendar.monthrange(year, month)[1]
    if not (1 <= day <= max_day):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp()


def _parse_absolute_ts(raw: Any, now_ts: float) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        if val >= 1e12:
            val /= 1000.0
        return val if _in_valid_window(val, now_ts) else None

    s = html.unescape(str(raw)).strip()
    if not s or len(s) > 120:
        return None
    s = re.sub(r"\s+", " ", s)

    # Pure epoch timestamp string (10-digit s or 13-digit ms)
    if re.fullmatch(r"\d{10}(?:\.\d+)?", s):
        val = float(s)
        return val if _in_valid_window(val, now_ts) else None
    if re.fullmatch(r"\d{13}", s):
        val = float(s) / 1000.0
        return val if _in_valid_window(val, now_ts) else None

    # ISO-8601 style
    iso_cand = s
    if iso_cand.endswith("Z"):
        iso_cand = iso_cand[:-1] + "+00:00"
    elif re.search(r"[+-]\d{4}$", iso_cand):
        iso_cand = iso_cand[:-2] + ":" + iso_cand[-2:]
    iso_cand = re.sub(
        r"^((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})(?=$|[T\s])",
        lambda m: f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
        iso_cand,
    )
    iso_cand = re.sub(
        r"^((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})(?=$|[T\s])",
        lambda m: f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
        iso_cand,
    )
    if re.match(r"^(?:19|20)\d{2}-\d{2}-\d{2}(?:[T\s]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:[+-]\d{2}:\d{2})?)?$", iso_cand):
        try:
            dt = datetime.fromisoformat(iso_cand)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            ts = dt.timestamp()
            return ts if _in_valid_window(ts, now_ts) else None
        except ValueError:
            pass

    # Chinese full date: YYYY年M月D日 [HH:MM[:SS]]
    m_cn = re.fullmatch(
        r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?"
        r"(?:\s*(\d{1,2}:\d{2}(?::\d{2})?))?",
        s,
    )
    if m_cn:
        hhmm = _parse_hhmm(m_cn.group(4)) if m_cn.group(4) else (0, 0, 0)
        if hhmm is not None:
            ts = _make_utc_ts(int(m_cn.group(1)), int(m_cn.group(2)), int(m_cn.group(3)), *hhmm)
            if ts is not None and _in_valid_window(ts, now_ts):
                return ts

    # Chinese short date without year: M月D日 [HH:MM[:SS]]
    m_cn_short = re.fullmatch(
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号](?:\s*(\d{1,2}:\d{2}(?::\d{2})?))?",
        s,
    )
    if m_cn_short:
        hhmm = _parse_hhmm(m_cn_short.group(3)) if m_cn_short.group(3) else (0, 0, 0)
        if hhmm is not None:
            cur_year = datetime.fromtimestamp(now_ts, tz=timezone.utc).year
            for yr in (cur_year, cur_year - 1):
                ts = _make_utc_ts(yr, int(m_cn_short.group(1)), int(m_cn_short.group(2)), *hhmm)
                if ts is not None and _in_valid_window(ts, now_ts):
                    return ts

    # English Month DD, YYYY
    m_en1 = re.fullmatch(
        r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{2})"
        r"(?:[,\s]+(?:at\s+)?(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?))?",
        s,
        re.I,
    )
    if m_en1:
        mon = _MONTHS.get(m_en1.group(1).lower())
        hhmm = _parse_hhmm(m_en1.group(4)) if m_en1.group(4) else (0, 0, 0)
        if mon and hhmm is not None:
            ts = _make_utc_ts(int(m_en1.group(3)), mon, int(m_en1.group(2)), *hhmm)
            if ts is not None and _in_valid_window(ts, now_ts):
                return ts

    # English DD Month YYYY
    m_en2 = re.fullmatch(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+((?:19|20)\d{2})"
        r"(?:[,\s]+(?:at\s+)?(\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]m)?))?",
        s,
        re.I,
    )
    if m_en2:
        mon = _MONTHS.get(m_en2.group(2).lower())
        hhmm = _parse_hhmm(m_en2.group(4)) if m_en2.group(4) else (0, 0, 0)
        if mon and hhmm is not None:
            ts = _make_utc_ts(int(m_en2.group(3)), mon, int(m_en2.group(1)), *hhmm)
            if ts is not None and _in_valid_window(ts, now_ts):
                return ts

    # RFC 2822 / HTTP-date fallback
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            ts = dt.timestamp()
            return ts if _in_valid_window(ts, now_ts) else None
    except (TypeError, ValueError, OverflowError):
        pass

    return None


def normalize_date(raw: Any, now: float | int | datetime | None = None) -> str:
    """Normalize a raw date value or relative expression into UTC ISO-8601, or ''."""
    now_ts = _resolve_now(now)
    ts = _parse_absolute_ts(raw, now_ts)
    if ts is None and isinstance(raw, str):
        ts = relative_ts(raw, now=now_ts)
    if ts is None:
        return ""
    return _format_utc(ts)


def _extract_date_from_line(line: str, now_ts: float) -> str:
    if not line or _NEGATIVE_HINT_RE.search(line):
        return ""
    for pat in _TEXT_DATE_PATTERNS:
        m = pat.search(line)
        if m:
            norm = normalize_date(m.group(1), now=now_ts)
            if norm:
                return norm
    m_short = _CN_MD_RE.search(line)
    if m_short:
        norm = normalize_date(m_short.group(1), now=now_ts)
        if norm:
            return norm
    if _is_relative_label(line) or _TEXT_PUBLISH_CUE_RE.search(line):
        rel = relative_ts(line, now=now_ts)
        if rel is not None:
            return _format_utc(rel)
    return ""


def published_from_text(text: str, now: float | int | datetime | None = None) -> str:
    """Extract a publication date from plain/markdown page text."""
    if not isinstance(text, str) or not text.strip():
        return ""
    now_ts = _resolve_now(now)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text[:6000].splitlines()]
    lines = [ln for ln in lines if ln][:80]

    # Pass 1: lines carrying an explicit publish cue
    for ln in lines:
        if len(ln) > 200 or not _TEXT_PUBLISH_CUE_RE.search(ln):
            continue
        found = _extract_date_from_line(ln, now_ts)
        if found:
            return found

    # Pass 2: concise metadata/byline lines near the top of the document
    for ln in lines[:25]:
        if len(ln) > 120:
            continue
        found = _extract_date_from_line(ln, now_ts)
        if found:
            return found

    return ""


def _walk_json_ld(obj: Any, primary: list[Any], fallback: list[Any]) -> None:
    if isinstance(obj, list):
        for item in obj:
            _walk_json_ld(item, primary, fallback)
        return
    if not isinstance(obj, dict):
        return
    for key in ("datePublished", "dateCreated", "uploadDate", "releaseDate"):
        val = obj.get(key)
        if isinstance(val, (str, int, float)):
            primary.append(val)
    mod = obj.get("dateModified")
    if isinstance(mod, (str, int, float)):
        fallback.append(mod)
    for k, v in obj.items():
        if k not in {"datePublished", "dateCreated", "uploadDate", "releaseDate", "dateModified"}:
            if isinstance(v, (dict, list)):
                _walk_json_ld(v, primary, fallback)


class _PageDateHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_candidates: dict[str, list[str]] = {}
        self.ld_json_blobs: list[str] = []
        self.time_candidates: list[tuple[bool, bool, str, str]] = []
        self.attr_candidates: list[tuple[bool, bool, str, str, str]] = []
        self.text_chunks: list[str] = []

        self._stack: list[tuple[str, bool, bool]] = []
        self._in_ignored_script = False
        self._in_ld_json = False
        self._ld_buf: list[str] = []
        self._capture_stack: list[dict[str, Any]] = []

    def _in_ignore_zone(self) -> bool:
        return any(ignored for _, ignored, _ in self._stack)

    def _has_publish_context(self) -> bool:
        return any(pub for _, _, pub in self._stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}

        if tag_l == "meta":
            key = (
                attr_map.get("property")
                or attr_map.get("name")
                or attr_map.get("itemprop")
                or attr_map.get("http-equiv")
                or ""
            ).lower()
            content = attr_map.get("content") or ""
            if key and content:
                self.meta_candidates.setdefault(key, []).append(content)
            return

        if tag_l == "script":
            stype = attr_map.get("type", "").lower()
            if "ld+json" in stype:
                self._in_ld_json = True
                self._ld_buf = []
            else:
                self._in_ignored_script = True
            return

        if tag_l in {"style", "noscript", "svg", "template"}:
            self._in_ignored_script = True
            return

        meta_str = " ".join(
            part
            for part in (
                attr_map.get("id", ""),
                attr_map.get("class", ""),
                attr_map.get("role", ""),
                attr_map.get("aria-label", ""),
                attr_map.get("itemprop", ""),
            )
            if part
        )
        is_ignored = (tag_l in _IGNORE_CONTAINER_TAGS) or bool(
            meta_str and _IGNORE_CONTAINER_RE.search(meta_str)
        )
        is_pub = bool(meta_str and _PUBLISH_HINT_RE.search(meta_str))
        if attr_map.get("itemprop", "").lower() in {"datepublished", "datecreated"}:
            is_pub = True

        self._stack.append((tag_l, is_ignored, is_pub))

        ignored_zone = self._in_ignore_zone()
        pub_ctx = self._has_publish_context()

        if tag_l == "time":
            dt_val = attr_map.get("datetime") or attr_map.get("content") or ""
            title_val = attr_map.get("title") or ""
            self._capture_stack.append(
                {
                    "kind": "time",
                    "depth": len(self._stack),
                    "ignored": ignored_zone,
                    "pub": pub_ctx or True,
                    "val": dt_val or title_val,
                    "title": title_val,
                    "buf": [],
                }
            )
            return

        # Direct attributes on elements
        direct_val = ""
        for k in _ATTR_DATE_KEYS:
            if attr_map.get(k):
                direct_val = attr_map[k]
                break
        if attr_map.get("itemprop", "").lower() in {"datepublished", "datecreated"}:
            direct_val = direct_val or attr_map.get("content", "")

        title_val = attr_map.get("title") or ""
        if direct_val or (title_val and not ignored_zone):
            self._capture_stack.append(
                {
                    "kind": "attr",
                    "depth": len(self._stack),
                    "ignored": ignored_zone,
                    "pub": pub_ctx,
                    "val": direct_val,
                    "title": title_val,
                    "buf": [],
                }
            )
        elif pub_ctx and not ignored_zone and len(self._stack) <= 12:
            self._capture_stack.append(
                {
                    "kind": "attr",
                    "depth": len(self._stack),
                    "ignored": ignored_zone,
                    "pub": True,
                    "val": "",
                    "title": "",
                    "buf": [],
                }
            )

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l == "script":
            if self._in_ld_json:
                self.ld_json_blobs.append("".join(self._ld_buf))
                self._ld_buf = []
                self._in_ld_json = False
            self._in_ignored_script = False
            return
        if tag_l in {"style", "noscript", "svg", "template"}:
            self._in_ignored_script = False
            return

        while self._stack:
            popped_tag, _, _ = self._stack.pop()
            while (
                self._capture_stack
                and len(self._stack) < self._capture_stack[-1]["depth"]
            ):
                cap = self._capture_stack.pop()
                inner = re.sub(r"\s+", " ", "".join(cap["buf"])).strip()
                if cap["kind"] == "time":
                    self.time_candidates.append(
                        (cap["ignored"], cap["pub"], cap["val"], inner)
                    )
                else:
                    self.attr_candidates.append(
                        (cap["ignored"], cap["pub"], cap["val"], cap["title"], inner)
                    )
            if popped_tag == tag_l:
                break

        if tag_l in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "header", "section", "article"}:
            self.text_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._ld_buf.append(data)
            return
        if self._in_ignored_script:
            return
        for cap in self._capture_stack:
            cap["buf"].append(data)
        if not self._in_ignore_zone():
            self.text_chunks.append(data)


def published_from_html(html_text: str, now: float | int | datetime | None = None) -> str:
    """Extract a validated UTC ISO-8601 publication timestamp from HTML."""
    if not isinstance(html_text, str) or not html_text.strip():
        return ""
    now_ts = _resolve_now(now)
    parser = _PageDateHTMLParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass

    # 1. Primary <meta> tags
    for key in _META_PRIMARY_KEYS:
        for val in parser.meta_candidates.get(key, ()):
            norm = normalize_date(val, now=now_ts)
            if norm:
                return norm

    # 2. JSON-LD blocks
    ld_primary: list[Any] = []
    ld_fallback: list[Any] = []
    for blob in parser.ld_json_blobs:
        raw_blob = blob.strip()
        if not raw_blob:
            continue
        try:
            data = json.loads(raw_blob)
        except Exception:
            continue
        _walk_json_ld(data, ld_primary, ld_fallback)
    for val in ld_primary:
        norm = normalize_date(val, now=now_ts)
        if norm:
            return norm

    # 3. <time> elements (prefer non-ignored, publish-context first)
    for want_ignored in (False, True):
        for ignored, _pub, val, inner in parser.time_candidates:
            if ignored != want_ignored:
                continue
            for cand in (val, inner):
                if cand and not _NEGATIVE_HINT_RE.search(cand):
                    norm = normalize_date(cand, now=now_ts)
                    if norm:
                        return norm

    # 4. Data/title attributes and relative-time labels with publish context
    for want_pub in (True, False):
        for ignored, pub, val, title, inner in parser.attr_candidates:
            if ignored or pub != want_pub:
                continue
            if inner and _NEGATIVE_HINT_RE.search(inner):
                continue
            if val:
                norm = normalize_date(val, now=now_ts)
                if norm:
                    return norm
            if title and (pub or _is_relative_label(inner)):
                norm = normalize_date(title, now=now_ts)
                if norm:
                    return norm
            if inner and (pub or _is_relative_label(inner)) and len(inner) <= 80:
                norm = normalize_date(inner, now=now_ts)
                if norm:
                    return norm

    # 5. Modified <meta> / JSON-LD fallback
    for key in _META_FALLBACK_KEYS:
        for val in parser.meta_candidates.get(key, ()):
            norm = normalize_date(val, now=now_ts)
            if norm:
                return norm
    for val in ld_fallback:
        norm = normalize_date(val, now=now_ts)
        if norm:
            return norm

    # 6. Extracted page text fallback
    plain = "".join(parser.text_chunks)
    if plain.strip():
        return published_from_text(plain, now=now_ts)
    return ""


__all__ = [
    "normalize_date",
    "published_from_html",
    "published_from_text",
    "relative_ts",
]
