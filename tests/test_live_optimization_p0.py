"""针对线上 P0 事故根因的回归与验收测试（配置三路合并、多步推理保权与报错引导、预算豁免与周期历史归档）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maisya.config import Settings
from maisya.trust import format_stream_spec
from plugin import MaisyaPlugin


TEST_GROUP_ID = "100246809"
TEST_GROUP_SID = "grp_p0_test_md5_0000000000000001"
TEST_ADMIN_ID = "10001"
TEST_ADMIN_DM_SID = "dm_p0_admin_md5_0000000000000001"


def _ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> float:
    return dt.datetime(
        y, m, d, hh, mm, tzinfo=dt.timezone(dt.timedelta(hours=8))
    ).timestamp()


def _dm_event(text: str, *, msg_id: str, user_id: str = TEST_ADMIN_ID, ts: float = 1000.0):
    return {
        "message_id": msg_id,
        "session_id": TEST_ADMIN_DM_SID,
        "platform": "qq",
        "timestamp": ts,
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": user_id, "user_nickname": "Admin"},
            "group_info": None,
            "additional_config": {},
        },
    }


async def _boot_plugin(
    tmp_path: Path,
    *,
    clock_box: list[float] | None = None,
    capability_fn=None,
    init_cfg: dict | None = None,
):
    data_dir = tmp_path / "data" / "plugins" / "chartyr.maisya"
    data_dir.mkdir(parents=True, exist_ok=True)
    base_cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "enabled_chats": [],
        "admins": [f"qq:{TEST_ADMIN_ID}"],
        "default_model": "",
        "agent_max_steps": 16,
        "agent_timeout": 180,
        "web_provider": "auto",
        "ui_style": "millennium",
        "sleep_window": "",
        "sandbox": {"enabled": False},
        **(init_cfg or {}),
    }
    wakes: list[tuple[str, dict]] = []

    async def _cap(name: str, **kwargs):
        if name == "maisaka.proactive.trigger":
            wakes.append((name, dict(kwargs)))
            return {"success": True, "queued": True}
        if name == "chat.get_all_streams":
            return {
                "success": True,
                "streams": [
                    {
                        "session_id": TEST_GROUP_SID,
                        "platform": "qq",
                        "group_id": TEST_GROUP_ID,
                        "group_name": "测试群",
                        "chat_type": "group",
                        "is_group_session": True,
                    },
                    {
                        "session_id": TEST_ADMIN_DM_SID,
                        "platform": "qq",
                        "user_id": TEST_ADMIN_ID,
                        "nickname": "Admin",
                        "chat_type": "private",
                        "is_group_session": False,
                    },
                ],
            }
        if name == "component.list_loaded_plugins":
            return {"success": True, "plugins": []}
        if name == "api.list":
            return {"success": True, "apis": []}
        if capability_fn is not None:
            res = await capability_fn(name, kwargs)
            if res is not None:
                return res
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "ok", "tool_calls": []}
        return {"success": True}

    async def _cfg_get(key: str, default=None):
        if key == "plugin.permission":
            return [f"qq:{TEST_ADMIN_ID}"]
        return default

    plugin = MaisyaPlugin()
    plugin._test_cfg = dict(base_cfg)
    plugin.get_plugin_config_data = lambda: dict(plugin._test_cfg)
    plugin.set_plugin_config = lambda data: plugin._test_cfg.update(dict(data))
    plugin._ctx = SimpleNamespace(
        paths=SimpleNamespace(data_dir=data_dir, runtime_dir=tmp_path / "runtime"),
        config=SimpleNamespace(get=_cfg_get),
        call_capability=_cap,
        logger=SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda *a, **k: None,
        ),
    )
    await plugin.on_load()
    if clock_box is not None:
        plugin.messages.clock = lambda: clock_box[0]
        plugin.intents.clock = lambda: clock_box[0]
        plugin.bridge.clock = lambda: clock_box[0]
    plugin.messages.upsert_route(
        TEST_GROUP_SID,
        platform="qq",
        chat_type="group",
        group_id=TEST_GROUP_ID,
    )
    plugin.messages.upsert_route(
        TEST_ADMIN_DM_SID,
        platform="qq",
        chat_type="private",
        user_id=TEST_ADMIN_ID,
    )
    return plugin, wakes


@pytest.mark.asyncio
async def test_p0_1_on_config_update_three_way_merge_and_chat_disabled_notice(tmp_path: Path):
    clock = [_ts(2026, 11, 19, 8, 29)]
    plugin, wakes = await _boot_plugin(tmp_path, clock_box=clock)
    try:
        # 1. 运行期通过 WebUI / maisya_shape 保存自定义设置到 settings.json
        runtime_custom = {
            **plugin.settings.model_dump(),
            "enabled_chats": [format_stream_spec("qq", "group", TEST_GROUP_ID)],
            "default_model": "step-5-preview",
            "agent_max_steps": 24,
            "agent_timeout": 300,
            "web_provider": "google_search_plugin",
            "ui_style": "auto",
            "sleep_window": "01-08",
        }
        await plugin.persist_settings(runtime_custom)
        assert plugin.settings.enabled_chats == [f"qq:group:{TEST_GROUP_ID}"]

        # 2. 模拟宿主机只改了 config.toml 的 [sandbox] 字段，宿主推送全量 config.toml（其余字段仍为初始默认）
        updated_toml = {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": [],
            "admins": [f"qq:{TEST_ADMIN_ID}"],
            "default_model": "",
            "agent_max_steps": 16,
            "agent_timeout": 180,
            "web_provider": "auto",
            "ui_style": "millennium",
            "sleep_window": "",
            "sandbox": {"enabled": True, "image": "maisya-sandbox:latest", "runtime": ""},
        }
        await plugin.on_config_update("self", updated_toml, 2)

        # sandbox 改动生效，但运行期自定义的 7 项配置必须完整保留！
        assert plugin.settings.sandbox.enabled is True
        assert plugin.settings.sandbox.image == "maisya-sandbox:latest"
        assert plugin.settings.enabled_chats == [f"qq:group:{TEST_GROUP_ID}"]
        assert plugin.settings.default_model == "step-5-preview"
        assert plugin.settings.agent_max_steps == 24
        assert plugin.settings.agent_timeout == 300
        assert plugin.settings.web_provider == "google_search_plugin"
        assert plugin.settings.ui_style == "auto"
        assert plugin.settings.sleep_window == "01-08"

        # 3. 再次在 config.toml 显式改动 agent_timeout=240（相对上次 base=180 有变），应采纳 240 且保留 enabled_chats
        updated_toml_2 = {**updated_toml, "agent_timeout": 240}
        await plugin.on_config_update("self", updated_toml_2, 3)
        assert plugin.settings.agent_timeout == 240
        assert plugin.settings.enabled_chats == [f"qq:group:{TEST_GROUP_ID}"]

        # 4. 当管理员定时工作流的目标群确实被关闭白名单时，Pulse 必须记录 wake_status="chat_disabled" 并去重私聊提醒发起管理员
        await plugin.persist_settings({**plugin.settings.model_dump(), "enabled_chats": []})
        intent = plugin.intents.schedule(
            kind="return",
            source="workflow",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] + 60,
            reason="每日早间简报",
            payload={
                "platform": "qq",
                "every_seconds": 86400,
                "daily_at": "08:30",
                "profile": "digest",
                "workspace": "digest",
                "scheduled_admin": True,
                "issuer_admin": f"qq:{TEST_ADMIN_ID}",
            },
            bypass_limits=True,
        )
        clock[0] += 65
        await plugin.pulse.tick()
        row = plugin.intents.get(intent["intent_id"])
        assert row["status"] == "pending"
        assert row["wake_status"] == "chat_disabled"
        dm_notices = [w for w in wakes if w[1].get("stream_id") == TEST_ADMIN_DM_SID]
        assert len(dm_notices) == 1
        assert "chat_disabled" in dm_notices[0][1].get("intent", "") or "未启用" in dm_notices[0][1].get("intent", "")

        # 短时间内再次 tick 不得重复刷屏通知管理员
        clock[0] += 120
        await plugin.pulse.tick()
        dm_notices_after = [w for w in wakes if w[1].get("stream_id") == TEST_ADMIN_DM_SID]
        assert len(dm_notices_after) == 1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_p0_2_failed_share_keeps_admin_turn_and_actionable_errors(tmp_path: Path):
    clock = [_ts(2026, 11, 19, 9, 3)]
    plugin, _ = await _boot_plugin(tmp_path, clock_box=clock)
    try:
        tools_all = [
            {"function": {"name": "reply"}},
            {"function": {"name": "maisya_agent"}},
            {"function": {"name": "maisya_recall"}},
            {"function": {"name": "maisya_later"}},
            {"function": {"name": "maisya_shape"}},
            {"function": {"name": "tool_search"}},
        ]
        # 1. 管理员在未启用私聊发消息
        await plugin.after_receive(message=_dm_event("为什么早报没发？帮我补发", msg_id="m-adm-1", ts=clock[0]))
        step1 = await plugin.before_planner(
            session_id=TEST_ADMIN_DM_SID,
            items=[],
            tool_definitions=[t for t in tools_all if t["function"]["name"] != "maisya_shape"],
        )
        step1_names = {t["function"]["name"] for t in step1["modified_kwargs"]["tool_definitions"]}
        assert "maisya_agent" in step1_names
        assert plugin._turn_sources.get(TEST_ADMIN_DM_SID) == ["m-adm-1"]

        # 2. Planner 决定调 share（注意宿主 after_response 在工具执行前触发，此时没有 tool_result！）
        await plugin.after_planner(
            session_id=TEST_ADMIN_DM_SID,
            output_items=[
                {
                    "tool_call": {
                        "func_name": "maisya_agent",
                        "args": {
                            "action": "share",
                            "thread_id": "th_0000000000000001",
                            "target_chat": f"qq:group:{TEST_GROUP_ID}",
                            "deliver_to": "target",
                            "source_msg_id": "m-adm-1",
                        },
                    }
                }
            ],
        )
        # share 尚未成功执行（随后会因目标群未启用而失败），本轮管理员权限绝不能被提前清空！
        assert plugin._turn_sources.get(TEST_ADMIN_DM_SID) == ["m-adm-1"]

        # 3. 同一轮下一步（tool_search 展开 maisya_shape 后）再进 before_planner，工具定义与管理员引导必须仍在
        step2 = await plugin.before_planner(
            session_id=TEST_ADMIN_DM_SID,
            items=[
                # 即使历史上下文里有前一步的引导文本或工具返回值提到过 <maisya_returns>，也不能误判成 Pulse 唤醒
                {
                    "item_type": "FunctionCallOutputItem",
                    "parts": [{"type": "text", "text": "错误：目标会话尚未加入 enabled_chats；参考 <maisya_returns> 说明"}],
                }
            ],
            tool_definitions=tools_all,
        )
        step2_names = {t["function"]["name"] for t in step2["modified_kwargs"]["tool_definitions"]}
        assert "maisya_shape" in step2_names
        assert "maisya_agent" in step2_names
        assert plugin._turn_sources.get(TEST_ADMIN_DM_SID) == ["m-adm-1"]

        # 4. 非法 action 报错必须给出完整合法枚举与启用会话示例（阻止模型盲猜 28 次）
        bad_shape = await plugin.shape_tool(
            action="enable",
            source_msg_id="m-adm-1",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            chat_id=TEST_ADMIN_DM_SID,
            platform="qq",
        )
        assert bad_shape["success"] is False
        err = bad_shape["error"]
        for expected_act in ("overview", "setup", "settings", "profile", "file", "planner"):
            assert expected_act in err, err
        assert "enabled_chats" in err, err

        # maisya_agent 非法 action 同样返回合法动作清单
        bad_agent = await plugin.agent_tool(
            action="bogus_action",
            source_msg_id="m-adm-1",
            chat_id=TEST_ADMIN_DM_SID,
            platform="qq",
        )
        assert bad_agent["success"] is False
        for expected_act in ("start", "send", "stop", "share"):
            assert expected_act in bad_agent["error"], bad_agent["error"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_p0_3_return_budget_exempt_and_recurring_history_rollover(tmp_path: Path):
    clock = [_ts(2026, 11, 19, 8, 29)]
    seen_prompts: list[str] = []
    call_idx = [0]

    async def _cap(name: str, kwargs: dict):
        if name != "llm.generate_with_tools":
            return None
        call_idx[0] += 1
        prompt_str = json.dumps(kwargs.get("prompt") or [], ensure_ascii=False)
        seen_prompts.append(prompt_str)
        # 第 1 周期第 1 步先调一次 workspace_file 写大文本制造膨胀历史，第 2 步完成
        if call_idx[0] == 1:
            return {
                "success": True,
                "response": "",
                "tool_calls": [
                    {
                        "id": "call_big_1",
                        "type": "function",
                        "function": {
                            "name": "workspace_file",
                            "arguments": json.dumps(
                                {
                                    "action": "write",
                                    "path": "project/day1.txt",
                                    "content": "DAY1_UNIQUE_BLOB_" + ("X" * 12000),
                                }
                            ),
                        },
                    }
                ],
                "total_tokens": 60000,
            }
        if call_idx[0] == 2:
            return {
                "success": True,
                "response": "第一天简报已完成。",
                "tool_calls": [],
                "total_tokens": 60000,
            }
        # 第 2 周期（次日 08:30）
        return {
            "success": True,
            "response": "第二天简报已完成。",
            "tool_calls": [],
            "total_tokens": 40000,
        }

    plugin, _ = await _boot_plugin(
        tmp_path,
        clock_box=clock,
        capability_fn=_cap,
        init_cfg={
            "enabled_chats": [format_stream_spec("qq", "group", TEST_GROUP_ID)],
            "daily_bg_runs_limit": 5,
            "daily_bg_tokens_limit": 50000,  # 故意低于单次 return 消耗的 120000 Token
        },
    )
    try:
        plugin.profiles.update("digest", enabled=True)
        intent = plugin.intents.schedule(
            kind="return",
            source="workflow",
            target=f"chat:{TEST_GROUP_SID}",
            due_at=clock[0] + 60,
            reason="每日早间简报",
            payload={
                "platform": "qq",
                "every_seconds": 86400,
                "daily_at": "08:30",
                "tz_offset_minutes": 480,
                "profile": "digest",
                "workspace": "digest",
                "prompt": "产出今日简报",
                "scheduled_admin": True,
                "issuer_admin": f"qq:{TEST_ADMIN_ID}",
            },
            bypass_limits=True,
        )
        # 触发第 1 天 08:30 运行（共消耗 120000 Token）
        clock[0] += 65
        await plugin.pulse.tick()
        await asyncio.wait_for(plugin.engine.drain(), timeout=5.0)
        clock[0] += 5
        await plugin.pulse.tick()

        # 1. kind="return" 定时回接任务豁免后台预算：不得写入 bg_budget_usage 把其他后台任务饿死
        budget_day1 = plugin.intents.bg_budget_state(now=clock[0])
        assert budget_day1["runs_used"] == 0, budget_day1
        assert budget_day1["tokens_used"] == 0, budget_day1
        assert budget_day1["exhausted"] is False, budget_day1

        # 2. 次日 08:30 再次触发同一周期工作流：旧周期超大历史应已归档折叠，不把 12KB 原文重放进次日 prompt
        pending = plugin.intents.list(f"chat:{TEST_GROUP_SID}")
        next_it = [it for it in pending if it["status"] == "pending"][0]
        clock[0] = float(next_it["due_at"]) + 5
        await plugin.pulse.tick()
        await asyncio.wait_for(plugin.engine.drain(), timeout=5.0)

        assert len(seen_prompts) >= 3
        day2_prompt = seen_prompts[-1]
        assert "DAY1_UNIQUE_BLOB_" not in day2_prompt, "次日周期运行不应重放前一天超大工具原文"
        # 且工作区文件与上一轮摘要仍然保留
        assert "第一天简报已完成" in day2_prompt or "context_archive" in day2_prompt or "thread_history" in day2_prompt
    finally:
        await plugin.on_unload()
