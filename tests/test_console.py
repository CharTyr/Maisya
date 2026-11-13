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
    assert stopped["success"] and stopped["state"] == "closed"
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
