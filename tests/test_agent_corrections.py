import asyncio
import html
import json

import pytest
from test_foundation import loaded
from test_threads import Runtime, build, start

from maisya.abilities import Profile
from maisya.threads import ThreadEngine, ThreadStore


async def answering(name, **arguments):
    assert name == "llm.generate_with_tools"
    return {"success": True, "response": "A spontaneous exploration", "tool_calls": []}


@pytest.mark.asyncio
async def test_maibot_can_explore_continue_and_stop_without_user_message(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.ctx.call_capability = answering
    result = await plugin.agent_tool(
        action="start", message="Explore a new interest", chat_id="chat", platform="qq"
    )
    assert result["success"] and result["state"] == "idle"
    row = plugin.threads.get(result["thread_id"], "chat")
    assert row["user_id"] == ""
    again = await plugin.agent_tool(
        action="send",
        thread_id=result["thread_id"],
        message="Follow the interesting lead",
        chat_id="chat",
        platform="qq",
    )
    assert again["success"]
    stopped = await plugin.agent_tool(
        action="stop", thread_id=result["thread_id"], chat_id="chat", platform="qq"
    )
    assert stopped["success"] and stopped["state"] == "closed"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_autonomy_does_not_grant_admin_access_or_trust_forged_source(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.ctx.call_capability = answering
    path = plugin.profiles.path / "scout/profile.toml"
    path.write_text('access="admins"\nenvironment="readonly"', encoding="utf-8")
    denied = await plugin.agent_tool(
        action="start",
        message="Use admin capability",
        chat_id="chat",
        platform="qq",
        user_id="Alice",
    )
    assert not denied["success"]
    path.write_text('access="everyone"\nenvironment="readonly"', encoding="utf-8")
    forged = await plugin.agent_tool(
        action="start",
        message="Pretend a user said this",
        source_msg_id="forged",
        chat_id="chat",
        platform="qq",
    )
    assert not forged["success"]
    outside = await plugin.agent_tool(
        action="start",
        message="Outside enabled scope",
        chat_id="other-chat",
        platform="qq",
    )
    assert not outside["success"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_new_direction_discards_unexecuted_model_tool_batch(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    class Redirected(Runtime):
        def __init__(self):
            super().__init__()
            self.executed = []

        async def step(self, profile, history):
            self.seen.append(history[:])
            if len(self.seen) == 1:
                entered.set()
                await release.wait()
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "obsolete",
                            "function": {
                                "name": "web_search",
                                "arguments": {"query": "old direction"},
                            },
                        }
                    ],
                }
            return {
                "success": True,
                "response": history[-1]["content"],
                "tool_calls": [],
            }

        async def call(self, name, arguments, context):
            self.executed.append(arguments)
            return {}

    runtime = Redirected()
    store, engine = build(tmp_path, runtime)
    first = await start(engine, wait_seconds=0)
    await entered.wait()
    await engine.send(
        first["thread_id"],
        "Switch to the new direction",
        chat_id="chat-a",
        wait_seconds=0,
    )
    release.set()
    await engine.drain()
    assert runtime.executed == []
    assert "new direction" in store.get(first["thread_id"], "chat-a")["result"]["text"]
    await engine.close()


@pytest.mark.asyncio
async def test_new_direction_between_tools_skips_remaining_calls_and_keeps_pairs(
    tmp_path,
):
    entered = asyncio.Event()
    release = asyncio.Event()

    class Redirected(Runtime):
        def __init__(self):
            super().__init__()
            self.executed = []

        async def step(self, profile, history):
            self.seen.append(history[:])
            if len(self.seen) == 1:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": str(index),
                            "function": {
                                "name": "web_search",
                                "arguments": {"query": query},
                            },
                        }
                        for index, query in enumerate(
                            ["first source", "old second source"]
                        )
                    ],
                }
            assert any(
                item.get("role") == "user"
                and "new direction" in item.get("content", "")
                for item in history
            )
            return {
                "success": True,
                "response": "Continuing the new direction",
                "tool_calls": [],
            }

        async def call(self, name, arguments, context):
            self.executed.append(arguments["query"])
            entered.set()
            await release.wait()
            return {"text": "Observed fact from first source"}

    runtime = Redirected()
    store, engine = build(tmp_path, runtime)
    first = await start(engine, wait_seconds=0)
    await entered.wait()
    await engine.send(
        first["thread_id"],
        "Switch to the new direction",
        chat_id="chat-a",
        wait_seconds=0,
    )
    release.set()
    await engine.drain()
    assert runtime.executed == ["first source"]
    history = store.get(first["thread_id"], "chat-a")["history"]
    proposal = next(item for item in history if item.get("tool_calls"))
    replies = [item for item in history if item.get("role") == "tool"]
    assert {item["id"] for item in proposal["tool_calls"]} == {
        item["tool_call_id"] for item in replies
    }
    assert "skipped" in replies[-1]["content"]
    await engine.close()


