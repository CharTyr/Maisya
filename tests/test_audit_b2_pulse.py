import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_foundation import event, loaded

from maisya.abilities import Profile, Profiles
from maisya.bridge import Bridge
from maisya.intents import Intents
from maisya.learning import Learning
from maisya.memory import MemoryLayers
from maisya.pulse import Pulse
from maisya.threads import ThreadEngine, ThreadStore
from maisya.trust import Message, MessageStore
from maisya.vault import Vault


class _GateRuntime:
    def __init__(self, *, text="任务已完成", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def available(self, profile):
        return None

    async def step(self, profile, history, **_kwargs):
        self.calls.append((profile.id, history))
        if self.error is not None:
            raise self.error
        return {"success": True, "response": self.text, "tool_calls": []}

    async def call(self, name, arguments, context):
        return {"text": "ok"}


def _raw_ev(
    text: str,
    *,
    msg_id: str,
    session_id: str,
    user: str,
    platform: str = "qq",
    group: bool = False,
    group_id: str = "",
):
    ev = event(text, user=user, platform=platform, group=group)
    ev["message_id"] = msg_id
    ev["session_id"] = session_id
    if group and group_id:
        ev["message_info"]["group_info"]["group_id"] = group_id
    return ev


def _record_tool_events(store: ThreadStore, thread_id: str, calls: list[dict], results: list[dict]):
    with store.connect() as db, db:
        store.event(db, thread_id, "tools", 1, {"calls": calls, "results": results})


def _utc8_ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(
        year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=8))
    ).timestamp()


