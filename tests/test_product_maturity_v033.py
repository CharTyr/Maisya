"""v0.3.3 产品成熟度全面升级 TDD 测试套件（P0-P4）。"""

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

from maisya.abilities import Profile, Profiles
from maisya.learning import Learning
from maisya.scout import Scout
from maisya.threads import ThreadEngine, ThreadStore
from maisya.trust import Message
from plugin import MaisyaPlugin


TEST_GROUP_ID = "100246809"
TEST_GROUP_SID = f"qq:group:{TEST_GROUP_ID}"
TEST_ADMIN_ID = "99999"
TEST_ADMIN_DM_SID = f"qq:private:{TEST_ADMIN_ID}"
PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _default_cap():
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
                        "nickname": "管理员",
                        "chat_type": "private",
                        "is_group_session": False,
                    },
                ],
            }
        if name == "component.list_loaded_plugins":
            return {"success": True, "plugins": [{"id": "chartyr.weather", "name": "Weather"}]}
        if name == "api.list":
            return {
                "success": True,
                "apis": [
                    {
                        "name": "weather.get_forecast",
                        "plugin_id": "chartyr.weather",
                        "description": "查询指定城市天气预报（只读）",
                        "public": True,
                        "version": "1",
                    },
                    {
                        "name": "admin.delete_all_records",
                        "plugin_id": "other.danger",
                        "description": "删除全部历史记录",
                        "public": True,
                        "version": "1",
                    },
                ],
            }
        if name == "llm.get_available_models":
            return {"success": True, "models": ["flash-a", "pro-b"], "tasks": {"utils": ["flash-a"]}}
        if name == "chat.get_avatar":
            return {"status": "unsupported"}
        if name == "web.search":
            return {"success": True, "results": [{"title": "ok", "url": "https://example.com"}]}
        if name == "render.html2png":
            return {"success": True, "image_base64": PNG_1X1_B64}
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "任务完成。", "tool_calls": []}
        return {"success": True}

    return _cap


async def _boot_plugin(
    tmp_path: Path,
    *,
    enabled_chats: list[str] | None = None,
    admins: list[str] | None = None,
    cap_fn=None,
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
        call_capability=cap_fn or _default_cap(),
        logger=SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            error=lambda *a, **k: None,
        ),
    )
    await plugin.on_load()
    plugin.messages.upsert_route(
        TEST_GROUP_SID, platform="qq", chat_type="group", group_id=TEST_GROUP_ID
    )
    plugin.messages.upsert_route(
        TEST_ADMIN_DM_SID, platform="qq", chat_type="private", user_id=TEST_ADMIN_ID
    )
    return plugin


def _dm_event(text: str, *, msg_id: str, ts: float | None = None) -> dict:
    return {
        "message_id": msg_id,
        "session_id": TEST_ADMIN_DM_SID,
        "platform": "qq",
        "timestamp": str(ts or time.time()),
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": TEST_ADMIN_ID, "user_nickname": "管理员"},
            "group_info": None,
        },
    }


def _grp_event(text: str, *, msg_id: str, user_id: str = "20001", ts: float | None = None) -> dict:
    return {
        "message_id": msg_id,
        "session_id": TEST_GROUP_SID,
        "platform": "qq",
        "timestamp": str(ts or time.time()),
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": user_id, "user_cardname": "群友甲"},
            "group_info": {"group_id": TEST_GROUP_ID, "group_name": "测试大群"},
        },
    }


