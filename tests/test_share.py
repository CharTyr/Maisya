import asyncio

import pytest
from test_foundation import event, loaded


async def prepared(tmp_path, text="准确来源：https://example.org/a?x=1&y=2", **options):
    plugin = await loaded(tmp_path)
    source = event()
    await plugin.after_receive(message=source)
    calls = []

    async def capability(name, **arguments):
        calls.append((name, arguments))
        if name == "llm.generate_with_tools":
            return {"success": True, "response": text, "tool_calls": []}
        if name == "maisaka.reply.generate":
            if options.get("voice_gate"):
                await options["voice_gate"].wait()
            if options.get("voice_error"):
                raise RuntimeError("voice unavailable")
            return {"success": True, "text": "找到出处了，放这儿。"}
        if name == "message.get_by_id":
            return {
                "success": True,
                "message": {
                    "session_id": options.get("media_chat", "chat"),
                    "message_id": arguments["message_id"],
                    "raw_message": [
                        {"type": "image", "binary_data_base64": "aW1hZ2U="}
                    ],
                },
            }
        assert name == "send.hybrid"
        if options.get("send_error"):
            raise TimeoutError("acknowledgement lost")
        if options.get("normalized_send"):
            return {"sent": True, "message_id": "native-outbound-id"}
        return {"success": True, "sent": True, "message_id": "native-outbound-id"}

    plugin.ctx.call_capability = capability
    result = await plugin.agent_tool(
        action="start", message="Check", chat_id="chat", platform="qq"
    )
    assert result["success"]
    return plugin, result["thread_id"], calls


async def share(plugin, thread_id, **options):
    return await plugin.agent_tool(
        action="share", thread_id=thread_id, chat_id="chat", platform="qq", **options
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["已经核实的纯文字结论", "预览：https://hello.here.now"]
)
async def test_plain_text_and_here_now_link_use_only_native_reply(tmp_path, text):
    plugin, thread_id, calls = await prepared(tmp_path, text)
    result = await share(plugin, thread_id)
    assert result["success"] and result["route"] == "reply"
    assert not any(name.startswith("send.") for name, _ in calls)
    native = await plugin.before_replyer(
        session_id="chat", request_type="maisaka.replyer"
    )
    assert text in __import__("html").unescape(
        native["modified_kwargs"]["extra_prompt"]
    )
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_accurate_link_is_sent_once_in_native_voice_and_blocks_duplicate_reply(
    tmp_path,
):
    plugin, thread_id, calls = await prepared(tmp_path)
    result = await share(plugin, thread_id)
    assert result["success"] and result["sent"]
    sent = [args for name, args in calls if name == "send.hybrid"]
    assert len(sent) == 1
    assert sent[0]["stream_id"] == "chat"
    assert sent[0]["sync_to_maisaka_history"] is True
    assert sent[0]["maisaka_source_kind"] == "guided_reply"
    text = "".join(
        item.get("data", "") for item in sent[0]["segments"] if item["type"] == "text"
    )
    assert "找到出处了" in text and text.count("https://example.org/a?x=1&y=2") == 1
    again = await share(plugin, thread_id)
    assert again["already_handled"]
    assert len([name for name, _ in calls if name == "send.hybrid"]) == 1
    duplicate = await plugin.before_replyer(
        session_id="chat", request_type="maisaka.replyer"
    )
    assert duplicate["action"] == "abort"
    fresh = event("另一个问题")
    fresh["message_id"] = "new-real-message"
    await plugin.after_receive(message=fresh)
    assert (
        await plugin.before_replyer(session_id="chat", request_type="maisaka.replyer")
    )["action"] == "continue"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_existing_native_reply_prevents_second_share_outlet(tmp_path):
    plugin, thread_id, calls = await prepared(tmp_path)
    await plugin.before_replyer(session_id="chat", request_type="maisaka.replyer")
    result = await share(plugin, thread_id)
    assert result["already_handled"] and result["route"] == "reply"
    assert not any(name == "send.hybrid" for name, _ in calls)
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_concurrent_share_calls_and_unknown_ack_never_resend(tmp_path):
    plugin, thread_id, calls = await prepared(tmp_path, send_error=True)
    results = await asyncio.gather(share(plugin, thread_id), share(plugin, thread_id))
    assert any(item.get("status") == "uncertain" for item in results)
    assert len([name for name, _ in calls if name == "send.hybrid"]) == 1
    # A fresh delivery facade uses the same persisted receipt.
    from maisya.share import Share

    plugin.sharing = Share(
        plugin.threads, plugin.bridge, plugin.ctx, plugin.enabled_for
    )
    again = await share(plugin, thread_id)
    assert again["already_handled"]
    assert len([name for name, _ in calls if name == "send.hybrid"]) == 1
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_reference_media_is_scoped_and_only_explicit_handles_are_sent(tmp_path):
    plugin, thread_id, calls = await prepared(tmp_path, "原图说明")
    result = await share(
        plugin,
        thread_id,
        attachment_handles=["message:original:0"],
        ref_msg_ids=["original"],
    )
    assert result["success"]
    sent = next(args for name, args in calls if name == "send.hybrid")
    assert [part["type"] for part in sent["segments"]] == ["reply", "text", "image"]
    assert sent["segments"][0]["data"]["target_message_id"] == "original"
    await plugin.on_unload()
    other, thread_id, calls = await prepared(
        tmp_path / "other", "原图说明", media_chat="different-chat"
    )
    rejected = await share(other, thread_id, attachment_handles=["message:original:0"])
    assert not rejected["success"]
    assert not any(name == "send.hybrid" for name, _ in calls)
    await other.on_unload()


