"""TDD tests for Plan A fast two-stage natural replies and tool-result amnesia elimination."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from test_foundation import loaded

import plugin as plugin_mod
from maisya.abilities import Profile
from maisya.digest import DigestWorkspace
from maisya.shaping import execute_shape
from maisya.threads import ThreadStore, encode


class _FakeConfig:
    def __init__(self, permissions: list[str] | None = None):
        self._data = {"plugin.permission": list(permissions or [])}

    async def get(self, key: str, default=None):
        return self._data.get(key, default)


class _FakeCtx:
    def __init__(self, data_dir, permissions: list[str] | None = None):
        self.paths = SimpleNamespace(data_dir=data_dir)
        self.config = _FakeConfig(permissions)
        self.logger = logging.getLogger("test.maisya.plan_a")
        self.streams: list[dict] = [
            {
                "stream_id": "10001",
                "platform": "qq",
                "group_id": "10001",
                "group_name": "独立游戏交流群",
                "chat_type": "group",
            }
        ]

    async def call_capability(self, name: str, **kwargs):
        if name == "chat.get_all_streams":
            return {"success": True, "streams": list(self.streams)}
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "已经按需求调好，后台正按新口味跑简报。"}
        return {"success": True, "response": "ok", "tool_calls": []}


async def _boot_plugin(
    tmp_path,
    *,
    permissions: list[str] = ("qq:Alice",),
    enabled_chats: list[str] = ("qq:group:10001",),
):
    data_dir = tmp_path / "data" / "plugins" / "maisya"
    data_dir.mkdir(parents=True, exist_ok=True)
    plg = plugin_mod.MaisyaPlugin()
    plg._ctx = _FakeCtx(data_dir, permissions=list(permissions))
    cfg = plg.build_default_config()
    cfg["plugin"]["enabled"] = True
    cfg["enabled_chats"] = list(enabled_chats)
    plg.set_plugin_config(cfg)
    await plg.on_load()
    return plg


def _private_msg(
    message_id: str,
    text: str,
    *,
    session_id: str = "priv_admin_md5",
    user_id: str = "Alice",
    platform: str = "qq",
):
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": str(time.time()),
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": None,
            "additional_config": {"relayed_by_plugin": ""},
        },
    }


@pytest.mark.parametrize("max_chars", [8000, 48000])
def test_compact_never_drops_latest_oversized_tool_block(tmp_path, max_chars):
    store = ThreadStore(tmp_path / "threads.db")
    row = store.create(
        Profile(),
        "collect indie game feeds",
        chat_id="chat-a",
        platform="qq",
        user_id="Alice",
    )
    revision = store.enqueue(row["thread_id"], "collect indie game feeds", "chat-a")
    huge_payload = "<external_data>" + ("factual_item " * 8000) + "</external_data>"
    history = [
        {"role": "user", "content": "collect indie game feeds"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "digest_feed", "arguments": '{"action":"collect"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": huge_payload,
        },
    ]
    store.checkpoint(row["thread_id"], history, revision, "tools", {"ok": True})
    summary_raw = store.compact(row["thread_id"], "chat-a", max_chars=max_chars)
    compacted = store.get(row["thread_id"], "chat-a")

    assert len(encode(compacted["history"])) <= max_chars
    assert len(compacted["history"]) >= 2
    assert compacted["history"][-2]["role"] == "assistant"
    assert compacted["history"][-2]["tool_calls"][0]["id"] == "call_1"
    assert compacted["history"][-1]["role"] == "tool"
    assert compacted["history"][-1]["tool_call_id"] == "call_1"
    tool_content = compacted["history"][-1]["content"]
    assert tool_content.startswith("<external_data>")
    assert tool_content.endswith("</external_data>")
    assert "factual_item" in tool_content
    assert "event_seq=" in tool_content

    archive_seq = int(json.loads(summary_raw)["archive_event"])
    archived = store.read_history(
        row["thread_id"], "chat-a", event_seq=archive_seq, offset=80000
    )
    assert archived
    assert "factual_item" in archived[0]["text"]


@pytest.mark.asyncio
async def test_digest_collect_bounds_payload_and_concurrent_add_feed_no_clobber(tmp_path):
    plugin = await loaded(tmp_path / "digest_bounds")
    try:
        cur = plugin.memory.read("profile", target="digest", document="profile.toml")
        plugin.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )
        row = plugin.threads.create(
            plugin.profiles.get("digest"),
            "bounds and add_feed test",
            chat_id="chat-bounds",
            platform="qq",
            user_id="",
            workspace="digest",
        )
        work = await plugin.runtime._work(row)
        recent_rfc = formatdate(time.time() - 3600, usegmt=True)

        async def _slow_rss_fetcher(url: str):
            await asyncio.sleep(0.02)
            xml = (
                f"<rss version='2.0'><channel><title>Feed {url}</title>"
                f"<item><title>Post {url}</title><link>{url}/post-1</link>"
                f"<pubDate>{recent_rfc}</pubDate><description>Release notes</description></item>"
                "</channel></rss>"
            ).encode()
            return {"body": xml, "content_type": "application/xml", "status": 200, "url": url, "error": ""}

        dw = DigestWorkspace(work, fetcher=_slow_rss_fetcher)

        # Concurrent add_feed must never clobber sibling feeds
        add_results = await asyncio.gather(
            *(dw.add_feed(f"https://feed{i}.example.com/rss.xml", auto=False) for i in range(6))
        )
        assert all(r.get("ok") for r in add_results)
        sources_after = dw._load_sources()
        assert len(sources_after["feeds"]) == 6

        # Oversized search/fetch results must stay compact in collect() return payload
        class _FakeWeb:
            async def search_broad(self, q, max_results=8, context=None):
                return {
                    "results": [
                        {
                            "url": f"https://game{i}.example.org/news/launch-{i}",
                            "title": f"Indie Game Devlog #{i}",
                            "snippet": "Long snippet " * 80,
                            "summary": "Long summary " * 80,
                            "published_at": recent_rfc,
                        }
                        for i in range(24)
                    ]
                }

            async def fetch(self, url, context=None):
                return {
                    "url": url,
                    "title": f"Full Article {url}",
                    "content": f"Steam peak players reached 42000 with 94% positive reviews for {url}. " + ("Details line. " * 1400),
                    "published_at": recent_rfc,
                    "image_url": f"{url}/cover.jpg",
                }

        collect_res = await dw.collect(web=_FakeWeb(), queries=["indie game"], max_items=20)
        encoded = json.dumps(collect_res, ensure_ascii=False)
        assert len(encoded) < 26000
        assert len(collect_res["candidates"]) <= 14
        for cand in collect_res["candidates"]:
            assert len(str(cand.get("source_text") or "")) <= 1200
            if cand.get("origin") == "search":
                assert cand.get("has_numbers") is True
            assert "summary" not in cand and "snippet" not in cand
        pool = dw._load_pool()
        assert len(pool.get("by_url") or {}) >= 20
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_setup_trial_submits_bridge_route_and_emits_plan_a_reply_hint(tmp_path):
    plg = await _boot_plugin(tmp_path, enabled_chats=["qq:group:10001"])
    try:
        await plg.after_receive(message=_private_msg("m-setup-1", "帮我试跑独立游戏资讯"))

        async def _fake_add_feed(self, url, *, title="", auto=False, origin="admin"):
            await asyncio.sleep(0.08)
            sources = self._load_sources()
            sources["feeds"].append({"id": url[-8:], "url": url, "title": url, "status": "active"})
            self._save_sources(sources)
            self.work.commit()
            return {"ok": True, "url": url}

        orig_add = DigestWorkspace.add_feed
        DigestWorkspace.add_feed = _fake_add_feed
        try:
            # First test concurrent handle_digest_sources speed
            t0 = time.monotonic()
            src_res = await execute_shape(
                plg,
                action="patch",
                kind="digest_sources",
                target_chat="qq:group:10001",
                current_chat_id="priv_admin_md5",
                current_platform="qq",
                source_msg_id="m-setup-1",
                patch={
                    "rss_urls": [f"https://rss{i}.example.org/feed.xml" for i in range(6)]
                },
            )
            elapsed_sources = time.monotonic() - t0
            assert len(src_res["added"]) == 6
            assert elapsed_sources < 0.35

            async def _fake_start(*args, **kwargs):
                return {"thread_id": "th_demo1", "state": "active", "result": {}}

            plg.engine.start = _fake_start
            setup_res = await execute_shape(
                plg,
                action="setup",
                kind="setup",
                target_chat="qq:group:10001",
                current_chat_id="priv_admin_md5",
                current_platform="qq",
                source_msg_id="m-setup-1",
                patch={
                    "enable_chat": True,
                    "enable_profiles": ["digest"],
                    "auto_seed_sources": False,
                    "run_now": True,
                    "deliver_to": "current",
                },
            )
            assert setup_res["trial_thread_id"] == "th_demo1"
            assert "next_step" in setup_res
            assert "reply" in setup_res["next_step"]
            assert "wait" in setup_res["next_step"]
            assert "maisya_recall" in setup_res["next_step"]

            with plg.threads.connect() as db:
                rem = db.execute("SELECT remaining FROM return_groups ORDER BY rowid DESC LIMIT 1").fetchone()
            assert rem is not None
            assert rem["remaining"] == 0
        finally:
            DigestWorkspace.add_feed = orig_add
    finally:
        await plg.on_unload()


@pytest.mark.asyncio
async def test_agent_and_recall_anti_polling_and_digest_fast_wait(tmp_path):
    plg = await _boot_plugin(tmp_path, enabled_chats=["qq:group:10001"])
    try:
        # Enable digest profile first
        cur = plg.memory.read("profile", target="digest", document="profile.toml")
        plg.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )
        await plg.after_receive(message=_private_msg("m-agent-1", "重跑独立游戏简报"))

        gate = asyncio.Event()
        real_rt = plg.engine.runtime

        class _SlowRuntime:
            def __init__(self, inner_rt):
                self._real = inner_rt

            def available(self, profile):
                return self._real.available(profile)

            async def step(self, profile, history):
                await gate.wait()
                return {"success": True, "response": "done", "tool_calls": []}

            async def call(self, name, arguments, context):
                return await self._real.call(name, arguments, context)

            def result(self, thread_id, result):
                return result

            async def finished(self, row):
                return None

        plg.engine.runtime = _SlowRuntime(real_rt)
        t0 = time.monotonic()
        res = await plg.agent_tool(
            action="start",
            profile="digest",
            source_msg_id="m-agent-1",
            target_chat="qq:group:10001",
            deliver_to="current",
            message="按独立游戏口味试跑一期",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        elapsed = time.monotonic() - t0
        assert res["success"] is True, res
        assert res["state"] == "active"
        assert elapsed < 3.0
        assert "next_step" in res
        assert "reply" in res["next_step"]
        assert "maisya_recall" in res["next_step"]

        # Calling recall_tool while active thread exists emits anti-polling active_notice
        rec = await plg.recall_tool(
            query="",
            target_chat="qq:group:10001",
            source_msg_id="m-agent-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert rec["success"] is True
        assert "active_notice" in rec
        assert "reply" in rec["active_notice"]
        content_obj = json.loads(rec["content"])
        assert content_obj["active_notice"] == rec["active_notice"]

        gate.set()
        await plg.engine.drain()

        # Fast-completing digest run within 2s wait still completes inline as idle
        fast_res = await plg.agent_tool(
            action="start",
            profile="digest",
            source_msg_id="m-agent-1",
            target_chat="qq:group:10001",
            deliver_to="current",
            message="快速完成验证",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert fast_res["success"] is True, fast_res
        assert fast_res["state"] == "idle"
    finally:
        await plg.on_unload()


@pytest.mark.asyncio
async def test_share_reply_block_clears_on_end_turn_return_wake_and_admin_bootstrap(tmp_path):
    plg = await _boot_plugin(tmp_path, enabled_chats=["qq:group:10001"])
    try:
        cur = plg.memory.read("profile", target="digest", document="profile.toml")
        plg.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )
        await plg.after_receive(message=_private_msg("m-blk-0", "跑个预览"))
        started = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="qq:group:10001",
            deliver_to="current",
            message="先给一份链接预览 https://example.org/preview",
            wait_seconds=1,
            source_msg_id="m-blk-0",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert started["success"] is True
        tid = started["thread_id"]
        row = plg.threads.get(tid, "10001")
        if row["state"] != "idle":
            plg.threads.finish(
                tid,
                "10001",
                {"text": "预览结果 https://example.org/preview", "attachments": []},
                [],
                row["revision"],
            )

        # Simulate Share.send consuming delivery and blocking duplicate same-turn reply
        plg.bridge.claim_delivery({**row, "delivery_chat_id": "priv_admin_md5"}, "share")
        plg.bridge.block_reply("priv_admin_md5")
        dup = await plg.before_replyer(
            session_id="priv_admin_md5", request_type="maisaka.replyer"
        )
        assert dup["action"] == "abort"

        # 1. end_turn clears _blocked
        await plg.bridge.end_turn("priv_admin_md5")
        assert "priv_admin_md5" not in plg.bridge._blocked

        # 2. Relayed echo does NOT clear _blocked, while real admin_bootstrap input DOES clear _blocked
        plg.bridge.block_reply("priv_admin_md5")
        relayed = _private_msg("m-blk-relayed", "刚才的预览汇报")
        relayed["message_info"]["additional_config"]["relayed_by_plugin"] = "chartyr.maisya"
        await plg.after_receive(message=relayed)
        assert "priv_admin_md5" in plg.bridge._blocked

        await plg.after_receive(message=_private_msg("m-unlock-1", "不要，别搞了"))
        assert "priv_admin_md5" not in plg.bridge._blocked
        resumed = await plg.before_replyer(
            session_id="priv_admin_md5", request_type="maisaka.replyer"
        )
        assert resumed["action"] == "continue"

        # 3. Pulse._returns waking an admin_direct session clears _blocked even with no user reply
        started2 = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="qq:group:10001",
            deliver_to="current",
            message="再跑一次预览",
            wait_seconds=0,
            source_msg_id="m-unlock-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert started2["success"] is True
        await plg.engine.drain()
        plg.bridge.clock = lambda now=time.time() + 100.0: now
        plg.bridge.block_reply("priv_admin_md5")

        async def _wake_cap(name, **kwargs):
            if name == "maisaka.proactive.trigger":
                return {"success": True, "queued": True}
            return {"success": True, "response": "ok", "tool_calls": []}

        plg.ctx.call_capability = _wake_cap
        await plg.pulse._returns()
        assert "priv_admin_md5" not in plg.bridge._blocked
    finally:
        await plg.on_unload()


@pytest.mark.asyncio
async def test_active_admin_direct_thread_keeps_3600s_continuation_until_consumed(tmp_path):
    plg = await _boot_plugin(tmp_path, enabled_chats=["qq:group:10001"])
    try:
        clock = [1_700_000_000.0]
        plg._message_now = lambda: clock[0]
        plg.messages.clock = lambda: clock[0]
        plg.bridge.clock = lambda: clock[0]
        plg.intents.clock = lambda: clock[0]

        cur = plg.memory.read("profile", target="digest", document="profile.toml")
        plg.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )

        gate = asyncio.Event()
        real_rt = plg.engine.runtime

        class _GateRuntime:
            def available(self, profile):
                return real_rt.available(profile)

            async def step(self, profile, history):
                await gate.wait()
                return {"success": True, "response": "完成独立游戏简报", "tool_calls": []}

            async def call(self, name, arguments, context):
                return await real_rt.call(name, arguments, context)

            def result(self, thread_id, result):
                return result

            async def finished(self, row):
                return None

        plg.engine.runtime = _GateRuntime()

        await plg.after_receive(message=_private_msg("m-adm-0", "按独立游戏口味试跑一期"))
        started = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="qq:group:10001",
            deliver_to="current",
            message="跑第一轮预览",
            wait_seconds=0,
            source_msg_id="m-adm-0",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert started["success"] is True, started
        tid = started["thread_id"]
        gate.set()
        await plg.engine.drain()
        row1 = plg.threads.get(tid, "10001")
        if row1["state"] != "idle":
            plg.threads.finish(tid, "10001", {"text": "rev1 timeout", "attachments": []}, [], row1["revision"])
        assert plg.bridge.has_admin_direct_for("priv_admin_md5") is True

        # Advance 563s (> 300s, < 3600s) and follow up with send while rev=2 stays active
        gate.clear()
        clock[0] += 563.0
        follow = await plg.agent_tool(
            action="send",
            thread_id=tid,
            message="直接用缓存重跑",
            wait_seconds=0,
            source_msg_id="m-adm-0",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert follow["success"] is True, follow
        assert plg.threads.get(tid, "10001")["state"] == "active"

        # Advance another 55s (total 618s > 300s) while rev=2 is STILL active
        clock[0] += 55.0
        assert plg.bridge.has_admin_direct_for("priv_admin_md5") is True
        assert plg._admin_source_max_age("priv_admin_md5", "qq") == 3600.0

        tools = [
            {"type": "function", "function": {"name": name}}
            for name in ["reply", "maisya_agent", "maisya_recall", "maisya_shape", "maisya_later"]
        ]
        plg._turn_sources.pop("priv_admin_md5", None)
        plg._pending_sources.pop("priv_admin_md5", None)
        hook_res = await plg.before_planner(
            session_id="priv_admin_md5", items=[], tool_definitions=tools
        )
        kept_names = {
            (t.get("function") or t).get("name")
            for t in hook_res["modified_kwargs"]["tool_definitions"]
        }
        assert "maisya_agent" in kept_names
        assert "maisya_recall" in kept_names
        assert "items" not in hook_res["modified_kwargs"]

        rec = await plg.recall_tool(
            query="digest",
            target_chat="qq:group:10001",
            source_msg_id="m-adm-0",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert rec["success"] is True, rec

        shp = await plg.shape_tool(
            action="read",
            kind="memory",
            target_chat="qq:group:10001",
            source_msg_id="m-adm-0",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert shp["success"] is True, shp

        # Finish rev=2 and consume its return delivery -> continuation window closes back to 300s
        gate.set()
        await plg.engine.drain()
        row2 = plg.threads.get(tid, "10001")
        if row2["state"] != "idle":
            plg.threads.finish(tid, "10001", {"text": "rev2 done", "attachments": []}, [], row2["revision"])
        plg.bridge.claim_delivery({**row2, "delivery_chat_id": "priv_admin_md5"}, "share")
        assert plg.bridge.has_admin_direct_for("priv_admin_md5") is False

        stale_start = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="qq:group:10001",
            deliver_to="current",
            message="消费后再复用旧消息开新线程",
            wait_seconds=0,
            source_msg_id="m-adm-0",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert stale_start["success"] is False
    finally:
        await plg.on_unload()


@pytest.mark.asyncio
async def test_pulse_admin_direct_preview_intent_runs_on_group_workspace_and_wakes_only_private_chat(tmp_path):
    plg = await _boot_plugin(tmp_path, enabled_chats=["qq:group:10001"])
    try:
        cur = plg.memory.read("profile", target="digest", document="profile.toml")
        plg.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )
        clock = [1_700_000_000.0]
        plg.bridge.clock = lambda: clock[0]
        plg.intents.clock = lambda: clock[0]

        cap_calls = []

        async def _cap(name, **kwargs):
            cap_calls.append((name, kwargs))
            if name == "maisaka.proactive.trigger":
                return {"success": True, "queued": True}
            return {"success": True, "response": "预览结果已生成", "tool_calls": []}

        plg.ctx.call_capability = _cap

        plg.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:priv_admin_md5",
            due_at=clock[0] - 1.0,
            reason="独立游戏简报试跑预览",
            payload={
                "platform": "qq",
                "profile": "digest",
                "workspace": "digest",
                "thread_chat_id": "10001",
                "scheduled_admin": True,
                "admin_direct": True,
                "source_msg_id": "admin-preview",
                "prompt": "试跑简报",
            },
            bypass_limits=True,
        )

        await plg.pulse.tick()
        await plg.engine.drain()
        clock[0] += 2.0
        await plg.pulse.tick()

        with plg.threads.connect() as db:
            t_rows = db.execute("SELECT thread_id, chat_id, profile FROM agent_threads WHERE profile='digest'").fetchall()
        assert len(t_rows) == 1
        assert t_rows[0]["chat_id"] == "10001"

        wakes = [kw for name, kw in cap_calls if name == "maisaka.proactive.trigger"]
        assert len(wakes) == 1
        assert wakes[0]["stream_id"] == "priv_admin_md5"
        assert wakes[0]["reason"] == "maisya:return"
        assert wakes[0]["intent"].startswith("之前由本会话真实用户消息触发的 Agent 线程已有结果")
        assert all(kw["stream_id"] != "10001" for kw in wakes)
    finally:
        await plg.on_unload()


@pytest.mark.asyncio
async def test_digest_collect_preserves_search_quota_when_aggregator_rss_floods(tmp_path):
    plg = await _boot_plugin(tmp_path / "collect_quota", enabled_chats=["qq:group:10001"])
    try:
        cur = plg.memory.read("profile", target="digest", document="profile.toml")
        plg.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )
        row = plg.threads.create(
            plg.profiles.get("digest"),
            "collect 配额均衡测试",
            chat_id="10001",
            platform="qq",
            user_id="",
            workspace="digest",
        )
        work = await plg.runtime._work(row)
        recent_rfc = formatdate(time.time() - 3600, usegmt=True)
        recent_iso = "2026-11-15T08:00:00Z"

        hn_titles = [
            "Postgres Vacuum Memory Overhead Measurement",
            "BGP Route Flap Dampening Study",
            "Zig Allocator Arena Fragmentation Report",
            "Wayland Color Management Protocol Update",
            "SQLite WAL Checkpoint Latency Analysis",
            "FreeBSD ZFS Snapshot Replication Benchmarks",
            "WebGPU Shader Compilation Pipeline Overview",
            "QUIC Congestion Window Recovery Notes",
        ]
        blog_titles = [
            "Rust Macro Hygiene Edge Cases",
            "LLVM Register Allocator Greedy Passes",
            "Font Subpixel Hinting On HiDPI Displays",
            "DNS Over HTTPS Recursive Resolver Metrics",
            "NVMe Namespace Queue Depth Tuning",
            "SIMD UTF8 Validation Lookup Tables",
        ]

        async def _aggregator_fetcher(url: str):
            if "hnrss.org" in url:
                items = "\n".join(
                    f"<item><title>{title}</title>"
                    f"<link>https://tech{i}.example/releases/patch-{i}</link>"
                    f"<pubDate>{recent_rfc}</pubDate>"
                    f"<description>Technical measurements and trace findings #{i}.</description></item>"
                    for i, title in enumerate(hn_titles)
                )
                return {
                    "body": f"<rss version='2.0'><channel><title>HN</title>{items}</channel></rss>".encode(),
                    "content_type": "application/xml",
                    "status": 200,
                    "url": url,
                    "error": "",
                }
            items = "\n".join(
                f"<item><title>{title}</title>"
                f"<link>https://blog{i}.example/notes/bench-{i}</link>"
                f"<pubDate>{recent_rfc}</pubDate>"
                f"<description>Engineering notes and profiler numbers #{i}.</description></item>"
                for i, title in enumerate(blog_titles)
            )
            return {
                "body": f"<rss version='2.0'><channel><title>AggBlog</title>{items}</channel></rss>".encode(),
                "content_type": "application/xml",
                "status": 200,
                "url": url,
                "error": "",
            }

        dw = DigestWorkspace(work, fetcher=_aggregator_fetcher)
        await dw.add_feed("https://hnrss.org/newest?points=100", title="HN", auto=False)
        await dw.add_feed("https://aggblog.example/feed.xml", title="AggBlog", auto=False)

        game_titles = [
            "After the Wane Rotoscoped Noir Adventure Preview",
            "Funeral for the Sun Nonlinear Detective Casebook",
            "Mindlock Apartment Point Click Thriller Demo",
            "Clockwork Harbour Maritime Puzzle Chronicle",
            "Ashlands Tactical Deckbuilder Steam Update",
            "Velvet Station Underground Narrative Indie Game",
        ]

        class _MockWeb:
            async def search_broad(self, query: str, *, max_results: int = 8, context=None):
                return {
                    "results": [
                        {
                            "title": title,
                            "url": f"https://indie{i}.example/games/item-{i}",
                            "snippet": f"Hands-on indie game preview #{i} with release details.",
                            "published_at": recent_iso,
                        }
                        for i, title in enumerate(game_titles)
                    ]
                }

            async def fetch(self, url: str, *, context=None):
                return {
                    "title": "Indie Game Hands-On",
                    "content": " ".join(["Full indie game impressions and release mechanics details."] * 8),
                    "published_at": recent_iso,
                }

        res = await dw.collect(
            web=_MockWeb(),
            queries=["indie detective game 2026"],
            topics=["独立游戏"],
            max_items=12,
        )
        cands = res["candidates"]
        search_cnt = sum(1 for c in cands if c.get("origin") == "search")
        rss_cnt = sum(1 for c in cands if c.get("origin") == "rss")
        assert search_cnt >= 5, res
        assert rss_cnt >= 5, res
    finally:
        await plg.on_unload()


@pytest.mark.asyncio
async def test_digest_poll_deduplicates_trial_candidates_across_repeated_polls(tmp_path):
    plg = await _boot_plugin(tmp_path / "poll_dedup", enabled_chats=["qq:group:10001"])
    try:
        cur = plg.memory.read("profile", target="digest", document="profile.toml")
        plg.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )
        row = plg.threads.create(
            plg.profiles.get("digest"),
            "poll 去重防误杀测试",
            chat_id="10001",
            platform="qq",
            user_id="",
            workspace="digest",
        )
        work = await plg.runtime._work(row)
        now_ts = time.time()
        clock = [now_ts]
        recent_rfc = formatdate(now_ts - 3600, usegmt=True)

        async def _static_fetcher(url: str):
            items = "\n".join(
                f"<item><title>Indie Preview #{i}</title>"
                f"<link>https://indiesource.example/posts/p-{i}</link>"
                f"<pubDate>{recent_rfc}</pubDate>"
                f"<description>Static RSS candidate #{i}.</description></item>"
                for i in range(4)
            )
            return {
                "body": f"<rss version='2.0'><channel><title>IndieSource</title>{items}</channel></rss>".encode(),
                "content_type": "application/xml",
                "status": 200,
                "url": url,
                "error": "",
            }

        dw = DigestWorkspace(work, fetcher=_static_fetcher, clock=lambda: clock[0])
        added = await dw.add_feed(
            "https://indiesource.example/feed.xml",
            title="IndieSource",
            auto=True,
            origin="starter",
        )
        assert added["ok"] and added["feed"]["status"] == "trial"

        polled = {}
        for _ in range(6):
            clock[0] += 300.0
            polled = await dw.poll()

        assert polled["unsubscribed"] == []
        feeds = dw.status()["feeds"]
        assert len(feeds) == 1
        assert feeds[0]["status"] == "trial"
        assert len(feeds[0]["candidates_14d"]) == 2
    finally:
        await plg.on_unload()


@pytest.mark.asyncio
async def test_share_auto_includes_declared_result_attachments_when_handles_omitted(tmp_path):
    import base64 as _b64

    plg = await _boot_plugin(tmp_path / "share_auto_att", enabled_chats=["qq:group:10001"])
    try:
        cur = plg.memory.read("profile", target="digest", document="profile.toml")
        plg.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )
        sent_calls = []

        async def _cap(name, **kwargs):
            if name == "chat.get_all_streams":
                return {"success": True, "streams": list(plg.ctx.streams)}
            if name == "maisaka.reply.generate":
                return {"success": True, "text": "今天的独立游戏精选出炉啦。"}
            if name == "send.hybrid":
                sent_calls.append(kwargs)
                return {"success": True, "sent": True, "message_id": "msg-share-att"}
            return {"success": True, "response": "ok", "tool_calls": []}

        plg.ctx.call_capability = _cap
        await plg.after_receive(
            message={
                "message_id": "m-grp-1",
                "session_id": "10001",
                "platform": "qq",
                "timestamp": str(time.time()),
                "processed_plain_text": "发一下简报",
                "raw_message": [{"type": "text", "data": "发一下简报"}],
                "message_info": {
                    "user_info": {"user_id": "Alice"},
                    "group_info": {"group_id": "10001"},
                    "additional_config": {"relayed_by_plugin": ""},
                },
            }
        )

        row = plg.threads.create(
            plg.profiles.get("digest"),
            "自动携带成果图片附件",
            chat_id="10001",
            platform="qq",
            user_id="qq:Alice",
            workspace="digest",
        )
        tid = row["thread_id"]
        work = await plg.runtime._work(row)
        png_bytes = _b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
        )
        work.write_bytes("out/digest-card.html", b"<html><body>card</body></html>")
        work.write_bytes("out/digest-card.png", png_bytes)
        work.commit()
        atts = plg.runtime.files.attach(row, work, ["out/digest-card.html", "out/digest-card.png"])
        plg.threads.finish(
            tid,
            "10001",
            {"text": "今日独立游戏简报 https://indie.example/game1", "attachments": atts},
            [],
            row["revision"],
        )
        plg.bridge.stage(plg.threads.get(tid, "10001"), "10001")

        shared = await plg.agent_tool(
            action="share",
            thread_id=tid,
            chat_id="10001",
            platform="qq",
            source_msg_id="m-grp-1",
        )
        assert shared["route"] == "share" and shared["sent"] is True, shared
        assert len(sent_calls) == 1
        seg_types = [seg.get("type") for seg in sent_calls[0]["segments"]]
        assert "image" in seg_types, sent_calls[0]["segments"]
        assert "file" not in seg_types, sent_calls[0]["segments"]
    finally:
        await plg.on_unload()


def test_on_load_requeues_orphaned_claimed_intents_after_runner_reload(tmp_path):
    """claimed 态但尚未建线程的 intent 在插件重载 on_load() 时必须自动回退为 pending。"""
    import asyncio
    import time

    async def _run():
        boot_dir = tmp_path / "requeue_boot"
        plg1 = await _boot_plugin(boot_dir, enabled_chats=["qq:group:10001"])
        try:
            await plg1.pulse.close()
            scheduled = plg1.intents.schedule(
                kind="return",
                source="workflow",
                target="chat:priv_admin_md5",
                due_at=time.time() + 3600,
                reason="digest private preview reload recovery test",
                payload={"platform": "qq"},
                bypass_limits=True,
            )
            intent_id = scheduled["intent_id"]
            with plg1.threads.connect() as db, db:
                db.execute(
                    "UPDATE intents SET status='claimed' WHERE intent_id=?",
                    (intent_id,),
                )
            assert plg1.intents.get(intent_id)["status"] == "claimed"
        finally:
            await plg1.on_unload()

        plg2 = await _boot_plugin(boot_dir, enabled_chats=["qq:group:10001"])
        try:
            assert plg2.intents.get(intent_id)["status"] == "pending"
        finally:
            await plg2.on_unload()

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_admin_direct_attachment_preview_auto_shares_when_planner_calls_wait_on_stale_history(tmp_path):
    """管理员早于试跑前的旧取消语不得让 Planner 在未分享预览图卡时静默吞掉交付；若确有晚于试跑的新人类输入则不自动发送。"""
    import base64 as _b64

    plg = await _boot_plugin(tmp_path / "auto_share_wait", enabled_chats=["qq:group:10001"])
    try:
        await plg.pulse.close()
        plg.pulse.closing = False
        cur = plg.memory.read("profile", target="digest", document="profile.toml")
        plg.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )

        clock = [time.time() - 200.0]
        plg.bridge.clock = lambda: clock[0]
        plg.intents.clock = lambda: clock[0]
        plg.messages.clock = lambda: clock[0]

        wake_calls = []
        sent_calls = []

        async def _cap(name, **kwargs):
            if name == "chat.get_all_streams":
                return {"success": True, "streams": list(plg.ctx.streams)}
            if name == "maisaka.proactive.trigger":
                wake_calls.append(kwargs)
                return {"success": True, "queued": True}
            if name == "maisaka.reply.generate":
                return {"success": True, "text": "刚跑好的独立游戏简报发你。"}
            if name == "send.hybrid":
                sent_calls.append(kwargs)
                return {
                    "success": True,
                    "sent": True,
                    "message_id": f"msg-priv-{len(sent_calls)}",
                }
            return {"success": True, "response": "ok", "tool_calls": []}

        plg.ctx.call_capability = _cap

        # 1. 记录一条早于本次试跑的管理员私聊“不要，别搞了”并模拟该旧轮结束
        await plg.after_receive(
            message=_private_msg("m-old-stop", "不要，别搞了")
        )
        plg._pending_sources.pop("priv_admin_md5", None)
        plg._turn_sources.pop("priv_admin_md5", None)

        # 2. 时间推到当前，发起 admin_direct 私聊预览试跑并完成 digest-card.png 挂载
        clock[0] = time.time()
        grant = {
            "scheduled_admin": True,
            "admin_direct": True,
            "source_chat_id": "priv_admin_md5",
            "source_platform": "qq",
            "source_msg_id": "admin-preview",
        }
        profile = plg.profiles.get("digest")
        row1 = plg.threads.create(
            profile,
            "试跑简报1",
            chat_id="10001",
            platform="qq",
            user_id="",
            owner="chat",
            workspace="digest",
            grant=grant,
        )
        tid = row1["thread_id"]
        route1 = await plg.bridge.route("priv_admin_md5", "admin-preview", 0)
        rev1 = plg.threads.enqueue(
            tid,
            "试跑简报1",
            "10001",
            return_route=route1,
            input_context={"grant": grant},
        )
        work1 = await plg.runtime._work(plg.threads.get(tid, "10001"))
        png_bytes = _b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
        )
        work1.write_bytes("out/digest-card.png", png_bytes)
        work1.commit()
        atts1 = plg.runtime.files.attach(
            plg.threads.get(tid, "10001"), work1, ["out/digest-card.png"]
        )
        plg.threads.finish(
            tid,
            "10001",
            {"text": "已生成并挂载独立游戏简报图卡（2 条精选）。", "attachments": atts1},
            [],
            rev1,
        )
        await plg.bridge.submitted(route1)

        # 3. Pulse._returns 唤醒管理员私聊，须保留标准前缀并提示 share 与忽略旧消息
        clock[0] += 2.0
        await plg.pulse._returns()
        assert len(wake_calls) == 1
        assert wake_calls[0]["stream_id"] == "priv_admin_md5"
        assert wake_calls[0]["intent"].startswith(
            "之前由本会话真实用户消息触发的 Agent 线程已有结果。"
        )
        assert "maisya_agent(action='share'" in wake_calls[0]["intent"]
        assert "旧" in wake_calls[0]["intent"]

        # 4. Planner 受旧聊天记录影响误调用 wait({}) -> after_planner 须自动兜底 share 发送预览图给私聊，且绝不发往群
        await plg.before_planner(
            session_id="priv_admin_md5",
            tool_definitions=[{"name": "maisya_agent"}, {"name": "wait"}],
            items=[],
        )
        await plg.after_planner(
            session_id="priv_admin_md5",
            output_items=[{"tool_call": {"func_name": "wait", "args": {}}}],
        )
        assert len(sent_calls) == 1
        assert sent_calls[0]["stream_id"] == "priv_admin_md5"
        assert any(seg.get("type") == "image" for seg in sent_calls[0]["segments"])
        assert all(call["stream_id"] != "10001" for call in sent_calls)
        delivery1 = plg.bridge.delivery(tid, rev1, "priv_admin_md5")
        assert delivery1 is not None and delivery1["status"] == "sent"

        # 5. 若试跑入队后确有更新的人类消息（> input_at）叫停，则 after_planner 调用 wait 不得再自动发送
        clock[0] += 10.0
        route2 = await plg.bridge.route("priv_admin_md5", "admin-preview-2", 0)
        rev2 = plg.threads.enqueue(
            tid,
            "试跑简报2",
            "10001",
            return_route=route2,
            input_context={"grant": grant},
        )
        work2 = await plg.runtime._work(plg.threads.get(tid, "10001"))
        work2.write_bytes("out/digest-card-2.png", png_bytes)
        work2.commit()
        atts2 = plg.runtime.files.attach(
            plg.threads.get(tid, "10001"), work2, ["out/digest-card-2.png"]
        )
        plg.threads.finish(
            tid,
            "10001",
            {"text": "第二轮预览图卡", "attachments": atts2},
            [],
            rev2,
        )
        await plg.bridge.submitted(route2)

        clock[0] += 2.0
        await plg.pulse._returns()
        await plg.before_planner(
            session_id="priv_admin_md5",
            tool_definitions=[{"name": "maisya_agent"}, {"name": "wait"}],
            items=[],
        )
        clock[0] = time.time() + 50.0
        await plg.after_receive(
            message=_private_msg("m-fresh-stop", "刚发错了，这次先别发")
        )
        plg._pending_sources.pop("priv_admin_md5", None)
        plg._turn_sources.pop("priv_admin_md5", None)
        await plg.after_planner(
            session_id="priv_admin_md5",
            output_items=[{"tool_call": {"func_name": "wait", "args": {}}}],
        )
        assert len(sent_calls) == 1
        assert plg.bridge.delivery(tid, rev2, "priv_admin_md5") is None
    finally:
        await plg.on_unload()
