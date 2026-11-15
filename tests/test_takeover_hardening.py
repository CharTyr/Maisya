import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_foundation import event, loaded

from maisya.abilities import Profile, Profiles
from maisya.learning import Learning
from maisya.sandbox import Sandbox, SandboxSettings
from maisya.scout import Scout, call_capability
from maisya.threads import ThreadEngine, ThreadStore
from maisya.trust import Message, MessageStore, strip_mention_all


def test_profile_task_slot_validation_and_preset_defaults(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    assert profiles.get("default").task == "utils"
    assert profiles.get("scout").id == "default"
    assert profiles.get("maker").id == "default"

    cfg = profiles.path / "default/profile.toml"
    cfg.write_text('environment = "readonly"\ntask = "custom_reasoning"\n', encoding="utf-8")
    assert profiles.get("default").task == "custom_reasoning"

    cfg.write_text('environment = "readonly"\ntask = "bad task!"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        profiles.get("default")


@pytest.mark.asyncio
async def test_scout_and_maker_pass_explicit_timeout_and_profile_task(tmp_path):
    calls = []

    class Host:
        async def call_capability(self, name, **arguments):
            calls.append((name, arguments))
            if name == "llm.generate_with_tools":
                return {"success": True, "response": "ok", "tool_calls": []}
            if name in {"web.search", "web.fetch"}:
                return {"success": True, "result": {"ok": True}}
            if name == "message.get_by_id":
                return {
                    "success": True,
                    "message": {
                        "session_id": "chat-a",
                        "message_id": "m1",
                        "raw_message": [
                            {"type": "image", "binary_data_base64": "aW1hZ2U="}
                        ],
                    },
                }
            if name == "llm.generate_for_image":
                return {"success": True, "response": "a cat"}
            return {"success": True, "messages": [], "content": "ok"}

    scout = Scout(Host())
    await scout.step(Profile(id="maker", task="planner"), [{"role": "user", "content": "hi"}])
    await scout.call("web_search", {"query": "python"}, {"chat_id": "chat-a", "owner": "chat"})
    await scout.call("web_fetch", {"url": "https://example.org"}, {"chat_id": "chat-a", "owner": "chat"})
    await scout.call(
        "image_read",
        {"message_id": "m1", "question": "what"},
        {"chat_id": "chat-a", "owner": "chat"},
    )

    by_name = {name: args for name, args in calls}
    assert by_name["llm.generate_with_tools"]["task_name"] == "planner"
    assert by_name["llm.generate_with_tools"]["timeout_ms"] == 90000
    assert by_name["web.search"]["timeout_ms"] == 70000
    assert by_name["web.fetch"]["timeout_ms"] == 70000
    assert by_name["llm.generate_for_image"]["timeout_ms"] == 60000


@pytest.mark.asyncio
async def test_learning_uses_explicit_timeout_budget(tmp_path):
    plugin = await loaded(tmp_path)
    calls = []

    async def capability(name, **arguments):
        calls.append((name, arguments))
        return {"success": True, "response": '{"note":"先缩窄查询关键词再重试。"}'}

    plugin.ctx.call_capability = capability
    try:
        row = plugin.threads.create(Profile(), "help", chat_id="chat", platform="qq", user_id="Alice")
        learner = Learning(plugin.ctx, plugin.memory)
        await learner._learn(
            {**row, "completed_revision": 1},
            [{"tool": "web_search", "status": "error"}, {"tool": "web_search", "status": "success"}],
        )
        assert calls and calls[0][0] == "llm.generate_with_tools"
        assert calls[0][1]["timeout_ms"] == 40000
        assert "先缩窄查询关键词再重试" in plugin.memory.read("notes", target="default")["content"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_session_routes_survive_trusted_expiry_and_reopen_cold_session(tmp_path):
    store = MessageStore(tmp_path / "messages.db", max_age=0.01)
    grp = event("hi", user="Alice", platform="qq", group=True)
    grp["message_id"] = "grp-msg-1"
    grp["session_id"] = "chat-grp"
    store.record(Message.from_event(grp))
    relayed = event("relay", user="Bot", platform="qq", group=True, relay="other.plugin")
    relayed["message_id"] = "relay-msg-1"
    relayed["session_id"] = "chat-relay"
    store.record(Message.from_event(relayed))
    time.sleep(0.02)
    other = event("fresh", user="Bob", platform="qq", group=False)
    other["message_id"] = "priv-msg-1"
    other["session_id"] = "chat-priv"
    store.record(Message.from_event(other))

    assert store.lookup("grp-msg-1", "chat-grp", "qq") is None
    assert store.platform_for("chat-grp") == "qq"
    assert store.route_for("chat-grp") == {
        "platform": "qq",
        "chat_type": "group",
        "group_id": "group",
        "user_id": "",
        "account_id": "",
        "scope": "",
    }
    assert store.route_for("chat-priv") == {
        "platform": "qq",
        "chat_type": "private",
        "group_id": "",
        "user_id": "Bob",
        "account_id": "",
        "scope": "",
    }
    assert store.route_for("chat-relay") is None

    plugin = await loaded(tmp_path)
    try:
        grp_plugin = event("hi", user="Alice", platform="qq", group=True)
        await plugin.after_receive(message=grp_plugin)
        with plugin.threads.connect() as db, db:
            db.execute("DELETE FROM trusted_sources")
        assert plugin.intents.known_chat("chat") is True

        calls = []
        attempts = 0

        async def capability(name, **arguments):
            nonlocal attempts
            calls.append((name, arguments))
            if name == "maisaka.proactive.trigger":
                attempts += 1
                if attempts == 1:
                    return {"success": False, "error": "未找到已存在的聊天流: chat"}
                return {"success": True, "queued": True}
            if name == "chat.open_session":
                return {"success": True, "created": True, "stream_id": "chat"}
            raise AssertionError(name)

        plugin.ctx.call_capability = capability
        row = plugin.intents.schedule(
            kind="proactive",
            source="longing",
            target="chat:chat",
            due_at=plugin.intents.clock() - 1,
            reason="来看看群里怎么样了",
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        await plugin.pulse._intents()
        assert [c[0] for c in calls] == [
            "maisaka.proactive.trigger",
            "chat.open_session",
            "maisaka.proactive.trigger",
        ]
        assert calls[1][1]["platform"] == "qq"
        assert calls[1][1]["chat_type"] == "group"
        assert calls[1][1]["group_id"] == "group"
        saved = plugin.intents.get(row["intent_id"])
        assert saved["status"] == "done" and saved["wake_status"] == "accepted"

        # If session remains missing after reopen fails, defer rather than fail permanently
        async def always_missing(name, **arguments):
            if name == "maisaka.proactive.trigger":
                return {"success": False, "error": "未找到已存在的聊天流: chat"}
            return {"success": False, "error": "adapter offline"}

        plugin.ctx.call_capability = always_missing
        plugin.intents.note_received("chat")
        row2 = plugin.intents.schedule(
            kind="proactive",
            source="longing",
            target="chat:chat",
            due_at=plugin.intents.clock() - 1,
            reason="重连前先别丢",
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        await plugin.pulse._intents()
        deferred = plugin.intents.get(row2["intent_id"])
        assert deferred["status"] == "pending" and deferred["due_at"] > time.time()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_config_toml_changes_override_settings_json_via_three_way_merge(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        orig = await plugin.manage_read("settings")
        vals = orig["settings"]
        vals["proactive_level"] = "quiet"
        saved = await plugin.manage_write(
            "settings",
            json.dumps({"settings": vals, "expected_version": orig["version"]}),
        )
        assert saved["success"]
        assert (plugin.data_root / "settings.base.json").exists()
    finally:
        await plugin.on_unload()

    # Reload with edited config.toml (enabled_chats updated + plugin disabled)
    reloaded = await loaded(tmp_path)
    await reloaded.on_unload()

    # Now mutate plugin config before on_load to simulate editing config.toml while stopped
    from maibot_sdk.context import PluginPaths
    from plugin import create_plugin
    import logging

    p2 = create_plugin()
    async def get(key, default=None):
        return ["qq:Alice"] if key == "plugin.permission" else default
    p2._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya"),
    )
    p2.set_plugin_config(
        {
            "plugin": {"enabled": False, "config_version": "0.1.0"},
            "enabled_chats": ["chat", "ops-group"],
        }
    )
    await p2.on_load()
    try:
        assert p2.settings.plugin.enabled is False
        assert p2.settings.enabled_chats == ["chat", "ops-group"]
        assert p2.settings.proactive_level == "quiet"
    finally:
        await p2.on_unload()


@pytest.mark.asyncio
async def test_later_proactive_daily_quota_and_non_admin_mention_all_strip(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        first = await plugin.later_tool(
            action="schedule",
            kind="proactive",
            in_seconds=60,
            reason="第一条主动惦记",
            chat_id="chat",
            platform="qq",
        )
        assert first["success"]
        second = await plugin.later_tool(
            action="schedule",
            kind="proactive",
            in_seconds=90,
            reason="第二条主动惦记",
            chat_id="chat",
            platform="qq",
        )
        assert not second["success"]

        # Reminders (kind="later") are still allowed beyond 1/day
        rem = await plugin.later_tool(
            action="schedule",
            kind="later",
            in_seconds=60,
            reason="普通提醒",
            chat_id="chat",
            platform="qq",
        )
        assert rem["success"]

        # Helper unit check + Share + before_send stripping for non-admin vs admin
        assert strip_mention_all("通知 @全体成员 看下 [CQ:at,qq=all]") == "通知 看下"
        assert strip_mention_all(
            [
                {"type": "at", "data": {"target_user_id": "all"}},
                {"type": "text", "data": "@所有人 查好了"},
            ]
        ) == [{"type": "text", "data": "查好了"}]

        # before_send strips mass mention for non-admin turn
        bob_msg = event("hi", user="Bob", platform="qq", group=True)
        bob_msg["message_id"] = "bob-msg-1"
        await plugin.after_receive(message=bob_msg)
        blocked = await plugin.before_send(
            session_id="chat",
            platform="qq",
            message="请 @全体成员 看看结果",
        )
        assert "@全体成员" not in blocked["modified_kwargs"]["message"]

        # Admin turn allows @全体成员
        alice_msg = event("hi", user="Alice", platform="qq", group=True)
        alice_msg["message_id"] = "alice-msg-1"
        await plugin.after_receive(message=alice_msg)
        allowed_send = await plugin.before_send(
            session_id="chat",
            platform="qq",
            message="请 @全体成员 看看结果",
        )
        assert allowed_send == {"action": "continue"}

        # Share._send passes timeout_ms and strips non-admin mass mention
        sent_calls = []

        async def capability(name, **arguments):
            sent_calls.append((name, arguments))
            if name == "llm.generate_with_tools":
                return {
                    "success": True,
                    "response": "@全体成员 查到了 https://example.org/doc",
                    "tool_calls": [],
                }
            if name == "maisaka.reply.generate":
                return {"success": True, "text": "@所有人 结果放这里啦"}
            if name == "send.hybrid":
                return {"success": True, "sent": True, "message_id": "out-1"}
            raise AssertionError(name)

        plugin.ctx.call_capability = capability
        # Use Bob (non-admin) turn
        plugin._turn_sources["chat"] = ["bob-msg-1"]
        started = await plugin.agent_tool(
            action="start",
            source_msg_id="bob-msg-1",
            message="查一下",
            chat_id="chat",
            platform="qq",
        )
        shared = await plugin.agent_tool(
            action="share",
            source_msg_id="bob-msg-1",
            thread_id=started["thread_id"],
            chat_id="chat",
            platform="qq",
        )
        assert shared["sent"]
        by_cap = dict(sent_calls)
        assert by_cap["maisaka.reply.generate"]["timeout_ms"] == 45000
        assert "不超过 60 字" in by_cap["maisaka.reply.generate"]["format_instruction"]
        assert by_cap["send.hybrid"]["timeout_ms"] == 60000
        assert "@所有人" not in by_cap["send.hybrid"]["processed_plain_text"]
        assert "@全体成员" not in by_cap["send.hybrid"]["processed_plain_text"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_heartbeat_triggers_self_exploration_only_when_configured_and_respects_interval(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    calls = []

    async def capability(name, **arguments):
        calls.append((name, arguments))
        return {"success": True, "response": "今天看了一个轻量 SQLite 扩展库。", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        # Empty HEARTBEAT.md -> zero LLM calls
        await plugin.pulse.tick()
        assert calls == []

        # Populated HEARTBEAT.md -> launches one self scout thread in workspace="heartbeat"
        saved = plugin.memory.write("heartbeat", "看看今天社区有什么好玩的工具库")
        assert saved["version"] == 1
        await plugin.pulse.tick()
        await plugin.engine.drain()
        assert len(calls) == 1
        threads = plugin.threads.search("", "self", limit=5)
        assert len(threads) == 1
        assert plugin.threads.get(threads[0]["thread_id"], "self")["owner"] == "self"

        # Immediate second tick respects cooldown interval
        await plugin.pulse.tick()
        await plugin.engine.drain()
        assert len(calls) == 1

        # Quiet level or sleep_window suppresses heartbeat even if interval elapsed
        plugin.pulse.heartbeat_interval = 0
        plugin.pulse.proactive_level = "quiet"
        await plugin.pulse.tick()
        await plugin.engine.drain()
        assert len(calls) == 1
        plugin.pulse.proactive_level = "care"
        plugin.pulse.sleep_window = "0-24"
        plugin.intents.clock = lambda: 1795202400
        await plugin.pulse.tick()
        await plugin.engine.drain()
        assert len(calls) == 1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_sandbox_settings_max_concurrent_default_and_semaphore(tmp_path):
    settings = SandboxSettings()
    assert settings.max_concurrent == 2
    sb = Sandbox(SandboxSettings(max_concurrent=2), ThreadStore(tmp_path / "sb.db"))
    async with sb.slot:
        async with sb.slot:
            assert sb.slot.locked()


@pytest.mark.asyncio
async def test_call_capability_normalizes_custom_rpc_exception():
    class FakeRPCError(Exception):
        pass

    class Host:
        async def call_capability(self, name, timeout_ms=None, **kwargs):
            raise FakeRPCError("transport timeout")

    with pytest.raises(RuntimeError, match="transport timeout"):
        await call_capability(Host(), "web.search", timeout_ms=5000, query="python")


@pytest.mark.asyncio
async def test_thread_engine_never_infinite_loops_on_unexpected_exception(tmp_path):
    class UnexpectedError(Exception):
        pass

    calls = 0

    class BrokenRuntime:
        async def step(self, profile, history):
            nonlocal calls
            calls += 1
            raise UnexpectedError("non-runtime exception")

    store = ThreadStore(tmp_path / "threads.db")
    engine = ThreadEngine(store, BrokenRuntime(), max_steps=4, timeout=10)
    started = await engine.start(
        Profile(id="scout", instructions="test", access="everyone"),
        "hello",
        chat_id="chat-1",
        platform="qq",
        user_id="alice",
        wait_seconds=5,
    )
    await engine.drain()
    row = store.get(started["thread_id"], "chat-1")
    assert calls == 1
    assert row["state"] == "idle"
    assert "error" in row["result"]
