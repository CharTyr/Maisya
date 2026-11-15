import asyncio
import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths
from pydantic import ValidationError
from test_admin_one_shot import _boot_plugin, _group_msg, _private_msg, _streams_rpc

from maisya.config import Settings
from maisya.intents import next_daily_due
from maisya.trust import Message
from plugin import create_plugin


def test_b3_item1_config_sleep_window_minutes_and_all_day():
    for valid in (
        "",
        "01-08",
        "1-8",
        "00-24",
        "0-24",
        "23:30-07:00",
        "1:00-8:00",
        "00:00-24:00",
        "00:00-07:30",
    ):
        cfg = Settings(sleep_window=valid)
        assert cfg.sleep_window == valid

    for invalid in (
        "25-07",
        "25:00-07:00",
        "24:01-08:00",
        "00-24:30",
        "00:00-24:01",
        "12:60-13:00",
        "23:30:00-07:00",
        "01-08-09",
        "abc-def",
        " 01-08",
    ):
        with pytest.raises((ValidationError, ValueError)):
            Settings(sleep_window=invalid)


@pytest.mark.asyncio
async def test_b3_item1_plugin_tz_offset_wiring_reload_and_later_tool_default(tmp_path):
    async def get(key, default=None):
        return ["qq:Alice"] if key == "plugin.permission" else default

    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya.b3"),
    )
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    initial_cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "enabled_chats": [group_sid],
        "tz_offset_minutes": -300,
        "sleep_window": "23:30-07:00",
    }
    plugin.set_plugin_config(initial_cfg)
    await asyncio.wait_for(plugin.on_load(), timeout=3.0)
    plugin.ctx.call_capability = _streams_rpc()
    try:
        assert plugin.settings.tz_offset_minutes == -300
        assert plugin.settings.sleep_window == "23:30-07:00"
        assert plugin.memory.tz_offset_minutes == -300
        assert getattr(plugin.intents, "tz_offset_minutes", None) == -300
        assert getattr(plugin.runtime, "tz_offset_minutes", None) == -300
        assert getattr(plugin.pulse, "tz_offset_minutes", None) == -300
        assert plugin.pulse.sleep_window == "23:30-07:00"

        # Verify MemoryLayers honors the injected -300 offset for journal date headers
        fixed_now = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc).timestamp()
        plugin.memory.clock = lambda: fixed_now
        plugin.memory.write("journal", "UTC+8 已是15日，但 UTC-5 还是14日")
        journal_doc = plugin.memory.read("journal")
        assert "## 2026-11-14" in journal_doc["content"]
        assert "## 2026-11-15" not in journal_doc["content"]

        # Verify later_tool defaults tz_offset_minutes from plugin.settings (-300), not hardcoded 480
        plugin.intents.clock = lambda: fixed_now
        bob_msg = _group_msg(
            "m-bob-tz1",
            "每日播报",
            session_id=group_sid,
            group_id="100246809",
            user_id="Bob",
        )
        await plugin.after_receive(message=bob_msg)
        sched_default = await plugin.later_tool(
            action="schedule",
            chat_id=group_sid,
            platform="qq",
            source_msg_id="m-bob-tz1",
            kind="reminder",
            daily_at=["08:30"],
            reason="按全局时区定时",
        )
        assert sched_default["success"] is True, sched_default
        row_default = plugin.intents.get(sched_default["intent_id"])
        expected_due_neg300, _ = next_daily_due(fixed_now, "08:30", -300)
        assert row_default["payload"]["tz_offset_minutes"] == -300
        assert row_default["due_at"] == pytest.approx(expected_due_neg300)

        # Explicit tz_offset_minutes override still wins
        sched_explicit = await plugin.later_tool(
            action="schedule",
            chat_id=group_sid,
            platform="qq",
            source_msg_id="m-bob-tz1",
            kind="reminder",
            daily_at=["08:30"],
            tz_offset_minutes=60,
            reason="显式时区覆盖",
        )
        assert sched_explicit["success"] is True, sched_explicit
        row_explicit = plugin.intents.get(sched_explicit["intent_id"])
        expected_due_60, _ = next_daily_due(fixed_now, "08:30", 60)
        assert row_explicit["payload"]["tz_offset_minutes"] == 60
        assert row_explicit["due_at"] == pytest.approx(expected_due_60)

        # Hot reload via on_config_update propagates new tz_offset_minutes and sleep_window
        updated_cfg = {
            **initial_cfg,
            "tz_offset_minutes": 120,
            "sleep_window": "00:00-24:00",
        }
        await asyncio.wait_for(plugin.on_config_update("self", updated_cfg, 2), timeout=3.0)
        assert plugin.settings.tz_offset_minutes == 120
        assert plugin.settings.sleep_window == "00:00-24:00"
        assert plugin.memory.tz_offset_minutes == 120
        assert getattr(plugin.intents, "tz_offset_minutes", None) == 120
        assert getattr(plugin.runtime, "tz_offset_minutes", None) == 120
        assert getattr(plugin.pulse, "tz_offset_minutes", None) == 120
        assert plugin.pulse.sleep_window == "00:00-24:00"

        sched_reloaded = await plugin.later_tool(
            action="schedule",
            chat_id=group_sid,
            platform="qq",
            source_msg_id="m-bob-tz1",
            kind="reminder",
            daily_at=["09:00"],
            reason="热更新后默认时区",
        )
        assert sched_reloaded["success"] is True, sched_reloaded
        row_reloaded = plugin.intents.get(sched_reloaded["intent_id"])
        expected_due_120, _ = next_daily_due(fixed_now, "09:00", 120)
        assert row_reloaded["payload"]["tz_offset_minutes"] == 120
        assert row_reloaded["due_at"] == pytest.approx(expected_due_120)
    finally:
        await plugin.on_unload()


