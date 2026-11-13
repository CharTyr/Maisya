from copy import deepcopy

import pytest
from test_foundation import event, loaded

from maisya.config import initialize_data
from maisya.trust import Message, MessageStore


def test_alias_collision_and_message_expiry(tmp_path, monkeypatch):
    monkeypatch.setattr("maisya.trust.alias", lambda message_id: "mcommon")
    store = MessageStore(tmp_path / "messages.db", max_age=10)
    for msg_id in ("first-long-message-id", "second-long-message-id"):
        store.record(Message(msg_id, "chat", "qq", "Alice", False))
    assert store.lookup("mcommon", "chat", "qq") is None
    assert store.lookup("first-long-message-id", "chat", "qq") is not None
    monkeypatch.setattr("maisya.trust.time.time", lambda: 10**12)
    assert store.lookup("first-long-message-id", "chat", "qq") is None


def test_sdk_path_independent_of_cwd_and_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        initialize_data(tmp_path / "unknown")
    root = tmp_path / "host/data"
    root.mkdir(parents=True)
    (root / "maisya").symlink_to(tmp_path / "outside")
    with pytest.raises(RuntimeError):
        initialize_data(root / "plugins/chartyr.maisya")


@pytest.mark.asyncio
async def test_send_redaction_preserves_routing_and_plain_chat(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    payload = event("ordinary message")
    assert await plugin.before_send(message=payload) == {"action": "continue"}
    secret = "synthetic-outbound-secret-12345"
    plugin.vault.set("test", secret)
    payload = event(secret)
    payload["message_info"]["additional_config"]["platform_io_account_id"] = "route"
    original = deepcopy(payload)
    result = await plugin.before_send(message=payload)
    sanitized = result["modified_kwargs"]["message"]
    assert secret not in str(sanitized)
    assert sanitized["message_info"] == original["message_info"]
    assert payload == original
    await plugin.on_unload()


@pytest.mark.asyncio
async def test_disabled_scope_and_self_config_update(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    payload = event()
    payload["session_id"] = "not-enabled"
    assert await plugin.after_receive(message=payload) == {"action": "continue"}
    assert plugin.messages.lookup(payload["message_id"], "not-enabled", "qq") is None
    await plugin.on_config_update(
        "self", {"plugin": {"enabled": False, "config_version": "0.1.0"}}, 1
    )
    payload = event("/vault set disabled synthetic-test-secret")
    assert "synthetic-test-secret" not in str(
        await plugin.after_receive(message=payload)
    )
    assert plugin.vault.handles() == []
    await plugin.on_unload()
