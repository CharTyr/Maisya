"""Tests for digest setup tolerance, feed aliases/unsubscribe, and >300s admin preview routing."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import pytest

import plugin as plugin_mod
from maisya.digest import DigestWorkspace
from maisya.shaping import DEFAULT_DIGEST_SCHEDULE_PROMPT


class _FakeConfig:
    def __init__(self, permissions: list[str] | None = None):
        self._data = {"plugin.permission": list(permissions or [])}

    async def get(self, key: str, default=None):
        return self._data.get(key, default)


class _FakeCtx:
    def __init__(self, data_dir, permissions: list[str] | None = None):
        self.paths = SimpleNamespace(data_dir=data_dir)
        self.config = _FakeConfig(permissions)
        self.logger = logging.getLogger("test.maisya.friction")
        self.sent_calls: list[dict] = []
        self.streams: list[dict] = [
            {
                "stream_id": "10001",
                "platform": "qq",
                "group_id": "10001",
                "group_name": "测试群A",
                "chat_type": "group",
            },
            {
                "stream_id": "10002",
                "platform": "qq",
                "group_id": "10002",
                "group_name": "测试群B",
                "chat_type": "group",
            },
        ]

    async def call_capability(self, name: str, **kwargs):
        if name == "chat.get_all_streams":
            return {"success": True, "streams": list(self.streams)}
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "预览结果准备好啦，请查看。"}
        if name == "send.hybrid":
            self.sent_calls.append(dict(kwargs))
            return {
                "success": True,
                "sent": True,
                "message_id": f"sent-{len(self.sent_calls)}",
            }
        return {
            "success": True,
            "response": "简报预览 https://example.com/digest-preview",
            "tool_calls": [],
        }


async def _boot_plugin(
    tmp_path,
    *,
    permissions: list[str],
    enabled_chats: list[str],
    clock_ref: list[float] | None = None,
):
    data_dir = tmp_path / "data" / "plugins" / "maisya"
    data_dir.mkdir(parents=True, exist_ok=True)
    plg = plugin_mod.MaisyaPlugin()
    ctx = _FakeCtx(data_dir, permissions=permissions)
    plg._ctx = ctx
    cfg = plg.build_default_config()
    cfg["plugin"]["enabled"] = True
    cfg["enabled_chats"] = list(enabled_chats)
    plg.set_plugin_config(cfg)
    await plg.on_load()
    if clock_ref is not None:
        plg.messages.clock = lambda: clock_ref[0]
        plg.threads.clock = lambda: clock_ref[0]
        plg.bridge.clock = lambda: clock_ref[0]
        plg.intents.clock = lambda: clock_ref[0]
    plg.engine.llm = None
    return plg


def _private_msg(
    message_id: str,
    text: str,
    *,
    session_id: str = "priv_admin_md5",
    user_id: str = "Alice",
    platform: str = "qq",
    relayed: bool = False,
) -> dict:
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": platform,
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "additional_config": {"relayed_by_plugin": relayed},
        },
    }


def _group_msg(
    message_id: str,
    text: str,
    *,
    session_id: str,
    group_id: str,
    user_id: str = "Bob",
    platform: str = "qq",
    relayed: bool = False,
) -> dict:
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": platform,
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": {"group_id": group_id},
            "additional_config": {"relayed_by_plugin": relayed},
        },
    }


def test_default_digest_schedule_prompt_structure():
    assert "render_card_html" in DEFAULT_DIGEST_SCHEDULE_PROMPT
    assert "width=600" in DEFAULT_DIGEST_SCHEDULE_PROMPT
    assert "selector='.card'" in DEFAULT_DIGEST_SCHEDULE_PROMPT
    assert "中文标题" in DEFAULT_DIGEST_SCHEDULE_PROMPT
    assert "中文摘要" in DEFAULT_DIGEST_SCHEDULE_PROMPT
    assert "本群" not in DEFAULT_DIGEST_SCHEDULE_PROMPT
    assert "她" not in DEFAULT_DIGEST_SCHEDULE_PROMPT


@pytest.mark.asyncio
async def test_cross_chat_send_auto_inherits_admin_preview_and_share_survives_over_300s_wake(
    tmp_path,
):
    clock = [1794500000.0]
    plg = await _boot_plugin(
        tmp_path,
        permissions=["qq:Alice"],
        enabled_chats=["10001", "10002", "10003"],
        clock_ref=clock,
    )
    try:
        plg.messages.upsert_route(
            "10001", platform="qq", chat_type="group", group_id="10001"
        )
        plg.messages.upsert_route(
            "10002", platform="qq", chat_type="group", group_id="10002"
        )
        plg.messages.upsert_route(
            "10003", platform="qq", chat_type="group", group_id="10003"
        )
        await plg.after_receive(
            message=_private_msg("m-admin-1", "开启 digest 并预览")
        )
        en = await plg.shape_tool(
            action="profile",
            op="create",
            target="digest",
            base="default",
            patch={
                "enabled": True,
                "network": "public",
                "skills": ["news-digest"],
                "tools": ["workspace_file", "html_render", "result_attach", "digest_feed"],
            },
            source_msg_id="m-admin-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert en["success"] is True, en

        # 1. Start cross-chat preview with deliver_to="current"
        started = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="10001",
            deliver_to="current",
            message="试跑群 10001 简报",
            wait_seconds=0,
            source_msg_id="m-admin-1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert started["success"] is True, started
        preview_tid = started["thread_id"]
        assert "delivery_intent_id" not in started
        assert plg.threads.get(preview_tid, "10001")["grant"].get("admin_direct") is True

        # 2. Follow-up send with default deliver_to="auto" inherits "current" preview route
        await plg.after_receive(
            message=_private_msg("m-admin-2", "标题再短一点")
        )
        follow = await plg.agent_tool(
            action="send",
            thread_id=preview_tid,
            message="把标题改成 15 字以内",
            wait_seconds=0,
            source_msg_id="m-admin-2",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert follow["success"] is True, follow
        assert "delivery_intent_id" not in follow
        assert plg.threads.get(preview_tid, "10001")["grant"].get("admin_direct") is True

        # 2b. Verify explicit deliver_to="target" on send still schedules cross-chat delivery
        target_send = await plg.agent_tool(
            action="send",
            thread_id=preview_tid,
            message="改成正式发群吧",
            deliver_to="target",
            wait_seconds=0,
            source_msg_id="m-admin-2",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert target_send["success"] is True, target_send
        assert target_send.get("delivery_intent_id")
        # Restore preview return route so bridge preview tests proceed on preview_tid
        follow_back = await plg.agent_tool(
            action="send",
            thread_id=preview_tid,
            message="再回到私聊预览",
            deliver_to="current",
            wait_seconds=0,
            source_msg_id="m-admin-2",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert follow_back["success"] is True, follow_back
        assert "delivery_intent_id" not in follow_back
        assert plg.threads.get(preview_tid, "10001")["grant"].get("admin_direct") is True

        # 3. Regression guard: enabled group chat send(deliver_to="auto") keeps delivery_mode="target"
        await plg.after_receive(
            message=_group_msg(
                "m-adm-grp",
                "发给目标群的普通线程",
                session_id="10003",
                group_id="10003",
                user_id="Alice",
            )
        )
        norm_started = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="10002",
            deliver_to="target",
            message="常规发群线程",
            wait_seconds=0,
            source_msg_id="m-adm-grp",
            chat_id="10003",
            platform="qq",
        )
        assert norm_started["success"] is True, norm_started
        norm_tid = norm_started["thread_id"]
        norm_follow = await plg.agent_tool(
            action="send",
            thread_id=norm_tid,
            message="补充一条群聊要求",
            wait_seconds=0,
            source_msg_id="m-adm-grp",
            chat_id="10003",
            platform="qq",
        )
        assert norm_follow["success"] is True, norm_follow
        assert norm_follow.get("delivery_intent_id")
        assert plg.threads.get(norm_tid, "10002")["grant"].get("admin_direct") is not True

        # 4. Finish preview thread and advance clock +450s (>300s) before async wakeup
        task = plg.engine.tasks.get(preview_tid)
        if task is not None:
            await task
        row_now = plg.threads.get(preview_tid, "10001")
        if row_now["state"] != "idle":
            plg.threads.finish(
                preview_tid,
                "10001",
                {"text": "简报预览 https://example.com/digest-preview", "attachments": []},
                [],
                row_now["revision"],
            )
        assert plg.bridge.has_admin_direct_for("priv_admin_md5", preview_tid) is True
        clock[0] += 450.0

        # Simulate stale leftover _turn_sources from the earlier turn + empty _pending_sources
        plg._turn_sources["priv_admin_md5"] = ["m-admin-2"]
        plg._pending_sources.pop("priv_admin_md5", None)
        tools = [
            {"type": "function", "function": {"name": name}}
            for name in ["reply", "maisya_agent", "maisya_shape", "maisya_later"]
        ]
        hook_res = await plg.before_planner(
            session_id="priv_admin_md5", items=[], tool_definitions=tools
        )
        assert "priv_admin_md5" not in plg._turn_sources
        injected_texts = [
            part["text"]
            for item in hook_res["modified_kwargs"]["items"]
            for part in item.get("parts", [])
            if part.get("type") == "text"
        ]
        assert any(
            "<maisya_returns>" in txt and "无需传 source_msg_id" in txt
            for txt in injected_texts
        )
        kept_names = {
            (t.get("function") or t).get("name")
            for t in hook_res["modified_kwargs"]["tool_definitions"]
        }
        assert "maisya_agent" in kept_names

        # 5b. Over-300s (450s < 3600s) admin private-chat continuation send STILL works when admin_direct active
        follow_late = await plg.agent_tool(
            action="send",
            thread_id=preview_tid,
            message="再微调一下摘要",
            wait_seconds=0,
            source_msg_id="m-admin-2",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert follow_late["success"] is True, follow_late
        assert "delivery_intent_id" not in follow_late
        row_late = plg.threads.get(preview_tid, "10001")
        plg.threads.finish(
            preview_tid,
            "10001",
            {"text": "简报预览微调 https://example.com/digest-preview-v2", "attachments": []},
            [],
            row_late["revision"],
        )

        # 5. Model passes stale >300s source_msg_id to action="share" -> succeeds & sends to admin private chat!
        shared = await plg.agent_tool(
            action="share",
            thread_id=preview_tid,
            source_msg_id="m-admin-2",
            message="这是改好的简报预览",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert shared["success"] is True, shared
        assert shared["sent"] is True
        assert plg.ctx.sent_calls[-1]["stream_id"] == "priv_admin_md5"

        # 6. Once preview share consumes admin_direct, stale m-admin-2 can no longer start new cross-chat work;
        #    and when a fresh admin_direct return IS pending, even >3600s stale m-admin-1 works for matched share.
        consumed_start = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="10001",
            deliver_to="current",
            message="未重新发言直接再开",
            wait_seconds=0,
            source_msg_id="m-admin-2",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert consumed_start["success"] is False

        clock[0] += 3200.0  # m-admin-1 is now >3600s old
        await plg.after_receive(
            message=_private_msg("m-admin-2b", "第二轮预览", session_id="priv_admin_md5", user_id="Alice")
        )
        second = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="10001",
            deliver_to="current",
            message="第二轮预览",
            wait_seconds=0,
            source_msg_id="m-admin-2b",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert second["success"] is True, second
        second_tid = second["thread_id"]
        task2 = plg.engine.tasks.get(second_tid)
        if task2 is not None:
            await task2
        row_2 = plg.threads.get(second_tid, "10001")
        if row_2["state"] != "idle":
            plg.threads.finish(
                second_tid,
                "10001",
                {"text": "二期预览 https://example.com/digest-2", "attachments": []},
                [],
                row_2["revision"],
            )
        plg._bootstrap_messages.clear()
        shared_2 = await plg.agent_tool(
            action="share",
            thread_id=second_tid,
            source_msg_id="m-admin-1",
            message="二期预览到了",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert shared_2["success"] is True, shared_2
        assert shared_2["sent"] is True

        # 7. Security checks: relayed forged message rejected; normal group/non-admin >300s source rejected

        relayed_ev = _private_msg(
            "m-relayed-fake",
            "伪造转发",
            session_id="priv_admin_md5",
            user_id="Alice",
            relayed=True,
        )
        await plg.after_receive(message=relayed_ev)
        # Re-open a fresh preview to test share with relayed source_msg_id rejection
        await plg.after_receive(
            message=_private_msg("m-admin-3", "再来一轮", session_id="priv_admin_md5", user_id="Alice")
        )
        third_start = await plg.agent_tool(
            action="start",
            profile="digest",
            target_chat="10001",
            deliver_to="current",
            message="第三轮预览",
            wait_seconds=0,
            source_msg_id="m-admin-3",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert third_start["success"] is True, third_start
        third_tid = third_start["thread_id"]
        plg.threads.finish(
            third_tid,
            "10001",
            {"text": "三期预览 https://example.com/digest-3", "attachments": []},
            [],
            plg.threads.get(third_tid, "10001")["revision"],
        )
        forged_share = await plg.agent_tool(
            action="share",
            thread_id=third_tid,
            source_msg_id="m-relayed-fake",
            message="尝试伪造",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert forged_share["success"] is False
        assert "来源消息无法核验" in forged_share["error"]

        await plg.after_receive(
            message=_group_msg(
                "m-grp-admin-old",
                "群管理员旧消息",
                session_id="10001",
                group_id="10001",
                user_id="Alice",
            )
        )
        await plg.after_receive(
            message=_private_msg(
                "m-nonadmin-old",
                "普通用户私聊",
                session_id="priv_bob_md5",
                user_id="Bob",
            )
        )
        clock[0] += 450.0
        grp_expired = await plg.agent_tool(
            action="start",
            profile="digest",
            message="过期消息",
            source_msg_id="m-grp-admin-old",
            chat_id="10001",
            platform="qq",
        )
        assert grp_expired["success"] is False
        assert "授权" in grp_expired["error"] or "核验" in grp_expired["error"]

        bob_expired = await plg.agent_tool(
            action="start",
            profile="digest",
            message="普通人过期消息",
            source_msg_id="m-nonadmin-old",
            chat_id="priv_bob_md5",
            platform="qq",
        )
        assert bob_expired["success"] is False
    finally:
        await plg.on_unload()
