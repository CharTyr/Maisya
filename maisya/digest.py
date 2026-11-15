"""Opt-in digest workflow engine: SSRF/XXE-safe RSS state machine, 5D scoring filter & card renderer."""

import asyncio
import hashlib
import html
import inspect
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import xml.parsers.expat
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .page_date import published_from_text
from . import digest_card, viz_spec

DAYS_TO_SECS = 86400.0
UTC8_OFFSET = 8 * 3600.0


def _numeric_score(value: object) -> float | None:
    """将数字或数字字符串解析为有限浮点数；域名等非数字文本返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            num = float(stripped)
        except ValueError:
            return None
    else:
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    return num

BIG_PLATFORM_DOMAINS = frozenset(
    {
        "github.com",
        "zhihu.com",
        "zhuanlan.zhihu.com",
        "bilibili.com",
        "weibo.com",
        "xiaohongshu.com",
        "mp.weixin.qq.com",
        "x.com",
        "twitter.com",
        "reddit.com",
        "youtube.com",
        "youtu.be",
        "v2ex.com",
        "juejin.cn",
        "csdn.net",
        "36kr.com",
        "ithome.com",
        "oschina.net",
        "sspai.com",
        "douban.com",
        "medium.com",
        "substack.com",
        "news.ycombinator.com",
    }
)

PROBE_PATHS = ("/feed", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "ref",
        "spm",
        "from",
        "source",
    }
)
_GOV_SITE_RE = re.compile(r"(?:^|\.)(?:gov\.cn|chinacourt\.org|[^.]*jcy[^.]*\.cn)$", re.I)

TRIAL_DAYS = 14.0
TRIAL_CAND_CAP = 2
TRIAL_CAND_MIN = 10
NET_USELESS_MIN = 2
FETCH_FAILURE_MIN = 5
FETCH_FAILURE_DAYS = 3.0
FAILURE_COOLDOWN_DAYS = 30.0
TRUSTED_WINDOW_DAYS = 30.0
TRUSTED_HIGH_AVG = 4.0
TRUSTED_MIN_HIGH = 4
TRUSTED_MIN_DAYS = 3

NEWS_MAX_AGE_DAYS = 7.0
GUIDE_MAX_AGE_DAYS = 60.0
HISTORY_DEDUP_DAYS = 14.0
TOPIC_SATURATION_DAYS = 3.0
TOPIC_SATURATION_CAP = 3
NOVELTY_REJECT_MAX = 2.0

RSS_SOURCES_FILE = "project/rss_sources.json"
DIGEST_HISTORY_FILE = "project/digest_history.json"
DIGEST_POOL_FILE = "project/digest_pool.json"


def _is_public_ip(raw_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError:
        return False
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _validate_public_url(url: str) -> urllib.parse.ParseResult:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("RSS 地址不能为空")
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("仅支持 http/https 公开链接")
    if parsed.username or parsed.password:
        raise ValueError("拒绝包含用户名或密码的链接")
    host = (parsed.hostname or "").strip().lower()
    if not host or host == "localhost" or host.endswith(".local"):
        raise ValueError("拒绝本地或未指定主机地址")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_public_ip(str(literal)):
            raise ValueError("拒绝非公网 IP 地址")
        return parsed
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as err:
        raise ValueError(f"无法解析公开域名: {err}") from err
    if not infos:
        raise ValueError("域名未解析到有效公网地址")
    for info in infos:
        addr = info[4][0]
        if not _is_public_ip(addr):
            raise ValueError("域名解析到内网或保留 IP，已拦截")
    return parsed


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _sync_fetch_public_bytes(url: str, timeout: float, max_bytes: int) -> dict[str, Any]:
    current = url
    opener = urllib.request.build_opener(_NoRedirectHandler())
    for _hop in range(5):
        _validate_public_url(current)
        req = urllib.request.Request(
            current,
            headers={
                "User-Agent": "MaisyaDigest/1.0 (+https://example.invalid)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html, image/*;q=0.9, */*;q=0.7",
            },
        )
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                if status in {301, 302, 303, 307, 308}:
                    loc = resp.headers.get("Location", "")
                    if not loc:
                        return {
                            "body": None,
                            "content_type": "",
                            "status": status,
                            "url": current,
                            "error": f"HTTP {status} 缺少重定向目标",
                        }
                    current = urllib.parse.urljoin(current, loc)
                    continue
                body = resp.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ValueError("响应体积超出 2MB 安全上限")
                return {
                    "body": body,
                    "content_type": str(resp.headers.get("Content-Type", "")),
                    "status": status,
                    "url": str(getattr(resp, "url", current) or current),
                    "error": "",
                }
        except urllib.error.HTTPError as err:
            if err.code in {301, 302, 303, 307, 308}:
                loc = err.headers.get("Location", "") if err.headers else ""
                if loc:
                    current = urllib.parse.urljoin(current, loc)
                    continue
            return {
                "body": None,
                "content_type": "",
                "status": int(err.code),
                "url": current,
                "error": f"HTTP {err.code}",
            }
        except urllib.error.URLError as err:
            return {
                "body": None,
                "content_type": "",
                "status": 0,
                "url": current,
                "error": str(err.reason or err),
            }
    return {
        "body": None,
        "content_type": "",
        "status": 0,
        "url": current,
        "error": "重定向次数过多",
    }


async def fetch_public_bytes(
    url: str, *, timeout: float = 7.5, max_bytes: int = 2_097_152
) -> dict[str, Any]:
    _validate_public_url(url)
    return await asyncio.to_thread(_sync_fetch_public_bytes, url, timeout, max_bytes)


def _strip_html(raw: str) -> str:
    unescaped = html.unescape(raw or "")
    cleaned = re.sub(r"<[^>]+>", " ", unescaped)
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _extract_first_img_src(raw_html: str) -> str:
    if not raw_html:
        return ""
    unescaped = html.unescape(raw_html)
    m = re.search(
        r"""<img\b[^>]*?\bsrc\s*=\s*(?:"(https?://[^"\s>]+)"|'(https?://[^'\s>]+)')""",
        unescaped,
        re.IGNORECASE,
    )
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


def _extract_item_image(node: ET.Element, html_blobs: list[str]) -> str:
    """从 RSS/Atom 条目节点抽取封面图片链接（enclosure/media:content/media:thumbnail/正文 img）。"""
    for child in node:
        tag = _local_tag(child.tag)
        if tag == "enclosure":
            u = (child.attrib.get("url") or "").strip()
            t = (child.attrib.get("type") or "").strip().lower()
            if u.lower().startswith(("http://", "https://")) and (
                t.startswith("image/")
                or (not t and re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:\?|$)", u, re.I))
            ):
                return u
        elif tag == "link":
            rel = (child.attrib.get("rel") or "").strip().lower()
            t = (child.attrib.get("type") or "").strip().lower()
            href = (child.attrib.get("href") or "").strip()
            if rel == "enclosure" and href.lower().startswith(("http://", "https://")) and t.startswith("image/"):
                return href

    for elem in node.iter():
        etag = _local_tag(elem.tag)
        if etag == "thumbnail":
            u = (elem.attrib.get("url") or "").strip()
            if u.lower().startswith(("http://", "https://")):
                return u
        elif etag == "content" and "media" in elem.tag.lower():
            u = (elem.attrib.get("url") or "").strip()
            medium = (elem.attrib.get("medium") or "").strip().lower()
            t = (elem.attrib.get("type") or "").strip().lower()
            if u.lower().startswith(("http://", "https://")) and (
                medium == "image"
                or t.startswith("image/")
                or re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:\?|$)", u, re.I)
            ):
                return u

    for blob in html_blobs:
        img_url = _extract_first_img_src(blob)
        if img_url:
            return img_url
    return ""


class _MetaImageParser(HTMLParser):
    """提取文章 HTML 中的 og:image / twitter:image 地址。"""

    def __init__(self) -> None:
        super().__init__()
        self.candidates: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}
        key = (
            attr_map.get("property")
            or attr_map.get("name")
            or attr_map.get("itemprop")
            or ""
        ).lower()
        content = attr_map.get("content") or ""
        if (
            key
            in {
                "og:image",
                "og:image:secure_url",
                "og:image:url",
                "twitter:image",
                "twitter:image:src",
            }
            and content
            and key not in self.candidates
        ):
            self.candidates[key] = content


def _extract_meta_image(html_bytes: bytes | str, page_url: str) -> str:
    text = (
        html_bytes.decode("utf-8", errors="replace")
        if isinstance(html_bytes, (bytes, bytearray))
        else str(html_bytes or "")
    )
    parser = _MetaImageParser()
    try:
        parser.feed(text[:200_000])
    except Exception:
        return ""
    for key in (
        "og:image:secure_url",
        "og:image",
        "og:image:url",
        "twitter:image",
        "twitter:image:src",
    ):
        raw = parser.candidates.get(key)
        if not raw:
            continue
        resolved = urllib.parse.urljoin(page_url, html.unescape(raw).strip())
        if resolved.lower().startswith(("http://", "https://")):
            return resolved
    return ""


def _is_safe_http_url(url: str) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return True
    return _is_public_ip(str(literal))


def parse_feed_xml(
    xml_data: bytes | str,
    *,
    now: float | None = None,
    lookback_days: float = 14.0,
    max_items: int = 30,
) -> dict[str, Any]:
    raw_bytes = xml_data.encode("utf-8") if isinstance(xml_data, str) else bytes(xml_data)
    if not raw_bytes.strip():
        raise ValueError("RSS XML 内容为空")

    parser = xml.parsers.expat.ParserCreate()

    def _reject_doctype(*_args):
        raise ValueError("拒绝包含 DOCTYPE 的 XML 输入")

    parser.StartDoctypeDeclHandler = _reject_doctype
    try:
        parser.Parse(raw_bytes, True)
    except ValueError:
        raise
    except xml.parsers.expat.ExpatError as err:
        raise ValueError(f"RSS XML 解析失败: {err}") from err

    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as err:
        raise ValueError(f"RSS XML 结构无效: {err}") from err

    now_ts = time.time() if now is None else float(now)
    cutoff = now_ts - float(lookback_days) * DAYS_TO_SECS
    root_name = _local_tag(root.tag)
    feed_title = ""
    items: list[dict[str, Any]] = []

    if root_name in {"rss", "rdf"}:
        channel = next((c for c in root if _local_tag(c.tag) == "channel"), root)
        for child in channel:
            if _local_tag(child.tag) == "title":
                feed_title = _strip_html(child.text or "")
                break
        item_nodes = [c for c in channel if _local_tag(c.tag) == "item"]
        if not item_nodes and root_name == "rdf":
            item_nodes = [c for c in root if _local_tag(c.tag) == "item"]
        for node in item_nodes:
            title = link = pub_raw = summary = ""
            html_blobs: list[str] = []
            for child in node:
                tag = _local_tag(child.tag)
                text = (child.text or "").strip()
                if tag == "title" and not title:
                    title = _strip_html(text)
                elif tag == "link" and not link:
                    link = text or (child.attrib.get("href") or "").strip()
                elif tag in {"pubdate", "date", "published", "updated"} and not pub_raw:
                    pub_raw = text
                elif tag in {"description", "encoded", "summary", "content"} and "media" not in child.tag.lower():
                    raw_blob = text or "".join(ET.tostring(sub, encoding="unicode") for sub in child)
                    if raw_blob:
                        html_blobs.append(raw_blob)
                    if not summary:
                        summary = _strip_html(text or "".join(child.itertext()))
            pub_ts = _parse_timestamp(pub_raw)
            if pub_ts is not None and pub_ts < cutoff:
                continue
            if title and link:
                items.append(
                    {
                        "title": title,
                        "url": link,
                        "published": pub_raw,
                        "published_ts": pub_ts,
                        "summary": summary[:400],
                        "image_url": _extract_item_image(node, html_blobs),
                    }
                )
    elif root_name == "feed":
        for child in root:
            if _local_tag(child.tag) == "title":
                feed_title = _strip_html("".join(child.itertext()))
                break
        for node in root:
            if _local_tag(node.tag) != "entry":
                continue
            title = link = pub_raw = summary = ""
            html_blobs = []
            for child in node:
                tag = _local_tag(child.tag)
                text = "".join(child.itertext()).strip()
                if tag == "title" and not title:
                    title = _strip_html(text)
                elif tag == "link":
                    rel = (child.attrib.get("rel") or "alternate").lower()
                    href = (child.attrib.get("href") or "").strip()
                    if href and rel != "enclosure" and (not link or rel == "alternate"):
                        link = href
                elif tag in {"published", "updated"} and not pub_raw:
                    pub_raw = text
                elif tag in {"summary", "content"} and "media" not in child.tag.lower():
                    raw_blob = (child.text or "") or "".join(
                        ET.tostring(sub, encoding="unicode") for sub in child
                    )
                    if raw_blob:
                        html_blobs.append(raw_blob)
                    if not summary:
                        summary = _strip_html(text)
            pub_ts = _parse_timestamp(pub_raw)
            if pub_ts is not None and pub_ts < cutoff:
                continue
            if title and link:
                items.append(
                    {
                        "title": title,
                        "url": link,
                        "published": pub_raw,
                        "published_ts": pub_ts,
                        "summary": summary[:400],
                        "image_url": _extract_item_image(node, html_blobs),
                    }
                )
    else:
        raise ValueError("不支持的订阅 XML 根标签")

    return {"title": feed_title, "items": items[:max_items]}


