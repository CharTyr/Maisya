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
            "default",
            "digest",
            "scout",
        }
        current = await plugin.manage_read(
            "asset", json.dumps({"kind": "notes", "target": "default"})
        )
        saved = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "notes",
                    "target": "default",
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
                    "target": "default",
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
        package = await plugin.manage_read("package", json.dumps({"target": "default"}))
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
                {"kind": "profile", "target": "default", "document": "profile.toml"}
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
                    "target": "default",
                    "document": "profile.toml",
                    "content": content,
                    "chat_id": "chat",
                    "expected_version": current["version"],
                }
            ),
        )
        assert saved["success"], saved
        assert plugin.profiles.get("default").allow_users == ("qq:123456789",)
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
        assert items["source-checking"]["used_by"] == ["scout"]
        assert items["ongoing-workspace"]["used_by"] == []
        assert items["news-digest"]["used_by"] == ["digest"]

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
            json.dumps({"skill": "rss-audit", "profile": "default", "bound": True}),
        )
        assert bound["success"], bound
        assert "rss-audit" in plugin.profiles.get("default").skills
        after_bind = {
            item["id"]: item
            for item in (await plugin.manage_read("abilities", "{}"))["skill_items"]
        }
        assert "default" in after_bind["rss-audit"]["used_by"]

        unbound = await plugin.manage_write(
            "bind_skill",
            json.dumps({"skill": "rss-audit", "profile": "default", "bound": False}),
        )
        assert unbound["success"], unbound
        assert "rss-audit" not in plugin.profiles.get("default").skills

        # Deleting a bound custom skill auto-unbinds roles and clears stale versions
        await plugin.manage_write(
            "bind_skill",
            json.dumps({"skill": "rss-audit-v2", "profile": "default", "bound": True}),
        )
        assert "rss-audit-v2" in plugin.profiles.get("default").skills
        deleted = await plugin.manage_write(
            "delete_skill", json.dumps({"target": "rss-audit-v2"})
        )
        assert deleted["success"], deleted
        assert "rss-audit-v2" not in plugin.profiles.get("default").skills
        assert "rss-audit-v2" not in plugin.memory.skills()
        assert isinstance(plugin.memory.methods(plugin.profiles.get("default")), str)
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


