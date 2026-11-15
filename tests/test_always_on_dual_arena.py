import time
from pathlib import Path

import pytest
from test_foundation import event, loaded

from maisya.abilities import Profile
from maisya.bridge import Bridge
from maisya.intents import Intents
from maisya.threads import ThreadStore
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


def _configure_chats(plugin, enabled_chats, **extra):
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": list(enabled_chats),
            **extra,
        }
    )


async def _answering_capability(name, **arguments):
    if name == "llm.generate_with_tools":
        return {
            "success": True,
            "response": "已完成探索：https://example.com/report",
            "tool_calls": [],
        }
    if name == "maisaka.reply.generate":
        return {"success": True, "text": "查好啦，链接放这里。"}
    if name.startswith("send.") or name == "napcat.send_msg":
        return {"success": True, "sent": True}
    return {"success": True, "text": "ok"}


def test_acquaintance_allowed_preserves_case_sensitive_user_id_and_private_stays_disabled(
    tmp_path,
):
    db_path = tmp_path / "maisya.db"
    store = MessageStore(db_path)
    threads = ThreadStore(db_path)
    intents = Intents(threads)
    store.record(
        Message.from_event(
            _make_event(
                "hi",
                msg_id="m_case_ok",
                user="CaseUser123",
                session_id="priv_ok",
                platform="QQ",
            )
        )
    )
    with threads.connect() as db, db:
        db.execute(
            "UPDATE session_routes SET human_count=0 WHERE session_id='priv_ok'"
        )
        db.execute("DELETE FROM trusted_sources WHERE session_id='priv_ok'")

    assert intents.acquaintance_allowed(
        "priv_ok", acquaintances=("qq:CaseUser123",)
    )
    assert intents.acquaintance_allowed(
        "priv_ok", acquaintances=("QQ:CaseUser123",)
    )
    assert not intents.acquaintance_allowed(
        "priv_ok", acquaintances=("qq:caseuser123",)
    )
    assert intents.acquaintance_allowed(
        "priv_payload",
        acquaintances=("qq:CaseOpenID_XYZ",),
        payload={"platform": "QQ", "user_id": "CaseOpenID_XYZ"},
    )
    assert not intents.acquaintance_allowed(
        "priv_payload",
        acquaintances=("qq:CaseOpenID_XYZ",),
        payload={"platform": "qq", "user_id": "caseopenid_xyz"},
    )


@pytest.mark.asyncio
async def test_private_chat_never_auto_enabled_by_acquaintance_users(tmp_path):
    plugin = await loaded(tmp_path)
    _configure_chats(plugin, ["chat"], acquaintance_users=["qq:CaseUser123"])
    assert not plugin.enabled_for(
        "qq:private:CaseUser123", "qq", user_id="CaseUser123"
    )
    assert not plugin.enabled_for("priv_session", "qq", user_id="CaseUser123")
    await plugin.on_unload()


