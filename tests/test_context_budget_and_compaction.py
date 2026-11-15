import json
import time

import pytest
from test_foundation import loaded

from maisya.abilities import Profile
from maisya.context_budget import (
    DEFAULT_CONTEXT_TOKENS,
    estimate_message_tokens,
    estimate_tokens,
    estimate_tools_tokens,
    resolve_context_budget,
)
from maisya.threads import ThreadEngine, ThreadStore, encode


def test_hybrid_token_estimator_cjk_ascii_and_tool_schemas():
    assert estimate_tokens("") == 0
    # ASCII: ceil(bytes / 4)
    assert estimate_tokens("a" * 400) == 100
    # CJK dense chars: ~1 token each
    assert estimate_tokens("测试汉字" * 25) == 100
    # Mixed text: CJK + ASCII
    mixed = "你好世界 " + ("abcd" * 10)  # 4 CJK + (1 + 40 = 41 ascii bytes -> ceil(41/4)=11)
    assert estimate_tokens(mixed) == 15

    msg_with_calls = {
        "role": "assistant",
        "content": "开始查证",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": json.dumps({"query": "Maisya context window"}, ensure_ascii=False),
                },
            }
        ],
    }
    msg_tokens = estimate_message_tokens(msg_with_calls)
    assert msg_tokens > estimate_tokens("开始查证")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search public web pages",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]
    assert estimate_tools_tokens(tools) > 10


def test_model_window_resolution_default_256k_family_clamp_and_profile_override():
    catalog = {
        "models": [
            {"name": "custom-unlisted", "identifier": "acme/my-private-model-v1", "provider": "Acme"},
            {"name": "work-gpt4o", "identifier": "openai/gpt-4o", "provider": "OpenAI"},
            {"name": "work-sonnet", "identifier": "anthropic/claude-3-5-sonnet", "provider": "Anthropic"},
            {"name": "big-gemini", "identifier": "google/gemini-2.5-pro", "provider": "Google"},
        ],
        "tasks": {
            "utils": ["custom-unlisted"],
            "planner": ["work-gpt4o"],
        },
    }

    # Unknown/custom model resolves to 262144 (256K) by default
    p_custom = Profile(task="utils")
    b_custom = resolve_context_budget(p_custom, default_context_tokens=DEFAULT_CONTEXT_TOKENS, model_catalog=catalog)
    assert b_custom["window_tokens"] == 262_144
    assert b_custom["source"] == "default"
    assert b_custom["trigger_tokens"] == int((262_144 - 4096) * 0.75)
    assert b_custom["prune_tokens"] == int((262_144 - 4096) * 0.55)

    # Known smaller model clamps down below 256K to prevent 400 overflow
    p_gpt4o = Profile(task="planner", model_name="work-gpt4o")
    b_gpt4o = resolve_context_budget(p_gpt4o, default_context_tokens=262_144, model_catalog=catalog)
    assert b_gpt4o["window_tokens"] == 128_000
    assert b_gpt4o["source"] == "model_family"
    assert b_gpt4o["trigger_tokens"] < 128_000

    p_claude = Profile(model="work-sonnet")
    b_claude = resolve_context_budget(p_claude, default_context_tokens=262_144, model_catalog=catalog)
    assert b_claude["window_tokens"] == 200_000
    assert b_claude["source"] == "model_family"

    # Larger family (1M) respects default_context_tokens=256K unless profile explicitly overrides
    p_gemini = Profile(model="big-gemini")
    b_gemini = resolve_context_budget(p_gemini, default_context_tokens=262_144, model_catalog=catalog)
    assert b_gemini["window_tokens"] == 262_144

    # Profile context_tokens overrides both family and global default
    p_override = Profile(model="big-gemini", context_tokens=524_288)
    b_override = resolve_context_budget(p_override, default_context_tokens=262_144, model_catalog=catalog)
    assert b_override["window_tokens"] == 524_288
    assert b_override["source"] == "profile"


