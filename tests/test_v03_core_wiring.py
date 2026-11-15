"""Wave 2 core-wiring integration tests for Maisya v0.3."""

from __future__ import annotations

import base64
import inspect
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import zipfile

import pytest

from maisya.abilities import TOOLSETS
from maisya.config import Settings
from maisya.runner import ExecutionResult, HostWorkspaceRunner
from maisya.sandbox import DockerRunner
from maisya.scout import TOOL_DEFINITIONS
from maisya.trust import PermissionGrantStore
from plugin import create_plugin


REPO_ROOT = Path(__file__).resolve().parents[1]
OWNED_SOURCE_FILES = [
    REPO_ROOT / "plugin.py",
    REPO_ROOT / "maisya" / "scout.py",
    REPO_ROOT / "maisya" / "maker.py",
    REPO_ROOT / "maisya" / "console.py",
    REPO_ROOT / "maisya" / "management.py",
    REPO_ROOT / "_manifest.json",
    REPO_ROOT / "webui.json",
]

TEST_GROUP_ID = "100246809"
TEST_GROUP_SESSION = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TEST_ADMIN_ID = "AdminUserOne"
TEST_OTHER_ID = "MemberUserTwo"


def _make_async_config_getter(host_config: dict[str, Any]):
    async def _get(key: str, default: Any = None) -> Any:
        current: Any = host_config
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    return _get


async def _load_plugin(
    tmp_path: Path,
    *,
    plugin_config: dict[str, Any] | None = None,
    host_config: dict[str, Any] | None = None,
    call_capability_fn: Any = None,
    write_model_toml: bool = False,
) -> Any:
    host_dir = tmp_path / "host"
    data_dir = host_dir / "data" / "plugins" / "chartyr.maisya"
    if write_model_toml:
        cfg_dir = host_dir / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "model_config.toml").write_text(
            "\n".join(
                [
                    "[model_task_config.planner]",
                    'model_list = ["main-chat-model"]',
                    "",
                    "[model_task_config.utils]",
                    'model_list = ["main-chat-model"]',
                    "",
                    "[[models]]",
                    'name = "main-chat-model"',
                    'model_identifier = "gpt-test"',
                    'api_provider = "test-provider"',
                    'api_key = "sk-host-secret-should-never-leak-9999"',
                    'base_url = "https://private-host-endpoint.invalid/v1"',
                    "",
                ]
            ),
            encoding="utf-8",
        )

    plugin = create_plugin()
    host_cfg: dict[str, Any] = {
        "plugin": {"permission": [f"qq:{TEST_ADMIN_ID}"]},
    }
    if host_config:
        host_cfg.update(host_config)

    async def _default_call_capability(cap: str, **kwargs: Any) -> dict[str, Any]:
        if call_capability_fn is not None:
            res = call_capability_fn(cap, kwargs)
            if inspect.isawaitable(res):
                return await res
            return res
        if cap == "llm.generate_with_tools":
            return {"success": True, "response": "已完成处理。", "tool_calls": []}
        if cap == "send.text":
            return {"success": True, "message_id": "sent-1"}
        if cap == "chat.get_all_streams":
            return {"success": True, "streams": []}
        if cap == "component.list_loaded_plugins":
            return {"success": True, "plugins": []}
        if cap == "api.list":
            return {"success": True, "apis": []}
        return {"success": True}

    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=_make_async_config_getter(host_cfg)),
        paths=SimpleNamespace(
            data_dir=data_dir,
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya.core_wiring"),
        call_capability=_default_call_capability,
    )

    merged_cfg = Settings().model_dump()
    merged_cfg["plugin"]["enabled"] = True
    if plugin_config:
        for section, values in plugin_config.items():
            if isinstance(values, dict) and isinstance(merged_cfg.get(section), dict):
                merged_cfg[section].update(values)
            else:
                merged_cfg[section] = values
    plugin.set_plugin_config(merged_cfg)
    await plugin.on_load()
    return plugin


def _dm_message(
    user_id: str,
    text: str,
    *,
    msg_id: str = "msg-dm-1",
    platform: str = "qq",
    session_id: str | None = None,
    relayed: bool = False,
) -> dict[str, Any]:
    return {
        "message_id": msg_id,
        "session_id": session_id or f"dm_{user_id}",
        "platform": platform,
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": None,
            "additional_config": {"relayed_by_plugin": True} if relayed else {},
        },
    }


