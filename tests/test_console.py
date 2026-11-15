import json

import pytest
from test_foundation import loaded


@pytest.mark.asyncio
async def test_private_console_snapshot_cursor_and_context_size(tmp_path):
    plugin = await loaded(tmp_path)

    async def capability(name, **arguments):
        return {"success": True, "response": "Verified answer", "tool_calls": []}

    plugin.ctx.call_capability = capability
    result = await plugin.agent_tool(
        action="start", message="A visible exploration", chat_id="chat", platform="qq"
    )
    snapshot = await plugin.console_query(thread_id=result["thread_id"])
    assert snapshot["success"]
    assert snapshot["threads"][0]["title"] == "A visible exploration"
    assert snapshot["selected"]["result"]["text"] == "Verified answer"
    assert snapshot["selected"]["context"]["messages"] >= 2
    assert snapshot["events"]
    after = await plugin.console_query(
        thread_id=result["thread_id"], cursor=snapshot["cursor"]
    )
    assert after["events"] == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_long_event_is_pageable_without_exposing_partial_secret_or_another_thread(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    plugin.vault.set("key", "secret-value-across-a-page")
    store = plugin.threads
    from maisya.abilities import Profile

    row = store.create(Profile(), "A trace", chat_id="chat", platform="qq", user_id="")
    other = store.create(
        Profile(), "Other", chat_id="elsewhere", platform="qq", user_id=""
    )
    with store.connect() as db, db:
        seq = store.event(
            db,
            row["thread_id"],
            "tools",
            1,
            {"text": "visible " * 900 + "secret-value-across-a-page"},
        )
    pages = []
    offset = 0
    while offset is not None:
        result = await plugin.console_event(
            thread_id=row["thread_id"], event_seq=seq, offset=offset
        )
        assert result["success"]
        pages.append(result["text"])
        offset = result["next_offset"]
    assert "secret-value-across-a-page" not in "".join(pages)
    assert "visible" in "".join(pages) and "REDACTED" in "".join(pages)
    rejected = await plugin.console_event(thread_id=other["thread_id"], event_seq=seq)
    assert not rejected["success"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_operator_note_uses_same_thread_and_stop_is_immediate(tmp_path):
    plugin = await loaded(tmp_path)

    async def capability(name, **arguments):
        assert name == "llm.generate_with_tools"
        return {"success": True, "response": "Continued", "tool_calls": []}

    plugin.ctx.call_capability = capability
    first = await plugin.agent_tool(
        action="start", message="Explore", chat_id="chat", platform="qq"
    )
    sent = await plugin.console_send(
        thread_id=first["thread_id"], message="New direction from operator"
    )
    assert sent["success"] and sent["thread_id"] == first["thread_id"]
    await plugin.engine.drain()
    row = plugin.threads.get(first["thread_id"], "chat")
    assert "New direction from operator" in str(row["history"])
    stopped = await plugin.console_stop(thread_id=first["thread_id"])
    assert stopped["success"] and stopped["state"] == "idle"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_console_output_is_bounded_redacted_and_disabled_threads_cannot_resume(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    plugin.vault.set("test", "a-secret-that-must-not-leave")

    async def capability(name, **arguments):
        return {"success": True, "response": "Text", "tool_calls": []}

    plugin.ctx.call_capability = capability
    first = await plugin.agent_tool(
        action="start", message="Explore", chat_id="chat", platform="qq"
    )
    with plugin.threads.connect() as db, db:
        for _ in range(80):
            plugin.threads.event(
                db,
                first["thread_id"],
                "tools",
                1,
                {"text": "群" * 100000 + "a-secret-that-must-not-leave"},
            )
    snapshot = await plugin.console_query(thread_id=first["thread_id"])
    encoded = json.dumps(snapshot, ensure_ascii=False).encode()
    assert len(encoded) < 400000
    assert b"a-secret-that-must-not-leave" not in encoded
    assert any(item["truncated"] for item in snapshot["events"])
    await plugin.on_config_update(
        "self",
        {"plugin": {"enabled": True, "config_version": "0.1.0"}, "enabled_chats": []},
        "0.1.0",
    )
    rejected = await plugin.console_send(
        thread_id=first["thread_id"], message="Continue"
    )
    assert not rejected["success"]
    components = {item["name"]: item for item in plugin.get_components()}
    for name in ("maisya_now", "maisya_note", "maisya_stop"):
        assert components[name]["metadata"]["public"] is False
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_console_snapshot_includes_system_memory_roles_intents_and_steer(tmp_path):
    import time

    plugin = await loaded(tmp_path)
    plugin.vault.set("secret", "hidden-token-value")
    plugin.memory.write("memory", "群主题：复古字体与排版", chat_id="chat")
    plugin.intents.schedule(
        kind="proactive",
        source="later",
        target="chat:chat",
        due_at=time.time() + 3600,
        reason="顺路提醒 hidden-token-value",
    )

    async def capability(name, **arguments):
        assert name == "llm.generate_with_tools"
        return {"success": True, "response": "Done", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        started = await plugin.agent_tool(
            action="start", message="Inspect fonts", chat_id="chat", platform="qq"
        )
        snap = await plugin.console_query(thread_id=started["thread_id"])
        assert snap["success"]
        roles = snap["selected"]["context"]["roles"]
        assert roles["system"] > 0
        assert roles["memory"] > 0
        assert snap["selected"]["context"]["chars"] == sum(roles.values())
        assert snap["selected"]["context"]["items"]
        assert snap["selected"]["context"]["history_points"]
        assert snap["stats"]["today_threads"] >= 1
        assert snap["profile_cards"]
        assert snap["pulse"]["enabled"] is True
        assert "hidden-token-value" not in json.dumps(snap, ensure_ascii=False)
        assert len(snap["scheduled_intents"]) == 1
        assert "hidden-token-value" not in snap["scheduled_intents"][0]["reason"]

        bad_steer = await plugin.console_send(
            thread_id=started["thread_id"], message="turn", steer="yes"
        )
        assert not bad_steer["success"]

        steered = await plugin.console_send(
            thread_id=started["thread_id"], message="Steer toward monospace", steer=True
        )
        assert steered["success"]
        await plugin.engine.drain()
        after = await plugin.console_query(thread_id=started["thread_id"])
        assert any(
            event["kind"] == "redirect"
            or "Steer toward monospace" in event["text"]
            for event in after["events"]
        )
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_console_and_memory_resolve_chat_sources_with_avatars_and_cache(tmp_path):
    plugin = await loaded(tmp_path)
    calls: list[tuple[str, dict]] = []

    async def capability(name, **arguments):
        calls.append((name, dict(arguments)))
        if name == "chat.get_all_streams":
            return {
                "success": True,
                "streams": [
                    {
                        "session_id": "group_101",
                        "stream_id": "group_101",
                        "platform": "qq",
                        "group_id": "101",
                        "group_name": "机械排版研习社",
                        "account_id": "bot_1",
                        "scope": "global",
                        "is_group_session": True,
                        "chat_type": "group",
                    },
                    {
                        "session_id": "dm_202",
                        "stream_id": "dm_202",
                        "platform": "telegram",
                        "user_id": "202",
                        "user_nickname": "Alice",
                        "user_cardname": "Alice (TG)",
                        "account_id": "tg_1",
                        "scope": "dm",
                        "is_group_session": False,
                        "chat_type": "private",
                    },
                    {
                        "session_id": "dm_unsafe",
                        "stream_id": "dm_unsafe",
                        "platform": "qq",
                        "user_id": "303",
                        "user_nickname": "Bob",
                        "is_group_session": False,
                        "chat_type": "private",
                    },
                ],
            }
        if name == "chat.get_avatar":
            target_id = arguments.get("target_id")
            if target_id == "101":
                return {
                    "success": True,
                    "avatar": {"status": "available", "url": "https://example.org/group101.png"},
                }
            if target_id == "202":
                return {
                    "success": True,
                    "avatar": {"status": "available", "url": "data:image/png;base64,iVBORw0KGgo="},
                }
            if target_id == "303":
                return {
                    "success": True,
                    "avatar": {"status": "available", "url": "javascript:alert(1)"},
                }
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "Ok", "tool_calls": []}
        return {"success": False}

    plugin.ctx.call_capability = capability
    try:
        plugin.memory.write("memory", "讨论了机械键盘字体", chat_id="group_101")
        plugin.memory.write("memory", "私聊约定明天同步", chat_id="dm_202")
        plugin.memory.write("memory", "不安全头像测试", chat_id="dm_unsafe")
        plugin.memory.write("memory", "自身观察记录", chat_id="self")

        from maisya.abilities import Profile

        plugin.threads.create(
            Profile(), "Summarize", chat_id="group_101", platform="qq", user_id=""
        )

        snap = await plugin.console_query()
        assert snap["success"]
        sources = snap["chat_sources"]
        assert sources["group_101"]["name"] == "机械排版研习社"
        assert sources["group_101"]["platform"] == "qq"
        assert sources["group_101"]["chat_type"] == "group"
        assert sources["group_101"]["avatar"] == "https://example.org/group101.png"

        mem = await plugin.manage_read("memory")
        assert mem["success"]
        mem_sources = mem["chat_sources"]
        assert mem_sources["dm_202"]["name"] == "Alice (TG)"
        assert mem_sources["dm_202"]["platform"] == "telegram"
        assert mem_sources["dm_202"]["chat_type"] == "private"
        assert mem_sources["dm_202"]["avatar"].startswith("data:image/png;base64,")
        assert mem_sources["dm_unsafe"]["name"] == "Bob"
        assert mem_sources["dm_unsafe"]["avatar"] == ""
        assert mem_sources["self"]["chat_type"] == "self"

        # Second call should use cached streams & avatars
        before_count = len([c for c in calls if c[0] in ("chat.get_all_streams", "chat.get_avatar")])
        await plugin.manage_read("memory")
        after_count = len([c for c in calls if c[0] in ("chat.get_all_streams", "chat.get_avatar")])
        assert after_count == before_count
    finally:
        await plugin.on_unload()
