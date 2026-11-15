"""管理员登记的 MCP stdio 服务池和固定凭据模板。"""

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import tempfile
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .pathsafe import (
    is_absolute_program_path,
    minimal_subprocess_env,
    path_is_link_like,
    rmtree_safe,
    safe_chmod,
)
from .scout import call_capability
from .workspaces import completed_thread


def _is_native_absolute_program_path(raw: str) -> bool:
    """校验程序路径匹配当前宿主操作系统的绝对路径风格（POSIX 拒绝 Windows 盘符）。"""
    if not is_absolute_program_path(raw):
        return False
    if os.name == "nt":
        return not raw.startswith("/")
    return raw.startswith("/")


def _unwrap_api_result(raw, key):
    if isinstance(raw, dict):
        if raw.get("success") is False:
            raise RuntimeError(str(raw.get("error") or "跨插件只读 API 调用失败"))
        if "success" in raw and key in raw:
            return raw[key]
    return raw


def validate_api_entry(name, spec):
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_-]{0,63}", name
    ):
        raise ValueError("跨插件 API 句柄无效")
    if not isinstance(spec, dict) or set(spec) - {
        "api_name",
        "version",
        "readonly",
        "access",
        "description",
        "schema",
    }:
        raise ValueError("跨插件 API 含未知配置项")
    api_name = spec.get("api_name", "")
    if (
        not isinstance(api_name, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", api_name)
        or api_name.startswith(("chartyr.maisya.", "maisya_"))
        or ".maisya_" in api_name
    ):
        raise ValueError("跨插件 API 名称无效，且不能登记 Maisya 自身内部接口")
    if "readonly" in spec and type(spec["readonly"]) is not bool:
        raise ValueError("跨插件 API readonly 必须是布尔值")
    if spec.get("access", "everyone") not in {"everyone", "admins"}:
        raise ValueError("跨插件 API 访问范围无效")
    version = spec.get("version", "")
    description = spec.get("description", "")
    if (
        not isinstance(version, str)
        or len(version) > 32
        or not isinstance(description, str)
        or len(description) > 500
    ):
        raise ValueError("跨插件 API 版本或说明无效")
    if "schema" in spec:
        schema = spec["schema"]
        if (
            not isinstance(schema, dict)
            or len(json.dumps(schema)) > 65536
            or '"$ref"' in json.dumps(schema)
        ):
            raise ValueError("跨插件 API 参数 schema 无效或含未绑定引用")
        Draft202012Validator.check_schema(schema)


def _validate_web_provider_entry(name, spec):
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_-]{0,63}", name
    ):
        raise ValueError("联网搜索提供方句柄无效")
    if not isinstance(spec, dict) or set(spec) - {
        "kind",
        "target",
        "service",
        "tool",
        "api",
        "api_name",
        "version",
        "arg_map",
        "defaults",
    }:
        raise ValueError("联网搜索提供方配置含未知字段")
    kind = spec.get("kind", "")
    if kind not in {"host", "google_search_plugin", "mcp", "plugin_api", "plugin"}:
        raise ValueError("联网搜索提供方类型无效")
    if "arg_map" in spec:
        arg_map = spec["arg_map"]
        if not isinstance(arg_map, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in arg_map.items()
        ):
            raise ValueError("联网搜索 arg_map 必须是字符串映射")
    if "defaults" in spec and not isinstance(spec["defaults"], dict):
        raise ValueError("联网搜索 defaults 必须是对象")


