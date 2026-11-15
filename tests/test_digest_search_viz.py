"""Tests for page_date, WebRouter.search_broad, DigestWorkspace.collect/seed, viz verification & card rendering."""

import asyncio
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from maisya import digest, page_date, web


class _MemoryWork:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.commits = 0

    def read(self, path: str, offset: int = 0) -> dict:
        if path not in self.files:
            raise FileNotFoundError(path)
        raw = self.files[path].encode("utf-8")
        chunk = raw[offset : offset + 12000]
        next_off = offset + len(chunk) if offset + len(chunk) < len(raw) else None
        return {
            "text": chunk.decode("utf-8"),
            "next_offset": next_off,
            "bytes": len(raw),
        }

    def write(self, path: str, content: str) -> dict:
        self.files[path] = content
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    def commit(self) -> str:
        self.commits += 1
        return f"v_{self.commits}"


NOW_TS = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc).timestamp()

_SAMPLE_RSS = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<rss version="2.0"><channel><title>Indie Blog</title>'
    b"<item><title>Alpha Engine Benchmark Notes</title>"
    b"<link>https://indie.example.org/posts/alpha-benchmark</link>"
    b"<pubDate>Thu, 22 Oct 2026 08:00:00 GMT</pubDate>"
    b"<description>Short teaser only.</description></item>"
    b"</channel></rss>"
)


def _third_party_cfg(extra_engines: dict | None = None) -> dict:
    engines = {
        "deepseek_enabled": False,
        "tavily_enabled": False,
        "anysearch_enabled": False,
        "exa_enabled": False,
        "you_enabled": False,
        "you_news_enabled": False,
        "you_contents_enabled": False,
        "duckduckgo_enabled": False,
    }
    if extra_engines:
        engines.update(extra_engines)
    return {
        "search_backend": {"timeout": 15, "max_results": 8},
        "engines": engines,
    }


def test_page_date_multi_source_and_bounds():
    html_meta = '<html><head><meta property="article:published_time" content="2026-10-20T09:30:00+08:00"></head><body>hi</body></html>'
    assert page_date.published_from_html(html_meta, now=NOW_TS) == "2026-10-20T01:30:00Z"

    html_ld = (
        '<html><head><script type="application/ld+json">'
        + json.dumps({"@type": "NewsArticle", "datePublished": "2026-10-21"})
        + "</script></head><body>post</body></html>"
    )
    assert page_date.published_from_html(html_ld, now=NOW_TS) == "2026-10-21T00:00:00Z"

    html_time = (
        '<html><body><div class="comments"><time datetime="2026-10-01">old</time></div>'
        '<article><header><time datetime="2026-10-22T15:00:00Z">yesterday</time></header></article>'
        "</body></html>"
    )
    assert page_date.published_from_html(html_time, now=NOW_TS) == "2026-10-22T15:00:00Z"

    html_attr = (
        '<html><body><article><span class="post-date" data-time="1792665600">'
        "</span></article></body></html>"
    )
    assert page_date.published_from_html(html_attr, now=NOW_TS) == "2026-10-22T10:40:00Z"

    html_rel_title = (
        '<html><body><div class="meta"><span title="2026-10-23 09:00:00">3小时前</span></div></body></html>'
    )
    assert page_date.published_from_html(html_rel_title, now=NOW_TS) == "2026-10-23T09:00:00Z"

    # Relative timestamp helper
    ts_rel = page_date.relative_ts("3小时前", now=NOW_TS)
    assert ts_rel == pytest.approx(NOW_TS - 3 * 3600)
    assert page_date.relative_ts("2 days ago", now=NOW_TS) == pytest.approx(NOW_TS - 2 * 86400)

    # Reject pre-2000 & far future (> +2 days) false positives
    assert page_date.normalize_date("1999-12-31", now=NOW_TS) == ""
    assert page_date.normalize_date("2026-10-30", now=NOW_TS) == ""
    assert page_date.normalize_date("2026-10-25", now=NOW_TS) == "2026-10-25T00:00:00Z"
    assert (
        page_date.published_from_text("© 1998 Old Co\n发布时间：2026年10月21日 14:20\n正文开始", now=NOW_TS)
        == "2026-10-21T14:20:00Z"
    )


