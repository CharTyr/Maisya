import asyncio
import hashlib
import hmac
import json
import time

import pytest
from test_foundation import loaded

from maisya.abilities import Profile
from maisya.intents import Intents
from maisya.memory import MemoryLayers
from maisya.threads import ThreadEngine, ThreadStore, encode


class _GateRuntime:
    def __init__(self, *, serendipity_tool=False):
        self.seen = []
        self.gate = None
        self.entered = None
        self.serendipity_tool = serendipity_tool
        self.tool_results = []

    async def step(self, profile, history):
        self.seen.append(json.loads(json.dumps(history)))
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.serendipity_tool and not any(m.get("role") == "tool" for m in history):
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": "call-ser",
                        "function": {
                            "name": "thread_later",
                            "arguments": {
                                "in_seconds": 60,
                                "next_step": "记下发现",
                                "kind": "serendipity",
                                "share_reason": "发现了一个复古终端字体",
                            },
                        },
                    }
                ],
            }
        if self.serendipity_tool:
            for msg in reversed(history):
                if msg.get("role") == "tool":
                    self.tool_results.append(msg["content"])
                    break
        return {
            "success": True,
            "response": "ok: " + str(history[-1]["content"]),
            "tool_calls": [],
        }

    async def call(self, name, arguments, context):
        return {"text": "tool-ok"}


def _sign(secret, **event):
    body = json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_background_limit_formula():
    store_dummy = None
    runtime = _GateRuntime()
    for max_active, expected in [(1, 1), (2, 2), (3, 2), (4, 2), (5, 3), (8, 6)]:
        eng = ThreadEngine(store_dummy, runtime, max_active=max_active)
        assert eng._background_limit() == expected


@pytest.mark.asyncio
async def test_background_lane_reserves_interactive_slots_and_cleans_up(tmp_path):
    store = ThreadStore(tmp_path / "maisya.db")
    runtime = _GateRuntime()
    runtime.gate = asyncio.Event()
    engine = ThreadEngine(store, runtime, max_active=4)
    try:
        bg1 = await engine.start(
            Profile(), "bg1", chat_id="chat-a", platform="qq", user_id="u1", wait_seconds=0, background=True
        )
        bg2 = await engine.start(
            Profile(), "bg2", chat_id="chat-a", platform="qq", user_id="u2", wait_seconds=0, background=True
        )
        assert engine.background_tasks == {bg1["thread_id"], bg2["thread_id"]}
        assert len(engine.tasks) == 2

        # Background lane is full (_background_limit(4) == 2) when defer_if_full=False
        with pytest.raises(ValueError, match="已满"):
            await engine.start(
                Profile(), "bg3", chat_id="chat-a", platform="qq", user_id="u3", wait_seconds=0, background=True
            )

        # Interactive lane still has 2 reserved slots!
        fg1 = await engine.start(
            Profile(), "fg1", chat_id="chat-a", platform="qq", user_id="u4", wait_seconds=0, background=False
        )
        fg2 = await engine.start(
            Profile(), "fg2", chat_id="chat-a", platform="qq", user_id="u5", wait_seconds=0, background=False
        )
        assert len(engine.tasks) == 4
        assert engine.background_tasks == {bg1["thread_id"], bg2["thread_id"]}

        # Total max_active == 4 now exhausted even for interactive
        with pytest.raises(ValueError, match="已满"):
            await engine.start(
                Profile(), "fg3", chat_id="chat-a", platform="qq", user_id="u6", wait_seconds=0, background=False
            )

        runtime.gate.set()
        await engine.drain()
        assert engine.tasks == {}
        assert engine.background_tasks == set()
        assert store.get(fg1["thread_id"], "chat-a")["state"] == "idle"
        assert store.get(fg2["thread_id"], "chat-a")["state"] == "idle"
    finally:
        runtime.gate.set()
        await engine.close()