def validate_web_bindings(configuration):
    web = configuration.get("web", {})
    if not isinstance(web, dict) or set(web) - {"search", "fetch", "providers"}:
        raise ValueError("联网服务绑定仅支持 search、fetch 与 providers")
    servers = configuration.get("servers", {})
    apis = configuration.get("apis", {})
    for kind in ("search", "fetch"):
        if kind not in web:
            continue
        binding = web[kind]
        if not isinstance(binding, dict):
            raise ValueError(f"联网服务 {kind} 绑定格式无效")
        base_keys = set(binding) - {"arg_map", "defaults"}
        if "arg_map" in binding and (
            not isinstance(binding["arg_map"], dict)
            or not all(
                isinstance(k, str) and isinstance(v, str) and v
                for k, v in binding["arg_map"].items()
            )
        ):
            raise ValueError("联网服务 arg_map 必须是字符串映射")
        if "defaults" in binding and not isinstance(binding["defaults"], dict):
            raise ValueError("联网服务 defaults 必须是对象")
        if base_keys == {"service", "tool"}:
            service, tool = binding["service"], binding["tool"]
            server = servers.get(service)
            if (
                not isinstance(server, dict)
                or tool not in server.get("tools", [])
                or server.get("readonly") is not True
            ):
                raise ValueError("联网服务绑定仅允许 readonly=true 的只读 MCP 服务与已登记工具")
        elif base_keys == {"api"}:
            api_id = binding["api"]
            spec = apis.get(api_id)
            if not isinstance(spec, dict) or spec.get("readonly") is not True:
                raise ValueError("联网服务绑定仅允许 readonly=true 的已登记跨插件只读 API")
        else:
            raise ValueError("联网服务绑定须指定 {service, tool} 或 {api}")
    if "providers" in web:
        providers = web["providers"]
        if not isinstance(providers, dict) or len(providers) > 32:
            raise ValueError("联网搜索提供方列表格式或数量无效")
        for name, spec in providers.items():
            _validate_web_provider_entry(name, spec)


def validate_endpoint_targets(targets):
    if not isinstance(targets, (list, tuple)) or not targets or len(targets) > 64:
        raise ValueError("受限网络需要 1 到 64 个明确目标")
    endpoints = []
    seen = set()
    for item in targets:
        if isinstance(item, dict):
            host = str(item.get("address", "")).strip()
            port = item.get("port")
        elif isinstance(item, str) and ":" in item:
            host, raw_port = item.rsplit(":", 1)
            host = host.strip()
            if not raw_port.isdigit():
                raise ValueError("受限网络目标端口无效")
            port = int(raw_port)
        else:
            raise ValueError("受限网络目标格式应为 host:port 或 {address, port}")
        if not host or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("受限网络目标主机或端口无效")
        try:
            ip_obj = ipaddress.IPv4Address(host)
            candidates = (
                [str(ip_obj)] if ip_obj.is_global and not ip_obj.is_multicast else []
            )
            if not candidates:
                raise ValueError("受限网络目标禁止内网、组播或保留地址")
        except ipaddress.AddressValueError:
            if len(host) > 253 or any(ch.isspace() for ch in host):
                raise ValueError("受限网络目标域名无效")
            try:
                resolved = socket.getaddrinfo(
                    host, port, socket.AF_INET, socket.SOCK_STREAM
                )
            except OSError as error:
                raise ValueError(f"无法解析目标主机 {host}") from error
            candidates = []
            for entry in resolved:
                ip_text = entry[4][0]
                resolved_ip = ipaddress.IPv4Address(ip_text)
                if not resolved_ip.is_global or resolved_ip.is_multicast:
                    raise ValueError("目标域名解析包含非公网地址，已拒绝")
                if ip_text not in candidates:
                    candidates.append(ip_text)
            if not candidates:
                raise ValueError(f"目标主机 {host} 未返回可用 IPv4 地址")
        for ip_text in candidates:
            pair = (ip_text, port)
            if pair not in seen:
                seen.add(pair)
                endpoints.append(pair)
    return endpoints


def _resolve_local_pointer(root: dict, ref: str):
    if ref == "#":
        return root
    if not isinstance(ref, str) or not ref.startswith("#/"):
        raise ValueError("能力参数 schema 无效或含未绑定引用")
    cur = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if part not in cur:
                raise ValueError("能力参数 schema 无效或含未绑定引用")
            cur = cur[part]
        elif isinstance(cur, list):
            if not part.isdigit():
                raise ValueError("能力参数 schema 无效或含未绑定引用")
            idx = int(part)
            if idx < 0 or idx >= len(cur):
                raise ValueError("能力参数 schema 无效或含未绑定引用")
            cur = cur[idx]
        else:
            raise ValueError("能力参数 schema 无效或含未绑定引用")
    if not isinstance(cur, (dict, bool)):
        raise ValueError("能力参数 schema 无效或含未绑定引用")
    return cur