def test_web_fetch_extracts_published_and_feed_links():
    html_page = """
    <html>
      <head>
        <title>Minimalist Tooling Post</title>
        <meta property="article:published_time" content="2026-10-20T12:00:00Z">
        <link rel="alternate" type="application/rss+xml" title="Blog RSS" href="/feed.xml">
        <link rel="alternate" type="application/atom+xml" href="https://indie.dev/atom.xml">
      </head>
      <body><article><p>Benchmark improved latency from 120ms to 45ms across 300 nodes.</p></article></body>
    </html>
    """.encode("utf-8")

    def fake_http(method, url, **_kwargs):
        return (
            200,
            {"content-type": "text/html; charset=utf-8"},
            html_page,
            "https://indie.dev/blog/tooling",
        )

    cfg = _third_party_cfg()
    res = web._run_third_party_fetch("https://indie.dev/blog/tooling", cfg, fake_http)
    assert res["published_at"] == "2026-10-20T12:00:00Z"
    assert res["feed_links"] == [
        "https://indie.dev/feed.xml",
        "https://indie.dev/atom.xml",
    ]
    assert "Benchmark improved latency" in res["content"]


def test_third_party_broad_search_priority_dedup_and_redaction(monkeypatch):
    import importlib.util

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **kw: object() if name == "ddgs" else real_find_spec(name, *a, **kw),
    )
    monkeypatch.setattr(web, "_search_duckduckgo", lambda *args, **kwargs: (True, [{"title": "DDG", "url": "https://ddg.example/x"}]))

    cfg = _third_party_cfg(
        {
            "exa_enabled": True,
            "exa_api_keys": ["exa-secret-123456"],
            "tavily_enabled": True,
            "tavily_api_keys": ["tvly-secret-654321"],
            "you_news_enabled": True,
            "you_enabled": True,
            "you_api_keys": ["you-secret-abcdef"],
            "duckduckgo_enabled": True,
        }
    )

    called_engines: list[str] = []

    def fake_http(method, url, **kwargs):
        if "exa.ai" in url:
            called_engines.append("exa")
            body = {
                "results": [
                    {
                        "title": "Exa Story (exa-secret-123456)",
                        "url": "https://blog.example.com/posts/release/?utm_source=exa",
                        "text": "Exa summary body",
                        "publishedDate": "2026-10-22T10:00:00Z",
                    },
                    {
                        "title": "Exa Unique",
                        "url": "https://unique-exa.example.com/post",
                        "text": "Unique content",
                    },
                ]
            }
            return 200, {}, json.dumps(body).encode(), url
        if "tavily.com" in url:
            called_engines.append("tavily")
            body = {
                "results": [
                    {
                        "title": "Tavily Duplicate",
                        "url": "https://www.blog.example.com/posts/release",
                        "content": "Longer Tavily snippet tvly-secret-654321",
                    },
                    {
                        "title": "Tavily Only",
                        "url": "https://tavily-only.example.org/news",
                        "content": "Tavily news",
                    },
                ]
            }
            return 200, {}, json.dumps(body).encode(), url
        if "news" in url:
            called_engines.append("you_news")
            body = {
                "news": {
                    "results": [
                        {
                            "title": "You News Item",
                            "url": "https://younews.example.net/article-1",
                            "description": "You news desc",
                            "page_age": "2026-10-21T08:00:00Z",
                        }
                    ]
                }
            }
            return 200, {}, json.dumps(body).encode(), url
        raise AssertionError(f"Unexpected call to {url}")

    out = web._run_third_party_broad_search("agent tooling", 8, cfg, fake_http, max_engines=3)
    # exa, tavily, you_news selected (<=3); duckduckgo skipped since >=2 API engines exist
    assert set(called_engines) == {"exa", "tavily", "you_news"}
    assert "duckduckgo" not in out["engines"]

    by_title = {r["title"]: r for r in out["results"]}
    # Deduplicated URL merges engines & preserves published_at + redacts secrets
    merged = next(r for r in out["results"] if "blog.example.com/posts/release" in r["url"])
    assert merged["engines"] == ["exa", "tavily"]
    assert merged["published_at"] == "2026-10-22T10:00:00Z"
    serialized = json.dumps(out, ensure_ascii=False)
    assert "exa-secret-123456" not in serialized
    assert "tvly-secret-654321" not in serialized
    assert "you-secret-abcdef" not in serialized
    assert set(by_title) >= {"Exa Unique", "Tavily Only", "You News Item"}

    # With < 2 API engines, duckduckgo IS included
    cfg_ddg = _third_party_cfg(
        {
            "exa_enabled": True,
            "exa_api_keys": ["exa-secret-123456"],
            "duckduckgo_enabled": True,
        }
    )
    out_ddg = web._run_third_party_broad_search("agent tooling", 6, cfg_ddg, fake_http, max_engines=3)
    assert out_ddg["engines"] == ["exa", "duckduckgo"]


