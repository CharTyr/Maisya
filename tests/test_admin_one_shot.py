import html
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from maisya.intents import next_daily_due
from maisya.trust import Message
from plugin import create_plugin


async def _boot_plugin(tmp_path, *, permissions=("qq:Alice",), enabled_chats=()):
    async def get(key, default=None):
        return list(permissions) if key == "plugin.permission" else default

    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya.one_shot"),
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": list(enabled_chats),
        }
    )
    await plugin.on_load()
    return plugin


def _group_msg(
    message_id: str,
    text: str,
    *,
    session_id: str = "c0300b39c80870992aca16d9fcb724c5",
    group_id: str = "100246809",
    user_id: str = "Alice",
    platform: str = "qq",
    relay: str = "",
):
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": "1794500000.0",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": {"group_id": group_id},
            "additional_config": {"relayed_by_plugin": relay},
        },
    }


def _private_msg(
    message_id: str,
    text: str,
    *,
    session_id: str = "priv_admin_md5",
    user_id: str = "Alice",
    platform: str = "qq",
    relay: str = "",
):
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": "1794500000.0",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": None,
            "additional_config": {"relayed_by_plugin": relay},
        },
    }


def _streams_rpc(calls=None):
    group_sid = "c0300b39c80870992aca16d9fcb724c5"

    async def capability(name, **arguments):
        if calls is not None:
            calls.append((name, arguments))
        if name == "chat.get_all_streams":
            return {
                "success": True,
                "streams": [
                    {
                        "session_id": group_sid,
                        "platform": "qq",
                        "is_group_session": True,
                        "chat_type": "group",
                        "group_id": "100246809",
                        "group_name": "独立开发社",
                    }
                ],
            }
        if name == "llm.get_available_models":
            return {"success": True, "models": ["claude-sonnet-4-6", "deepseek-v3"]}
        if name == "statistics.local.models":
            return {
                "success": True,
                "models": [
                    {"model_name": "claude-sonnet-4-6", "api_provider": "anthropic"},
                    {"model_name": "deepseek-v3", "api_provider": "deepseek"},
                ],
            }
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "今日人工智能与二次元游戏早报完成。", "tool_calls": []}
        if name == "maisaka.proactive.trigger":
            return {"success": True}
        if name == "chat.open_session":
            return {"success": True, "session_id": arguments.get("group_id") or group_sid}
        return {"success": True}

    return capability


