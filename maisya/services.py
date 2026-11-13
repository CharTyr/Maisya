"""管理员登记的 MCP stdio 服务池和固定凭据模板。"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from .sandbox import MINIMAL_ENV
from .workspaces import completed_thread


def validate(schema, arguments):
    if (
        not isinstance(schema, dict)
        or len(json.dumps(schema)) > 65536
        or '"$ref"' in json.dumps(schema)
    ):
        raise ValueError("能力参数 schema 无效或含未绑定引用")
    Draft202012Validator.check_schema(schema)
    try:
        Draft202012Validator(schema).validate(arguments)
    except ValidationError:
        raise ValueError("能力调用参数未通过 schema 校验") from None


class StdioSession:
    def __init__(self, child, home, cgroup=None):
        self.child, self.home, self.counter = child, home, 0
        self.lock = asyncio.Lock()
        self.tools = {}
        self.cgroup = cgroup

    async def notify(self, message):
        self.child.stdin.write(
            (
                json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n"
            ).encode()
        )
        await self.child.stdin.drain()

    async def request(self, method, params):
        async with self.lock, asyncio.timeout(30):
            self.counter += 1
            request_id = self.counter
            await self.notify({"id": request_id, "method": method, "params": params})
            for _attempt in range(64):
                line = await self.child.stdout.readline()
                if not line or len(line) > 12 * 1024**2:
                    raise RuntimeError("MCP 连接已关闭或响应过大")
                message = json.loads(line)
                if message.get("method"):
                    if "id" in message:
                        await self.notify(
                            {
                                "id": message["id"],
                                **(
                                    {"result": {}}
                                    if message["method"] == "ping"
                                    else {
                                        "error": {
                                            "code": -32601,
                                            "message": "Client capability unavailable",
                                        }
                                    }
                                ),
                            }
                        )
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError("MCP 服务返回错误")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise TypeError("MCP 响应格式错误")
                return result
            raise RuntimeError("MCP 通知数量超限")

    async def close(self):
        if self.cgroup and self.cgroup.exists():
            await completed_thread((self.cgroup / "cgroup.kill").write_text, "1")
        if self.child.returncode is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.child.wait(), 2)
            except TimeoutError:
                try:
                    os.killpg(self.child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.child.wait()
        await completed_thread(shutil.rmtree, self.home)
        if self.cgroup and self.cgroup.exists():
            await completed_thread(self.cgroup.rmdir)


class Services:
    def __init__(self, path, vault):
        self.path, self.vault = Path(path), vault
        self.sessions = {}
        self.locks = {}
        self.digest = ""
        self.configuration = {"servers": {}, "templates": {}}
        self.closed = False
        self.cgroup_name = "maisya-mcp-" + uuid.uuid4().hex[:16]
        self.group_lock = asyncio.Lock()
        self.quarantines = []

    def load(self):
        if not self.path.exists():
            raw = b'{"servers":{},"templates":{}}'
        else:
            if self.path.is_symlink() or self.path.stat().st_size > 262144:
                raise ValueError("能力服务登记文件无效")
            raw = self.path.read_bytes()
        configuration = json.loads(raw)
        if not isinstance(configuration, dict) or set(configuration) - {
            "servers",
            "templates",
            "inputs",
        }:
            raise ValueError("能力服务登记格式无效")
        digest = hashlib.sha256(raw).hexdigest()
        for group in ("servers", "templates"):
            if not isinstance(configuration.get(group, {}), dict):
                raise TypeError("能力服务登记格式无效")
            for name in configuration.get(group, {}):
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                    raise ValueError("能力服务编号无效")
        self.configuration, self.digest = configuration, digest
        return configuration

    def describe(self, profile):
        configuration = self.load()
        return {
            name: {
                "tools": configuration.get("servers", {}).get(name, {}).get("tools", [])
            }
            for name in profile.mcp
        }

    async def _session(self, service, context, config):
        key = service, self.digest, context["chat_id"], context["thread_id"]
        if key in self.sessions:
            return self.sessions[key]
        for old_key in list(self.sessions):
            if old_key[1] != self.digest:
                await self.sessions.pop(old_key).close()
        if len(self.sessions) >= 16:
            oldest = next(iter(self.sessions))
            await self.sessions.pop(oldest).close()
        command = config.get("command")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 32
            or not all(
                isinstance(value, str) and "\x00" not in value for value in command
            )
            or not Path(command[0]).is_absolute()
        ):
            raise ValueError("MCP 需要管理员登记的绝对程序路径与固定参数")
        credentials = config.get("credentials", {})
        if not isinstance(credentials, dict) or any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) for name in credentials
        ):
            raise ValueError("MCP 凭据环境绑定无效")
        if set(credentials) & {
            "PATH",
            "HOME",
            "PYTHONPATH",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
        }:
            raise ValueError("MCP 凭据不能覆盖启动环境")
        environment = dict(MINIMAL_ENV)
        for name, handle in credentials.items():
            environment[name] = await completed_thread(self.vault.for_execution, handle)
        home = Path(tempfile.mkdtemp(prefix="maisya-mcp-"))
        home.chmod(0o700)
        uid = 2000000 + int.from_bytes(os.urandom(3), "big") % 1000000
        if sys.platform.startswith("linux") and os.geteuid() == 0:
            os.chown(home, uid, uid)
        environment["HOME"] = str(home)
        memory = config.get("memory_mib", 256)
        if type(memory) is not int or not 128 <= memory <= 4096:
            raise ValueError("MCP 资源配置无效")
        if sys.platform.startswith("linux") and os.geteuid() == 0:
            async with self.group_lock:
                await completed_thread(self._prepare_group)
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(Path(__file__).with_name("mcp_worker.py")),
            json.dumps([command, memory, uid, self.cgroup_name]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            cwd=home,
            start_new_session=True,
            limit=12 * 1024**2,
        )
        cgroup = (
            Path("/sys/fs/cgroup") / self.cgroup_name / str(uid)
            if sys.platform.startswith("linux") and os.geteuid() == 0
            else None
        )
        session = StdioSession(child, home, cgroup)
        try:
            response = await session.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "maisya", "version": "0.0.1"},
                },
            )
            if response.get("protocolVersion") not in {
                "2024-11-05",
                "2025-03-26",
                "2025-06-18",
                "2025-11-25",
            } or "tools" not in response.get("capabilities", {}):
                raise ValueError("MCP 协议或工具能力不兼容")
            await session.notify({"method": "notifications/initialized"})
            tools = await session.request("tools/list", {})
            allowed = config.get("tools", [])
            if not isinstance(allowed, list) or not all(
                isinstance(name, str) for name in allowed
            ):
                raise ValueError("MCP 工具白名单无效")
            session.tools = {
                item["name"]: item
                for item in tools.get("tools", [])
                if isinstance(item, dict) and item.get("name") in allowed
            }
            self.sessions[key] = session
            return session
        except BaseException:
            await session.close()
            raise

    def _prepare_group(self):
        base = Path("/sys/fs/cgroup") / self.cgroup_name
        if base.exists():
            return
        base.mkdir()
        (base / "memory.max").write_text(str(512 * 1024**2))
        (base / "pids.max").write_text("128")
        (base / "cpu.max").write_text("100000 100000")
        (base / "cgroup.subtree_control").write_text("+memory +pids +cpu")

    async def call(self, profile, arguments, context, runtime):
        if self.closed:
            raise RuntimeError("能力服务正在关闭")
        configuration = self.load()
        service, tool = arguments["service"], arguments["tool"]
        config = configuration.get("servers", {}).get(service)
        if (
            service not in profile.mcp
            or not isinstance(config, dict)
            or tool not in config.get("tools", [])
        ):
            raise PermissionError("本档案未授权该 MCP 服务或工具")
        if config.get("access", "everyone") == "admins" and not await completed_thread(
            runtime.authorized, context
        ):
            raise PermissionError("MCP 服务需要当前管理员授权")
        if profile.environment == "readonly" and not config.get("readonly", False):
            raise PermissionError("只读档案不能调用可写 MCP 服务")
        lock = self.locks.setdefault((service, context["thread_id"]), asyncio.Lock())
        async with lock:
            session = await self._session(service, context, config)
            if tool not in session.tools:
                raise PermissionError("MCP 未提供登记的工具")
            await completed_thread(
                validate,
                session.tools[tool].get("inputSchema", {}),
                arguments["arguments"],
            )
            key = context["thread_id"], context["revision"], context["_tool_call_id"]
            previous = await completed_thread(runtime.sandbox._receipt, key)
            if previous:
                return {
                    "status": previous["status"],
                    **json.loads(previous["result"]),
                    "repeated": True,
                }
            await completed_thread(runtime.sandbox._receipt, key, status="running")
            try:
                result = await session.request(
                    "tools/call", {"name": tool, "arguments": arguments["arguments"]}
                )
                if result.get("isError"):
                    raise RuntimeError("MCP 工具执行失败")
                response = await self._content(
                    result.get("content", []), context, runtime
                )
                await completed_thread(
                    runtime.sandbox._receipt, key, status="completed", result=response
                )
                return response
            except BaseException:
                await completed_thread(
                    runtime.sandbox._receipt, key, status="uncertain"
                )
                for session_key in list(self.sessions):
                    if self.sessions[session_key] is session:
                        self.sessions.pop(session_key)
                await session.close()
                raise

    async def _content(self, content, context, runtime):
        if not isinstance(content, list) or len(content) > 32:
            raise ValueError("MCP 内容数量超限")
        text, binary = [], []
        for item in content:
            if item.get("type") == "text":
                text.append(str(self.vault.redact(item.get("text", "")))[:12000])
            elif item.get("type") == "image":
                mime = item.get("mimeType")
                extension = {
                    "image/png": "png",
                    "image/jpeg": "jpg",
                    "image/webp": "webp",
                    "image/gif": "gif",
                }.get(mime)
                raw = item.get("data", "")
                if not extension or not isinstance(raw, str) or len(raw) > 8 * 1024**2:
                    raise ValueError("MCP 图片格式或大小无效")
                data = base64.b64decode(raw, validate=True)
                binary.append((extension, data))
            elif (
                item.get("type") == "resource"
                and isinstance(item.get("resource"), dict)
                and "blob" in item["resource"]
            ):
                raw = item["resource"]["blob"]
                if not isinstance(raw, str) or len(raw) > 8 * 1024**2:
                    raise ValueError("MCP 文件资源过大")
                binary.append(("bin", base64.b64decode(raw, validate=True)))
            elif (
                item.get("type") == "resource"
                and isinstance(item.get("resource"), dict)
                and "text" in item["resource"]
            ):
                text.append(str(self.vault.redact(item["resource"]["text"]))[:12000])
            else:
                raise ValueError("MCP 返回未支持的内容类型")
        attachments = []
        if binary:
            if len(binary) > 4:
                raise ValueError("MCP 附件超过四个")
            work = await runtime._work(context)
            for index, (extension, data) in enumerate(binary):
                destination = work.path / "out" / f"mcp-{index}.{extension}"
                await completed_thread(destination.write_bytes, data)
                attachments += await completed_thread(
                    runtime.files.attach,
                    context,
                    work,
                    [f"out/mcp-{index}.{extension}"],
                )
            runtime.attachments[context["thread_id"]] = attachments
        return {
            "text": "\n".join(text)[:12000],
            **({"attachments": attachments} if attachments else {}),
        }

    async def input_file(self, profile, message_id, file_ref, context, runtime):
        binding = self.load().get("inputs", {}).get(context["platform"])
        if (
            not isinstance(binding, dict)
            or "mcp_call" not in profile.tools
            or binding.get("service") not in profile.mcp
        ):
            raise ValueError("该平台的文件读取器尚未登记或未授权给当前档案")
        old = runtime.attachments.get(context["thread_id"])
        try:
            result = await self.call(
                profile,
                {
                    "service": binding["service"],
                    "tool": binding["tool"],
                    "arguments": {"message_id": message_id, "file_ref": file_ref},
                },
                {
                    **context,
                    "_tool_call_id": "input-"
                    + hashlib.sha256(
                        (message_id + json.dumps(file_ref)).encode()
                    ).hexdigest()[:24],
                },
                runtime,
            )
            attachments = result.get("attachments", [])
            if len(attachments) != 1:
                raise ValueError("平台文件读取器需要返回单个二进制资源")
            path = await completed_thread(
                runtime.files.resolve, attachments[0]["handle"], context["chat_id"]
            )
            return await completed_thread(path.read_bytes)
        finally:
            if old is None:
                runtime.attachments.pop(context["thread_id"], None)
            else:
                runtime.attachments[context["thread_id"]] = old

    async def credential(self, profile, arguments, context, runtime):
        config = self.load().get("templates", {}).get(profile.template)
        if (
            not isinstance(config, dict)
            or not profile.secrets
            or profile.access != "admins"
            or not await completed_thread(runtime.authorized, context)
        ):
            raise PermissionError("私有凭据执行模板未绑定或缺少当前授权")
        await completed_thread(validate, config.get("schema", {}), arguments)
        program = config.get("argv")
        if (
            not isinstance(program, list)
            or not program
            or not all(isinstance(value, str) for value in program)
            or not re.fullmatch(
                r"/opt/maisya/templates/[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", program[0]
            )
        ):
            raise ValueError("凭据模板必须是固定镜像内的受信程序")
        if set(profile.secrets) != set(config.get("secrets", [])):
            raise PermissionError("档案凭据与固定模板绑定不一致")
        targets = config.get("targets", [])
        if not isinstance(targets, list) or not targets or len(targets) > 8:
            raise ValueError("固定模板需要明确目标")
        endpoints = []
        for item in targets:
            address = str(ipaddress.IPv4Address(item["address"]))
            port = item["port"]
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("固定模板目标端口无效")
            endpoints.append((address, port))
        blank = {**context, "workspace_id": "ws_" + os.urandom(12).hex()}
        manager = runtime.files.activity(blank)
        work = await completed_thread(manager.__enter__)
        secret_directory = Path(
            tempfile.mkdtemp(prefix="secrets-", dir=runtime.files.runtime)
        )
        secret_volume = runtime.sandbox.volume(secret_directory)
        mounted = False
        try:
            await completed_thread(secret_volume.__enter__)
            mounted = True
            secrets = {}
            for handle in profile.secrets:
                value = await completed_thread(self.vault.for_execution, handle)
                path = secret_directory / handle
                await completed_thread(path.write_text, value)
                await completed_thread(os.chown, path, 10000, 10000)
                await completed_thread(path.chmod, 0o400)
                secrets[handle] = path
            result = await runtime.sandbox.execute(
                context,
                work,
                "fixed-template",
                call_id=context["_tool_call_id"],
                mode="restricted",
                targets=endpoints,
                secrets=secrets,
                entrypoint=[*program, json.dumps(arguments, ensure_ascii=False)],
            )
            return self.vault.redact(result)
        finally:
            if mounted and work.quarantined:
                self.quarantines.append((secret_volume, secret_directory))
            elif mounted:
                await completed_thread(secret_volume.__exit__, None, None, None)
                await completed_thread(shutil.rmtree, secret_directory)
            else:
                await completed_thread(shutil.rmtree, secret_directory)
            await completed_thread(manager.__exit__, None, None, None)

    async def release_quarantines(self):
        for volume, path in list(self.quarantines):
            await completed_thread(volume.__exit__, None, None, None)
            await completed_thread(shutil.rmtree, path)
            self.quarantines.remove((volume, path))

    async def close_thread(self, thread_id):
        for key in list(self.sessions):
            if key[3] == thread_id:
                await self.sessions.pop(key).close()

    async def close(self):
        self.closed = True
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()
        cgroup = Path("/sys/fs/cgroup") / self.cgroup_name
        if sys.platform.startswith("linux") and cgroup.exists():
            await completed_thread(cgroup.rmdir)

    async def close_sessions(self):
        for key in list(self.sessions):
            await self.sessions.pop(key).close()