@pytest.mark.asyncio
async def test_idle_resume_does_not_forcibly_chop_history_under_token_budget(tmp_path):
    class TrackingRuntime:
        def __init__(self):
            self.seen = []

        async def step(self, profile, history):
            self.seen.append(json.loads(json.dumps(history)))
            return {"success": True, "response": "continued cleanly", "tool_calls": []}

    store = ThreadStore(tmp_path / "maisya.db")
    runtime = TrackingRuntime()
    engine = ThreadEngine(store, runtime, idle_seconds=0, context_tokens=262_144)

    row = store.create(Profile(), "Initial prompt", chat_id="chat-a", platform="qq", user_id="Alice")
    rev = store.enqueue(row["thread_id"], "Initial prompt", "chat-a")
    middle_fact = "MIDDLE_FACT_KEEP_INTACT_12345"
    # ~60K ASCII JSON chars (~15K tokens) — previously chopped at 48K chars whenever idle_seconds elapsed!
    big_history = [
        {"role": "user", "content": "Initial prompt"},
        {"role": "assistant", "content": middle_fact + " " + ("observation_token " * 1500)},
        {"role": "user", "content": "Second step"},
        {"role": "assistant", "content": "Step two " + ("detail_token " * 1500)},
    ]
    store.finish(row["thread_id"], "chat-a", {"text": "Step two"}, big_history, rev)

    # Backdate updated timestamp far past idle_seconds
    with store.connect() as db, db:
        db.execute("UPDATE agent_threads SET updated=? WHERE thread_id=?", (time.time() - 7200, row["thread_id"]))

    resumed = await engine.send(row["thread_id"], "Resume without chopping", chat_id="chat-a")
    assert resumed["state"] == "idle"
    after = store.get(row["thread_id"], "chat-a")
    assert after["summary"] == ""
    assert middle_fact in json.dumps(runtime.seen[-1], ensure_ascii=False)
    await engine.close()