@pytest.mark.asyncio
async def test_returns_marks_inline_only_after_accepted_and_prevents_double_wake(
    tmp_path,
):
    store = ThreadStore(tmp_path / "maisya.db")
    clock = [1_795_000_000.0]
    runtime = _GateRuntime(text="第一轮调研已完成")
    bridge = Bridge(store, clock=lambda: clock[0])
    engine = ThreadEngine(store, runtime)

    wake_mode = ["rejected"]
    wakes = []

    async def capability(name, **arguments):
        if name == "maisaka.proactive.trigger":
            wakes.append(arguments)
            if wake_mode[0] == "rejected":
                return {"success": False, "error": "busy"}
            return {"success": True}
        return {"success": True}

    pulse = Pulse(
        bridge,
        SimpleNamespace(call_capability=capability),
        lambda *_: True,
    )
    try:
        route = await bridge.route("chat-a", "msg-user-1", 0)
        started = await asyncio.wait_for(
            engine.start(
                Profile(),
                "查一下最新进度",
                chat_id="chat-a",
                platform="qq",
                user_id="u1",
                owner="chat",
                wait_seconds=0,
                return_route=route,
            ),
            timeout=3.0,
        )
        await bridge.submitted(route)
        await asyncio.wait_for(engine.drain(), timeout=3.0)
        tid = started["thread_id"]

        # 1) Rejected wake MUST keep inline=0 so backoff retry still works
        clock[0] += 2.0
        await asyncio.wait_for(pulse.tick(), timeout=3.0)
        assert len(wakes) == 1
        with store.connect() as db:
            row_after_reject = db.execute(
                "SELECT inline, consumed FROM return_intents WHERE thread_id=?",
                (tid,),
            ).fetchone()
            assert row_after_reject is not None
            assert row_after_reject["inline"] == 0
            assert row_after_reject["consumed"] == 0

        # 2) Accepted wake MUST mark inline=1 so it cannot be re-claimed/double-fed
        wake_mode[0] = "accepted"
        clock[0] += 20.0
        await asyncio.wait_for(pulse.tick(), timeout=3.0)
        assert len(wakes) == 2
        with store.connect() as db:
            row_after_accept = db.execute(
                "SELECT inline, consumed FROM return_intents WHERE thread_id=?",
                (tid,),
            ).fetchone()
            group_row = db.execute(
                "SELECT wake_status FROM return_groups WHERE chat_id='chat-a'"
            ).fetchone()
            assert row_after_accept["inline"] == 1
            assert group_row["wake_status"] == "accepted"

        # 3) Subsequent tick never double-wakes even before consume() runs
        clock[0] += 100.0
        await asyncio.wait_for(pulse.tick(), timeout=3.0)
        assert len(wakes) == 2
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_corrupt_payload_deleted_profile_and_empty_silent_stop_looping(tmp_path):
    plugin = await loaded(tmp_path / "resilience")
    try:
        clock = [1_795_000_000.0]
        plugin.intents.clock = lambda: clock[0]

        # 1) Non-dict / invalid JSON payloads must not abort intents.due() for sibling rows
        with plugin.threads.connect() as db, db:
            for iid, raw_payload in (
                ("in_corrupt_list", "[]"),
                ("in_corrupt_null", "null"),
                ("in_corrupt_json", "{broken_json"),
            ):
                db.execute(
                    "INSERT INTO intents (intent_id, kind, source, target, due_at, reason, thread_id, payload, status, wake_status, created, handled) "
                    "VALUES (?, 'proactive', 'later', 'chat:chat', ?, '损坏意图', '', ?, 'pending', '', ?, NULL)",
                    (iid, clock[0] - 10, raw_payload, clock[0] - 20),
                )

        valid = plugin.intents.schedule(
            kind="proactive",
            source="later",
            target="chat:chat",
            due_at=clock[0] - 5,
            reason="正常提醒",
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        claimed = plugin.intents.due(enabled=lambda *_: True)
        assert any(it["intent_id"] == valid["intent_id"] for it in claimed)
        listed = plugin.intents.list_all(status="")
        assert all(isinstance(it["payload"], dict) for it in listed)
        assert plugin.intents.get("in_corrupt_list")["payload"] == {}
        assert plugin.intents.get("in_corrupt_json")["payload"] == {}

        # 2) Deleted profile recurring intents retire permanently as failed/profile_missing (no 900s loop)
        plugin.profiles.clone("scout", "ephemeral_role")
        del_workflow = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target="chat:chat",
            due_at=clock[0] - 1,
            reason="已删角色工作流",
            payload={
                "platform": "qq",
                "profile": "ephemeral_role",
                "prompt": "巡检",
                "every_seconds": 600,
                "scheduled_admin": True,
            },
            bypass_limits=True,
        )
        del_silent = plugin.intents.schedule(
            kind="silent",
            source="workflow",
            target="chat:chat",
            due_at=clock[0] - 1,
            reason="已删角色静默巡检",
            payload={
                "platform": "qq",
                "profile": "ephemeral_role",
                "prompt": "静默",
                "every_seconds": 600,
                "scheduled_admin": True,
            },
            bypass_limits=True,
        )
        # Also test existing silent continuation thread whose profile was subsequently deleted
        ghost_thread = plugin.threads.create(
            plugin.profiles.get("ephemeral_role"),
            "延续任务",
            chat_id="chat",
            platform="qq",
            user_id="",
        )
        del_cont = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target="chat:chat",
            due_at=clock[0] - 1,
            reason="续跑已删档案",
            thread_id=ghost_thread["thread_id"],
            payload={"platform": "qq", "every_seconds": 600, "scheduled_admin": True},
            bypass_limits=True,
        )
        plugin.profiles.delete("ephemeral_role")

        await asyncio.wait_for(plugin.pulse._intents(), timeout=3.0)
        for iid in (del_workflow["intent_id"], del_silent["intent_id"], del_cont["intent_id"]):
            got = plugin.intents.get(iid)
            assert got["status"] == "failed"
            assert got["wake_status"] == "profile_missing"
        # No rescheduled pending intent and no 900s deferred retry
        remaining = [
            it
            for it in plugin.intents.list("chat:chat")
            if it["intent_id"] not in {valid["intent_id"]}
        ]
        assert remaining == []

        # 3) Profile-less recurring _silent with closed thread or empty thread_id stops cleanly (no empty reschedule loop)
        closed_thread = plugin.threads.create(
            plugin.profiles.get("default"),
            "已关闭线程",
            chat_id="chat",
            platform="qq",
            user_id="",
        )
        plugin.threads.stop(closed_thread["thread_id"], "chat")
        assert plugin.threads.get(closed_thread["thread_id"], "chat")["state"] == "idle"
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='closed' WHERE thread_id=?",
                (closed_thread["thread_id"],),
            )
        silent_closed = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target="chat:chat",
            due_at=clock[0] - 1,
            reason="关闭后的空线程后续",
            thread_id=closed_thread["thread_id"],
            payload={"platform": "qq", "every_seconds": 600},
            bypass_limits=True,
        )
        silent_empty = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target="chat:chat",
            due_at=clock[0] - 1,
            reason="无线程且无档案",
            thread_id="",
            payload={"platform": "qq", "every_seconds": 600},
            bypass_limits=True,
        )
        await asyncio.wait_for(plugin.pulse._intents(), timeout=3.0)
        assert plugin.intents.get(silent_closed["intent_id"])["wake_status"] == "thread_closed"
        assert plugin.intents.get(silent_empty["intent_id"])["status"] in {"done", "failed"}
        pending_after = [
            it
            for it in plugin.intents.list("chat:chat")
            if (it.get("payload") or {}).get("every_seconds") == 600
        ]
        assert pending_after == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_recent_runs_skips_rejected_and_thread_error(tmp_path):
    plugin = await loaded(tmp_path / "recent_runs")
    try:
        clock = [_utc8_ts(2026, 11, 16, 10, 0)]
        plugin.intents.clock = lambda: clock[0]
        plugin.engine.store = plugin.threads
        await plugin.after_receive(
            message=event("开启巡检", user="admin", group=True, platform="qq")
        )
        with plugin.threads.connect() as db:
            chat_id = str(
                db.execute("SELECT session_id FROM session_routes LIMIT 1").fetchone()[
                    "session_id"
                ]
            )

        prev_seed = [{"at": 1, "summary": "上次正常结论"}]
        plugin.runtime.step = lambda *a, **kw: asyncio.sleep(
            0, result={"response": "本次错误输出不应进入历史", "tool_calls": []}
        )

        async def reject_wake(name, **kwargs):
            if name == "maisaka.proactive.trigger":
                return {"success": False, "error": "rejected"}
            return {"success": True}

        plugin.ctx.call_capability = reject_wake

        rej_id = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{chat_id}",
            due_at=clock[0],
            reason="被拒唤醒测试",
            payload={
                "profile": "scout",
                "prompt": "巡检",
                "every_seconds": 600,
                "scheduled_admin": True,
                "recent_runs": list(prev_seed),
            },
            bypass_limits=True,
        )["intent_id"]
        await asyncio.wait_for(plugin.pulse.tick(), timeout=3.0)
        await asyncio.wait_for(
            asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True),
            timeout=3.0,
        )
        clock[0] += 3
        await asyncio.wait_for(plugin.pulse.tick(), timeout=3.0)
        assert plugin.intents.get(rej_id)["status"] == "failed"
        rescheduled = plugin.intents.list(f"chat:{chat_id}")
        assert len(rescheduled) == 1
        assert rescheduled[0]["payload"].get("recent_runs") == prev_seed
        plugin.intents.cancel(rescheduled[0]["intent_id"], f"chat:{chat_id}")

        # Thread execution error (result.get("error")) must fail cleanly and NOT enter recent_runs
        async def fail_step(*a, **kw):
            raise RuntimeError("INTERNAL_ERR_DO_NOT_CACHE")

        plugin.runtime.step = fail_step
        wakes = []

        async def ok_wake(name, **kwargs):
            if name == "maisaka.proactive.trigger":
                wakes.append(kwargs)
                return {"success": True}
            return {"success": True}

        plugin.ctx.call_capability = ok_wake
        plugin.intents.note_received(chat_id)
        err_id = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{chat_id}",
            due_at=clock[0],
            reason="线程报错测试",
            payload={
                "profile": "scout",
                "prompt": "巡检",
                "every_seconds": 600,
                "scheduled_admin": True,
                "recent_runs": list(prev_seed),
            },
            bypass_limits=True,
        )["intent_id"]
        await asyncio.wait_for(plugin.pulse.tick(), timeout=3.0)
        await asyncio.wait_for(
            asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True),
            timeout=3.0,
        )
        clock[0] += 3
        await asyncio.wait_for(plugin.pulse.tick(), timeout=3.0)
        got_err = plugin.intents.get(err_id)
        assert got_err["status"] == "failed"
        assert got_err["wake_status"] == "thread_error"
        assert wakes == []
        rescheduled2 = plugin.intents.list(f"chat:{chat_id}")
        assert len(rescheduled2) == 1
        assert rescheduled2[0]["payload"].get("recent_runs") == prev_seed
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_daily_at_trigger_day_quota_and_runtime_gate_recheck(tmp_path):
    plugin = await loaded(tmp_path / "daily_quota")
    try:
        day1 = _utc8_ts(2026, 11, 16, 8, 0)
        day2_morning = _utc8_ts(2026, 11, 17, 9, 0)
        day2_noon = _utc8_ts(2026, 11, 17, 12, 0)
        clock = [day1]
        plugin.intents.clock = lambda: clock[0]

        await plugin.after_receive(
            message=event("早上好", user="Bob", group=True, platform="qq")
        )
        with plugin.threads.connect() as db:
            chat_id = str(
                db.execute("SELECT session_id FROM session_routes LIMIT 1").fetchone()[
                    "session_id"
                ]
            )

        # Day 1 takes its 1/1 quota slot
        day1_id = plugin.intents.schedule(
            kind="proactive",
            source="afterglow",
            target=f"chat:{chat_id}",
            due_at=day1 + 1200,
            reason="Day 1 事后惦记",
            payload={"platform": "qq"},
        )["intent_id"]
        # Day 1 is full, so another Day 1 proactive fails
        with pytest.raises(ValueError, match="每日上限"):
            plugin.intents.schedule(
                kind="proactive",
                source="later",
                target=f"chat:{chat_id}",
                due_at=day1 + 2400,
                reason="Day 1 超额",
                payload={"platform": "qq"},
            )
        # Mark Day 1 intent done so Day 2 due() advances directly to daily_id
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE intents SET status='done', wake_status='accepted' WHERE intent_id=?",
                (day1_id,),
            )

        # Scheduling profile-less daily_at for Day 2 succeeds because Day 2 trigger day has 0/1 used
        daily_id = plugin.intents.schedule(
            kind="proactive",
            source="later",
            target=f"chat:{chat_id}",
            due_at=day2_morning,
            reason="每日问候",
            payload={
                "platform": "qq",
                "daily_at": "09:00",
                "tz_offset_minutes": 480,
                "every_seconds": 86400,
            },
            bypass_limits=False,
        )["intent_id"]

        # A second proactive on Day 2 MUST fail against Day 2's trigger-day count
        with pytest.raises(ValueError, match="每日上限"):
            plugin.intents.schedule(
                kind="proactive",
                source="later",
                target=f"chat:{chat_id}",
                due_at=day2_noon,
                reason="Day 2 超额",
                payload={"platform": "qq"},
                bypass_limits=False,
            )

        # Advance to Day 2 08:00 and complete an earlier Day 2 proactive intent (simulating Day 2 slot consumption)
        clock[0] = _utc8_ts(2026, 11, 17, 8, 0)
        wakes = []

        async def ok_wake(name, **kwargs):
            if name == "maisaka.proactive.trigger":
                wakes.append(kwargs)
                return {"success": True}
            return {"success": True}

        plugin.ctx.call_capability = ok_wake
        with plugin.threads.connect() as db, db:
            db.execute(
                "INSERT INTO intents (intent_id, kind, source, target, due_at, reason, thread_id, payload, status, wake_status, created, handled, bypass_limits) "
                "VALUES ('in_day2_done', 'proactive', 'afterglow', ?, ?, 'Day 2 早先已发', '', '{\"platform\":\"qq\"}', 'done', 'accepted', ?, ?, 0)",
                (f"chat:{chat_id}", day2_morning - 1800, clock[0] - 10, clock[0]),
            )

        # Also simulate legacy daily_at without profile that had bypass_limits=1 in DB: must STILL be gated!
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE intents SET bypass_limits=1 WHERE intent_id=?", (daily_id,)
            )

        clock[0] = day2_morning + 1
        await asyncio.wait_for(plugin.pulse._intents(), timeout=3.0)
        got = plugin.intents.get(daily_id)
        assert got["status"] == "failed"
        assert got["wake_status"] == "daily_proactive_limit"
        assert wakes == []

        # Rescheduled Day 3 intent MUST NOT carry bypass_limits=True
        day3_pending = plugin.intents.list(f"chat:{chat_id}")
        day3_daily = [
            it for it in day3_pending if (it.get("payload") or {}).get("daily_at") == "09:00"
        ]
        assert len(day3_daily) == 1
        with plugin.threads.connect() as db:
            row_flag = db.execute(
                "SELECT bypass_limits FROM intents WHERE intent_id=?",
                (day3_daily[0]["intent_id"],),
            ).fetchone()
            assert row_flag["bypass_limits"] == 0

        # On Day 3 with 0 other proactive intents, daily_at does NOT self-block itself as claimed and succeeds
        clock[0] = _utc8_ts(2026, 11, 18, 9, 0, ) + 1
        await asyncio.wait_for(plugin.pulse._intents(), timeout=3.0)
        assert plugin.intents.get(day3_daily[0]["intent_id"])["status"] == "done"
        assert len(wakes) == 1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_heartbeat_ability_gate_before_cooldown_and_silent_continuation_scope(
    tmp_path,
):
    plugin = await loaded(tmp_path / "hb_and_silent")
    try:
        clock = [_utc8_ts(2026, 11, 16, 14, 0)]
        plugin.intents.clock = lambda: clock[0]
        plugin.pulse.proactive_level = "care"
        plugin.pulse.heartbeat_interval = 3600

        # Seed a clean HEARTBEAT plan
        plugin.memory.write("heartbeat", "## 自主探索方向\n探索开源工具", allow_custom=True)

        # Disable default -> _heartbeat MUST NOT claim cooldown in social_state or advance _heartbeat_next_at
        orig_get = plugin.profiles.get
        mode = ["disabled"]

        def fake_get(pid):
            prof = orig_get(pid)
            if pid != "default":
                return prof
            if mode[0] == "disabled":
                return type(prof)(**{**prof.__dict__, "enabled": False})
            if mode[0] == "listed":
                return type(prof)(
                    **{
                        **prof.__dict__,
                        "access": "listed",
                        "allow_chats": ("qq:group:100246809",),
                    }
                )
            return prof

        plugin.profiles.get = fake_get
        started = []

        async def spy_step(profile, history, **_kw):
            started.append(profile.id)
            return {
                "success": True,
                "response": "已探索",
                "tool_calls": [],
            }

        plugin.runtime.step = spy_step

        await asyncio.wait_for(plugin.pulse._heartbeat(), timeout=3.0)
        mode[0] = "listed"
        await asyncio.wait_for(plugin.pulse._heartbeat(), timeout=3.0)
        assert started == []
        with plugin.threads.connect() as db:
            assert (
                db.execute(
                    "SELECT 1 FROM social_state WHERE chat_id='__self_heartbeat__'"
                ).fetchone()
                is None
            )

        # Immediately restoring default to enabled+autonomous runs heartbeat without waiting interval
        mode[0] = "ok"
        await asyncio.wait_for(plugin.pulse._heartbeat(), timeout=3.0)
        await asyncio.wait_for(plugin.engine.drain(), timeout=3.0)
        assert started == ["default"]

        # _silent continuation MUST block listed/allow_chats/sandbox_targets/bound=False snapshots AND live restricted profiles
        plugin.profiles.get = orig_get
        await plugin.after_receive(
            message=event("群聊", user="Bob", group=True, platform="qq")
        )
        with plugin.threads.connect() as db:
            chat_id = str(
                db.execute("SELECT session_id FROM session_routes LIMIT 1").fetchone()[
                    "session_id"
                ]
            )
        t_listed = plugin.threads.create(
            type(orig_get("default"))(
                **{
                    **orig_get("default").__dict__,
                    "access": "listed",
                    "allow_chats": ("qq:group:100246809",),
                }
            ),
            "listed续跑",
            chat_id=chat_id,
            platform="qq",
            user_id="",
        )
        iid_listed = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target=f"chat:{chat_id}",
            due_at=clock[0],
            reason="继续listed线程",
            thread_id=t_listed["thread_id"],
            payload={"platform": "qq", "scheduled_admin": False},
            bypass_limits=True,
        )["intent_id"]

        # Live profile downgraded to listed even if thread snapshot was everyone
        plugin.profiles.clone("scout", "live_scoped")
        t_live = plugin.threads.create(
            plugin.profiles.get("live_scoped"),
            "曾是everyone",
            chat_id=chat_id,
            platform="qq",
            user_id="",
        )
        plugin.memory.write(
            "profile",
            plugin.memory.read("profile", target="live_scoped", document="profile.toml")[
                "content"
            ].replace('access = "everyone"', 'access = "listed"\nallow_chats = ["qq:group:100246809"]'),
            target="live_scoped",
            document="profile.toml",
            allow_custom=True,
        )
        iid_live = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target=f"chat:{chat_id}",
            due_at=clock[0],
            reason="活档降权拦截",
            thread_id=t_live["thread_id"],
            payload={"platform": "qq", "profile": "live_scoped", "scheduled_admin": False},
            bypass_limits=True,
        )["intent_id"]

        await asyncio.wait_for(plugin.pulse._intents(), timeout=3.0)
        assert plugin.intents.get(iid_listed)["status"] == "failed"
        assert plugin.intents.get(iid_listed)["wake_status"] == "fresh_authorization_required"
        assert plugin.intents.get(iid_live)["status"] == "failed"
        assert plugin.intents.get(iid_live)["wake_status"] == "fresh_authorization_required"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_unreplied_lock_synced_across_session_id_and_stream_spec_aliases(
    tmp_path,
):
    store = ThreadStore(tmp_path / "maisya.db")
    msg_store = MessageStore(tmp_path / "maisya.db")
    intents = Intents(store, daily_proactive_limit=2)
    clock = [1_795_000_000.0]
    intents.clock = lambda: clock[0]

    msg_store.record(
        Message.from_event(
            _raw_ev(
                "群里先聊两句",
                msg_id="m_g1",
                user="Bob",
                group=True,
                platform="qq",
                group_id="100246809",
                session_id="sid_grp_hex",
            )
        )
    )
    msg_store.record(
        Message.from_event(
            _raw_ev(
                "私聊先打招呼",
                msg_id="m_p1",
                user="Alice",
                group=False,
                platform="qq",
                session_id="sid_priv_hex",
            )
        )
    )

    # 1) mark_proactive on stream_spec blocks social_gate and longing_ready on session_id
    intents.mark_proactive("chat:qq:group:100246809")
    assert intents.social_gate("chat:sid_grp_hex") == (False, "unreplied_lock")
    assert intents.longing_ready("sid_grp_hex") is False

    # 2) note_received on session_id unlocks stream_spec too
    intents.note_received("sid_grp_hex", engaged=True)
    assert intents.social_gate("chat:qq:group:100246809") == (True, "")
    assert intents.social_gate("chat:sid_grp_hex") == (True, "")

    # 3) Reverse direction + private case sensitivity preserved
    intents.mark_proactive("chat:sid_priv_hex")
    assert intents.social_gate("chat:qq:private:Alice") == (False, "unreplied_lock")
    # Different casing must NOT unlock Alice
    intents.note_received("qq:private:alice", engaged=True)
    assert intents.social_gate("chat:sid_priv_hex") == (False, "unreplied_lock")
    intents.note_received("qq:private:Alice", engaged=True)
    assert intents.social_gate("chat:sid_priv_hex") == (True, "")


