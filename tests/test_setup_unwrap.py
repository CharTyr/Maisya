import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from maisya.digest import DigestWorkspace
from maisya.shaping import (
    _coerce_bool,
    _coerce_int,
    _unwrap_item_wrappers,
    handle_digest_sources,
    patch_profile,
    patch_settings,
)
from plugin import create_plugin


async def _boot_plugin(tmp_path: Path, *, enabled_chats=(), streams=()):
    async def get(key, default=None):
        return ["qq:Alice"] if key == "plugin.permission" else default

    async def call_capability(name, **arguments):
        if name == "chat.get_all_streams":
            return {"success": True, "streams": list(streams)}
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "ok", "tool_calls": []}
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "ok"}
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
        logger=logging.getLogger("test.maisya.setup_unwrap"),
        call_capability=call_capability,
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": list(enabled_chats),
        }
    )
    await plugin.on_load()
    return plugin


def _private_msg(message_id: str, text: str, *, session_id: str = "priv_admin_md5"):
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": "qq",
        "timestamp": "1794500000.0",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": "Alice"},
            "group_info": {},
            "additional_config": {"relayed_by_plugin": ""},
        },
    }


def test_unwrap_item_wrappers_recursive_and_cycle_safe():
    wrapped = {
        "daily_at": {"item": ["09:00", "20:00"]},
        "enable_chat": {"item": {"item": "true"}},
        "profiles": {
            "digest": {
                "enabled": {"item": "true"},
                "model_list": {"item": [{"item": "m1"}, "m2"]},
            }
        },
        "keep_multi": {"item": "val", "other": 1},
        "schedules": [
            {"item": {"daily_at": {"item": ["08:30"]}, "tz_offset_minutes": {"item": "480"}}}
        ],
    }
    assert _unwrap_item_wrappers(wrapped) == {
        "daily_at": ["09:00", "20:00"],
        "enable_chat": "true",
        "profiles": {
            "digest": {
                "enabled": "true",
                "model_list": ["m1", "m2"],
            }
        },
        "keep_multi": {"item": "val", "other": 1},
        "schedules": [{"daily_at": ["08:30"], "tz_offset_minutes": "480"}],
    }

    # Shared non-cyclic DAG node must unwrap cleanly on both paths
    shared = {"item": "09:00"}
    assert _unwrap_item_wrappers({"a": shared, "b": shared}) == {"a": "09:00", "b": "09:00"}

    # Cyclic item wrapper must not raise RecursionError or hang
    cyclic_dict: dict = {}
    cyclic_dict["item"] = cyclic_dict
    assert _unwrap_item_wrappers(cyclic_dict) is cyclic_dict

    cyclic_list: list = []
    cyclic_list.append({"item": cyclic_list})
    res_list = _unwrap_item_wrappers(cyclic_list)
    assert isinstance(res_list, list) and len(res_list) == 1


def test_coerce_bool_and_coerce_int():
    assert _coerce_bool(True, "flag") is True
    assert _coerce_bool(False, "flag") is False
    assert _coerce_bool("true", "flag") is True
    assert _coerce_bool("  True ", "flag") is True
    assert _coerce_bool("false", "flag") is False
    assert _coerce_bool(" FALSE\n", "flag") is False

    for bad_bool in (1, 0, None, "", "yes", "no", "1", [], {}):
        with pytest.raises(ValueError, match="flag 必须是布尔值"):
            _coerce_bool(bad_bool, "flag")

    assert _coerce_int(480, "tz") == 480
    assert _coerce_int(0, "tz") == 0
    assert _coerce_int("480", "tz") == 480
    assert _coerce_int(" 0 ", "tz") == 0
    assert _coerce_int("20", "tz") == 20
    assert _coerce_int("-300", "tz") == -300

    for bad_int in (True, False, 1.5, "480.0", "4_80", "", "abc", None):
        with pytest.raises(ValueError, match="tz 必须是整数"):
            _coerce_int(bad_int, "tz")