def _validate_local_refs(root: dict, node) -> None:
    if isinstance(node, list):
        for item in node:
            _validate_local_refs(root, item)
        return
    if not isinstance(node, dict):
        return
    if "$dynamicRef" in node or "$recursiveRef" in node:
        raise ValueError("能力参数 schema 无效或含未绑定引用")
    if "$ref" in node:
        _resolve_local_pointer(root, node["$ref"])
    for key, val in node.items():
        if key in {"properties", "patternProperties", "$defs", "definitions"} and isinstance(val, dict):
            for sub in val.values():
                _validate_local_refs(root, sub)
        else:
            _validate_local_refs(root, val)


def validate(schema, arguments):
    if not isinstance(schema, dict):
        raise ValueError("能力参数 schema 无效或含未绑定引用")
    try:
        dumped = json.dumps(schema)
    except (TypeError, ValueError):
        raise ValueError("能力参数 schema 无效或含未绑定引用") from None
    if len(dumped) > 65536:
        raise ValueError("能力参数 schema 无效或含未绑定引用")
    _validate_local_refs(schema, schema)
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(arguments)
    except (ValidationError, SchemaError, RecursionError):
        raise ValueError("能力调用参数未通过 schema 校验") from None


class StdioSession:
    def __init__(self, child, home, cgroup=None, uid=None, on_close=None):
        self.child, self.home, self.counter = child, home, 0
        self.lock = asyncio.Lock()
        self.tools = {}
        self.cgroup = cgroup
        self.uid = uid
        self._on_close = on_close

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
        try:
            if self.cgroup and self.cgroup.exists():
                try:
                    await completed_thread(
                        (self.cgroup / "cgroup.kill").write_text, "1", encoding="utf-8"
                    )
                except OSError:
                    pass
            if self.child.returncode is None:
                use_killpg = os.name != "nt" and hasattr(os, "killpg")
                try:
                    if use_killpg:
                        os.killpg(self.child.pid, signal.SIGTERM)
                    else:
                        self.child.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.child.wait(), 2)
                except TimeoutError:
                    try:
                        if use_killpg:
                            os.killpg(self.child.pid, signal.SIGKILL)
                        else:
                            self.child.kill()
                    except ProcessLookupError:
                        pass
                    await self.child.wait()
            await completed_thread(rmtree_safe, self.home, ignore_errors=True)
            if self.cgroup and self.cgroup.exists():
                try:
                    await completed_thread(self.cgroup.rmdir)
                except OSError:
                    pass
        finally:
            if self._on_close is not None and self.uid is not None:
                self._on_close(self.uid)


