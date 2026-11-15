"""Phase 3 backend tests: cleanup, Watch mode, minimal shaping (<=600 lines), and 5-step onboarding."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sqlite3
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from maisya.abilities import Profiles
from maisya.config import PluginSettings, Settings
from maisya.intents import Intents
from maisya.memory import MemoryLayers
from maisya.pulse import Pulse
from maisya.services import Services, validate_endpoint_targets
from maisya.threads import ThreadStore
from maisya.trust import Message, MessageStore
from maisya.vault import Vault


class _FakeBridge:
    def __init__(self, store: ThreadStore, redact=lambda v: v):
        self.store = store
        self.redact = redact
        self.staged: list[tuple[dict, str, bool]] = []
        self.new_inputs: list[str] = []

    def claim_ready(self, enabled, plugin_enabled=lambda: True):
        return {}

    def current(self, rows, pending=True):
        return rows

    def material(self, rows):
        return rows

    def new_input(self, chat_id: str):
        self.new_inputs.append(chat_id)

    async def offer(self, chat_id: str):
        return None

    def wake_status(self, rows, status: str):
        return None

    def stage(self, thread, chat_id: str, archived: bool = False):
        self.staged.append((thread, chat_id, archived))


class _FakeHost:
    def __init__(self, wakes: list[dict]):
        self.wakes = wakes

    async def call_capability(self, name: str, **arguments):
        if name == "maisaka.proactive.trigger":
            self.wakes.append(arguments)
            return {"success": True}
        if name == "chat.open_session":
            tid = str(arguments.get("group_id") or arguments.get("user_id") or "100246809")
            return {"success": True, "session_id": tid}
        raise AssertionError(f"unexpected host capability: {name}")


class _FakeEngine:
    def __init__(self, store: ThreadStore, reply_by_prompt: dict[str, dict] | None = None):
        self.store = store
        self.reply_by_prompt = reply_by_prompt or {}
        self.tasks: dict[str, object] = {}
        self.started_calls: list[dict] = []

    async def start(
        self,
        ability,
        prompt: str,
        *,
        chat_id: str,
        platform: str = "qq",
        user_id: str = "",
        owner: str = "chat",
        wait_seconds: float = 0,
        workspace: str = "",
        grant: dict | None = None,
        background: bool = True,
        defer_if_full: bool = True,
    ):
        self.started_calls.append(
            {
                "profile": getattr(ability, "profile_id", getattr(ability, "name", "")),
                "prompt": prompt,
                "chat_id": chat_id,
                "platform": platform,
                "workspace": workspace,
                "grant": dict(grant or {}),
            }
        )
        row = self.store.create(
            ability,
            prompt,
            chat_id=chat_id,
            platform=platform,
            user_id=user_id,
            owner=owner,
            workspace=workspace,
            grant=grant or {},
        )
        result = {"text": "[WATCH:NO_CHANGE]", "triggered": False}
        for needle, custom in self.reply_by_prompt.items():
            if needle in prompt:
                result = dict(custom)
                break
        with self.store.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='idle', result=? WHERE thread_id=?",
                (json.dumps(result, ensure_ascii=False), row["thread_id"]),
            )
        return self.store.get(row["thread_id"], chat_id, allow_self=(owner == "self"))


def _make_memory(root: Path, store: ThreadStore, vault: Vault, clock=time.time) -> MemoryLayers:
    data_root = root / "data"
    profiles = Profiles(data_root / "profiles", contains_secret=vault.contains_secret)
    return MemoryLayers(
        data_root,
        store,
        profiles,
        vault,
        clock=clock,
    )


def _human_event(msg_id: str, user_id: str = "AdminOne", *, platform: str = "qq", chat_id: str = "100246809"):
    return {
        "message_id": msg_id,
        "session_id": chat_id,
        "platform": platform,
        "timestamp": "1794459000.0",
        "processed_plain_text": "修改配置",
        "raw_message": [{"type": "text", "data": "修改配置"}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": {"group_id": "100246809"},
            "additional_config": {},
        },
    }


def _make_shaping_plugin(tmp_path: Path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store = ThreadStore(tmp_path / "data" / "maisya.db")
    vault = Vault(tmp_path / "data" / "vault")
    messages = MessageStore(tmp_path / "data" / "messages.db")
    intents = Intents(store)
    memory = _make_memory(tmp_path, store, vault)
    settings = Settings(
        plugin=PluginSettings(enabled=True, config_version="0.3.0"),
        enabled_chats=["qq:group:100246809"],
        proactive_level="care",
    )
    host_admins = {"qq:AdminOne"}
    saved_configs: list[dict] = []

    class _FakePlugin:
        def __init__(self):
            self.data_root = tmp_path / "data"
            self.plugin_root = tmp_path
            self.store = store
            self.vault = vault
            self.messages = messages
            self.intents = intents
            self.memory = memory
            self.profiles = memory.profiles
            self.settings = settings
            self.host_admins = host_admins
            self.saved_configs = saved_configs
            self.pulse = None
            self.runtime = None

        def effective_admins(self) -> set[str]:
            extra = set(getattr(self.settings, "admins", []) or [])
            return set(self.host_admins) | extra

        def authorize_admin_source(self, source_msg_id: str, chat_id: str = "", platform: str = "") -> bool:
            if not source_msg_id:
                return False
            plat = platform or (self.messages.platform_for(chat_id) if chat_id else "qq")
            msg = self.messages.lookup(source_msg_id, chat_id, plat)
            if msg is None or msg.is_plugin_relayed:
                return False
            if chat_id and msg.session_id != chat_id:
                return False
            ident = f"{msg.platform.lower()}:{msg.user_id}"
            return ident in set(self.effective_admins())

        def canonical_stream_spec(self, chat_id: str, platform: str = "") -> str:
            return chat_id if ":" in chat_id else f"{platform or 'qq'}:group:{chat_id}"

        def chat_aliases_for(self, chat_id: str, platform: str = "") -> set[str]:
            return {chat_id}

        async def _resolve_chat_sources(self) -> dict:
            return {
                "100246809": {
                    "session_id": "100246809",
                    "stream_spec": "qq:group:100246809",
                    "platform": "qq",
                    "chat_type": "group",
                    "group_id": "100246809",
                    "name": "测试群",
                }
            }

        async def persist_settings(self, data: dict):
            saved_configs.append(dict(data))
            self.settings = Settings(**data)

    return _FakePlugin()


def test_deleted_incoming_and_recipes_and_trimmed_services(tmp_path):
    repo_root = Path(__file__).resolve().parent.parent
    assert not (repo_root / "maisya" / "incoming.py").exists()
    assert not (repo_root / "maisya" / "recipes.py").exists()
    assert not (repo_root / "tests" / "test_incoming_events.py").exists()
    assert not (repo_root / "tests" / "test_recipes.py").exists()
    sys.modules.pop("maisya.incoming", None)
    sys.modules.pop("maisya.recipes", None)
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("maisya.incoming")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("maisya.recipes")

    vault = Vault(tmp_path / "vault")
    services = Services(tmp_path / "services.json", vault)
    assert not hasattr(services, "credential")
    cfg = services.load()
    assert "templates" not in cfg
    assert cfg.get("servers") == {}

    assert validate_endpoint_targets(["93.184.216.34:443"]) == [("93.184.216.34", 443)]
    with pytest.raises(ValueError):
        validate_endpoint_targets(["127.0.0.1:8080"])
    with pytest.raises(ValueError):
        validate_endpoint_targets(["10.0.0.1:80"])


def test_intents_watch_mode_and_legacy_sqlite_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE intents (
            intent_id TEXT PRIMARY KEY,
            kind TEXT,
            source TEXT,
            target TEXT,
            due_at REAL,
            reason TEXT,
            thread_id TEXT,
            payload TEXT,
            status TEXT,
            wake_status TEXT,
            created REAL,
            handled REAL
        );
        INSERT INTO intents VALUES (
            'in_legacy1', 'return', 'later', 'chat:qq:group:100246809',
            1000.0, '旧提醒', '', '{"platform":"qq"}', 'pending', '', 900.0, NULL
        );
        """
    )
    conn.close()

    store = ThreadStore(db_path)
    now = 2000.0
    intents = Intents(store, clock=lambda: now, daily_proactive_limit=1)

    legacy = intents.get("in_legacy1")
    assert legacy["mode"] == "schedule"
    assert legacy["condition"] == ""
    assert legacy["last_checked_at"] is None
    assert legacy["last_triggered_at"] is None

    watched = intents.schedule(
        kind="return",
        source="watch",
        mode="watch",
        condition="有新报错渠道时汇报，无事不打扰",
        target="chat:qq:group:100246809",
        due_at=now + 60,
        reason="渠道异常盯梢",
        payload={"platform": "qq", "every_seconds": 300, "scheduled_admin": True},
        bypass_limits=True,
    )
    assert watched["mode"] == "watch"
    assert watched["condition"] == "有新报错渠道时汇报，无事不打扰"
    assert watched["last_checked_at"] is None
    assert watched["last_triggered_at"] is None

    # Compat with existing modes
    for mode_val in ("once", "recurring", "afterthought", "schedule"):
        item = intents.schedule(
            kind="return",
            source="later",
            mode=mode_val,
            target="chat:qq:group:100246809",
            due_at=now + 120,
            reason=f"mode-{mode_val}",
            payload={"platform": "qq"},
        )
        assert item["mode"] == mode_val

    # scheduled_admin protection against non-admin cancellation / pause
    with pytest.raises(PermissionError):
        intents.cancel(watched["intent_id"], "chat:qq:group:100246809", is_admin=False)
    with pytest.raises(PermissionError):
        intents.pause(watched["intent_id"], "chat:qq:group:100246809", is_admin=False)
    assert intents.get(watched["intent_id"])["status"] == "pending"

    paused = intents.pause(watched["intent_id"], "chat:qq:group:100246809", is_admin=True)
    assert paused["status"] == "paused"
    resumed = intents.resume(watched["intent_id"], "chat:qq:group:100246809", is_admin=True)
    assert resumed["status"] == "pending"


