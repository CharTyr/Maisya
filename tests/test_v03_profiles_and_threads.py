import asyncio
import io
import json
import time
import tomllib
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from maisya.abilities import Profile, Profiles
from maisya.threads import ThreadEngine, ThreadStore


class RecordingRuntime:
    def __init__(self, resolver=None):
        self.seen = []
        self.profiles_used = []
        self.gate = None
        self.resolver = resolver

    def resolve_profile(self, profile):
        if self.resolver is not None:
            return self.resolver(profile)
        return profile

    async def step(self, profile, history):
        resolved = self.resolve_profile(profile)
        self.profiles_used.append(resolved)
        self.seen.append(json.loads(json.dumps(history)))
        if self.gate is not None:
            await self.gate.wait()
        return {
            "success": True,
            "response": f"[{resolved.id}] {history[-1]['content']}",
            "tool_calls": [],
        }

    async def call(self, name, arguments, context):
        return {"text": "ok"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _make_zip(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            raw = data.encode("utf-8") if isinstance(data, str) else data
            zf.writestr(name, raw)
    return buf.getvalue()


def test_presets_converged_to_single_default_and_legacy_aliases(tmp_path):
    presets_dir = _repo_root() / "presets" / "profiles"
    for builtin_id in ("default", "scout", "digest"):
        assert (presets_dir / builtin_id / "profile.toml").is_file()
        assert (presets_dir / builtin_id / "PROFILE.md").is_file()
    assert not (presets_dir / "maker").exists()

    profiles = Profiles(tmp_path / "profiles")
    listed_ids = [p.id for p in profiles.list_profiles()]
    assert listed_ids == ["default", "digest", "scout"]

    default = profiles.get("default")
    assert default.id == "default"
    assert default.icon_shape
    assert default.theme_color
    assert default.session_mode in {"ephemeral", "task"}
    assert set(default.toolsets) == {"context", "web", "workspace", "exec", "schedule"}

    # maker redundant preset aliases cleanly to default
    aliased = profiles.get("maker")
    assert aliased.id == "default"
    assert aliased.toolsets == default.toolsets
    assert [p.id for p in profiles.list_profiles()] == ["default", "digest", "scout"]

    for protected_id in ("default", "scout", "digest"):
        with pytest.raises(ValueError):
            profiles.delete(protected_id)


def test_toolsets_definition_and_validation(tmp_path):
    from maisya.abilities import TOOLSETS

    assert set(TOOLSETS) == {"context", "web", "workspace", "exec", "schedule"}
    assert {"chat_read", "memory_read", "ask_user"} <= set(TOOLSETS["context"])
    assert {"web_search", "web_fetch"} <= set(TOOLSETS["web"])
    assert {"workspace_file", "html_render", "render_report", "result_attach"} <= set(
        TOOLSETS["workspace"]
    )
    assert {"exec_command", "sandbox_exec"} & set(TOOLSETS["exec"])
    assert "thread_later" in TOOLSETS["schedule"]

    profiles = Profiles(tmp_path / "profiles")
    with pytest.raises(ValueError):
        profiles.create(
            "bad-toolset",
            name="坏工具集",
            description="测试未注册工具集",
            toolsets=["context", "nonexistent_toolset"],
        )


def test_hermes_profile_crud_autogen_and_150_line_limit(tmp_path):
    profiles = Profiles(tmp_path / "profiles")

    # Custom profile defaults to workspace session_mode and auto-generates PROFILE.md
    keeper = profiles.create(
        "coc-keeper",
        name="克苏鲁守秘人",
        description="主持 CoC 7 版跑团，管理调查员卡与检定状态",
        icon_shape="shield",
        theme_color="#5B4B8A",
        model_name="gpt-4o",
        fallback_models=["gpt-4o-mini"],
        toolsets=["context", "workspace", "schedule"],
        skills=["source-checking"],
        mcp_servers=["dice_roller"],
        secret_handles=["vault_dice_token"],
        shared_secrets=True,
        execution_mode="host",
        permission_mode="ask_risky",
        enabled_chats=["qq:group:100246809", "qq:private:10001"],
        access="listed",
    )
    assert keeper.id == "coc-keeper"
    assert keeper.name == "克苏鲁守秘人"
    assert keeper.session_mode == "workspace"
    assert keeper.execution_mode == "host"
    assert keeper.permission_mode == "ask_risky"
    assert keeper.icon_shape == "shield"
    assert keeper.theme_color == "#5B4B8A"
    assert keeper.model_list == ("gpt-4o", "gpt-4o-mini")
    assert keeper.mcp_servers == ("dice_roller",)
    assert keeper.secret_handles == ("vault_dice_token",)
    assert keeper.shared_secrets is True
    assert keeper.enabled_chats == ("qq:group:100246809", "qq:private:10001")
    assert "克苏鲁守秘人" in keeper.instructions
    assert "CoC" in keeper.instructions
    assert "context" in keeper.instructions or "workspace" in keeper.instructions
    assert len(keeper.instructions.splitlines()) <= 150

    # List includes built-in presets + custom
    assert sorted(p.id for p in profiles.list_profiles()) == ["coc-keeper", "default", "digest", "scout"]

    # Boundary: exactly 150 lines succeeds, 151 lines fails
    exact_150 = "\n".join(f"规则条目 {i}" for i in range(150))
    updated = profiles.update("coc-keeper", instructions=exact_150, description="更新描述")
    assert len(updated.instructions.splitlines()) == 150
    assert updated.description == "更新描述"

    too_long = "\n".join(f"超限条目 {i}" for i in range(151))
    with pytest.raises(ValueError, match="150"):
        profiles.update("coc-keeper", instructions=too_long)

    with pytest.raises(ValueError, match="150"):
        profiles.create(
            "too-verbose",
            name="超长规则",
            description="超限测试",
            instructions=too_long,
        )

    # Clone with shared_secrets=True vs False
    shared_clone = profiles.clone("coc-keeper", "coc-shared", shared_secrets=True)
    assert shared_clone.clone_from == "coc-keeper"
    assert shared_clone.secret_handles == ("vault_dice_token",)
    assert shared_clone.shared_secrets is True

    isolated_clone = profiles.clone("coc-keeper", "coc-isolated", shared_secrets=False)
    assert isolated_clone.clone_from == "coc-keeper"
    assert isolated_clone.secret_handles == ()
    assert isolated_clone.shared_secrets is False

    # Delete custom works, built-in default stays protected
    profiles.delete("coc-isolated")
    assert all(p.id != "coc-isolated" for p in profiles.list_profiles())
    with pytest.raises(ValueError):
        profiles.delete("default")


def test_legacy_profile_snapshot_deserialization_and_toolsets_inference():
    legacy_snapshot = {
        "id": "legacy-scout",
        "title": "旧版探索",
        "model": "legacy-m1",
        "fallback_models": ["legacy-m2"],
        "task": "utils",
        "environment": "readonly",
        "tools": ["web_search", "chat_read", "workspace_file"],
        "access": "everyone",
        "instructions": "旧版提示词",
        "mcp": ["legacy_mcp"],
        "secrets": ["legacy_handle"],
        "allow_chats": ["qq:group:100246809"],
    }
    restored = Profile.from_snapshot(legacy_snapshot)
    assert restored.id == "legacy-scout"
    assert restored.name == "旧版探索"
    assert restored.model_list == ("legacy-m1", "legacy-m2")
    assert restored.mcp_servers == ("legacy_mcp",)
    assert restored.secret_handles == ("legacy_handle",)
    assert restored.enabled_chats == ("qq:group:100246809",)
    assert {"web", "context", "workspace"} <= set(restored.toolsets)

    # Direct **kwargs compatibility for existing callers
    direct = Profile(**legacy_snapshot)
    assert direct.id == "legacy-scout"


def test_zip_export_import_security_and_secret_scrubbing(tmp_path):
    secret_value = "SUPER_SECRET_TOKEN_XYZ_98765"
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_profiles = Profiles(
        src_dir / "profiles", contains_secret=lambda raw: secret_value.encode() in raw
    )
    dst_profiles = Profiles(
        dst_dir / "profiles", contains_secret=lambda raw: secret_value.encode() in raw
    )

    src_profiles.create(
        "ops-helper",
        name="运维助手",
        description="排查日志与告警",
        toolsets=["context", "exec"],
        mcp_servers=["logs_mcp"],
        secret_handles=["prod_ssh_handle"],
        enabled_chats=["qq:group:100246809"],
        access="listed",
        instructions="只引用凭据句柄 prod_ssh_handle 执行只读排查。",
    )

    archive_bytes = src_profiles.export_profile_zip("ops-helper")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
        names = set(zf.namelist())
        assert {"profile.toml", "PROFILE.md"} <= names
        toml_data = tomllib.loads(zf.read("profile.toml").decode("utf-8"))
        assert toml_data.get("secret_handles") == ["prod_ssh_handle"] or toml_data.get(
            "secrets"
        ) == ["prod_ssh_handle"]
        assert not toml_data.get("enabled_chats")
        assert not toml_data.get("allow_chats")
        raw_blob = b"".join(zf.read(n) for n in names)
        assert secret_value.encode() not in raw_blob

    imported = dst_profiles.import_profile_zip(archive_bytes, "ops-helper")
    assert imported.id == "ops-helper"
    assert imported.secret_handles == ("prod_ssh_handle",)
    assert imported.toolsets == ("context", "exec")

    # Refuse overwrite unless overwrite=True
    with pytest.raises(ValueError, match="已存在"):
        dst_profiles.import_profile_zip(archive_bytes, "ops-helper", overwrite=False)

    overwritten = dst_profiles.import_profile_zip(
        archive_bytes, "ops-helper", overwrite=True
    )
    assert overwritten.id == "ops-helper"

    # Reject secret leakage and host path leakage on export
    src_profiles.create(
        "leaky-secret",
        name="泄密测试",
        description="含明文凭据",
        instructions=f"token is {secret_value}",
    )
    with pytest.raises(ValueError, match="凭据"):
        src_profiles.export_profile_zip("leaky-secret")

    src_profiles.create(
        "leaky-path",
        name="路径泄露",
        description="含宿主机路径",
        instructions="请读取 /home/operator/private/config.toml 进行分析",
    )
    with pytest.raises(ValueError, match="路径"):
        src_profiles.export_profile_zip("leaky-path")

    # Reject ZipSlip and >150 lines PROFILE.md on import
    zip_slip = _make_zip(
        {
            "../escape.txt": "bad",
            "profile.toml": 'title = "bad"\n',
            "PROFILE.md": "ok\n",
        }
    )
    with pytest.raises(ValueError):
        dst_profiles.import_profile_zip(zip_slip, "safe-slug")

    with pytest.raises(ValueError):
        dst_profiles.import_profile_zip(archive_bytes, "../bad_slug")

    overlong_zip = _make_zip(
        {
            "profile.toml": 'title = "Overlong"\ntoolsets = ["context"]\n',
            "PROFILE.md": "\n".join(f"line {i}" for i in range(151)),
        }
    )
    with pytest.raises(ValueError, match="150"):
        dst_profiles.import_profile_zip(overlong_zip, "overlong-prof")


@pytest.mark.asyncio
async def test_hot_edit_profile_does_not_lock_workspace_or_thread(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    prof = profiles.create(
        "analyst",
        name="分析助手",
        description="持续跟踪工作区数据",
        model_name="m1",
        toolsets=["context", "workspace"],
        session_mode="persistent",
    )
    store = ThreadStore(tmp_path / "maisya.db")
    runtime = RecordingRuntime(resolver=lambda p: profiles.get(p.id))
    engine = ThreadEngine(store, runtime)

    first = await engine.start(
        prof,
        "第一阶段调研",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
        workspace="shared_ws",
    )
    assert first["state"] == "idle"
    assert runtime.profiles_used[-1].model_name == "m1"

    # Hot edit rules, skills, model, and toolsets while workspace/thread already exist
    updated = profiles.update(
        "analyst",
        model_name="m2",
        toolsets=["context", "workspace", "web"],
        instructions="新版热更新规则：先查最新网络资料再写结论。",
    )

    # Creating another thread on same workspace with updated snapshot MUST NOT raise PermissionError
    continued = await engine.send(
        first["thread_id"],
        "继续第二阶段分析",
        chat_id="qq:group:100246809",
    )
    assert continued["thread_id"] == first["thread_id"]
    assert continued["state"] == "idle"
    assert runtime.profiles_used[-1].model_name == "m2"
    assert "web" in runtime.profiles_used[-1].toolsets

    rebound = store.create(
        updated,
        "新回合复用工作区",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
        workspace="shared_ws",
    )
    first_row = store.get(first["thread_id"], "qq:group:100246809")
    assert rebound["thread_id"] == first["thread_id"]
    assert rebound["workspace_id"] == first_row["workspace_id"]
    assert rebound["profile_snapshot"]["model_name"] == "m2"
    await engine.close()


@pytest.mark.asyncio
async def test_dual_track_persistent_primary_thread_and_fresh_archive(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    persistent_prof = profiles.create(
        "dm-host",
        name="常驻主持人",
        description="同会话长线叙事",
        session_mode="persistent",
    )
    ephemeral_prof = profiles.get("default")
    store = ThreadStore(tmp_path / "maisya.db")
    runtime = RecordingRuntime(resolver=lambda p: profiles.get(p.id))
    engine = ThreadEngine(store, runtime)

    # Ephemeral default profile starts a new thread each time
    e1 = await engine.start(
        ephemeral_prof,
        "临时任务 A",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
    )
    e2 = await engine.start(
        ephemeral_prof,
        "临时任务 B",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
    )
    assert e1["thread_id"] != e2["thread_id"]

    # Persistent profile reuses the same primary thread for (chat_id, profile_id)
    p1 = await engine.start(
        persistent_prof,
        "第一幕：雨夜来客",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
    )
    p2 = await engine.start(
        persistent_prof,
        "第二幕：地下室线索",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
    )
    assert p2["thread_id"] == p1["thread_id"]
    history_contents = [m.get("content", "") for m in store.get(p1["thread_id"], "qq:group:100246809")["history"]]
    assert any("第一幕" in text for text in history_contents)
    assert any("第二幕" in text for text in history_contents)

    # fresh=True archives the previous primary thread and opens a clean replacement
    p3 = await engine.start(
        persistent_prof,
        "全新模组开局",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
        fresh=True,
    )
    assert p3["thread_id"] != p1["thread_id"]
    old_row = store.get(p1["thread_id"], "qq:group:100246809")
    assert old_row["state"] == "archived"

    # Re-calling start without fresh reuses the new active primary p3
    p4 = await engine.start(
        persistent_prof,
        "推进新模组线索",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
    )
    assert p4["thread_id"] == p3["thread_id"]

    # Archive search & reopen brings p1 back in-place with original context
    archived = store.list_threads(
        chat_id="qq:group:100246809",
        profile="dm-host",
        state="archived",
        keyword="雨夜来客",
        since=time.time() - 60,
        until=time.time() + 60,
    )
    assert [item["thread_id"] for item in archived] == [p1["thread_id"]]

    resumed = await engine.resume_thread(
        p1["thread_id"],
        "回到雨夜老档继续调查",
        chat_id="qq:group:100246809",
    )
    assert resumed["thread_id"] == p1["thread_id"]
    assert resumed["state"] == "idle"
    resumed_history = store.get(p1["thread_id"], "qq:group:100246809")["history"]
    assert any("第一幕" in m.get("content", "") for m in resumed_history)
    assert any("回到雨夜老档" in m.get("content", "") for m in resumed_history)
    await engine.close()


@pytest.mark.asyncio
async def test_stop_repairs_dangling_tool_calls_and_returns_idle_for_resume(tmp_path):
    class HangingToolRuntime(RecordingRuntime):
        async def step(self, profile, history):
            self.seen.append(json.loads(json.dumps(history)))
            if len(self.seen) == 1:
                return {
                    "success": True,
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "hanging-call-1",
                            "func_name": "web_search",
                            "args": {"query": "slow query"},
                        }
                    ],
                }
            return await super().step(profile, history)

        async def call(self, name, arguments, context):
            await self.gate.wait()
            return {"text": "late tool output"}

    runtime = HangingToolRuntime()
    runtime.gate = asyncio.Event()
    store = ThreadStore(tmp_path / "maisya.db")
    engine = ThreadEngine(store, runtime)

    started = await engine.start(
        Profile(),
        "查一下慢速任务",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="10001",
        wait_seconds=0,
    )
    # Wait briefly until tool call enters gate
    await asyncio.sleep(0.02)

    stopped = await engine.stop(started["thread_id"], chat_id="qq:group:100246809")
    runtime.gate.set()
    await engine.drain()

    assert stopped["state"] == "idle"
    row_after_stop = store.get(started["thread_id"], "qq:group:100246809")
    assert row_after_stop["state"] == "idle"

    # Any assistant message with tool_calls in saved history must have a matching tool receipt
    saved_history = row_after_stop["history"]
    for idx, msg in enumerate(saved_history):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            call_ids = {tc["id"] for tc in msg["tool_calls"]}
            following_tool_ids = {
                m.get("tool_call_id")
                for m in saved_history[idx + 1 :]
                if m.get("role") == "tool"
            }
            assert call_ids <= following_tool_ids

    # Resuming via send works seamlessly
    resumed = await engine.send(
        started["thread_id"],
        "换个角度继续",
        chat_id="qq:group:100246809",
    )
    assert resumed["thread_id"] == started["thread_id"]
    assert resumed["state"] == "idle"
    assert "换个角度继续" in resumed["result"]["text"]

    # Legacy closed/done states can also reopen cleanly
    done_row = store.get(started["thread_id"], "qq:group:100246809")
    store.finish(
        started["thread_id"],
        "qq:group:100246809",
        {"text": "阶段完结"},
        done_row["history"],
        done_row["revision"],
        state="done",
    )
    assert store.get(started["thread_id"], "qq:group:100246809")["state"] == "done"
    reopened = store.reopen(started["thread_id"], chat_id="qq:group:100246809")
    assert reopened["state"] == "idle"
    await engine.close()
