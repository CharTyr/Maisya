import asyncio
import dataclasses
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from test_foundation import event, loaded

from maisya.abilities import Profiles
from maisya.intents import Intents
from maisya.memory import MemoryLayers
from maisya.pulse import Pulse
from maisya.threads import ThreadStore
from maisya.vault import Vault


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0, offset_minutes: int = 480) -> float:
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=timezone(timedelta(minutes=offset_minutes)),
    ).timestamp()



def test_intents_tz_offset_day_bounds_and_minute_sleep_window(tmp_path):
    store = ThreadStore(tmp_path / "tz_intents.db")
    clock_now = [_ts(2026, 11, 16, 23, 30, 0)]

    default_intents = Intents(store, clock=lambda: clock_now[0])
    assert default_intents.tz_offset_minutes == 480

    with pytest.raises(ValueError):
        Intents(store, tz_offset_minutes=True)
    with pytest.raises(ValueError):
        Intents(store, tz_offset_minutes=-721)
    with pytest.raises(ValueError):
        Intents(store, tz_offset_minutes=841)

    tz_holder = [-300]
    dyn_intents = Intents(
        store,
        clock=lambda: clock_now[0],
        tz_offset_minutes=lambda: tz_holder[0],
    )

    # Minute precision overnight: 23:30-07:00 in UTC-5
    # 2026-11-17 04:29 UTC == 2026-11-16 23:29 UTC-5 (just before)
    # 2026-11-17 04:30 UTC == 2026-11-16 23:30 UTC-5 (inside)
    # 2026-11-17 11:59 UTC == 2026-11-17 06:59 UTC-5 (inside)
    # 2026-11-17 12:00 UTC == 2026-11-17 07:00 UTC-5 (outside)
    assert dyn_intents.in_sleep_window("23:30-07:00", now=_ts(2026, 11, 17, 4, 29, 0)) is False
    assert dyn_intents.in_sleep_window("23:30-07:00", now=_ts(2026, 11, 17, 4, 30, 0)) is True
    assert dyn_intents.in_sleep_window("23:30-07:00", now=_ts(2026, 11, 17, 11, 59, 0)) is True
    assert dyn_intents.in_sleep_window("23:30-07:00", now=_ts(2026, 11, 17, 12, 0, 0)) is False

    # Same UTC instant 04:30 UTC under UTC+8 is 12:30 -> NOT inside 23:30-07:00
    tz_holder[0] = 480
    assert dyn_intents.in_sleep_window("23:30-07:00", now=_ts(2026, 11, 17, 4, 30, 0)) is False

    # Same-day minute precision + hour shorthand + full-day 00-24 / 00:00-24:00
    assert dyn_intents.in_sleep_window("13:15-13:45", now=_ts(2026, 11, 16, 13, 14, 480)) is False
    assert dyn_intents.in_sleep_window("13:15-13:45", now=_ts(2026, 11, 16, 13, 15, 480)) is True
    assert dyn_intents.in_sleep_window("13:15-13:45", now=_ts(2026, 11, 16, 13, 44, 480)) is True
    assert dyn_intents.in_sleep_window("13:15-13:45", now=_ts(2026, 11, 16, 13, 45, 480)) is False
    assert dyn_intents.in_sleep_window("01-08", now=_ts(2026, 11, 16, 1, 30, 480)) is True
    assert dyn_intents.in_sleep_window("01-08", now=_ts(2026, 11, 16, 8, 0, 480)) is False

    for full_day in ("00-24", "0-24", "00:00-24:00", "00:00 - 24:00"):
        for hh, mm in ((0, 0), (12, 34), (23, 59)):
            assert dyn_intents.in_sleep_window(
                full_day, now=_ts(2026, 11, 16, hh, mm, 480)
            ) is True, (full_day, hh, mm)

    for invalid in ("", "00-00", "00:00-00:00", "24:00-24:00", "24:30-07:00", "25-07", "bad"):
        assert dyn_intents.in_sleep_window(
            invalid, now=_ts(2026, 11, 16, 3, 0, 480)
        ) is False, invalid

    # Day boundary follows configured tz_offset_minutes (attribute mutation works too):
    # 15:30 UTC == 23:30 +08:00 (Nov 16) vs 16:30 UTC == 00:30 +08:00 (Nov 17)
    t_nov16_1530_utc = _ts(2026, 11, 16, 15, 30, 0)
    t_nov16_1630_utc = _ts(2026, 11, 16, 16, 30, 0)
    clock_now[0] = t_nov16_1530_utc

    utc8 = Intents(store, clock=lambda: clock_now[0], daily_proactive_limit=1, tz_offset_minutes=480)
    utc8.schedule(
        kind="proactive",
        source="afterglow",
        target="chat:qq:group:100246809",
        due_at=t_nov16_1530_utc,
        reason="11月16日晚问候",
        payload={"platform": "qq"},
    )
    # In +08:00, 16:30 UTC is already Nov 17 -> quota available
    assert utc8.proactive_quota_available("chat:qq:group:100246809", now=t_nov16_1630_utc) is True
    # Switch dynamically to UTC+0 -> 15:30 UTC and 16:30 UTC are SAME UTC day (Nov 16)!
    utc8.tz_offset_minutes = 0
    assert utc8.proactive_quota_available("chat:qq:group:100246809", now=t_nov16_1630_utc) is False
    with pytest.raises(ValueError, match="每日上限"):
        utc8.schedule(
            kind="proactive",
            source="afterglow",
            target="chat:qq:group:100246809",
            due_at=t_nov16_1630_utc,
            reason="同UTC日应拒绝",
            payload={"platform": "qq"},
        )


