import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

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


@pytest.mark.asyncio
async def test_shape_settings_rejects_lockout_and_invalid_scalars(tmp_path):
    plugin = await _boot_plugin(tmp_path, enabled_chats=["qq:group:100246809"])
    try:
        await plugin.after_receive(message=_private_msg("m-unwrap-1", "调整设置"))

        lockout_res = await plugin.shape_tool(
            action="settings",
            op="patch",
            source_msg_id="m-unwrap-1",
            patch={"plugin": {"enabled": "false"}},
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert lockout_res["success"] is False

        bad_sleep = await plugin.shape_tool(
            action="settings",
            op="patch",
            source_msg_id="m-unwrap-1",
            patch={"sleep_window": "invalid-window"},
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert bad_sleep["success"] is False

        bad_limit = await plugin.shape_tool(
            action="settings",
            op="patch",
            source_msg_id="m-unwrap-1",
            patch={"daily_proactive_limit": "abc"},
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert bad_limit["success"] is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_shape_settings_write_via_json_content_and_rollback(tmp_path):
    plugin = await _boot_plugin(tmp_path, enabled_chats=["qq:group:100246809"])
    try:
        await plugin.after_receive(message=_private_msg("m-unwrap-2", "整库写入并回滚"))
        state = await plugin.shape_tool(
            action="settings",
            op="read",
            source_msg_id="m-unwrap-2",
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert state["success"] is True, state
        base_ver = state["version"]
        candidate = dict(state["settings"])
        candidate["sleep_window"] = "01-08"
        candidate["daily_proactive_limit"] = 5

        written = await plugin.shape_tool(
            action="settings",
            op="write",
            source_msg_id="m-unwrap-2",
            content=json.dumps(candidate, ensure_ascii=False),
            expected_version=base_ver,
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert written["success"] is True, written
        assert plugin.settings.sleep_window == "01-08"
        assert plugin.settings.daily_proactive_limit == 5

        rolled = await plugin.shape_tool(
            action="settings",
            op="rollback",
            source_msg_id="m-unwrap-2",
            version=base_ver,
            expected_version=written["version"],
            chat_id="priv_admin_md5",
            platform="qq",
        )
        assert rolled["success"] is True, rolled
        assert plugin.settings.sleep_window == ""
        assert plugin.settings.daily_proactive_limit == 3
    finally:
        await plugin.on_unload()
