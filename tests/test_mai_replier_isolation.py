import asyncio

import pytest
import pytest_asyncio

from test_admin_one_shot import _write_host_model_config
from test_foundation import event, loaded
from test_share import prepared, share


CASUAL_MODEL = "mai_replier_today"
TASK_MODEL = "gemini-3.8-flash"
CHAT = {"chat_id": "chat", "platform": "qq"}


def configure_replyer(tmp_path):
    _write_host_model_config(
        tmp_path,
        f'''[[models]]
name = "{CASUAL_MODEL}"

[[models]]
name = "{TASK_MODEL}"

[model_task_config.replyer]
model_list = ["{TASK_MODEL}", "{CASUAL_MODEL}"]
''',
    )


@pytest_asyncio.fixture
async def plugin(tmp_path):
    configure_replyer(tmp_path)
    instance = await loaded(tmp_path, ["qq:Alice"])
    try:
        await instance.after_receive(message=event(user="Bob", group=True))
        yield instance
    finally:
        await instance.on_unload()


async def native_reply(plugin, **kwargs):
    return await plugin.before_replyer(
        **{
            "session_id": "chat",
            "request_type": "maisaka.replyer",
            "model_name": CASUAL_MODEL,
            **kwargs,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [True, False])
async def test_share_requests_the_selected_model_only_when_available(tmp_path, configured):
    if configured:
        configure_replyer(tmp_path)
    plugin, thread_id, calls = await prepared(tmp_path)
    try:
        assert (await share(plugin, thread_id))["sent"]
        (generate_args,) = [args for name, args in calls if name == "maisaka.reply.generate"]
        if configured:
            assert generate_args["model_name"] == TASK_MODEL
        else:
            assert "model_name" not in generate_args
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["", CASUAL_MODEL, TASK_MODEL])
async def test_plugin_replyer_only_steers_the_model(plugin, monkeypatch, requested):
    def unexpected(*args):
        pytest.fail("plugin.replyer must bypass native reply processing")

    monkeypatch.setattr(plugin.bridge, "suppress_reply", unexpected)
    monkeypatch.setattr(plugin.bridge, "pending_offered_attachments", unexpected)
    monkeypatch.setattr(plugin.bridge, "reply_material", unexpected)
    monkeypatch.setattr(plugin.memory, "context", unexpected)
    reply = await plugin.before_replyer(
        session_id="chat",
        request_type="plugin.replyer",
        model_name=requested,
        extra_prompt="分享表达要求",
    )
    expected = {"action": "continue"}
    if requested != TASK_MODEL:
        expected["modified_kwargs"] = {"model_name": TASK_MODEL}
    assert reply == expected
    # 未启用会话且无活跃 Maisya share 时不干扰其他插件的 plugin.replyer
    assert (
        await plugin.before_replyer(
            session_id="unenabled-other-chat",
            request_type="plugin.replyer",
            model_name=CASUAL_MODEL,
        )
        == {"action": "continue"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("agent_tool", {"action": "start", "message": "查找信息", "wait_seconds": 0}),
        ("agent_tool", {"action": "list"}),
        ("recall_tool", {"kind": "memory"}),
        ("later_tool", {"action": "list"}),
    ],
)
async def test_non_admin_tools_steer_native_reply_and_its_retry(plugin, tool, arguments):
    async def capability(name, **kwargs):
        if name == "llm.generate_with_tools":
            await asyncio.Future()
        return {"success": False}

    plugin.ctx.call_capability = capability
    assert (await getattr(plugin, tool)(**CHAT, **arguments))["success"]
    original_args = {"reply_style": "简短"}
    first = await native_reply(
        plugin, model_name="", call_id="reply-1", reply_tool_args=original_args
    )
    assert first["action"] == "continue"
    modified = first["modified_kwargs"]
    token = modified["reply_tool_args"]["_maisya_task_token"]
    assert token
    assert modified == {
        "model_name": TASK_MODEL,
        "reply_tool_args": {**original_args, "_maisya_task_token": token},
    }
    assert original_args == {"reply_style": "简短"}
    assert await native_reply(
        plugin, attempt=2, reply_tool_args=modified["reply_tool_args"]
    ) == first
    retry = await native_reply(plugin, attempt=2, call_id="reply-1")
    assert retry["modified_kwargs"] == {
        "model_name": TASK_MODEL,
        "reply_tool_args": {"_maisya_task_token": token},
    }
    assert await native_reply(plugin, attempt=2, call_id="another-reply") == {
        "action": "continue"
    }


@pytest.mark.asyncio
async def test_only_a_waiting_chat_thread_in_the_same_session_steers_reply(plugin):
    thread_id = plugin.threads.create(
        plugin.profiles.get("default"), "需要确认", user_id="Bob", **CHAT
    )["thread_id"]
    assert await native_reply(plugin) == {"action": "continue"}
    with plugin.threads.connect() as db, db:
        db.execute("UPDATE agent_threads SET state='waiting' WHERE thread_id=?", (thread_id,))
    assert await native_reply(plugin) == {
        "action": "continue",
        "modified_kwargs": {"model_name": TASK_MODEL},
    }
    assert await native_reply(plugin, session_id="another-chat") == {"action": "continue"}


@pytest.mark.asyncio
async def test_casual_reply_keeps_model_with_or_without_memory(plugin):
    assert await native_reply(plugin) == {"action": "continue"}
    plugin.memory.write("memory", "软件工程与开源工具", chat_id="chat", expected_version=0)
    modified = (await native_reply(plugin))["modified_kwargs"]
    assert "<maisya_memory>" in modified["extra_prompt"]
    assert "<maisya_ops_receipt>" not in modified["extra_prompt"]
    assert "model_name" not in modified


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["send", "planner", "unload"])
async def test_task_marker_is_cleared_at_turn_end(plugin, end):
    assert (await plugin.recall_tool(kind="memory", **CHAT))["success"]
    assert "chat" in plugin._turn_task_replyer
    if end == "send":
        await plugin.before_send(session_id="chat", message="处理好了。")
    elif end == "planner":
        await plugin.after_planner(session_id="chat", output_items=[])
    else:
        await plugin.on_unload()
    assert "chat" not in plugin._turn_task_replyer
    assert await native_reply(plugin) == {"action": "continue"}


@pytest.mark.asyncio
async def test_failed_tool_does_not_steer_reply(plugin):
    assert not (await plugin.recall_tool(kind="invalid", **CHAT))["success"]
    assert await native_reply(plugin) == {"action": "continue"}


@pytest.mark.asyncio
async def test_expired_task_marker_does_not_steer_retry(plugin):
    assert (await plugin.later_tool(action="list", **CHAT))["success"]
    first = (await native_reply(plugin))["modified_kwargs"]
    plugin._turn_task_replyer["chat"]["at"] -= plugin._OPS_RECEIPT_TTL + 1
    assert await native_reply(
        plugin, attempt=2, reply_tool_args=first["reply_tool_args"]
    ) == {"action": "continue"}