@pytest.mark.asyncio
async def test_pulse_syncs_tz_offset_and_sleep_window_with_mock_compat(tmp_path):
    plugin = await loaded(tmp_path / "pulse_tz")
    try:
        # 2026-11-17 04:40 UTC == 2026-11-16 23:40 UTC-5 == 2026-11-17 12:40 UTC+8
        now = [_ts(2026, 11, 17, 4, 40, 0)]
        plugin.intents.clock = lambda: now[0]
        await plugin.after_receive(
            message=event("你好", user="friend", group=False, platform="qq")
        )
        with plugin.threads.connect() as db:
            chat_id = str(
                db.execute("SELECT session_id FROM session_routes LIMIT 1").fetchone()[
                    "session_id"
                ]
            )
        plugin.settings.enabled_chats.append(chat_id)

        plugin.pulse.sleep_window = "23:30-07:00"
        plugin.pulse.tz_offset_minutes = -300
        plugin.intents.tz_offset_minutes = -300

        wakes = []

        async def capability(name, **arguments):
            if name == "maisaka.proactive.trigger":
                wakes.append(arguments)
                return {"success": True}
            return {"success": True, "response": "ok", "tool_calls": []}

        plugin.ctx.call_capability = capability

        p_row = plugin.intents.schedule(
            kind="proactive",
            source="afterglow",
            target=f"chat:{chat_id}",
            due_at=now[0],
            reason="深夜测试",
            payload={"platform": "qq"},
            bypass_limits=True,
        )
        # 23:40 in UTC-5 is inside 23:30-07:00 -> proactive deferred & heartbeat skipped
        hb_path = Path(plugin.memory.root) / "memory/self/HEARTBEAT.md"
        hb_path.write_text(
            "## 气质与分寸\n温柔\n\n## 自主探索方向\n查看开源新闻\n",
            encoding="utf-8",
        )
        await asyncio.wait_for(plugin.pulse.tick(), timeout=3.0)
        assert plugin.intents.get(p_row["intent_id"])["status"] == "pending"
        assert plugin.intents.get(p_row["intent_id"])["due_at"] > now[0]
        assert wakes == []

        # Full-day 00:00-24:00 blocks heartbeat and defers even at midday
        plugin.pulse.tz_offset_minutes = 480
        plugin.intents.tz_offset_minutes = 480
        plugin.pulse.sleep_window = "00:00-24:00"
        assert plugin.intents.in_sleep_window("00:00-24:00", now=now[0]) is True
        await asyncio.wait_for(plugin.pulse._heartbeat(), timeout=3.0)
        assert plugin.engine.tasks == {}

        # _reschedule_recurring uses Pulse tz_offset_minutes fallback if payload omits tz_offset_minutes
        plugin.pulse.tz_offset_minutes = -300
        await plugin.pulse._reschedule_recurring(
            {
                "intent_id": "in_test_tz",
                "kind": "silent",
                "source": "workflow",
                "target": f"chat:{chat_id}",
                "reason": "每日定时",
                "payload": {
                    "profile": "scout",
                    "prompt": "查一查",
                    "daily_at": "08:00",
                    "every_seconds": 86400,
                    "scheduled_admin": True,
                },
            }
        )
        daily_rows = [
            it
            for it in plugin.intents.list(f"chat:{chat_id}")
            if (it.get("payload") or {}).get("daily_at") == "08:00"
        ]
        assert len(daily_rows) == 1
        assert daily_rows[0]["payload"]["tz_offset_minutes"] == -300

        # Mock Intents without tz_offset_minutes parameter must NOT TypeError
        class LegacyIntents:
            def __init__(self):
                self.seen = []

            def clock(self):
                return now[0]

            def in_sleep_window(self, sleep_window="", *, now=None):
                self.seen.append(("in_sleep_window", sleep_window))
                return False

            def social_gate(self, target, *, sleep_window="", now=None, aliases=()):
                self.seen.append(("social_gate", target, sleep_window))
                return True, ""

            def acquaintance_allowed(self, chat_id, *, admins=(), acquaintances=(), payload=None):
                return True

            def proactive_quota_available(self):
                return True

        legacy = LegacyIntents()
        compat_pulse = Pulse(
            plugin.bridge,
            plugin.ctx,
            lambda cid, p="": True,
            legacy,
            plugin.engine,
            sleep_window="23:30-07:00",
            tz_offset_minutes=-300,
            proactive_level="lively",
        )
        assert compat_pulse._proactive_gate(
            {
                "intent_id": "in_legacy",
                "kind": "proactive",
                "source": "afterglow",
                "target": f"chat:{chat_id}",
                "payload": {"platform": "qq"},
            }
        ) == (True, "")
        assert ("social_gate", f"chat:{chat_id}", "23:30-07:00") in legacy.seen
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_propose_serendipity_prechecks_daily_quota_by_due_at_and_arena(tmp_path):
    plugin = await loaded(tmp_path / "serendipity_quota")
    try:
        day1_now = _ts(2026, 11, 16, 10, 0, 480)
        day2_due = _ts(2026, 11, 17, 10, 0, 480)
        plugin.intents.clock = lambda: day1_now

        g_lively = "qq:group:100246809"
        p_care = "qq:private:100246801"
        for cid, ctype in ((g_lively, "group"), (p_care, "private")):
            with plugin.threads.connect() as db, db:
                db.execute(
                    "INSERT OR REPLACE INTO session_routes(session_id, platform, chat_type, group_id, user_id, account_id, scope, updated, human_count, last_human_at) "
                    "VALUES (?, 'qq', ?, ?, ?, '', '', ?, 5, ?)",
                    (
                        cid,
                        ctype,
                        "100246809" if ctype == "group" else "",
                        "100246801" if ctype == "private" else "",
                        day1_now,
                        day1_now,
                    ),
                )

        plugin.settings.enabled_chats.extend([g_lively, p_care])
        plugin.pulse.enabled_chats = lambda: [g_lively, p_care]
        plugin.pulse.acquaintance_users = {"qq:100246809", "qq:100246801"}
        plugin.pulse.chat_proactive_levels = {g_lively: "lively", p_care: "care"}
        plugin.memory.write("memory", "量子计算 终端排版 Rust 编译器", chat_id=g_lively)
        plugin.memory.write("memory", "Rust 笔记本", chat_id=p_care)

        orig_mem_read = plugin.memory.read
        orig_schedule = plugin.intents.schedule
        mem_reads = []
        schedule_calls = []

        def spy_mem_read(kind, **kwargs):
            mem_reads.append((kind, kwargs.get("chat_id")))
            return orig_mem_read(kind, **kwargs)

        def spy_schedule(*args, **kwargs):
            schedule_calls.append(kwargs.get("target"))
            return orig_schedule(*args, **kwargs)

        plugin.memory.read = spy_mem_read
        plugin.intents.schedule = spy_schedule

        # 1. Exhaust ONLY group quota on day1 -> g_lively must be skipped BEFORE memory.read,
        #    and p_care (even with fewer keyword hits!) gets scheduled cleanly.
        orig_schedule(
            kind="proactive",
            source="afterglow",
            target=f"chat:{g_lively}",
            due_at=day1_now + 60,
            reason="群今日占满",
            payload={"platform": "qq"},
        )
        mem_reads.clear()
        schedule_calls.clear()
        res_private = plugin.pulse.propose_serendipity(
            thread_id="s-day1-1",
            due_at=day1_now + 120,
            reason="量子计算 终端排版 Rust 编译器 笔记整理",
        )
        assert res_private["scheduled"] is True
        assert schedule_calls == [f"chat:{p_care}"]
        assert ("memory", g_lively) not in mem_reads
        assert ("memory", p_care) in mem_reads

        # 2. Now BOTH group and private arenas are full on day1 ->
        #    propose_serendipity on day1 early-returns with ZERO memory.read & ZERO schedule calls!
        mem_reads.clear()
        schedule_calls.clear()
        res_full = plugin.pulse.propose_serendipity(
            thread_id="s-day1-2",
            due_at=day1_now + 180,
            reason="Rust 编译器新版本发布",
        )
        assert res_full == {"scheduled": False}
        assert mem_reads == []
        assert schedule_calls == []

        # 3. Same full day1, but due_at targets day2 -> quota precheck uses due_at day -> succeeds!
        mem_reads.clear()
        schedule_calls.clear()
        res_day2 = plugin.pulse.propose_serendipity(
            thread_id="s-day2-1",
            due_at=day2_due,
            reason="量子计算 终端排版 Rust 编译器",
        )
        assert res_day2["scheduled"] is True
        assert schedule_calls == [f"chat:{g_lively}"]

        # 4. daily_proactive_limit == 0 -> immediate early exit even on fresh day
        plugin.intents.daily_proactive_limit = 0
        mem_reads.clear()
        schedule_calls.clear()
        res_zero = plugin.pulse.propose_serendipity(
            thread_id="s-day3",
            due_at=day2_due + 86400,
            reason="Rust 编译器",
        )
        assert res_zero == {"scheduled": False}
        assert mem_reads == []
        assert schedule_calls == []
    finally:
        await plugin.on_unload()


