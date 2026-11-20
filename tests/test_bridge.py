import asyncio
from types import SimpleNamespace

import pytest
from test_foundation import event, loaded
from test_threads import Runtime, build

from maisya.abilities import Profile
from maisya.bridge import Bridge
from maisya.pulse import Pulse


def setup_bridge(tmp_path):
    clock = SimpleNamespace(now=1000.0)
    store, engine = build(tmp_path)
    bridge = Bridge(store, clock=lambda: clock.now)
    calls = []

    async def capability(name, **arguments):
        calls.append((name, arguments))
        assert name == "maisaka.proactive.trigger"
        return {"success": True, "queued": True}

    pulse = Pulse(bridge, SimpleNamespace(call_capability=capability), lambda *_: True)
    return clock, store, engine, bridge, pulse, calls


def test_legacy_delivery_receipt_survives_destination_schema_migration(tmp_path):
    from maisya.threads import ThreadStore

    store = ThreadStore(tmp_path / "state.db")
    row = store.create(Profile(), "legacy", chat_id="chat-a", platform="qq", user_id="")
    with store.connect() as db, db:
        db.execute(
            "CREATE TABLE result_deliveries (thread_id TEXT,revision INTEGER,route TEXT,status TEXT,message_id TEXT,at REAL,PRIMARY KEY(thread_id,revision))"
        )
        db.execute(
            'INSERT INTO result_deliveries VALUES (?,0,"share","sent","legacy-message",1)',
            (row["thread_id"],),
        )
    bridge = Bridge(store)
    assert bridge.delivery(row["thread_id"], 0)["message_id"] == "legacy-message"
    assert bridge.delivery(row["thread_id"], 0, "other") is None
    Bridge(store)
    assert bridge.delivery(row["thread_id"], 0)["status"] == "sent"


async def dispatch(
    engine, bridge, *, source="real-user-message", wait=0, message="Explore"
):
    route = await bridge.route("chat-a", source, wait)
    try:
        result = await engine.start(
            Profile(),
            message,
            chat_id="chat-a",
            platform="qq",
            user_id="Alice",
            wait_seconds=wait,
            return_route=route,
        )
        if result["state"] in {"idle", "waiting"}:
            await bridge.inline(result["thread_id"], "chat-a")
        return result
    finally:
        await bridge.submitted(route)


@pytest.mark.asyncio
async def test_inline_result_reaches_only_own_replyer_once_and_never_wakes(tmp_path):
    clock, _store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    result = await dispatch(engine, bridge, wait=1, message="Confirmed temple hours")
    assert result["state"] == "idle"
    assert await bridge.consume("chat-b") == ""
    brief = await bridge.consume("chat-a")
    assert "Confirmed temple hours" in brief
    assert await bridge.consume("chat-a") == ""
    clock.now += 100
    await pulse.tick()
    assert calls == []
    await engine.close()


@pytest.mark.asyncio
async def test_parallel_returns_merge_and_wake_only_original_chat_once(tmp_path):
    clock, _store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    engine.runtime.gate = asyncio.Event()
    await bridge.plan("chat-a", [("real-user-message", 0)] * 2)
    first = await dispatch(engine, bridge, message="Weather")
    second = await dispatch(engine, bridge, message="Temple hours")
    engine.runtime.gate.set()
    await engine.drain()
    clock.now += 2
    await pulse.tick()
    await pulse.tick()
    assert len(calls) == 1
    arguments = calls[0][1]
    assert arguments["stream_id"] == "chat-a"
    assert arguments["metadata"]["kind"] == "return"
    assert first["thread_id"] in arguments["intent"]
    assert second["thread_id"] in arguments["intent"]
    offered = await bridge.offer("chat-a")
    assert len(offered) == 2
    assert "Weather" in await bridge.consume("chat-a")
    assert await bridge.consume("chat-a") == ""
    await engine.close()