def test_web_router_search_broad_dispatch_and_fallback(monkeypatch):
    cfg = _third_party_cfg(
        {
            "exa_enabled": True,
            "exa_api_keys": ["exa-k1"],
            "tavily_enabled": True,
            "tavily_api_keys": ["tvly-k1"],
        }
    )

    class _FakeRouter(web.WebRouter):
        def __init__(self, provider: str = "auto"):
            super().__init__(None, provider=provider)
            self.search_calls: list[tuple[str, str]] = []
            self.host_disabled = True

        async def load_third_party_config(self):
            return cfg

        async def _call_host_web(self, name: str, **kwargs):
            if self.host_disabled:
                raise web._HostWebDisabledError("统一联网功能未启用")
            return {"query": kwargs["query"], "results": [{"title": "HostHit", "url": "https://host.example/1"}], "service": "host"}

        async def search(self, query, max_results=5, context=None, *, provider="", limit=None):
            self.search_calls.append((query, provider or self.current_provider()))
            return {"query": query, "results": [{"title": "Fallback", "url": "https://fb.example/1"}], "service": "fallback"}

    monkeypatch.setattr(
        web,
        "_run_third_party_broad_search",
        lambda q, mr, c, req, me=3: {
            "query": q,
            "results": [{"title": "BroadHit", "url": "https://broad.example/1", "engines": ["exa", "tavily"]}],
            "service": "google_search_plugin:broad",
            "engines": ["exa", "tavily"],
        },
    )

    async def _run():
        router = _FakeRouter(provider="auto")
        # auto falls back to third-party broad search when host disabled & >=2 engines
        res_auto = await router.search_broad("llm agents")
        assert res_auto["service"] == "google_search_plugin:broad"

        # explicit non-google provider falls back to self.search
        res_other = await router.search_broad("llm agents", provider="web:custom")
        assert res_other["service"] == "fallback"
        assert ("llm agents", "web:custom") in router.search_calls

        # auto with host enabled uses host cleanly
        router.host_disabled = False
        res_host = await router.search_broad("llm agents")
        assert res_host["service"] == "host"

    asyncio.run(_run())