class _FeedLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "link":
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs if k}
        rels = {r.strip().lower() for r in attr_map.get("rel", "").split()}
        ftype = attr_map.get("type", "").lower()
        href = attr_map.get("href", "").strip()
        if (
            href
            and "alternate" in rels
            and ("rss" in ftype or "atom" in ftype or "xml" in ftype)
        ):
            self.links.append((href, attr_map.get("title", "")))


def _is_comment_feed(href: str, title: str = "") -> bool:
    low_url = href.lower()
    low_title = title.lower()
    return (
        "/comments" in low_url
        or "comments/feed" in low_url
        or "comment" in low_title
        or "评论" in title
    )


def _normalize_url_key(url: str) -> str:
    parsed = urllib.parse.urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query_pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    ]
    path = (parsed.path or "/").rstrip("/") or "/"
    query = urllib.parse.urlencode(query_pairs)
    return f"{host}{path}" + (f"?{query}" if query else "")


def _title_tokens(title: str) -> set[str]:
    clean = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (title or "").lower()).strip()
    tokens: set[str] = set()
    for part in clean.split():
        if re.search(r"[\u4e00-\u9fff]", part):
            if len(part) <= 2:
                tokens.add(part)
            else:
                for i in range(len(part) - 1):
                    tokens.add(part[i : i + 2])
        elif len(part) >= 2:
            tokens.add(part)
    return tokens


