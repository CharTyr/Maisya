import base64
import io
import json
import socket
import tarfile
import urllib.error
import urllib.request
import zipfile

import pytest
from test_foundation import loaded

from maisya.abilities import Profiles
from maisya.recipes import (
    _PinnedHTTPConnection,
    build_recipe_draft,
    export_recipe_archive,
    fetch_public_recipe_bytes,
    import_recipe_archive,
    normalize_public_recipe_url,
    validate_public_recipe_url,
)


@pytest.mark.asyncio
async def test_recipe_draft_strips_allowlists_scans_privacy_and_blocks_secrets(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        clean_scout = build_recipe_draft(plugin, "scout")
        assert clean_scout["can_export"] is True
        assert clean_scout["findings"] == []

        plugin.vault.set("paid_api", "super-secret-vault-value-8899")
        plugin.profiles.clone("scout", "analyst")

        # Configure local allowlists, custom skill + companion, notes with warnings, and a routine
        plugin.memory.import_skill_archive(
            b"---\nname: chart-spec\ndescription: \xe5\x9b\xbe\xe8\xa1\xa8\xe6\xa0\xb8\xe9\xaa\x8c\n---\n# Chart Spec\n",
            target="chart-spec",
            filename="SKILL.md",
        )
        comp_zip = io.BytesIO()
        with zipfile.ZipFile(comp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "chart-spec/SKILL.md",
                "---\nname: chart-spec\ndescription: 图表核验规范\n---\n# Chart Spec\n",
            )
            zf.writestr("chart-spec/references/checklist.md", "- Check axes\n")
        plugin.memory.import_skill_archive(comp_zip.getvalue(), filename="chart-spec.zip")

        toml_cur = plugin.memory.read("profile", target="analyst", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "行业分析师"\n'
            'environment = "readonly"\n'
            'access = "admins"\n'
            'tools = ["web_search", "web_fetch"]\n'
            'allow_chats = ["chat", "902106123"]\n'
            'allow_users = ["qq:Alice"]\n'
            'secrets = ["paid_api"]\n'
            'skills = ["chart-spec"]\n',
            target="analyst",
            document="profile.toml",
            expected_version=toml_cur["version"],
            allow_custom=True,
        )
        notes_path = plugin.profiles.path / "analyst/NOTES.md"
        notes_path.write_text(
            "测试环境地址 192.168.1.50，联系 dev@example.com，路径 /Users/alice/work，群 902106123。\n",
            encoding="utf-8",
        )
        preset_dir = plugin.profiles.path / "analyst/preset"
        preset_dir.mkdir(parents=True)
        (preset_dir / "starter.txt").write_text("初始模板内容\n", encoding="utf-8")

        plugin.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:chat",
            due_at=plugin.intents.clock() + 3600,
            reason="每日行业巡检",
            payload={
                "platform": "qq",
                "profile": "analyst",
                "workspace": "daily_ws",
                "prompt": "巡检行业一手发布并汇总",
                "every_seconds": 86400,
            },
            bypass_limits=True,
        )

        draft = build_recipe_draft(
            plugin,
            "analyst",
            {"secret_notes": {"paid_api": "请在供应商后台申请只读密钥并写入 /vault set paid_api"}},
        )
        assert draft["can_export"] is True
        assert draft["blocker_count"] == 0
        assert draft["stripped"] == {
            "allow_chats": 2,
            "allow_users": 1,
            "routine_chat_ids": 1,
        }
        categories = {f["category"] for f in draft["findings"]}
        assert {"private_ip", "email", "local_path", "numeric_id"} <= categories

        # Export archive and inspect sanitized contents + backward compatibility with Profiles.import_package
        archive_bytes, meta = export_recipe_archive(
            plugin,
            "analyst",
            {"secret_notes": {"paid_api": "只读查询密钥"}},
        )
        assert meta["bytes"] == len(archive_bytes)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            names = set(zf.namelist())
            assert {
                "PROFILE.md",
                "profile.toml",
                "NOTES.md",
                "preset/starter.txt",
                "preset/skills/chart-spec/SKILL.md",
                "preset/skills/chart-spec/references/checklist.md",
                "recipe.json",
                "RECIPE.md",
            } <= names
            exported_toml = zf.read("profile.toml").decode("utf-8")
            assert "allow_chats" not in exported_toml
            assert "allow_users" not in exported_toml
            assert "902106123" not in exported_toml
            manifest = json.loads(zf.read("recipe.json").decode("utf-8"))
            assert manifest["routines"][0]["title"] == "每日行业巡检"
            assert "chat" not in json.dumps(manifest["routines"], ensure_ascii=False)
            assert manifest["dependencies"]["credentials"][0] == {
                "handle": "paid_api",
                "purpose": "只读查询密钥",
            }
            assert "只读查询密钥" in zf.read("RECIPE.md").decode("utf-8")

        # Legacy Profiles.import_package still imports the recipe zip seamlessly
        legacy_dest = Profiles(tmp_path / "legacy_profiles")
        legacy_prof = legacy_dest.import_package(archive_bytes, "legacy_analyst")
        assert legacy_prof.id == "legacy_analyst"
        assert legacy_prof.allow_chats == ()
        assert legacy_prof.allow_users == ()
        assert legacy_prof.bound is False

        # Toggling off optional assets omits them cleanly
        slim_bytes, _ = export_recipe_archive(
            plugin,
            "analyst",
            {
                "include_skills": False,
                "include_notes": False,
                "include_presets": False,
                "include_routines": False,
            },
        )
        with zipfile.ZipFile(io.BytesIO(slim_bytes)) as zf:
            slim_names = set(zf.namelist())
            assert slim_names == {"PROFILE.md", "profile.toml", "recipe.json", "RECIPE.md"}

        # Vault secret or raw API key in NOTES.md / preset triggers blocker and refuses export
        notes_path.write_text("泄露密钥 super-secret-vault-value-8899", encoding="utf-8")
        blocked_draft = build_recipe_draft(plugin, "analyst")
        assert blocked_draft["can_export"] is False
        assert blocked_draft["blocker_count"] >= 1
        with pytest.raises(ValueError, match="凭据"):
            export_recipe_archive(plugin, "analyst")

        notes_path.write_text("硬编码 sk-proj1234567890abcdefghijkl", encoding="utf-8")
        with pytest.raises(ValueError, match="Token|私钥"):
            export_recipe_archive(plugin, "analyst")
    finally:
        await plugin.on_unload()