def _group_message(
    user_id: str,
    text: str,
    *,
    group_id: str = TEST_GROUP_ID,
    session_id: str = TEST_GROUP_SESSION,
    msg_id: str = "msg-grp-1",
    platform: str = "qq",
) -> dict[str, Any]:
    return {
        "message_id": msg_id,
        "session_id": session_id,
        "platform": platform,
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": {"group_id": group_id},
            "additional_config": {},
        },
    }


def test_static_manifest_webui_components_and_cleanliness() -> None:
    manifest = json.loads((REPO_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    webui = json.loads((REPO_ROOT / "webui.json").read_text(encoding="utf-8"))

    assert webui["schema_version"] == 2
    assert len(webui["pages"]) == 1
    page = webui["pages"][0]
    assert page["entry"] == "webui/now.html"
    assert list(page["queries"].keys()) == ["event", "snapshot", "read"]
    assert list(page["actions"].keys()) == ["note", "stop", "write"]
    assert "incoming_event" not in json.dumps(webui, ensure_ascii=False)
    assert "incoming_event" not in json.dumps(manifest, ensure_ascii=False)

    forbidden_patterns = [
        "她",
        "maisya.incoming",
        "maisya.recipes",
        "maisya_incoming_event",
        "credential_call",
        "services.templates",
        "/Users/",
        "/root/",
    ]
    for path in OWNED_SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        for pat in forbidden_patterns:
            assert pat not in text, f"Forbidden pattern {pat!r} found in {path.name}"

    assert "exec_command" in TOOL_DEFINITIONS
    assert "sandbox_exec" in TOOL_DEFINITIONS
    assert "credential_call" not in TOOL_DEFINITIONS
    assert "exec_command" in TOOLSETS["exec"]
    assert "sandbox_exec" in TOOLSETS["exec"]
    assert "credential_call" not in TOOLSETS["exec"]

    plugin = create_plugin()
    components = plugin.get_components()
    by_name: dict[str, dict[str, Any]] = {}
    for comp in components:
        if isinstance(comp, dict):
            name = comp.get("name")
            meta = comp.get("metadata") or {}
            if name:
                by_name[name] = {"name": name, **comp, **dict(meta)}
        else:
            name = getattr(comp, "name", None)
            meta = getattr(comp, "metadata", None) or {}
            if name:
                by_name[name] = {"name": name, **dict(meta)}

    for visible_tool in ("maisya_agent", "maisya_recall", "maisya_later"):
        assert visible_tool in by_name
        assert by_name[visible_tool].get("visibility") == "visible"

    assert "maisya_shape" in by_name
    assert "visibility" not in by_name["maisya_shape"]
    assert "maisya_incoming_event" not in by_name


@pytest.mark.asyncio
async def test_dual_source_admins_and_anti_lockout(tmp_path: Path) -> None:
    plugin = await _load_plugin(
        tmp_path,
        host_config={"plugin": {"permission": [f"qq:{TEST_ADMIN_ID}", "qq:HostAdmin"]}},
        plugin_config={"admins": [f"qq:{TEST_ADMIN_ID}", "qq:SettingsAdmin"]},
    )
    try:
        info = plugin.admins_info
        assert isinstance(plugin.grants, PermissionGrantStore)
        assert isinstance(plugin.host_runner, HostWorkspaceRunner)
        assert isinstance(plugin.docker_runner, DockerRunner)
        assert plugin.onboarding is not None

        assert info["settings_admins"] == [f"qq:{TEST_ADMIN_ID}", "qq:SettingsAdmin"]
        assert set(info["host_admins"]) == {f"qq:{TEST_ADMIN_ID}", "qq:HostAdmin"}
        assert "qq:SettingsAdmin" in info["effective_admins"]
        # Case preserved + exact duplicate merged between host and settings admins
        assert f"qq:{TEST_ADMIN_ID}" in info["effective_admins"]
        assert info["effective_admins"].count(f"qq:{TEST_ADMIN_ID}") == 1

        # Clearing settings_admins succeeds while host_admins cover effective admins
        res_empty = await plugin.manage_write("settings_update", json.dumps({"admins": []}))
        assert res_empty["success"] is True

        # Removing all admins when host_admins is ALSO empty must trigger anti-lockout error
        plugin._ctx.config = SimpleNamespace(get=_make_async_config_getter({"plugin": {"permission": []}}))
        await plugin._refresh_admins()
        res_lockout = await plugin.manage_write("settings_update", json.dumps({"admins": []}))
        assert res_lockout["success"] is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_verified_admin_dm_passthrough_and_shape_delayed_tool(tmp_path: Path) -> None:
    plugin = await _load_plugin(
        tmp_path,
        plugin_config={"enabled_chats": []},
    )
    try:
        dm_admin_sess = f"dm_{TEST_ADMIN_ID}"
        dm_other_sess = f"dm_{TEST_OTHER_ID}"
        admin_dm = _dm_message(TEST_ADMIN_ID, "帮我看下最新状态", msg_id="dm-admin-1", session_id=dm_admin_sess)
        other_dm = _dm_message(TEST_OTHER_ID, "帮我执行命令", msg_id="dm-other-1", session_id=dm_other_sess)

        assert await plugin.after_receive(message=admin_dm) == {"action": "continue"}
        assert await plugin.after_receive(message=other_dm) == {"action": "continue"}

        assert plugin.is_verified_admin_dm("dm-admin-1", dm_admin_sess, "qq") is True
        assert plugin.is_verified_admin_dm("dm-other-1", dm_other_sess, "qq") is False

        tools_in = [
            {"name": "maisya_agent"},
            {"name": "maisya_recall"},
            {"name": "maisya_later"},
            {"name": "subagent"},
            {"name": "reply"},
        ]
        admin_hook = await plugin.before_planner(
            session_id=dm_admin_sess,
            tool_definitions=list(tools_in),
            items=[],
        )
        admin_defs = (admin_hook.get("modified_kwargs") or {}).get("tool_definitions", tools_in)
        admin_tool_names = [t["name"] for t in admin_defs]
        assert "maisya_agent" in admin_tool_names
        assert "maisya_later" in admin_tool_names
        assert "subagent" not in admin_tool_names

        # Unenabled non-admin DM strips Maisya tools
        other_hook = await plugin.before_planner(
            session_id=dm_other_sess,
            tool_definitions=list(tools_in),
            items=[],
        )
        other_defs = (other_hook.get("modified_kwargs") or {}).get("tool_definitions", [])
        other_tool_names = [t["name"] for t in other_defs]
        assert "maisya_agent" not in other_tool_names
        assert "maisya_later" not in other_tool_names

        denied_agent = await plugin.agent_tool(
            action="start",
            profile="default",
            message="运行诊断",
            source_msg_id="dm-other-1",
            chat_id=dm_other_sess,
            platform="qq",
        )
        assert denied_agent["success"] is False

        # Spoofed/missing source_msg_id cannot gain admin privileges
        assert plugin.authorize_admin_source("forged-missing-id", dm_admin_sess, "qq") is False

        # Verified admin DM passes without enabled_chats
        ok_agent = await plugin.agent_tool(
            action="start",
            profile="default",
            message="总结一下当前工作区状态",
            source_msg_id="dm-admin-1",
            chat_id=dm_admin_sess,
            platform="qq",
        )
        assert ok_agent["success"] is True
        assert ok_agent.get("thread_id")

        # Verified admin can inspect overview via maisya_shape; non-admin is rejected
        shape_denied = await plugin.shape_tool(
            action="overview",
            source_msg_id="dm-other-1",
            chat_id=dm_other_sess,
            platform="qq",
        )
        assert shape_denied["success"] is False
        shape_ok = await plugin.shape_tool(
            action="overview",
            source_msg_id="dm-admin-1",
            chat_id=dm_admin_sess,
            platform="qq",
        )
        assert shape_ok["success"] is True
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_dual_track_execution_and_risk_gate_approval_loop(tmp_path: Path) -> None:
    phase = {"step": 0}

    def fake_llm(cap: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if cap != "llm.generate_with_tools":
            return {"success": True}
        phase["step"] += 1
        msgs = kwargs.get("prompt") or []
        last_role = msgs[-1].get("role") if isinstance(msgs, list) and msgs else ""
        if last_role != "tool":
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": f"call-exec-{phase['step']}",
                        "type": "function",
                        "function": {
                            "name": "exec_command",
                            "arguments": json.dumps({"command": "rm -rf ./tmp_cache && echo cleaned"}),
                        },
                    }
                ],
            }
        return {"success": True, "response": "已完成操作确认并清理结束。", "tool_calls": []}

    plugin = await _load_plugin(
        tmp_path,
        plugin_config={
            "enabled_chats": [f"qq:group:{TEST_GROUP_ID}"],
            "permission_mode": "ask_risky",
            "sandbox": {"enabled": False},
        },
        call_capability_fn=fake_llm,
    )
    try:
        # Create an exec-capable profile accessible in group chat
        exec_profile_res = await plugin.manage_write(
            "profile_create",
            json.dumps(
                {
                    "profile_id": "ops-runner",
                    "name": "Ops Runner",
                    "description": "Operations profile with exec toolset",
                    "instructions": "Run commands carefully in workspace.",
                    "toolsets": ["context", "workspace", "exec"],
                    "session_mode": "ephemeral",
                    "access": "everyone",
                    "permission_mode": "ask_risky",
                }
            ),
        )
        assert exec_profile_res["success"] is True

        # 1. Non-admin in enabled group: arbitrary exec_command hard-blocked when Docker is off
        grp_other = _group_message(TEST_OTHER_ID, "跑个命令看看", msg_id="grp-other-1")
        await plugin.after_receive(message=grp_other)
        res_non_admin = await plugin.agent_tool(
            action="start",
            profile="ops-runner",
            message="执行 rm -rf",
            source_msg_id="grp-other-1",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert res_non_admin["success"] is True
        snap_non_admin = await plugin.console_query(thread_id=res_non_admin["thread_id"])
        assert snap_non_admin["selected"]["pending_approval"] is None
        # Tool call should be error/denied inside step trace without touching host
        non_admin_steps = json.dumps(snap_non_admin["selected"]["steps"], ensure_ascii=False)
        assert "禁止执行任意" in non_admin_steps or "Docker" in non_admin_steps

        # 2. Docker enabled routes non-admin group execution to credential-free DockerRunner
        docker_calls: list[Any] = []

        async def _fake_docker_exec(req: Any) -> ExecutionResult:
            docker_calls.append(req)
            return ExecutionResult(
                status="ok",
                exit_code=0,
                stdout="sandboxed-ok",
                stderr="",
                runner_name="docker",
            )

        plugin.settings.sandbox.enabled = True
        plugin.runtime.sandbox.ready = True
        plugin.docker_runner.execute = _fake_docker_exec  # type: ignore[method-assign]

        res_docker = await plugin.agent_tool(
            action="start",
            profile="ops-runner",
            message="沙箱跑一个检查",
            source_msg_id="grp-other-1",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert res_docker["success"] is True
        assert len(docker_calls) == 1
        assert docker_calls[0].is_admin is False
        assert docker_calls[0].secrets == ()
        assert docker_calls[0].mount_secret_files is False

        # 3. Disable Docker and test verified admin host risk gate -> waiting -> console + planner approve
        plugin.settings.sandbox.enabled = False
        plugin.runtime.sandbox.ready = False

        grp_admin = _group_message(TEST_ADMIN_ID, "帮我执行清理", msg_id="grp-admin-1")
        await plugin.after_receive(message=grp_admin)

        res_wait = await plugin.agent_tool(
            action="start",
            profile="ops-runner",
            message="清理缓存目录",
            source_msg_id="grp-admin-1",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert res_wait["success"] is True
        assert res_wait["state"] == "waiting"
        thread_id = res_wait["thread_id"]

        snap = await plugin.console_query(thread_id=thread_id)
        assert snap["ui_style"] == "millennium"
        assert snap["permission_mode"] == "ask_risky"
        assert "admins_info" in snap
        assert "docker" in snap
        assert "onboarding" in snap
        assert snap["selected"]["pending_approval"] is not None
        assert snap["selected"]["pending_approval"]["tool"] == "exec_command"
        assert "context" in snap["selected"]
        assert "tokens" in snap["selected"]["context"]
        assert "token_limit" in snap["selected"]["context"]
        assert "roles" in snap["selected"]["context"]
        assert isinstance(snap["selected"]["steps"], list)

        now_detail = await plugin.manage_read("now_detail", json.dumps({"thread_id": thread_id}))
        assert now_detail["success"] is True
        assert now_detail["selected"]["pending_approval"]["tool"] == "exec_command"

        # Non-admin cannot approve
        denied_approve = await plugin.agent_tool(
            action="approve",
            thread_id=thread_id,
            grant_tier="once",
            source_msg_id="grp-other-1",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert denied_approve["success"] is False

        # Verified admin approves via maisya_agent(action="approve", grant_tier="session")
        approve_agent = await plugin.agent_tool(
            action="approve",
            thread_id=thread_id,
            grant_tier="session",
            source_msg_id="grp-admin-1",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert approve_agent["success"] is True
        assert approve_agent["resolved"] is True

        grants_res = await plugin.manage_read("grants", "{}")
        assert grants_res["success"] is True
        assert len(grants_res["items"]) >= 1
        grant_key = grants_res["items"][0]["grant_key"]

        revoke_res = await plugin.manage_write(
            "grant_revoke", json.dumps({"grant_key": grant_key})
        )
        assert revoke_res["success"] is True
        assert len(revoke_res["items"]) == 0

        # 4. Also verify manage_write("thread_approve", decision="always") path
        res_wait2 = await plugin.agent_tool(
            action="start",
            profile="ops-runner",
            message="再运行一次清理确认",
            source_msg_id="grp-admin-1",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert res_wait2["success"] is True
        assert res_wait2["state"] == "waiting"
        tid2 = res_wait2["thread_id"]

        ui_approve = await plugin.manage_write(
            "thread_approve",
            json.dumps({"thread_id": tid2, "decision": "always"}),
        )
        assert ui_approve["success"] is True
        grants_after_ui = await plugin.manage_read("grants", "{}")
        assert any(g["tier"] == "always" for g in grants_after_ui["items"])
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_thread_reuse_fresh_stop_idle_reopen_and_list(tmp_path: Path) -> None:
    plugin = await _load_plugin(
        tmp_path,
        plugin_config={"enabled_chats": [f"qq:group:{TEST_GROUP_ID}"]},
    )
    try:
        grp_admin = _group_message(TEST_ADMIN_ID, "开启持久工作", msg_id="grp-adm-thread")
        await plugin.after_receive(message=grp_admin)

        create_res = await plugin.manage_write(
            "profile_create",
            json.dumps(
                {
                    "profile_id": "research-bot",
                    "name": "Research Bot",
                    "description": "Long-lived researcher",
                    "instructions": "Keep track of research notes carefully.",
                    "toolsets": ["workspace"],
                }
            ),
        )
        assert create_res["success"] is True

        first = await plugin.agent_tool(
            action="start",
            profile="research-bot",
            message="记录第一条进展",
            source_msg_id="grp-adm-thread",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert first["success"] is True
        tid1 = first["thread_id"]

        # Persistent custom profile reuses primary thread when fresh=False
        second = await plugin.agent_tool(
            action="start",
            profile="research-bot",
            message="补充第二条进展",
            source_msg_id="grp-adm-thread",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert second["success"] is True
        assert second["thread_id"] == tid1

        # Stop transitions state to "idle" (never "closed")
        stopped = await plugin.agent_tool(
            action="stop",
            thread_id=tid1,
            source_msg_id="grp-adm-thread",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert stopped["success"] is True
        assert stopped["state"] == "idle"

        # fresh=True archives old thread and opens a brand-new thread id
        fresh_run = await plugin.agent_tool(
            action="start",
            profile="research-bot",
            fresh=True,
            message="开一条新线程",
            source_msg_id="grp-adm-thread",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert fresh_run["success"] is True
        tid2 = fresh_run["thread_id"]
        assert tid2 != tid1

        listed = await plugin.agent_tool(
            action="list",
            profile="research-bot",
            source_msg_id="grp-adm-thread",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert listed["success"] is True
        listed_ids = {row["thread_id"] for row in listed["threads"]}
        assert tid1 in listed_ids
        assert tid2 in listed_ids

        reopened = await plugin.agent_tool(
            action="reopen",
            thread_id=tid1,
            message="续接旧线程",
            source_msg_id="grp-adm-thread",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert reopened["success"] is True
        assert reopened["thread_id"] == tid1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_later_watch_pulse_and_management_intent_controls(tmp_path: Path) -> None:
    plugin = await _load_plugin(
        tmp_path,
        plugin_config={"enabled_chats": [f"qq:group:{TEST_GROUP_ID}"]},
    )
    try:
        dm_admin_sess = f"dm_{TEST_ADMIN_ID}"
        admin_dm = _dm_message(TEST_ADMIN_ID, "盯住发布状态", msg_id="dm-adm-watch", session_id=dm_admin_sess)
        other_grp = _group_message(TEST_OTHER_ID, "取消任务", msg_id="grp-other-watch")
        await plugin.after_receive(message=admin_dm)
        await plugin.after_receive(message=other_grp)

        watch_res = await plugin.later_tool(
            action="schedule",
            mode="watch",
            condition="发现新 release tag 时通知",
            every_seconds=300,
            prompt="检查仓库 tag",
            source_msg_id="dm-adm-watch",
            chat_id=dm_admin_sess,
            platform="qq",
        )
        assert watch_res["success"] is True
        intent_id = watch_res["intent_id"]

        snap = await plugin.console_query()
        w_item = next(w for w in snap["watches"] if w["intent_id"] == intent_id)
        assert w_item["mode"] == "watch"
        assert "release tag" in w_item["condition"]

        # Non-admin cannot cancel scheduled_admin intent
        non_admin_cancel = await plugin.later_tool(
            action="cancel",
            intent_id=intent_id,
            source_msg_id="grp-other-watch",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
        )
        assert non_admin_cancel["success"] is False

        for act in ("intent_pause", "intent_resume", "intent_run_now", "intent_delete"):
            res = await plugin.manage_write(act, json.dumps({"intent_id": intent_id}))
            assert res["success"] is True, f"{act} failed: {res}"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_onboarding_rpc_and_probe_model_scrubbing(tmp_path: Path) -> None:
    async def leaking_probe(cap: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": False,
            "error": (
                "Upstream failed at https://secret-gateway.internal/v1/chat "
                "with Authorization: Bearer sk-verysecretkey123456"
            ),
        }

    # Unconfigured fresh install -> show=True
    plugin = await _load_plugin(
        tmp_path,
        host_config={"plugin": {"permission": []}},
        plugin_config={"enabled_chats": [], "admins": []},
        call_capability_fn=leaking_probe,
        write_model_toml=True,
    )
    try:
        ob = await plugin.manage_read("onboarding", "{}")
        assert ob["success"] is True
        assert ob["show"] is True

        verify_fail = await plugin.manage_write(
            "onboarding_verify_model", json.dumps({"model_name": "main-chat-model"})
        )
        assert verify_fail["success"] is True
        verify_msg = json.dumps(verify_fail, ensure_ascii=False)
        assert "secret-gateway.internal" not in verify_msg
        assert "sk-verysecretkey123456" not in verify_msg

        async def ok_probe(cap: str, **kwargs: Any) -> dict[str, Any]:
            return {"success": True, "response": "ok"}

        plugin._ctx.call_capability = ok_probe
        verify_ok = await plugin.probe_model(model_name="main-chat-model")
        assert verify_ok["ok"] is True

        for act, payload in (
            ("onboarding_advance", {"step": "admins"}),
            ("onboarding_skip", {}),
            ("onboarding_reopen", {}),
            ("onboarding_reset", {}),
            ("onboarding_complete", {}),
        ):
            res = await plugin.manage_write(act, json.dumps(payload))
            assert res["success"] is True, f"{act} failed: {res}"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_management_profiles_zip_fs_vault_models_and_settings(tmp_path: Path) -> None:
    plugin = await _load_plugin(
        tmp_path,
        plugin_config={"enabled_chats": [f"qq:group:{TEST_GROUP_ID}"]},
        write_model_toml=True,
    )
    try:
        assert (
            await plugin.manage_write(
                "profile_create",
                json.dumps(
                    {
                        "profile_id": "codex-helper",
                        "name": "Codex Helper",
                        "description": "Code review helper",
                        "instructions": "Inspect patches carefully.",
                    }
                ),
            )
        )["success"] is True

        assert (
            await plugin.manage_write(
                "profile_update",
                json.dumps({"profile_id": "codex-helper", "name": "Codex Helper v2"}),
            )
        )["success"] is True

        assert (
            await plugin.manage_write(
                "profile_clone",
                json.dumps({"source_id": "codex-helper", "target_id": "codex-clone"}),
            )
        )["success"] is True

        exported = await plugin.manage_write(
            "profile_export_zip", json.dumps({"profile_id": "codex-helper"})
        )
        assert exported["success"] is True
        zip_bytes = base64.b64decode(exported["base64"])
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            assert any(name.endswith("profile.toml") for name in zf.namelist())

        assert (
            await plugin.manage_write(
                "profile_delete", json.dumps({"profile_id": "codex-helper"})
            )
        )["success"] is True

        imported = await plugin.manage_write(
            "profile_import_zip",
            json.dumps({"base64": exported["base64"], "profile_id": "codex-helper"}),
        )
        assert imported["success"] is True

        # fs_write / fs_list / fs_read + path traversal blocked
        fs_ok = await plugin.manage_write(
            "fs_write",
            json.dumps({"path": "JOURNAL.md", "content": "# Journal\n测试手账写入\n"}),
        )
        assert fs_ok["success"] is True
        fs_read = await plugin.manage_read("fs_read", json.dumps({"path": "JOURNAL.md"}))
        assert fs_read["success"] is True
        assert "测试手账写入" in fs_read["content"]

        fs_escape = await plugin.manage_write(
            "fs_write",
            json.dumps({"path": "../../outside.txt", "content": "bad"}),
        )
        assert fs_escape["success"] is False

        # vault_set / vault_list (no plaintext) / vault_delete
        assert (
            await plugin.manage_write(
                "vault_set",
                json.dumps(
                    {
                        "handle": "demo_token",
                        "value": "super-secret-value-987654",
                        "note": "Demo API handle",
                    }
                ),
            )
        )["success"] is True
        vault_list = await plugin.manage_read("vault_list", "{}")
        assert vault_list["success"] is True
        assert "super-secret-value-987654" not in json.dumps(vault_list)
        assert (
            await plugin.manage_write("vault_delete", json.dumps({"handle": "demo_token"}))
        )["success"] is True

        # models exposes NO api_key or base_url
        models_res = await plugin.manage_read("models", "{}")
        assert models_res["success"] is True
        assert "main-chat-model" in models_res["available_models"]
        models_dump = json.dumps(models_res)
        assert "sk-host-secret" not in models_dump
        assert "private-host-endpoint" not in models_dump

        # settings_update canonicalizes maiwork -> modern
        upd = await plugin.manage_write(
            "settings_update",
            json.dumps({"ui_style": "maiwork", "daily_proactive_limit": 5}),
        )
        assert upd["success"] is True
        settings_res = await plugin.manage_read("settings", "{}")
        assert settings_res["ui_style"] == "modern"
        assert settings_res["settings"]["daily_proactive_limit"] == 5

        for section in ("profiles", "skills", "connectors", "library", "fs_list"):
            sec_res = await plugin.manage_read(section, "{}")
            assert sec_res["success"] is True, f"read section {section} failed: {sec_res}"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_wave2_cross_branch_rpc_alignment_fixes(tmp_path: Path) -> None:
    plugin = await _load_plugin(
        tmp_path,
        host_config={"plugin": {"permission": ["100001"]}},
        plugin_config={"enabled_chats": [], "admins": []},
        write_model_toml=True,
    )
    try:
        # Record a recent DM so recent_dms & known_chats are populated
        dm_msg = _dm_message("100246809", "hello dm", msg_id="dm-align-1", session_id="dm_align_1")
        await plugin.after_receive(message=dm_msg)

        # 1) Profile create/update compatibility: connectors, allowed_chats, access="chats", session_mode="thread"
        created = await plugin.manage_write(
            "profile_create",
            json.dumps(
                {
                    "profile_id": "ui-compat-role",
                    "name": "UI Compat Role",
                    "access": "chats",
                    "session_mode": "thread",
                    "allowed_chats": [f"qq:group:{TEST_GROUP_ID}"],
                    "connectors": ["github_ro"],
                    "instructions": "Compat profile body",
                }
            ),
        )
        assert created["success"] is True, created
        prof_data = created["profile"]
        assert prof_data["access"] == "listed"
        assert prof_data["session_mode"] == "ephemeral"
        assert prof_data["mcp_servers"] == ["github_ro"]
        assert prof_data["mcp"] == ["github_ro"]
        assert "github_ro" in prof_data["connectors"]
        assert prof_data["allow_chats"] == [f"qq:group:{TEST_GROUP_ID}"]
        assert prof_data["enabled_chats"] == [f"qq:group:{TEST_GROUP_ID}"]
        assert prof_data["allowed_chats"] == [f"qq:group:{TEST_GROUP_ID}"]

        # 2) Onboarding alignment: top-level run_id/sequence, target_step, onboarding wrapper, sandbox_enabled
        ob = await plugin.manage_read("onboarding", "{}")
        assert ob["success"] is True
        assert ob["run_id"]
        assert isinstance(ob["sequence"], int)
        assert "100001" in "".join(ob["host_admins"])
        assert isinstance(ob["recent_dms"], list) and len(ob["recent_dms"]) >= 1
        assert isinstance(ob["known_chats"], list)
        assert "sandbox_enabled" in ob

        snap = await plugin.console_query()
        assert snap["chat_meta"] == snap["chat_sources"]
        assert "sandbox_enabled" in snap
        assert snap["onboarding"]["run_id"] == ob["run_id"]

        adv1 = await plugin.manage_write(
            "onboarding_advance",
            json.dumps(
                {
                    "step": "hello",
                    "target_step": "admins",
                    "run_id": ob["run_id"],
                    "sequence": ob["sequence"],
                    "data": {},
                }
            ),
        )
        assert adv1["success"] is True
        assert adv1["current_step"] == "admins"
        assert adv1["onboarding"]["current_step"] == "admins"

        adv_runtime = await plugin.manage_write(
            "onboarding_advance",
            json.dumps(
                {
                    "step": "runtime",
                    "target_step": "done",
                    "run_id": ob["run_id"],
                    "sequence": adv1["sequence"],
                    "data": {
                        "default_model": "main-chat-model",
                        "permission_mode": "ask_risky",
                        "sandbox_enabled": True,
                    },
                }
            ),
        )
        assert adv_runtime["success"] is True
        assert adv_runtime["current_step"] == "done"
        assert adv_runtime["sandbox_enabled"] is True
        assert adv_runtime["onboarding"]["sandbox_enabled"] is True

        settings_after_onb = await plugin.manage_read("settings", "{}")
        assert settings_after_onb["sandbox_enabled"] is True
        assert settings_after_onb["settings"]["sandbox"]["enabled"] is True

        # Top-level settings_update with sandbox_enabled=False
        upd_sb = await plugin.manage_write(
            "settings_update", json.dumps({"sandbox_enabled": False})
        )
        assert upd_sb["success"] is True
        assert upd_sb["sandbox_enabled"] is False
        assert upd_sb["settings"]["sandbox"]["enabled"] is False

        # Stale probe ticket is rejected cleanly without mutating newer run_id
        stale_verify = await plugin.manage_write(
            "onboarding_verify_model",
            json.dumps({"run_id": "stale-run-id", "sequence": 0, "probe_sequence": 7}),
        )
        assert stale_verify["success"] is True
        assert stale_verify["stale"] is True
        assert stale_verify["onboarding"]["stale"] is True

        # 3) _artifact accepts kind="workspace_out" and infers kind when omitted
        prof = plugin.profiles.get("default")
        trow = plugin.threads.create(
            prof,
            "整理总结文档",
            chat_id=TEST_GROUP_SESSION,
            platform="qq",
            user_id=TEST_ADMIN_ID,
            workspace="art-align-ws",
        )
        ws_id = trow["workspace_id"]
        with plugin.runtime.files.activity(trow) as work:
            work.write("out/summary.md", "# Summary\nok\n")
            work.commit()

        art_out = await plugin.manage_read(
            "artifact",
            json.dumps(
                {
                    "kind": "workspace_out",
                    "workspace_id": ws_id,
                    "path": "out/summary.md",
                }
            ),
        )
        assert art_out["success"] is True
        assert "Summary" in art_out["text"]

        art_inferred = await plugin.manage_read(
            "artifact",
            json.dumps(
                {
                    "workspace_id": ws_id,
                    "path": "out/summary.md",
                }
            ),
        )
        assert art_inferred["success"] is True
        assert "Summary" in art_inferred["text"]
    finally:
        await plugin.on_unload()
