"""Batch 2 (P1) TDD regression tests for digest, page_date, web, and viz_spec/viz.css."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
import types
from typing import Any

import pytest

from maisya import digest, page_date, viz_spec, web


class _ThreadSafeLocalWork:
    """Thread-safe local WorkingCopy test harness (no skipped I/O)."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root)
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.commits = 0

    def read(self, rel_path: str, *, offset: int = 0) -> dict[str, Any]:
        target = self.path / rel_path
        if not target.exists():
            raise FileNotFoundError(rel_path)
        text = target.read_text(encoding="utf-8")
        if offset >= len(text):
            return {"text": "", "next_offset": None}
        return {"text": text[offset:], "next_offset": None}

    def write(self, rel_path: str, content: str) -> dict[str, Any]:
        data = content.encode("utf-8")
        target = self.path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target.write_bytes(data)
        return {"path": rel_path, "bytes": len(data)}

    def commit(self) -> str:
        with self._lock:
            self.commits += 1
            return f"v_{self.commits:032x}"


def _redact_marker(marker: str):
    def _scrub(val: Any) -> Any:
        if isinstance(val, str):
            return val.replace(marker, "[VAULT_REDACTED]")
        if isinstance(val, list):
            return [_scrub(x) for x in val]
        if isinstance(val, tuple):
            return tuple(_scrub(x) for x in val)
        if isinstance(val, dict):
            return {k: _scrub(v) for k, v in val.items()}
        return val

    return _scrub


