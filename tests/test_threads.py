import asyncio
import json

import pytest

from maisya.abilities import Profile
from maisya.threads import ThreadEngine, ThreadStore


class Runtime:
    def __init__(self):
        self.seen = []
        self.gate = None

    async def step(self, profile, history):
        self.seen.append(json.loads(json.dumps(history)))
        if self.gate:
            await self.gate.wait()
        return {
            "success": True,
            "response": "answer: " + history[-1]["content"],
            "tool_calls": [],
        }

    async def call(self, name, arguments, context):
        return {"text": "read-only result"}


def build(tmp_path, runtime=None, **options):
    store = ThreadStore(tmp_path / "maisya.db")
    engine = ThreadEngine(store, runtime or Runtime(), **options)
    return store, engine


async def start(engine, **options):
    return await engine.start(
        Profile(),
        "Find Kamakura weather",
        chat_id="chat-a",
        platform="qq",
        user_id="Alice",
        **options,
    )


@pytest.mark.asyncio
async def test_start_continue_recall_and_scope(tmp_path):
    runtime = Runtime()
    store, engine = build(tmp_path, runtime)
    first = await start(engine)
    assert first["state"] == "idle"
    assert "Kamakura" in first["result"]["text"]
    second = await engine.send(
        first["thread_id"], "Also consider rain", chat_id="chat-a"
    )
    assert second["thread_id"] == first["thread_id"]
    assert len(runtime.seen[-1]) >= 3
    assert len(store.search("Kamakura", "chat-a")) == 1
    assert store.search("Kamakura", "chat-b") == []
    with pytest.raises(PermissionError):
        await engine.send(first["thread_id"], "steal", chat_id="chat-b")
    await engine.close()


@pytest.mark.asyncio
async def test_stop_surfaces_unconfirmed_cleanup_in_thread_result(tmp_path):
    class Unclean(Runtime):
        async def stopped(self, thread_id):
            raise RuntimeError("执行容器清理未确认")

    store, engine = build(tmp_path, Unclean())
    row = await start(engine)
    with pytest.raises(RuntimeError, match="清理未确认"):
        await engine.stop(row["thread_id"], chat_id="chat-a")
    stopped = store.get(row["thread_id"], "chat-a")
    assert stopped["state"] == "closed"
    assert "清理未确认" in stopped["result"]["error"]
    await engine.close()


@pytest.mark.asyncio
async def test_timeout_preserves_background_and_later_result(tmp_path):
    runtime = Runtime()
    runtime.gate = asyncio.Event()
    store, engine = build(tmp_path, runtime)
    first = await start(engine, wait_seconds=0.01)
    assert first["state"] == "active"
    runtime.gate.set()
    await engine.drain()
    assert store.get(first["thread_id"], "chat-a")["state"] == "idle"
    await engine.close()


@pytest.mark.asyncio
async def test_input_during_model_call_is_not_lost(tmp_path):
    runtime = Runtime()
    runtime.gate = asyncio.Event()
    store, engine = build(tmp_path, runtime)
    first = await start(engine, wait_seconds=0)
    await asyncio.sleep(0.01)
    added = await engine.send(
        first["thread_id"], "New direction: Enoshima", chat_id="chat-a", wait_seconds=0
    )
    assert added["state"] == "active"
    runtime.gate.set()
    await engine.drain()
    final = store.get(first["thread_id"], "chat-a")
    assert "Enoshima" in final["result"]["text"]
    assert final["completed_revision"] == 2
    await engine.close()


@pytest.mark.asyncio
async def test_stop_prevents_late_result_and_closes_thread(tmp_path):
    runtime = Runtime()
    runtime.gate = asyncio.Event()
    store, engine = build(tmp_path, runtime)
    first = await start(engine, wait_seconds=0)
    stopped = await engine.stop(first["thread_id"], chat_id="chat-a")
    runtime.gate.set()
    await engine.drain()
    assert stopped["state"] == "closed"
    assert store.get(first["thread_id"], "chat-a")["state"] == "closed"
    with pytest.raises(ValueError):
        await engine.send(first["thread_id"], "again", chat_id="chat-a")
    await engine.close()


@pytest.mark.asyncio
async def test_tool_roundtrip_and_waiting_question_bound(tmp_path):
    class Asking(Runtime):
        async def step(self, profile, history):
            if len(self.seen) == 0:
                self.seen.append(history)
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "call1",
                            "func_name": "ask_user",
                            "args": {
                                "question": "Please provide the missing file",
                                "reason": "missing_input",
                            },
                        }
                    ],
                }
            return await super().step(profile, history)

    runtime = Asking()
    _store, engine = build(tmp_path, runtime)
    first = await start(engine)
    assert first["state"] == "waiting"
    assert "missing" in first["result"]["question"]
    second = await engine.send(first["thread_id"], "file supplied", chat_id="chat-a")
    assert second["state"] == "idle"
    assert "file supplied" in second["result"]["text"]
    await engine.close()


@pytest.mark.asyncio
async def test_restart_recovers_interrupted_thread_and_cold_resume(tmp_path):
    runtime = Runtime()
    runtime.gate = asyncio.Event()
    store, engine = build(tmp_path, runtime, idle_seconds=0)
    first = await start(engine, wait_seconds=0)
    await engine.close()
    restarted = ThreadEngine(ThreadStore(store.path), Runtime(), idle_seconds=0)
    await restarted.recover()
    result = await restarted.send(
        first["thread_id"], "Continue the exploration", chat_id="chat-a"
    )
    assert result["state"] == "idle"
    assert "Continue" in result["result"]["text"]
    await restarted.close()


