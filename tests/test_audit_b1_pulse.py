import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from test_foundation import loaded

from maisya.abilities import Profiles
from maisya.memory import MemoryLayers
from maisya.threads import ThreadStore
from maisya.vault import Vault


def _utc8_ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    return datetime(
        year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=8))
    ).timestamp()


@pytest.mark.asyncio
async def test_silent_start_and_send_when_engine_full_retain_intent_and_kick_queued(
    tmp_path,
):
    plugin = await loaded(tmp_path / "silent_full")
    clock = [_utc8_ts(2026, 11, 16, 14, 0)]
    plugin.intents.clock = lambda: clock[0]
    plugin.bridge.clock = lambda: clock[0]
    plugin.engine.max_active = 1

    gate = asyncio.Event()
    entered = asyncio.Event()
    prompts = []

    async def capability(name, **arguments):
        prompts.append((name, arguments.get("prompt", "")))
        if len(prompts) == 1:
            entered.set()
            await gate.wait()
        return {"success": True, "response": "任务完成", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        scout = plugin.profiles.get("scout")
        await plugin.engine.start(
            scout,
            "blocker task",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
            wait_seconds=0,
            background=True,
        )
        await entered.wait()
        assert len(plugin.engine.tasks) == 1

        # 1. New _silent thread while engine is full MUST defer with already_started + thread_id
        intent = plugin.intents.schedule(
            kind="silent",
            source="workflow",
            target="chat:chat",
            due_at=clock[0],
            reason="新巡检任务",
            payload={"profile": "scout", "prompt": "新线程巡检", "every_seconds": 600},
            bypass_limits=True,
        )
        await plugin.pulse.tick()
        deferred = plugin.intents.get(intent["intent_id"])
        assert deferred["status"] == "pending", deferred
        assert deferred["thread_id"], deferred
        assert deferred["payload"].get("already_started") is True
        queued_tid = deferred["thread_id"]
        assert queued_tid not in plugin.engine.tasks
        row = plugin.threads.get(queued_tid, "chat")
        assert row["processed_revision"] == 0 and row["revision"] == 1
        pending_after_defer = [
            r
            for r in plugin.intents.list(target="chat:chat")
            if r["kind"] == "silent" and r["status"] == "pending"
        ]
        assert len(pending_after_defer) == 1

        # Still full after +15s -> kick_queued returns False -> remains pending
        clock[0] += 16
        await plugin.pulse.tick()
        still_deferred = plugin.intents.get(intent["intent_id"])
        assert still_deferred["status"] == "pending"

        # Release blocker slot -> next tick kicks queued thread to completion and reschedules
        gate.set()
        await plugin.engine.drain()
        assert plugin.threads.get(queued_tid, "chat")["completed_revision"] == 0

        clock[0] += 16
        await plugin.pulse.tick()
        await plugin.engine.drain()
        assert plugin.intents.get(intent["intent_id"])["status"] == "done"
        assert plugin.threads.get(queued_tid, "chat")["completed_revision"] == 1
        next_rows = [
            r
            for r in plugin.intents.list(target="chat:chat")
            if r["kind"] == "silent"
            and r["status"] == "pending"
            and r["intent_id"] != intent["intent_id"]
        ]
        assert len(next_rows) == 1
        assert "already_started" not in next_rows[0]["payload"]

        # 2. _silent send to existing idle thread while engine is full also defers cleanly
        gate.clear()
        entered.clear()
        saved_len = len(prompts)
        prompts.clear()
        await plugin.engine.start(
            scout,
            "blocker-2",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
            wait_seconds=0,
            background=True,
        )
        await entered.wait()

        follow_intent = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target="chat:chat",
            due_at=clock[0],
            reason="续跑线程",
            thread_id=queued_tid,
            payload={},
            bypass_limits=True,
        )
        await plugin.pulse.tick()
        follow_row = plugin.intents.get(follow_intent["intent_id"])
        assert follow_row["status"] == "pending", follow_row
        assert follow_row["payload"].get("already_started") is True
        assert plugin.threads.get(queued_tid, "chat")["revision"] == 2
        assert plugin.threads.get(queued_tid, "chat")["completed_revision"] == 1

        gate.set()
        await plugin.engine.drain()
        clock[0] += 16
        await plugin.pulse.tick()
        await plugin.engine.drain()
        assert plugin.intents.get(follow_intent["intent_id"])["status"] == "done"
        assert plugin.threads.get(queued_tid, "chat")["completed_revision"] == 2
        assert saved_len >= 1
    finally:
        gate.set()
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_heartbeat_when_engine_full_does_not_stack_orphans_and_kicks_queued(
    tmp_path,
):
    plugin = await loaded(tmp_path / "hb_full")
    clock = [_utc8_ts(2026, 11, 16, 15, 0)]
    plugin.intents.clock = lambda: clock[0]
    plugin.bridge.clock = lambda: clock[0]
    plugin.engine.max_active = 1
    plugin.pulse.heartbeat_interval = 60

    hb_path = plugin.memory.root / "memory/self/HEARTBEAT.md"
    hb_path.write_text(
        "## 气质与分寸\n轻声\n\n## 自主探索方向\n查看今日站内动态\n\n## 临时备忘\n",
        encoding="utf-8",
    )

    gate = asyncio.Event()
    entered = asyncio.Event()
    seen_prompts = []

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            seen_prompts.append(str(arguments.get("prompt", "")))
            if len(seen_prompts) == 1:
                entered.set()
                await gate.wait()
            return {"success": True, "response": "探索记录完毕", "tool_calls": []}
        return {"success": True}

    plugin.ctx.call_capability = capability
    try:
        # Check ability.autonomous before _claim_heartbeat so non-autonomous does not poison cooldown
        cur_toml = plugin.memory.read("profile", target="default", document="profile.toml")
        original_toml = cur_toml["content"]
        admins_toml = (
            original_toml.replace('access = "everyone"', 'access = "admins"')
            if "access =" in original_toml
            else original_toml + '\naccess = "admins"\n'
        )
        plugin.memory.write(
            "profile",
            admins_toml,
            target="default",
            document="profile.toml",
            expected_version=cur_toml["version"],
            allow_custom=True,
        )
        await plugin.pulse._heartbeat()
        with plugin.intents.store.connect() as db:
            claimed_row = db.execute(
                "SELECT last_proactive FROM social_state WHERE chat_id='__self_heartbeat__'"
            ).fetchone()
        assert claimed_row is None

        # Restore autonomous profile
        cur_toml2 = plugin.memory.read("profile", target="default", document="profile.toml")
        plugin.memory.write(
            "profile",
            original_toml,
            target="default",
            document="profile.toml",
            expected_version=cur_toml2["version"],
            allow_custom=True,
        )
        plugin.pulse.invalidate_caches()

        scout = plugin.profiles.get("default")
        await plugin.engine.start(
            scout,
            "busy blocker",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
            wait_seconds=0,
            background=True,
        )
        await entered.wait()

        # First heartbeat attempt while engine full queues exactly 1 self thread
        await plugin.pulse._heartbeat()
        with plugin.threads.connect() as db:
            hb_threads = db.execute(
                "SELECT thread_id, state, revision, processed_revision FROM agent_threads WHERE owner='self'"
            ).fetchall()
        assert len(hb_threads) == 1
        hb_tid = hb_threads[0]["thread_id"]
        assert hb_tid not in plugin.engine.tasks
        assert hb_threads[0]["processed_revision"] == 0

        # Advancing clock past interval + invalidating caches while still full MUST NOT stack orphans
        for _ in range(3):
            clock[0] += 120
            plugin.pulse.invalidate_caches()
            await plugin.pulse._heartbeat()
        with plugin.threads.connect() as db:
            hb_threads_after = db.execute(
                "SELECT thread_id FROM agent_threads WHERE owner='self'"
            ).fetchall()
        assert len(hb_threads_after) == 1

        # Free capacity -> next _heartbeat (even without waiting another interval) kicks queued thread
        gate.set()
        await plugin.engine.drain()
        clock[0] += 5
        await plugin.pulse._heartbeat()
        await plugin.engine.drain()
        row = plugin.threads.get(hb_tid, "self", allow_self=True)
        assert row["completed_revision"] == 1
        assert row["state"] == "idle"
        assert any("查看今日站内动态" in p for p in seen_prompts[1:])
    finally:
        gate.set()
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_return_routed_when_engine_full_retains_intent_kicks_and_delivers_return(
    tmp_path,
):
    plugin = await loaded(tmp_path / "rr_full", permissions=["qq:Alice"])
    clock = [_utc8_ts(2026, 11, 16, 15, 30)]
    plugin.intents.clock = lambda: clock[0]
    plugin.bridge.clock = lambda: clock[0]
    plugin.engine.max_active = 1
    plugin.messages.upsert_route(
        "chat",
        platform="qq",
        chat_type="group",
        group_id="100246809",
    )
    plugin.messages.upsert_route(
        "priv_admin_md5",
        platform="qq",
        chat_type="private",
        user_id="Alice",
    )

    gate = asyncio.Event()
    entered = asyncio.Event()
    cap_calls = []

    async def capability(name, **arguments):
        cap_calls.append((name, arguments))
        if name == "maisaka.proactive.trigger":
            return {"success": True, "queued": True}
        if name == "llm.generate_with_tools":
            if len([c for c in cap_calls if c[0] == "llm.generate_with_tools"]) == 1:
                entered.set()
                await gate.wait()
            return {"success": True, "response": "跨会话试跑结论", "tool_calls": []}
        return {"success": True}

    plugin.ctx.call_capability = capability
    try:
        scout = plugin.profiles.get("scout")
        await plugin.engine.start(
            scout,
            "occupy capacity",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
            wait_seconds=0,
            background=True,
        )
        await entered.wait()

        intent = plugin.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:priv_admin_md5",
            due_at=clock[0],
            reason="跨会话试跑预览",
            payload={
                "platform": "qq",
                "profile": "scout",
                "thread_chat_id": "chat",
                "scheduled_admin": True,
                "admin_direct": True,
                "source_msg_id": "admin-preview-1",
                "prompt": "查一下状态并预览回传",
                "every_seconds": 600,
            },
            bypass_limits=True,
        )
        await plugin.pulse.tick()

        # Must NOT prematurely mark done/return_routed while thread is queued and unexecuted
        deferred = plugin.intents.get(intent["intent_id"])
        assert deferred["status"] == "pending", deferred
        assert deferred["wake_status"] != "return_routed"
        queued_tid = deferred["thread_id"]
        assert queued_tid
        assert queued_tid not in plugin.engine.tasks
        assert plugin.threads.get(queued_tid, "chat")["processed_revision"] == 0

        # Release blocker, advance past defer delay -> kick_queued runs -> Bridge delivers return
        gate.set()
        await plugin.engine.drain()
        clock[0] += 10
        await plugin.pulse.tick()
        await plugin.engine.drain()
        clock[0] += 2
        await plugin.pulse.tick()

        finished = plugin.intents.get(intent["intent_id"])
        assert finished["status"] == "done" and finished["wake_status"] == "return_routed"
        assert plugin.threads.get(queued_tid, "chat")["completed_revision"] == 1
        wakes = [kw for name, kw in cap_calls if name == "maisaka.proactive.trigger"]
        assert any(kw["stream_id"] == "priv_admin_md5" and kw["reason"] == "maisya:return" for kw in wakes)
        # Recurring copy must not inherit return_routed_queued
        next_rows = [
            r
            for r in plugin.intents.list(target="chat:priv_admin_md5")
            if r["status"] == "pending" and r["intent_id"] != intent["intent_id"]
        ]
        assert len(next_rows) == 1
        assert "return_routed_queued" not in next_rows[0]["payload"]
    finally:
        gate.set()
        await plugin.on_unload()


