import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import safe_symlink
from pydantic import ValidationError
from test_foundation import event, loaded

from maisya.abilities import Profile
from maisya.bridge import Bridge
from maisya.config import Settings, initialize_data
from maisya.pulse import Pulse
from maisya.scout import Scout
from maisya.share import Share
from maisya.threads import ThreadEngine, ThreadStore
from maisya.vault import VaultError


def _make_event(
    text: str,
    *,
    msg_id: str,
    user: str,
    session_id: str,
    platform: str = "qq",
    group_id: str = "",
    relay: str = "",
    ts: float | None = None,
):
    now = time.time() if ts is None else ts
    payload = event(text, user=user, platform=platform, group=bool(group_id), relay=relay)
    payload["message_id"] = msg_id
    payload["session_id"] = session_id
    payload["timestamp"] = str(now)
    if group_id:
        payload["message_info"]["group_info"] = {"group_id": group_id}
    return payload


@pytest.mark.asyncio
async def test_item1_manifest_has_get_avatar_and_failure_logs_with_short_backoff(
    tmp_path, caplog, monkeypatch
):
    manifest = json.loads(Path("_manifest.json").read_text(encoding="utf-8"))
    assert "chat.get_avatar" in manifest["capabilities"]

    plugin = await loaded(tmp_path)
    try:
        plugin.settings.enabled_chats = ["group_err", "group_exc"]
        avatar_calls: list[str] = []
        now_mono = [1000.0]
        monkeypatch.setattr("plugin.time.monotonic", lambda: now_mono[0])

        async def capability(name, **arguments):
            if name == "chat.get_all_streams":
                return {
                    "success": True,
                    "streams": [
                        {
                            "session_id": "group_err",
                            "platform": "qq",
                            "group_id": "201",
                            "group_name": "Err",
                            "chat_type": "group",
                            "is_group_session": True,
                        },
                        {
                            "session_id": "group_exc",
                            "platform": "qq",
                            "group_id": "202",
                            "group_name": "Exc",
                            "chat_type": "group",
                            "is_group_session": True,
                        },
                    ],
                }
            if name == "chat.get_avatar":
                tid = str(arguments.get("target_id") or "")
                avatar_calls.append(tid)
                if tid == "201":
                    return {"success": False, "error": "upstream denied"}
                raise RuntimeError("avatar transport boom")
            return {"success": True}

        plugin.ctx.call_capability = capability
        with caplog.at_level(logging.WARNING, logger="test.maisya"):
            snap = await plugin.console_query()
        assert snap["chat_sources"]["group_err"]["avatar"] == ""
        assert snap["chat_sources"]["group_exc"]["avatar"] == ""
        warn_text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert "upstream denied" in warn_text
        assert "avatar transport boom" in warn_text

        # Within short backoff (2s), cached empty avoids storming upstream
        now_mono[0] += 2.0
        await plugin.console_query()
        assert avatar_calls.count("201") == 1
        assert avatar_calls.count("202") == 1

        # Well under old 180s (e.g. 20s), failure cache expires so transient error recovers
        now_mono[0] += 25.0
        await plugin.console_query()
        assert avatar_calls.count("201") == 2
        assert avatar_calls.count("202") == 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_item2_before_send_redacts_all_fields_for_admin_disabled_direct_and_mixed(
    tmp_path,
):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        secret = "sk-b1-audit-secret-value-7890"
        plugin.vault.set("b1_token", secret)

        # 1) Enabled chat + Admin turn + mixed batch (bob + alice) -> MUST redact message/segments/processed_plain_text
        bob = _make_event("hi", msg_id="m-bob", user="Bob", session_id="chat", group_id="group")
        alice = _make_event("hi", msg_id="m-alice", user="Alice", session_id="chat", group_id="group")
        await plugin.after_receive(message=bob)
        await plugin.after_receive(message=alice)

        res_admin = await plugin.before_send(
            session_id="chat",
            platform="qq",
            message={
                "session_id": "chat",
                "platform": "qq",
                "processed_plain_text": f"@全体成员 {secret}",
                "raw_message": [{"type": "text", "data": f"@全体成员 {secret}"}],
            },
            segments=[{"type": "text", "data": f"@全体成员 key={secret}"}],
            processed_plain_text=f"@全体成员 key={secret}",
        )
        mk = res_admin["modified_kwargs"]
        assert secret not in str(mk["message"])
        assert secret not in str(mk["segments"])
        assert secret not in mk["processed_plain_text"]
        assert "@全体成员" in mk["processed_plain_text"]

        # 2) Disabled session -> MUST redact all three fields
        res_disabled = await plugin.before_send(
            session_id="unlisted_chat",
            platform="qq",
            message={"processed_plain_text": f"leak {secret}", "raw_message": [{"type": "text", "data": secret}]},
            segments=[{"type": "text", "data": f"seg {secret}"}],
            processed_plain_text=f"plain {secret}",
        )
        mk2 = res_disabled["modified_kwargs"]
        assert secret not in str(mk2["message"])
        assert secret not in str(mk2["segments"])
        assert secret not in mk2["processed_plain_text"]

        # 3) Empty session_id -> MUST still redact all three fields
        res_empty = await plugin.before_send(
            message={"processed_plain_text": secret},
            segments=[{"type": "text", "data": secret}],
            processed_plain_text=secret,
        )
        mk3 = res_empty["modified_kwargs"]
        assert secret not in str(mk3["message"])
        assert secret not in str(mk3["segments"])
        assert secret not in mk3["processed_plain_text"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_item3_private_to_group_blocked_even_with_deliver_to_current_or_inherit_current(
    tmp_path,
):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        grp = "qq:group:100246809"
        priv = "qq:private:Alice"
        # Group is enabled; private is unlisted (admin direct) to also exercise inherit_current
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": [grp],
            }
        )
        await plugin.on_load()

        async def capability(name, **arguments):
            return {"success": True, "response": "done", "tool_calls": []}

        plugin.ctx.call_capability = capability
        await plugin.after_receive(
            message=_make_event("私聊查", msg_id="m_priv", user="Alice", session_id=priv)
        )
        await plugin.after_receive(
            message=_make_event("群聊建", msg_id="m_grp", user="Alice", session_id=grp, group_id="100246809")
        )
        priv_thread = await plugin.agent_tool(
            action="start",
            message="私聊隐私上下文",
            source_msg_id="m_priv",
            chat_id=priv,
            platform="qq",
        )
        assert priv_thread["success"]
        grp_thread = await plugin.agent_tool(
            action="start",
            message="群聊公开任务",
            source_msg_id="m_grp",
            chat_id=grp,
            platform="qq",
        )
        assert grp_thread["success"]

        # 1) Cross-chat start targeting group with deliver_to="current" MUST block private from_threads
        blocked_start = await plugin.agent_tool(
            action="start",
            target_chat=grp,
            deliver_to="current",
            message="把私聊线程带入群预览",
            from_threads=[priv_thread["thread_id"]],
            source_msg_id="m_priv",
            chat_id=priv,
            platform="qq",
        )
        assert not blocked_start["success"]
        assert "私聊线程结果默认不可直接注入群聊线程" in blocked_start["error"]

        # 2) Cross-chat send to existing group thread with deliver_to="current" MUST block private from_threads
        blocked_send_current = await plugin.agent_tool(
            action="send",
            thread_id=grp_thread["thread_id"],
            deliver_to="current",
            message="注入群线程",
            from_threads=[priv_thread["thread_id"]],
            source_msg_id="m_priv",
            chat_id=priv,
            platform="qq",
        )
        assert not blocked_send_current["success"]
        assert "私聊线程结果默认不可直接注入群聊线程" in blocked_send_current["error"]

        # 3) Cross-chat send with deliver_to="auto" triggering inherit_current MUST also block private from_threads
        blocked_send_auto = await plugin.agent_tool(
            action="send",
            thread_id=grp_thread["thread_id"],
            deliver_to="auto",
            message="隐式回私聊注入群线程",
            from_threads=[priv_thread["thread_id"]],
            source_msg_id="m_priv",
            chat_id=priv,
            platform="qq",
        )
        assert not blocked_send_auto["success"]
        assert "私聊线程结果默认不可直接注入群聊线程" in blocked_send_auto["error"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_item4_turn_sources_ops_receipts_max_age_and_relayed_exclusion(
    tmp_path,
):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        clock = [time.time()]
        plugin.messages.clock = lambda: clock[0]
        plugin.bridge.clock = lambda: clock[0]
        grp = "qq:group:100246809"
        priv = "qq:private:Alice"
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": [grp],
            }
        )
        await plugin.on_load()
        plugin.messages.clock = lambda: clock[0]
        plugin.bridge.clock = lambda: clock[0]
        (plugin.profiles.path / "admin_only").mkdir(parents=True, exist_ok=True)
        (plugin.profiles.path / "admin_only/PROFILE.md").write_text("admin\n", encoding="utf-8")
        (plugin.profiles.path / "admin_only/profile.toml").write_text(
            'access = "admins"\nenvironment = "readonly"\n', encoding="utf-8"
        )
        tools = [
            {"type": "function", "function": {"name": name}}
            for name in ["reply", "maisya_agent", "maisya_shape", "maisya_later"]
        ]

        # 4e) Relayed admin message MUST NOT enter _pending_sources
        relayed = _make_event(
            "代投管理员消息",
            msg_id="m-relayed",
            user="Alice",
            session_id=grp,
            group_id="100246809",
            relay="other.plugin",
            ts=clock[0],
        )
        await plugin.after_receive(message=relayed)
        assert "m-relayed" not in (plugin._pending_sources.get(grp) or [])

        # 4a) Normal non-stop/wait reply in after_planner MUST clear _turn_sources
        await plugin.after_receive(
            message=_make_event(
                "正常管理员提问", msg_id="m-grp-adm", user="Alice", session_id=grp, group_id="100246809", ts=clock[0]
            )
        )
        await plugin.before_planner(session_id=grp, items=[], tool_definitions=tools)
        assert plugin._turn_sources.get(grp) == ["m-grp-adm"]
        await plugin.after_planner(
            session_id=grp,
            output_items=[{"tool_call": {"func_name": "reply", "args": {"text": "好"}}}],
        )
        assert grp not in plugin._turn_sources
        next_plan = await plugin.before_planner(session_id=grp, items=[], tool_definitions=tools)
        next_names = {(t.get("function") or t).get("name") for t in next_plan["modified_kwargs"]["tool_definitions"]}
        assert "maisya_shape" not in next_names

        # 4b) Pulse proactive wakeup clears residual _turn_sources even if after_planner hadn't run
        plugin._turn_sources[grp] = ["m-grp-adm"]
        pulse_plan = await plugin.before_planner(
            session_id=grp,
            items=[
                {
                    "item_type": "UserMessageItem",
                    "parts": [{"type": "text", "text": "<plugin_proactive_task>回访</plugin_proactive_task>"}],
                }
            ],
            tool_definitions=tools,
        )
        assert grp not in plugin._turn_sources
        pulse_names = {(t.get("function") or t).get("name") for t in pulse_plan["modified_kwargs"]["tool_definitions"]}
        assert "maisya_shape" not in pulse_names

        # 4d) Unconsumed _turn_ops_receipts MUST survive incoming user message in before_planner
        plugin._record_ops_receipt(grp, "shape:patch")
        await plugin.after_receive(
            message=_make_event(
                "插话", msg_id="m-bob", user="Bob", session_id=grp, group_id="100246809", ts=clock[0]
            )
        )
        await plugin.before_planner(session_id=grp, items=[], tool_definitions=tools)
        assert grp in plugin._turn_ops_receipts
        assert plugin._turn_ops_receipts[grp]["consumed"] is False
        await plugin.after_planner(
            session_id=grp,
            output_items=[{"tool_call": {"func_name": "reply", "args": {"text": "好的"}}}],
        )

        # 4c) Continuing admin_direct private turn >300s uses _admin_source_max_age (3600s) at 3176 & 3275
        async def capability(name, **arguments):
            return {"success": True, "response": "ok", "tool_calls": []}

        plugin.ctx.call_capability = capability
        await plugin.after_receive(
            message=_make_event("私聊试跑", msg_id="m-priv-adm", user="Alice", session_id=priv, ts=clock[0])
        )
        gate_req = await plugin.agent_tool(
            action="start",
            target_chat=grp,
            deliver_to="current",
            message="后台跑个任务",
            wait_seconds=0,
            source_msg_id="m-priv-adm",
            chat_id=priv,
            platform="qq",
        )
        assert gate_req["success"]
        await plugin.before_planner(session_id=priv, items=[], tool_definitions=tools)
        # Step 1 calls maisya_recall (turn continues, not reply/stop/wait)
        await plugin.after_planner(
            session_id=priv,
            output_items=[{"tool_call": {"func_name": "maisya_recall", "args": {"query": "x"}}}],
        )
        clock[0] += 450.0  # >300s, <3600s while admin_direct thread is active
        step2 = await plugin.before_planner(session_id=priv, items=[], tool_definitions=tools)
        assert plugin._turn_sources.get(priv) == ["m-priv-adm"]
        step2_text = "\n".join(p["text"] for p in step2["modified_kwargs"]["items"][-1]["parts"])
        assert "admin_only" in step2_text
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_item5_bridge_rows_1800s_alignment_and_claim_ready_backoff_retry(tmp_path):
    clock = SimpleNamespace(now=1000.0)
    db_path = tmp_path / "bridge.db"
    store = ThreadStore(db_path)
    bridge = Bridge(store, clock=lambda: clock.now)

    async def llm_cap(name, **_kwargs):
        return {"success": True, "response": "slow result finished", "tool_calls": []}

    engine = ThreadEngine(store, Scout(SimpleNamespace(call_capability=llm_cap)), idle_seconds=0)
    ability = Profile(id="scout", environment="readonly")
    route = await bridge.route("chat-a", "user-msg-1", 0)
    await engine.start(
        ability,
        "Slow job",
        chat_id="chat-a",
        platform="qq",
        user_id="Alice",
        wait_seconds=0,
        return_route=route,
    )
    await bridge.submitted(route)
    await engine.drain()

    # 5a) Advance 900s (>600s, <1800s): MUST still claim & offer cleanly!
    clock.now += 900.0
    wake_mode = ["rejected"]
    calls: list[tuple[str, dict]] = []

    async def capability(name, **arguments):
        calls.append((name, arguments))
        if wake_mode[0] == "rejected":
            return {"success": False, "error": "host busy"}
        if wake_mode[0] == "uncertain":
            raise TimeoutError("ack lost")
        return {"success": True, "queued": True}

    plugin_active = [False]
    pulse = Pulse(
        bridge,
        SimpleNamespace(call_capability=capability),
        lambda *_: True,
        plugin_enabled=lambda: plugin_active[0],
    )

    # 5b.1) Temporary skip (plugin_enabled=False) MUST NOT lock wake_attempted
    await pulse.tick()
    assert calls == []
    plugin_active[0] = True
    await pulse.tick()
    assert len(calls) == 1  # RPC called & rejected

    # Immediate tick within backoff does not stampede
    await pulse.tick()
    assert len(calls) == 1

    # After backoff (20s), rejected group is reclaimed and accepted
    wake_mode[0] = "accepted"
    clock.now += 20.0
    await pulse.tick()
    assert len(calls) == 2
    offered = await bridge.offer("chat-a")
    assert len(offered) == 1 and "slow result finished" in str(offered[0])

    # Accepted group is terminal and never re-wakes
    clock.now += 50.0
    await pulse.tick()
    assert len(calls) == 2
    await engine.close()


