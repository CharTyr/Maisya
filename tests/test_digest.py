import hashlib
import hmac
import json
import time
from email.utils import formatdate

import pytest
from test_foundation import loaded

from maisya.abilities import Profiles
from maisya.digest import DigestWorkspace, fetch_public_bytes, parse_feed_xml


def _signed(secret: str, **event) -> str:
    body = json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_profile_enabled_toggle_and_runtime_enforcement(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    assert profiles.get("scout").enabled is True
    assert profiles.get("maker").enabled is True
    digest = profiles.get("digest")
    assert digest.enabled is False
    assert digest.autonomous is False
    assert digest.autonomous_config is True

    with pytest.raises(ValueError, match="预设角色不可删除"):
        profiles.delete("digest")

    bad_dir = profiles.path / "bad_toggle"
    bad_dir.mkdir()
    (bad_dir / "PROFILE.md").write_text("bad profile")
    (bad_dir / "profile.toml").write_text('title = "坏开关"\nenabled = "yes"\n')
    with pytest.raises(ValueError, match="布尔"):
        profiles.get("bad_toggle")

    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        # 1. Disabled by default: absent from before_planner, rejected at every execution entrypoint
        planner = await plugin.before_planner(
            session_id="chat",
            tool_definitions=[{"function": {"name": "maisya_agent"}}],
            items=[],
        )
        planner_text = "".join(
            part.get("text", "")
            for item in planner.get("modified_kwargs", {}).get("items", [])
            for part in item.get("parts", [])
        )
        assert "digest" not in planner_text
        assert "scout" in planner_text

        res_agent = await plugin.agent_tool(
            action="start",
            profile="digest",
            message="梳理今天热点",
            chat_id="chat",
            platform="qq",
        )
        assert res_agent["success"] is False
        assert "停用" in res_agent["error"]

        res_console = await plugin.console_send(
            profile="digest", chat_id="chat", message="梳理今天热点"
        )
        assert res_console["success"] is False
        assert "停用" in res_console["error"]

        res_sched = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": "chat",
                    "profile": "digest",
                    "workspace": "digest",
                    "in_seconds": 300,
                    "every_seconds": 86400,
                    "prompt": "每日资讯简报",
                }
            ),
        )
        assert res_sched["success"] is False
        assert "停用" in res_sched["error"]

        # 2. One-click toggle enables digest cleanly
        toggled = await plugin.manage_write(
            "toggle_profile", json.dumps({"target": "digest", "enabled": True})
        )
        assert toggled["success"] is True
        assert toggled["enabled"] is True
        assert plugin.profiles.get("digest").enabled is True
        assert plugin.profiles.get("digest").autonomous is True

        planner_on = await plugin.before_planner(
            session_id="chat",
            tool_definitions=[{"function": {"name": "maisya_agent"}}],
            items=[],
        )
        planner_on_text = "".join(
            part.get("text", "")
            for item in planner_on.get("modified_kwargs", {}).get("items", [])
            for part in item.get("parts", [])
        )
        assert "digest" in planner_on_text

        # Can create webhook + schedule when enabled, and running console thread works
        plugin.vault.set("digest_hook_key", "digest-webhook-secret-key-999")
        wh_cur = await plugin.manage_read("webhooks")
        wh_saved = await plugin.manage_write(
            "webhooks",
            json.dumps(
                {
                    "configuration": {
                        "digest_wh": {
                            "target": "chat:chat",
                            "kind": "proactive",
                            "profile": "digest",
                            "secret_handle": "digest_hook_key",
                        }
                    },
                    "expected_version": wh_cur["version"],
                }
            ),
        )
        assert wh_saved["success"] is True

        row = plugin.threads.create(
            plugin.profiles.get("digest"),
            "梳理今日资讯",
            chat_id="chat",
            platform="qq",
            user_id="",
        )
        sched_on = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": "chat",
                    "profile": "digest",
                    "workspace": "digest",
                    "in_seconds": 300,
                    "every_seconds": 86400,
                    "prompt": "每日资讯简报",
                }
            ),
        )
        assert sched_on["success"] is True

        # 3. Disabling later blocks existing thread send, webhook receive, and Pulse dispatch,
        # while webhooks manage_read still succeeds cleanly
        toggled_off = await plugin.manage_write(
            "toggle_profile", json.dumps({"target": "digest", "enabled": False})
        )
        assert toggled_off["success"] is True
        assert toggled_off["enabled"] is False

        wh_read_after = await plugin.manage_read("webhooks")
        assert wh_read_after["success"] is True

        send_blocked = await plugin.console_send(
            thread_id=row["thread_id"], message="继续跑一轮"
        )
        assert send_blocked["success"] is False
        assert "停用" in send_blocked["error"]

        agent_send_blocked = await plugin.agent_tool(
            action="send",
            thread_id=row["thread_id"],
            message="继续跑一轮",
            chat_id="chat",
            platform="qq",
        )
        assert agent_send_blocked["success"] is False

        ev = {
            "channel": "digest_wh",
            "event_id": "wh-disabled-1",
            "timestamp": int(time.time()),
            "payload": '{"reason":"触发简报"}',
        }
        ev_res = await plugin.incoming_event(**ev, signature=_signed("digest-webhook-secret-key-999", **ev))
        assert ev_res["success"] is False
        assert "停用" in ev_res["error"]

        intent_id = sched_on["intent"]["intent_id"]
        plugin.pulse.sleep_window = ()
        plugin.intents.clock = lambda: time.time() + 400
        due_rows = plugin.intents.due(plugin.pulse.enabled)
        due_row = next(r for r in due_rows if r["intent_id"] == intent_id)
        await plugin.pulse._dispatch(due_row)
        after_pulse = plugin.intents.get(intent_id)
        assert after_pulse["status"] == "failed"
        assert after_pulse["wake_status"] == "profile_disabled"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_rss_xml_xxe_ssrf_and_auto_trial_unsubscribe_state_machine(tmp_path):
    now = 1_794_480_000.0
    pub_recent = formatdate(now - 2 * 86400, usegmt=True)
    pub_old = formatdate(now - 30 * 86400, usegmt=True)

    rss_xml = f"""<?xml version="1.0" encoding="utf-8"?>
    <!-- <!DOCTYPE note [ignored comment]> -->
    <rss version="2.0"><channel>
      <title>Indie Craft Blog</title>
      <item>
        <title>New Shader Graph Trick</title>
        <link>https://indiecraft.example/posts/shader-trick</link>
        <pubDate>{pub_recent}</pubDate>
        <description>&lt;p&gt;Benchmark shows 40% frame time reduction.&lt;/p&gt;</description>
      </item>
      <item>
        <title>Old Archive Post</title>
        <link>https://indiecraft.example/posts/old-archive</link>
        <pubDate>{pub_old}</pubDate>
        <description>Should be filtered by lookback window.</description>
      </item>
    </channel></rss>"""
    parsed = parse_feed_xml(rss_xml, now=now, lookback_days=14)
    assert parsed["title"] == "Indie Craft Blog"
    assert len(parsed["items"]) == 1
    assert parsed["items"][0]["summary"] == "Benchmark shows 40% frame time reduction."

    atom_xml = f"""<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Atom Lab</title>
      <entry>
        <title>Compiler Pass Notes</title>
        <link href="https://atomlab.example/2026/compiler-pass" rel="alternate" />
        <updated>2026-11-12T08:00:00Z</updated>
        <summary>Measured 1.8x throughput gain on ARM64.</summary>
      </entry>
    </feed>"""
    atom_parsed = parse_feed_xml(atom_xml, now=now, lookback_days=14)
    assert atom_parsed["title"] == "Atom Lab"
    assert atom_parsed["items"][0]["url"] == "https://atomlab.example/2026/compiler-pass"

    xxe_xml = """<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss version="2.0"><channel><title>&xxe;</title></channel></rss>"""
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse_feed_xml(xxe_xml, now=now, lookback_days=14)

    for bad_url in (
        "file:///etc/passwd",
        "http://user:pass@example.org/feed.xml",
        "http://localhost/feed.xml",
        "http://127.0.0.1/feed.xml",
        "http://169.254.169.254/latest/meta-data",
    ):
        with pytest.raises(ValueError):
            await fetch_public_bytes(bad_url)

    # Workspace state machine test with mocked public fetcher
    plugin = await loaded(tmp_path / "rss_rt")

    fail_mode = {"slowfail": False}

    def _rss_payload(title: str, url_prefix: str, count: int) -> bytes:
        items = "\n".join(
            f"<item><title>{title} #{i}</title><link>{url_prefix}/p-{i}</link>"
            f"<pubDate>{pub_recent}</pubDate>"
            f"<description>Concrete measurement #{i} shows 25% latency drop.</description></item>"
            for i in range(count)
        )
        return f"<rss version='2.0'><channel><title>{title}</title>{items}</channel></rss>".encode()

    async def fake_fetcher(url: str):
        if url in ("https://goodsite.example/", "https://goodsite.example"):
            return {
                "body": (
                    "<html><head>"
                    "<link rel='alternate' type='application/rss+xml' title='Comments' href='/comments/feed/'>"
                    "<link rel='alternate' type='application/rss+xml' title='Main Feed' href='/feed.xml'>"
                    "</head><body>ok</body></html>"
                ).encode(),
                "content_type": "text/html",
                "status": 200,
                "url": "https://goodsite.example/",
                "error": "",
            }
        if url == "https://goodsite.example/feed.xml":
            return {
                "body": _rss_payload("GoodSite", "https://goodsite.example/articles", 4),
                "content_type": "application/xml",
                "status": 200,
                "url": url,
                "error": "",
            }
        if url == "https://alice.substack.com/feed":
            return {
                "body": _rss_payload("Alice Substack", "https://alice.substack.com/p", 1),
                "content_type": "application/xml",
                "status": 200,
                "url": url,
                "error": "",
            }
        if url == "https://gradsite.example/feed.xml":
            return {
                "body": _rss_payload("GradSite", "https://gradsite.example/post", 2),
                "content_type": "application/xml",
                "status": 200,
                "url": url,
                "error": "",
            }
        if url == "https://deadsite.example/feed.xml":
            if fail_mode["dead"]:
                return {"body": None, "content_type": "", "status": 503, "url": url, "error": "HTTP 503"}
            return {
                "body": _rss_payload("DeadSite", "https://deadsite.example/post", 1),
                "content_type": "application/xml",
                "status": 200,
                "url": url,
                "error": "",
            }
        if url == "https://trustedblog.example/" or url == "https://trustedblog.example":
            return {
                "body": "<link rel='alternate' type='application/rss+xml' href='/rss.xml'>".encode(),
                "content_type": "text/html",
                "status": 200,
                "url": "https://trustedblog.example/",
                "error": "",
            }
        if url == "https://trustedblog.example/rss.xml":
            return {
                "body": _rss_payload("TrustedBlog", "https://trustedblog.example/posts", 2),
                "content_type": "application/xml",
                "status": 200,
                "url": url,
                "error": "",
            }
        return {"body": None, "content_type": "", "status": 404, "url": url, "error": "HTTP 404"}

    fail_mode["dead"] = False
    clock_now = [now]
    try:
        row = plugin.threads.create(
            plugin.profiles.get("maker"),
            "RSS 状态机测试",
            chat_id="chat",
            platform="qq",
            user_id="",
            workspace="rss_sm",
        )
        work = await plugin.runtime._work(row)
        dw = DigestWorkspace(work, fetcher=fake_fetcher, clock=lambda: clock_now[0])

        # Big platform rejected while author newsletter (substack) allowed
        blocked = await dw.discover("https://github.com/trending")
        assert blocked["ok"] is False and "大平台" in blocked["reason"]

        sub_ok = await dw.discover("https://alice.substack.com/p/first-post")
        assert sub_ok["ok"] is True
        assert sub_ok["feed"]["origin"] == "map"
        assert sub_ok["feed"]["status"] == "trial"

        # Discover skips /comments/feed and picks /feed.xml
        disc = await dw.discover("https://goodsite.example/")
        assert disc["ok"] is True
        assert disc["feed"]["url"] == "https://goodsite.example/feed.xml"
        good_id = disc["feed"]["id"]

        # Trial cap <= 2 per round
        polled = await dw.poll()
        good_items = [it for it in polled["candidates"] if it["feed_id"] == good_id]
        assert len(good_items) == 2

        # Rule 1: >=10 candidates in 14d with 0 kept -> auto-unsubscribe
        for _ in range(4):
            await dw.poll()
        polled_retire = await dw.poll()
        assert any(u["feed_id"] == good_id and "候选" in u["reason"] for u in polled_retire["unsubscribed"])

        # Re-discover blocked because permanently rejected
        blocked_again = await dw.discover("https://goodsite.example/")
        assert blocked_again["ok"] is False

        # Rule 2 & 3: GradSite gets 1 kept item so it graduates after 14d; Alice Substack gets 0 kept so expires at 14d
        grad = await dw.add_feed("https://gradsite.example/feed.xml", title="GradSite", auto=True, origin="map")
        grad_id = grad["feed"]["id"]
        alice_id = sub_ok["feed"]["id"]
        await dw.poll()
        dw.record_selected_items(
            [
                {
                    "id": "it_grad_1",
                    "title": "GradSite #0",
                    "url": "https://gradsite.example/post/p-0",
                    "feed_id": grad_id,
                    "source": "gradsite.example",
                    "topic": "编译器",
                    "avg": 4.4,
                    "created": clock_now[0],
                }
            ]
        )
        clock_now[0] += 14.5 * 86400.0
        grad_poll = await dw.poll()
        assert any(u["feed_id"] == alice_id and "试用期" in u["reason"] for u in grad_poll["unsubscribed"])
        assert any(g["feed_id"] == grad_id for g in grad_poll["graduated"])
        grad_feed = next(f for f in dw.status()["feeds"] if f["id"] == grad_id)
        assert grad_feed["status"] == "active"
        assert grad_feed["trial_until"] == 0.0

        # Rule 4: net_useless >= 2 triggers unsubscribe
        fb1 = dw.feedback(item_id="it_grad_1", signal="down")
        fb2 = dw.feedback(item_id="it_grad_1", signal="down")
        assert fb2["net_useless"] == 2
        fb_poll = await dw.poll()
        assert any(u["feed_id"] == grad_id and "没用" in u["reason"] for u in fb_poll["unsubscribed"])

        # Rule 5: >= 5 consecutive fetch errors over >= 3 days -> unsubscribe with 30d cooldown
        dead = await dw.add_feed("https://deadsite.example/feed.xml", title="DeadSite", auto=True, origin="map")
        dead_id = dead["feed"]["id"]
        fail_mode["dead"] = True
        for i in range(5):
            await dw.poll()
            if i < 4:
                clock_now[0] += 0.9 * 86400.0
        dead_poll = await dw.poll()
        assert any(u["feed_id"] == dead_id and "连续抓取失败" in u["reason"] for u in dead_poll["unsubscribed"])
        cooldown_blocked = await dw.discover("https://deadsite.example/feed.xml")
        assert cooldown_blocked["ok"] is False

        # Advance 31 days to clear 30d cooldown, then record 4 recent non-RSS high-score items across >=3 days
        clock_now[0] += 31 * 86400.0
        hist_items = []
        base_day = clock_now[0]
        for day_idx in range(4):
            hist_items.append(
                {
                    "id": f"tr_{day_idx}",
                    "title": f"Trusted Post {day_idx}",
                    "url": f"https://trustedblog.example/posts/{day_idx}",
                    "feed_id": "",
                    "source": "trustedblog.example",
                    "topic": "系统优化",
                    "avg": 4.2,
                    "up": 1 if day_idx == 0 else 0,
                    "down": 0,
                    "reply": 0,
                    "created": base_day - day_idx * 86400.0,
                }
            )
        dw.record_selected_items(hist_items)
        trusted_poll = await dw.poll()
        assert any(s["origin"] == "trusted" and "trustedblog.example" in s["url"] for s in trusted_poll["subscribed"])
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_digest_hard_filter_five_dimension_scoring_and_card_render(tmp_path):
    now = 1_794_480_000.0
    plugin = await loaded(tmp_path)
    try:
        profile = plugin.profiles.get("digest")
        assert profile.enabled is False
        # Enable digest cleanly via Profiles TOML edit helper
        cur = plugin.memory.read("profile", target="digest", document="profile.toml")
        plugin.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )
        enabled_profile = plugin.profiles.get("digest")
        assert enabled_profile.enabled is True

        row = plugin.threads.create(
            enabled_profile,
            "执行资讯简报打分与渲染",
            chat_id="chat",
            platform="qq",
            user_id="",
            workspace="digest_test",
        )
        ctx = {**row, "profile_snapshot": row["profile_snapshot"]}
        work = await plugin.runtime._work(ctx)

        # Seed prior history: 1 prior URL/title for 14-day dedup & 3 items in "GPU驱动" within 3 days for saturation
        hist = {
            "items": [
                {
                    "id": "old_1",
                    "title": "Vulkan 1.4 规范正式发布",
                    "url": "https://khronos.org/news/vulkan-1-4",
                    "url_key": "khronos.org/news/vulkan-1-4",
                    "topic": "图形API",
                    "source": "khronos.org",
                    "created": now - 2 * 86400,
                },
                *[
                    {
                        "id": f"sat_{i}",
                        "title": f"GPU 驱动更新第 {i} 弹",
                        "url": f"https://gpu.example/{i}",
                        "url_key": f"gpu.example/{i}",
                        "topic": "GPU驱动",
                        "source": "gpu.example",
                        "created": now - (i + 1) * 3600,
                    }
                    for i in range(3)
                ],
            ],
            "blocked_domains": [],
        }
        work.write("project/digest_history.json", json.dumps(hist, ensure_ascii=False))
        work.commit()

        recent_iso = "2026-11-11T08:00:00+00:00"
        old_news_iso = "2026-10-20T08:00:00+00:00"
        old_guide_iso = "2026-08-01T08:00:00+00:00"

        candidates = [
            # 1. Valid high-score news
            {
                "title": "SQLite 3.51 发布并把 WAL 并发读延迟降了 30%",
                "url": "https://sqlite.org/releasenotes/3_51_0.html",
                "kind": "news",
                "published": recent_iso,
                "source_text": "SQLite 3.51.0 reduces WAL read lock contention by 30 percent on multi-core hosts.",
                "quote": "reduces WAL read lock contention by 30 percent",
                "summary": "SQLite 3.51.0 降低多线程读 WAL 锁竞争 30%。",
                "why": "内核存储引擎实打实的性能提升。",
                "topic": "数据库",
                "profile_hit": "本地数据库性能",
                "scores": {"info": 5, "source": 5, "relevance": 5, "timeliness": 5, "chat": 4, "novelty": 5, "surprise": 4},
            },
            # 2. Explore bridge item (rel=2, info>=4, surprise>=4, timeliness>=3, bridge present)
            {
                "title": "掌机散热铜管改成均热板后的噪音对比实测",
                "url": "https://thermals.example/handheld-vapor-chamber",
                "kind": "news",
                "published": recent_iso,
                "source_text": "Switching from copper heatpipes to a thin vapor chamber dropped fan speed by 900 RPM.",
                "quote": "dropped fan speed by 900 RPM",
                "summary": "实测改用均热板后掌机风扇转速下降 900 RPM。",
                "bridge": "群里前天讨论掌机高温降频，这条拆解背后的均热板风噪收益",
                "why": "解释了掌机散热差异的物理原因。",
                "topic": "硬件散热",
                "scores": {"info": 4, "source": 4, "relevance": 2, "timeliness": 4, "chat": 4, "novelty": 4, "surprise": 5},
            },
            # 3. Valid guide #1
            {
                "title": "用 eBPF 定位容器尾延迟抖动的实战排查笔记",
                "url": "https://ops.example/ebpf-tail-latency-guide",
                "kind": "guide",
                "published": recent_iso,
                "source_text": "Using runqlat histograms reveals scheduler throttling inside cgroup v2 quotas.",
                "quote": "runqlat histograms reveals scheduler throttling",
                "summary": "用 runqlat 直方图排查 cgroup v2 配额调度抖动。",
                "topic": "系统排障",
                "profile_hit": "容器性能调优",
                "scores": {"info": 5, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4, "novelty": 4, "surprise": 4},
            },
            # 4. Guide #2 (should be cut by guide<=1 cap)
            {
                "title": "PostgreSQL 18 索引膨胀诊断完整手册",
                "url": "https://pg.example/bloat-guide",
                "kind": "guide",
                "published": recent_iso,
                "source_text": "pgstatindex helps quantify leaf fragmentation before reindex.",
                "quote": "pgstatindex helps quantify leaf fragmentation",
                "summary": "利用 pgstatindex 量化索引页碎片后再决定重建。",
                "topic": "数据库教程",
                "profile_hit": "本地数据库性能",
                "scores": {"info": 4, "source": 4, "relevance": 4, "timeliness": 4, "chat": 3, "novelty": 4, "surprise": 3},
            },
            # 5. Reject: news > 7 days
            {
                "title": "两周前的旧发布",
                "url": "https://old.example/news",
                "kind": "news",
                "published": old_news_iso,
                "source_text": "Old news body quote.",
                "quote": "Old news body quote",
                "summary": "旧闻测试。",
                "profile_hit": "测试",
                "scores": {"info": 5, "source": 5, "relevance": 5, "timeliness": 5, "chat": 5, "novelty": 5, "surprise": 5},
            },
            # 6. Reject: guide missing published date
            {
                "title": "没有日期的教程",
                "url": "https://nodate.example/guide",
                "kind": "guide",
                "source_text": "Undated guide text.",
                "quote": "Undated guide text",
                "summary": "无日期文章。",
                "profile_hit": "测试",
                "scores": {"info": 5, "source": 5, "relevance": 5, "timeliness": 5, "chat": 5, "novelty": 5, "surprise": 5},
            },
            # 7. Reject: guide > 60 days
            {
                "title": "四个月前的老教程",
                "url": "https://stale.example/guide",
                "kind": "guide",
                "published": old_guide_iso,
                "source_text": "Old guide text.",
                "quote": "Old guide text",
                "summary": "过时教程。",
                "profile_hit": "测试",
                "scores": {"info": 5, "source": 5, "relevance": 5, "timeliness": 5, "chat": 5, "novelty": 5, "surprise": 5},
            },
            # 8. Reject: quote not grounded in source_text
            {
                "title": "编造引用的新闻",
                "url": "https://fakequote.example/news",
                "kind": "news",
                "published": recent_iso,
                "source_text": "Actual page mentions something completely different.",
                "quote": "99% speed improvement guaranteed",
                "summary": "引用对不上正文。",
                "profile_hit": "测试",
                "scores": {"info": 5, "source": 5, "relevance": 5, "timeliness": 5, "chat": 5, "novelty": 5, "surprise": 5},
            },
            # 9. Reject: gov site without relevance >= 4 & profile_hit
            {
                "title": "某地检察院发布中秋微信表情包",
                "url": "https://news.jcy.gov.cn/sticker-pack",
                "kind": "news",
                "published": recent_iso,
                "source_text": "中秋普法表情包正式上线。",
                "quote": "中秋普法表情包正式上线",
                "summary": "政务宣传表情包。",
                "scores": {"info": 3, "source": 5, "relevance": 3, "timeliness": 5, "chat": 3, "novelty": 4, "surprise": 3},
            },
            # 10. Reject: topic saturation ("GPU驱动" already has 3 items in 3 days)
            {
                "title": "GPU 驱动新增小修补",
                "url": "https://gpu.example/4",
                "kind": "news",
                "published": recent_iso,
                "source_text": "Minor driver hotfix addresses shader stutter.",
                "quote": "Minor driver hotfix",
                "summary": "驱动小修补。",
                "topic": "GPU驱动",
                "profile_hit": "显卡折腾",
                "scores": {"info": 4, "source": 4, "relevance": 4, "timeliness": 4, "chat": 4, "novelty": 4, "surprise": 3},
            },
            # 11. Reject: novelty <= 2
            {
                "title": "群友刚才已经聊完的发布会",
                "url": "https://known.example/event",
                "kind": "news",
                "published": recent_iso,
                "source_text": "Event livestream recap.",
                "quote": "Event livestream recap",
                "summary": "已经知道的事。",
                "topic": "发布会",
                "profile_hit": "发布会",
                "scores": {"info": 5, "source": 5, "relevance": 5, "timeliness": 5, "chat": 5, "novelty": 2, "surprise": 2},
            },
            # 12. Reject: 14-day duplicate URL
            {
                "title": "Vulkan 1.4 重发报道",
                "url": "https://khronos.org/news/vulkan-1-4",
                "kind": "news",
                "published": recent_iso,
                "source_text": "Khronos releases Vulkan 1.4 specification.",
                "quote": "Vulkan 1.4 specification",
                "summary": "重复链接。",
                "topic": "图形API",
                "profile_hit": "图形API",
                "scores": {"info": 5, "source": 5, "relevance": 5, "timeliness": 5, "chat": 5, "novelty": 5, "surprise": 4},
            },
            # 13. Reject: same-story in batch (lower avg than candidate #1)
            {
                "title": "SQLite 3.51 发布 WAL 并发读延迟降 30%",
                "url": "https://mirror.example/sqlite-351",
                "kind": "news",
                "published": recent_iso,
                "source_text": "SQLite 3.51.0 reduces WAL read lock contention by 30 percent.",
                "quote": "WAL read lock contention",
                "summary": "同一件事的二手转载。",
                "topic": "数据库",
                "profile_hit": "本地数据库性能",
                "scores": {"info": 3, "source": 3, "relevance": 5, "timeliness": 5, "chat": 3, "novelty": 4, "surprise": 2},
            },
        ]

        res = await plugin.runtime.call(
            "digest_feed",
            {"action": "score_select", "candidates": candidates, "max_items": 5},
            ctx,
        )
        kept_urls = {it["url"] for it in res["selected"]}
        assert kept_urls == {
            "https://sqlite.org/releasenotes/3_51_0.html",
            "https://thermals.example/handheld-vapor-chamber",
            "https://ops.example/ebpf-tail-latency-guide",
        }
        rej_by_url = {it["url"]: it["reject_reason"] for it in res["rejected"]}
        assert "7 天" in rej_by_url["https://old.example/news"]
        assert "发布时间" in rej_by_url["https://nodate.example/guide"]
        assert "60 天" in rej_by_url["https://stale.example/guide"]
        assert "原文" in rej_by_url["https://fakequote.example/news"]
        assert "政务" in rej_by_url["https://news.jcy.gov.cn/sticker-pack"]
        assert "饱和" in rej_by_url["https://gpu.example/4"]
        assert "新鲜感" in rej_by_url["https://known.example/event"]
        assert "重复" in rej_by_url["https://khronos.org/news/vulkan-1-4"]
        assert "同一件事" in rej_by_url["https://mirror.example/sqlite-351"]
        assert "上限" in rej_by_url["https://pg.example/bloat-guide"]

        # Verify persistent history updated & offline time-of-day card HTML renders safely into out/
        stored_hist = json.loads(work.read("project/digest_history.json")["text"])
        assert any(it["url"] == "https://sqlite.org/releasenotes/3_51_0.html" for it in stored_hist["items"])

        card_res = await plugin.runtime.call(
            "digest_feed",
            {
                "action": "render_card_html",
                "candidates": res["selected"],
                "slot_label": "11月13日 · 下午",
                "group_title": "探索实验室",
                "output_path": "out/digest-card.html",
            },
            ctx,
        )
        assert card_res["path"] == "out/digest-card.html"
        html_doc = work.read("out/digest-card.html")["text"]
        assert "11月13日 · 下午" in html_doc
        assert "SQLite 3.51" in html_doc
        assert "<script" not in html_doc.lower()
        assert "http://" not in html_doc.split("<!--")[0]  # no external CSS/font links
    finally:
        await plugin.on_unload()
