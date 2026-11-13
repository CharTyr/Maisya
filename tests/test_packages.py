import pytest

from maisya.abilities import Profiles
from maisya.memory import MemoryLayers
from maisya.threads import ThreadStore
from maisya.vault import Vault


def test_profile_package_includes_bound_method_skills(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    profiles = Profiles(source / "abilities/profiles")
    memory = MemoryLayers(
        source, ThreadStore(source / "state.db"), profiles, Vault(source / "vault")
    )
    memory.write("skill", "先核实原始来源，再总结发现。", target="sources")
    settings = memory.read("profile", target="scout", document="profile.toml")
    memory.write(
        "profile",
        settings["content"] + '\nskills=["sources"]\n',
        target="scout",
        document="profile.toml",
        expected_version=settings["version"],
    )
    package = profiles.export("scout")
    destination = tmp_path / "destination"
    destination.mkdir()
    imported = Profiles(destination / "abilities/profiles")
    profile = imported.import_package(package, "imported")
    other = MemoryLayers(
        destination,
        ThreadStore(destination / "state.db"),
        imported,
        Vault(destination / "vault"),
    )
    assert "原始来源" in other.methods(profile)


def test_export_rejects_over_budget_uncompressed_assets(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    assets = profiles.path / "scout/preset"
    assets.mkdir()
    for index in range(3):
        (assets / f"asset-{index}").write_bytes(b"0" * (6 * 1024**2))
    with pytest.raises(ValueError, match="资产"):
        profiles.export("scout")