@pytest.mark.asyncio
async def test_item6_after_planner_parallel_validation_failure_order_and_target_alias(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        clock = [1000.0]
        plugin.bridge.clock = lambda: clock[0]
        plugin.messages.clock = lambda: clock[0]
        grp = "qq:group:100246809"
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": [grp],
            }
        )
        await plugin.on_load()
        plugin.bridge.clock = lambda: clock[0]
        plugin.messages.clock = lambda: clock[0]

        wake_calls: list[dict] = []

        async def capability(name, **arguments):
            if name == "llm.generate_with_tools":
                return {"success": True, "response": "sibling ok", "tool_calls": []}
            if name == "maisaka.proactive.trigger":
                wake_calls.append(arguments)
                return {"success": True}
            return {"success": True}

        plugin.ctx.call_capability = capability
        await plugin.after_receive(
            message=_make_event("并行跑", msg_id="m-par", user="Alice", session_id=grp, group_id="100246809", ts=clock[0])
        )

        # 6c) target_chat case/whitespace normalization ("QQ:group:100246809" -> aliases of grp)
        call_norm = {
            "tool_call": {
                "func_name": "maisya_agent",
                "args": {
                    "action": "start",
                    "profile": "scout",
                    "target_chat": "QQ:group:100246809",
                    "source_msg_id": "m-par",
                    "wait_seconds": 20,
                },
            }
        }
        await plugin.after_planner(session_id=grp, output_items=[call_norm])
        assert any(k[0] == grp for k in plugin.bridge._plans)
        await plugin.bridge.end_turn(grp)

        # 6a) 2 parallel calls where one fails validation before bridge.route
        call_bad = {
            "tool_call": {
                "func_name": "maisya_agent",
                "args": {
                    "action": "start",
                    "profile": "nonexistent_profile",
                    "target_chat": grp,
                    "source_msg_id": "m-par",
                    "wait_seconds": 0,
                },
            }
        }
        call_good = {
            "tool_call": {
                "func_name": "maisya_agent",
                "args": {
                    "action": "start",
                    "profile": "scout",
                    "target_chat": grp,
                    "source_msg_id": "m-par",
                    "wait_seconds": 0,
                },
            }
        }
        await plugin.after_planner(session_id=grp, output_items=[call_bad, call_good])
        bad_res = await plugin.agent_tool(
            action="start",
            profile="nonexistent_profile",
            target_chat=grp,
            message="fail",
            source_msg_id="m-par",
            wait_seconds=0,
            chat_id=grp,
            platform="qq",
        )
        assert not bad_res["success"]
        good_res = await plugin.agent_tool(
            action="start",
            profile="scout",
            target_chat=grp,
            message="succeed",
            source_msg_id="m-par",
            wait_seconds=0,
            chat_id=grp,
            platform="qq",
        )
        assert good_res["success"]
        await plugin.engine.drain()

        with plugin.threads.connect() as db:
            rem = db.execute(
                "SELECT remaining FROM return_groups WHERE chat_id=? ORDER BY rowid DESC LIMIT 1",
                (grp,),
            ).fetchone()["remaining"]
        assert rem == 0

        # Sibling wakes at +2s without starving until +10s deadline
        clock[0] += 2.0
        await plugin.pulse._returns()
        assert len(wake_calls) == 1

        # 6b) wait/stop appearing before maisya_agent MUST NOT leave dirty bridge._plans
        await plugin.after_planner(
            session_id=grp,
            output_items=[
                {"tool_call": {"func_name": "wait", "args": {}}},
                call_good,
            ],
        )
        assert not any(k[0] == grp for k in plugin.bridge._plans)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_item7_napcat_unwrapped_values_and_partial_file_retry_without_text_dup(
    tmp_path,
):
    # 7a) Unit check on _napcat_result_ok
    assert Share._napcat_result_ok(None) is True
    assert Share._napcat_result_ok("file_id_123") is True
    assert Share._napcat_result_ok(0) is True
    assert Share._napcat_result_ok(42) is True
    assert Share._napcat_result_ok(True) is True
    assert Share._napcat_result_ok(False) is False
    assert Share._napcat_result_ok(-1) is False
    assert Share._napcat_result_ok({"retcode": 1403}) is False
    assert Share._napcat_result_ok({"status": "failed"}) is False
    assert Share._napcat_result_ok({"status": "error"}) is False
    assert Share._napcat_result_ok({"success": False}) is False
    assert Share._napcat_result_ok({"ok": False}) is False
    assert Share._napcat_result_ok({"error": "bad"}) is False

    # 7b) Main message succeeds, file1 succeeds, file2 fails; retry sends ONLY file2!
    plugin = await loaded(tmp_path)
    try:
        grp = "qq:group:987654321"
        plugin.set_plugin_config(
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": [grp],
            }
        )
        await plugin.on_load()

        send_log: list[dict] = []
        fail_file2 = [True]

        async def capability(name, **arguments):
            if name == "llm.generate_with_tools":
                return {"success": True, "response": "分析见两个文件", "tool_calls": []}
            if name == "maisaka.reply.generate":
                return {"success": True, "text": "文件整理好啦。"}
            if name == "send.hybrid":
                send_log.append(arguments)
                segs = arguments.get("segments") or []
                for seg in segs:
                    if seg.get("type") == "file":
                        fname = (seg.get("data") or {}).get("name")
                        if fname == "b.txt" and fail_file2[0]:
                            return {"success": False, "sent": False, "error": "hybrid b failed"}
                return {"success": True, "sent": True, "message_id": f"m-{len(send_log)}"}
            if name == "api.call":
                fname = (arguments.get("args") or {}).get("params", {}).get("name")
                if fname == "b.txt" and fail_file2[0]:
                    return {"success": False, "retcode": 500, "msg": "napcat temp err"}
                return None  # SDK unwrapped None success!
            return {"success": True}

        plugin.ctx.call_capability = capability
        await plugin.after_receive(
            message=_make_event("整理文件", msg_id="m-files", user="Alice", session_id=grp, group_id="987654321")
        )
        started = await plugin.agent_tool(
            action="start",
            message="生成报告",
            source_msg_id="m-files",
            chat_id=grp,
            platform="qq",
        )
        tid = started["thread_id"]
        rev = plugin.threads.enqueue(tid, "补充附件", grp)
        row = plugin.threads.get(tid, grp)
        work = await plugin.runtime._work(row)
        work.write_bytes("out/a.txt", b"file A content")
        work.write_bytes("out/b.txt", b"file B content")
        work.commit()
        atts = plugin.runtime.files.attach(row, work, ["out/a.txt", "out/b.txt"])
        plugin.threads.finish(
            tid,
            grp,
            {"text": "主报告正文", "attachments": atts},
            [],
            rev,
        )
        handles = [a["handle"] for a in atts]

        first_try = await plugin.agent_tool(
            action="share",
            thread_id=tid,
            chat_id=grp,
            platform="qq",
            attachment_handles=handles,
        )
        assert first_try["status"] == "failed"
        assert first_try["sent"] is False
        main_sends_1 = [s for s in send_log if any(p.get("type") == "text" for p in s["segments"])]
        a_sends_1 = [
            s
            for s in send_log
            if any(isinstance(p.get("data"), dict) and p["data"].get("name") == "a.txt" for p in s["segments"])
        ]
        assert len(main_sends_1) == 1
        assert len(a_sends_1) == 1

        # Recover b.txt and retry share
        fail_file2[0] = False
        retry_try = await plugin.agent_tool(
            action="share",
            thread_id=tid,
            chat_id=grp,
            platform="qq",
            attachment_handles=handles,
        )
        assert retry_try["status"] == "sent" and retry_try["sent"] is True
        main_sends_2 = [s for s in send_log if any(p.get("type") == "text" for p in s["segments"])]
        a_sends_2 = [
            s
            for s in send_log
            if any(isinstance(p.get("data"), dict) and p["data"].get("name") == "a.txt" for p in s["segments"])
        ]
        b_sends_2 = [
            s
            for s in send_log
            if any(isinstance(p.get("data"), dict) and p["data"].get("name") == "b.txt" for p in s["segments"])
        ]
        assert len(main_sends_2) == 1  # Main text NOT re-sent!
        assert len(a_sends_2) == 1     # Already succeeded file a.txt NOT re-sent!
        assert len(b_sends_2) == 2     # Failed file b.txt retried once and succeeded!
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_item8_settings_tz_offset_bounded_initialize_data_and_async_offload(tmp_path):
    # 8a) Settings.tz_offset_minutes defaults and bounds
    cfg = Settings()
    assert cfg.tz_offset_minutes == 480
    assert Settings(tz_offset_minutes=-720).tz_offset_minutes == -720
    assert Settings(tz_offset_minutes=840).tz_offset_minutes == 840
    for bad in (-721, 841, True):
        with pytest.raises((ValidationError, ValueError)):
            Settings(tz_offset_minutes=bad)

    # 8b) initialize_data allows symlinked ancestor above data/ (e.g. macOS /var), rejects symlinked data/maisya or subdir
    real_host = tmp_path / "real_host"
    (real_host / "data/plugins").mkdir(parents=True)
    linked_host = tmp_path / "linked_host"
    safe_symlink(linked_host, real_host)
    inited = initialize_data(linked_host / "data/plugins/chartyr.maisya")
    assert inited.name == "maisya"
    assert (inited / "vault").is_dir()

    bad_root = tmp_path / "bad_host/data"
    bad_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    safe_symlink(bad_root / "maisya", outside)
    with pytest.raises(VaultError):
        initialize_data(bad_root / "plugins/chartyr.maisya")

    bad_sub = tmp_path / "bad_sub_host/data"
    (bad_sub / "maisya").mkdir(parents=True)
    safe_symlink(bad_sub / "maisya/vault", outside)
    with pytest.raises(VaultError):
        initialize_data(bad_sub / "plugins/chartyr.maisya")

    # 8c) Async entry points offload sync SQLite / disk reads via asyncio.to_thread
    plugin = await loaded(tmp_path / "offload", ["qq:Alice"])
    try:
        loop_tid = threading.get_ident()
        observed_threads: dict[str, int] = {}

        orig_enabled = plugin.enabled_for
        orig_aliases = plugin.chat_aliases_for
        orig_auth = plugin.authorize_admin_source
        orig_catalog = plugin.get_model_catalog
        orig_resolve = plugin.runtime.files.resolve

        def wrap_enabled(*args, **kwargs):
            observed_threads["enabled_for"] = threading.get_ident()
            return orig_enabled(*args, **kwargs)

        def wrap_aliases(*args, **kwargs):
            observed_threads["chat_aliases_for"] = threading.get_ident()
            return orig_aliases(*args, **kwargs)

        def wrap_auth(*args, **kwargs):
            observed_threads["authorize_admin_source"] = threading.get_ident()
            return orig_auth(*args, **kwargs)

        def wrap_catalog():
            observed_threads["get_model_catalog"] = threading.get_ident()
            return orig_catalog()

        def wrap_resolve(*args, **kwargs):
            observed_threads["files_resolve"] = threading.get_ident()
            return orig_resolve(*args, **kwargs)

        plugin.enabled_for = wrap_enabled
        plugin.chat_aliases_for = wrap_aliases
        plugin.authorize_admin_source = wrap_auth
        plugin.get_model_catalog = wrap_catalog
        plugin.runtime.files.resolve = wrap_resolve

        await plugin.after_receive(message=event("hi", user="Alice", group=True))
        assert observed_threads.get("enabled_for") not in (None, loop_tid)

        await plugin._vault_input(
            event("/vault set k v", user="Alice"),
            SimpleNamespace(message_id="m", session_id="chat", platform="qq"),
            "/vault set k v",
        )
        assert observed_threads.get("authorize_admin_source") not in (None, loop_tid)

        observed_threads.pop("enabled_for", None)
        await plugin.before_planner(
            session_id="chat",
            items=[],
            tool_definitions=[{"name": "reply"}, {"name": "maisya_agent"}],
        )
        assert observed_threads.get("enabled_for") not in (None, loop_tid)

        observed_threads.pop("enabled_for", None)
        await plugin.after_planner(
            session_id="chat",
            output_items=[
                {
                    "tool_call": {
                        "func_name": "maisya_agent",
                        "args": {"action": "start", "target_chat": "chat", "wait_seconds": 0},
                    }
                }
            ],
        )
        assert observed_threads.get("enabled_for") not in (None, loop_tid)
        assert observed_threads.get("chat_aliases_for") not in (None, loop_tid)

        observed_threads.pop("enabled_for", None)
        await plugin.before_replyer(
            session_id="chat", request_type="maisaka.replyer", attempt=1
        )
        assert observed_threads.get("enabled_for") not in (None, loop_tid)

        observed_threads.pop("enabled_for", None)
        await plugin.before_send(session_id="chat", platform="qq", message="hello")
        assert observed_threads.get("enabled_for") not in (None, loop_tid)

        await plugin.console_query()
        assert observed_threads.get("get_model_catalog") not in (None, loop_tid)

        # Share._segments stat + DB read must run off the event loop thread
        row = plugin.threads.create(
            Profile(), "share file", chat_id="chat", platform="qq", user_id=""
        )
        work = await plugin.runtime._work(row)
        work.write_bytes("out/note.txt", b"hello")
        work.commit()
        atts = plugin.runtime.files.attach(row, work, ["out/note.txt"])
        real_connect = plugin.sharing.store.connect
        conn_tids: list[int] = []

        def wrap_connect():
            conn_tids.append(threading.get_ident())
            return real_connect()

        plugin.sharing.store.connect = wrap_connect
        media, _refs = await plugin.sharing._segments("chat", [atts[0]["handle"]], [])
        assert len(media) == 1
        assert conn_tids and all(tid != loop_tid for tid in conn_tids)
    finally:
        await plugin.on_unload()