async def _stage_admin_direct_preview_attachment(
    plugin,
    *,
    dest_chat_id: str,
    dest_platform: str = "",
    grant_source_platform: str | None = None,
    group_chat_id: str = "100246809",
    group_platform: str = "qq",
    register_route: bool = False,
):
    profile = plugin.profiles.get("digest")
    grant = {
        "scheduled_admin": True,
        "admin_direct": True,
        "source_chat_id": dest_chat_id,
        "source_msg_id": f"src-{dest_chat_id}",
    }
    if grant_source_platform is not None:
        grant["source_platform"] = grant_source_platform
    if register_route and dest_platform:
        plugin.messages.upsert_route(
            dest_chat_id,
            platform=dest_platform,
            chat_type="private",
            user_id="Alice",
        )
    row = plugin.threads.create(
        profile,
        "跨平台试跑",
        chat_id=group_chat_id,
        platform=group_platform,
        user_id="",
        owner="chat",
        workspace="",
        grant=grant,
    )
    tid = row["thread_id"]
    route = await plugin.bridge.route(dest_chat_id, f"src-{dest_chat_id}", 0)
    rev = plugin.threads.enqueue(
        tid,
        "跨平台试跑",
        group_chat_id,
        return_route=route,
        input_context={"grant": grant},
    )
    work = await plugin.runtime._work(plugin.threads.get(tid, group_chat_id))
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )
    work.write_bytes("out/preview.png", png_bytes)
    work.commit()
    atts = plugin.runtime.files.attach(
        plugin.threads.get(tid, group_chat_id), work, ["out/preview.png"]
    )
    plugin.threads.finish(
        tid,
        group_chat_id,
        {"text": "生成了图卡", "attachments": atts},
        [],
        rev,
    )
    await plugin.bridge.submitted(route)
    await plugin.bridge.offer(dest_chat_id)
    return tid


@pytest.mark.asyncio
async def test_b3_item2_after_planner_auto_share_resolves_real_platform(tmp_path):
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    plugin = await _boot_plugin(
        tmp_path,
        permissions=["qq:Alice", "telegram:Alice", "discord:Alice", "feishu:Alice"],
        enabled_chats=[group_sid],
    )
    sent_calls = []

    async def capability(name, **kwargs):
        if name == "maisaka.reply.generate":
            return {"success": True, "text": "预览图发你。"}
        if name == "send.hybrid":
            sent_calls.append(kwargs)
            return {"success": True, "sent": True, "message_id": f"msg-{len(sent_calls)}"}
        return await _streams_rpc()(name, **kwargs)

    plugin.ctx.call_capability = capability
    recorded_targets = []
    orig_share_send = plugin.sharing.send

    async def spy_share_send(row, **kwargs):
        recorded_targets.append((kwargs.get("target_chat_id"), kwargs.get("target_platform")))
        return await orig_share_send(row, **kwargs)

    plugin.sharing.send = spy_share_send
    wait_output = [{"tool_call": {"func_name": "wait", "args": {}}}]
    try:
        # Case 1: session_routes resolves "telegram" even when thread group platform is "qq"
        await _stage_admin_direct_preview_attachment(
            plugin,
            dest_chat_id="opaque-tg-priv-md5",
            dest_platform="telegram",
            grant_source_platform=None,
            group_chat_id=group_sid,
            group_platform="qq",
            register_route=True,
        )
        await asyncio.wait_for(
            plugin.after_planner(session_id="opaque-tg-priv-md5", output_items=wait_output),
            timeout=3.0,
        )
        assert recorded_targets[-1] == ("opaque-tg-priv-md5", "telegram")

        # Case 2: parse_stream_spec(chat_id) resolves "discord" with NO session_routes or grant.source_platform
        await _stage_admin_direct_preview_attachment(
            plugin,
            dest_chat_id="discord:private:777888",
            dest_platform="",
            grant_source_platform=None,
            group_chat_id=group_sid,
            group_platform="qq",
            register_route=False,
        )
        await asyncio.wait_for(
            plugin.after_planner(session_id="discord:private:777888", output_items=wait_output),
            timeout=3.0,
        )
        assert recorded_targets[-1] == ("discord:private:777888", "discord")

        # Case 3: source context (grant.source_platform) resolves "feishu" when session_routes absent
        await _stage_admin_direct_preview_attachment(
            plugin,
            dest_chat_id="opaque-feishu-priv-md5",
            dest_platform="",
            grant_source_platform="feishu",
            group_chat_id=group_sid,
            group_platform="qq",
            register_route=False,
        )
        await asyncio.wait_for(
            plugin.after_planner(session_id="opaque-feishu-priv-md5", output_items=wait_output),
            timeout=3.0,
        )
        assert recorded_targets[-1] == ("opaque-feishu-priv-md5", "feishu")

        # Case 4: completely unrecorded platform does NOT fabricate hardcoded "qq"
        await _stage_admin_direct_preview_attachment(
            plugin,
            dest_chat_id="opaque-unknown-priv",
            dest_platform="",
            grant_source_platform=None,
            group_chat_id=group_sid,
            group_platform="",
            register_route=False,
        )
        await asyncio.wait_for(
            plugin.after_planner(session_id="opaque-unknown-priv", output_items=wait_output),
            timeout=3.0,
        )
        assert recorded_targets[-1][0] == "opaque-unknown-priv"
        assert recorded_targets[-1][1] != "qq"
        assert len(sent_calls) == 4
    finally:
        await plugin.on_unload()