@pytest.mark.asyncio
async def test_defer_if_full_persists_durably_and_kick_queued_runs_later(tmp_path):
    store = ThreadStore(tmp_path / "maisya.db")
    runtime = _GateRuntime()
    runtime.gate = asyncio.Event()
    engine = ThreadEngine(store, runtime, max_active=3)
    try:
        # Fill background lane (_background_limit(3) == 2)
        b1 = await engine.start(
            Profile(), "bg1", chat_id="chat-a", platform="qq", user_id="u1", wait_seconds=0, background=True
        )
        b2 = await engine.start(
            Profile(), "bg2", chat_id="chat-a", platform="qq", user_id="u2", wait_seconds=0, background=True
        )
        assert len(engine.tasks) == 2

        # Deferred start must persist thread + input event without spawning task
        deferred = await engine.start(
            Profile(),
            "deferred bg task",
            chat_id="chat-b",
            platform="qq",
            user_id="",
            wait_seconds=0,
            background=True,
            defer_if_full=True,
        )
        assert deferred["thread_id"] not in engine.tasks
        row = store.get(deferred["thread_id"], "chat-b")
        assert row["state"] == "active"
        assert row["revision"] == 1
        assert row["processed_revision"] == 0
        assert store.pending(deferred["thread_id"], 0) == [(1, "deferred bg task")]

        # Also test deferred send on a separate idle thread while background lane is full
        idle_row = store.create(
            Profile(), "idle seed", chat_id="chat-c", platform="qq", user_id=""
        )
        queued_send = await engine.send(
            idle_row["thread_id"],
            "queued follow-up",
            chat_id="chat-c",
            wait_seconds=0,
            background=True,
            defer_if_full=True,
        )
        assert queued_send["thread_id"] not in engine.tasks
        assert store.get(idle_row["thread_id"], "chat-c")["revision"] == 1
        assert store.pending(idle_row["thread_id"], 0) == [(1, "queued follow-up")]

        # While still full, kick_queued returns False
        assert await engine.kick_queued(deferred["thread_id"], "chat-b", background=True) is False
        assert await engine.kick_queued("th_missing", "chat-b", background=True) is False
        assert await engine.kick_queued(b1["thread_id"], "chat-a", background=True) is False

        # Drain active tasks -> capacity opens up
        runtime.gate.set()
        await engine.drain()
        assert len(engine.tasks) == 0

        # Kick both deferred threads -> they run and finish cleanly
        assert await engine.kick_queued(deferred["thread_id"], "chat-b", background=True) is True
        assert deferred["thread_id"] in engine.background_tasks
        assert await engine.kick_queued(idle_row["thread_id"], "chat-c", background=True) is True
        await engine.drain()

        done_deferred = store.get(deferred["thread_id"], "chat-b")
        assert done_deferred["state"] == "idle"
        assert done_deferred["processed_revision"] == 1
        assert "deferred bg task" in done_deferred["result"]["text"]

        done_send = store.get(idle_row["thread_id"], "chat-c")
        assert done_send["state"] == "idle"
        assert done_send["processed_revision"] == 1
        assert "queued follow-up" in done_send["result"]["text"]

        # Already up-to-date thread cannot be kicked again
        assert await engine.kick_queued(deferred["thread_id"], "chat-b", background=True) is False

        # Closed thread cannot be kicked
        closed_row = store.create(Profile(), "to close", chat_id="chat-d", platform="qq", user_id="")
        store.enqueue(closed_row["thread_id"], "queued before close", "chat-d")
        store.stop(closed_row["thread_id"], "chat-d")
        assert await engine.kick_queued(closed_row["thread_id"], "chat-d", background=True) is False
    finally:
        runtime.gate.set()
        await engine.close()