@pytest.mark.asyncio
async def test_pulse_watch_silent_unmet_then_wakes_when_triggered(tmp_path):
    now = 10_000.0
    store = ThreadStore(tmp_path / "watch.db")
    intents = Intents(store, clock=lambda: now, daily_proactive_limit=1)
    bridge = _FakeBridge(store)
    wakes: list[dict] = []
    host = _FakeHost(wakes)
    checks: list[str] = []
    should_trigger = False

    async def evaluator(row, ctx):
        checks.append(row["condition"])
        if should_trigger:
            return {"triggered": True, "summary": "发现渠道 alpha 连续 502"}
        return {"triggered": False, "summary": "一切正常"}

    pulse = Pulse(
        bridge,
        host,
        lambda chat_id, platform="": True,
        intents=intents,
        watch_evaluator=evaluator,
    )

    watched = intents.schedule(
        kind="return",
        source="watch",
        mode="watch",
        condition="存在新的 401/500 渠道报错",
        target="chat:qq:group:100246809",
        due_at=now - 1,
        reason="巡检模型渠道",
        payload={"platform": "qq", "every_seconds": 300, "scheduled_admin": True},
        bypass_limits=True,
    )

    # First tick: condition unmet -> silent, last_checked_at updated, last_triggered_at None,
    # rescheduled for next cycle, zero wakes, daily proactive quota untouched.
    await pulse._intents()
    assert len(checks) == 1
    assert wakes == []
    after_unmet = intents.get(watched["intent_id"])
    assert after_unmet["status"] == "pending"
    assert after_unmet["last_checked_at"] == pytest.approx(now)
    assert after_unmet["last_triggered_at"] is None
    assert after_unmet["due_at"] >= now + 300
    assert intents.proactive_quota_available("chat:qq:group:100246809") is True

    # Second tick: condition met -> wakes target, records last_triggered_at, still respects quota
    now = after_unmet["due_at"] + 1
    should_trigger = True
    await pulse._intents()
    assert len(checks) == 2
    assert len(wakes) == 1
    assert "发现渠道 alpha 连续 502" in wakes[0]["intent"]
    assert wakes[0]["metadata"]["mode"] == "watch"
    after_met = intents.get(watched["intent_id"])
    assert after_met["last_checked_at"] == pytest.approx(now)
    assert after_met["last_triggered_at"] == pytest.approx(now)
    assert after_met["status"] == "pending"
    assert after_met["due_at"] >= now + 300
    assert intents.proactive_quota_available("chat:qq:group:100246809") is True