@pytest.mark.asyncio
async def test_setup_unwraps_item_wrappers_and_coerces_scalars_first_try(tmp_path, monkeypatch):
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    streams = [
        {
            "session_id": group_sid,
            "platform": "qq",
            "is_group_session": True,
            "chat_type": "group",
            "group_id": "100246809",
            "group_name": "测试群",
            "account_id": "bot-1",
            "human_count": 5,
            "last_human_at": 2000.0,
        }
    ]
    plugin = await _boot_plugin(tmp_path, streams=streams)
    try:
        added_feeds = []
        seed_calls = []

        async def fake_add_feed(self, url, *, title="", auto=True, origin="manual"):
            added_feeds.append((url, auto))
            sources = self._load_sources()
            feed = {"id": f"feed_{len(added_feeds)}", "url": url, "title": title or "Feed"}
            sources["feeds"].append(feed)
            self._save_sources(sources)
            self.work.commit()
            return {"ok": True, "feed": feed}

        async def fake_seed(self, topics_or_text="", *, broad=False, max_add=4):
            seed_calls.append((topics_or_text, broad))
            return {"ok": True, "seeded": []}

        monkeypatch.setattr(DigestWorkspace, "add_feed", fake_add_feed)
        monkeypatch.setattr(DigestWorkspace, "seed_starter_feeds", fake_seed, raising=False)

        await plugin.after_receive(message=_private_msg("m-setup-1", "配置测试群"))

        # First call: every array/scalar wrapped in {"item": ...} and stringified bools/ints, run_now="false"
        res = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="100246809",
            source_msg_id="m-setup-1",
            patch={
                "enable_chat": {"item": "true"},
                "daily_proactive_limit": {"item": "5"},
                "sleep_window": {"item": "1:00-8:00"},
                "profiles": {
                    "digest": {
                        "enabled": {"item": "true"},
                        "model_list": {"item": ["gpt-6-luna", "backup-m"]},
                        "max_steps": {"item": "25"},
                    }
                },
                "rss_urls": {
                    "item": [{"url": "https://feed.example/rss.xml", "auto": {"item": "false"}}]
                },
                "topics": {"item": ["开源AI"]},
                "auto_seed_sources": {"item": "true"},
                "broad": {"item": "false"},
                "daily_at": {"item": ["09:00", "20:00"]},
                "tz_offset_minutes": {"item": "480"},
                "replace_schedules": {"item": "true"},
                "run_now": {"item": "false"},
            },
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res["success"] is True, res
        assert "qq:group:100246809" in plugin.settings.enabled_chats
        assert plugin.settings.daily_proactive_limit == 5
        assert plugin.settings.sleep_window == "01-08"
        digest_prof = plugin.profiles.get("digest")
        assert digest_prof.enabled is True
        assert digest_prof.model_list == ("gpt-6-luna", "backup-m")
        assert digest_prof.max_steps == 25
        assert added_feeds == [("https://feed.example/rss.xml", False)]
        assert seed_calls == [("开源AI", False)]
        assert {s["daily_at"] for s in res["schedules"]} == {"09:00", "20:00"}
        assert res["trial_thread_id"] == ""

        # Second call via JSON content string with run_now="true" & wait_seconds="0" & schedules tz_offset_minutes
        res2 = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="100246809",
            source_msg_id="m-setup-1",
            content=json.dumps(
                {
                    "schedules": {
                        "item": [
                            {
                                "daily_at": {"item": ["07:30"]},
                                "tz_offset_minutes": {"item": "-300"},
                                "profile": "digest",
                            }
                        ]
                    },
                    "replace_schedules": {"item": "true"},
                    "run_now": {"item": "true"},
                    "wait_seconds": {"item": "0"},
                    "deliver_to": "target",
                },
                ensure_ascii=False,
            ),
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res2["success"] is True, res2
        assert len(res2["schedules"]) == 1
        assert res2["schedules"][0]["tz_offset_minutes"] == -300
        assert res2["trial_thread_id"].startswith("th_")
        await plugin.engine.drain()

        # Lockout guard catches stringified/wrapped plugin.enabled="false"
        lockout = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="100246809",
            source_msg_id="m-setup-1",
            patch={"plugin": {"enabled": {"item": "false"}}},
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert lockout["success"] is False

        # Disabling digest via stringified/wrapped profiles[pid].enabled does not re-seed digest
        seed_count_before = len(seed_calls)
        res_disable = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="100246809",
            source_msg_id="m-setup-1",
            patch={"profiles": {"digest": {"enabled": {"item": "false"}}}},
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res_disable["success"] is True, res_disable
        assert plugin.profiles.get("digest").enabled is False
        assert len(seed_calls) == seed_count_before

        # Invalid scalar types fail clearly
        for bad_patch in (
            {"enable_chat": "yes"},
            {"daily_proactive_limit": True},
            {"auto_seed_sources": "1"},
            {"daily_at": ["09:00"], "tz_offset_minutes": True},
            {"daily_at": ["09:00"], "replace_schedules": "no"},
            {"run_now": "yes"},
            {"run_now": True, "wait_seconds": True},
            {"topics": ["AI"], "broad": "maybe"},
        ):
            bad_res = await plugin.shape_tool(
                action="setup",
                kind="setup",
                target_chat="100246809",
                source_msg_id="m-setup-1",
                patch=bad_patch,
                chat_id="priv_admin_md5",
                platform="qq",
            )
            assert bad_res["success"] is False, bad_patch
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_setup_replace_memory_boolean_strings_and_text_preservation(tmp_path):
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    plugin = await _boot_plugin(tmp_path, enabled_chats=[f"qq:group:100246809"])
    try:
        plugin.memory.write("memory", "已有记忆：关注本地模型", chat_id="qq:group:100246809")
        await plugin.after_receive(message=_private_msg("m-mem-1", "调整记忆"))

        # 1. replace_memory="false" appends without deleting existing memory or writing "false"
        res_append = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="qq:group:100246809",
            source_msg_id="m-mem-1",
            patch={
                "replace_memory": {"item": "false"},
                "preferences": {"item": ["新增偏好：Rust 工具链"]},
                "auto_seed_sources": False,
            },
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res_append["success"] is True, res_append
        assert res_append["applied"]["memory"]["mode"] == "append"
        mem1 = plugin.memory.read("memory", chat_id="qq:group:100246809")["content"]
        assert "已有记忆：关注本地模型" in mem1
        assert "新增偏好：Rust 工具链" in mem1
        assert "false" not in mem1.lower()

        # 2. replace_memory="true" replaces without writing literal "true"
        res_rep_true = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="qq:group:100246809",
            source_msg_id="m-mem-1",
            patch={
                "replace_memory": " true ",
                "preferences": ["纯新偏好：Agent 评测"],
                "auto_seed_sources": False,
            },
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res_rep_true["success"] is True, res_rep_true
        assert res_rep_true["applied"]["memory"]["mode"] == "replace"
        mem2 = plugin.memory.read("memory", chat_id="qq:group:100246809")["content"]
        assert "已有记忆" not in mem2
        assert "纯新偏好：Agent 评测" in mem2
        assert "true" not in mem2.lower()

        # 3. Non-boolean text in replace_memory preserves existing replace-with-text behavior
        res_rep_text = await plugin.shape_tool(
            action="setup",
            kind="setup",
            target_chat="qq:group:100246809",
            source_msg_id="m-mem-1",
            patch={
                "replace_memory": "只保留这一条：专注开源基础设施",
                "auto_seed_sources": False,
            },
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert res_rep_text["success"] is True, res_rep_text
        assert res_rep_text["applied"]["memory"]["mode"] == "replace"
        mem3 = plugin.memory.read("memory", chat_id="qq:group:100246809")["content"]
        assert mem3.strip() == "只保留这一条：专注开源基础设施"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_patch_settings_profile_and_digest_sources_unwrap_wrappers(tmp_path, monkeypatch):
    plugin = await _boot_plugin(tmp_path)
    try:
        # patch_settings with {"item": ...} wrappers & string int
        s_res = await patch_settings(
            plugin,
            {
                "add_chats": {"item": ["qq:group:100246809"]},
                "daily_proactive_limit": {"item": "4"},
                "sleep_window": {"item": "01-08"},
            },
        )
        assert "qq:group:100246809" in s_res["settings"]["enabled_chats"]
        assert s_res["settings"]["daily_proactive_limit"] == 4
        assert s_res["settings"]["sleep_window"] == "01-08"

        # Top-level {"item": {...}} wrapper on patch_settings
        s_res_top = await patch_settings(plugin, {"item": {"sleep_window": "02-06"}})
        assert s_res_top["settings"]["sleep_window"] == "02-06"

        # patch_settings rejects bool for int field & string lockout
        with pytest.raises(ValueError, match="daily_proactive_limit 必须是整数"):
            await patch_settings(plugin, {"daily_proactive_limit": True})
        with pytest.raises(ValueError, match="锁死"):
            await patch_settings(plugin, {"plugin": {"enabled": {"item": "false"}}})

        # patch_profile with {"item": ...} wrappers & string bool/int
        p_res = await patch_profile(
            plugin,
            "digest",
            {
                "enabled": {"item": "true"},
                "model_list": {"item": ["gpt-6-luna", "step-5-preview"]},
                "max_steps": {"item": "22"},
            },
        )
        assert p_res["profile"]["enabled"] is True
        assert p_res["profile"]["model_list"] == ["gpt-6-luna", "step-5-preview"]
        assert p_res["profile"]["max_steps"] == 22

        # handle_digest_sources with {"item": ...} wrappers & string auto="false"
        seen = []

        async def fake_add_feed(self, url, *, title="", auto=True, origin="manual"):
            seen.append((url, auto))
            return {"ok": True, "feed": {"id": "f1", "url": url, "title": title}}

        monkeypatch.setattr(DigestWorkspace, "add_feed", fake_add_feed)
        d_res = await handle_digest_sources(
            plugin,
            action="patch",
            target="digest",
            target_chat="qq:group:100246809",
            patch={
                "add_urls": {"item": [{"url": "https://news.example/rss", "auto": {"item": "false"}}]},
                "auto": {"item": "false"},
            },
        )
        assert d_res["kind"] == "digest_sources"
        assert seen == [("https://news.example/rss", False)]
    finally:
        await plugin.on_unload()