@pytest.mark.asyncio
async def test_recover_and_kick_queued_resumes_deferred_and_completed_threads(tmp_path):
    store = ThreadStore(tmp_path / "maisya.db")
    runtime = _GateRuntime()
    engine = ThreadEngine(store, runtime, max_active=2)
    try:
        # Completed rev 1, then deferred rev 2 when full
        t1 = await engine.start(
            Profile(), "first run", chat_id="chat-a", platform="qq", user_id="Alice", wait_seconds=5
        )
        assert t1["state"] == "idle"

        runtime.gate = asyncio.Event()
        blocker1 = await engine.start(
            Profile(), "b1", chat_id="chat-a", platform="qq", user_id="Alice", wait_seconds=0
        )
        blocker2 = await engine.start(
            Profile(), "b2", chat_id="chat-a", platform="qq", user_id="Alice", wait_seconds=0
        )
        assert len(engine.tasks) == 2

        await engine.send(
            t1["thread_id"], "second deferred run", chat_id="chat-a", wait_seconds=0, defer_if_full=True
        )
        deferred_new = await engine.start(
            Profile(), "self queued", chat_id="self", platform="self", user_id="", owner="self", wait_seconds=0, background=True, defer_if_full=True
        )
        await engine.close()
    finally:
        if runtime.gate:
            runtime.gate.set()

    restarted = ThreadEngine(ThreadStore(store.path), _GateRuntime(), max_active=4)
    try:
        await restarted.recover()
        r_t1 = restarted.store.get(t1["thread_id"], "chat-a")
        assert r_t1["state"] == "idle"
        assert r_t1["processed_revision"] == 1
        assert r_t1["revision"] == 2
        assert "first run" in r_t1["result"]["text"]

        r_self = restarted.store.get(deferred_new["thread_id"], "self", allow_self=True)
        assert r_self["state"] == "idle"
        assert r_self["processed_revision"] == 0
        assert r_self["revision"] == 1

        assert await restarted.kick_queued(t1["thread_id"], "chat-a", background=True) is True
        assert await restarted.kick_queued(deferred_new["thread_id"], "", background=True) is True
        assert restarted.chats[deferred_new["thread_id"]] == "self"
        await restarted.drain()

        assert "second deferred run" in restarted.store.get(t1["thread_id"], "chat-a")["result"]["text"]
        assert "self queued" in restarted.store.get(deferred_new["thread_id"], "self")["result"]["text"]
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
async def test_drive_serendipity_supports_sync_and_async_callables(tmp_path, is_async):
    store = ThreadStore(tmp_path / "maisya.db")
    intents = Intents(store)
    calls = []

    if is_async:
        async def serendipity_fn(*, thread_id, due_at, reason):
            await asyncio.sleep(0)
            calls.append(("async", thread_id, due_at, reason))
            return {"scheduled": True, "mode": "async"}
    else:
        def serendipity_fn(*, thread_id, due_at, reason):
            calls.append(("sync", thread_id, due_at, reason))
            return {"scheduled": True, "mode": "sync"}

    runtime = _GateRuntime(serendipity_tool=True)
    engine = ThreadEngine(store, runtime, intents=intents, serendipity=serendipity_fn)
    try:
        profile = Profile(
            id="scout",
            access="everyone",
            tools=("web_search",),
        )
        res = await engine.start(
            profile,
            "随便逛逛",
            chat_id="self",
            platform="self",
            user_id="",
            owner="self",
            wait_seconds=5,
        )
        assert res["state"] == "idle"
        assert len(calls) == 1
        assert calls[0][0] == ("async" if is_async else "sync")
        assert '"scheduled": true' in runtime.tool_results[0].lower()
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_webhook_with_profile_persists_durably_when_full_and_validates_first(tmp_path):
    plugin = await loaded(tmp_path)
    secret = "webhook-full-lane-secret"
    plugin.vault.set("hook_key", secret)
    plugin.engine.max_active = 3  # _background_limit == 2
    gate = asyncio.Event()

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            await gate.wait()
            return {"success": True, "response": "处理完成", "tool_calls": []}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    try:
        # Occupy both background slots
        b1 = await plugin.engine.start(
            plugin.profiles.get("scout"),
            "bg task 1",
            chat_id="chat",
            platform="qq",
            user_id="",
            wait_seconds=0,
            background=True,
        )
        b2 = await plugin.engine.start(
            plugin.profiles.get("scout"),
            "bg task 2",
            chat_id="chat",
            platform="qq",
            user_id="",
            wait_seconds=0,
            background=True,
        )
        assert len(plugin.engine.background_tasks) == 2

        current = await plugin.manage_read("webhooks", "{}")
        saved = await plugin.manage_write(
            "webhooks",
            json.dumps(
                {
                    "configuration": {
                        "alerts": {
                            "target": "chat:chat",
                            "kind": "proactive",
                            "profile": "scout",
                            "secret_handle": "hook_key",
                        }
                    },
                    "expected_version": current["version"],
                }
            ),
        )
        assert saved["success"], saved

        # Validation error before creating thread or intent
        too_big = {
            "channel": "alerts",
            "event_id": "bad-size",
            "timestamp": int(time.time()),
            "payload": json.dumps({"reason": "ok", "data": {"blob": "x" * 15950}}),
        }
        bad_res = await plugin.incoming_event(**too_big, signature=_sign(secret, **too_big))
        assert not bad_res["success"]
        assert plugin.intents.list("chat:chat") == []

        # Valid webhook arrives while background lane is full -> durably queued, not dropped!
        ev = {
            "channel": "alerts",
            "event_id": "evt-full-1",
            "timestamp": int(time.time()),
            "payload": json.dumps({"reason": "紧急告警：" + ("A" * 520), "data": {"id": 42}}),
        }
        accepted = await plugin.incoming_event(**ev, signature=_sign(secret, **ev))
        assert accepted["success"], accepted
        tid = accepted["thread_id"]
        assert tid.startswith("th_")
        assert tid not in plugin.engine.tasks
        assert len(plugin.engine.tasks) == 2

        queued_row = plugin.threads.get(tid, "chat")
        assert queued_row["state"] == "active"
        assert queued_row["revision"] == 1
        assert queued_row["processed_revision"] == 0
        intent_row = plugin.intents.get(accepted["intent_id"])
        assert intent_row["thread_id"] == tid
        assert intent_row["status"] == "pending"

        # Replay returns exact same intent_id & thread_id
        replayed = await plugin.incoming_event(**ev, signature=_sign(secret, **ev))
        assert replayed["intent_id"] == accepted["intent_id"]
        assert replayed["thread_id"] == tid

        # Release blockers and drain; then kick_queued finishes the webhook thread
        gate.set()
        await plugin.engine.drain()
        assert await plugin.engine.kick_queued(tid, "chat", background=True) is True
        await plugin.engine.drain()
        finished = plugin.threads.get(tid, "chat")
        assert finished["state"] == "idle"
        assert finished["processed_revision"] == 1
        assert finished["result"]["text"] == "处理完成"
    finally:
        gate.set()
        await plugin.on_unload()