@pytest.mark.asyncio
async def test_chat_alias_matching_and_canonical_resolution(tmp_path):
    plugin = await _boot_plugin(tmp_path, permissions=["qq:Alice"], enabled_chats=[])
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    try:
        plugin.ctx.call_capability = _streams_rpc()

        for raw in ("100246809", "qq:100246809", "qq:group:100246809", "独立开发社", group_sid):
            cid, plat = await plugin.resolve_chat_id(
                raw, current_chat_id="priv_admin_md5", current_platform="qq"
            )
            assert cid == group_sid, (raw, cid)
            assert plat == "qq"

        cid, plat = await plugin.resolve_chat_id(
            "current", current_chat_id="priv_admin_md5", current_platform="qq"
        )
        assert (cid, plat) == ("priv_admin_md5", "qq")

        # Stream cache seeded session_routes so Pulse._reopen_session works before any group message arrives
        route = plugin.messages.route_for(group_sid)
        assert route is not None
        assert route["platform"] == "qq"
        assert route["chat_type"] == "group"
        assert route["group_id"] == "100246809"

        # Even if manual raw ID '100246809' is configured directly, matching works everywhere
        await plugin.on_config_update(
            "self",
            {
                **plugin.settings.model_dump(),
                "enabled_chats": ["100246809"],
                "chat_proactive_levels": {"100246809": "lively"},
            },
            0,
        )
        assert plugin.enabled_for(group_sid, "qq") is True
        assert plugin.pulse.level_for(group_sid) == "lively"

        # Profile.allow_chats with raw group_id matches incoming MD5 session_id
        plugin.memory.write(
            "profile",
            'title = "白名单侦察"\naccess = "listed"\nallow_chats = ["100246809"]\ntools = ["web_search"]\n',
            target="default",
            document="profile.toml",
            expected_version=plugin.memory.read("profile", target="default", document="profile.toml")["version"],
            allow_custom=True,
        )
        assert plugin._profile_allowed(plugin.profiles.get("default"), None, group_sid, "qq") is True
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_zero_leakage_admin_bootstrap_and_private_vault(tmp_path):
    plugin = await _boot_plugin(tmp_path, permissions=["qq:Alice"], enabled_chats=[])
    try:
        tools = [
            {"type": "function", "function": {"name": name}}
            for name in ["reply", "subagent", "maisya_agent", "maisya_recall", "maisya_later"]
        ]

        # 1. Non-admin in unlisted group: zero DB writes, all 4 Maisya tools stripped, zero prompt injection
        bob_msg = _group_msg("m-bob-1", "hi", session_id="unlisted_group", group_id="88888", user_id="Bob")
        await plugin.after_receive(message=bob_msg)
        with plugin.messages.path and plugin.threads.connect() as db:
            rows = db.execute("SELECT * FROM trusted_sources WHERE session_id='unlisted_group'").fetchall()
            assert rows == []
        non_admin_hook = await plugin.before_planner(session_id="unlisted_group", items=[], tool_definitions=tools)
        kept = [(t.get("function") or t).get("name") for t in non_admin_hook["modified_kwargs"]["tool_definitions"]]
        assert set(kept) == {"reply", "subagent"}
        assert "items" not in non_admin_hook["modified_kwargs"]

        # 2. Admin in un-enabled private chat: /vault set succeeds without enabled_chats containing private session
        secret_val = "sk-test-private-admin-vault-9999"
        vault_ev = _private_msg("m-alice-vault", f"/vault set exa_key {secret_val}")
        vault_res = await plugin.after_receive(message=vault_ev)
        assert secret_val not in str(vault_res)
        assert plugin.vault.handles() == ["exa_key"]

        # 3. Admin normal message in un-enabled private chat bootstraps tools & guide
        alice_ev = _private_msg("m-alice-boot", "帮我给独立开发社开启 Maisya 并配好每日资讯")
        await plugin.after_receive(message=alice_ev)
        boot_hook = await plugin.before_planner(session_id="priv_admin_md5", items=[], tool_definitions=tools)
        boot_names = [(t.get("function") or t).get("name") for t in boot_hook["modified_kwargs"]["tool_definitions"]]
        assert "maisya_later" in boot_names
        assert "maisya_agent" in boot_names
        boot_text = html.unescape(boot_hook["modified_kwargs"]["items"][-1]["parts"][0]["text"])
        assert "<maisya_admin_bootstrap>" in boot_text
        assert "tool_search" in boot_text
        assert "maisya_shape" in boot_text
        assert "priv_admin_md5" in boot_text
        assert "<maisya_profiles>" in boot_text and "default" in boot_text
        assert "<maisya_planner_manual>" in boot_text
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_single_turn_one_paragraph_full_configuration_and_run(tmp_path):
    _write_host_model_config(
        tmp_path,
        "\n".join(
            [
                "[[models]]",
                'name = "claude-sonnet-4-6"',
                'model_identifier = "claude-sonnet-4-6"',
                'api_provider = "anthropic"',
            ]
        ),
    )
    plugin = await _boot_plugin(tmp_path, permissions=["qq:Alice"], enabled_chats=[])
    calls = []
    plugin.ctx.call_capability = _streams_rpc(calls)
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    try:
        plugin.vault.set("demo_secret", "top-secret-never-leak-777")
        admin_ev = _private_msg(
            "m-admin-one-shot",
            "帮我把 100246809（独立开发社）开启 Maisya，打开 digest 用 claude-sonnet-4-6，创建一个 ai-radar 技能绑上，记下关注开源AI与二游，设每天 08:30 和 19:30 推送并试跑一期",
        )
        await plugin.after_receive(message=admin_ev)
        await plugin.before_planner(
            session_id="priv_admin_md5",
            items=[],
            tool_definitions=[
                {"type": "function", "function": {"name": n}}
                for n in ("reply", "maisya_agent", "maisya_recall", "maisya_later", "maisya_shape")
            ],
        )

        # 1. Overview
        ov = await plugin.shape_tool(
            action="read",
            kind="overview",
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert ov["success"] is True, ov
        assert "top-secret-never-leak-777" not in json.dumps(ov, ensure_ascii=False)
        assert "demo_secret" in plugin.vault.handles()
        assert any(c["chat_id"] == group_sid and c["name"] == "独立开发社" for c in ov["known_chats"])
        assert any(p["profile_id"] == "default" and p["enabled"] is True for p in ov["profiles"])
        assert "claude-sonnet-4-6" in {m["name"] for m in plugin.get_model_catalog().get("models", [])}

        # 2. Settings patch + anti-self-lockout + rollback
        patched = await plugin.shape_tool(
            action="patch",
            kind="settings",
            patch={
                "add_chats": ["100246809"],
                "chat_proactive_levels": {"100246809": "lively"},
                "sleep_window": "01-08",
                "daily_proactive_limit": 3,
            },
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert patched["success"] is True, patched
        assert "qq:group:100246809" in plugin.settings.enabled_chats
        assert plugin.settings.chat_proactive_levels["qq:group:100246809"] == "lively"
        assert plugin.enabled_for(group_sid, "qq") is True
        assert plugin.settings.sleep_window == "01-08"
        assert plugin.settings.daily_proactive_limit == 3
        assert plugin.pulse.level_for(group_sid) == "lively"

        lockout = await plugin.shape_tool(
            action="patch",
            kind="settings",
            patch={"plugin": {"enabled": False}},
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert lockout["success"] is False
        assert "锁死" in lockout["error"]

        # 3. Profile create/patch (digest from default)
        prof_res = await plugin.shape_tool(
            action="profile",
            op="create",
            target="digest",
            base="default",
            patch={
                "enabled": True,
                "model_name": "claude-sonnet-4-6",
                "fallback_models": ["deepseek-v3"],
                "instructions": "重点关注开源 AI 与二次元游戏动态。",
            },
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert prof_res["success"] is True, prof_res
        digest = plugin.profiles.get("digest")
        assert digest.enabled is True
        assert digest.model_list == ("claude-sonnet-4-6", "deepseek-v3")
        assert "开源 AI 与二次元游戏动态" in digest.instructions

        # 4. Skill create via file + bind to digest
        sk_res = await plugin.shape_tool(
            action="file",
            op="write",
            document="skills/ai-radar/SKILL.md",
            content="---\nname: ai-radar\ntitle: AI 雷达\ndescription: 提炼开源仓库重点\n---\n# AI 雷达\n只保留有源码链接的项目。\n",
            expected_version=0,
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert sk_res["success"] is True, sk_res
        bind_res = await plugin.shape_tool(
            action="profile",
            op="patch",
            target="digest",
            patch={"skills": ["ai-radar"]},
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert bind_res["success"] is True, bind_res
        assert "ai-radar" in plugin.profiles.get("digest").skills

        # 5. Cross-chat group MEMORY.md write by group name
        mem_res = await plugin.shape_tool(
            action="file",
            op="write",
            target_chat="独立开发社",
            document="MEMORY.md",
            content="群友偏好：主要关注开源 AI 与二游资讯",
            expected_version=0,
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert mem_res["success"] is True, mem_res
        assert "二游资讯" in plugin.memory.read("memory", chat_id=group_sid)["content"]
        assert plugin.memory.read("memory", chat_id="priv_admin_md5")["content"] == ""

        # 6. Schedule two daily wall-clock slots, then replace with one slot
        sched_res = await plugin.later_tool(
            action="schedule",
            target_chat="100246809",
            profile="digest",
            workspace="daily_news",
            daily_at=["08:30", "19:30"],
            reason="每日早晚资讯简报",
            prompt="整理今日资讯简报",
            replace=True,
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert sched_res["success"] is True, sched_res
        assert len(sched_res["intents"]) == 2
        assert len(plugin.intents.list(f"chat:{group_sid}")) == 2

        sched_replace = await plugin.later_tool(
            action="schedule",
            target_chat="独立开发社",
            profile="digest",
            workspace="daily_news",
            daily_at=["09:00"],
            reason="改为每日早报",
            prompt="整理每日早报",
            replace=True,
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert sched_replace["success"] is True, sched_replace
        assert len(sched_replace["cancelled_ids"]) == 2
        pending = plugin.intents.list(f"chat:{group_sid}")
        assert len(pending) == 1
        assert pending[0]["payload"]["daily_at"] == "09:00"
        assert pending[0]["payload"]["scheduled_admin"] is True

        # 7. Immediate cross-chat test run wakes target group (not private chat)
        run_res = await plugin.agent_tool(
            action="start",
            target_chat="100246809",
            profile="digest",
            workspace="daily_news",
            message="先试跑一期今日资讯",
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert run_res["success"] is True, run_res
        assert run_res["state"] == "active"
        await plugin.engine.drain()
        await plugin.pulse.tick()
        wake_targets = [args["stream_id"] for cap, args in calls if cap == "maisaka.proactive.trigger"]
        assert group_sid in wake_targets
        assert "priv_admin_md5" not in wake_targets

        # Rollback settings history check (anti-lockout blocks rolling back to empty enabled_chats; non-empty rollback works)
        hist = await plugin.shape_tool(
            action="history",
            kind="settings",
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert hist["success"] is True and len(hist["history"]) >= 2
        blocked_rb = await plugin.shape_tool(
            action="rollback",
            kind="settings",
            version=hist["history"][0]["version"],
            expected_version=patched["version"],
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert blocked_rb["success"] is False and "锁死" in blocked_rb["error"]

        patched2 = await plugin.shape_tool(
            action="patch",
            kind="settings",
            patch={"sleep_window": "02-07", "daily_proactive_limit": 5},
            expected_version=patched["version"],
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert patched2["success"] is True, patched2
        rb = await plugin.shape_tool(
            action="rollback",
            kind="settings",
            version=patched["version"],
            expected_version=patched2["version"],
            source_msg_id="m-admin-one-shot",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert rb["success"] is True, rb
        assert plugin.settings.sleep_window == "01-08"
        assert plugin.settings.daily_proactive_limit == 3
        assert plugin.settings.enabled_chats == ["qq:group:100246809"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_wall_clock_drift_free_reschedule_and_admin_workflow_guard(tmp_path):
    tz_cn = timezone(timedelta(hours=8))
    # Start clock at 2026-11-15 08:29:00 UTC+8
    now_dt = datetime(2026, 11, 15, 8, 29, 0, tzinfo=tz_cn)
    clock_box = [now_dt.timestamp()]

    due_ts, norm_hhmm = next_daily_due(clock_box[0], "8:30", 480)
    assert norm_hhmm == "08:30"
    assert due_ts == datetime(2026, 11, 15, 8, 30, 0, tzinfo=tz_cn).timestamp()

    plugin = await _boot_plugin(
        tmp_path, permissions=["qq:Alice"], enabled_chats=["c0300b39c80870992aca16d9fcb724c5"]
    )
    plugin.intents.clock = lambda: clock_box[0]
    plugin.ctx.call_capability = _streams_rpc()
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    try:
        alice_ev = _group_msg("m-alice-wf", "每天早上八点半发简报", user_id="Alice")
        await plugin.after_receive(message=alice_ev)
        # Enable built-in digest role
        plugin.profiles.update("digest", enabled=True)

        res = await plugin.later_tool(
            action="schedule",
            profile="digest",
            workspace="daily",
            daily_at=["08:30"],
            reason="早报",
            prompt="生成早报",
            source_msg_id="m-alice-wf",
            chat_id=group_sid,
            platform="qq",
        )
        assert res["success"] is True, res

        # Non-admin Bob in group cannot cancel admin workflow
        bob_ev = _group_msg("m-bob-cancel", "取消早报", user_id="Bob")
        await plugin.after_receive(message=bob_ev)
        denied = await plugin.later_tool(
            action="cancel",
            intent_id=res["intent_id"],
            source_msg_id="m-bob-cancel",
            chat_id=group_sid,
            platform="qq",
        )
        assert denied["success"] is False

        # Trigger 137s after 08:30 -> next day's due_at still anchored at exact 08:30:00 (not 08:32:17!)
        clock_box[0] = datetime(2026, 11, 15, 8, 32, 17, tzinfo=tz_cn).timestamp()
        await plugin.pulse._intents()
        await plugin.engine.drain()
        # Dispatch wake phase of newly created thread
        clock_box[0] += 10
        await plugin.pulse._intents()

        pending = plugin.intents.list(f"chat:{group_sid}")
        assert len(pending) == 1
        expected_next = datetime(2026, 11, 16, 8, 30, 0, tzinfo=tz_cn).timestamp()
        assert pending[0]["due_at"] == expected_next
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_unseen_stream_spec_preconfiguration_and_runtime_match(tmp_path):
    plugin = await _boot_plugin(tmp_path, permissions=["qq:Alice"], enabled_chats=[])
    opened_calls = []

    async def rpc(name, **arguments):
        if name == "chat.get_all_streams":
            return {"streams": []}
        if name == "llm.get_available_models":
            return ["claude-sonnet-4-6"]
        if name == "chat.open_session":
            opened_calls.append(dict(arguments))
            return {"success": True, "session_id": "md5_unseen_88888"}
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "88888 群预配置试跑完成。", "tool_calls": []}
        if name == "maisaka.proactive.trigger":
            return {"success": True}
        return {"success": True}

    plugin.ctx.call_capability = rpc
    try:
        admin_ev = _private_msg("m-precfg", "给 qq:group:88888 预先开通", user_id="Alice")
        await plugin.after_receive(message=admin_ev)

        res = await plugin.shape_tool(
            action="patch",
            kind="settings",
            patch={
                "add_chats": ["qq:group:88888"],
                "chat_proactive_levels": {"qq:group:88888": "quiet"},
            },
            source_msg_id="m-precfg",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res["success"] is True, res
        assert plugin.settings.enabled_chats == ["qq:group:88888"]
        assert plugin.settings.chat_proactive_levels == {"qq:group:88888": "quiet"}

        prof_res = await plugin.shape_tool(
            action="patch",
            kind="profile",
            target="default",
            patch={"access": "listed", "allow_chats": ["qq:group:88888"]},
            source_msg_id="m-precfg",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert prof_res["success"] is True, prof_res
        assert plugin.profiles.get("default").allow_chats == ("qq:group:88888",)

        # Before any message arrives in 88888, admin schedules via qq:group:88888 -> opens runtime session
        later = await plugin.later_tool(
            action="schedule",
            target_chat="qq:group:88888",
            profile="default",
            workspace="precfg",
            daily_at=["09:00"],
            reason="新群早安",
            prompt="准备新群简报",
            source_msg_id="m-precfg",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert later["success"] is True, later
        assert opened_calls and opened_calls[0]["group_id"] == "88888"

        # Now incoming message arrives from MD5 runtime session_id with group_id="88888"
        group_ev = _group_msg(
            "m-unseen-hi", "大家好", session_id="md5_unseen_88888", group_id="88888", user_id="Bob"
        )
        await plugin.after_receive(message=group_ev)
        assert plugin.enabled_for("md5_unseen_88888", "qq") is True
        assert plugin.pulse.level_for("md5_unseen_88888") == "quiet"
        assert plugin._profile_allowed(
            plugin.profiles.get("default"),
            Message.from_event(group_ev),
            "md5_unseen_88888",
            "qq",
            admin_verified=False,
        )
    finally:
        await plugin.on_unload()


def _install_offline_public_rss(monkeypatch, *, install_fetch: bool = False):
    """仅用离线 DNS 与受校验的假传输层替代外网，保留真实 SSRF 公网 IP 校验。"""
    import socket
    import maisya.digest as digest_mod

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        host_str = str(host or "").strip().lower()
        if host_str in {"broken.invalid"} or host_str.endswith(".invalid"):
            raise socket.gaierror(8, "nodename nor servname provided")
        if host_str in {"localhost", "127.0.0.1", "::1"}:
            return real_getaddrinfo(host, port, family, type, proto, flags)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", int(port) if port else 0),
            )
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    if install_fetch:

        def fake_fetch(url, timeout, max_bytes):
            digest_mod._validate_public_url_with_ips(url)
            return {
                "body": (
                    b'<?xml version="1.0" encoding="utf-8"?>'
                    b"<rss version='2.0'><channel><title>Tech Feed</title>"
                    b"<item><title>Quantum Preview</title><link>https://news.example.com/q1</link>"
                    b"<description>Quantum report summary</description></item>"
                    b"</channel></rss>"
                ),
                "content_type": "application/rss+xml",
                "status": 200,
                "url": url,
                "error": "",
            }

        monkeypatch.setattr(digest_mod, "_sync_fetch_public_bytes", fake_fetch)


@pytest.mark.asyncio
async def test_private_chat_unenabled_admin_full_control(tmp_path, monkeypatch):
    """未加白名单的管理员私聊也能跨任意消息流全量配置、RSS 源预设、预览试跑、召回、分享推群与个人提醒。"""
    import maisya.digest as digest_mod

    _install_offline_public_rss(monkeypatch, install_fetch=True)

    plugin = await _boot_plugin(
        tmp_path, permissions=("qq:Alice",), enabled_chats=("chat",)
    )
    plugin.settings.sandbox.enabled = False
    hybrid_sends = []
    proactive_wakes = []
    open_calls = []

    async def capability(name, **arguments):
        if name == "chat.get_all_streams":
            return {"success": True, "streams": []}
        if name == "chat.open_session":
            open_calls.append(arguments)
            return {"success": False}
        if name == "send.hybrid":
            hybrid_sends.append(arguments)
            return {"success": True, "message_id": f"sent-{len(hybrid_sends)}"}
        if name == "maisaka.proactive.trigger":
            proactive_wakes.append(arguments)
            return {"success": True}
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "交付结果给你过目。"}
        if name in {"llm.generate", "llm.generate_with_tools"}:
            return {
                "success": True,
                "response": "量子评测报告完成：https://example.com/report",
                "tool_calls": [],
            }
        return {"success": False}

    plugin.ctx.call_capability = capability
    try:
        # 管理员在未加白名单的私聊（priv_admin_md5 / qq:private:Alice）发话
        priv_ev = _private_msg(
            "m-priv-1",
            "在私聊把未启用新群 qq:group:77777 的记忆、RSS 与任务都安排好并试跑给我看",
            session_id="priv_admin_md5",
            user_id="Alice",
        )
        await plugin.after_receive(message=priv_ev)
        assert plugin.enabled_for("priv_admin_md5", "qq") is False

        # before_planner 仍向未启用私聊里的管理员暴露引导与全量工具
        plan_res = await plugin.before_planner(
            session_id="priv_admin_md5",
            tool_definitions=[
                {"name": "maisya_agent"},
                {"name": "maisya_recall"},
                {"name": "maisya_shape"},
                {"name": "maisya_later"},
            ],
            items=[],
        )
        boot_text = "\n".join(
            p["text"]
            for it in plan_res["modified_kwargs"]["items"]
            for p in it.get("parts", [])
        )
        assert "<maisya_admin_bootstrap>" in boot_text
        assert "qq:private:Alice" in boot_text
        assert "deliver_to" in boot_text

        # 1. 不按先后顺序：目标群 qq:group:77777 还没加入 enabled_chats，先写该群手账
        mem_res = await plugin.shape_tool(
            action="file",
            op="write",
            document="MEMORY.md",
            target_chat="qq:group:77777",
            content="量子前沿论文追踪清单，少发纯营销号。",
            expected_version=0,
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert mem_res["success"] is True, mem_res
        with pytest.raises(ValueError):
            digest_mod._validate_public_url("http://127.0.0.1/rss.xml")
        with pytest.raises(ValueError):
            digest_mod._validate_public_url("https://localhost/feed.xml")
        with pytest.raises(ValueError):
            digest_mod._validate_public_url_with_ips("http://169.254.169.254/latest/meta-data/")

        ov = await plugin.shape_tool(
            action="read",
            kind="overview",
            target_chat="qq:group:77777",
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert ov["success"] is True, ov
        mem_read = await plugin.shape_tool(
            action="file",
            op="read",
            document="MEMORY.md",
            target_chat="qq:group:77777",
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert mem_read["success"] is True, mem_read
        assert "量子前沿论文追踪清单" in mem_read["content"]

        # 2. 目标群尚未进白名单：直接从私聊用 deliver_to="current" 试跑预览，只回私聊不建目标群待办
        preview_run = await plugin.agent_tool(
            action="start",
            profile="default",
            target_chat="qq:group:77777",
            deliver_to="current",
            message="先帮我看一轮量子前沿链接",
            wait_seconds=10,
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert preview_run["success"] is True, preview_run
        assert preview_run["state"] == "idle"
        assert "delivery_intent_id" not in preview_run
        tid = preview_run["thread_id"]

        # 模拟 after_planner 记录本轮私聊预览计划 + before_replyer 向未启用私聊投递 <maisya_result>
        await plugin.after_planner(
            session_id="priv_admin_md5",
            output_items=[
                {
                    "tool_call": {
                        "func_name": "maisya_agent",
                        "args": {
                            "action": "start",
                            "target_chat": "qq:group:77777",
                            "deliver_to": "current",
                            "source_msg_id": "m-priv-1",
                            "wait_seconds": 10,
                        },
                    }
                }
            ],
        )
        assert plugin.bridge.has_pending_for("priv_admin_md5") is True
        rep = await plugin.before_replyer(
            session_id="priv_admin_md5",
            request_type="maisaka.replyer",
            attempt=1,
            reply_tool_args={},
        )
        assert "modified_kwargs" in rep
        extra_prompt = rep["modified_kwargs"]["extra_prompt"]
        assert "<maisya_result>" in extra_prompt
        assert "https://example.com/report" in extra_prompt
        assert "<maisya_memory>" not in extra_prompt

        # 3. 管理员仅传 thread_id（不传 target_chat）在私聊续接与停止跨群线程
        cont_res = await plugin.agent_tool(
            action="send",
            thread_id=tid,
            deliver_to="current",
            message="补充核对一条出处",
            wait_seconds=10,
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert cont_res["success"] is True, cont_res
        stop_other = await plugin.agent_tool(
            action="start",
            profile="default",
            target_chat="qq:group:77777",
            deliver_to="none",
            message="单独建一个待停线程",
            wait_seconds=10,
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert stop_other["success"] is True, stop_other
        stopped = await plugin.agent_tool(
            action="stop",
            thread_id=stop_other["thread_id"],
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert stopped["success"] is True and stopped["state"] == "idle", stopped

        # 4. 从未启用私聊执行跨群检索（指定 target_chat 与 target_chat="all"）
        rec_one = await plugin.recall_tool(
            query="量子",
            target_chat="qq:group:77777",
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert rec_one["success"] is True, rec_one
        assert rec_one["memories"] and rec_one["threads"]
        assert rec_one["threads"][0]["stream_spec"] == "qq:group:77777"

        rec_all = await plugin.recall_tool(
            query="",
            target_chat="all",
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert rec_all["success"] is True, rec_all
        assert any(t["thread_id"] == tid for t in rec_all["threads"])

        # 5. 先在未启用私聊 share 预览该线程（带链接），确认不阻塞随后正式推往目标群
        priv_share = await plugin.agent_tool(
            action="share",
            thread_id=tid,
            message="先私聊发我一份",
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert priv_share["success"] is True, priv_share
        assert hybrid_sends and hybrid_sends[-1]["stream_id"] == "priv_admin_md5"

        # 开启 qq:group:77777 白名单并模拟真群 MD5 session 连线
        enable_res = await plugin.shape_tool(
            action="patch",
            kind="settings",
            patch={"add_chats": ["qq:group:77777"]},
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert enable_res["success"] is True, enable_res

        grp_ev = _group_msg(
            "m-77777-hi",
            "新群来消息了",
            session_id="md5_group_77777",
            group_id="77777",
            user_id="Bob",
        )
        await plugin.after_receive(message=grp_ev)

        # 验证事先配好的手账自动对齐到 md5_group_77777
        assert "量子前沿论文追踪清单" in plugin.memory.context("md5_group_77777")["memory"]

        # 再将同一个 tid 通过 share 正式推往目标群：不应被之前私聊预览的 delivery 拦截
        grp_share = await plugin.agent_tool(
            action="share",
            thread_id=tid,
            target_chat="qq:group:77777",
            message="正式推送到群里",
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert grp_share["success"] is True and not grp_share.get("already_handled"), grp_share
        assert hybrid_sends[-1]["stream_id"] == "md5_group_77777"

        # 6. 在未启用私聊给管理员自己设一条个人提醒（admin_direct=True），到点由 Pulse 正常唤醒
        later_personal = await plugin.later_tool(
            action="schedule",
            in_seconds=30,
            reason="提醒我晚上看一眼日志",
            source_msg_id="m-priv-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert later_personal["success"] is True, later_personal
        assert later_personal["payload"].get("admin_direct") is True

        base_now = later_personal["due_at"] + 1
        plugin.intents.clock = lambda: base_now
        await plugin.pulse.tick()
        assert proactive_wakes and proactive_wakes[-1]["stream_id"] == "priv_admin_md5"
        assert "<maisya_reminder>" in proactive_wakes[-1]["intent"]

        # 7. 未启用私聊里的非管理员仍然零落盘、零工具暴露、直接调用全拒绝
        stranger_ev = _private_msg(
            "m-stranger-1",
            "帮我改设置或看看别的群",
            session_id="priv_stranger_md5",
            user_id="Stranger",
        )
        await plugin.after_receive(message=stranger_ev)
        stranger_plan = await plugin.before_planner(
            session_id="priv_stranger_md5",
            tool_definitions=[
                {"name": "maisya_agent"},
                {"name": "maisya_recall"},
                {"name": "maisya_shape"},
                {"name": "maisya_later"},
            ],
            items=[],
        )
        assert stranger_plan["modified_kwargs"]["tool_definitions"] == []
        for fn in (
            plugin.recall_tool(
                query="量子",
                target_chat="qq:group:77777",
                source_msg_id="m-stranger-1",
                chat_id="priv_stranger_md5",
                platform="qq",
            ),
            plugin.agent_tool(
                action="start",
                target_chat="qq:group:77777",
                deliver_to="current",
                message="越权",
                source_msg_id="m-stranger-1",
                chat_id="priv_stranger_md5",
                platform="qq",
            ),
            plugin.later_tool(
                action="schedule",
                in_seconds=60,
                reason="越权提醒",
                source_msg_id="m-stranger-1",
                chat_id="priv_stranger_md5",
                platform="qq",
            ),
        ):
            res = await fn
            assert res["success"] is False

        with plugin.threads.connect() as db:
            stranger_src = db.execute(
                "SELECT COUNT(*) FROM trusted_sources WHERE session_id='priv_stranger_md5'"
            ).fetchone()[0]
            stranger_rt = db.execute(
                "SELECT COUNT(*) FROM session_routes WHERE session_id='priv_stranger_md5'"
            ).fetchone()[0]
        assert stranger_src == 0
        assert stranger_rt == 0
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_multi_session_id_disambiguation_and_empty_account_upgrade(tmp_path):
    plugin = await _boot_plugin(
        tmp_path, permissions=["qq:Alice"], enabled_chats=["qq:group:100246809"]
    )

    async def rpc(name, **arguments):
        if name == "chat.get_all_streams":
            return {"streams": []}
        if name == "llm.get_available_models":
            return ["claude-sonnet-4-6"]
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "多会话归并测试完成。", "tool_calls": []}
        return {"success": True}

    plugin.ctx.call_capability = rpc
    try:
        # 同一逻辑群 qq:group:100246809 下存在多个历史/多账号/仅记忆 session_id
        plugin.messages.upsert_route(
            "sid_legacy_empty",
            platform="qq",
            chat_type="group",
            group_id="100246809",
            account_id="",
        )
        plugin.messages.upsert_route(
            "sid_bot_old",
            platform="qq",
            chat_type="group",
            group_id="100246809",
            account_id="30002",
        )
        plugin.messages.record(
            Message(
                "m-grp-human",
                "sid_bot_active",
                "qq",
                "Bob",
                False,
                False,
                group_id="100246809",
                account_id="30001",
            )
        )
        plugin.messages.upsert_route(
            "sid_mem_sibling",
            platform="qq",
            chat_type="group",
            group_id="100246809",
            account_id="",
        )
        mem_write = plugin.memory.write(
            "memory",
            "关注独立游戏与开源工具，拒绝水文。",
            chat_id="sid_mem_sibling",
            expected_version=0,
        )
        assert mem_write["content"] == "关注独立游戏与开源工具，拒绝水文。"

        admin_ev = _private_msg(
            "m-admin-multi",
            "查看独立开发社群并排好每日早报",
            session_id="priv_admin_30001",
            user_id="Alice",
        )
        admin_ev["message_info"]["account_id"] = "30001"
        await plugin.after_receive(message=admin_ev)
        plugin.messages.upsert_route(
            "priv_admin_30001",
            platform="qq",
            chat_type="private",
            user_id="Alice",
            account_id="30001",
        )

        # 1. overview 精准消歧并折叠 known_chats，且桥接 sibling session_id 的 MEMORY.md
        ov = await plugin.shape_tool(
            action="read",
            kind="overview",
            target_chat="qq:group:100246809",
            source_msg_id="m-admin-multi",
            chat_id="priv_admin_30001",
            platform="qq",
        )
        assert ov["success"] is True, ov
        group_known = [
            item
            for item in ov["known_chats"]
            if item.get("stream_spec") == "qq:group:100246809"
        ]
        assert len(group_known) == 1
        assert group_known[0]["chat_id"] == "sid_bot_active"
        mem_res = await plugin.shape_tool(
            action="file",
            op="read",
            document="MEMORY.md",
            target_chat="qq:group:100246809",
            source_msg_id="m-admin-multi",
            chat_id="priv_admin_30001",
            platform="qq",
        )
        assert mem_res["success"] is True, mem_res
        assert "关注独立游戏与开源工具" in mem_res["content"]
        assert "关注独立游戏与开源工具" in plugin.memory.context("sid_bot_active")["memory"]

        # 2. maisya_later 按 qq:group:100246809 安排每日计划应精准落到 sid_bot_active
        later_daily = await plugin.later_tool(
            action="schedule",
            daily_at=["09:00"],
            profile="default",
            workspace="digest",
            reason="执行每日资讯简报",
            target_chat="qq:group:100246809",
            replace=True,
            source_msg_id="m-admin-multi",
            chat_id="priv_admin_30001",
            platform="qq",
        )
        assert later_daily["success"] is True, later_daily
        assert later_daily["target"] == "chat:sid_bot_active"

        # 3. 显式传入旧的空 account_id session_id 也会自动升级到 sid_bot_active
        assert plugin.resolve_known_session_id("sid_legacy_empty") == "sid_bot_active"
        later_legacy = await plugin.later_tool(
            action="schedule",
            in_seconds=60,
            reason="测试空账号会话自动升级",
            target_chat="sid_legacy_empty",
            source_msg_id="m-admin-multi",
            chat_id="priv_admin_30001",
            platform="qq",
        )
        assert later_legacy["success"] is True, later_legacy
        assert later_legacy["target"] == "chat:sid_bot_active"

        # 4. maisya_agent 按 qq:group:100246809 启动线程也应绑定 sid_bot_active
        agent_res = await plugin.agent_tool(
            action="start",
            profile="default",
            target_chat="qq:group:100246809",
            deliver_to="none",
            message="核查多 session 归并目标",
            wait_seconds=10,
            source_msg_id="m-admin-multi",
            chat_id="priv_admin_30001",
            platform="qq",
        )
        assert agent_res["success"] is True, agent_res
        row = plugin.threads.get(agent_res["thread_id"], "priv_admin_30001", allow_any_chat=True)
        assert row["chat_id"] == "sid_bot_active"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_reused_workspace_thread_admin_direct_preview_wake_and_isolation(
    tmp_path, monkeypatch
):
    """复用已有 digest 工作区线程时，admin_direct grant 须按 revision 更新，支持后台唤醒并允许在未启用私聊 share 预览图而不泄漏。"""
    import asyncio

    _install_offline_public_rss(monkeypatch, install_fetch=False)

    clock = [1794500000.0]
    plugin = await _boot_plugin(
        tmp_path, permissions=("qq:Alice",), enabled_chats=("qq:group:100246809",)
    )
    plugin.settings.sandbox.enabled = False
    plugin.bridge.clock = lambda: clock[0]
    plugin.pulse.clock = lambda: clock[0]

    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    llm_phase = {"step": 0, "emit_card": False}
    hybrid_sends = []
    proactive_wakes = []

    async def capability(name, **arguments):
        if name == "chat.get_all_streams":
            return {"success": True, "streams": []}
        if name == "chat.open_session":
            return {"success": False}
        if name == "render.html2png":
            return {
                "success": True,
                "result": {
                    "image_base64": png_b64,
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                },
            }
        if name == "send.hybrid":
            hybrid_sends.append(arguments)
            return {"success": True, "sent": True, "message_id": f"sent-{len(hybrid_sends)}"}
        if name == "maisaka.proactive.trigger":
            proactive_wakes.append(arguments)
            return {"success": True}
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "试跑简报卡片已备好。"}
        if name in {"llm.generate", "llm.generate_with_tools"}:
            llm_phase["step"] += 1
            if llm_phase["emit_card"] and llm_phase["step"] == 1:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "call_render",
                            "type": "function",
                            "function": {
                                "name": "html_render",
                                "arguments": json.dumps(
                                    {"html": "<div>digest card</div>", "output_path": "out/card.png"}
                                ),
                            },
                        }
                    ],
                }
            if llm_phase["emit_card"] and llm_phase["step"] == 2:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "call_attach",
                            "type": "function",
                            "function": {
                                "name": "result_attach",
                                "arguments": json.dumps({"paths": ["out/card.png"]}),
                            },
                        }
                    ],
                }
            return {
                "success": True,
                "response": "资讯预览图已生成，请查收。" if llm_phase["emit_card"] else "群常规简报已完成。",
                "tool_calls": [],
            }
        return {"success": False}

    plugin.ctx.call_capability = capability
    try:
        # 1. 先在白名单群以常规非预览身份建好 digest 工作区绑定线程（无 admin_direct）
        group_sid = "sid_group_digest"
        plugin.messages.upsert_route(
            group_sid,
            platform="qq",
            chat_type="group",
            group_id="100246809",
        )
        setup_ev = _private_msg(
            "m-enable-digest",
            "开启 digest 角色",
            session_id="priv_admin_md5",
            user_id="Alice",
        )
        await plugin.after_receive(message=setup_ev)
        en = await plugin.shape_tool(
            action="profile",
            op="create",
            target="digest",
            base="default",
            patch={
                "enabled": True,
                "network": "public",
                "max_steps": 20,
                "session_mode": "persistent",
            },
            source_msg_id="m-enable-digest",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert en["success"] is True, en

        grp_ev = _group_msg(
            "m-grp-1",
            "生成常规群简报",
            session_id=group_sid,
            group_id="100246809",
            user_id="Bob",
        )
        await plugin.after_receive(message=grp_ev)
        initial = await plugin.agent_tool(
            action="start",
            profile="digest",
            workspace="digest",
            message="先建好 digest 工作区常规线程",
            source_msg_id="m-grp-1",
            wait_seconds=10,
            chat_id=group_sid,
            platform="qq",
        )
        assert initial["success"] is True, initial
        assert initial["state"] == "idle"
        shared_tid = initial["thread_id"]
        assert plugin.threads.get(shared_tid, group_sid)["grant"].get("admin_direct") is not True

        # 2. 在未加白名单的管理员私聊配置并执行 cross-chat start(deliver_to="current") 复用同一 digest 工作区线程
        admin_ev = _private_msg(
            "m-priv-preview",
            "配置并试跑群 100246809 的 digest 卡片发我预览",
            session_id="priv_admin_md5",
            user_id="Alice",
        )
        await plugin.after_receive(message=admin_ev)
        assert plugin.enabled_for("priv_admin_md5", "qq") is False

        llm_phase["step"] = 0
        llm_phase["emit_card"] = True
        patch_res = await plugin.shape_tool(
            action="profile",
            op="patch",
            target="digest",
            patch={"enabled": True},
            source_msg_id="m-priv-preview",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert patch_res["success"] is True, patch_res
        setup_res = await plugin.agent_tool(
            action="start",
            profile="digest",
            target_chat="qq:group:100246809",
            deliver_to="current",
            workspace="digest",
            message="生成群 100246809 的 digest 卡片发我预览",
            wait_seconds=0,
            source_msg_id="m-priv-preview",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert setup_res["success"] is True, setup_res
        assert setup_res["thread_id"] == shared_tid

        # 等待后台 revision=2 产图完成
        for _ in range(100):
            row_after = plugin.threads.get(shared_tid, "priv_admin_md5", allow_any_chat=True)
            if row_after["state"] == "idle" and row_after["completed_revision"] >= 2:
                break
            await asyncio.sleep(0.01)
        assert row_after["state"] == "idle"
        assert row_after["completed_revision"] == 2
        assert row_after["grant"].get("admin_direct") is True
        assert row_after["result"].get("attachments")

        # 推进时钟 900 秒（>10 分钟，仍在 admin_direct 30 分钟窗口内），Pulse._returns 应唤醒私聊
        clock[0] += 900.0
        await plugin.pulse._returns()
        assert len(proactive_wakes) == 1
        assert proactive_wakes[0]["stream_id"] == "priv_admin_md5"
        assert proactive_wakes[0]["reason"] == "maisya:return"

        # 模拟后台主动唤醒进入 before_planner（此时无实时管理员消息来源）
        plugin._turn_sources.pop("priv_admin_md5", None)
        plugin._pending_sources.pop("priv_admin_md5", None)
        plan_res = await plugin.before_planner(
            session_id="priv_admin_md5",
            tool_definitions=[
                {"name": "maisya_agent"},
                {"name": "maisya_recall"},
                {"name": "maisya_shape"},
                {"name": "maisya_later"},
                {"name": "subagent"},
            ],
            items=[],
            item_schema_version=2,
        )
        kept_names = [t["name"] for t in plan_res["modified_kwargs"]["tool_definitions"]]
        assert "maisya_agent" in kept_names
        assert "maisya_shape" not in kept_names
        assert "maisya_later" not in kept_names
        assert "subagent" not in kept_names
        assert plan_res["modified_kwargs"]["item_schema_version"] == 2
        injected_text = "\n".join(
            p["text"]
            for it in plan_res["modified_kwargs"]["items"]
            for p in it.get("parts", [])
        )
        assert "<maisya_returns>" in injected_text
        assert shared_tid in injected_text

        # Planner 在未启用私聊调用 maisya_agent(action="share") 发出预览图卡片
        att_handle = row_after["result"]["attachments"][0]["handle"]
        share_res = await plugin.agent_tool(
            action="share",
            thread_id=shared_tid,
            message="这是刚跑出来的预览简报",
            attachment_handles=[att_handle],
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert share_res["success"] is True, share_res
        assert share_res["sent"] is True
        assert len(hybrid_sends) == 1
        assert hybrid_sends[0]["stream_id"] == "priv_admin_md5"
        assert any(seg["type"] == "image" for seg in hybrid_sends[0]["segments"])

        # 已消费后再次进入无源 before_planner 应重新剥离全部 maisya 工具；陌生未启用会话同样零授权
        after_consume = await plugin.before_planner(
            session_id="priv_admin_md5",
            tool_definitions=[
                {"name": "maisya_agent"},
                {"name": "maisya_recall"},
                {"name": "maisya_shape"},
                {"name": "maisya_later"},
            ],
            items=[],
        )
        assert after_consume["modified_kwargs"]["tool_definitions"] == []
        stranger = await plugin.agent_tool(
            action="share",
            thread_id=shared_tid,
            chat_id="priv_stranger_md5",
            platform="qq",
        )
        assert stranger["success"] is False

        # 后续同一复用工作区线程开启非预览新修订版，不得继承陈旧的 admin_direct grant
        clock[0] += 10.0
        grp_ev2 = _group_msg(
            "m-grp-2",
            "继续在原工作区生成常规群简报",
            session_id=group_sid,
            group_id="100246809",
            user_id="Bob",
        )
        await plugin.after_receive(message=grp_ev2)
        llm_phase["step"] = 0
        llm_phase["emit_card"] = False
        followup = await plugin.agent_tool(
            action="start",
            profile="digest",
            workspace="digest",
            message="再次生成群内常规简报",
            source_msg_id="m-grp-2",
            wait_seconds=10,
            chat_id=group_sid,
            platform="qq",
        )
        assert followup["success"] is True, followup
        assert followup["thread_id"] == shared_tid
        row_rev3 = plugin.threads.get(shared_tid, group_sid)
        assert row_rev3["revision"] == 3
        assert row_rev3["grant"].get("admin_direct") is not True
    finally:
        await plugin.on_unload()


def _write_host_model_config(tmp_path, toml_body: str):
    cfg = tmp_path / "host/config/model_config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(toml_body, encoding="utf-8")
    return cfg


@pytest.mark.asyncio
async def test_replyer_ops_and_delivery_steering_away_from_mai_replier(
    tmp_path, monkeypatch
):
    """Scheme A：配置/排班 ops 回执与 bridge 交付事实避开 mai_replier，普通闲聊不被改写模型。"""

    _install_offline_public_rss(monkeypatch, install_fetch=False)
    _write_host_model_config(
        tmp_path,
        "\n".join(
            [
                "[[models]]",
                'name = "mai_replier_today"',
                'model_identifier = "mai_replier_today"',
                'api_provider = "test"',
                "",
                "[[models]]",
                'name = "gemini-3.8-flash"',
                'model_identifier = "gemini-3.8-flash"',
                'api_provider = "test"',
                "",
                "[model_task_config.replyer]",
                'model_list = ["mai_replier_today", "gemini-3.8-flash"]',
                "",
                "[model_task_config.planner]",
                'model_list = ["gemini-3.8-flash"]',
            ]
        ),
    )

    plugin = await _boot_plugin(
        tmp_path, permissions=("qq:Alice",), enabled_chats=("qq:group:100246809",)
    )
    plugin.settings.sandbox.enabled = False

    async def capability(name, **arguments):
        if name == "chat.get_all_streams":
            return {"success": True, "streams": []}
        if name == "chat.open_session":
            return {"success": False}
        if name in {"llm.generate", "llm.generate_with_tools"}:
            return {"success": True, "response": "群内调查结果如下。", "tool_calls": []}
        return {"success": False}

    plugin.ctx.call_capability = capability
    try:
        # 1. 普通白名单群闲聊（即使带 <maisya_memory>，无 ops 回执且无 bridge 交付）不改写 replyer 模型
        group_sid = "sid_enabled_group"
        plugin.messages.upsert_route(
            group_sid, platform="qq", chat_type="group", group_id="100246809"
        )
        plugin.memory.write(
            "memory",
            "讨论软件工程与开源工具。",
            chat_id=group_sid,
            expected_version=0,
        )
        casual = await plugin.before_replyer(
            session_id=group_sid,
            request_type="maisaka.replyer",
            attempt=1,
            model_name="mai_replier_today",
            reply_tool_args={"reply_style": "简短"},
        )
        assert casual["action"] == "continue"
        assert "<maisya_memory>" in casual["modified_kwargs"]["extra_prompt"]
        assert "<maisya_ops_receipt>" not in casual["modified_kwargs"]["extra_prompt"]
        assert "model_name" not in casual["modified_kwargs"]

        # 2. 未加白名单管理员私聊执行 maisya_shape(action="patch", kind="settings") -> before_replyer 强制纠偏并默认长回复
        admin_ev = _private_msg(
            "m-ops-setup",
            "帮我把群 100246809 加入配置并设好安静时段",
            session_id="priv_admin_md5",
            user_id="Alice",
        )
        await plugin.after_receive(message=admin_ev)
        await plugin.before_planner(
            session_id="priv_admin_md5",
            tool_definitions=[{"name": "maisya_shape"}],
            items=[],
        )
        shape_ok = await plugin.shape_tool(
            action="patch",
            kind="settings",
            patch={"add_chats": ["qq:group:100246809"], "sleep_window": "00:00-07:00"},
            source_msg_id="m-ops-setup",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert shape_ok["success"] is True

        rep1 = await plugin.before_replyer(
            session_id="priv_admin_md5",
            request_type="maisaka.replyer",
            attempt=1,
            model_name="mai_replier_today",
            reply_tool_args={},
        )
        mod1 = rep1["modified_kwargs"]
        assert mod1["model_name"] == "gemini-3.8-flash"
        assert mod1["reply_tool_args"]["reply_style"] == "长回复"
        assert "<maisya_ops_receipt>" in mod1["extra_prompt"]

        # 同一 reply 重试（attempt=2 携带 _maisya_ops_token）保持相同引导与模型选择
        rep_retry = await plugin.before_replyer(
            session_id="priv_admin_md5",
            request_type="maisaka.replyer",
            attempt=2,
            model_name="mai_replier_today",
            reply_tool_args=mod1["reply_tool_args"],
        )
        assert rep_retry["modified_kwargs"]["model_name"] == "gemini-3.8-flash"
        assert rep_retry["modified_kwargs"]["reply_tool_args"]["reply_style"] == "长回复"
        assert "<maisya_ops_receipt>" in rep_retry["modified_kwargs"]["extra_prompt"]

        # 发出后（before_send）及后续普通轮次不再误触发 ops 纠偏
        await plugin.before_send(session_id="priv_admin_md5", message="好呀，已经配好了。")
        rep_next = await plugin.before_replyer(
            session_id="priv_admin_md5",
            request_type="maisaka.replyer",
            attempt=1,
            model_name="mai_replier_today",
            reply_tool_args={},
        )
        assert rep_next == {"action": "continue"}

        # 只读总览使用任务模型，保留普通回复风格。
        read_res = await plugin.shape_tool(
            action="read",
            kind="overview",
            target_chat="qq:group:100246809",
            source_msg_id="m-ops-setup",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert read_res["success"] is True
        read_reply = await plugin.before_replyer(
            session_id="priv_admin_md5",
            request_type="maisaka.replyer",
            attempt=1,
            model_name="mai_replier_today",
            reply_tool_args={},
        )
        assert read_reply["action"] == "continue"
        read_kwargs = read_reply["modified_kwargs"]
        assert read_kwargs["model_name"] == "gemini-3.8-flash"
        assert "extra_prompt" not in read_kwargs
        assert set(read_kwargs["reply_tool_args"]) == {"_maisya_task_token"}

        later_res = await plugin.later_tool(
            action="schedule",
            in_seconds=120,
            reason="提醒检查配置",
            target_chat="qq:group:100246809",
            source_msg_id="m-ops-setup",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert later_res["success"] is True
        rep_later = await plugin.before_replyer(
            session_id="priv_admin_md5",
            request_type="maisaka.replyer",
            attempt=1,
            model_name="mai_replier_today",
            reply_tool_args={"reply_style": "逐条"},
        )
        assert rep_later["modified_kwargs"]["model_name"] == "gemini-3.8-flash"
        assert rep_later["modified_kwargs"]["reply_tool_args"]["reply_style"] == "逐条"

        # 3. 已启用群有 bridge.reply_material 线程交付事实时，也会避开 mai_replier
        grp_ev = _group_msg(
            "m-grp-bridge",
            "查一下问题",
            session_id=group_sid,
            group_id="100246809",
            user_id="Bob",
        )
        await plugin.after_receive(message=grp_ev)
        run_res = await plugin.agent_tool(
            action="start",
            profile="default",
            message="核查一个事实",
            source_msg_id="m-grp-bridge",
            wait_seconds=10,
            chat_id=group_sid,
            platform="qq",
        )
        assert run_res["success"] is True
        await plugin.after_planner(
            session_id=group_sid,
            output_items=[
                {
                    "tool_call": {
                        "func_name": "maisya_agent",
                        "args": {"action": "start", "source_msg_id": "m-grp-bridge", "wait_seconds": 10},
                    }
                }
            ],
        )
        bridge_rep = await plugin.before_replyer(
            session_id=group_sid,
            request_type="maisaka.replyer",
            attempt=1,
            model_name="mai_replier_today",
            reply_tool_args={},
        )
        assert "群内调查结果如下" in html.unescape(bridge_rep["modified_kwargs"]["extra_prompt"])
        assert bridge_rep["modified_kwargs"]["model_name"] == "gemini-3.8-flash"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_replyer_model_steering_fallback_and_non_mai_skip(tmp_path):
    """replyer 全是 mai_replier 时回退 planner/utils；显式非 mai_replier 或无 mai_replier 不改写。"""
    plugin = await _boot_plugin(
        tmp_path, permissions=("qq:Alice",), enabled_chats=("qq:group:100246809",)
    )
    try:
        # 1. replyer 全是 v2p1_contract / mai_replier -> 回退到 planner 模型
        _write_host_model_config(
            tmp_path,
            "\n".join(
                [
                    "[[models]]",
                    'name = "replier-alias"',
                    'model_identifier = "v2p1_contract"',
                    "",
                    "[[models]]",
                    'name = "gpt-5.4"',
                    'model_identifier = "gpt-5.4"',
                    "",
                    "[model_task_config.replyer]",
                    'model_list = ["replier-alias"]',
                    "",
                    "[model_task_config.planner]",
                    'model_list = ["gpt-5.4"]',
                ]
            ),
        )
        assert plugin.refresh_host_model_catalog() is True
        assert plugin._select_ops_replyer_model("") == "gpt-5.4"
        assert plugin._select_ops_replyer_model("replier-alias") == "gpt-5.4"
        # 请求已是常规非 mai_replier 模型 -> 不改写
        assert plugin._select_ops_replyer_model("gpt-5.4") == ""

        # 2. replyer 列表里不含任何 mai_replier 模型 -> 不做多余覆盖
        _write_host_model_config(
            tmp_path,
            "\n".join(
                [
                    "[[models]]",
                    'name = "gemini-3.8-flash"',
                    'model_identifier = "gemini-3.8-flash"',
                    "",
                    "[model_task_config.replyer]",
                    'model_list = ["gemini-3.8-flash"]',
                ]
            ),
        )
        plugin._host_model_mtime = None
        assert plugin.refresh_host_model_catalog() is True
        assert plugin._select_ops_replyer_model("") == ""
    finally:
        await plugin.on_unload()