@pytest.mark.asyncio
async def test_tool_guard_coercion_actionable_hints_and_slim_profiles(tmp_path: Path):
    plugin = await _boot_plugin(tmp_path)
    try:
        await plugin.after_receive(message=_dm_event("开启新群", msg_id="m-adm-1"))

        # 1. maisya_shape 只读同义词 (help / status / chats) 自动透明路由到 overview
        help_res = await plugin.shape_tool(
            action="help",
            source_msg_id="m-adm-1",
            chat_id=TEST_ADMIN_DM_SID,
            platform="qq",
        )
        assert help_res["success"] is True, help_res
        assert help_res.get("kind") == "overview" and "known_chats" in help_res

        # 2. maisya_shape 高频写操作误猜 (enable_chat / whitelist) 返回带真实 target_chat 与 source_msg_id 的可照抄模板
        wrong_res = await plugin.shape_tool(
            action="enable_chat",
            target_chat="qq:group:888999",
            source_msg_id="m-adm-1",
            chat_id=TEST_ADMIN_DM_SID,
            platform="qq",
        )
        assert wrong_res["success"] is False
        assert "action='setup'" in wrong_res["error"] or 'action="setup"' in wrong_res["error"]
        assert "qq:group:888999" in wrong_res["error"]
        assert "m-adm-1" in wrong_res["error"]

        # 3. 弱类型参数保守纠偏：字符串 patch JSON、字符串列表、字符串布尔
        coerced_res = await plugin.shape_tool(
            action="settings",
            op="patch",
            patch='{"heartbeat_mode": "gentle", "daily_proactive_limit": "4"}',
            source_msg_id="m-adm-1",
            chat_id=TEST_ADMIN_DM_SID,
            platform="qq",
        )
        assert coerced_res["success"] is True, coerced_res
        assert plugin.settings.heartbeat_mode == "gentle"
        assert plugin.settings.daily_proactive_limit == 4

        # 4. maisya_agent 对未加白名单目标群报错时给出可直接照抄的修复模板
        unenabled_res = await plugin.agent_tool(
            action="start",
            profile="scout",
            target_chat="qq:group:777666",
            message="查查天气",
            source_msg_id="m-adm-1",
            chat_id=TEST_ADMIN_DM_SID,
            platform="qq",
        )
        assert unenabled_res["success"] is False
        assert "deliver_to='current'" in unenabled_res["error"] or 'deliver_to="current"' in unenabled_res["error"]
        assert "maisya_shape" in unenabled_res["error"]
        assert "qq:group:777666" in unenabled_res["error"]

        # 5. <maisya_profiles> 极薄名册瘦身（3 个预设角色 < 1000 字符，且保留既有关键字段）
        plugin.profiles.update("digest", enabled=True)
        await plugin.after_receive(message=_grp_event("今天吃什么", msg_id="m-g-1"))
        plan = await plugin.before_planner(
            session_id=TEST_GROUP_SID,
            items=[],
            tool_definitions=[
                {"function": {"name": "reply"}},
                {"function": {"name": "maisya_agent"}},
                {"function": {"name": "maisya_recall"}},
                {"function": {"name": "maisya_later"}},
            ],
        )
        injected = plan["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        m = re.search(r"<maisya_profiles>(.*?)</maisya_profiles>", injected, flags=re.S)
        assert m is not None
        assert len(m.group(0)) < 1100, f"<maisya_profiles> still too bloated: {len(m.group(0))} chars"
        roster = {p["id"]: p for p in json.loads(html.unescape(m.group(1)))}
        assert {"default", "scout", "digest"} <= set(roster)
        assert roster["default"]["mode"] and roster["default"]["description"]
        assert "html_render" in roster["default"]["capabilities"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_scout_auto_default_fallback_and_model_health_tracking():
    calls: list[str] = []

    async def cap(name, **arguments):
        assert name == "llm.generate_with_tools"
        m = str(arguments.get("model_name") or "")
        calls.append(m)
        if m == "flaky-flash":
            raise TimeoutError("Request timed out.")
        return {"success": True, "response": "默认槽位救场成功", "tool_calls": []}

    scout = Scout(SimpleNamespace(call_capability=cap))
    # 角色仅配单模型且未填 fallback_models
    prof = Profile(id="scout", model_name="flaky-flash", fallback_models=())
    res = await scout.step(prof, [{"role": "user", "content": "帮我查一下"}])
    assert res["response"] == "默认槽位救场成功"
    assert calls == ["flaky-flash", ""]

    health = scout.model_health_snapshot()
    assert health["flaky-flash"]["timeouts"] >= 1
    assert health["(default_slot)"]["ok"] >= 1


@pytest.mark.asyncio
async def test_thread_engine_transient_checkpoint_auto_retry_and_human_error(tmp_path: Path):
    store = ThreadStore(tmp_path / "maisya.db")
    step_count = 0

    class FlakyOnceRuntime:
        def resolve_profile(self, p):
            return p

        async def step(self, profile, history):
            nonlocal step_count
            step_count += 1
            if step_count == 1:
                return {
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "web_search", "arguments": '{"query":"test"}'},
                        }
                    ],
                }
            if step_count == 2:
                # 模拟已经推进到第 2 步后上游瞬态超时
                raise RuntimeError("当前角色的模型均未返回可用结果：APITimeoutError Request timed out.")
            return {"response": "断点自愈续跑完成", "tool_calls": []}

        async def call(self, name, arguments, context):
            return {"results": [{"title": "T", "url": "https://example.com"}]}

        async def end(self, thread_id):
            pass

    engine = ThreadEngine(store, FlakyOnceRuntime(), timeout=30)
    try:
        prof = Profile(id="scout", tools=("web_search",))
        res = await engine.start(
            prof,
            "查个资料",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="20001",
            wait_seconds=10,
        )
        assert res["state"] == "idle"
        assert res["result"]["text"] == "断点自愈续跑完成"
        assert step_count == 3
    finally:
        await engine.close()

    # 永久超时时应返回清晰的大白话原因（而非泛泛“检查配置”）
    class AlwaysTimeoutRuntime:
        def resolve_profile(self, p):
            return p

        async def step(self, profile, history):
            raise RuntimeError("当前角色的模型均未返回可用结果：APITimeoutError Request timed out.")

        async def end(self, thread_id):
            pass

    engine2 = ThreadEngine(store, AlwaysTimeoutRuntime(), timeout=30)
    try:
        res2 = await engine2.start(
            Profile(id="scout"),
            "查个资料2",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id="20001",
            wait_seconds=10,
        )
        err_txt = str((res2.get("result") or {}).get("error") or "")
        assert "超时" in err_txt
    finally:
        await engine2.close()


