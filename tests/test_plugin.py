import ast
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from maisya.trust import Message, alias as trust_alias
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


def test_package_import_branch_parity_and_bootstrap_alias_lookup(tmp_path):
    source = Path("plugin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    branch = next(
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "__package__"
    )

    def _imported_names(stmts):
        names = set()
        for stmt in stmts:
            if isinstance(stmt, ast.ImportFrom):
                for item in stmt.names:
                    names.add(item.asname or item.name)
        return names

    pkg_names = _imported_names(branch.body)
    else_names = _imported_names(branch.orelse)
    assert pkg_names == else_names

    pkg_name = "_maibot_pkg_test_maisya"
    pkg_mod = types.ModuleType(pkg_name)
    pkg_mod.__path__ = [str(Path(".").resolve())]
    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.plugin",
        str(Path("plugin.py").resolve()),
    )
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    saved = {k: sys.modules.get(k) for k in (pkg_name, f"{pkg_name}.plugin") if k in sys.modules}
    try:
        sys.modules[pkg_name] = pkg_mod
        sys.modules[f"{pkg_name}.plugin"] = loaded
        spec.loader.exec_module(loaded)
        plugin = loaded.create_plugin()
        plugin.messages = None
        plugin._bootstrap_messages[("unenabled-admin-chat", "qq", "long-message-id-abc12345")] = (
            plugin._message_now(),
            Message(
                message_id="long-message-id-abc12345",
                session_id="unenabled-admin-chat",
                platform="qq",
                user_id="admin-user",
                is_plugin_relayed=False,
            ),
        )
        short = trust_alias("long-message-id-abc12345")
        found = plugin._lookup_message(short, "unenabled-admin-chat", "qq")
        assert found is not None
        assert found.message_id == "long-message-id-abc12345"
    finally:
        for key in list(sys.modules):
            if key == pkg_name or key.startswith(pkg_name + "."):
                sys.modules.pop(key, None)
        sys.modules.update(saved)
