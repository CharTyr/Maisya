import asyncio
import base64
import json
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest
from test_foundation import event, loaded

from maisya.abilities import Profiles
from maisya.config import Settings
from maisya.sandbox import SandboxSettings
from maisya.services import validate_endpoint_targets
from maisya.trust import Message


def test_manifest_exposes_host_discovery_and_render_capabilities():
    manifest = json.loads(Path("_manifest.json").read_text())
    for cap in (
        "chat.get_all_streams",
        "llm.get_available_models",
        "component.list_loaded_plugins",
        "person.get_id",
        "person.get_id_by_name",
        "person.get_value",
        "render.html2png",
    ):
        assert cap in manifest["capabilities"]


def test_profile_knobs_and_delete(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    writer_dir = profiles.path / "host_designer"
    writer_dir.mkdir(parents=True)
    (writer_dir / "PROFILE.md").write_text("生成网页与图片，不需要容器。")
    (writer_dir / "profile.toml").write_text(
        'title = "轻量出图"\n'
        'environment = "readonly"\n'
        'access = "everyone"\n'
        'task = "replyer"\n'
        'model_name = "step-5-preview"\n'
        "max_tokens = 4096\n"
        "temperature = 0.25\n"
        "max_steps = 24\n"
        'web_provider = "plugin:custom.web"\n'
        'tools = ["web_search", "workspace_file", "html_render", "result_attach", "person_read", "recall_read"]\n'
    )
    loaded_profile = profiles.get("host_designer")
    assert loaded_profile.uses_workspace is True
    assert loaded_profile.model_name == "step-5-preview"
    assert loaded_profile.max_tokens == 4096
    assert loaded_profile.temperature == 0.25
    assert loaded_profile.max_steps == 24
    assert loaded_profile.web_provider == "plugin:custom.web"

    profiles.delete("host_designer")
    assert not writer_dir.exists()
    with pytest.raises(ValueError):
        profiles.delete("scout")


def test_admin_secrets_sandbox_and_custom_template_targets(tmp_path):
    profiles = Profiles(tmp_path / "profiles")
    ops_dir = profiles.path / "ops_worker"
    ops_dir.mkdir(parents=True)
    (ops_dir / "PROFILE.md").write_text("管理员私密运维工作流。")
    (ops_dir / "profile.toml").write_text(
        'title = "私密运维"\n'
        'environment = "sandbox"\n'
        'access = "admins"\n'
        'network = "restricted"\n'
        'secrets = ["api_token"]\n'
        'sandbox_targets = ["1.1.1.1:443"]\n'
        'tools = ["workspace_file", "sandbox_exec", "result_attach"]\n'
    )
    prof = profiles.get("ops_worker")
    assert prof.secrets == ("api_token",)
    assert prof.sandbox_targets == ("1.1.1.1:443",)

    with pytest.raises(ValueError):
        validate_endpoint_targets(["127.0.0.1:8080"])


@pytest.mark.asyncio
async def test_containerless_workspace_html_render_and_attach(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        prof_dir = plugin.profiles.path / "painter"
        prof_dir.mkdir(parents=True)
        (prof_dir / "PROFILE.md").write_text("整理网页卡片并渲染为图片交付。")
        (prof_dir / "profile.toml").write_text(
            'title = "画卡"\n'
            'environment = "readonly"\n'
            'tools = ["workspace_file", "html_render", "result_attach"]\n'
        )
        profile = plugin.profiles.get("painter")
        plugin.runtime.available(profile)

        png_bytes = b"\x89PNG\r\n\x1a\nfake-png-content"

        async def capability(name, **arguments):
            if name == "render.html2png":
                assert arguments.get("allow_network") is False
                assert "<h1>Hi</h1>" in arguments["html"]
                return {
                    "success": True,
                    "result": {
                        "image_base64": base64.b64encode(png_bytes).decode(),
                        "mime_type": "image/png",
                        "width": 800,
                        "height": 600,
                    },
                }
            raise AssertionError(name)

        plugin.ctx.call_capability = capability
        row = plugin.threads.create(
            profile,
            "画一张图",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
        )
        # Write html source via workspace_file without Docker/sandbox ready
        res_write = await plugin.runtime.call(
            "workspace_file",
            {"action": "write", "path": "project/card.html", "content": "<h1>Hi</h1>"},
            row,
        )
        assert res_write["path"] == "project/card.html"

        res_render = await plugin.runtime.call(
            "html_render",
            {"html_path": "project/card.html", "output_path": "out/card.png"},
            row,
        )
        assert res_render["path"] == "out/card.png"
        assert res_render["mime_type"] == "image/png"

        res_attach = await plugin.runtime.call(
            "result_attach", {"paths": ["out/card.png"]}, row
        )
        assert len(res_attach["attachments"]) == 1
        assert res_attach["attachments"][0]["name"] == "card.png"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_admin_sandbox_secrets_exec_and_writable_mcp_api(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        plugin.vault.set("api_token", "secret-token-xyz")
        await plugin.after_receive(message=event(user="Alice"))
        alice_msg = event(user="Alice")["message_id"]
        mallory = event(user="Mallory")
        mallory["message_id"] = "mallory-msg"
        await plugin.after_receive(message=mallory)

        # Register writable MCP, writable API, custom credential template
        cur = await plugin.manage_read("services", "{}")
        cfg = {
            "servers": {
                "notes_rw": {
                    "command": [sys.executable, "-c", "pass"],
                    "tools": ["add"],
                    "readonly": False,
                    "access": "admins",
                    "description": "可写 MCP 服务",
                }
            },
            "apis": {
                "custom_post": {
                    "api_name": "other.plugin.post_item",
                    "version": "1",
                    "readonly": False,
                    "access": "admins",
                    "description": "可写跨插件 API",
                }
            },
            "templates": {
                "custom_tpl": {
                    "description": "自定义脚本模板",
                    "access": "admins",
                    "argv": ["/workspace/run.py"],
                    "secrets": ["api_token"],
                    "targets": ["1.1.1.1:443"],
                }
            },
        }
        saved = await plugin.manage_write(
            "services",
            json.dumps({"configuration": cfg, "expected_version": cur["version"]}),
        )
        assert saved["success"], saved

        # Admin sandbox profile can hold secrets & call sandbox_exec
        pdir = plugin.profiles.path / "admin_dev"
        pdir.mkdir(parents=True)
        (pdir / "PROFILE.md").write_text("自由调试并在沙箱内使用凭据。")
        (pdir / "profile.toml").write_text(
            'environment = "sandbox"\n'
            'access = "admins"\n'
            'network = "restricted"\n'
            'secrets = ["api_token"]\n'
            'sandbox_targets = ["1.1.1.1:443"]\n'
            'tools = ["workspace_file", "sandbox_exec", "mcp_call", "plugin_api", "credential_call"]\n'
            'mcp = ["notes_rw"]\n'
            'apis = ["custom_post"]\n'
            'template = "custom_tpl"\n'
        )
        plugin.runtime.sandbox.ready = True
        captured = {}

        async def fake_execute(row_arg, work_arg, command, *, secrets=None, targets=(), **kwargs):
            captured["command"] = command
            captured["secret_keys"] = sorted((secrets or {}).keys())
            captured["secret_value"] = secrets["api_token"].read_text()
            captured["targets"] = tuple(targets)
            return {"exit_code": 0, "stdout": "ok", "stderr": ""}

        plugin.runtime.sandbox.volume = lambda _p: nullcontext()
        plugin.runtime.sandbox.execute = fake_execute

        profile = plugin.profiles.get("admin_dev")
        plugin.runtime.available(profile)
        row = plugin.threads.create(
            profile,
            "执行命令",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
            grant={"source_msg_id": alice_msg},
        )
        out = await plugin.runtime.call(
            "sandbox_exec", {"command": "echo ok"}, {**row, "_tool_call_id": "exec-1"}
        )
        assert out["exit_code"] == 0
        assert captured["secret_keys"] == ["api_token"]
        assert captured["secret_value"] == "secret-token-xyz"
        assert captured["targets"] == (("1.1.1.1", 443),)

        # Readonly profile with admins + writable MCP configured explicitly can call writable MCP
        ro_dir = plugin.profiles.path / "ro_mcp"
        ro_dir.mkdir(parents=True)
        (ro_dir / "PROFILE.md").write_text("只读档案调用授权可写 MCP。")
        (ro_dir / "profile.toml").write_text(
            'environment = "readonly"\n'
            'access = "admins"\n'
            'tools = ["mcp_call", "plugin_api"]\n'
            'mcp = ["notes_rw"]\n'
            'apis = ["custom_post"]\n'
        )
        ro_profile = plugin.profiles.get("ro_mcp")
        ro_row = plugin.threads.create(
            ro_profile,
            "调 MCP",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
            grant={"source_msg_id": alice_msg},
        )

        class FakeSession:
            tools = {"add": {"inputSchema": {"type": "object"}}}

            async def request(self, method, params):
                return {"content": [{"type": "text", "text": json.dumps({"ok": True, "tool": params["name"]})}]}

            async def close(self):
                pass

        async def fake_session(service, context, config):
            return FakeSession()

        plugin.runtime.services._session = fake_session
        mcp_out = await plugin.runtime.call(
            "mcp_call",
            {"service": "notes_rw", "tool": "add", "arguments": {"x": 1}},
            {**ro_row, "_tool_call_id": "mcp-1"},
        )
        assert json.loads(mcp_out["text"])["ok"] is True

        # Non-admin fails admin MCP / writable API
        mallory_row = {**ro_row, "grant": {"source_msg_id": "mallory-msg"}, "_tool_call_id": "mcp-2"}
        with pytest.raises(PermissionError):
            await plugin.runtime.call(
                "mcp_call",
                {"service": "notes_rw", "tool": "add", "arguments": {}},
                mallory_row,
            )
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_flexible_web_provider_and_arg_map_routing(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        # Configure custom web provider in settings & services.web
        cur = await plugin.manage_read("services", "{}")
        saved = await plugin.manage_write(
            "services",
            json.dumps(
                {
                    "configuration": {
                        "apis": {
                            "my_web": {
                                "api_name": "third.search.run",
                                "version": "1",
                                "readonly": True,
                                "description": "自定义搜索接口",
                            }
                        },
                        "web": {
                            "providers": {
                                "custom": {
                                    "kind": "plugin_api",
                                    "target": "third.search.run",
                                    "arg_map": {"query": "q", "limit": "n"},
                                    "defaults": {"lang": "zh-CN"},
                                }
                            }
                        },
                    },
                    "expected_version": cur["version"],
                }
            ),
        )
        assert saved["success"], saved

        plugin.settings = Settings.model_validate(
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": ["chat"],
                "web_provider": "plugin:third.search.run",
            }
        )
        calls = []

        async def capability(name, **arguments):
            calls.append((name, arguments))
            if name == "api.list":
                return [
                    {"name": "run", "plugin_id": "third.search", "version": "1", "description": "s"}
                ]
            if name == "api.call":
                return {
                    "items": [{"title": "Hit", "url": "https://example.com", "snippet": "ok"}]
                }
            raise AssertionError(name)

        plugin.ctx.call_capability = capability
        out = await plugin.runtime.web.search("AI agent", limit=3, provider="web:custom")
        assert out["provider"] == "third.search.run"
        assert out["items"][0]["title"] == "Hit"
        api_call_args = [args for cap, args in calls if cap == "api.call"][0]["args"]
        assert api_call_args == {"q": "AI agent", "n": 3, "lang": "zh-CN"}
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_scout_tools_person_read_recall_read_and_model_forwarding(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        calls = []

        async def capability(name, **arguments):
            calls.append((name, arguments))
            if name == "message.get_recent":
                return {
                    "success": True,
                    "messages": [
                        {
                            "session_id": "chat",
                            "message_id": "m-1",
                            "sender_name": "Alice",
                            "user_id": "u-1",
                            "timestamp": 1794459000,
                            "processed_plain_text": "群友发言",
                        }
                    ],
                }
            if name == "person.get_id":
                return {"success": True, "person_id": "p-alice"}
            if name == "person.get_value":
                return {"success": True, "value": "喜欢写自动化工作流"}
            if name == "llm.generate_with_tools":
                return {"success": True, "response": "done", "tool_calls": []}
            raise AssertionError(name)

        plugin.ctx.call_capability = capability
        row = plugin.threads.create(
            plugin.profiles.get("scout"),
            "调查群友与历史经验",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
        )
        chat_res = await plugin.runtime.call("chat_read", {"limit": 50, "hours": 48}, row)
        assert chat_res[0]["sender_name"] == "Alice"
        assert chat_res[0]["user_id"] == "u-1"

        person_res = await plugin.runtime.call(
            "person_read", {"user_id": "u-1", "field_name": "memory_points"}, row
        )
        assert person_res["person_id"] == "p-alice"
        assert "工作流" in str(person_res["value"])

        # Seed a journal memory & verify recall_read returns it
        plugin.memory.write(
            "journal",
            "之前总结了日报定时汇总工作流",
            chat_id="chat",
            expected_version=0,
            allow_custom=True,
        )
        recall_res = await plugin.runtime.call(
            "recall_read", {"query": "日报", "kind": "journal"}, row
        )
        assert len(recall_res["memories"]) >= 1

        # Verify custom model knobs forwarded to llm.generate_with_tools
        custom_dir = plugin.profiles.path / "custom_llm"
        custom_dir.mkdir(parents=True)
        (custom_dir / "PROFILE.md").write_text("自定义模型参数。")
        (custom_dir / "profile.toml").write_text(
            'environment = "readonly"\n'
            'task = "planner"\n'
            'model_name = "my-custom-model"\n'
            "temperature = 0.15\n"
            "max_tokens = 2048\n"
        )
        cprof = plugin.profiles.get("custom_llm")
        await plugin.runtime.step(cprof, [{"role": "user", "content": "hi"}], max_steps=12)
        llm_calls = [args for cap, args in calls if cap == "llm.generate_with_tools"]
        assert llm_calls[-1]["model_name"] == "my-custom-model"
        assert llm_calls[-1]["temperature"] == 0.15
        assert llm_calls[-1]["max_tokens"] == 2048
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_admin_custom_memory_skill_delete_and_scheduled_workflow_intent(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        # Non-admin direct write without allow_custom still blocks custom instructions
        with pytest.raises(ValueError):
            plugin.memory.write("skill", "本群每天 9 点抓 GitHub 热门并总结", target="daily_gh", expected_version=0)

        # Admin write via management allows full workflow skill/memory
        wrote = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "skill",
                    "target": "daily_gh",
                    "expected_version": 0,
                    "content": "# 每日 GitHub 简报\n每天早上用 web_search 查趋势并用 painter 画卡片。",
                }
            ),
        )
        assert wrote["success"], wrote
        assert "daily_gh" in plugin.memory.skills()

        deleted = await plugin.manage_write("delete_skill", json.dumps({"target": "daily_gh"}))
        assert deleted["success"], deleted
        assert "daily_gh" not in plugin.memory.skills()

        # Schedule a recurring workflow intent via manage_write -> triggers profile thread on due
        scheduled = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": "chat",
                    "platform": "qq",
                    "reason": "汇总今日热点",
                    "profile": "scout",
                    "prompt": "查一下今天 AI 新闻并总结",
                    "in_seconds": 60,
                    "every_seconds": 3600,
                }
            ),
        )
        assert scheduled["success"], scheduled
        intent_id = scheduled["intent"]["intent_id"]

        seen = []

        async def fake_step(profile, history, **_kwargs):
            seen.append(profile.id)
            return {"response": "今日总结完成", "tool_calls": []}

        plugin.runtime.step = fake_step
        plugin.intents.clock = lambda: scheduled["intent"]["due_at"] + 1
        sent = []

        async def capability(name, **arguments):
            if name in {"maisaka.proactive.trigger", "maisaka.reply.generate"}:
                sent.append((name, arguments))
                return {"success": True, "reply": "ok"}
            return {"success": True}

        plugin.ctx.call_capability = capability
        await plugin.pulse.tick()
        assert seen == ["scout"]
        assert sent and "今日总结完成" in str(sent[0][1])

        # Recurring intent automatically rescheduled itself for next run!
        remaining = plugin.intents.list("chat:chat")
        next_rows = [r for r in remaining if r["source"] == "workflow" and r["status"] == "pending"]
        assert len(next_rows) == 1
        assert next_rows[0]["payload"]["every_seconds"] == 3600
        assert next_rows[0]["intent_id"] != intent_id
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_management_discovery_and_console_direct_thread_launch(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        async def capability(name, **arguments):
            if name == "chat.get_all_streams":
                return {"success": True, "streams": [{"stream_id": "chat", "platform": "qq"}]}
            if name == "llm.get_available_models":
                return {"success": True, "models": {"planner": ["step-5-preview"]}}
            if name == "component.list_loaded_plugins":
                return {"success": True, "plugins": [{"id": "third.search", "enabled": True}]}
            if name == "api.list":
                return [{"plugin_id": "third.search", "name": "run", "version": "1"}]
            return {"success": True}

        plugin.ctx.call_capability = capability
        disc = await plugin.manage_read("discovery", "{}")
        assert disc["success"]
        assert disc["streams"][0]["stream_id"] == "chat"
        assert "planner" in disc["models"]
        assert disc["plugins"][0]["id"] == "third.search"
        assert disc["apis"][0]["name"] == "run"

        # Launch a new thread directly from console (`thread_id=""`) even for admin-only profile!
        admin_dir = plugin.profiles.path / "admin_only"
        admin_dir.mkdir(parents=True)
        (admin_dir / "PROFILE.md").write_text("控制台管理员直起线程。")
        (admin_dir / "profile.toml").write_text(
            'environment = "readonly"\naccess = "admins"\ntools = ["recall_read"]\n'
        )

        started = asyncio.Event()

        async def fake_step(profile, history, **_kwargs):
            started.set()
            return {"response": "控制台工作流已完成", "tool_calls": []}

        plugin.runtime.step = fake_step
        res = await plugin.console_send(
            "",
            "在控制台直接运行 admin_only",
            profile="admin_only",
            chat_id="chat",
        )
        assert res["success"], res
        await asyncio.wait_for(started.wait(), timeout=2)
        await plugin.engine.drain()
        row = plugin.threads.get(res["thread_id"], "chat")
        assert row["state"] == "idle"
        assert row["result"]["text"] == "控制台工作流已完成"
    finally:
        await plugin.on_unload()
