import asyncio
from copy import deepcopy

import pytest
from test_foundation import event, loaded


async def llm_rpc(name, **arguments):
    if name == "llm.generate_with_tools":
        return {
            "success": True,
            "response": "A useful exploration result",
            "tool_calls": [],
        }
    raise AssertionError("unexpected capability: " + name)


async def source(plugin, *, user="Alice", session="chat"):
    payload = event(user=user)
    payload["session_id"] = session
    await plugin.after_receive(message=payload)
    return payload["message_id"]


@pytest.mark.asyncio
async def test_agent_tool_uses_source_identity_and_recall(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.ctx.call_capability = llm_rpc
    msg_id = await source(plugin)
    result = await plugin.agent_tool(
        action="start",
        message="Explore Kamakura",
        source_msg_id=msg_id,
        chat_id="chat",
        platform="qq",
        user_id="Mallory",
    )
    assert result["success"]
    assert result["state"] == "idle"
    row = plugin.threads.get(result["thread_id"], "chat")
    assert row["user_id"] == "Alice"
    recall = await plugin.recall_tool(query="Kamakura", chat_id="chat", platform="qq")
    assert recall["threads"][0]["thread_id"] == result["thread_id"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_offscope_and_relay_cannot_open_threads(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.ctx.call_capability = llm_rpc
    msg_id = await source(plugin)
    result = await plugin.agent_tool(
        action="start",
        message="read other chat",
        source_msg_id=msg_id,
        chat_id="elsewhere",
        platform="qq",
    )
    assert result["success"] is False
    payload = event(relay="other.plugin")
    payload["message_id"] = "relayed-message"
    await plugin.after_receive(message=payload)
    result = await plugin.agent_tool(
        action="start",
        message="relay",
        source_msg_id=payload["message_id"],
        chat_id="chat",
        platform="qq",
    )
    assert result["success"] is False
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_planner_keeps_native_and_third_party_and_does_not_start_for_chat(
    tmp_path,
):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await source(plugin)
    tools = [
        {"type": "function", "function": {"name": name}}
        for name in [
            "reply",
            "query_memory",
            "other_plugin",
            "subagent",
            "subagent_status",
            "subagent_stop",
            "maisya_agent",
            "maisya_recall",
        ]
    ]
    items = [
        {
            "item_type": "SystemMessageItem",
            "parts": [{"type": "text", "text": "original host identity"}],
        }
    ]
    before = deepcopy(items)
    result = await plugin.before_planner(
        session_id="chat", items=items, tool_definitions=tools
    )
    modified = result["modified_kwargs"]
    names = [item["function"]["name"] for item in modified["tool_definitions"]]
    assert names == [
        "reply",
        "query_memory",
        "other_plugin",
        "maisya_agent",
        "maisya_recall",
    ]
    assert modified["items"][0] == before[0]
    assert items == before
    assert plugin.threads.search("", "chat") == []
    disabled = await plugin.before_planner(
        session_id="elsewhere", items=items, tool_definitions=tools
    )
    assert "subagent" in str(disabled)
    assert "maisya_agent" not in str(disabled["modified_kwargs"]["tool_definitions"])
    assert await plugin.before_planner(
        session_id="chat", items=items, tool_definitions=[]
    ) == {"action": "continue"}
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_admin_profile_hidden_and_execution_rechecks_source(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    plugin.ctx.call_capability = llm_rpc
    config = plugin.profiles.path / "scout/profile.toml"
    config.write_text('access="admins"\nenvironment="readonly"', encoding="utf-8")
    maker_config = plugin.profiles.path / "maker/profile.toml"
    maker_config.write_text('access="admins"\nenvironment="sandbox"', encoding="utf-8")
    msg_id = await source(plugin, user="Mallory")
    tools = [
        {"name": "reply"},
        {"name": "subagent"},
        {"name": "maisya_agent"},
        {"name": "maisya_recall"},
    ]
    result = await plugin.before_planner(
        session_id="chat", items=[], tool_definitions=tools
    )
    assert "maisya_agent" not in str(result["modified_kwargs"]["tool_definitions"])
    assert "subagent" in str(result["modified_kwargs"]["tool_definitions"])
    result = await plugin.agent_tool(
        action="start",
        message="test",
        source_msg_id=msg_id,
        chat_id="chat",
        platform="qq",
        user_id="Alice",
    )
    assert not result["success"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_disabling_chat_stops_active_threads(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    gate = asyncio.Event()

    async def blocked(name, **arguments):
        await gate.wait()
        return {"success": True, "response": "late result", "tool_calls": []}

    plugin.ctx.call_capability = blocked
    msg_id = await source(plugin)
    result = await plugin.agent_tool(
        action="start",
        message="long exploration",
        source_msg_id=msg_id,
        wait_seconds=0,
        chat_id="chat",
        platform="qq",
    )
    assert result["state"] == "active"
    await plugin.on_config_update(
        "self",
        {"plugin": {"enabled": True, "config_version": "0.1.0"}, "enabled_chats": []},
        2,
    )
    assert plugin.threads.get(result["thread_id"], "chat")["state"] == "closed"
    gate.set()
    await plugin.on_unload()