@pytest.mark.asyncio
async def test_pulse_watch_thread_agent_integration(tmp_path):
    now = 20_000.0
    store = ThreadStore(tmp_path / "watch_thread.db")
    vault = Vault(tmp_path / "vault")
    memory = _make_memory(tmp_path, store, vault, clock=lambda: now)
    intents = Intents(store, clock=lambda: now, daily_proactive_limit=1)
    bridge = _FakeBridge(store)
    wakes: list[dict] = []
    host = _FakeHost(wakes)
    engine = _FakeEngine(
        store,
        reply_by_prompt={
            "发现新版本": {"text": "检测到 v2.1.0 已发布。", "attachments": [], "links": []}
        },
    )
    pulse = Pulse(
        bridge,
        host,
        lambda chat_id, platform="": True,
        intents=intents,
        engine=engine,
        profiles=memory.profiles,
    )
    one_shot = intents.schedule(
        kind="return",
        source="watch",
        mode="watch",
        condition="检查是否有新发布",
        target="chat:qq:group:100246809",
        due_at=now - 1,
        reason="发现新版本时汇报",
        payload={"platform": "qq", "profile": "scout", "scheduled_admin": True},
        bypass_limits=True,
    )
    await pulse._intents()
    assert len(wakes) == 1
    assert "v2.1.0" in wakes[0]["intent"]
    row = intents.get(one_shot["intent_id"])
    assert row["status"] == "done"
    assert row["last_triggered_at"] == pytest.approx(now)
    assert engine.started_calls[0]["workspace"].startswith("watch_")


