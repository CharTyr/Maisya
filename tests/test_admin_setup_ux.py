import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from maisya.abilities import WORKSPACE_TOOLS, Profiles
from maisya.memory import MemoryLayers
from maisya.scout import TOOL_DEFINITIONS, Scout, validate_arguments
from maisya.shaping import (
    DEFAULT_DIGEST_SCHEDULE_PROMPT,
    execute_shape,
    normalize_profile_workspace,
    normalize_sleep_window,
)
from maisya.threads import ThreadStore
from maisya.vault import Vault
from plugin import _profile_capabilities, create_plugin


def _find_schema_union_types(node, path="root"):
    hits = []
    if isinstance(node, dict):
        tval = node.get("type")
        if isinstance(tval, list):
            hits.append((path, tval))
        for k, v in node.items():
            hits.extend(_find_schema_union_types(v, f"{path}.{k}"))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            hits.extend(_find_schema_union_types(item, f"{path}[{idx}]"))
    return hits


async def _boot_setup_plugin(
    tmp_path: Path,
    *,
    permissions=("qq:Alice",),
    enabled_chats=(),
    streams=(),
    llm_response="已完成测试摘要",
):
    async def get(key, default=None):
        return list(permissions) if key == "plugin.permission" else default

    llm_calls = []

    async def call_capability(name, **arguments):
        llm_calls.append((name, arguments))
        if name == "chat.get_all_streams":
            return {"success": True, "streams": list(streams)}
        if name == "llm.generate_with_tools":
            return {"success": True, "response": llm_response, "tool_calls": []}
        if name == "maisaka.reply.generate":
            return {"success": True, "text": llm_response}
        if name == "send.hybrid":
            return {"success": True, "sent": True, "message_id": "sent-1"}
        return {"success": True}

    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya.setup_ux"),
        call_capability=call_capability,
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": list(enabled_chats),
        }
    )
    await plugin.on_load()
    return plugin, llm_calls


def _group_msg(
    message_id: str,
    text: str,
    *,
    session_id: str = "c0300b39c80870992aca16d9fcb724c5",
    group_id: str = "100246809",
    user_id: str = "Alice",
    platform: str = "qq",
):
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": "1794500000.0",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": {"group_id": group_id},
            "additional_config": {"relayed_by_plugin": ""},
        },
    }


def _private_msg(
    message_id: str,
    text: str,
    *,
    session_id: str = "priv_admin_md5",
    user_id: str = "Alice",
    platform: str = "qq",
):
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": platform,
        "timestamp": "1794500000.0",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": {},
            "additional_config": {"relayed_by_plugin": ""},
        },
    }


