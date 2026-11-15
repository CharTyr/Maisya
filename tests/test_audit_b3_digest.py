"""Batch 3 (P2 + blind-spot closure) TDD tests for maisya/digest.py & maisya/digest_card.py."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import io
import math
from pathlib import Path
import random
from typing import Any

from PIL import Image, ImageDraw
import pytest

from maisya import digest, digest_card


class _LocalWork:
    """Minimal disk-backed WorkingCopy test harness."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root)
        self.path.mkdir(parents=True, exist_ok=True)
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
        target.write_bytes(data)
        return {"path": rel_path, "bytes": len(data)}

    def commit(self) -> str:
        self.commits += 1
        return f"v_{self.commits:032x}"


def _sample_rss(title: str, pub_rfc: str = "Thu, 15 Nov 2026 08:00:00 GMT") -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<rss version="2.0"><channel><title>{title}</title>'
        f"<item><title>{title} Dispatch</title>"
        f"<link>https://news.example/{abs(hash(title)) % 9999}</link>"
        f"<pubDate>{pub_rfc}</pubDate>"
        "<description>Detailed verified content body with benchmark 42%.</description>"
        "</item></channel></rss>"
    ).encode("utf-8")


def _make_framed_photo_bytes(
    w: int = 800,
    h: int = 600,
    *,
    border_ratio: float = 0.24,
    border_color: tuple[int, int, int] = (255, 255, 255),
    monochrome: bool = False,
) -> bytes:
    """Build a continuous-tone photograph centered inside a solid border (~70-73% frame area)."""
    img = Image.new("RGB", (w, h), border_color)
    px = img.load()
    assert px is not None
    bx = int(w * border_ratio)
    by = int(h * border_ratio)
    iw = max(1, w - 2 * bx)
    ih = max(1, h - 2 * by)
    for y in range(by, h - by):
        for x in range(bx, w - bx):
            lx = x - bx
            ly = y - by
            wave = int(20 * math.sin(lx / 22.0) + 18 * math.cos(ly / 18.0))
            if monochrome:
                val = max(10, min(245, 50 + (lx * 140) // iw + (ly * 40) // ih + wave))
                px[x, y] = (val, val, val)
            else:
                r = max(0, min(255, 35 + (lx * 190) // iw + wave))
                g = max(0, min(255, 45 + (ly * 180) // ih + wave))
                b = max(0, min(255, 210 - (lx * 140) // iw + (ly * 30) // ih))
                px[x, y] = (r, g, b)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _make_noisy_photo_bytes(w: int, h: int, *, seed: int = 1) -> bytes:
    """Create a large high-entropy noisy image that compresses to substantial JPEG bytes."""
    rng = random.Random(seed)
    raw = bytes(rng.randrange(0, 256) for _ in range(w * h * 3))
    img = Image.frombytes("RGB", (w, h), raw)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_seed_starter_feeds_unmatched_topic_no_fallback_vs_empty_cold_start(tmp_path):
    now = 1_794_740_000.0
    probed: list[str] = []

    async def fake_fetcher(url: str) -> dict[str, Any]:
        probed.append(url)
        return {
            "body": _sample_rss(url),
            "content_type": "application/rss+xml",
            "status": 200,
            "url": url,
            "error": "",
        }

    # 1. Explicit non-matching topic must NOT silently fall back to unrelated starter feeds
    dw_unmatched = digest.DigestWorkspace(_LocalWork(tmp_path / "u1"), fetcher=fake_fetcher, clock=lambda: now)
    res_unmatched = await asyncio.wait_for(
        dw_unmatched.seed_starter_feeds("水下量子织篮工艺XYZ", max_add=4),
        timeout=3.0,
    )
    assert res_unmatched["added"] == []
    assert res_unmatched["count"] == 0
    assert res_unmatched.get("unmatched") is True
    assert res_unmatched.get("matched") is False
    assert probed == [], f"Unmatched explicit topic must not probe unrelated feeds: {probed}"

    # 2. Same guarantee via run_digest_tool(action="seed")
    work_tool = _LocalWork(tmp_path / "u2")
    res_tool = await asyncio.wait_for(
        digest.run_digest_tool(
            work_tool,
            {"action": "seed", "topics": ["绝对冷门太空折纸XYZ"], "max_items": 4},
        ),
        timeout=3.0,
    )
    assert res_tool["added"] == []
    assert res_tool["count"] == 0
    assert res_tool.get("unmatched") is True

    # 3. Empty / whitespace cold-start must still seed default starter feeds
    probed.clear()
    dw_empty = digest.DigestWorkspace(_LocalWork(tmp_path / "e1"), fetcher=fake_fetcher, clock=lambda: now)
    res_empty = await asyncio.wait_for(
        dw_empty.seed_starter_feeds("", max_add=3),
        timeout=3.0,
    )
    assert res_empty["count"] == 3
    assert len(res_empty["added"]) == 3

    probed.clear()
    dw_punct = digest.DigestWorkspace(_LocalWork(tmp_path / "e2"), fetcher=fake_fetcher, clock=lambda: now)
    res_punct = await asyncio.wait_for(
        dw_punct.seed_starter_feeds(["  ", " , "], max_add=2),
        timeout=3.0,
    )
    assert res_punct["count"] == 2
    assert len(res_punct["added"]) == 2


@pytest.mark.asyncio
async def test_digest_card_and_workspace_honor_tz_offset_minutes(tmp_path):
    # Dead hardcoded UTC+8 constant must be removed
    assert not hasattr(digest_card, "_SHANGHAI_TZ")

    # 2026-11-15T01:00:00Z -> UTC+8 is 09:00 Sun (morning), UTC+0 is 01:00 Sun (dawn),
    # UTC-5 (-300) is 20:00 Sat Nov 14 (night), UTC+12 (720) is 13:00 Sun (noon).
    ts_01_utc = datetime(2026, 11, 15, 1, 0, 0, tzinfo=timezone.utc).timestamp()

    assert digest_card.match_sky(now_ts=ts_01_utc, tz_offset_minutes=480)[0] == "morning"
    assert digest_card.match_sky(now_ts=ts_01_utc, tz_offset_minutes=0)[0] == "dawn"
    assert digest_card.match_sky(now_ts=ts_01_utc, tz_offset_minutes=-300)[0] == "night"
    assert digest_card.match_sky(now_ts=ts_01_utc, tz_offset_minutes=720)[0] == "noon"

    # _ago >30 days boundary crossing midnight across timezones:
    # 2026-09-30T22:00:00Z -> UTC+8 is 10月1日, UTC+0 is 9月30日
    pub_boundary = datetime(2026, 9, 30, 22, 0, 0, tzinfo=timezone.utc).timestamp()
    assert digest_card._ago(pub_boundary, ts_01_utc, tz_offset_minutes=480) == "10月1日"
    assert digest_card._ago(pub_boundary, ts_01_utc, tz_offset_minutes=0) == "9月30日"

    sample_item = [
        {
            "title": "时区渲染测试条目",
            "url": "https://news.example/tz-test",
            "brief": "验证卡片日期与条目发布时间使用同一偏移。",
            "why": "避免跨时区显示漂移。",
            "source": "news.example",
            "published_ts": pub_boundary,
            "kind": "news",
        }
    ]
    html_utc8 = digest_card.build_card_html(sample_item, now_ts=ts_01_utc, tz_offset_minutes=480)
    html_est = digest_card.build_card_html(sample_item, now_ts=ts_01_utc, tz_offset_minutes=-300)
    html_utc0 = digest_card.build_card_html(sample_item, now_ts=ts_01_utc, tz_offset_minutes=0)

    assert 'data-sky="morning"' in html_utc8
    assert "11月15日 · 周日" in html_utc8
    assert "10月1日" in html_utc8

    assert 'data-sky="night"' in html_est
    assert "11月14日 · 周六" in html_est
    assert "9月30日" in html_est

    assert 'data-sky="dawn"' in html_utc0
    assert "11月15日 · 周日" in html_utc0
    assert "9月30日" in html_utc0

    # Validation guard: reject bool and out-of-range offsets
    with pytest.raises(ValueError):
        digest_card.build_card_html(sample_item, now_ts=ts_01_utc, tz_offset_minutes=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        digest_card.build_card_html(sample_item, now_ts=ts_01_utc, tz_offset_minutes=-900)

    # DigestWorkspace accepts optional tz_offset_minutes and forwards to render_card_html + collect
    work = _LocalWork(tmp_path / "dw_tz")
    dw = digest.DigestWorkspace(work, clock=lambda: ts_01_utc, tz_offset_minutes=-300)
    assert dw.tz_offset_minutes == -300
    card_res = dw.render_card_html(sample_item, output_path="out/tz-card.html", total_candidates=1)
    written = (work.path / "out/tz-card.html").read_text(encoding="utf-8")
    assert card_res["path"] == "out/tz-card.html"
    assert "11月14日 · 周六" in written
    assert 'data-sky="night"' in written

    # setattr + run_digest_tool compatibility (entry/cross-slice contract)
    dw.tz_offset_minutes = 720
    dw.render_card_html(sample_item, output_path="out/tz-card-12.html", total_candidates=1)
    assert 'data-sky="noon"' in (work.path / "out/tz-card-12.html").read_text(encoding="utf-8")

    class _FakeWeb:
        async def search_broad(self, *args, **kwargs):
            return {"results": [], "provider": "fake"}

    # Backward-compatible no-kwarg instantiation + setattr via run_digest_tool context
    work_ctx = _LocalWork(tmp_path / "ctx_tz")
    work_ctx.tz_offset_minutes = -300  # type: ignore[attr-defined]
    res_tool = await asyncio.wait_for(
        digest.run_digest_tool(
            work_ctx,
            {
                "action": "render_card_html",
                "candidates": sample_item,
                "output_path": "out/ctx-card.html",
            },
            web=_FakeWeb(),
        ),
        timeout=3.0,
    )
    assert res_tool["count"] == 1
    ctx_html = (work_ctx.path / "out/ctx-card.html").read_text(encoding="utf-8")
    assert "9月30日" in ctx_html


def test_judge_cover_accepts_framed_photos_and_rejects_flat_logos():
    # 1. White mat frame (~73% border), dark mat frame (~73% border), B&W framed photo, letterbox bars (~72%)
    white_framed = _make_framed_photo_bytes(800, 600, border_ratio=0.24, border_color=(255, 255, 255))
    dark_framed = _make_framed_photo_bytes(800, 600, border_ratio=0.24, border_color=(14, 16, 22))
    bw_framed = _make_framed_photo_bytes(
        800, 600, border_ratio=0.24, border_color=(245, 245, 245), monochrome=True
    )

    letterbox = Image.new("RGB", (800, 600), (16, 16, 16))
    px = letterbox.load()
    assert px is not None
    for y in range(216, 384):
        for x in range(800):
            wave = int(16 * math.sin(x / 18.0))
            px[x, y] = (
                max(0, min(255, (x * 255) // 800 + wave)),
                max(0, min(255, ((y - 216) * 235) // 168)),
                max(0, min(255, ((800 - x) * 190) // 800)),
            )
    lb_buf = io.BytesIO()
    letterbox.save(lb_buf, format="JPEG", quality=90)
    letterbox_bytes = lb_buf.getvalue()

    for label, raw in (
        ("white_framed", white_framed),
        ("dark_framed", dark_framed),
        ("bw_framed", bw_framed),
        ("letterbox", letterbox_bytes),
    ):
        reason = digest_card.judge_cover(raw, url=f"https://cdn.example/{label}.jpg")
        assert reason == "", f"Framed real photo {label} should be accepted, got: {reason}"
        uri, _small = digest_card.encode_cover_data_uri(raw, url=f"https://cdn.example/{label}.jpg")
        assert uri.startswith("data:image/jpeg;base64,")

    # 2. Reject flat/simple logos & placeholders even with solid borders
    bordered_4color = Image.new("RGB", (640, 480), (255, 255, 255))
    d4 = ImageDraw.Draw(bordered_4color)
    d4.rectangle([160, 120, 320, 240], fill=(220, 40, 40))
    d4.rectangle([320, 120, 480, 240], fill=(40, 180, 70))
    d4.rectangle([160, 240, 320, 360], fill=(40, 90, 220))
    d4.rectangle([320, 240, 480, 360], fill=(240, 190, 30))
    b4 = io.BytesIO()
    bordered_4color.save(b4, format="PNG")
    assert digest_card.judge_cover(b4.getvalue())

    bordered_circle_icon = Image.new("RGB", (640, 480), (255, 255, 255))
    dc = ImageDraw.Draw(bordered_circle_icon)
    dc.ellipse([200, 120, 440, 360], fill=(16, 108, 220))
    dc.ellipse([260, 180, 380, 300], fill=(255, 255, 255))
    bc = io.BytesIO()
    bordered_circle_icon.save(bc, format="PNG")
    assert digest_card.judge_cover(bc.getvalue())

    bordered_flat_gray = Image.new("RGB", (640, 480), (255, 255, 255))
    ImageDraw.Draw(bordered_flat_gray).rectangle([140, 100, 500, 380], fill=(120, 125, 132))
    bf = io.BytesIO()
    bordered_flat_gray.save(bf, format="PNG")
    assert digest_card.judge_cover(bf.getvalue())

    bordered_text_banner = Image.new("RGB", (800, 600), (255, 255, 255))
    dtb = ImageDraw.Draw(bordered_text_banner)
    dtb.rectangle([160, 120, 640, 480], fill=(24, 34, 58))
    for r in range(180, 420, 24):
        for c in range(200, 600, 28):
            dtb.rectangle([c, r, c + 14, r + 10], fill=(255, 255, 255))
    btb = io.BytesIO()
    bordered_text_banner.save(btb, format="PNG")
    assert digest_card.judge_cover(btb.getvalue())


def test_build_card_html_over_512kb_preserves_recompressed_lead_cover():
    now_ts = 1_794_740_000.0
    # Part A: Multi-item >512KB even after initial 760/66 pass drops secondary covers FIRST
    # and keeps lead cover intact without deleting it.
    lead_bytes = _make_noisy_photo_bytes(760, 720, seed=101)
    sec1_bytes = _make_noisy_photo_bytes(760, 720, seed=102)
    sec2_bytes = _make_noisy_photo_bytes(760, 720, seed=103)
    lead_uri = f"data:image/jpeg;base64,{base64.b64encode(lead_bytes).decode('ascii')}"
    sec1_uri = f"data:image/jpeg;base64,{base64.b64encode(sec1_bytes).decode('ascii')}"
    sec2_uri = f"data:image/jpeg;base64,{base64.b64encode(sec2_bytes).decode('ascii')}"

    lead_recomp = digest_card._recompress_data_uri(lead_uri, max_width=760, quality=66)
    sec1_recomp = digest_card._recompress_data_uri(sec1_uri, max_width=760, quality=66)
    sec2_recomp = digest_card._recompress_data_uri(sec2_uri, max_width=760, quality=66)
    assert len(lead_recomp) + len(sec1_recomp) + len(sec2_recomp) > 512_000
    assert len(lead_recomp) < 420_000

    items_multi = [
        {
            "title": "头条重大开源架构发布",
            "url": "https://news.example/lead",
            "brief": "带头图的主条目。",
            "why": "头图应优先保留。",
            "source": "news.example",
            "published_ts": now_ts - 1200,
            "cover_data": lead_uri,
        },
        {
            "title": "次条报道 A",
            "url": "https://news.example/sec1",
            "brief": "次条 A 概要。",
            "why": "超限时可先降级配图。",
            "source": "news.example",
            "published_ts": now_ts - 2400,
            "cover_data": sec1_uri,
        },
        {
            "title": "次条报道 B",
            "url": "https://news.example/sec2",
            "brief": "次条 B 概要。",
            "why": "超限时可先降级配图。",
            "source": "news.example",
            "published_ts": now_ts - 3600,
            "cover_data": sec2_uri,
        },
    ]

    html_multi = digest_card.build_card_html(items_multi, now_ts=now_ts)
    assert len(html_multi.encode("utf-8")) <= 512_000
    assert '<article class="item lead has-cover">' in html_multi
    assert '<article class="item has-cover">' not in html_multi
    assert html_multi.count('<article class="item">') == 2
    assert "data:image/jpeg;base64," in html_multi

    # Part B: Single oversized lead cover (>512KB even after initial 760px/q66 pass) must progressively
    # downscale & recompress rather than dropping the lead cover completely!
    tall_lead_bytes = _make_noisy_photo_bytes(760, 1280, seed=303)
    tall_lead_uri = f"data:image/jpeg;base64,{base64.b64encode(tall_lead_bytes).decode('ascii')}"
    recomp_760 = digest_card._recompress_data_uri(tall_lead_uri, max_width=760, quality=66)
    assert len(recomp_760.encode("utf-8")) > 512_000

    items_single = [
        {
            "title": "超大头图单条目降级保留验证",
            "url": "https://news.example/single-huge",
            "brief": "即便单张头图初始体积超 512KB，也应逐步缩图降质保留头图而非直接删光。",
            "why": "头图视觉锚点重要。",
            "source": "news.example",
            "published_ts": now_ts - 600,
            "cover_data": tall_lead_uri,
        }
    ]
    html_single = digest_card.build_card_html(items_single, now_ts=now_ts)
    assert len(html_single.encode("utf-8")) <= 512_000
    assert '<article class="item lead has-cover">' in html_single
    assert "data:image/jpeg;base64," in html_single