class Services:
    def __init__(self, path, vault):
        self.path, self.vault = Path(path), vault
        self.sessions = {}
        self.locks = {}
        self.active_uids = set()
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
            if path_is_link_like(self.path) or self.path.stat().st_size > 262144:
                raise ValueError("能力服务登记文件无效")
            raw = self.path.read_bytes()
        configuration = json.loads(raw)
        if not isinstance(configuration, dict) or set(configuration) - {
            "servers",
            "templates",
            "inputs",
            "apis",
            "web",
        }:
            raise ValueError("能力服务登记格式无效")
        digest = hashlib.sha256(raw).hexdigest()
        for group in ("servers", "templates"):
            if not isinstance(configuration.get(group, {}), dict):
                raise TypeError("能力服务登记格式无效")
            for name in configuration.get(group, {}):
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                    raise ValueError("能力服务编号无效")
        apis = configuration.get("apis", {})
        if not isinstance(apis, dict) or len(apis) > 32:
            raise ValueError("跨插件 API 登记数量或格式无效")
        for name, spec in apis.items():
            validate_api_entry(name, spec)
        validate_web_bindings(configuration)
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

    def describe_apis(self, profile):
        apis = self.load().get("apis", {})
        return {
            name: {
                "api_name": apis[name]["api_name"],
                **({"version": apis[name]["version"]} if apis[name].get("version") else {}),
                **({"description": apis[name]["description"]} if apis[name].get("description") else {}),
                "access": apis[name].get("access", "everyone"),
            }
            for name in profile.apis
            if name in apis
        }

    async def call_api(self, profile, arguments, context, runtime):
        if self.closed:
            raise RuntimeError("能力服务正在关闭")
        apis_cfg = self.load().get("apis", {})
        action = arguments["action"]
        needs_admin_check = any(
            apis_cfg.get(name, {}).get("access", "everyone") == "admins"
            for name in profile.apis
        )
        is_admin = (
            await completed_thread(runtime.authorized, context)
            if needs_admin_check
            else False
        )
        if action == "list":
            listed_online = set()
            try:
                raw_list = await call_capability(
                    runtime.host, "api.list", timeout_ms=10000
                )
                items = _unwrap_api_result(raw_list, "apis") or []
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        api_n = str(item.get("name") or "")
                        plugin_id = str(item.get("plugin_id") or "")
                        if api_n:
                            listed_online.add(api_n)
                        if plugin_id and api_n:
                            listed_online.add(f"{plugin_id}.{api_n}")
            except (RuntimeError, OSError, ValueError, TypeError):
                pass
            visible = []
            for name in profile.apis:
                spec = apis_cfg.get(name)
                if not spec or (spec.get("access", "everyone") == "admins" and not is_admin):
                    continue
                visible.append(
                    {
                        "id": name,
                        "api_name": spec["api_name"],
                        "version": spec.get("version", ""),
                        "description": spec.get("description", ""),
                        **({"online": spec["api_name"] in listed_online} if listed_online else {}),
                    }
                )
            return self.vault.redact({"apis": visible})

        api_id = arguments.get("api", "")
        spec = apis_cfg.get(api_id)
        if not api_id or api_id not in profile.apis or not isinstance(spec, dict):
            raise PermissionError("本档案未授权该跨插件 API")
        if spec.get("access", "everyone") == "admins" and not is_admin:
            raise PermissionError("该跨插件 API 需要当前管理员授权")
        if (
            profile.environment == "readonly"
            and spec.get("readonly") is not True
            and not (
                profile.access == "admins"
                and await completed_thread(runtime.authorized, context)
            )
        ):
            raise PermissionError("只读普通档案不能调用非只读跨插件 API")

        if action == "get":
            raw = await call_capability(
                runtime.host,
                "api.get",
                timeout_ms=10000,
                api_name=spec["api_name"],
                version=spec.get("version", ""),
            )
            meta = _unwrap_api_result(raw, "api")
            if meta is None:
                raise RuntimeError("宿主未找到已登记的跨插件只读 API")
            payload = {"id": api_id, "api": meta}
            if "schema" in spec:
                payload["schema"] = spec["schema"]
            return self.vault.redact(payload)

        if action == "call":
            call_args = arguments.get("arguments")
            if call_args is None:
                call_args = {}
            if "schema" in spec:
                await completed_thread(validate, spec["schema"], call_args)
            raw = await call_capability(
                runtime.host,
                "api.call",
                timeout_ms=30000,
                api_name=spec["api_name"],
                version=spec.get("version", ""),
                args=call_args,
            )
            result = self.vault.redact(_unwrap_api_result(raw, "result"))
            wrapped = {"api": api_id, "result": result}
            encoded = json.dumps(wrapped, ensure_ascii=False)
            if len(encoded) > 20000:
                return {
                    "api": api_id,
                    "text": encoded[:20000],
                    "truncated": True,
                    "original_chars": len(encoded),
                }
            return wrapped

        raise ValueError("未知跨插件 API 动作")

    def _allocate_uid(self) -> int:
        is_root_linux = sys.platform.startswith("linux") and os.geteuid() == 0
        cgroup_base = Path("/sys/fs/cgroup") / self.cgroup_name if is_root_linux else None
        for _ in range(128):
            cand = 2000000 + int.from_bytes(os.urandom(3), "big") % 1000000
            if cand in self.active_uids:
                continue
            if cgroup_base is not None and (cgroup_base / str(cand)).exists():
                continue
            self.active_uids.add(cand)
            return cand
        raise RuntimeError("无法分配可用的沙箱 UID")

    def _session_lock_busy(
        self, session_key, caller_service: str, caller_thread_id: str
    ) -> bool:
        s_service, _, _, s_thread_id = session_key
        if (s_service, s_thread_id) == (caller_service, caller_thread_id):
            return False
        lock = self.locks.get((s_service, s_thread_id))
        return bool(
            lock is not None and (lock.locked() or getattr(lock, "_waiters", None))
        )

    async def _close_session_key_safe(self, session_key) -> bool:
        session = self.sessions.pop(session_key, None)
        if session is None:
            return False
        try:
            await session.close()
        except Exception:
            pass
        return True

    async def _evict_session_key(
        self, session_key, caller_service: str, caller_thread_id: str
    ) -> bool:
        if self._session_lock_busy(session_key, caller_service, caller_thread_id):
            return False
        s_service, _, _, s_thread_id = session_key
        if (s_service, s_thread_id) == (caller_service, caller_thread_id):
            return await self._close_session_key_safe(session_key)
        cand_lock = self.locks.setdefault((s_service, s_thread_id), asyncio.Lock())
        async with cand_lock:
            return await self._close_session_key_safe(session_key)

    async def _session(self, service, context, config):
        key = service, self.digest, context["chat_id"], context["thread_id"]
        if key in self.sessions:
            return self.sessions[key]
        command = config.get("command")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 32
            or not all(
                isinstance(value, str) and "\x00" not in value for value in command
            )
            or not _is_native_absolute_program_path(command[0])
        ):
            raise ValueError("MCP 需要管理员登记的绝对程序路径与固定参数")
        for old_key in list(self.sessions):
            if old_key[1] != self.digest:
                await self._evict_session_key(old_key, service, context["thread_id"])
        while len(self.sessions) >= 16:
            evicted = False
            for cand_key in list(self.sessions):
                if await self._evict_session_key(cand_key, service, context["thread_id"]):
                    evicted = True
                    break
            if not evicted:
                raise RuntimeError("当前活动 MCP 会话已达上限且均在使用中")
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
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "PYTHONUTF8",
            "PYTHONIOENCODING",
        }:
            raise ValueError("MCP 凭据不能覆盖启动环境")
        cred_values = {}
        for name, handle in credentials.items():
            cred_values[name] = await completed_thread(
                self.vault.for_execution, handle
            )
        home = Path(tempfile.mkdtemp(prefix="maisya-mcp-"))
        uid = self._allocate_uid()
        try:
            safe_chmod(home, 0o700)
            if sys.platform.startswith("linux") and os.geteuid() == 0:
                os.chown(home, uid, uid)
            environment = minimal_subprocess_env(home=home)
            environment.update(cred_values)
            memory = config.get("memory_mib", 256)
            if type(memory) is not int or not 128 <= memory <= 4096:
                raise ValueError("MCP 资源配置无效")
            if sys.platform.startswith("linux") and os.geteuid() == 0:
                async with self.group_lock:
                    await completed_thread(self._prepare_group)
            if os.name == "nt":
                child = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=environment,
                    cwd=home,
                    limit=12 * 1024**2,
                )
            else:
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
        except BaseException:
            self.active_uids.discard(uid)
            await completed_thread(rmtree_safe, home, ignore_errors=True)
            raise
        cgroup = (
            Path("/sys/fs/cgroup") / self.cgroup_name / str(uid)
            if sys.platform.startswith("linux") and os.geteuid() == 0
            else None
        )
        session = StdioSession(
            child, home, cgroup, uid=uid, on_close=self.active_uids.discard
        )
        try:
            response = await session.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "maisya", "version": "0.2.0"},
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
        try:
            (base / "memory.max").write_text(str(512 * 1024**2), encoding="utf-8")
            (base / "pids.max").write_text("128", encoding="utf-8")
            (base / "cpu.max").write_text("100000 100000", encoding="utf-8")
            (base / "cgroup.subtree_control").write_text(
                "+memory +pids +cpu", encoding="utf-8"
            )
        except BaseException:
            try:
                base.rmdir()
            except OSError:
                pass
            raise

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
        if (
            profile.environment == "readonly"
            and not config.get("readonly", False)
            and not (
                profile.access == "admins"
                and await completed_thread(runtime.authorized, context)
            )
        ):
            raise PermissionError("只读普通档案不能调用可写 MCP 服务")
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
            raw_call_id = context["_tool_call_id"]
            step_index = context.get("_step_index")
            receipt_call_id = (
                f"s{step_index}:{raw_call_id}"
                if step_index is not None
                else raw_call_id
            )
            key = context["thread_id"], context["revision"], receipt_call_id
            previous = await completed_thread(runtime.sandbox._receipt, key)
            if previous and previous.get("status") not in {
                "running",
                "interrupted",
                "uncertain",
            }:
                try:
                    prev_result = json.loads(previous.get("result") or "{}")
                except (TypeError, ValueError):
                    prev_result = {}
                if isinstance(prev_result, dict) and prev_result:
                    return {
                        "status": previous["status"],
                        **prev_result,
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
            if not isinstance(item, dict):
                raise ValueError("MCP 返回内容项格式无效")
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

    @asynccontextmanager
    async def mount_secrets(self, runtime, handles, *, work=None):
        secret_directory = Path(
            tempfile.mkdtemp(prefix="secrets-", dir=runtime.files.runtime)
        )
        secret_volume = runtime.sandbox.volume(secret_directory)
        mounted = False
        try:
            await completed_thread(secret_volume.__enter__)
            mounted = True
            secrets = {}
            for handle in handles:
                value = await completed_thread(self.vault.for_execution, handle)
                path = secret_directory / handle
                await completed_thread(path.write_text, value, encoding="utf-8")
                if sys.platform.startswith("linux") and os.geteuid() == 0:
                    await completed_thread(os.chown, path, 10000, 10000)
                await completed_thread(safe_chmod, path, 0o400)
                secrets[handle] = path
            yield secrets
        finally:
            if mounted and work is not None and work.quarantined:
                self.quarantines.append((secret_volume, secret_directory))
            elif mounted:
                await completed_thread(secret_volume.__exit__, None, None, None)
                await completed_thread(rmtree_safe, secret_directory)
            else:
                await completed_thread(rmtree_safe, secret_directory)

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
            or not all(isinstance(value, str) and "\x00" not in value for value in program)
            or not _is_native_absolute_program_path(program[0])
        ):
            raise ValueError("凭据模板程序必须是有效的绝对路径")
        if set(profile.secrets) != set(config.get("secrets", [])):
            raise PermissionError("档案凭据与固定模板绑定不一致")
        targets = config.get("targets") or list(profile.sandbox_targets)
        endpoints = await completed_thread(validate_endpoint_targets, targets)
        blank = {**context, "workspace_id": "ws_" + os.urandom(12).hex()}
        manager = runtime.files.activity(blank)
        work = await completed_thread(manager.__enter__)
        try:
            async with self.mount_secrets(runtime, profile.secrets, work=work) as secrets:
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
            await completed_thread(manager.__exit__, None, None, None)

    async def release_quarantines(self):
        for volume, path in list(self.quarantines):
            await completed_thread(volume.__exit__, None, None, None)
            await completed_thread(rmtree_safe, path)
            self.quarantines.remove((volume, path))

    async def close_thread(self, thread_id):
        for key in list(self.sessions):
            if key[3] == thread_id:
                await self._close_session_key_safe(key)

    async def close(self):
        self.closed = True
        await self.close_sessions()
        cgroup = Path("/sys/fs/cgroup") / self.cgroup_name
        if sys.platform.startswith("linux") and cgroup.exists():
            try:
                await completed_thread(cgroup.rmdir)
            except OSError:
                pass

    async def close_sessions(self):
        for key in list(self.sessions):
            await self._close_session_key_safe(key)
        self.sessions.clear()

    @staticmethod
    def apply_web_mapping(arguments, binding):
        defaults = dict((binding or {}).get("defaults") or {})
        arg_map = (binding or {}).get("arg_map") or {}
        if not arg_map:
            return {**defaults, **arguments}
        mapped = dict(defaults)
        for src, dst in arg_map.items():
            if src in arguments:
                mapped[dst] = arguments[src]
            elif src == "limit" and "max_results" in arguments:
                mapped[dst] = arguments["max_results"]
            elif src == "max_results" and "limit" in arguments:
                mapped[dst] = arguments["limit"]
        for key, value in arguments.items():
            if key in arg_map:
                continue
            if key in {"limit", "max_results"} and (
                "limit" in arg_map or "max_results" in arg_map
            ):
                continue
            mapped.setdefault(key, value)
        return mapped

    @staticmethod
    def _prepare_web_args(kind, arguments, schema, binding=None):
        call_args = Services.apply_web_mapping(arguments, binding)
        if (
            kind == "search"
            and isinstance(schema, dict)
            and schema.get("additionalProperties") is False
            and "max_results" not in (schema.get("properties") or {})
        ):
            call_args.pop("max_results", None)
        return call_args

    async def _call_web_mcp(self, service, tool, kind, arguments, context, runtime, config, *, binding=None):
        if (
            not isinstance(config, dict)
            or tool not in config.get("tools", [])
            or (
                config.get("readonly") is not True
                and not (
                    config.get("access") == "admins"
                    and runtime is not None
                    and await completed_thread(runtime.authorized, context)
                )
            )
        ):
            raise PermissionError("联网 MCP 服务仅允许只读工具或管理员显式授权工具")
        if config.get("access", "everyone") == "admins":
            if context.get("owner") == "self" or runtime is None or not await completed_thread(runtime.authorized, context):
                raise PermissionError("该联网 MCP 服务需要当前管理员授权")
        thread_id = context.get("thread_id") or "web"
        lock = self.locks.setdefault((service, thread_id), asyncio.Lock())
        async with lock:
            session = await self._session(
                service,
                {"chat_id": context.get("chat_id", ""), "thread_id": thread_id},
                config,
            )
            if tool not in session.tools:
                raise PermissionError("MCP 未提供登记的联网工具")
            schema = session.tools[tool].get("inputSchema", {})
            call_args = self._prepare_web_args(kind, arguments, schema, binding)
            await completed_thread(validate, schema, call_args)
            try:
                result = await session.request(
                    "tools/call", {"name": tool, "arguments": call_args}
                )
                if result.get("isError"):
                    raise RuntimeError("MCP 联网工具执行失败")
            except BaseException:
                for session_key in list(self.sessions):
                    if self.sessions[session_key] is session:
                        self.sessions.pop(session_key)
                await session.close()
                raise
        parts = []
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(self.vault.redact(item.get("text", "")))[:16000])
            elif (
                isinstance(item, dict)
                and item.get("type") == "resource"
                and isinstance(item.get("resource"), dict)
                and "text" in item["resource"]
            ):
                parts.append(str(self.vault.redact(item["resource"]["text"]))[:16000])
        joined = "\n".join(parts).strip()
        try:
            parsed = json.loads(joined)
            if isinstance(parsed, dict):
                return self.vault.redact(parsed)
        except ValueError:
            pass
        if kind == "fetch":
            return {
                "url": arguments["url"],
                "title": arguments["url"],
                "content": joined[:16000],
                "content_kind": "page_text",
                "service": f"mcp:{service}:{tool}",
            }
        return {
            "query": arguments["query"],
            "results": [],
            "summary": joined[:16000],
            "content_kind": "search_results",
            "service": f"mcp:{service}:{tool}",
        }

    async def _call_web_plugin_api(self, label, spec, kind, arguments, context, runtime, *, binding=None):
        if not isinstance(spec, dict):
            raise PermissionError(f"未找到联网 API {label}")
        if (
            spec.get("readonly") is not True
            and spec.get("access") != "admins"
        ):
            raise PermissionError("联网 API 需要只读声明或管理员授权")
        if spec.get("access", "everyone") == "admins":
            if context.get("owner") == "self" or runtime is None or not await completed_thread(runtime.authorized, context):
                raise PermissionError("该联网 API 需要当前管理员授权")
        call_args = self._prepare_web_args(kind, arguments, spec.get("schema"), binding)
        if "schema" in spec:
            await completed_thread(validate, spec["schema"], call_args)
        if runtime is None or runtime.host is None:
            raise RuntimeError("跨插件 API 调用缺少宿主上下文")
        raw = await call_capability(
            runtime.host,
            "api.call",
            timeout_ms=30000,
            api_name=spec["api_name"],
            version=spec.get("version", ""),
            args=call_args,
        )
        value = self.vault.redact(_unwrap_api_result(raw, "result"))
        if isinstance(value, dict):
            items = value.get("items") if isinstance(value.get("items"), list) else value.get("results")
            return {
                "provider": spec["api_name"],
                **value,
                **({"items": items, "results": items} if isinstance(items, list) else {}),
            }
        if kind == "fetch":
            return {
                "provider": spec["api_name"],
                "url": arguments["url"],
                "title": arguments["url"],
                "content": str(value)[:16000],
                "content_kind": "page_text",
                "service": f"plugin_api:{label}",
            }
        return {
            "provider": spec["api_name"],
            "query": arguments["query"],
            "items": [],
            "results": [],
            "summary": str(value)[:16000],
            "content_kind": "search_results",
            "service": f"plugin_api:{label}",
        }

    async def call_web(self, kind, arguments, context, runtime, *, mode="any"):
        if self.closed:
            raise RuntimeError("能力服务正在关闭")
        configuration = self.load()
        web_cfg = configuration.get("web") or {}
        if mode.startswith("mcp:"):
            service = mode.split(":", 1)[1]
            config = (configuration.get("servers") or {}).get(service)
            if not isinstance(config, dict) or not config.get("tools"):
                raise RuntimeError(f"未登记 MCP 服务 {service}")
            tool = next(
                (t for t in config["tools"] if kind in t.lower()),
                config["tools"][0],
            )
            return await self._call_web_mcp(
                service, tool, kind, arguments, context, runtime, config
            )
        if mode.startswith("plugin_api:"):
            api_id = mode.split(":", 1)[1]
            spec = (configuration.get("apis") or {}).get(api_id)
            return await self._call_web_plugin_api(
                api_id, spec, kind, arguments, context, runtime
            )
        binding = web_cfg.get(kind)
        if not isinstance(binding, dict):
            if mode == "any":
                return None
            raise RuntimeError(f"services.web 尚未绑定 {kind}")
        if "service" in binding and "tool" in binding:
            if mode not in {"any", "mcp"}:
                raise RuntimeError(f"services.web.{kind} 未绑定为 mcp 模式")
            service, tool = binding["service"], binding["tool"]
            config = (configuration.get("servers") or {}).get(service)
            return await self._call_web_mcp(
                service, tool, kind, arguments, context, runtime, config, binding=binding
            )
        if "api" in binding:
            if mode not in {"any", "plugin_api"}:
                raise RuntimeError(f"services.web.{kind} 未绑定为 plugin_api 模式")
            api_id = binding["api"]
            spec = (configuration.get("apis") or {}).get(api_id)
            return await self._call_web_plugin_api(
                api_id, spec, kind, arguments, context, runtime, binding=binding
            )
        raise ValueError("未知联网服务绑定方式")