@pytest.mark.asyncio
async def test_scout_step_injects_beijing_date_and_schemas_have_no_unions(tmp_path):
    captured = []

    async def call_capability(name, **arguments):
        captured.append((name, arguments))
        return {"success": True, "response": "ok", "tool_calls": []}

    scout = Scout(SimpleNamespace(call_capability=call_capability))
    profiles = Profiles(tmp_path / "profiles")
    default_prof = profiles.get("default")
    await scout.step(default_prof, [{"role": "user", "content": "查最近进展"}])

    assert captured
    sys_msg = captured[0][1]["prompt"][0]["content"]
    today_bj = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    assert f"当前现实时间：{today_bj}" in sys_msg
    assert "UTC+8，北京时间" in sys_msg

    assert "render_report" in WORKSPACE_TOOLS
    assert "render_report" in TOOL_DEFINITIONS
    assert "collect" in TOOL_DEFINITIONS["digest_feed"]["function"]["parameters"]["properties"]["action"]["enum"]
    assert "seed" in TOOL_DEFINITIONS["digest_feed"]["function"]["parameters"]["properties"]["action"]["enum"]
    for prop in ("queries", "topics", "broad"):
        assert prop in TOOL_DEFINITIONS["digest_feed"]["function"]["parameters"]["properties"]

    validate_arguments(
        "render_report",
        {
            "path": "reports/indie.html",
            "title": "独立开发周报",
            "subtitle": "真实数据核验",
            "blocks": [{"type": "kpi", "items": [{"label": "项目数", "value": "12"}]}],
            "theme": "light",
            "layout": "page",
            "source_text": "事实来源摘录",
        },
    )
    validate_arguments("digest_feed", {"action": "collect", "queries": ["AI"], "broad": True})
    validate_arguments("digest_feed", {"action": "seed", "topics": ["独立开发"], "broad": False})

    from dataclasses import replace

    assert _find_schema_union_types(TOOL_DEFINITIONS) == []
    assert "render_report" in _profile_capabilities(
        replace(default_prof, environment="sandbox", tools=(*default_prof.tools, "render_report")),
        "sandbox",
    )
    assert "render_report" in _profile_capabilities(
        replace(default_prof, tools=(*default_prof.tools, "render_report")),
        "readonly",
    )

    plugin, _ = await _boot_setup_plugin(tmp_path / "plug")
    try:
        from plugin import MaisyaPlugin

        shape_fn = MaisyaPlugin.shape_tool
        comp_info = getattr(shape_fn, "__maibot_component_info__", None)
        assert comp_info is not None
        assert isinstance(comp_info.metadata, dict)
        assert "visibility" not in comp_info.metadata
        assert isinstance(comp_info.parameters_raw, dict)
        action_enum = set(comp_info.parameters_raw["properties"]["action"]["enum"])
        kind_enum = set(comp_info.parameters_raw["properties"]["kind"]["enum"])
        assert {"overview", "setup", "settings", "profile", "file"}.issubset(action_enum)
        assert {"overview", "setup", "settings", "profile", "file"}.issubset(kind_enum)
        assert _find_schema_union_types(comp_info.parameters_raw) == []

        rejected = await plugin.shape_tool(
            action="overview",
            source_msg_id="",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert rejected["success"] is False

        await plugin.after_receive(message=_private_msg("schema-admin-m1", "检查配置"))
        res_overview = await plugin.shape_tool(
            action="overview",
            source_msg_id="schema-admin-m1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res_overview["success"] is True
        assert res_overview["kind"] == "overview"
        assert [p["profile_id"] for p in res_overview["profiles"]] == ["default", "digest", "scout"]
    finally:
        await plugin.on_unload()


def test_profiles_idempotent_upgrade_preserves_custom_fields(tmp_path):
    profiles_dir = tmp_path / "abilities/profiles"
    default_dir = profiles_dir / "default"
    default_dir.mkdir(parents=True)

    (default_dir / "PROFILE.md").write_text("# 自定义默认助手\n", encoding="utf-8")
    (default_dir / "profile.toml").write_text(
        "\n".join(
            [
                'title = "默认助手"',
                "enabled = true",
                'model = "custom-default-model"',
                'fallback_models = ["backup-m"]',
                'task = "planner"',
                'environment = "sandbox"',
                'network = "public"',
                'access = "listed"',
                'allow_chats = ["qq:group:100246809"]',
                'skills = ["research-report"]',
                'tools = ["web_search", "web_fetch", "chat_read", "message_read", "memory_read", "person_read", "recall_read", "image_read", "workspace_file", "html_render", "sandbox_exec", "result_attach"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    p1 = Profiles(profiles_dir)
    prof = p1.get("default")
    assert prof.skills == ("research-report",)
    assert prof.enabled is True
    assert prof.model == "custom-default-model"
    assert prof.fallback_models == ("backup-m",)
    assert prof.allow_chats == ("qq:group:100246809",)
    assert "sandbox_exec" in _profile_capabilities(prof, "sandbox")

    first_toml = (default_dir / "profile.toml").read_text(encoding="utf-8")
    p2 = Profiles(profiles_dir)
    assert (default_dir / "profile.toml").read_text(encoding="utf-8") == first_toml
    assert p2.get("default").skills == ("research-report",)
    assert p2.get("default").model == "custom-default-model"


def test_memory_chat_id_latest_write_wins_and_news_digest_refresh(tmp_path):
    root = tmp_path / "maisya"
    root.mkdir(parents=True, exist_ok=True)
    store = ThreadStore(root / "threads.db")
    profiles = Profiles(root / "abilities/profiles")
    vault = Vault(root / "vault")

    t = 1000.0
    memory = MemoryLayers(root, store, profiles, vault, clock=lambda: t)
    memory.chat_aliases = lambda cid: (
        ["hash_aaa_old", "hash_zzz_new", "qq:group:100246809"]
        if cid in {"hash_aaa_old", "hash_zzz_new", "qq:group:100246809"}
        else [cid]
    )

    t = 1000.0
    memory.write("memory", "旧偏好：周报", chat_id="hash_aaa_old")
    t = 2000.0
    memory._memory_path_for("hash_zzz_new")[1].parent.mkdir(parents=True, exist_ok=True)
    memory.write("memory", "最新偏好：AI与独立开发", chat_id="hash_zzz_new")

    assert "最新偏好：AI与独立开发" in memory.read("memory", chat_id="qq:group:100246809")["content"]
    assert "最新偏好：AI与独立开发" in memory.read("memory", chat_id="hash_aaa_old")["content"]

    root2 = tmp_path / "maisya_disk"
    root2.mkdir(parents=True, exist_ok=True)
    store2 = ThreadStore(root2 / "threads.db")
    profiles2 = Profiles(root2 / "abilities/profiles")
    vault2 = Vault(root2 / "vault")
    mem2 = MemoryLayers(root2, store2, profiles2, vault2)
    mem2.chat_aliases = lambda cid: (
        ["aaa_old", "zzz_new", "qq:group:100246809"]
        if cid in {"aaa_old", "zzz_new", "qq:group:100246809"}
        else [cid]
    )
    p_old = mem2._memory_path_for("aaa_old")[1]
    p_new = mem2._memory_path_for("zzz_new")[1]
    p_old.parent.mkdir(parents=True, exist_ok=True)
    p_new.parent.mkdir(parents=True, exist_ok=True)
    p_old.write_text("磁盘旧记录", encoding="utf-8")
    p_new.write_text("磁盘新记录", encoding="utf-8")
    os.utime(p_old, (1000, 1000))
    os.utime(p_new, (2000, 2000))
    assert "磁盘新记录" in mem2.read("memory", chat_id="qq:group:100246809")["content"]

    preset_text = (Path(__file__).resolve().parents[1] / "presets/skills/news-digest/SKILL.md").read_text(
        encoding="utf-8"
    )
    news_path = root / "abilities/skills/news-digest/SKILL.md"
    memory.read("skill", target="news-digest")
    news_path.write_text("旧版简报模板内容", encoding="utf-8")
    refreshed = MemoryLayers(root, store, profiles, vault, clock=lambda: 3000.0)
    assert news_path.read_text(encoding="utf-8") == preset_text
    assert refreshed.read("skill", target="news-digest")["version"] == 0

    cur = refreshed.read("skill", target="news-digest")
    refreshed.write(
        "skill",
        "---\nname: news-digest\ndescription: 自定义简报\n---\n# 用户定制内容\n",
        target="news-digest",
        expected_version=cur["version"],
    )
    again = MemoryLayers(root, store, profiles, vault, clock=lambda: 4000.0)
    assert "用户定制内容" in again.read("skill", target="news-digest")["content"]


@pytest.mark.asyncio
async def test_normalizers_and_profile_summary_shaping(tmp_path):
    assert normalize_sleep_window("1-8") == "01-08"
    assert normalize_sleep_window("01-08") == "01-08"
    assert normalize_sleep_window("1:00-8:00") == "01-08"
    assert normalize_sleep_window("01:00 - 08:00") == "01-08"
    assert normalize_sleep_window("23:30-7:00") == "23:30-07:00"
    assert normalize_sleep_window("00-24") == "00-24"
    assert normalize_sleep_window("00:00-24:00") == "00-24"
    assert normalize_sleep_window("off") == ""
    assert normalize_sleep_window("关闭") == ""
    assert normalize_sleep_window("") == ""

    assert normalize_profile_workspace("digest", "digest-feeds") == "digest"
    assert normalize_profile_workspace("digest", "digest_feeds") == "digest"
    assert normalize_profile_workspace("digest", "") == "digest"
    assert normalize_profile_workspace("maker", "digest-feeds") == "digest-feeds"

    plugin, _ = await _boot_setup_plugin(tmp_path, enabled_chats=["qq:group:100246809"])
    try:
        await plugin.after_receive(message=_group_msg("m0", "在吗"))
        await plugin.shape_tool(
            action="profile",
            op="create",
            target="digest",
            source_msg_id="m0",
            patch={
                "base": "default",
                "enabled": True,
                "network": "public",
                "model_name": "digest-x1",
                "skills": ["news-digest"],
            },
            chat_id="c0300b39c80870992aca16d9fcb724c5",
            platform="qq",
        )
        read_res = await execute_shape(
            plugin, action="profile", op="read", target="digest", admin_verified=True
        )
        assert read_res["profile"]["profile_id"] == "digest"
        assert read_res["profile"]["enabled"] is True
        assert plugin.profiles.get("digest").model_name == "digest-x1"

        sched = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            source_msg_id="m0",
            target_chat="qq:group:100246809",
            profile="digest",
            workspace="digest",
            daily_at=["08:30"],
            prompt=DEFAULT_DIGEST_SCHEDULE_PROMPT,
            chat_id="c0300b39c80870992aca16d9fcb724c5",
            platform="qq",
        )
        assert sched["success"] is True, sched
        intent = plugin.intents.get(sched["intents"][0]["intent_id"])
        assert intent["payload"]["workspace"] == "digest"
        assert intent["payload"]["prompt"] == DEFAULT_DIGEST_SCHEDULE_PROMPT

        started = await plugin.agent_tool(
            action="start",
            source_msg_id="m0",
            target_chat="qq:group:100246809",
            profile="digest",
            workspace="digest",
            message="立即试跑一次简报",
            wait_seconds=0,
            chat_id="c0300b39c80870992aca16d9fcb724c5",
            platform="qq",
        )
        assert started["success"] is True, started
        await plugin.engine.drain()
        with plugin.threads.connect() as db:
            ws_names = [
                row["name"]
                for row in db.execute(
                    "SELECT name FROM agent_workspaces WHERE profile='digest'"
                ).fetchall()
            ]
        assert ws_names and set(ws_names) == {"digest"}
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_admin_shaping_cross_chat_from_private_and_non_admin_guard(tmp_path):
    active_sid = "c0300b39c80870992aca16d9fcb724c5"
    streams = [
        {
            "session_id": active_sid,
            "platform": "qq",
            "is_group_session": True,
            "chat_type": "group",
            "group_id": "100246809",
            "group_name": "独立开发小馆",
            "account_id": "bot-1",
            "human_count": 10,
            "last_human_at": 2000.0,
        },
        {
            "session_id": "priv_admin_md5",
            "platform": "qq",
            "chat_type": "private",
            "user_id": "Alice",
            "nickname": "Alice",
            "account_id": "bot-1",
        },
    ]
    plugin, _ = await _boot_setup_plugin(tmp_path, streams=streams)
    try:
        await plugin.after_receive(message=_private_msg("priv-m1", "帮我把独立开发小馆开启并设安静时段1点到8点"))

        before = await plugin.before_planner(
            session_id="priv_admin_md5",
            items=[],
            tool_definitions=[
                {"type": "function", "function": {"name": "tool_search", "description": "s"}},
                {"type": "function", "function": {"name": "maisya_agent", "description": "a"}},
            ],
        )
        prompt_text = before["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "maisya_shape" in prompt_text and "tool_search" in prompt_text
        assert "她" not in prompt_text

        ov = await plugin.shape_tool(
            action="overview",
            target_chat="独立开发小馆",
            source_msg_id="priv-m1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert ov["success"] is True, ov
        assert any(c.get("stream_spec") == "qq:group:100246809" for c in ov["known_chats"])

        res_set = await plugin.shape_tool(
            action="settings",
            op="patch",
            source_msg_id="priv-m1",
            patch={"add_chats": ["独立开发小馆"], "sleep_window": "1:00-8:00"},
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res_set["success"] is True, res_set
        assert "qq:group:100246809" in plugin.settings.enabled_chats
        assert plugin.settings.sleep_window == "01-08"

        # Anti-lockout guard & non-admin authorization guard
        await plugin.after_receive(message=_group_msg("grp-m1", "确认", session_id=active_sid))
        lockout = await plugin.shape_tool(
            action="settings",
            op="patch",
            source_msg_id="grp-m1",
            patch={"plugin": {"enabled": False}},
            chat_id=active_sid,
            platform="qq",
        )
        assert lockout["success"] is False

        await plugin.after_receive(
            message=_group_msg("bob-m1", "我是普通群友想改配置", session_id=active_sid, user_id="Bob")
        )
        non_admin = await plugin.shape_tool(
            action="settings",
            op="patch",
            source_msg_id="bob-m1",
            patch={"sleep_window": "02-06"},
            chat_id=active_sid,
            platform="qq",
        )
        assert non_admin["success"] is False
    finally:
        await plugin.on_unload()


def test_profiles_export_falls_back_to_presets_skills(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    profiles.clone("default", "digest-custom")
    custom = profiles.update("digest-custom", skills=["news-digest"])
    assert "news-digest" in custom.skills
    package = profiles.export("digest-custom")
    assert len(package) > 0
    cloned = profiles.clone("digest-custom", "digest-copy")
    assert "news-digest" in cloned.skills
    profiles.delete("digest-copy")
    profiles.delete("digest-custom")