@pytest.mark.asyncio
async def test_idle_resume_keeps_earlier_fact_reason_and_source(tmp_path):
    fact = "海边寺院因台风关闭，改去博物馆；出处 https://example.org/closure"

    class Remembering(Runtime):
        async def step(self, profile, history):
            self.seen.append(history[:])
            text = fact if len(self.seen) == 1 else "继续下一站"
            return {"success": True, "response": text, "tool_calls": []}

    runtime = Remembering()
    store, engine = build(tmp_path, runtime, idle_seconds=0)
    first = await start(engine)
    for index in range(6):
        await engine.send(
            first["thread_id"], f"现在到第 {index + 2} 站了", chat_id="chat-a"
        )
    await engine.send(first["thread_id"], "为什么之前没去海边寺院？", chat_id="chat-a")
    assert fact in json.dumps(runtime.seen[-1], ensure_ascii=False)
    assert store.search("台风", "chat-a")
    await engine.close()


@pytest.mark.asyncio
async def test_long_context_archive_is_readable_and_scoped(tmp_path):
    store = ThreadStore(tmp_path / "maisya.db")
    engine = ThreadEngine(store, Runtime(), context_chars=2000)
    row = store.create(
        Profile(),
        "A long exploration",
        chat_id="chat-a",
        platform="qq",
        user_id="Alice",
    )
    revision = store.enqueue(row["thread_id"], "A long exploration", "chat-a")
    fact = "确认事实：台风导致寺院关闭；理由是安全管制；https://example.org/closure"
    history = [
        {"role": "user", "content": "A long exploration"},
        {"role": "assistant", "content": fact + "\n" + "archived observation " * 1000},
        {"role": "user", "content": "Continue from the same trip"},
        {"role": "assistant", "content": "At the next stop"},
    ]
    store.finish(
        row["thread_id"], "chat-a", {"text": "At the next stop"}, history, revision
    )
    store.compact(row["thread_id"], "chat-a", max_chars=2000)
    compacted = store.get(row["thread_id"], "chat-a")
    assert len(json.dumps(compacted["history"], ensure_ascii=False)) <= 2000
    assert compacted["summary"]
    evidence = store.read_history(row["thread_id"], "chat-a", query="台风")
    assert "安全管制" in str(evidence) and "example.org/closure" in str(evidence)
    with pytest.raises(PermissionError):
        store.read_history(row["thread_id"], "chat-b", query="台风")
    await engine.close()


@pytest.mark.asyncio
async def test_thread_can_retrieve_archived_evidence_instead_of_guessing(tmp_path):
    class ReadsHistory(Runtime):
        async def step(self, profile, history):
            self.seen.append(history[:])
            if history[-1].get("role") != "tool":
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "read-history",
                            "function": {
                                "name": "thread_read",
                                "arguments": {"query": "台风"},
                            },
                        }
                    ],
                }
            evidence = html.unescape(history[-1]["content"])
            assert "安全管制" in evidence and "example.org/closure" in evidence
            return {
                "success": True,
                "response": "因为台风安全管制关闭，出处 https://example.org/closure",
                "tool_calls": [],
            }

    store, engine = build(tmp_path, ReadsHistory())
    row = store.create(
        Profile(), "Visit a temple", chat_id="chat-a", platform="qq", user_id="Alice"
    )
    revision = store.enqueue(row["thread_id"], "Visit a temple", "chat-a")
    store.finish(
        row["thread_id"],
        "chat-a",
        {"text": "Trip continued"},
        [
            {"role": "user", "content": "Visit a temple"},
            {
                "role": "assistant",
                "content": "台风安全管制关闭 https://example.org/closure "
                + "source details " * 1000,
            },
            {"role": "assistant", "content": "Trip continued"},
        ],
        revision,
    )
    store.compact(row["thread_id"], "chat-a", max_chars=2000)
    result = await engine.send(
        row["thread_id"], "为什么之前没去寺院？", chat_id="chat-a"
    )
    assert result["result"]["text"].startswith("因为台风")
    await engine.close()