def _title_similarity(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _same_story(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if _normalize_url_key(a.get("url", "")) == _normalize_url_key(b.get("url", "")):
        return True
    sim = _title_similarity(a.get("title", ""), b.get("title", ""))
    if sim >= 0.52:
        return True
    nums_a = set(re.findall(r"\d+(?:\.\d+)?", a.get("title", "")))
    nums_b = set(re.findall(r"\d+(?:\.\d+)?", b.get("title", "")))
    if nums_a and (nums_a & nums_b) and sim >= 0.32:
        return True
    return False


def _map_author_feed(url: str) -> tuple[str, str] | None:
    parsed = urllib.parse.urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host.endswith(".substack.com") and host not in {"www.substack.com", "substack.com"}:
        return f"https://{host}/feed", "map"
    if host == "medium.com" or host == "www.medium.com":
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0].startswith("@"):
            return f"https://medium.com/feed/{parts[0]}", "map"
    if host in {"buttondown.email", "buttondown.com"}:
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            return f"https://{host}/{parts[0]}/rss", "map"
    return None


def _host_of(url: str) -> str:
    return (urllib.parse.urlparse((url or "").strip()).hostname or "").lower()


def _is_big_platform(url: str) -> bool:
    host = _host_of(url)
    if host.startswith("www."):
        host = host[4:]
    return host in BIG_PLATFORM_DOMAINS


def _feed_id_for(url: str) -> str:
    return "rss_" + hashlib.sha256(url.encode()).hexdigest()[:12]


STARTER_FEEDS: tuple[dict[str, Any], ...] = (
    {
        "url": "https://huggingface.co/blog/feed.xml",
        "title": "Hugging Face Blog",
        "category": "ai",
        "keywords": ("ai", "llm", "agent", "ml", "model", "open-source", "人工智能", "大模型", "开源"),
    },
    {
        "url": "https://simonwillison.net/atom/everything/",
        "title": "Simon Willison's Weblog",
        "category": "ai-dev",
        "keywords": (
            "ai",
            "llm",
            "agent",
            "python",
            "sqlite",
            "dev",
            "tool",
            "indie dev",
            "indie hacker",
            "solo dev",
            "编程",
            "开发",
            "大模型",
            "独立开发",
            "独立开发者",
            "开源",
            "工具",
        ),
    },
    {
        "url": "https://hnrss.org/frontpage",
        "title": "Hacker News",
        "category": "tech-dev",
        "keywords": (
            "tech",
            "programming",
            "startup",
            "open-source",
            "ai",
            "engineering",
            "indie dev",
            "indie hacker",
            "solo dev",
            "编程",
            "开源",
            "科技",
            "技术",
            "独立开发",
            "独立开发者",
        ),
    },
    {
        "url": "https://blog.cloudflare.com/rss/",
        "title": "Cloudflare Blog",
        "category": "dev",
        "keywords": (
            "cloudflare",
            "engineering",
            "systems",
            "network",
            "security",
            "ai",
            "indie dev",
            "indie hacker",
            "solo dev",
            "独立开发",
            "独立开发者",
            "系统",
            "后端",
            "开源",
        ),
    },
    {
        "url": "https://blog.rust-lang.org/feed.xml",
        "title": "Rust Blog",
        "category": "dev",
        "keywords": (
            "rust",
            "systems",
            "engineering",
            "open-source",
            "compiler",
            "indie dev",
            "独立开发",
            "编程",
            "系统",
            "后端",
            "开源",
        ),
    },
    {
        "url": "https://go.dev/blog/feed.atom",
        "title": "The Go Blog",
        "category": "dev",
        "keywords": (
            "go",
            "golang",
            "systems",
            "backend",
            "engineering",
            "open-source",
            "indie dev",
            "独立开发",
            "编程",
            "系统",
            "后端",
            "开源",
        ),
    },
    {
        "url": "https://feeds.arstechnica.com/arstechnica/index",
        "title": "Ars Technica",
        "category": "tech-media",
        "keywords": ("tech", "hardware", "science", "ai", "security", "科技", "硬件", "科学"),
    },
    {
        "url": "https://www.gcores.com/rss",
        "title": "机核 GCORES",
        "category": "game",
        "keywords": ("game", "indie game", "gamedev", "hardware", "acg", "culture", "游戏", "独立游戏", "主机", "文化"),
    },
    {
        "url": "https://www.chuapp.com/feed",
        "title": "触乐",
        "category": "game",
        "keywords": ("game", "indie game", "gamedev", "mobile", "review", "游戏", "独立游戏", "评测", "行业"),
    },
    {
        "url": "https://www.yystv.cn/rss/feed",
        "title": "游研社",
        "category": "game",
        "keywords": ("game", "indie game", "gamedev", "retro", "hardware", "游戏", "独立游戏", "复古", "主机"),
    },
    {
        "url": "https://itch.io/blog.xml",
        "title": "itch.io Blog",
        "category": "indie-game",
        "keywords": ("indie game", "game", "gamedev", "独立游戏", "游戏开发", "像素"),
    },
    {
        "url": "https://www.rockpapershotgun.com/feed",
        "title": "Rock Paper Shotgun",
        "category": "pc-game",
        "keywords": ("pc", "game", "indie game", "gamedev", "steam", "review", "游戏", "独立游戏", "评测"),
    },
    {
        "url": "https://lobste.rs/rss",
        "title": "Lobsters",
        "category": "dev",
        "keywords": (
            "programming",
            "systems",
            "engineering",
            "rust",
            "linux",
            "security",
            "open-source",
            "indie dev",
            "独立开发",
            "编程",
            "系统",
            "后端",
            "开源",
        ),
    },
    {
        "url": "https://github.blog/feed/",
        "title": "The GitHub Blog",
        "category": "dev",
        "keywords": (
            "github",
            "dev",
            "engineering",
            "open-source",
            "copilot",
            "ai",
            "indie dev",
            "indie hacker",
            "solo dev",
            "独立开发",
            "独立开发者",
            "系统",
            "后端",
            "编程",
            "开源",
            "工程",
        ),
    },
    {
        "url": "https://sspai.com/feed",
        "title": "少数派",
        "category": "tech-life",
        "keywords": (
            "tech",
            "app",
            "hardware",
            "tool",
            "efficiency",
            "indie dev",
            "独立开发",
            "独立开发者",
            "科技",
            "效率",
            "工具",
            "硬件",
            "数码",
        ),
    },
    {
        "url": "https://lilianweng.github.io/index.xml",
        "title": "Lil'Log",
        "category": "ai-research",
        "keywords": ("ai", "llm", "agent", "research", "safety", "大模型", "智能体", "论文"),
    },
)

_ENCYCLOPEDIA_EXACT_HOSTS = frozenset(
    {
        "wikipedia.org",
        "wiktionary.org",
        "baike.baidu.com",
        "zh.moegirl.org.cn",
        "moegirl.org.cn",
        "en.moegirl.org.cn",
        "baike.sogou.com",
        "baike.so.com",
        "wiki.biligame.com",
        "fandom.com",
    }
)
_ENCYCLOPEDIA_SUFFIXES = (".wikipedia.org", ".wiktionary.org", ".fandom.com", ".moegirl.org.cn")
_ENCYCLOPEDIA_TITLE_RE = re.compile(
    r"(?:-\s*(?:维基百科|wikipedia|萌娘百科)|_百度百科|\(disambiguation\)|（消歧义）)\s*$",
    re.I,
)
_YEAR_ARCHIVE_RE = re.compile(
    r"(?:^|/)(?:year/|archives?/)?(?:19|20)\d{2}(?:/(?:0?[1-9]|1[0-2])(?:/(?:0?[1-9]|[12]\d|3[01]))?)?(?:/page/\d+|/index\.html?)?/?$",
    re.I,
)
_LISTING_SEGMENT_RE = re.compile(
    r"/(?:tags?|categor(?:y|ies)|topics?|archives?)(?:/|$)|/page(?:/\d+)?/?$",
    re.I,
)
_LOGIN_NOISE_HOST_RE = re.compile(
    r"^(?:accounts\.google\.com|(?:login|signin|auth|sso|passport)\.)",
    re.I,
)
_LOGIN_NOISE_PATH_RE = re.compile(
    r"(?:^|/)(?:login|signin|sign-in|signup|sign-up|register|logout|oauth\d*|sso|wp-login\.php|user/login|account/login|auth(?:/|$)|intent/(?:tweet|like|retweet)|sharer\.php)(?:/|$|\?)",
    re.I,
)
_LOGIN_NOISE_TITLE_RE = re.compile(
    r"^\s*(?:登录|注册|请登录|sign\s*in|log\s*in|just a moment\.{0,3}|access denied|403 forbidden|404 not found)\s*$",
    re.I,
)
_NUM_RE = re.compile(r"(?<![0-9A-Za-z_.#])\d[\d,]*(?:\.\d+)?")
_VIZ_STATIC_MAX_H = 2400
_VIZ_MAX_HTML_CHARS = 16000
_FALLBACK_VIZ_TYPES = frozenset(
    {
        "stat",
        "stats",
        "kpi",
        "metric",
        "bars",
        "bar",
        "compare",
        "timeline",
        "steps",
        "ratio",
        "spark",
        "callout",
        "table",
        "grid",
        "pills",
    }
)
_FALLBACK_VIZ_DATA_KEYS = frozenset(
    {
        "items",
        "rows",
        "cards",
        "steps",
        "left",
        "right",
        "value",
        "values",
        "text",
        "body",
        "points",
        "bars",
        "segments",
        "cells",
        "blocks",
    }
)


def _significant_numbers(text: str) -> set[float]:
    nums: set[float] = set()
    for raw in _NUM_RE.findall(str(text or "")):
        cleaned = raw.replace(",", "")
        try:
            val = float(cleaned)
        except ValueError:
            continue
        if val > 10.0 and val == val:
            nums.add(round(val, 4))
    return nums


def _has_distinct_numbers(source_text: str) -> bool:
    return len(_significant_numbers(source_text)) >= 2


def _collect_viz_strings(node: Any, *, key_name: str = "") -> list[str]:
    if key_name in {"type", "v", "color", "accent", "id", "uid", "tone", "icon", "style"}:
        return []
    if isinstance(node, bool) or node is None:
        return []
    if isinstance(node, (int, float)):
        return [str(node)]
    if isinstance(node, str):
        if re.fullmatch(r"#[0-9a-fA-F]{3,8}", node.strip()):
            return []
        return [node]
    if isinstance(node, list):
        out: list[str] = []
        for item in node:
            out.extend(_collect_viz_strings(item))
        return out
    if isinstance(node, dict):
        out = []
        for k, v in node.items():
            out.extend(_collect_viz_strings(v, key_name=str(k)))
        return out
    return []


def _check_viz_result(res: Any) -> tuple[bool, str]:
    if res is True or res is None:
        return True, ""
    if res is False:
        return False, "viz 数字未在原文出现"
    if isinstance(res, tuple) and res and isinstance(res[0], bool):
        ok = res[0]
        detail = str(res[1]) if len(res) > 1 and res[1] else ""
        return ok, ("" if ok else (detail or "viz 数字未在原文出现"))
    if isinstance(res, dict) and "ok" in res:
        ok = bool(res["ok"])
        return ok, ("" if ok else str(res.get("reason") or res.get("missing") or "viz 数字未在原文出现"))
    if isinstance(res, (list, set, tuple)):
        bad = [str(x) for x in res if x is not None]
        if bad:
            return False, f"viz 数字未在原文出现: {', '.join(bad[:5])}"
        return True, ""
    return True, ""


def _validate_and_check_viz(
    viz_raw: Any, source_text: Any
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(viz_raw, dict):
        return None, "viz 必须为对象"
    spec_cand = (
        viz_raw
        if ("blocks" in viz_raw and "type" not in viz_raw)
        else {"v": 1, "blocks": [viz_raw]}
    )

    if viz_spec is not None:
        try:
            validated = viz_spec.validate(spec_cand, "news")
        except Exception as exc:
            return None, f"viz 结构校验失败: {exc}"
        if source_text is not None:
            try:
                res = viz_spec.check_spec_numbers(validated, str(source_text))
            except Exception as exc:
                return None, f"viz 数字核验失败: {exc}"
            ok, reason = _check_viz_result(res)
            if not ok:
                return None, reason or "viz 数字未在原文出现"
        return dict(viz_raw), ""

    # Fallback validation & anti-hallucination number verification before viz_spec is merged
    blocks = (
        spec_cand.get("blocks")
        if isinstance(spec_cand.get("blocks"), list)
        else []
    )
    if not blocks or not all(isinstance(b, dict) for b in blocks):
        return None, "viz 缺少有效的 blocks"
    for blk in blocks:
        btype = str(blk.get("type") or "").strip().lower()
        if btype not in _FALLBACK_VIZ_TYPES:
            return None, f"viz 块类型不受支持: {btype or 'empty'}"
        if not any(k in blk and blk[k] not in (None, "", [], {}) for k in _FALLBACK_VIZ_DATA_KEYS):
            return None, "viz 块缺少数据字段"

    if source_text is not None:
        src_nums = _significant_numbers(str(source_text))
        viz_texts = _collect_viz_strings(spec_cand)
        viz_nums = _significant_numbers(" ".join(viz_texts))
        missing = sorted(viz_nums - src_nums)
        if missing:
            shown = ", ".join(str(int(n) if n.is_integer() else n) for n in missing[:5])
            return None, f"viz 数字未在原文出现: {shown}"
    return dict(viz_raw), ""


def _fallback_viz_css() -> str:
    return """
.vs{margin-top:10px;padding:10px 12px;border-radius:8px;background:var(--fill);border:1px solid var(--line);color:var(--ink);font-size:12.5px}
.vs-title{font-weight:700;font-size:12.5px;color:var(--ink);margin:0 0 8px}
.vs-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.vs-stat{background:var(--bg-elev);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.vs-stat-val{font-size:17px;font-weight:700;color:var(--accent);line-height:1.2}
.vs-stat-lbl{font-size:11.5px;color:var(--ink-2);margin-top:3px}
.vs-bars{display:flex;flex-direction:column;gap:6px}
.vs-bar-row{display:grid;grid-template-columns:minmax(72px,130px) 1fr auto;align-items:center;gap:8px}
.vs-bar-lbl{color:var(--ink);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vs-bar-track{height:8px;border-radius:999px;background:var(--fill-2);overflow:hidden}
.vs-bar-fill{height:100%;border-radius:999px;background:var(--accent)}
.vs-bar-val{font-family:ui-monospace,monospace;font-size:11.5px;color:var(--ink-2)}
.vs-compare{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.vs-cmp-col{background:var(--bg-elev);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.vs-cmp-head{font-weight:700;color:var(--accent);margin-bottom:4px;font-size:12px}
.vs-list{margin:0;padding-left:16px;color:var(--ink-2);font-size:12px;line-height:1.45}
.vs-steps{display:flex;flex-direction:column;gap:6px}
.vs-step{display:flex;gap:8px;align-items:flex-start}
.vs-step-dot{font-weight:700;color:var(--accent);min-width:44px;font-size:11.5px}
.vs-callout{border-left:3px solid var(--accent);padding:6px 10px;background:var(--bg-elev);border-radius:0 6px 6px 0;color:var(--ink)}
.vs-table{width:100%;border-collapse:collapse;font-size:12px}
.vs-table th,.vs-table td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:left}
.vs-table th{color:var(--ink-2);font-weight:600}
""".strip()


def _load_viz_css_text() -> str:
    css = ""
    if viz_spec is not None and hasattr(viz_spec, "load_viz_css"):
        try:
            css = str(viz_spec.load_viz_css() or "")
        except Exception:
            css = ""
    if not css:
        css_path = Path(__file__).with_name("viz.css")
        if css_path.is_file():
            try:
                css = css_path.read_text(encoding="utf-8")
            except OSError:
                css = ""
    if not css:
        css = _fallback_viz_css()
    return css.replace("</style", "<\\/style")


def _esc_br(value: Any) -> str:
    return html.escape(str(value or "")).replace("\n", "<br>")


def _render_fallback_viz_block(blk: dict[str, Any]) -> tuple[str, int]:
    btype = str(blk.get("type") or "").strip().lower()
    title = str(blk.get("title") or blk.get("label") or "").strip()
    head_html = f'<div class="vs-title">{_esc_br(title)}</div>' if title else ""
    parts: list[str] = ['<div class="vs vs-static">', head_html]
    est_h = 44 if title else 24

    if btype in {"stat", "stats", "kpi", "metric"}:
        items = blk.get("items") or blk.get("cards") or (
            [{"label": blk.get("label") or title or "", "value": blk.get("value")}]
            if blk.get("value") is not None
            else []
        )
        parts.append('<div class="vs-stats">')
        for entry in list(items)[:6]:
            if not isinstance(entry, dict):
                continue
            val = entry.get("value", "")
            unit = entry.get("unit") or ""
            lbl = entry.get("label") or entry.get("name") or ""
            parts.append(
                f'<div class="vs-stat"><div class="vs-stat-val">{_esc_br(f"{val}{unit}")}</div>'
                f'<div class="vs-stat-lbl">{_esc_br(lbl)}</div></div>'
            )
        parts.append("</div>")
        est_h += 76
    elif btype in {"bars", "bar", "ratio"}:
        items = list(blk.get("items") or blk.get("bars") or blk.get("segments") or [])[:8]
        max_v = 0.0
        parsed_rows: list[tuple[str, float, str]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            lbl = str(entry.get("label") or entry.get("name") or "")
            raw_v = entry.get("value")
            num = _numeric_score(raw_v)
            if num is None and isinstance(raw_v, str):
                m = _NUM_RE.search(raw_v)
                num = float(m.group(0).replace(",", "")) if m else 0.0
            v = float(num or 0.0)
            max_v = max(max_v, v)
            disp = str(entry.get("display") or f"{raw_v}{entry.get('unit') or ''}")
            parsed_rows.append((lbl, v, disp))
        denom = max_v if max_v > 0 else 1.0
        parts.append('<div class="vs-bars">')
        for lbl, v, disp in parsed_rows:
            pct = max(4.0, min(100.0, (v / denom) * 100.0)) if v > 0 else 0.0
            parts.append(
                f'<div class="vs-bar-row"><span class="vs-bar-lbl">{_esc_br(lbl)}</span>'
                f'<div class="vs-bar-track"><div class="vs-bar-fill" style="width:{pct:.1f}%"></div></div>'
                f'<span class="vs-bar-val">{_esc_br(disp)}</span></div>'
            )
            est_h += 24
        parts.append("</div>")
    elif btype == "compare":
        left = blk.get("left") if isinstance(blk.get("left"), dict) else {}
        right = blk.get("right") if isinstance(blk.get("right"), dict) else {}
        parts.append('<div class="vs-compare">')
        for col in (left, right):
            ctitle = col.get("title") or col.get("label") or ""
            citems = col.get("items") or col.get("points") or []
            lis = "".join(f"<li>{_esc_br(x)}</li>" for x in list(citems)[:5])
            parts.append(
                f'<div class="vs-cmp-col"><div class="vs-cmp-head">{_esc_br(ctitle)}</div>'
                f'<ul class="vs-list">{lis}</ul></div>'
            )
        parts.append("</div>")
        est_h += 120
    elif btype in {"timeline", "steps"}:
        rows = list(blk.get("items") or blk.get("steps") or [])[:8]
        parts.append('<div class="vs-steps">')
        for entry in rows:
            if not isinstance(entry, dict):
                parts.append(f'<div class="vs-step"><span>{_esc_br(entry)}</span></div>')
                est_h += 22
                continue
            dot = entry.get("time") or entry.get("step") or entry.get("label") or "•"
            desc = entry.get("text") or entry.get("detail") or entry.get("title") or ""
            parts.append(
                f'<div class="vs-step"><span class="vs-step-dot">{_esc_br(dot)}</span>'
                f'<span>{_esc_br(desc)}</span></div>'
            )
            est_h += 24
        parts.append("</div>")
    elif btype == "table":
        cols = list(blk.get("columns") or blk.get("headers") or [])[:6]
        rows = list(blk.get("rows") or [])[:8]
        parts.append('<table class="vs-table">')
        if cols:
            parts.append("<thead><tr>" + "".join(f"<th>{_esc_br(c)}</th>" for c in cols) + "</tr></thead>")
            est_h += 28
        parts.append("<tbody>")
        for r in rows:
            if isinstance(r, (list, tuple)):
                parts.append("<tr>" + "".join(f"<td>{_esc_br(c)}</td>" for c in r[:6]) + "</tr>")
            elif isinstance(r, dict):
                cells = [r.get(c, "") for c in cols] if cols else list(r.values())[:6]
                parts.append("<tr>" + "".join(f"<td>{_esc_br(c)}</td>" for c in cells) + "</tr>")
            est_h += 26
        parts.append("</tbody></table>")
    else:
        body = blk.get("body") or blk.get("text") or ""
        parts.append(f'<div class="vs-callout">{_esc_br(body)}</div>')
        est_h += 56

    parts.append("</div>")
    return "".join(parts), est_h


def _strip_script_tags(fragment: str) -> str:
    return re.sub(r"<script\b[^>]*>.*?</script>", "", fragment, flags=re.I | re.S)


def _render_viz_fragment(viz_raw: Any, *, uid: str = "vs") -> tuple[str, int]:
    if not isinstance(viz_raw, dict):
        return "", 0
    spec = (
        viz_raw
        if ("blocks" in viz_raw and "type" not in viz_raw)
        else {"v": 1, "blocks": [viz_raw]}
    )
    if viz_spec is not None:
        try:
            validated = viz_spec.validate(spec, "news")
            render_fn = viz_spec.render
            sig = inspect.signature(render_fn)
            kwargs: dict[str, Any] = {}
            if "uid" in sig.parameters:
                kwargs["uid"] = uid
            if "static" in sig.parameters:
                kwargs["static"] = True
            rendered = render_fn(validated, **kwargs)
            if isinstance(rendered, tuple):
                frag = str(rendered[0] or "")
                est_h = int(rendered[1] or 180) if len(rendered) > 1 else 180
            else:
                frag = str(rendered or "")
                est_h = max(120, min(600, 60 + frag.count("vs-") * 18))
            frag = _strip_script_tags(frag)
            if not frag.strip() or "<script" in frag.lower() or len(frag) > _VIZ_MAX_HTML_CHARS:
                return "", 0
            return frag, est_h
        except Exception:
            return "", 0

    validated_blk, err = _validate_and_check_viz(viz_raw, None)
    if validated_blk is None or err:
        return "", 0
    blocks = (
        spec.get("blocks")
        if isinstance(spec.get("blocks"), list)
        else [validated_blk]
    )
    rendered_parts: list[str] = []
    total_h = 0
    for blk in blocks[:2]:
        frag, h = _render_fallback_viz_block(blk)
        rendered_parts.append(frag)
        total_h += h
    joined = _strip_script_tags("".join(rendered_parts))
    if not joined or "<script" in joined.lower() or len(joined) > _VIZ_MAX_HTML_CHARS:
        return "", 0
    return joined, total_h


def prefilter_search_item(
    url: str,
    title: str,
    prior_urls: set[str] | frozenset[str] | list[str] | tuple[str, ...] = (),
    prior_titles: list[str] | tuple[str, ...] | set[str] = (),
    blocked_domains: set[str] | frozenset[str] | list[str] | tuple[str, ...] = (),
) -> str | None:
    """Programmatic coarse filter for search candidates; returns rejection reason or None."""
    raw_url = str(url or "").strip()
    clean_title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not raw_url or not clean_title:
        return "缺少标题或链接"
    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError:
        return "无效链接"
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return "非公开 HTTP 链接"

    host = (parsed.hostname or "").lower()
    if not host:
        return "无效域名"
    bare_host = host[4:] if host.startswith("www.") else host

    blocked_set = {str(d).strip().lower() for d in (blocked_domains or ()) if str(d).strip()}
    for bd in blocked_set:
        bd_bare = bd[4:] if bd.startswith("www.") else bd
        if bare_host == bd_bare or bare_host.endswith("." + bd_bare):
            return "来源域名已屏蔽"

    # Encyclopedia entries
    if (
        host in _ENCYCLOPEDIA_EXACT_HOSTS
        or bare_host in _ENCYCLOPEDIA_EXACT_HOSTS
        or host.endswith(_ENCYCLOPEDIA_SUFFIXES)
        or _ENCYCLOPEDIA_TITLE_RE.search(clean_title)
    ):
        return "百科词条页"

    # Strip tracking params before checking query-less homepages
    kept_q = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    path = parsed.path or "/"
    path_lower = path.lower()

    if (
        path_lower in {"", "/"}
        or re.fullmatch(r"/(?:index|default|home)\.(?:html?|php|aspx?|jsp)", path_lower)
    ) and not kept_q:
        return "网站首页"

    if _YEAR_ARCHIVE_RE.search(path_lower) and not kept_q:
        return "年月归档索引页"

    if _LISTING_SEGMENT_RE.search(path_lower):
        return "列表/分类/标签归档页"

    if (
        _LOGIN_NOISE_HOST_RE.search(host)
        or _LOGIN_NOISE_PATH_RE.search(path_lower)
        or _LOGIN_NOISE_TITLE_RE.search(clean_title)
    ):
        return "登录或社交噪点页"

    norm_prior_urls: set[str] = set()
    for u in prior_urls or ():
        s = str(u or "").strip()
        if not s:
            continue
        norm_prior_urls.add(_normalize_url_key(s) if "://" in s else s)
    url_key = _normalize_url_key(raw_url)
    if url_key in norm_prior_urls:
        return "14 天已记录重复链接"

    for pt in prior_titles or ():
        if pt and _title_similarity(clean_title, str(pt)) >= 0.78:
            return "与已有标题高度重复"

    return None


class DigestWorkspace:
    """Manages RSS source trial/graduation/unsubscribe and digest history inside a workspace."""

    def __init__(self, work, *, fetcher=None, clock=None):
        self.work = work
        self.fetcher = fetcher or fetch_public_bytes
        self.clock = clock or time.time
        self._sources_lock = asyncio.Lock()

    def _read_text(self, path: str) -> str | None:
        offset = 0
        chunks: list[str] = []
        while True:
            try:
                res = self.work.read(path, offset=offset)
            except FileNotFoundError:
                return None if offset == 0 else "".join(chunks)
            chunks.append(res.get("text") or "")
            nxt = res.get("next_offset")
            if nxt is None:
                break
            offset = nxt
        return "".join(chunks)

    def _load_sources(self) -> dict[str, Any]:
        raw = self._read_text(RSS_SOURCES_FILE)
        if not raw:
            return {"feeds": [], "rejected": []}
        data = json.loads(raw)
        data.setdefault("feeds", [])
        data.setdefault("rejected", [])
        return data

    def _save_sources(self, data: dict[str, Any]) -> None:
        self.work.write(
            RSS_SOURCES_FILE, json.dumps(data, ensure_ascii=False, indent=2)
        )

    def _load_history(self) -> dict[str, Any]:
        raw = self._read_text(DIGEST_HISTORY_FILE)
        if not raw:
            return {"items": [], "blocked_domains": []}
        data = json.loads(raw)
        data.setdefault("items", [])
        data.setdefault("blocked_domains", [])
        return data

    def _save_history(self, data: dict[str, Any]) -> None:
        now = self.clock()
        cutoff = now - 45.0 * DAYS_TO_SECS
        data["items"] = [
            it
            for it in data.get("items", [])
            if float(it.get("created", now) or now) >= cutoff
        ][-500:]
        self.work.write(
            DIGEST_HISTORY_FILE, json.dumps(data, ensure_ascii=False, indent=2)
        )

    def _load_pool(self) -> dict[str, Any]:
        raw = self._read_text(DIGEST_POOL_FILE)
        if not raw:
            return {"total_discovered": 0, "by_url": {}}
        try:
            data = json.loads(raw)
        except Exception:
            return {"total_discovered": 0, "by_url": {}}
        if not isinstance(data, dict):
            return {"total_discovered": 0, "by_url": {}}
        data.setdefault("total_discovered", 0)
        data.setdefault("by_url", {})
        return data

    def _save_pool(self, data: dict[str, Any]) -> None:
        self.work.write(
            DIGEST_POOL_FILE, json.dumps(data, ensure_ascii=False, indent=2)
        )

    def _prune_rejected(self, sources: dict[str, Any], now: float) -> bool:
        kept = []
        changed = False
        for rej in sources.get("rejected", []):
            until = float(rej.get("cooldown_until", 0.0) or 0.0)
            if until > 0 and now >= until:
                changed = True
                continue
            kept.append(rej)
        if changed:
            sources["rejected"] = kept
        return changed

    def _is_rejected(
        self, sources: dict[str, Any], url: str, host: str, now: float
    ) -> dict[str, Any] | None:
        norm = _normalize_url_key(url)
        for rej in sources.get("rejected", []):
            until = float(rej.get("cooldown_until", 0.0) or 0.0)
            if until > 0 and now >= until:
                continue
            rej_url = rej.get("url", "")
            rej_host = rej.get("host", "")
            if (rej_url and _normalize_url_key(rej_url) == norm) or (
                host and rej_host == host
            ):
                return rej
        return None

    def _retire_feed(
        self,
        sources: dict[str, Any],
        feed: dict[str, Any],
        reason: str,
        now: float,
        *,
        cooldown_days: float = 0.0,
    ) -> dict[str, Any]:
        feed["status"] = "unsubscribed"
        sources["feeds"] = [
            f for f in sources["feeds"] if f.get("id") != feed.get("id")
        ]
        host = _host_of(feed.get("url", ""))
        cooldown_until = now + cooldown_days * DAYS_TO_SECS if cooldown_days > 0 else 0.0
        sources["rejected"].append(
            {
                "feed_id": feed.get("id", ""),
                "url": feed.get("url", ""),
                "host": host,
                "reason": reason,
                "rejected_at": now,
                "cooldown_until": cooldown_until,
            }
        )
        return {
            "feed_id": feed.get("id", ""),
            "url": feed.get("url", ""),
            "title": feed.get("title", ""),
            "reason": reason,
        }

    def status(self) -> dict[str, Any]:
        now = self.clock()
        sources = self._load_sources()
        changed = self._prune_rejected(sources, now)
        if changed:
            self._save_sources(sources)
            self.work.commit()
        history = self._load_history()
        return {
            "feeds": list(sources.get("feeds", [])),
            "rejected": list(sources.get("rejected", [])),
            "history_count": len(history.get("items", [])),
        }

    async def add_feed(
        self,
        url: str,
        *,
        title: str = "",
        auto: bool = True,
        origin: str = "manual",
    ) -> dict[str, Any]:
        now = self.clock()
        sources = self._load_sources()
        self._prune_rejected(sources, now)
        host = _host_of(url)
        if not host:
            return {"ok": False, "reason": "订阅链接无效"}
        if _is_big_platform(url):
            return {"ok": False, "reason": "大平台域名不做全站自动订阅"}
        rej = self._is_rejected(sources, url, host, now)
        if rej is not None:
            return {"ok": False, "reason": f"该来源处于退订/冷却列表: {rej.get('reason', '')}"}
        norm = _normalize_url_key(url)
        for existing in sources["feeds"]:
            if _normalize_url_key(existing.get("url", "")) == norm:
                return {"ok": True, "existing": True, "feed": existing}

        resp = await self.fetcher(url)
        body = resp.get("body")
        if not body or int(resp.get("status", 0) or 0) >= 400:
            return {
                "ok": False,
                "reason": f"抓取订阅源失败: {resp.get('error') or resp.get('status')}",
            }
        parsed = parse_feed_xml(body, now=now, lookback_days=30.0)
        resolved_title = title.strip() or parsed.get("title") or host
        async with self._sources_lock:
            now = self.clock()
            sources = self._load_sources()
            self._prune_rejected(sources, now)
            rej = self._is_rejected(sources, url, host, now)
            if rej is not None:
                return {"ok": False, "reason": f"该来源处于退订/冷却列表: {rej.get('reason', '')}"}
            for existing in sources["feeds"]:
                if _normalize_url_key(existing.get("url", "")) == norm:
                    return {"ok": True, "existing": True, "feed": existing}
            feed = {
                "id": _feed_id_for(url),
                "url": str(resp.get("url") or url),
                "host": host,
                "title": resolved_title,
                "origin": origin,
                "status": "trial" if auto else "active",
                "added_at": now,
                "trial_until": (now + TRIAL_DAYS * DAYS_TO_SECS) if auto else 0.0,
                "candidates_14d": [],
                "kept_total": 0,
                "up": 0,
                "down": 0,
                "reply": 0,
                "fail_count": 0,
                "first_fail_at": 0.0,
                "last_polled_at": 0.0,
            }
            sources["feeds"].append(feed)
            self._save_sources(sources)
            self.work.commit()
        return {"ok": True, "feed": feed}

    async def discover(self, url: str, *, origin: str = "discover") -> dict[str, Any]:
        now = self.clock()
        mapped = _map_author_feed(url)
        if mapped:
            feed_url, map_origin = mapped
            effective_origin = origin if origin == "trusted" else map_origin
            return await self.add_feed(feed_url, auto=True, origin=effective_origin)
        if _is_big_platform(url):
            return {"ok": False, "reason": "大平台域名不做全站自动订阅"}

        sources = self._load_sources()
        self._prune_rejected(sources, now)
        host = _host_of(url)
        rej = self._is_rejected(sources, url, host, now)
        if rej is not None:
            return {"ok": False, "reason": f"该来源处于退订/冷却列表: {rej.get('reason', '')}"}

        resp = await self.fetcher(url)
        body = resp.get("body")
        final_url = str(resp.get("url") or url)
        if not body or int(resp.get("status", 0) or 0) >= 400:
            return {
                "ok": False,
                "reason": f"无法访问来源主页: {resp.get('error') or resp.get('status')}",
            }

        ctype = (resp.get("content_type") or "").lower()
        sniff = body[:256].lstrip()
        if "xml" in ctype or sniff.startswith((b"<?xml", b"<rss", b"<feed")):
            try:
                parse_feed_xml(body, now=now, lookback_days=30.0)
                return await self.add_feed(final_url, auto=True, origin=origin)
            except ValueError:
                pass

        parser = _FeedLinkParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        candidates: list[tuple[str, str]] = []
        for href, link_title in parser.links:
            if _is_comment_feed(href, link_title):
                continue
            abs_href = urllib.parse.urljoin(final_url, href)
            if _is_comment_feed(abs_href, link_title):
                continue
            candidates.append((abs_href, link_title))

        base_root = f"{urllib.parse.urlparse(final_url).scheme}://{urllib.parse.urlparse(final_url).netloc}"
        for path in PROBE_PATHS[:4]:
            probe = urllib.parse.urljoin(base_root, path)
            if all(existing_url != probe for existing_url, _ in candidates):
                candidates.append((probe, ""))

        for cand_url, cand_title in candidates[:6]:
            if self._is_rejected(sources, cand_url, _host_of(cand_url), now):
                continue
            res = await self.add_feed(
                cand_url, title=cand_title, auto=True, origin=origin
            )
            if res.get("ok"):
                return res
        return {"ok": False, "reason": "未在该站点发现有效的公开 RSS/Atom 订阅源"}

    def unsubscribe(
        self, feed_id: str, *, reason: str = "手动退订", cooldown_days: float = 0.0
    ) -> dict[str, Any]:
        now = self.clock()
        sources = self._load_sources()
        key = str(feed_id or "").strip()
        if not key:
            return {"ok": False, "reason": "未找到对应订阅源"}
        key_low = key.lower()
        key_norm = _normalize_url_key(key) if "://" in key else ""
        key_host = _host_of(key) if "://" in key else key_low
        if key_host.startswith("www."):
            key_host = key_host[4:]

        feeds = sources.get("feeds", [])
        target = next((f for f in feeds if f.get("id") == key), None)
        if not target and key_norm:
            target = next(
                (f for f in feeds if _normalize_url_key(f.get("url", "")) == key_norm),
                None,
            )
        if not target:
            target = next(
                (
                    f
                    for f in feeds
                    if str(f.get("title") or "").strip().lower() == key_low
                ),
                None,
            )
        if not target and key_host:
            def _bare_host(f: dict[str, Any]) -> str:
                h = str(f.get("host") or _host_of(f.get("url", ""))).strip().lower()
                return h[4:] if h.startswith("www.") else h

            target = next((f for f in feeds if _bare_host(f) == key_host), None)

        if not target:
            return {"ok": False, "reason": "未找到对应订阅源"}
        info = self._retire_feed(
            sources, target, reason, now, cooldown_days=cooldown_days
        )
        self._save_sources(sources)
        self.work.commit()
        return {"ok": True, **info}

    def record_selected_items(self, items: list[dict[str, Any]]) -> None:
        now = self.clock()
        history = self._load_history()
        sources = self._load_sources()
        feed_map = {f["id"]: f for f in sources["feeds"]}
        for raw in items:
            created = float(raw.get("created", now) or now)
            entry = {
                "id": raw.get("id")
                or "it_" + hashlib.sha256((raw.get("url", "") + str(created)).encode()).hexdigest()[:10],
                "title": str(raw.get("title") or ""),
                "url": str(raw.get("url") or ""),
                "url_key": raw.get("url_key") or _normalize_url_key(raw.get("url", "")),
                "feed_id": str(raw.get("feed_id") or ""),
                "source": str(raw.get("source") or _host_of(raw.get("url", ""))),
                "topic": str(raw.get("topic") or ""),
                "avg": float(raw.get("avg", 0.0) or 0.0),
                "up": int(raw.get("up", 0) or 0),
                "down": int(raw.get("down", 0) or 0),
                "reply": int(raw.get("reply", 0) or 0),
                "created": created,
            }
            history["items"].append(entry)
            fid = entry["feed_id"]
            if fid and fid in feed_map:
                feed_map[fid]["kept_total"] = int(feed_map[fid].get("kept_total", 0)) + 1
        self._save_history(history)
        self._save_sources(sources)
        self.work.commit()

    def feedback(
        self, *, item_id: str, signal: str, note: str = ""
    ) -> dict[str, Any]:
        if signal not in {"up", "down", "reply"}:
            raise ValueError("反馈信号仅支持 up / down / reply")
        history = self._load_history()
        sources = self._load_sources()
        target = next((it for it in history["items"] if it.get("id") == item_id), None)
        if not target:
            return {"ok": False, "reason": "未找到对应的历史推送条目"}
        target[signal] = int(target.get(signal, 0) or 0) + 1
        if note:
            target["last_note"] = note[:256]
        net_useless = 0
        fid = target.get("feed_id")
        if fid:
            feed = next((f for f in sources["feeds"] if f.get("id") == fid), None)
            if feed:
                feed[signal] = int(feed.get(signal, 0) or 0) + 1
                net_useless = max(
                    0,
                    int(feed.get("down", 0) or 0)
                    - (int(feed.get("up", 0) or 0) + int(feed.get("reply", 0) or 0)),
                )
        self._save_history(history)
        self._save_sources(sources)
        self.work.commit()
        return {
            "ok": True,
            "item_id": item_id,
            "signal": signal,
            "net_useless": net_useless,
        }

    async def poll(self, *, lookback_days: float = 14.0) -> dict[str, Any]:
        now = self.clock()
        sources = self._load_sources()
        changed = self._prune_rejected(sources, now)
        graduated: list[dict[str, Any]] = []
        unsubscribed: list[dict[str, Any]] = []
        subscribed: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []

        # Phase 1: Evaluate lifecycle transitions before fetching
        active_or_trial = list(sources.get("feeds", []))
        for feed in active_or_trial:
            net_useless = int(feed.get("down", 0) or 0) - (
                int(feed.get("up", 0) or 0) + int(feed.get("reply", 0) or 0)
            )
            if net_useless >= NET_USELESS_MIN:
                unsubscribed.append(
                    self._retire_feed(
                        sources, feed, f"群友连续反馈没用（净负反馈 {net_useless} 次）", now
                    )
                )
                changed = True
                continue
            fail_count = int(feed.get("fail_count", 0) or 0)
            first_fail = float(feed.get("first_fail_at", 0.0) or 0.0)
            if (
                fail_count >= FETCH_FAILURE_MIN
                and first_fail > 0
                and (now - first_fail) >= FETCH_FAILURE_DAYS * DAYS_TO_SECS
            ):
                unsubscribed.append(
                    self._retire_feed(
                        sources,
                        feed,
                        "连续抓取失败超过 3 天，进入 30 天冷却退订",
                        now,
                        cooldown_days=FAILURE_COOLDOWN_DAYS,
                    )
                )
                changed = True
                continue
            if feed.get("status") == "trial":
                recent_cands = [
                    float(ts)
                    for ts in feed.get("candidates_14d", [])
                    if float(ts) >= now - TRIAL_DAYS * DAYS_TO_SECS
                ]
                if len(recent_cands) != len(feed.get("candidates_14d", [])):
                    feed["candidates_14d"] = recent_cands
                    changed = True
                raw_url_map = feed.get("candidate_urls_14d")
                if isinstance(raw_url_map, dict):
                    cutoff = now - TRIAL_DAYS * DAYS_TO_SECS
                    pruned_url_map = {
                        str(k): float(ts)
                        for k, ts in raw_url_map.items()
                        if k and float(ts) >= cutoff
                    }
                    if len(pruned_url_map) != len(raw_url_map):
                        feed["candidate_urls_14d"] = pruned_url_map
                        changed = True
                kept_total = int(feed.get("kept_total", 0) or 0)
                if kept_total == 0 and len(recent_cands) >= TRIAL_CAND_MIN:
                    unsubscribed.append(
                        self._retire_feed(
                            sources, feed, "14 天内已出满 10 条候选仍无一条入选", now
                        )
                    )
                    changed = True
                    continue
                trial_until = float(feed.get("trial_until", 0.0) or 0.0)
                if trial_until > 0 and now >= trial_until:
                    if kept_total >= 1:
                        feed["status"] = "active"
                        feed["trial_until"] = 0.0
                        graduated.append(
                            {
                                "feed_id": feed["id"],
                                "url": feed["url"],
                                "title": feed.get("title", ""),
                            }
                        )
                        changed = True
                    else:
                        unsubscribed.append(
                            self._retire_feed(
                                sources, feed, "14 天试用期到期仍无一条入选", now
                            )
                        )
                        changed = True
                        continue

        # Phase 2: Trusted source auto-discovery from last 30d non-RSS high-signal history
        history = self._load_history()
        win_start = now - TRUSTED_WINDOW_DAYS * DAYS_TO_SECS
        by_host: dict[str, list[dict[str, Any]]] = {}
        for item in history.get("items", []):
            created = float(item.get("created", 0.0) or 0.0)
            if created < win_start or item.get("feed_id"):
                continue
            host = (item.get("source") or _host_of(item.get("url", ""))).lower()
            if not host or _is_big_platform(f"https://{host}/"):
                continue
            by_host.setdefault(host, []).append(item)

        known_hosts = {_host_of(f.get("url", "")) for f in sources["feeds"]}
        for host, host_items in by_host.items():
            if host in known_hosts or self._is_rejected(sources, f"https://{host}/", host, now):
                continue
            high_items = [
                it for it in host_items if float(it.get("avg", 0.0) or 0.0) >= TRUSTED_HIGH_AVG
            ]
            utc8_days = {
                int((float(it.get("created", 0.0) or 0.0) + UTC8_OFFSET) // DAYS_TO_SECS)
                for it in high_items
            }
            positive = sum(
                int(it.get("up", 0) or 0) + int(it.get("reply", 0) or 0)
                for it in host_items
            )
            if (
                len(high_items) >= TRUSTED_MIN_HIGH
                and len(utc8_days) >= TRUSTED_MIN_DAYS
                and positive >= 1
            ):
                if changed:
                    self._save_sources(sources)
                    self.work.commit()
                    changed = False
                res = await self.discover(f"https://{host}/", origin="trusted")
                if res.get("ok") and not res.get("existing"):
                    subscribed.append(res["feed"])
                    sources = self._load_sources()

        # Phase 3: Fetch remaining active & trial feeds concurrently (Semaphore(6))
        feeds_list = list(sources.get("feeds", []))
        if feeds_list:
            sem_poll = asyncio.Semaphore(6)

            async def _fetch_feed(feed_obj: dict[str, Any]) -> dict[str, Any]:
                async with sem_poll:
                    try:
                        return await self.fetcher(feed_obj["url"])
                    except Exception as exc:
                        return {
                            "body": None,
                            "status": 0,
                            "url": feed_obj["url"],
                            "error": str(exc),
                        }

            responses = await asyncio.gather(*(_fetch_feed(f) for f in feeds_list))
            for feed, resp in zip(feeds_list, responses):
                body = resp.get("body")
                status_code = int(resp.get("status", 0) or 0)
                feed["last_polled_at"] = now
                if not body or status_code >= 400:
                    feed["fail_count"] = int(feed.get("fail_count", 0) or 0) + 1
                    if not feed.get("first_fail_at"):
                        feed["first_fail_at"] = now
                    changed = True
                    continue
                try:
                    parsed = parse_feed_xml(
                        body, now=now, lookback_days=lookback_days, max_items=20
                    )
                except ValueError:
                    feed["fail_count"] = int(feed.get("fail_count", 0) or 0) + 1
                    if not feed.get("first_fail_at"):
                        feed["first_fail_at"] = now
                    changed = True
                    continue
                feed["fail_count"] = 0
                feed["first_fail_at"] = 0.0
                cap = TRIAL_CAND_CAP if feed.get("status") == "trial" else 8
                picked = parsed["items"][:cap]
                is_trial = feed.get("status") == "trial"
                seen_url_map = (
                    dict(feed.get("candidate_urls_14d") or {})
                    if is_trial and isinstance(feed.get("candidate_urls_14d"), dict)
                    else {}
                )
                for it in picked:
                    if is_trial:
                        u_key = (
                            _normalize_url_key(str(it.get("url") or ""))
                            or str(it.get("title") or "").strip()
                        )
                        if u_key and u_key not in seen_url_map:
                            seen_url_map[u_key] = now
                            feed.setdefault("candidates_14d", []).append(now)
                    candidates.append(
                        {
                            **it,
                            "feed_id": feed["id"],
                            "source": feed.get("host") or _host_of(it.get("url", "")),
                            "feed_title": feed.get("title", ""),
                        }
                    )
                if is_trial:
                    feed["candidate_urls_14d"] = seen_url_map
                changed = True

        if changed:
            self._save_sources(sources)
            self.work.commit()

        return {
            "candidates": candidates,
            "graduated": graduated,
            "unsubscribed": unsubscribed,
            "subscribed": subscribed,
            "feeds_count": len(sources.get("feeds", [])),
        }

    async def seed_starter_feeds(
        self,
        topics_or_text: str | list[str] | tuple[str, ...] = "",
        *,
        max_add: int = 4,
    ) -> dict[str, Any]:
        cap = max(0, int(max_add or 0))
        if isinstance(topics_or_text, (list, tuple, set)):
            hint_text = " ".join(str(x) for x in topics_or_text if str(x).strip())
        else:
            hint_text = str(topics_or_text or "")
        hint_lower = hint_text.lower()
        hint_tokens = [
            tok
            for tok in re.split(r"[\s,，、;；/|]+", hint_lower)
            if len(tok) >= 2
        ]

        indie_dev_terms = ("独立开发", "indie dev", "indie hacker", "solo dev")
        game_terms = ("游戏", "game")
        indie_dev_without_game = any(t in hint_lower for t in indie_dev_terms) and not any(
            t in hint_lower for t in game_terms
        )

        def _match_score(entry: dict[str, Any]) -> int:
            if not hint_lower.strip():
                return 0
            cat_l = str(entry.get("category") or "").lower()
            if indie_dev_without_game and "game" in cat_l:
                return 0
            score = 0
            title_l = str(entry.get("title") or "").lower()
            title_cat_words = {
                w for w in re.split(r"[^a-z0-9\u4e00-\u9fff]+", f"{title_l} {cat_l}") if w
            }
            for kw in entry.get("keywords") or ():
                kw_l = str(kw).lower()
                if not kw_l:
                    continue
                if kw_l in hint_lower:
                    score += 3
                elif any(
                    tok == kw_l or (len(kw_l) >= 4 and kw_l in tok)
                    for tok in hint_tokens
                ):
                    score += 1
            if any(
                tok in title_cat_words or (len(tok) >= 4 and (tok in title_l or tok in cat_l))
                for tok in hint_tokens
            ):
                score += 2
            return score

        scored = [
            (entry, _match_score(entry), idx)
            for idx, entry in enumerate(STARTER_FEEDS)
        ]
        has_positive = any(sc > 0 for _, sc, _ in scored)
        if has_positive:
            ranked = [
                entry
                for entry, _sc, _idx in sorted(
                    (row for row in scored if row[1] > 0),
                    key=lambda row: (-row[1], row[2]),
                )
            ]
        else:
            ranked = [
                entry
                for entry, _sc, _idx in sorted(scored, key=lambda row: (-row[1], row[2]))
            ]

        now = self.clock()
        sources = self._load_sources()
        self._prune_rejected(sources, now)
        existing_urls = {
            _normalize_url_key(f.get("url", ""))
            for f in sources.get("feeds", [])
            if isinstance(f, dict) and f.get("url")
        }

        added: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        if cap == 0:
            return {
                "added": added,
                "skipped": skipped,
                "count": 0,
                "total_feeds": len(sources.get("feeds", [])),
            }

        eligible: list[dict[str, Any]] = []
        for entry in ranked:
            url = str(entry["url"])
            norm = _normalize_url_key(url)
            if norm in existing_urls:
                skipped.append({"url": url, "reason": "已存在订阅"})
                continue
            if self._is_rejected(sources, url, _host_of(url), now) is not None:
                skipped.append({"url": url, "reason": "位于退订冷却列表"})
                continue
            eligible.append(entry)

        probe_limit = max(cap * 2, cap + 6)
        to_probe = eligible[:probe_limit]
        sem_seed = asyncio.Semaphore(5)

        async def _probe_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], bool, str, dict[str, Any] | None]:
            url = str(entry["url"])
            async with sem_seed:
                try:
                    resp = await self.fetcher(url)
                    body = resp.get("body")
                    status_code = int(resp.get("status", 0) or 0)
                    if not body or status_code >= 400:
                        return (
                            entry,
                            False,
                            f"抓取订阅源失败: {resp.get('error') or resp.get('status')}",
                            None,
                        )
                    parsed = parse_feed_xml(body, now=now, lookback_days=30.0)
                except Exception as exc:
                    return entry, False, f"抓取异常: {exc}", None
            final_url = str(resp.get("url") or url)
            host = _host_of(final_url) or _host_of(url)
            resolved_title = str(entry.get("title") or "").strip() or str(parsed.get("title") or "").strip() or host
            feed_meta = {
                "id": _feed_id_for(url),
                "url": final_url,
                "host": host,
                "title": resolved_title,
                "origin": "starter",
                "status": "trial",
                "added_at": now,
                "trial_until": now + TRIAL_DAYS * DAYS_TO_SECS,
                "candidates_14d": [],
                "kept_total": 0,
                "up": 0,
                "down": 0,
                "reply": 0,
                "fail_count": 0,
                "first_fail_at": 0.0,
                "last_polled_at": 0.0,
            }
            return entry, True, "", feed_meta

        probe_results = await asyncio.gather(*(_probe_entry(e) for e in to_probe))
        changed = False
        for entry, ok, reason, feed_meta in probe_results:
            if len(added) >= cap:
                break
            url = str(entry["url"])
            norm = _normalize_url_key(url)
            if not ok or feed_meta is None:
                skipped.append({"url": url, "reason": reason})
                continue
            final_norm = _normalize_url_key(feed_meta.get("url") or url)
            if norm in existing_urls or final_norm in existing_urls:
                skipped.append({"url": url, "reason": "已存在订阅"})
                continue
            sources["feeds"].append(feed_meta)
            existing_urls.add(norm)
            existing_urls.add(final_norm)
            added.append(feed_meta)
            changed = True

        if changed:
            self._save_sources(sources)
            self.work.commit()

        total_feeds = len(self._load_sources().get("feeds", []))
        return {
            "added": added,
            "skipped": skipped,
            "count": len(added),
            "total_feeds": total_feeds,
        }

    async def collect(
        self,
        *,
        web: Any = None,
        context: dict[str, Any] | None = None,
        queries: list[str] | tuple[str, ...] = (),
        topics: list[str] | tuple[str, ...] = (),
        lookback_days: float = 7,
        max_items: int = 12,
        broad: bool = True,
    ) -> dict[str, Any]:
        cap_items = max(1, min(int(max_items or 12), 14))
        clean_queries = [str(q).strip() for q in (queries or ()) if str(q).strip()][:16]
        if isinstance(topics, str):
            clean_topics = [t.strip() for t in re.split(r"[,，、;；\s]+", topics) if t.strip()]
        else:
            clean_topics = [str(t).strip() for t in (topics or ()) if str(t).strip()]

        seeded_urls: list[str] = []
        sources_before = self._load_sources()
        if not sources_before.get("feeds") and (clean_topics or clean_queries):
            topic_hint = " ".join([*clean_topics, *clean_queries])
            try:
                seed_res = await self.seed_starter_feeds(topic_hint, max_add=4)
                seeded_urls = [
                    str(f.get("url") or "")
                    for f in (seed_res.get("added") or [])
                    if isinstance(f, dict) and f.get("url")
                ]
            except Exception:
                seeded_urls = []

        poll_res = await self.poll(lookback_days=float(lookback_days or 7))
        rss_raw = list(poll_res.get("candidates") or [])

        now = self.clock()
        history = self._load_history()
        blocked_domains = {
            str(d).lower()
            for d in history.get("blocked_domains", [])
            if isinstance(d, str) and d.strip()
        }
        prior_14d = [
            it
            for it in history.get("items", [])
            if float(it.get("created", 0.0) or 0.0) >= now - HISTORY_DEDUP_DAYS * DAYS_TO_SECS
        ]
        seen_urls: set[str] = {
            (it.get("url_key") or _normalize_url_key(it.get("url", "")))
            for it in prior_14d
            if it.get("url_key") or it.get("url")
        }
        seen_titles: list[str] = [
            str(it.get("title") or "") for it in prior_14d if it.get("title")
        ]

        prefilter_reasons: dict[str, int] = {}
        rss_kept: list[dict[str, Any]] = []
        for it in rss_raw:
            if not isinstance(it, dict):
                continue
            r_url = str(it.get("url") or "").strip()
            r_title = str(it.get("title") or "").strip()
            reason = prefilter_search_item(
                r_url, r_title, seen_urls, seen_titles, blocked_domains
            )
            if reason is not None:
                prefilter_reasons[reason] = prefilter_reasons.get(reason, 0) + 1
                continue
            seen_urls.add(_normalize_url_key(r_url))
            seen_titles.append(r_title)
            cand = dict(it)
            cand.setdefault("origin", "rss")
            cand.setdefault("source", cand.get("feed_title") or _host_of(r_url))
            rss_kept.append(cand)

        search_raw_count = 0
        search_errors = 0
        search_kept: list[dict[str, Any]] = []
        if web is not None and clean_queries:
            sem_search = asyncio.Semaphore(4)
            use_broad = bool(broad and hasattr(web, "search_broad"))

            async def _run_one_query(q: str) -> list[dict[str, Any]]:
                async with sem_search:
                    if use_broad:
                        res = await web.search_broad(q, max_results=8, context=context)
                    else:
                        res = await web.search(q, max_results=8, context=context)
                    if isinstance(res, dict) and isinstance(res.get("results"), list):
                        return list(res["results"])
                    if isinstance(res, list):
                        return list(res)
                    return []

            query_results = await asyncio.gather(
                *(_run_one_query(q) for q in clean_queries),
                return_exceptions=True,
            )
            for q_res in query_results:
                if isinstance(q_res, Exception):
                    search_errors += 1
                    continue
                for row in q_res:
                    if not isinstance(row, dict):
                        continue
                    search_raw_count += 1
                    s_url = str(row.get("url") or "").strip()
                    s_title = str(row.get("title") or "").strip()
                    reason = prefilter_search_item(
                        s_url, s_title, seen_urls, seen_titles, blocked_domains
                    )
                    if reason is not None:
                        prefilter_reasons[reason] = prefilter_reasons.get(reason, 0) + 1
                        continue
                    seen_urls.add(_normalize_url_key(s_url))
                    seen_titles.append(s_title)
                    cand: dict[str, Any] = {
                        "title": s_title,
                        "url": s_url,
                        "source": _host_of(s_url),
                        "snippet": str(row.get("snippet") or row.get("summary") or "").strip(),
                        "summary": str(row.get("summary") or row.get("snippet") or "").strip(),
                        "origin": "search",
                    }
                    if row.get("published_at"):
                        cand["published"] = str(row["published_at"])
                        cand["published_at"] = str(row["published_at"])
                    if row.get("engines"):
                        cand["engines"] = list(row["engines"])
                    search_kept.append(cand)

        # Balance RSS and search quotas with round-robin scatter so aggregator RSS feeds never starve search results
        def _scatter_by(items: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
            buckets: dict[str, list[dict[str, Any]]] = {}
            bucket_order: list[str] = []
            for item in items:
                key = str(key_fn(item) or "unknown")
                if key not in buckets:
                    buckets[key] = []
                    bucket_order.append(key)
                buckets[key].append(item)
            out: list[dict[str, Any]] = []
            idx = 0
            while len(out) < len(items):
                added_any = False
                for key in bucket_order:
                    bucket = buckets[key]
                    if idx < len(bucket):
                        out.append(bucket[idx])
                        added_any = True
                if not added_any:
                    break
                idx += 1
            return out

        rss_scattered = _scatter_by(
            rss_kept,
            lambda c: str(c.get("feed_id") or _host_of(c.get("url", "")) or "rss"),
        )
        search_scattered = _scatter_by(
            search_kept,
            lambda c: (_host_of(c.get("url", "")) or "search").removeprefix("www."),
        )
        combined = rss_scattered + search_scattered
        if rss_scattered and search_scattered:
            search_quota = min(len(search_scattered), max(cap_items // 2, 1))
            rss_quota = min(len(rss_scattered), cap_items - search_quota)
            paired = min(rss_quota, search_quota)
            scattered: list[dict[str, Any]] = []
            for r_it, s_it in zip(rss_scattered[:paired], search_scattered[:paired]):
                scattered.append(r_it)
                scattered.append(s_it)
            scattered.extend(rss_scattered[paired:rss_quota])
            scattered.extend(search_scattered[paired:search_quota])
            scattered.extend(rss_scattered[rss_quota:])
            scattered.extend(search_scattered[search_quota:])
        else:
            scattered = rss_scattered or search_scattered

        working_pool = [dict(c) for c in scattered[:cap_items]]

        # Fetch pages missing body text concurrently (max concurrency 5)
        fetched_count = 0
        fetch_errors = 0
        discovered_feed_candidates: list[tuple[str, str]] = []

        if web is not None and hasattr(web, "fetch"):
            sem_fetch = asyncio.Semaphore(5)

            async def _fetch_candidate(cand: dict[str, Any]) -> None:
                nonlocal fetched_count, fetch_errors
                existing_body = str(cand.get("source_text") or cand.get("body") or "").strip()
                if len(existing_body) >= 200:
                    return
                async with sem_fetch:
                    try:
                        page = await asyncio.wait_for(
                            web.fetch(cand["url"], context=context), timeout=8.0
                        )
                    except Exception:
                        fetch_errors += 1
                        return
                if not isinstance(page, dict):
                    return
                fetched_count += 1
                raw_content = str(page.get("content") or page.get("text") or "").strip()
                if raw_content:
                    cleaned = re.sub(r"[ \t]+", " ", raw_content)
                    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()[:2200]
                    cand["source_text"] = cleaned
                pub = str(
                    page.get("published_at")
                    or cand.get("published")
                    or cand.get("published_at")
                    or ""
                ).strip()
                if not pub and cand.get("source_text"):
                    pub = published_from_text(cand["source_text"])
                if pub:
                    cand["published"] = pub
                    cand["published_at"] = pub
                fetched_title = str(page.get("title") or "").strip()
                if fetched_title and (
                    not cand.get("title") or cand.get("title") == cand.get("url")
                ):
                    cand["title"] = fetched_title
                fetched_img = str(page.get("image_url") or page.get("og_image") or "").strip()
                if fetched_img and not cand.get("image_url"):
                    cand["image_url"] = fetched_img
                for link in page.get("feed_links") or ():
                    if isinstance(link, str) and link.strip():
                        discovered_feed_candidates.append((cand["url"], link.strip()))

            await asyncio.gather(*(_fetch_candidate(c) for c in working_pool))

        final_candidates: list[dict[str, Any]] = []
        for cand in working_pool:
            if not cand.get("source_text"):
                fallback_body = str(cand.get("body") or cand.get("summary") or cand.get("snippet") or "").strip()
                if fallback_body:
                    cand["source_text"] = re.sub(r"\s+", " ", fallback_body).strip()[:2200]
            src_txt = str(cand.get("source_text") or "")
            if not cand.get("published"):
                fallback_pub = str(cand.get("published_at") or "").strip()
                if not fallback_pub and src_txt:
                    fallback_pub = published_from_text(src_txt)
                if fallback_pub:
                    cand["published"] = fallback_pub
            cand["has_numbers"] = _has_distinct_numbers(src_txt)
            if len(src_txt) > 1100:
                cand["source_text"] = src_txt[:1100]
            if len(cand.get("source_text") or "") >= 120:
                cand.pop("summary", None)
                cand.pop("snippet", None)
            else:
                for text_key in ("summary", "snippet"):
                    if text_key in cand and len(str(cand[text_key] or "")) > 240:
                        cand[text_key] = str(cand[text_key])[:240]
            cand.pop("body", None)
            cand.pop("feed_links", None)
            final_candidates.append(cand)

        pool_by_url: dict[str, dict[str, Any]] = {}
        for cand in [*combined, *final_candidates]:
            c_url = str(cand.get("url") or "").strip()
            if not c_url:
                continue
            u_key = _normalize_url_key(c_url)
            prev = pool_by_url.get(u_key) or {}
            pool_by_url[u_key] = {
                "url": c_url,
                "image_url": str(cand.get("image_url") or prev.get("image_url") or ""),
                "published": str(
                    cand.get("published")
                    or cand.get("published_at")
                    or prev.get("published")
                    or ""
                ),
                "published_ts": (
                    cand.get("published_ts")
                    if cand.get("published_ts") is not None
                    else prev.get("published_ts")
                ),
                "feed_id": str(cand.get("feed_id") or prev.get("feed_id") or ""),
                "feed_title": str(cand.get("feed_title") or prev.get("feed_title") or ""),
            }
        self._save_pool(
            {
                "updated_at": now,
                "total_discovered": max(len(combined), len(final_candidates)),
                "by_url": pool_by_url,
            }
        )
        self.work.commit()

        # Opportunistically subscribe to at most 1 non-big-platform RSS feed found during fetch
        discovered_feeds: list[str] = []
        if discovered_feed_candidates:
            current_sources = self._load_sources()
            known_feed_keys = {
                _normalize_url_key(f.get("url", ""))
                for f in current_sources.get("feeds", [])
                if isinstance(f, dict) and f.get("url")
            }
            for page_url, feed_url in discovered_feed_candidates:
                if _is_big_platform(page_url) or _is_big_platform(feed_url):
                    continue
                if _is_comment_feed(feed_url):
                    continue
                f_key = _normalize_url_key(feed_url)
                if f_key in known_feed_keys:
                    continue
                if self._is_rejected(current_sources, feed_url, _host_of(feed_url), now) is not None:
                    continue
                try:
                    add_res = await self.add_feed(
                        feed_url, auto=True, origin="collect-auto"
                    )
                except Exception:
                    continue
                if add_res.get("ok") and not add_res.get("existing"):
                    discovered_feeds.append(str((add_res.get("feed") or {}).get("url") or feed_url))
                    break

        return {
            "candidates": final_candidates,
            "stats": {
                "seeded_feeds": seeded_urls,
                "feeds_total": int(poll_res.get("feeds_count", 0) or 0),
                "rss_candidates": len(rss_raw),
                "rss_kept": len(rss_kept),
                "queries_run": len(clean_queries),
                "search_raw": search_raw_count,
                "search_kept": len(search_kept),
                "search_errors": search_errors,
                "prefiltered": sum(prefilter_reasons.values()),
                "prefilter_reasons": prefilter_reasons,
                "fetched": fetched_count,
                "fetch_errors": fetch_errors,
                "discovered_feeds": discovered_feeds,
                "returned": len(final_candidates),
            },
        }

    def score_select(
        self, candidates: list[dict[str, Any]], *, max_items: int = 5
    ) -> dict[str, Any]:
        now = self.clock()
        history = self._load_history()
        sources = self._load_sources()
        pool = self._load_pool()
        pool_by_url = pool.get("by_url") if isinstance(pool.get("by_url"), dict) else {}
        pool_total = int(pool.get("total_discovered") or 0)
        blocked_domains = {
            d.lower() for d in history.get("blocked_domains", []) if isinstance(d, str)
        }
        prior_14d = [
            it
            for it in history.get("items", [])
            if float(it.get("created", 0.0) or 0.0) >= now - HISTORY_DEDUP_DAYS * DAYS_TO_SECS
        ]
        prior_urls = {
            (it.get("url_key") or _normalize_url_key(it.get("url", "")))
            for it in prior_14d
        }
        prior_titles = [str(it.get("title") or "") for it in prior_14d]
        topic_counts_3d: dict[str, int] = {}
        for it in history.get("items", []):
            if float(it.get("created", 0.0) or 0.0) >= now - TOPIC_SATURATION_DAYS * DAYS_TO_SECS:
                topic = str(it.get("topic") or "").strip()
                if topic:
                    topic_counts_3d[topic] = topic_counts_3d.get(topic, 0) + 1

        rejected: list[dict[str, Any]] = []
        regular_pool: list[dict[str, Any]] = []
        explore_pool: list[dict[str, Any]] = []
        feed_map = {
            f["id"]: f
            for f in sources.get("feeds", [])
            if isinstance(f, dict) and f.get("id")
        }
        prev_last_selected = list(history.get("last_selected") or [])
        prev_total_candidates = int(history.get("last_total_candidates") or 0)

        for raw in candidates:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            host = _host_of(url)
            if not title or not url or not host or host in blocked_domains:
                item["reject_reason"] = "缺少标题或有效公开链接，或来源域名已屏蔽"
                rejected.append(item)
                continue
            if item.get("paywall") is True:
                item["reject_reason"] = "付费墙内容无法直读"
                rejected.append(item)
                continue

            url_key = _normalize_url_key(url)
            pool_meta = pool_by_url.get(url_key)
            if isinstance(pool_meta, dict):
                if not item.get("image_url") and pool_meta.get("image_url"):
                    item["image_url"] = str(pool_meta["image_url"])
                if (
                    not item.get("published")
                    and not item.get("published_at")
                    and item.get("published_ts") is None
                ):
                    if pool_meta.get("published"):
                        item["published"] = str(pool_meta["published"])
                    if pool_meta.get("published_ts") is not None:
                        item["published_ts"] = pool_meta["published_ts"]
                if not item.get("feed_id") and pool_meta.get("feed_id"):
                    item["feed_id"] = str(pool_meta["feed_id"])
                if not item.get("feed_title") and pool_meta.get("feed_title"):
                    item["feed_title"] = str(pool_meta["feed_title"])

            kind = str(item.get("kind") or "news").strip().lower()
            pub_ts = _parse_timestamp(
                item.get("published") or item.get("published_at") or item.get("published_ts")
            )
            if kind == "guide":
                if pub_ts is None:
                    item["reject_reason"] = "教程/指南缺少可核实的发布时间"
                    rejected.append(item)
                    continue
                if now - pub_ts > GUIDE_MAX_AGE_DAYS * DAYS_TO_SECS:
                    item["reject_reason"] = "教程/指南发布时间超过 60 天上限"
                    rejected.append(item)
                    continue
            else:
                if pub_ts is not None and now - pub_ts > NEWS_MAX_AGE_DAYS * DAYS_TO_SECS:
                    item["reject_reason"] = "新闻发布时间超过 7 天时效上限"
                    rejected.append(item)
                    continue

            source_text = item.get("source_text")
            if source_text is not None:
                quote = str(item.get("quote") or "").strip()
                norm_src = re.sub(r"\s+", " ", str(source_text)).strip().lower()
                norm_quote = re.sub(r"\s+", " ", quote).strip().lower()
                if not norm_quote or norm_quote not in norm_src:
                    item["reject_reason"] = "引用片段未能在原文中找到对照依据"
                    rejected.append(item)
                    continue

            summary_text = str(item.get("brief") or item.get("summary") or "").strip()
            if not summary_text:
                item["reject_reason"] = "缺少中文摘要（请提供 1~2 句 brief 或 summary 事实摘要）"
                rejected.append(item)
                continue
            item["summary"] = summary_text

            if "viz" in item and item["viz"] is not None:
                valid_viz, viz_drop = _validate_and_check_viz(item.get("viz"), source_text)
                if valid_viz is not None:
                    item["viz"] = valid_viz
                else:
                    item.pop("viz", None)
                    item["viz_dropped_reason"] = viz_drop

            scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}

            def _pick_score(score_key: str, *fallback_keys: str, default: float = 0.0) -> float:
                for val in (scores.get(score_key), *(item.get(k) for k in fallback_keys)):
                    parsed = _numeric_score(val)
                    if parsed is not None:
                        return parsed
                return default

            info = _pick_score("info", "info")
            source_s = _pick_score("source", "source_score", "source")
            rel = _pick_score("relevance", "relevance")
            timeliness = _pick_score("timeliness", "timeliness")
            chat = _pick_score("chat", "chat")
            novelty = _pick_score("novelty", "novelty", default=5.0)
            surprise = _pick_score("surprise", "surprise")
            avg = round((info + source_s + rel + timeliness + chat) / 5.0, 2)
            profile_hit = str(item.get("profile_hit") or "").strip()

            if _GOV_SITE_RE.search(host) and not (profile_hit and rel >= 4.0):
                item["reject_reason"] = "政务或司法站点未命中高相关兴趣（relevance>=4）"
                rejected.append(item)
                continue

            url_key = _normalize_url_key(url)
            if url_key in prior_urls or any(
                _title_similarity(title, prev_t) >= 0.78 for prev_t in prior_titles
            ):
                item["reject_reason"] = "与近 14 天已记录资讯链接或主题重复"
                rejected.append(item)
                continue

            topic = str(item.get("topic") or "").strip()
            if topic and topic_counts_3d.get(topic, 0) >= TOPIC_SATURATION_CAP:
                item["reject_reason"] = f"话题「{topic}」近 3 天已饱和（>=3 条）"
                rejected.append(item)
                continue

            if novelty <= NOVELTY_REJECT_MAX:
                item["reject_reason"] = "新鲜感过低（众人皆知或刚讨论过）"
                rejected.append(item)
                continue

            feed_meta = feed_map.get(str(item.get("feed_id") or "")) or {}
            feed_title = str(item.get("feed_title") or feed_meta.get("title") or "").strip()
            raw_source = item.get("source")
            if (
                not isinstance(raw_source, str)
                or not raw_source.strip()
                or _numeric_score(raw_source) is not None
            ):
                raw_source = item.get("source_name") or feed_title or host
            source_label = digest_card.clean_source_label(
                raw_source, url=url, feed_title=feed_title
            )

            enriched = {
                **item,
                "url_key": url_key,
                "source": source_label,
                "kind": kind,
                "avg": avg,
                "_rank_tuple": (avg, info, rel, chat),
            }
            if feed_title:
                enriched["feed_title"] = feed_title
            if pub_ts is not None:
                enriched["published_ts"] = pub_ts

            if kind == "guide":
                if rel >= 4.0 and info >= 4.0 and avg >= 3.8:
                    enriched["slot"] = "normal"
                    regular_pool.append(enriched)
                else:
                    enriched["reject_reason"] = "教程/指南未达到 relevance>=4 且均分>=3.8 门槛"
                    rejected.append(enriched)
                continue

            bridge = str(item.get("bridge") or "").strip()
            if (
                rel >= 2.0
                and rel < 3.0
                and info >= 4.0
                and chat >= 4.0
                and surprise >= 4.0
                and timeliness >= 3.0
                and bridge
            ):
                enriched["slot"] = "explore"
                enriched["_explore_rank"] = (surprise + info + chat, avg)
                explore_pool.append(enriched)
            elif rel >= 3.0 and info >= 3.0 and avg >= 3.5 and timeliness >= 2.0:
                enriched["slot"] = "normal"
                regular_pool.append(enriched)
            else:
                enriched["reject_reason"] = "五维均值或关键维度低于入选线"
                rejected.append(enriched)

        regular_pool.sort(key=lambda it: it["_rank_tuple"], reverse=True)
        explore_pool.sort(key=lambda it: it["_explore_rank"], reverse=True)

        selected: list[dict[str, Any]] = []
        guides_used = 0
        explore_used = 0
        explore_cap = min(2, max(1, max_items // 3)) if max_items >= 3 else 1

        # Reserve up to explore_cap slots if explore candidates exist; fill remaining with regular first
        regular_budget = max(1, max_items - min(explore_cap, len(explore_pool)))
        for cand in regular_pool:
            if any(_same_story(cand, chosen) for chosen in selected):
                cand["reject_reason"] = "同一件事已在更高分候选入选"
                rejected.append(cand)
                continue
            if cand["kind"] == "guide":
                if guides_used >= 1:
                    cand["reject_reason"] = "单轮教程/指南数量已达上限（最多 1 篇）"
                    rejected.append(cand)
                    continue
                guides_used += 1
            if len(selected) >= regular_budget and explore_used < min(explore_cap, len(explore_pool)):
                # Only reject/defer if we still need to evaluate explore slots vs regular tail
                pass
            if len(selected) >= max_items:
                cand["reject_reason"] = "超出本轮输出条数上限"
                rejected.append(cand)
                continue
            if len(selected) >= regular_budget and len(explore_pool) > explore_used:
                continue
            selected.append(cand)

        for cand in explore_pool:
            if len(selected) >= max_items or explore_used >= explore_cap:
                cand["reject_reason"] = "探索位名额已满"
                rejected.append(cand)
                continue
            if any(_same_story(cand, chosen) for chosen in selected):
                cand["reject_reason"] = "与已入选主榜属于同一件事"
                rejected.append(cand)
                continue
            selected.append(cand)
            explore_used += 1

        # Top up any unused capacity with remaining regular items if explore didn't use all reserved slots
        selected_urls = {it["url_key"] for it in selected}
        rejected_urls = {it.get("url_key") for it in rejected}
        for cand in regular_pool:
            if len(selected) >= max_items:
                break
            if cand["url_key"] in selected_urls or cand["url_key"] in rejected_urls:
                continue
            if any(_same_story(cand, chosen) for chosen in selected):
                cand["reject_reason"] = "同一件事已在更高分候选入选"
                rejected.append(cand)
                continue
            if cand["kind"] == "guide":
                if guides_used >= 1:
                    cand["reject_reason"] = "单轮教程/指南数量已达上限（最多 1 篇）"
                    rejected.append(cand)
                    continue
                guides_used += 1
            selected.append(cand)
            selected_urls.add(cand["url_key"])

        clean_selected = []
        for idx, cand in enumerate(selected, 1):
            clean = {
                k: v for k, v in cand.items() if not k.startswith("_") and k != "source_text"
            }
            clean["id"] = clean.get("id") or f"it_{int(now)}_{idx}"
            clean["created"] = float(clean.get("created") or now)
            clean_selected.append(clean)
            history["items"].append(
                {
                    "id": clean["id"],
                    "title": clean["title"],
                    "url": clean["url"],
                    "url_key": clean["url_key"],
                    "feed_id": str(clean.get("feed_id") or ""),
                    "source": clean["source"],
                    "topic": str(clean.get("topic") or ""),
                    "avg": clean["avg"],
                    "up": 0,
                    "down": 0,
                    "reply": 0,
                    "created": clean["created"],
                }
            )
            fid = str(clean.get("feed_id") or "")
            if fid and fid in feed_map:
                feed_map[fid]["kept_total"] = int(feed_map[fid].get("kept_total", 0)) + 1

        total_candidates = max(len(candidates), pool_total)
        history["last_selected"] = clean_selected
        history["last_total_candidates"] = total_candidates
        if (
            clean_selected
            or prev_last_selected
            or total_candidates != prev_total_candidates
        ):
            self._save_history(history)
            if clean_selected:
                self._save_sources(sources)
            self.work.commit()

        clean_rejected = [
            {k: v for k, v in item.items() if not k.startswith("_") and k != "source_text"}
            for item in rejected
        ]
        return {
            "selected": clean_selected,
            "rejected": clean_rejected,
            "total_candidates": total_candidates,
        }

    async def prepare_covers(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """并发抓取与校验候选条目封面图及染底配色，任何异常静默降级。"""
        target_items = list(items or [])
        if not target_items:
            return []
        sem = asyncio.Semaphore(3)

        def _apply_cover_bytes(item: dict[str, Any], raw_bytes: bytes, url: str) -> bool:
            if not raw_bytes:
                return False
            if digest_card.judge_cover(raw_bytes, url=url):
                return False
            data_uri, cover_small = digest_card.encode_cover_data_uri(raw_bytes, url=url)
            if not data_uri:
                return False
            item["cover_data"] = data_uri
            item["cover_small"] = bool(cover_small)
            tint = digest_card.cover_tint(raw_bytes)
            if tint is not None:
                item["cover_tint"] = tint
            return True

        async def _one(item: Any) -> None:
            if not isinstance(item, dict):
                return
            if str(item.get("cover_data") or "").startswith("data:image/"):
                return
            img_url = str(item.get("image_url") or item.get("cover_url") or "").strip()
            article_url = str(item.get("url") or "").strip()

            async with sem:
                if (
                    img_url
                    and _is_safe_http_url(img_url)
                    and not digest_card._url_rejection_reason(img_url)
                ):
                    try:
                        resp = await self.fetcher(img_url)
                        body = resp.get("body")
                        status_code = int(resp.get("status", 0) or 0)
                        ctype = str(resp.get("content_type") or "").lower()
                        final_img_url = str(resp.get("url") or img_url)
                        if (
                            body
                            and status_code < 400
                            and "text/html" not in ctype
                            and _apply_cover_bytes(item, body, final_img_url)
                        ):
                            return
                    except Exception:
                        pass

                if not img_url and isinstance(item.get("viz"), dict):
                    return
                if article_url and _is_safe_http_url(article_url):
                    try:
                        page_resp = await self.fetcher(article_url)
                        page_body = page_resp.get("body")
                        page_status = int(page_resp.get("status", 0) or 0)
                        page_ctype = str(page_resp.get("content_type") or "").lower()
                        final_page_url = str(page_resp.get("url") or article_url)
                        if (
                            not page_body
                            or page_status >= 400
                            or page_ctype.startswith("image/")
                        ):
                            return
                        og_url = _extract_meta_image(page_body, final_page_url)
                        if (
                            not og_url
                            or og_url == img_url
                            or not _is_safe_http_url(og_url)
                            or digest_card._url_rejection_reason(og_url)
                        ):
                            return
                        img_resp = await self.fetcher(og_url)
                        img_body = img_resp.get("body")
                        img_status = int(img_resp.get("status", 0) or 0)
                        img_ctype = str(img_resp.get("content_type") or "").lower()
                        final_og_url = str(img_resp.get("url") or og_url)
                        if img_body and img_status < 400 and "text/html" not in img_ctype:
                            _apply_cover_bytes(item, img_body, final_og_url)
                    except Exception:
                        return

        await asyncio.gather(*(_one(it) for it in target_items))
        return target_items

    def render_card_html(
        self,
        items: list[dict[str, Any]],
        *,
        slot_label: str = "",
        group_title: str = "",
        output_path: str = "out/digest-card.html",
        total_candidates: int = 0,
    ) -> dict[str, Any]:
        if not output_path.startswith("out/") or not output_path.endswith(".html"):
            raise ValueError("简报卡 HTML 必须写入 out/*.html")

        picked: list[dict[str, Any]] = []
        total_viz_h = 0
        total_viz_chars = 0
        any_viz = False
        for idx, raw_it in enumerate((items or [])[:8], 1):
            if not isinstance(raw_it, dict):
                continue
            it = dict(raw_it)
            if not str(it.get("cover_data") or "").startswith("data:image/") and isinstance(
                it.get("viz"), dict
            ):
                frag, est_h = _render_viz_fragment(it["viz"], uid=f"vs-{idx}")
                if (
                    frag
                    and (total_viz_h + est_h) <= _VIZ_STATIC_MAX_H
                    and (total_viz_chars + len(frag)) <= 40000
                ):
                    it["viz_frag"] = frag
                    any_viz = True
                    total_viz_h += est_h
                    total_viz_chars += len(frag)
            picked.append(it)

        viz_css = _load_viz_css_text() if any_viz else ""
        resolved_total = int(total_candidates or 0)
        if resolved_total <= 0:
            hist_total = int(self._load_history().get("last_total_candidates") or 0)
            resolved_total = max(len(picked), hist_total)

        doc = digest_card.build_card_html(
            picked,
            slot_label=slot_label,
            group_title=group_title,
            total_candidates=resolved_total,
            viz_css=viz_css,
            now_ts=self.clock(),
        )
        doc = _strip_script_tags(doc)
        res = self.work.write(output_path, doc)
        self.work.commit()
        return {"path": output_path, "bytes": res["bytes"], "count": len(picked)}


async def run_digest_tool(
    work,
    arguments: dict[str, Any],
    *,
    web: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dw = DigestWorkspace(work)
    action = arguments["action"]
    if action == "status":
        return dw.status()
    if action == "seed":
        return await dw.seed_starter_feeds(
            arguments.get("topics") or arguments.get("query") or arguments.get("note") or "",
            max_add=int(arguments.get("max_items", 4) or 4),
        )
    if action == "collect":
        raw_topics = arguments.get("topics") or ()
        if isinstance(raw_topics, str):
            topics_arg: Any = raw_topics
        else:
            topics_arg = list(raw_topics)
        return await dw.collect(
            web=web,
            context=context,
            queries=list(arguments.get("queries") or ()),
            topics=topics_arg,
            lookback_days=float(arguments.get("lookback_days", 7) or 7),
            max_items=int(arguments.get("max_items", 12) or 12),
            broad=bool(arguments.get("broad", True)),
        )
    if action == "poll":
        return await dw.poll(lookback_days=float(arguments.get("lookback_days", 14) or 14))
    if action == "discover":
        return await dw.discover(
            arguments.get("url", ""),
            origin=str(arguments.get("origin") or "discover"),
        )
    if action == "add_feed":
        return await dw.add_feed(
            arguments.get("url", ""),
            title=str(arguments.get("title") or ""),
            auto=bool(arguments.get("auto", True)),
            origin=str(arguments.get("origin") or "manual"),
        )
    if action == "unsubscribe":
        return dw.unsubscribe(
            arguments.get("feed_id", ""),
            reason=str(arguments.get("note") or "手动退订"),
        )
    if action == "feedback":
        return dw.feedback(
            item_id=arguments.get("item_id", ""),
            signal=arguments.get("signal", "up"),
            note=str(arguments.get("note") or ""),
        )
    if action == "score_select":
        return dw.score_select(
            list(arguments.get("candidates") or []),
            max_items=int(arguments.get("max_items", 5) or 5),
        )
    if action == "render_card_html":
        raw_max = arguments.get("max_items")
        max_items = max(1, min(int(raw_max if raw_max is not None else 3), 8))
        candidates = arguments.get("candidates")
        history = dw._load_history()
        if candidates is None:
            candidates = history.get("last_selected") or []
        picked = [
            dict(c) for c in list(candidates or [])[:max_items] if isinstance(c, dict)
        ]
        await dw.prepare_covers(picked)
        total_cand = int(
            arguments.get("total_candidates")
            or history.get("last_total_candidates")
            or len(picked)
        )
        return dw.render_card_html(
            picked,
            slot_label=str(arguments.get("slot_label") or ""),
            group_title=str(arguments.get("group_title") or ""),
            output_path=str(arguments.get("output_path") or "out/digest-card.html"),
            total_candidates=total_cand,
        )
    raise ValueError("未知 digest_feed 操作")
