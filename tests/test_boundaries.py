import os
import stat
from copy import deepcopy
from types import SimpleNamespace

import pytest
from conftest import safe_symlink
from test_foundation import event, loaded

from maisya.config import initialize_data
from maisya.pathsafe import is_link_like, lstat_regular, path_is_link_like
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
    safe_symlink(root / "maisya", tmp_path / "outside")
    with pytest.raises(RuntimeError):
        initialize_data(root / "plugins/chartyr.maisya")


def test_is_link_like_rejects_windows_reparse_point(tmp_path, monkeypatch):
    assert is_link_like(SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_reparse_tag=0)) is False
    assert is_link_like(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_reparse_tag=0)) is True
    assert is_link_like(SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_reparse_tag=0xA0000003)) is True
    assert is_link_like(SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_reparse_tag=0xA000000C)) is True

    fake_junction = tmp_path / "junction"
    fake_junction.mkdir()
    real_lstat = os.lstat

    def fake_lstat(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if str(path) == str(fake_junction):
            return SimpleNamespace(
                st_mode=st.st_mode,
                st_ino=st.st_ino,
                st_dev=st.st_dev,
                st_nlink=st.st_nlink,
                st_size=st.st_size,
                st_mtime_ns=st.st_mtime_ns,
                st_reparse_tag=0xA0000003,
            )
        return st

    monkeypatch.setattr("maisya.pathsafe.os.lstat", fake_lstat)
    assert path_is_link_like(fake_junction) is True
    with pytest.raises(ValueError, match="符号链接"):
        lstat_regular(fake_junction)


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
