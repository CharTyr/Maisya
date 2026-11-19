"""针对线上 P1 降本提效与 46.8% 工具失败率治理的 TDD 验收测试。"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maisya.abilities import Profile, Profiles
from maisya.maker import AgentRuntime
from maisya.sandbox import SandboxSettings
from maisya.scout import Scout
from maisya.shaping import execute_shape, resolve_chat_target
from maisya.threads import ThreadEngine, ThreadStore, _safe_tool_error_message
from maisya.trust import format_stream_spec
from maisya.vault import Vault
from maisya.web import WebRouter
from maisya.workspaces import Workspaces
from plugin import MaisyaPlugin


TEST_GROUP_ID = "100246809"
TEST_GROUP_SID = "grp_p1_test_md5_0000000000000001"
TEST_ADMIN_ID = "10001"
TEST_ADMIN_DM_SID = "dm_p1_admin_md5_0000000000000001"


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


def _grp_event(text: str, *, msg_id: str, user_id: str = "member_1", ts: float = 1000.0):
    return {
        "message_id": msg_id,
        "session_id": TEST_GROUP_SID,
        "platform": "qq",
        "timestamp": ts,
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": user_id, "user_nickname": "Member"},
            "group_info": {"group_id": TEST_GROUP_ID, "group_name": "测试群"},
            "additional_config": {},
        },
    }


async def _boot_plugin(tmp_path: Path, *, clock_box: list[float] | None = None, init_cfg: dict | None = None):
    data_dir = tmp_path / "data" / "plugins" / "chartyr.maisya"
    data_dir.mkdir(parents=True, exist_ok=True)
    base_cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "enabled_chats": [format_stream_spec("qq", "group", TEST_GROUP_ID)],
        "admins": [f"qq:{TEST_ADMIN_ID}"],
        "sandbox": {"enabled": False},
        **(init_cfg or {}),
    }

    async def _cap(name: str, **kwargs):
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
        if name in {"component.list_loaded_plugins", "api.list"}:
            return {"success": True, "plugins": [], "apis": []}
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "任务完成。", "tool_calls": []}
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
        TEST_GROUP_SID, platform="qq", chat_type="group", group_id=TEST_GROUP_ID
    )
    plugin.messages.upsert_route(
        TEST_ADMIN_DM_SID, platform="qq", chat_type="private", user_id=TEST_ADMIN_ID
    )
    return plugin


@pytest.mark.asyncio
async def test_p1_1_prompt_slimming_no_quot_and_preview_isolation(tmp_path: Path):
    clock = [_ts(2026, 11, 19, 10, 0)]
    plugin = await _boot_plugin(tmp_path, clock_box=clock)
    try:
        tools = [
            {"function": {"name": "reply"}},
            {"function": {"name": "maisya_agent"}},
            {"function": {"name": "maisya_recall"}},
            {"function": {"name": "maisya_later"}},
        ]
        # 1. 创建一个 4 小时前的旧 idle 线程（带超长 600 字正文）
        prof = plugin.profiles.get("scout")
        old_row = plugin.threads.create(
            prof, "旧任务标题", chat_id=TEST_GROUP_SID, platform="qq", user_id="u1", owner="chat"
        )
        plugin.threads.finish(
            old_row["thread_id"],
            TEST_GROUP_SID,
            {"text": "OLD_LONG_BLOB_" + ("长文" * 300)},
            [],
            1,
        )
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET updated=? WHERE thread_id=?",
                (clock[0] - 4 * 3600, old_row["thread_id"]),
            )

        # 2. 创建一个管理员私聊跨群预览线程（admin_direct=True, source_chat_id=TEST_ADMIN_DM_SID）
        preview_row = plugin.threads.create(
            prof,
            "私聊预览任务",
            chat_id=TEST_GROUP_SID,
            platform="qq",
            user_id=TEST_ADMIN_ID,
            owner="chat",
            grant={
                "admin_direct": True,
                "source_chat_id": TEST_ADMIN_DM_SID,
                "issuer_admin": f"qq:{TEST_ADMIN_ID}",
            },
        )
        plugin.threads.finish(
            preview_row["thread_id"],
            TEST_GROUP_SID,
            {"text": "SECRET_PREVIEW_ONLY_COMMENTARY 只回传私聊预览不发群"},
            [],
            1,
        )

        # 3. 群里普通成员发消息 -> before_planner 注入
        await plugin.after_receive(message=_grp_event("早上好", msg_id="m-grp-1", ts=clock[0]))
        plan = await plugin.before_planner(
            session_id=TEST_GROUP_SID, items=[], tool_definitions=tools
        )
        injected = plan["modified_kwargs"]["items"][-1]["parts"][0]["text"]

        # 断言 A：XML 标签内 JSON 不再把双引号膨胀为 &quot;
        assert "&quot;" not in injected, "提示词注入不应把 JSON 双引号膨胀为 &quot;"
        # 断言 B：管理员私聊预览线程不得泄漏到目标群普通成员的 <maisya_threads>
        assert "SECRET_PREVIEW_ONLY_COMMENTARY" not in injected
        assert preview_row["thread_id"] not in injected
        # 断言 C：超过 2 小时的旧 idle 线程不得在 <maisya_threads> 中整段携带 600 字长正文
        assert "OLD_LONG_BLOB_" not in injected
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_p1_2_canonical_stream_disambiguation_auth_hints_and_clear_file_errors(tmp_path: Path):
    clock = [_ts(2026, 11, 19, 10, 0)]
    plugin = await _boot_plugin(tmp_path, clock_box=clock)
    try:
        # 1. 构造两条指向同一规范流 qq:group:100246809 的历史/重复路由，加上一个同 ID 私聊路由，规范流仍应精确命中不报多会话歧义
        plugin.messages.upsert_route(
            "alt_dup_sid_00000000000000000002",
            platform="qq",
            chat_type="group",
            group_id=TEST_GROUP_ID,
        )
        plugin.messages.upsert_route(
            "priv_collide_sid_000000000000003",
            platform="qq",
            chat_type="private",
            user_id=TEST_GROUP_ID,
        )
        sid, plat = await resolve_chat_target(
            plugin,
            f"qq:group:{TEST_GROUP_ID}",
            current_chat_id=TEST_ADMIN_DM_SID,
            current_platform="qq",
        )
        assert sid in {TEST_GROUP_SID, "alt_dup_sid_00000000000000000002"}
        assert plat == "qq"

        # 2. 管理员私聊本轮发过 m-adm-real，但模型忘记传 source_msg_id -> 仍拒绝越权，但报错里带出可用的 m-adm-real 提示
        await plugin.after_receive(message=_dm_event("查看群状态", msg_id="m-adm-real", ts=clock[0]))
        await plugin.before_planner(
            session_id=TEST_ADMIN_DM_SID,
            items=[],
            tool_definitions=[{"function": {"name": "maisya_agent"}}],
        )
        res_no_src = await plugin.recall_tool(
            query="简报",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            source_msg_id="",
            chat_id=TEST_ADMIN_DM_SID,
            platform="qq",
        )
        assert res_no_src["success"] is False
        assert "跨会话检索需要真实管理员消息授权" in res_no_src["error"]
        assert "m-adm-real" in res_no_src["error"], res_no_src["error"]

        # 3. maisya_shape(action="file", op="write") 漏传 expected_version 时提示当前版本号与 op='patch'
        res_no_ver = await plugin.shape_tool(
            action="file",
            op="write",
            document="MEMORY.md",
            target_chat=f"qq:group:{TEST_GROUP_ID}",
            content="测试内容",
            source_msg_id="m-adm-real",
            chat_id=TEST_ADMIN_DM_SID,
            platform="qq",
        )
        assert res_no_ver["success"] is False
        assert "expected_version" in res_no_ver["error"]
        assert "version=" in res_no_ver["error"] or "op='patch'" in res_no_ver["error"]

        # 4. FileNotFoundError 不再被笼统掩盖成 "工具文件或系统操作未完成"
        fnf_msg = _safe_tool_error_message(FileNotFoundError(2, "No such file", "/root/secret/project/NOTES.md"))
        assert "不存在" in fnf_msg
        assert "/root/secret" not in fnf_msg
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_p1_3_web_negative_cache_local_time_image_compress_and_profile_timeout(tmp_path: Path):
    # 1. WebRouter 宿主未启用负缓存：连续 3 次 auto 搜索只打 1 次宿主 RPC
    host_calls = []

    async def _host_cap(name: str, **kwargs):
        if name in {"web.search", "web.fetch"}:
            host_calls.append(name)
            return {"success": False, "error": "统一联网功能未启用"}
        if name == "config.get_plugin":
            return {
                "success": True,
                "value": {
                    "plugin": {"enabled": True},
                    "search_backend": {"default_engine": "tavily"},
                    "engines": {
                        "tavily_enabled": True,
                        "tavily_api_keys": ["tvly-test-key"],
                    },
                },
            }
        return {"success": True}

    def _fake_http(method, url, **_kw):
        body = json.dumps(
            {"results": [{"title": "T", "url": "https://example.com/a", "content": "S"}]}
        ).encode()
        return 200, {"content-type": "application/json"}, body, url

    router = WebRouter(
        SimpleNamespace(call_capability=_host_cap),
        provider="auto",
        http_request=_fake_http,
    )
    await router.search("q1")
    await router.search("q2")
    await router.search("q3")
    assert host_calls == ["web.search"], f"应负缓存宿主禁用状态，实际 RPC 次数: {host_calls}"
    router.invalidate_host_cache()
    await router.search("q4")
    assert len(host_calls) == 2

    # 2. Scout._message_text 输出可读本地时间 time_local（UTC+8）
    sample_ts = _ts(2026, 11, 18, 10, 50)
    formatted = Scout._message_text(
        {
            "message_id": "m1",
            "sender_name": "Tester",
            "user_id": "u1",
            "timestamp": sample_ts,
            "processed_plain_text": "hello",
            "raw_message": [],
        },
        tz_offset_minutes=480,
    )
    assert formatted["timestamp"] == sample_ts
    assert formatted["time_local"] == "2026-11-18 10:50:00", formatted

    # 3. html_render 支持输出压缩 JPEG 且 result_attach 同文件哈希去重不双写磁盘
    r_ch = Image.effect_noise((1200, 1800), 60).filter(ImageFilter.GaussianBlur(1))
    g_ch = Image.effect_noise((1200, 1800), 50).filter(ImageFilter.GaussianBlur(1))
    b_ch = Image.effect_noise((1200, 1800), 70).filter(ImageFilter.GaussianBlur(1))
    img = Image.merge("RGB", (r_ch, g_ch, b_ch))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    big_png = buf.getvalue()
    assert len(big_png) > 500_000

    async def _render_cap(name: str, **kwargs):
        if name == "render.html2png":
            return {
                "success": True,
                "result": {
                    "image_base64": base64.b64encode(big_png).decode("ascii"),
                    "mime_type": "image/png",
                    "width": 1200,
                    "height": 1800,
                },
            }
        return {"success": True}

    store = ThreadStore(tmp_path / "m.db")
    vault = Vault(tmp_path / "vault")
    runtime = AgentRuntime(
        SimpleNamespace(call_capability=_render_cap),
        tmp_path,
        store,
        SandboxSettings(enabled=False),
        vault,
        authorized=lambda *a, **k: True,
    )
    prof = Profile(
        id="digest",
        environment="readonly",
        tools=("workspace_file", "html_render", "result_attach"),
        timeout_seconds=300,
    )
    row = store.create(prof, "render test", chat_id="c1", platform="qq", user_id="u1", workspace="digest")
    ctx = {**row, "revision": 1}
    rendered = await runtime.call(
        "html_render",
        {"html": "<div>hi</div>", "output_path": "out/digest-card.jpg"},
        ctx,
    )
    assert rendered["mime_type"] == "image/jpeg"
    assert rendered["bytes"] < len(big_png) // 2, f"JPEG 应明显小于原 PNG ({rendered['bytes']} vs {len(big_png)})"

    att1 = await runtime.call("result_attach", {"paths": ["out/digest-card.jpg"]}, ctx)
    att2 = await runtime.call("result_attach", {"paths": ["out/digest-card.jpg"]}, ctx)
    assert att1["attachments"][0]["handle"] == att2["attachments"][0]["handle"], "同轮相同文件哈希应复用附件句柄"

    # 4. 预设 digest 档案配置了 timeout_seconds=300，且 Profile 解析保留该字段
    preset_profs = Profiles(ROOT / "presets" / "profiles")
    digest_prof = preset_profs.get("digest")
    assert digest_prof.timeout_seconds == 300