def test_journal_utc8_midnight_preserved_and_crlf_trailing_spaces(tmp_path):
    # 2026-11-16 01:30:00 +08:00 == 2026-11-15 17:30:00 UTC (the 00:00-08:00 UTC+8 hazard window)
    now = [_utc8_ts(2026, 11, 16, 1, 30)]
    root = tmp_path / "mem_tz"
    root.mkdir(parents=True, exist_ok=True)
    store = ThreadStore(root / "threads.db")
    profiles = Profiles(root / "abilities/profiles")
    vault = Vault(root / "vault")

    memory = MemoryLayers(root, store, profiles, vault, clock=lambda: now[0])
    memory.write("journal", "凌晨散步看到萤火虫")
    assert memory.read("journal")["content"].startswith("## 2026-11-16\n")
    ctx = memory.context("chat")
    assert "凌晨散步看到萤火虫" in ctx["journal"]
    assert "凌晨散步看到萤火虫" in memory.read("journal")["content"]

    # External edit using CRLF and trailing whitespace after header date
    cur = memory.read("journal")
    memory.write(
        "journal",
        "## 2026-11-16   \t\r\n- 凌晨手账CRLF\r\n\r\n## 2026-11-15 \r\n- 昨天记录\r\n",
        expected_version=cur["version"],
    )
    ctx_crlf = memory.context("chat")
    assert "凌晨手账CRLF" in ctx_crlf["journal"]
    assert "昨天记录" in ctx_crlf["journal"]
    assert "凌晨手账CRLF" in memory.read("journal")["content"]

    # Custom timezone offset (-300 == UTC-5) + +24h future tolerance + expiry after 8 days
    now_ny = datetime(
        2026, 11, 16, 1, 30, tzinfo=timezone(timedelta(minutes=-300))
    ).timestamp()
    ny_root = tmp_path / "mem_ny"
    ny_root.mkdir(parents=True, exist_ok=True)
    mem_ny = MemoryLayers(
        ny_root,
        ThreadStore(ny_root / "threads.db"),
        Profiles(ny_root / "abilities/profiles"),
        Vault(ny_root / "vault"),
        clock=lambda: now_ny,
        tz_offset_minutes=-300,
    )
    mem_ny.write("journal", "纽约凌晨笔记")
    assert mem_ny.read("journal")["content"].startswith("## 2026-11-16\n")
    assert "纽约凌晨笔记" in mem_ny.context("")["journal"]

    # UTC reader encountering UTC+8 today header at 17:30Z (+6.5h ahead, within +24h) keeps entry,
    # while truly far-future header (> +24h) is filtered
    utc_root = tmp_path / "mem_utc"
    utc_root.mkdir(parents=True, exist_ok=True)
    mem_utc = MemoryLayers(
        utc_root,
        ThreadStore(utc_root / "threads.db"),
        Profiles(utc_root / "abilities/profiles"),
        Vault(utc_root / "vault"),
        clock=lambda: datetime(2026, 11, 15, 17, 30, tzinfo=UTC).timestamp(),
        tz_offset_minutes=0,
    )
    mem_utc.write("journal", "## 2026-11-16\n- 东八区同日\n\n## 2026-11-25\n- 远期未来")
    utc_ctx = mem_utc.context("")["journal"]
    assert "东八区同日" in utc_ctx
    assert "远期未来" not in utc_ctx

    # 8 days later, UTC+8 midnight entry expires cleanly
    now[0] += 8 * 86400
    assert memory.context("chat")["journal"] == ""


