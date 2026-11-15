import pytest
from test_foundation import event, loaded


@pytest.mark.asyncio
async def test_listed_profile_uses_verified_speaker_and_chat_scope(tmp_path):
    plugin = await loaded(tmp_path)
    calls = []

    async def capability(name, **arguments):
        calls.append(arguments)
        return {"response": "done", "tool_calls": []}

    plugin.ctx.call_capability = capability
    settings = plugin.profiles.path / "default/profile.toml"
    settings.write_text(
        settings.read_text(encoding="utf-8").replace('access = "everyone"', 'access = "listed"')
        + '\nallow_users=["qq:Alice"]\nallow_chats=["chat"]\n',
        encoding="utf-8",
    )
    try:
        forged = await plugin.agent_tool(
            action="start",
            message="fake",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
        )
        assert not forged["success"]
        await plugin.after_receive(message=event())
        allowed = await plugin.agent_tool(
            action="start",
            message="real",
            source_msg_id=event()["message_id"],
            chat_id="chat",
            platform="qq",
        )
        assert allowed["success"] and allowed["state"] == "idle", allowed
        bob = event(user="Bob")
        bob["message_id"] = "bob-source"
        await plugin.after_receive(message=bob)
        denied = await plugin.agent_tool(
            action="send",
            thread_id=allowed["thread_id"],
            message="continue",
            source_msg_id="bob-source",
            chat_id="chat",
            platform="qq",
        )
        assert not denied["success"]
        assert len(calls) == 1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_chat_only_profile_allows_autonomy_inside_its_scope(tmp_path):
    plugin = await loaded(tmp_path)

    async def capability(name, **arguments):
        return {"response": "done", "tool_calls": []}

    plugin.ctx.call_capability = capability
    settings = plugin.profiles.path / "default/profile.toml"
    settings.write_text(
        settings.read_text(encoding="utf-8").replace('access = "everyone"', 'access = "listed"')
        + '\nallow_chats=["chat"]\n',
        encoding="utf-8",
    )
    plugin.settings.enabled_chats.append("other")
    try:
        allowed = await plugin.agent_tool(
            action="start", message="public", chat_id="chat", platform="qq"
        )
        assert allowed["success"], allowed
        denied = await plugin.agent_tool(
            action="start", message="outside", chat_id="other", platform="qq"
        )
        assert not denied["success"]
    finally:
        await plugin.on_unload()
