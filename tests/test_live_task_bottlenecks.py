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