def test_proactive_quota_splits_group_and_private_arenas_and_ignores_failed(tmp_path):
    db_path = tmp_path / "maisya.db"
    MessageStore(db_path)
    threads = ThreadStore(db_path)
    intents = Intents(threads, daily_proactive_limit=1)
    now = 1795250000.0
    intents.clock = lambda: now

    # Group arena takes 1 slot
    g1 = intents.schedule(
        kind="proactive",
        source="afterglow",
        target="chat:qq:group:100246809",
        due_at=now + 60,
        reason="群里刚才的话题",
    )
    assert not intents.proactive_quota_available("chat:qq:group:100246809", now=now)
    with pytest.raises(ValueError, match="每日上限"):
        intents.schedule(
            kind="proactive",
            source="serendipity",
            target="chat:qq:group:999888777",
            due_at=now + 120,
            reason="同在群 arena 应受限",
        )

    # Private arena still has its own independent bucket
    assert intents.proactive_quota_available("chat:qq:private:Alice", now=now)
    assert intents.proactive_quota_available("", now=now)
    p1 = intents.schedule(
        kind="proactive",
        source="longing",
        target="chat:qq:private:Alice",
        due_at=now + 90,
        reason="私聊独立配额",
    )
    assert not intents.proactive_quota_available("chat:qq:private:Alice", now=now)
    assert not intents.proactive_quota_available("", now=now)
    with pytest.raises(ValueError, match="每日上限"):
        intents.schedule(
            kind="proactive",
            source="afterglow",
            target="chat:qq:private:Bob",
            due_at=now + 150,
            reason="私聊 arena 满额",
        )

    # Failed intent must NOT consume arena quota
    intents.clock = lambda: now + 100
    claimed = intents.due(enabled=lambda cid, plat: True)
    assert any(item["intent_id"] == g1["intent_id"] for item in claimed)
    intents.finish(g1["intent_id"], status="failed", wake_status="unreplied_lock")
    assert intents.proactive_quota_available("chat:qq:group:100246809", now=now)
    assert intents.proactive_quota_available("", now=now)
    g2 = intents.schedule(
        kind="proactive",
        source="afterglow",
        target="chat:qq:group:100246809",
        due_at=now + 180,
        reason="失败后不占配额",
    )
    assert g2["intent_id"].startswith("in_")

    # Opaque session_id resolves arena via session_routes
    intents.clock = lambda: now + 120
    claimed_priv = intents.due(enabled=lambda cid, plat: True)
    if any(item["intent_id"] == p1["intent_id"] for item in claimed_priv):
        intents.finish(p1["intent_id"], status="failed", wake_status="sleep_deferred")
    else:
        with threads.connect() as db, db:
            db.execute("UPDATE intents SET status='failed' WHERE intent_id=?", (p1["intent_id"],))
    MessageStore(db_path).record(
        Message.from_event(
            _make_event("hi", msg_id="m_opaque", user="Alice", session_id="opaque_priv_sid")
        )
    )
    assert intents.proactive_quota_available("chat:opaque_priv_sid", now=now)
    intents.schedule(
        kind="proactive",
        source="later",
        target="chat:opaque_priv_sid",
        due_at=now + 200,
        reason="路由私聊槽位",
    )
    assert not intents.proactive_quota_available("chat:qq:private:Alice", now=now)


def test_note_received_engaged_flag_controls_unlock(tmp_path):
    intents = Intents(ThreadStore(tmp_path / "maisya.db"))
    intents.mark_proactive("chat:grp1")
    intents.note_received("grp1", engaged=False)
    assert not intents.social_gate("chat:grp1")[0]
    intents.note_received("grp1", engaged=True)
    assert intents.social_gate("chat:grp1")[0]

    intents.mark_proactive("chat:grp2")
    intents.note_received("grp2")
    assert intents.social_gate("chat:grp2")[0]


@pytest.mark.asyncio
async def test_bridge_material_includes_chat_type_and_messages_since_trigger(tmp_path):
    db_path = tmp_path / "maisya.db"
    messages = MessageStore(db_path)
    threads = ThreadStore(db_path)
    bridge = Bridge(threads)

    t0 = time.time() - 30
    grp = "qq:group:100246809"
    messages.record(
        Message.from_event(
            _make_event("trigger", msg_id="m_trig", user="Alice", session_id=grp, group_id="100246809", ts=t0)
        )
    )
    time.sleep(0.005)
    messages.record(
        Message.from_event(
            _make_event("follow1", msg_id="m_after_1", user="Bob", session_id=grp, group_id="100246809", ts=t0 + 2)
        )
    )
    messages.record(
        Message.from_event(
            _make_event("follow2", msg_id="m_after_2", user="Carol", session_id=grp, group_id="100246809", ts=t0 + 4)
        )
    )
    messages.record(
        Message.from_event(
            _make_event("relayed", msg_id="m_relayed", user="Bot", session_id=grp, group_id="100246809", relay="other.plugin", ts=t0 + 5)
        )
    )

    row = threads.create(
        Profile(id="scout"),
        "查一下",
        chat_id=grp,
        platform="qq",
        user_id="Alice",
        owner="chat",
    )
    route = await bridge.route(grp, "m_trig", 0)
    threads.enqueue(row["thread_id"], "查一下", chat_id=grp, return_route=route)
    ok = threads.finish(
        row["thread_id"],
        grp,
        {"text": "查好了"},
        [],
        1,
    )
    assert ok
    items = await bridge.offer(grp)
    assert len(items) == 1
    assert items[0]["chat_type"] == "group"
    assert items[0]["messages_since_trigger"] == 2

    # Standalone Bridge without session_routes / trusted_sources works safely
    standalone_threads = ThreadStore(tmp_path / "standalone.db")
    standalone_bridge = Bridge(standalone_threads)
    srow = standalone_threads.create(
        Profile(id="scout"),
        "查一下私聊",
        chat_id="qq:private:Alice",
        platform="qq",
        user_id="Alice",
        owner="chat",
    )
    sroute = await standalone_bridge.route("qq:private:Alice", "m0", 0)
    standalone_threads.enqueue(
        srow["thread_id"], "查一下私聊", chat_id="qq:private:Alice", return_route=sroute
    )
    assert standalone_threads.finish(
        srow["thread_id"], "qq:private:Alice", {"text": "ok"}, [], 1
    )
    standalone_items = await standalone_bridge.offer("qq:private:Alice")
    assert len(standalone_items) == 1
    assert standalone_items[0]["chat_type"] == "private"
    assert "messages_since_trigger" not in standalone_items[0]


