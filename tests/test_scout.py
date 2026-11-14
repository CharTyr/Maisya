from types import SimpleNamespace

import pytest

from maisya.abilities import Profile, Profiles
from maisya.scout import Scout


class Host:
    def __init__(self):
        self.calls = []

    async def call_capability(self, name, **arguments):
        self.calls.append((name, arguments))
        if name == "llm.generate_with_tools":
            return {"success": True, "response": "verified answer", "tool_calls": []}
        if name == "web.search":
            return {
                "success": True,
                "result": {
                    "results": [{"url": "https://example.org", "title": "Example"}]
                },
            }
        if name == "message.get_recent":
            return {
                "success": True,
                "messages": [
                    {
                        "session_id": "chat-a",
                        "processed_plain_text": "public in this chat",
                    }
                ],
            }
        return {"success": True, "content": "read-only data"}


def test_profiles_seed_without_overwriting_and_reject_write_tools(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    profile = profiles.get("scout")
    assert profile.environment == "readonly"
    notes = profiles.path / "scout/PROFILE.md"
    notes.write_text("custom exploration style", encoding="utf-8")
    assert (
        Profiles(profiles.path).get("scout").instructions == "custom exploration style"
    )
    config = profiles.path / "scout/profile.toml"
    config.write_text('environment="readonly"\ntools=["shell_exec"]', encoding="utf-8")
    with pytest.raises(ValueError):
        profiles.get("scout")


@pytest.mark.asyncio
async def test_native_model_and_web_tools_stay_inside_scout():
    host = Host()
    scout = Scout(host)
    response = await scout.step(Profile(), [{"role": "user", "content": "question"}])
    assert response["response"] == "verified answer"
    assert host.calls[0][0] == "llm.generate_with_tools"
    assert host.calls[0][1]["task_name"] == "utils"
    result = await scout.call(
        "web_search", {"query": "Kamakura"}, {"chat_id": "chat-a", "owner": "chat"}
    )
    assert result["results"][0]["url"] == "https://example.org"
    assert host.calls[-1][0] == "web.search"
    with pytest.raises(ValueError):
        await scout.call("shell_exec", {"cmd": "echo unsafe"}, {"chat_id": "chat-a"})


@pytest.mark.asyncio
async def test_chat_and_memory_are_scoped_by_runtime_not_model():
    host = Host()
    scout = Scout(host)
    context = {"chat_id": "chat-a", "owner": "chat"}
    result = await scout.call("chat_read", {"query": "public"}, context)
    assert "public" in str(result)
    assert host.calls[-1][1]["chat_id"] == "chat-a"
    with pytest.raises(ValueError):
        await scout.call("chat_read", {"chat_id": "chat-b"}, context)
    with pytest.raises(PermissionError):
        await scout.call("chat_read", {}, {"chat_id": "chat-a", "owner": "self"})
    await scout.call("memory_read", {"query": "group preferences"}, context)
    assert host.calls[-1][1]["chat_id"] == "chat-a"


@pytest.mark.asyncio
async def test_empty_model_response_is_not_reported_as_success():
    async def empty(*args, **kwargs):
        return {"success": False, "error": "backend failed"}

    scout = Scout(SimpleNamespace(call_capability=empty))
    with pytest.raises(RuntimeError):
        await scout.step(Profile(), [{"role": "user", "content": "question"}])


@pytest.mark.asyncio
async def test_fallback_keeps_original_context():
    host = Host()
    calls = []

    async def generate(name, **arguments):
        calls.append(arguments)
        if len(calls) == 1:
            return {"success": False, "error": "429"}
        return {"success": True, "response": "Recovered with context", "tool_calls": []}

    host.call_capability = generate
    history = [{"role": "user", "content": "Preserve the exploration"}]
    result = await Scout(host).step(
        Profile(model="first", fallback_models=("backup",)), history
    )
    assert result["response"] == "Recovered with context"
    assert calls[0]["prompt"] == calls[1]["prompt"]
    assert calls[1]["model_name"] == "backup"


@pytest.mark.asyncio
async def test_image_binary_only_goes_to_visual_capability():
    import base64

    binary = base64.b64encode(b"\xff\xd8\xff" + b"test-only-image-content").decode()
    calls = []

    async def rpc(name, **arguments):
        calls.append((name, arguments))
        if name == "message.get_by_id":
            return {
                "success": True,
                "message": {
                    "session_id": "chat-a",
                    "raw_message": [{"type": "image", "binary_data_base64": binary}],
                },
            }
        return {"success": True, "response": "A seaside view"}

    result = await Scout(SimpleNamespace(call_capability=rpc)).call(
        "image_read",
        {"message_id": "image", "question": "What is visible?"},
        {"chat_id": "chat-a", "owner": "chat"},
    )
    assert result == "A seaside view"
    assert calls[1][0] == "llm.generate_for_image"
    assert calls[1][1]["image_format"] == "jpeg"
    assert binary not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://10.0.0.1",
        "http://localhost",
        "file:///etc/passwd",
        "https://user:pass@example.org",
    ],
)
async def test_fetch_rejects_internal_and_nonpublic_addresses(url):
    host = Host()
    with pytest.raises(ValueError):
        await Scout(host).call("web_fetch", {"url": url}, {"chat_id": "chat-a"})
    assert host.calls == []


