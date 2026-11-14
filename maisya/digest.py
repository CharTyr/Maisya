"""Opt-in digest workflow engine: SSRF/XXE-safe RSS state machine, 5D scoring filter & card renderer."""

import asyncio
import hashlib
import html
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
from typing import Any

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
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8",
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
    url: str, *, timeout: float = 12.0, max_bytes: int = 2_097_152
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
            for child in node:
                tag = _local_tag(child.tag)
                text = (child.text or "").strip()
                if tag == "title" and not title:
                    title = _strip_html(text)
                elif tag == "link" and not link:
                    link = text or (child.attrib.get("href") or "").strip()
                elif tag in {"pubdate", "date", "published", "updated"} and not pub_raw:
                    pub_raw = text
                elif tag in {"description", "encoded", "summary", "content"} and not summary:
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
            for child in node:
                tag = _local_tag(child.tag)
                text = "".join(child.itertext()).strip()
                if tag == "title" and not title:
                    title = _strip_html(text)
                elif tag == "link":
                    rel = (child.attrib.get("rel") or "alternate").lower()
                    href = (child.attrib.get("href") or "").strip()
                    if href and (not link or rel == "alternate"):
                        link = href
                elif tag in {"published", "updated"} and not pub_raw:
                    pub_raw = text
                elif tag in {"summary", "content"} and not summary:
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


