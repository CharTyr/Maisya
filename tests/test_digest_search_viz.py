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