@pytest.mark.asyncio
async def test_real_sdk_unwraps_message_and_memory_results():
    from maibot_sdk.context import PluginContext

    async def rpc(method, plugin_id, payload):
        name = payload["capability"]
        if name == "message.get_recent":
            return {
                "success": True,
                "messages": [
                    {"session_id": "chat-a", "processed_plain_text": "SDK result"}
                ],
            }
        if name == "knowledge.search":
            return {"success": True, "content": "scoped knowledge"}
        return {"success": False, "error": "not expected"}

    scout = Scout(PluginContext("chartyr.maisya", rpc_call=rpc))
    context = {"chat_id": "chat-a", "owner": "chat"}
    assert "SDK result" in str(await scout.call("chat_read", {}, context))
    assert (
        await scout.call("memory_read", {"query": "knowledge"}, context)
        == "scoped knowledge"
    )


@pytest.mark.asyncio
async def test_profile_model_list_and_three_step_fallback(tmp_path):
    from maisya.abilities import Profile, Profiles

    profiles = Profiles(tmp_path / "profiles")
    (tmp_path / "profiles" / "scout" / "profile.toml").write_text(
        'title = "探索者"\nmodel_list = ["primary-m", "backup-a", "backup-b"]\n',
        encoding="utf-8",
    )
    loaded_profile = profiles.get("scout")
    assert loaded_profile.model_list == ("primary-m", "backup-a", "backup-b")
    assert loaded_profile.model_name == "primary-m"
    assert loaded_profile.fallback_models == ("backup-a", "backup-b")

    host = Host()
    tried = []

    async def generate(name, **arguments):
        tried.append(arguments.get("model_name"))
        if len(tried) == 1:
            return {"success": False, "error": "rate_limit"}
        if len(tried) == 2:
            return {"success": True, "response": "", "tool_calls": []}
        return {"success": True, "response": "Third backup ok", "tool_calls": []}

    host.call_capability = generate
    result = await Scout(host).step(
        Profile(id="scout", model_name="primary-m", fallback_models=("backup-a", "backup-b")),
        [{"role": "user", "content": "check fallback"}],
    )
    assert tried == ["primary-m", "backup-a", "backup-b"]
    assert result["response"] == "Third backup ok"
    assert result["used_model"] == "backup-b"