def test_memory_multi_alias_merge_search_and_old_journal_rollback(tmp_path):
    now = [_utc8_ts(2026, 11, 1, 12, 0)]
    vault = Vault(tmp_path / "vault")
    profiles = Profiles(tmp_path / "abilities/profiles")
    store = ThreadStore(tmp_path / "maisya.db")
    memory = MemoryLayers(tmp_path, store, profiles, vault, clock=lambda: now[0])

    legacy_spec = "qq:group:100246809"
    active_sid = "sid_active_100246809"
    alias_group = [legacy_spec, active_sid]

    # 1) Write older content to legacy_spec, newer content to active_sid BEFORE session_routes links them
    memory.write("memory", "老群规：禁止发测试刷屏", chat_id=legacy_spec)
    now[0] += 600
    memory.write("memory", "最新偏好：只看开源和独立游戏", chat_id=active_sid)
    memory.chat_aliases = lambda cid: alias_group if cid in alias_group else [cid]

    # Reading via either alias AND context() MUST include BOTH without dropping older alias
    for q in (active_sid, legacy_spec):
        merged = memory.read("memory", chat_id=q)["content"]
        assert "最新偏好：只看开源和独立游戏" in merged
        assert "老群规：禁止发测试刷屏" in merged
    ctx_mem = memory.context(active_sid)["memory"]
    assert "最新偏好：只看开源和独立游戏" in ctx_mem
    assert "老群规：禁止发测试刷屏" in ctx_mem

    # Unindexed on-disk losing alias written out-of-band MUST be found by search()
    disk_extra_sid = "sid_disk_losing"
    alias_group.append(disk_extra_sid)
    _, extra_path = memory._memory_path_for(disk_extra_sid)
    extra_path.parent.mkdir(parents=True, exist_ok=True)
    extra_path.write_text("神秘口令：星际漫游指南", encoding="utf-8")
    hits = memory.search("星际漫游指南", active_sid, kind="memory")
    assert hits and any("星际漫游指南" in h["content"] for h in hits)

    # 2) Rollback > 7 days old JOURNAL.md must survive next context() calls and search()
    v0 = memory.write("journal", "我在老城书店发现一本绝版画册。", allow_custom=True)
    now[0] += 10 * 86400  # 10 days later -> expires in context()
    assert memory.context(active_sid)["journal"] == ""
    expired = memory.read("journal")
    assert expired["content"] == ""

    restored = memory.rollback(
        "journal", v0["version"], expected_version=expired["version"]
    )
    assert "绝版画册" in restored["content"]
    # Multiple context() ticks must NOT wipe out the rolled-back journal entry
    assert "绝版画册" in memory.context(active_sid)["journal"]
    assert "绝版画册" in memory.context(active_sid)["journal"]
    assert memory.search("绝版画册", active_sid, kind="journal")

    # 3) Rollback without explicit allow_custom keeps working for notes containing non-secret group words while blocking real secrets
    vault.set("secret_key", "super-secret-value-xyz")
    n0 = memory.write(
        "notes",
        "先检查返回码再重试。",
        target="scout",
    )
    n1 = memory.write(
        "notes",
        "本群专用提示覆盖",
        target="scout",
        expected_version=n0["version"],
        allow_custom=True,
    )
    n2 = memory.write(
        "notes",
        "临时清空",
        target="scout",
        expected_version=n1["version"],
    )
    rb_notes = memory.rollback("notes", n1["version"], target="scout", expected_version=n2["version"])
    assert "本群专用提示覆盖" in rb_notes["content"]


