"""Batch 1 (P0) security, concurrency, anti-hallucination, and card rendering audit tests."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import random
import re
import socket
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from maisya import digest, digest_card, report, viz_spec, web


class _LocalWork:
    """Minimal disk-backed workspace double compatible with DigestWorkspace and report."""

    def __init__(self, root: Path) -> None:
        self.path = root
        for sub in ("project", "in", "out", "cache"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        self.commits = 0
        self.io_threads: list[int] = []

    def read(self, rel_path: str, offset: int = 0) -> dict[str, Any]:
        self.io_threads.append(threading.get_ident())
        target = self.path / rel_path
        if not target.exists():
            raise FileNotFoundError(rel_path)
        raw = target.read_bytes()
        chunk = raw[offset : offset + 12000]
        return {
            "text": chunk.decode("utf-8", errors="replace"),
            "next_offset": offset + len(chunk) if offset + len(chunk) < len(raw) else None,
            "bytes": len(raw),
        }

    def write(self, rel_path: str, content: str) -> dict[str, Any]:
        return self.write_bytes(rel_path, content.encode("utf-8"))

    def write_bytes(self, rel_path: str, data: bytes) -> dict[str, Any]:
        self.io_threads.append(threading.get_ident())
        target = self.path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return {"path": rel_path, "bytes": len(data)}

    def commit(self) -> str:
        self.io_threads.append(threading.get_ident())
        self.commits += 1
        return f"v_{self.commits:032x}"


def _make_noisy_png_bytes(width: int, height: int, *, seed: int = 7) -> bytes:
    rng = random.Random(seed)
    raw = bytearray(width * height * 3)
    for idx in range(0, len(raw), 3):
        raw[idx] = rng.randrange(256)
        raw[idx + 1] = rng.randrange(256)
        raw[idx + 2] = rng.randrange(256)
    im = Image.frombytes("RGB", (width, height), bytes(raw))
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_fetch_public_bytes_offloads_dns_and_blocks_private_in_worker_thread(
    monkeypatch,
):
    loop_tid = threading.get_ident()
    dns_tids: list[int] = []

    def _slow_private_dns(host: str, port: int, *args, **kwargs):
        dns_tids.append(threading.get_ident())
        time.sleep(0.12)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", _slow_private_dns)

    ticks: list[float] = []

    async def _heartbeat() -> None:
        for _ in range(3):
            await asyncio.sleep(0.02)
            ticks.append(time.monotonic())

    hb = asyncio.create_task(_heartbeat())
    with pytest.raises(ValueError, match="内网|保留|本地"):
        await digest.fetch_public_bytes("https://rebind-test.example/feed.xml")
    await hb

    assert dns_tids, "socket.getaddrinfo must be invoked"
    assert all(tid != loop_tid for tid in dns_tids), "DNS resolution must run off event loop"
    assert len(ticks) == 3, "Concurrent asyncio tasks must not be starved during DNS lookup"


@pytest.mark.asyncio
async def test_fetch_public_bytes_pins_ip_against_dns_rebinding_and_disables_proxy(
    monkeypatch,
):
    dns_calls = 0
    pinned_connect_targets: list[tuple[str, int]] = []
    seen_proxy_handlers: list[dict[str, str]] = []
    captured_headers: list[tuple[str, str]] = []

    def _rebinding_dns(host: str, port: int, *args, **kwargs):
        nonlocal dns_calls
        dns_calls += 1
        ip = "93.184.216.34" if dns_calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    class _FakeResp:
        status = 200
        url = "http://rebind.example.com/feed.xml"
        headers = {"Content-Type": "application/xml"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n: int = -1) -> bytes:
            return b"<rss><channel><title>OK</title></channel></rss>"

    class _FakeSock:
        def __init__(self, family, kind):
            self.family = family
            self.kind = kind
            self.timeout = None

        def settimeout(self, t):
            self.timeout = t

        def connect(self, addr):
            pinned_connect_targets.append((addr[0], addr[1]))

        def close(self):
            pass

    real_build_opener = urllib.request.build_opener

    def _spy_build_opener(*handlers):
        for h in handlers:
            if isinstance(h, urllib.request.ProxyHandler):
                seen_proxy_handlers.append(dict(h.proxies))
        opener = real_build_opener(*handlers)

        def _fake_open(req, timeout=None):
            captured_headers.append((req.full_url, req.host))
            for h in opener.handlers:
                conn_cls = getattr(h, "_conn_cls", None)
                if conn_cls is not None and type(h) is not urllib.request.HTTPSHandler:
                    conn = conn_cls(req.host, timeout=timeout)
                    conn.connect()
                    assert conn.host == "rebind.example.com"
                    break
            return _FakeResp()

        opener.open = _fake_open
        return opener

    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_dns)
    monkeypatch.setattr(socket, "socket", _FakeSock)
    monkeypatch.setattr(urllib.request, "build_opener", _spy_build_opener)

    res = await digest.fetch_public_bytes("http://rebind.example.com/feed.xml")
    assert res["status"] == 200
    assert dns_calls == 1, f"DNS must resolve only once per hop, got {dns_calls}"
    assert seen_proxy_handlers == [{}], "ProxyHandler({}) must disable environment proxies"
    assert pinned_connect_targets == [("93.184.216.34", 80)]
    assert captured_headers == [("http://rebind.example.com/feed.xml", "rebind.example.com")]


def test_web_default_http_request_pins_dns_ip_against_rebinding(monkeypatch):
    dns_calls = 0
    connect_targets: list[str] = []

    def _rebinding_dns(host: str, port: int, *args, **kwargs):
        nonlocal dns_calls
        dns_calls += 1
        ip = "93.184.216.34" if dns_calls == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    class _FakeSock:
        def __init__(self, family, kind):
            pass

        def settimeout(self, t):
            pass

        def connect(self, addr):
            connect_targets.append(addr[0])

        def close(self):
            pass

    class _FakeResp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def geturl(self):
            return "http://rebind.example.com/api"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n: int = -1) -> bytes:
            return b'{"ok":true}'

    real_build_opener = urllib.request.build_opener

    def _spy_build_opener(*handlers):
        opener = real_build_opener(*handlers)

        def _fake_open(req, timeout=None):
            for h in opener.handlers:
                conn_cls = getattr(h, "_conn_cls", None)
                if conn_cls is not None and type(h) is not urllib.request.HTTPHandler:
                    conn = conn_cls(req.host, timeout=timeout)
                    conn.connect()
                    break
            return _FakeResp()

        opener.open = _fake_open
        return opener

    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_dns)
    monkeypatch.setattr(socket, "socket", _FakeSock)
    monkeypatch.setattr(urllib.request, "build_opener", _spy_build_opener)

    status, _, raw, _ = web._default_http_request(
        "GET", "http://rebind.example.com/api", public_only=True
    )
    assert status == 200
    assert raw == b'{"ok":true}'
    assert dns_calls == 1
    assert connect_targets == ["93.184.216.34"]


@pytest.mark.asyncio
async def test_run_digest_tool_and_prepare_covers_run_blocking_work_off_event_loop(
    tmp_path, monkeypatch
):
    loop_tid = threading.get_ident()
    work = _LocalWork(tmp_path)

    await digest.run_digest_tool(work, {"action": "status"})
    assert work.io_threads and all(tid != loop_tid for tid in work.io_threads)
    work.io_threads.clear()

    png = _make_noisy_png_bytes(640, 400, seed=11)
    pil_threads: list[int] = []
    real_encode = digest_card.encode_cover_data_uri

    def _spy_encode(*args, **kwargs):
        pil_threads.append(threading.get_ident())
        return real_encode(*args, **kwargs)

    monkeypatch.setattr(digest_card, "encode_cover_data_uri", _spy_encode)

    async def _fake_fetcher(url: str) -> dict[str, Any]:
        return {
            "body": png,
            "content_type": "image/png",
            "status": 200,
            "url": url,
            "error": "",
        }

    dw = digest.DigestWorkspace(work, fetcher=_fake_fetcher)
    items = [
        {
            "title": "Async PIL cover",
            "url": "https://news.example.com/a1",
            "image_url": "https://cdn.example.com/hero.png",
            "source": "example.com",
            "why": "worth reading",
            "summary": "summary text",
            "quote": "summary text",
        }
    ]
    await dw.prepare_covers(items)
    assert pil_threads and all(tid != loop_tid for tid in pil_threads)
    assert items[0].get("cover_data", "").startswith("data:image/")

    work.io_threads.clear()
    out = await digest.run_digest_tool(
        work,
        {"action": "render_card_html", "candidates": items, "total_candidates": 1},
    )
    assert out["path"] == "out/digest-card.html"
    assert work.io_threads and all(tid != loop_tid for tid in work.io_threads)


def test_render_report_card_recompresses_large_local_images_under_512kib(tmp_path):
    work = _LocalWork(tmp_path)
    img1 = _make_noisy_png_bytes(960, 600, seed=1)
    img2 = _make_noisy_png_bytes(900, 550, seed=2)
    assert 550_000 < len(img1) < report.MAX_IMAGE_BYTES
    work.write_bytes("in/hero.png", img1)
    work.write_bytes("in/detail.png", img2)

    res = report.render_report_artifact(
        work,
        {
            "title": "图文卡片压缩验证",
            "layout": "card",
            "path": "out/report-card.html",
            "blocks": [
                {"type": "heading", "text": "核心配图"},
                {"type": "image", "path": "in/hero.png", "alt": "主图"},
                {"type": "image", "path": "in/detail.png", "alt": "附图"},
                {"type": "text", "text": "图表结论完成。"},
            ],
        },
    )
    html_text = (tmp_path / "out/report-card.html").read_text(encoding="utf-8")
    raw_bytes = len(html_text.encode("utf-8"))
    assert res["bytes"] == raw_bytes
    assert raw_bytes < 524_288, f"Card HTML exceeded 512 KiB limit: {raw_bytes}"
    assert "data:image/jpeg;base64," in html_text
    assert 'http-equiv="Content-Security-Policy"' in html_text


@pytest.mark.asyncio
async def test_collect_preserves_long_article_beyond_2200_chars_for_quote_check(tmp_path):
    work = _LocalWork(tmp_path)
    prefix = "Opening context paragraph about infrastructure. " * 90  # > 4000 chars
    tail_quote = "Late-section benchmark confirms 42% p99 latency drop."
    long_article = f"{prefix} {tail_quote} Concluding remarks."

    class _FakeWeb:
        async def search_broad(self, query: str, **kwargs):
            return {
                "results": [
                    {
                        "title": "Long Engineering Deep Dive",
                        "url": "https://eng.example.com/posts/deep-dive",
                        "snippet": "Opening context snippet",
                    }
                ]
            }

        async def fetch(self, url: str, **kwargs):
            return {
                "url": url,
                "title": "Long Engineering Deep Dive",
                "content": long_article,
                "published_at": "2026-11-14T10:00:00Z",
                "feed_links": [],
            }

    dw = digest.DigestWorkspace(work, clock=lambda: 1794700000.0)
    collected = await dw.collect(
        web=_FakeWeb(),
        queries=["engineering deep dive"],
        lookback_days=7,
        max_items=6,
    )
    assert len(collected["candidates"]) == 1
    assert len(collected["candidates"][0]["source_text"]) <= 850

    pool = json.loads((tmp_path / "project/digest_pool.json").read_text(encoding="utf-8"))
    pooled_text = pool["by_url"]["eng.example.com/posts/deep-dive"]["source_text"]
    assert tail_quote in pooled_text

    sel = dw.score_select(
        [
            {
                "title": "Long Engineering Deep Dive",
                "url": "https://eng.example.com/posts/deep-dive",
                "source": "eng.example.com",
                "topic": "Engineering",
                "kind": "news",
                "source_text": collected["candidates"][0]["source_text"],
                "quote": tail_quote,
                "why": "Concrete tail quote check",
                "summary": "Deep dive reported lower latency.",
                "scores": {
                    "info": 5,
                    "source": 5,
                    "relevance": 5,
                    "timeliness": 5,
                    "chat": 5,
                    "novelty": 5,
                },
            }
        ],
        max_items=3,
    )
    assert len(sel["selected"]) == 1, f"Expected tail quote to verify, rejected={sel['rejected']}"


def test_score_select_rejects_empty_quote_unsafe_urls_and_domain_variants(tmp_path):
    work = _LocalWork(tmp_path)
    dw = digest.DigestWorkspace(work, clock=lambda: 1794700000.0)

    # Seed pool + blocked domains (both bare domain and www. prefix)
    dw._save_history({"items": [], "blocked_domains": ["blocked.org", "www.spam.net"]})
    dw._save_pool(
        {
            "updated_at": 1794700000.0,
            "total_discovered": 1,
            "by_url": {
                "good.example.com/post-1": {
                    "url": "https://good.example.com/post-1",
                    "source_text": "Verified sentence inside the article body.",
                }
            },
        }
    )

    good_scores = {
        "info": 5,
        "source": 5,
        "relevance": 5,
        "timeliness": 5,
        "chat": 5,
        "novelty": 5,
    }
    candidates = [
        {
            "title": "Control valid article",
            "url": "https://good.example.com/post-control",
            "source_text": "Control article body sentence here.",
            "quote": "Control article body sentence here.",
            "summary": "Control valid summary",
            "scores": good_scores,
        },
        {
            "title": "Empty quote with pool entry",
            "url": "https://good.example.com/post-1",
            "quote": "",
            "summary": "Valid summary",
            "scores": good_scores,
        },
        {
            "title": "Missing quote without pool or source",
            "url": "https://unpooled.example.com/post-2",
            "summary": "Valid summary",
            "scores": good_scores,
        },
        {
            "title": "WWW blocked",
            "url": "https://www.blocked.org/news/1",
            "source_text": "Verified sentence inside the article body.",
            "quote": "Verified sentence inside the article body.",
            "summary": "Valid summary",
            "scores": good_scores,
        },
        {
            "title": "Subdomain blocked",
            "url": "https://blog.blocked.org/news/2",
            "source_text": "Verified sentence inside the article body.",
            "quote": "Verified sentence inside the article body.",
            "summary": "Valid summary",
            "scores": good_scores,
        },
        {
            "title": "Bare blocked when www stored",
            "url": "https://spam.net/news/3",
            "source_text": "Verified sentence inside the article body.",
            "quote": "Verified sentence inside the article body.",
            "summary": "Valid summary",
            "scores": good_scores,
        },
        {
            "title": "Private IP",
            "url": "http://127.0.0.1/admin",
            "source_text": "Verified sentence inside the article body.",
            "quote": "Verified sentence inside the article body.",
            "summary": "Valid summary",
            "scores": good_scores,
        },
        {
            "title": "Metadata IP",
            "url": "http://169.254.169.254/latest/meta-data/",
            "source_text": "Verified sentence inside the article body.",
            "quote": "Verified sentence inside the article body.",
            "summary": "Valid summary",
            "scores": good_scores,
        },
        {
            "title": "JS scheme",
            "url": "javascript:alert(1)",
            "source_text": "Verified sentence inside the article body.",
            "quote": "Verified sentence inside the article body.",
            "summary": "Valid summary",
            "scores": good_scores,
        },
    ]

    res = dw.score_select(candidates, max_items=8)
    assert [it["title"] for it in res["selected"]] == ["Control valid article"]
    rejected_by_title = {it["title"]: it.get("reject_reason", "") for it in res["rejected"]}
    assert set(rejected_by_title) == {
        "Empty quote with pool entry",
        "Missing quote without pool or source",
        "WWW blocked",
        "Subdomain blocked",
        "Bare blocked when www stored",
        "Private IP",
        "Metadata IP",
        "JS scheme",
    }
    assert "引用" in rejected_by_title["Empty quote with pool entry"] or "原文" in rejected_by_title["Empty quote with pool entry"]
    assert "引用" in rejected_by_title["Missing quote without pool or source"] or "原文" in rejected_by_title["Missing quote without pool or source"]
    for url_bad in ("WWW blocked", "Subdomain blocked", "Bare blocked when www stored", "Private IP", "Metadata IP", "JS scheme"):
        assert "链接" in rejected_by_title[url_bad] or "域名" in rejected_by_title[url_bad]


def test_score_select_guide_quota_not_consumed_on_defer_and_overflow_in_rejected(tmp_path):
    # Case 1: regular guide deferred because of explore slot, explore slot cannot fill (same story),
    # Pass 2 MUST still select the guide rather than wrongly rejecting guides_used >= 1.
    work1 = _LocalWork(tmp_path / "case1")
    dw1 = digest.DigestWorkspace(work1, clock=lambda: 1794700000.0)
    src = "Shared topic detail and guide explanation in verified source."
    reg_scores = {
        "info": 5,
        "source": 5,
        "relevance": 5,
        "timeliness": 5,
        "chat": 5,
        "novelty": 5,
    }
    guide_scores = {
        "info": 5,
        "source": 5,
        "relevance": 4,
        "timeliness": 4,
        "chat": 4,
        "novelty": 5,
    }
    explore_scores = {
        "info": 5,
        "source": 4,
        "relevance": 2.5,
        "timeliness": 4,
        "chat": 5,
        "novelty": 5,
        "surprise": 5,
    }
    res1 = dw1.score_select(
        [
            {
                "title": "Alpha Release Launches New Kernel Features",
                "url": "https://news.example.com/alpha",
                "kind": "news",
                "source_text": src,
                "quote": "Shared topic detail",
                "summary": "Alpha release summary",
                "scores": reg_scores,
            },
            {
                "title": "Step-by-step Rust Migration Handbook",
                "url": "https://docs.example.com/rust-guide",
                "kind": "guide",
                "published": "2026-11-14T10:00:00Z",
                "source_text": src,
                "quote": "guide explanation in verified source",
                "summary": "Rust migration guide summary",
                "scores": guide_scores,
            },
            {
                "title": "Alpha Release Launches New Kernel Features Update",
                "url": "https://explore.example.com/alpha-dup",
                "kind": "news",
                "bridge": "Related kernel topic",
                "source_text": src,
                "quote": "Shared topic detail",
                "summary": "Explore duplicate summary",
                "scores": explore_scores,
            },
        ],
        max_items=2,
    )
    selected_urls1 = {it["url"] for it in res1["selected"]}
    assert "https://docs.example.com/rust-guide" in selected_urls1

    # Case 2: explore slot fills max_items, so remaining deferred regular candidates MUST appear in rejected.
    work2 = _LocalWork(tmp_path / "case2")
    dw2 = digest.DigestWorkspace(work2, clock=lambda: 1794700000.0)
    res2 = dw2.score_select(
        [
            {
                "title": "Top Story One",
                "url": "https://news.example.com/one",
                "kind": "news",
                "source_text": src,
                "quote": "Shared topic detail",
                "summary": "Story one summary",
                "scores": reg_scores,
            },
            {
                "title": "Second Regular Deferred Guide",
                "url": "https://news.example.com/two",
                "kind": "guide",
                "published": "2026-11-14T10:00:00Z",
                "source_text": src,
                "quote": "guide explanation",
                "summary": "Guide two summary",
                "scores": guide_scores,
            },
            {
                "title": "Third Regular Overflow Story",
                "url": "https://news.example.com/three",
                "kind": "news",
                "source_text": src,
                "quote": "verified source",
                "summary": "Story three summary",
                "scores": {
                    "info": 4,
                    "source": 4,
                    "relevance": 4,
                    "timeliness": 4,
                    "chat": 4,
                    "novelty": 5,
                },
            },
            {
                "title": "Distinct Wildcard Discovery",
                "url": "https://explore.example.com/wildcard",
                "kind": "news",
                "bridge": "Surprising angle worth sharing",
                "source_text": src,
                "quote": "Shared topic detail",
                "summary": "Wildcard discovery summary",
                "scores": explore_scores,
            },
        ],
        max_items=2,
    )
    assert len(res2["selected"]) == 2
    rejected_urls2 = {it["url"] for it in res2["rejected"]}
    assert "https://news.example.com/two" in rejected_urls2
    assert "https://news.example.com/three" in rejected_urls2


def test_digest_card_blocks_svg_data_uri_css_injection_xss_and_emits_csp(tmp_path):
    svg_b64 = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>').decode()
    svg_uri = f"data:image/svg+xml;base64,{svg_b64}"

    malicious_viz = (
        '<div class="vs-wrap" style="position:fixed;background:url(https://evil.test/x);width:48%">'
        '<script>alert(1)</script>'
        '<img src="x" onerror="alert(1)">'
        '<input class="vs-tab-radio" type="radio" name="t" id="t1" checked>'
        '<label class="vs-tab-label" for="t1">T</label>'
        '<iframe src="data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;"></iframe>'
        '<object data="javascript:alert(1)"></object>'
        '<a href="JaVaScRiPt:alert(1)">bad</a>'
        '<svg class="vs-line-svg" viewBox="0 0 100 40" onload="alert(1)">'
        '<animate onbegin="alert(1)" attributeName="x"/>'
        '<foreignObject><div onclick="alert(1)">hi</div></foreignObject>'
        '<a xlink:href="javascript:alert(1)">svglink</a>'
        '<circle cx="10" cy="20" r="3"></circle>'
        '</svg>'
        "</div>"
    )

    doc = digest_card.build_card_html(
        [
            {
                "title": "Security Card",
                "url": "https://good.example.com/a1",
                "source": "good.example.com",
                "why": "Check sanitization",
                "summary": "Safe body",
                "cover_data": svg_uri,
                "cover_tint": {
                    "glow": "red;position:fixed;inset:0;background-image:url(https://evil.test)",
                    "why_bg": "#fff;position:fixed",
                    "why_ink": "#000;animation:spin 1s",
                },
                "viz_frag": malicious_viz,
            }
        ],
        total_candidates=1,
    )

    lowered = doc.lower()
    assert "data:image/svg+xml" not in lowered
    assert "position:fixed" not in lowered
    assert "url(https://evil.test" not in lowered
    assert "<script" not in lowered
    assert "<iframe" not in lowered
    assert "<object" not in lowered
    assert "<foreignobject" not in lowered
    assert "<animate" not in lowered
    assert "onerror=" not in lowered
    assert "onload=" not in lowered
    assert "onbegin=" not in lowered
    assert "onclick=" not in lowered
    assert "javascript:" not in lowered
    assert "data:text/html" not in lowered
    assert '<meta http-equiv="Content-Security-Policy"' in doc
    assert "default-src 'none'" in doc
    # Safe chart geometry & styling must survive sanitization
    assert 'class="vs-line-svg"' in doc
    assert "<circle " in doc
    assert 'type="radio"' in doc
    assert "width:48%" in doc or "width: 48%" in doc

    # Also verify valid viz tabs + line chart survive render_card_html intact
    work = _LocalWork(tmp_path)
    dw = digest.DigestWorkspace(work, clock=lambda: 1794700000.0)
    res = dw.render_card_html(
        [
            {
                "title": "Valid Interactive Tabs Card",
                "url": "https://good.example.com/tabs",
                "source": "good.example.com",
                "why": "Interactive viz",
                "summary": "Safe body",
                "cover_data": svg_uri,
                "viz": {
                    "v": 1,
                    "blocks": [
                        {
                            "type": "tabs",
                            "tabs": [
                                {
                                    "label": "趋势",
                                    "blocks": [
                                        {
                                            "type": "line",
                                            "points": [
                                                {"x": "1月", "y": 10},
                                                {"x": "2月", "y": 20},
                                                {"x": "3月", "y": 30},
                                            ],
                                            "unit": "%",
                                        }
                                    ],
                                },
                                {
                                    "label": "对比",
                                    "blocks": [
                                        {
                                            "type": "bars",
                                            "items": [
                                                {"label": "甲", "value": 30},
                                                {"label": "乙", "value": 20},
                                            ],
                                            "unit": "%",
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                },
            }
        ],
        total_candidates=1,
    )
    rendered_doc = (tmp_path / res["path"]).read_text(encoding="utf-8")
    assert "data:image/svg+xml" not in rendered_doc.lower()
    assert "vs-tab-section" in rendered_doc
    assert "vs-line-svg" in rendered_doc


def test_viz_numbers_sign_comma_decimal_and_codename_verification():
    assert viz_spec.numbers_in("同比暴跌 -15%，区间 10,20，日期 2026-11-15，范围 10-20 天") == [
        -15.0,
        10.0,
        20.0,
        2026.0,
        11.0,
        15.0,
        10.0,
        20.0,
    ]

    ok_flip1, _ = viz_spec.check_numbers(["涨幅 15%"], "同比下降 -15%")
    assert not ok_flip1
    ok_flip2, _ = viz_spec.check_numbers(["跌幅 -15%"], "同比增长 15%")
    assert not ok_flip2
    ok_flip_small, _ = viz_spec.check_numbers(["增速 -5%"], "同比增长 5%")
    assert not ok_flip_small
    ok_pct_large, _ = viz_spec.check_numbers(["暴涨 42%", "涨幅 999%"], "营收 100 亿元")
    assert not ok_pct_large

    ok_dec1, _ = viz_spec.check_numbers(["转化率 15.6%，营收 1.5万"], "转化率 0.156，营收 15000 元")
    assert ok_dec1
    ok_dec2, _ = viz_spec.check_numbers(["转化率 0.156，营收 15000"], "转化率 15.6%，营收 1.5万")
    assert ok_dec2

    ok_model_txt, _ = viz_spec.check_numbers(
        ["搭载 H200 芯片，Llama3 与 GPT5 实测提升 15%"], "实测提升 15%"
    )
    assert not ok_model_txt

    ok_model_spec, _ = viz_spec.check_spec_numbers(
        {"v": 1, "blocks": [{"type": "bars", "items": [{"label": "H200", "value": 15}]}]},
        "另一款芯片实测提升 15%",
    )
    assert not ok_model_spec

    ok_real_model, _ = viz_spec.check_spec_numbers(
        {"v": 1, "blocks": [{"type": "bars", "items": [{"label": "H200 / Llama3", "value": 15}]}]},
        "H200 搭配 Llama 3 实测提升 15%",
    )
    assert ok_real_model


def test_render_bars_renders_negative_bars_downward_from_zero_baseline():
    spec = viz_spec.validate(
        {
            "v": 1,
            "blocks": [
                {
                    "type": "bars",
                    "items": [
                        {"label": "下降项", "value": -50, "unit": "%"},
                        {"label": "增长项", "value": 50, "unit": "%"},
                    ],
                }
            ],
        },
        profile="report",
    )
    html_out = viz_spec.render(spec, static=True)
    assert "vs-bar-zero" in html_out
    assert "is-neg" in html_out
    assert "is-pos" in html_out
    assert re.search(r'class="vs-bar-b[^"]*is-neg[^"]*"[^>]*style="[^"]*top:\s*50\.0%', html_out)
    assert re.search(r'class="vs-bar-b[^"]*is-pos[^"]*"[^>]*style="[^"]*bottom:\s*50\.0%', html_out)