@pytest.mark.asyncio
async def test_host_model_config_broadcast_sanitizes_secrets_and_feeds_abilities(tmp_path):
    from test_foundation import loaded

    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        async def capability(name, **arguments):
            if name == "llm.get_available_models":
                return {"success": True, "models": ["planner", "utils"]}
            if name == "statistics.local.models":
                return {
                    "success": True,
                    "models": [{"model_name": "stat-model-x"}],
                }
            return {"success": True}

        plugin.ctx.call_capability = capability
        await plugin.on_config_update(
            "model",
            {
                "models": [
                    {
                        "name": "step-5-preview",
                        "model_identifier": "stepfun/step-5",
                        "api_provider": "StepFun",
                    },
                    {
                        "name": "glm-5",
                        "model_identifier": "zhipu/glm-5",
                        "api_provider": "Zhipu",
                    },
                ],
                "model_task_config": {
                    "planner": {"model_list": ["step-5-preview", "glm-5"]}
                },
                "api_providers": [
                    {
                        "name": "StepFun",
                        "api_key": "sk-secret-do-not-leak",
                        "base_url": "https://secret.example/v1",
                    }
                ],
            },
            2,
        )
        disc = await plugin.manage_read("discovery", "{}")
        assert disc["success"]
        abilities = await plugin.manage_read("abilities", "{}")
        assert abilities["success"]
        for expected in ("step-5-preview", "glm-5", "stat-model-x"):
            assert expected in abilities["available_models"]
        serialized = (
            str(disc)
            + str(abilities)
            + (plugin.data_root / "host_models.json").read_text(encoding="utf-8")
        )
        assert "sk-secret-do-not-leak" not in serialized
        assert "secret.example" not in serialized
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_host_model_config_toml_lists_unused_unassigned_models_without_reload_or_secret_leak(
    tmp_path,
):
    import json as _json
    import os
    from pathlib import Path
    from types import SimpleNamespace

    from maibot_sdk.context import PluginPaths
    from test_foundation import event, loaded

    from plugin import create_plugin

    cfg_dir = tmp_path / "host/config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    model_cfg = cfg_dir / "model_config.toml"
    initial_toml = """
[[api_providers]]
name = "DeepSeek"
base_url = "https://private-gateway.internal/v1"
api_key = "sk-top-secret-provider-key"

[[models]]
name = "step-5-preview"
model_identifier = "stepfun/step-5"
api_provider = "StepFun"

[[models]]
name = "deepseek-r1-standby"
model_identifier = "deepseek/deepseek-reasoner"
api_provider = "DeepSeek"

[[models]]
name = "qwen-coder-unused"
model_identifier = "qwen/qwen3-coder"
api_provider = "Bailian"

[model_task_config.planner]
model_list = ["step-5-preview"]

[model_task_config.web_search]
model_list = ["tavily"]
"""
    model_cfg.write_text(initial_toml, encoding="utf-8")

    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        llm_calls = []

        async def capability(name, **arguments):
            if name == "llm.get_available_models":
                return {"success": True, "models": ["planner", "utils", "web_search"]}
            if name == "statistics.local.models":
                return {"success": True, "models": []}
            if name == "llm.generate_with_tools":
                llm_calls.append(dict(arguments))
                return {"success": True, "response": "ok", "tool_calls": []}
            return {"success": True}

        plugin.ctx.call_capability = capability
        import time as _time

        msg_evt = event(text="检查可用模型", user="Alice", platform="qq", group=False)
        msg_evt["timestamp"] = str(int(_time.time()))
        await plugin.after_receive(message=msg_evt)

        # 1. Immediately after on_load() (no on_config_update, empty statistics):
        # all [[models]] including unassigned/unused ones appear, and web_search is excluded
        abilities = await plugin.manage_read("abilities", "{}")
        assert abilities["success"] is True
        for name in ("step-5-preview", "deepseek-r1-standby", "qwen-coder-unused"):
            assert name in abilities["available_models"]
        assert "tavily" not in abilities["available_models"]
        by_name = {m["name"]: m for m in abilities["model_catalog"]}
        assert by_name["deepseek-r1-standby"]["provider"] == "DeepSeek"
        assert by_name["deepseek-r1-standby"]["identifier"] == "deepseek/deepseek-reasoner"
        assert by_name["deepseek-r1-standby"]["assigned_tasks"] == []
        assert by_name["step-5-preview"]["assigned_tasks"] == ["planner"]

        ov = await plugin.shape_tool(
            action="read",
            kind="overview",
            source_msg_id="host-message-123456789",
            chat_id="chat",
            platform="qq",
        )
        assert ov["success"] is True, ov
        assert "qwen-coder-unused" in ov["host_models"]["names"]
        assert "deepseek-r1-standby" in ov["host_models"]["names"]
        assert "tavily" not in ov["host_models"]["names"]

        # 2. Live modification of model_config.toml on disk picks up newly added unused model without reload
        model_cfg.write_text(
            initial_toml
            + '\n[[models]]\nname = "glm-4-air-unused"\nmodel_identifier = "zhipu/glm-4-air"\napi_provider = "Zhipu"\n',
            encoding="utf-8",
        )
        abilities2 = await plugin.manage_read("abilities", "{}")
        assert "glm-4-air-unused" in abilities2["available_models"]
        ov2 = await plugin.shape_tool(
            action="read",
            kind="overview",
            source_msg_id="host-message-123456789",
            chat_id="chat",
            platform="qq",
        )
        assert "glm-4-air-unused" in ov2["host_models"]["names"]

        # 3. Assigning an unused host model to a role works end-to-end with Scout.step
        patched = await plugin.shape_tool(
            action="patch",
            kind="profile",
            target="scout",
            patch={"model_name": "qwen-coder-unused"},
            source_msg_id="host-message-123456789",
            chat_id="chat",
            platform="qq",
        )
        assert patched["success"] is True, patched
        res = await Scout(plugin.ctx).step(
            plugin.profiles.get("scout"),
            [{"role": "user", "content": "test unused model"}],
        )
        assert res["response"] == "ok"
        assert llm_calls[-1]["model_name"] == "qwen-coder-unused"

        # 4. Zero secret leakage across abilities, overview, discovery, and persisted host_models.json
        disc = await plugin.manage_read("discovery", "{}")
        host_models_disk = (plugin.data_root / "host_models.json").read_text(encoding="utf-8")
        combined = (
            _json.dumps(abilities2, ensure_ascii=False)
            + _json.dumps(ov2, ensure_ascii=False)
            + _json.dumps(disc, ensure_ascii=False)
            + host_models_disk
        )
        assert "sk-top-secret-provider-key" not in combined
        assert "private-gateway.internal" not in combined

        saved_payload = _json.loads(host_models_disk)
        saved_by_name = {m["name"]: m for m in saved_payload["models"]}
        assert saved_by_name["deepseek-r1-standby"]["provider"] == "DeepSeek"
        assert saved_by_name["deepseek-r1-standby"]["identifier"] == "deepseek/deepseek-reasoner"
    finally:
        await plugin.on_unload()

    # 5. Reloading from saved host_models.json when model_config.toml is absent preserves fields
    os.remove(Path(tmp_path / "host/config/model_config.toml"))

    async def get_perm(key, default=None):
        return ["qq:Alice"] if key == "plugin.permission" else default

    reloaded = create_plugin()
    reloaded._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get_perm),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=plugin.ctx.logger,
    )
    reloaded.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": ["chat"],
        }
    )
    await reloaded.on_load()
    try:
        ab_reloaded = await reloaded.manage_read("abilities", "{}")
        by_name = {m["name"]: m for m in ab_reloaded["model_catalog"]}
        assert by_name["deepseek-r1-standby"]["provider"] == "DeepSeek"
        assert by_name["deepseek-r1-standby"]["identifier"] == "deepseek/deepseek-reasoner"
        assert by_name["step-5-preview"]["assigned_tasks"] == ["planner"]
        assert "glm-4-air-unused" in ab_reloaded["available_models"]
    finally:
        await reloaded.on_unload()


