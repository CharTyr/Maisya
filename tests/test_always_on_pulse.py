import asyncio
import dataclasses
import json
import os
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from maisya.config import Settings
from maisya.pulse import Pulse
from test_foundation import event, loaded


def _make_fake_pulse_clock(start: float = 1_700_000_000.0):
    now = [start]
    return now, lambda: now[0]


@pytest.mark.asyncio
async def test_longing_disabled_by_default_config_webui_and_pulse(tmp_path):
    default_settings = Settings()
    assert default_settings.longing_quiet_hours == 0

    assert Settings(longing_quiet_hours=0).longing_quiet_hours == 0
    assert Settings(longing_quiet_hours=12).longing_quiet_hours == 12
    assert Settings(longing_quiet_hours=720).longing_quiet_hours == 720

    for bad in (-1, 1, 11, 721):
        with pytest.raises(ValidationError) as exc_info:
            Settings(longing_quiet_hours=bad)
        if 1 <= bad <= 11:
            assert "久违问候安静阈值须为 0（关闭）或 12 到 720 小时" in str(
                exc_info.value
            )

    root = Path(__file__).resolve().parent.parent
    example_toml = tomllib.loads((root / "config.example.toml").read_text("utf-8"))
    assert example_toml["longing_quiet_hours"] == 0

    expected_help = "设为 0 默认关闭；设为 12-720 时，熟人会话安静超过设定小时数后才可能温柔打招呼。"
    for rel in ("webui/management.html.in", "webui/settings.html"):
        text = (root / rel).read_text("utf-8")
        assert 'id="set-longing"' in text
        assert 'min="0"' in text
        assert "longing_quiet_hours ?? 0" in text
        assert expected_help in text

    plugin = await loaded(tmp_path)
    try:
        assert plugin.pulse.longing_quiet_hours == 0
        # Even constructed directly without kwarg, Pulse defaults longing_quiet_hours=0
        bare = Pulse(plugin.bridge, plugin.ctx, plugin.enabled_for)
        assert bare.longing_quiet_hours == 0

        now, clock = _make_fake_pulse_clock()
        plugin.intents.clock = clock
        await plugin.after_receive(
            message=event("你好", user="friend", group=False, platform="qq")
        )
        plugin.config.acquaintance_users = ["qq:friend"]
        plugin.pulse.acquaintance_users = {"qq:friend"}
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE session_routes SET last_human_at=? WHERE session_id='chat'",
                (now[0] - 100 * 3600,),
            )

        wakes = []

        async def capability(name, **kwargs):
            if name == "maisaka.proactive.trigger":
                wakes.append((kwargs.get("stream_id"), kwargs.get("intent")))
            return {"success": True}

        plugin.ctx.call_capability = capability
        await plugin.pulse.tick()
        assert wakes == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_recurring_survives_failures_safety_floor_and_recent_runs_carryover(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        now, clock = _make_fake_pulse_clock(1_700_000_000.0)
        plugin.intents.clock = clock
        plugin.engine.store = plugin.threads

        await plugin.after_receive(
            message=event("开启巡检", user="admin", group=True, platform="qq")
        )
        # Identify target chat_id from session_routes
        with plugin.threads.connect() as db:
            row = db.execute("SELECT session_id FROM session_routes LIMIT 1").fetchone()
        assert row is not None
        chat_id = str(row["session_id"])

        # 1. Safety floor: if daily_at resolves < clock + 60, clamp to clock + 60
        # Pick 08:59:50 +08:00 (2023-11-15 00:59:50 UTC = 1700009990)
        now[0] = 1_700_009_990.0
        await plugin.pulse._reschedule_recurring(
            {
                "kind": "proactive",
                "source": "workflow",
                "target": f"chat:{chat_id}",
                "reason": "每日早报",
                "payload": {
                    "profile": "scout",
                    "prompt": "查变化",
                    "daily_at": "09:00",
                    "tz_offset_minutes": 480,
                    "every_seconds": 86400,
                    "scheduled_admin": True,
                },
            }
        )
        pending = plugin.intents.list(f"chat:{chat_id}")
        assert len(pending) == 1
        assert pending[0]["due_at"] >= now[0] + 60
        plugin.intents.cancel(pending[0]["intent_id"], f"chat:{chat_id}")

        # 2. Failure survival in _dispatch(): proactive gate fail, profile_disabled,
        # fresh_authorization_required, and wake rejected must ALL reschedule.
        wakes = []

        async def reject_capability(name, **kwargs):
            if name == "maisaka.proactive.trigger":
                wakes.append((kwargs.get("stream_id"), kwargs.get("intent")))
                return {"success": False}
            return {"success": True}

        plugin.ctx.call_capability = reject_capability

        async def step_fn(profile, history, **_kwargs):
            return {"response": f"巡检结论@{int(now[0])} 密钥sk-secret123456", "tool_calls": []}

        plugin.runtime.step = step_fn
        plugin.pulse.redact = lambda s: s.replace("sk-secret123456", "[REDACTED]")

        base_payload = {
            "profile": "scout",
            "prompt": "巡检新变化",
            "every_seconds": 600,
            "scheduled_admin": True,
        }

        # Case A: proactive gate quota_exhausted -> failed -> MUST reschedule
        plugin.intents.mark_proactive(f"chat:{chat_id}")
        plugin.intents.mark_proactive(f"chat:{chat_id}")
        iid_quota = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{chat_id}",
            due_at=now[0],
            reason="巡检A",
            payload=dict(base_payload),
            bypass_limits=True,
        )["intent_id"]
        await plugin.pulse.tick()
        got_quota = plugin.intents.get(iid_quota)
        assert got_quota["status"] == "failed"
        assert got_quota["wake_status"] in {
            "unreplied_lock",
            "quota_exhausted",
            "daily_proactive_limit",
            "daily_quota",
        }
        rescheduled = [
            it
            for it in plugin.intents.list(f"chat:{chat_id}")
            if it["status"] == "pending"
        ]
        assert len(rescheduled) == 1
        assert rescheduled[0]["due_at"] >= now[0] + 600
        plugin.intents.cancel(rescheduled[0]["intent_id"], f"chat:{chat_id}")
        plugin.intents.note_received(chat_id)  # human message unlocks un-replied lock
        now[0] += 86400  # reset daily quota before Case C/D

        # Case B: sleep_window -> deferred -> MUST NOT duplicate reschedule
        plugin.pulse.sleep_window = "0-24"
        iid_sleep = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{chat_id}",
            due_at=now[0],
            reason="巡检睡眠",
            payload=dict(base_payload),
            bypass_limits=True,
        )["intent_id"]
        await plugin.pulse.tick()
        assert plugin.intents.get(iid_sleep)["status"] == "pending"
        assert plugin.intents.get(iid_sleep)["due_at"] > now[0]
        extra = [
            it
            for it in plugin.intents.list(f"chat:{chat_id}")
            if it["status"] == "pending" and it["intent_id"] != iid_sleep
        ]
        assert extra == []
        plugin.intents.cancel(iid_sleep, f"chat:{chat_id}")
        plugin.pulse.sleep_window = ""

        # Case C: profile_disabled -> failed -> MUST reschedule
        orig_get = plugin.profiles.get
        plugin.profiles.get = (
            lambda pid: dataclasses.replace(orig_get(pid), enabled=False)
            if pid == "scout"
            else orig_get(pid)
        )
        try:
            iid_disabled = plugin.intents.schedule(
                kind="proactive",
                source="workflow",
                target=f"chat:{chat_id}",
                due_at=now[0],
                reason="巡检禁用",
                payload={**base_payload, "profile": "scout"},
                bypass_limits=True,
            )["intent_id"]
            await plugin.pulse.tick()
            got_disabled = plugin.intents.get(iid_disabled)
            assert got_disabled["status"] == "failed"
            assert got_disabled["wake_status"] == "profile_disabled"
            rescheduled = [
                it
                for it in plugin.intents.list(f"chat:{chat_id}")
                if it["status"] == "pending"
            ]
            assert len(rescheduled) == 1
            plugin.intents.cancel(rescheduled[0]["intent_id"], f"chat:{chat_id}")
        finally:
            plugin.profiles.get = orig_get

        # Case D: fresh_authorization_required -> failed -> MUST reschedule
        iid_auth = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{chat_id}",
            due_at=now[0],
            reason="巡检授权",
            payload={
                "profile": "scout",
                "prompt": "巡检",
                "every_seconds": 600,
                "scheduled_admin": False,
            },
            bypass_limits=True,
        )["intent_id"]
        orig_get = plugin.profiles.get
        plugin.profiles.get = (
            lambda pid: dataclasses.replace(orig_get(pid), access="admins")
            if pid == "scout"
            else orig_get(pid)
        )
        try:
            await plugin.pulse.tick()
            got_auth = plugin.intents.get(iid_auth)
            assert got_auth["status"] == "failed"
            assert got_auth["wake_status"] == "fresh_authorization_required"
            rescheduled = [
                it
                for it in plugin.intents.list(f"chat:{chat_id}")
                if it["status"] == "pending"
            ]
            assert len(rescheduled) == 1
            plugin.intents.cancel(rescheduled[0]["intent_id"], f"chat:{chat_id}")
        finally:
            plugin.profiles.get = orig_get

        # Case E: wake failed (reject_wake returned False) -> STILL reschedules + recent_runs
        now[0] += 86400  # fresh day quota
        plugin.intents.note_received(chat_id)
        iid_wake_fail = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{chat_id}",
            due_at=now[0],
            reason="巡检唤醒失败",
            payload=dict(base_payload),
            bypass_limits=True,
        )["intent_id"]
        # First tick launches thread non-blocking (wait_seconds=0), wait task then next tick wakes
        await plugin.pulse.tick()
        await asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True)
        now[0] += 3
        await plugin.pulse.tick()
        assert len(wakes) == 1
        assert "stop" in wakes[0][1]
        assert plugin.intents.get(iid_wake_fail)["status"] == "failed"
        rescheduled = [
            it
            for it in plugin.intents.list(f"chat:{chat_id}")
            if it["status"] == "pending"
        ]
        assert len(rescheduled) == 1
        runs = rescheduled[0]["payload"].get("recent_runs") or []
        assert len(runs) == 1
        assert "[REDACTED]" in runs[0]["summary"]
        assert "sk-secret123456" not in runs[0]["summary"]
        plugin.intents.cancel(rescheduled[0]["intent_id"], f"chat:{chat_id}")

        # 3. _silent() failure survival (profile_disabled & fresh_authorization_required & done)
        for mode, p_override, expected_wake_status in (
            (
                "disabled",
                {"profile": "scout", "every_seconds": 600, "scheduled_admin": True},
                "profile_disabled",
            ),
            (
                "nonauto",
                {"profile": "scout", "every_seconds": 600, "scheduled_admin": False},
                "fresh_authorization_required",
            ),
        ):
            if mode == "disabled":
                plugin.profiles.get = (
                    lambda pid: dataclasses.replace(orig_get(pid), enabled=False)
                    if pid == "scout"
                    else orig_get(pid)
                )
            else:
                plugin.profiles.get = (
                    lambda pid: dataclasses.replace(orig_get(pid), access="admins")
                    if pid == "scout"
                    else orig_get(pid)
                )
            try:
                iid_s = plugin.intents.schedule(
                    kind="silent",
                    source="workflow",
                    target=f"chat:{chat_id}",
                    due_at=now[0],
                    reason="静默巡检",
                    payload=p_override,
                    bypass_limits=True,
                )["intent_id"]
                await plugin.pulse.tick()
                got_s = plugin.intents.get(iid_s)
                assert got_s["status"] == "failed"
                assert got_s["wake_status"] == expected_wake_status
                nxt = [
                    it
                    for it in plugin.intents.list(f"chat:{chat_id}")
                    if it["status"] == "pending"
                ]
                assert len(nxt) == 1
                plugin.intents.cancel(nxt[0]["intent_id"], f"chat:{chat_id}")
            finally:
                plugin.profiles.get = orig_get

        # 4. recent_runs carryover rolling cap (<= 3 items, JSON <= 1200 chars), <previous_runs> injection,
        #    and skipped when workspace is non-empty.
        long_text = "A" * 390
        row_carry = {
            "kind": "proactive",
            "source": "workflow",
            "target": f"chat:{chat_id}",
            "reason": "周期核查",
            "payload": {
                "profile": "scout",
                "prompt": "核查新增",
                "every_seconds": 600,
                "scheduled_admin": True,
                "recent_runs": [
                    {"at": 1, "summary": long_text},
                    {"at": 2, "summary": long_text},
                    {"at": 3, "summary": long_text},
                ],
            },
        }
        await plugin.pulse._reschedule_recurring(
            row_carry, thread={"result": {"text": "B" * 400}}
        )
        pending = plugin.intents.list(f"chat:{chat_id}")
        assert len(pending) == 1
        rr = pending[0]["payload"]["recent_runs"]
        assert len(rr) <= 3
        assert len(json.dumps(rr, ensure_ascii=False)) <= 1200
        assert rr[-1]["summary"].startswith("BBBB")

        # Next run injects <previous_runs> into prompt started in engine
        captured_starts = []
        orig_start = plugin.engine.start

        async def spy_start(*args, **kwargs):
            captured_starts.append((args, kwargs))
            return await orig_start(*args, **kwargs)

        plugin.engine.start = spy_start
        now[0] += 700
        await plugin.pulse.tick()
        assert captured_starts
        started_prompt = captured_starts[0][0][1]
        assert "<previous_runs>" in started_prompt
        assert "请对比前序巡检结论，聚焦新增变化，避免重复播报相同旧闻。" in started_prompt
        await asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True)

        # Workspace-bound workflow does NOT carry over recent_runs
        await plugin.pulse._reschedule_recurring(
            {
                "kind": "proactive",
                "source": "workflow",
                "target": f"chat:{chat_id}",
                "reason": "长期项目",
                "payload": {
                    "profile": "scout",
                    "prompt": "继续推进",
                    "every_seconds": 600,
                    "workspace": "proj-alpha",
                    "scheduled_admin": True,
                },
            },
            thread={"result": {"text": "项目进展"}},
        )
        ws_pending = [
            it
            for it in plugin.intents.list(f"chat:{chat_id}")
            if (it.get("payload") or {}).get("workspace") == "proj-alpha"
        ]
        assert len(ws_pending) == 1
        assert "recent_runs" not in ws_pending[0]["payload"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_dispatch_nonblocking_short_defer_and_silent_kick_queued(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        now, clock = _make_fake_pulse_clock(1_700_000_000.0)
        plugin.intents.clock = clock
        await plugin.after_receive(
            message=event("启动任务", user="admin", group=False, platform="qq")
        )
        with plugin.threads.connect() as db:
            chat_id = str(
                db.execute("SELECT session_id FROM session_routes LIMIT 1").fetchone()[
                    "session_id"
                ]
            )

        # Wrap engine with cross-slice future signature (background, defer_if_full, kick_queued)
        start_calls = []
        send_calls = []
        kick_calls = []
        kick_result = [True]

        class FutureEngineProxy:
            def __init__(self, inner):
                self.inner = inner
                self.tasks = inner.tasks
                self.chats = inner.chats

            async def start(
                self,
                profile,
                message,
                *,
                chat_id,
                platform,
                user_id,
                owner="chat",
                wait_seconds=20,
                return_route=None,
                workspace="",
                grant=None,
                references=None,
                from_threads=None,
                background=False,
                defer_if_full=False,
            ):
                start_calls.append(
                    {
                        "wait_seconds": wait_seconds,
                        "background": background,
                        "defer_if_full": defer_if_full,
                    }
                )
                return await self.inner.start(
                    profile,
                    message,
                    chat_id=chat_id,
                    platform=platform,
                    user_id=user_id,
                    owner=owner,
                    wait_seconds=wait_seconds,
                    return_route=return_route,
                    workspace=workspace,
                    grant=grant,
                    references=references,
                    from_threads=from_threads,
                )

            async def send(
                self,
                thread_id,
                message,
                *,
                chat_id,
                wait_seconds=20,
                return_route=None,
                input_context=None,
                steer=False,
                background=False,
                defer_if_full=False,
            ):
                send_calls.append(
                    {
                        "wait_seconds": wait_seconds,
                        "background": background,
                        "defer_if_full": defer_if_full,
                    }
                )
                return await self.inner.send(
                    thread_id,
                    message,
                    chat_id=chat_id,
                    wait_seconds=wait_seconds,
                    return_route=return_route,
                    input_context=input_context,
                    steer=steer,
                )

            async def kick_queued(self, thread_id, chat_id, *, background=True):
                kick_calls.append((thread_id, chat_id, background))
                return kick_result[0]

        gate = asyncio.Event()

        async def slow_step(profile, history, **_kwargs):
            await gate.wait()
            return {"response": "完成", "tool_calls": []}

        plugin.runtime.step = slow_step
        plugin.pulse.engine = FutureEngineProxy(plugin.engine)

        # 1. _dispatch workflow launches with wait_seconds=0, background=True, defer_if_full=True
        #    and defers with delay=2 while active
        iid = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{chat_id}",
            due_at=now[0],
            reason="慢任务",
            payload={
                "profile": "scout",
                "prompt": "跑一个异步步",
                "scheduled_admin": True,
            },
            bypass_limits=True,
        )["intent_id"]
        await plugin.pulse.tick()
        assert start_calls and start_calls[0] == {
            "wait_seconds": 0,
            "background": True,
            "defer_if_full": True,
        }
        deferred = plugin.intents.get(iid)
        assert deferred["status"] == "pending"
        assert deferred["due_at"] == pytest.approx(now[0] + 2)

        # Simulate wait_thread with queued revision and no running task -> calls kick_queued
        tid = deferred["thread_id"]
        plugin.threads.enqueue(tid, "追加消息", chat_id)
        plugin.engine.tasks.pop(tid, None)
        with plugin.threads.connect() as db, db:
            db.execute("UPDATE intents SET due_at=? WHERE intent_id=?", (now[0], iid))
        await plugin.pulse.tick()
        assert any(c[0] == tid and c[2] is True for c in kick_calls)

        gate.set()
        await asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True)

        # 2. _silent(): active thread defers with delay=15; queued already_started calls kick_queued
        t_silent = plugin.threads.create(
            plugin.profiles.get("scout"),
            "初始",
            chat_id=chat_id,
            platform="qq",
            user_id="u1",
        )
        sid = t_silent["thread_id"]
        plugin.threads.enqueue(sid, "排队消息", chat_id)
        plugin.engine.tasks.pop(sid, None)

        # 2a. already_started + kick_queued returns False -> defer delay=15
        kick_result[0] = False
        kick_calls.clear()
        iid_as = plugin.intents.schedule(
            kind="silent",
            source="webhook",
            target=f"chat:{chat_id}",
            due_at=now[0],
            reason="webhook",
            thread_id=sid,
            payload={"profile": "scout", "already_started": True, "every_seconds": 600},
            bypass_limits=True,
        )["intent_id"]
        await plugin.pulse.tick()
        assert kick_calls == [(sid, chat_id, True)]
        row_as = plugin.intents.get(iid_as)
        assert row_as["status"] == "pending"
        assert row_as["due_at"] == pytest.approx(now[0] + 15)

        # 2b. already_started + kick_queued returns True -> done + reschedule
        kick_result[0] = True
        with plugin.threads.connect() as db, db:
            db.execute("UPDATE intents SET due_at=? WHERE intent_id=?", (now[0], iid_as))
        await plugin.pulse.tick()
        assert plugin.intents.get(iid_as)["status"] == "done"
        assert len(plugin.intents.list(f"chat:{chat_id}")) >= 1

        # 2c. _silent target thread active -> delay=15
        plugin.engine.tasks[sid] = asyncio.create_task(asyncio.sleep(0))
        iid_active = plugin.intents.schedule(
            kind="silent",
            source="workflow",
            target=f"chat:{chat_id}",
            due_at=now[0],
            reason="繁忙",
            thread_id=sid,
            payload={"profile": "scout"},
            bypass_limits=True,
        )["intent_id"]
        await plugin.pulse.tick()
        row_active = plugin.intents.get(iid_active)
        assert row_active["status"] == "pending"
        assert row_active["due_at"] == pytest.approx(now[0] + 15)
        await plugin.engine.tasks[sid]

        # 2d. _silent target thread idle + send passes background=True, defer_if_full=True
        plugin.engine.tasks.pop(sid, None)
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='idle', processed_revision=revision WHERE thread_id=?",
                (sid,),
            )
            db.execute("UPDATE intents SET due_at=? WHERE intent_id=?", (now[0], iid_active))
        await plugin.pulse.tick()
        assert send_calls and send_calls[-1]["background"] is True
        assert send_calls[-1]["defer_if_full"] is True
        await asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_zero_idle_polling_cache_invalidation_and_bg_longing_throttle(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        now, clock = _make_fake_pulse_clock(1_700_000_000.0)
        plugin.intents.clock = clock
        await plugin.after_receive(
            message=event("你好", user="friend", group=False, platform="qq")
        )
        hb_path = Path(plugin.memory.root) / "memory/self/HEARTBEAT.md"

        read_calls = 0
        claim_calls = 0
        orig_read = plugin.memory.read
        orig_claim = plugin.pulse._claim_heartbeat

        def count_read(kind, **kwargs):
            nonlocal read_calls
            if kind == "heartbeat":
                read_calls += 1
            return orig_read(kind, **kwargs)

        def count_claim(ts):
            nonlocal claim_calls
            claim_calls += 1
            return orig_claim(ts)

        plugin.memory.read = count_read
        plugin.pulse._claim_heartbeat = count_claim

        # 1. Empty HEARTBEAT.md: reads once on first tick, ZERO reads & ZERO _claim_heartbeat on repeated ticks
        hb_path.write_text(
            "## 气质与分寸\n温柔\n\n## 自主探索方向\n\n## 临时备忘\n", encoding="utf-8"
        )
        os.utime(hb_path, (1000, 1000))
        await plugin.pulse._heartbeat()
        assert read_calls == 1
        assert claim_calls == 0

        for _ in range(5):
            now[0] += 10
            await plugin.pulse._heartbeat()
        assert read_calls == 1
        assert claim_calls == 0

        # 2. Updating HEARTBEAT.md (new mtime/content) populates plan and claims once;
        #    idle ticks before heartbeat_interval do ZERO reads & ZERO _claim_heartbeat calls.
        plugin.pulse.heartbeat_interval = 3600
        hb_path.write_text(
            "## 气质与分寸\n温柔\n\n## 自主探索方向\n检查站内更新\n\n## 临时备忘\n",
            encoding="utf-8",
        )
        os.utime(hb_path, (2000, 2000))
        await plugin.pulse._heartbeat()
        assert claim_calls == 1
        await asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True)
        reads_after_run = read_calls

        for _ in range(5):
            now[0] += 100
            await plugin.pulse._heartbeat()
        assert read_calls == reads_after_run
        assert claim_calls == 1

        # 3. invalidate_caches clears mtime + next_at, but if SQLite cooldown is still active
        #    _claim_heartbeat returns False and re-populates _heartbeat_next_at = last_run + interval.
        plugin.pulse.invalidate_caches()
        now[0] += 10
        await plugin.pulse._heartbeat()
        assert read_calls == reads_after_run + 1
        assert claim_calls == 2
        await plugin.pulse._heartbeat()
        assert read_calls == reads_after_run + 1
        assert claim_calls == 2

        # 4. Clock rewind resets _heartbeat_next_at so SQLite claim is checked again (not skipped by next_at)
        now[0] -= 500
        await plugin.pulse._heartbeat()
        assert claim_calls == 3
        assert read_calls == reads_after_run + 1

        # 5. background_tick=True throttles _longing to <= 1 call per 30s; direct tick() runs always
        longing_calls = 0

        async def count_longing():
            nonlocal longing_calls
            longing_calls += 1

        plugin.pulse._longing = count_longing
        plugin.pulse._heartbeat_next_at = now[0] + 99999
        await plugin.pulse.tick(background_tick=True)
        await plugin.pulse.tick(background_tick=True)
        now[0] += 10
        await plugin.pulse.tick(background_tick=True)
        assert longing_calls == 1

        now[0] += 25  # +35s total
        await plugin.pulse.tick(background_tick=True)
        assert longing_calls == 2

        # Direct tick() is never throttled
        await plugin.pulse.tick()
        await plugin.pulse.tick()
        assert longing_calls == 4
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_group_scroll_prompt_journal_dedup_and_serendipity_upgrade(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        now, clock = _make_fake_pulse_clock(1_700_000_000.0)
        plugin.intents.clock = clock

        # 1. Group return scroll prompt when messages_since_trigger >= 5 + group + source_msg_id + not admin_direct
        wakes = []

        async def capture_capability(name, **kwargs):
            if name == "maisaka.proactive.trigger":
                wakes.append((kwargs.get("stream_id"), kwargs.get("intent")))
            return {"success": True}

        plugin.ctx.call_capability = capture_capability

        class ScrollBridge:
            def __init__(self, items, claim_rows):
                self._items = items
                self._claim_rows = claim_rows
                self.statuses = []

            def claim_ready(self, enabled, *, plugin_enabled=None):
                rows = list(self._claim_rows)
                self._claim_rows.clear()
                return {"qq:group:100246809": rows} if rows else {}

            def current(self, rows, *, pending=False):
                return list(rows)

            def material(self, rows):
                return list(self._items)

            def redact(self, val):
                return val

            def wake_status(self, rows, status):
                self.statuses.append(status)

            def new_input(self, chat_id):
                return False

        orig_bridge = plugin.pulse.bridge
        orig_enabled = plugin.pulse.enabled
        plugin.pulse.enabled = lambda cid, plat="": True
        plugin.pulse.bridge = ScrollBridge(
            [
                {
                    "thread_id": "t1",
                    "source_msg_id": "m_orig_99",
                    "chat_type": "group",
                    "messages_since_trigger": 5,
                    "text": "结论 ready",
                }
            ],
            [{"chat_id": "qq:group:100246809", "platform": "qq", "group_id": "g1", "raw_grant": {}}],
        )
        await plugin.pulse._returns()
        assert len(wakes) == 1
        assert "当前群聊自原提问起已滚过多条新消息" in wakes[0][1]
        assert "ref_msg_ids=[source_msg_id]" in wakes[0][1]

        # Should NOT inject when messages_since_trigger < 5, private chat, or admin_direct
        for extra_item, grant in (
            ({"chat_type": "group", "messages_since_trigger": 4, "source_msg_id": "m1"}, {}),
            ({"chat_type": "private", "messages_since_trigger": 10, "source_msg_id": "m2"}, {}),
            ({"chat_type": "group", "messages_since_trigger": 10, "source_msg_id": ""}, {}),
            (
                {"chat_type": "group", "messages_since_trigger": 10, "source_msg_id": "m3"},
                {"admin_direct": True},
            ),
        ):
            wakes.clear()
            plugin.pulse.bridge = ScrollBridge(
                [extra_item],
                [{"chat_id": "qq:group:100246809", "platform": "qq", "group_id": "g1", "raw_grant": grant}],
            )
            await plugin.pulse._returns()
            assert len(wakes) == 1
            assert "当前群聊自原提问起已滚过多条新消息" not in wakes[0][1]
        plugin.pulse.bridge = orig_bridge
        plugin.pulse.enabled = orig_enabled

        # 2. Heartbeat injects <recent_journal> (last 600 chars, HTML-escaped, redacted)
        hb_path = Path(plugin.memory.root) / "memory/self/HEARTBEAT.md"
        hb_path.write_text(
            "## 气质与分寸\n温柔\n\n## 自主探索方向\n探索新鲜工具\n\n## 临时备忘\n",
            encoding="utf-8",
        )
        journal_prefix = "早期废话" * 200
        journal_tail = "旧主题<&> secret-tok 不要重复记录"
        plugin.memory.write("journal", journal_prefix + journal_tail)
        plugin.pulse.redact = lambda s: s.replace("secret-tok", "[SEC]")

        sent_prompts = []

        async def spy_step(profile, history, **_kwargs):
            sent_prompts.append(str(history))
            return {"response": "ok", "tool_calls": []}

        plugin.runtime.step = spy_step
        await plugin.after_receive(
            message=event("你好", user="u1", group=False, platform="qq")
        )
        plugin.pulse.invalidate_caches()
        await plugin.pulse._heartbeat()
        await asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True)
        assert sent_prompts
        hb_msg = sent_prompts[-1]
        assert "<recent_journal>" in hb_msg
        recent_block = hb_msg.split("<recent_journal>", 1)[1].split("</recent_journal>", 1)[0]
        assert "旧主题&lt;&amp;&gt; [SEC] 不要重复记录" in recent_block
        assert "secret-tok" not in recent_block
        assert len(recent_block) <= 640

        # 3. Upgraded propose_serendipity:
        #    - private chat enabled at "care" (not "quiet"), group requires "lively"
        #    - matches thread history even without MEMORY.md
        #    - filters generic CJK filler tokens
        p_care = "qq:private:100246801"
        p_quiet = "qq:private:100246802"
        g_care = "qq:group:100246803"
        g_lively = "qq:group:100246804"
        for cid, ctype in (
            (p_care, "private"),
            (p_quiet, "private"),
            (g_care, "group"),
            (g_lively, "group"),
        ):
            with plugin.threads.connect() as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO session_routes(session_id, platform, chat_type, group_id, user_id, account_id, scope, updated, human_count, last_human_at) VALUES (?, 'qq', ?, ?, ?, '', '', ?, 5, ?)",
                    (
                        cid,
                        ctype,
                        cid.split(":")[-1] if ctype == "group" else "",
                        cid.split(":")[-1] if ctype == "private" else "",
                        now[0],
                        now[0],
                    ),
                )
        plugin.settings.enabled_chats.extend([p_care, p_quiet, g_care, g_lively])
        plugin.pulse.enabled_chats = lambda: [p_care, p_quiet, g_care, g_lively]
        plugin.pulse.acquaintance_users = {
            "qq:100246801",
            "qq:100246802",
            "qq:100246803",
            "qq:100246804",
            "qq:u_ok",
        }
        plugin.pulse.chat_proactive_levels = {
            p_care: "care",
            p_quiet: "quiet",
            g_care: "care",
            g_lively: "lively",
        }

        # Populate thread history ONLY (no MEMORY.md) for p_care & p_quiet & g_care
        scout_prof = plugin.profiles.get("scout")
        plugin.threads.create(
            scout_prof,
            "最近想了解终端排版工具",
            chat_id=p_care,
            platform="qq",
            user_id="100246801",
        )
        plugin.threads.create(
            scout_prof,
            "终端排版工具也很赞",
            chat_id=p_quiet,
            platform="qq",
            user_id="100246802",
        )
        plugin.threads.create(
            scout_prof,
            "终端排版工具在群里聊过",
            chat_id=g_care,
            platform="qq",
            user_id="100246803",
        )
        plugin.threads.create(
            scout_prof,
            "今天刚发现一个可以看看尝试",
            chat_id=g_lively,
            platform="qq",
            user_id="100246804",
        )

        # Filler-only overlap must NOT match g_lively
        plugin.pulse.propose_serendipity(
            thread_id="self-1",
            due_at=now[0] + 60,
            reason="这个那个今天刚发现一个不错可以尝试看看",
        )
        assert plugin.intents.list(f"chat:{g_lively}") == []

        # Technical keyword matches p_care (private + care) without MEMORY.md,
        # while ignoring p_quiet and g_care; and _proactive_gate allows private care serendipity
        plugin.pulse.propose_serendipity(
            thread_id="self-2",
            due_at=now[0] + 120,
            reason="刚发现一个终端排版工具很适合整理笔记",
        )
        p_care_intents = plugin.intents.list(f"chat:{p_care}")
        assert len(p_care_intents) == 1
        assert plugin.intents.list(f"chat:{p_quiet}") == []
        assert plugin.intents.list(f"chat:{g_care}") == []
        assert plugin.pulse._proactive_gate(p_care_intents[0]) == (True, "")
        plugin.intents.cancel(p_care_intents[0]["intent_id"], f"chat:{p_care}")

        # 4. Per-arena proactive_quota_available compatibility in _longing()
        plugin.pulse.longing_quiet_hours = 72
        plugin.pulse.acquaintance_users = {"qq:u_ok"}
        for cid, uid in ((g_lively, "u_ok"), (p_care, "u_ok")):
            with plugin.threads.connect() as db, db:
                db.execute(
                    "UPDATE session_routes SET last_human_at=?, user_id=? WHERE session_id=?",
                    (now[0] - 80 * 3600, uid, cid),
                )

        # Wrap proactive_quota_available with 1-arg arena-scoped signature where g_lively is exhausted
        quota_calls = []

        def arena_quota(target="", *, now=None):
            quota_calls.append(target)
            return target != f"chat:{g_lively}"

        plugin.intents.proactive_quota_available = arena_quota
        plugin.pulse.enabled_chats = lambda: [g_lively, p_care]
        plugin.intents.schedule = MagicMock(wraps=plugin.intents.schedule)
        await plugin.pulse._longing()
        assert any(f"chat:{g_lively}" in str(c) for c in quota_calls)
        scheduled_targets = [
            call.kwargs["target"] for call in plugin.intents.schedule.call_args_list
        ]
        assert f"chat:{p_care}" in scheduled_targets
    finally:
        await plugin.on_unload()