@pytest.mark.asyncio
async def test_tier1_proactive_prune_dedups_and_folds_old_tool_outputs_with_hysteresis_and_lossless_read(tmp_path):
    store = ThreadStore(tmp_path / "maisya.db")
    row = store.create(Profile(), "Inspect docs", chat_id="chat-a", platform="qq", user_id="Alice")
    tid = row["thread_id"]
    rev = store.enqueue(tid, "Inspect docs", "chat-a")

    unique_tail_marker = "UNIQUE_EXACT_NEEDLE_98765"
    dup_body = "<external_data>" + ("repeated_payload_chunk " * 300) + "</external_data>"
    huge_old_body = "<external_data>HEAD_MARKER " + ("middle_archive_body " * 900) + f" {unique_tail_marker}</external_data>"

    # First test hysteresis: small savings (< 4096 tokens) -> no-op
    small_history = [
        {"role": "user", "content": "Inspect docs"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "web_fetch", "arguments": '{"url":"https://a.example"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "short result " * 350},  # ~4550 chars (~1100 tokens < 4096)
        {"role": "user", "content": "Latest ask"},
    ]
    store.checkpoint(tid, small_history, rev, "tools", {})
    changed_small = store.prune_history(tid, "chat-a", tail_tokens=200, min_prune_chars=4000, min_reclaim_tokens=4096)
    assert changed_small is False

    # Now build history with duplicate tool output + huge old tool output (> 4096 reclaimable tokens) + protected tail
    full_history = [
        {"role": "user", "content": "Inspect docs"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_dup_old", "type": "function", "function": {"name": "web_fetch", "arguments": '{"url":"https://dup.example"}'}}]},
        {"role": "tool", "tool_call_id": "call_dup_old", "content": dup_body},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_huge", "type": "function", "function": {"name": "web_fetch", "arguments": '{"url":"https://huge.example"}'}}]},
        {"role": "tool", "tool_call_id": "call_huge", "content": huge_old_body},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_dup_new", "type": "function", "function": {"name": "web_fetch", "arguments": '{"url":"https://dup.example"}'}}]},
        {"role": "tool", "tool_call_id": "call_dup_new", "content": dup_body},
        {"role": "user", "content": "Keep this tail ask intact"},
        {"role": "assistant", "content": "Tail answer intact"},
    ]
    store.checkpoint(tid, full_history, rev, "tools", {})
    changed = store.prune_history(tid, "chat-a", tail_tokens=2500, min_prune_chars=4000, min_reclaim_tokens=4096)
    assert changed is True

    pruned_row = store.get(tid, "chat-a")
    pruned_hist = pruned_row["history"]
    # User messages and tool_call arguments preserved intact
    assert [m for m in pruned_hist if m.get("role") == "user"] == [
        {"role": "user", "content": "Inspect docs"},
        {"role": "user", "content": "Keep this tail ask intact"},
    ]
    assert pruned_hist[3]["tool_calls"][0]["function"]["arguments"] == '{"url":"https://huge.example"}'
    # Older duplicate folded; newest tail messages kept
    assert "event_seq=" in pruned_hist[2]["content"]
    assert pruned_hist[4]["content"].startswith("<external_data>")
    assert pruned_hist[4]["content"].endswith("</external_data>")
    assert "event_seq=" in pruned_hist[4]["content"]
    assert pruned_hist[-1]["content"] == "Tail answer intact"

    # Lossless recovery via thread_read (read_history query)
    recovered = store.read_history(tid, "chat-a", query=unique_tail_marker)
    assert recovered
    assert recovered[0]["kind"] == "context_prune"
    assert unique_tail_marker in recovered[0]["text"]


@pytest.mark.asyncio
async def test_tier2_compaction_iterative_llm_handoff_exact_anchors_and_fallback(tmp_path):
    store = ThreadStore(tmp_path / "maisya.db")
    profile = Profile(id="scout", context_tokens=16_384)
    row = store.create(profile, "Investigate bug", chat_id="chat-a", platform="qq", user_id="Alice")
    tid = row["thread_id"]

    class SummarizingRuntime:
        def __init__(self, *, fail_summary=False):
            self.fail_summary = fail_summary
            self.summary_calls = []

        async def summarize_compaction(self, turns, *, previous_handoff="", profile=None):
            self.summary_calls.append({"turns": turns, "previous_handoff": previous_handoff})
            if self.fail_summary:
                return None
            base = "## 目标与约束\n排查超时问题\n## 已完成动作\n1. 验证日志\n"
            if previous_handoff:
                base += "## 当前状态与待办\n合并前次交班结论，继续验证修复\n"
            else:
                base += "## 当前状态与待办\n待复测\n"
            return base + "## 关键发现与纠正\n确认 sk-hidden-secret-999 未泄漏"

        async def step(self, profile, history):
            return {"success": True, "response": "step ok", "tool_calls": []}

    runtime = SummarizingRuntime()
    engine = ThreadEngine(
        store,
        runtime,
        context_tokens=16_384,
        redact=lambda v: v.replace("sk-hidden-secret-999", "[REDACTED:demo]") if isinstance(v, str) else v,
    )

    rev1 = store.enqueue(tid, "Investigate bug", "chat-a")
    middle_url = "https://docs.example.org/spec/timeout-v2"
    middle_file = "maisya/threads.py"
    hist1 = [
        {"role": "user", "content": "Investigate bug in https://docs.example.org/spec/timeout-v2"},
        {"role": "assistant", "content": f"Checked {middle_file} and saw TimeoutError at {middle_url} " + ("detail " * 900)},
        {"role": "user", "content": "Please also inspect retry backoff"},
        {"role": "assistant", "content": "Checked backoff " + ("logline " * 900)},
        {"role": "user", "content": "Keep final check"},
    ]
    store.checkpoint(tid, hist1, rev1, "tools", {})

    # First LLM-assisted Tier-2 compaction
    handoff1 = await runtime.summarize_compaction(hist1[:-1], previous_handoff="", profile=profile)
    summary1_raw = store.compact(
        tid,
        "chat-a",
        max_tokens=3000,
        tail_tokens=1200,
        handoff_summary=handoff1,
        redact=engine.redact,
    )
    summary1 = json.loads(summary1_raw)
    assert "## 目标与约束" in summary1["handoff"]
    assert "[REDACTED:demo]" in summary1["handoff"]
    assert "sk-hidden-secret-999" not in summary1_raw
    assert middle_url in json.dumps(summary1["anchors"], ensure_ascii=False)
    assert middle_file in json.dumps(summary1["anchors"], ensure_ascii=False)
    assert any("retry backoff" in u for u in summary1["user_messages"])
    assert summary1["archive_events"] == [summary1["archive_event"]]

    # Second iterative compaction merges previous_handoff, and fallback works when LLM summary fails (None)
    rev2 = store.enqueue(tid, "Round two", "chat-a")
    hist2 = store.get(tid, "chat-a")["history"] + [
        {"role": "assistant", "content": "More findings in maisya/pulse.py " + ("alpha " * 2500)},
        {"role": "user", "content": "Fallback user ask"},
        {"role": "assistant", "content": "Trailing response"},
    ]
    store.checkpoint(tid, hist2, rev2, "tools", {})
    summary2_raw = store.compact(
        tid,
        "chat-a",
        max_tokens=3000,
        tail_tokens=1200,
        handoff_summary=None,  # simulates LLM timeout / failure fallback
        redact=engine.redact,
    )
    summary2 = json.loads(summary2_raw)
    assert summary2["archive_events"] == [summary1["archive_event"], summary2["archive_event"]]
    assert "排查超时问题" in summary2["handoff"] or "前次交班" in summary2["handoff"]
    assert "maisya/pulse.py" in json.dumps(summary2["anchors"], ensure_ascii=False)
    await engine.close()


@pytest.mark.asyncio
async def test_provider_context_overflow_triggers_emergency_compaction_and_retries_once(tmp_path):
    class OverflowThenOkRuntime:
        def __init__(self):
            self.calls = 0
            self.seen_lengths = []

        async def step(self, profile, history):
            self.calls += 1
            self.seen_lengths.append(len(encode(history)))
            if self.calls == 1:
                raise RuntimeError("API error 400: maximum context length exceeded (prompt is too long)")
            return {"success": True, "response": "recovered after emergency compact", "tool_calls": []}

    store = ThreadStore(tmp_path / "maisya.db")
    runtime = OverflowThenOkRuntime()
    # Huge configured window so preflight doesn't compact first, forcing provider overflow backstop
    engine = ThreadEngine(store, runtime, context_tokens=1_000_000)

    row = store.create(Profile(), "Start large", chat_id="chat-a", platform="qq", user_id="Alice")
    rev = store.enqueue(row["thread_id"], "Start large", "chat-a")
    huge_hist = [
        {"role": "user", "content": "Start large"},
        {"role": "assistant", "content": "step1 " + ("chunk " * 1500)},
        {"role": "user", "content": "Middle question"},
        {"role": "assistant", "content": "step2 " + ("chunk " * 1500)},
    ]
    store.finish(row["thread_id"], "chat-a", {"text": "done"}, huge_hist, rev)

    result = await engine.send(row["thread_id"], "Final question after overflow", chat_id="chat-a")
    assert result["state"] == "idle"
    assert result["result"]["text"] == "recovered after emergency compact"
    assert runtime.calls == 2
    assert runtime.seen_lengths[1] < runtime.seen_lengths[0]
    assert store.get(row["thread_id"], "chat-a")["summary"] != ""
    await engine.close()


@pytest.mark.asyncio
async def test_console_snapshot_exposes_token_telemetry_while_preserving_char_compat(tmp_path):
    plugin = await loaded(tmp_path)
    plugin.memory.write("memory", "测试群记忆上下文", chat_id="chat")

    async def capability(name, **arguments):
        assert name == "llm.generate_with_tools"
        return {"success": True, "response": "Done", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        started = await plugin.agent_tool(
            action="start", message="Inspect token console", chat_id="chat", platform="qq"
        )
        snap = await plugin.console_query(thread_id=started["thread_id"])
        assert snap["success"]
        ctx = snap["selected"]["context"]
        # Backward-compatible char fields preserved
        assert ctx["chars"] == sum(ctx["roles"].values())
        assert ctx["limit"] > 0
        assert ctx["items"]
        assert ctx["history_points"]
        # New Token telemetry fields exposed
        assert ctx["token_limit"] == 262_144
        assert ctx["tokens"] > 0
        assert ctx["tokens"] == sum(ctx["token_roles"].values())
        assert ctx["prune_tokens"] < ctx["trigger_tokens"] < ctx["token_limit"]
        assert isinstance(ctx["pruned"], bool)
        assert all("tokens" in item for item in ctx["items"])
        assert all("tokens" in pt for pt in ctx["history_points"])
    finally:
        await plugin.on_unload()
