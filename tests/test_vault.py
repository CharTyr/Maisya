import base64
import json
import os

import pytest
from conftest import safe_symlink

from maisya.vault import Vault, VaultError


def test_encryption_restart_replacement_and_deletion(tmp_path):
    secret = "test-only-" + os.urandom(20).hex()
    vault = Vault(tmp_path / "vault")
    vault.set("image_key", secret)
    assert vault.handles() == ["image_key"]
    assert all(
        secret.encode() not in path.read_bytes() for path in vault.path.iterdir()
    )
    if os.name != "nt":
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in vault.path.iterdir())
        assert vault.path.stat().st_mode & 0o777 == 0o700
    assert not hasattr(vault, "get")
    restarted = Vault(vault.path)
    assert restarted.redact(
        {"text": secret, "nested": [base64.b64encode(secret.encode()).decode()]}
    ) == {"text": "[REDACTED]", "nested": ["[REDACTED]"]}
    restarted.set("image_key", "replacement-test-value")
    assert restarted.redact(secret) == "[REDACTED]"
    assert Vault(vault.path).redact("replacement-test-value") == "[REDACTED]"
    restarted.delete("image_key")
    assert restarted.handles() == []


def test_tampered_ciphertext_and_missing_key_fail_closed(tmp_path):
    vault = Vault(tmp_path / "vault")
    vault.set("key", "test-secret-value")
    encrypted = vault.path / "key.enc"
    original = encrypted.read_bytes()
    encrypted.write_bytes(b"invalid-token")
    with pytest.raises(VaultError, match="保险箱"):
        Vault(vault.path)
    encrypted.write_bytes(original)
    (vault.path / "master.key").unlink()
    with pytest.raises(VaultError, match="保险箱"):
        Vault(vault.path)
    assert not (vault.path / "master.key").exists()


@pytest.mark.parametrize("handle", ["../escape", "/tmp/key", "a/b", "", "master.key"])
def test_invalid_handles_never_write(tmp_path, handle):
    vault = Vault(tmp_path / "vault")
    before = set(vault.path.iterdir())
    with pytest.raises(ValueError):
        vault.set(handle, "test-secret")
    assert set(vault.path.iterdir()) == before


def test_symlink_and_ciphertext_swap_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    safe_symlink(tmp_path / "link", outside)
    with pytest.raises(VaultError):
        Vault(tmp_path / "link")
    vault = Vault(tmp_path / "vault")
    vault.set("first", "first-test-secret")
    vault.set("second", "second-test-secret")
    (vault.path / "second.enc").write_bytes((vault.path / "first.enc").read_bytes())
    with pytest.raises(VaultError):
        Vault(vault.path)


def test_multiline_key_and_json_escaping_redacted(tmp_path):
    secret = "test-line-one\ntest-line-two"
    vault = Vault(tmp_path / "vault")
    vault.set("ssh", secret)
    assert secret not in vault.redact(secret)
    assert "test-line-one" not in vault.redact(secret.splitlines()[0])
    assert (
        secret not in json.loads(vault.redact(json.dumps({"value": secret})))["value"]
    )