def test_profile_export_2mib_skill_and_read_skill_file_preset_fallback(tmp_path):
    src = tmp_path / "src"
    profiles = Profiles(src / "abilities/profiles")
    store = ThreadStore(src / "threads.db")
    vault = Vault(src / "vault")
    memory = MemoryLayers(src, store, profiles, vault)

    large_doc = b"# Big Companion\n" + (b"x" * (200 * 1024))
    large_skill_md = b"---\nname: big-skill\ndescription: large\n---\n" + (
        b"y" * (160 * 1024)
    )
    skill_dir = src / "abilities/skills/big-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(large_skill_md)
    (skill_dir / "references/guide.md").write_bytes(large_doc)

    # Create profile referencing big-skill
    prof_dir = profiles.path / "big_prof"
    prof_dir.mkdir(parents=True)
    (prof_dir / "PROFILE.md").write_text("大技能角色。", encoding="utf-8")
    (prof_dir / "profile.toml").write_text(
        'title = "Big"\nenvironment = "readonly"\nskills = ["big-skill"]\n',
        encoding="utf-8",
    )

    package = profiles.export("big_prof")
    assert len(package) > 0

    # Oversized > 2 MiB skill file must still be rejected
    (skill_dir / "references/too_big.bin").write_bytes(b"z" * (2 * 1024 * 1024 + 10))
    with pytest.raises(ValueError):
        profiles.export("big_prof")
    (skill_dir / "references/too_big.bin").unlink()

    # Import into fresh destination where abilities/skills/big-skill does NOT exist globally
    dst = tmp_path / "dst"
    dst_profiles = Profiles(dst / "abilities/profiles")
    dst_store = ThreadStore(dst / "threads.db")
    dst_vault = Vault(dst / "vault")
    dst_memory = MemoryLayers(dst, dst_store, dst_profiles, dst_vault)

    imported = dst_profiles.import_package(package, "imported_big")
    assert imported.id == "imported_big"
    assert not (dst / "abilities/skills/big-skill").exists()

    # read_skill_file MUST fall back to <profile>/preset/skills/<skill>/ even without profile_id
    info = dst_memory.read_skill_file("big-skill", "references/guide.md")
    assert info["bytes"] == len(large_doc)
    assert info["is_binary"] is False
    assert info["truncated"] is True
    assert info["content"].startswith("# Big Companion\n")