@pytest.mark.asyncio
async def test_group_vs_private_unreplied_lock_and_longing_fallback_zero(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    grp = "qq:group:100246809"
    priv = "qq:private:Alice"
    _configure_chats(plugin, [grp, priv])
    await plugin.on_load()

    # Check _chat_arena resolution & fallback=0 default in plugin.py
    assert plugin._chat_arena(grp, "qq") == "group"
    assert plugin._chat_arena(priv, "qq") == "private"
    assert plugin._chat_arena("unknown_chat_id", "qq") == ""
    assert '"longing_quiet_hours", 0' in Path("plugin.py").read_text(encoding="utf-8")

    # 1. Private chat unlocks on after_receive of real human message
    plugin.intents.mark_proactive(f"chat:{priv}")
    assert not plugin.intents.social_gate(f"chat:{priv}")[0]
    await plugin.after_receive(
        message=_make_event("你好", msg_id="p_msg_1", user="Alice", session_id=priv)
    )
    assert plugin.intents.social_gate(f"chat:{priv}")[0]

    # 2. Group chat stays locked on bystander message in after_receive
    plugin.intents.mark_proactive(f"chat:{grp}")
    assert not plugin.intents.social_gate(f"chat:{grp}")[0]
    await plugin.after_receive(
        message=_make_event(
            "路人发言", msg_id="g_msg_1", user="Bob", session_id=grp, group_id="100246809"
        )
    )
    assert not plugin.intents.social_gate(f"chat:{grp}")[0]

    # 3. Pure background proactive/return turn (no _turn_sources) with reply does NOT unlock group
    plugin._turn_sources.pop(grp, None)
    await plugin.after_planner(
        session_id=grp,
        output_items=[{"tool_call": {"func_name": "reply", "args": {"text": "主动说句"}}}],
    )
    assert not plugin.intents.social_gate(f"chat:{grp}")[0]

    # 4. Human turn calling wait/stop without reply or maisya_agent stays locked
    plugin._turn_sources[grp] = ["g_msg_1"]
    await plugin.after_planner(
        session_id=grp,
        output_items=[{"tool_call": {"func_name": "wait", "args": {}}}],
    )
    assert not plugin.intents.social_gate(f"chat:{grp}")[0]

    # 5. Human turn with reply unlocks group
    plugin._turn_sources[grp] = ["g_msg_1"]
    await plugin.after_planner(
        session_id=grp,
        output_items=[{"tool_call": {"func_name": "reply", "args": {"text": "回你一下"}}}],
    )
    assert plugin.intents.social_gate(f"chat:{grp}")[0]

    # 6. Human turn calling maisya_agent in after_planner unlocks group
    plugin.intents.mark_proactive(f"chat:{grp}")
    plugin._turn_sources[grp] = ["g_msg_1"]
    await plugin.after_planner(
        session_id=grp,
        output_items=[
            {
                "tool_call": {
                    "func_name": "maisya_agent",
                    "args": {"action": "start", "source_msg_id": "g_msg_1"},
                }
            }
        ],
    )
    assert plugin.intents.social_gate(f"chat:{grp}")[0]

    # 7. Successful action="share" with human identity unlocks group
    plugin.ctx.call_capability = _answering_capability
    started = await plugin.agent_tool(
        action="start",
        message="找一个链接给我",
        source_msg_id="g_msg_1",
        chat_id=grp,
        platform="qq",
    )
    assert started["success"]
    plugin.intents.mark_proactive(f"chat:{grp}")
    sent_calls = []

    async def share_cap(name, **arguments):
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "链接在这"}
        if name.startswith("send.") or name == "napcat.send_msg":
            sent_calls.append((name, arguments))
            return {"success": True, "sent": True}
        return await _answering_capability(name, **arguments)

    plugin.ctx.call_capability = share_cap
    shared = await plugin.agent_tool(
        action="share",
        thread_id=started["thread_id"],
        message="把这个链接发群里",
        source_msg_id="g_msg_1",
        chat_id=grp,
        platform="qq",
    )
    assert shared["success"] and sent_calls
    assert plugin.intents.social_gate(f"chat:{grp}")[0]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_group_thread_ownership_protection(tmp_path):
    plugin = await loaded(tmp_path, ["qq:AdminUser"])
    grp = "qq:group:100246809"
    _configure_chats(plugin, [grp])
    await plugin.on_load()
    plugin.ctx.call_capability = _answering_capability

    await plugin.after_receive(
        message=_make_event("Alice请求", msg_id="m_alice", user="Alice", session_id=grp, group_id="100246809")
    )
    await plugin.after_receive(
        message=_make_event("Bob路过", msg_id="m_bob", user="Bob", session_id=grp, group_id="100246809")
    )
    await plugin.after_receive(
        message=_make_event("Admin发言", msg_id="m_admin", user="AdminUser", session_id=grp, group_id="100246809")
    )

    personal = await plugin.agent_tool(
        action="start",
        message="帮Alice查个人委托",
        source_msg_id="m_alice",
        chat_id=grp,
        platform="qq",
    )
    assert personal["success"]

    coop = await plugin.agent_tool(
        action="start",
        message="一起做活动策划",
        workspace="team_plan",
        source_msg_id="m_alice",
        chat_id=grp,
        platform="qq",
    )
    assert coop["success"]

    auto_res = await plugin.agent_tool(
        action="start",
        message="自发探索",
        chat_id=grp,
        platform="qq",
    )
    assert auto_res["success"]

    # Bob cannot stop Alice's personal thread
    bob_stop = await plugin.agent_tool(
        action="stop",
        thread_id=personal["thread_id"],
        source_msg_id="m_bob",
        chat_id=grp,
        platform="qq",
    )
    assert not bob_stop["success"]
    assert "发起人或管理员" in bob_stop["error"]

    # identity=None cannot stop Alice's personal thread either
    anon_stop = await plugin.agent_tool(
        action="stop",
        thread_id=personal["thread_id"],
        chat_id=grp,
        platform="qq",
    )
    assert not anon_stop["success"]
    assert "发起人或管理员" in anon_stop["error"]

    # Bob cannot continue or steer personal thread
    bob_send = await plugin.agent_tool(
        action="send",
        thread_id=personal["thread_id"],
        message="Bob插话继续",
        source_msg_id="m_bob",
        chat_id=grp,
        platform="qq",
    )
    assert not bob_send["success"]
    assert "发起人或管理员" in bob_send["error"]

    bob_steer = await plugin.agent_tool(
        action="send",
        thread_id=personal["thread_id"],
        message="Bob强行转向",
        steer=True,
        source_msg_id="m_bob",
        chat_id=grp,
        platform="qq",
    )
    assert not bob_steer["success"]
    assert "转向" in bob_steer["error"]

    # Bob CANNOT steer co-op thread, but CAN continue co-op thread without steer
    bob_coop_steer = await plugin.agent_tool(
        action="send",
        thread_id=coop["thread_id"],
        message="Bob强行改向",
        steer=True,
        source_msg_id="m_bob",
        chat_id=grp,
        platform="qq",
    )
    assert not bob_coop_steer["success"]
    assert "转向" in bob_coop_steer["error"]

    bob_coop_send = await plugin.agent_tool(
        action="send",
        thread_id=coop["thread_id"],
        message="Bob补充一条协作想法",
        source_msg_id="m_bob",
        chat_id=grp,
        platform="qq",
    )
    assert bob_coop_send["success"]

    # Initiator Alice CAN steer her personal thread
    alice_steer = await plugin.agent_tool(
        action="send",
        thread_id=personal["thread_id"],
        message="Alice转向",
        steer=True,
        source_msg_id="m_alice",
        chat_id=grp,
        platform="qq",
    )
    assert alice_steer["success"]

    # AdminUser CAN stop Alice's personal thread
    admin_stop = await plugin.agent_tool(
        action="stop",
        thread_id=personal["thread_id"],
        source_msg_id="m_admin",
        chat_id=grp,
        platform="qq",
    )
    assert admin_stop["success"] and admin_stop["state"] == "closed"

    # Unattributed autonomous thread can still be stopped without source_msg_id
    auto_stop = await plugin.agent_tool(
        action="stop",
        thread_id=auto_res["thread_id"],
        chat_id=grp,
        platform="qq",
    )
    assert auto_stop["success"] and auto_stop["state"] == "closed"
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_one_way_privacy_wall_blocks_private_to_group_leaks(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    grp = "qq:group:100246809"
    priv = "qq:private:Alice"
    _configure_chats(plugin, [grp, priv])
    await plugin.on_load()
    plugin.ctx.call_capability = _answering_capability

    await plugin.after_receive(
        message=_make_event("私聊查件", msg_id="m_priv_alice", user="Alice", session_id=priv)
    )
    await plugin.after_receive(
        message=_make_event("群聊管理", msg_id="m_grp_alice", user="Alice", session_id=grp, group_id="100246809")
    )

    priv_thread = await plugin.agent_tool(
        action="start",
        message="私聊秘密研究 private_marker",
        source_msg_id="m_priv_alice",
        chat_id=priv,
        platform="qq",
    )
    assert priv_thread["success"]
    plugin.memory.write("memory", "私聊专属秘密记事 private_mem_marker", chat_id=priv)

    grp_thread = await plugin.agent_tool(
        action="start",
        message="群里公开讨论 group_marker",
        source_msg_id="m_grp_alice",
        chat_id=grp,
        platform="qq",
    )
    assert grp_thread["success"]

    # 1. Admin calling recall(target_chat="all") from group must NEVER see private threads or private memories
    recalled = await plugin.recall_tool(
        query="",
        kind="all",
        target_chat="all",
        source_msg_id="m_grp_alice",
        chat_id=grp,
        platform="qq",
        limit=10,
    )
    assert recalled["success"]
    thread_ids = {t["thread_id"] for t in recalled["threads"]}
    assert grp_thread["thread_id"] in thread_ids
    assert priv_thread["thread_id"] not in thread_ids
    mem_texts = " ".join(m.get("content", "") for m in recalled["memories"])
    assert "private_mem_marker" not in mem_texts

    # 2. Admin in group cannot inject private thread via from_threads into group start or send
    leak_start = await plugin.agent_tool(
        action="start",
        message="把私聊结论带到群里",
        from_threads=[priv_thread["thread_id"]],
        source_msg_id="m_grp_alice",
        chat_id=grp,
        platform="qq",
    )
    assert not leak_start["success"]
    assert "私聊线程结果默认不可直接注入群聊线程" in leak_start["error"]

    leak_send = await plugin.agent_tool(
        action="send",
        thread_id=grp_thread["thread_id"],
        message="把私聊结果继续注入群线程",
        from_threads=[priv_thread["thread_id"]],
        source_msg_id="m_grp_alice",
        chat_id=grp,
        platform="qq",
    )
    assert not leak_send["success"]
    assert "私聊线程结果默认不可直接注入群聊线程" in leak_send["error"]

    # 3. Private->Group from_threads isolation applies even when admin in private previews group with deliver_to="current"
    preview_blocked = await plugin.agent_tool(
        action="start",
        message="在私聊里用私人上下文试跑群任务",
        target_chat=grp,
        deliver_to="current",
        from_threads=[priv_thread["thread_id"]],
        source_msg_id="m_priv_alice",
        chat_id=priv,
        platform="qq",
    )
    assert not preview_blocked["success"]
    assert "私聊线程结果默认不可直接注入群聊线程" in preview_blocked["error"]
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_one_way_privacy_wall_allows_group_to_private_self_continuity(tmp_path):
    plugin = await loaded(tmp_path, ["qq:AdminOnly"])
    grp = "qq:group:100246809"
    priv_alice = "qq:private:Alice"
    priv_unlisted = "qq:private:Carol"
    _configure_chats(plugin, [grp, priv_alice])
    (plugin.profiles.path / "secret_tool").mkdir(parents=True, exist_ok=True)
    (plugin.profiles.path / "secret_tool/PROFILE.md").write_text(
        "secret tool\n", encoding="utf-8"
    )
    (plugin.profiles.path / "secret_tool/profile.toml").write_text(
        'access = "admins"\nenvironment = "sandbox"\ntemplate = "sandbox"\nsecrets = ["API_KEY"]\n',
        encoding="utf-8",
    )
    (plugin.profiles.path / "listed_tool").mkdir(parents=True, exist_ok=True)
    (plugin.profiles.path / "listed_tool/PROFILE.md").write_text(
        "listed tool\n", encoding="utf-8"
    )
    (plugin.profiles.path / "listed_tool/profile.toml").write_text(
        'access = "listed"\nallow_chats = ["qq:group:100246809"]\nenvironment = "readonly"\n',
        encoding="utf-8",
    )
    await plugin.on_load()
    plugin.ctx.call_capability = _answering_capability

    await plugin.after_receive(
        message=_make_event("Alice群里查", msg_id="m_g_alice", user="Alice", session_id=grp, group_id="100246809")
    )
    await plugin.after_receive(
        message=_make_event("Bob群里查", msg_id="m_g_bob", user="Bob", session_id=grp, group_id="100246809")
    )
    await plugin.after_receive(
        message=_make_event("Alice私聊", msg_id="m_p_alice", user="Alice", session_id=priv_alice)
    )
    await plugin.after_receive(
        message=_make_event("relayed", msg_id="m_p_relay", user="Alice", session_id=priv_alice, relay="other.plugin")
    )
    await plugin.after_receive(
        message=_make_event("Carol未启用私聊", msg_id="m_p_carol", user="Carol", session_id=priv_unlisted)
    )

    t_alice_pub = await plugin.agent_tool(
        action="start",
        profile="scout",
        message="Alice在群里的公开调研",
        source_msg_id="m_g_alice",
        chat_id=grp,
        platform="qq",
    )
    t_bob_pub = await plugin.agent_tool(
        action="start",
        profile="scout",
        message="Bob在群里的调研",
        source_msg_id="m_g_bob",
        chat_id=grp,
        platform="qq",
    )
    t_alice_listed = await plugin.agent_tool(
        action="start",
        profile="listed_tool",
        message="Alice群专用工具",
        source_msg_id="m_g_alice",
        chat_id=grp,
        platform="qq",
    )
    sec_row = plugin.threads.create(
        plugin.profiles.get("secret_tool"),
        "Alice带凭据沙箱",
        chat_id=grp,
        platform="qq",
        user_id="Alice",
        owner="chat",
    )
    t_alice_secret_id = sec_row["thread_id"]

    assert t_alice_pub["success"] and t_bob_pub["success"] and t_alice_listed["success"]
    plugin.memory.write("memory", "群里专属约定 group_mem_secret_marker", chat_id=grp)

    # Alice in enabled private chat recalls with kind="all", scope="available"
    res = await plugin.recall_tool(
        query="",
        scope="available",
        kind="all",
        source_msg_id="m_p_alice",
        chat_id=priv_alice,
        platform="qq",
        limit=5,
    )
    assert res["success"]
    found_ids = {t["thread_id"] for t in res["threads"]}
    assert t_alice_pub["thread_id"] in found_ids
    assert t_bob_pub["thread_id"] not in found_ids
    assert t_alice_listed["thread_id"] not in found_ids
    assert t_alice_secret_id not in found_ids
    mem_blob = " ".join(m.get("content", "") for m in res["memories"])
    assert "group_mem_secret_marker" not in mem_blob

    # Relayed source is rejected; unlisted private chat is rejected
    relayed_res = await plugin.recall_tool(
        query="",
        scope="available",
        kind="threads",
        source_msg_id="m_p_relay",
        chat_id=priv_alice,
        platform="qq",
    )
    assert not relayed_res["success"]

    unlisted_res = await plugin.recall_tool(
        query="",
        scope="available",
        kind="threads",
        source_msg_id="m_p_carol",
        chat_id=priv_unlisted,
        platform="qq",
    )
    assert not unlisted_res["success"]

    # Verify final readable threads truncate to `limit`
    for idx in range(3):
        extra = await plugin.agent_tool(
            action="start",
            profile="scout",
            message=f"Alice额外探索 {idx}",
            source_msg_id="m_g_alice",
            chat_id=grp,
            platform="qq",
        )
        assert extra["success"]
    capped = await plugin.recall_tool(
        query="",
        scope="available",
        kind="threads",
        source_msg_id="m_p_alice",
        chat_id=priv_alice,
        platform="qq",
        limit=2,
    )
    assert capped["success"] and len(capped["threads"]) == 2
    await plugin.on_unload()
