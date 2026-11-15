import pytest

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


def test_dual_source_admins_grants_and_dm_helpers(tmp_path):
    from maisya.trust import (
        PermissionGrantStore,
        admin_dm_allowed,
        merge_admins,
        normalize_admin_id,
        validate_admin_update,
    )

    assert normalize_admin_id("10001") == "qq:10001"
    assert normalize_admin_id("qqbot:MixedCaseID") == "qqbot:MixedCaseID"
    assert normalize_admin_id("qq:group:100246809") is None

    merged = merge_admins({"qqbot": ["AbCd_1"]}, ["10001", "qq:private:20002"])
    assert merged == {"qqbot:AbCd_1", "qq:10001", "qq:20002"}

    with pytest.raises(ValueError):
        validate_admin_update([], actor="qq:10001", host_admins=[])

    store = MessageStore(tmp_path / "maisya.db")
    store.record(Message("dm-1", "s-dm", "qq", "10001", False, group_id=""))
    store.record(Message("grp-1", "s-grp", "qq", "10001", False, group_id="100246809"))
    assert admin_dm_allowed(store, "dm-1", "s-dm", "qq", merged)
    assert not admin_dm_allowed(store, "grp-1", "s-grp", "qq", merged)

    grants = PermissionGrantStore(tmp_path / "maisya.db")
    grants.grant(scope="qq:private:10001", action_key="exec:test", tier="once")
    assert grants.check_or_consume(scope="qq:private:10001", action_key="exec:test")
    assert not grants.check_or_consume(scope="qq:private:10001", action_key="exec:test")
