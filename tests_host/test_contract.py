"""真实宿主隔离契约；缺宿主或依赖直接报错。"""

import asyncio
import importlib.metadata
import json
import logging
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


def no_network(*args, **kwargs):
    raise AssertionError("契约测试不允许网络调用")


socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.socket.bind = no_network

from maibot_sdk.context import PluginContext
from src.core.local_operator import has_plugin_management_permission
from src.llm_models.payload_content.context_protocol import ContextProtocolMode
from src.llm_models.payload_content.tool_option import normalize_tool_options
from src.plugin_runtime.capabilities.components import RuntimeComponentCapabilityMixin
from src.plugin_runtime.hook_payloads import (
    deserialize_prompt_items,
    deserialize_session_message,
    serialize_session_message,
)
from src.plugin_runtime.host.message_utils import PluginMessageUtils
from src.plugin_runtime.runner.manifest_validator import ManifestValidator
from src.plugin_runtime.runner.plugin_loader import PluginLoader
from src.plugin_runtime.runner.plugin_paths import build_plugin_paths
from src.plugin_runtime.webui_schema import load_webui_extension
from src.services.llm_service import _build_prompt_context_factory

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

from maisya.abilities import Profile
from maisya.scout import TOOL_DEFINITIONS, Scout
from maisya.threads import ThreadEngine, ThreadStore
from maisya.trust import Message, MessageStore, allowed, normalize_admins
from plugin import PLUGIN_ID, create_plugin


def event():
    return {
        "message_id": "contract-message-id-12345",
        "platform": "qq",
        "account_id": "contract-account",
        "session_id": "contract-chat",
        "timestamp": "1794459000.0",
        "processed_plain_text": "ordinary message",
        "raw_message": [{"type": "text", "data": "ordinary message"}],
        "message_info": {
            "user_info": {"user_id": "Alice", "user_nickname": "Alice"},
            "group_info": None,
            "additional_config": {},
        },
    }


