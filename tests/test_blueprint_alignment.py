import asyncio
import json
import re
from pathlib import Path

import pytest
from test_foundation import event, loaded

from maisya.abilities import Profile, Profiles
from maisya.memory import MemoryLayers
from maisya.services import Services
from maisya.threads import ThreadStore
from maisya.vault import Vault


@pytest.mark.asyncio
async def test_presets_seed_without_overwriting_and_fresh_pulse_is_idle(tmp_path):
    plugin = await loaded(tmp_path)
    llm_calls = []

    async def capability(name, **arguments):
        llm_calls.append((name, arguments))
        return {"success": True, "response": "公开探索近况。", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        skills_dir = plugin.data_root / "abilities/skills"
        source_skill = skills_dir / "source-checking/SKILL.md"
        workspace_skill = skills_dir / "ongoing-workspace/SKILL.md"
        digest_skill = skills_dir / "news-digest/SKILL.md"
        heartbeat_file = plugin.data_root / "memory/self/HEARTBEAT.md"
        assert source_skill.is_file()
        assert workspace_skill.is_file()
        assert digest_skill.is_file()
        assert heartbeat_file.is_file()

        abilities = await plugin.manage_read("abilities", "{}")
        assert "source-checking" in abilities["skills"]
        assert "ongoing-workspace" in abilities["skills"]
        assert "news-digest" in abilities["skills"]
        assert plugin.profiles.get("digest").enabled is False

        for kind, content in [
            ("profile", (plugin.profiles.path / "scout/PROFILE.md").read_text(encoding="utf-8")),
            ("profile", (plugin.profiles.path / "maker/PROFILE.md").read_text(encoding="utf-8")),
            ("profile", (plugin.profiles.path / "digest/PROFILE.md").read_text(encoding="utf-8")),
            ("skill", source_skill.read_text(encoding="utf-8")),
            ("skill", workspace_skill.read_text(encoding="utf-8")),
            ("skill", digest_skill.read_text(encoding="utf-8")),
            ("heartbeat", heartbeat_file.read_text(encoding="utf-8")),
        ]:
            assert "她" not in content
            plugin.memory._guard(kind, content, "")

        # Fresh install HEARTBEAT.md has temperament + commented empty topics -> zero unsolicited LLM calls
        await plugin.pulse.tick()
        assert llm_calls == []
        assert plugin.memory.context("chat")["journal"] == ""

        # Writing plain HEARTBEAT.md produces version 1 and activates _heartbeat()
        saved = plugin.memory.write("heartbeat", "看看今天社区有什么好玩的工具库")
        assert saved["version"] == 1
        await plugin.pulse.tick()
        await plugin.engine.drain()
        assert len(llm_calls) == 1
    finally:
        await plugin.on_unload()

    # Custom edits are preserved across reloads
    custom_skill = "自定义核验规范。"
    custom_hb = "## 气质与分寸\n保持安静。\n\n## 自主探索方向\n本地工具生态"
    source_skill.write_text(custom_skill, encoding="utf-8")
    heartbeat_file.write_text(custom_hb, encoding="utf-8")
    again = await loaded(tmp_path)
    try:
        assert (again.data_root / "abilities/skills/source-checking/SKILL.md").read_text(encoding="utf-8") == custom_skill
        assert (again.data_root / "memory/self/HEARTBEAT.md").read_text(encoding="utf-8") == custom_hb
    finally:
        await again.on_unload()


@pytest.mark.asyncio
async def test_cross_plugin_readonly_api_validation_binding_and_execution(tmp_path):
    manifest = json.loads(Path("_manifest.json").read_text(encoding="utf-8"))
    for cap in ("api.list", "api.get", "api.call"):
        assert cap in manifest["capabilities"]

    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        plugin.vault.set("secret_key", "super-secret-api-token")
        # Reject invalid readonly type, invalid access, and self APIs
        current = await plugin.manage_read("services", "{}")
        bad_configs = [
            {"apis": {"w": {"api_name": "other.plugin.weather", "readonly": "yes"}}},
            {"apis": {"w": {"api_name": "other.plugin.weather", "access": "root"}}},
            {"apis": {"self": {"api_name": "chartyr.maisya.maisya_now", "readonly": True}}},
            {"apis": {"self2": {"api_name": "maisya_now", "readonly": True}}},
        ]
        for bad in bad_configs:
            res = await plugin.manage_write(
                "services",
                json.dumps({"configuration": bad, "expected_version": current["version"]}),
            )
            assert not res["success"], bad

        good_cfg = {
            "servers": {},
            "templates": {},
            "apis": {
                "weather": {
                    "api_name": "other.plugin.get_weather",
                    "version": "1",
                    "readonly": True,
                    "access": "everyone",
                    "description": "查询公开城市天气",
                    "schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    },
                },
                "admin_stat": {
                    "api_name": "other.plugin.get_stats",
                    "version": "1",
                    "readonly": True,
                    "access": "admins",
                    "description": "只读运行统计",
                },
            },
        }
        saved = await plugin.manage_write(
            "services",
            json.dumps({"configuration": good_cfg, "expected_version": current["version"]}),
        )
        assert saved["success"], saved

        # Clone & configure a readonly profile with apis + plugin_api
        cloned = await plugin.manage_write("clone", json.dumps({"base": "scout", "target": "api_reader"}))
        assert cloned["success"]
        toml_ver = plugin.memory.read("profile", target="api_reader", document="profile.toml")["version"]
        wrote = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "profile",
                    "target": "api_reader",
                    "document": "profile.toml",
                    "expected_version": toml_ver,
                    "content": (
                        'title = "接口查询者"\n'
                        'environment = "readonly"\n'
                        'access = "everyone"\n'
                        'tools = ["plugin_api"]\n'
                        'apis = ["weather", "admin_stat"]\n'
                    ),
                }
            ),
        )
        assert wrote["success"], wrote
        ability = plugin.profiles.get("api_reader")
        assert ability.apis == ("weather", "admin_stat")

        # Package import clears bound when apis is non-empty
        pkg = plugin.profiles.export("api_reader")
        imported = Profiles(tmp_path / "import_profiles")
        imp = imported.import_package(pkg, "imported_api")
        assert imp.bound is False

        # Unregistered api fails runtime.available
        missing_dir = plugin.profiles.path / "bad_api"
        missing_dir.mkdir(parents=True)
        (missing_dir / "PROFILE.md").write_text("查接口。", encoding="utf-8")
        (missing_dir / "profile.toml").write_text(
            'environment = "readonly"\ntools = ["plugin_api"]\napis = ["nonexistent"]\n',
            encoding="utf-8",
        )
        with pytest.raises(PermissionError):
            plugin.runtime.available(plugin.profiles.get("bad_api"))

        # Test runtime.call for list, get, schema validation, admin gate, SDK unwrap (None/primitive/error), redaction
        await plugin.after_receive(message=event(user="Alice"))
        alice_msg = event(user="Alice")["message_id"]
        mallory_ev = event(user="Mallory")
        mallory_ev["message_id"] = "mallory-msg-1"
        await plugin.after_receive(message=mallory_ev)

        api_calls = []

        async def capability(name, **arguments):
            api_calls.append((name, arguments))
            if name == "api.list":
                # SDK-unwrapped list return
                return [
                    {"name": "get_weather", "plugin_id": "other.plugin", "version": "1"},
                    {"name": "get_stats", "plugin_id": "other.plugin", "version": "1"},
                ]
            if name == "api.get":
                return {
                    "name": "get_weather",
                    "plugin_id": "other.plugin",
                    "version": "1",
                    "description": "weather",
                }
            if name == "api.call":
                if arguments.get("api_name") == "other.plugin.get_stats":
                    return {"uptime": 42}
                if arguments.get("args", {}).get("city") == "none":
                    return None  # Valid SDK-unwrapped None result!
                if arguments.get("args", {}).get("city") == "fail":
                    return {"success": False, "error": "upstream offline"}
                return {
                    "city": arguments["args"]["city"],
                    "token_leak": "super-secret-api-token",
                }
            raise AssertionError(name)

        plugin.ctx.call_capability = capability
        mallory_ctx = {
            "chat_id": "chat",
            "platform": "qq",
            "owner": "chat",
            "profile": "api_reader",
            "profile_snapshot": {**__import__("dataclasses").asdict(ability)},
            "grant": {"source_msg_id": "mallory-msg-1"},
        }
        alice_ctx = {**mallory_ctx, "grant": {"source_msg_id": alice_msg}}

        # list for non-admin hides admin_stat
        listed = await plugin.runtime.call("plugin_api", {"action": "list"}, mallory_ctx)
        assert [item["id"] for item in listed["apis"]] == ["weather"]

        # list for admin shows both
        listed_admin = await plugin.runtime.call("plugin_api", {"action": "list"}, alice_ctx)
        assert [item["id"] for item in listed_admin["apis"]] == ["weather", "admin_stat"]

        # get weather succeeds
        got = await plugin.runtime.call("plugin_api", {"action": "get", "api": "weather"}, mallory_ctx)
        assert got["api"]["name"] == "get_weather"

        # schema rejects wrong args
        with pytest.raises(ValueError):
            await plugin.runtime.call("plugin_api", {"action": "call", "api": "weather", "arguments": {}}, mallory_ctx)

        # non-admin cannot call admin_stat
        with pytest.raises(PermissionError):
            await plugin.runtime.call("plugin_api", {"action": "call", "api": "admin_stat", "arguments": {}}, mallory_ctx)

        # admin can call admin_stat
        admin_res = await plugin.runtime.call(
            "plugin_api", {"action": "call", "api": "admin_stat", "arguments": {}}, alice_ctx
        )
        assert admin_res["api"] == "admin_stat"

        # admin/member can call weather; unwrapped dict returns & redacts secret
        res = await plugin.runtime.call(
            "plugin_api",
            {"action": "call", "api": "weather", "arguments": {"city": "Tokyo"}},
            mallory_ctx,
        )
        assert res["result"]["city"] == "Tokyo"
        assert "super-secret-api-token" not in str(res)

        # SDK-unwrapped None does not raise error!
        none_res = await plugin.runtime.call(
            "plugin_api",
            {"action": "call", "api": "weather", "arguments": {"city": "none"}},
            mallory_ctx,
        )
        assert none_res == {"api": "weather", "result": None}

        # Error envelope raises RuntimeError
        with pytest.raises(RuntimeError, match="upstream offline"):
            await plugin.runtime.call(
                "plugin_api",
                {"action": "call", "api": "weather", "arguments": {"city": "fail"}},
                mallory_ctx,
            )
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_before_planner_embeds_summary_omits_empty_blocks_and_gates_shape_hint(
    tmp_path,
):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        tools = [
            {"type": "function", "function": {"name": name}}
            for name in [
                "reply",
                "maisya_agent",
                "maisya_recall",
                "maisya_later",
                "maisya_shape",
            ]
        ]
        # Idle non-admin turn
        mallory = event(user="Mallory")
        mallory["message_id"] = "mallory-msg-1"
        await plugin.after_receive(message=mallory)
        res = await plugin.before_planner(
            session_id="chat", items=[], tool_definitions=tools
        )
        text = res["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        unescaped = __import__("html").unescape(text)
        assert "here.now" not in unescaped
        assert "maisya_later" in unescaped
        assert "maisya_shape" not in unescaped
        assert "query_memory" in unescaped
        assert "maisya_recall" in unescaped
        assert "<maisya_profiles>" in unescaped
        from maisya.scout import TOOL_DEFINITIONS

        assert "A_Memorix" in TOOL_DEFINITIONS["memory_read"]["function"]["description"]
        assert '"summary"' in unescaped and '"autonomous"' in unescaped
        assert "<maisya_threads>" not in unescaped
        assert "<maisya_returns>" not in unescaped
        assert "<maisya_memory>" not in unescaped

        # Admin turn with non-empty memory includes maisya_shape & <maisya_memory>
        plugin.memory.write("memory", "群名：测试实验室", chat_id="chat")
        alice = event(user="Alice")
        alice["message_id"] = "alice-msg-1"
        await plugin.after_receive(message=alice)
        admin_res = await plugin.before_planner(
            session_id="chat", items=[], tool_definitions=tools
        )
        admin_text = __import__("html").unescape(
            admin_res["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        )
        assert "maisya_shape" in admin_text
        assert "<maisya_memory>" in admin_text
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_share_splits_file_attachments_into_separate_send_hybrid_calls(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "报告整理好了，放这里。"}
        if name == "send.hybrid":
            sent.append(arguments)
            return {"success": True, "sent": True, "message_id": f"m-{len(sent)}"}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    try:
        ability = plugin.profiles.get("maker")
        row = plugin.threads.create(
            ability, "生成报告", chat_id="chat", platform="qq", user_id=""
        )
        with plugin.runtime.files.activity(row) as work:
            work.write("out/report.txt", "final report body")
            att = plugin.runtime.files.attach(row, work, ["out/report.txt"])[0]
        plugin.threads.finish(
            row["thread_id"],
            "chat",
            {
                "text": "完整对照见 https://example.org/spec",
                "attachments": [att],
            },
            [{"role": "user", "content": "生成报告"}],
            0,
        )
        updated = plugin.threads.get(row["thread_id"], "chat")
        delivered = await plugin.sharing.send(
            updated,
            attachment_handles=[att["handle"]],
        )
        assert delivered["sent"] is True
        assert len(sent) == 2
        assert all(seg["type"] != "file" for seg in sent[0]["segments"])
        assert "报告整理好了" in sent[0]["processed_plain_text"]
        assert "https://example.org/spec" in sent[0]["processed_plain_text"]
        assert [seg["type"] for seg in sent[1]["segments"]] == ["file"]
        assert sent[1]["segments"][0]["data"]["name"] == "report.txt"
        assert sent[1]["processed_plain_text"] == "[文件：report.txt]"
        assert sent[1]["maisaka_source_kind"] == "guided_reply"
        assert sent[1]["sync_to_maisaka_history"] is True
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_per_chat_proactive_level_and_private_acquaintance_gate(tmp_path):
    import time

    from maisya.trust import Message
    from plugin import _merge_settings_dict

    # Atomic map replacement in _merge_settings_dict
    base = {"chat_proactive_levels": {"a": "care"}}
    saved = {"chat_proactive_levels": {}}
    assert _merge_settings_dict(base, saved, base)["chat_proactive_levels"] == {}

    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.settings.enabled_chats.extend(["quiet-chat", "priv-stranger", "priv-admin", "priv-carol", "priv-human"])
    await plugin.on_config_update(
        "self",
        {
            **plugin.settings.model_dump(),
            "proactive_level": "care",
            "chat_proactive_levels": {"chat": "lively", "quiet-chat": "quiet"},
            "acquaintance_users": ["qq:Carol"],
        },
        1,
    )
    sent = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            sent.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    try:
        # 1. Per-chat override: quiet-chat blocks longing; chat=lively allows serendipity
        plugin.messages.record(Message.from_event(event("hi", user="Bob", platform="qq", group=True)))
        q_ev = event("hi", user="Bob", platform="qq", group=True)
        q_ev["session_id"] = "quiet-chat"
        plugin.messages.record(Message.from_event(q_ev))

        r_quiet = plugin.intents.schedule(
            kind="proactive",
            source="longing",
            target="chat:quiet-chat",
            due_at=time.time() - 1,
            reason="安静会话",
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        r_lively = plugin.intents.schedule(
            kind="proactive",
            source="serendipity",
            target="chat:chat",
            due_at=time.time() - 1,
            reason="活跃会话偶遇",
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        await plugin.pulse._intents()
        await plugin.pulse._intents()
        assert plugin.intents.get(r_quiet["intent_id"])["wake_status"] == "quiet_level"
        assert plugin.intents.get(r_lively["intent_id"])["status"] == "done"

        # 2. Private stranger (only relayed message) is blocked
        relayed = event("relayed", user="Stranger", platform="qq", group=False, relay="other.plugin")
        relayed["session_id"] = "priv-stranger"
        relayed["message_id"] = "relayed-priv-1"
        plugin.messages.record(Message.from_event(relayed))
        r_stranger = plugin.intents.schedule(
            kind="proactive",
            source="longing",
            target="chat:priv-stranger",
            due_at=time.time() - 1,
            reason="陌生私聊",
            payload={"platform": "qq", "user_id": "Stranger"},
            bypass_limits=True,
        )
        await plugin.pulse._intents()
        assert plugin.intents.get(r_stranger["intent_id"])["wake_status"] == "unfamiliar_chat"

        # 3. Cold admin / acquaintance_users private targets are allowed
        r_admin = plugin.intents.schedule(
            kind="proactive",
            source="longing",
            target="chat:priv-admin",
            due_at=time.time() - 1,
            reason="管理员私聊",
            payload={"platform": "qq", "user_id": "Alice"},
            bypass_limits=True,
        )
        r_carol = plugin.intents.schedule(
            kind="proactive",
            source="longing",
            target="chat:priv-carol",
            due_at=time.time() - 1,
            reason="熟人私聊",
            payload={"platform": "qq", "user_id": "Carol"},
            bypass_limits=True,
        )
        await plugin.pulse._intents()
        await plugin.pulse._intents()
        assert plugin.intents.get(r_admin["intent_id"])["status"] == "done"
        assert plugin.intents.get(r_carol["intent_id"])["status"] == "done"

        # 4. Real non-relayed private interaction grants familiarity
        priv_ev = event("hello", user="Dave", platform="qq", group=False)
        priv_ev["session_id"] = "priv-human"
        priv_ev["message_id"] = "priv-human-1"
        plugin.messages.record(Message.from_event(priv_ev))
        r_human = plugin.intents.schedule(
            kind="proactive",
            source="longing",
            target="chat:priv-human",
            due_at=time.time() - 1,
            reason="聊过天的私聊",
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        await plugin.pulse._intents()
        assert plugin.intents.get(r_human["intent_id"])["status"] == "done"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_self_thread_serendipity_routes_to_lively_chat_with_temperament(tmp_path):
    import time

    from maisya.trust import Message

    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.settings.enabled_chats.append("chat-care")
    await plugin.on_config_update(
        "self",
        {
            **plugin.settings.model_dump(),
            "proactive_level": "care",
            "chat_proactive_levels": {"chat": "lively", "chat-care": "care"},
        },
        1,
    )
    plugin.memory.write("memory", "大家经常讨论终端排版和复古字体。", chat_id="chat")
    plugin.memory.write("memory", "喜欢音乐。", chat_id="chat-care")
    plugin.messages.record(Message.from_event(event("hi", user="Bob", platform="qq", group=True)))
    c2 = event("hi", user="Bob", platform="qq", group=True)
    c2["session_id"] = "chat-care"
    c2["message_id"] = "c2-msg"
    c2["message_info"]["group_info"]["group_id"] = "group-care"
    plugin.messages.record(Message.from_event(c2))

    # Reject kind=serendipity from a chat thread (verified via agent_tool + thread_later below)
    chat_step = 0
    wakes = []

    async def capability(name, **arguments):
        nonlocal chat_step
        if name == "llm.generate_with_tools":
            prompt_str = str(arguments.get("prompt", ""))
            if "普通会话测拒" in prompt_str:
                chat_step += 1
                if chat_step == 1:
                    return {
                        "success": True,
                        "tool_calls": [
                            {
                                "id": "bad_ser",
                                "type": "function",
                                "function": {
                                    "name": "thread_later",
                                    "arguments": {
                                        "in_seconds": 60,
                                        "next_step": "继续",
                                        "kind": "serendipity",
                                        "share_reason": "尝试发偶遇",
                                    },
                                },
                            }
                        ],
                    }
                return {"success": True, "response": "结束", "tool_calls": []}
            # self exploration thread:
            if not any(m.get("role") == "tool" for m in arguments.get("prompt", [])):
                return {
                    "success": True,
                    "tool_calls": [
                        {
                            "id": "self_ser",
                            "type": "function",
                            "function": {
                                "name": "thread_later",
                                "arguments": {
                                    "in_seconds": 60,
                                    "next_step": "记录下来",
                                    "kind": "serendipity",
                                    "share_reason": "刚发现一个精致的终端排版工具",
                                },
                            },
                        }
                    ],
                }
            return {"success": True, "response": "今天逛到一个不错的终端排版工具。", "tool_calls": []}
        if name == "maisaka.proactive.trigger":
            wakes.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    try:
        denied_thread = await plugin.agent_tool(
            action="start",
            message="普通会话测拒",
            chat_id="chat",
            platform="qq",
            wait_seconds=5,
        )
        assert plugin.intents.list("chat:chat") == []

        self_thread = await plugin.agent_tool(
            action="start",
            owner="self",
            message="看看今日新鲜工具",
            chat_id="chat",
            platform="qq",
            wait_seconds=5,
        )
        assert self_thread["success"] and self_thread["state"] == "idle"
        pending = plugin.intents.list("chat:chat")
        assert len(pending) == 1
        assert pending[0]["kind"] == "proactive"
        assert pending[0]["source"] == "serendipity"
        assert pending[0]["thread_id"] == self_thread["thread_id"]

        # Advance clock past due_at and dispatch through Pulse
        plugin.intents.clock = lambda: time.time() + 120
        await plugin.pulse._intents()
        assert len(wakes) == 1
        assert wakes[0]["stream_id"] == "chat"
        assert "<maisya_temperament>" in wakes[0]["intent"]
        assert "<maisya_thread_result>" in wakes[0]["intent"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_longing_triggers_once_for_quiet_familiar_chat_and_obeys_cooldown(tmp_path):
    import time

    from maisya.trust import Message

    now = [1795226400.0]  # 2026-11-21 10:00 Asia/Shanghai
    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.messages.clock = plugin.intents.clock = lambda: now[0]
    wakes = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            wakes.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    try:
        for i in range(3):
            ev = event(f"msg-{i}", user="Bob", platform="qq", group=True)
            ev["message_id"] = f"m-{i}"
            plugin.messages.record(Message.from_event(ev))

        # Recent activity -> no longing
        await plugin.pulse.tick()
        assert wakes == []

        # Default longing_quiet_hours=0 keeps longing disabled even after 73 hours
        assert plugin.pulse.longing_quiet_hours == 0
        with plugin.messages.path and plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE session_routes SET last_human_at=? WHERE session_id='chat'",
                (now[0] - 73 * 3600,),
            )
        await plugin.pulse.tick()
        assert wakes == []

        # Enabling longing_quiet_hours=72 triggers once after 73 quiet hours
        plugin.pulse.longing_quiet_hours = 72
        await plugin.pulse.tick()
        assert len(wakes) == 1
        assert wakes[0]["metadata"]["source"] == "longing"
        assert "<maisya_temperament>" in wakes[0]["intent"]

        # Re-open lock by human message 2 days later -> still in 7-day cooldown
        now[0] += 2 * 86400
        plugin.intents.note_received("chat")
        await plugin.pulse.tick()
        assert len(wakes) == 1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_webhook_with_profile_runs_thread_first_and_idempotently_delivers_via_pulse(
    tmp_path,
):
    import hashlib
    import hmac
    import time

    from maisya.trust import Message

    plugin = await loaded(tmp_path)
    plugin.messages.record(Message.from_event(event("hi", user="Bob", platform="qq", group=True)))
    secret = "webhook-thread-secret-key"
    plugin.vault.set("hook_key", secret)
    gate = asyncio.Event()
    wakes = []
    llm_calls = []

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            llm_calls.append(arguments)
            await gate.wait()
            return {"success": True, "response": "早报归纳完成：共 3 项更新。", "tool_calls": []}
        if name == "maisaka.proactive.trigger":
            wakes.append(arguments)
            return {"success": True}
        raise AssertionError(name)

    plugin.ctx.call_capability = capability
    try:
        current = await plugin.manage_read("webhooks", "{}")
        saved = await plugin.manage_write(
            "webhooks",
            json.dumps(
                {
                    "configuration": {
                        "digest": {
                            "target": "chat:chat",
                            "kind": "proactive",
                            "profile": "scout",
                            "workspace": "daily_digest",
                            "secret_handle": "hook_key",
                        }
                    },
                    "expected_version": current["version"],
                }
            ),
        )
        assert saved["success"], saved

        ev = {
            "channel": "digest",
            "event_id": "digest-2026-11-13",
            "timestamp": int(time.time()),
            "payload": '{"reason":"整理今日订阅更新","data":{"count":3}}',
        }
        sig = hmac.new(
            secret.encode(),
            json.dumps(ev, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
            hashlib.sha256,
        ).hexdigest()

        accepted = await plugin.incoming_event(**ev, signature=sig)
        assert accepted["success"] and accepted["thread_id"].startswith("th_")
        # Replay does not spawn a second thread or duplicate revision
        replayed = await plugin.incoming_event(**ev, signature=sig)
        assert replayed["intent_id"] == accepted["intent_id"]
        assert replayed["thread_id"] == accepted["thread_id"]

        # While thread is still active, Pulse defers without waking the chat
        await plugin.pulse._intents()
        assert wakes == []

        # Let thread complete and advance clock beyond brief 5s defer
        gate.set()
        await plugin.engine.drain()
        assert len(llm_calls) == 1
        plugin.intents.clock = lambda: time.time() + 10
        await plugin.pulse._intents()
        assert len(wakes) == 1
        assert "<maisya_thread_result>" in wakes[0]["intent"]
        assert "<external_event>" in wakes[0]["intent"]
        assert "早报归纳完成" in wakes[0]["intent"]
    finally:
        gate.set()
        await plugin.on_unload()
