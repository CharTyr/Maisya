"""统一联网路由：宿主预设优先，未启用时兼容第三方搜索插件或已登记服务。"""

import asyncio
import http.client
import importlib.util
import ipaddress
import json
import random
import re
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .page_date import published_from_html, published_from_text
from .scout import call_capability, safe_url
from .vault import redact_values, secret_variants
from .workspaces import completed_thread

GOOGLE_SEARCH_PLUGIN_ID = "xxxxx7258.google-search-plugin"
HOST_DISABLED_MARKERS = ("统一联网功能未启用", "当前操作未配置联网服务")
ENGINE_ORDER = (
    "deepseek",
    "tavily",
    "anysearch",
    "exa",
    "you",
    "you_news",
    "duckduckgo",
)
KEYED_ENGINES = frozenset({"deepseek", "tavily", "exa", "you", "you_news"})
DEFAULT_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
)
NON_HTML_PREFIXES = (
    "image/",
    "audio/",
    "video/",
    "application/pdf",
    "application/zip",
    "application/octet-stream",
)


class _HostWebDisabledError(RuntimeError):
    """宿主统一联网功能未开启或未给当前动作分配服务。"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _is_valid_http_url(url: Any) -> bool:
    if not isinstance(url, str) or not url.strip() or any(ch.isspace() for ch in url):
        return False
    parts = urlsplit(url.strip())
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


_BROAD_API_ENGINE_ORDER = ("exa", "tavily", "you_news", "you", "anysearch")
_BROAD_TRACKING_PARAM_PREFIXES = ("utm_", "mtm_", "pk_")
_BROAD_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "yclid",
        "spm",
        "from",
        "ref",
        "source",
        "_ga",
        "mc_cid",
        "mc_eid",
        "igshid",
        "share_token",
    }
)


def _normalize_search_url_key(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(":80") or host.endswith(":443"):
        host = host.rsplit(":", 1)[0]
    path = re.sub(r"/+", "/", parts.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    kept_q = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not (
            k.lower() in _BROAD_TRACKING_PARAMS
            or k.lower().startswith(_BROAD_TRACKING_PARAM_PREFIXES)
        )
    ]
    kept_q.sort()
    query = urlencode(kept_q)
    return urlunsplit(("", host, path, query, ""))


def _normalize_search_item(item: dict[str, Any]) -> dict[str, Any] | None:
    raw_url = item.get("url")
    url = raw_url.strip() if isinstance(raw_url, str) else ""
    if not _is_valid_http_url(url):
        return None
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip() or url
    snippet = str(item.get("snippet") or item.get("summary") or "").strip()
    summary = str(item.get("summary") or snippet).strip()
    published = item.get("published_at")
    return {
        "title": title,
        "url": url,
        "snippet": snippet,
        "summary": summary,
        "published_at": str(published) if published else None,
    }


def normalize_search_payload(query: str, items: list[dict[str, Any]], service: str) -> dict[str, Any]:
    normalized = []
    seen = set()
    for item in items:
        row = _normalize_search_item(item)
        if row and row["url"] not in seen:
            seen.add(row["url"])
            normalized.append(row)
    return {
        "query": query,
        "results": normalized,
        "content_kind": "search_results",
        "service": service,
        "retrieved_at": _now_iso(),
    }


def normalize_fetch_payload(
    url: str,
    title: str,
    content: str,
    service: str,
    *,
    published_at: str = "",
    feed_links: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "url": url,
        "title": title,
        "content": content,
        "content_kind": "page_text",
        "service": service,
        "published_at": str(published_at or ""),
        "feed_links": list(feed_links or []),
        "retrieved_at": _now_iso(),
    }


def _is_global_ip(raw_ip: str) -> bool:
    bare = raw_ip.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(bare)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
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


def _resolve_public_ips(url: str, *, proxy: str = "") -> tuple[str, ...]:
    safe_url(url)
    if proxy:
        return ()
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower().rstrip(".")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        bare = hostname.split("%", 1)[0]
        ip = ipaddress.ip_address(bare)
        if not _is_global_ip(str(ip)):
            raise ValueError("不能读取内网或云元数据地址")
        return (str(ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip),)
    except ValueError as exc:
        if "不能读取内网或云元数据地址" in str(exc):
            raise
    try:
        packed = socket.inet_aton(hostname)
    except OSError:
        packed = None
    if packed is not None:
        ipv4 = ipaddress.IPv4Address(packed)
        if not _is_global_ip(str(ipv4)):
            raise ValueError("不能读取内网或云元数据地址")
        return (str(ipv4),)
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        return ()
    pinned: list[str] = []
    seen: set[str] = set()
    for _family, _kind, _proto, _canon, sockaddr in infos:
        host_ip = str(sockaddr[0]).split("%", 1)[0]
        if not _is_global_ip(host_ip):
            raise ValueError("不能读取内网或云元数据地址")
        if host_ip not in seen:
            seen.add(host_ip)
            pinned.append(host_ip)
    return tuple(pinned)


def _assert_public_host(url: str, *, proxy: str = "") -> str:
    _resolve_public_ips(url, proxy=proxy)
    return url


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _connect_pinned_ips(
    pinned_ips: tuple[str, ...],
    port: int,
    timeout: Any,
    source_address: Any = None,
) -> socket.socket:
    last_err: OSError | None = None
    sock_timeout = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
    for raw_ip in pinned_ips:
        ip_str = raw_ip.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            if sock_timeout is not None:
                sock.settimeout(sock_timeout)
            if source_address is not None:
                sock.bind(source_address)
            if ip_obj.version == 6:
                sock.connect((str(ip_obj), port, 0, 0))
            else:
                sock.connect((str(ip_obj), port))
            return sock
        except OSError as err:
            last_err = err
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    if last_err is not None:
        raise last_err
    raise OSError("无法连接到已核验的公网 IP 地址")


def _make_pinned_http_cls(pinned_ips: tuple[str, ...]):
    class _PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = _connect_pinned_ips(
                pinned_ips, self.port, self.timeout, self.source_address
            )
            if self._tunnel_host:
                self._tunnel()

    return _PinnedHTTPConnection


def _make_pinned_https_cls(pinned_ips: tuple[str, ...]):
    class _PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            raw_sock = _connect_pinned_ips(
                pinned_ips, self.port, self.timeout, self.source_address
            )
            self.sock = raw_sock
            try:
                if self._tunnel_host:
                    self._tunnel()
                server_hostname = self._tunnel_host if self._tunnel_host else self.host
                self.sock = self._context.wrap_socket(
                    raw_sock, server_hostname=server_hostname
                )
            except BaseException:
                try:
                    raw_sock.close()
                except OSError:
                    pass
                raise

    return _PinnedHTTPSConnection


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ips: tuple[str, ...], debuglevel: int = 0) -> None:
        super().__init__(debuglevel=debuglevel)
        self._conn_cls = _make_pinned_http_cls(pinned_ips)

    def http_open(self, req: urllib.request.Request):
        return self.do_open(self._conn_cls, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(
        self, pinned_ips: tuple[str, ...], debuglevel: int = 0, context: Any = None
    ) -> None:
        super().__init__(debuglevel=debuglevel, context=context)
        self._conn_cls = _make_pinned_https_cls(pinned_ips)

    def https_open(self, req: urllib.request.Request):
        return self.do_open(self._conn_cls, req, context=self._context)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, max_redirects: int = 5, proxy: str = ""):
        self.max_redirects = max_redirects
        self.proxy = proxy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        visited = getattr(req, "redirect_dict", {})
        if len(visited) >= self.max_redirects:
            raise ValueError("网页重定向次数过多")
        target = urljoin(req.full_url, str(newurl))
        _assert_public_host(target, proxy=self.proxy)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _validated_proxy(proxy: str) -> str:
    cleaned = str(proxy or "").strip()
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("第三方搜索代理仅支持 HTTP(S) 地址")
    return cleaned


def _default_http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 20.0,
    proxy: str = "",
    max_bytes: int = 2 * 1024 * 1024,
    public_only: bool = True,
) -> tuple[int, dict[str, str], bytes, str]:
    proxy_url = _validated_proxy(proxy)
    body = (
        json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        if json_body is not None
        else None
    )
    req_headers = dict(headers or {})
    if body is not None:
        req_headers.setdefault("Content-Type", "application/json")
    proxy_map = {"http": proxy_url, "https": proxy_url} if proxy_url else {}

    current = url
    for _hop in range(5):
        if public_only:
            pinned_ips = _resolve_public_ips(current, proxy=proxy_url)
        else:
            safe_url(current)
            pinned_ips = ()
        request = urllib.request.Request(
            current, data=body, headers=req_headers, method=method.upper()
        )
        handlers: list[Any] = [urllib.request.ProxyHandler(proxy_map)]
        if public_only and not proxy_url:
            handlers.append(_NoRedirectHandler())
            if pinned_ips:
                handlers.extend(
                    [_PinnedHTTPHandler(pinned_ips), _PinnedHTTPSHandler(pinned_ips)]
                )
        else:
            handlers.append(_SafeRedirectHandler(proxy=proxy_url))
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                if public_only and not proxy_url and status in {301, 302, 303, 307, 308}:
                    loc = response.headers.get("Location", "")
                    if loc:
                        current = urljoin(current, loc)
                        continue
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raw = raw[:max_bytes]
                resp_headers = {k.lower(): v for k, v in response.headers.items()}
                return status, resp_headers, raw, str(response.geturl() or current)
        except urllib.error.HTTPError as err:
            if public_only and not proxy_url and err.code in {301, 302, 303, 307, 308}:
                loc = err.headers.get("Location", "") if err.headers else ""
                if loc:
                    current = urljoin(current, loc)
                    continue
            err_body = err.read(65536) if err.fp else b""
            resp_headers = {k.lower(): v for k, v in (err.headers or {}).items()}
            return int(err.code), resp_headers, err_body, str(err.geturl() or current)
    raise ValueError("网页重定向次数过多")


class _TextExtractor(HTMLParser):
    _SKIP_TAGS = frozenset(
        {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside"}
    )
    _BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "article",
            "section",
            "main",
            "blockquote",
            "pre",
        }
    )

    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._base_url = base_url
        self.title_parts: list[str] = []
        self.parts: list[str] = []
        self.feed_links: list[str] = []
        self._seen_feed_links: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        name = tag.lower()
        if name == "link":
            attr_map = {k.lower(): (v or "").strip() for k, v in attrs if k}
            rels = set(attr_map.get("rel", "").lower().split())
            ltype = attr_map.get("type", "").lower().split(";", 1)[0].strip()
            href = attr_map.get("href", "").strip()
            if (
                "alternate" in rels
                and ltype in {"application/rss+xml", "application/atom+xml"}
                and href
            ):
                resolved = urljoin(self._base_url, href) if self._base_url else href
                try:
                    scheme = urlsplit(resolved).scheme.lower()
                except ValueError:
                    scheme = ""
                if scheme in {"http", "https"} and resolved not in self._seen_feed_links:
                    self._seen_feed_links.add(resolved)
                    self.feed_links.append(resolved)
        if name == "title" and self._skip_depth == 0:
            self._in_title = True
        if name in self._SKIP_TAGS:
            self._skip_depth += 1
        elif name in self._BLOCK_TAGS and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "title":
            self._in_title = False
        if name in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif name in self._BLOCK_TAGS and self._skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip_depth == 0 and data.strip():
            self.parts.append(data)

    def title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()

    def text(self) -> str:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _decode_bytes(raw: bytes, content_type: str) -> str:
    match = re.search(r"charset=([A-Za-z0-9_.-]+)", content_type or "", re.IGNORECASE)
    candidates = [match.group(1)] if match else []
    candidates.extend(["utf-8", "gb18030"])
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_readable_with_meta(
    url: str, raw: bytes, content_type: str, max_chars: int
) -> tuple[str, str, str, list[str]]:
    decoded = _decode_bytes(raw, content_type)
    if "html" not in content_type.lower() and "<html" not in decoded[:512].lower():
        content = decoded.strip()[:max_chars]
        return url, content, published_from_text(content), []

    parser = _TextExtractor(base_url=url)
    parser.feed(decoded)
    parser.close()
    title = parser.title() or url
    feed_links = list(parser.feed_links)

    content = ""
    try:
        import trafilatura

        extracted = trafilatura.extract(
            decoded, include_comments=False, include_tables=True, no_fallback=False
        )
        if extracted and len(extracted.strip()) > 100:
            content = extracted.strip()[:max_chars]
    except Exception:
        pass

    if not content:
        try:
            from readability import Document

            summary_html = Document(
                decoded, min_text_length=50, retry_length=250, url=url
            ).summary()
            summary_parser = _TextExtractor()
            summary_parser.feed(summary_html)
            summary_parser.close()
            text = summary_parser.text()
            if text and len(text) > 100:
                content = text[:max_chars]
        except Exception:
            pass

    if not content:
        content = parser.text()[:max_chars]

    pub = published_from_html(decoded) or published_from_text(content)
    return title, content, pub, feed_links


def _extract_readable_text(url: str, raw: bytes, content_type: str, max_chars: int) -> tuple[str, str]:
    title, content, _pub, _feeds = _extract_readable_with_meta(url, raw, content_type, max_chars)
    return title, content


def _ephemeral_secrets(cfg: dict[str, Any]) -> set[str]:
    secrets: set[str] = set()
    engines = cfg.get("engines") if isinstance(cfg.get("engines"), dict) else {}
    backend = (
        cfg.get("search_backend") if isinstance(cfg.get("search_backend"), dict) else {}
    )
    for key_field in (
        "exa_api_keys",
        "tavily_api_keys",
        "anysearch_api_keys",
        "you_api_keys",
        "deepseek_api_keys",
    ):
        values = engines.get(key_field)
        if isinstance(values, list):
            for entry in values:
                if isinstance(entry, str) and entry.strip():
                    secrets.update(secret_variants(entry.strip()))
    for single in ("tavily_api_key",):
        val = engines.get(single)
        if isinstance(val, str) and val.strip():
            secrets.update(secret_variants(val.strip()))
    cookies = backend.get("zhihu_cookies")
    if isinstance(cookies, str) and cookies.strip():
        secrets.update(secret_variants(cookies.strip()))
        for part in cookies.split(";"):
            part = part.strip()
            if "=" in part:
                _, cval = part.split("=", 1)
                if len(cval.strip()) >= 6:
                    secrets.update(secret_variants(cval.strip()))
    proxy = backend.get("proxy")
    if isinstance(proxy, str) and proxy.strip():
        parsed = urlsplit(proxy.strip())
        for cred in (parsed.username, parsed.password):
            if cred:
                secrets.update(secret_variants(cred))
    return secrets


def _tavily_keys(engines: dict[str, Any]) -> list[str]:
    keys = [
        k.strip()
        for k in (engines.get("tavily_api_keys") or [])
        if isinstance(k, str) and k.strip()
    ]
    single = engines.get("tavily_api_key")
    if isinstance(single, str) and single.strip() and single.strip() not in keys:
        keys.append(single.strip())
    return keys


def _has_usable_keys(engine: str, engines: dict[str, Any]) -> bool:
    if engine == "tavily":
        return bool(_tavily_keys(engines))
    if engine == "exa":
        return any(isinstance(k, str) and k.strip() for k in (engines.get("exa_api_keys") or []))
    if engine in {"you", "you_news"}:
        return any(isinstance(k, str) and k.strip() for k in (engines.get("you_api_keys") or []))
    if engine == "deepseek":
        return any(
            isinstance(k, str) and k.strip() for k in (engines.get("deepseek_api_keys") or [])
        )
    if engine == "duckduckgo":
        return importlib.util.find_spec("ddgs") is not None
    return engine == "anysearch"


def enabled_third_party_engines(cfg: dict[str, Any]) -> list[str]:
    plugin_sec = cfg.get("plugin") if isinstance(cfg.get("plugin"), dict) else {}
    if plugin_sec.get("enabled", True) is False:
        return []
    backend = (
        cfg.get("search_backend") if isinstance(cfg.get("search_backend"), dict) else {}
    )
    engines = cfg.get("engines") if isinstance(cfg.get("engines"), dict) else {}
    default_engine = str(backend.get("default_engine") or "")
    ordered = list(ENGINE_ORDER)
    if default_engine in ENGINE_ORDER:
        ordered = [default_engine, *(name for name in ENGINE_ORDER if name != default_engine)]
    return [
        name
        for name in ordered
        if engines.get(f"{name}_enabled") is True and _has_usable_keys(name, engines)
    ]


def _unwrap_plugin_config(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if isinstance(raw, dict) and "success" in raw and "value" in raw:
        if raw.get("success") is False:
            return None
        raw = raw.get("value")
    if not isinstance(raw, dict):
        return None
    plugin_sec = raw.get("plugin")
    if isinstance(plugin_sec, dict) and plugin_sec.get("enabled") is False:
        return None
    return raw


def _exa_payload(query: str, limit: int, engines: dict[str, Any]) -> dict[str, Any]:
    search_type = str(engines.get("exa_search_type") or "auto")
    include_highlights = bool(engines.get("exa_include_highlights", True))
    contents: dict[str, Any] = {"highlights": include_highlights}
    if include_highlights:
        hl: dict[str, Any] = {}
        hq = str(engines.get("exa_highlights_query") or "").strip()
        if hq:
            hl["query"] = hq
        hmax = int(engines.get("exa_highlights_max_characters") or 0)
        if hmax > 0:
            hl["maxCharacters"] = hmax
        if hl:
            contents["highlights"] = hl
    if engines.get("exa_include_text"):
        tmax = int(engines.get("exa_text_max_characters") or 0)
        contents["text"] = {"maxCharacters": tmax} if tmax > 0 else True
    max_age = int(engines.get("exa_max_age_hours", -2))
    if max_age != -2:
        contents["maxAgeHours"] = max_age
    livecrawl = int(engines.get("exa_livecrawl_timeout_ms") or 0)
    if livecrawl > 0:
        contents["livecrawlTimeout"] = livecrawl

    payload: dict[str, Any] = {
        "query": query,
        "numResults": limit,
        "type": search_type,
        "contents": contents,
    }
    for cfg_key, api_key in (
        ("exa_category", "category"),
        ("exa_start_published_date", "startPublishedDate"),
        ("exa_end_published_date", "endPublishedDate"),
        ("exa_user_location", "userLocation"),
        ("exa_objective", "objective"),
    ):
        val = engines.get(cfg_key)
        if isinstance(val, str) and val.strip():
            payload[api_key] = val.strip()
    for cfg_key, api_key in (
        ("exa_include_domains", "includeDomains"),
        ("exa_exclude_domains", "excludeDomains"),
    ):
        vals = [str(x).strip() for x in (engines.get(cfg_key) or []) if str(x).strip()]
        if vals:
            payload[api_key] = vals
    if engines.get("exa_moderation"):
        payload["moderation"] = True
    add_q = [
        str(x).strip()
        for x in (engines.get("exa_additional_queries") or [])
        if str(x).strip()
    ]
    if add_q and search_type in {"deep-lite", "deep"}:
        payload["additionalQueries"] = add_q[:10]
    return payload


def _deepseek_url(base: str) -> str:
    url = (base or "https://api.deepseek.com").strip().rstrip("/")
    if url.endswith("/messages"):
        return safe_url(url)
    if url.endswith("/anthropic/v1"):
        return safe_url(f"{url}/messages")
    if url.endswith("/anthropic"):
        return safe_url(f"{url}/v1/messages")
    return safe_url(f"{url}/anthropic/v1/messages")


def _search_exa(
    query: str,
    limit: int,
    engines: dict[str, Any],
    timeout: float,
    proxy: str,
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
) -> tuple[bool, list[dict[str, Any]]]:
    payload = _exa_payload(query, limit, engines)
    for api_key in [
        k.strip() for k in (engines.get("exa_api_keys") or []) if isinstance(k, str) and k.strip()
    ]:
        status, _, raw, _ = http_request(
            "POST",
            "https://api.exa.ai/search",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-api-key": api_key,
            },
            json_body=payload,
            timeout=timeout,
            proxy=proxy,
            public_only=True,
        )
        if status >= 400 or not raw:
            continue
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("results"), list):
            continue
        items = []
        for entry in data["results"]:
            if not isinstance(entry, dict):
                continue
            highlights = entry.get("highlights")
            excerpts = (
                [t.strip() for t in highlights if isinstance(t, str) and t.strip()]
                if isinstance(highlights, list)
                else []
            )
            text = str(entry.get("text") or "").strip()
            snippet = "\n".join(excerpts) or text
            items.append(
                {
                    "title": entry.get("title"),
                    "url": entry.get("url"),
                    "snippet": snippet,
                    "summary": snippet,
                    "published_at": entry.get("publishedDate") or entry.get("published_date"),
                }
            )
        return True, items[:limit]
    return False, []


def _search_tavily(
    query: str,
    limit: int,
    engines: dict[str, Any],
    timeout: float,
    proxy: str,
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
) -> tuple[bool, list[dict[str, Any]]]:
    custom = str(engines.get("tavily_base_url") or "").strip().rstrip("/")
    base_url = safe_url(custom) if custom else "https://api.tavily.com"
    base_payload: dict[str, Any] = {
        "query": query,
        "search_depth": str(engines.get("tavily_search_depth") or "basic"),
        "max_results": limit,
        "include_answer": bool(engines.get("tavily_include_answer", True)),
        "include_raw_content": bool(engines.get("tavily_include_raw_content", False)),
        "turbo": bool(engines.get("tavily_turbo", False)),
    }
    topic = str(engines.get("tavily_topic") or "").strip()
    if topic:
        base_payload["topic"] = topic
    for api_key in _tavily_keys(engines):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        payload = dict(base_payload)
        if custom:
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            payload["api_key"] = api_key
        status, _, raw, _ = http_request(
            "POST",
            f"{base_url}/search",
            headers=headers,
            json_body=payload,
            timeout=timeout,
            proxy=proxy,
            public_only=True,
        )
        if status >= 400 or not raw:
            continue
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            continue
        items = [
            {
                "title": entry.get("title"),
                "url": entry.get("url"),
                "snippet": entry.get("content") or entry.get("snippet") or entry.get("raw_content") or "",
                "summary": entry.get("content") or entry.get("snippet") or "",
                "published_at": entry.get("published_date"),
            }
            for entry in data["results"]
            if isinstance(entry, dict)
        ]
        return True, items[:limit]
    return False, []


def _search_anysearch(
    query: str,
    limit: int,
    engines: dict[str, Any],
    timeout: float,
    proxy: str,
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
) -> tuple[bool, list[dict[str, Any]]]:
    keys = [
        k.strip()
        for k in (engines.get("anysearch_api_keys") or [])
        if isinstance(k, str) and k.strip()
    ] or [None]
    payload: dict[str, Any] = {
        "query": query,
        "max_results": max(1, min(limit, 10)),
        "format": str(engines.get("anysearch_format") or "json"),
    }
    tag = str(engines.get("anysearch_tag") or "").strip()
    if tag:
        payload["tag"] = tag
    zone = str(engines.get("anysearch_zone") or "").strip()
    if zone in {"cn", "intl"}:
        payload["zone"] = zone
    language = str(engines.get("anysearch_language") or "").strip()
    if language:
        payload["language"] = language
    raw_params = str(engines.get("anysearch_params") or "").strip()
    if raw_params:
        try:
            parsed_params = json.loads(raw_params)
            if isinstance(parsed_params, dict) and parsed_params:
                payload["params"] = parsed_params
        except ValueError:
            pass
    for api_key in keys:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        status, _, raw, _ = http_request(
            "POST",
            "https://api.anysearch.com/v1/search",
            headers=headers,
            json_body=payload,
            timeout=timeout,
            proxy=proxy,
            public_only=True,
        )
        if status >= 400 or not raw:
            continue
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict) or data.get("code") != 0:
            continue
        items = (data.get("data") or {}).get("results") if isinstance(data.get("data"), dict) else None
        if not isinstance(items, list):
            continue
        mapped = [
            {
                "title": entry.get("title"),
                "url": entry.get("url"),
                "snippet": entry.get("snippet") or entry.get("content") or "",
                "summary": entry.get("content") or entry.get("snippet") or "",
            }
            for entry in items
            if isinstance(entry, dict)
        ]
        return True, mapped[:limit]
    return False, []


def _search_you(
    query: str,
    limit: int,
    engines: dict[str, Any],
    timeout: float,
    proxy: str,
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
    *,
    news: bool = False,
) -> tuple[bool, list[dict[str, Any]]]:
    keys = [
        k.strip() for k in (engines.get("you_api_keys") or []) if isinstance(k, str) and k.strip()
    ]
    if news:
        endpoint = "https://api.ydc-index.io/livenews?" + urlencode({"q": query, "count": limit})
    else:
        params: dict[str, Any] = {"query": query, "count": limit}
        for key in ("freshness", "country", "language", "safesearch", "livecrawl", "livecrawl_formats"):
            val = engines.get(f"you_{key}")
            if isinstance(val, str) and val.strip():
                params[key] = val.strip()
        endpoint = "https://ydc-index.io/v1/search?" + urlencode(params)
    for api_key in keys:
        status, _, raw, _ = http_request(
            "GET",
            endpoint,
            headers={"Accept": "application/json", "X-API-Key": api_key},
            timeout=timeout,
            proxy=proxy,
            public_only=True,
        )
        if status >= 400 or not raw:
            continue
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict):
            continue
        if news:
            entries = (data.get("news") or {}).get("results") if isinstance(data.get("news"), dict) else None
            if not isinstance(entries, list):
                continue
            items = [
                {"title": e.get("title"), "url": e.get("url"), "snippet": e.get("description") or ""}
                for e in entries
                if isinstance(e, dict)
            ]
            return True, items[:limit]
        results_obj = data.get("results")
        if not isinstance(results_obj, dict):
            continue
        items = []
        for section_key in ("web", "news"):
            section = results_obj.get(section_key)
            if not isinstance(section, list):
                continue
            for e in section:
                if not isinstance(e, dict):
                    continue
                snippets = e.get("snippets")
                first_snip = next((s for s in snippets if isinstance(s, str) and s.strip()), "") if isinstance(snippets, list) else ""
                items.append(
                    {
                        "title": e.get("title"),
                        "url": e.get("url"),
                        "snippet": first_snip or e.get("description") or "",
                        "summary": e.get("description") or first_snip,
                    }
                )
        return True, items[:limit]
    return False, []


def _search_deepseek(
    query: str,
    limit: int,
    engines: dict[str, Any],
    timeout: float,
    proxy: str,
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
) -> tuple[bool, list[dict[str, Any]]]:
    endpoint = _deepseek_url(str(engines.get("deepseek_base_url") or ""))
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "model": str(engines.get("deepseek_model") or "deepseek-chat"),
        "max_tokens": int(engines.get("deepseek_max_tokens") or 2048),
        "system": f"当前现实时间为：{now_str} UTC。",
        "messages": [{"role": "user", "content": query}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }
    for api_key in [
        k.strip() for k in (engines.get("deepseek_api_keys") or []) if isinstance(k, str) and k.strip()
    ]:
        status, _, raw, _ = http_request(
            "POST",
            endpoint,
            headers={
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-api-key": api_key,
            },
            json_body=payload,
            timeout=timeout,
            proxy=proxy,
            public_only=True,
        )
        if status >= 400 or not raw:
            continue
        data = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            continue
        items = []
        for block in data["content"]:
            if not isinstance(block, dict) or block.get("type") != "web_search_tool_result":
                continue
            for item in block.get("content") or []:
                if not isinstance(item, dict):
                    continue
                entry = (
                    item["web_search_result"]
                    if isinstance(item.get("web_search_result"), dict)
                    else item
                )
                items.append(
                    {
                        "title": entry.get("title"),
                        "url": entry.get("url"),
                        "snippet": entry.get("snippet") or entry.get("content") or entry.get("title") or "",
                    }
                )
        return True, items[:limit]
    return False, []


def _search_duckduckgo(query: str, limit: int, engines: dict[str, Any], timeout: float) -> tuple[bool, list[dict[str, Any]]]:
    from ddgs import DDGS

    kwargs: dict[str, Any] = {
        "max_results": limit,
        "region": str(engines.get("duckduckgo_region") or "wt-wt"),
        "backend": str(engines.get("duckduckgo_backend") or "auto"),
        "safesearch": str(engines.get("duckduckgo_safesearch") or "moderate"),
    }
    tl = str(engines.get("duckduckgo_timelimit") or "").strip()
    if tl and tl != "none":
        kwargs["timelimit"] = tl
    with DDGS(timeout=int(timeout)) as ddgs:
        rows = ddgs.text(query, **kwargs)
    return True, [
        {
            "title": r.get("title"),
            "url": r.get("href"),
            "snippet": r.get("body") or "",
        }
        for r in (rows or [])
        if isinstance(r, dict)
    ]


def _query_third_party_engine(
    engine: str,
    query: str,
    limit: int,
    engines: dict[str, Any],
    timeout: float,
    proxy: str,
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
) -> tuple[bool, list[dict[str, Any]]]:
    if engine == "exa":
        return _search_exa(query, limit, engines, timeout, proxy, http_request)
    if engine == "tavily":
        return _search_tavily(query, limit, engines, timeout, proxy, http_request)
    if engine == "anysearch":
        return _search_anysearch(query, limit, engines, timeout, proxy, http_request)
    if engine == "you":
        return _search_you(query, limit, engines, timeout, proxy, http_request, news=False)
    if engine == "you_news":
        return _search_you(query, limit, engines, timeout, proxy, http_request, news=True)
    if engine == "deepseek":
        return _search_deepseek(query, limit, engines, timeout, proxy, http_request)
    if engine == "duckduckgo":
        return _search_duckduckgo(query, limit, engines, timeout)
    return False, []


def _select_broad_engines(cfg: dict[str, Any], max_engines: int) -> list[str]:
    candidates = enabled_third_party_engines(cfg)
    if not candidates:
        return []
    active = set(candidates)
    selected = [e for e in _BROAD_API_ENGINE_ORDER if e in active]
    if len(selected) < 2 and "duckduckgo" in active:
        selected.append("duckduckgo")
    if not selected:
        selected = list(candidates)
    cap = max(1, min(int(max_engines or 3), 5))
    return selected[:cap]


def _run_third_party_broad_search(
    query: str,
    max_results: int,
    cfg: dict[str, Any],
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
    max_engines: int = 3,
) -> dict[str, Any]:
    backend = (
        cfg.get("search_backend") if isinstance(cfg.get("search_backend"), dict) else {}
    )
    engines = cfg.get("engines") if isinstance(cfg.get("engines"), dict) else {}
    timeout = float(max(5, min(int(backend.get("timeout") or 20), 45)))
    proxy = str(backend.get("proxy") or "").strip()
    configured_max = int(backend.get("max_results") or 10)
    per_engine_limit = max(1, min(int(max_results or 8), configured_max, 10))
    total_limit = max(per_engine_limit, min(int(max_results or 8), 24))
    selected = _select_broad_engines(cfg, max_engines)
    if not selected:
        raise RuntimeError("第三方联网搜索插件未启用任何可用搜索引擎")

    per_engine_rows: dict[str, tuple[bool, list[dict[str, Any]]]] = {}
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {
            pool.submit(
                _query_third_party_engine,
                engine,
                query,
                per_engine_limit,
                engines,
                timeout,
                proxy,
                http_request,
            ): engine
            for engine in selected
        }
        for fut in as_completed(futures):
            engine = futures[fut]
            try:
                per_engine_rows[engine] = fut.result()
            except Exception:
                per_engine_rows[engine] = (False, [])

    merged_by_key: dict[str, dict[str, Any]] = {}
    ordered_keys: list[str] = []
    ok_engines: list[str] = []
    for engine in selected:
        ok, items = per_engine_rows.get(engine, (False, []))
        if ok:
            ok_engines.append(engine)
        if not ok or not items:
            continue
        for raw_item in items:
            row = _normalize_search_item(raw_item)
            if row is None:
                continue
            key = _normalize_search_url_key(row["url"]) or row["url"]
            existing = merged_by_key.get(key)
            if existing is None:
                row["engines"] = [engine]
                merged_by_key[key] = row
                ordered_keys.append(key)
            else:
                if engine not in existing["engines"]:
                    existing["engines"].append(engine)
                if not existing.get("published_at") and row.get("published_at"):
                    existing["published_at"] = row["published_at"]
                if (
                    existing.get("title") == existing.get("url")
                    and row.get("title")
                    and row["title"] != row["url"]
                ):
                    existing["title"] = row["title"]
                if (not existing.get("snippet") or len(existing["snippet"]) < 20) and row.get("snippet"):
                    existing["snippet"] = row["snippet"]
                    existing["summary"] = row["summary"]

    if not ok_engines and not ordered_keys:
        raise RuntimeError("第三方联网搜索未返回可用结果，请检查搜索引擎与密钥配置")

    merged = [merged_by_key[k] for k in ordered_keys[:total_limit]]
    used = ok_engines or selected
    payload = {
        "query": query,
        "results": merged,
        "content_kind": "search_results",
        "service": (
            "google_search_plugin:broad"
            if len(used) > 1
            else f"google_search_plugin:{used[0]}"
        ),
        "engines": used,
        "retrieved_at": _now_iso(),
    }
    return redact_values(payload, _ephemeral_secrets(cfg))


def _run_third_party_search(
    query: str,
    max_results: int,
    cfg: dict[str, Any],
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
) -> dict[str, Any]:
    backend = (
        cfg.get("search_backend") if isinstance(cfg.get("search_backend"), dict) else {}
    )
    engines = cfg.get("engines") if isinstance(cfg.get("engines"), dict) else {}
    timeout = float(max(5, min(int(backend.get("timeout") or 20), 45)))
    proxy = str(backend.get("proxy") or "").strip()
    configured_max = int(backend.get("max_results") or 10)
    limit = max(1, min(int(max_results or 5), configured_max, 10))
    candidates = enabled_third_party_engines(cfg)
    if not candidates:
        raise RuntimeError("第三方联网搜索插件未启用任何可用搜索引擎")
    ok_engine = ""
    for engine in candidates:
        try:
            ok, items = _query_third_party_engine(
                engine, query, limit, engines, timeout, proxy, http_request
            )
            if ok:
                payload = normalize_search_payload(
                    query, items, f"google_search_plugin:{engine}"
                )
                if payload["results"]:
                    return payload
                if not ok_engine:
                    ok_engine = engine
        except Exception:
            continue
    if ok_engine:
        return normalize_search_payload(query, [], f"google_search_plugin:{ok_engine}")
    raise RuntimeError("第三方联网搜索未返回可用结果，请检查搜索引擎与密钥配置")


def _run_third_party_fetch(
    url: str,
    cfg: dict[str, Any],
    http_request: Callable[..., tuple[int, dict[str, str], bytes, str]],
) -> dict[str, Any]:
    url = safe_url(url)
    backend = (
        cfg.get("search_backend") if isinstance(cfg.get("search_backend"), dict) else {}
    )
    engines = cfg.get("engines") if isinstance(cfg.get("engines"), dict) else {}
    timeout = float(max(5, min(int(backend.get("content_timeout") or 15), 45)))
    proxy = str(backend.get("proxy") or "").strip()
    max_chars = max(int(backend.get("max_content_length") or 0), 12000)
    max_chars = min(max_chars, 16000)

    you_keys = [
        k.strip() for k in (engines.get("you_api_keys") or []) if isinstance(k, str) and k.strip()
    ]
    if engines.get("you_contents_enabled") and you_keys:
        fmt = str(engines.get("you_contents_format") or "markdown")
        if fmt not in {"markdown", "html"}:
            fmt = "markdown"
        for api_key in you_keys:
            try:
                status, _, raw, _ = http_request(
                    "POST",
                    "https://ydc-index.io/v1/contents",
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "X-API-Key": api_key,
                    },
                    json_body={"urls": [url], "format": fmt},
                    timeout=timeout,
                    proxy=proxy,
                    public_only=True,
                )
                if status >= 400 or not raw:
                    continue
                data = json.loads(raw.decode("utf-8", errors="replace"))
                if not isinstance(data, list):
                    continue
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    text = item.get(fmt) or item.get("markdown") or item.get("html") or ""
                    if isinstance(text, str) and text.strip():
                        trimmed = text.strip()[:max_chars]
                        pub = ""
                        links: list[str] = []
                        if fmt == "html" or "<html" in text[:512].lower():
                            pub = published_from_html(text)
                            parser = _TextExtractor(base_url=url)
                            try:
                                parser.feed(text)
                                parser.close()
                                links = list(parser.feed_links)
                            except Exception:
                                links = []
                        if not pub:
                            pub = published_from_text(trimmed)
                        return normalize_fetch_payload(
                            url,
                            url,
                            trimmed,
                            "google_search_plugin:you_contents",
                            published_at=pub,
                            feed_links=links,
                        )
            except Exception:
                continue

    user_agents = [
        str(ua).strip()
        for ua in (backend.get("user_agents") or DEFAULT_USER_AGENTS)
        if str(ua).strip()
    ] or list(DEFAULT_USER_AGENTS)
    status, headers, raw, final_url = http_request(
        "GET",
        url,
        headers={"User-Agent": random.choice(user_agents)},
        timeout=timeout,
        proxy=proxy,
        max_bytes=2 * 1024 * 1024,
        public_only=True,
    )
    if status >= 400:
        raise RuntimeError(f"网页读取失败（HTTP {status}）")
    ctype = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if any(ctype.startswith(prefix) for prefix in NON_HTML_PREFIXES):
        raise ValueError("目标链接不是可读取的网页或文本内容")
    title, content, pub, feed_links = _extract_readable_with_meta(
        final_url or url, raw, headers.get("content-type", ""), max_chars
    )
    return normalize_fetch_payload(
        final_url or url,
        title,
        content,
        "google_search_plugin:http_fetch",
        published_at=pub,
        feed_links=feed_links,
    )


class WebRouter:
    """按 web_provider 配置路由 web_search 与 web_fetch。"""

    def __init__(
        self,
        host,
        *,
        redact=lambda value: value,
        provider: str | Callable[[], str] = "auto",
        services=None,
        runtime=None,
        http_request=None,
    ):
        self.host = host
        self.redact = redact
        self._provider = provider
        self.services = services
        self.runtime = runtime
        self._http_request = http_request or _default_http_request

    def current_provider(self) -> str:
        value = self._provider() if callable(self._provider) else self._provider
        return str(value or "auto")

    async def load_third_party_config(self) -> dict[str, Any] | None:
        if self.host is None or not hasattr(self.host, "call_capability"):
            return None
        try:
            raw = await call_capability(
                self.host,
                "config.get_plugin",
                timeout_ms=5000,
                plugin_name=GOOGLE_SEARCH_PLUGIN_ID,
            )
        except Exception:
            return None
        return _unwrap_plugin_config(raw)

    async def _call_host_web(self, capability: str, **kwargs):
        try:
            result = await call_capability(
                self.host, capability, timeout_ms=70000, **kwargs
            )
        except Exception as exc:
            message = str(exc) or "宿主只读能力调用失败"
            if any(marker in message for marker in HOST_DISABLED_MARKERS):
                raise _HostWebDisabledError(message) from None
            raise RuntimeError(message) from None
        if result is None:
            raise RuntimeError("宿主只读能力调用失败，请检查该能力配置")
        if isinstance(result, dict) and result.get("success") is False:
            message = str(result.get("error") or "宿主只读能力调用失败，请检查该能力配置")
            if any(marker in message for marker in HOST_DISABLED_MARKERS):
                raise _HostWebDisabledError(message)
            raise RuntimeError(message)
        if isinstance(result, dict) and "result" in result:
            return result["result"]
        return result

    async def _call_third_party(
        self, kind: str, arguments: dict[str, Any], cfg: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if cfg is None:
            cfg = await self.load_third_party_config()
        if cfg is None:
            raise RuntimeError(
                f"未检测到已启用的第三方联网搜索插件 {GOOGLE_SEARCH_PLUGIN_ID}"
            )
        secrets = _ephemeral_secrets(cfg)
        try:
            if kind == "search_broad":
                raw = await completed_thread(
                    _run_third_party_broad_search,
                    arguments["query"],
                    arguments.get("max_results", 8),
                    cfg,
                    self._http_request,
                    arguments.get("max_engines", 3),
                )
            elif kind == "search":
                raw = await completed_thread(
                    _run_third_party_search,
                    arguments["query"],
                    arguments.get("max_results", 5),
                    cfg,
                    self._http_request,
                )
            else:
                raw = await completed_thread(
                    _run_third_party_fetch,
                    arguments["url"],
                    cfg,
                    self._http_request,
                )
            return self.redact(redact_values(raw, secrets))
        except (ValueError, PermissionError) as exc:
            raise type(exc)(redact_values(str(exc), secrets)) from None
        except Exception as exc:
            cleaned = redact_values(str(exc), secrets) or "第三方联网调用失败"
            raise RuntimeError(cleaned) from None

    async def _call_service_web(
        self, kind: str, arguments: dict[str, Any], context: dict[str, Any], mode: str
    ):
        if self.services is None:
            if mode == "any":
                return None
            raise RuntimeError("能力服务尚未登记")
        return await self.services.call_web(
            kind, arguments, context, self.runtime, mode=mode
        )

    async def _call_plugin_api_name(
        self,
        api_name: str,
        kind: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
        *,
        version: str = "",
        binding: dict[str, Any] | None = None,
    ):
        if self.host is None or not hasattr(self.host, "call_capability"):
            raise RuntimeError("跨插件联网调用缺少宿主上下文")
        if self.services is not None:
            apis_cfg = self.services.load().get("apis", {})
            for spec in apis_cfg.values():
                if spec.get("api_name") == api_name and spec.get("access") == "admins":
                    if (
                        context.get("owner") == "self"
                        or self.runtime is None
                        or not await completed_thread(self.runtime.authorized, context)
                    ):
                        raise PermissionError("该联网插件接口需要当前管理员授权")
            call_args = self.services.apply_web_mapping(arguments, binding)
        else:
            call_args = dict(arguments)
        raw = await call_capability(
            self.host,
            "api.call",
            timeout_ms=30000,
            api_name=api_name,
            version=version,
            args=call_args,
        )
        if isinstance(raw, dict):
            if raw.get("success") is False:
                raise RuntimeError(str(raw.get("error") or f"跨插件接口 {api_name} 调用失败"))
            value = raw["result"] if ("success" in raw and "result" in raw) else raw
        else:
            value = raw
        value = self.redact(value)
        if isinstance(value, dict):
            items = value.get("items") if isinstance(value.get("items"), list) else value.get("results")
            return {
                "provider": api_name,
                **value,
                **({"items": items, "results": items} if isinstance(items, list) else {}),
            }
        if kind == "fetch":
            return {
                "provider": api_name,
                "url": arguments["url"],
                "title": arguments["url"],
                "content": str(value)[:16000],
                "content_kind": "page_text",
                "service": f"plugin:{api_name}",
            }
        return {
            "provider": api_name,
            "query": arguments["query"],
            "items": [],
            "results": [],
            "summary": str(value)[:16000],
            "content_kind": "search_results",
            "service": f"plugin:{api_name}",
        }

    async def _resolve_plugin_target(self, target: str, kind: str) -> tuple[str, str]:
        if self.host is not None and hasattr(self.host, "call_capability"):
            try:
                raw = await call_capability(self.host, "api.list", timeout_ms=10000)
                items = (
                    raw.get("apis", [])
                    if isinstance(raw, dict) and "success" in raw
                    else (raw if isinstance(raw, list) else [])
                )
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    plugin_id = str(item.get("plugin_id") or "")
                    name = str(item.get("name") or "")
                    full = f"{plugin_id}.{name}" if plugin_id and name else name
                    if full == target or name == target:
                        return full, str(item.get("version") or "")
                matches = [
                    item
                    for item in items
                    if isinstance(item, dict) and item.get("plugin_id") == target and item.get("name")
                ]
                if matches:
                    chosen = next(
                        (m for m in matches if kind in str(m.get("name", "")).lower()),
                        matches[0],
                    )
                    return f"{chosen['plugin_id']}.{chosen['name']}", str(chosen.get("version") or "")
            except (RuntimeError, OSError, ValueError, TypeError):
                pass
        return target, ""

    async def _call_named_provider(
        self, name: str, kind: str, arguments: dict[str, Any], context: dict[str, Any]
    ):
        if self.services is None:
            raise RuntimeError("能力服务尚未登记")
        cfg = await completed_thread(self.services.load)
        providers = (cfg.get("web") or {}).get("providers") or {}
        spec = providers.get(name)
        if not isinstance(spec, dict):
            raise RuntimeError(f"未在 services.web.providers 中找到 {name}")
        pkind = spec.get("kind", "")
        if pkind == "host":
            cap = "web.search" if kind == "search" else "web.fetch"
            mapped = self.services.apply_web_mapping(arguments, spec)
            return await self._call_host_web(cap, **mapped)
        if pkind == "google_search_plugin":
            return await self._call_third_party(kind, arguments)
        if pkind == "mcp":
            service = spec.get("service") or spec.get("target", "")
            srv_cfg = (cfg.get("servers") or {}).get(service)
            if not isinstance(srv_cfg, dict) or not srv_cfg.get("tools"):
                raise RuntimeError(f"提供方 {name} 关联的 MCP 服务 {service} 未登记")
            tool = spec.get("tool") or next(
                (t for t in srv_cfg["tools"] if kind in t.lower()),
                srv_cfg["tools"][0],
            )
            return await self.services._call_web_mcp(
                service, tool, kind, arguments, context, self.runtime, srv_cfg, binding=spec
            )
        if pkind in {"plugin_api", "plugin"}:
            api_id = spec.get("api")
            api_spec = (cfg.get("apis") or {}).get(api_id) if api_id else None
            api_name = (
                (api_spec or {}).get("api_name")
                or spec.get("api_name")
                or spec.get("target")
                or ""
            )
            version = str(spec.get("version") or (api_spec or {}).get("version") or "")
            if not api_name:
                raise RuntimeError(f"提供方 {name} 缺少目标插件 API 名称")
            return await self._call_plugin_api_name(
                api_name, kind, arguments, context, version=version, binding=spec
            )
        raise RuntimeError(f"未知的自定义联网提供方类型 {pkind}")

    async def _dispatch(
        self,
        mode: str,
        kind: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ):
        if mode == "host":
            try:
                cap = "web.search" if kind == "search" else "web.fetch"
                return await self._call_host_web(cap, **arguments)
            except _HostWebDisabledError as exc:
                raise RuntimeError(str(exc)) from None
        if mode == "google_search_plugin":
            return await self._call_third_party(kind, arguments)
        if mode in {"mcp", "plugin_api"} or mode.startswith(("mcp:", "plugin_api:")):
            return await self._call_service_web(kind, arguments, context, mode)
        if mode.startswith("plugin:"):
            target = mode.split(":", 1)[1]
            api_name, version = await self._resolve_plugin_target(target, kind)
            return await self._call_plugin_api_name(
                api_name, kind, arguments, context, version=version
            )
        if mode.startswith("web:"):
            return await self._call_named_provider(
                mode.split(":", 1)[1], kind, arguments, context
            )
        return None

    async def search(
        self,
        query: str,
        max_results: int = 5,
        context: dict[str, Any] | None = None,
        *,
        provider: str = "",
        limit: int | None = None,
    ):
        ctx = context or {}
        count = max_results if limit is None else int(limit)
        mode = str(provider or ctx.get("web_provider") or self.current_provider() or "auto")
        if mode != "auto":
            out = await self._dispatch(
                mode, "search", {"query": query, "max_results": count}, ctx
            )
            if out is not None:
                return out
        # mode == "auto": 宿主预设优先，仅在宿主明确未启用/未分配时自动兼容外部绑定或第三方插件
        try:
            return await self._call_host_web(
                "web.search", query=query, max_results=count
            )
        except _HostWebDisabledError:
            via_service = await self._call_service_web(
                "search", {"query": query, "max_results": count}, ctx, "any"
            )
            if via_service is not None:
                return via_service
            cfg = await self.load_third_party_config()
            if cfg is not None:
                return await self._call_third_party(
                    "search", {"query": query, "max_results": count}, cfg=cfg
                )
            raise RuntimeError(
                "宿主统一联网未启用，且未检测到可用的第三方联网插件或服务登记"
            ) from None

    async def search_broad(
        self,
        query: str,
        max_results: int = 8,
        context: dict[str, Any] | None = None,
        *,
        provider: str = "",
        max_engines: int = 3,
    ):
        ctx = context or {}
        mode = str(provider or ctx.get("web_provider") or self.current_provider() or "auto")
        if mode == "google_search_plugin":
            cfg = await self.load_third_party_config()
            if cfg is not None and len(enabled_third_party_engines(cfg)) > 1:
                return await self._call_third_party(
                    "search_broad",
                    {
                        "query": query,
                        "max_results": max_results,
                        "max_engines": max_engines,
                    },
                    cfg=cfg,
                )
            return await self.search(
                query, max_results=max_results, context=ctx, provider=mode
            )
        if mode != "auto":
            return await self.search(
                query, max_results=max_results, context=ctx, provider=provider
            )
        try:
            return await self._call_host_web(
                "web.search", query=query, max_results=max_results
            )
        except _HostWebDisabledError:
            via_service = await self._call_service_web(
                "search", {"query": query, "max_results": max_results}, ctx, "any"
            )
            if via_service is not None:
                return via_service
            cfg = await self.load_third_party_config()
            if cfg is not None:
                kind = (
                    "search_broad"
                    if len(enabled_third_party_engines(cfg)) > 1
                    else "search"
                )
                return await self._call_third_party(
                    kind,
                    {
                        "query": query,
                        "max_results": max_results,
                        "max_engines": max_engines,
                    },
                    cfg=cfg,
                )
            raise RuntimeError(
                "宿主统一联网未启用，且未检测到可用的第三方联网插件或服务登记"
            ) from None

    async def fetch(
        self,
        url: str,
        context: dict[str, Any] | None = None,
        *,
        provider: str = "",
    ):
        url = safe_url(url)
        ctx = context or {}
        mode = str(provider or ctx.get("web_provider") or self.current_provider() or "auto")
        if mode != "auto":
            out = await self._dispatch(mode, "fetch", {"url": url}, ctx)
            if out is not None:
                return out
        try:
            return await self._call_host_web("web.fetch", url=url)
        except _HostWebDisabledError:
            via_service = await self._call_service_web(
                "fetch", {"url": url}, ctx, "any"
            )
            if via_service is not None:
                return via_service
            cfg = await self.load_third_party_config()
            if cfg is not None:
                return await self._call_third_party("fetch", {"url": url}, cfg=cfg)
            raise RuntimeError(
                "宿主统一联网未启用，且未检测到可用的第三方联网插件或服务登记"
            ) from None

    async def status(self) -> dict[str, Any]:
        services_bound: list[str] = []
        custom_providers: list[str] = []
        if self.services is not None:
            try:
                cfg = await completed_thread(self.services.load)
                web_cfg = cfg.get("web") if isinstance(cfg.get("web"), dict) else {}
                services_bound = [k for k in ("search", "fetch") if k in web_cfg]
                custom_providers = sorted((web_cfg.get("providers") or {}).keys())
            except (OSError, ValueError, TypeError):
                services_bound = []
        third_party = await self.load_third_party_config()
        engines = enabled_third_party_engines(third_party) if third_party else []
        backend = (
            third_party.get("search_backend")
            if isinstance(third_party, dict)
            and isinstance(third_party.get("search_backend"), dict)
            else {}
        )
        return {
            "provider": self.current_provider(),
            "services_bound": services_bound,
            "custom_providers": custom_providers,
            "third_party_enabled": third_party is not None,
            "third_party_default": str(backend.get("default_engine") or ""),
            "third_party_engines": engines,
        }