def test_recall_handles_fts_syntax_without_cross_chat_leak(tmp_path):
    store, _engine = build(tmp_path)
    store.create(
        Profile(),
        "private Kamakura notes",
        chat_id="chat-a",
        platform="qq",
        user_id="Alice",
    )
    assert store.search("Kamakura", "chat-b") == []
    assert store.search('" OR * --', "chat-b") == []


@pytest.mark.asyncio
async def test_runtime_failure_is_recorded_and_can_be_continued(tmp_path):
    class Broken(Runtime):
        async def step(self, profile, history):
            raise RuntimeError("model unavailable")

    _store, engine = build(tmp_path, Broken())
    result = await start(engine)
    assert result["state"] == "idle"
    assert result["result"]["error"]
    engine.runtime = Runtime()
    result = await engine.send(result["thread_id"], "try again", chat_id="chat-a")
    assert result["result"]["text"]
    await engine.close()


@pytest.mark.asyncio
async def test_simultaneous_starts_respect_concurrency_limit(tmp_path):
    runtime = Runtime()
    runtime.gate = asyncio.Event()
    _store, engine = build(tmp_path, runtime, max_active=1)
    results = await asyncio.gather(
        *(start(engine, wait_seconds=0) for _ in range(4)), return_exceptions=True
    )
    assert sum(isinstance(item, dict) for item in results) == 1
    assert len(engine.tasks) == 1
    runtime.gate.set()
    await engine.drain()
    await engine.close()


@pytest.mark.asyncio
async def test_failure_during_new_input_does_not_lose_that_input(tmp_path):
    class OnceBroken(Runtime):
        async def step(self, profile, history):
            if not self.seen:
                self.seen.append(history)
                await self.gate.wait()
                raise RuntimeError("first request interrupted")
            self.gate = None
            return await super().step(profile, history)

    runtime = OnceBroken()
    runtime.gate = asyncio.Event()
    store, engine = build(tmp_path, runtime)
    first = await start(engine, wait_seconds=0)
    await asyncio.sleep(0.01)
    await engine.send(
        first["thread_id"], "Keep this new direction", chat_id="chat-a", wait_seconds=0
    )
    runtime.gate.set()
    await engine.drain()
    assert (
        "Keep this new direction"
        in store.get(first["thread_id"], "chat-a")["result"]["text"]
    )
    await engine.close()


@pytest.mark.asyncio
async def test_native_openai_tool_calls_are_paired_and_readonly(tmp_path):
    class Native(Runtime):
        async def step(self, profile, history):
            self.seen.append(history[:])
            if len(self.seen) == 1:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "native-call",
                            "function": {
                                "name": "web_search",
                                "arguments": {"query": "Kamakura"},
                            },
                        }
                    ],
                }
            assert history[-1]["role"] == "tool"
            assert history[-1]["tool_call_id"] == history[-2]["tool_calls"][0]["id"]
            return {"success": True, "response": "Sources checked", "tool_calls": []}

    store, engine = build(tmp_path, Native())
    result = await start(engine)
    assert result["result"]["text"] == "Sources checked"
    history = store.get(result["thread_id"], "chat-a")["history"]
    assert "external_data" in history[-2]["content"]
    await engine.close()


@pytest.mark.asyncio
async def test_second_question_is_not_exposed(tmp_path):
    class RepeatedQuestion(Runtime):
        async def step(self, profile, history):
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": "ask",
                        "func_name": "ask_user",
                        "args": {
                            "question": "Need the input file",
                            "reason": "missing_input",
                        },
                    }
                ],
            }

    store, engine = build(tmp_path, RepeatedQuestion(), max_steps=2)
    first = await start(engine)
    assert first["state"] == "waiting"
    second = await engine.send(first["thread_id"], "Here is the file", chat_id="chat-a")
    assert second["state"] == "idle"
    assert "question" not in second["result"]
    assert store.get(first["thread_id"], "chat-a")["questions"] == 1
    await engine.close()


def test_self_recall_is_available_but_chat_context_is_not_promoted(tmp_path):
    store, _engine = build(tmp_path)
    store.create(
        Profile(),
        "Kamakura self experience",
        chat_id="self",
        platform="",
        user_id="",
        owner="self",
    )
    store.create(
        Profile(),
        "Kamakura private preference",
        chat_id="chat-a",
        platform="qq",
        user_id="Alice",
    )
    assert len(store.search("Kamakura", "chat-a")) == 2
    assert len(store.search("Kamakura", "chat-b")) == 1
    assert store.search("Kamakura", "chat-b")[0]["owner"] == "self"


def test_chinese_substrings_and_exact_thread_id_are_searchable(tmp_path):
    store, _engine = build(tmp_path)
    row = store.create(
        Profile(),
        "今天在镰仓海边散步",
        chat_id="chat-a",
        platform="qq",
        user_id="Alice",
    )
    assert store.search("镰仓", "chat-a")[0]["thread_id"] == row["thread_id"]
    assert store.search(row["thread_id"], "chat-a")[0]["thread_id"] == row["thread_id"]
    assert store.search("镰仓", "chat-b") == []


@pytest.mark.asyncio
async def test_model_timeout_finishes_with_recoverable_error(tmp_path):
    runtime = Runtime()
    runtime.gate = asyncio.Event()
    _store, engine = build(tmp_path, runtime, timeout=0.02)
    result = await start(engine)
    assert result["state"] == "idle"
    assert result["result"]["error"]
    await engine.close()
