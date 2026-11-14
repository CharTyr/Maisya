import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from plugin import create_plugin


@pytest.mark.asyncio
async def test_lifecycle_and_hook(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=async_get),
        paths=PluginPaths(
            data_dir=tmp_path / "data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": ["chat"],
        }
    )
    await plugin.on_load()
    assert (tmp_path / "data/maisya/maisya.db").exists()
    event = {
        "message_id": "message-123456789",
        "session_id": "chat",
        "platform": "qq",
        "message_info": {"user_info": {"user_id": "alice"}, "additional_config": {}},
    }
    assert await plugin.after_receive(message=event) == {"action": "continue"}
    assert plugin.messages.lookup(event["message_id"], "chat", "qq").user_id == "alice"
    await plugin.on_unload()


async def async_get(key, default=None):
    return {}


def test_manifest_and_webui():
    manifest = json.loads(Path("_manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "chartyr.maisya"
    assert "config.get" in manifest["capabilities"]
    assert json.loads(Path("webui.json").read_text(encoding="utf-8"))["schema_version"] == 2