@pytest.mark.asyncio
async def test_shaping_minimal_actions_admin_guard_anti_lockout_and_files(tmp_path):
    shaping = importlib.import_module("maisya.shaping")
    shaping_path = Path(shaping.__file__)
    line_count = len(shaping_path.read_text(encoding="utf-8").splitlines())
    assert line_count <= 600, f"shaping.py must be <= 600 lines, got {line_count}"
    sig = inspect.signature(shaping.execute_shape)
    assert "visibility" not in sig.parameters

    plugin = _make_shaping_plugin(tmp_path)
    plugin.messages.record(Message.from_event(_human_event("admin_msg_1", "AdminOne")))
    plugin.messages.record(Message.from_event(_human_event("user_msg_1", "OrdinaryUser")))

    # Non-admin or unverified source must be blocked for mutations
    with pytest.raises(PermissionError):
        await shaping.execute_shape(
            plugin,
            action="settings",
            op="patch",
            patch={"proactive_level": "lively"},
            current_chat_id="100246809",
            current_platform="qq",
            source_msg_id="",
        )
    with pytest.raises(PermissionError):
        await shaping.execute_shape(
            plugin,
            action="settings",
            op="patch",
            patch={"proactive_level": "lively"},
            current_chat_id="100246809",
            current_platform="qq",
            source_msg_id="user_msg_1",
        )

    # 1. overview
    ov = await shaping.execute_shape(
        plugin,
        action="overview",
        current_chat_id="100246809",
        current_platform="qq",
        source_msg_id="admin_msg_1",
    )
    assert ov["kind"] == "overview"
    assert ov["plugin_enabled"] is True
    assert len(ov["known_chats"]) >= 1

    # 2. settings: anti-lockout guards + rollback hint
    with pytest.raises(ValueError):
        await shaping.execute_shape(
            plugin,
            action="settings",
            op="patch",
            patch={"enabled_chats": []},
            current_chat_id="100246809",
            current_platform="qq",
            source_msg_id="admin_msg_1",
        )
    with pytest.raises(ValueError):
        await shaping.execute_shape(
            plugin,
            action="settings",
            op="patch",
            patch={"remove_chats": ["qq:group:100246809"]},
            current_chat_id="100246809",
            current_platform="qq",
            source_msg_id="admin_msg_1",
        )
    plugin.host_admins = set()
    plugin.settings = Settings(
        plugin=PluginSettings(enabled=True, config_version="0.3.0"),
        enabled_chats=["qq:group:100246809"],
        admins=["qq:AdminOne"],
    ) if "admins" in Settings.model_fields else plugin.settings
    if hasattr(plugin.settings, "admins"):
        with pytest.raises(ValueError):
            await shaping.execute_shape(
                plugin,
                action="settings",
                op="patch",
                patch={"admins": []},
                current_chat_id="100246809",
                current_platform="qq",
                source_msg_id="admin_msg_1",
            )
        with pytest.raises(ValueError):
            await shaping.execute_shape(
                plugin,
                action="settings",
                op="patch",
                patch={"admins": ["qq:OtherAdmin"]},
                current_chat_id="100246809",
                current_platform="qq",
                source_msg_id="admin_msg_1",
            )
    else:
        plugin.host_admins = {"qq:AdminOne"}

    patched = await shaping.execute_shape(
        plugin,
        action="settings",
        op="patch",
        patch={"add_chats": ["qq:private:10001"], "sleep_window": "01-07"},
        current_chat_id="100246809",
        current_platform="qq",
        source_msg_id="admin_msg_1",
    )
    assert "qq:private:10001" in patched["settings"]["enabled_chats"]
    assert "rollback_hint" in patched
    rolled = await shaping.execute_shape(
        plugin,
        action="settings",
        op="rollback",
        version=patched["previous_version"],
        current_chat_id="100246809",
        current_platform="qq",
        source_msg_id="admin_msg_1",
    )
    assert "qq:private:10001" not in rolled["settings"]["enabled_chats"]

    # 3. profile
    created = await shaping.execute_shape(
        plugin,
        action="profile",
        op="create",
        target="keeper",
        base="scout",
        patch={"title": "守秘人"},
        current_chat_id="100246809",
        current_platform="qq",
        source_msg_id="admin_msg_1",
    )
    assert created["profile"]["profile_id"] == "keeper"
    assert created["profile"]["title"] == "守秘人"
    deleted = await shaping.execute_shape(
        plugin,
        action="profile",
        op="delete",
        target="keeper",
        current_chat_id="100246809",
        current_platform="qq",
        source_msg_id="admin_msg_1",
    )
    assert deleted["deleted"] is True

    # 4. file: read/write/rollback + traversal & secret protection
    plugin.vault.set("secret_tok", "super-secret-value-999")
    with pytest.raises(ValueError):
        await shaping.execute_shape(
            plugin,
            action="file",
            op="write",
            document="../../etc/passwd",
            content="bad",
            current_chat_id="100246809",
            current_platform="qq",
            source_msg_id="admin_msg_1",
        )
    with pytest.raises(ValueError):
        await shaping.execute_shape(
            plugin,
            action="file",
            op="write",
            document="JOURNAL.md",
            content="token=super-secret-value-999",
            expected_version=0,
            current_chat_id="100246809",
            current_platform="qq",
            source_msg_id="admin_msg_1",
        )
    j_read = await shaping.execute_shape(
        plugin,
        action="file",
        op="read",
        document="JOURNAL.md",
        current_chat_id="100246809",
        current_platform="qq",
        source_msg_id="admin_msg_1",
    )
    j_write = await shaping.execute_shape(
        plugin,
        action="file",
        op="write",
        document="JOURNAL.md",
        content="# 手账\n今天修好了条件盯梢。\n",
        expected_version=j_read["version"],
        current_chat_id="100246809",
        current_platform="qq",
        source_msg_id="admin_msg_1",
    )
    assert j_write["version"] > j_read["version"]
    assert "rollback_hint" in j_write
    j_back = await shaping.execute_shape(
        plugin,
        action="file",
        op="rollback",
        document="JOURNAL.md",
        version=j_read["version"],
        expected_version=j_write["version"],
        current_chat_id="100246809",
        current_platform="qq",
        source_msg_id="admin_msg_1",
    )
    assert j_back["content"] == j_read["content"]