class HostContract(unittest.TestCase):
    def test_self_activity_and_management_private_apis_with_real_sdk(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:

                async def rpc(method, plugin_id, payload, **kwargs):
                    if payload["capability"] == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    self.assertEqual(payload["capability"], "llm.generate_with_tools")
                    return {
                        "success": True,
                        "response": "我沿着海岸散步。",
                        "tool_calls": [],
                    }

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                try:
                    result = await plugin.agent_tool(
                        action="start",
                        owner="self",
                        workspace="coast",
                        message="去看海",
                        chat_id="contract-chat",
                        platform="qq",
                    )
                    self.assertTrue(result["success"], result)
                    self.assertEqual(result["owner"], "self")
                    continued = await plugin.agent_tool(
                        action="send",
                        thread_id=result["thread_id"],
                        message="继续走",
                        chat_id="contract-chat",
                        platform="qq",
                    )
                    self.assertEqual(continued["state"], "idle")
                    catalog = await plugin.manage_read("abilities")
                    self.assertEqual(
                        {p["id"] for p in catalog["profiles"]}, {"scout", "maker"}
                    )
                    cloned = await plugin.manage_write(
                        "clone", '{"base":"scout","target":"traveler"}'
                    )
                    self.assertTrue(cloned["success"], cloned)
                    self.assertIn(
                        "海岸", plugin.memory.context("contract-chat")["journal"]
                    )
                    current = await plugin.manage_read("settings")
                    self.assertNotIn("value", current)
                    metadata = {
                        component["name"]: component["metadata"]
                        for component in plugin.get_components()
                    }
                    self.assertFalse(metadata["maisya_manage_write"]["public"])
                    self.assertTrue(metadata["maisya_incoming_event"]["public"])
                finally:
                    await plugin.on_unload()

        asyncio.run(run())

    def test_shape_memory_versions_and_live_instructions_with_real_sdk(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                prompts = []

                async def rpc(method, plugin_id, payload, **kwargs):
                    name = payload["capability"]
                    if name == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    if name == "llm.generate_with_tools":
                        prompts.append(payload["args"]["prompt"])
                        return {
                            "success": True,
                            "response": "confirmed",
                            "tool_calls": [],
                        }
                    raise AssertionError(name)

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                try:
                    await plugin.after_receive(message=event())
                    options = {
                        "source_msg_id": event()["message_id"],
                        "chat_id": "contract-chat",
                        "platform": "qq",
                    }
                    denied = await plugin.shape_tool(
                        action="write",
                        kind="notes",
                        target="scout",
                        content="changed",
                        expected_version=0,
                        chat_id="contract-chat",
                        platform="qq",
                        user_id="Alice",
                    )
                    self.assertFalse(denied["success"])
                    saved = await plugin.shape_tool(
                        action="write",
                        kind="notes",
                        target="scout",
                        content="查证前先核实来源。",
                        expected_version=0,
                        **options,
                    )
                    self.assertTrue(saved["success"], saved)
                    await plugin.agent_tool(
                        action="start",
                        message="verify",
                        chat_id="contract-chat",
                        platform="qq",
                    )
                    self.assertIn("查证前先核实来源", str(prompts[-1]))
                    restored = await plugin.shape_tool(
                        action="rollback",
                        kind="notes",
                        target="scout",
                        version=0,
                        expected_version=saved["version"],
                        **options,
                    )
                    self.assertTrue(restored["success"], restored)
                    self.assertEqual(restored["content"], "")
                    components = {
                        item["name"]: item for item in plugin.get_components()
                    }
                    self.assertNotIn(
                        "visibility", components["maisya_shape"]["metadata"]
                    )
                finally:
                    await plugin.on_unload()

        asyncio.run(run())

    def test_maker_attachments_roundtrip_through_real_host_file_codec(self):
        import base64

        from maisya.share import Share
        from maisya.workspaces import Workspaces

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ThreadStore(root / "state.db")
            row = store.create(
                Profile(environment="sandbox"),
                "make file",
                chat_id="contract-chat",
                platform="qq",
                user_id="",
            )
            files = Workspaces(root / "workspaces", store)
            with files.activity(row) as work:
                work.write("out/result.txt", "forty two")
                attachment = files.attach(row, work, ["out/result.txt"])[0]
            sharing = Share(store, None, None, lambda *args: True, files=files)
            media, references = asyncio.run(
                sharing._segments("contract-chat", [attachment["handle"]], [])
            )
            self.assertEqual(references, [])
            sequence = PluginMessageUtils._message_sequence_from_dict(media)
            rebuilt = PluginMessageUtils._message_sequence_to_dict(sequence)
            self.assertEqual(rebuilt[0]["type"], "file")
            self.assertEqual(rebuilt[0]["data"]["name"], "result.txt")
            self.assertEqual(
                rebuilt[0]["data"]["base64"], base64.b64encode(b"forty two").decode()
            )

    def test_manifest(self):
        validator = ManifestValidator()
        manifest = validator.load_from_plugin_path(PLUGIN_DIR)
        self.assertIsNotNone(manifest, validator.errors)
        self.assertEqual(validator.errors, [])
        self.assertEqual(validator.warnings, [])

    def test_webui(self):
        extension = load_webui_extension(str(PLUGIN_DIR))
        self.assertEqual(extension.schema_version, 2)
        self.assertEqual(len(extension.pages), 4)
        self.assertEqual(
            {page.id for page in extension.pages},
            {"now", "abilities", "memory", "settings"},
        )
        for workspace in extension.pages[1:]:
            self.assertEqual(workspace.queries["read"].api, "maisya_manage_read")
            self.assertEqual(workspace.actions["write"].api, "maisya_manage_write")
            workspace.actions["write"].validate_args(
                {"action": "asset", "payload": "{}"}
            )
        self.assertEqual(extension.pages[0].mode, "custom")
        self.assertEqual(extension.pages[0].placement, "workspace")
        self.assertIn("Maisya", extension.pages[0].document)
        page = extension.pages[0]
        self.assertEqual(page.queries["snapshot"].api, "maisya_now")
        self.assertEqual(page.actions["note"].api, "maisya_note")
        page.queries["snapshot"].validate_args({"thread_id": "", "cursor": 0})
        page.actions["note"].validate_args(
            {"thread_id": "thread", "message": "补充方向"}
        )
        page.actions["note"].validate_args(
            {"thread_id": "thread", "message": "补充方向", "steer": True}
        )
        with self.assertRaises(ValueError):
            page.actions["stop"].validate_args(
                {"thread_id": "thread", "unexpected": True}
            )

    def test_component_and_lifecycle_contract(self):
        plugin = create_plugin()
        PluginLoader._validate_sdk_plugin_contract(PLUGIN_ID, plugin)
        components = {item["name"]: item for item in plugin.get_components()}
        self.assertEqual(
            set(components),
            {
                "maisya_receive",
                "maisya_redact",
                "vault",
                "maisya_agent",
                "maisya_recall",
                "maisya_later",
                "maisya_shape",
                "maisya_planner",
                "maisya_planner_observe",
                "maisya_replyer",
                "maisya_now",
                "maisya_event",
                "maisya_note",
                "maisya_stop",
                "maisya_manage_read",
                "maisya_manage_write",
                "maisya_incoming_event",
            },
        )
        self.assertEqual(
            components["vault"]["metadata"]["command_pattern"], r"^/vault(?:\s|$)"
        )
        self.assertEqual(
            components["maisya_redact"]["metadata"]["error_policy"], "abort"
        )
        for name in ("maisya_agent", "maisya_recall"):
            self.assertEqual(components[name]["metadata"]["chat_scope"], "all")
            self.assertEqual(components[name]["metadata"]["visibility"], "visible")
        self.assertEqual(components["maisya_later"]["metadata"]["chat_scope"], "all")
        self.assertNotIn("visibility", components["maisya_later"]["metadata"])
        self.assertNotIn("visibility", components["maisya_shape"]["metadata"])
        self.assertEqual(components["maisya_shape"]["metadata"]["chat_scope"], "all")
        for name in (
            "maisya_now",
            "maisya_event",
            "maisya_note",
            "maisya_stop",
            "maisya_manage_read",
            "maisya_manage_write",
        ):
            self.assertFalse(components[name]["metadata"]["public"])
        private = SimpleNamespace(plugin_id=PLUGIN_ID, public=False)
        self.assertTrue(
            RuntimeComponentCapabilityMixin._is_api_visible_to_plugin(
                private, PLUGIN_ID
            )
        )
        self.assertFalse(
            RuntimeComponentCapabilityMixin._is_api_visible_to_plugin(
                private, "another.plugin"
            )
        )

    def test_share_native_sdk_segments_and_outbound_identity(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                sent = []
                png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nmccAAAAASUVORK5CYII="

                async def rpc(method, plugin_id, payload, **kwargs):
                    name, args = payload["capability"], payload["args"]
                    if name == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    if name == "llm.generate_with_tools":
                        return {
                            "success": True,
                            "response": "来源 https://example.org/info?x=1&y=2",
                            "tool_calls": [],
                        }
                    if name == "maisaka.reply.generate":
                        return {"success": True, "text": "查到了，给你这个。"}
                    if name == "message.get_by_id":
                        return {
                            "success": True,
                            "message": {
                                **event(),
                                "raw_message": [
                                    {"type": "image", "binary_data_base64": png}
                                ],
                            },
                        }
                    self.assertEqual(name, "send.hybrid")
                    sequence = PluginMessageUtils._message_sequence_from_dict(
                        args["segments"]
                    )
                    rebuilt = PluginMessageUtils._message_sequence_to_dict(sequence)
                    self.assertEqual(
                        [part["type"] for part in rebuilt], ["reply", "text", "image"]
                    )
                    self.assertEqual(args["stream_id"], "contract-chat")
                    self.assertTrue(args["sync_to_maisaka_history"])
                    self.assertEqual(args["maisaka_source_kind"], "guided_reply")
                    self.assertIn(
                        "https://example.org/info?x=1&y=2", rebuilt[1]["data"]
                    )
                    sent.append(args)
                    return {
                        "success": True,
                        "sent": True,
                        "message_id": "contract-outbound-id",
                    }

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                result = await plugin.agent_tool(
                    action="start",
                    message="Check",
                    chat_id="contract-chat",
                    platform="qq",
                )
                delivered = await plugin.agent_tool(
                    action="share",
                    thread_id=result["thread_id"],
                    chat_id="contract-chat",
                    platform="qq",
                    attachment_handles=["message:contract-message-id-12345:0"],
                    ref_msg_ids=["contract-message-id-12345"],
                )
                self.assertTrue(delivered["sent"])
                self.assertEqual(delivered["message_id"], "contract-outbound-id")
                again = await plugin.agent_tool(
                    action="share",
                    thread_id=result["thread_id"],
                    chat_id="contract-chat",
                    platform="qq",
                )
                self.assertTrue(again["already_handled"])
                self.assertEqual(len(sent), 1)
                await plugin.on_unload()

        asyncio.run(run())

    def test_console_snapshot_and_operator_actions_use_private_apis(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:

                async def rpc(method, plugin_id, payload, **kwargs):
                    if payload["capability"] == "config.get":
                        return {"success": True, "value": []}
                    self.assertEqual(payload["capability"], "llm.generate_with_tools")
                    return {"success": True, "response": "Confirmed", "tool_calls": []}

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                result = await plugin.agent_tool(
                    action="start",
                    message="Check",
                    chat_id="contract-chat",
                    platform="qq",
                )
                view = await plugin.console_query(thread_id=result["thread_id"])
                self.assertTrue(view["success"])
                self.assertIn("Confirmed", view["selected"]["result"]["text"])
                self.assertTrue(
                    (
                        await plugin.console_send(
                            thread_id=result["thread_id"], message="Follow up"
                        )
                    )["success"]
                )
                await plugin.engine.drain()
                self.assertEqual(
                    (await plugin.console_stop(thread_id=result["thread_id"]))["state"],
                    "closed",
                )
                await plugin.on_unload()

        asyncio.run(run())

    def test_real_payload_and_host_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MessageStore(Path(directory) / "messages.db")
            for platform in ("qq", "bot_console"):
                for relayed in (False, True):
                    raw = event()
                    raw["platform"] = platform
                    raw["message_id"] += str(relayed)
                    raw["message_info"]["additional_config"] = {
                        "is_local_operator": True,
                        "relayed_by_plugin": "contract.plugin" if relayed else "",
                    }
                    decoded = deserialize_session_message(raw)
                    actual = serialize_session_message(decoded)
                    identity = Message.from_event(actual)
                    store.record(identity)
                    expected = has_plugin_management_permission(
                        platform,
                        "Alice",
                        ["QQ:Alice"],
                        local_operator=platform == "bot_console",
                        relayed_by_plugin=relayed,
                    )
                    self.assertEqual(
                        allowed(
                            store,
                            raw["message_id"],
                            raw["session_id"],
                            platform,
                            normalize_admins(["QQ:Alice"]),
                        ),
                        expected,
                    )

    def test_lifecycle_external_data_survives_plugin_replacement(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                host = Path(directory)
                paths = build_plugin_paths(PLUGIN_ID, host)

                async def get(key, default=None):
                    return ["qq:Alice"] if key == "plugin.permission" else default

                def instance():
                    plugin = create_plugin()
                    plugin._set_context(
                        SimpleNamespace(
                            config=SimpleNamespace(get=get),
                            paths=paths,
                            logger=logging.getLogger("contract.maisya"),
                        )
                    )
                    plugin.set_plugin_config(
                        {
                            "plugin": {"enabled": True, "config_version": "0.1.0"},
                            "enabled_chats": ["contract-chat"],
                        }
                    )
                    return plugin

                plugin = instance()
                await plugin.on_load()
                secret = "synthetic-contract-value-12345"
                raw = event()
                raw["processed_plain_text"] = "/vault set contract_key " + secret
                raw["raw_message"][0]["data"] = raw["processed_plain_text"]
                result = await plugin.after_receive(
                    message=serialize_session_message(deserialize_session_message(raw))
                )
                clean = serialize_session_message(
                    deserialize_session_message(result["modified_kwargs"]["message"])
                )
                self.assertNotIn(secret, str(clean))
                self.assertEqual(clean["processed_plain_text"], "/vault")
                self.assertEqual((await plugin.vault_command(message=clean))[2], 2)
                data_root = plugin.data_root
                self.assertEqual(data_root, host / "data/maisya")
                await plugin.on_unload()
                installed = host / "plugins/Maisya"
                installed.mkdir(parents=True)
                (installed / "plugin.py").write_text("old plugin")
                shutil.rmtree(installed)
                installed.mkdir()
                fresh = instance()
                await fresh.on_load()
                self.assertEqual(fresh.vault.handles(), ["contract_key"])
                outgoing = event()
                outgoing["processed_plain_text"] = secret
                outgoing["raw_message"][0]["data"] = secret
                result = await fresh.before_send(message=outgoing)
                clean = deserialize_session_message(
                    result["modified_kwargs"]["message"]
                )
                self.assertNotIn(secret, str(serialize_session_message(clean)))
                await fresh.on_unload()

        asyncio.run(run())

    def test_scout_tool_schema_and_model_tool_roundtrip(self):
        options = normalize_tool_options(list(TOOL_DEFINITIONS.values()))
        self.assertEqual(len(options), len(TOOL_DEFINITIONS))
        calls = ThreadEngine._calls(
            [
                {
                    "id": "test_call",
                    "function": {
                        "name": "web_search",
                        "arguments": {"query": "Kamakura"},
                    },
                }
            ]
        )
        prompt = [
            {"role": "system", "content": "Read-only exploration"},
            {"role": "user", "content": "Find a source"},
            {"role": "assistant", "content": "", "tool_calls": calls},
            {"role": "tool", "tool_call_id": calls[0]["id"], "content": "A source"},
            {"role": "assistant", "content": "A sourced answer"},
        ]
        items = _build_prompt_context_factory(prompt)(None)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[2].tool_call.func_name, "web_search")

    def test_real_sdk_native_readonly_rpc(self):
        async def run():
            received = []

            async def rpc(method, plugin_id, payload, **kwargs):
                received.append(payload)
                name = payload["capability"]
                if name == "message.get_recent":
                    return {"success": True, "messages": [event()]}
                if name == "knowledge.search":
                    return {"success": True, "content": "Only this chat"}
                if name == "llm.generate_with_tools":
                    return {
                        "success": True,
                        "response": "Local contract answer",
                        "tool_calls": [],
                    }
                raise AssertionError("unexpected capability")

            scout = Scout(PluginContext(PLUGIN_ID, rpc_call=rpc))
            context = {"chat_id": "contract-chat", "owner": "chat"}
            self.assertIn(
                "ordinary message", str(await scout.call("chat_read", {}, context))
            )
            self.assertEqual(
                await scout.call("memory_read", {"query": "a topic"}, context),
                "Only this chat",
            )
            self.assertEqual(received[-1]["args"]["chat_id"], "contract-chat")
            result = await scout.step(
                Profile(), [{"role": "user", "content": "Explore"}]
            )
            self.assertEqual(result["response"], "Local contract answer")

        asyncio.run(run())

    def test_sync_return_and_retry_with_real_sdk(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                received = []

                async def rpc(method, plugin_id, payload, **kwargs):
                    received.append(payload)
                    if payload["capability"] == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    self.assertEqual(payload["capability"], "llm.generate_with_tools")
                    return {
                        "success": True,
                        "response": "Verified opening time is 10:00",
                        "tool_calls": [],
                    }

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                await plugin.after_receive(message=event())
                result = await plugin.agent_tool(
                    action="start",
                    message="Check opening time",
                    source_msg_id=event()["message_id"],
                    chat_id="contract-chat",
                    platform="qq",
                )
                self.assertTrue(result["success"])
                await plugin.after_planner(
                    session_id="contract-chat",
                    output_items=[
                        {"tool_call": {"func_name": "reply", "args": {}}},
                        {"tool_call": {"func_name": "stop", "args": {}}},
                    ],
                )
                first = await plugin.before_replyer(
                    session_id="contract-chat",
                    request_type="maisaka.replyer",
                    extra_prompt="Use the original tone",
                    reply_tool_args={"reply_style": "简短"},
                    attempt=1,
                )
                extra = first["modified_kwargs"]["extra_prompt"]
                self.assertIn("10:00", extra)
                self.assertIn("Use the original tone", extra)
                self.assertEqual(
                    first["modified_kwargs"]["reply_tool_args"]["reply_style"], "简短"
                )
                retry = await plugin.before_replyer(
                    session_id="contract-chat",
                    request_type="maisaka.replyer",
                    reply_tool_args=first["modified_kwargs"]["reply_tool_args"],
                    attempt=2,
                )
                self.assertIn("10:00", retry["modified_kwargs"]["extra_prompt"])
                unrelated = await plugin.before_replyer(
                    session_id="contract-chat",
                    request_type="maisaka.replyer",
                    attempt=1,
                )
                self.assertNotIn("modified_kwargs", unrelated)
                self.assertFalse(
                    any(item["capability"].startswith("send.") for item in received)
                )
                await plugin.on_unload()

        asyncio.run(run())

    def test_background_group_uses_native_proactive_rpc_once(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                received = []
                gate = asyncio.Event()

                async def rpc(method, plugin_id, payload, **kwargs):
                    name = payload["capability"]
                    if name == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    if name == "llm.generate_with_tools":
                        await gate.wait()
                        return {
                            "success": True,
                            "response": "Confirmed fact",
                            "tool_calls": [],
                        }
                    self.assertEqual(name, "maisaka.proactive.trigger")
                    received.append(payload)
                    return {"success": True, "queued": True, "task_id": "contract-task"}

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                await plugin.after_receive(message=event())
                call = {
                    "item_type": "FunctionCallItem",
                    "tool_call": {
                        "func_name": "maisya_agent",
                        "args": {
                            "action": "start",
                            "source_msg_id": event()["message_id"],
                            "wait_seconds": 0,
                        },
                    },
                }
                await plugin.after_planner(
                    session_id="contract-chat", output_items=[call, call]
                )
                ids = []
                for _ in range(2):
                    result = await plugin.agent_tool(
                        action="start",
                        message="Explore",
                        wait_seconds=0,
                        source_msg_id=event()["message_id"],
                        chat_id="contract-chat",
                        platform="qq",
                    )
                    self.assertTrue(result["success"])
                    ids.append(result["thread_id"])
                gate.set()
                await plugin.engine.drain()
                moment = plugin.bridge.clock()
                plugin.bridge.clock = lambda: moment + 2
                await plugin.pulse.tick()
                await plugin.pulse.tick()
                self.assertEqual(len(received), 1)
                args = received[0]["args"]
                self.assertEqual(args["stream_id"], "contract-chat")
                self.assertEqual(args["metadata"]["kind"], "return")
                self.assertTrue(all(thread_id in args["intent"] for thread_id in ids))
                await plugin.on_unload()

        asyncio.run(run())

    def test_planner_append_is_accepted_by_host(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                paths = build_plugin_paths(PLUGIN_ID, Path(directory))

                async def rpc(method, plugin_id, payload):
                    return {"success": True, "value": ["qq:Alice"]}

                plugin = create_plugin()
                plugin._set_context(PluginContext(PLUGIN_ID, rpc_call=rpc, paths=paths))
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                await plugin.after_receive(message=event())
                tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                    for name in (
                        "reply",
                        "query_memory",
                        "third_party_tool",
                        "subagent",
                        "subagent_status",
                        "subagent_stop",
                        "maisya_agent",
                        "maisya_recall",
                    )
                ]
                result = await plugin.before_planner(
                    session_id="contract-chat", tool_definitions=tools, items=[]
                )
                modified = result["modified_kwargs"]
                rebuilt = deserialize_prompt_items(
                    modified["items"],
                    item_schema_version=1,
                    mode=ContextProtocolMode.REQUEST_CONTEXT,
                )
                self.assertEqual(len(rebuilt), 1)
                self.assertEqual(len(modified["tool_definitions"]), 5)
                await plugin.on_unload()

        asyncio.run(run())

    def test_agent_thread_native_rpc_roundtrip(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                requests = []

                async def rpc(method, plugin_id, payload):
                    name, arguments = payload["capability"], payload["args"]
                    requests.append(name)
                    if name == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    if name == "web.search":
                        return {
                            "success": True,
                            "result": {
                                "query": "Kamakura",
                                "results": [
                                    {
                                        "title": "Contract source",
                                        "url": "https://example.org/kamakura",
                                        "snippet": "A coastline",
                                    }
                                ],
                            },
                        }
                    if name == "llm.generate_with_tools":
                        if arguments["prompt"][-1]["role"] == "user":
                            return {
                                "success": True,
                                "response": "",
                                "tool_calls": [
                                    {
                                        "id": "contract_search",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": {"query": "Kamakura"},
                                        },
                                    }
                                ],
                            }
                        context = _build_prompt_context_factory(arguments["prompt"])(
                            None
                        )
                        self.assertGreaterEqual(len(context), 4)
                        self.assertIn(
                            "example.org/kamakura", arguments["prompt"][-1]["content"]
                        )
                        return {
                            "success": True,
                            "response": "Found a sourced coastline description.",
                            "tool_calls": [],
                        }
                    raise AssertionError("unexpected capability")

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                await plugin.after_receive(message=event())
                result = await plugin.agent_tool(
                    action="start",
                    message="Explore Kamakura",
                    source_msg_id=event()["message_id"],
                    chat_id="contract-chat",
                    platform="qq",
                )
                self.assertTrue(result["success"])
                self.assertEqual(result["state"], "idle")
                self.assertIn("sourced", result["result"]["text"])
                recalled = await plugin.recall_tool(
                    query="Kamakura", chat_id="contract-chat", platform="qq"
                )
                self.assertEqual(
                    recalled["threads"][0]["thread_id"], result["thread_id"]
                )
                self.assertEqual(requests.count("web.search"), 1)
                await plugin.on_unload()

        asyncio.run(run())

    def test_autonomous_tool_schema_and_admin_boundary(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:

                async def rpc(method, plugin_id, payload):
                    if payload["capability"] == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    if payload["capability"] == "llm.generate_with_tools":
                        return {
                            "success": True,
                            "response": "An independent observation",
                            "tool_calls": [],
                        }
                    raise AssertionError("unexpected capability")

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                component = next(
                    item
                    for item in plugin.get_components()
                    if item["name"] == "maisya_agent"
                )
                self.assertNotIn(
                    "source_msg_id", component["metadata"]["parameters_raw"]["required"]
                )
                result = await plugin.agent_tool(
                    action="start",
                    message="Explore independently",
                    chat_id="contract-chat",
                    platform="qq",
                )
                self.assertTrue(result["success"])
                self.assertEqual(
                    plugin.threads.get(result["thread_id"], "contract-chat")["user_id"],
                    "",
                )
                (plugin.profiles.path / "scout/profile.toml").write_text(
                    'environment="readonly"\naccess="admins"'
                )
                denied = await plugin.agent_tool(
                    action="start",
                    message="Restricted",
                    chat_id="contract-chat",
                    platform="qq",
                    user_id="Alice",
                )
                self.assertFalse(denied["success"])
                await plugin.on_unload()

        asyncio.run(run())

    def test_redirected_batch_keeps_host_tool_protocol_valid(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                entered, released = asyncio.Event(), asyncio.Event()
                executed = []

                class Runtime:
                    async def step(self, profile, history):
                        if not executed:
                            return {
                                "success": True,
                                "response": "",
                                "tool_calls": [
                                    {
                                        "id": name,
                                        "function": {
                                            "name": "web_search",
                                            "arguments": {"query": name},
                                        },
                                    }
                                    for name in ("first", "old-second")
                                ],
                            }
                        _build_prompt_context_factory(history)(None)
                        return {
                            "success": True,
                            "response": "Following the revised direction",
                            "tool_calls": [],
                        }

                    async def call(self, name, arguments, context):
                        executed.append(arguments["query"])
                        entered.set()
                        await released.wait()
                        return {"text": "An observed fact"}

                store = ThreadStore(Path(directory) / "threads.db")
                engine = ThreadEngine(store, Runtime())
                result = await engine.start(
                    Profile(),
                    "Old direction",
                    chat_id="contract-chat",
                    platform="qq",
                    user_id="",
                    wait_seconds=0,
                )
                await entered.wait()
                await engine.send(
                    result["thread_id"],
                    "Revised direction",
                    chat_id="contract-chat",
                    wait_seconds=0,
                )
                released.set()
                await engine.drain()
                self.assertEqual(executed, ["first"])
                history = store.get(result["thread_id"], "contract-chat")["history"]
                self.assertIn("skipped", str(history))
                _build_prompt_context_factory(history)(None)
                await engine.close()

        asyncio.run(run())

    def test_bounded_context_preserves_recoverable_source_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ThreadStore(Path(directory) / "threads.db")
            row = store.create(
                Profile(),
                "A journey",
                chat_id="contract-chat",
                platform="qq",
                user_id="",
            )
            revision = store.enqueue(row["thread_id"], "A journey", "contract-chat")
            calls = ThreadEngine._calls(
                [
                    {
                        "id": "source",
                        "function": {
                            "name": "web_fetch",
                            "arguments": {"url": "https://example.org/weather"},
                        },
                    }
                ]
            )
            history = [
                {"role": "user", "content": "A journey"},
                {"role": "assistant", "content": "", "tool_calls": calls},
                {
                    "role": "tool",
                    "tool_call_id": calls[0]["id"],
                    "content": "寺院因台风安全管制关闭 https://example.org/weather "
                    + "weather " * 2000,
                },
                {"role": "user", "content": "Continue the same journey"},
                {"role": "assistant", "content": "Next stop"},
            ]
            store.finish(
                row["thread_id"],
                "contract-chat",
                {"text": "Next stop"},
                history,
                revision,
            )
            store.compact(row["thread_id"], "contract-chat", max_chars=2000)
            retained = store.get(row["thread_id"], "contract-chat")["history"]
            _build_prompt_context_factory(retained)(None)
            self.assertLess(len(str(retained)), 2000)
            evidence = store.read_history(
                row["thread_id"], "contract-chat", query="台风"
            )
            self.assertIn("安全管制", str(evidence))
            self.assertIn("example.org/weather", str(evidence))
            with self.assertRaises(PermissionError):
                store.read_history(row["thread_id"], "another-chat", query="台风")

    def test_cold_session_reopen_via_chat_open_session(self):
        validator = ManifestValidator()
        manifest = validator.load_from_plugin_path(PLUGIN_DIR)
        self.assertIsNotNone(manifest, validator.errors)
        self.assertIn("chat.open_session", manifest.capabilities)

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                calls = []
                attempts = 0

                async def rpc(method, plugin_id, payload, **kwargs):
                    nonlocal attempts
                    name, args = payload["capability"], payload["args"]
                    calls.append((name, args, kwargs.get("timeout_ms")))
                    if name == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    if name == "maisaka.proactive.trigger":
                        attempts += 1
                        if attempts == 1:
                            return {
                                "success": False,
                                "error": "未找到已存在的聊天流: contract-chat",
                            }
                        return {"success": True, "queued": True}
                    if name == "chat.open_session":
                        return {
                            "success": True,
                            "created": True,
                            "stream_id": "contract-chat",
                        }
                    raise AssertionError(name)

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                try:
                    group_event = {
                        **event(),
                        "message_info": {
                            "user_info": {"user_id": "Alice", "user_nickname": "Alice"},
                            "group_info": {"group_id": "contract-group"},
                            "additional_config": {},
                        },
                    }
                    await plugin.after_receive(message=group_event)
                    with plugin.threads.connect() as db, db:
                        db.execute("DELETE FROM trusted_sources")
                    self.assertTrue(plugin.intents.known_chat("contract-chat"))
                    row = plugin.intents.schedule(
                        kind="proactive",
                        source="longing",
                        target="chat:contract-chat",
                        due_at=plugin.intents.clock() - 1,
                        reason="重启后确认群会话可以自动唤醒",
                        payload={"platform": "qq"},
                        bypass_limits=True,
                    )
                    await plugin.pulse._intents()
                    cap_names = [name for name, _, _ in calls if name != "config.get"]
                    self.assertEqual(
                        cap_names,
                        [
                            "maisaka.proactive.trigger",
                            "chat.open_session",
                            "maisaka.proactive.trigger",
                        ],
                    )
                    open_args = calls[2][1]
                    self.assertEqual(open_args["platform"], "qq")
                    self.assertEqual(open_args["chat_type"], "group")
                    self.assertEqual(open_args["group_id"], "contract-group")
                    saved = plugin.intents.get(row["intent_id"])
                    self.assertEqual(saved["status"], "done")
                    self.assertEqual(saved["wake_status"], "accepted")
                finally:
                    await plugin.on_unload()

        asyncio.run(run())

    def test_third_party_web_search_fallback_and_config_get_plugin_with_real_sdk(self):
        validator = ManifestValidator()
        manifest = validator.load_from_plugin_path(PLUGIN_DIR)
        self.assertIsNotNone(manifest, validator.errors)
        self.assertIn("config.get_plugin", manifest.capabilities)

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                exa_secret = "contract-exa-secret-key"
                step = 0
                cap_names = []
                third_party_available = True

                async def rpc(method, plugin_id, payload, **kwargs):
                    nonlocal step
                    name, args = payload["capability"], payload["args"]
                    cap_names.append(name)
                    if name == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    if name == "web.search":
                        return {"success": False, "error": "统一联网功能未启用"}
                    if name == "config.get_plugin":
                        self.assertEqual(
                            args["plugin_name"], "xxxxx7258.google-search-plugin"
                        )
                        if not third_party_available:
                            return {
                                "success": False,
                                "value": None,
                                "error": "未找到插件配置",
                            }
                        return {
                            "success": True,
                            "value": {
                                "plugin": {"enabled": True},
                                "search_backend": {"default_engine": "exa"},
                                "engines": {
                                    "exa_enabled": True,
                                    "exa_api_keys": [exa_secret],
                                },
                            },
                        }
                    if name == "llm.generate_with_tools":
                        step += 1
                        if step == 1:
                            return {
                                "success": True,
                                "response": "",
                                "tool_calls": [
                                    {
                                        "id": "call_web_1",
                                        "function": {
                                            "name": "web_search",
                                            "arguments": {"query": "Maisya"},
                                        },
                                    }
                                ],
                            }
                        self.assertNotIn(exa_secret, str(args["prompt"]))
                        self.assertIn("exa.contract/doc", str(args["prompt"][-1]))
                        return {
                            "success": True,
                            "response": "Confirmed via https://exa.contract/doc",
                            "tool_calls": [],
                        }
                    raise AssertionError(name)

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                try:
                    plugin.runtime.web._http_request = lambda m, u, **kw: (
                        200,
                        {"content-type": "application/json"},
                        json.dumps(
                            {
                                "results": [
                                    {
                                        "title": f"Doc {exa_secret}",
                                        "url": "https://exa.contract/doc",
                                        "highlights": ["contract ok"],
                                    }
                                ]
                            }
                        ).encode(),
                        u,
                    )
                    res = await plugin.agent_tool(
                        action="start",
                        message="Search Maisya",
                        chat_id="contract-chat",
                        platform="qq",
                    )
                    self.assertTrue(res["success"], res)
                    self.assertIn("https://exa.contract/doc", res["result"]["text"])
                    self.assertIn("web.search", cap_names)
                    self.assertIn("config.get_plugin", cap_names)

                    # When config.get_plugin returns {"success": False, "value": None},
                    # real PluginContext unwraps it to None and WebRouter fails cleanly.
                    third_party_available = False
                    with self.assertRaises(RuntimeError):
                        await plugin.runtime.web.search(
                            "Maisya", 3, {"chat_id": "contract-chat", "owner": "chat"}
                        )
                finally:
                    await plugin.on_unload()

        asyncio.run(run())

    def test_workflow_freedom_host_capabilities_and_direct_console_launch(self):
        async def run():
            import base64

            manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text())
            for cap in (
                "chat.get_all_streams",
                "llm.get_available_models",
                "component.list_loaded_plugins",
                "person.get_id",
                "person.get_id_by_name",
                "person.get_value",
                "render.html2png",
            ):
                self.assertIn(cap, manifest["capabilities"])

            with tempfile.TemporaryDirectory() as directory:
                step = 0
                render_calls = []

                async def rpc(method, plugin_id, payload, **kwargs):
                    nonlocal step
                    name, args = payload["capability"], payload["args"]
                    if name == "config.get":
                        return {"success": True, "value": ["qq:Alice"]}
                    if name == "chat.get_all_streams":
                        return {
                            "success": True,
                            "streams": [{"stream_id": "contract-chat", "platform": "qq"}],
                        }
                    if name == "llm.get_available_models":
                        return {"success": True, "models": {"planner": ["step-5-preview"]}}
                    if name == "component.list_loaded_plugins":
                        return {
                            "success": True,
                            "plugins": [{"id": "third.search", "enabled": True}],
                        }
                    if name == "api.list":
                        return {
                            "success": True,
                            "apis": [{"plugin_id": "third.search", "name": "run", "version": "1"}],
                        }
                    if name == "render.html2png":
                        render_calls.append(args)
                        return {
                            "success": True,
                            "image_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nhost-ok").decode(),
                            "mime_type": "image/png",
                            "width": 960,
                            "height": 640,
                        }
                    if name == "llm.generate_with_tools":
                        step += 1
                        if step == 1:
                            return {
                                "success": True,
                                "response": "",
                                "tool_calls": [
                                    {
                                        "id": "call_render",
                                        "function": {
                                            "name": "html_render",
                                            "arguments": {
                                                "html": "<h1>Contract Card</h1>",
                                                "output_path": "out/card.png",
                                            },
                                        },
                                    }
                                ],
                            }
                        if step == 2:
                            return {
                                "success": True,
                                "response": "",
                                "tool_calls": [
                                    {
                                        "id": "call_attach",
                                        "function": {
                                            "name": "result_attach",
                                            "arguments": {"paths": ["out/card.png"]},
                                        },
                                    }
                                ],
                            }
                        return {
                            "success": True,
                            "response": "渲染完成，卡片已附带。",
                            "tool_calls": [],
                        }
                    raise AssertionError(name)

                plugin = create_plugin()
                plugin._set_context(
                    PluginContext(
                        PLUGIN_ID,
                        rpc_call=rpc,
                        paths=build_plugin_paths(PLUGIN_ID, Path(directory)),
                    )
                )
                plugin.set_plugin_config(
                    {
                        "plugin": {"enabled": True, "config_version": "0.1.0"},
                        "enabled_chats": ["contract-chat"],
                    }
                )
                await plugin.on_load()
                try:
                    disc = await plugin.manage_read("discovery", "{}")
                    self.assertTrue(disc["success"], disc)
                    self.assertEqual(disc["streams"][0]["stream_id"], "contract-chat")
                    self.assertIn("planner", disc["models"])
                    self.assertEqual(disc["plugins"][0]["id"], "third.search")

                    pdir = plugin.profiles.path / "painter"
                    pdir.mkdir(parents=True)
                    (pdir / "PROFILE.md").write_text("无容器渲染卡片并附上。")
                    (pdir / "profile.toml").write_text(
                        'environment = "readonly"\n'
                        'tools = ["workspace_file", "html_render", "result_attach"]\n'
                    )

                    launched = await plugin.console_send(
                        "",
                        "画一张简报卡",
                        profile="painter",
                        chat_id="contract-chat",
                    )
                    self.assertTrue(launched["success"], launched)
                    await plugin.engine.drain()
                    row = plugin.threads.get(launched["thread_id"], "contract-chat")
                    self.assertEqual(row["state"], "idle")
                    self.assertEqual(len(render_calls), 1)
                    self.assertIs(render_calls[0]["allow_network"], False)
                    self.assertEqual(row["result"]["attachments"][0]["name"], "card.png")
                finally:
                    await plugin.on_unload()

        asyncio.run(run())


if __name__ == "__main__":
    print("SDK:", importlib.metadata.version("maibot-plugin-sdk"))
    unittest.main(verbosity=2)
