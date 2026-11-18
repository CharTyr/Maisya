import asyncio
import time
from types import SimpleNamespace

import pytest

from maisya.abilities import Profiles
from maisya.bridge import Bridge
from maisya.intents import Intents
from maisya.maker import AgentRuntime
from maisya.memory import MemoryLayers
from maisya.pulse import Pulse
from maisya.sandbox import SandboxSettings
from maisya.threads import ThreadEngine, ThreadStore
from maisya.trust import Message, MessageStore
from maisya.vault import Vault


def _event(text="hello", *, user="Alice", platform="qq", group=False, relay=""):
    return {
        "message_id": "host-message-123456789",
        "session_id": "chat",
        "platform": platform,
        "timestamp": "1794459000.0",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user},
            "group_info": {"group_id": "group"} if group else None,
            "additional_config": {"relayed_by_plugin": relay},
        },
    }


class _Harness:
    def __init__(self, tmp_path):
        self.data_root = tmp_path / "data"
        self.data_root.mkdir(parents=True, exist_ok=True)
        (self.data_root / "abilities").mkdir(parents=True, exist_ok=True)
        self.vault = Vault(self.data_root / "vault")
        self.threads = ThreadStore(self.data_root / "maisya.db")
        self.messages = MessageStore(self.data_root / "maisya.db")
        self.intents = Intents(self.threads, daily_proactive_limit=1)
        self.bridge = Bridge(self.threads, redact=self.vault.redact)
        self.profiles = Profiles(
            self.data_root / "profiles", contains_secret=self.vault.contains_secret
        )

        async def _default_cap(name, **arguments):
            return {"success": True, "response": "ok", "tool_calls": []}

        self.ctx = SimpleNamespace(call_capability=_default_cap)
        self.runtime = AgentRuntime(
            self.ctx,
            self.data_root,
            self.threads,
            SandboxSettings(enabled=False),
            self.vault,
            authorized=lambda context: True,
        )
        self.runtime.memory = MemoryLayers(
            self.data_root, self.threads, self.profiles, self.vault
        )
        self.engine = ThreadEngine(
            self.threads,
            self.runtime,
            redact=self.vault.redact,
            intents=self.intents,
        )
        self.enabled_chats = {"chat"}
        self.pulse = Pulse(
            self.bridge,
            self.ctx,
            lambda chat_id, platform="": chat_id in self.enabled_chats,
            intents=self.intents,
            engine=self.engine,
            profiles=self.profiles,
            redact=self.vault.redact,
            routes=self.messages.route_for,
            memory=self.runtime.memory,
        )

    async def on_unload(self):
        await self.engine.close()
        await self.runtime.services.close()
        await self.runtime.sandbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["proactive", "return"])
async def test_watch_reserves_run_budget_before_dispatch(tmp_path, kind):
    h = _Harness(tmp_path)
    now = 20_000.0
    h.intents.clock = lambda: now
    h.pulse.daily_bg_runs_limit = 1
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def model(name, **kwargs):
        assert name == "llm.generate_with_tools"
        calls.append(name)
        started.set()
        await release.wait()
        return {"success": True, "response": "[WATCH:NO_CHANGE]", "total_tokens": 50}

    h.ctx.call_capability = model
    first, second = [
        h.intents.schedule(
            kind=kind,
            source="watch",
            target="chat:chat",
            due_at=now - 2 + index,
            reason=f"检查 {index}",
            mode="watch",
            payload={"profile": "scout", "platform": "qq"},
        )["intent_id"]
        for index in range(2)
    ]
    try:
        await h.pulse._intents()
        await asyncio.wait_for(started.wait(), 3)
        await h.pulse._intents()
        assert h.intents.get(second)["status"] == "budget_paused"
        assert h.intents.bg_budget_state()["runs_used"] == 1

        release.set()
        await h.engine.drain()
        now += 3
        await h.pulse._intents()
        assert h.intents.get(first)["status"] == "done"
        assert len(calls) == 1
        state = h.intents.bg_budget_state()
        assert (state["runs_used"], state["tokens_used"]) == (1, 50)

        now = h.intents.get(second)["due_at"]
        await h.pulse._intents()
        await h.engine.drain()
        now += 3
        await h.pulse._intents()
        assert h.intents.get(second)["status"] == "done"
        assert len(calls) == 2
        assert h.intents.bg_budget_state()["runs_used"] == 1
    finally:
        release.set()
        await h.on_unload()