@pytest.mark.asyncio
async def test_skill_archive_zip_tar_multi_bundle_companions_and_security(tmp_path):
    import base64
    import io
    import tarfile
    import zipfile

    plugin = await loaded(tmp_path)
    try:
        # 1. Single-skill .zip with companions + macOS junk files via chunked import_skill RPC
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("__MACOSX/._SKILL.md", b"junk-appledouble")
            zf.writestr("web-audit-main/.DS_Store", b"junk-ds")
            zf.writestr(
                "web-audit-main/SKILL.md",
                "---\nname: web-audit\ndescription: 来源审查与截图核验\n---\n# 网页核验\n读取 references/rules.md。\n",
            )
            zf.writestr("web-audit-main/references/rules.md", "# Rules\nAlways cite source URLs.\n")
            zf.writestr("web-audit-main/scripts/check.py", "print('ok')\n")
        zip_bytes = zbuf.getvalue()

        res = await plugin.manage_write(
            "import_skill",
            json.dumps(
                {
                    "filename": "web-audit-main.zip",
                    "offset": 0,
                    "data": base64.b64encode(zip_bytes).decode(),
                    "done": True,
                }
            ),
        )
        assert res["success"] and res["id"] == "web-audit" and res["count"] == 1
        assert res["imported"][0]["companions"] == 2

        assets = await plugin.manage_read("abilities", "{}")
        by_id = {item["id"]: item for item in assets["skill_items"]}
        assert by_id["web-audit"]["companion_count"] == 2
        assert by_id["web-audit"]["companion_files"] == ["references/rules.md", "scripts/check.py"]

        comp_read = await plugin.manage_read(
            "skill_file", json.dumps({"target": "web-audit", "path": "references/rules.md"})
        )
        assert comp_read["success"] and "Always cite source URLs." in comp_read["content"]

        # Export skill archive round-trip + clone copies companions
        pkg = await plugin.manage_read("skill_package", json.dumps({"target": "web-audit"}))
        assert pkg["success"] and pkg["total"] > 0
        raw_export = base64.b64decode(pkg["data"])
        with zipfile.ZipFile(io.BytesIO(raw_export)) as zcheck:
            assert set(zcheck.namelist()) >= {
                "web-audit/SKILL.md",
                "web-audit/references/rules.md",
                "web-audit/scripts/check.py",
            }
        cloned = await plugin.manage_write(
            "create_skill", json.dumps({"target": "web-audit-copy", "base": "web-audit"})
        )
        assert cloned["success"]
        assert "references/rules.md" in {
            c["path"] for c in plugin.memory.skill_files("web-audit-copy")
        }

        # Runtime materializes companions read-only under in/skills/<id>/ inside workspace
        await plugin.manage_write(
            "bind_skill",
            json.dumps({"skill": "web-audit", "profile": "default", "bound": True}),
        )
        default_prof = plugin.profiles.get("default")
        assert "in/skills/web-audit/" in plugin.memory.methods(default_prof)
        trow = plugin.threads.create(default_prof, "执行网页核验", chat_id="chat", platform="qq", user_id="qq:u1")
        work = await plugin.runtime._work(trow)
        assert (work.path / "in/skills/web-audit/references/rules.md").is_file()
        assert (work.path / "in/skills/web-audit/scripts/check.py").is_file()

        # 2. .tar.gz with PAX global header + .skill archive
        tbuf = io.BytesIO()
        with tarfile.open(fileobj=tbuf, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
            tf.pax_headers = {"comment": "github-commit-deadbeef"}
            md = "---\nname: tar-skill\ndescription: Tar 技能包\n---\n# Tar Skill\n".encode("utf-8")
            ti = tarfile.TarInfo("tar-skill-master/SKILL.md")
            ti.size = len(md)
            tf.addfile(ti, io.BytesIO(md))
        tar_res = await plugin.manage_write(
            "import_skill",
            json.dumps(
                {
                    "filename": "tar-skill-master.tar.gz",
                    "offset": 0,
                    "data": base64.b64encode(tbuf.getvalue()).decode(),
                    "done": True,
                }
            ),
        )
        assert tar_res["success"] and tar_res["id"] == "tar-skill"

        sbuf = io.BytesIO()
        with zipfile.ZipFile(sbuf, "w") as zf:
            zf.writestr("SKILL.md", "---\nname: dot-skill\ndescription: .skill 扩展名\n---\n# Dot\n")
        dot_res = await plugin.manage_write(
            "import_skill",
            json.dumps(
                {
                    "filename": "dot-skill.skill",
                    "offset": 0,
                    "data": base64.b64encode(sbuf.getvalue()).decode(),
                    "done": True,
                }
            ),
        )
        assert dot_res["success"] and dot_res["id"] == "dot-skill"

        # 3. Multi-skill bundle + nested examples/SKILL.md kept inside parent skill
        mbuf = io.BytesIO()
        with zipfile.ZipFile(mbuf, "w") as zf:
            zf.writestr("bundle/alpha/SKILL.md", "---\nname: alpha\ndescription: Alpha skill\n---\n# Alpha\n")
            zf.writestr("bundle/alpha/examples/demo/SKILL.md", "# Nested example\n")
            zf.writestr("bundle/beta/SKILL.md", "---\nname: beta\ndescription: Beta skill\n---\n# Beta\n")
        multi_res = await plugin.manage_write(
            "import_skill",
            json.dumps(
                {
                    "filename": "skills-bundle.zip",
                    "offset": 0,
                    "data": base64.b64encode(mbuf.getvalue()).decode(),
                    "done": True,
                }
            ),
        )
        assert multi_res["success"] and multi_res["count"] == 2
        assert {x["id"] for x in multi_res["imported"]} == {"alpha", "beta"}
        assert "examples/demo/SKILL.md" in {
            c["path"] for c in plugin.memory.skill_files("alpha")
        }

        # 4. Security guards: traversal, symlink, corrupt zip, secret in companion
        bad_zip = io.BytesIO()
        with zipfile.ZipFile(bad_zip, "w") as zf:
            zf.writestr("../escape/SKILL.md", "# bad")
        with pytest.raises(ValueError, match="越界"):
            plugin.memory.import_skill_archive(bad_zip.getvalue(), filename="bad.zip")

        sym_tar = io.BytesIO()
        with tarfile.open(fileobj=sym_tar, mode="w:gz") as tf:
            md = b"# ok\n"
            t1 = tarfile.TarInfo("sym-skill/SKILL.md")
            t1.size = len(md)
            tf.addfile(t1, io.BytesIO(md))
            t2 = tarfile.TarInfo("sym-skill/leak")
            t2.type = tarfile.SYMTYPE
            t2.linkname = "/etc/passwd"
            tf.addfile(t2)
        with pytest.raises(ValueError, match="符号链接"):
            plugin.memory.import_skill_archive(sym_tar.getvalue(), filename="sym.tar.gz")

        with pytest.raises(ValueError, match="损坏"):
            plugin.memory.import_skill_archive(b"not-a-zip", filename="broken.zip")

        plugin.vault.set("secret_token", "sk-secret-vault-value-1234567890")
        sec_zip = io.BytesIO()
        with zipfile.ZipFile(sec_zip, "w") as zf:
            zf.writestr("sec/SKILL.md", "---\nname: sec\n---\n# Sec\n")
            zf.writestr("sec/scripts/env.txt", "TOKEN=sk-secret-vault-value-1234567890")
        with pytest.raises(ValueError, match="凭据"):
            plugin.memory.import_skill_archive(sec_zip.getvalue(), filename="sec.zip")
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_management_proactive_quota_and_intent_metadata(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        settings_res = await plugin.manage_read("settings")
        memory_res = await plugin.manage_read("memory")
        assert settings_res["proactive_quota"] == {"used": 0, "limit": 1, "remaining": 1}
        assert memory_res["proactive_quota"] == {"used": 0, "limit": 1, "remaining": 1}

        now = plugin.intents.clock()
        active_intent = plugin.intents.schedule(
            kind="proactive",
            source="later",
            target="chat:100246809",
            due_at=now + 60,
            reason="稍后提醒一下",
        )
        cancelled_intent = plugin.intents.schedule(
            kind="proactive",
            source="afterglow",
            target="chat:100246809",
            due_at=now + 120,
            reason="被取消的事后惦记",
            bypass_limits=True,
        )
        plugin.intents.cancel(cancelled_intent["intent_id"], "chat:100246809")

        settings_after = await plugin.manage_read("settings")
        memory_after = await plugin.manage_read("memory")
        assert settings_after["proactive_quota"] == {"used": 1, "limit": 1, "remaining": 0}
        assert memory_after["proactive_quota"] == {"used": 1, "limit": 1, "remaining": 0}

        new_cfg = dict(settings_after["settings"])
        new_cfg["daily_proactive_limit"] = 3
        saved_cfg = await plugin.manage_write(
            "settings",
            json.dumps({"settings": new_cfg, "expected_version": settings_after["version"]}),
        )
        assert saved_cfg["success"]
        assert saved_cfg["proactive_quota"] == {"used": 1, "limit": 3, "remaining": 2}

        zero_cfg = dict(saved_cfg["settings"])
        zero_cfg["daily_proactive_limit"] = 0
        saved_zero = await plugin.manage_write(
            "settings",
            json.dumps({"settings": zero_cfg, "expected_version": saved_cfg["version"]}),
        )
        assert saved_zero["success"]
        assert saved_zero["proactive_quota"] == {"used": 1, "limit": 0, "remaining": 0}

        scheduled = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": "100246809",
                    "platform": "qq",
                    "prompt": "定时整理近况",
                    "in_seconds": 60,
                    "every_seconds": 600,
                }
            ),
        )
        assert scheduled["success"], scheduled
        admin_id = scheduled["intent"]["intent_id"]

        silent_intent = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target="chat:self",
            due_at=now + 180,
            reason="后台探索记录",
            bypass_limits=True,
        )

        intents_res = await plugin.manage_read("intents")
        assert intents_res["success"]
        by_id = {row["intent_id"]: row for row in intents_res["intents"]}

        assert by_id[admin_id]["protected"] is True
        assert by_id[active_intent["intent_id"]]["protected"] is False
        assert by_id[silent_intent["intent_id"]]["protected"] is False

        banned_terms = ("scheduled_admin", "A_Memorix", "只进不出", "default_task_slot")
        for row in intents_res["intents"]:
            label = row.get("kind_label", "")
            assert isinstance(label, str) and label.strip()
            for term in banned_terms:
                assert term not in label

        assert by_id[admin_id]["kind_label"] != by_id[active_intent["intent_id"]]["kind_label"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_management_set_profile_models(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        toml_cur = await plugin.manage_read(
            "asset", json.dumps({"kind": "profile", "target": "default", "document": "profile.toml"})
        )
        res = await plugin.manage_write(
            "set_profile_models",
            json.dumps(
                {
                    "profile_id": "default",
                    "models": ["gpt-5", "claude-4", "gpt-5"],
                    "fallback_slot": "planner",
                    "expected_version": toml_cur["version"],
                }
            ),
        )
        assert res["success"], res
        assert res["profile_id"] == "default"
        assert res["models"] == ["gpt-5", "claude-4"]
        assert res["model_name"] == "gpt-5"
        assert res["fallback_models"] == ["claude-4"]
        assert res["fallback_slot"] == "planner"
        assert res["version"] > toml_cur["version"]

        prof = plugin.profiles.get("default")
        assert prof.model_name == "gpt-5"
        assert prof.fallback_models == ("claude-4",)
        assert prof.model_list == ("gpt-5", "claude-4")
        assert prof.task == "planner"

        stale = await plugin.manage_write(
            "set_profile_models",
            json.dumps(
                {
                    "profile_id": "default",
                    "models": ["stale-model"],
                    "expected_version": toml_cur["version"],
                }
            ),
        )
        assert not stale["success"]

        reordered = await plugin.manage_write(
            "set_profile_models",
            json.dumps({"profile_id": "default", "models": ["claude-4", "gpt-5"]}),
        )
        assert reordered["success"], reordered
        assert reordered["models"] == ["claude-4", "gpt-5"]
        assert plugin.profiles.get("default").model_name == "claude-4"
        assert plugin.profiles.get("default").fallback_models == ("gpt-5",)

        bad_slot = await plugin.manage_write(
            "set_profile_models",
            json.dumps({"profile_id": "default", "models": ["claude-4"], "fallback_slot": "bad slot!"}),
        )
        assert not bad_slot["success"]

        bad_models = await plugin.manage_write(
            "set_profile_models",
            json.dumps({"profile_id": "default", "models": [""]}),
        )
        assert not bad_models["success"]

        cleared = await plugin.manage_write(
            "set_profile_models",
            json.dumps({"profile_id": "default", "models": []}),
        )
        assert cleared["success"], cleared
        assert cleared["models"] == []
        assert plugin.profiles.get("default").model_list == ()
        assert plugin.profiles.get("default").task == "planner"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_management_upsert_and_delete_mcp_service_item(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        stored_secret = await plugin.manage_write(
            "vault", json.dumps({"handle": "svc_key", "value": "secret-service-value-99"})
        )
        assert stored_secret["success"]

        svc0 = await plugin.manage_read("services")
        mcp_add = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "item": {
                        "name": "weather",
                        "command": ["/usr/bin/python3", "/opt/mcp/weather.py"],
                        "tools": ["forecast"],
                        "credentials": {"WEATHER_KEY": "svc_key"},
                        "readonly": True,
                        "access": "everyone",
                    },
                    "expected_version": svc0["version"],
                }
            ),
        )
        assert mcp_add["success"], mcp_add
        assert mcp_add["kind"] == "mcp"
        assert mcp_add["name"] == "weather"
        assert "name" not in mcp_add["configuration"]["servers"]["weather"]

        stale_add = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "item": {
                        "name": "weather2",
                        "command": ["/usr/bin/python3", "/opt/mcp/w2.py"],
                        "tools": ["ping"],
                    },
                    "expected_version": svc0["version"],
                }
            ),
        )
        assert not stale_add["success"]

        rejected_tpl = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "template",
                    "item": {
                        "id": "fetch_doc",
                        "argv": ["/bin/echo", "{doc}"],
                        "targets": ["93.184.216.34:443"],
                    },
                }
            ),
        )
        assert not rejected_tpl["success"]

        stale_del = await plugin.manage_write(
            "delete_service_item",
            json.dumps(
                {"kind": "mcp", "name": "weather", "expected_version": svc0["version"]}
            ),
        )
        assert not stale_del["success"]

        del_mcp = await plugin.manage_write(
            "delete_service_item",
            json.dumps(
                {"kind": "mcp", "name": "weather", "expected_version": mcp_add["version"]}
            ),
        )
        assert del_mcp["success"], del_mcp
        assert "weather" not in plugin.runtime.services.load()["servers"]

        missing_del = await plugin.manage_write(
            "delete_service_item",
            json.dumps({"kind": "mcp", "name": "nonexistent"}),
        )
        assert not missing_del["success"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_management_undo_learning_note(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        v1 = plugin.memory.write("notes", "方法一：先核对来源。", target="default", author="admin")
        v2 = plugin.memory.write(
            "notes",
            "方法一：先核对来源。\n\n遇到限流先等待后重试。",
            target="default",
            expected_version=v1["version"],
            author="thread:t-learn-1",
        )

        stale = await plugin.manage_write(
            "undo_learning_note",
            json.dumps({"profile_id": "default", "expected_version": v1["version"]}),
        )
        assert not stale["success"]

        undone = await plugin.manage_write(
            "undo_learning_note",
            json.dumps({"profile_id": "default", "expected_version": v2["version"]}),
        )
        assert undone["success"], undone
        assert undone["profile_id"] == "default"
        assert undone["content"] == "方法一：先核对来源。"
        assert undone["restored_from"] == v1["version"]
        assert plugin.memory.read("notes", target="default")["content"] == "方法一：先核对来源。"

        v4 = plugin.memory.write(
            "notes",
            "方法一：先核对来源。\n\n二次自动学习补充。",
            target="default",
            expected_version=undone["version"],
            author="thread:t-learn-2",
        )
        undone_by_thread = await plugin.manage_write(
            "undo_learning_note",
            json.dumps({"profile_id": "default", "history_id": "t-learn-2"}),
        )
        assert undone_by_thread["success"], undone_by_thread
        assert undone_by_thread["content"] == "方法一：先核对来源。"

        undone_by_ver = await plugin.manage_write(
            "undo_learning_note",
            json.dumps({"profile_id": "default", "history_id": f"v{v4['version']}"}),
        )
        assert undone_by_ver["success"], undone_by_ver
        assert "二次自动学习补充" in undone_by_ver["content"]

        # Undoing the very first auto-learned note clears back to empty
        plugin.profiles.clone("default", "maker")
        maker_v1 = plugin.memory.write(
            "notes",
            "初次自动记录的经验。",
            target="maker",
            author="thread:t-maker-1",
        )
        maker_undone = await plugin.manage_write(
            "undo_learning_note",
            json.dumps({"profile_id": "maker", "expected_version": maker_v1["version"]}),
        )
        assert maker_undone["success"], maker_undone
        assert maker_undone["content"] == ""
        assert maker_undone["restored_from"] == 0
        assert plugin.memory.read("notes", target="maker")["content"] == ""
    finally:
        await plugin.on_unload()