@pytest.mark.asyncio
async def test_learning_fenced_json_scope_isolation_and_transient_retry(
    tmp_path, caplog
):
    vault = Vault(tmp_path / "vault")
    profiles = Profiles(tmp_path / "abilities/profiles")
    store = ThreadStore(tmp_path / "maisya.db")
    memory = MemoryLayers(tmp_path, store, profiles, vault)

    # 1) Scoped 'everyone' profile (allow_chats / allow_users) MUST skip NOTES.md & _experience
    profiles.clone("scout", "scoped_chat")
    memory.write(
        "profile",
        memory.read("profile", target="scoped_chat", document="profile.toml")["content"].replace(
            'access = "everyone"', 'access = "everyone"\nallow_chats = ["qq:group:100246809"]'
        ),
        target="scoped_chat",
        document="profile.toml",
        allow_custom=True,
    )
    scoped_row = store.create(
        profiles.get("scoped_chat"),
        "scoped",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="",
        owner="self",
    )
    _record_tool_events(
        store,
        scoped_row["thread_id"],
        [
            {"id": "c1", "function": {"name": "web_search"}},
            {"id": "c2", "function": {"name": "web_search"}},
        ],
        [
            {"call_id": "c1", "value": {"error": "timeout"}},
            {"call_id": "c2", "value": {"ok": True}},
        ],
    )
    memory.experience({**scoped_row, "completed_revision": 1, "result": {"text": "私有群结果不要公开"}})
    assert memory.read("journal")["content"] == ""

    llm_calls = []

    async def should_not_call(*a, **kw):
        llm_calls.append((a, kw))
        return {"response": '{"note": "不应写入"}'}

    learning_skip = Learning(SimpleNamespace(call_capability=should_not_call), memory)
    await asyncio.wait_for(
        learning_skip.schedule(
            {**scoped_row, "completed_revision": 1, "result": {"text": "完成"}}
        ),
        timeout=3.0,
    )
    await asyncio.wait_for(learning_skip.drain(), timeout=3.0)
    assert llm_calls == []
    assert memory.read("notes", target="scoped_chat")["content"] == ""

    # 2) {"ok": False} -> {"ok": True} recovery detection + transient TimeoutError -> interrupted -> retry with fenced JSON -> applied
    normal_row = store.create(
        profiles.get("default"),
        "normal",
        chat_id="chat-learn",
        platform="qq",
        user_id="",
    )
    _record_tool_events(
        store,
        normal_row["thread_id"],
        [
            {"id": "d1", "function": {"name": "digest_feed"}},
            {"id": "d2", "function": {"name": "digest_feed"}},
        ],
        [
            {"call_id": "d1", "value": {"ok": False, "reason": "bad param"}},
            {"call_id": "d2", "value": {"ok": True}},
        ],
    )

    llm_mode = ["transient"]

    async def flaky_llm(name, **kwargs):
        if llm_mode[0] == "transient":
            raise TimeoutError("transient timeout")
        if llm_mode[0] == "fenced":
            return {"response": '```json\n{"note": "先校验订阅参数再调用。"}\n```'}
        if llm_mode[0] == "note2":
            return {"response": '```json\n{"note": "接口异常时先退避再重试。"}\n```'}
        return {"response": "not valid json at all"}

    learning = Learning(SimpleNamespace(call_capability=flaky_llm), memory)
    row_input = {**normal_row, "completed_revision": 1, "result": {"text": "恢复完成"}}
    caplog.set_level(logging.WARNING)
    await asyncio.wait_for(learning.schedule(row_input), timeout=3.0)
    await asyncio.wait_for(learning.drain(), timeout=3.0)
    with store.connect() as db:
        status1 = db.execute(
            "SELECT status FROM learning_receipts WHERE thread_id=? AND revision=1",
            (normal_row["thread_id"],),
        ).fetchone()["status"]
    assert status1 == "interrupted"

    # Retry on next schedule() succeeds and parses fenced JSON
    llm_mode[0] = "fenced"
    await asyncio.wait_for(learning.schedule(row_input), timeout=3.0)
    await asyncio.wait_for(learning.drain(), timeout=3.0)
    assert "先校验订阅参数再调用" in memory.read("notes", target="default")["content"]
    with store.connect() as db:
        status2 = db.execute(
            "SELECT status FROM learning_receipts WHERE thread_id=? AND revision=1",
            (normal_row["thread_id"],),
        ).fetchone()["status"]
    assert status2 == "applied"

    # 3) Version conflict on memory.write -> interrupted (retryable); genuinely malformed JSON -> rejected
    row2 = store.create(
        profiles.get("default"), "conflict", chat_id="chat-learn", platform="qq", user_id=""
    )
    _record_tool_events(
        store,
        row2["thread_id"],
        [
            {"id": "x1", "function": {"name": "web_search"}},
            {"id": "x2", "function": {"name": "web_search"}},
        ],
        [
            {"call_id": "x1", "value": {"error": "fail"}},
            {"call_id": "x2", "value": {"text": "ok"}},
        ],
    )
    orig_write = memory.write
    fail_once = [True]

    def conflict_write(*a, **kw):
        if fail_once[0]:
            fail_once[0] = False
            raise ValueError("版本已变化，请重新读取再修改")
        return orig_write(*a, **kw)

    memory.write = conflict_write
    llm_mode[0] = "note2"
    row2_input = {**row2, "completed_revision": 1, "result": {"text": "完成"}}
    await asyncio.wait_for(learning.schedule(row2_input), timeout=3.0)
    await asyncio.wait_for(learning.drain(), timeout=3.0)
    with store.connect() as db:
        st_conflict = db.execute(
            "SELECT status FROM learning_receipts WHERE thread_id=? AND revision=1",
            (row2["thread_id"],),
        ).fetchone()["status"]
    assert st_conflict == "interrupted"

    # Bad JSON permanently marks rejected
    memory.write = orig_write
    row3 = store.create(
        profiles.get("default"), "badjson", chat_id="chat-learn", platform="qq", user_id=""
    )
    _record_tool_events(
        store,
        row3["thread_id"],
        [
            {"id": "b1", "function": {"name": "web_search"}},
            {"id": "b2", "function": {"name": "web_search"}},
        ],
        [
            {"call_id": "b1", "value": {"error": "fail"}},
            {"call_id": "b2", "value": {"text": "ok"}},
        ],
    )
    llm_mode[0] = "invalid"
    await asyncio.wait_for(
        learning.schedule({**row3, "completed_revision": 1, "result": {"text": "完成"}}),
        timeout=3.0,
    )
    await asyncio.wait_for(learning.drain(), timeout=3.0)
    with store.connect() as db:
        st_bad = db.execute(
            "SELECT status FROM learning_receipts WHERE thread_id=? AND revision=1",
            (row3["thread_id"],),
        ).fetchone()["status"]
    assert st_bad == "rejected"