def test_compact_builds_multi_revision_archive_directory_and_bounds_summary(tmp_path):
    store = ThreadStore(tmp_path / "threads.db")
    row = store.create(
        Profile(), "start task", chat_id="chat-a", platform="qq", user_id="Alice"
    )
    tid = row["thread_id"]
    history = []
    archive_seqs = []
    for rev in range(1, 26):
        rev_num = store.enqueue(tid, f"goal round {rev} with extra context", "chat-a")
        history.extend(
            [
                {"role": "user", "content": f"goal round {rev} " + ("detail " * 25)},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_{rev}_a",
                            "type": "function",
                            "function": {"name": f"web_search", "arguments": "{}"},
                        },
                        {
                            "id": f"call_{rev}_b",
                            "type": "function",
                            "function": {"name": f"tool_round_{rev}", "arguments": "{}"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call_{rev}_a",
                    "content": "<external_data>" + ("payload " * 320) + "</external_data>",
                },
                {
                    "role": "tool",
                    "tool_call_id": f"call_{rev}_b",
                    "content": "<external_data>ok</external_data>",
                },
                {
                    "role": "assistant",
                    "content": f"结论第 {rev} 轮：" + ("重要事实 " * 15),
                },
            ]
        )
        store.checkpoint(tid, history, rev_num, "tools", {"round": rev})
        summary_raw = store.compact(tid, "chat-a", max_chars=2000)
        history = store.get(tid, "chat-a")["history"]
        summary_obj = json.loads(summary_raw)
        assert len(encode(summary_obj)) <= 2400
        archive_seqs.append(summary_obj["archive_event"])

    final_summary = json.loads(store.get(tid, "chat-a")["summary"])
    assert final_summary["archive_event"] == archive_seqs[-1]
    assert final_summary["read_tool"] == "thread_read"
    assert "note" in final_summary
    assert final_summary["archive_events"] == archive_seqs[-20:]
    assert len(final_summary["archive_events"]) == 20
    assert len(final_summary["archive_events"]) == len(set(final_summary["archive_events"]))
    assert isinstance(final_summary["archived_runs"], list)
    assert len(final_summary["archived_runs"]) >= 1
    latest_run = final_summary["archived_runs"][-1]
    assert latest_run["archive_event"] == archive_seqs[-1]
    assert latest_run["revision"] == 25
    assert len(latest_run["goals"]) <= 3
    assert all(len(g) <= 80 for g in latest_run["goals"])
    assert len(latest_run["tools"]) <= 8
    assert len(latest_run["tools"]) == len(set(latest_run["tools"]))
    assert len(latest_run["highlights"]) <= 2
    assert all(len(h) <= 100 for h in latest_run["highlights"])
    assert any(
        "web_search" in run["tools"] and any("结论第" in h for h in run["highlights"])
        for run in final_summary["archived_runs"]
    )


