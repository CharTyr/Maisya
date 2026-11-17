import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from test_admin_one_shot import _boot_plugin, _group_msg, _private_msg, _streams_rpc
from test_bridge import dispatch, setup_bridge
from test_foundation import loaded

from maisya.abilities import Profile
from maisya.trust import Message, MessageStore


def _make_event(
    text: str,
    *,
    msg_id: str,
    user: str,
    session_id: str,
    platform: str = "qq",
    group_id: str = "",
    relay: str = "",
):
    if group_id:
        return _group_msg(
            msg_id,
            text,
            session_id=session_id,
            group_id=group_id,
            user_id=user,
            platform=platform,
            relay=relay,
        )
    return _private_msg(
        msg_id,
        text,
        session_id=session_id,
        user_id=user,
        platform=platform,
        relay=relay,
    )


@pytest.mark.asyncio
async def test_b2_item1_bridge_consume_skips_already_handled_claims(tmp_path):
    _clock, store, engine, bridge, _pulse, _calls = setup_bridge(tmp_path)

    async def cap(name, **arguments):
        assert name == "llm.generate_with_tools"
        return {"success": True, "response": arguments.get("prompt", "ok"), "tool_calls": []}

    engine.ctx = SimpleNamespace(call_capability=cap)
    handled = await dispatch(engine, bridge, wait=0, message="Already handled via share")
    tid_handled = handled["thread_id"]
    await asyncio.wait_for(engine.drain(), timeout=3.0)
    handled_row = store.get(tid_handled, "chat-a")
    rev = handled_row["completed_revision"]
    bridge.record_delivery_part(tid_handled, rev, "chat-a", "share", "sent", "native-msg-1")

    # Simulate an un-consumed return_intents row after share already succeeded
    with store.connect() as db, db:
        db.execute(
            "UPDATE return_intents SET consumed=0 WHERE thread_id=?",
            (tid_handled,),
        )
    assert bridge.claim_delivery(handled_row, "reply").get("already_handled") is True

    assert await asyncio.wait_for(bridge.consume("chat-a"), timeout=3.0) == ""

    # Fresh un-handled result must still be consumed cleanly alongside skipped claim
    fresh = await dispatch(engine, bridge, wait=1, message="Fresh undelivered conclusion")
    await asyncio.wait_for(engine.drain(), timeout=3.0)
    brief = await asyncio.wait_for(bridge.consume("chat-a"), timeout=3.0)
    assert "Fresh undelivered conclusion" in brief
    assert fresh["thread_id"] in brief
    assert "Already handled via share" not in brief


