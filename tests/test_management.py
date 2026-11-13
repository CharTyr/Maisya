import asyncio
import json

import pytest
from test_foundation import loaded


@pytest.mark.asyncio
async def test_management_assets_versions_and_credentials_are_private(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        catalog = await plugin.manage_read("abilities")
        assert catalog["success"] and {p["id"] for p in catalog["profiles"]} == {
            "scout",
            "maker",
            "digest",
        }
        current = await plugin.manage_read(
            "asset", json.dumps({"kind": "notes", "target": "scout"})
        )
        saved = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "notes",
                    "target": "scout",
                    "content": "先核对来源。",
                    "expected_version": current["version"],
                }
            ),
        )
        assert saved["success"], saved
        stale = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "notes",
                    "target": "scout",
                    "content": "stale",
                    "expected_version": current["version"],
                }
            ),
        )
        assert not stale["success"]
        secret = "management-test-private-value"
        stored = await plugin.manage_write(
            "vault", json.dumps({"handle": "drawing", "value": secret})
        )
        assert stored["success"] and secret not in str(stored)
        settings = await plugin.manage_read("settings")
        assert "drawing" in settings["handles"] and secret not in str(settings)
        components = {c["name"]: c for c in plugin.get_components()}
        assert not components["maisya_manage_read"]["metadata"]["public"]
        assert not components["maisya_manage_write"]["metadata"]["public"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_unload_waits_for_management_requests_to_cancel(tmp_path):
    plugin = await loaded(tmp_path)
    entered = asyncio.Event()

    async def blocked(*arguments):
        entered.set()
        await asyncio.Event().wait()

    plugin.management._write = blocked
    request = asyncio.create_task(plugin.manage_write("settings", "{}"))
    try:
        await entered.wait()
        await plugin.on_unload()
        assert request.done()
        assert request.cancelled()
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_management_settings_persist_with_version_conflict_and_intent_cancel(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    try:
        original = await plugin.manage_read("settings")
        values = original["settings"]
        values["proactive_level"] = "quiet"
        saved = await plugin.manage_write(
            "settings",
            json.dumps({"settings": values, "expected_version": original["version"]}),
        )
        assert saved["success"], saved
        assert plugin.pulse.proactive_level == "quiet"
        assert (plugin.data_root / "settings.json").exists()
        stale = await plugin.manage_write(
            "settings",
            json.dumps({"settings": values, "expected_version": original["version"]}),
        )
        assert not stale["success"]
        intent = plugin.intents.schedule(
            kind="proactive",
            source="later",
            target="chat:chat",
            due_at=plugin.intents.clock() + 60,
            reason="remember",
        )
        cancelled = await plugin.manage_write(
            "cancel_intent", json.dumps({"intent_id": intent["intent_id"]})
        )
        assert (
            cancelled["success"]
            and plugin.intents.get(intent["intent_id"])["status"] == "cancelled"
        )
    finally:
        await plugin.on_unload()
    reloaded = await loaded(tmp_path)
    try:
        assert reloaded.settings.proactive_level == "quiet"
    finally:
        await reloaded.on_unload()


@pytest.mark.asyncio
async def test_chunked_package_roundtrip_and_service_registration_do_not_expose_keys(
    tmp_path,
):
    import base64

    plugin = await loaded(tmp_path)
    try:
        package = await plugin.manage_read("package", json.dumps({"target": "scout"}))
        assert package["success"], package
        imported = await plugin.manage_write(
            "import",
            json.dumps(
                {"target": "copied", "offset": 0, "data": package["data"], "done": True}
            ),
        )
        assert imported["success"] and plugin.profiles.get("copied").id == "copied"
        oversized = await plugin.manage_write(
            "import",
            json.dumps(
                {
                    "target": "bad",
                    "data": base64.b64encode(b"x" * 24577).decode(),
                    "done": True,
                }
            ),
        )
        assert not oversized["success"]
        current = await plugin.manage_read("services")
        config = {
            "servers": {
                "draw": {
                    "command": ["/usr/bin/python3", "/opt/trusted/server.py"],
                    "credentials": {"DRAW_KEY": "drawing"},
                    "tools": ["draw"],
                    "readonly": False,
                }
            },
            "templates": {},
        }
        saved = await plugin.manage_write(
            "services",
            json.dumps(
                {"configuration": config, "expected_version": current["version"]}
            ),
        )
        assert saved["success"], saved
        config["servers"]["draw"]["api_key"] = "raw-key"
        rejected = await plugin.manage_write(
            "services",
            json.dumps({"configuration": config, "expected_version": saved["version"]}),
        )
        assert not rejected["success"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_scope_configuration_can_be_edited_without_exposing_group_data_as_methods(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    try:
        current = await plugin.manage_read(
            "asset",
            json.dumps(
                {"kind": "profile", "target": "scout", "document": "profile.toml"}
            ),
        )
        content = (
            current["content"].replace('access = "everyone"', 'access = "listed"')
            + '\nallow_users=["qq:123456789"]\nallow_chats=["chat"]\n'
        )
        saved = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "profile",
                    "target": "scout",
                    "document": "profile.toml",
                    "content": content,
                    "chat_id": "chat",
                    "expected_version": current["version"],
                }
            ),
        )
        assert saved["success"], saved
        assert plugin.profiles.get("scout").allow_users == ("qq:123456789",)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_skill_catalog_lifecycle_preset_reset_and_role_binding(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        catalog = await plugin.manage_read("abilities", "{}")
        assert catalog["success"], catalog
        assert set(catalog["skills"]) >= {
            "source-checking",
            "ongoing-workspace",
            "news-digest",
        }
        items = {item["id"]: item for item in catalog["skill_items"]}
        assert set(items) >= {"source-checking", "ongoing-workspace", "news-digest"}
        for sid in ("source-checking", "ongoing-workspace", "news-digest"):
            assert items[sid]["is_preset"] is True
            assert items[sid]["is_modified"] is False
            assert items[sid]["description"]
            assert items[sid]["title"]
            assert items[sid]["version"] >= 0
        assert items["source-checking"]["used_by"] == []
        assert items["ongoing-workspace"]["used_by"] == []
        assert "digest" in items["news-digest"]["used_by"]

        # Create a new starter skill
        created = await plugin.manage_write(
            "create_skill",
            json.dumps(
                {
                    "target": "rss-audit",
                    "title": "信源巡检",
                    "description": "巡检独立作者博客与更新频率",
                }
            ),
        )
        assert created["success"], created
        assert "rss-audit" in plugin.memory.skills()
        after_create = {
            item["id"]: item
            for item in (await plugin.manage_read("abilities", "{}"))["skill_items"]
        }
        assert after_create["rss-audit"]["is_preset"] is False
        assert "巡检独立作者博客与更新频率" in after_create["rss-audit"]["description"]

        dup = await plugin.manage_write(
            "create_skill", json.dumps({"target": "rss-audit"})
        )
        assert dup["success"] is False and "已存在" in dup["error"]

        # Clone an existing skill
        cloned = await plugin.manage_write(
            "create_skill",
            json.dumps({"target": "rss-audit-v2", "base": "rss-audit"}),
        )
        assert cloned["success"], cloned
        cloned_doc = plugin.memory.read("skill", target="rss-audit-v2")["content"]
        assert "name: rss-audit-v2" in cloned_doc

        # One-click role binding and unbinding
        bound = await plugin.manage_write(
            "bind_skill",
            json.dumps({"skill": "rss-audit", "profile": "scout", "bound": True}),
        )
        assert bound["success"], bound
        assert "rss-audit" in plugin.profiles.get("scout").skills
        after_bind = {
            item["id"]: item
            for item in (await plugin.manage_read("abilities", "{}"))["skill_items"]
        }
        assert "scout" in after_bind["rss-audit"]["used_by"]

        unbound = await plugin.manage_write(
            "bind_skill",
            json.dumps({"skill": "rss-audit", "profile": "scout", "bound": False}),
        )
        assert unbound["success"], unbound
        assert "rss-audit" not in plugin.profiles.get("scout").skills

        # Deleting a bound custom skill auto-unbinds roles and clears stale versions
        await plugin.manage_write(
            "bind_skill",
            json.dumps({"skill": "rss-audit-v2", "profile": "scout", "bound": True}),
        )
        assert "rss-audit-v2" in plugin.profiles.get("scout").skills
        deleted = await plugin.manage_write(
            "delete_skill", json.dumps({"target": "rss-audit-v2"})
        )
        assert deleted["success"], deleted
        assert "rss-audit-v2" not in plugin.profiles.get("scout").skills
        assert "rss-audit-v2" not in plugin.memory.skills()
        assert isinstance(plugin.memory.methods(plugin.profiles.get("scout")), str)
        recreated = await plugin.manage_write(
            "create_skill", json.dumps({"target": "rss-audit-v2"})
        )
        assert recreated["success"], recreated

        # Preset skill protection & reset (with rollback history preserved)
        del_preset = await plugin.manage_write(
            "delete_skill", json.dumps({"target": "news-digest"})
        )
        assert del_preset["success"] is False and "预设技能不可删除" in del_preset["error"]

        cur_digest = plugin.memory.read("skill", target="news-digest")
        edited = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "skill",
                    "target": "news-digest",
                    "content": "---\nname: news-digest\ndescription: 临时改动\n---\n# 临时改动\n",
                    "expected_version": cur_digest["version"],
                }
            ),
        )
        assert edited["success"], edited
        mod_items = {
            item["id"]: item
            for item in (await plugin.manage_read("abilities", "{}"))["skill_items"]
        }
        assert mod_items["news-digest"]["is_modified"] is True

        reset = await plugin.manage_write(
            "reset_skill", json.dumps({"target": "news-digest"})
        )
        assert reset["success"], reset
        restored_items = {
            item["id"]: item
            for item in (await plugin.manage_read("abilities", "{}"))["skill_items"]
        }
        assert restored_items["news-digest"]["is_modified"] is False
        assert "临时改动" not in plugin.memory.read("skill", target="news-digest")["content"]
        hist = plugin.memory.history("skill", target="news-digest")
        assert len(hist) >= 3
        prev_v = plugin.memory.read("skill", target="news-digest", version=edited["version"])
        assert "临时改动" in prev_v["content"]
    finally:
        await plugin.on_unload()
