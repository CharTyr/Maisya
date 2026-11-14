import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from maisya.abilities import WORKSPACE_TOOLS, Profiles
from maisya.digest import DigestWorkspace
from maisya.memory import MemoryLayers
from maisya.scout import TOOL_DEFINITIONS, Scout, validate_arguments
from maisya.shaping import (
    DEFAULT_DIGEST_SCHEDULE_PROMPT,
    normalize_profile_workspace,
    normalize_sleep_window,
    profile_summary,
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
    await scout.step(profiles.get("scout"), [{"role": "user", "content": "查最近进展"}])

    assert captured
    sys_msg = captured[0][1]["prompt"][0]["content"]
    today_bj = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    assert f"当前现实时间：{today_bj}" in sys_msg
    assert "UTC+8，北京时间" in sys_msg

    # Tool definitions & validate_arguments checks
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
    maker = profiles.get("maker")
    assert "render_report" in _profile_capabilities(
        replace(maker, tools=(*maker.tools, "render_report")), "sandbox"
    )
    assert "render_report" in _profile_capabilities(
        replace(profiles.get("scout"), tools=(*profiles.get("scout").tools, "render_report")),
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
        assert "setup" in comp_info.parameters_raw["properties"]["action"]["enum"]
        assert "setup" in comp_info.parameters_raw["properties"]["kind"]["enum"]
        assert _find_schema_union_types(comp_info.parameters_raw) == []

        rejected = await plugin.shape_tool(
            action="setup",
            kind="setup",
            source_msg_id="",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert rejected["success"] is False

        await plugin.after_receive(message=_private_msg("schema-admin-m1", "检查配置"))
        res_empty = await plugin.shape_tool(
            action="setup",
            kind="setup",
            source_msg_id="schema-admin-m1",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res_empty["success"] is True
        assert res_empty["stream_spec"] == "qq:private:Alice"
    finally:
        await plugin.on_unload()


def test_profiles_idempotent_upgrade_preserves_custom_fields(tmp_path):
    profiles_dir = tmp_path / "abilities/profiles"
    maker_dir = profiles_dir / "maker"
    digest_dir = profiles_dir / "digest"
    maker_dir.mkdir(parents=True)
    digest_dir.mkdir(parents=True)

    (maker_dir / "PROFILE.md").write_text("# 自定义创作者\n", encoding="utf-8")
    (maker_dir / "profile.toml").write_text(
        "\n".join(
            [
                'title = "创作者"',
                "enabled = false",
                'model = "custom-maker-model"',
                'fallback_models = ["backup-m"]',
                'task = "planner"',
                'environment = "sandbox"',
                'network = "public"',
                'access = "listed"',
                'allow_chats = ["qq:group:100246809"]',
                'tools = ["web_search", "web_fetch", "chat_read", "message_read", "memory_read", "person_read", "recall_read", "image_read", "workspace_file", "html_render", "sandbox_exec", "result_attach"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (digest_dir / "PROFILE.md").write_text("# 自定义简报\n", encoding="utf-8")
    (digest_dir / "profile.toml").write_text(
        "\n".join(
            [
                'title = "资讯简报"',
                "enabled = true",
                'model_name = "custom-digest-model"',
                "max_steps = 12",
                'environment = "readonly"',
                'network = "public"',
                'tools = ["chat_read", "web_search", "digest_feed"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    p1 = Profiles(profiles_dir)
    maker = p1.get("maker")
    digest = p1.get("digest")

    assert "render_report" in maker.tools
    assert maker.skills == ("research-report",)
    assert maker.enabled is False
    assert maker.model == "custom-maker-model"
    assert maker.fallback_models == ("backup-m",)
    assert maker.allow_chats == ("qq:group:100246809",)
    assert "render_report" in _profile_capabilities(maker, "sandbox")

    assert digest.max_steps == 20
    assert digest.enabled is True
    assert digest.model_name == "custom-digest-model"

    # Re-run __init__ cleanly (idempotent)
    first_toml = (maker_dir / "profile.toml").read_text(encoding="utf-8")
    p2 = Profiles(profiles_dir)
    assert (maker_dir / "profile.toml").read_text(encoding="utf-8") == first_toml
    assert p2.get("maker").skills == ("research-report",)
    assert p2.get("digest").max_steps == 20


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

    # First an alphabetically earlier session_id writes MEMORY.md
    t = 1000.0
    memory.write("memory", "旧偏好：周报", chat_id="hash_aaa_old")
    # Later a newer session_id writes MEMORY.md directly
    t = 2000.0
    memory._memory_path_for("hash_zzz_new")[1].parent.mkdir(parents=True, exist_ok=True)
    memory.write("memory", "最新偏好：AI与独立开发", chat_id="hash_zzz_new")

    # Querying by stream_spec or even old session_id must resolve to latest updated hash_zzz_new
    assert "最新偏好：AI与独立开发" in memory.read("memory", chat_id="qq:group:100246809")["content"]
    assert "最新偏好：AI与独立开发" in memory.read("memory", chat_id="hash_aaa_old")["content"]

    # Also verify st_mtime fallback when MEMORY.md files exist on disk without memory_versions entries
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

    # news-digest auto-refresh when unmodified (no version >= 1) vs preserved when edited (version >= 1)
    preset_text = (Path(__file__).resolve().parents[1] / "presets/skills/news-digest/SKILL.md").read_text(
        encoding="utf-8"
    )
    news_path = root / "abilities/skills/news-digest/SKILL.md"
    # Read v0 baseline first, then simulate legacy file content without user version>=1
    memory.read("skill", target="news-digest")
    news_path.write_text("旧版简报模板内容", encoding="utf-8")
    refreshed = MemoryLayers(root, store, profiles, vault, clock=lambda: 3000.0)
    assert news_path.read_text(encoding="utf-8") == preset_text
    assert refreshed.read("skill", target="news-digest")["version"] == 0

    # User writes version 1 -> subsequent MemoryLayers init preserves customization
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
async def test_normalizers_profile_summary_and_later_workspace_defaults(tmp_path):
    assert normalize_sleep_window("1-8") == "01-08"
    assert normalize_sleep_window("01-08") == "01-08"
    assert normalize_sleep_window("1:00-8:00") == "01-08"
    assert normalize_sleep_window("01:00 - 08:00") == "01-08"
    assert normalize_sleep_window("23:30-7:00") == "23-07"
    assert normalize_sleep_window("off") == ""
    assert normalize_sleep_window("关闭") == ""
    assert normalize_sleep_window("") == ""

    assert normalize_profile_workspace("digest", "digest-feeds") == "digest"
    assert normalize_profile_workspace("digest", "digest_feeds") == "digest"
    assert normalize_profile_workspace("digest", "") == "digest"
    assert normalize_profile_workspace("maker", "digest-feeds") == "digest-feeds"

    plugin, _ = await _boot_setup_plugin(tmp_path, enabled_chats=["qq:group:100246809"])
    try:
        # profile_summary exposes model when profile.toml uses `model = "..."`
        await plugin.after_receive(message=_group_msg("m0", "在吗"))
        await plugin.shape_tool(
            action="patch",
            kind="profile",
            target="digest",
            source_msg_id="m0",
            patch={"enabled": True, "network": "public", "model_name": "digest-x1"},
            chat_id="c0300b39c80870992aca16d9fcb724c5",
            platform="qq",
        )
        summary = profile_summary(plugin, "digest")
        assert summary["model_name"] == "digest-x1"
        assert summary["model"] == "digest-x1"

        # later_tool normalizes workspace="digest-feeds" -> "digest" & defaults prompt
        sched = await plugin.later_tool(
            action="schedule",
            source_msg_id="m0",
            target_chat="qq:group:100246809",
            profile="digest",
            workspace="digest-feeds",
            daily_at=["08:30"],
            prompt="",
            chat_id="c0300b39c80870992aca16d9fcb724c5",
            platform="qq",
        )
        assert sched["success"] is True, sched
        intent = plugin.intents.get(sched["intents"][0]["intent_id"])
        assert intent["payload"]["workspace"] == "digest"
        assert intent["payload"]["prompt"] == DEFAULT_DIGEST_SCHEDULE_PROMPT

        # agent_tool normalizes workspace="digest-feeds" -> "digest"
        started = await plugin.agent_tool(
            action="start",
            source_msg_id="m0",
            target_chat="qq:group:100246809",
            profile="digest",
            workspace="digest-feeds",
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
async def test_one_shot_setup_from_private_and_group_chat(tmp_path, monkeypatch):
    legacy_sid = "aaaa_legacy_group_sid"
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
            "session_id": legacy_sid,
            "platform": "qq",
            "is_group_session": True,
            "chat_type": "group",
            "group_id": "100246809",
            "group_name": "独立开发小馆",
            "account_id": "",
            "human_count": 1,
            "last_human_at": 100.0,
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
        # Pre-existing legacy schedule under legacy hash + old workspace "digest-feeds"
        old_intent = plugin.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:" + legacy_sid,
            due_at=plugin.intents.clock() + 3600,
            reason="旧版遗留简报",
            payload={
                "platform": "qq",
                "every_seconds": 86400,
                "daily_at": "09:00",
                "profile": "digest",
                "workspace": "digest-feeds",
            },
            bypass_limits=True,
        )
        # Pre-existing older MEMORY.md on legacy hash, and newer on active hash
        plugin.memory.write("memory", "老群偏好：随便聊聊", chat_id=legacy_sid)
        plugin.memory.write("memory", "已有偏好：关注本地离线模型", chat_id=active_sid)

        # Simulate 1 unreachable RSS feed (must go to warnings, NOT abort setup) + optional seed_starter_feeds
        seeded_calls = []

        async def fake_add_feed(self, url, *, title="", auto=True, origin="manual"):
            if "broken" in url:
                return {"ok": False, "reason": "connection timeout"}
            sources = self._load_sources()
            feed = {"id": "feed_ok_1", "url": url, "title": title or "Indie Hackers"}
            sources["feeds"].append(feed)
            self._save_sources(sources)
            self.work.commit()
            return {"ok": True, "feed": feed}

        async def fake_seed(self, topics_or_text="", *, max_add=4):
            seeded_calls.append(topics_or_text)
            sources = self._load_sources()
            if not any(f.get("id") == "seed_1" for f in sources["feeds"]):
                sources["feeds"].append({"id": "seed_1", "url": "https://indie.example/rss", "title": "Starter Indie"})
                self._save_sources(sources)
                self.work.commit()
            return {"ok": True, "seeded": [{"id": "seed_1"}]}

        monkeypatch.setattr(DigestWorkspace, "add_feed", fake_add_feed)
        monkeypatch.setattr(DigestWorkspace, "seed_starter_feeds", fake_seed, raising=False)

        # Admin speaks from un-enabled private chat priv_admin_md5
        await plugin.after_receive(message=_private_msg("priv-m1", "帮我把独立开发小馆配好每天8点半和晚上7点半的简报，安静时段1点到8点"))

        before = await plugin.before_planner(
            session_id="priv_admin_md5",
            items=[],
            tool_definitions=[
                {"type": "function", "function": {"name": "tool_search", "description": "s"}},
                {"type": "function", "function": {"name": "maisya_agent", "description": "a"}},
            ],
        )
        prompt_text = before["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "setup" in prompt_text and "maisya_shape" in prompt_text and "tool_search" in prompt_text
        assert "她" not in prompt_text

        # One-shot setup call
        res = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="独立开发小馆",
            source_msg_id="priv-m1",
            patch={
                "sleep_window": "1:00-8:00",
                "chat_proactive_level": "quiet",
                "enable_profiles": ["digest"],
                "model_name": "step-digest-preview",
                "preferences": ["独立开发产品出海", "开源 AI Agent 工具"],
                "rss_urls": ["https://broken.invalid/rss.xml", "https://valid.example/feed.xml"],
                "topics": ["独立开发", "AI工具"],
                "daily_at": ["08:30", "19:30", "08:30"],
                "run_now": True,
                "deliver_to": "target",
            },
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res["success"] is True, res
        assert res["stream_spec"] == "qq:group:100246809"
        assert "qq:group:100246809" in plugin.settings.enabled_chats
        assert plugin.settings.sleep_window == "01-08"
        assert plugin.settings.chat_proactive_levels["qq:group:100246809"] == "quiet"

        digest_info = profile_summary(plugin, "digest")
        assert digest_info["enabled"] is True
        assert digest_info["network"] == "public"
        assert digest_info["max_steps"] >= 20
        assert digest_info["model_name"] == "step-digest-preview"

        mem_text = plugin.memory.read("memory", chat_id="qq:group:100246809")["content"]
        assert "已有偏好：关注本地离线模型" in mem_text
        assert "独立开发产品出海" in mem_text
        assert "老群偏好" not in mem_text

        assert seeded_calls == ["独立开发 AI工具"]
        assert any("broken.invalid" in w for w in res["warnings"])
        assert res["applied"]["digest_sources"]["workspace"] == "digest"
        assert res["applied"]["digest_sources"]["feeds_total"] == 2

        # Legacy schedule under old hash was cancelled; 2 deduplicated schedules created in "digest" workspace
        assert plugin.intents.get(old_intent["intent_id"])["status"] == "cancelled"
        assert len(res["schedules"]) == 2
        assert {s["daily_at"] for s in res["schedules"]} == {"08:30", "19:30"}
        for s in res["schedules"]:
            row = plugin.intents.get(s["intent_id"])
            assert row["payload"]["tz_offset_minutes"] == 480
            assert row["payload"]["workspace"] == "digest"
            assert row["payload"]["prompt"] == DEFAULT_DIGEST_SCHEDULE_PROMPT

        assert res["trial_thread_id"].startswith("th_")
        assert res.get("delivery_intent_id", "").startswith("in_")
        assert "qq:group:100246809" in res["summary"]
        assert "她" not in res["summary"]

        await plugin.engine.drain()
        with plugin.threads.connect() as db:
            ws_names = [
                r["name"]
                for r in db.execute(
                    "SELECT name FROM agent_workspaces WHERE profile='digest'"
                ).fetchall()
            ]
        assert set(ws_names) == {"digest"}

        # Tolerant routing (action="setup", kind="overview") from inside enabled group + deliver_to="current"
        await plugin.after_receive(message=_group_msg("grp-m1", "帮我微调安静时段并试跑", session_id=active_sid))
        res_group = await plugin.shape_tool(
            action="setup",
            kind="overview",
            target_chat="",
            source_msg_id="grp-m1",
            patch={
                "sleep_window": "off",
                "run_now": True,
                "deliver_to": "current",
                "trial_prompt": "立即试跑一期",
            },
            chat_id=active_sid,
            platform="qq",
        )
        assert res_group["success"] is True, res_group
        assert plugin.settings.sleep_window == ""
        assert res_group["trial_thread_id"].startswith("th_")
        await plugin.engine.drain()

        # Anti-lockout guard & non-admin authorization guard
        lockout = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="qq:group:100246809",
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
            action="setup",
            kind="setup",
            target_chat="qq:group:100246809",
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
    digest = profiles.get("digest")
    assert "news-digest" in digest.skills
    package = profiles.export("digest")
    assert len(package) > 0
    cloned = profiles.clone("digest", "digest-copy")
    assert "news-digest" in cloned.skills
    profiles.delete("digest-copy")