class DigestWorkspace:
    """Manages RSS source trial/graduation/unsubscribe and digest history inside a workspace."""

    def __init__(self, work, *, fetcher=fetch_public_bytes, clock=time.time):
        self.work = work
        self.fetcher = fetcher
        self.clock = clock

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
        target = next((f for f in sources["feeds"] if f.get("id") == feed_id), None)
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

        # Phase 3: Fetch remaining active & trial feeds
        for feed in sources.get("feeds", []):
            resp = await self.fetcher(feed["url"])
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
            for it in picked:
                if feed.get("status") == "trial":
                    feed.setdefault("candidates_14d", []).append(now)
                candidates.append(
                    {
                        **it,
                        "feed_id": feed["id"],
                        "source": feed.get("host") or _host_of(it.get("url", "")),
                        "feed_title": feed.get("title", ""),
                    }
                )
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

    def score_select(
        self, candidates: list[dict[str, Any]], *, max_items: int = 5
    ) -> dict[str, Any]:
        now = self.clock()
        history = self._load_history()
        sources = self._load_sources()
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

            kind = str(item.get("kind") or "news").strip().lower()
            pub_ts = _parse_timestamp(item.get("published") or item.get("published_ts"))
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

            raw_source = item.get("source")
            if (
                isinstance(raw_source, str)
                and raw_source.strip()
                and _numeric_score(raw_source) is None
            ):
                source_label = raw_source.strip()
            else:
                feed_meta = feed_map.get(str(item.get("feed_id") or "")) or {}
                source_label = str(item.get("source_name") or feed_meta.get("title") or host)

            enriched = {
                **item,
                "url_key": url_key,
                "source": source_label,
                "kind": kind,
                "avg": avg,
                "_rank_tuple": (avg, info, rel, chat),
            }

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

        history["last_selected"] = clean_selected
        if clean_selected or prev_last_selected:
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
            "total_candidates": len(candidates),
        }

    def render_card_html(
        self,
        items: list[dict[str, Any]],
        *,
        slot_label: str = "",
        group_title: str = "",
        output_path: str = "out/digest-card.html",
    ) -> dict[str, Any]:
        if not output_path.startswith("out/") or not output_path.endswith(".html"):
            raise ValueError("简报卡 HTML 必须写入 out/*.html")
        now_dt = datetime.fromtimestamp(self.clock() + UTC8_OFFSET, tz=timezone.utc)
        hour = now_dt.hour
        if "夜" in slot_label or "晚" in slot_label or hour >= 20 or hour < 5:
            palette = ("#0F172A", "#1E293B", "#F8FAFC", "#CBD5E1", "#60A5FA", "#1E293B")
            band = "夜览"
        elif "晨" in slot_label or "早" in slot_label or 5 <= hour < 10:
            palette = ("#FFF8EE", "#FFE4C4", "#231815", "#6B4F43", "#D97706", "#FFFFFF")
            band = "晨报"
        elif "傍晚" in slot_label or "暮" in slot_label or 17 <= hour < 20:
            palette = ("#FFF2EC", "#FFD6C2", "#26181A", "#6D4C52", "#E11D48", "#FFFDFC")
            band = "暮色"
        elif "上午" in slot_label or 10 <= hour < 13:
            palette = ("#F1F8FF", "#DCEEFF", "#16202A", "#4A6075", "#2563EB", "#FFFFFF")
            band = "午前"
        else:
            palette = ("#F4F9F6", "#DDF1E6", "#18241E", "#4A6355", "#0F766E", "#FFFFFF")
            band = "午后"

        bg1, bg2, ink, muted, accent, card_bg = palette
        label_text = html.escape(slot_label or now_dt.strftime("%m月%d日 · %H:%M"))
        title_text = html.escape(group_title or "今日高信噪比资讯精选")

        cards_html: list[str] = []
        for idx, it in enumerate(items[:8], 1):
            is_explore = it.get("slot") == "explore"
            badge = "跨界视角" if is_explore else (html.escape(str(it.get("topic") or "精选")))
            source_host = html.escape(str(it.get("source") or _host_of(it.get("url", ""))))
            summary = html.escape(str(it.get("summary") or it.get("why") or ""))
            why_line = html.escape(
                str(
                    (it.get("bridge") if is_explore and it.get("bridge") else "")
                    or it.get("why")
                    or it.get("note")
                    or it.get("bridge")
                    or ""
                )
            )
            avg_score = it.get("avg")
            score_tag = f" · 评分 {avg_score:.1f}" if isinstance(avg_score, (int, float)) else ""
            cards_html.append(
                f"""<article class="card">
  <div class="meta">
    <span class="num">{idx:02d}</span>
    <span class="badge">{badge}</span>
    <span class="src">{source_host}{html.escape(score_tag)}</span>
  </div>
  <h2>{html.escape(str(it.get("title") or ""))}</h2>
  <p class="sum">{summary}</p>
  {f'<p class="why">看点：{why_line}</p>' if why_line else ''}
</article>"""
            )

        doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title_text} — {label_text}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 32px 28px;
    font-family: "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", system-ui, sans-serif;
    background: linear-gradient(155deg, {bg1} 0%, {bg2} 100%);
    color: {ink};
  }}
  .wrap {{ max-width: 780px; margin: 0 auto; }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    padding-bottom: 16px;
    margin-bottom: 18px;
    border-bottom: 2px solid {accent};
  }}
  .kicker {{ font-size: 12px; letter-spacing: 0.14em; color: {accent}; font-weight: 700; text-transform: uppercase; }}
  h1 {{ margin: 4px 0 0; font-size: 24px; line-height: 1.25; }}
  .slot {{ font-size: 13px; color: {muted}; font-weight: 600; }}
  .card {{
    background: {card_bg};
    border: 1px solid rgba(120, 130, 145, 0.22);
    border-left: 4px solid {accent};
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 12px;
  }}
  .meta {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 12px; color: {muted}; }}
  .num {{ font-family: ui-monospace, monospace; font-weight: 700; color: {accent}; }}
  .badge {{
    padding: 1px 7px;
    border-radius: 4px;
    background: rgba(120, 130, 145, 0.14);
    color: {ink};
    font-weight: 600;
  }}
  .src {{ margin-left: auto; font-family: ui-monospace, monospace; }}
  h2 {{ margin: 0 0 6px; font-size: 16px; line-height: 1.38; }}
  p {{ margin: 0; font-size: 13.5px; line-height: 1.55; color: {muted}; }}
  .why {{ margin-top: 6px; color: {ink}; font-size: 13px; }}
  footer {{ margin-top: 14px; font-size: 11.5px; color: {muted}; text-align: right; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <div class="kicker">DIGEST · {band}简报</div>
      <h1>{title_text}</h1>
    </div>
    <div class="slot">{label_text}</div>
  </header>
  {"".join(cards_html)}
  <footer>共入选 {len(items[:8])} 条 · 基于时效、引文核验与五维精选</footer>
</div>
</body>
</html>"""
        res = self.work.write(output_path, doc)
        self.work.commit()
        return {"path": output_path, "bytes": res["bytes"], "count": len(items[:8])}


async def run_digest_tool(work, arguments: dict[str, Any]) -> dict[str, Any]:
    dw = DigestWorkspace(work)
    action = arguments["action"]
    if action == "status":
        return dw.status()
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
        candidates = arguments.get("candidates")
        if candidates is None:
            candidates = dw._load_history().get("last_selected") or []
        return dw.render_card_html(
            list(candidates or []),
            slot_label=str(arguments.get("slot_label") or ""),
            group_title=str(arguments.get("group_title") or ""),
            output_path=str(arguments.get("output_path") or "out/digest-card.html"),
        )
    raise ValueError("未知 digest_feed 操作")