def test_compact_captures_dropped_goals_tools_and_highlights(tmp_path):
    store = ThreadStore(tmp_path / "threads.db")
    row = store.create(Profile(), "init", chat_id="chat-a", platform="qq", user_id="Alice")
    tid = row["thread_id"]
    rev = store.enqueue(tid, "init", "chat-a")
    history = [
        {"role": "user", "content": "goal alpha one"},
        {"role": "user", "content": "goal beta two"},
        {
            "role": "assistant",
            "content": "dropped highlight text",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "web_fetch", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "<external_data>" + ("x" * 1600) + "</external_data>"},
        {"role": "tool", "tool_call_id": "c2", "content": "<external_data>ok</external_data>"},
        {"role": "user", "content": "final retained prompt"},
        {"role": "assistant", "content": "final retained answer " + ("R" * 485)},
    ]
    store.checkpoint(tid, history, rev, "tools", {})
    summary_raw = store.compact(tid, "chat-a", max_chars=900)
    summary = json.loads(summary_raw)
    assert len(summary["archived_runs"]) == 1
    entry = summary["archived_runs"][0]
    assert entry["revision"] == rev
    assert "goal beta two" in entry["goals"]
    assert entry["tools"] == ["web_search", "web_fetch"]
    assert "dropped highlight text" in entry["highlights"][0]


def test_experience_merges_same_day_entries_and_trims_oldest_entries_first(tmp_path):
    from maisya.abilities import Profiles
    from maisya.vault import Vault

    clock_val = [1794715200.0]  # 2026-11-15T04:00:00Z
    store = ThreadStore(tmp_path / "maisya.db")
    vault = Vault(tmp_path / "vault.key")
    profiles = Profiles(tmp_path / "profiles")
    memory = MemoryLayers(
        tmp_path / "memory", store, vault, profiles, clock=lambda: clock_val[0]
    )
    prof = profiles.get("scout")

    for idx, text in enumerate(["第一条今日经历", "第二条今日经历", "第三条今日经历"], 1):
        row = store.create(
            prof, f"trip {idx}", chat_id="self", platform="self", user_id="", owner="self"
        )
        memory.experience(
            {**row, "result": {"text": text}, "completed_revision": idx}
        )

    journal_text = memory.read("journal")["content"]
    assert journal_text.count("## 2026-11-15") == 1
    assert "- 第一条今日经历" in journal_text
    assert "- 第二条今日经历" in journal_text
    assert "- 第三条今日经历" in journal_text

    # Legacy duplicate headers on same day also merge cleanly
    legacy = "## 2026-11-15\n旧条目一\n\n## 2026-11-15\n旧条目二"
    merged = memory._recent_journal(legacy)
    assert merged.count("## 2026-11-15") == 1
    assert "旧条目一" in merged and "旧条目二" in merged

    # Trimming under LIMITS["journal"] removes oldest entries inside oldest day before dropping the day
    memory.LIMITS = {**memory.LIMITS, "journal": 140}
    multi_day = (
        "## 2026-11-14\n"
        "- yesterday-entry-1-alpha-00001\n"
        "- yesterday-entry-2-beta-00002\n"
        "- yesterday-entry-3-gamma-00003\n\n"
        "## 2026-11-15\n"
        "- today-entry-1-delta-00004"
    )
    trimmed = memory._recent_journal(multi_day)
    assert len(trimmed) <= 140
    assert "## 2026-11-14" in trimmed
    assert "yesterday-entry-1" not in trimmed
    assert "yesterday-entry-3" in trimmed
    assert "## 2026-11-15" in trimmed
    assert "today-entry-1" in trimmed

    # If even 1 entry on oldest day + today exceeds limit, drop oldest day block
    memory.LIMITS = {**memory.LIMITS, "journal": 70}
    trimmed_day = memory._recent_journal(multi_day)
    assert len(trimmed_day) <= 70
    assert "## 2026-11-14" not in trimmed_day
    assert "## 2026-11-15" in trimmed_day
    assert "today-entry-1" in trimmed_day