def test_prefilter_search_item_rules():
    prior_urls = {"https://seen.example.com/old-story/?utm_source=x"}
    prior_titles = ["Open Source Agent Benchmarks Released Today"]
    blocked = {"spam.example"}

    assert digest.prefilter_search_item("https://example.com/", "Home", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/?utm_medium=social", "Home", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/2026/", "2026 Archive", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/news/2025/10/", "Oct 2025", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/tag/llm/", "LLM Tag", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/category/ai", "AI", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/topics/agents", "Agents", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/archive/2026", "Archive", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/page/2", "Page 2", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://zh.wikipedia.org/wiki/Agent", "Agent - 维基百科", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://baike.baidu.com/item/Agent", "Agent_百度百科", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://zh.moegirl.org.cn/Test", "Test - 萌娘百科", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://example.com/login?next=/post", "Login", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://sub.spam.example/article", "Valid Title", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item("https://seen.example.com/old-story", "Different Title", prior_urls, prior_titles, blocked) is not None
    assert digest.prefilter_search_item(
        "https://other.example.org/story",
        "Open Source Agent Benchmarks Released Today Now",
        prior_urls,
        prior_titles,
        blocked,
    ) is not None

    # Valid article under dated path MUST pass
    assert (
        digest.prefilter_search_item(
            "https://example.com/2026/10/22/open-source-compiler-release",
            "New Open Source Compiler Release Details",
            prior_urls,
            prior_titles,
            blocked,
        )
        is None
    )


def test_digest_seed_and_collect_end_to_end(monkeypatch):
    work = _MemoryWork()
    fetched_feed_urls: list[str] = []

    async def fake_fetcher(url: str) -> dict:
        fetched_feed_urls.append(url)
        if "fail-feed" in url or "huggingface" in url:
            return {"status": 500, "body": b"", "error": "timeout", "url": url}
        return {"status": 200, "body": _SAMPLE_RSS, "url": url}

    monkeypatch.setattr(digest, "fetch_public_bytes", fake_fetcher)

    class _FakeWeb:
        def __init__(self) -> None:
            self.broad_calls: list[str] = []
            self.fetch_calls: list[str] = []

        async def search_broad(self, query: str, max_results: int = 8, context=None, **_kw):
            self.broad_calls.append(query)
            return {
                "results": [
                    {
                        "title": "Index Noise",
                        "url": "https://blog.noise.dev/2026/",
                        "snippet": "Archive page",
                    },
                    {
                        "title": "Agent - Wikipedia",
                        "url": "https://en.wikipedia.org/wiki/Agent",
                        "snippet": "Encyclopedia",
                    },
                    {
                        "title": "TinyBench 2.0 Cuts Latency by 42 Percent Across 128 GPUs",
                        "url": "https://indie-research.dev/2026/10/tinybench-2",
                        "snippet": "Latency down 42% on 128 GPUs.",
                        "engines": ["exa", "tavily"],
                    },
                    {
                        "title": "GitHub Repo Notes",
                        "url": "https://github.com/org/repo/releases/tag/v1.0",
                        "snippet": "Release notes",
                        "engines": ["exa"],
                    },
                ]
            }

        async def fetch(self, url: str, context=None, **_kw):
            self.fetch_calls.append(url)
            if "tinybench-2" in url:
                return {
                    "url": url,
                    "title": "TinyBench 2.0 Cuts Latency by 42 Percent Across 128 GPUs",
                    "content": "TinyBench 2.0 measured 42% lower tail latency on 128 GPUs and 91% cache hit ratio.",
                    "published_at": "2026-10-22T09:00:00Z",
                    "feed_links": [
                        "https://github.com/org/feed.xml",  # big platform must be ignored
                        "https://indie-research.dev/feed.xml",  # indie blog discovered!
                    ],
                }
            if "alpha-benchmark" in url:
                return {
                    "url": url,
                    "title": "Alpha Engine Benchmark Notes",
                    "content": "Alpha Engine ran on 16 nodes with 240 req/s throughput.",
                    "published_at": "2026-10-22T08:00:00Z",
                    "feed_links": [],
                }
            return {
                "url": url,
                "title": "GitHub Repo Notes",
                "content": "Minor patch release.",
                "published_at": "2026-10-21T10:00:00Z",
                "feed_links": ["https://github.com/evil/feed.xml"],
            }

    dw = digest.DigestWorkspace(work, fetcher=fake_fetcher, clock=lambda: NOW_TS)
    fake_web = _FakeWeb()

    # Verify run_digest_tool seed + collect entrypoints cleanly
    tool_work = _MemoryWork()
    seed_tool_res = asyncio.run(
        digest.run_digest_tool(
            tool_work,
            {"action": "seed", "topics": ["独立游戏", "indie"], "max_items": 2},
        )
    )
    assert seed_tool_res["count"] == 2
    assert any("gcores" in f["url"] or "chuapp" in f["url"] for f in seed_tool_res["added"])

    collect_tool_res = asyncio.run(
        digest.run_digest_tool(
            _MemoryWork(),
            {
                "action": "collect",
                "topics": ["AI", "Agent"],
                "queries": ["TinyBench 2.0 inference latency"],
                "lookback_days": 7,
                "max_items": 10,
                "broad": True,
            },
            web=fake_web,
        )
    )
    assert collect_tool_res["stats"]["discovered_feeds"] == ["https://indie-research.dev/feed.xml"]

    # Test directly via dw so clock is fixed to NOW_TS cleanly
    res = asyncio.run(
        dw.collect(
            web=fake_web,
            queries=["TinyBench 2.0 inference latency"],
            topics=["AI", "Agent"],
            lookback_days=7,
            max_items=10,
            broad=True,
        )
    )

    stats = res["stats"]
    assert len(stats["seeded_feeds"]) == 4  # HuggingFace failed & skipped, 4 others succeeded as trial
    assert stats["prefiltered"] >= 2  # /2026/ and wikipedia filtered
    assert stats["discovered_feeds"] == ["https://indie-research.dev/feed.xml"]
    sources_saved = dw._load_sources()
    assert all(f["status"] == "trial" for f in sources_saved["feeds"])

    by_url = {c["url"]: c for c in res["candidates"]}
    assert "https://blog.noise.dev/2026/" not in by_url
    assert "https://en.wikipedia.org/wiki/Agent" not in by_url
    tb = by_url["https://indie-research.dev/2026/10/tinybench-2"]
    assert tb["published"] == "2026-10-22T09:00:00Z"
    assert tb["has_numbers"] is True
    assert "42% lower tail latency" in tb["source_text"]


def test_score_select_drops_hallucinated_viz_without_dropping_news(monkeypatch):
    work = _MemoryWork()
    dw = digest.DigestWorkspace(work, clock=lambda: NOW_TS)

    # First test fallback (viz_spec=None)
    monkeypatch.setattr(digest, "viz_spec", None)

    candidates = [
        {
            "title": "Grounded Release With Valid Numbers",
            "url": "https://valid.example.org/p1",
            "published_at": "2026-10-22T10:00:00Z",
            "source_text": "Throughput jumped from 120 tokens/s to 240 tokens/s on 64 cards.",
            "quote": "Throughput jumped from 120 tokens/s to 240 tokens/s",
            "summary": "Valid numbers summary",
            "why": "2x speedup",
            "scores": {"info": 5, "source": 5, "relevance": 4, "timeliness": 5, "chat": 4, "novelty": 5},
            "viz": {
                "type": "bars",
                "title": "Throughput",
                "items": [{"label": "Before", "value": 120}, {"label": "After", "value": 240}],
            },
        },
        {
            "title": "Article With Hallucinated Chart",
            "url": "https://halluc.example.org/p2",
            "published_at": "2026-10-22T11:00:00Z",
            "source_text": "Release cut memory overhead by 15 percent over baseline.",
            "quote": "Release cut memory overhead by 15 percent",
            "summary": "Valid news body",
            "why": "Good news",
            "scores": {"info": 4, "source": 4, "relevance": 4, "timeliness": 5, "chat": 4, "novelty": 5},
            "viz": {
                "type": "bars",
                "title": "Fake Comparison",
                "items": [{"label": "Baseline", "value": 15}, {"label": "Invented", "value": 999}],
            },
        },
    ]

    sel = dw.score_select(candidates, max_items=5)
    assert len(sel["selected"]) == 2
    by_url = {it["url"]: it for it in sel["selected"]}
    assert "viz" in by_url["https://valid.example.org/p1"]
    assert "viz" not in by_url["https://halluc.example.org/p2"]
    assert "viz_dropped_reason" in by_url["https://halluc.example.org/p2"]

    # Render card HTML in night dark mode & check CSS vars + viz + newline preservation + no <script>
    card_res = dw.render_card_html(sel["selected"], slot_label="夜览 · 21:30", group_title="测试简报")
    html_out = work.files[card_res["path"]]
    assert card_res["count"] == 2
    assert 'data-theme="dark"' in html_out
    assert "--bg:" in html_out and "--bg-elev:" in html_out and "--ink:" in html_out and "--accent:" in html_out
    assert "vs-bars" in html_out
    assert "<script" not in html_out.lower()

    # Now verify strong viz_spec integration when viz_spec module IS available
    fake_viz_spec = SimpleNamespace(
        validate=lambda spec, prof: spec,
        check_spec_numbers=lambda spec, src: (False, "999 not found") if "999" in json.dumps(spec) else (True, ""),
        render=lambda spec, static=False: '<div class="vs vs-spec-mock">MockStaticViz\nLine2</div>',
        load_viz_css=lambda: ".vs-spec-mock { color: var(--accent); }",
    )
    monkeypatch.setattr(digest, "viz_spec", fake_viz_spec)

    dw2 = digest.DigestWorkspace(_MemoryWork(), clock=lambda: NOW_TS)
    sel2 = dw2.score_select(candidates, max_items=5)
    by_url2 = {it["url"]: it for it in sel2["selected"]}
    assert len(sel2["selected"]) == 2
    assert "viz" in by_url2["https://valid.example.org/p1"]
    assert "viz" not in by_url2["https://halluc.example.org/p2"]
    assert "999" in by_url2["https://halluc.example.org/p2"]["viz_dropped_reason"]

    card_res2 = dw2.render_card_html(sel2["selected"], slot_label="午后")
    html_out2 = dw2.work.files[card_res2["path"]]
    assert ".vs-spec-mock" in html_out2
    assert "MockStaticViz" in html_out2
    assert "<script" not in html_out2.lower()


def test_digest_uses_real_viz_spec_and_renders_static_card():
    assert digest.viz_spec is not None
    dw = digest.DigestWorkspace(_MemoryWork(), clock=lambda: NOW_TS)
    candidates = [
        {
            "title": "Real Viz Card Grounded Item",
            "url": "https://realviz.example.org/a1",
            "published_at": "2026-10-22T10:00:00Z",
            "source_text": "吞吐从 120 tokens/s 提升到 240 tokens/s，支持 64 卡集群。",
            "quote": "吞吐从 120 tokens/s 提升到 240 tokens/s",
            "summary": "Real viz summary",
            "why": "Speedup",
            "scores": {"info": 5, "source": 5, "relevance": 4, "timeliness": 5, "chat": 4, "novelty": 5},
            "viz": {
                "type": "bars",
                "title": "实测吞吐",
                "items": [{"label": "Before", "value": 120}, {"label": "After", "value": 240}],
            },
        },
        {
            "title": "Report-Only Block Rejected For News",
            "url": "https://realviz.example.org/a2",
            "published_at": "2026-10-22T10:30:00Z",
            "source_text": "发布说明含 24 页评测白皮书。",
            "quote": "发布说明含 24 页评测白皮书",
            "summary": "News should remain even when report-only viz is stripped",
            "why": "Graceful fallback",
            "scores": {"info": 4, "source": 5, "relevance": 4, "timeliness": 5, "chat": 4, "novelty": 5},
            "viz": {"type": "heading", "text": "Allowed in report only"},
        },
    ]
    sel = dw.score_select(candidates, max_items=5)
    assert len(sel["selected"]) == 2
    by_url = {it["url"]: it for it in sel["selected"]}
    assert "viz" in by_url["https://realviz.example.org/a1"]
    assert "viz" not in by_url["https://realviz.example.org/a2"]
    assert "viz_dropped_reason" in by_url["https://realviz.example.org/a2"]

    card_res = dw.render_card_html(sel["selected"], slot_label="暮色 · 18:30", group_title="真实图解检验")
    html_out = dw.work.files[card_res["path"]]
    assert 'id="vs-1"' in html_out
    assert "vs-static" in html_out
    assert "vs-bars" in html_out
    assert ".vs-bar-t" in html_out
    assert "<script" not in html_out.lower()
    assert html_out.index("item-summary") < html_out.index("card-viz")


@pytest.mark.asyncio
async def test_prepare_covers_preserves_viz_without_explicit_image_url():
    called_urls: list[str] = []

    async def trap_fetcher(url: str):
        called_urls.append(url)
        return {
            "body": b'<meta property="og:image" content="https://realviz.example.org/banner.jpg">',
            "content_type": "text/html",
            "status": 200,
            "url": url,
            "error": "",
        }

    dw = digest.DigestWorkspace(_MemoryWork(), fetcher=trap_fetcher, clock=lambda: NOW_TS)
    items = [
        {
            "title": "Latency Dropped",
            "url": "https://realviz.example.org/a1",
            "summary": "Latency improved across regions.",
            "why": "2x speedup",
            "viz": {"type": "bars", "title": "p95", "items": [{"label": "Now", "value": 40, "unit": "ms"}]},
        }
    ]
    await dw.prepare_covers(items)
    assert called_urls == []
    assert "cover_data" not in items[0]


@pytest.mark.asyncio
async def test_seed_starter_feeds_disambiguation_concurrent_poll_and_flexible_unsubscribe():
    rss_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0"><channel><title>Dev Feed</title>'
        "<item><title>Release Notes</title><link>https://dev.example/p1</link>"
        "<pubDate>Thu, 22 Oct 2026 10:00:00 GMT</pubDate>"
        "<description>Engine details</description></item>"
        "</channel></rss>"
    ).encode()

    async def fast_fetcher(url: str):
        return {
            "body": rss_body,
            "content_type": "application/rss+xml",
            "status": 200,
            "url": url,
            "error": "",
        }

    # 1. "独立开发者, AI Agent, 开源工具" 严禁误订任何游戏媒体源
    dw_dev = digest.DigestWorkspace(_MemoryWork(), fetcher=fast_fetcher, clock=lambda: NOW_TS)
    seed_dev = await dw_dev.seed_starter_feeds("独立开发者, AI Agent, 开源工具", max_add=4)
    assert seed_dev["count"] == 4
    game_hosts = {
        "gcores.com",
        "chuapp.com",
        "yystv.cn",
        "itch.io",
        "rockpapershotgun.com",
        "godotengine.org",
        "gamedeveloper.com",
    }
    assert all(f["host"].removeprefix("www.") not in game_hosts for f in seed_dev["added"])

    # 2. 单独 2 字 "独立" 禁止子串命中 "独立游戏"
    dw_short = digest.DigestWorkspace(_MemoryWork(), fetcher=fast_fetcher, clock=lambda: NOW_TS)
    seed_short = await dw_short.seed_starter_feeds(["独立"], max_add=4)
    assert all(f["host"].removeprefix("www.") not in game_hosts for f in seed_short["added"])

    # 3. 明确 "独立游戏" 仍可正常命中游戏媒体源
    dw_game = digest.DigestWorkspace(_MemoryWork(), fetcher=fast_fetcher, clock=lambda: NOW_TS)
    seed_game = await dw_game.seed_starter_feeds(["独立游戏", "indie"], max_add=2)
    assert seed_game["count"] == 2
    assert all(f["host"].removeprefix("www.") in game_hosts for f in seed_game["added"])

    # 4. 灵活退订：支持 id、规范化 URL（含跟踪参数）、不区分大小写 title、域名 host
    dw_unsub = digest.DigestWorkspace(_MemoryWork(), fetcher=fast_fetcher, clock=lambda: NOW_TS)
    await dw_unsub.add_feed("https://blog.cloudflare.com/rss/", title="Cloudflare Blog", origin="manual")
    await dw_unsub.add_feed("https://www.gcores.com/rss", title="机核 GCORES", origin="manual")
    await dw_unsub.add_feed("https://github.blog/feed/", title="The GitHub Blog", origin="manual")

    u1 = dw_unsub.unsubscribe("https://gcores.com/rss?utm_source=digest")
    assert u1["ok"] is True and "gcores.com" in u1["url"]
    u2 = dw_unsub.unsubscribe("the github blog")
    assert u2["ok"] is True and u2["title"] == "The GitHub Blog"
    u3 = dw_unsub.unsubscribe("blog.cloudflare.com")
    assert u3["ok"] is True and "cloudflare.com" in u3["url"]
    assert dw_unsub.unsubscribe("   ")["ok"] is False

    # 5. poll() 并发抓取 6 个源，远快于串行 6 * 0.08 = 0.48s
    async def slow_fetcher(url: str):
        await asyncio.sleep(0.08)
        return {
            "body": rss_body,
            "content_type": "application/rss+xml",
            "status": 200,
            "url": url,
            "error": "",
        }

    dw_poll = digest.DigestWorkspace(_MemoryWork(), fetcher=slow_fetcher, clock=lambda: NOW_TS)
    for idx in range(6):
        await dw_poll.add_feed(f"https://feed{idx}.example/rss.xml", title=f"Feed {idx}", auto=False)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    polled = await dw_poll.poll(lookback_days=14)
    elapsed = loop.time() - t0
    assert polled["feeds_count"] == 6
    assert elapsed < 0.30, f"poll 应并发执行而非串行，实测耗时 {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_collect_pool_caches_full_source_text_for_quote_and_viz_without_echoing_body():
    import time
    from email.utils import formatdate

    recent_rfc = formatdate(time.time() - 3600, usegmt=True)
    work = _MemoryWork()

    async def _local_rss(url: str):
        return {
            "body": _SAMPLE_RSS,
            "content_type": "application/xml",
            "status": 200,
            "url": url,
            "error": "",
        }

    dw = digest.DigestWorkspace(work, fetcher=_local_rss)
    await dw.add_feed("https://eng.example/rss.xml", title="Local Game Wire", auto=False)

    distinct_topics = [
        ("crimson-clockwork", "新作实机"),
        ("verdant-hollow", "玩法拆解"),
        ("asterisk-frontier", "销量口碑"),
        ("solstice-harbor", "引擎工具"),
        ("obsidian-foundry", "开发者说"),
        ("lantern-drift", "独立新品"),
        ("cobalt-spire", "独立新品"),
        ("ember-railway", "独立新品"),
        ("velvet-circuit", "独立新品"),
        ("tundra-echo", "独立新品"),
        ("monolith-ward", "独立新品"),
        ("kestrel-harbor", "独立新品"),
        ("saffron-vault", "独立新品"),
        ("quartz-odyssey", "独立新品"),
        ("aurora-tactics", "独立新品"),
        ("basalt-chronicle", "独立新品"),
        ("vermilion-isle", "独立新品"),
        ("graphite-signal", "独立新品"),
        ("halcyon-foundry", "独立新品"),
        ("topaz-lattice", "独立新品"),
    ]

    class _FakeWeb:
        async def search_broad(self, q, max_results=8, context=None):
            return {
                "results": [
                    {
                        "url": f"https://gamedev{i}.example.org/news/{slug}",
                        "title": f"{slug.replace('-', ' ').title()} Steam Demo & Mechanics Breakdown",
                        "published_at": recent_rfc,
                    }
                    for i, (slug, _) in enumerate(distinct_topics)
                ]
            }

        async def fetch(self, url, context=None):
            prefix = f"Article {url} launches combat overhaul. " + ("Gameplay details " * 55)
            tail = " TailFact: peak concurrent players reached 48200 with 93% positive Steam reviews in October."
            return {
                "url": url,
                "title": f"Detail {url}",
                "content": (prefix + tail)[:2100],
                "published_at": recent_rfc,
            }

    collected = await dw.collect(web=_FakeWeb(), queries=["indie game"], max_items=20)
    assert len(collected["candidates"]) == 20
    assert len(json.dumps(collected, ensure_ascii=False)) < 26000
    for cand in collected["candidates"]:
        assert len(str(cand.get("source_text") or "")) <= 850

    pool = dw._load_pool()
    first_key = digest._normalize_url_key(f"https://gamedev0.example.org/news/{distinct_topics[0][0]}")
    cached_src = pool["by_url"][first_key]["source_text"]
    assert len(cached_src) > 900
    assert "48200" in cached_src and "93%" in cached_src

    # score_select omitting source_text still verifies quote & viz against cached pool tail (> 850 chars)
    distinct_briefs = [
        "动作肉鸽《赤色发条》本周重构了弹反与精力循环机制，Boss 二阶段出招节奏更紧凑，Steam 在线峰值冲至 48200。",
        "像素解谜《翠谷幽径》公开开发者关卡通关热力图，团队重写了谜题引导提示后玩家卡关率下降明显。",
        "复古战术 RPG《星标边境》公布首周十万份销量复盘，官方确认下月追加无尽模式与创意工坊模组接口。",
        "《冬至港湾》团队分享用 Godot 4.4 自研水面反射着色器的源码思路，集显核显设备也能稳跑高帧率。",
        "叙事冒险《黑曜石铸造厂》放出一小时试玩 Demo 与剧情分支架构专访，好评率维持在 93% 以上。",
    ]
    candidates = [
        {
            "title": f"{distinct_topics[i][0].replace('-', ' ').title()} 重点更新 #{i}",
            "url": f"https://gamedev{i}.example.org/news/{distinct_topics[i][0]}",
            "quote": "peak concurrent players reached 48200 with 93% positive Steam reviews",
            "brief": distinct_briefs[i],
            "why": f"从第 {i + 1} 个子方向切入，机制与数据细节都很扎实。",
            "topic": distinct_topics[i][1],
            "source": f"GameDev{i}",
            "viz": {
                "type": "stats",
                "items": [
                    {"label": "在线峰值", "value": "48200"},
                    {"label": "Steam 好评", "value": "93%"},
                ],
            }
            if i == 0
            else None,
            "info": 5,
            "source_score": 5,
            "relevance": 5,
            "timeliness": 5,
            "chat": 4,
        }
        for i in range(5)
    ]
    candidates.append(
        {
            "title": "伪造引文的候选应当被池子原文拦截",
            "url": f"https://gamedev5.example.org/news/{distinct_topics[5][0]}",
            "quote": "completely fabricated nonexistent quote snippet",
            "brief": "一段引用无法对照缓存原文的候选，检验防幻觉拒绝逻辑。",
            "why": "验证防幻觉机制。",
            "topic": "防幻觉",
            "source": "GameDev5",
            "info": 5,
            "source_score": 5,
            "relevance": 5,
            "timeliness": 5,
            "chat": 5,
        }
    )
    selected_res = dw.score_select(candidates, max_items=5)
    assert len(selected_res["selected"]) == 5
    assert selected_res["selected"][0].get("viz", {}).get("type") == "stats"
    assert any(
        r["url"] == f"https://gamedev5.example.org/news/{distinct_topics[5][0]}"
        and "引用片段未能在原文中找到对照依据" in r["reject_reason"]
        for r in selected_res["rejected"]
    )

    # render_card_html without explicit max_items defaults to rendering 5 items
    card_res = await digest.run_digest_tool(
        work,
        {
            "action": "render_card_html",
            "group_title": "独立游戏小站",
            "slot_label": "试跑预览",
            "output_path": "out/digest-card.html",
        },
    )
    assert card_res["count"] == 5


