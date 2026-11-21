import pytest
from test_foundation import event, loaded


@pytest.mark.asyncio
async def test_self_activity_is_global_can_resume_and_pulse_advances_it(tmp_path):
    plugin = await loaded(tmp_path)
    prompts = []

    async def capability(name, **arguments):
        prompts.append(arguments["prompt"])
        return {"response": "我今天在海边散步。", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        first = await plugin.agent_tool(
            action="start",
            owner="self",
            workspace="海岸旅行",
            message="去探索一段海岸",
            chat_id="chat",
            platform="qq",
        )
        assert first["success"] and first["owner"] == "self", first
        row = plugin.threads.get(first["thread_id"], "self")
        assert row["chat_id"] == "self" and row["user_id"] == ""
        assert "海边" in plugin.memory.context("chat")["journal"]
        plugin.settings.enabled_chats.append("other")
        resumed = await plugin.agent_tool(
            action="send",
            thread_id=first["thread_id"],
            message="继续向前走",
            chat_id="other",
            platform="qq",
        )
        assert resumed["success"] and resumed["thread_id"] == first["thread_id"]
        scheduled = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target="chat:self",
            due_at=plugin.intents.clock(),
            reason="看看落日",
            thread_id=first["thread_id"],
        )
        await plugin.pulse._intents()
        await plugin.engine.drain()
        assert plugin.intents.get(scheduled["intent_id"])["status"] == "done"
        assert "看看落日" in str(prompts[-1])
        stopped = await plugin.agent_tool(
            action="stop", thread_id=first["thread_id"], chat_id="chat", platform="qq"
        )
        assert stopped["success"] and stopped["state"] == "idle"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_self_activity_rejects_chat_references_and_private_memory(tmp_path):
    plugin = await loaded(tmp_path)
    plugin.memory.write("memory", "群名：玄武厅", chat_id="chat")
    try:
        for options in [
            {"ref_msg_ids": ["original"]},
            {"from_threads": ["th_0123456789abcdef"]},
            {"message": "去玄武厅探险"},
        ]:
            arguments = {
                "action": "start",
                "owner": "self",
                "message": "trip",
                "chat_id": "chat",
                "platform": "qq",
                **options,
            }
            result = await plugin.agent_tool(**arguments)
            assert not result["success"], result
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_public_console_and_pulse_resumes_do_not_reuse_expired_user_grant(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    prompts = []

    async def capability(name, **arguments):
        prompts.append(arguments["prompt"])
        return {"response": "done", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        await plugin.after_receive(message=event())
        first = await plugin.agent_tool(
            action="start",
            message="first",
            source_msg_id=event()["message_id"],
            chat_id="chat",
            platform="qq",
        )
        with plugin.threads.connect() as db, db:
            db.execute("UPDATE trusted_sources SET received=received-600")
        await plugin.console_send(first["thread_id"], "console continuation")
        await plugin.engine.drain()
        assert "console continuation" in str(prompts[-1])
        plugin.intents.schedule(
            kind="silent",
            source="thread",
            target="chat:chat",
            due_at=plugin.intents.clock(),
            reason="pulse continuation",
            thread_id=first["thread_id"],
        )
        await plugin.pulse._intents()
        await plugin.engine.drain()
        assert "pulse continuation" in str(prompts[-1])
        assert plugin.threads.get(first["thread_id"], "chat")["completed_revision"] == 3
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_multiple_explicit_revisions_trigger_one_bounded_generic_note(tmp_path):
    import asyncio

    plugin = await loaded(tmp_path)
    entered = [asyncio.Event(), asyncio.Event()]
    turns = 0
    extracted = []

    async def capability(name, **arguments):
        nonlocal turns
        if name == "web.search":
            return {"success": True, "result": {"text": "verified"}}
        if arguments.get("max_tokens") == 500:
            if "调度手册" in str(arguments.get("prompt", "")):
                return {"response": '{"title":"","lesson":"","rationale":""}'}
            extracted.append(arguments)
            return {"response": '{"note":"反复调整方向时，先确认目标再选择工具。"}'}
        turns += 1
        if turns <= 2:
            entered[turns - 1].set()
            await asyncio.Event().wait()
        if turns == 3:
            return {
                "tool_calls": [
                    {
                        "id": "verify",
                        "function": {
                            "name": "web_search",
                            "arguments": {"query": "fixture"},
                        },
                    }
                ]
            }
        return {"response": "done", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        first = await plugin.agent_tool(
            action="start",
            message="first",
            wait_seconds=0,
            chat_id="chat",
            platform="qq",
        )
        await entered[0].wait()
        await plugin.agent_tool(
            action="send",
            thread_id=first["thread_id"],
            steer=True,
            message="second",
            wait_seconds=0,
            chat_id="chat",
            platform="qq",
        )
        await entered[1].wait()
        result = await plugin.agent_tool(
            action="send",
            thread_id=first["thread_id"],
            steer=True,
            message="third",
            chat_id="chat",
            platform="qq",
        )
        assert result["state"] == "idle"
        await plugin.runtime.learning.drain()
        assert len(extracted) == 1 and "multiple_redirects" in str(extracted)
        assert "确认目标" in plugin.memory.read("notes", target="default")["content"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_self_result_shares_exact_links_in_current_conversation_once(tmp_path):
    plugin = await loaded(tmp_path)
    sent = []

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            return {"response": "海岸见闻 https://example.org/coast", "tool_calls": []}
        if name == "maisaka.reply.generate":
            return {"text": "刚才看到这个。"}
        assert name == "send.hybrid"
        sent.append(arguments)
        return {"success": True, "sent": True, "message_id": "self-share"}

    plugin.ctx.call_capability = capability
    try:
        first = await plugin.agent_tool(
            action="start",
            owner="self",
            message="海岸见闻",
            chat_id="chat",
            platform="qq",
        )
        shared = await plugin.agent_tool(
            action="share", thread_id=first["thread_id"], chat_id="chat", platform="qq"
        )
        assert shared.get("sent"), shared
        repeated = await plugin.agent_tool(
            action="share", thread_id=first["thread_id"], chat_id="chat", platform="qq"
        )
        assert repeated.get("already_handled") and len(sent) == 1
        plugin.settings.enabled_chats.append("other")
        elsewhere = await plugin.agent_tool(
            action="share", thread_id=first["thread_id"], chat_id="other", platform="qq"
        )
        assert elsewhere.get("sent") and len(sent) == 2, elsewhere
        assert sent[0]["stream_id"] == "chat" and "https://example.org/coast" in str(
            sent
        )
    finally:
        await plugin.on_unload()
