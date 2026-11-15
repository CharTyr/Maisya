import base64
import io
import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from maisya.shaping import normalize_sleep_window, patch_settings
from maisya.vault import Vault, VaultError, redact_values, secret_variants
from plugin import create_plugin


async def _boot_b3_plugin(tmp_path, *, permissions=("qq:Alice",), enabled_chats=()):
    async def get(key, default=None):
        return list(permissions) if key == "plugin.permission" else default

    async def call_capability(name, **arguments):
        if name == "chat.get_all_streams":
            return {"success": True, "streams": []}
        return {"success": True}

    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya.audit_b3_shape"),
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


@pytest.mark.asyncio
async def test_b3_normalize_sleep_window_preserves_minutes_and_full_day(tmp_path):
    assert normalize_sleep_window("1:00-8:00") == "01-08"
    assert normalize_sleep_window("01:00 - 08:00") == "01-08"
    assert normalize_sleep_window("23:30-7:00") == "23:30-07:00"
    assert normalize_sleep_window("23:30-07:15") == "23:30-07:15"
    assert normalize_sleep_window("00:00-07:30") == "00:00-07:30"
    assert normalize_sleep_window("00-24") == "00-24"
    assert normalize_sleep_window("0-24") == "00-24"
    assert normalize_sleep_window("00:00-24:00") == "00-24"
    assert normalize_sleep_window("08:00-24:00") == "08-24"
    assert normalize_sleep_window("23:30-24:00") == "23:30-24:00"
    assert normalize_sleep_window("off") == ""
    assert normalize_sleep_window("") == ""

    for bad in ("23:00-24:30", "24:30-07:00", "00-00", "00:00-00:00", "08:00-08:00", "12-65"):
        with pytest.raises(ValueError):
            normalize_sleep_window(bad)

    plugin = await _boot_b3_plugin(tmp_path, enabled_chats=["qq:group:100246809"])
    try:
        res_full = await patch_settings(plugin, {"sleep_window": "00:00-24:00"})
        assert res_full["settings"]["sleep_window"] == "00-24"
        assert plugin.settings.sleep_window == "00-24"

        for bad in ("23:00-24:30", "00-00"):
            with pytest.raises(ValueError):
                await patch_settings(plugin, {"sleep_window": bad})
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b3_preset_skill_protection_and_symlink_rejection(tmp_path):
    plugin = await _boot_b3_plugin(tmp_path, enabled_chats=["tg:group:998877"])
    try:
        # 1. Built-in preset skills cannot be deleted directly
        for preset_skill in ("news-digest", "source-checking"):
            with pytest.raises(ValueError, match="预设技能"):
                plugin.memory.delete_skill(preset_skill)

        # 2. Symlink inside abilities/skills ancestor is rejected by import_skill_archive
        sym_skills = plugin.data_root / "abilities/skills/sym-bad"
        outside = tmp_path / "outside_skill"
        outside.mkdir()
        sym_skills.symlink_to(outside, target_is_directory=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("sym-bad/SKILL.md", "---\nname: sym-bad\n---\n# X\n")
        with pytest.raises(ValueError, match="符号链接|链接|无效"):
            plugin.memory.import_skill_archive(buf.getvalue(), target="sym-bad", filename="sym-bad.zip")
    finally:
        await plugin.on_unload()


def test_b3_vault_empty_key_no_path_leak_bytes_urlsafe_and_entropy(tmp_path):
    vault_dir = tmp_path / "host_secret_root" / "vault"
    v = Vault(vault_dir)
    v.set("github_pat", "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456")
    with pytest.raises(VaultError) as missing_exc:
        v.for_execution("absent_handle")
    assert "absent_handle" in str(missing_exc.value)
    assert str(tmp_path) not in str(missing_exc.value)
    assert ".enc" not in str(missing_exc.value)

    (vault_dir / "github_pat.enc").write_bytes(b"corrupt")
    with pytest.raises(VaultError) as corrupt_exc:
        v.for_execution("github_pat")
    assert "github_pat" in str(corrupt_exc.value)
    assert str(tmp_path) not in str(corrupt_exc.value)
    assert ".enc" not in str(corrupt_exc.value)

    with pytest.raises(VaultError) as empty_with_enc:
        (vault_dir / "master.key").write_bytes(b"")
        Vault(vault_dir)
    assert str(tmp_path) not in str(empty_with_enc.value)

    empty_clean = tmp_path / "empty_clean_vault"
    empty_clean.mkdir()
    (empty_clean / "master.key").write_bytes(b"")
    recovered = Vault(empty_clean)
    assert (empty_clean / "master.key").stat().st_size > 0
    recovered.set("clean_tok", "recovered-secret-value-9911")
    assert Vault(empty_clean).for_execution("clean_tok") == "recovered-secret-value-9911"

    race_dir = tmp_path / "concurrent_b3_vault"
    with ThreadPoolExecutor(max_workers=6) as pool:
        instances = list(pool.map(lambda _: Vault(race_dir), range(6)))
    instances[0].set("k", "concurrent-secret-xyz-123")
    assert all(inst.for_execution("k") == "concurrent-secret-xyz-123" for inst in instances)

    raw_bin = bytes.fromhex("deadbeefcafebabe0123456789abcdef")
    variants = secret_variants(raw_bin)
    assert variants
    b64_url = base64.urlsafe_b64encode(raw_bin).decode("ascii")
    b64_url_unpadded = b64_url.rstrip("=")
    b64_std = base64.b64encode(raw_bin).decode("ascii")
    assert b64_url in variants
    assert b64_url_unpadded in variants
    assert b64_std in variants
    assert raw_bin.hex() in variants

    assert "-" in b64_url or "_" in b64_url
    assert redact_values(f"jwt={b64_url_unpadded} hex={raw_bin.hex()}", variants) == "jwt=[REDACTED] hex=[REDACTED]"
    assert redact_values(b"raw=" + raw_bin + b";hex=" + raw_bin.hex().encode(), variants) == b"raw=[REDACTED];hex=[REDACTED]"

    utf8_bytes = b"Bearer_SuperSecret_Token_Value_42"
    utf8_vars = secret_variants(utf8_bytes)
    assert base64.urlsafe_b64encode(utf8_bytes).decode("ascii").rstrip("=") in utf8_vars
    assert redact_values({"raw": b"hdr " + utf8_bytes, "txt": ("ok", b"end " + utf8_bytes)}, utf8_vars) == {
        "raw": b"hdr [REDACTED]",
        "txt": ("ok", b"end [REDACTED]"),
    }

    v_bin = Vault(tmp_path / "bin_vault")
    v_bin.protect(raw_bin)
    assert v_bin.contains_secret(b"chunk:" + raw_bin)
    assert v_bin.redact(b"chunk:" + raw_bin) == b"chunk:[REDACTED]"
    assert v_bin.redact(bytearray(b"hex=" + raw_bin.hex().encode())) == bytearray(b"hex=[REDACTED]")

    for harmless in ("ok", "user", "token", "admin", "secret", b"ok", "aaaaaaaaaaaa", "abababababab"):
        assert secret_variants(harmless) == set()
    v_bin.set("short_pin", "ok")
    assert v_bin.redact("user token is ok and admin status") == "user token is ok and admin status"
