"""Regression tests for the 6 live task execution & delivery bottlenecks."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from maisya.abilities import Profile
from maisya.config import Settings
from maisya.runner import HostWorkspaceRunner
from plugin import create_plugin


TEST_GROUP_ID = "100246809"
TEST_GROUP_SID = "d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f"
TEST_ADMIN_ID = "admin_1"
TEST_PRIV_SID = "a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4"

PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _dm_message(
    text: str,
    *,
    msg_id: str = "m-dm-1",
    user_id: str = TEST_ADMIN_ID,
    session_id: str = TEST_PRIV_SID,
    platform: str = "qq",
) -> dict:
    return {
        "message_id": msg_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": str(time.time()),
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": None,
            "additional_config": {"relayed_by_plugin": ""},
        },
    }


def _group_message(
    text: str,
    *,
    msg_id: str = "m-grp-1",
    user_id: str = "member_1",
    group_id: str = TEST_GROUP_ID,
    session_id: str = TEST_GROUP_SID,
    platform: str = "qq",
) -> dict:
    return {
        "message_id": msg_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": str(time.time()),
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": {"group_id": group_id},
            "additional_config": {"relayed_by_plugin": ""},
        },
    }


async def _boot_plugin(
    tmp_path: Path,
    *,
    enabled_chats: list[str] | None = None,
    permissions: list[str] | None = None,
    capability_fn=None,
):
    host_dir = tmp_path / "host"
    data_dir = host_dir / "data" / "plugins" / "chartyr.maisya"
    data_dir.mkdir(parents=True, exist_ok=True)
    plugin = create_plugin()
    host_cfg = {
        "plugin.permission": list(permissions or [f"qq:{TEST_ADMIN_ID}"]),
    }
    streams = [
        {
            "session_id": TEST_GROUP_SID,
            "platform": "qq",
            "group_id": TEST_GROUP_ID,
            "group_name": "测试交流群",
            "chat_type": "group",
            "is_group_session": True,
        },
        {
            "session_id": TEST_PRIV_SID,
            "platform": "qq",
            "user_id": TEST_ADMIN_ID,
            "nickname": "AdminOne",
            "chat_type": "private",
            "is_group_session": False,
        },
    ]

    async def _get_cfg(key: str, default=None):
        return host_cfg.get(key, default)

    async def _cap(name: str, **kwargs):
        if capability_fn is not None:
            res = capability_fn(name, kwargs)
            if hasattr(res, "__await__"):
                return await res
            if res is not None:
                return res
        if name == "chat.get_all_streams":
            return {"success": True, "streams": list(streams)}
        if name == "component.list_loaded_plugins":
            return {"success": True, "plugins": []}
        if name == "api.list":
            return {"success": True, "apis": []}
        if name == "maisaka.proactive.trigger":
            return {"success": True, "queued": True}
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "简报图卡放在这里啦。"}
        if name == "send.hybrid":
            return {"success": True, "sent": True, "message_id": "sent-hybrid-1"}
        if name == "render.html2png":
            return {
                "success": True,
                "result": {
                    "image_base64": PNG_1X1_B64,
                    "mime_type": "image/png",
                    "width": 1,
                    "height": 1,
                },
            }
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "任务已完成。", "tool_calls": []}
        return {"success": True}

    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=_get_cfg),
        paths=SimpleNamespace(data_dir=data_dir, runtime_dir=tmp_path / "runtime"),
        logger=logging.getLogger("test.maisya.live_bottlenecks"),
        call_capability=_cap,
    )
    cfg = Settings().model_dump()
    cfg["plugin"]["enabled"] = True
    cfg["enabled_chats"] = list(
        enabled_chats if enabled_chats is not None else [f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID]
    )
    plugin.set_plugin_config(cfg)
    await plugin.on_load()
    if plugin.pulse is not None and plugin.pulse.task is not None:
        plugin.pulse.task.cancel()
        await asyncio.gather(plugin.pulse.task, return_exceptions=True)
        plugin.pulse.task = None
    return plugin


async def _create_exec_profile(plugin, profile_id: str = "ops-runner", *, access: str = "everyone"):
    res = await plugin.manage_write(
        "profile_create",
        json.dumps(
            {
                "profile_id": profile_id,
                "name": "Ops Runner",
                "description": "Operations profile for test",
                "instructions": "Run operations carefully.",
                "toolsets": ["context", "workspace", "exec"],
                "session_mode": "ephemeral",
                "access": access,
                "permission_mode": "ask_risky",
            }
        ),
    )
    assert res["success"] is True, res


async def _enable_digest_profile(plugin):
    if not (plugin.profiles.path / "digest").is_dir():
        plugin.profiles.clone("default", "digest")
    cur = plugin.memory.read("profile", target="digest", document="profile.toml")
    if "enabled = false" in cur["content"]:
        plugin.memory.write(
            "profile",
            cur["content"].replace("enabled = false", "enabled = true"),
            target="digest",
            document="profile.toml",
            expected_version=cur["version"],
            allow_custom=True,
        )


@pytest.mark.asyncio
async def test_cross_chat_pending_approval_notifies_issuer_admin_not_target_group(
    tmp_path: Path,
) -> None:
    wakes: list[tuple[str, dict]] = []
    phase = {"step": 0}

    def _cap(name: str, kwargs: dict):
        if name == "maisaka.proactive.trigger":
            wakes.append((str(kwargs.get("stream_id") or ""), dict(kwargs)))
            return {"success": True, "queued": True}
        if name == "llm.generate_with_tools":
            phase["step"] += 1
            if phase["step"] == 1:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "tc-sed-1",
                            "type": "function",
                            "function": {
                                "name": "exec_command",
                                "arguments": json.dumps(
                                    {
                                        "command": "sed -i 's/552px/600px/' out/digest.html",
                                    }
                                ),
                            },
                        }
                    ],
                }
            return {"success": True, "response": "已继续完成。", "tool_calls": []}
        return None

    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
        capability_fn=_cap,
    )
    try:
        plugin.settings.sandbox.enabled = False
        plugin.runtime.sandbox.ready = False
        await _create_exec_profile(plugin, "ops-runner", access="everyone")
        await plugin.after_receive(message=_dm_message("立刻往群里发一个卡片", msg_id="m-dm-1"))

        started = await plugin.agent_tool(
            action="start",
            profile="ops-runner",
            message="生成并发群",
            source_msg_id="m-dm-1",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="target",
            chat_id=TEST_PRIV_SID,
            platform="qq",
            wait_seconds=0,
        )
        assert started["success"] is True, started
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)

        row = plugin.threads.get(started["thread_id"], TEST_GROUP_SID)
        assert row["state"] == "waiting"
        assert row["result"].get("pending_approval")

        await plugin.pulse.tick()

        assert len(wakes) == 1, f"expected 1 wake to issuing admin DM, got {wakes}"
        wake_sid, wake_kwargs = wakes[0]
        assert wake_sid == TEST_PRIV_SID
        assert TEST_GROUP_SID not in wake_sid
        assert f"qq:group:{TEST_GROUP_ID}" not in wake_sid
        assert "apr_" in wake_kwargs["intent"] or "审批" in wake_kwargs["intent"]

        rep = await plugin.before_replyer(
            session_id=TEST_PRIV_SID,
            request_type="maisaka.replyer",
            reply_tool_args={},
        )
        assert rep["action"] == "continue"
        extra = rep["modified_kwargs"]["extra_prompt"]
        assert "<maisya_result>" in extra
        assert "pending_approval" in extra
        assert "apr_" in extra
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_webui_thread_approve_and_console_send_schedule_return_delivery(
    tmp_path: Path,
) -> None:
    wakes: list[tuple[str, dict]] = []
    phase = {"mode": "approve_exec"}

    def _cap(name: str, kwargs: dict):
        if name == "maisaka.proactive.trigger":
            wakes.append((str(kwargs.get("stream_id") or ""), dict(kwargs)))
            return {"success": True, "queued": True}
        if name == "llm.generate_with_tools":
            if phase["mode"] == "approve_exec":
                phase["mode"] = "done"
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "tc-exec-1",
                            "type": "function",
                            "function": {
                                "name": "exec_command",
                                "arguments": json.dumps({"command": "echo run-once"}),
                            },
                        }
                    ],
                }
            return {"success": True, "response": f"已完成:{phase['mode']}", "tool_calls": []}
        return None

    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
        capability_fn=_cap,
    )
    now = [2000.0]
    plugin.bridge.clock = lambda: now[0]
    plugin.intents.clock = lambda: now[0]

    try:
        plugin.settings.sandbox.enabled = False
        plugin.runtime.sandbox.ready = False
        await _create_exec_profile(plugin, "ops-runner", access="everyone")
        await plugin.after_receive(message=_group_message("跑一下任务", msg_id="m-grp-apr-1", user_id=TEST_ADMIN_ID))

        started = await plugin.agent_tool(
            action="start",
            profile="ops-runner",
            message="跑一下命令",
            source_msg_id="m-grp-apr-1",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            wait_seconds=20,
        )
        assert started["success"] is True, started
        assert started["state"] == "waiting", started
        tid = started["thread_id"]

        # Simulate WebUI thread_approve without chat approve
        phase["mode"] = "approved_resumed"
        approved = await plugin.manage_write(
            "thread_approve", json.dumps({"thread_id": tid, "decision": "once"})
        )
        assert approved["success"] is True, approved
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)

        now[0] += 2.0
        await plugin.pulse.tick()
        assert any(
            sid == TEST_GROUP_SID and "已完成:approved_resumed" in kw.get("intent", "")
            for sid, kw in wakes
        ), f"thread_approve did not schedule return wake: {wakes}"

        # Simulate console_send continuation on the same group thread
        wakes.clear()
        phase["mode"] = "console_followup"
        con_res = await plugin.console_send(thread_id=tid, message="补查一次并告知群里")
        assert con_res["success"] is True, con_res
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)

        now[0] += 2.0
        await plugin.pulse.tick()
        assert any(
            sid == TEST_GROUP_SID and "已完成:console_followup" in kw.get("intent", "")
            for sid, kw in wakes
        ), f"console_send did not schedule return wake: {wakes}"

        # Simulate admin_direct waiting thread approved via WebUI routes back to priv_admin_sid
        wakes.clear()
        phase["mode"] = "approve_exec"
        await plugin.after_receive(message=_dm_message("私聊试跑命令", msg_id="m-dm-apr-2"))
        dm_started = await plugin.agent_tool(
            action="start",
            profile="ops-runner",
            message="私聊试跑审批",
            source_msg_id="m-dm-apr-2",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="current",
            chat_id=TEST_PRIV_SID,
            platform="qq",
            wait_seconds=20,
        )
        assert dm_started["success"] is True, dm_started
        assert dm_started["state"] == "waiting", dm_started
        dm_tid = dm_started["thread_id"]

        phase["mode"] = "admin_direct_approved"
        dm_approved = await plugin.manage_write(
            "thread_approve", json.dumps({"thread_id": dm_tid, "decision": "once"})
        )
        assert dm_approved["success"] is True, dm_approved
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)

        now[0] += 2.0
        await plugin.pulse.tick()
        assert any(
            sid == TEST_PRIV_SID and "已完成:admin_direct_approved" in kw.get("intent", "")
            for sid, kw in wakes
        ), f"admin_direct WebUI approve did not route back to priv_admin_sid: {wakes}"
        assert all(sid != TEST_GROUP_SID for sid, _ in wakes), wakes
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_staged_attachment_visible_in_offer_and_not_cannibalized_by_unrelated_reply(
    tmp_path: Path,
) -> None:
    sent_hybrid: list[dict] = []

    def _cap(name: str, kwargs: dict):
        if name == "send.hybrid":
            sent_hybrid.append(dict(kwargs))
            return {"success": True, "sent": True, "message_id": "msg-shared-png"}
        return None

    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
        capability_fn=_cap,
    )
    try:
        await _enable_digest_profile(plugin)
        await plugin.after_receive(
            message=_group_message("群里随便聊聊别的", msg_id="m-grp-chat-1")
        )
        row = plugin.threads.create(
            plugin.profiles.get("digest"),
            "正式发群简报",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="",
            workspace="digest",
        )
        tid = row["thread_id"]
        work = await plugin.runtime._work(row)
        work.write_bytes("out/digest.png", base64.b64decode(PNG_1X1_B64))
        work.commit()
        atts = plugin.runtime.files.attach(row, work, ["out/digest.png"])
        rev = plugin.threads.enqueue(tid, "正式发群简报", TEST_GROUP_SID)
        assert (
            plugin.threads.finish(
                tid,
                TEST_GROUP_SID,
                {"text": "修正版简报图卡已发到群里。", "attachments": atts},
                [],
                rev,
            )
            is True
        )
        finished_row = plugin.threads.get(tid, TEST_GROUP_SID)

        # Simulate Pulse._dispatch staging finished cross-chat thread into target group
        plugin.bridge.stage(finished_row, TEST_GROUP_SID, archived=False)

        # 1. Planner offer includes staged manual attachment row so compaction cannot erase it
        offered = await plugin.bridge.offer(TEST_GROUP_SID)
        assert any(item["thread_id"] == tid for item in offered), offered

        planner_res = await plugin.before_planner(
            session_id=TEST_GROUP_SID,
            items=[],
            tool_definitions=[
                {"type": "function", "function": {"name": "reply"}},
                {"type": "function", "function": {"name": "maisya_agent"}},
            ],
        )
        injected_text = planner_res["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "<maisya_returns>" in injected_text
        assert atts[0]["handle"] in injected_text
        assert "尚未发送" in injected_text or "share" in injected_text

        # 2. Unrelated plain-text reply MUST NOT swallow the staged attachment delivery
        facts, receipt = await plugin.bridge.reply_material(TEST_GROUP_SID, 1, {})
        assert facts == ""
        assert receipt == ""
        assert plugin.bridge.delivery(tid, rev, TEST_GROUP_SID) is None

        # 3. Real share succeeds and removes it from offer
        shared = await plugin.agent_tool(
            action="share",
            thread_id=tid,
            message="本期简报图卡来了",
            chat_id=TEST_GROUP_SID,
            platform="qq",
        )
        assert shared["success"] is True, shared
        assert shared.get("route") == "share" and shared.get("sent") is True, shared
        assert len(sent_hybrid) == 1
        assert plugin.bridge.delivery(tid, rev, TEST_GROUP_SID)["route"] == "share"
        assert plugin.bridge.delivery(tid, rev, TEST_GROUP_SID)["status"] == "sent"
        after_offered = await plugin.bridge.offer(TEST_GROUP_SID)
        assert not any(item["thread_id"] == tid for item in after_offered), after_offered
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_start_omitted_profile_adopts_workspace_binding_or_matching_profile_and_clear_mismatch_error(
    tmp_path: Path,
) -> None:
    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
    )
    try:
        await _enable_digest_profile(plugin)

        # Sub-case B: brand-new workspace matching enabled profile "digest" + omitted profile
        started_match = await plugin.agent_tool(
            action="start",
            profile="",
            workspace="digest",
            message="先建一个简报工作区",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            wait_seconds=10,
        )
        assert started_match["success"] is True, started_match
        assert started_match["profile"] == "digest"

        # Bind another workspace explicitly to digest (fresh archives prior primary thread so digest_mix binds cleanly)
        started_mix = await plugin.agent_tool(
            action="start",
            profile="digest",
            fresh=True,
            workspace="digest_mix",
            message="按混合口味建简报工作区",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            wait_seconds=10,
        )
        assert started_mix["success"] is True, started_mix
        assert started_mix["profile"] == "digest"

        # Sub-case A: omitted profile on existing workspace "digest_mix" adopts bound "digest"
        continued = await plugin.agent_tool(
            action="start",
            profile="",
            workspace="digest_mix",
            message="漏传 profile 续跑工作区",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            wait_seconds=10,
        )
        assert continued["success"] is True, continued
        assert continued["thread_id"] == started_mix["thread_id"]
        assert continued["profile"] == "digest"

        # Sub-case C: explicit wrong profile yields clear error naming bound profile
        mismatch = await plugin.agent_tool(
            action="start",
            profile="default",
            workspace="digest_mix",
            message="错用 default 跑 digest_mix",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            wait_seconds=10,
        )
        assert mismatch["success"] is False
        assert "该工作区已绑定另一个角色（digest）" in mismatch["error"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_result_attach_overwrites_same_path_and_keeps_distinct_paths(
    tmp_path: Path,
) -> None:
    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
    )
    try:
        row = plugin.threads.create(
            plugin.profiles.get("default"),
            "重渲覆盖测试",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="",
            workspace="attach_dedup",
        )
        tid = row["thread_id"]
        work = await plugin.runtime._work(row)
        plugin.runtime.begin_turn(tid)

        v1_bytes = b"png-v1-short"
        v2_bytes = b"png-v2-longer-content-replaced"
        extra_bytes = b"extra-notes"

        work.write_bytes("out/digest.png", v1_bytes)
        work.commit()
        r1 = await plugin.runtime.call("result_attach", {"paths": ["out/digest.png"]}, row)

        work.write_bytes("out/extra.txt", extra_bytes)
        work.commit()
        await plugin.runtime.call("result_attach", {"paths": ["out/extra.txt"]}, row)

        work.write_bytes("out/digest.png", v2_bytes)
        work.commit()
        r3 = await plugin.runtime.call("result_attach", {"paths": ["out/digest.png"]}, row)

        final_result = plugin.runtime.result(tid, {"text": "done"})
        atts = final_result.get("attachments") or []
        assert len(atts) == 2, atts
        by_name = {item["name"]: item for item in atts}
        assert set(by_name) == {"digest.png", "extra.txt"}
        assert by_name["digest.png"]["handle"] == r3["attachments"][0]["handle"]
        assert by_name["digest.png"]["handle"] != r1["attachments"][0]["handle"]
        assert by_name["digest.png"]["bytes"] == len(v2_bytes)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_get_avatar_handles_top_level_status_available_and_unsupported_without_warnings(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    avatar_calls: list[str] = []
    now_mono = [1000.0]
    monkeypatch.setattr("plugin.time.monotonic", lambda: now_mono[0])
    data_uri = f"data:image/png;base64,{PNG_1X1_B64}"

    def _cap(name: str, kwargs: dict):
        if name == "chat.get_all_streams":
            return {
                "success": True,
                "streams": [
                    {
                        "session_id": "grp_avail",
                        "platform": "qq",
                        "group_id": "100246809",
                        "group_name": "AvailGroup",
                        "chat_type": "group",
                        "is_group_session": True,
                    },
                    {
                        "session_id": "grp_unsup",
                        "platform": "qq",
                        "group_id": "100246810",
                        "group_name": "UnsupGroup",
                        "chat_type": "group",
                        "is_group_session": True,
                    },
                ],
            }
        if name == "chat.get_avatar":
            tid = str(kwargs.get("target_id") or "")
            avatar_calls.append(tid)
            if tid == "100246809":
                return {
                    "status": "available",
                    "url": data_uri,
                    "expires_at": time.time() + 600,
                }
            return {
                "status": "unsupported",
                "url": None,
                "expires_at": None,
            }
        return None

    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=["grp_avail", "grp_unsup"],
        capability_fn=_cap,
    )
    try:
        with caplog.at_level(logging.WARNING, logger="test.maisya.live_bottlenecks"):
            snap1 = await plugin.console_query()
        warn_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert warn_msgs == [], f"unexpected warnings: {warn_msgs}"
        assert snap1["chat_sources"]["grp_avail"]["avatar"] == data_uri
        assert snap1["chat_sources"]["grp_avail"]["avatar_data"] == data_uri
        assert snap1["chat_sources"]["grp_unsup"]["avatar"] == ""
        assert len(avatar_calls) == 2

        now_mono[0] += 30.0
        with caplog.at_level(logging.WARNING, logger="test.maisya.live_bottlenecks"):
            snap2 = await plugin.console_query()
        assert snap2["chat_sources"]["grp_avail"]["avatar"] == data_uri
        assert len(avatar_calls) == 2, f"unexpected cache miss after 30s: {avatar_calls}"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_admin_bootstrap_clarifies_share_unshared_returned_and_self_msg_auth_error(
    tmp_path: Path,
) -> None:
    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
    )
    try:
        await _enable_digest_profile(plugin)

        # 1) Create a recalled historical group thread with attachment (staged archived=True on recall)
        hist_row = plugin.threads.create(
            plugin.profiles.get("digest"),
            "历史简报",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="",
            workspace="digest",
        )
        hist_work = await plugin.runtime._work(hist_row)
        hist_work.write_bytes("out/old.png", base64.b64decode(PNG_1X1_B64))
        hist_work.commit()
        hist_atts = plugin.runtime.files.attach(hist_row, hist_work, ["out/old.png"])
        hist_rev = plugin.threads.enqueue(hist_row["thread_id"], "历史简报", TEST_GROUP_SID)
        assert plugin.threads.finish(
            hist_row["thread_id"],
            TEST_GROUP_SID,
            {"text": "历史结果", "attachments": hist_atts},
            [],
            hist_rev,
        )

        # 2) Create a separate preview thread whose unshared attachment is staged via admin_direct (archived=False)
        prev_row = plugin.threads.create(
            plugin.profiles.get("default"),
            "私聊预览重做",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="",
            workspace="digest_preview",
        )
        prev_work = await plugin.runtime._work(prev_row)
        prev_work.write_bytes("out/digest.png", base64.b64decode(PNG_1X1_B64))
        prev_work.commit()
        prev_atts = plugin.runtime.files.attach(prev_row, prev_work, ["out/digest.png"])
        prev_rev = plugin.threads.enqueue(prev_row["thread_id"], "重做 600px", TEST_GROUP_SID)
        assert plugin.threads.finish(
            prev_row["thread_id"],
            TEST_GROUP_SID,
            {"text": "修正版简报图卡已发到群里。", "attachments": prev_atts},
            [],
            prev_rev,
        )
        finished_prev = plugin.threads.get(prev_row["thread_id"], TEST_GROUP_SID)

        await plugin.after_receive(message=_dm_message("看看进度", msg_id="m-dm-boot-1"))
        await plugin.after_receive(
            message=_dm_message(
                "好的喵",
                msg_id="bot_self_reply_1",
                user_id="bot_self",
            )
        )

        # Recalling target group threads MUST NOT pollute <maisya_returned> via archived=True staging
        recalled = await plugin.recall_tool(
            kind="threads",
            query="",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-boot-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert recalled["success"] is True, recalled

        plugin.bridge.stage(finished_prev, TEST_PRIV_SID, archived=False, admin_direct=True)

        # Re-record latest real admin message for planner turn
        await plugin.after_receive(message=_dm_message("能发吗", msg_id="m-dm-boot-2"))
        boot_res = await plugin.before_planner(
            session_id=TEST_PRIV_SID,
            items=[],
            tool_definitions=[
                {"type": "function", "function": {"name": "reply"}},
                {"type": "function", "function": {"name": "maisya_agent"}},
                {"type": "function", "function": {"name": "maisya_recall"}},
                {"type": "function", "function": {"name": "maisya_later"}},
                {"type": "function", "function": {"name": "maisya_shape"}},
            ],
        )
        boot_text = boot_res["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "<maisya_admin_bootstrap>" in boot_text
        assert "action='share'" in boot_text and "result_attach" in boot_text
        assert prev_row["thread_id"] in boot_text
        assert hist_row["thread_id"] not in boot_text
        assert "未真正" in boot_text or "尚未发出" in boot_text or "待交付" in boot_text

        # Passing bot's own reply msg_id as source_msg_id gives actionable error hint
        bad_auth = await plugin.agent_tool(
            action="send",
            thread_id=prev_row["thread_id"],
            message="重发",
            source_msg_id="bot_self_reply_1",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert bad_auth["success"] is False
        assert "跨会话调度需要真实管理员消息授权" in bad_auth["error"]
        assert "bot_self_reply_1" in bad_auth["error"]
        assert "reply" in bad_auth["error"] or "管理员" in bad_auth["error"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_cross_chat_auto_non_attachment_and_error_returns_notify_admin_dm(
    tmp_path: Path,
) -> None:
    wakes: list[tuple[str, dict]] = []
    mode = {"val": "text_only"}

    def _cap(name: str, kwargs: dict):
        if name == "maisaka.proactive.trigger":
            wakes.append((str(kwargs.get("stream_id") or ""), dict(kwargs)))
            return {"success": True, "queued": True}
        if name == "llm.generate_with_tools":
            if mode["val"] == "error":
                return {"success": False, "error": "upstream timeout"}
            return {
                "success": True,
                "response": f"核查结论({mode['val']})：今天只有早上发过一次。",
                "tool_calls": [],
            }
        return None

    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
        capability_fn=_cap,
    )
    try:
        pro_tools = [
            {"type": "function", "function": {"name": "reply"}},
            {"type": "function", "function": {"name": "maisya_agent"}},
            {"type": "function", "function": {"name": "maisya_shape"}},
            {"type": "function", "function": {"name": "maisya_later"}},
        ]
        await plugin.after_receive(
            message=_dm_message("去群里查下今天发了没", msg_id="m-dm-auto-1")
        )
        await plugin.before_planner(
            session_id=TEST_PRIV_SID,
            items=[],
            tool_definitions=pro_tools,
        )
        # Subcase A: cross-chat deliver_to='auto' with plain-text result wakes issuing admin DM
        res_a = await plugin.agent_tool(
            action="start",
            profile="scout",
            message="查群今天消息记录",
            source_msg_id="m-dm-auto-1",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            chat_id=TEST_PRIV_SID,
            platform="qq",
            wait_seconds=0,
        )
        assert res_a["success"] is True, res_a
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        intent_a = plugin.intents.get(res_a["delivery_intent_id"])
        assert intent_a is not None
        await plugin.pulse.tick()

        assert len(wakes) == 1, wakes
        wake_sid_a, wake_kw_a = wakes[0]
        assert wake_sid_a == TEST_PRIV_SID
        assert "<maisya_thread_result>" in wake_kw_a["intent"]
        assert "核查结论(text_only)" in wake_kw_a["intent"]
        assert plugin.intents.get(res_a["delivery_intent_id"])["wake_status"] == "return_routed_source"

        # Proactive wakeup turn in unenabled admin DM retains maisya_agent without duplicate return prompt
        pro_res = await plugin.before_planner(
            session_id=TEST_PRIV_SID,
            items=[
                {
                    "item_type": "UserMessageItem",
                    "parts": [
                        {
                            "type": "text",
                            "text": f"<plugin_proactive_task>{wake_kw_a['intent']}</plugin_proactive_task>",
                        }
                    ],
                }
            ],
            tool_definitions=pro_tools,
        )
        kept_names = [t["function"]["name"] for t in pro_res["modified_kwargs"]["tool_definitions"]]
        assert "maisya_agent" in kept_names
        assert "items" not in pro_res["modified_kwargs"], pro_res["modified_kwargs"]

        # Overlapping pending source on a wake turn keeps bootstrap without duplicate <maisya_returned>
        await plugin.after_receive(
            message=_dm_message("查到了吗", msg_id="m-dm-auto-2")
        )
        boot_wake_res = await plugin.before_planner(
            session_id=TEST_PRIV_SID,
            items=[
                {
                    "item_type": "UserMessageItem",
                    "parts": [
                        {
                            "type": "text",
                            "text": f"<plugin_proactive_task>{wake_kw_a['intent']}</plugin_proactive_task>",
                        }
                    ],
                }
            ],
            tool_definitions=pro_tools,
        )
        boot_wake_text = boot_wake_res["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "<maisya_admin_bootstrap>" in boot_wake_text
        assert "<maisya_returned>" not in boot_wake_text

        # Subcase B: explicit deliver_to='target' wakes target group even without attachments
        wakes.clear()
        mode["val"] = "explicit_target"
        res_b = await plugin.agent_tool(
            action="start",
            profile="scout",
            message="在群里说一声",
            source_msg_id="m-dm-auto-1",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="target",
            chat_id=TEST_PRIV_SID,
            platform="qq",
            wait_seconds=0,
        )
        assert res_b["success"] is True, res_b
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        await plugin.pulse.tick()
        assert len(wakes) == 1 and wakes[0][0] == TEST_GROUP_SID, wakes

        # Subcase C: cross-chat worker failure notifies issuing admin DM instead of silent drop
        wakes.clear()
        mode["val"] = "error"
        res_c = await plugin.agent_tool(
            action="start",
            profile="scout",
            message="触发上游报错",
            source_msg_id="m-dm-auto-1",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="target",
            chat_id=TEST_PRIV_SID,
            platform="qq",
            wait_seconds=0,
        )
        assert res_c["success"] is True, res_c
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        await plugin.pulse.tick()
        finished_c = plugin.intents.get(res_c["delivery_intent_id"])
        assert finished_c["status"] == "failed"
        assert finished_c["wake_status"] == "thread_error"
        assert len(wakes) == 1 and wakes[0][0] == TEST_PRIV_SID, wakes
        assert wakes[0][1]["reason"] == "maisya:workflow_error"
        assert "<maisya_thread_error>" in wakes[0][1]["intent"]
        thread_c = plugin.threads.get(res_c["thread_id"], TEST_GROUP_SID)
        assert thread_c["result"]["error"] and thread_c["result"]["error"] in wakes[0][1]["intent"]
        assert "本轮 Agent 活动中断" in wakes[0][1]["intent"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_maisya_recall_reports_real_delivery_status_and_unshared_prior_attachments(
    tmp_path: Path,
) -> None:
    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
    )
    try:
        await _enable_digest_profile(plugin)
        row = plugin.threads.create(
            plugin.profiles.get("default"),
            "群简报任务",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="",
            workspace="digest",
        )
        tid = row["thread_id"]
        work = await plugin.runtime._work(row)
        work.write_bytes("out/digest.png", base64.b64decode(PNG_1X1_B64))
        work.commit()
        rev1 = plugin.threads.enqueue(tid, "生成600px图卡", TEST_GROUP_SID)
        atts_v1 = plugin.runtime.files.attach(
            plugin.threads.get(tid, TEST_GROUP_SID), work, ["out/digest.png"]
        )
        assert plugin.threads.finish(
            tid,
            TEST_GROUP_SID,
            {"text": "修正版简报图卡已发到群里。", "attachments": atts_v1},
            [],
            rev1,
        )
        # Simulate stale reply receipt with empty message_id
        plugin.bridge.claim_delivery(
            plugin.threads.get(tid, TEST_GROUP_SID), "reply"
        )

        await plugin.after_receive(message=_dm_message("查下这个线程", msg_id="m-dm-rec-1"))
        rec1 = await plugin.recall_tool(
            kind="threads",
            query="",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-rec-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert rec1["success"] is True, rec1
        entry1 = next(t for t in rec1["threads"] if t["thread_id"] == tid)
        assert entry1["delivery"]["shared"] is False
        assert entry1["delivery"]["records"][0]["route"] == "reply"
        assert "尚未通过 share" in entry1["delivery"]["note"]

        # Advance thread to rev2 with pure text (e.g. thread reported it lacked send permission)
        rev2 = plugin.threads.enqueue(tid, "查看为何没发", TEST_GROUP_SID)
        assert plugin.threads.finish(
            tid,
            TEST_GROUP_SID,
            {"text": "我没有直接发群权限，result_attach 仅挂载。", "attachments": []},
            [],
            rev2,
        )
        rec2 = await plugin.recall_tool(
            kind="threads",
            query="",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-rec-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        entry2 = next(t for t in rec2["threads"] if t["thread_id"] == tid)
        assert entry2["delivery"]["shared"] is False
        assert entry2["unshared_attachments"][0]["handle"] == atts_v1[0]["handle"]
        assert entry2["unshared_attachments"][0]["revision"] == rev1
        assert entry2["unshared_attachments"][0]["name"] == "digest.png"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_share_upgrades_stale_reply_receipt_and_allows_same_thread_prior_revision_attachment(
    tmp_path: Path,
) -> None:
    sent_hybrid: list[dict] = []
    voice_refs: list[str] = []

    def _cap(name: str, kwargs: dict):
        if name == "send.hybrid":
            sent_hybrid.append(dict(kwargs))
            return {"success": True, "sent": True, "message_id": f"msg-{len(sent_hybrid)}"}
        if name == "maisaka.reply.generate":
            voice_refs.append(str(kwargs.get("reference") or ""))
            return {"success": True, "text": "今天的简报图卡发过来啦。"}
        return None

    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
        capability_fn=_cap,
    )
    try:
        # Thread 1: rev 1 has att1 + stale reply receipt in result_deliveries
        t1 = plugin.threads.create(
            plugin.profiles.get("default"),
            "重发带旧reply回执的图卡",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="",
            workspace="ws_t1",
        )
        w1 = await plugin.runtime._work(t1)
        w1.write_bytes("out/digest.png", base64.b64decode(PNG_1X1_B64))
        w1.commit()
        rev1_t1 = plugin.threads.enqueue(t1["thread_id"], "出图", TEST_GROUP_SID)
        atts1 = plugin.runtime.files.attach(
            plugin.threads.get(t1["thread_id"], TEST_GROUP_SID), w1, ["out/digest.png"]
        )
        assert plugin.threads.finish(
            t1["thread_id"],
            TEST_GROUP_SID,
            {"text": "图卡做好了。", "attachments": atts1},
            [],
            rev1_t1,
        )
        plugin.bridge.claim_delivery(plugin.threads.get(t1["thread_id"], TEST_GROUP_SID), "reply")

        await plugin.after_receive(message=_dm_message("补发到群里", msg_id="m-dm-share-1"))
        shared1 = await plugin.agent_tool(
            action="share",
            thread_id=t1["thread_id"],
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="target",
            source_msg_id="m-dm-share-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert shared1["success"] is True and shared1["sent"] is True, shared1
        assert len(sent_hybrid) == 1 and sent_hybrid[0]["stream_id"] == TEST_GROUP_SID
        assert any(seg.get("type") == "image" for seg in sent_hybrid[0]["segments"])
        deliv1 = plugin.bridge.delivery(t1["thread_id"], rev1_t1, TEST_GROUP_SID)
        assert deliv1["route"] == "share" and deliv1["status"] == "sent"

        # Second share of same revision is idempotent
        dup1 = await plugin.agent_tool(
            action="share",
            thread_id=t1["thread_id"],
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="target",
            source_msg_id="m-dm-share-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert dup1.get("already_handled") is True
        assert len(sent_hybrid) == 1

        # Thread 2: rev 1 made att2, rev 2 finished plain text; share explicitly passes rev 1's att2
        t2 = plugin.threads.create(
            plugin.profiles.get("default"),
            "续聊后补发上一版附件",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="",
            workspace="ws_t2",
        )
        w2 = await plugin.runtime._work(t2)
        w2.write_bytes("out/digest2.png", base64.b64decode(PNG_1X1_B64))
        w2.commit()
        rev1_t2 = plugin.threads.enqueue(t2["thread_id"], "出图2", TEST_GROUP_SID)
        atts2 = plugin.runtime.files.attach(
            plugin.threads.get(t2["thread_id"], TEST_GROUP_SID), w2, ["out/digest2.png"]
        )
        assert plugin.threads.finish(
            t2["thread_id"],
            TEST_GROUP_SID,
            {"text": "修订1卡片摘要：独立游戏5条。", "attachments": atts2},
            [],
            rev1_t2,
        )
        rev2_t2 = plugin.threads.enqueue(t2["thread_id"], "查为什么没发", TEST_GROUP_SID)
        assert plugin.threads.finish(
            t2["thread_id"],
            TEST_GROUP_SID,
            {"text": "抱歉我发不出去", "attachments": []},
            [],
            rev2_t2,
        )

        shared2 = await plugin.agent_tool(
            action="share",
            thread_id=t2["thread_id"],
            attachment_handles=[atts2[0]["handle"]],
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="target",
            message="补发今天的简报",
            source_msg_id="m-dm-share-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert shared2["success"] is True and shared2["sent"] is True, shared2
        assert len(sent_hybrid) == 2 and sent_hybrid[1]["stream_id"] == TEST_GROUP_SID
        assert "独立游戏5条" in voice_refs[-1]
        assert "抱歉我发不出去" not in voice_refs[-1]

        dup2 = await plugin.agent_tool(
            action="share",
            thread_id=t2["thread_id"],
            attachment_handles=[atts2[0]["handle"]],
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="target",
            message="补发今天的简报",
            source_msg_id="m-dm-share-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert dup2.get("already_handled") is True
        assert len(sent_hybrid) == 2

        rec_t2 = await plugin.recall_tool(
            kind="threads",
            query="",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-share-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert rec_t2["success"] is True, rec_t2
        entry_t2 = next(t for t in rec_t2["threads"] if t["thread_id"] == t2["thread_id"])
        assert all(
            item["handle"] != atts2[0]["handle"]
            for item in entry_t2.get("unshared_attachments") or []
        )

        # Foreign thread attachment handle MUST still be rejected
        foreign = await plugin.agent_tool(
            action="share",
            thread_id=t2["thread_id"],
            attachment_handles=[atts1[0]["handle"]],
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            deliver_to="target",
            source_msg_id="m-dm-share-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert foreign["success"] is False
        assert "附件不属于当前版本的线程结果" in foreign["error"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_scheduled_workflow_reclaims_hijacked_workspace_preserving_digest_files(
    tmp_path: Path,
) -> None:
    wakes: list[tuple[str, dict]] = []

    def _cap(name: str, kwargs: dict):
        if name == "maisaka.proactive.trigger":
            wakes.append((str(kwargs.get("stream_id") or ""), dict(kwargs)))
            return {"success": True, "queued": True}
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "早间简报已生成。", "tool_calls": []}
        return None

    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
        capability_fn=_cap,
    )
    try:
        await _enable_digest_profile(plugin)
        # 1. Seed original archived digest workspace holding rss_sources.json & digest_history.json
        orig_row = plugin.threads.create(
            plugin.profiles.get("digest"),
            "初始简报工作区",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="",
            workspace="digest",
        )
        with plugin.runtime.files.activity(orig_row) as orig_work:
            orig_work.write(
                "project/rss_sources.json",
                json.dumps(
                    {
                        "feeds": [{"url": "https://example.com/rss.xml", "title": "Indie Feed"}],
                        "topics": ["独立游戏"],
                    },
                    ensure_ascii=False,
                ),
            )
            orig_work.write(
                "project/digest_history.json",
                json.dumps({"items": [{"url": "https://example.com/1"}]}, ensure_ascii=False),
            )
            orig_work.commit()
        rev_orig = plugin.threads.enqueue(orig_row["thread_id"], "初版简报", TEST_GROUP_SID)
        assert plugin.threads.finish(
            orig_row["thread_id"],
            TEST_GROUP_SID,
            {"text": "初版完成", "attachments": []},
            [],
            rev_orig,
        )
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='archived' WHERE thread_id=?",
                (orig_row["thread_id"],),
            )

        # 2. Hijack workspace name="digest" with an ad-hoc default thread using fresh=True
        hijack_row = plugin.threads.create(
            plugin.profiles.get("default"),
            "临时重跑简报",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id=TEST_ADMIN_ID,
            workspace="digest",
            fresh_workspace=True,
        )
        assert (
            plugin.threads.workspace_profile_for(
                "chat", TEST_GROUP_SID, "digest", platform="qq"
            )
            == "default"
        )
        rev_h = plugin.threads.enqueue(hijack_row["thread_id"], "出图", TEST_GROUP_SID)
        with plugin.runtime.files.activity(hijack_row) as h_work:
            h_work.write_bytes("out/digest.png", base64.b64decode(PNG_1X1_B64))
            h_work.commit()
            atts_h = plugin.runtime.files.attach(
                plugin.threads.get(hijack_row["thread_id"], TEST_GROUP_SID),
                h_work,
                ["out/digest.png"],
            )
        assert plugin.threads.finish(
            hijack_row["thread_id"],
            TEST_GROUP_SID,
            {"text": "临时出图", "attachments": atts_h},
            [],
            rev_h,
        )

        # 3. Trigger due recurring digest workflow -> must reclaim digest workspace (no deadlock)
        clock = [plugin.intents.clock()]
        plugin.intents.clock = lambda: clock[0]
        sched = plugin.intents.schedule(
            kind="return",
            source="workflow",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] - 2,
            reason="早间独立游戏资讯简报图卡",
            payload={
                "platform": "qq",
                "every_seconds": 86400,
                "daily_at": "08:30",
                "tz_offset_minutes": 480,
                "profile": "digest",
                "workspace": "digest",
                "prompt": "早间简报",
                "scheduled_admin": True,
            },
            bypass_limits=True,
        )
        await plugin.pulse.tick()
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        clock[0] += 5.0
        await plugin.pulse.tick()

        done_intent = plugin.intents.get(sched["intent_id"])
        assert done_intent["status"] == "done", done_intent
        assert done_intent["failure_count"] == 0
        assert done_intent["last_error"] == ""
        assert len(wakes) >= 1 and wakes[0][0] == TEST_GROUP_SID
        pending_next = [
            it
            for it in plugin.intents.list(f"chat:{TEST_GROUP_SID}")
            if it["status"] == "pending" and it["payload"].get("daily_at") == "08:30"
        ]
        assert len(pending_next) == 1

        new_digest_row = plugin.threads.get(done_intent["thread_id"], TEST_GROUP_SID)
        assert new_digest_row["profile"] == "digest"
        with plugin.runtime.files.activity(new_digest_row) as restored_work:
            assert "https://example.com/rss.xml" in restored_work.read("project/rss_sources.json")["text"]
            assert "https://example.com/1" in restored_work.read("project/digest_history.json")["text"]
        assert plugin.runtime.files.resolve(atts_h[0]["handle"], TEST_GROUP_SID) is not None

        # 4. Also verify maisya_shape(digest_sources) recovers even when hijacked again
        hijack2 = plugin.threads.create(
            plugin.profiles.get("default"),
            "再次错占工作区",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id=TEST_ADMIN_ID,
            workspace="digest",
            fresh_workspace=True,
        )
        rev_h2 = plugin.threads.enqueue(hijack2["thread_id"], "完成", TEST_GROUP_SID)
        assert plugin.threads.finish(hijack2["thread_id"], TEST_GROUP_SID, {"text": "ok", "attachments": []}, [], rev_h2)

        await plugin.after_receive(message=_dm_message("查简报源", msg_id="m-dm-ds-1"))
        ds_res = await plugin.shape_tool(
            action="digest_sources",
            op="read",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-ds-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert ds_res["success"] is True, ds_res
        assert any(f.get("url") == "https://example.com/rss.xml" for f in ds_res.get("feeds") or []), ds_res
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_pulse_intents_exception_logs_and_records_backoff_to_error_paused(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
    )
    try:
        clock = [1795000000.0]
        plugin.intents.clock = lambda: clock[0]
        sched = plugin.intents.schedule(
            kind="return",
            source="workflow",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] - 5,
            reason="巡检定时",
            payload={
                "platform": "qq",
                "profile": "scout",
                "prompt": "巡检",
                "scheduled_admin": True,
            },
            bypass_limits=True,
        )

        orig_start = plugin.engine.start

        async def broken_start(*args, **kwargs):
            raise ValueError("simulated broken config")

        plugin.engine.start = broken_start

        # 1st failure -> warning logged + retry_backoff +60s
        with caplog.at_level(logging.WARNING, logger="maisya.pulse"):
            await plugin.pulse.tick()
        assert "意图执行异常" in caplog.text
        assert "simulated broken config" in caplog.text

        r1 = plugin.intents.get(sched["intent_id"])
        assert r1["status"] == "pending"
        assert r1["wake_status"] == "retry_backoff"
        assert r1["failure_count"] == 1
        assert "simulated broken config" in r1["last_error"]
        assert r1["due_at"] == pytest.approx(clock[0] + 60.0)

        # 2nd failure -> failure_count=2, backed off further
        clock[0] = r1["due_at"] + 1.0
        await plugin.pulse.tick()
        r2 = plugin.intents.get(sched["intent_id"])
        assert r2["status"] == "pending"
        assert r2["failure_count"] == 2
        assert r2["due_at"] > clock[0] + 60.0

        # 3rd failure -> hits default max failures (3) and transitions to error_paused
        clock[0] = r2["due_at"] + 1.0
        await plugin.pulse.tick()
        r3 = plugin.intents.get(sched["intent_id"])
        assert r3["status"] == "error_paused"
        assert r3["wake_status"] == "error_paused"
        assert r3["failure_count"] == 3

        # Admin list all exposes error_paused intents, and run_now resumes & executes cleanly
        await plugin.after_receive(message=_dm_message("查所有定时并重试", msg_id="m-dm-err-1"))
        listed = await plugin.later_tool(
            action="list",
            target_chat="all",
            source_msg_id="m-dm-err-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert listed["success"] is True, listed
        assert any(
            it["intent_id"] == sched["intent_id"] and it["status"] == "error_paused"
            for it in listed["intents"]
        ), listed

        plugin.engine.start = orig_start
        resumed = await plugin.later_tool(
            action="run_now",
            intent_id=sched["intent_id"],
            source_msg_id="m-dm-err-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert resumed["success"] is True, resumed
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        clock[0] += 5.0
        await plugin.pulse.tick()
        assert plugin.intents.get(sched["intent_id"])["status"] == "done"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_integer_source_msg_id_and_later_update_hint_work_in_unenabled_admin_dm(
    tmp_path: Path,
) -> None:
    plugin = await _boot_plugin(
        tmp_path,
        enabled_chats=[f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
    )
    try:
        await plugin.after_receive(
            message=_dm_message("改一下早上定时", msg_id="-2139870001")
        )
        sched = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            daily_at=["08:30"],
            profile="scout",
            workspace="scout_daily",
            prompt="旧提示词",
            reason="每日巡检",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id=-2139870001,
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert sched["success"] is True, sched

        upd = await plugin.later_tool(
            action="update",
            intent_id=sched["intent_id"],
            source_msg_id=-2139870001,
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert upd["success"] is False
        assert "replace=True" in upd["error"]

        ov = await plugin.shape_tool(
            action="overview",
            source_msg_id=-2139870001,
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert ov["success"] is True, ov

        rec = await plugin.recall_tool(
            kind="threads",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id=-2139870001,
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert rec["success"] is True, rec

        cancelled = await plugin.later_tool(
            action="cancel",
            intent_id=sched["intent_id"],
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id=-2139870001,
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert cancelled["success"] is True, cancelled
    finally:
        await plugin.on_unload()
