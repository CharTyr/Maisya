import logging
from copy import deepcopy
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from maisya.trust import Message, MessageStore, allowed, filter_tools, normalize_admins
from plugin import create_plugin


def event(text="hello", *, user="Alice", platform="qq", group=False, relay=""):
    return {
        "message_id": "host-message-123456789",
        "session_id": "chat",
        "platform": platform,
        "timestamp": "1794459000.0",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user},
            "group_info": {"group_id": "group"} if group else None,
            "additional_config": {"relayed_by_plugin": relay},
        },
    }


async def loaded(tmp_path, permissions=None):
    async def get(key, default=None):
        return permissions if key == "plugin.permission" else default

    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya"),
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": ["chat"],
        }
    )
    await plugin.on_load()
    return plugin


def test_real_host_payload_and_operator_normalization(tmp_path):
    store = MessageStore(tmp_path / "messages.db")
    admins = normalize_admins([" QQ:Alice ", "qq:caseSensitive", None, "invalid"])
    assert admins == {"qq:Alice", "qq:caseSensitive"}
    msg = Message.from_event(event())
    store.record(msg)
    assert allowed(store, msg.message_id, "chat", "qq", admins)
    assert not allowed(store, msg.message_id, "other", "qq", admins)
    assert not allowed(store, msg.message_id, "chat", "qq", {"qq:alice"})
    relayed = event(relay="other.plugin")
    relayed["message_id"] = "relay"
    store.record(Message.from_event(relayed))
    assert not allowed(store, "relay", "chat", "qq", admins)


def test_local_operator_requires_trusted_platform(tmp_path):
    store = MessageStore(tmp_path / "messages.db")
    for platform in ("qq", "bot_console"):
        payload = event(platform=platform)
        payload["message_info"]["additional_config"]["is_local_operator"] = True
        store.record(Message.from_event(payload))
        assert allowed(store, payload["message_id"], "chat", platform, set()) == (
            platform == "bot_console"
        )


def test_conflicting_identity_and_retention_bound(tmp_path):
    store = MessageStore(tmp_path / "messages.db", max_messages=2)
    original = Message.from_event(event())
    store.record(original)
    changed = event(user="Mallory")
    store.record(Message.from_event(changed))
    assert not allowed(
        store, original.message_id, "chat", "qq", {"qq:Alice", "qq:Mallory"}
    )
    for index in range(3):
        payload = event()
        payload["message_id"] = str(index)
        store.record(Message.from_event(payload))
    assert store.lookup("0", "chat", "qq") is None
    assert store.lookup("2", "chat", "qq") is not None


def test_filter_preserves_host_and_third_party_tools():
    tools = [
        {"name": "reply"},
        {"type": "function", "function": {"name": "maisya_shape"}},
        {"name": "other_plugin_tool"},
        {"name": "subagent"},
    ]
    original = deepcopy(tools)
    assert filter_tools(tools, is_admin=False) == [tools[0], tools[2], tools[3]]
    assert filter_tools(tools, is_admin=True) == tools
    assert tools == original


@pytest.mark.asyncio
async def test_vault_command_sanitized_before_history(tmp_path, caplog):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    secret = "private-test-secret-55555"
    payload = event("/vault set image_key " + secret)
    result = await plugin.after_receive(message=payload)
    assert result["action"] == "continue"
    sanitized = result["modified_kwargs"]["message"]
    assert secret not in str(sanitized)
    assert sanitized["processed_plain_text"] == "/vault"
    assert plugin.vault.handles() == ["image_key"]
    assert secret not in caplog.text
    response = await plugin.vault_command(message=sanitized)
    assert response[2] == 2
    assert secret not in str(response)
    await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides", [{"user": "Mallory"}, {"group": True}, {"relay": "other.plugin"}]
)
async def test_unauthorized_secret_never_saved_or_forwarded(tmp_path, overrides):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    secret = "blocked-test-secret-123"
    payload = event("/vault set blocked " + secret, **overrides)
    payload["message_info"]["additional_config"]["adapter_note"] = secret
    result = await plugin.after_receive(message=payload)
    assert secret not in str(result)
    assert plugin.vault.handles() == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_normal_chat_unchanged_and_bare_key_redacted(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    payload = event("hello secret key travel")
    original = deepcopy(payload)
    assert await plugin.after_receive(message=payload) == {"action": "continue"}
    assert payload == original
    key = (
        "-----BEGIN "
        + "PRIVATE KEY-----\nTEST-KEY-BODY\n-----END "
        + "PRIVATE KEY-----"
    )
    result = await plugin.after_receive(message=event("before " + key + " after"))
    assert "TEST-KEY-BODY" not in str(result)
    assert "before" in result["modified_kwargs"]["message"]["processed_plain_text"]
    assert plugin.vault.handles() == []
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_reload_revokes_permissions_and_keeps_external_assets(tmp_path):
    permissions = ["qq:Alice"]
    plugin = await loaded(tmp_path, permissions)
    await plugin.after_receive(message=event("/vault set key test-value-123"))
    root = tmp_path / "host/data/maisya"
    assert plugin.data_root == root
    permissions.clear()
    await plugin.on_config_update("bot", {}, 2)
    assert not plugin.authorize("host-message-123456789", "chat", "qq")
    await plugin.on_unload()
    again = await loaded(tmp_path, [])
    assert again.vault.handles() == ["key"]
    await again.on_unload()
