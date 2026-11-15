import asyncio
import io
import json
import socket
import time
import urllib.error
import urllib.request
import zipfile
from types import SimpleNamespace

import pytest
from test_foundation import loaded

from maisya.abilities import READONLY_TOOLS
from maisya.digest import DigestWorkspace
from maisya.recipes import (
    _prevalidate_routine_mount,
    export_recipe_archive,
    fetch_public_recipe_bytes,
    import_recipe_archive,
)
from maisya.services import Services, validate as validate_service_args
from maisya.shaping import (
    _apply_setup_digest_sources,
    execute_shape,
    handle_services_or_webhooks,
    handle_skill,
    patch_profile,
)
from maisya.trust import Message


def _signed_headers_and_body(incoming, vault_val: str, channel: str, event_id: str, ts: int, body: dict):
    import hashlib
    import hmac

    payload = json.dumps(body, ensure_ascii=False)
    canonical = json.dumps(
        {
            "channel": channel,
            "event_id": event_id,
            "timestamp": ts,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    sig = hmac.new(vault_val.encode(), canonical, hashlib.sha256).hexdigest()
    return payload, sig


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

        patched = await patch_profile(
            plugin,
            "minimal",
            {"add_tools": ["image_read"]},
            author="test",
        )
        expected_added = set(READONLY_TOOLS) | {"image_read"}
        assert set(plugin.profiles.get("minimal").tools) == expected_added
        assert set(patched["profile"]["tools"]) == expected_added

        # 2. Removing a tool from another omitted-tools profile removes only that tool
        minimal2_dir = plugin.profiles.path / "minimal2"
        minimal2_dir.mkdir(parents=True, exist_ok=True)
        (minimal2_dir / "PROFILE.md").write_text("# Minimal2\n", encoding="utf-8")
        (minimal2_dir / "profile.toml").write_text(
            'title = "Minimal2"\nenvironment = "readonly"\naccess = "everyone"\n',
            encoding="utf-8",
        )
        await patch_profile(
            plugin,
            "minimal2",
            {"remove_tools": ["web_search"]},
            author="test",
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
        await patch_profile(
            plugin,
            "empty_tools",
            {"add_tools": ["image_read"]},
            author="test",
        )
        assert plugin.profiles.get("empty_tools").tools == ("image_read",)

        # 4. shape(action="create", kind="profile") seeds version history, rolls back on patch failure,
        #    and shape(action="delete", kind="profile") purges memory versions/search
        created = await execute_shape(
            plugin,
            action="create",
            kind="profile",
            target="helper_b2",
            patch={"base": "scout", "title": "Helper B2"},
            author="admin:test",
        )
        assert created["profile"]["title"] == "Helper B2"
        hist = plugin.memory.history("profile", target="helper_b2", document="profile.toml")
        assert len(hist) >= 2 and {h["version"] for h in hist} >= {0, 1}

        with pytest.raises(ValueError):
            await execute_shape(
                plugin,
                action="create",
                kind="profile",
                target="broken_clone",
                patch={"base": "scout", "unknown_bad_key": 123},
                author="admin:test",
            )
        assert not (plugin.profiles.path / "broken_clone").exists()
        with plugin.memory.store.connect() as db:
            cnt_broken = db.execute(
                "SELECT COUNT(*) FROM memory_versions WHERE asset=?",
                ("profile:broken_clone:profile.toml",),
            ).fetchone()[0]
            assert cnt_broken == 0

        deleted = await execute_shape(
            plugin,
            action="delete",
            kind="profile",
            target="helper_b2",
            author="admin:test",
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
async def test_b2_handle_skill_read_delete_frontmatter_and_secret(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        plugin.vault.set("api_secret", "super-secret-token-value-b2")

        # 1. Reading non-existent skill must not create empty dir or v0 DB record
        with pytest.raises(ValueError):
            await handle_skill(plugin, action="read", target="ghost-skill")
        assert not (plugin.data_root / "abilities/skills/ghost-skill").exists()
        with plugin.memory.store.connect() as db:
            cnt = db.execute(
                "SELECT COUNT(*) FROM memory_versions WHERE asset=?",
                ("skill:ghost-skill:SKILL.md",),
            ).fetchone()[0]
            assert cnt == 0

        # 2. Create skill with CRLF + custom YAML frontmatter metadata
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
        await handle_skill(
            plugin,
            action="create",
            target="custom-ops",
            content=raw_crlf,
            patch={"bind_profiles": ["scout"]},
            author="admin:test",
        )
        assert "custom-ops" in plugin.profiles.get("scout").skills

        # Patch title only -> preserves unknown frontmatter fields & updates cleanly
        patched = await handle_skill(
            plugin,
            action="patch",
            target="custom-ops",
            patch={"title": "新标题"},
            author="admin:test",
        )
        md_after = patched["content"]
        assert "新标题" in md_after
        assert "name: custom-ops" in md_after
        assert "allowed-tools: Read, Grep" in md_after
        assert "tags:" in md_after and "- alpha" in md_after and "- beta" in md_after
        assert "Keep this line." in md_after

        # 3. Empty patch without editable field or bindings must fail (not silent read)
        with pytest.raises(ValueError):
            await handle_skill(plugin, action="patch", target="custom-ops", content="")

        # Bind-only patch honors expected_version
        with pytest.raises(ValueError):
            await handle_skill(
                plugin,
                action="patch",
                target="custom-ops",
                patch={"bind_profiles": ["maker"]},
                expected_version=9999,
            )

        # 4. _assert_no_secret also checks `content` when `opts` is non-empty
        with pytest.raises(ValueError, match="凭据"):
            await handle_skill(
                plugin,
                action="patch",
                target="custom-ops",
                content="secret is super-secret-token-value-b2",
                patch={"title": "Innocent"},
            )

        # 5. Deleting a skill unbinds referencing profiles so Profiles.get stays valid
        del_res = await handle_skill(plugin, action="delete", target="custom-ops")
        assert del_res["deleted"] is True
        assert "custom-ops" not in plugin.profiles.get("scout").skills
        assert not (plugin.data_root / "abilities/skills/custom-ops").exists()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_handle_services_and_webhooks_optimistic_lock_and_empty_dict(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        for kind in ("services", "webhooks"):
            cur = await handle_services_or_webhooks(plugin, kind=kind, action="read")
            ver = cur["version"]

            # write without expected_version must fail
            with pytest.raises(ValueError):
                await execute_shape(
                    plugin,
                    action="write",
                    kind=kind,
                    patch={},
                    author="admin:test",
                )

            # write / patch with stale expected_version must fail
            with pytest.raises(ValueError):
                await execute_shape(
                    plugin,
                    action="write",
                    kind=kind,
                    patch={},
                    expected_version="stale-version-hash",
                    author="admin:test",
                )
            with pytest.raises(ValueError):
                await execute_shape(
                    plugin,
                    action="patch",
                    kind=kind,
                    patch={"dummy": {}} if kind == "webhooks" else {"servers": {}},
                    expected_version="stale-version-hash",
                    author="admin:test",
                )

            # omitted patch and empty content still fail
            with pytest.raises(ValueError):
                await execute_shape(
                    plugin,
                    action="write",
                    kind=kind,
                    expected_version=ver,
                    author="admin:test",
                )

            # explicit patch={} is valid empty config when expected_version matches
            if kind == "webhooks":
                plugin.vault.set("wh_key", "webhook-secret-xyz")
                seeded = await execute_shape(
                    plugin,
                    action="write",
                    kind="webhooks",
                    patch={
                        "ch_a": {
                            "target": "qq:group:100246809",
                            "kind": "proactive",
                            "secret_handle": "wh_key",
                            "profile": "scout",
                        }
                    },
                    expected_version=ver,
                    author="admin:test",
                )
                ver = seeded["version"]
            cleared = await execute_shape(
                plugin,
                action="write",
                kind=kind,
                patch={},
                expected_version=ver,
                author="admin:test",
            )
            assert cleared["version"] != ""
            reread = await handle_services_or_webhooks(plugin, kind=kind, action="read")
            assert reread["configuration"] == {}
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_setup_digest_sources_no_broad_arg_and_within_8s_budget(
    tmp_path, monkeypatch
):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        captured_kwargs: list[dict] = []

        async def strict_seed_starter_feeds(self, topics_or_text="", *, max_add=4):
            captured_kwargs.append({"topics_or_text": topics_or_text, "max_add": max_add})
            return {"ok": True, "seeded": []}

        monkeypatch.setattr(DigestWorkspace, "seed_starter_feeds", strict_seed_starter_feeds)

        warnings: list[str] = []
        res = await _apply_setup_digest_sources(
            plugin,
            {"topics": ["AI", "Rust"], "broad": True, "auto_seed_sources": True},
            effective_chat_id="qq:group:100246809",
            effective_platform="qq",
            digest_involved=True,
            warnings=warnings,
        )
        assert res is not None
        assert captured_kwargs and "broad" not in captured_kwargs[0]

        # Slow add_feed + discover + seed must fit well inside <8s gateway budget
        async def slow_add(self, url, *, title="", auto=False, origin="admin"):
            await asyncio.sleep(6.0)
            return {"ok": True}

        async def slow_discover(self, url, *, origin="admin"):
            await asyncio.sleep(6.0)
            return {"ok": True}

        async def slow_seed(self, topics_or_text="", *, max_add=4):
            await asyncio.sleep(6.0)
            return {"ok": True, "seeded": []}

        monkeypatch.setattr(DigestWorkspace, "add_feed", slow_add)
        monkeypatch.setattr(DigestWorkspace, "discover", slow_discover)
        monkeypatch.setattr(DigestWorkspace, "seed_starter_feeds", slow_seed)

        warnings2: list[str] = []
        t0 = time.monotonic()
        res2 = await asyncio.wait_for(
            _apply_setup_digest_sources(
                plugin,
                {
                    "rss_urls": ["https://feed.example/rss.xml"],
                    "discover_urls": ["https://site.example/"],
                    "topics": ["AI"],
                    "auto_seed_sources": True,
                },
                effective_chat_id="qq:group:100246809",
                effective_platform="qq",
                digest_involved=True,
                warnings=warnings2,
            ),
            timeout=9.0,
        )
        elapsed = time.monotonic() - t0
        assert res2 is not None
        assert elapsed < 8.0, f"digest setup exceeded 8s budget: {elapsed:.2f}s"
        assert warnings2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_recipes_model_list_preset_skill_2mib_pathsafe_update_url_and_budget(
    tmp_path, monkeypatch
):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        # 1. model_list allowed in recipe profile.toml & large skill (>128KiB, <2MiB) + PRESETS_DIR fallback
        skill_zip = io.BytesIO()
        with zipfile.ZipFile(skill_zip, "w") as zf:
            zf.writestr("big-skill/SKILL.md", "---\ntitle: Big Skill\n---\n# Big\n")
            zf.writestr("big-skill/references/guide.txt", "x" * (160 * 1024))
        plugin.memory.import_skill_archive(
            skill_zip.getvalue(),
            target="big-skill",
            filename="big-skill.zip",
        )
        # Also verify >128KiB SKILL.md on disk exports cleanly (<2MiB)
        disk_big = plugin.data_root / "abilities/skills/disk-big-skill"
        disk_big.mkdir(parents=True, exist_ok=True)
        (disk_big / "SKILL.md").write_text(
            "---\ntitle: Disk Big\n---\n# Disk Big\n" + ("y" * (160 * 1024)),
            encoding="utf-8",
        )
        plugin.profiles.clone("scout", "disk_big_src")
        db_toml = plugin.memory.read("profile", target="disk_big_src", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "Disk Big"\nskills = ["disk-big-skill"]\n',
            target="disk_big_src",
            document="profile.toml",
            expected_version=db_toml["version"],
            allow_custom=True,
        )
        raw_disk_zip, _ = export_recipe_archive(plugin, "disk_big_src")
        assert raw_disk_zip
        # Delete runtime news-digest copy so export tests PRESETS_DIR fallback
        nd_dir = plugin.data_root / "abilities/skills/news-digest"
        if nd_dir.exists():
            import shutil

            shutil.rmtree(nd_dir)

        plugin.profiles.clone("scout", "recipe_src")
        cur_toml = plugin.memory.read("profile", target="recipe_src", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "Recipe Source"\n'
            'environment = "readonly"\n'
            'model_list = ["m-primary", "m-backup"]\n'
            'skills = ["big-skill", "news-digest"]\n',
            target="recipe_src",
            document="profile.toml",
            expected_version=cur_toml["version"],
            allow_custom=True,
        )
        raw_zip, _ = export_recipe_archive(plugin, "recipe_src")
        imported_copy = import_recipe_archive(
            plugin,
            raw_zip,
            target="recipe_dst",
            mode="copy",
            install_skills=True,
            overwrite_skills=False,
        )
        assert imported_copy["id"] == "recipe_dst"
        assert plugin.profiles.get("recipe_dst").model_list == ("m-primary", "m-backup")

        # 2. mode="update" strictly respects explicit overwrite_skills=False,
        #    while omitted overwrite_skills defaults to True on update
        sk_cur = plugin.memory.read("skill", target="big-skill", allow_custom=True)
        plugin.memory.write(
            "skill",
            "---\ntitle: Local Override\n---\nLOCAL-KEEP\n",
            target="big-skill",
            expected_version=sk_cur["version"],
            author="test",
            allow_custom=True,
        )
        import_recipe_archive(
            plugin,
            raw_zip,
            target="recipe_dst",
            mode="update",
            install_skills=True,
            overwrite_skills=False,
        )
        assert "LOCAL-KEEP" in plugin.memory.read("skill", target="big-skill")["content"]
        # Omitted overwrite_skills in mode="update" overwrites as upgrade default
        import_recipe_archive(
            plugin,
            raw_zip,
            target="recipe_dst",
            mode="update",
            install_skills=True,
        )
        assert "Big Skill" in plugin.memory.read("skill", target="big-skill")["content"]

        # Over-2MiB skill fails cleanly on export
        oversized_skill = plugin.data_root / "abilities/skills/oversized-sk"
        oversized_skill.mkdir(parents=True, exist_ok=True)
        (oversized_skill / "SKILL.md").write_bytes(b"#" + b"a" * (2 * 1024 * 1024 + 100))
        plugin.profiles.clone("scout", "too_big_src")
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
            export_recipe_archive(plugin, "too_big_src")

        # 3. Reject Windows reserved name CON.txt & 3-part preset/skills/foo file collision
        bad_con = io.BytesIO()
        with zipfile.ZipFile(bad_con, "w") as zf:
            zf.writestr("PROFILE.md", "# R\n")
            zf.writestr("profile.toml", 'title = "R"\n')
            zf.writestr("preset/CON.txt", "bad")
        with pytest.raises(ValueError):
            import_recipe_archive(plugin, bad_con.getvalue(), target="bad_con", mode="copy")

        bad_3part = io.BytesIO()
        with zipfile.ZipFile(bad_3part, "w") as zf:
            zf.writestr("PROFILE.md", "# R\n")
            zf.writestr("profile.toml", 'title = "R"\n')
            zf.writestr("preset/skills/foo", "collision")
        with pytest.raises(ValueError):
            import_recipe_archive(plugin, bad_3part.getvalue(), target="bad_3p", mode="copy")

        # 4. Routine mount accepts MD5 <-> qq:group alias via enabled_for/aliases
        await plugin.on_config_update(
            "self",
            {**plugin.settings.model_dump(), "enabled_chats": ["qq:group:100246809"]},
            0,
        )
        md5_sid = "c0300b39c80870992aca16d9fcb724c5"
        plugin.messages.record(
            Message(
                message_id="m-r1",
                session_id=md5_sid,
                platform="qq",
                user_id="111",
                is_plugin_relayed=False,
                group_id="100246809",
            )
        )
        _prevalidate_routine_mount(
            plugin,
            routines=[{"id": "rt1", "title": "t", "prompt": "p", "every_seconds": 3600}],
            selected_ids=["rt1"],
            chat_id=md5_sid,
            final_enabled=True,
            final_bound=True,
        )

        # 5. GitHub URL with '/' in branch name (e.g. feat/v2) + <8s remote fetch budget
        sample_zip = io.BytesIO()
        with zipfile.ZipFile(sample_zip, "w") as zf:
            zf.writestr("repo-feat-v2/roles/scout/PROFILE.md", "# Scout\n")
            zf.writestr("repo-feat-v2/roles/scout/profile.toml", 'title = "Scout"\n')
        zip_bytes = sample_zip.getvalue()

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
            ],
        )

        class SlashBranchResp:
            headers = {}

            def __init__(self, data: bytes):
                self._data = data
                self._read = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, n):
                if self._read:
                    return b""
                self._read = True
                return self._data

        requested_urls: list[str] = []

        class SlashBranchOpener:
            def open(self, req, timeout=15):
                requested_urls.append(req.full_url)
                if req.full_url.endswith("/zip/feat"):
                    raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
                if req.full_url.endswith("/zip/feat/v2"):
                    return SlashBranchResp(zip_bytes)
                raise AssertionError(f"unexpected url: {req.full_url}")

        monkeypatch.setattr(
            urllib.request, "build_opener", lambda *h: SlashBranchOpener()
        )
        got_bytes, _fname, subpath = fetch_public_recipe_bytes(
            "https://github.com/acme/repo/tree/feat/v2/roles/scout"
        )
        assert got_bytes == zip_bytes
        assert subpath == "roles/scout"
        assert requested_urls[-1].endswith("/zip/feat/v2")

        # Slow trickle / sleep budget enforcement (< 8s total even with default timeout)
        call_timeouts: list[float] = []

        class BudgetOpener:
            def open(self, req, timeout=15):
                call_timeouts.append(float(timeout))
                time.sleep(0.15)
                raise urllib.error.HTTPError(
                    req.full_url,
                    302,
                    "Found",
                    {"Location": "https://public.example/hop"},
                    None,
                )

        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: BudgetOpener())
        t0 = time.monotonic()
        with pytest.raises(ValueError):
            fetch_public_recipe_bytes("https://public.example/slow.zip")
        assert time.monotonic() - t0 < 8.0
        assert call_timeouts and call_timeouts[0] <= 8.0
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
            "templates": {},
        }
        svc.load = lambda: svc.configuration
        svc._session = fake_session
        profile = plugin.profiles.get("scout")
        profile_with_mcp = SimpleNamespace(
            **{
                **profile.__dict__,
                "tools": (*profile.tools, "mcp_call"),
                "mcp": ("demo",),
            }
        )
        args = {"service": "demo", "tool": "echo", "arguments": {"v": "a"}}
        # Step 0 and Step 1 share revision & _tool_call_id="call_0", must NOT collide!
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

        # Same step + same call_id hits completed receipt
        r0_dup = await svc.call(
            profile_with_mcp,
            args,
            {"thread_id": "th_b2", "revision": 1, "_tool_call_id": "call_0", "_step_index": 0},
            plugin.runtime,
        )
        assert server_calls == 2
        assert r0_dup["repeated"] is True and r0_dup["text"] == "call-1-a"

        # Uncertain / empty {} receipt must NOT short-circuit
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


@pytest.mark.asyncio
async def test_b2_incoming_secret_handle_and_proactive_thread_validation(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        plugin.vault.set("wh_sec", "webhook-hmac-secret-1234")
        # 1. non-str secret_handle raises ValueError (never TypeError)
        for bad_handle in (123, None, ["wh_sec"], {"h": "wh_sec"}):
            with pytest.raises(ValueError):
                plugin.incoming.validate(
                    {
                        "bad": {
                            "target": "qq:group:100246809",
                            "kind": "proactive",
                            "secret_handle": bad_handle,
                            "profile": "scout",
                        }
                    }
                )

        # 2. silent with both profile and thread_id omitted must fail;
        #    proactive plain event webhook (both omitted) stays valid
        with pytest.raises(ValueError):
            plugin.incoming.validate(
                {
                    "empty_silent": {
                        "target": "qq:group:100246809",
                        "kind": "silent",
                        "secret_handle": "wh_sec",
                    }
                }
            )
        plugin.incoming.validate(
            {
                "plain_proactive": {
                    "target": "qq:group:100246809",
                    "kind": "proactive",
                    "secret_handle": "wh_sec",
                }
            }
        )

        # 3. proactive + invalid / other-chat / unauthorized thread_id must fail
        other_row = plugin.threads.create(
            plugin.profiles.get("scout"),
            "other chat task",
            chat_id="qq:group:99999999",
            platform="qq",
            user_id="1",
        )
        plugin.profiles.clone("scout", "restricted_prof")
        cur_p = plugin.memory.read("profile", target="restricted_prof", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "Restricted"\naccess = "admins"\n',
            target="restricted_prof",
            document="profile.toml",
            expected_version=cur_p["version"],
            allow_custom=True,
        )
        restricted_row = plugin.threads.create(
            plugin.profiles.get("restricted_prof"),
            "admin only task",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="1",
        )
        for bad_tid in ("th_missing", other_row["thread_id"], restricted_row["thread_id"]):
            with pytest.raises((ValueError, PermissionError)):
                plugin.incoming.validate(
                    {
                        "bad_proactive": {
                            "target": "qq:group:100246809",
                            "kind": "proactive",
                            "secret_handle": "wh_sec",
                            "thread_id": bad_tid,
                        }
                    }
                )

        # 4. Valid proactive + autonomous thread_id passes validate and receive
        good_row = plugin.threads.create(
            plugin.profiles.get("scout"),
            "good proactive thread",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="1",
        )
        plugin.incoming.validate(
            {
                "good_proactive": {
                    "target": "qq:group:100246809",
                    "kind": "proactive",
                    "secret_handle": "wh_sec",
                    "thread_id": good_row["thread_id"],
                }
            }
        )
        plugin.incoming.path.write_text(
            json.dumps(
                {
                    "good_proactive": {
                        "target": "qq:group:100246809",
                        "kind": "proactive",
                        "secret_handle": "wh_sec",
                        "thread_id": good_row["thread_id"],
                    }
                }
            ),
            encoding="utf-8",
        )
        now = int(time.time())
        bad_payload, bad_sig = _signed_headers_and_body(
            plugin.incoming, "webhook-hmac-secret-1234", "good_proactive", "evt-bad-reason", now, {"reason": 12345}
        )
        with pytest.raises(ValueError):
            await plugin.incoming.receive("good_proactive", "evt-bad-reason", now, bad_payload, bad_sig)

        long_reason = "R" * 700
        ok_payload, ok_sig = _signed_headers_and_body(
            plugin.incoming, "webhook-hmac-secret-1234", "good_proactive", "evt-ok-reason", now, {"reason": long_reason}
        )
        ok_res = await plugin.incoming.receive("good_proactive", "evt-ok-reason", now, ok_payload, ok_sig)
        assert ok_res["success"] is True
        intent = plugin.intents.get(ok_res["intent_id"])
        assert len(intent["reason"]) == 500
    finally:
        await plugin.on_unload()