def test_clone_preserves_local_bound_and_scope_while_import_package_strips_allow_lists(
    tmp_path,
):
    src = tmp_path / "src"
    profiles = Profiles(src / "abilities/profiles")

    # 1. Local bound listed profile with local scope & bindings
    local_dir = profiles.path / "local_bound"
    local_dir.mkdir(parents=True)
    (local_dir / "PROFILE.md").write_text("本地受权角色。", encoding="utf-8")
    (local_dir / "profile.toml").write_text(
        'title = "本地受权"\n'
        'environment = "readonly"\n'
        'access = "listed"\n'
        "bound = true\n"
        'apis = ["weather"]\n'
        'allow_chats = ["chat1"]\n'
        'allow_users = ["qq:Alice"]\n',
        encoding="utf-8",
    )
    cloned = profiles.clone("local_bound", "local_bound_copy")
    assert cloned.bound is True
    assert cloned.access == "listed"
    assert cloned.allow_chats == ("chat1",)
    assert cloned.allow_users == ("qq:Alice",)
    assert cloned.apis == ("weather",)

    # 2. External import_package MUST clear allow_* & unbind even when access == "everyone"
    ev_dir = profiles.path / "foreign_everyone"
    ev_dir.mkdir(parents=True)
    (ev_dir / "PROFILE.md").write_text("外来公开角色。", encoding="utf-8")
    (ev_dir / "profile.toml").write_text(
        'title = "外来公开"\n'
        'environment = "readonly"\n'
        'access = "everyone"\n'
        "bound = true\n"
        'allow_chats = ["foreign_group"]\n'
        'allow_users = ["qq:ForeignUser"]\n',
        encoding="utf-8",
    )
    ev_pkg = profiles.export("foreign_everyone")

    dst = Profiles(tmp_path / "dst/abilities/profiles")
    imp_ev = dst.import_package(ev_pkg, "imp_ev")
    assert imp_ev.allow_chats == ()
    assert imp_ev.allow_users == ()
    assert imp_ev.bound is False

    # 3. External import_package with access == "listed" clears allow_*, upgrades access away from listed, unbinds
    listed_pkg = profiles.export("local_bound")
    imp_listed = dst.import_package(listed_pkg, "imp_listed")
    assert imp_listed.allow_chats == ()
    assert imp_listed.allow_users == ()
    assert imp_listed.access == "admins"
    assert imp_listed.bound is False


