from maisya.trust import (
    Message,
    MessageStore,
    alias,
    allowed,
    is_vault_input,
    visible_tools,
)


def test_message_mapping_and_authorization(tmp_path):
    store = MessageStore(tmp_path / "messages.db")
    msg = Message("long-message-id-123", "chat", "qq", "alice", False)
    store.record(msg)
    assert store.lookup(alias(msg.message_id), "chat", "qq") == msg
    assert allowed(store, alias(msg.message_id), "chat", "qq", {"qq:alice"})
    assert not allowed(store, msg.message_id, "other", "qq", {"qq:alice"})
    store.record(Message("relay", "chat", "qq", "alice", True))
    assert not allowed(store, "relay", "chat", "qq", {"qq:alice"})


def test_visibility_and_vault_input():
    assert visible_tools(["open", "admin"], {"admin"}, False) == ["open"]
    assert is_vault_input("/vault set x y", private=True)
    assert is_vault_input(
        "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----", private=True
    )
    assert not is_vault_input("hello secret", private=True)
    assert not is_vault_input("/vault set x y", private=False)