def test_public_recipe_url_normalizer_and_ssrf_redirect_guards(monkeypatch):
    assert normalize_public_recipe_url("https://github.com/acme/maisya-recipes") == (
        "https://codeload.github.com/acme/maisya-recipes/zip/HEAD",
        "",
    )
    assert normalize_public_recipe_url(
        "https://github.com/acme/maisya-recipes/tree/main/roles/painter"
    ) == (
        "https://codeload.github.com/acme/maisya-recipes/zip/main",
        "roles/painter",
    )
    assert normalize_public_recipe_url(
        "https://github.com/acme/maisya-recipes/blob/v1.2/packs/scout.zip"
    ) == (
        "https://raw.githubusercontent.com/acme/maisya-recipes/v1.2/packs/scout.zip",
        "",
    )
    assert normalize_public_recipe_url("https://gist.github.com/acme/12345abcdef") == (
        "https://gist.github.com/acme/12345abcdef/raw",
        "",
    )

    # Reject bad schemes, ports, credentials, localhost
    for bad_url in (
        "",
        "file:///etc/passwd",
        "ftp://example.com/a.zip",
        "https://user:pass@example.com/a.zip",
        "http://localhost/a.zip",
        "https://printer.local/a.zip",
        "https://example.com:8443/a.zip",
    ):
        with pytest.raises(ValueError):
            validate_public_recipe_url(bad_url)

    def fake_getaddrinfo(host, port, *args, **kwargs):
        mapping = {
            "public.example": "93.184.216.34",
            "codeload.github.com": "140.82.112.10",
            "loopback.evil": "127.0.0.1",
            "private10.evil": "10.0.0.8",
            "metadata.evil": "169.254.169.254",
            "cgnat.evil": "100.64.1.2",
            "v6mapped.evil": "::ffff:127.0.0.1",
        }
        if host not in mapping:
            raise socket.gaierror("not found")
        ip = mapping[host]
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(fam, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    assert validate_public_recipe_url("https://public.example/recipe.zip") == (
        "https://public.example/recipe.zip"
    )
    for blocked_host in (
        "loopback.evil",
        "private10.evil",
        "metadata.evil",
        "cgnat.evil",
        "v6mapped.evil",
    ):
        with pytest.raises(ValueError, match="内网|回环|保留"):
            validate_public_recipe_url(f"https://{blocked_host}/recipe.zip")

    # Test 302 redirect from public host to loopback is blocked before connecting!
    class RedirectToLoopbackOpener:
        def open(self, req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url,
                302,
                "Found",
                {"Location": "http://loopback.evil/internal.zip"},
                None,
            )

    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *handlers: RedirectToLoopbackOpener()
    )
    with pytest.raises(ValueError, match="内网|回环|保留"):
        fetch_public_recipe_bytes("https://public.example/start.zip")

    # Test oversize response (> 8 MiB) is aborted immediately
    class OversizeResp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n):
            return b"x" * n

    class OversizeOpener:
        def open(self, req, timeout=15):
            return OversizeResp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: OversizeOpener())
    with pytest.raises(ValueError, match="8 MiB"):
        fetch_public_recipe_bytes("https://public.example/huge.zip")


