"""共享工作区按事独立线程、群聊话题账本、Planner 极薄帧与结案轻沉淀测试（TDD）。"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from maisya.abilities import Profiles
from maisya.threads import ThreadEngine, ThreadStore
from maisya.trust import Message
from maisya.workspaces import Workspaces
from plugin import MaisyaPlugin


TEST_GROUP_ID = "100246809"
TEST_GROUP_SID = f"qq:group:{TEST_GROUP_ID}"
TEST_ADMIN_ID = "99999"
TEST_ADMIN_DM_SID = f"qq:private:{TEST_ADMIN_ID}"
PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _default_cap_fn(tmp_path: Path):
    async def _cap(name: str, **kwargs):
        if name == "chat.get_all_streams":
            return {
                "success": True,
                "streams": [
                    {
                        "session_id": TEST_GROUP_SID,
                        "platform": "qq",
                        "group_id": TEST_GROUP_ID,
                        "group_name": "测试大群",
                        "chat_type": "group",
                        "is_group_session": True,
                    },
                    {
                        "session_id": TEST_ADMIN_DM_SID,
                        "platform": "qq",
                        "user_id": TEST_ADMIN_ID,
                        "nickname": "群主",
                        "chat_type": "private",
                        "is_group_session": False,
                    },
                ],
            }
        if name in {"component.list_loaded_plugins", "api.list"}:
            return {"success": True, "plugins": [], "apis": []}
        if name == "llm.get_available_models":
            return {"success": True, "models": [], "tasks": {}}
        if name == "chat.get_avatar":
            return {"status": "unsupported"}
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "任务完成。", "tool_calls": []}
        return {"success": True}

    return _cap


async def _boot_plugin(
    tmp_path: Path,
    *,
    enabled_chats: list[str] | None = None,
    admins: list[str] | None = None,
    clock_box: list[float] | None = None,
) -> MaisyaPlugin:
    data_dir = tmp_path / "data" / "plugins" / "chartyr.maisya"
    data_dir.mkdir(parents=True, exist_ok=True)
    admin_list = admins if admins is not None else [f"qq:{TEST_ADMIN_ID}"]
    base_cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "enabled_chats": (
            enabled_chats if enabled_chats is not None else [TEST_GROUP_SID]
        ),
        "admins": admin_list,
        "permission_mode": "autonomous",
        "heartbeat_mode": "off",
        "sandbox": {"enabled": False},
    }

    async def _cfg_get(key: str, default=None):
        if key == "plugin.permission":
            return list(admin_list)
        return default

    plugin = MaisyaPlugin()
    plugin._test_cfg = dict(base_cfg)
    plugin.get_plugin_config_data = lambda: dict(plugin._test_cfg)
    plugin.set_plugin_config = lambda data: plugin._test_cfg.update(dict(data))
    plugin._ctx = SimpleNamespace(
        plugin_id="chartyr.maisya",
        paths=SimpleNamespace(data_dir=data_dir, runtime_dir=tmp_path / "runtime"),
        config=SimpleNamespace(get=_cfg_get),
        call_capability=_default_cap_fn(tmp_path),
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
        TEST_GROUP_SID, platform="qq", chat_type="group", group_id=TEST_GROUP_ID
    )
    plugin.messages.upsert_route(
        TEST_ADMIN_DM_SID, platform="qq", chat_type="private", user_id=TEST_ADMIN_ID
    )
    return plugin


def _group_event(
    text: str,
    *,
    msg_id: str,
    user_id: str,
    user_name: str,
    ts: float,
) -> dict:
    return {
        "message_id": msg_id,
        "session_id": TEST_GROUP_SID,
        "platform": "qq",
        "timestamp": str(ts),
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": user_id, "user_cardname": user_name},
            "group_info": {"group_id": TEST_GROUP_ID, "group_name": "测试大群"},
        },
    }


def _extract_tag_json(text: str, tag: str):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.S)
    assert match is not None, f"missing <{tag}> in prompt:\n{text}"
    return json.loads(html.unescape(match.group(1)))


@pytest.mark.asyncio
async def test_workspace_mode_defaults_shared_files_isolated_threads_and_concurrent_queue(
    tmp_path: Path,
):
    plugin = await _boot_plugin(tmp_path)
    try:
        # 1. digest 预设与新建/克隆自定义角色默认均为 workspace 模式
        digest_prof = plugin.profiles.get("digest")
        assert digest_prof.session_mode == "workspace"

        analyst = plugin.profiles.create(
            "analyst",
            name="群数据专岗",
            description="共享工作区数据分析角色",
            toolsets=["context", "workspace"],
            permission_mode="autonomous",
        )
        assert analyst.session_mode == "workspace"

        cloned = plugin.profiles.clone("default", "ops-helper")
        assert cloned.session_mode == "workspace"

        plugin.vault.set("ws_secret", "top-secret-token-xyz-999")

        # 记录小王与老李两条真实群消息
        for mid, uid, uname in (
            ("m-wang-1", "20001", "小王"),
            ("m-li-1", "20002", "老李"),
            ("m-wang-2", "20001", "小王"),
            ("m-c1", "20001", "小王"),
            ("m-c2", "20002", "老李"),
        ):
            plugin.messages.record(
                Message.from_event(
                    _group_event("测试", msg_id=mid, user_id=uid, user_name=uname, ts=time.time())
                )
            )

        captured_prompts: list[list[dict]] = []
        call_idx = 0

        async def capability(name, **arguments):
            nonlocal call_idx
            if name == "llm.generate_with_tools":
                call_idx += 1
                prompt = list(arguments["prompt"])
                captured_prompts.append(prompt)
                user_texts = "\n".join(
                    str(m.get("content") or "")
                    for m in prompt
                    if m.get("role") == "user"
                )
                # 小王第一轮：写 STATE.md 和 report.csv 并挂载附件
                if "拉上周活跃数据" in user_texts and call_idx == 1:
                    return {
                        "response": "",
                        "tool_calls": [
                            {
                                "id": "tc_w1",
                                "function": {
                                    "name": "workspace_file",
                                    "arguments": json.dumps(
                                        {
                                            "action": "write",
                                            "path": "project/STATE.md",
                                            "content": "当前进度：已完成第12周活跃统计；密钥=top-secret-token-xyz-999",
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            },
                            {
                                "id": "tc_w2",
                                "function": {
                                    "name": "workspace_file",
                                    "arguments": json.dumps(
                                        {
                                            "action": "write",
                                            "path": "project/report.csv",
                                            "content": "day,active\nMon,42\n",
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            },
                            {
                                "id": "tc_w3",
                                "function": {
                                    "name": "workspace_file",
                                    "arguments": json.dumps(
                                        {
                                            "action": "write",
                                            "path": "out/summary.txt",
                                            "content": "活跃汇总",
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            },
                        ],
                    }
                if "拉上周活跃数据" in user_texts and call_idx == 2:
                    return {
                        "response": "小王的活跃表已生成（含 top-secret-token-xyz-999），已保存到 project/report.csv。",
                        "tool_calls": [],
                    }
                # 老李的独立任务：检查干净上下文与工作区快照
                if "算季度报销总额" in user_texts:
                    return {"response": "老李的报销总额为 3200 元。", "tool_calls": []}
                # 小王追问原线程
                if "再加一列留存率" in user_texts:
                    return {"response": "已按原表追加留存率列。", "tool_calls": []}
                return {"response": "ok", "tool_calls": []}
            return await _default_cap_fn(tmp_path)(name, **arguments)

        plugin._ctx.call_capability = capability

        # 2. 小王开启第一趟任务
        r_wang = await plugin.agent_tool(
            action="start",
            profile="analyst",
            source_msg_id="m-wang-1",
            message="拉上周活跃数据做个表",
            wait_seconds=10,
            chat_id=TEST_GROUP_SID,
            platform="qq",
        )
        assert r_wang["success"] is True, r_wang
        th_wang = r_wang["thread_id"]
        row_wang = plugin.threads.get(th_wang, TEST_GROUP_SID)
        ws_wang = row_wang["workspace_id"]
        assert ws_wang.startswith("ws_")

        # 3. 老李开启第二趟任务：独立新 thread_id + 共享同一 workspace_id + 干净 history + 自动垫入 <workspace_context>
        r_li = await plugin.agent_tool(
            action="start",
            profile="analyst",
            source_msg_id="m-li-1",
            message="算季度报销总额",
            wait_seconds=10,
            chat_id=TEST_GROUP_SID,
            platform="qq",
        )
        assert r_li["success"] is True, r_li
        th_li = r_li["thread_id"]
        assert th_li != th_wang, "workspace 模式下群友 B 的 start 必须开启独立新 thread_id"

        row_li = plugin.threads.get(th_li, TEST_GROUP_SID)
        assert row_li["workspace_id"] == ws_wang, "workspace 模式下同角色应共享同一个 workspace_id"

        li_prompt_text = "\n".join(str(m.get("content") or "") for m in captured_prompts[-1])
        assert "<workspace_context>" in li_prompt_text
        assert "project/report.csv" in li_prompt_text
        assert "已完成第12周活跃统计" in li_prompt_text
        assert "top-secret-token-xyz-999" not in li_prompt_text, "工作区快照与结案锚点必须脱敏"
        # 不把小王的原始对话消息塞进老李的 history
        assert "拉上周活跃数据做个表" not in [
            str(m.get("content") or "") for m in row_li["history"]
        ]

        # 4. 小王追问 th_wang：小王原线程 history 完好无损，完全不被老李冲掉
        r_wang_follow = await plugin.agent_tool(
            action="send",
            thread_id=th_wang,
            source_msg_id="m-wang-2",
            message="把刚才我的表再加一列留存率",
            wait_seconds=10,
            chat_id=TEST_GROUP_SID,
            platform="qq",
        )
        assert r_wang_follow["success"] is True, r_wang_follow
        wang_follow_prompt = "\n".join(
            str(m.get("content") or "") for m in captured_prompts[-1]
        )
        assert "拉上周活跃数据做个表" in wang_follow_prompt
        assert "算季度报销总额" not in wang_follow_prompt

        # 5. fresh=True 在 workspace 模式下不得清空或归档掉持久工作区目录里的已有文件
        r_fresh = await plugin.agent_tool(
            action="start",
            profile="analyst",
            fresh=True,
            source_msg_id="m-li-1",
            message="查看现有数据",
            wait_seconds=10,
            chat_id=TEST_GROUP_SID,
            platform="qq",
        )
        assert r_fresh["success"] is True
        row_fresh = plugin.threads.get(r_fresh["thread_id"], TEST_GROUP_SID)
        assert row_fresh["workspace_id"] == ws_wang
        snap = plugin.runtime.files.read_workspace_snapshot(row_fresh)
        assert any(f["path"] == "project/report.csv" for f in snap["files"])

        # 6. 并发压测：两位群友同时 start 同一 workspace 角色，底层按 workspace_id 串行排队，不抛锁冲突，后者能看到前者新提交的文件
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        conc_prompts: dict[str, str] = {}

        async def concurrent_cap(name, **arguments):
            if name == "llm.generate_with_tools":
                txt = "\n".join(str(m.get("content") or "") for m in arguments["prompt"])
                if "并发任务一" in txt and "project/concurrent_a.txt" not in txt:
                    if not first_entered.is_set():
                        first_entered.set()
                        await release_first.wait()
                        return {
                            "response": "",
                            "tool_calls": [
                                {
                                    "id": "tc_conc_1",
                                    "function": {
                                        "name": "workspace_file",
                                        "arguments": json.dumps(
                                            {
                                                "action": "write",
                                                "path": "project/concurrent_a.txt",
                                                "content": "from-thread-a",
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    return {"response": "并发一完成", "tool_calls": []}
                if "并发任务二" in txt:
                    conc_prompts["two"] = txt
                    return {"response": "并发二完成", "tool_calls": []}
                return {"response": "并发一完成", "tool_calls": []}
            return await _default_cap_fn(tmp_path)(name, **arguments)

        plugin._ctx.call_capability = concurrent_cap

        t_a = asyncio.create_task(
            plugin.agent_tool(
                action="start",
                profile="analyst",
                source_msg_id="m-c1",
                message="并发任务一",
                wait_seconds=10,
                chat_id=TEST_GROUP_SID,
                platform="qq",
            )
        )
        await asyncio.wait_for(first_entered.wait(), timeout=5.0)
        t_b = asyncio.create_task(
            plugin.agent_tool(
                action="start",
                profile="analyst",
                source_msg_id="m-c2",
                message="并发任务二",
                wait_seconds=10,
                chat_id=TEST_GROUP_SID,
                platform="qq",
            )
        )
        await asyncio.sleep(0.05)
        release_first.set()
        res_a, res_b = await asyncio.gather(t_a, t_b)
        assert res_a["success"] is True and res_a["result"].get("text") == "并发一完成"
        assert res_b["success"] is True and res_b["result"].get("text") == "并发二完成"
        assert res_a["thread_id"] != res_b["thread_id"]
        assert "project/concurrent_a.txt" in conc_prompts.get("two", ""), (
            "排队后的第二个并发线程拿到工作区锁后应看到第一个线程刚提交的文件"
        )
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_workspace_mode_preserves_taint_scope_isolation(tmp_path: Path):
    plugin = await _boot_plugin(tmp_path)
    try:
        plugin.profiles.create(
            "shared-desk",
            name="共享资料桌",
            description="测试跨权限污染隔离",
            toolsets=["context", "workspace"],
            session_mode="workspace",
            permission_mode="autonomous",
        )
        plugin.messages.record(
            Message.from_event(
                _group_event("私密入库", msg_id="m-adm-1", user_id=TEST_ADMIN_ID, user_name="管理员", ts=time.time())
            )
        )
        plugin.messages.record(
            Message.from_event(
                _group_event("群友查资料", msg_id="m-user-1", user_id="20008", user_name="普通群友", ts=time.time())
            )
        )

        async def cap(name, **arguments):
            if name == "llm.generate_with_tools":
                return {"response": "完成", "tool_calls": []}
            return await _default_cap_fn(tmp_path)(name, **arguments)

        plugin._ctx.call_capability = cap

        # 管理员以 admin_only 污染作用域写入该共享工作区
        r_adm = await plugin.engine.start(
            plugin.profiles.get("shared-desk"),
            "管理员写入私密备忘",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id=TEST_ADMIN_ID,
            wait_seconds=5,
            grant={"admin_verified": True, "visibility_scope": "admin_only"},
        )
        ws_adm = plugin.threads.get(r_adm["thread_id"], TEST_GROUP_SID)["workspace_id"]

        # 普通群友再调同一 workspace 角色，必须被污染格隔离到干净工作区，不能读到管理员污染工作区
        r_user = await plugin.agent_tool(
            action="start",
            profile="shared-desk",
            source_msg_id="m-user-1",
            message="普通群友查资料",
            wait_seconds=5,
            chat_id=TEST_GROUP_SID,
            platform="qq",
        )
        assert r_user["success"] is True
        ws_user = plugin.threads.get(r_user["thread_id"], TEST_GROUP_SID)["workspace_id"]
        assert ws_user != ws_adm
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_planner_group_topic_ledger_initiator_age_and_cold_eviction(
    tmp_path: Path,
):
    now = 1_763_520_000.0
    clock = [now]
    plugin = await _boot_plugin(tmp_path, clock_box=clock)
    try:
        tools = [
            {"function": {"name": "reply"}},
            {"function": {"name": "maisya_agent"}},
            {"function": {"name": "maisya_recall"}},
            {"function": {"name": "maisya_later"}},
        ]
        # 登记群友昵称到 trusted_sources
        await plugin.after_receive(
            message=_group_event("嗨", msg_id="m-u1", user_id="20001", user_name="小王", ts=now - 300)
        )
        await plugin.after_receive(
            message=_group_event("在吗", msg_id="m-u2", user_id="20002", user_name="老李", ts=now - 200)
        )

        prof = plugin.profiles.get("scout")
        # 1. 热线程：小王 5 分钟前刚跑完的 idle 线程
        hot_row = plugin.threads.create(
            prof, "查上周活跃数据", chat_id=TEST_GROUP_SID, platform="qq", user_id="20001", owner="chat"
        )
        rev_hot = plugin.threads.enqueue(hot_row["thread_id"], "查上周活跃数据", TEST_GROUP_SID)
        plugin.threads.finish(hot_row["thread_id"], TEST_GROUP_SID, {"text": "活跃统计已完成"}, [], rev_hot)
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET updated=? WHERE thread_id=?",
                (now - 300, hot_row["thread_id"]),
            )

        # 2. 冷线程：老李 4 小时前跑完且已交付的 idle 线程 -> 应自动下桌
        cold_row = plugin.threads.create(
            prof, "四小时前的旧查询", chat_id=TEST_GROUP_SID, platform="qq", user_id="20002", owner="chat"
        )
        rev_cold = plugin.threads.enqueue(cold_row["thread_id"], "四小时前的旧查询", TEST_GROUP_SID)
        plugin.threads.finish(cold_row["thread_id"], TEST_GROUP_SID, {"text": "已结束的旧结论"}, [], rev_cold)
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET updated=? WHERE thread_id=?",
                (now - 4 * 3600, cold_row["thread_id"]),
            )
            db.execute(
                "INSERT OR REPLACE INTO result_deliveries (thread_id, revision, destination, route, status, message_id, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cold_row["thread_id"], rev_cold, TEST_GROUP_SID, "reply", "sent", "m-sent-old", now - 4 * 3600),
            )

        # 3. 活线程：老李 3 小时前发起但仍在 waiting 的线程 -> 即使超 2 小时也必须留在桌面
        wait_row = plugin.threads.create(
            prof, "待确认的高风险操作", chat_id=TEST_GROUP_SID, platform="qq", user_id="20002", owner="chat"
        )
        rev_wait = plugin.threads.enqueue(wait_row["thread_id"], "待确认的高风险操作", TEST_GROUP_SID)
        plugin.threads.finish(
            wait_row["thread_id"],
            TEST_GROUP_SID,
            {"question": "是否继续？"},
            [],
            rev_wait,
            waiting=True,
        )
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET updated=? WHERE thread_id=?",
                (now - 3 * 3600, wait_row["thread_id"]),
            )

        await plugin.after_receive(
            message=_group_event("加一列留存率", msg_id="m-u1-follow", user_id="20001", user_name="小王", ts=now)
        )
        plan = await plugin.before_planner(
            session_id=TEST_GROUP_SID, items=[], tool_definitions=tools
        )
        injected = plan["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        ledger = _extract_tag_json(injected, "maisya_threads")
        by_id = {item["thread_id"]: item for item in ledger}

        assert hot_row["thread_id"] in by_id
        assert "小王" in str(by_id[hot_row["thread_id"]].get("initiator") or "")
        assert str(by_id[hot_row["thread_id"]].get("age") or "").strip() != ""

        assert wait_row["thread_id"] in by_id
        assert "老李" in str(by_id[wait_row["thread_id"]].get("initiator") or "")

        assert cold_row["thread_id"] not in by_id, "超过 2 小时且已交付的旧 idle 线程应自动下桌"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_planner_admin_two_tier_slimming_and_memory_soft_cap(tmp_path: Path):
    now = 1_763_520_000.0
    clock = [now]
    plugin = await _boot_plugin(tmp_path, clock_box=clock)
    try:
        # 写入超长会话 MEMORY.md 与超长 7 天 JOURNAL.md，验证软顶保护
        long_mem = "【核心群规】不要发剧透。\n" + ("中段旧流水记录；" * 450) + "\n【最新口味】大家最近迷上了独立肉鸽游戏。"
        long_journal = "## 2026-11-18\n" + ("前几天的详细手账流水；" * 350) + "\n## 2026-11-19\n今天刚折腾完一个新脚本。"
        plugin.memory.write("memory", long_mem, chat_id=TEST_GROUP_SID)
        plugin.memory.write("journal", long_journal)

        # 管理员在已启用群里日常水群（尚未展开 maisya_shape -> has_shape=False）
        await plugin.after_receive(
            message=_group_event("哈哈哈太好笑了", msg_id="m-adm-chat", user_id=TEST_ADMIN_ID, user_name="群主", ts=now)
        )
        casual_tools = [
            {"function": {"name": "reply"}},
            {"function": {"name": "tool_search"}},
            {"function": {"name": "maisya_agent"}},
            {"function": {"name": "maisya_recall"}},
            {"function": {"name": "maisya_later"}},
        ]
        casual_plan = await plugin.before_planner(
            session_id=TEST_GROUP_SID, items=[], tool_definitions=casual_tools
        )
        casual_text = casual_plan["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert 'tool_search(query="maisya_shape")' in casual_text
        # 未展开 maisya_shape 时不陪跑冗长配置说明书
        assert "chat_proactive_levels" not in casual_text
        assert "heartbeat_interval_minutes" not in casual_text
        assert "digest_sources" not in casual_text

        # 验证 <maisya_memory> 软顶生效且保头保尾
        mem_obj = _extract_tag_json(casual_text, "maisya_memory")
        assert len(mem_obj["memory"]) < len(long_mem)
        assert "不要发剧透" in mem_obj["memory"]
        assert "独立肉鸽游戏" in mem_obj["memory"]
        assert "maisya_recall" in mem_obj["memory"]
        assert len(mem_obj["journal"]) < len(long_journal)
        assert "今天刚折腾完一个新脚本" in mem_obj["journal"]

        # 管理员展开 maisya_shape 后（has_shape=True），完整配置手册与旋钮映射自动就位
        expanded_tools = [
            *casual_tools,
            {"function": {"name": "maisya_shape"}},
        ]
        expanded_plan = await plugin.before_planner(
            session_id=TEST_GROUP_SID, items=[], tool_definitions=expanded_tools
        )
        expanded_text = expanded_plan["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "heartbeat_mode" in expanded_text
        assert "per_chat_daily_proactive_limit" in expanded_text
        assert "chat_proactive_levels" in expanded_text
        assert len(expanded_text) > len(casual_text) + 300
    finally:
        await plugin.on_unload()
