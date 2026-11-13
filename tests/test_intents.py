import asyncio
import time

import pytest
from test_foundation import loaded


@pytest.mark.asyncio
async def test_later_schedules_reminder_and_pulse_wakes_original_chat(tmp_path):
    plugin = await loaded(tmp_path)
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    result = await plugin.later_tool(
        action="schedule",
        in_seconds=30,
        reason="问问她奶茶到了没",
        chat_id="chat",
        platform="qq",
    )
    assert result["success"] and result["intent_id"].startswith("in_")
    plugin.intents.clock = lambda: time.time() + 31
    await plugin.pulse._intents()
    assert len(sent) == 1
    assert sent[0]["stream_id"] == "chat"
    assert "奶茶" in sent[0]["intent"]
    assert sent[0]["metadata"]["kind"] == "return"
    row = plugin.intents.get(result["intent_id"])
    assert row["status"] == "done" and row["wake_status"] == "accepted"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_proactive_intent_respects_unreplied_lock_and_sleep(tmp_path):
    plugin = await loaded(tmp_path)
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    target = "chat:chat"
    ok = plugin.intents.schedule(
        kind="proactive",
        source="afterglow",
        target=target,
        due_at=time.time() - 1,
        reason="刚才聊到一半的新梗",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    plugin.intents.mark_proactive(target)
    await plugin.pulse._intents()
    assert not sent
    row = plugin.intents.get(ok["intent_id"])
    assert row["status"] == "failed" and row["wake_status"] == "unreplied_lock"

    plugin.intents.note_received("chat")
    from test_foundation import event

    from maisya.trust import Message

    plugin.messages.record(Message.from_event(event("hi", user="Bob", platform="qq")))
    again = plugin.intents.schedule(
        kind="proactive",
        source="longing",
        target=target,
        due_at=time.time() - 1,
        reason="好久没说早安",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    plugin.pulse.sleep_window = "2-7"
    plugin.intents.clock = lambda: 1795202400  # 2026-11-21 03:20 Asia/Shanghai
    await plugin.pulse._intents()
    assert not sent
    row = plugin.intents.get(again["intent_id"])
    assert row["status"] == "pending" and row["due_at"] > 1795202400
    plugin.pulse.sleep_window = ""
    plugin.intents.clock = time.time
    with plugin.threads.connect() as db, db:
        db.execute(
            "UPDATE intents SET due_at=? WHERE intent_id=?",
            (time.time() - 1, again["intent_id"]),
        )
    await plugin.pulse._intents()
    assert len(sent) == 1 and sent[0]["metadata"]["kind"] == "proactive"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_later_list_and_cancel_and_quota(tmp_path):
    plugin = await loaded(tmp_path)
    first = await plugin.later_tool(
        action="schedule",
        in_seconds=60,
        reason="回访上次的决定",
        chat_id="chat",
        platform="qq",
    )
    second = await plugin.later_tool(
        action="schedule",
        in_seconds=120,
        reason="再回访一次",
        chat_id="chat",
        platform="qq",
    )
    listed = await plugin.later_tool(action="list", chat_id="chat", platform="qq")
    assert {item["intent_id"] for item in listed["intents"]} == {
        first["intent_id"],
        second["intent_id"],
    }
    cancelled = await plugin.later_tool(
        action="cancel", intent_id=first["intent_id"], chat_id="chat", platform="qq"
    )
    assert cancelled["status"] == "cancelled"

    casual = plugin.intents.schedule(
        kind="proactive",
        source="afterglow",
        target="chat:chat",
        due_at=time.time() - 1,
        reason="闲聊惦记",
        payload={"platform": "qq"},
    )
    with pytest.raises(ValueError):
        plugin.intents.schedule(
            kind="proactive",
            source="afterglow",
            target="chat:chat",
            due_at=time.time() - 1,
            reason="当天第二条闲聊惦记",
            payload={"platform": "qq"},
        )
    plugin.intents.cancel(casual["intent_id"], "chat:chat")
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_silent_intent_requeues_when_thread_busy_and_clears_when_done(tmp_path):
    plugin = await loaded(tmp_path)

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "站到了。", "tool_calls": []}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    started = await plugin.agent_tool(
        action="start",
        message="去镰仓走一圈",
        chat_id="chat",
        platform="qq",
        wait_seconds=5,
    )
    row = plugin.threads.get(started["thread_id"], "chat")
    intent = plugin.intents.schedule(
        kind="silent",
        source="thread",
        target="chat:chat",
        due_at=time.time() - 1,
        reason="下一站",
        thread_id=row["thread_id"],
    )
    await plugin.pulse._intents()
    assert plugin.intents.get(intent["intent_id"])["status"] == "done"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_proactive_level_gates_sources(tmp_path):
    plugin = await loaded(tmp_path)
    plugin.intents.note_received("chat")
    from test_foundation import event

    from maisya.trust import Message

    plugin.messages.record(Message.from_event(event("hi", user="Bob", platform="qq")))
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    target = "chat:chat"
    # quiet blocks everything
    plugin.pulse.proactive_level = "quiet"
    row = plugin.intents.schedule(
        kind="proactive",
        source="longing",
        target=target,
        due_at=time.time() - 1,
        reason="想念",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await plugin.pulse._intents()
    assert not sent
    assert plugin.intents.get(row["intent_id"])["status"] == "failed"

    # care allows longing but caps serendipity
    plugin.pulse.proactive_level = "care"
    for source in ("longing", "serendipity"):
        row = plugin.intents.schedule(
            kind="proactive",
            source=source,
            target=target,
            due_at=time.time() - 1,
            reason=source,
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        await plugin.pulse._intents()
        status = plugin.intents.get(row["intent_id"])["status"]
        if source == "longing":
            assert status == "done"
        else:
            assert status == "failed"
    assert len(sent) == 1

    # lively allows serendipity
    plugin.intents.note_received("chat")
    plugin.pulse.proactive_level = "lively"
    row = plugin.intents.schedule(
        kind="proactive",
        source="serendipity",
        target=target,
        due_at=time.time() - 1,
        reason="偶遇",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await plugin.pulse._intents()
    assert plugin.intents.get(row["intent_id"])["status"] == "done"
    assert len(sent) == 2
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_proactive_never_leaves_enabled_chat_boundary(tmp_path):
    plugin = await loaded(tmp_path)
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    row = plugin.intents.schedule(
        kind="proactive",
        source="longing",
        target="chat:stranger",
        due_at=time.time() - 1,
        reason="陌生人",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await plugin.pulse._intents()
    assert not sent
    # non-enabled chat intent is left pending for later enablement, not claimed
    assert plugin.intents.get(row["intent_id"])["status"] == "pending"

    # enabled chat without any real human message is unfamiliar and fails
    row2 = plugin.intents.schedule(
        kind="proactive",
        source="longing",
        target="chat:chat",
        due_at=time.time() - 1,
        reason="空会话",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await plugin.pulse._intents()
    assert not sent
    assert plugin.intents.get(row2["intent_id"])["wake_status"] == "unfamiliar_chat"

    # a real human message makes it familiar
    from test_foundation import event

    from maisya.trust import Message

    plugin.messages.record(Message.from_event(event("hi", user="Bob", platform="qq")))
    row3 = plugin.intents.schedule(
        kind="proactive",
        source="longing",
        target="chat:chat",
        due_at=time.time() - 1,
        reason="打过招呼了",
        payload={"platform": "qq"},
        bypass_limits=True,
    )
    await plugin.pulse._intents()
    assert len(sent) == 1
    assert plugin.intents.get(row3["intent_id"])["status"] == "done"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_later_proactive_kind_respects_gate(tmp_path):
    plugin = await loaded(tmp_path)
    from test_foundation import event

    from maisya.trust import Message

    plugin.messages.record(Message.from_event(event("hi", user="Bob", platform="qq")))
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    result = await plugin.later_tool(
        action="schedule",
        kind="proactive",
        in_seconds=30,
        reason="想跟她分享刚看到的",
        chat_id="chat",
        platform="qq",
    )
    assert result["success"]
    plugin.intents.clock = lambda: time.time() + 31
    await plugin.pulse._intents()
    assert len(sent) == 1
    assert sent[0]["metadata"]["kind"] == "proactive"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_thread_later_schedules_silent_and_pulse_advances_thread(tmp_path):
    plugin = await loaded(tmp_path)
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

    plugin.ctx.call_capability = capability
    started = await plugin.agent_tool(
        action="start",
        message="规划一次镰仓旅行",
        chat_id="chat",
        platform="qq",
        wait_seconds=5,
    )
    thread_id = started["thread_id"]
    rows = plugin.intents.list("chat:chat")
    assert len(rows) == 1 and rows[0]["kind"] == "silent"
    assert rows[0]["thread_id"] == thread_id

    plugin.intents.clock = lambda: time.time() + 61
    await plugin.pulse._intents()
    assert plugin.intents.get(rows[0]["intent_id"])["status"] == "done"
    await asyncio.sleep(0.05)
    row = plugin.threads.get(thread_id, "chat")
    assert "第二站" in str(row["result"]) or "去下一站" in str(
        [e for e in row["history"] if e.get("role") == "user"]
    )
    await plugin.on_unload()