@pytest.mark.asyncio
async def test_public_github_url_preview_alignment_smooth_update_rollback_and_copy(
    tmp_path, monkeypatch
):
    src_plugin = await loaded(tmp_path / "src", ["qq:Admin"])
    dst_plugin = await loaded(tmp_path / "dst", ["qq:Alice"])
    try:
        # 1. Build a v2 recipe on src_plugin with bundled skill, notes, preset file, secret handle, and routine
        src_plugin.profiles.clone("maker", "designer")
        sk_zip = io.BytesIO()
        with zipfile.ZipFile(sk_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "poster-grid/SKILL.md",
                "---\nname: poster-grid\ndescription: 瑞士网格海报排版规范\n---\n# Poster Grid\n",
            )
            zf.writestr("poster-grid/references/tokens.css", ":root { --grid: 8px; }\n")
        src_plugin.memory.import_skill_archive(sk_zip.getvalue(), filename="poster-grid.zip")

        t_cur = src_plugin.memory.read("profile", target="designer", document="profile.toml")
        src_plugin.memory.write(
            "profile",
            'title = "视觉设计师 V2"\n'
            'environment = "sandbox"\n'
            'network = "public"\n'
            'access = "admins"\n'
            'task = "planner"\n'
            'web_provider = "google_search_plugin"\n'
            'tools = ["web_search", "workspace_file", "html_render", "result_attach", "sandbox_exec"]\n'
            'skills = ["poster-grid"]\n'
            'secrets = ["brand_token"]\n',
            target="designer",
            document="profile.toml",
            expected_version=t_cur["version"],
            allow_custom=True,
        )
        md_cur = src_plugin.memory.read("profile", target="designer", document="PROFILE.md")
        src_plugin.memory.write(
            "profile",
            "# 视觉设计师 V2\n负责网格卡片排版与视觉输出。\n",
            target="designer",
            document="PROFILE.md",
            expected_version=md_cur["version"],
            allow_custom=True,
        )
        (src_plugin.profiles.path / "designer/NOTES.md").write_text(
            "优先使用高对比度字号与留白。\n", encoding="utf-8"
        )
        (src_plugin.profiles.path / "designer/preset").mkdir(parents=True, exist_ok=True)
        (src_plugin.profiles.path / "designer/preset/layout.html").write_text(
            "<main>V2 Layout</main>\n", encoding="utf-8"
        )
        src_plugin.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:chat",
            due_at=src_plugin.intents.clock() + 600,
            reason="每周视觉周报卡",
            payload={
                "platform": "qq",
                "profile": "designer",
                "workspace": "weekly_card",
                "prompt": "生成本周设计周报卡片",
                "every_seconds": 604800,
            },
            bypass_limits=True,
        )
        recipe_zip, _ = export_recipe_archive(
            src_plugin,
            "designer",
            {"secret_notes": {"brand_token": "品牌字体授权 Token"}},
        )

        # Wrap inside a GitHub-style repo zipball (`maisya-recipes-main/roles/designer/...`)
        gh_zip_buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(recipe_zip)) as src_zf, zipfile.ZipFile(
            gh_zip_buf, "w", zipfile.ZIP_DEFLATED
        ) as gh_zf:
            gh_zf.writestr("maisya-recipes-main/README.md", "# Recipes Repo\n")
            for item in src_zf.infolist():
                gh_zf.writestr(
                    f"maisya-recipes-main/roles/designer/{item.filename}",
                    src_zf.read(item.filename),
                )
        gh_zip_bytes = gh_zip_buf.getvalue()

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *a, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.112.10", port))
            ],
        )

        class FakeGitHubResp:
            headers = {"Content-Disposition": 'attachment; filename="maisya-recipes-main.zip"'}

            def __init__(self, payload):
                self._buf = io.BytesIO(payload)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, n=-1):
                return self._buf.read(n)

        class FakeGitHubOpener:
            def open(self, req, timeout=15):
                assert req.full_url == "https://codeload.github.com/acme/maisya-recipes/zip/main"
                return FakeGitHubResp(gh_zip_bytes)

        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: FakeGitHubOpener())

        # 2. Prepare existing local `designer` on dst_plugin with local allowlists, disabled state, and active workspace
        dst_plugin.profiles.clone("maker", "designer")
        dst_toml = dst_plugin.memory.read("profile", target="designer", document="profile.toml")
        dst_plugin.memory.write(
            "profile",
            'title = "旧版设计师"\n'
            'enabled = false\n'
            'bound = true\n'
            'environment = "sandbox"\n'
            'access = "listed"\n'
            'allow_chats = ["chat"]\n'
            'allow_users = ["qq:Alice"]\n'
            'tools = ["workspace_file"]\n',
            target="designer",
            document="profile.toml",
            expected_version=dst_toml["version"],
            allow_custom=True,
        )
        dst_md_v1 = dst_plugin.memory.read("profile", target="designer", document="PROFILE.md")
        dst_plugin.memory.write(
            "profile",
            "# 旧版设计师 V1\n保留旧说明以便验证回滚。\n",
            target="designer",
            document="PROFILE.md",
            expected_version=dst_md_v1["version"],
            allow_custom=True,
        )
        ws_row = dst_plugin.threads.create(
            dst_plugin.profiles.get("scout"),
            "既有设计工作区",
            chat_id="chat",
            platform="qq",
            user_id="qq:Alice",
            workspace="keep_design_ws",
        )
        with dst_plugin.runtime.files.activity(ws_row) as work:
            work.write("project/progress.json", '{"step": "local_work_intact"}')
            work.commit()

        # 3. Preview via public GitHub tree URL BEFORE binding brand_token -> alignment shows missing secret
        pv1 = await dst_plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {"url": "https://github.com/acme/maisya-recipes/tree/main/roles/designer"}
            ),
        )
        assert pv1["success"], pv1
        assert pv1["existing_profile"]["id"] == "designer"
        assert pv1["existing_profile"]["allow_chats_count"] == 1
        assert pv1["alignment"]["requires_rebind"] is True
        secret_item = next(
            it for it in pv1["alignment"]["items"] if it["kind"] == "secret"
        )
        assert secret_item["status"] == "missing" and "品牌字体授权" in secret_item["detail"]

        # 4. Bind `brand_token` on dst_plugin and re-preview -> secret is now ready!
        dst_plugin.vault.set("brand_token", "dst-local-brand-secret-value")
        pv2 = await dst_plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {"url": "https://github.com/acme/maisya-recipes/tree/main/roles/designer"}
            ),
        )
        assert pv2["success"]
        assert pv2["alignment"]["requires_rebind"] is False

        # 5. Execute Smooth Upgrade (`mode="update"`) on `designer`
        upd = await dst_plugin.manage_write(
            action="recipe_import",
            payload=json.dumps(
                {
                    "preview_token": pv2["preview_token"],
                    "target": "designer",
                    "mode": "update",
                    "install_skills": True,
                }
            ),
        )
        assert upd["success"], upd
        upgraded = dst_plugin.profiles.get("designer")
        assert upgraded.title == "视觉设计师 V2"
        assert "poster-grid" in upgraded.skills
        # Local allowlists & enabled state preserved! (Note: secrets forces access="admins" per security boundary, while allow_chats/allow_users stay intact)
        assert upgraded.enabled is False
        assert upgraded.bound is True
        assert upgraded.allow_chats == ("chat",)
        assert upgraded.allow_users == ("qq:Alice",)
        assert (dst_plugin.profiles.path / "designer/preset/layout.html").read_text("utf-8") == "<main>V2 Layout</main>\n"
        assert (
            dst_plugin.data_root
            / "abilities/skills/poster-grid/references/tokens.css"
        ).is_file()

        # Runtime workspace `keep_design_ws` is 100% untouched!
        with dst_plugin.runtime.files.activity(ws_row) as work:
            assert json.loads((work.path / "project/progress.json").read_text("utf-8")) == {
                "step": "local_work_intact"
            }

        # Rollback `PROFILE.md` restores V1 cleanly!
        hist = await dst_plugin.manage_read(
            section="asset",
            payload=json.dumps(
                {"kind": "profile", "target": "designer", "document": "PROFILE.md"}
            ),
        )
        assert len(hist["history"]) >= 2
        prev_ver = hist["history"][-2]["version"]
        rb = await dst_plugin.manage_write(
            action="rollback",
            payload=json.dumps(
                {
                    "kind": "profile",
                    "target": "designer",
                    "document": "PROFILE.md",
                    "version": prev_ver,
                    "expected_version": hist["version"],
                }
            ),
        )
        assert rb["success"], rb
        assert "旧版设计师 V1" in dst_plugin.profiles.get("designer").instructions

        # 6. Import a clean no-secret `.tar.gz` recipe as a new copy (`mode="copy"`) and mount its routine in one click!
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
            for name, body in {
                "brief-bot/PROFILE.md": b"# Brief Bot\nDaily summary helper.\n",
                "brief-bot/profile.toml": (
                    b'title = "Brief Bot"\nenvironment = "readonly"\n'
                    b'tools = ["web_search", "recall_read"]\n'
                ),
                "brief-bot/recipe.json": json.dumps(
                    {
                        "schema": "maisya.recipe/v1",
                        "profile": {"id": "brief-bot", "title": "Brief Bot"},
                        "routines": [
                            {
                                "id": "rt_daily",
                                "title": "早间速览",
                                "prompt": "汇总今早公开技术要闻",
                                "every_seconds": 86400,
                                "in_seconds": 120,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            }.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(body)
                tf.addfile(info, io.BytesIO(body))
        tar_bytes = tar_buf.getvalue()

        pv_tar = await dst_plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "brief-bot.tar.gz",
                    "data": base64.b64encode(tar_bytes).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        assert pv_tar["success"], pv_tar
        assert pv_tar["alignment"]["ready"] is True

        imp_copy = await dst_plugin.manage_write(
            action="recipe_import",
            payload=json.dumps(
                {
                    "preview_token": pv_tar["preview_token"],
                    "target": "brief_copy",
                    "mode": "copy",
                    "mount_routines": ["rt_daily"],
                    "routine_chat_id": "chat",
                }
            ),
        )
        assert imp_copy["success"], imp_copy
        assert dst_plugin.profiles.get("brief_copy").title == "Brief Bot"
        assert len(imp_copy["mounted_routines"]) == 1
        assert any(
            row["reason"] == "早间速览" and row["source"] == "workflow"
            for row in dst_plugin.intents.list("chat:chat")
        )
    finally:
        await src_plugin.on_unload()
        await dst_plugin.on_unload()


@pytest.mark.asyncio
async def test_recipe_rejects_malicious_archives_symlinks_and_unknown_files(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        # 1. Flat zip containing unexpected arbitrary file outside allowlist
        bad_flat = io.BytesIO()
        with zipfile.ZipFile(bad_flat, "w") as zf:
            zf.writestr("PROFILE.md", "# Role\n")
            zf.writestr("profile.toml", 'title = "Role"\n')
            zf.writestr("backdoor.py", "import os\n")
        res1 = await plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "bad.zip",
                    "data": base64.b64encode(bad_flat.getvalue()).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        assert not res1["success"] and "未允许的文件" in res1["error"]

        # 2. Zip entry marked as symlink
        sym_zip = io.BytesIO()
        with zipfile.ZipFile(sym_zip, "w") as zf:
            zf.writestr("PROFILE.md", "# Role\n")
            zf.writestr("profile.toml", 'title = "Role"\n')
            sinfo = zipfile.ZipInfo("preset/link.txt")
            sinfo.external_attr = 0o120777 << 16
            zf.writestr(sinfo, "/etc/passwd")
        res2 = await plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "sym.zip",
                    "data": base64.b64encode(sym_zip.getvalue()).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        assert not res2["success"] and "符号链接" in res2["error"]

        # 3. Path traversal in archive & bogus preview_token & attacker allowlist injection on smooth update
        trav_zip = io.BytesIO()
        with zipfile.ZipFile(trav_zip, "w") as zf:
            zf.writestr("../escape.toml", 'title = "Bad"\n')
        res3 = await plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "trav.zip",
                    "data": base64.b64encode(trav_zip.getvalue()).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        assert not res3["success"]

        bad_tok = await plugin.manage_write(
            action="recipe_import",
            payload=json.dumps({"preview_token": "pv_nonexistent", "target": "x", "mode": "copy"}),
        )
        assert not bad_tok["success"] and "过期" in bad_tok["error"]

        # Incoming recipe carrying attacker allow_chats + unbound secret -> stripped & set bound=False on update
        plugin.profiles.clone("scout", "local_guard")
        c_cur = plugin.memory.read("profile", target="local_guard", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "本地守卫"\nenvironment = "readonly"\naccess = "listed"\nallow_chats = ["chat"]\ntools = ["web_search"]\n',
            target="local_guard",
            document="profile.toml",
            expected_version=c_cur["version"],
            allow_custom=True,
        )
        inj_zip = io.BytesIO()
        with zipfile.ZipFile(inj_zip, "w") as zf:
            zf.writestr("PROFILE.md", "# Upgraded Guard\nNew instructions.\n")
            zf.writestr(
                "profile.toml",
                'title = "Upgraded Guard"\n'
                'environment = "readonly"\n'
                'access = "everyone"\n'
                'allow_chats = ["attacker_group"]\n'
                'allow_users = ["qq:Attacker"]\n'
                'secrets = ["missing_vault_handle"]\n'
                'tools = ["web_search"]\n',
            )
        pv_inj = await plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "inj.zip",
                    "data": base64.b64encode(inj_zip.getvalue()).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        assert pv_inj["success"] and pv_inj["alignment"]["requires_rebind"] is True
        # Copy onto existing role must fail cleanly
        dup_copy = await plugin.manage_write(
            action="recipe_import",
            payload=json.dumps(
                {"preview_token": pv_inj["preview_token"], "target": "local_guard", "mode": "copy"}
            ),
        )
        assert not dup_copy["success"] and "已存在" in dup_copy["error"]
        # Re-preview since preview_token is single-use or still valid? Check update with fresh preview:
        pv_inj2 = await plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "inj.zip",
                    "data": base64.b64encode(inj_zip.getvalue()).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        upd_inj = await plugin.manage_write(
            action="recipe_import",
            payload=json.dumps(
                {"preview_token": pv_inj2["preview_token"], "target": "local_guard", "mode": "update"}
            ),
        )
        assert upd_inj["success"], upd_inj
        upgraded = plugin.profiles.get("local_guard")
        assert upgraded.allow_chats == ("chat",)
        assert upgraded.allow_users == ()
        assert upgraded.access == "admins"
        assert upgraded.bound is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_recipe_import_privacy_guard_companions_gist_subpath_and_cleanup(
    tmp_path, monkeypatch
):
    from maisya.recipes import build_recipe_preview

    plugin = await loaded(tmp_path, ["qq:Admin"])
    try:
        # 1. Recipe containing warning-level / IPv4 / 8-digit patterns in PROFILE.md & NOTES.md
        warn_zip = io.BytesIO()
        with zipfile.ZipFile(warn_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "PROFILE.md",
                "# 规范巡检助手 v1.2.3.4 (Build 20261113)\n负责检查公开规范更新。\n",
            )
            zf.writestr(
                "profile.toml",
                'title = "规范巡检助手"\n'
                'environment = "sandbox"\n'
                'tools = ["workspace_file", "result_attach"]\n'
                'skills = ["local-only-spec"]\n',
            )
            zf.writestr(
                "NOTES.md",
                "示例占位地址 192.168.1.50，文档反馈 dev@example.com，示例路径 /Users/demo/project。\n",
            )
            zf.writestr(
                "preset/skills/local-only-spec/SKILL.md",
                "---\nname: local-only-spec\ndescription: 仅随角色预设携带的规范\n---\n# Local Spec\n",
            )
            zf.writestr(
                "preset/skills/local-only-spec/references/checklist.md",
                "- Check item A\n",
            )
        warn_bytes = warn_zip.getvalue()

        pv_warn = await plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "warn-role.zip",
                    "data": base64.b64encode(warn_bytes).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        assert pv_warn["success"], pv_warn
        assert len(pv_warn["privacy_warnings"]) >= 1

        # Copy import with install_skills=False: must NOT crash on MemoryLayers._guard,
        # must seed NOTES.md, and must still mount preset skill companions into workspace!
        imp_warn = await plugin.manage_write(
            action="recipe_import",
            payload=json.dumps(
                {
                    "preview_token": pv_warn["preview_token"],
                    "target": "warn_role",
                    "mode": "copy",
                    "install_skills": False,
                }
            ),
        )
        assert imp_warn["success"], imp_warn
        assert not (plugin.data_root / "abilities/skills/local-only-spec").exists()

        notes_read = await plugin.manage_read(
            section="asset",
            payload=json.dumps({"kind": "notes", "target": "warn_role"}),
        )
        assert notes_read["success"], notes_read
        assert "dev@example.com" in notes_read["content"]

        warn_prof = plugin.profiles.get("warn_role")
        methods_txt = plugin.memory.methods(warn_prof)
        assert "references/checklist.md" in methods_txt

        th_row = plugin.threads.create(
            warn_prof,
            "测试预设技能附属文件挂载",
            chat_id="chat",
            platform="qq",
            user_id="qq:Admin",
        )
        work = await plugin.runtime._work(th_row)
        assert (
            work.path / "in/skills/local-only-spec/references/checklist.md"
        ).read_text("utf-8") == "- Check item A\n"
        await plugin.runtime.end(th_row["thread_id"])

        # Delete profile must clean memory_versions so re-import starts fresh at version 0
        del_res = await plugin.manage_write(
            action="delete_profile",
            payload=json.dumps({"target": "warn_role"}),
        )
        assert del_res["success"], del_res
        with plugin.threads.connect() as db:
            leftover = db.execute(
                "SELECT COUNT(*) FROM memory_versions WHERE target='warn_role'"
            ).fetchone()[0]
        assert leftover == 0

        # 2. Bundled skill update removes stale companion files; smooth update preserves bound=True when sandbox_targets unchanged
        plugin.profiles.clone("maker", "net_maker")
        nm_toml = plugin.memory.read("profile", target="net_maker", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "受控联网创作"\n'
            'environment = "sandbox"\n'
            'network = "restricted"\n'
            'access = "admins"\n'
            'bound = true\n'
            'sandbox_targets = ["api.example.com:443"]\n'
            'tools = ["workspace_file", "sandbox_exec"]\n'
            'skills = ["evolving-sk"]\n',
            target="net_maker",
            document="profile.toml",
            expected_version=nm_toml["version"],
            allow_custom=True,
        )
        sk_v1 = io.BytesIO()
        with zipfile.ZipFile(sk_v1, "w") as zf:
            zf.writestr("evolving-sk/SKILL.md", "---\nname: evolving-sk\ndescription: v1\n---\n# V1\n")
            zf.writestr("evolving-sk/references/stale.md", "obsolete\n")
        plugin.memory.import_skill_archive(sk_v1.getvalue(), filename="evolving-sk.zip")
        assert (plugin.data_root / "abilities/skills/evolving-sk/references/stale.md").is_file()

        up_v2 = io.BytesIO()
        with zipfile.ZipFile(up_v2, "w") as zf:
            zf.writestr("PROFILE.md", "# 受控联网创作 V2\n更新说明。\n")
            zf.writestr(
                "profile.toml",
                'title = "受控联网创作 V2"\n'
                'environment = "sandbox"\n'
                'network = "restricted"\n'
                'access = "admins"\n'
                'sandbox_targets = ["api.example.com:443"]\n'
                'tools = ["workspace_file", "sandbox_exec"]\n'
                'skills = ["evolving-sk"]\n',
            )
            zf.writestr("preset/skills/evolving-sk/SKILL.md", "---\nname: evolving-sk\ndescription: v2\n---\n# V2\n")
            zf.writestr("preset/skills/evolving-sk/references/fresh.md", "new\n")
        pv_v2 = await plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "net_maker.zip",
                    "data": base64.b64encode(up_v2.getvalue()).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        assert pv_v2["success"], pv_v2
        imp_v2 = await plugin.manage_write(
            action="recipe_import",
            payload=json.dumps(
                {
                    "preview_token": pv_v2["preview_token"],
                    "target": "net_maker",
                    "mode": "update",
                    "install_skills": True,
                }
            ),
        )
        assert imp_v2["success"], imp_v2
        assert plugin.profiles.get("net_maker").bound is True
        assert (plugin.data_root / "abilities/skills/evolving-sk/references/fresh.md").is_file()
        assert not (plugin.data_root / "abilities/skills/evolving-sk/references/stale.md").exists()

        # Adding a new unapproved sandbox_target on update MUST require rebind (bound=False)
        up_v3 = io.BytesIO()
        with zipfile.ZipFile(up_v3, "w") as zf:
            zf.writestr("PROFILE.md", "# 受控联网创作 V3\n新增目标域名。\n")
            zf.writestr(
                "profile.toml",
                'title = "受控联网创作 V3"\n'
                'environment = "sandbox"\n'
                'network = "restricted"\n'
                'access = "admins"\n'
                'sandbox_targets = ["api.example.com:443", "extra.example.com:443"]\n'
                'tools = ["workspace_file", "sandbox_exec"]\n',
            )
        pv_v3 = await plugin.manage_write(
            action="recipe_preview",
            payload=json.dumps(
                {
                    "filename": "net_maker_v3.zip",
                    "data": base64.b64encode(up_v3.getvalue()).decode("ascii"),
                    "offset": 0,
                    "done": True,
                }
            ),
        )
        imp_v3 = await plugin.manage_write(
            action="recipe_import",
            payload=json.dumps(
                {
                    "preview_token": pv_v3["preview_token"],
                    "target": "net_maker",
                    "mode": "update",
                }
            ),
        )
        assert imp_v3["success"] and plugin.profiles.get("net_maker").bound is False

        # 3. URL normalizer + multi-file Gist fallback + subpath mismatch error + source_id inference
        assert normalize_public_recipe_url("https://gist.github.com/abcdef123456") == (
            "https://gist.github.com/abcdef123456/raw",
            "",
        )
        assert normalize_public_recipe_url(
            "https://github.com/acme/maisya-recipes/releases/tag/v2.1.0"
        ) == (
            "https://codeload.github.com/acme/maisya-recipes/zip/v2.1.0",
            "",
        )

        multi_repo_buf = io.BytesIO()
        with zipfile.ZipFile(multi_repo_buf, "w") as zf:
            zf.writestr("maisya-recipes-main/roles/alpha/PROFILE.md", "# Alpha\n")
            zf.writestr("maisya-recipes-main/roles/alpha/profile.toml", 'title = "Alpha"\n')
            zf.writestr("maisya-recipes-main/roles/painter/PROFILE.md", "# Painter\n")
            zf.writestr("maisya-recipes-main/roles/painter/profile.toml", 'title = "Painter"\n')
        multi_repo_bytes = multi_repo_buf.getvalue()

        with pytest.raises(ValueError, match="未找到|子目录"):
            build_recipe_preview(
                plugin,
                multi_repo_bytes,
                filename="maisya-recipes-main.zip",
                subpath_hint="roles/nonexistent",
            )

        _, painter_pv = build_recipe_preview(
            plugin,
            multi_repo_bytes,
            filename="maisya-recipes-main.zip",
            subpath_hint="roles/painter",
        )
        assert painter_pv["source_id"] == "painter"

        # Multi-file Gist URL where /raw only returns 1st markdown file and API returns full file map
        gist_api_payload = json.dumps(
            {
                "files": {
                    "PROFILE.md": {"content": "# Gist Role\nMulti-file gist recipe.\n", "truncated": False},
                    "profile.toml": {"content": 'title = "Gist Role"\ntools = ["web_search"]\n', "truncated": False},
                }
            }
        ).encode("utf-8")

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *a, **kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("140.82.112.10", port))
            ],
        )

        class FakeGistResp:
            headers = {}

            def __init__(self, body):
                self._buf = io.BytesIO(body)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, n=-1):
                return self._buf.read(n)

        class FakeGistOpener:
            def open(self, req, timeout=15):
                if req.full_url == "https://gist.github.com/acme/99887766aabb/raw":
                    return FakeGistResp(b"# Gist Role\nMulti-file gist recipe.\n")
                if req.full_url == "https://api.github.com/gists/99887766aabb":
                    return FakeGistResp(gist_api_payload)
                raise AssertionError(f"unexpected url: {req.full_url}")

        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: FakeGistOpener())
        raw_gist, gist_fname, _ = fetch_public_recipe_bytes(
            "https://gist.github.com/acme/99887766aabb"
        )
        _, gist_pv = build_recipe_preview(plugin, raw_gist, filename=gist_fname)
        assert gist_pv["profile"]["title"] == "Gist Role"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_recipe_routine_mount_atomicity_and_pinned_ip_connection(
    tmp_path, monkeypatch
):
    # 1. _PinnedHTTPConnection must dial the validated public IP directly (anti-DNS-rebinding)
    dialed = []

    class FakeSock:
        pass

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, timeout=None, source_address=None: (
            dialed.append(address) or FakeSock()
        ),
    )
    conn = _PinnedHTTPConnection("public.example", "93.184.216.34", port=80, timeout=5)
    conn.connect()
    assert dialed == [("93.184.216.34", 80)]

    # 2. If routine mounting fails pre-validation (bound=False due to unbound secret),
    #    no partial profile directory or memory version may be left on disk.
    src_plugin = await loaded(tmp_path / "src_atom", ["qq:Admin"])
    dst_plugin = await loaded(tmp_path / "dst_atom", ["qq:Alice"])
    try:
        src_plugin.profiles.clone("maker", "atomic_role")
        t_cur = src_plugin.memory.read("profile", target="atomic_role", document="profile.toml")
        src_plugin.memory.write(
            "profile",
            'title = "Atom Role"\n'
            'environment = "sandbox"\n'
            'access = "admins"\n'
            'tools = ["workspace_file"]\n'
            'secrets = ["needs_key"]\n',
            target="atomic_role",
            document="profile.toml",
            expected_version=t_cur["version"],
            allow_custom=True,
        )
        src_plugin.intents.schedule(
            kind="return",
            source="workflow",
            target="chat:chat",
            due_at=src_plugin.intents.clock() + 300,
            reason="每日检查",
            payload={
                "platform": "qq",
                "profile": "atomic_role",
                "workspace": "",
                "prompt": "运行检查",
                "every_seconds": 3600,
            },
            bypass_limits=True,
        )
        recipe_zip, _ = export_recipe_archive(src_plugin, "atomic_role")

        with pytest.raises(ValueError, match="待绑定|绑定"):
            import_recipe_archive(
                dst_plugin,
                recipe_zip,
                target="atomic_target",
                mode="copy",
                mount_routines=["*"],
                routine_chat_id="chat",
            )
        assert not (dst_plugin.profiles.path / "atomic_target").exists()

        # Retrying without mounting routines immediately succeeds cleanly
        res = import_recipe_archive(
            dst_plugin,
            recipe_zip,
            target="atomic_target",
            mode="copy",
            mount_routines=[],
            routine_chat_id="",
        )
        assert res["id"] == "atomic_target"
        assert res["bound"] is False
    finally:
        await src_plugin.on_unload()
        await dst_plugin.on_unload()
