"""Regression tests for main@5eb8bb4 review findings (2 P1 + 2 P2)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from maisya.config import Settings
from maisya.intents import Intents, next_daily_due
from maisya.shaping import execute_shape
from maisya.trust import Message
from plugin import create_plugin

TEST_GROUP_ID = "100246809"
TEST_GROUP_SID = "d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f"
TEST_ADMIN_ID = "admin_1"
TEST_PRIV_SID = "a9b8c7d6e5f4a3b2c1d0e9f8a7b6c5d4"


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0, tz_hours: int = 8) -> float:
    tz = timezone(timedelta(hours=tz_hours))
    return datetime(year, month, day, hour, minute, 0, tzinfo=tz).timestamp()


def _dm_message(
    text: str,
    *,
    msg_id: str = "m-dm-1",
    user_id: str = TEST_ADMIN_ID,
    session_id: str = TEST_PRIV_SID,
    platform: str = "qq",
    ts: float = 1794960000.0,
) -> dict:
    return {
        "message_id": msg_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": str(ts),
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
    ts: float = 1794960000.0,
) -> dict:
    return {
        "message_id": msg_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": str(ts),
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
    clock_box: list[float] | None = None,
    enabled_chats: list[str] | None = None,
    permissions: list[str] | None = None,
    capability_fn=None,
    extra_cfg: dict | None = None,
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
                res = await res
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
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "任务已完成。", "tool_calls": []}
        return {"success": True}

    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=_get_cfg),
        paths=SimpleNamespace(data_dir=data_dir, runtime_dir=tmp_path / "runtime"),
        logger=logging.getLogger("test.maisya.review_p1_p2"),
        call_capability=_cap,
    )
    cfg = Settings().model_dump()
    cfg["plugin"]["enabled"] = True
    cfg["enabled_chats"] = list(
        enabled_chats
        if enabled_chats is not None
        else [f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID]
    )
    cfg["admins"] = [f"qq:{TEST_ADMIN_ID}"]
    cfg["sandbox"] = {"enabled": False}
    if extra_cfg:
        cfg.update(extra_cfg)
    plugin.set_plugin_config(cfg)
    await plugin.on_load()
    if plugin.pulse is not None and plugin.pulse.task is not None:
        plugin.pulse.task.cancel()
        await asyncio.gather(plugin.pulse.task, return_exceptions=True)
        plugin.pulse.task = None
    if clock_box is not None:
        clock_fn = lambda: clock_box[0]
        plugin.intents.clock = clock_fn
        plugin.messages.clock = clock_fn
        plugin.bridge.clock = clock_fn
    return plugin


async def _enable_profile(plugin, profile_id: str) -> None:
    if not (plugin.profiles.path / profile_id).is_dir():
        plugin.profiles.clone("default", profile_id)
    cur = plugin.memory.read("profile", target=profile_id, document="profile.toml")
    text = cur["content"]
    if "enabled = false" in text:
        text = text.replace("enabled = false", "enabled = true")
    elif "enabled = true" not in text:
        text = text + "\nenabled = true\n"
    plugin.memory.write(
        "profile",
        text,
        target=profile_id,
        document="profile.toml",
        expected_version=cur["version"],
        allow_custom=True,
    )


# ──────────────────── [P1-1] replace=True 失败不得取消原计划 ────────────────────


@pytest.mark.asyncio
async def test_p1_replace_schedule_validation_failure_preserves_existing_intent(
    tmp_path: Path,
) -> None:
    clock = [_ts(2026, 11, 18, 7, 0)]
    plugin = await _boot_plugin(tmp_path, clock_box=clock)
    try:
        await _enable_profile(plugin, "digest")
        await plugin.after_receive(
            message=_dm_message("安排每日简报", msg_id="m-dm-replace-1", ts=clock[0])
        )
        sched = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            daily_at=["08:30"],
            profile="digest",
            workspace="digest",
            prompt="整理今日群聊简报",
            reason="每日简报 08:30",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-replace-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert sched["success"] is True, sched
        orig_id = sched["intent_id"]
        assert plugin.intents.get(orig_id)["status"] == "pending"

        # 1. 非法 daily_at=["25:00"]（单槽位）
        bad_time = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            replace=True,
            daily_at=["25:00"],
            profile="digest",
            workspace="digest",
            prompt="整理今日群聊简报",
            reason="改成非法时间",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-replace-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert bad_time["success"] is False
        assert plugin.intents.get(orig_id)["status"] == "pending"

        # 2. 多槽位部分非法 ["09:00", "25:00"] -> 原计划仍在，09:00 也未写入
        bad_multi = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            replace=True,
            daily_at=["09:00", "25:00"],
            profile="digest",
            workspace="digest",
            prompt="整理今日群聊简报",
            reason="多槽位部分非法",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-replace-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert bad_multi["success"] is False
        pending_now = plugin.intents.list(f"chat:{TEST_GROUP_SID}", include_paused=True)
        assert [it["intent_id"] for it in pending_now if it["status"] == "pending"] == [orig_id]

        # 3. 非法 every_seconds=60 (< 300)
        bad_every = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            replace=True,
            every_seconds=60,
            profile="digest",
            workspace="digest",
            prompt="整理今日群聊简报",
            reason="非法周期",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-replace-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert bad_every["success"] is False
        assert plugin.intents.get(orig_id)["status"] == "pending"

        # 4. Intents 层校验失败（例如 reason 超长 > 500 字符）
        bad_reason = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            replace=True,
            daily_at=["09:30"],
            profile="digest",
            workspace="digest",
            prompt="整理今日群聊简报",
            reason="太长" * 260,
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-replace-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert bad_reason["success"] is False
        assert plugin.intents.get(orig_id)["status"] == "pending"

        # 5. shaping setup 传非法周期替换时同样不得取消原计划
        with pytest.raises(ValueError):
            await execute_shape(
                plugin,
                action="setup",
                op="patch",
                target="",
                target_chat=f"qq:group:{TEST_GROUP_ID}",
                patch={
                    "profile": "digest",
                    "workspace": "digest",
                    "every_seconds": 60,
                },
                source_msg_id="m-dm-replace-1",
                current_chat_id=TEST_PRIV_SID,
                current_platform="qq",
            )
        assert plugin.intents.get(orig_id)["status"] == "pending"

        # 6. 合法 replace=True 时原子替换：旧计划取消，新计划 pending
        good = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            replace=True,
            daily_at=["09:30"],
            profile="digest",
            workspace="digest",
            prompt="整理今日群聊简报",
            reason="每日简报 09:30",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="m-dm-replace-1",
            chat_id=TEST_PRIV_SID,
            platform="qq",
        )
        assert good["success"] is True, good
        assert plugin.intents.get(orig_id)["status"] == "cancelled"
        assert orig_id in (good.get("cancelled_ids") or [])
        assert good["intent_id"] != orig_id
        assert plugin.intents.get(good["intent_id"])["status"] == "pending"
    finally:
        await plugin.on_unload()


# ──────────────────── [P1-2] 立即执行与 Pulse 竞态仅派发一次 ────────────────────


@pytest.mark.asyncio
async def test_p1_run_now_and_pulse_concurrent_claim_dispatches_only_once(
    tmp_path: Path,
) -> None:
    clock = [_ts(2026, 11, 18, 9, 0)]
    wakes: list[dict] = []
    wake_entered = asyncio.Event()

    async def _cap(name: str, kwargs: dict):
        if name == "maisaka.proactive.trigger":
            wakes.append(dict(kwargs))
            wake_entered.set()
            await asyncio.sleep(0.03)
            return {"success": True}
        return None

    plugin = await _boot_plugin(tmp_path, clock_box=clock, capability_fn=_cap)
    try:
        await plugin.after_receive(
            message=_dm_message("立即执行测试", msg_id="m-dm-race-1", ts=clock[0])
        )

        # Case A: later_tool(run_now) 在初始读取状态与正式认领之间被 Pulse 先认领
        it_a = plugin.intents.schedule(
            kind="return",
            source="later",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] - 1,
            reason="检查竞态 A",
            payload={"platform": "qq", "scheduled_admin": True},
            bypass_limits=True,
        )["intent_id"]

        orig_get = plugin.intents.get
        pulse_sniped = [False]

        def _raced_get_for_later(iid: str):
            row = orig_get(iid)
            if iid == it_a and not pulse_sniped[0]:
                pulse_sniped[0] = True
                # 模拟在读取状态后、认领前 Pulse 通过 intents.due() 抢先认领
                due_rows = plugin.intents.due(plugin.enabled_for)
                assert any(r["intent_id"] == it_a for r in due_rows)
            return row

        wakes.clear()
        plugin.intents.get = _raced_get_for_later
        try:
            run_a = await plugin.later_tool(
                action="run_now",
                intent_id=it_a,
                target_chat=f"qq:group:{TEST_GROUP_ID}",
                source_msg_id="m-dm-race-1",
                chat_id=TEST_PRIV_SID,
                platform="qq",
            )
        finally:
            plugin.intents.get = orig_get

        assert run_a["success"] is False, run_a
        assert wakes == [], f"Pulse 抢先认领后 run_now 不应再派发: {wakes}"
        assert plugin.intents.get(it_a)["status"] == "claimed"

        # Case B: manage_write(intent_run_now) 对 paused 计划，在读取与认领之间被 Pulse 抢先认领
        it_b = plugin.intents.schedule(
            kind="return",
            source="later",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] + 300,
            reason="检查竞态 B",
            payload={"platform": "qq", "scheduled_admin": True},
            bypass_limits=True,
        )["intent_id"]
        plugin.intents.pause(it_b, f"chat:{TEST_GROUP_SID}", is_admin=True)

        sniped_b = [False]

        def _raced_get_for_mgmt(iid: str):
            row = orig_get(iid)
            if iid == it_b and not sniped_b[0]:
                sniped_b[0] = True
                # 外部把 paused 恢复并立刻被 Pulse due() 认领
                plugin.intents.resume(it_b, f"chat:{TEST_GROUP_SID}", is_admin=True)
                with plugin.intents.store.connect() as db, db:
                    db.execute("UPDATE intents SET due_at=? WHERE intent_id=?", (clock[0] - 1, it_b))
                due_rows = plugin.intents.due(plugin.enabled_for)
                assert any(r["intent_id"] == it_b for r in due_rows)
            return row

        wakes.clear()
        plugin.intents.get = _raced_get_for_mgmt
        try:
            run_b = await plugin.manage_write("intent_run_now", json.dumps({"intent_id": it_b}))
        finally:
            plugin.intents.get = orig_get

        assert run_b["success"] is False, run_b
        assert wakes == [], f"管理台 intent_run_now 竞争失败后不应二次派发: {wakes}"

        # Case C: 两个 run_now（工具 + 管理台）并发竞争同一 pending 计划，仅胜出方派发 1 次
        it_c = plugin.intents.schedule(
            kind="return",
            source="later",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] + 3600,
            reason="检查并发 C",
            payload={"platform": "qq", "scheduled_admin": True},
            bypass_limits=True,
        )["intent_id"]
        wakes.clear()
        res_tool, res_mgmt = await asyncio.gather(
            plugin.later_tool(
                action="run_now",
                intent_id=it_c,
                target_chat=f"qq:group:{TEST_GROUP_ID}",
                source_msg_id="m-dm-race-1",
                chat_id=TEST_PRIV_SID,
                platform="qq",
            ),
            plugin.manage_write("intent_run_now", json.dumps({"intent_id": it_c})),
        )
        wins = [r for r in (res_tool, res_mgmt) if r.get("success") is True]
        losses = [r for r in (res_tool, res_mgmt) if r.get("success") is False]
        assert len(wins) == 1 and len(losses) == 1, (res_tool, res_mgmt)
        c_wakes = [w for w in wakes if (w.get("metadata") or {}).get("intent_id") == it_c]
        assert len(c_wakes) == 1, c_wakes

        # Case D: run_now 正在 _dispatch 等待外部 RPC 时并发触发 pulse.tick() -> 同样只派发 1 次
        it_d = plugin.intents.schedule(
            kind="return",
            source="later",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] - 1,
            reason="检查派发期 tick 并发 D",
            payload={"platform": "qq", "scheduled_admin": True},
            bypass_limits=True,
        )["intent_id"]
        wakes.clear()
        wake_entered.clear()
        run_d_task = asyncio.create_task(
            plugin.later_tool(
                action="run_now",
                intent_id=it_d,
                target_chat=f"qq:group:{TEST_GROUP_ID}",
                source_msg_id="m-dm-race-1",
                chat_id=TEST_PRIV_SID,
                platform="qq",
            )
        )
        await asyncio.wait_for(wake_entered.wait(), timeout=2.0)
        await plugin.pulse._intents()
        res_d = await run_d_task
        assert res_d["success"] is True, res_d
        d_wakes = [w for w in wakes if (w.get("metadata") or {}).get("intent_id") == it_d]
        assert len(d_wakes) == 1, d_wakes
        assert plugin.intents.get(it_d)["status"] == "done"
    finally:
        await plugin.on_unload()


# ──────────────────── [P2-3] 循环提醒撞到单日额度不得永久停排 ────────────────────


@pytest.mark.asyncio
async def test_p2_recurring_proactive_reminder_survives_full_daily_quota(
    tmp_path: Path,
) -> None:
    clock = [_ts(2026, 11, 18, 8, 50)]  # Day 1 08:50
    wakes: list[dict] = []

    async def _cap(name: str, kwargs: dict):
        if name == "maisaka.proactive.trigger":
            wakes.append(dict(kwargs))
            return {"success": True}
        return None

    plugin = await _boot_plugin(
        tmp_path,
        clock_box=clock,
        capability_fn=_cap,
        extra_cfg={
            "proactive_level": "lively",
            "daily_proactive_limit": 3,
            "per_chat_daily_proactive_limit": 1,
        },
    )
    try:
        plugin.messages.record(
            Message.from_event(_group_message("早安", msg_id="m-grp-quota-1", ts=clock[0]))
        )
        target = f"chat:{TEST_GROUP_SID}"

        # 无 profile 的每日 09:00 循环主动提醒（Day 1 触发）
        day1_0900, _ = next_daily_due(clock[0], "09:00", 480)
        rec = plugin.intents.schedule(
            kind="proactive",
            source="afterglow",
            target=target,
            due_at=day1_0900,
            reason="每天早上聊两句",
            mode="recurring",
            payload={
                "platform": "qq",
                "daily_at": "09:00",
                "every_seconds": 86400,
                "tz_offset_minutes": 480,
            },
        )
        # 预先在 Day 2 占满该会话当日唯一一条主动额度
        day2_1000 = _ts(2026, 11, 19, 10, 0)
        oneoff_day2 = plugin.intents.schedule(
            kind="proactive",
            source="afterglow",
            target=target,
            due_at=day2_1000,
            reason="次日一次性提醒占额",
            mode="once",
            payload={"platform": "qq"},
        )

        # Day 1 09:01 触发第一轮循环提醒 -> 本轮 done，下一轮撞到 Day 2 额度
        clock[0] = _ts(2026, 11, 18, 9, 1)
        await plugin.pulse.tick()
        assert len(wakes) == 1
        assert plugin.intents.get(rec["intent_id"])["status"] == "done"

        # 关键断言：循环提醒未被警告日志丢弃，而是顺延到下一个未满周期（Day 3 09:00）
        plugin.intents.note_received(TEST_GROUP_SID)
        rows = plugin.intents.list(target, include_paused=True)
        deferred_next = [
            it
            for it in rows
            if it["intent_id"] not in {rec["intent_id"], oneoff_day2["intent_id"]}
            and (it.get("payload") or {}).get("daily_at") == "09:00"
        ]
        assert len(deferred_next) == 1, rows
        assert deferred_next[0]["status"] == "pending"
        assert deferred_next[0]["due_at"] == pytest.approx(_ts(2026, 11, 20, 9, 0), abs=2)

        # Day 2 一次性提醒正常触发
        clock[0] = _ts(2026, 11, 19, 10, 1)
        await plugin.pulse.tick()
        assert plugin.intents.get(oneoff_day2["intent_id"])["status"] == "done"
        assert len(wakes) == 2

        # Day 3 09:01 循环提醒继续触发并续排到 Day 4
        plugin.intents.note_received(TEST_GROUP_SID)
        clock[0] = _ts(2026, 11, 20, 9, 1)
        await plugin.pulse.tick()
        assert plugin.intents.get(deferred_next[0]["intent_id"])["status"] == "done"
        assert len(wakes) == 3
        day4_rows = [
            it
            for it in plugin.intents.list(target, include_paused=True)
            if (it.get("payload") or {}).get("daily_at") == "09:00"
        ]
        assert len(day4_rows) == 1 and day4_rows[0]["due_at"] > clock[0]

        # Case B: 全局与单会话主动额度降为 0 时，短周期循环提醒进入可恢复的 budget_paused，额度恢复后自动续排
        for old_row in day4_rows:
            plugin.intents.cancel(old_row["intent_id"], target, is_admin=True)
        clock[0] = _ts(2026, 11, 22, 10, 0)  # 进入全新空额日（Day 5）
        short_rec = plugin.intents.schedule(
            kind="proactive",
            source="afterglow",
            target=target,
            due_at=clock[0] + 60,
            reason="高频循环惦记",
            mode="recurring",
            payload={"platform": "qq", "every_seconds": 3600},
        )
        await plugin.on_config_update(
            "self",
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": [f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
                "admins": [f"qq:{TEST_ADMIN_ID}"],
                "proactive_level": "lively",
                "daily_proactive_limit": 0,
                "per_chat_daily_proactive_limit": 0,
                "sandbox": {"enabled": False},
            },
            2,
        )
        clock[0] += 70
        await plugin.pulse.tick()
        # _proactive_gate 因 daily_proactive_limit=0 失败并触发 _reschedule_recurring -> 落为 budget_paused 而非丢弃
        paused_items = [
            it
            for it in plugin.intents.list(target, include_paused=True)
            if it["reason"] == "高频循环惦记" and it["status"] in {"pending", "budget_paused"}
        ]
        assert len(paused_items) == 1, plugin.intents.list(target, include_paused=True)
        assert paused_items[0]["status"] == "budget_paused"
        assert paused_items[0]["wake_status"] == "daily_proactive_limit"

        # 恢复额度到 >=1 且跨到新一天 -> due() 自动唤醒该 budget_paused 循环提醒
        await plugin.on_config_update(
            "self",
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": [f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
                "admins": [f"qq:{TEST_ADMIN_ID}"],
                "proactive_level": "lively",
                "daily_proactive_limit": 3,
                "per_chat_daily_proactive_limit": 1,
                "sandbox": {"enabled": False},
            },
            3,
        )
        plugin.intents.note_received(TEST_GROUP_SID)
        clock[0] = max(clock[0] + 86400, paused_items[0]["due_at"] + 1)
        await plugin.pulse.tick()
        assert plugin.intents.get(paused_items[0]["intent_id"])["status"] == "done"
        assert len(wakes) == 4
    finally:
        await plugin.on_unload()


# ──────────────────── [P2-4] 后台静默续跑门控与常规模型 Token 结算 ────────────────────


@pytest.mark.asyncio
async def test_p2_silent_thread_continuation_and_background_llm_tokens_accounted(
    tmp_path: Path,
) -> None:
    clock = [_ts(2026, 11, 18, 10, 0)]
    llm_call_log: list[str] = []
    llm_step_mode = ["fg_only"]

    async def _cap(name: str, kwargs: dict):
        if name != "llm.generate_with_tools":
            return None
        prompt_txt = json.dumps(kwargs.get("prompt") or [], ensure_ascii=False)
        llm_call_log.append(prompt_txt)
        if llm_step_mode[0] == "fg_only":
            # 前台用户消息触发，返回 500 Token，绝不应记入后台预算
            return {
                "success": True,
                "response": "前台初始回复完成。",
                "tool_calls": [],
                "prompt_tokens": 300,
                "completion_tokens": 200,
                "total_tokens": 500,
            }
        if llm_step_mode[0] == "bg_silent_continue":
            return {
                "success": True,
                "response": "后台静默续跑一步完成。",
                "tool_calls": [],
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            }
        if llm_step_mode[0] == "bg_two_step_1":
            llm_step_mode[0] = "bg_two_step_2"
            # 第 1 步模型调用（宿主标准顶层 total_tokens=50）触发 thread_read 工具
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": "call_tok_1",
                        "type": "function",
                        "function": {
                            "name": "thread_read",
                            "arguments": '{"query":"第"}',
                        },
                    }
                ],
                "prompt_tokens": 35,
                "completion_tokens": 15,
                "total_tokens": 50,
            }
        if llm_step_mode[0] == "bg_two_step_2":
            llm_step_mode[0] = "done"
            # 第 2 步模型调用（嵌套 usage.total_tokens=50）完成，两步共 100 Token
            return {
                "success": True,
                "response": "两次后台模型调用已完成汇总。",
                "tool_calls": [],
                "usage": {"total_tokens": 50},
            }
        if llm_step_mode[0] == "heartbeat_decide":
            return {
                "success": True,
                "response": '{"action":"ok"}',
                "tool_calls": [],
                "total_tokens": 42,
            }
        return {"success": True, "response": "ok", "tool_calls": []}

    plugin = await _boot_plugin(
        tmp_path,
        clock_box=clock,
        capability_fn=_cap,
        extra_cfg={
            "daily_bg_runs_limit": 0,
            "daily_bg_tokens_limit": 5000,
        },
    )
    try:
        # 1. 创建初始线程（模拟前台用户请求，消耗 500 Token 不计入 bg_budget_usage）
        plugin.messages.record(
            Message.from_event(
                _group_message("先启动个线程", msg_id="m-grp-bg-1", ts=clock[0])
            )
        )
        fg = await plugin.agent_tool(
            action="start",
            profile="default",
            message="前台触发执行",
            source_msg_id="m-grp-bg-1",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            wait_seconds=5,
        )
        assert fg["success"] is True, fg
        tid = fg["thread_id"]
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        assert plugin.intents.bg_budget_state(now=clock[0])["tokens_used"] == 0
        assert plugin.intents.bg_budget_state(now=clock[0])["runs_used"] == 0

        # 2. 已有线程的静默续跑在 daily_bg_runs_limit == 0 时不得派发，且应保留 thread_id
        silent_it = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] + 5,
            reason="既有线程静默续跑",
            thread_id=tid,
            payload={"platform": "qq", "next_step": "后台继续检查"},
            bypass_limits=True,
        )
        llm_call_log.clear()
        llm_step_mode[0] = "bg_silent_continue"
        clock[0] += 10
        await plugin.pulse.tick()
        assert llm_call_log == [], "daily_bg_runs_limit=0 时不应派发静默续跑"
        paused_silent = plugin.intents.get(silent_it["intent_id"])
        assert paused_silent["status"] == "budget_paused"
        assert paused_silent["wake_status"] == "daily_bg_runs_limit"
        assert paused_silent["thread_id"] == tid

        # 3. 放开运行次数上限到 10 并在次日到期恢复 -> 静默续跑执行，且计入 runs_used=1 与 tokens_used=30
        await plugin.on_config_update(
            "self",
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": [f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
                "admins": [f"qq:{TEST_ADMIN_ID}"],
                "daily_bg_runs_limit": 10,
                "daily_bg_tokens_limit": 1000,
                "sandbox": {"enabled": False},
            },
            2,
        )
        clock[0] = max(clock[0] + 86400, paused_silent["due_at"] + 1)
        await plugin.pulse.tick()
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        assert plugin.intents.get(silent_it["intent_id"])["status"] == "done"
        budget_after_silent = plugin.intents.bg_budget_state(now=clock[0])
        assert budget_after_silent["runs_used"] == 1
        assert budget_after_silent["tokens_used"] == 30

        # 4. 新一天测常规后台模型路径两次调用共返回 100 Token -> 账本准确记录 100 Token
        clock[0] += 86400
        llm_step_mode[0] = "bg_two_step_1"
        wf = plugin.intents.schedule(
            kind="silent",
            source="workflow",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] + 5,
            reason="两次模型调用后台任务",
            payload={
                "platform": "qq",
                "profile": "default",
                "prompt": "执行两步检查",
                "scheduled_admin": True,
                "issuer_admin": f"qq:{TEST_ADMIN_ID}",
            },
            bypass_limits=True,
        )
        clock[0] += 10
        await plugin.pulse.tick()
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        assert plugin.intents.get(wf["intent_id"])["status"] == "done"
        budget_after_wf = plugin.intents.bg_budget_state(now=clock[0])
        assert budget_after_wf["runs_used"] == 1
        assert budget_after_wf["tokens_used"] == 100

        # 5. 心跳决策路径（llm.generate_with_tools max_tokens=200）也会把 Token 记入账本
        await plugin.on_config_update(
            "self",
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": [f"qq:group:{TEST_GROUP_ID}", TEST_GROUP_SID],
                "admins": [f"qq:{TEST_ADMIN_ID}"],
                "heartbeat_mode": "gentle",
                "heartbeat_enabled": True,
                "daily_bg_runs_limit": 10,
                "daily_bg_tokens_limit": 142,
                "sandbox": {"enabled": False},
            },
            3,
        )
        plugin.memory.write("heartbeat", "## 性情\n温柔安静。\n\n## 自主探索方向\n- 看看公开技术动态。\n")
        plugin.pulse.invalidate_caches()
        llm_step_mode[0] = "heartbeat_decide"
        await plugin.pulse.tick()
        budget_after_hb = plugin.intents.bg_budget_state(now=clock[0])
        assert budget_after_hb["runs_used"] == 2
        assert budget_after_hb["tokens_used"] == 142
        assert budget_after_hb["exhausted"] is True
        assert budget_after_hb["exhausted_reason"] == "daily_bg_tokens_limit"

        # 6. Token 预算耗尽后，下一条静默续跑立即进入 budget_paused 而不会再调模型
        llm_call_log.clear()
        blocked_silent = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] + 5,
            reason="Token耗尽拦截",
            thread_id=tid,
            payload={"platform": "qq", "next_step": "不应执行"},
            bypass_limits=True,
        )
        clock[0] += 10
        await plugin.pulse.tick()
        assert llm_call_log == []
        assert plugin.intents.get(blocked_silent["intent_id"])["status"] == "budget_paused"
        assert (
            plugin.intents.get(blocked_silent["intent_id"])["wake_status"]
            == "daily_bg_tokens_limit"
        )
    finally:
        await plugin.on_unload()