def test_symlink_checks_only_inside_managed_roots(tmp_path):
    real_root = tmp_path / "real_storage"
    real_root.mkdir(parents=True)
    # Simulate macOS /var -> private/var or mounted drive symlink ABOVE / AT managed root
    var_mount = tmp_path / "var_mount"
    var_mount.symlink_to(real_root)

    profiles = Profiles(var_mount / "abilities/profiles")
    store = ThreadStore(real_root / "threads.db")
    vault = Vault(real_root / "vault")
    memory = MemoryLayers(var_mount, store, profiles, vault)
    memory.write("journal", "挂载盘符下的手账写入成功")
    assert "挂载盘符下的手账写入成功" in memory.read("journal")["content"]

    # Create a bundled skill inside <profile>/preset/skills/inside-skill
    prof_dir = profiles.path / "preset_prof"
    prof_dir.mkdir(parents=True)
    (prof_dir / "PROFILE.md").write_text("预设技能角色。", encoding="utf-8")
    (prof_dir / "profile.toml").write_text(
        'title = "Preset"\nenvironment = "readonly"\nskills = ["inside-skill"]\n',
        encoding="utf-8",
    )
    bundled = prof_dir / "preset/skills/inside-skill"
    (bundled / "references").mkdir(parents=True)
    (bundled / "SKILL.md").write_text(
        "---\nname: inside-skill\ndescription: ok\n---\n# Method\nUse clean data.\n",
        encoding="utf-8",
    )
    (bundled / "references/note.md").write_text("参考说明", encoding="utf-8")

    pkg = profiles.export("preset_prof")
    assert len(pkg) > 0
    companions = memory.read_skill_companions("inside-skill", profile_id="preset_prof")
    assert dict(companions).get("references/note.md") == "参考说明".encode("utf-8")
    assert "Use clean data." in memory.methods(profiles.get("preset_prof"))
    assert memory.read_skill_file("inside-skill", "references/note.md")["content"] == "参考说明"

    # Symlink INSIDE managed skill directory MUST still be rejected
    outside = tmp_path / "outside.txt"
    outside.write_text("secret leak", encoding="utf-8")
    evil_dir = real_root / "abilities/skills/evil-skill"
    evil_dir.mkdir(parents=True)
    (evil_dir / "SKILL.md").symlink_to(outside)
    evil_prof = real_root / "abilities/profiles/evil_prof"
    evil_prof.mkdir(parents=True)
    (evil_prof / "PROFILE.md").write_text("恶意软链。", encoding="utf-8")
    (evil_prof / "profile.toml").write_text(
        'title = "Evil"\nenvironment = "readonly"\nskills = ["evil-skill"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        profiles.export("evil_prof")

    evil_bundled_link = prof_dir / "preset/skills/evil-preset"
    evil_bundled_target = tmp_path / "outside_dir"
    evil_bundled_target.mkdir()
    (evil_bundled_target / "SKILL.md").write_text("# Outside\n", encoding="utf-8")
    evil_bundled_link.symlink_to(evil_bundled_target)
    with pytest.raises(ValueError):
        memory.methods(
            profiles.get("preset_prof").__class__(
                **{
                    **profiles.get("preset_prof").__dict__,
                    "skills": ("evil-preset",),
                }
            )
        )
