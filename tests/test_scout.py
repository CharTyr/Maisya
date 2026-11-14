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