@pytest.mark.asyncio
async def test_planned_group_waits_for_remaining_native_sequential_tool_calls(tmp_path):
    clock, _store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    await bridge.plan("chat-a", [("real-user-message", 0)] * 2)
    engine.runtime.gate = asyncio.Event()
    await dispatch(engine, bridge)
    engine.runtime.gate.set()
    await engine.drain()
    clock.now += 2
    await pulse.tick()
    assert calls == []
    await dispatch(engine, bridge, message="Second", wait=0)
    await engine.drain()
    clock.now += 2
    await pulse.tick()
    assert len(calls) == 1
    await engine.close()


@pytest.mark.asyncio
async def test_group_deadline_returns_available_results_without_waiting_forever(
    tmp_path,
):
    clock, _store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    await bridge.plan("chat-a", [("real-user-message", 0)] * 2)
    engine.runtime.gate = asyncio.Event()
    await dispatch(engine, bridge)
    engine.runtime.gate.set()
    await engine.drain()
    clock.now += 20
    await pulse.tick()
    assert len(calls) == 1
    await engine.close()


@pytest.mark.asyncio
async def test_new_direction_and_stop_invalidate_old_return_intents(tmp_path):
    clock, _store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    engine.runtime.gate = asyncio.Event()
    first = await dispatch(engine, bridge)
    engine.runtime.gate.set()
    await engine.drain()
    engine.runtime.gate.clear()
    await engine.send(
        first["thread_id"], "New direction", chat_id="chat-a", wait_seconds=0
    )
    clock.now += 20
    await pulse.tick()
    assert calls == []
    await engine.stop(first["thread_id"], chat_id="chat-a")
    assert await bridge.offer("chat-a") == []
    assert await bridge.consume("chat-a") == ""
    await engine.close()


@pytest.mark.asyncio
async def test_unclaimed_returns_survive_reload_and_claims_are_not_replayed(tmp_path):
    clock, store, engine, bridge, _pulse, calls = setup_bridge(tmp_path)
    engine.runtime.gate = asyncio.Event()
    await dispatch(engine, bridge)
    engine.runtime.gate.set()
    await engine.drain()
    clock.now += 20
    restarted = Bridge(store, clock=lambda: clock.now)

    async def capability(name, **arguments):
        calls.append((name, arguments))
        raise TimeoutError("The host may already have accepted this")

    pulse = Pulse(
        restarted, SimpleNamespace(call_capability=capability), lambda *_: True
    )
    await pulse.tick()
    await pulse.tick()
    assert len(calls) == 1
    again = Bridge(store, clock=lambda: clock.now)
    await Pulse(
        again, SimpleNamespace(call_capability=capability), lambda *_: True
    ).tick()
    assert len(calls) == 1
    assert len(await again.offer("chat-a")) == 1
    await engine.close()


@pytest.mark.asyncio
async def test_autonomous_exploration_never_bypasses_social_gates_by_return(tmp_path):
    clock, _store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    engine.runtime.gate = asyncio.Event()
    await dispatch(engine, bridge, source="")
    engine.runtime.gate.set()
    await engine.drain()
    clock.now += 20
    await pulse.tick()
    assert calls == []
    assert len(await bridge.offer("chat-a")) == 1
    await engine.close()


@pytest.mark.asyncio
async def test_disabled_session_never_wakes_and_declined_offer_stays_searchable(
    tmp_path,
):
    clock, store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    engine.runtime.gate = asyncio.Event()
    first = await dispatch(engine, bridge)
    engine.runtime.gate.set()
    await engine.drain()
    clock.now += 20
    pulse.enabled = lambda *_: False
    await pulse.tick()
    assert calls == []
    await bridge.offer("chat-a")
    await bridge.end_turn("chat-a")
    pulse.enabled = lambda *_: True
    await pulse.tick()
    assert calls == []
    assert store.search(first["thread_id"], "chat-a")
    await engine.close()