@pytest.mark.asyncio
async def test_onboarding_five_steps_checklist_and_anti_race_probe(tmp_path):
    onboarding = importlib.import_module("maisya.onboarding")
    assert onboarding.STEPS == ("hello", "admins", "chats", "runtime", "done")

    mgr = onboarding.OnboardingManager(tmp_path / "data" / "onboarding.json")

    fresh_settings = {
        "plugin": {"enabled": False},
        "enabled_chats": [],
        "admins": [],
        "permission_mode": "ask_risky",
    }
    state = mgr.view(
        settings=fresh_settings,
        host_model_catalog={"models": [{"name": "m1", "identifier": "m1", "provider": "p", "api_key": "sk-NEVER-LEAK", "base_url": "https://secret.example/v1"}]},
        host_admins=set(),
        mcp_servers={},
        docker_status={"enabled": False, "ready": False},
        existing_threads_count=0,
        is_admin_view=True,
    )
    assert state["show"] is True
    assert state["status"] == "not_started"
    assert state["current_step"] == "hello"
    assert state["usable"] is False
    assert len(state["items"]) == 7
    assert "sk-NEVER-LEAK" not in json.dumps(state)
    assert "https://secret.example/v1" not in json.dumps(state)

    # Non-admin view never pops onboarding
    non_admin_view = mgr.view(settings=fresh_settings, is_admin_view=False)
    assert non_admin_view["show"] is False

    # Existing active install stays silent on upgrade
    existing_mgr = onboarding.OnboardingManager(tmp_path / "existing" / "onboarding.json")
    existing_state = existing_mgr.view(
        settings={"plugin": {"enabled": True}, "enabled_chats": ["qq:group:100246809"]},
        host_model_catalog={"models": [{"name": "m1"}]},
        host_admins={"qq:AdminOne"},
        existing_threads_count=3,
        is_admin_view=True,
    )
    assert existing_state["show"] is False

    # Advance through steps & test usable gate preventing fake complete
    s1 = mgr.advance(
        step="admins",
        run_id=state["model_verify"]["run_id"],
        sequence=state["model_verify"]["sequence"],
        data={"count": 1, "verified": True},
        settings=fresh_settings,
    )
    assert s1["status"] == "in_progress"
    assert "hello" in s1["completed_steps"]

    # Attempt complete while plugin disabled & no chats/admins -> MUST NOT fake completed
    fake_done = mgr.complete(settings=fresh_settings, host_admins=set())
    assert fake_done["status"] != "completed"
    assert fake_done["usable"] is False

    # Anti-race model probe: slow stale probe must not clobber reset state
    started_event = asyncio.Event()
    release_event = asyncio.Event()

    async def slow_probe():
        started_event.set()
        await release_event.wait()
        return {"ok": True, "model_name": "m1", "message": "ok api_key=sk-SECRET-XYZ base_url=https://evil.internal"}

    probe_task = asyncio.create_task(
        mgr.verify_model(slow_probe, model_name="m1", timeout_seconds=5.0)
    )
    await started_event.wait()

    # While probe #1 is in flight, reset onboarding (bumps run_id + sequence)
    after_reset = mgr.reset()
    assert after_reset["status"] == "in_progress"
    assert after_reset["current_step"] == "hello"
    assert after_reset["completed_steps"] == []
    assert after_reset["model_verify"]["state"] == "idle"
    assert after_reset["model_verify"]["run_id"] != state["model_verify"]["run_id"]
    assert after_reset["model_verify"]["sequence"] > state["model_verify"]["sequence"]

    release_event.set()
    stale_res = await probe_task
    assert stale_res["stale"] is True
    assert mgr.read()["model_verify"]["state"] == "idle"

    # Run clean probe on fresh epoch and ensure zero secret leak + timeout handling
    async def good_probe():
        return {"ok": True, "model_name": "m1", "message": "connected sk-SHOULD-REDACT"}

    ok_res = await mgr.verify_model(good_probe, model_name="m1", timeout_seconds=5.0)
    assert ok_res["stale"] is False
    assert ok_res["model_verify"]["state"] == "ok"
    assert "sk-SHOULD-REDACT" not in json.dumps(ok_res)

    async def hanging_probe():
        await asyncio.sleep(2.0)
        return {"ok": True}

    to_res = await mgr.verify_model(hanging_probe, model_name="m1", timeout_seconds=0.1)
    assert to_res["model_verify"]["error_code"] == "timeout"
    assert to_res["model_verify"]["state"] == "failed"

    # Failed model probe must block usable & complete
    ready_settings = {
        "plugin": {"enabled": True},
        "enabled_chats": ["qq:group:100246809"],
        "admins": ["qq:AdminOne"],
        "permission_mode": "ask_risky",
    }
    blocked_done = mgr.complete(
        settings=ready_settings,
        host_model_catalog={"models": [{"name": "m1"}]},
        host_admins={"qq:AdminOne"},
    )
    assert blocked_done["usable"] is False
    assert blocked_done["status"] != "completed"

    # Once fresh probe succeeds and requirements pass, complete succeeds
    await mgr.verify_model(lambda: {"ok": True, "model_name": "m1", "message": "ready"})
    mgr.advance(step="runtime", data={"permission_mode": "ask_risky", "safety_acknowledged": True})
    done = mgr.complete(
        settings=ready_settings,
        host_model_catalog={"models": [{"name": "m1"}]},
        host_admins={"qq:AdminOne"},
        default_profile_ready=True,
        docker_status={"enabled": False, "ready": False, "host_runner_ready": True},
    )
    assert done["usable"] is True
    assert done["status"] == "completed"
    assert done["show"] is False

    # Reopen & skip persistence
    reopened = mgr.reopen(step="chats")
    assert reopened["show"] is True
    assert reopened["status"] == "in_progress"
    assert reopened["current_step"] == "chats"
    skipped = mgr.skip()
    assert skipped["status"] == "skipped"
    assert skipped["dismissed_at"] is not None
    assert skipped["show"] is False

    raw_json = (tmp_path / "data" / "onboarding.json").read_text(encoding="utf-8")
    assert "sk-SHOULD-REDACT" not in raw_json
    assert stat.S_IMODE((tmp_path / "data" / "onboarding.json").stat().st_mode) in (0o600, 0o644)