@pytest.mark.asyncio
async def test_voice_failure_uses_planner_expression_and_stop_prevents_late_send(
    tmp_path,
):
    plugin, thread_id, calls = await prepared(tmp_path, voice_error=True)
    result = await share(plugin, thread_id, message="查到了，给你这个来源。")
    assert result["sent"]
    sent = next(args for name, args in calls if name == "send.hybrid")
    assert "查到了" in str(sent["segments"])
    await plugin.on_unload()
    gate = asyncio.Event()
    other, thread_id, calls = await prepared(tmp_path / "late", voice_gate=gate)
    delivery = asyncio.create_task(share(other, thread_id))
    while not any(name == "maisaka.reply.generate" for name, _ in calls):
        await asyncio.sleep(0)
    await other.engine.stop(thread_id, chat_id="chat")
    gate.set()
    result = await delivery
    assert not result.get("sent")
    assert not any(name == "send.hybrid" for name, _ in calls)
    await other.on_unload()


@pytest.mark.asyncio
async def test_share_cannot_target_another_session_or_accept_forged_source(tmp_path):
    plugin, thread_id, calls = await prepared(tmp_path)
    rejected = await plugin.agent_tool(
        action="share", thread_id=thread_id, chat_id="elsewhere", platform="qq"
    )
    assert not rejected["success"]
    rejected = await share(plugin, thread_id, source_msg_id="forged")
    assert not rejected["success"]
    assert not any(name == "send.hybrid" for name, _ in calls)
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_unload_blocks_late_voice_result_and_recovers_unconfirmed_receipt(
    tmp_path,
):
    gate = asyncio.Event()
    plugin, thread_id, calls = await prepared(tmp_path, voice_gate=gate)
    delivery = asyncio.create_task(share(plugin, thread_id))
    while not any(name == "maisaka.reply.generate" for name, _ in calls):
        await asyncio.sleep(0)
    await plugin.on_unload()
    gate.set()
    await asyncio.gather(delivery, return_exceptions=True)
    assert not any(name == "send.hybrid" for name, _ in calls)


@pytest.mark.asyncio
async def test_recalled_old_result_is_available_to_native_replyer(tmp_path):
    plugin, thread_id, _calls = await prepared(tmp_path, "早先核实的事实")
    await plugin.before_replyer(session_id="chat", request_type="maisaka.replyer")
    await plugin.bridge.end_turn("chat")
    await plugin.engine.stop(thread_id, chat_id="chat")
    recall = await plugin.recall_tool(query=thread_id, chat_id="chat", platform="qq")
    assert recall["success"]
    native = await plugin.before_replyer(
        session_id="chat", request_type="maisaka.replyer"
    )
    assert "早先核实的事实" in native["modified_kwargs"]["extra_prompt"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_sdk_detailed_send_receipt_without_success_envelope(tmp_path):
    plugin, thread_id, _calls = await prepared(tmp_path, normalized_send=True)
    delivered = await share(plugin, thread_id)
    assert delivered["sent"] and delivered["message_id"] == "native-outbound-id"
    await plugin.on_unload()