@pytest.mark.asyncio
async def test_watch_refunds_budget_when_thread_submission_fails(tmp_path, monkeypatch):
    h = _Harness(tmp_path)
    h.pulse.daily_bg_runs_limit = 1
    now = h.intents.clock()
    first, second = [
        h.intents.schedule(
            kind="return",
            source="watch",
            target="chat:chat",
            due_at=now - 2 + index,
            reason=f"检查 {index}",
            mode="watch",
            payload={"profile": "scout", "platform": "qq"},
        )["intent_id"]
        for index in range(2)
    ]
    start = h.pulse._start_workflow_thread
    observed_runs = []

    async def fail_first(*args, **kwargs):
        observed_runs.append(h.intents.bg_budget_state()["runs_used"])
        if len(observed_runs) == 1:
            raise RuntimeError("工作区正在运行中")
        return await start(*args, **kwargs)

    monkeypatch.setattr(h.pulse, "_start_workflow_thread", fail_first)
    try:
        await h.pulse._intents()
        assert h.intents.get(first)["status"] == "pending"
        assert observed_runs == [1, 1]
        assert h.intents.get(second)["status"] in {"pending", "done"}
        assert h.intents.bg_budget_state()["runs_used"] == 1
    finally:
        await h.on_unload()


@pytest.mark.asyncio
async def test_later_schedules_reminder_and_pulse_wakes_original_chat(tmp_path):
    h = _Harness(tmp_path)
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    h.ctx.call_capability = capability
    now = time.time()
    result = h.intents.schedule(
        kind="return",
        source="later",
        target="chat:chat",
        due_at=now + 30,
        reason="问问对方奶茶到了没",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    assert result["intent_id"].startswith("in_")
    h.intents.clock = lambda: now + 31
    await h.pulse._intents()
    assert len(sent) == 1
    assert sent[0]["stream_id"] == "chat"
    assert "奶茶" in sent[0]["intent"]
    assert sent[0]["metadata"]["kind"] == "return"
    row = h.intents.get(result["intent_id"])
    assert row["status"] == "done" and row["wake_status"] == "accepted"
    await h.on_unload()


@pytest.mark.asyncio
async def test_proactive_intent_respects_unreplied_lock_and_sleep(tmp_path):
    h = _Harness(tmp_path)
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    h.ctx.call_capability = capability
    target = "chat:chat"
    ok = h.intents.schedule(
        kind="proactive",
        source="afterglow",
        target=target,
        due_at=time.time() - 1,
        reason="刚才聊到一半的新梗",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    h.intents.mark_proactive(target)
    await h.pulse._intents()
    assert not sent
    row = h.intents.get(ok["intent_id"])
    assert row["status"] == "failed" and row["wake_status"] == "unreplied_lock"

    h.intents.note_received("chat")
    h.messages.record(Message.from_event(_event("hi", user="Bob", platform="qq")))
    again = h.intents.schedule(
        kind="proactive",
        source="longing",
        target=target,
        due_at=time.time() - 1,
        reason="好久没说早安",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    h.pulse.sleep_window = "2-7"
    h.intents.clock = lambda: 1795202400  # 2026-11-21 03:20 Asia/Shanghai
    await h.pulse._intents()
    assert not sent
    row = h.intents.get(again["intent_id"])
    assert row["status"] == "pending" and row["due_at"] > 1795202400
    h.pulse.sleep_window = ""
    h.intents.clock = time.time
    with h.threads.connect() as db, db:
        db.execute(
            "UPDATE intents SET due_at=? WHERE intent_id=?",
            (time.time() - 1, again["intent_id"]),
        )
    await h.pulse._intents()
    assert len(sent) == 1 and sent[0]["metadata"]["kind"] == "proactive"
    await h.on_unload()


@pytest.mark.asyncio
async def test_later_list_and_cancel_and_quota(tmp_path):
    h = _Harness(tmp_path)
    first = h.intents.schedule(
        kind="return",
        source="later",
        target="chat:chat",
        due_at=time.time() + 60,
        reason="回访上次的决定",
        payload={"platform": "qq"},
    )
    second = h.intents.schedule(
        kind="return",
        source="later",
        target="chat:chat",
        due_at=time.time() + 120,
        reason="再回访一次",
        payload={"platform": "qq"},
    )
    listed = h.intents.list("chat:chat")
    assert {item["intent_id"] for item in listed} == {
        first["intent_id"],
        second["intent_id"],
    }
    cancelled = h.intents.cancel(first["intent_id"], "chat:chat")
    assert cancelled["status"] == "cancelled"

    casual = h.intents.schedule(
        kind="proactive",
        source="afterglow",
        target="chat:chat",
        due_at=time.time() - 1,
        reason="闲聊惦记",
        payload={"platform": "qq"},
    )
    with pytest.raises(ValueError):
        h.intents.schedule(
            kind="proactive",
            source="afterglow",
            target="chat:chat",
            due_at=time.time() - 1,
            reason="当天第二条闲聊惦记",
            payload={"platform": "qq"},
        )
    h.intents.cancel(casual["intent_id"], "chat:chat")
    await h.on_unload()


@pytest.mark.asyncio
async def test_silent_intent_requeues_when_thread_busy_and_clears_when_done(tmp_path):
    h = _Harness(tmp_path)

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "站到了。", "tool_calls": []}
        raise AssertionError(name)

    h.ctx.call_capability = capability
    scout = h.profiles.get("scout")
    row = await h.engine.start(
        scout,
        "去镰仓走一圈",
        chat_id="chat",
        platform="qq",
        user_id="",
        wait_seconds=5,
    )
    intent = h.intents.schedule(
        kind="silent",
        source="thread",
        target="chat:chat",
        due_at=time.time() - 1,
        reason="下一站",
        thread_id=row["thread_id"],
    )
    await h.pulse._intents()
    assert h.intents.get(intent["intent_id"])["status"] == "done"
    await h.on_unload()


@pytest.mark.asyncio
async def test_proactive_level_gates_sources(tmp_path):
    h = _Harness(tmp_path)
    h.intents.note_received("chat")
    h.messages.record(Message.from_event(_event("hi", user="Bob", platform="qq")))
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    h.ctx.call_capability = capability
    target = "chat:chat"
    h.pulse.proactive_level = "quiet"
    row = h.intents.schedule(
        kind="proactive",
        source="longing",
        target=target,
        due_at=time.time() - 1,
        reason="想念",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await h.pulse._intents()
    assert not sent
    assert h.intents.get(row["intent_id"])["status"] == "failed"

    h.pulse.proactive_level = "care"
    for source in ("longing", "serendipity"):
        row = h.intents.schedule(
            kind="proactive",
            source=source,
            target=target,
            due_at=time.time() - 1,
            reason=source,
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        await h.pulse._intents()
        status = h.intents.get(row["intent_id"])["status"]
        if source == "longing":
            assert status == "done"
        else:
            assert status == "failed"
    assert len(sent) == 1

    h.intents.note_received("chat")
    h.pulse.proactive_level = "lively"
    row = h.intents.schedule(
        kind="proactive",
        source="serendipity",
        target=target,
        due_at=time.time() - 1,
        reason="偶遇",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await h.pulse._intents()
    assert h.intents.get(row["intent_id"])["status"] == "done"
    assert len(sent) == 2
    await h.on_unload()


@pytest.mark.asyncio
async def test_proactive_never_leaves_enabled_chat_boundary(tmp_path):
    h = _Harness(tmp_path)
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    h.ctx.call_capability = capability
    row = h.intents.schedule(
        kind="proactive",
        source="longing",
        target="chat:stranger",
        due_at=time.time() - 1,
        reason="陌生人",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await h.pulse._intents()
    assert not sent
    assert h.intents.get(row["intent_id"])["status"] == "pending"

    row2 = h.intents.schedule(
        kind="proactive",
        source="longing",
        target="chat:chat",
        due_at=time.time() - 1,
        reason="空会话",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await h.pulse._intents()
    assert not sent
    assert h.intents.get(row2["intent_id"])["wake_status"] == "unfamiliar_chat"

    h.messages.record(Message.from_event(_event("hi", user="Bob", platform="qq")))
    row3 = h.intents.schedule(
        kind="proactive",
        source="longing",
        target="chat:chat",
        due_at=time.time() - 1,
        reason="打过招呼了",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await h.pulse._intents()
    assert len(sent) == 1
    assert h.intents.get(row3["intent_id"])["status"] == "done"
    await h.on_unload()


@pytest.mark.asyncio
async def test_later_proactive_kind_respects_gate(tmp_path):
    h = _Harness(tmp_path)
    h.messages.record(Message.from_event(_event("hi", user="Bob", platform="qq")))
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    h.ctx.call_capability = capability
    now = time.time()
    result = h.intents.schedule(
        kind="proactive",
        source="later",
        target="chat:chat",
        due_at=now + 30,
        reason="想跟对方分享刚看到的",
        payload={"platform": "qq"},
    )
    h.intents.clock = lambda: now + 31
    await h.pulse._intents()
    assert len(sent) == 1
    assert sent[0]["metadata"]["kind"] == "proactive"
    assert h.intents.get(result["intent_id"])["status"] == "done"
    await h.on_unload()


@pytest.mark.asyncio
async def test_thread_later_schedules_silent_and_pulse_advances_thread(tmp_path):
    h = _Harness(tmp_path)
    calls = iter([1, 2])

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            n = next(calls)
            if n == 1:
                return {
                    "success": True,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "thread_later",
                                "arguments": '{"in_seconds":60,"next_step":"去下一站"}',
                            },
                        }
                    ],
                }
            return {"success": True, "response": "第二站走到了", "tool_calls": []}
        raise AssertionError(name)

    h.ctx.call_capability = capability
    scout = h.profiles.get("scout")
    now = time.time()
    h.intents.clock = lambda: now
    started = await h.engine.start(
        scout,
        "规划一次镰仓旅行",
        chat_id="chat",
        platform="qq",
        user_id="",
        wait_seconds=5,
    )
    thread_id = started["thread_id"]
    rows = h.intents.list("chat:chat")
    assert len(rows) == 1 and rows[0]["kind"] == "silent"
    assert rows[0]["thread_id"] == thread_id

    h.intents.clock = lambda: now + 61
    await h.pulse._intents()
    assert h.intents.get(rows[0]["intent_id"])["status"] == "done"
    await asyncio.sleep(0.05)
    row = h.threads.get(thread_id, "chat")
    assert "第二站" in str(row["result"]) or "去下一站" in str(
        [e for e in row["history"] if e.get("role") == "user"]
    )
    await h.on_unload()