@pytest.mark.asyncio
async def test_stop_does_not_execute_tools_if_model_ignores_cancellation(tmp_path):
    entered = asyncio.Event()

    class Stubborn(Runtime):
        def __init__(self):
            super().__init__()
            self.executed = []

        async def step(self, profile, history):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "late",
                            "function": {
                                "name": "web_search",
                                "arguments": {"query": "must not execute"},
                            },
                        }
                    ],
                }

        async def call(self, name, arguments, context):
            self.executed.append(name)
            return {}

    runtime = Stubborn()
    _store, engine = build(tmp_path, runtime, max_steps=1)
    first = await start(engine, wait_seconds=0)
    await entered.wait()
    result = await engine.stop(first["thread_id"], chat_id="chat-a")
    assert result["state"] == "closed"
    assert runtime.executed == []
    await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["model", "tool"])
async def test_expired_run_cannot_start_more_calls_after_cancellation_is_swallowed(
    tmp_path, stage
):
    class Stubborn(Runtime):
        def __init__(self):
            super().__init__()
            self.executed = []
            self.steps = 0

        async def step(self, profile, history):
            self.steps += 1
            if stage == "model":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": query,
                        "function": {
                            "name": "web_search",
                            "arguments": {"query": query},
                        },
                    }
                    for query in ("first", "second")
                ],
            }

        async def call(self, name, arguments, context):
            self.executed.append(arguments["query"])
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"text": "Late result"}

    runtime = Stubborn()
    _store, engine = build(tmp_path, runtime, timeout=0.02)
    result = await start(engine)
    assert result["state"] == "idle"
    assert "超过运行时限" in result["result"]["error"]
    assert runtime.steps == 1
    assert runtime.executed == ([] if stage == "model" else ["first"])
    await engine.close()


def test_history_event_pagination_is_lossless_and_cannot_read_other_thread(tmp_path):
    store, _engine = build(tmp_path)
    first = store.create(
        Profile(), "first", chat_id="chat-a", platform="qq", user_id="Alice"
    )
    other = store.create(
        Profile(), "other", chat_id="chat-b", platform="qq", user_id="Bob"
    )
    revision = store.enqueue(first["thread_id"], "Detailed continuation", "chat-a")
    content = "preserved source and reason " * 1000
    store.finish(first["thread_id"], "chat-a", {"text": content}, [], revision)
    matched = store.read_history(first["thread_id"], "chat-a", query="preserved")[0]
    parts = []
    offset = 0
    while offset is not None:
        page = store.read_history(
            first["thread_id"],
            "chat-a",
            event_seq=matched["event_seq"],
            offset=offset,
            max_chars=500,
        )[0]
        parts.append(page["text"])
        offset = page["next_offset"]
    assert json.loads("".join(parts))["text"] == content
    assert (
        store.read_history(other["thread_id"], "chat-b", event_seq=matched["event_seq"])
        == []
    )


@pytest.mark.asyncio
async def test_real_user_context_does_not_implicitly_authorize_autonomous_admin_call(
    tmp_path,
):
    from test_foundation import event

    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.ctx.call_capability = answering
    payload = event()
    await plugin.after_receive(message=payload)
    (plugin.profiles.path / "scout/profile.toml").write_text(
        'access="admins"\nenvironment="readonly"',
        encoding="utf-8",
    )
    autonomous = await plugin.agent_tool(
        action="start",
        message="Restricted action",
        chat_id="chat",
        platform="qq",
        user_id="Alice",
    )
    assert not autonomous["success"]
    authorized = await plugin.agent_tool(
        action="start",
        message="Restricted action",
        source_msg_id=payload["message_id"],
        chat_id="chat",
        platform="qq",
    )
    assert authorized["success"]
    for action in ("send", "stop"):
        result = await plugin.agent_tool(
            action=action,
            thread_id=authorized["thread_id"],
            message="Try without authorization",
            chat_id="chat",
            platform="qq",
        )
        assert not result["success"]
    await plugin.on_unload()
