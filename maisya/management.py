"""宿主认证管理页的静态 API；正文与秘密走各自的执行边界。"""

import asyncio
import base64
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict

from .config import Settings
from .memory import MemoryLayers
from .vault import atomic_private_write
from .workspaces import completed_thread, regular_open


class Management:
    def __init__(self, plugin):
        self.plugin = plugin
        self.transfers = {}
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.closed = False

    @asynccontextmanager
    async def request(self):
        if self.closed:
            raise RuntimeError("插件正在卸载")
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            yield
        finally:
            self.tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.transfers.clear()

    @staticmethod
    def arguments(payload):
        if not isinstance(payload, str) or len(payload.encode()) > 60000:
            raise ValueError("请求过大")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise TypeError("请求需要对象")
        return result

    def _version(self, value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _assets(self):
        plugin = self.plugin
        profiles = []
        for path in sorted(plugin.profiles.path.iterdir()):
            if path.is_dir() and not path.is_symlink():
                try:
                    profile = plugin.profiles.get(path.name)
                    profiles.append(
                        {
                            key: value
                            for key, value in asdict(profile).items()
                            if key
                            in {
                                "id",
                                "title",
                                "environment",
                                "access",
                                "model",
                                "bound",
                                "skills",
                            }
                        }
                    )
                except (ValueError, OSError) as error:
                    profiles.append(
                        {"id": path.name, "title": path.name, "error": str(error)}
                    )
        skills = plugin.data_root / "abilities/skills"
        return {
            "profiles": profiles[:100],
            "skills": [
                p.name
                for p in sorted(skills.iterdir())
                if p.is_dir() and not p.is_symlink()
            ][:100],
            "handles": plugin.vault.handles(),
        }

    def _settings(self):
        settings = self.plugin.settings.model_dump()
        return {
            "settings": settings,
            "version": self._version(settings),
            "admins": sorted(self.plugin.admins),
            "handles": self.plugin.vault.handles(),
        }

    def _memory(self, options):
        plugin = self.plugin
        with plugin.threads.connect() as db:
            chats = {
                row[0]
                for row in db.execute(
                    'SELECT DISTINCT chat_id FROM agent_threads WHERE owner="chat"'
                )
            }
            chats.update(
                row[0]
                for row in db.execute(
                    'SELECT DISTINCT chat_id FROM memory_versions WHERE kind="memory"'
                )
            )
            workspaces = [
                dict(row)
                for row in db.execute(
                    "SELECT workspace_id,owner,chat_id,name,profile,thread_id FROM agent_workspaces ORDER BY rowid DESC LIMIT 60"
                )
            ]
        chats.update(plugin.settings.enabled_chats)
        chat_id = options.get("chat_id", "")
        return {
            "chats": sorted(chats),
            "context": plugin.memory.context(chat_id),
            "results": plugin.memory.search(options.get("query", ""), chat_id)
            if chat_id
            else [],
            "experiences": plugin.threads.search(
                options.get("query", ""), chat_id, scope="self"
            ),
            "workspaces": workspaces,
        }

    def _intents(self):
        with self.plugin.threads.connect() as db:
            return {
                "intents": [
                    dict(row)
                    for row in db.execute(
                        "SELECT intent_id,kind,source,target,due_at,reason,status,wake_status FROM intents ORDER BY created DESC LIMIT 60"
                    )
                ],
                "learning": [
                    dict(row)
                    for row in db.execute(
                        "SELECT thread_id,revision,status FROM learning_receipts ORDER BY rowid DESC LIMIT 30"
                    )
                ],
            }

    def _transfer(self, options):
        self.transfers = {
            key: value
            for key, value in self.transfers.items()
            if value["expires"] > time.time()
        }
        token = options.get("token", "")
        if not token:
            if len(self.transfers) >= 4:
                raise ValueError("传输较多，请稍后再试")
            token = uuid.uuid4().hex
            data = self.plugin.profiles.export(options["target"])
            self.transfers[token] = {
                "data": data,
                "expires": time.time() + 300,
                "direction": "download",
            }
        transfer = self.transfers.get(token)
        if not transfer or transfer["direction"] != "download":
            raise ValueError("导出已过期，请重新导出")
        offset = options.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(transfer["data"]):
            raise ValueError("传输位置无效")
        data = transfer["data"][offset : offset + 24576]
        return {
            "token": token,
            "data": base64.b64encode(data).decode(),
            "offset": offset + len(data),
            "total": len(transfer["data"]),
        }

    async def read(self, section, payload):
        async with self.request():
            return await self._read(section, payload)

    async def _read(self, section, payload):
        options = self.arguments(payload)
        if section == "abilities":
            result = await completed_thread(self._assets)
        elif section == "asset":
            allowed = {
                key: value
                for key, value in options.items()
                if key in {"target", "chat_id", "document", "version"}
            }
            kind = options["kind"]
            result = await completed_thread(self.plugin.memory.read, kind, **allowed)
            result["history"] = await completed_thread(
                self.plugin.memory.history,
                kind,
                **{key: value for key, value in allowed.items() if key != "version"},
            )
        elif section == "settings":
            result = self._settings()
        elif section == "services":
            config = await completed_thread(self.plugin.runtime.services.load)
            await completed_thread(self._validate_services, config)
            result = {"configuration": config, "version": self._version(config)}
        elif section == "webhooks":
            config = await completed_thread(self.plugin.incoming.load)
            result = {"configuration": config, "version": self._version(config)}
        elif section == "memory":
            result = await completed_thread(self._memory, options)
        elif section == "workspace":
            result = await completed_thread(self._workspace, options)
        elif section == "intents":
            result = await completed_thread(self._intents)
        elif section == "health":
            sandbox = self.plugin.runtime.sandbox
            result = {
                "enabled": sandbox.settings.enabled,
                "ready": sandbox.ready,
                "error": sandbox.error,
                "running": len(sandbox.containers),
                "quarantined": len(self.plugin.runtime.files.quarantines),
                "bytes": await completed_thread(self.plugin.runtime.files.usage),
                "limit": self.plugin.runtime.files.total_bytes,
            }
        elif section == "package":
            async with self.lock:
                result = await completed_thread(self._transfer, options)
        else:
            raise ValueError("未知管理查询")
        return {"success": True, **self.plugin.vault.redact(result)}

    def _workspace(self, options):
        import re

        workspace_id = options["workspace_id"]
        if not isinstance(workspace_id, str) or not re.fullmatch(
            r"ws_[a-f0-9]{24}", workspace_id
        ):
            raise ValueError("工作区编号无效")
        with self.plugin.threads.connect() as db:
            if not db.execute(
                "SELECT 1 FROM agent_workspaces WHERE workspace_id=?", (workspace_id,)
            ).fetchone():
                raise ValueError("工作区不存在")
        root = self.plugin.runtime.files.root / workspace_id
        pointer = MemoryLayers._bytes(root / "current.json")
        if not pointer:
            return {"files": [], "text": ""}
        version = json.loads(pointer)["version"]
        if not re.fullmatch(r"v_[a-f0-9]{32}", version):
            raise ValueError("工作区版本无效")
        path = root / version
        files, _size = self.plugin.runtime.files.inventory(path)
        text = ""
        if options.get("path"):
            with regular_open(path, options["path"]) as source:
                text = source.read(24000).decode("utf-8", errors="replace")
        return {
            "files": [{"path": name, "bytes": size} for name, size in files[:200]],
            "text": text,
        }

    async def write(self, action, payload):
        async with self.request(), self.lock:
            return await self._write(action, payload)

    async def _write(self, action, payload):
        options = self.arguments(payload)
        plugin = self.plugin
        if action in {"asset", "rollback"}:
            kind = options.pop("kind")
            allowed = {
                key: value
                for key, value in options.items()
                if key in {"target", "chat_id", "document", "expected_version"}
            }
            if type(allowed.get("expected_version")) is not int:
                raise ValueError("请先读取当前版本")
            function = (
                plugin.memory.write if action == "asset" else plugin.memory.rollback
            )
            result = await completed_thread(
                function,
                kind,
                options["content"] if action == "asset" else options["version"],
                **allowed,
            )
        elif action == "clone":
            profile = await completed_thread(
                plugin.profiles.clone, options.get("base", "scout"), options["target"]
            )
            result = {"id": profile.id}
        elif action == "vault":
            if options.get("delete") is True:
                await completed_thread(plugin.vault.delete, options["handle"])
            else:
                await completed_thread(
                    plugin.vault.set, options["handle"], options["value"]
                )
            result = {"handles": await completed_thread(plugin.vault.handles)}
        elif action == "settings":
            if options.get("expected_version") != self._settings()["version"]:
                raise ValueError("设置已变化，请刷新后再保存")
            settings = Settings.model_validate(options["settings"])
            await plugin.on_config_update("self", settings.model_dump(), 0)
            await completed_thread(
                atomic_private_write,
                plugin.data_root / "settings.json",
                json.dumps(settings.model_dump(), ensure_ascii=False).encode(),
            )
            result = self._settings()
        elif action == "services":
            current = await completed_thread(plugin.runtime.services.load)
            if options.get("expected_version") != self._version(current):
                raise ValueError("服务登记已变化，请刷新后重试")
            content = json.dumps(options["configuration"], ensure_ascii=False)
            await completed_thread(self._validate_services, options["configuration"])
            await completed_thread(
                atomic_private_write, plugin.runtime.services.path, content.encode()
            )
            await plugin.runtime.services.close_sessions()
            result = {"version": self._version(options["configuration"])}
        elif action == "webhooks":
            current = await completed_thread(plugin.incoming.load)
            if options.get("expected_version") != self._version(current):
                raise ValueError("事件通道已变化，请刷新后重试")
            config = options["configuration"]
            await completed_thread(plugin.incoming.validate, config)
            content = json.dumps(config, ensure_ascii=False).encode()
            if plugin.vault.contains_secret(content) or len(content) > 48000:
                raise ValueError("事件通道只保存凭据句柄")
            await completed_thread(atomic_private_write, plugin.incoming.path, content)
            result = {"version": self._version(config)}
        elif action == "cancel_intent":
            row = await completed_thread(plugin.intents.get, options["intent_id"])
            await completed_thread(
                plugin.intents.cancel, row["intent_id"], row["target"]
            )
            result = {"intent_id": row["intent_id"]}
        elif action == "import":
            result = await completed_thread(self._upload, options)
        else:
            raise ValueError("未知管理操作")
        return {"success": True, **plugin.vault.redact(result)}

    def _validate_services(self, config):
        raw = json.dumps(config).encode()
        if (
            self.plugin.vault.contains_secret(raw)
            or len(raw) > 48000
            or not isinstance(config, dict)
            or set(config) - {"servers", "templates", "inputs"}
        ):
            raise ValueError("服务登记只接受结构化配置和凭据句柄")
        if not isinstance(config.get("servers", {}), dict) or not isinstance(
            config.get("templates", {}), dict
        ):
            raise TypeError("服务登记需要 servers 和 templates 对象")
        inputs = config.get("inputs", {})
        if not isinstance(inputs, dict) or len(inputs) > 16:
            raise ValueError("平台文件读取器登记无效")
        for binding in inputs.values():
            if (
                not isinstance(binding, dict)
                or set(binding) != {"service", "tool"}
                or binding["service"] not in config.get("servers", {})
                or binding["tool"]
                not in config["servers"][binding["service"]].get("tools", [])
            ):
                raise ValueError("平台文件读取器需要已登记的服务和工具")
        for name, server in config.get("servers", {}).items():
            self.plugin.vault._validate_handle(name)
            if not isinstance(server, dict) or set(server) - {
                "command",
                "credentials",
                "tools",
                "readonly",
                "access",
                "memory_mib",
            }:
                raise ValueError("MCP 服务含未知配置项")
            command = server.get("command", [])
            if (
                not isinstance(command, list)
                or not command
                or not all(
                    isinstance(part, str) and "\x00" not in part for part in command
                )
                or not command[0].startswith("/")
            ):
                raise ValueError("MCP 程序使用绝对路径与固定参数")
            for handle in server.get("credentials", {}).values():
                self.plugin.vault._validate_handle(handle)
        for template in config.get("templates", {}).values():
            if not isinstance(template, dict) or set(template) - {
                "argv",
                "schema",
                "secrets",
                "targets",
            }:
                raise ValueError("固定模板含未知配置项")
            for handle in template.get("secrets", []):
                self.plugin.vault._validate_handle(handle)

    def _upload(self, options):
        self.transfers = {
            key: value
            for key, value in self.transfers.items()
            if value["expires"] > time.time()
        }
        token = options.get("token", "")
        if not token:
            if len(self.transfers) >= 4:
                raise ValueError("传输较多，请稍后再试")
            token = uuid.uuid4().hex
            self.transfers[token] = {
                "data": bytearray(),
                "target": options["target"],
                "expires": time.time() + 300,
                "direction": "upload",
            }
        transfer = self.transfers.get(token)
        if (
            not transfer
            or transfer["direction"] != "upload"
            or options.get("offset", 0) != len(transfer["data"])
        ):
            raise ValueError("导入位置无效或传输过期")
        data = base64.b64decode(options["data"], validate=True)
        if len(data) > 24576 or len(transfer["data"]) + len(data) > 8 * 1024**2:
            self.transfers.pop(token, None)
            raise ValueError("档案包过大")
        transfer["data"].extend(data)
        if options.get("done"):
            self.transfers.pop(token)
            profile = self.plugin.profiles.import_package(
                bytes(transfer["data"]), transfer["target"]
            )
            return {"id": profile.id, "done": True}
        return {"token": token, "offset": len(transfer["data"])}