def test_score_select_quote_normalizes_cjk_punctuation_and_rejects_translation():
    work = _MemoryWork()
    dw = digest.DigestWorkspace(work)
    chinese_src = (
        "独立开发团队今天公布《山海行》试玩版更新："
        "「两版做的是同一件事：先改剧情战，再改比武大会。」"
        "测试期共有 3200 名玩家通关。"
    )
    res = dw.score_select(
        [
            {
                "title": "《山海行》更新战斗与比武大会节奏",
                "url": "https://indie-zh.example/shanhai-1",
                "source_text": chinese_src,
                "quote": '「两版做的是同一件事：先改剧情战，再改比武大会。」',
                "brief": "独立团队重写了《山海行》剧情战与比武大会难度曲线，测试期已有 3200 名玩家通关新版关卡。",
                "why": "战斗节奏直接大改，值得试试新版手感。",
                "topic": "开发日志",
                "source": "IndieZH",
                "info": 5,
                "source_score": 5,
                "relevance": 5,
                "timeliness": 5,
                "chat": 4,
            },
            {
                "title": "中文弯引号规范化也应命中原文",
                "url": "https://indie-zh.example/shanhai-2",
                "source_text": chinese_src,
                "quote": '"两版做的是同一件事：先改剧情战，再改比武大会。"',
                "brief": "另一条用直双引号引用同一中文原句的测试条目，验证弯引号与直角引号可正常对齐通过。",
                "why": "核验不同引号形态不会误杀原语言引文。",
                "topic": "开发日志",
                "source": "IndieZH2",
                "info": 5,
                "source_score": 5,
                "relevance": 5,
                "timeliness": 5,
                "chat": 4,
            },
            {
                "title": "把中文原文翻译成英文 quote 必须拒绝",
                "url": "https://indie-zh.example/shanhai-3",
                "source_text": chinese_src,
                "quote": "The two versions did the same thing: first story battles, then the tournament.",
                "brief": "模拟模型把中文 source_text 翻译成英文 quote 提交给核验接口的场景，检验严格拒收提示。",
                "why": "防止跨语言意译绕过逐字对照。",
                "topic": "防翻译绕过",
                "source": "IndieZH3",
                "info": 5,
                "source_score": 5,
                "relevance": 5,
                "timeliness": 5,
                "chat": 4,
            },
        ],
        max_items=5,
    )
    assert len(res["selected"]) == 2
    assert {item["url"] for item in res["selected"]} == {
        "https://indie-zh.example/shanhai-1",
        "https://indie-zh.example/shanhai-2",
    }
    assert len(res["rejected"]) == 1
    assert res["rejected"][0]["url"] == "https://indie-zh.example/shanhai-3"
    assert "原语言逐字复制" in res["rejected"][0]["reject_reason"]