@pytest.mark.asyncio
async def test_before_replyer_attachment_guard_and_memory_cap(tmp_path: Path):
    plugin = await _boot_plugin(tmp_path)
    try:
        # 写入超长会话备忘
        long_mem = "【群规】文明交流。\n" + ("流水账；" * 600) + "\n【近期偏好】喜欢独立游戏。"
        plugin.memory.write("memory", long_mem, chat_id=TEST_GROUP_SID)

        # 创建一个带 att_ 图片附件的已完成线程，并 stage/offer 给本轮 Planner
        prof = plugin.profiles.get("default")
        row = plugin.threads.create(
            prof, "做张图卡", chat_id=TEST_GROUP_SID, platform="qq", user_id="20001", owner="chat"
        )
        route = await plugin.bridge.route(TEST_GROUP_SID, "m-g-att-1", 0)
        rev = plugin.threads.enqueue(
            row["thread_id"], "做张图卡", TEST_GROUP_SID, return_route=route
        )
        await plugin.bridge.submitted(route)
        with plugin.runtime.files.activity(row) as work:
            work.write_bytes("out/card.png", base64.b64decode(PNG_1X1_B64))
            work.commit()
            atts = plugin.runtime.files.attach(
                plugin.threads.get(row["thread_id"], TEST_GROUP_SID),
                work,
                ["out/card.png"],
            )
        plugin.threads.finish(
            row["thread_id"],
            TEST_GROUP_SID,
            {"text": "图卡已生成并挂载待发", "attachments": atts},
            [],
            rev,
        )
        # 模拟本轮 Planner 看到了 <maisya_returns> 中的待发附件
        offered = await plugin.bridge.offer(TEST_GROUP_SID)
        assert offered and offered[0]["thread_id"] == row["thread_id"]

        # 第一次 Planner 绕过 share 直接调 reply -> 应被附件防漏发守门员拦截并提示调用 share
        first_try = await plugin.before_replyer(
            session_id=TEST_GROUP_SID,
            request_type="maisaka.replyer",
            attempt=1,
            extra_prompt="",
            reply_tool_args={},
        )
        assert first_try["action"] == "abort"
        reason = str((first_try.get("custom_result") or {}).get("reason") or "")
        assert "share" in reason and row["thread_id"] in reason

        # 第二次如果仍坚持 reply -> 放行避免死循环，且注入的 <maisya_memory> 已软顶去 &quot;
        second_try = await plugin.before_replyer(
            session_id=TEST_GROUP_SID,
            request_type="maisaka.replyer",
            attempt=1,
            extra_prompt="",
            reply_tool_args={},
        )
        assert second_try["action"] == "continue"
        ep = str((second_try.get("modified_kwargs") or {}).get("extra_prompt") or "")
        assert "&quot;" not in ep
        assert "文明交流" in ep and "喜欢独立游戏" in ep and len(ep) < len(long_mem)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_learning_expands_to_multi_step_delivery_and_followup(tmp_path: Path):
    plugin = await _boot_plugin(tmp_path)
    try:
        prof = plugin.profiles.get("default")
        # 构造一个 3 步多工具成功交付（零工具报错，带附件产出）
        row = plugin.threads.create(
            prof, "做份研究图卡", chat_id=TEST_GROUP_SID, platform="qq", user_id="20001", owner="chat"
        )
        rev = plugin.threads.enqueue(row["thread_id"], "做份研究图卡", TEST_GROUP_SID)
        with plugin.threads.connect() as db, db:
            for idx, (tool_name, args_json) in enumerate(
                (
                    ("web_search", '{"query":"a"}'),
                    ("html_render", '{"html_path":"out/a.html"}'),
                    ("result_attach", '{"paths":["out/a.png"]}'),
                ),
                start=1,
            ):
                plugin.threads.event(
                    db,
                    row["thread_id"],
                    "tools",
                    rev,
                    {
                        "calls": [{"id": f"c{idx}", "function": {"name": tool_name, "arguments": args_json}}],
                        "results": [{"call_id": f"c{idx}", "value": {"ok": True}}],
                    },
                )
        plugin.threads.finish(
            row["thread_id"],
            TEST_GROUP_SID,
            {"text": "研究图卡已生成", "attachments": [{"handle": "att_1234", "name": "a.png"}]},
            [],
            rev,
        )
        finished = plugin.threads.get(row["thread_id"], TEST_GROUP_SID)
        learner = plugin.runtime.ensure_learning()
        trace = learner._trace(finished)
        assert trace, "多步多工具成功交付应触发轻量自省 trace"
        assert any(item.get("signal") == "multi_step_delivery" for item in trace)

        # 构造一个 revision=2 的用户追问纠偏结案
        rev2 = plugin.threads.enqueue(row["thread_id"], "把标题改成更短的", TEST_GROUP_SID)
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                row["thread_id"],
                "tools",
                rev2,
                {
                    "calls": [{"id": "c4", "function": {"name": "workspace_file", "arguments": "{}"}}],
                    "results": [{"call_id": "c4", "value": {"ok": True}}],
                },
            )
        plugin.threads.finish(
            row["thread_id"],
            TEST_GROUP_SID,
            {"text": "已按反馈缩短标题", "attachments": []},
            [],
            rev2,
        )
        finished2 = plugin.threads.get(row["thread_id"], TEST_GROUP_SID)
        trace2 = learner._trace(finished2)
        assert trace2, "用户追问纠偏结案应触发 followup_refinement trace"
        assert any(item.get("signal") == "followup_refinement" for item in trace2)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_doctor_scene_templates_and_readonly_api_mount(tmp_path: Path):
    plugin = await _boot_plugin(tmp_path)
    try:
        # 1. 一键体检（doctor）
        doc = await plugin.manage_read("doctor", "{}")
        assert doc["status"] in {"ok", "warn", "error"}
        check_ids = {c["id"] for c in doc["checks"]}
        assert {"config", "models", "web_search", "html_render", "sandbox", "profiles"} <= check_ids
        assert all(c["status"] in {"ok", "warn", "error", "info"} for c in doc["checks"])

        # 2. 场景模板目录与一键套用
        tpls = await plugin.manage_read("templates", "{}")
        tpl_ids = {t["id"] for t in tpls["templates"]}
        assert {"researcher", "group_digest", "trpg_host", "watcher"} <= tpl_ids
        assert all(t.get("try_prompt") for t in tpls["templates"])

        applied = await plugin.manage_write(
            "template_apply",
            json.dumps({"template_id": "trpg_host", "profile_id": "coc_host"}),
        )
        assert applied["success"] is True, applied
        coc = plugin.profiles.get("coc_host")
        assert coc.session_mode == "persistent"
        assert coc.enabled is True

        # 3. 跨插件只读 @API 一键登记并挂载到角色；高危写名必须被拒绝
        mounted = await plugin.manage_write(
            "mount_readonly_api",
            json.dumps(
                {
                    "api_name": "weather.get_forecast",
                    "profile_id": "default",
                    "description": "查询天气预报",
                }
            ),
        )
        assert mounted["success"] is True, mounted
        handle = mounted["handle"]
        assert handle in plugin.profiles.get("default").apis
        svc_api = plugin.runtime.services.load()["apis"][handle]
        assert svc_api["api_name"] == "weather.get_forecast"
        assert svc_api["readonly"] is True

        rejected = await plugin.manage_write(
            "mount_readonly_api",
            json.dumps(
                {
                    "api_name": "admin.delete_all_records",
                    "profile_id": "default",
                }
            ),
        )
        assert rejected["success"] is False

        # 4. discovery 返回的跨插件接口自动标注 safe_readonly
        disc = await plugin.manage_read("discovery", "{}")
        apis_by_name = {a["name"]: a for a in disc["apis"]}
        assert apis_by_name["weather.get_forecast"]["safe_readonly"] is True
        assert apis_by_name["admin.delete_all_records"]["safe_readonly"] is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_v033_learning_cooldown_doctor_redaction_and_webui_wiring(tmp_path: Path):
    secret_val = "sk-test-super-secret-value-987654321"

    async def leaky_cap(name: str, **kwargs):
        if name == "render.html2png":
            return {"success": False, "error": f"render failed with {secret_val}"}
        return await _default_cap()(name, **kwargs)

    plugin = await _boot_plugin(tmp_path, cap_fn=leaky_cap)
    try:
        plugin.vault.set("demo_secret", secret_val)

        # 1. Doctor 输出强制脱敏，绝不泄密
        doc = await plugin.manage_read("doctor", "{}")
        assert secret_val not in json.dumps(doc, ensure_ascii=False)
        render_chk = next(c for c in doc["checks"] if c["id"] == "html_render")
        assert render_chk["status"] == "warn"

        # 2. Learning 扩展软信号带 30 分钟冷却，避免高频刷提炼
        learner = plugin.runtime.ensure_learning()
        prof = plugin.profiles.get("default")
        rows = []
        for idx in range(2):
            row = plugin.threads.create(
                prof, f"多步任务{idx}", chat_id=TEST_GROUP_SID, platform="qq", user_id="20001", owner="chat"
            )
            rev = plugin.threads.enqueue(row["thread_id"], f"多步任务{idx}", TEST_GROUP_SID)
            with plugin.threads.connect() as db, db:
                for s_idx, tname in enumerate(("web_search", "html_render", "result_attach"), 1):
                    plugin.threads.event(
                        db,
                        row["thread_id"],
                        "tools",
                        rev,
                        {
                            "calls": [{"id": f"c{idx}_{s_idx}", "function": {"name": tname, "arguments": "{}"}}],
                            "results": [{"call_id": f"c{idx}_{s_idx}", "value": {"ok": True}}],
                        },
                    )
            plugin.threads.finish(
                row["thread_id"],
                TEST_GROUP_SID,
                {"text": "图卡完成", "attachments": [{"handle": f"att_{idx}", "name": "a.png"}]},
                [],
                rev,
            )
            rows.append(plugin.threads.get(row["thread_id"], TEST_GROUP_SID))

        await learner.schedule(rows[0])
        await learner.schedule(rows[1])
        diag = learner.diagnostics()
        assert diag["scheduled"] == 1
        assert diag["skipped"].get("soft_signal_cooldown", 0) >= 1
        await learner.drain()

        # 3. 编译后的 webui/now.html 完整接线新体检、场景模板与安全只读挂载入口
        now_html = (Path(__file__).resolve().parent.parent / "webui" / "now.html").read_text(
            encoding="utf-8"
        )
        for token in (
            "set-btn-doctor",
            "set-doctor-results",
            "pr-btn-templates",
            "pr-tpl-scrim",
            "template_apply",
            "mount_readonly_api",
            "data-conn-mount-api",
        ):
            assert token in now_html, f"missing {token} in compiled webui/now.html"
    finally:
        await plugin.on_unload()