@pytest.mark.asyncio
async def test_scout_retries_same_model_without_unsupported_temperature_and_caches(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("maisya.scout.time.monotonic", lambda: clock[0])

    calls = []

    async def call_capability(name, **arguments):
        calls.append(dict(arguments))
        if arguments.get("model_name") == "gpt-6-luna" and "temperature" in arguments:
            return {
                "success": False,
                "error": "HTTP 400: Unsupported parameter: 'temperature' is not supported with this model.",
            }
        return {"success": True, "response": "luna answer", "tool_calls": []}

    scout = Scout(SimpleNamespace(call_capability=call_capability))
    profile = Profile(
        id="scout",
        model_name="gpt-6-luna",
        fallback_models=("backup-m",),
        temperature=0.7,
    )

    # First step: fails on temperature -> immediately retries same model without temperature
    res1 = await scout.step(profile, [{"role": "user", "content": "q1"}])
    assert res1["response"] == "luna answer"
    assert res1["used_model"] == "gpt-6-luna"
    assert len(calls) == 2
    assert calls[0]["model_name"] == "gpt-6-luna" and calls[0]["temperature"] == 0.7
    assert calls[1]["model_name"] == "gpt-6-luna" and "temperature" not in calls[1]
    assert "gpt-6-luna" in scout._no_temperature_models
    assert "gpt-6-luna" not in scout._model_cooldown

    # Second step within 3600s TTL: skips temperature on the first try
    calls.clear()
    clock[0] = 2000.0
    res2 = await scout.step(profile, [{"role": "user", "content": "q2"}])
    assert res2["used_model"] == "gpt-6-luna"
    assert len(calls) == 1
    assert "temperature" not in calls[0]

    # After 3600s TTL expires: sends temperature again, handles raised RuntimeError too
    calls.clear()
    clock[0] = 4601.0

    async def raise_temp_error(name, **arguments):
        calls.append(dict(arguments))
        if "temperature" in arguments:
            raise RuntimeError("400 Bad Request: temperature 不支持自定义值，only the default is supported")
        return {"success": True, "response": "ok after ttl", "tool_calls": []}

    scout.host = SimpleNamespace(call_capability=raise_temp_error)
    res3 = await scout.step(profile, [{"role": "user", "content": "q3"}])
    assert res3["response"] == "ok after ttl"
    assert len(calls) == 2
    assert calls[0]["temperature"] == 0.7
    assert "temperature" not in calls[1]


@pytest.mark.asyncio
async def test_scout_temperature_retry_failure_falls_back_without_temp_cooldown(monkeypatch):
    clock = [500.0]
    monkeypatch.setattr("maisya.scout.time.monotonic", lambda: clock[0])

    calls = []

    async def call_capability(name, **arguments):
        calls.append(dict(arguments))
        model = arguments.get("model_name")
        if model == "gpt-6-luna":
            if "temperature" in arguments:
                raise ValueError("HTTP 400: unknown parameter 'temperature'")
            return {"success": False, "error": "429 rate limit"}
        return {"success": True, "response": "backup ok", "tool_calls": []}

    scout = Scout(SimpleNamespace(call_capability=call_capability))
    profile = Profile(
        id="scout",
        model_name="gpt-6-luna",
        fallback_models=("backup-m",),
        temperature=0.5,
    )

    res = await scout.step(profile, [{"role": "user", "content": "fallback check"}])
    assert res["used_model"] == "backup-m"
    assert res["response"] == "backup ok"
    assert [(c["model_name"], "temperature" in c) for c in calls] == [
        ("gpt-6-luna", True),
        ("gpt-6-luna", False),
        ("backup-m", True),
    ]
    assert "gpt-6-luna" in scout._no_temperature_models
    assert "gpt-6-luna" not in scout._model_cooldown

    # Verify bounded size (_MODEL_COOLDOWN_MAX = 64) for _no_temperature_models
    for idx in range(80):
        scout._mark_no_temperature_model(f"m-{idx}", 500.0 + idx)
    assert len(scout._no_temperature_models) <= 64