@pytest.mark.asyncio
async def test_parse_feed_xml_doctype_gbk_link_rel_urljoin_ms_and_newest_sort(tmp_path):
    now = 1_794_700_000.0  # 2026-11-13-ish UTC

    # 1. Entity-free <!DOCTYPE> must parse cleanly while <!ENTITY> XXE must still fail loudly
    doctype_rss = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" '
        '"http://my.netscape.com/publish/formats/rss-0.91.dtd">\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel><title>Safe Doctype Feed</title>"
        "<item>"
        "<title>Article One</title>"
        '<atom:link rel="self" href="https://feed.example/self.xml"/>'
        "<link>https://site.example/articles/real-one</link>"
        "<pubDate>1794696000000</pubDate>"
        "<description>Body</description>"
        "</item>"
        "</channel></rss>"
    ).encode("utf-8")
    parsed_doc = digest.parse_feed_xml(doctype_rss, now=now, lookback_days=14)
    assert parsed_doc["title"] == "Safe Doctype Feed"
    assert len(parsed_doc["items"]) == 1
    assert parsed_doc["items"][0]["url"] == "https://site.example/articles/real-one"
    assert parsed_doc["items"][0]["published_ts"] == pytest.approx(1_794_696_000.0)

    # 13-digit ms timestamp outside lookback window must still be filtered out
    old_ms_rss = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel><title>Old MS Feed</title>'
        "<item><title>Ancient</title><link>https://site.example/old</link>"
        f"<pubDate>{int((now - 20 * 86400) * 1000)}</pubDate></item>"
        "</channel></rss>"
    ).encode("utf-8")
    assert digest.parse_feed_xml(old_ms_rss, now=now, lookback_days=14)["items"] == []

    # XXE <!ENTITY> inside DOCTYPE must remain blocked (matching existing "DOCTYPE" expectation)
    xxe_xml = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<rss version='2.0'><channel><title>&xxe;</title></channel></rss>"
    )
    with pytest.raises(ValueError, match="DOCTYPE"):
        digest.parse_feed_xml(xxe_xml, now=now, lookback_days=14)

    # 2. GB2312 / GBK declaration decoded cleanly to UTF-8
    gb_xml = (
        '<?xml version="1.0" encoding="gb2312"?>'
        "<rss version='2.0'><channel><title>中文订阅源</title>"
        "<item><title>测试中文文章</title>"
        "<link>https://cn.example/post/1</link>"
        "<description>这是一段中文摘要。</description>"
        "</item></channel></rss>"
    ).encode("gbk")
    parsed_gb = digest.parse_feed_xml(gb_xml, now=now, lookback_days=14)
    assert parsed_gb["title"] == "中文订阅源"
    assert parsed_gb["items"][0]["title"] == "测试中文文章"
    assert "中文摘要" in parsed_gb["items"][0]["summary"]

    # 3. Atom relative href resolved via urljoin(base_url, ...) + <content> leading text + <img>
    atom_rel = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<title>Rel Atom</title>"
        "<entry>"
        "<title>Relative Link Entry</title>"
        '<link rel="self" href="https://feed.example/atom.xml"/>'
        '<link rel="alternate" href="/posts/relative-one"/>'
        "<updated>2026-11-12T10:00:00Z</updated>"
        '<content type="xhtml">Intro text before image <img src="/assets/hero.jpg"/> end</content>'
        "</entry></feed>"
    ).encode("utf-8")
    parsed_atom = digest.parse_feed_xml(
        atom_rel, now=now, lookback_days=14, base_url="https://blog.example/feeds/atom.xml"
    )
    assert parsed_atom["items"][0]["url"] == "https://blog.example/posts/relative-one"
    assert parsed_atom["items"][0]["image_url"] == "https://blog.example/assets/hero.jpg"

    # 4. Ascending-order feed must sort newest-first BEFORE TRIAL_CAND_CAP (2) slices
    old_iso = datetime.fromtimestamp(now - 5 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mid_iso = datetime.fromtimestamp(now - 2 * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_iso = datetime.fromtimestamp(now - 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    asc_rss = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel><title>Ascending Feed</title>'
        f"<item><title>Oldest</title><link>https://asc.example/1</link><pubDate>{old_iso}</pubDate></item>"
        f"<item><title>Middle</title><link>https://asc.example/2</link><pubDate>{mid_iso}</pubDate></item>"
        f"<item><title>Newest</title><link>https://asc.example/3</link><pubDate>{new_iso}</pubDate></item>"
        "</channel></rss>"
    ).encode("utf-8")

    async def asc_fetcher(url: str):
        return {"body": asc_rss, "content_type": "application/rss+xml", "status": 200, "url": url, "error": ""}

    dw = digest.DigestWorkspace(_ThreadSafeLocalWork(tmp_path), fetcher=asc_fetcher, clock=lambda: now)
    await dw.add_feed("https://asc.example/rss.xml", auto=True)
    polled = await dw.poll(lookback_days=14)
    picked_titles = [c["title"] for c in polled["candidates"]]
    assert picked_titles == ["Newest", "Middle"]


@pytest.mark.asyncio
async def test_digest_sources_concurrent_writes_keyword_word_boundary_and_flexible_unsubscribe(tmp_path):
    now = 1_794_700_000.0
    pub_iso = datetime.fromtimestamp(now - 7200, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _make_rss(title: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<rss version="2.0"><channel><title>{title}</title>'
            f"<item><title>{title} Item</title><link>https://item.example/{hash(title) & 0xffff}</link>"
            f"<pubDate>{pub_iso}</pubDate></item></channel></rss>"
        ).encode("utf-8")

    # 1. Word-boundary matching: "email html compiler" must NOT falsely score "ai"/"ml" starter feeds
    work_kw = _ThreadSafeLocalWork(tmp_path / "kw")

    async def kw_fetcher(url: str):
        await asyncio.sleep(0.01)
        return {"body": _make_rss(url), "content_type": "application/rss+xml", "status": 200, "url": url, "error": ""}

    dw_kw = digest.DigestWorkspace(work_kw, fetcher=kw_fetcher, clock=lambda: now)
    seeded = await dw_kw.seed_starter_feeds("email html compiler", max_add=6)
    added_urls = [f["url"] for f in seeded["added"]]
    assert added_urls == ["https://blog.rust-lang.org/feed.xml"]

    # Also verify "algo specs compiler" does NOT match "go" (algo) or "pc" (specs)
    dw_kw2 = digest.DigestWorkspace(_ThreadSafeLocalWork(tmp_path / "kw2"), fetcher=kw_fetcher, clock=lambda: now)
    seeded2 = await dw_kw2.seed_starter_feeds("algo specs compiler", max_add=6)
    assert [f["url"] for f in seeded2["added"]] == ["https://blog.rust-lang.org/feed.xml"]

    # 2. Concurrent read-modify-write protection on digest_sources.json (no lost feeds!)
    work_conc = _ThreadSafeLocalWork(tmp_path / "conc")

    async def staggered_fetcher(url: str):
        if "slow1" in url:
            await asyncio.sleep(0.06)
        elif "slow2" in url:
            await asyncio.sleep(0.03)
        else:
            await asyncio.sleep(0.01)
        return {"body": _make_rss(url), "content_type": "application/rss+xml", "status": 200, "url": url, "error": ""}

    dw_conc = digest.DigestWorkspace(work_conc, fetcher=staggered_fetcher, clock=lambda: now)
    # Seed initial feed to ensure poll executes Phase 3 while concurrent add_feeds arrive
    await dw_conc.add_feed("https://base.example/rss.xml", title="Base Feed", auto=False)
    await asyncio.gather(
        dw_conc.poll(lookback_days=7),
        dw_conc.seed_starter_feeds("compiler", max_add=1),
        dw_conc.add_feed("https://slow1.example/rss.xml", title="Slow One", auto=False),
        dw_conc.add_feed("https://slow2.example/rss.xml", title="Slow Two", auto=False),
    )
    saved_urls = {f["url"] for f in dw_conc._load_sources()["feeds"]}
    assert {
        "https://base.example/rss.xml",
        "https://blog.rust-lang.org/feed.xml",
        "https://slow1.example/rss.xml",
        "https://slow2.example/rss.xml",
    }.issubset(saved_urls)

    # 3. run_digest_tool(action="unsubscribe") without feed_id via url / domain / title
    r1 = await digest.run_digest_tool(
        work_conc,
        {"action": "unsubscribe", "url": "https://www.slow1.example/rss.xml?utm_source=test"},
    )
    assert r1["ok"] is True and r1["title"] == "Slow One"
    r2 = await digest.run_digest_tool(
        work_conc,
        {"action": "unsubscribe", "domain": "slow2.example"},
    )
    assert r2["ok"] is True and r2["title"] == "Slow Two"
    r3 = await digest.run_digest_tool(
        work_conc,
        {"action": "unsubscribe", "title": "base feed"},
    )
    assert r3["ok"] is True and r3["url"] == "https://base.example/rss.xml"


def test_page_date_cjk_boundaries_future_short_date_nested_ignore_footer_and_tz():
    now_noon = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp()

    # 1. No-space Chinese boundaries & 1-digit hour & >80 char cue line
    assert page_date.relative_ts("发布于3小时前", now=now_noon) == pytest.approx(now_noon - 3 * 3600)
    assert page_date.relative_ts("3小时前发布", now=now_noon) == pytest.approx(now_noon - 3 * 3600)
    assert page_date.published_from_text("发布于2026-11-15", now=now_noon) == "2026-11-15T00:00:00Z"
    assert page_date.published_from_text("2026-11-15发布", now=now_noon) == "2026-11-15T00:00:00Z"
    assert page_date.published_from_text("发布于2026-11-15 9:05", now=now_noon) == "2026-11-15T09:05:00Z"

    long_cue = "发布时间：" + ("系统架构更新说明" * 12) + " 3小时前"  # > 80 and <= 200 chars
    assert len(long_cue) > 80
    assert page_date.published_from_text(long_cue, now=now_noon) == "2026-11-15T09:00:00Z"

    # Old out-of-window year (< 2000) must NOT fall through to matching trailing "11月15日" as current year
    assert page_date.published_from_text("发布时间：1998年11月15日", now=now_noon) == ""

    # 2. Near-term future short date M月D日 must NOT rewind to last year
    assert page_date.published_from_text("发布时间：11月25日将发布正式版", now=now_noon) == ""
    assert page_date.normalize_date("11月25日", now=now_noon) == ""

    # Cross-year boundary (Jan reading late Dec) still maps to previous year cleanly
    jan_now = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    assert page_date.published_from_text("发布时间：12月28日", now=jan_now) == "2025-12-28T00:00:00Z"

    # 3. Nested ignored containers (<template><svg><svg>...) + void tags inside <footer>
    nested_html = (
        "<html><body>"
        "<template><svg><svg></svg><div>2020-01-01</div></svg></template>"
        '<footer><img src="logo.png"><br><hr><input type="hidden">'
        '<div class="copyright">2021-01-01</div></footer>'
        "<article><p>发布于2026-11-14</p></article>"
        "</body></html>"
    )
    assert page_date.published_from_html(nested_html, now=now_noon) == "2026-11-14T00:00:00Z"

    # 4. Meta article:modified_time must beat <footer><time>
    meta_vs_footer = (
        '<html><head><meta property="article:modified_time" content="2026-11-14T10:00:00Z"></head>'
        '<body><footer><time datetime="2024-01-01T00:00:00Z">© 2024</time></footer>'
        "<main><p>Article body without date.</p></main></body></html>"
    )
    assert page_date.published_from_html(meta_vs_footer, now=now_noon) == "2026-11-14T10:00:00Z"

    # 5. Configurable tz_offset_minutes (default 480 = UTC+8)
    # 2026-11-14 17:00 UTC == 2026-11-15 01:00 CST (UTC+8)
    utc_17 = datetime(2026, 11, 14, 17, 0, 0, tzinfo=timezone.utc).timestamp()
    assert page_date.published_from_text("发布时间：今天", now=utc_17) == "2026-11-14T16:00:00Z"
    assert page_date.published_from_text("发布时间：昨天 09:30", now=utc_17) == "2026-11-14T01:30:00Z"
    assert (
        page_date.published_from_text("发布时间：今天", now=utc_17, tz_offset_minutes=0)
        == "2026-11-14T00:00:00Z"
    )


@pytest.mark.asyncio
async def test_web_router_json_fallback_unknown_provider_ddg_proxy_and_full_redaction(monkeypatch):
    # 1. All JSON search engines survive HTTP 200 HTML block page on key 1 and succeed on key 2
    engines_cases = [
        ("exa", "exa_api_keys", {"results": [{"title": "Exa2", "url": "https://exa.example/2", "text": "ok"}]}),
        ("tavily", "tavily_api_keys", {"results": [{"title": "Tav2", "url": "https://tav.example/2", "content": "ok"}]}),
        ("anysearch", "anysearch_api_keys", {"code": 0, "data": {"results": [{"title": "Any2", "url": "https://any.example/2", "snippet": "ok"}]}}),
        ("you", "you_api_keys", {"results": {"web": [{"title": "You2", "url": "https://you.example/2", "description": "ok"}]}}),
        ("you_news", "you_api_keys", {"news": {"results": [{"title": "News2", "url": "https://yn.example/2", "description": "ok"}]}}),
        ("deepseek", "deepseek_api_keys", {"content": [{"type": "web_search_tool_result", "content": [{"title": "DS2", "url": "https://ds.example/2", "snippet": "ok"}]}]}),
    ]

    for eng_name, key_field, good_payload in engines_cases:
        calls: list[str] = []

        def _http_stub(method, url, *, headers=None, json_body=None, **kwargs):
            used_key = (headers or {}).get("x-api-key") or (headers or {}).get("X-API-Key") or (headers or {}).get("Authorization") or (json_body or {}).get("api_key") or ""
            calls.append(str(used_key))
            if "bad-k1" in str(used_key):
                return (200, {"content-type": "text/html"}, b"<html><body>WAF Challenge</body></html>", url)
            return (200, {"content-type": "application/json"}, json.dumps(good_payload).encode(), url)

        cfg = {
            "search_backend": {"default_engine": eng_name, "enabled_engines": [eng_name], "timeout": 10},
            "engines": {
                key_field: ["bad-k1", "good-k2"],
                f"{eng_name}_enabled": True,
                **({"you_enabled": True} if eng_name == "you_news" else {}),
            },
        }

        class HostCfg:
            async def call_capability(self, name, **kwargs):
                if name == "config.get_plugin":
                    return cfg
                raise AssertionError(name)

        res = await web.WebRouter(
            HostCfg(), provider="google_search_plugin", http_request=_http_stub
        ).search("test q", 3, {"chat_id": "c"})
        assert res["service"] == f"google_search_plugin:{eng_name}"
        assert len(calls) == 2
        assert len(res["results"]) == 1

    # 2. Explicit unknown provider MUST raise loudly instead of falling back to host/auto
    host_calls: list[str] = []

    class NoFallbackHost:
        async def call_capability(self, name, **kwargs):
            host_calls.append(name)
            return {"success": True, "result": {"results": []}}

    bad_router = web.WebRouter(NoFallbackHost(), provider="typo_provider")
    for call in (
        bad_router.search("q", 3, {"chat_id": "c"}),
        bad_router.search_broad("q", 5, {"chat_id": "c"}),
        bad_router.fetch("https://example.com/a", {"chat_id": "c"}),
    ):
        with pytest.raises((ValueError, RuntimeError)):
            await call
    assert host_calls == []

    # Context-provided unknown mode on auto router also must reject
    auto_router = web.WebRouter(NoFallbackHost(), provider="auto")
    with pytest.raises((ValueError, RuntimeError)):
        await auto_router.search("q", 3, {"chat_id": "c", "web_provider": "nonexistent_engine"})
    assert host_calls == []

    # 3. DuckDuckGo passes configured proxy
    captured_ddgs: list[dict[str, Any]] = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            captured_ddgs.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def text(self, query, **kwargs):
            return [{"title": "DDG Hit", "href": "https://ddg.example/1", "body": "proxy works"}]

    fake_ddgs_mod = types.ModuleType("ddgs")
    fake_ddgs_mod.DDGS = FakeDDGS
    monkeypatch.setitem(sys.modules, "ddgs", fake_ddgs_mod)

    class HostDDG:
        async def call_capability(self, name, **kwargs):
            return {
                "search_backend": {"default_engine": "duckduckgo", "enabled_engines": ["duckduckgo"], "proxy": "http://127.0.0.1:7890"},
                "engines": {"duckduckgo_enabled": True},
            }

    ddg_res = await web.WebRouter(HostDDG(), provider="google_search_plugin").search("q", 2, {"chat_id": "c"})
    assert ddg_res["service"] == "google_search_plugin:duckduckgo"
    assert captured_ddgs and captured_ddgs[0].get("proxy") == "http://127.0.0.1:7890"

    # 4. Full self.redact on host web, plugin_api, and third-party error paths
    vault_secret = "VLT_TOP_SECRET_999"
    scrub = _redact_marker(vault_secret)

    class LeakyHost:
        def __init__(self, *, fail_mode: str = "") -> None:
            self.fail_mode = fail_mode

        async def call_capability(self, name, **kwargs):
            if name == "web.search":
                if self.fail_mode == "exc":
                    raise RuntimeError(f"host error with {vault_secret}")
                if self.fail_mode == "dict_err":
                    return {"success": False, "error": f"denied {vault_secret}"}
                return {"success": True, "result": {"results": [{"title": f"Leak {vault_secret}", "url": "https://a.example"}]}}
            if name == "api.list":
                return {"success": True, "apis": [{"plugin_id": "demo", "name": "search", "version": "1"}]}
            if name == "api.call":
                if self.fail_mode == "dict_err":
                    return {"success": False, "error": f"api err {vault_secret}"}
                if self.fail_mode == "exc":
                    raise RuntimeError(f"api boom {vault_secret}")
                return {"success": True, "result": {"summary": f"token={vault_secret}"}}
            if name == "config.get_plugin":
                return {
                    "search_backend": {"default_engine": "exa", "enabled_engines": ["exa"]},
                    "engines": {"exa_enabled": True, "exa_api_keys": ["exa-k1"]},
                }
            raise AssertionError(name)

    host_ok = await web.WebRouter(LeakyHost(), redact=scrub, provider="host").search("q", 2, {"chat_id": "c"})
    assert vault_secret not in json.dumps(host_ok)
    assert "[VAULT_REDACTED]" in json.dumps(host_ok)

    for mode in ("exc", "dict_err"):
        with pytest.raises(RuntimeError) as exc_h:
            await web.WebRouter(LeakyHost(fail_mode=mode), redact=scrub, provider="host").search("q", 2, {"chat_id": "c"})
        assert vault_secret not in str(exc_h.value)

    api_ok = await web.WebRouter(LeakyHost(), redact=scrub, provider="plugin:demo.search").search("q", 2, {"chat_id": "c"})
    assert vault_secret not in json.dumps(api_ok)
    assert "[VAULT_REDACTED]" in json.dumps(api_ok)

    for mode in ("exc", "dict_err"):
        with pytest.raises(RuntimeError) as exc_a:
            await web.WebRouter(LeakyHost(fail_mode=mode), redact=scrub, provider="plugin:demo.search").search("q", 2, {"chat_id": "c"})
        assert vault_secret not in str(exc_a.value)

    def third_party_boom(*args, **kwargs):
        raise ValueError(f"bad value with {vault_secret}")

    with pytest.raises(ValueError) as exc_tp:
        await web.WebRouter(
            LeakyHost(), redact=scrub, provider="google_search_plugin", http_request=third_party_boom
        ).fetch("https://a.example/page", {"chat_id": "c"})
    assert vault_secret not in str(exc_tp.value)
    assert "[VAULT_REDACTED]" in str(exc_tp.value)

    # _call_service_web and _call_named_provider (MCP) also redact both returns and raised errors
    class LeakyServices:
        def __init__(self, fail: bool = False):
            self.fail = fail

        def load(self):
            return {
                "web": {"providers": {"custom_mcp": {"kind": "mcp", "service": "srv", "tool": "search"}}},
                "servers": {"srv": {"tools": ["search"]}},
            }

        async def call_web(self, kind, arguments, context, runtime, mode="auto"):
            if self.fail:
                raise RuntimeError(f"svc err {vault_secret}")
            return {"results": [{"title": f"svc {vault_secret}", "url": "https://svc.example"}]}

        async def _call_web_mcp(self, service, tool, kind, arguments, context, runtime, srv_cfg, binding=None):
            if self.fail:
                raise RuntimeError(f"mcp err {vault_secret}")
            return {"results": [{"title": f"mcp {vault_secret}", "url": "https://mcp.example"}]}

    svc_ok = await web.WebRouter(LeakyHost(), redact=scrub, services=LeakyServices(), provider="mcp").search("q", 2, {"chat_id": "c"})
    assert vault_secret not in json.dumps(svc_ok)
    assert "[VAULT_REDACTED]" in json.dumps(svc_ok)
    with pytest.raises(RuntimeError) as exc_svc:
        await web.WebRouter(LeakyHost(), redact=scrub, services=LeakyServices(fail=True), provider="mcp").search("q", 2, {"chat_id": "c"})
    assert vault_secret not in str(exc_svc.value)
    assert "[VAULT_REDACTED]" in str(exc_svc.value)

    named_ok = await web.WebRouter(LeakyHost(), redact=scrub, services=LeakyServices(), provider="web:custom_mcp").search("q", 2, {"chat_id": "c"})
    assert vault_secret not in json.dumps(named_ok)
    assert "[VAULT_REDACTED]" in json.dumps(named_ok)
    with pytest.raises(RuntimeError) as exc_named:
        await web.WebRouter(LeakyHost(), redact=scrub, services=LeakyServices(fail=True), provider="web:custom_mcp").search("q", 2, {"chat_id": "c"})
    assert vault_secret not in str(exc_named.value)
    assert "[VAULT_REDACTED]" in str(exc_named.value)


def test_viz_tabs_css_checked_line_long_y_axis_pad_and_null_note_tone():
    # 1. note.tone = None falls back safely to "info" while invalid tones still raise
    spec_note = {"blocks": [{"type": "note", "text": "说明文本", "tone": None}]}
    viz_spec.validate(spec_note, profile="report")
    note_html = viz_spec.render(spec_note)
    assert 'class="vs-note tone-info"' in note_html
    with pytest.raises(viz_spec.SpecError):
        viz_spec.validate({"blocks": [{"type": "note", "text": "x", "tone": "purple"}]}, profile="report")

    # 2. _render_line expands left pad_l for long Y-axis labels and avoids X-axis end collision (n=8)
    line_spec = {
        "blocks": [
            {
                "type": "line",
                "title": "营收趋势",
                "unit": "万元",
                "points": [{"x": f"D{i}", "y": 100000 + i * 5000} for i in range(8)],
            }
        ]
    }
    line_html = viz_spec.render(line_spec)
    m_grid = re.search(r'<line class="vs-line-grid" x1="([0-9.]+)"', line_html)
    assert m_grid is not None
    pad_l_val = float(m_grid.group(1))
    assert pad_l_val > 64.0, f"pad_l too narrow for long Y labels: {pad_l_val}"

    x_ticks = re.findall(r'<text class="vs-line-axis"[^>]*y="200\.0"[^>]*>([^<]+)</text>', line_html)
    assert "D0" in x_ticks and "D7" in x_ticks
    assert not ("D6" in x_ticks and "D7" in x_ticks), f"Adjacent D6 & D7 collide: {x_ticks}"

    # 3. Dynamic tabs must not emit conflicting static is-active and must switch cleanly via pure CSS :checked
    tabs_spec = {
        "blocks": [
            {
                "type": "tabs",
                "tabs": [
                    {"label": "第一页", "blocks": [{"type": "stat", "label": "指标A", "value": "100"}]},
                    {"label": "第二页", "blocks": [{"type": "stat", "label": "指标B", "value": "200"}]},
                ],
            }
        ]
    }
    dyn_html = viz_spec.render(tabs_spec, static=False)
    assert "is-active" not in dyn_html
    assert 'data-tab-idx="0" checked' in dyn_html

    # Real browser pure-CSS test (JavaScript disabled) in headless Chromium via Playwright
    from playwright.sync_api import sync_playwright

    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<style>{viz_spec.load_viz_css()}</style></head><body>{dyn_html}</body></html>"
    )
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(doc)
            assert page.locator('.vs-tab-panel[data-tab-idx="0"]').evaluate("el => getComputedStyle(el).display") == "block"
            assert page.locator('.vs-tab-panel[data-tab-idx="1"]').evaluate("el => getComputedStyle(el).display") == "none"

            # Click Tab 2 label with zero JS; Panel 0 MUST hide and Panel 1 MUST show
            page.locator('label.vs-tab-lbl[data-tab-idx="1"]').click()
            assert page.locator('.vs-tab-panel[data-tab-idx="0"]').evaluate("el => getComputedStyle(el).display") == "none"
            assert page.locator('.vs-tab-panel[data-tab-idx="1"]').evaluate("el => getComputedStyle(el).display") == "block"
        finally:
            browser.close()