@pytest.mark.asyncio
async def test_plugin_injects_facts_only_into_native_replyer_and_preserves_extra(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    source = event()
    await plugin.after_receive(message=source)
    calls = []

    async def capability(name, **arguments):
        calls.append(name)
        assert name == "llm.generate_with_tools"
        return {"success": True, "response": "Museum closes at 18:00", "tool_calls": []}

    plugin.ctx.call_capability = capability
    result = await plugin.agent_tool(
        action="start",
        source_msg_id=source["message_id"],
        message="Check hours",
        chat_id="chat",
        platform="qq",
    )
    assert result["success"]
    skip = await plugin.before_replyer(session_id="chat", request_type="plugin.replyer")
    assert "modified_kwargs" not in skip
    injected = await plugin.before_replyer(
        session_id="chat", request_type="maisaka.replyer", extra_prompt="Keep the style"
    )
    extra = injected["modified_kwargs"]["extra_prompt"]
    assert "Keep the style" in extra and "Museum closes at 18:00" in extra
    assert "modified_kwargs" not in await plugin.before_replyer(
        session_id="chat", request_type="maisaka.replyer"
    )
    assert calls == ["llm.generate_with_tools"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_offered_result_changed_before_replyer_cannot_inject_stale_facts(
    tmp_path,
):
    _clock, _store, engine, bridge, _pulse, _calls = setup_bridge(tmp_path)
    first = await dispatch(engine, bridge, wait=1)
    engine.runtime.gate = asyncio.Event()
    await engine.send(
        first["thread_id"], "New direction", chat_id="chat-a", wait_seconds=0
    )
    assert await bridge.consume("chat-a") == ""
    await engine.stop(first["thread_id"], chat_id="chat-a")
    await engine.close()


@pytest.mark.asyncio
async def test_older_autonomous_results_cannot_starve_a_new_user_return(tmp_path):
    clock, _store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    for _ in range(17):
        await dispatch(engine, bridge, source="", wait=1)
    engine.runtime.gate = asyncio.Event()
    latest = await dispatch(engine, bridge, message="The user is waiting for this")
    engine.runtime.gate.set()
    await engine.drain()
    clock.now += 20
    await pulse.tick()
    assert len(calls) == 1
    assert latest["thread_id"] in calls[0][1]["intent"]
    await engine.close()


@pytest.mark.asyncio
async def test_native_replyer_retry_keeps_same_facts_without_leaking_to_next_reply(
    tmp_path,
):
    plugin = await loaded(tmp_path)

    async def capability(name, **arguments):
        return {"success": True, "response": "Verified fact", "tool_calls": []}

    plugin.ctx.call_capability = capability
    await plugin.agent_tool(
        action="start", message="Check", chat_id="chat", platform="qq"
    )
    first = await plugin.before_replyer(
        session_id="chat",
        request_type="maisaka.replyer",
        attempt=1,
        reply_tool_args={"reply_style": "简短"},
        extra_prompt="Original style",
    )
    modified = first["modified_kwargs"]
    assert modified["reply_tool_args"]["reply_style"] == "简短"
    retry = await plugin.before_replyer(
        session_id="chat",
        request_type="maisaka.replyer",
        attempt=2,
        reply_tool_args=modified["reply_tool_args"],
        extra_prompt="Original style",
    )
    assert retry["modified_kwargs"]["extra_prompt"].count("Verified fact") == 1
    unrelated = await plugin.before_replyer(
        session_id="chat",
        request_type="maisaka.replyer",
        attempt=1,
        reply_tool_args=modified["reply_tool_args"],
    )
    assert "modified_kwargs" not in unrelated
    await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_background_question_or_failure_returns_to_planner(tmp_path, failure):
    clock, _store, engine, bridge, pulse, calls = setup_bridge(tmp_path)

    class NeedsAttention(Runtime):
        async def step(self, profile, history):
            if failure:
                raise RuntimeError("Model unavailable")
            return {
                "tool_calls": [
                    {
                        "function": {
                            "name": "ask_user",
                            "arguments": {
                                "reason": "missing_input",
                                "question": "Please provide the missing source file",
                            },
                        }
                    }
                ]
            }

    engine.runtime = NeedsAttention()
    route = await bridge.route("chat-a", "real-user-message", 0)
    await engine.start(
        Profile(),
        "Read the source file",
        chat_id="chat-a",
        platform="qq",
        user_id="Alice",
        wait_seconds=0,
        return_route=route,
    )
    await bridge.submitted(route)
    await engine.drain()
    clock.now += 20
    await pulse.tick()
    assert len(calls) == 1
    assert ("error" if failure else "question") in calls[0][1]["intent"]
    await engine.close()


@pytest.mark.asyncio
async def test_native_after_response_reserves_group_before_thread_tools_run(tmp_path):
    plugin = await loaded(tmp_path)
    source = event()
    await plugin.after_receive(message=source)
    gate = asyncio.Event()
    calls = []

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            await gate.wait()
            return {"success": True, "response": "Confirmed", "tool_calls": []}
        calls.append((name, arguments))
        return {"success": True, "queued": True}

    plugin.ctx.call_capability = capability
    await plugin.after_planner(
        session_id="chat",
        output_items=[
            {
                "tool_call": {
                    "func_name": "maisya_agent",
                    "args": {
                        "action": "start",
                        "source_msg_id": source["message_id"],
                        "wait_seconds": 0,
                    },
                }
            }
        ]
        * 2,
    )
    ids = []
    for _ in range(2):
        result = await plugin.agent_tool(
            action="start",
            message="Check",
            wait_seconds=0,
            source_msg_id=source["message_id"],
            chat_id="chat",
            platform="qq",
        )
        assert result["success"]
        ids.append(result["thread_id"])
    gate.set()
    await plugin.engine.drain()
    current_time = plugin.bridge.clock()
    plugin.bridge.clock = lambda: current_time + 2
    await plugin.pulse.tick()
    assert len(calls) == 1
    assert all(thread_id in calls[0][1]["intent"] for thread_id in ids)
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_stopped_thread_cannot_reappear_in_replyer_retry(tmp_path):
    plugin = await loaded(tmp_path)

    async def capability(name, **arguments):
        return {"success": True, "response": "Old direction", "tool_calls": []}

    plugin.ctx.call_capability = capability
    result = await plugin.agent_tool(
        action="start", message="Check", chat_id="chat", platform="qq"
    )
    first = await plugin.before_replyer(
        session_id="chat", request_type="maisaka.replyer", attempt=1
    )
    await plugin.engine.stop(result["thread_id"], chat_id="chat")
    retry = await plugin.before_replyer(
        session_id="chat",
        request_type="maisaka.replyer",
        attempt=2,
        reply_tool_args=first["modified_kwargs"]["reply_tool_args"],
    )
    assert retry["action"] == "continue"
    assert "extra_prompt" not in retry["modified_kwargs"]
    assert retry["modified_kwargs"]["reply_tool_args"]["_maisya_task_token"] == (
        first["modified_kwargs"]["reply_tool_args"]["_maisya_task_token"]
    )
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_stop_between_intent_claim_and_rpc_prevents_wakeup(tmp_path):
    clock, store, engine, bridge, pulse, calls = setup_bridge(tmp_path)
    engine.runtime.gate = asyncio.Event()
    first = await dispatch(engine, bridge)
    engine.runtime.gate.set()
    await engine.drain()
    original_claim = bridge.claim_ready

    def claim_then_stop(enabled):
        claimed = original_claim(enabled)
        store.stop(first["thread_id"], "chat-a")
        return claimed

    bridge.claim_ready = claim_then_stop
    clock.now += 20
    await pulse.tick()
    assert calls == []
    await engine.close()


@pytest.mark.asyncio
async def test_same_native_batch_reply_then_stop_keeps_facts_until_reply_runs(tmp_path):
    plugin = await loaded(tmp_path)

    async def capability(name, **arguments):
        return {"success": True, "response": "Verified fact", "tool_calls": []}

    plugin.ctx.call_capability = capability
    await plugin.agent_tool(
        action="start", message="Check", chat_id="chat", platform="qq"
    )
    await plugin.after_planner(
        session_id="chat",
        output_items=[
            {"tool_call": {"func_name": "reply", "args": {}}},
            {"tool_call": {"func_name": "stop", "args": {}}},
        ],
    )
    actual = await plugin.before_replyer(
        session_id="chat", request_type="maisaka.replyer"
    )
    assert "Verified fact" in actual["modified_kwargs"]["extra_prompt"]
    await plugin.on_unload()