def test_all_bundled_presets_auto_upgrade_and_preserve_manual_edits(
    tmp_path, monkeypatch
):
    presets_root = Path(__file__).resolve().parents[1] / "presets"

    # Part 1: Unique default preset loads complete toolsets and preserves manual profile.toml edits
    profiles_dir = tmp_path / "abilities/profiles"
    upgraded_profiles = Profiles(profiles_dir)
    assert [p.id for p in upgraded_profiles.list_profiles()] == ["default", "digest", "scout"]
    default_prof = upgraded_profiles.get("default")
    assert "person_read" in default_prof.tools and "recall_read" in default_prof.tools
    assert "render_report" in default_prof.tools and "workspace_file" in default_prof.tools
    assert default_prof.skills == ()

    # Manual customization on default/profile.toml MUST be preserved
    custom_default_toml = (
        'title = "我的定制默认助手"\n'
        "enabled = false\n"
        'environment = "readonly"\n'
        'access = "everyone"\n'
        'tools = ["web_search"]\n'
    )
    (profiles_dir / "default/profile.toml").write_text(custom_default_toml, encoding="utf-8")
    reloaded_profiles = Profiles(profiles_dir)
    assert (profiles_dir / "default/profile.toml").read_text("utf-8") == custom_default_toml
    assert reloaded_profiles.get("default").tools == ("web_search",)
    assert reloaded_profiles.get("default").enabled is False

    # Part 2: MemoryLayers upgrades ALL bundled profiles/*/PROFILE.md and ALL skills/*/SKILL.md
    # when unmodified (no version >= 1), and preserves ANY customized asset (version >= 1)
    root = tmp_path / "mem_root"
    root.mkdir(parents=True, exist_ok=True)
    store = ThreadStore(root / "threads.db")
    profiles = Profiles(root / "abilities/profiles")
    vault = Vault(root / "vault")

    # Simulate pre-upgrade files for ALL bundled profiles and skills (no version >= 1 in DB)
    p_md = profiles.path / "default" / "PROFILE.md"
    p_md.write_text("# 老版本 default 说明\n", encoding="utf-8")
    # Historical unedited HEARTBEAT.md (exact commit 9d35d476 hash) should also auto-upgrade
    historical_heartbeat = (
        "## 气质与分寸\n"
        "只在确有新意、自然关联或恰当问候时开口；拿不准气氛时宁可静默收起。\n"
        "每次主动搭话保持一两句自然口语，不汇报后台步骤，不重复追问未获回应的话题。\n"
        "自身探索与近况只围绕公开主题，不带入任何具体会话或个人私密信息。\n"
        "\n## 自主探索方向\n"
        "<!-- 在下方写下闲暇时想浏览的公开主题；留空则不自动发起后台探索 -->\n"
    )
    (root / "memory/self").mkdir(parents=True, exist_ok=True)
    (root / "memory/self/HEARTBEAT.md").write_text(historical_heartbeat, encoding="utf-8")
    skills_dir = root / "abilities/skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for skill_dir in sorted((presets_root / "skills").iterdir()):
        if not skill_dir.is_dir():
            continue
        dst = skills_dir / skill_dir.name / "SKILL.md"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(f"---\nname: {skill_dir.name}\ndescription: old\n---\n# old\n", encoding="utf-8")

    # Also test oversized (>128KiB, <2MiB) unedited skill does NOT crash MemoryLayers init
    big_custom_skill = skills_dir / "research-report/SKILL.md"
    big_custom_skill.write_text(
        "---\nname: research-report\ndescription: legacy-big\n---\n" + ("B" * 160_000),
        encoding="utf-8",
    )
    mem_pre = MemoryLayers(root, store, profiles, vault, clock=lambda: 1000.0)

    # Bundled default PROFILE.md, all SKILL.md, and unedited HEARTBEAT.md should now equal current presets
    expected_default_md = (presets_root / "profiles/default/PROFILE.md").read_text("utf-8")
    assert (profiles.path / "default/PROFILE.md").read_text("utf-8") == expected_default_md
    for skill_dir in sorted((presets_root / "skills").iterdir()):
        if not skill_dir.is_dir():
            continue
        expected_skill = (skill_dir / "SKILL.md").read_text("utf-8")
        assert (skills_dir / skill_dir.name / "SKILL.md").read_text("utf-8") == expected_skill
    assert (root / "memory/self/HEARTBEAT.md").read_text("utf-8") == (
        presets_root / "memory/HEARTBEAT.md"
    ).read_text("utf-8")

    # User customizes default PROFILE.md, HEARTBEAT.md, and source-checking SKILL.md (creating version >= 1)
    v_hb = mem_pre.read("heartbeat")["version"]
    mem_pre.write(
        "heartbeat",
        "## 气质与分寸\n保持安静。\n\n## 自主探索方向\n- 关注本地天文动态\n",
        expected_version=v_hb,
    )
    v_default = mem_pre.read("profile", target="default", document="PROFILE.md", allow_custom=True)["version"]
    mem_pre.write(
        "profile",
        "# 用户手工定制的默认助手规则\n",
        target="default",
        document="PROFILE.md",
        expected_version=v_default,
        allow_custom=True,
    )
    v_sc = mem_pre.read("skill", target="source-checking")["version"]
    mem_pre.write(
        "skill",
        "---\nname: source-checking\ndescription: custom\n---\n# 用户定制核验清单\n",
        target="source-checking",
        expected_version=v_sc,
    )

    mem_post = MemoryLayers(root, store, profiles, vault, clock=lambda: 2000.0)
    assert "关注本地天文动态" in (root / "memory/self/HEARTBEAT.md").read_text("utf-8")
    assert "用户手工定制的默认助手规则" in (profiles.path / "default/PROFILE.md").read_text("utf-8")
    assert "用户定制核验清单" in mem_post.read("skill", target="source-checking")["content"]

    # Future preset skill with companion file + edited skill protection
    import maisya.memory as mem_mod

    synth_presets = tmp_path / "synth_presets"
    for sub in ("memory", "profiles/default", "skills/future-tool/references"):
        (synth_presets / sub).mkdir(parents=True, exist_ok=True)
    (synth_presets / "memory/HEARTBEAT.md").write_text(
        (presets_root / "memory/HEARTBEAT.md").read_text("utf-8"), encoding="utf-8"
    )
    (synth_presets / "profiles/default/PROFILE.md").write_text(
        "# new default preset\n", encoding="utf-8"
    )
    (synth_presets / "skills/future-tool/SKILL.md").write_text(
        "---\nname: future-tool\ndescription: new\n---\n# v2\n", encoding="utf-8"
    )
    (synth_presets / "skills/future-tool/references/guide.md").write_text(
        "guide-v2\n", encoding="utf-8"
    )
    monkeypatch.setattr(mem_mod, "PRESETS_DIR", synth_presets)
    synth_root = tmp_path / "synth_root"
    synth_root.mkdir(parents=True, exist_ok=True)
    synth_store = ThreadStore(synth_root / "threads.db")
    synth_profiles = Profiles(synth_root / "abilities/profiles")
    synth_vault = Vault(synth_root / "vault")
    # Existing old unedited companion file gets upgraded alongside SKILL.md
    ft_dir = synth_root / "abilities/skills/future-tool/references"
    ft_dir.mkdir(parents=True, exist_ok=True)
    (synth_root / "abilities/skills/future-tool/SKILL.md").write_text(
        "---\nname: future-tool\ndescription: old\n---\n# v1\n", encoding="utf-8"
    )
    (ft_dir / "guide.md").write_text("guide-v1\n", encoding="utf-8")
    synth_mem = MemoryLayers(
        synth_root, synth_store, synth_profiles, synth_vault, clock=lambda: 3000.0
    )
    assert "# v2" in (synth_root / "abilities/skills/future-tool/SKILL.md").read_text("utf-8")
    assert (ft_dir / "guide.md").read_text("utf-8") == "guide-v2\n"

    # Once customized (version >= 1), companion files and SKILL.md remain protected on next init
    v_ft = synth_mem.read("skill", target="future-tool")["version"]
    synth_mem.write(
        "skill",
        "---\nname: future-tool\ndescription: custom\n---\n# custom-v3\n",
        target="future-tool",
        expected_version=v_ft,
    )
    (ft_dir / "guide.md").write_text("guide-custom\n", encoding="utf-8")
    (synth_presets / "skills/future-tool/SKILL.md").write_text(
        "---\nname: future-tool\ndescription: newer\n---\n# v4\n", encoding="utf-8"
    )
    (synth_presets / "skills/future-tool/references/guide.md").write_text(
        "guide-v4\n", encoding="utf-8"
    )
    MemoryLayers(
        synth_root, synth_store, synth_profiles, synth_vault, clock=lambda: 3100.0
    )
    assert "# custom-v3" in (synth_root / "abilities/skills/future-tool/SKILL.md").read_text("utf-8")
    assert (ft_dir / "guide.md").read_text("utf-8") == "guide-custom\n"