@pytest.mark.asyncio
async def test_b2_item2_later_tool_ops_receipt_replace_aliases_and_daily_limit(tmp_path):
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    priv_sid = "priv_admin_md5"
    plugin = await _boot_plugin(
        tmp_path, permissions=["qq:Alice"], enabled_chats=[group_sid]
    )
    plugin.ctx.call_capability = _streams_rpc()
    fixed_morning_ts = datetime(
        2026, 11, 15, 8, 0, 0, tzinfo=timezone(timedelta(hours=8))
    ).timestamp()
    plugin.intents.clock = lambda: fixed_morning_ts
    try:
        # 2a) Non-admin later_tool calls must NEVER record _turn_ops_receipts
        bob_msg = _group_msg(
            "m-bob-later",
            "提醒我",
            session_id=group_sid,
            group_id="100246809",
            user_id="Bob",
        )
        await plugin.after_receive(message=bob_msg)

        scheduled = await plugin.later_tool(
            action="schedule",
            chat_id=group_sid,
            platform="qq",
            source_msg_id="m-bob-later",
            kind="reminder",
            delay_seconds=300,
            reason="喝水",
        )
        assert scheduled["success"] is True, scheduled
        assert group_sid not in plugin._turn_ops_receipts

        replyer_hook = await plugin.before_replyer(
            session_id=group_sid,
            request_type="maisaka.replyer",
            reply_tool_args={},
            model_name="mai_replier",
        )
        assert replyer_hook.get("modified_kwargs", {}).get("reply_tool_args", {}).get("reply_style") != "长回复"
        assert "model_name" not in replyer_hook.get("modified_kwargs", {})

        cancelled = await plugin.later_tool(
            action="cancel",
            chat_id=group_sid,
            platform="qq",
            source_msg_id="m-bob-later",
            intent_id=scheduled["intent_id"],
        )
        assert cancelled["success"] is True
        assert group_sid not in plugin._turn_ops_receipts

        daily_user = await plugin.later_tool(
            action="schedule",
            chat_id=group_sid,
            platform="qq",
            source_msg_id="m-bob-later",
            daily_at=["09:00"],
            reason="每日打卡",
        )
        assert daily_user["success"] is True
        assert group_sid not in plugin._turn_ops_receipts
        replyer_after_daily = await plugin.before_replyer(
            session_id=group_sid,
            request_type="maisaka.replyer",
            reply_tool_args={},
            model_name="mai_replier",
        )
        assert replyer_after_daily.get("modified_kwargs", {}).get("reply_tool_args", {}).get("reply_style") != "长回复"

        # 2c) daily_at with kind="proactive" and empty profile must NOT bypass daily limits
        daily_proactive = await plugin.later_tool(
            action="schedule",
            chat_id=group_sid,
            platform="qq",
            source_msg_id="m-bob-later",
            kind="proactive",
            profile="",
            daily_at=["10:00"],
            reason="闲聊惦记",
        )
        assert daily_proactive["success"] is True, daily_proactive
        proactive_row = plugin.intents.get(daily_proactive["intent_id"])
        assert not proactive_row["payload"].get("bypass_limits")
        # Attempting a second non-workflow proactive daily_at hits daily_proactive_limit (default 1)
        daily_proactive_over = await plugin.later_tool(
            action="schedule",
            chat_id=group_sid,
            platform="qq",
            source_msg_id="m-bob-later",
            kind="proactive",
            profile="",
            daily_at=["11:00"],
            reason="第二次闲聊惦记",
        )
        assert daily_proactive_over["success"] is False
        assert "上限" in daily_proactive_over.get("error", "")

        # Contrast: verified admin DOES still record ops receipt and triggers 长回复
        alice_priv = _private_msg("m-alice-priv", "设定提醒", session_id=priv_sid, user_id="Alice")
        await plugin.after_receive(message=alice_priv)
        admin_sched = await plugin.later_tool(
            action="schedule",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-alice-priv",
            kind="reminder",
            delay_seconds=300,
            reason="管理员提醒",
        )
        assert admin_sched["success"] is True
        assert priv_sid in plugin._turn_ops_receipts
        admin_replyer = await plugin.before_replyer(
            session_id=priv_sid,
            request_type="maisaka.replyer",
            reply_tool_args={},
            model_name="mai_replier",
        )
        assert admin_replyer["modified_kwargs"]["reply_tool_args"]["reply_style"] == "长回复"
        assert "<maisya_ops_receipt>" in admin_replyer["modified_kwargs"]["extra_prompt"]

        # 2b) replace=True must cancel prior scheduled_admin workflows across chat_aliases_for
        await plugin.resolve_chat_id("100246809", current_chat_id=priv_sid, current_platform="qq")
        if not (plugin.profiles.path / "digest").is_dir():
            plugin.profiles.clone("default", "digest")
        digest_doc = plugin.memory.read("profile", target="digest", document="profile.toml")
        plugin.memory.write(
            "profile",
            digest_doc["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=digest_doc["version"],
            allow_custom=True,
        )
        old_alias_id = plugin.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:qq:group:100246809",
            due_at=plugin.intents.clock() + 1800,
            reason="旧早报",
            payload={
                "profile": "digest",
                "workspace": "digest",
                "prompt": "旧定时",
                "every_seconds": 3600,
                "bypass_limits": True,
                "scheduled_admin": True,
            },
        )
        replaced = await plugin.later_tool(
            action="schedule",
            chat_id=priv_sid,
            platform="qq",
            target_chat=group_sid,
            source_msg_id="m-alice-priv",
            profile="digest",
            daily_at=["08:30"],
            replace=True,
            prompt="新早报",
        )
        assert replaced["success"] is True, replaced
        assert old_alias_id["intent_id"] in (replaced.get("cancelled_ids") or [])
        assert plugin.intents.get(old_alias_id["intent_id"])["status"] == "cancelled"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_item3_vault_gates_redaction_and_outcomes(tmp_path):
    plugin = await _boot_plugin(tmp_path, permissions=["qq:Alice"], enabled_chats=[])
    try:
        # (a) Group chat /vault rejected
        grp_ev = _group_msg(
            "m-vault-grp",
            "/vault set api_key secret-grp-111",
            session_id="grp_vault_test",
            user_id="Alice",
        )
        grp_rx = await plugin.after_receive(message=grp_ev)
        assert "secret-grp-111" not in json.dumps(grp_rx, ensure_ascii=False)
        assert plugin.vault.handles() == []
        ok_g, msg_g, lvl_g = await plugin.vault_command(message=grp_ev)
        assert (ok_g, lvl_g) == (False, 2)
        assert "secret-grp-111" not in msg_g

        # (b) Non-admin private /vault rejected
        bob_priv = _private_msg(
            "m-vault-bob",
            "/vault set api_key secret-bob-222",
            session_id="priv_bob",
            user_id="Bob",
        )
        bob_rx = await plugin.after_receive(message=bob_priv)
        assert "secret-bob-222" not in json.dumps(bob_rx, ensure_ascii=False)
        assert plugin.vault.handles() == []
        ok_b, msg_b, lvl_b = await plugin.vault_command(message=bob_priv)
        assert (ok_b, lvl_b) == (False, 2)
        assert "secret-bob-222" not in msg_b

        # (c) Disabled plugin rejected while still redacting secret
        plugin.settings.plugin.enabled = False
        dis_ev = _private_msg(
            "m-vault-dis",
            "/vault set api_key secret-dis-333",
            session_id="priv_admin_md5",
            user_id="Alice",
        )
        dis_rx = await plugin.after_receive(message=dis_ev)
        assert "secret-dis-333" not in json.dumps(dis_rx, ensure_ascii=False)
        assert plugin.vault.handles() == []
        ok_d, msg_d, lvl_d = await plugin.vault_command(message=dis_ev)
        assert (ok_d, lvl_d) == (False, 2)
        assert "secret-dis-333" not in msg_d
        plugin.settings.plugin.enabled = True

        # (d) Bad syntax rejected
        bad_set = _private_msg(
            "m-vault-syntax-1", "/vault set only_handle", session_id="priv_admin_md5", user_id="Alice"
        )
        await plugin.after_receive(message=bad_set)
        ok_s1, msg_s1, lvl_s1 = await plugin.vault_command(message=bad_set)
        assert (ok_s1, lvl_s1) == (False, 2)
        assert "用法" in msg_s1

        bad_del = _private_msg(
            "m-vault-syntax-2", "/vault delete my_key extra", session_id="priv_admin_md5", user_id="Alice"
        )
        await plugin.after_receive(message=bad_del)
        ok_s2, _msg_s2, lvl_s2 = await plugin.vault_command(message=bad_del)
        assert (ok_s2, lvl_s2) == (False, 2)

        # (e) VaultError on invalid handle rejected without leaking secret
        err_ev = _private_msg(
            "m-vault-err",
            "/vault set ../bad secret-err-444",
            session_id="priv_admin_md5",
            user_id="Alice",
        )
        err_rx = await plugin.after_receive(message=err_ev)
        assert "secret-err-444" not in json.dumps(err_rx, ensure_ascii=False)
        ok_e, msg_e, lvl_e = await plugin.vault_command(message=err_ev)
        assert (ok_e, lvl_e) == (False, 2)
        assert "secret-err-444" not in msg_e
        assert plugin.vault.handles() == []

        # (f) Direct vault_command call without prior after_receive is still safely gated
        direct_non_admin = _private_msg(
            "m-vault-direct-bob",
            "/vault set exa_key secret-direct-000",
            session_id="priv_bob",
            user_id="Bob",
        )
        ok_dir, msg_dir, lvl_dir = await plugin.vault_command(message=direct_non_admin)
        assert (ok_dir, lvl_dir) == (False, 2)
        assert "secret-direct-000" not in msg_dir
        assert plugin.vault.handles() == []

        host_obj_msg = SimpleNamespace(
            message_id="m-vault-obj-bob",
            session_id="priv_bob",
            platform="qq",
            processed_plain_text="/vault set exa_key secret-obj-999",
            message_info=SimpleNamespace(
                user_info=SimpleNamespace(user_id="Bob"),
                group_info=None,
                additional_config=SimpleNamespace(relayed_by_plugin=""),
            ),
        )
        ok_obj, msg_obj, lvl_obj = await plugin.vault_command(message=host_obj_msg)
        assert (ok_obj, lvl_obj) == (False, 2)
        assert "secret-obj-999" not in msg_obj

        # (g) Valid admin private set & delete succeed cleanly
        ok_ev = _private_msg(
            "m-vault-ok",
            "/vault set exa_key secret-ok-555",
            session_id="priv_admin_md5",
            user_id="Alice",
        )
        ok_rx = await plugin.after_receive(message=ok_ev)
        assert "secret-ok-555" not in json.dumps(ok_rx, ensure_ascii=False)
        ok_set, msg_set, lvl_set = await plugin.vault_command(message=ok_ev)
        assert (ok_set, lvl_set) == (True, 2)
        assert "secret-ok-555" not in msg_set
        assert plugin.vault.for_execution("exa_key") == "secret-ok-555"

        del_ev = _private_msg(
            "m-vault-del",
            "/vault delete exa_key",
            session_id="priv_admin_md5",
            user_id="Alice",
        )
        await plugin.after_receive(message=del_ev)
        ok_del, _msg_del, lvl_del = await plugin.vault_command(message=del_ev)
        assert (ok_del, lvl_del) == (True, 2)
        assert "exa_key" not in plugin.vault.handles()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_item4_entry_routes_console_locks_and_quota(tmp_path):
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    priv_sid = "priv_admin_md5"
    plugin = await _boot_plugin(
        tmp_path, permissions=["qq:Alice"], enabled_chats=[group_sid]
    )
    calls = []

    async def capability(name, **arguments):
        calls.append((name, arguments))
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "查看链接 https://example.org/b2", "tool_calls": []}
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "链接在这里：https://example.org/b2"}
        if name == "send.hybrid":
            return {"success": True, "sent": True, "message_id": "sent-msg-1"}
        return await _streams_rpc()(name, **arguments)

    plugin.ctx.call_capability = capability
    try:
        # 4a) Un-enabled private admin chat send(deliver_to="auto") on a deliver_to="target" group thread
        # must stay "target", NOT coerce to "current"
        await plugin.after_receive(
            message=_private_msg("m-priv-1", "开始任务", session_id=priv_sid, user_id="Alice")
        )
        await plugin.after_receive(
            message=_private_msg("m-priv-2", "补充任务", session_id=priv_sid, user_id="Alice")
        )
        started_target = await plugin.agent_tool(
            action="start",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-1",
            target_chat=group_sid,
            deliver_to="target",
            message="查群资料",
            wait_seconds=0,
        )
        assert started_target["success"] is True, started_target
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        await asyncio.wait_for(plugin.bridge.consume(group_sid), timeout=3.0)

        sent_auto = await plugin.agent_tool(
            action="send",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-2",
            thread_id=started_target["thread_id"],
            deliver_to="auto",
            message="继续补充",
            wait_seconds=10,
        )
        assert sent_auto["success"] is True, sent_auto
        assert await plugin.bridge.consume(priv_sid) == ""
        assert "delivery_intent_id" in sent_auto
        assert (
            plugin.intents.get(sent_auto["delivery_intent_id"])["target"]
            == f"chat:{group_sid}"
        )
        # Drain sent_auto's thread result in group_sid before checking sent_inherit isolation
        await asyncio.wait_for(plugin.bridge.consume(group_sid), timeout=3.0)

        # Positive contrast: thread originally started with deliver_to="current" still inherits "current"
        started_cur = await plugin.agent_tool(
            action="start",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-1",
            target_chat=group_sid,
            deliver_to="current",
            message="私聊试跑",
            wait_seconds=0,
        )
        assert started_cur["success"] is True, started_cur
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        await asyncio.wait_for(plugin.bridge.consume(priv_sid), timeout=3.0)
        sent_inherit = await plugin.agent_tool(
            action="send",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-2",
            thread_id=started_cur["thread_id"],
            deliver_to="auto",
            message="继续试跑",
            wait_seconds=10,
        )
        assert sent_inherit["success"] is True, sent_inherit
        assert "delivery_intent_id" not in sent_inherit
        assert "查看链接" in await plugin.bridge.consume(priv_sid)
        assert await plugin.bridge.consume(group_sid) == ""

        # Explicitly switching preview thread to deliver_to="target" clears admin_direct stickiness,
        # so subsequent deliver_to="auto" stays on "target" instead of bouncing back to "current"
        switched_target = await plugin.agent_tool(
            action="send",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-2",
            thread_id=started_cur["thread_id"],
            deliver_to="target",
            message="改成正式发群",
            wait_seconds=0,
        )
        assert switched_target["success"] is True, switched_target
        assert "delivery_intent_id" in switched_target
        assert (
            plugin.threads.get(started_cur["thread_id"], group_sid)["grant"].get(
                "admin_direct"
            )
            is False
        )
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        await asyncio.wait_for(plugin.bridge.consume(group_sid), timeout=3.0)

        after_switch_auto = await plugin.agent_tool(
            action="send",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-2",
            thread_id=started_cur["thread_id"],
            deliver_to="auto",
            message="切正式后再 auto 续聊",
            wait_seconds=10,
        )
        assert after_switch_auto["success"] is True, after_switch_auto
        assert "delivery_intent_id" in after_switch_auto
        assert (
            plugin.intents.get(after_switch_auto["delivery_intent_id"])["target"]
            == f"chat:{group_sid}"
        )
        assert await plugin.bridge.consume(priv_sid) == ""
        await asyncio.wait_for(plugin.bridge.consume(group_sid), timeout=3.0)

        # 4b) action="share" with deliver_to="current" delivers to current private chat;
        # deliver_to="target" delivers to target group; deliver_to="none" fails
        started_none = await plugin.agent_tool(
            action="start",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-1",
            target_chat=group_sid,
            deliver_to="none",
            message="后台取链接",
            wait_seconds=10,
        )
        assert started_none["success"] is True, started_none
        calls.clear()
        shared_cur = await plugin.agent_tool(
            action="share",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-2",
            thread_id=started_none["thread_id"],
            target_chat=group_sid,
            deliver_to="current",
            message="先发给我预览",
        )
        assert shared_cur["success"] is True, shared_cur
        sent_cur = [args for name, args in calls if name == "send.hybrid"]
        assert len(sent_cur) == 1
        assert sent_cur[0]["stream_id"] == priv_sid

        started_target2 = await plugin.agent_tool(
            action="start",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-1",
            target_chat=group_sid,
            deliver_to="none",
            message="再取一个链接",
            wait_seconds=10,
        )
        assert started_target2["success"] is True
        calls.clear()
        shared_tgt = await plugin.agent_tool(
            action="share",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-2",
            thread_id=started_target2["thread_id"],
            target_chat=group_sid,
            deliver_to="target",
            message="发到目标群",
        )
        assert shared_tgt["success"] is True, shared_tgt
        sent_tgt = [args for name, args in calls if name == "send.hybrid"]
        assert len(sent_tgt) == 1
        assert sent_tgt[0]["stream_id"] == group_sid

        bad_share = await plugin.agent_tool(
            action="share",
            chat_id=priv_sid,
            platform="qq",
            source_msg_id="m-priv-2",
            thread_id=started_none["thread_id"],
            target_chat=group_sid,
            deliver_to="none",
            message="不应投递",
        )
        assert bad_share["success"] is False

        # 4c) console_send preserves explicit custom workspace name
        if not (plugin.profiles.path / "digest").is_dir():
            plugin.profiles.clone("default", "digest")
        digest_doc = plugin.memory.read("profile", target="digest", document="profile.toml")
        plugin.memory.write(
            "profile",
            digest_doc["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=digest_doc["version"],
            allow_custom=True,
        )
        con_res = await plugin.console_send(
            profile="digest",
            chat_id=group_sid,
            workspace="digest-feeds",
            message="开始整理早报",
        )
        assert con_res["success"] is True, con_res
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        con_row = plugin.threads.get(con_res["thread_id"], group_sid)
        assert con_row is not None and con_row.get("workspace_id")
        with plugin.threads.connect() as db:
            ws_row = db.execute(
                "SELECT name FROM agent_workspaces WHERE workspace_id=?",
                (con_row["workspace_id"],),
            ).fetchone()
        assert ws_row is not None and ws_row["name"] == "digest-feeds"

        # 4d) _bootstrap_lock concurrent safety + _relayed_bootstrap_sources clearing
        plugin._record_relayed_bootstrap_source(
            Message("m-relay-1", "tmp_chat", "qq", "Bot", True, False, "", "", "")
        )
        assert plugin._relayed_bootstrap_sources
        base_cfg = plugin.settings.model_dump()
        await plugin.on_config_update(
            "self",
            {**base_cfg, "plugin": {**base_cfg["plugin"], "enabled": False}},
            0,
        )
        assert plugin._relayed_bootstrap_sources == {}
        await plugin.on_config_update(
            "self",
            {**base_cfg, "plugin": {**base_cfg["plugin"], "enabled": True}},
            0,
        )

        async def _writer():
            for idx in range(40):
                msg = Message(
                    f"boot-w-{idx}",
                    f"boot-sess-{idx}",
                    "qq",
                    f"user-{idx}",
                    False,
                    False,
                    f"grp-{idx}" if idx % 2 == 0 else "",
                    "",
                    "",
                )
                plugin._record_bootstrap_message(msg)
                await asyncio.sleep(0)

        async def _reader():
            for idx in range(40):
                await asyncio.to_thread(plugin.chat_aliases_for, f"boot-sess-{idx}", "qq")
                await asyncio.to_thread(plugin.canonical_stream_spec, f"boot-sess-{idx}", "qq")
                await asyncio.to_thread(plugin._lookup_message, f"boot-w-{idx}", f"boot-sess-{idx}", "qq")

        await asyncio.wait_for(
            asyncio.gather(_writer(), _reader(), _reader()), timeout=3.0
        )

        # 4e) console_query 的今日主动额度按全局总量计（docs/08 第四节），分舞台桶仅作展示；忽略 failed/cancelled
        await plugin.on_config_update(
            "self", {**base_cfg, "daily_proactive_limit": 5, "per_chat_daily_proactive_limit": 5}, 0
        )
        now = plugin.intents.clock()
        plugin.messages.upsert_route("priv_quota_sid", platform="qq", chat_type="private", user_id="UserQ")
        g_sent = plugin.intents.schedule(
            kind="proactive", source="serendipity", target=f"chat:{group_sid}", due_at=now - 1, reason="g1"
        )
        g_fail = plugin.intents.schedule(
            kind="proactive", source="serendipity", target=f"chat:{group_sid}", due_at=now - 1, reason="g2"
        )
        g_cancel = plugin.intents.schedule(
            kind="proactive", source="serendipity", target=f"chat:{group_sid}", due_at=now - 1, reason="g3"
        )
        p_sent1 = plugin.intents.schedule(
            kind="proactive", source="serendipity", target="chat:priv_quota_sid", due_at=now - 1, reason="p1"
        )
        p_sent2 = plugin.intents.schedule(
            kind="proactive", source="serendipity", target="chat:priv_quota_sid", due_at=now - 1, reason="p2"
        )
        plugin.intents.cancel(g_cancel["intent_id"], f"chat:{group_sid}")
        for iid, st, ws in (
            (g_sent["intent_id"], "done", "sent"),
            (g_fail["intent_id"], "failed", "failed"),
            (p_sent1["intent_id"], "done", "sent"),
            (p_sent2["intent_id"], "done", "sent"),
        ):
            claimed = plugin.intents.due(lambda *_: True, limit=10)
            assert any(row["intent_id"] == iid for row in claimed)
            plugin.intents.finish(iid, st, ws)

        q_all = await plugin.console_query()
        assert q_all["pulse"]["daily_used"] == 3
        assert q_all["pulse"]["daily_buckets"] == {"group": 1, "private": 2}

        q_grp = await plugin.console_query(chat_id=group_sid)
        assert q_grp["pulse"]["daily_used"] == 3
        assert q_grp["pulse"]["daily_buckets"] == {"group": 1, "private": 2}

        q_priv = await plugin.console_query(chat_id="priv_quota_sid")
        assert q_priv["pulse"]["daily_used"] == 3
        assert q_priv["pulse"]["daily_buckets"] == {"group": 1, "private": 2}

        plugin._record_relayed_bootstrap_source(
            Message("m-relay-2", "tmp_chat2", "qq", "Bot", True, False, "", "", "")
        )
        await plugin.on_unload()
        assert plugin._relayed_bootstrap_sources == {}
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_item5_aliases_for_uid_and_multi_alias_unreplied_unlock(tmp_path):
    # 5a) MessageStore.aliases_for includes bare user_id for private chats, case-sensitive
    store = MessageStore(tmp_path / "messages.db")
    store.upsert_route("md5_priv_case", platform="QQ", chat_type="private", user_id="CaseUser_77")
    aliases = store.aliases_for("md5_priv_case")
    assert aliases == {
        "md5_priv_case",
        "CaseUser_77",
        "private:CaseUser_77",
        "qq:CaseUser_77",
        "qq:private:CaseUser_77",
    }
    assert "caseuser_77" not in aliases

    # 5b) Multi-alias unreplied_lock unlock in after_receive (private) and after_planner (group)
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    priv_user_sid = "priv_bob_md5"
    plugin = await _boot_plugin(
        tmp_path / "plugin",
        permissions=["qq:Alice"],
        enabled_chats=[group_sid, priv_user_sid],
    )
    plugin.ctx.call_capability = _streams_rpc()
    try:
        plugin.messages.upsert_route(priv_user_sid, platform="qq", chat_type="private", user_id="Bob")
        await plugin.resolve_chat_id("100246809", current_chat_id=priv_user_sid, current_platform="qq")

        # Lock private aliases by stream spec & bare uid, unlock via md5 session_id message
        plugin.intents.mark_proactive("chat:qq:private:Bob")
        plugin.intents.mark_proactive("chat:Bob")
        assert not plugin.intents.social_gate("chat:qq:private:Bob")[0]
        assert not plugin.intents.social_gate("chat:Bob")[0]

        await plugin.after_receive(
            message=_make_event(
                "在吗",
                msg_id="m-bob-priv",
                user="Bob",
                session_id=priv_user_sid,
            )
        )
        assert plugin.intents.social_gate("chat:qq:private:Bob")[0]
        assert plugin.intents.social_gate("chat:Bob")[0]
        assert plugin.intents.social_gate(f"chat:{priv_user_sid}")[0]

        # Lock group aliases by stream spec & raw group_id; after_receive preserves locked state
        plugin.intents.mark_proactive("chat:qq:group:100246809")
        plugin.intents.mark_proactive("chat:100246809")
        await plugin.after_receive(
            message=_make_event(
                "群里大家好",
                msg_id="m-bob-grp",
                user="Bob",
                session_id=group_sid,
                group_id="100246809",
            )
        )
        assert not plugin.intents.social_gate("chat:qq:group:100246809")[0]
        assert not plugin.intents.social_gate("chat:100246809")[0]

        # before_planner + after_planner with reply unlocks ALL group aliases
        await plugin.before_planner(
            session_id=group_sid,
            items=[],
            tool_definitions=[{"type": "function", "function": {"name": "reply"}}],
        )
        await plugin.after_planner(
            session_id=group_sid,
            output_items=[{"tool_call": {"func_name": "reply", "args": {"text": "收到"}}}],
        )
        assert plugin.intents.social_gate("chat:qq:group:100246809")[0]
        assert plugin.intents.social_gate("chat:100246809")[0]
        assert plugin.intents.social_gate(f"chat:{group_sid}")[0]
    finally:
        await plugin.on_unload()
