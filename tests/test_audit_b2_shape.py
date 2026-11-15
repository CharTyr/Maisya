import io
import json
import shutil
import zipfile
from types import SimpleNamespace

import pytest
from test_foundation import loaded

from maisya.abilities import READONLY_TOOLS
from maisya.services import Services, validate as validate_service_args
from maisya.shaping import execute_shape


@pytest.mark.asyncio
async def test_b2_patch_profile_preserves_implicit_readonly_tools_and_syncs_create_delete(
    tmp_path,
):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        # 1. Profile omitting `tools` gets READONLY_TOOLS (6) by default
        minimal_dir = plugin.profiles.path / "minimal"
        minimal_dir.mkdir(parents=True, exist_ok=True)
        (minimal_dir / "PROFILE.md").write_text("# Minimal\nReadonly role.\n", encoding="utf-8")
        (minimal_dir / "profile.toml").write_text(
            'title = "Minimal"\nenvironment = "readonly"\naccess = "everyone"\n',
            encoding="utf-8",
        )
        assert set(plugin.profiles.get("minimal").tools) == set(READONLY_TOOLS)

        patched = await execute_shape(
            plugin,
            action="profile",
            op="patch",
            target="minimal",
            patch={"tools": sorted(set(READONLY_TOOLS) | {"image_read"})},
            author="test",
            admin_verified=True,
        )
        assert "minimal" == patched["profile"]["profile_id"]
        assert set(plugin.profiles.get("minimal").tools) == set(READONLY_TOOLS)

        # 2. Removing a tool explicitly updates only that tool
        minimal2_dir = plugin.profiles.path / "minimal2"
        minimal2_dir.mkdir(parents=True, exist_ok=True)
        (minimal2_dir / "PROFILE.md").write_text("# Minimal2\n", encoding="utf-8")
        (minimal2_dir / "profile.toml").write_text(
            'title = "Minimal2"\nenvironment = "readonly"\naccess = "everyone"\n',
            encoding="utf-8",
        )
        await execute_shape(
            plugin,
            action="profile",
            op="patch",
            target="minimal2",
            patch={"tools": sorted(set(READONLY_TOOLS) - {"web_search"})},
            author="test",
            admin_verified=True,
        )
        assert set(plugin.profiles.get("minimal2").tools) == set(READONLY_TOOLS) - {
            "web_search"
        }

        # 3. Profile explicitly declaring `tools = []` keeps [] baseline
        empty_dir = plugin.profiles.path / "empty_tools"
        empty_dir.mkdir(parents=True, exist_ok=True)
        (empty_dir / "PROFILE.md").write_text("# Empty\n", encoding="utf-8")
        (empty_dir / "profile.toml").write_text(
            'title = "Empty"\nenvironment = "readonly"\ntools = []\n',
            encoding="utf-8",
        )
        assert plugin.profiles.get("empty_tools").tools == ()
        await execute_shape(
            plugin,
            action="profile",
            op="patch",
            target="empty_tools",
            patch={"tools": ["image_read"]},
            author="test",
            admin_verified=True,
        )
        assert plugin.profiles.get("empty_tools").tools == ("image_read",)

        # 4. shape(action="profile", op="create") seeds version history,
        #    and shape(action="profile", op="delete") purges memory versions/search
        created = await execute_shape(
            plugin,
            action="profile",
            op="create",
            target="helper_b2",
            patch={"base": "default", "title": "Helper B2"},
            author="admin:test",
            admin_verified=True,
        )
        assert created["profile"]["title"] == "Helper B2"
        hist = plugin.memory.history("profile", target="helper_b2", document="profile.toml")
        assert len(hist) >= 2 and {h["version"] for h in hist} >= {0, 1}

        deleted = await execute_shape(
            plugin,
            action="profile",
            op="delete",
            target="helper_b2",
            author="admin:test",
            admin_verified=True,
        )
        assert deleted["deleted"] is True
        assert not (plugin.profiles.path / "helper_b2").exists()
        with plugin.memory.store.connect() as db:
            cnt_deleted = db.execute(
                "SELECT COUNT(*) FROM memory_versions WHERE asset=?",
                ("profile:helper_b2:profile.toml",),
            ).fetchone()[0]
            assert cnt_deleted == 0
        with pytest.raises(ValueError):
            plugin.memory.read("profile", target="helper_b2", document="profile.toml")
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_skill_file_read_delete_frontmatter_and_secret(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        plugin.vault.set("api_secret", "super-secret-token-value-b2")

        # 1. Listing skills or deleting non-existent skill must not create empty files/DB records
        listed = await execute_shape(
            plugin,
            action="file",
            op="list",
            admin_verified=True,
        )
        assert "skills/ghost-skill/SKILL.md" not in listed["files"]
        with pytest.raises(ValueError, match="不存在"):
            plugin.memory.delete_skill("ghost-skill")
        assert not (plugin.data_root / "abilities/skills/ghost-skill").exists()
        with plugin.memory.store.connect() as db:
            cnt = db.execute(
                "SELECT COUNT(*) FROM memory_versions WHERE asset=?",
                ("skill:ghost-skill:SKILL.md",),
            ).fetchone()[0]
            assert cnt == 0

        # 2. Create skill with CRLF + custom YAML frontmatter metadata & bind to default
        raw_crlf = (
            "---\r\n"
            "name: custom-ops\r\n"
            "title: 原标题\r\n"
            "description: 原描述\r\n"
            "allowed-tools: Read, Grep\r\n"
            "tags:\r\n"
            "  - alpha\r\n"
            "  - beta\r\n"
            "---\r\n\r\n"
            "# 正文\r\n"
            "Keep this line.\r\n"
        )
        created_sk = plugin.memory.create_skill("custom-ops", content=raw_crlf)
        await execute_shape(
            plugin,
            action="profile",
            op="patch",
            target="default",
            patch={"skills": ["custom-ops"]},
            author="admin:test",
            admin_verified=True,
        )
        assert "custom-ops" in plugin.profiles.get("default").skills

        # Secret check on file write rejects vault plaintext
        with pytest.raises(ValueError, match="凭据"):
            await execute_shape(
                plugin,
                action="file",
                op="write",
                document="skills/custom-ops/SKILL.md",
                content="secret is super-secret-token-value-b2",
                expected_version=created_sk["version"],
                admin_verified=True,
            )

        # Updating skill content via file write preserves version optimistic locking
        written = await execute_shape(
            plugin,
            action="file",
            op="write",
            document="skills/custom-ops/SKILL.md",
            content=raw_crlf.replace("原标题", "新标题"),
            expected_version=created_sk["version"],
            admin_verified=True,
        )
        assert "新标题" in written["content"]
        assert "allowed-tools: Read, Grep" in written["content"]
        assert "Keep this line." in written["content"]

        # Unbind & delete custom skill cleans filesystem + DB records
        await execute_shape(
            plugin,
            action="profile",
            op="patch",
            target="default",
            patch={"skills": []},
            author="admin:test",
            admin_verified=True,
        )
        del_res = plugin.memory.delete_skill("custom-ops")
        assert del_res["deleted"] is True
        assert not (plugin.data_root / "abilities/skills/custom-ops").exists()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_shaping_rejects_removed_areas_and_enforces_file_optimistic_lock(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        for removed in ("setup", "webhooks", "services", "recipe", "digest_sources"):
            with pytest.raises(ValueError, match="已收敛移除"):
                await execute_shape(plugin, action=removed, admin_verified=True)

        cur = await execute_shape(
            plugin,
            action="file",
            op="read",
            document="MEMORY.md",
            target_chat="qq:group:100246809",
            admin_verified=True,
        )
        with pytest.raises(ValueError, match="expected_version"):
            await execute_shape(
                plugin,
                action="file",
                op="write",
                document="MEMORY.md",
                target_chat="qq:group:100246809",
                content="missing expected_version",
                admin_verified=True,
            )
        with pytest.raises(ValueError):
            await execute_shape(
                plugin,
                action="file",
                op="write",
                document="MEMORY.md",
                target_chat="qq:group:100246809",
                content="stale version",
                expected_version=cur["version"] + 99,
                admin_verified=True,
            )
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_skill_archive_and_profile_zip_boundaries(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        # 1. import_skill_archive supports >128KiB companion guide & PRESETS_DIR fallback on export
        skill_zip = io.BytesIO()
        with zipfile.ZipFile(skill_zip, "w") as zf:
            zf.writestr("big-skill/SKILL.md", "---\ntitle: Big Skill\n---\n# Big\n")
            zf.writestr("big-skill/references/guide.txt", "x" * (160 * 1024))
        plugin.memory.import_skill_archive(
            skill_zip.getvalue(),
            target="big-skill",
            filename="big-skill.zip",
        )
        nd_dir = plugin.data_root / "abilities/skills/news-digest"
        if nd_dir.exists():
            shutil.rmtree(nd_dir)

        plugin.profiles.clone("default", "zip_src")
        cur_toml = plugin.memory.read("profile", target="zip_src", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "Zip Source"\n'
            'environment = "readonly"\n'
            'fallback_models = ["m-primary", "m-backup"]\n'
            'skills = ["big-skill", "news-digest"]\n',
            target="zip_src",
            document="profile.toml",
            expected_version=cur_toml["version"],
            allow_custom=True,
        )
        raw_zip = plugin.profiles.export_profile_zip("zip_src")
        imported = plugin.profiles.import_profile_zip(raw_zip, "zip_dst")
        assert imported.id == "zip_dst"
        assert imported.fallback_models == ("m-primary", "m-backup")

        # 2. Oversized skill (>2MiB) fails cleanly on export_skill_archive / export
        oversized_skill = plugin.data_root / "abilities/skills/oversized-sk"
        oversized_skill.mkdir(parents=True, exist_ok=True)
        (oversized_skill / "SKILL.md").write_bytes(b"#" + b"a" * (2 * 1024 * 1024 + 100))
        plugin.profiles.clone("default", "too_big_src")
        tb_toml = plugin.memory.read("profile", target="too_big_src", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "Too Big"\nskills = ["oversized-sk"]\n',
            target="too_big_src",
            document="profile.toml",
            expected_version=tb_toml["version"],
            allow_custom=True,
        )
        with pytest.raises(ValueError):
            plugin.profiles.export("too_big_src")

        # 3. Reject ZipSlip traversal and unallowed root files inside profile zip
        bad_traversal = io.BytesIO()
        with zipfile.ZipFile(bad_traversal, "w") as zf:
            zf.writestr("PROFILE.md", "# R\n")
            zf.writestr("profile.toml", 'title = "R"\n')
            zf.writestr("../escape.txt", "bad")
        with pytest.raises(ValueError):
            plugin.profiles.import_profile_zip(bad_traversal.getvalue(), "bad_trav")

        bad_extra = io.BytesIO()
        with zipfile.ZipFile(bad_extra, "w") as zf:
            zf.writestr("PROFILE.md", "# R\n")
            zf.writestr("profile.toml", 'title = "R"\n')
            zf.writestr("other/unexpected.txt", "bad")
        with pytest.raises(ValueError):
            plugin.profiles.import_profile_zip(bad_extra.getvalue(), "bad_ext")
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_services_schema_ref_content_non_dict_and_step_receipt(tmp_path):
    # 1. validate() permits FastMCP/Pydantic local #/$defs/... and #/definitions/... refs,
    #    rejects external URI refs and unresolved local refs
    pydantic_schema = {
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {"count": {"type": "integer", "minimum": 1}},
                "required": ["count"],
            }
        },
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item"}},
        "required": ["item"],
    }
    validate_service_args(pydantic_schema, {"item": {"count": 2}})
    with pytest.raises(ValueError):
        validate_service_args(pydantic_schema, {"item": {"count": 0}})

    for bad_schema in (
        {"type": "object", "properties": {"x": {"$ref": "https://evil.example/s.json"}}},
        {"type": "object", "properties": {"x": {"$ref": "file:///etc/passwd"}}},
        {"type": "object", "properties": {"x": {"$ref": "#/$defs/Missing"}}},
    ):
        with pytest.raises(ValueError):
            validate_service_args(bad_schema, {"x": 1})

    # 2. _content defends against non-dict items without AttributeError
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        svc = plugin.runtime.services
        for bad_content in (["plain string"], [123], [None]):
            with pytest.raises(ValueError):
                await svc._content(bad_content, {"thread_id": "t1"}, plugin.runtime)

        # 3. mcp_call step-scoped receipt key + does not short-circuit on {} / uncertain
        server_calls = 0

        class FakeSession:
            tools = {"echo": {"inputSchema": {"type": "object", "properties": {"v": {"type": "string"}}}}}

            async def request(self, method, params):
                nonlocal server_calls
                server_calls += 1
                return {"content": [{"type": "text", "text": f"call-{server_calls}-{params['arguments']['v']}"}]}

            async def close(self):
                pass

        async def fake_session(*args, **kwargs):
            return FakeSession()

        svc.configuration = {
            "servers": {"demo": {"command": ["/bin/echo"], "tools": ["echo"], "readonly": True}},
        }
        svc.load = lambda: svc.configuration
        svc._session = fake_session
        profile = plugin.profiles.get("default")
        profile_with_mcp = SimpleNamespace(
            **{
                **profile.__dict__,
                "tools": (*profile.tools, "mcp_call"),
                "mcp": ("demo",),
            }
        )
        args = {"service": "demo", "tool": "echo", "arguments": {"v": "a"}}
        r0 = await svc.call(
            profile_with_mcp,
            args,
            {"thread_id": "th_b2", "revision": 1, "_tool_call_id": "call_0", "_step_index": 0},
            plugin.runtime,
        )
        r1 = await svc.call(
            profile_with_mcp,
            args,
            {"thread_id": "th_b2", "revision": 1, "_tool_call_id": "call_0", "_step_index": 1},
            plugin.runtime,
        )
        assert server_calls == 2
        assert r0["text"] == "call-1-a"
        assert r1["text"] == "call-2-a"

        r0_dup = await svc.call(
            profile_with_mcp,
            args,
            {"thread_id": "th_b2", "revision": 1, "_tool_call_id": "call_0", "_step_index": 0},
            plugin.runtime,
        )
        assert server_calls == 2
        assert r0_dup["repeated"] is True and r0_dup["text"] == "call-1-a"

        plugin.runtime.sandbox._receipt(
            ("th_b2", 1, "s2:call_0"), status="uncertain", result={}
        )
        r2 = await svc.call(
            profile_with_mcp,
            args,
            {"thread_id": "th_b2", "revision": 1, "_tool_call_id": "call_0", "_step_index": 2},
            plugin.runtime,
        )
        assert server_calls == 3
        assert r2["text"] == "call-3-a" and not r2.get("repeated")
    finally:
        await plugin.on_unload()
