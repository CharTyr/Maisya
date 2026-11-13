import asyncio
import base64
import hashlib

import pytest
from test_foundation import loaded

from maisya.abilities import Profiles


def test_maker_preset_and_profile_archive_do_not_export_runtime_bindings(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    maker = profiles.get("maker")
    assert maker.environment == "sandbox" and maker.network == "public"
    assert "sandbox_exec" in maker.tools
    profiles.clone("maker", "artist")
    package = profiles.export("artist")
    profiles.import_package(package, "another")
    imported = profiles.get("another")
    assert imported.environment == "sandbox"
    assert imported.secrets == ()
    assert imported.mcp == ()


def test_profile_package_rejects_unknown_secret_fields_and_known_secret_assets(
    tmp_path,
):
    from maisya.vault import Vault

    vault = Vault(tmp_path / "vault")
    vault.set("test", "test-secret-value-for-export")
    profiles = Profiles(tmp_path / "profiles", contains_secret=vault.contains_secret)
    profile_file = profiles.path / "maker/profile.toml"
    original = profile_file.read_text()
    profile_file.write_text(original + '\napi_key = "raw-key-must-not-be-in-profile"\n')
    with pytest.raises(ValueError):
        profiles.export("maker")
    profile_file.write_text(original)
    (profiles.path / "maker/NOTES.md").write_text("test-secret-value-for-export")
    with pytest.raises(ValueError, match="凭据"):
        profiles.export("maker")


@pytest.mark.asyncio
async def test_unavailable_maker_refuses_execution_without_affecting_scout(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        # maker without container degrades to light_workspace mode (disabling sandbox_exec)
        maker = plugin.profiles.get("maker")
        assert plugin.runtime.effective_mode(maker) == "light_workspace"
        row = plugin.threads.create(
            maker, "轻量工作区测试", chat_id="chat", platform="qq", user_id="Alice"
        )
        with pytest.raises(RuntimeError, match="sandbox_exec|沙箱"):
            await plugin.runtime.call(
                "sandbox_exec",
                {"command": "echo hi"},
                {**row, "_tool_call_id": "exec-no-sandbox"},
            )
        # Strict container-only profile still refuses execution when sandbox is unavailable
        strict_dir = plugin.profiles.path / "strict_shell"
        strict_dir.mkdir(parents=True)
        (strict_dir / "PROFILE.md").write_text("仅允许隔离容器命令执行。")
        (strict_dir / "profile.toml").write_text(
            'environment = "sandbox"\naccess = "everyone"\ntools = ["sandbox_exec"]\n'
        )
        rejected = await plugin.agent_tool(
            action="start",
            profile="strict_shell",
            message="计算 2+2",
            chat_id="chat",
            platform="qq",
        )
        assert not rejected["success"]
        assert "沙箱" in rejected["error"]
        assert plugin.profiles.get("scout").environment == "readonly"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_named_maker_uses_files_and_real_new_input_on_silent_resume(tmp_path):
    plugin = await loaded(tmp_path)
    plugin.runtime.sandbox.ready = True
    calls = []

    async def fake_execution(row, work, command, **kwargs):
        work.write("out/result.txt", "42")
        return {"exit_code": 0, "output": "42", "checkpoint": work.commit()}

    plugin.runtime.sandbox.execute = fake_execution

    async def capability(name, **arguments):
        assert name == "llm.generate_with_tools"
        calls.append(arguments)
        if len(calls) == 1:
            return {
                "tool_calls": [
                    {
                        "id": "exec-1",
                        "function": {
                            "name": "sandbox_exec",
                            "arguments": {"command": 'python -c "print(6*7)"'},
                        },
                    }
                ]
            }
        if len(calls) == 2:
            return {
                "tool_calls": [
                    {
                        "id": "attach-1",
                        "function": {
                            "name": "result_attach",
                            "arguments": {"paths": ["out/result.txt"]},
                        },
                    }
                ]
            }
        return {"response": "答案是 42", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        result = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="calculation",
            message="计算并保存",
            chat_id="chat",
            platform="qq",
        )
        assert result["success"], result
        assert result["result"]["attachments"][0]["handle"].startswith("att_")
        thread_id = result["thread_id"]
        again = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="calculation",
            message="继续解释",
            chat_id="chat",
            platform="qq",
        )
        assert again["thread_id"] == thread_id
        previous = len(calls)
        intent = plugin.intents.schedule(
            kind="silent",
            source="thread",
            target="chat:chat",
            due_at=plugin.intents.clock(),
            reason="第三轮继续",
            thread_id=thread_id,
            payload={"platform": "qq"},
        )
        await plugin.pulse._intents()
        await plugin.engine.drain()
        row = plugin.threads.get(thread_id, "chat")
        assert row["revision"] >= 3
        assert len(calls) > previous
        assert any(
            item.get("role") == "user" and "第三轮继续" in item["content"]
            for item in row["history"]
        )
        assert plugin.intents.get(intent["intent_id"])["status"] == "done"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_concurrent_named_inputs_are_processed_individually_in_order(tmp_path):
    plugin = await loaded(tmp_path)
    plugin.runtime.sandbox.ready = True
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def capability(name, **arguments):
        messages = [
            item["content"]
            for item in arguments["prompt"]
            if item.get("role") == "user"
        ]
        seen.append(messages[-1])
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return {"response": "完成 " + messages[-1], "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        first = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="ordered",
            message="第一步",
            wait_seconds=0,
            chat_id="chat",
            platform="qq",
        )
        await entered.wait()
        second = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="ordered",
            message="第二步",
            wait_seconds=0,
            chat_id="chat",
            platform="qq",
        )
        assert first["thread_id"] == second["thread_id"]
        release.set()
        await plugin.engine.drain()
        assert seen == ["第一步", "第二步"]
        row = plugin.threads.get(first["thread_id"], "chat")
        assert row["completed_revision"] == row["revision"] == 2
        assert row["state"] == "idle"
    finally:
        release.set()
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_reference_files_are_imported_without_binary_model_history(tmp_path):
    plugin = await loaded(tmp_path)
    plugin.runtime.sandbox.ready = True
    prompts = []

    async def capability(name, **arguments):
        if name == "message.get_by_id":
            assert arguments["chat_id"] == "chat"
            return {
                "message_id": "reference",
                "session_id": "chat",
                "raw_message": [
                    {
                        "type": "file",
                        "data": {
                            "name": "input.csv",
                            "base64": base64.b64encode(b"a,b\n1,2").decode(),
                        },
                    }
                ],
            }
        assert name == "llm.generate_with_tools"
        prompts.append(arguments["prompt"])
        return {"response": "已读取输入文件", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        result = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="files",
            message="分析 CSV",
            ref_msg_ids=["reference"],
            chat_id="chat",
            platform="qq",
        )
        assert result["success"], result
        assert "input.csv" in str(prompts)
        assert base64.b64encode(b"a,b\n1,2").decode() not in str(prompts)
        row = plugin.threads.get(result["thread_id"], "chat")
        with plugin.runtime.files.activity(row) as work:
            reference_key = hashlib.sha256(b"reference").hexdigest()[:12]
            assert (
                work.read(f"in/ref-{reference_key}-0-input.csv")["text"] == "a,b\n1,2"
            )
        again = await plugin.agent_tool(
            action="send",
            thread_id=result["thread_id"],
            message="再看一次",
            ref_msg_ids=["reference"],
            chat_id="chat",
            platform="qq",
        )
        assert again["success"] and again["state"] == "idle", again
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_restricted_history_requires_fresh_grant_before_model_request(tmp_path):
    from dataclasses import replace

    plugin = await loaded(tmp_path)
    seen = []
    profile = replace(plugin.profiles.get("scout"), access="admins")

    async def capability(name, **arguments):
        seen.append(name)
        return {"response": "private result", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        row = await plugin.engine.start(
            profile,
            "private request",
            chat_id="chat",
            platform="qq",
            user_id="admin",
            wait_seconds=2,
        )
        assert "error" in row["result"]
        assert not seen
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_sandbox_config_update_replaces_runtime_after_cancelling_threads(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    previous = plugin.runtime.sandbox
    config = plugin.settings.model_dump()
    config["sandbox"]["timeout"] = 45
    try:
        await plugin.on_config_update("self", config, 2)
        assert previous.closing
        assert plugin.runtime.sandbox is not previous
        assert plugin.runtime.sandbox.settings.timeout == 45
        assert plugin.runtime.files.volume is None
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_explicit_steering_interrupts_named_activity_and_supersedes_queue(
    tmp_path,
):
    plugin = await loaded(tmp_path)
    plugin.runtime.sandbox.ready = True
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def capability(name, **arguments):
        messages = [
            item["content"]
            for item in arguments["prompt"]
            if item.get("role") == "user"
        ]
        seen.append(messages[-1])
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return {"response": "完成 " + messages[-1], "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        first = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="steering",
            message="第一步",
            wait_seconds=0,
            chat_id="chat",
            platform="qq",
        )
        await entered.wait()
        await plugin.agent_tool(
            action="send",
            thread_id=first["thread_id"],
            message="第二步",
            wait_seconds=0,
            chat_id="chat",
            platform="qq",
        )
        steered = await plugin.agent_tool(
            action="send",
            thread_id=first["thread_id"],
            message="立即转向",
            steer=True,
            wait_seconds=2,
            chat_id="chat",
            platform="qq",
        )
        assert steered["success"] and steered["state"] == "idle", steered
        assert seen == ["第一步", "立即转向"]
        row = plugin.threads.get(first["thread_id"], "chat")
        assert row["completed_revision"] == 3
        assert "第二步" in str(row["history"])
    finally:
        release.set()
        await plugin.on_unload()
