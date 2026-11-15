"""固定运行时、受控出站与单次容器执行边界。"""

import asyncio
import errno
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .pathsafe import (
    acquire_lock_handle,
    minimal_subprocess_env,
    path_is_link_like,
    release_lock_handle,
    rmtree_safe,
    safe_chmod,
)
from .workspaces import completed_thread


async def _uninterruptible_await(awaitable):
    """在重复取消下等待关键清理任务彻底完成，返回 `(result, saw_cancel)`。"""
    task = asyncio.ensure_future(awaitable)
    saw_cancel = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            saw_cancel = True
    return task.result(), saw_cancel

DENIED_V4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)
MINIMAL_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
}


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    image: str = ""
    socket: str = "unix:///var/run/docker.sock"
    runtime: str = "runsc"
    subnet: str = "172.30.253.0/24"
    dns: list[str] = Field(default_factory=lambda: ["1.1.1.1"])
    host_addresses: list[str] = Field(default_factory=list)
    memory_mib: int = Field(default=512, ge=128, le=4096)
    work_mib: int = Field(default=256, ge=16, le=2048)
    timeout: int = Field(default=60, ge=1, le=180)
    output_bytes: int = Field(default=2097152, ge=1024, le=8388608)
    max_concurrent: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def validate_boundary(self):
        if not re.fullmatch(
            r"unix:///[A-Za-z0-9_./-]+", self.socket
        ) or ".." in self.socket.split("/"):
            raise ValueError("Docker 连接只接受本机 Unix socket")
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,40}", self.runtime)
            or self.runtime == "runc"
        ):
            raise ValueError("沙箱只接受专用隔离运行时")
        if self.enabled and not re.fullmatch(
            r"(?:[A-Za-z0-9][A-Za-z0-9._/:_-]*@)?sha256:[a-f0-9]{64}", self.image
        ):
            raise ValueError("启用沙箱需要固定 digest 的镜像")
        subnet = ipaddress.ip_network(self.subnet, strict=True)
        if subnet.version != 4 or subnet.prefixlen != 24 or not subnet.is_private:
            raise ValueError("沙箱子网必须是独立的小型私有 IPv4 网段")
        if not self.dns or any(
            not ipaddress.ip_address(value).is_global
            or ipaddress.ip_address(value).version != 4
            for value in self.dns
        ):
            raise ValueError("DNS 必须为明确的公网 IPv4 解析器")
        for value in self.host_addresses:
            ipaddress.ip_address(value)
        return self


class NetworkPolicy:
    def __init__(self, mode, *, host_addresses=(), dns=("1.1.1.1",), targets=()):
        if mode not in {"public", "none", "restricted"}:
            raise ValueError("未知网络策略")
        self.mode = mode
        self.host_addresses = {ipaddress.ip_address(value) for value in host_addresses}
        self.dns = {ipaddress.ip_address(value) for value in dns}
        self.targets = {
            (ipaddress.ip_address(address), int(port)) for address, port in targets
        }
        if any(
            address.version != 4 or not 1 <= port <= 65535
            for address, port in self.targets
        ):
            raise ValueError("限定目标无效")

    def permits(self, address, port, protocol):
        address = ipaddress.ip_address(address)
        if self.mode == "none" or address.version != 4:
            return False
        if self.mode == "restricted":
            return protocol == "tcp" and (address, port) in self.targets
        if not address.is_global or address in self.host_addresses:
            return False
        if port == 53:
            return address in self.dns and protocol in {"tcp", "udp"}
        return (
            protocol == "tcp"
            and 1 <= port <= 65535
            or protocol == "udp"
            and port == 53
            and address in self.dns
        )

    def nft_rules(self, table, bridge, subnet):
        if not re.fullmatch(r"ms_[a-z0-9_]{1,20}", table) or not re.fullmatch(
            r"br-ms-[a-z0-9-]{1,9}", bridge
        ):
            raise ValueError("内部网络标识无效")
        subnet = str(ipaddress.IPv4Network(subnet, strict=True))
        deny = list(DENIED_V4) + [
            str(address) + "/32"
            for address in self.host_addresses
            if address.version == 4
        ]
        blocked = ", ".join(
            str(network)
            for network in ipaddress.collapse_addresses(
                ipaddress.ip_network(value) for value in deny
            )
        )
        dns = ", ".join(str(address) for address in sorted(self.dns))
        if self.mode == "public":
            outgoing = f"ip daddr {{ {blocked} }} drop; tcp dport 53 ip daddr != {{ {dns} }} drop; meta l4proto tcp accept; ip daddr {{ {dns} }} udp dport 53 accept; drop;"
        elif self.mode == "restricted":
            outgoing = (
                " ".join(
                    f"ip daddr {address} tcp dport {port} accept;"
                    for address, port in sorted(self.targets)
                )
                + " drop;"
            )
        else:
            outgoing = "drop;"
        return f'''table inet {table} {{
 chain outgoing {{ meta nfproto ipv6 drop; ip saddr != {subnet} drop; {outgoing} }}
 chain before_nat {{ type filter hook prerouting priority -150; policy accept; iifname "{bridge}" jump outgoing; }}
 chain host_input {{ type filter hook input priority -150; policy accept; iifname "{bridge}" drop; }}
 chain forwarding {{ type filter hook forward priority -150; policy accept; iifname "{bridge}" oifname "{bridge}" drop; iifname "{bridge}" jump outgoing; oifname "{bridge}" meta nfproto ipv6 drop; oifname "{bridge}" ct state established,related accept; oifname "{bridge}" drop; }}
 chain source_nat {{ type nat hook postrouting priority 90; policy accept; iifname "{bridge}" ip saddr {subnet} oifname != "{bridge}" masquerade; }}
}}
table bridge {table} {{
 chain peers {{ type filter hook forward priority -150; policy accept; meta ibrname "{bridge}" drop; }}
}}
'''


@dataclass
class ProcessResult:
    code: int
    output: str


async def process(arguments, *, timeout=20, output_bytes=2097152, stdin=None):
    child = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.PIPE
        if stdin is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=minimal_subprocess_env(),
    )
    chunks, size = [], 0

    async def collect():
        nonlocal size
        if stdin is not None:
            child.stdin.write(stdin)
            await child.stdin.drain()
            child.stdin.close()
        while chunk := await child.stdout.read(16384):
            size += len(chunk)
            if size > output_bytes:
                raise RuntimeError("执行输出超过上限")
            chunks.append(chunk)
        await child.wait()

    try:
        async with asyncio.timeout(timeout):
            await collect()
    except BaseException:
        if child.returncode is None:
            child.kill()
        await child.wait()
        raise
    return ProcessResult(
        child.returncode, b"".join(chunks).decode("utf-8", errors="replace")
    )


class Sandbox:
    def __init__(
        self,
        settings,
        store,
        *,
        runner=process,
        redact=lambda value: value,
        authorized=lambda row: False,
    ):
        self.settings, self.store, self.runner = settings, store, runner
        self.redact, self.authorized = redact, authorized
        self.instance = uuid.uuid4().hex[:8]
        self.network = "maisya-" + self.instance
        self.bridge = "br-ms-" + self.instance
        self.table = "ms_" + self.instance
        self.networks = {}
        self.containers = {}
        self._network_lock = asyncio.Lock()
        self._active_networks = {}
        self._container_networks = {}
        self.ready = False
        self.closing = False
        self.instance_lock = None
        self.error = "沙箱未启用"
        self.slot = asyncio.Semaphore(settings.max_concurrent)
        with store.connect() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS executions (thread_id TEXT, revision INTEGER, call_id TEXT, container TEXT, status TEXT, result TEXT, PRIMARY KEY(thread_id, revision, call_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS execution_resources (kind TEXT,name TEXT PRIMARY KEY,data TEXT)"
            )

    def _resource(self, kind, name, data):
        with self.store.connect() as db, db:
            db.execute(
                "INSERT OR REPLACE INTO execution_resources VALUES (?,?,?)",
                (kind, name, json.dumps(data)),
            )

    def _forget(self, name):
        with self.store.connect() as db, db:
            db.execute("DELETE FROM execution_resources WHERE name=?", (name,))

    def _resources(self):
        with self.store.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM execution_resources")
            ]

    def _lock_instance(self):
        if self.instance_lock is not None:
            return
        try:
            self.instance_lock = acquire_lock_handle(
                self.store.path.parent / ".sandbox.lock",
                nonblocking=True,
            )
        except BlockingIOError:
            raise RuntimeError("同一数据目录已有沙箱执行器运行") from None

    async def _recover_resources(self):
        resources = await completed_thread(self._resources)
        for resource in sorted(
            resources,
            key=lambda item: {"container": 0, "mount": 1, "network": 2}.get(
                item["kind"], 3
            ),
        ):
            data = json.loads(resource["data"])
            if data.get("socket", self.settings.socket) != self.settings.socket:
                raise RuntimeError(
                    "旧执行资源属于其他 Docker socket，请先恢复原配置完成清理"
                )
            if resource["kind"] == "container":
                if not re.fullmatch(
                    r"maisya-[a-f0-9]{8}-[a-f0-9]{12}", resource["name"]
                ) or not await self._terminate(resource["name"]):
                    raise RuntimeError(
                        "上次执行的容器清理未确认，保留工作副本与网络隔离"
                    )
                await completed_thread(self._forget, resource["name"])
            elif resource["kind"] == "mount":
                path = Path(resource["name"])
                runtime = self.store.path.parent / "workspaces/runtime"
                if path.parent != runtime or path_is_link_like(path):
                    raise RuntimeError("旧工作副本路径无效，已拒绝自动清理")
                observed = await self.runner(
                    [
                        "findmnt",
                        "--mountpoint",
                        str(path),
                        "--noheadings",
                        "--output",
                        "SOURCE",
                    ]
                )
                if observed.code == 0:
                    if observed.output.strip() != data["source"]:
                        raise RuntimeError("旧挂载来源变化，已拒绝自动清理")
                    if (await self.runner(["umount", str(path)])).code:
                        raise RuntimeError("旧工作副本卸载失败，保留目录")
                elif observed.code != 1:
                    raise RuntimeError("无法确认旧工作副本挂载状态")
                if path.exists():
                    await completed_thread(rmtree_safe, path)
                await completed_thread(self._forget, resource["name"])
            elif resource["kind"] == "network":
                if not re.fullmatch(r"maisya-[a-f0-9]{8}-[0-7]", data.get("name", "")):
                    raise RuntimeError("旧隔离网络标识无效")
                await self._remove_network(data)
            else:
                raise RuntimeError("无法识别旧执行资源")

    async def docker(self, *arguments, timeout=20, checked=True, **kwargs):
        result = await self.runner(
            ["docker", "--host", self.settings.socket, *arguments],
            timeout=timeout,
            **kwargs,
        )
        if checked and result.code:
            raise RuntimeError("容器运行时操作失败：" + arguments[0])
        return result

    async def probe(self):
        if not self.settings.enabled and not await completed_thread(self._resources):
            return False
        if (
            not sys.platform.startswith("linux")
            or os.geteuid() != 0
            or any(
                not shutil.which(name)
                for name in ("docker", "nft", "mount", "umount", "ip", "findmnt")
            )
        ):
            self.error = "沙箱需要 Linux、本机 Docker、nft 和受限挂载权限"
            return False
        try:
            await completed_thread(self._lock_instance)
            info = json.loads(
                (await self.docker("info", "--format", "{{json .}}")).output
            )
            if (
                self.settings.runtime not in info.get("Runtimes", {})
                or info.get("CgroupVersion") != "2"
            ):
                raise RuntimeError("缺少专用运行时或 cgroup v2")
            if (await self.runner(["nft", "list", "tables"])).code:
                raise RuntimeError("无法读取防火墙规则")
            await self._recover_resources()
            if not self.settings.enabled:
                return False
            await self.docker("image", "inspect", self.settings.image)
            self.ready, self.error = True, ""
        except (RuntimeError, OSError, ValueError) as error:
            self.ready, self.error = False, str(error)
        return self.ready

    @contextmanager
    def volume(self, path):
        if not self.ready or self.closing:
            raise RuntimeError(self.error or "沙箱尚未就绪")
        self._admit(path)
        options = f"size={self.settings.work_mib}m,nr_inodes=8192,nosuid,nodev,noswap,mode=0700"
        try:
            subprocess.run(
                [
                    "mount",
                    "-t",
                    "tmpfs",
                    "-o",
                    options,
                    "maisya-" + self.instance,
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=10,
                env=minimal_subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["umount", str(path)],
                check=False,
                capture_output=True,
                timeout=5,
                env=minimal_subprocess_env(),
            )
            self._forget(str(path))
            raise
        try:
            self._resource(
                "mount",
                str(path),
                {"source": "maisya-" + self.instance, "socket": self.settings.socket},
            )
            yield
        finally:
            subprocess.run(
                ["umount", str(path)],
                check=True,
                capture_output=True,
                timeout=10,
                env=minimal_subprocess_env(),
            )
            self._forget(str(path))

    def _admit(self, path=None):
        if not sys.platform.startswith("linux"):
            return
        memory = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
        )
        available = int(memory["MemAvailable"].split()[0]) * 1024
        reserve = (self.settings.memory_mib + self.settings.work_mib + 1024) * 1024**2
        if available < reserve:
            raise RuntimeError("可用内存不足，执行器为宿主保留 1 GiB 后暂缓执行")
        if path is not None and shutil.disk_usage(Path(path).parent).free < 2 * 1024**3:
            raise RuntimeError("工作区盘剩余空间不足 2 GiB")

    async def _network(self, mode, targets=(), hold=False):
        if mode == "none":
            return "none"
        targets = () if targets is None else targets
        key = json.dumps([mode, targets], sort_keys=True)
        async with self._network_lock:
            if key in self.networks:
                expected = self.networks.pop(key)
                self.networks[key] = expected
                await self._check_network(expected)
                if hold:
                    name = expected["name"]
                    self._active_networks[name] = self._active_networks.get(name, 0) + 1
                return expected["name"]
            if len(self.networks) >= 8:
                in_use_names = set(self._container_networks.values())
                evict_key = None
                evict_entry = None
                for cand_key, cand_entry in self.networks.items():
                    cname = cand_entry["name"]
                    if (
                        self._active_networks.get(cname, 0) == 0
                        and cname not in in_use_names
                    ):
                        evict_key = cand_key
                        evict_entry = cand_entry
                        break
                if evict_entry is None:
                    raise RuntimeError("本次运行的网络策略过多")
                await self._remove_network(evict_entry)
                self.networks.pop(evict_key, None)
            used_slots = {
                item["slot"]
                for item in self.networks.values()
                if isinstance(item.get("slot"), int)
            }
            suffix = next(
                (i for i in range(8) if i not in used_slots),
                len(self.networks),
            )
            name = self.network + "-" + str(suffix)
            bridge = "br-ms-" + self.instance[:6] + str(suffix)
            table = self.table + "_" + str(suffix)
            base = ipaddress.ip_network(self.settings.subnet)
            subnet = (
                str(list(base.subnets(new_prefix=28))[suffix])
                if base.prefixlen < 28
                else str(base)
            )
            routes = await self.runner(["ip", "-j", "route", "show", "table", "all"])
            if routes.code:
                raise RuntimeError("无法核实沙箱子网是否冲突")
            candidate = ipaddress.ip_network(subnet)
            for route in json.loads(routes.output):
                destination = route.get("dst", "default")
                if destination != "default" and candidate.overlaps(
                    ipaddress.ip_network(destination, strict=False)
                ):
                    raise RuntimeError("沙箱子网与现有路由冲突，请配置独立网段")
            addresses = json.loads(
                (await self.runner(["ip", "-j", "address", "show"])).output
            )
            host_addresses = list(self.settings.host_addresses) + [
                item["local"]
                for interface in addresses
                for item in interface.get("addr_info", [])
            ]
            policy = NetworkPolicy(
                mode,
                host_addresses=host_addresses,
                dns=self.settings.dns,
                targets=targets,
            )
            rules = policy.nft_rules(table, bridge, subnet)
            entry = {
                "name": name,
                "bridge": bridge,
                "table": table,
                "subnet": subnet,
                "rules": rules,
                "slot": suffix,
            }
            self.networks[key] = entry
            await completed_thread(
                self._resource,
                "network",
                name,
                {**entry, "socket": self.settings.socket},
            )
            try:
                result = await self.runner(["nft", "-f", "-"], stdin=rules.encode())
                if result.code:
                    raise RuntimeError("无法建立出站防火墙")
                await self._legacy_prepare(entry)
                await completed_thread(
                    self._resource,
                    "network",
                    name,
                    {**entry, "socket": self.settings.socket},
                )
                await self.docker(
                    "network",
                    "create",
                    "--driver",
                    "bridge",
                    "--ipv6=false",
                    "--subnet",
                    subnet,
                    "--opt",
                    "com.docker.network.bridge.name=" + bridge,
                    "--opt",
                    "com.docker.network.bridge.enable_icc=false",
                    "--label",
                    "maisya.instance=" + self.instance,
                    name,
                )
                await self._check_network(entry)
            except BaseException:
                await self._remove_network(entry)
                self.networks.pop(key, None)
                raise
            if hold:
                self._active_networks[name] = self._active_networks.get(name, 0) + 1
            return name

    async def _check_network(self, entry):
        observed = {}
        for family in ("inet", "bridge"):
            result = await self.runner(["nft", "list", "table", family, entry["table"]])
            if result.code or entry["bridge"] not in result.output:
                raise RuntimeError("网络隔离规则缺失，已拒绝执行")
            observed[family] = hashlib.sha256(result.output.encode()).hexdigest()
        if entry.get("fingerprint") and entry["fingerprint"] != observed:
            raise RuntimeError("网络隔离规则被更改，已拒绝执行")
        entry["fingerprint"] = observed
        if entry.get("legacy"):
            legacy = entry["legacy"]
            for direction in ("-i", "-o"):
                result = await self.runner(
                    [
                        "iptables",
                        "-w",
                        "5",
                        "-C",
                        legacy["hook"],
                        direction,
                        entry["bridge"],
                        "-j",
                        legacy["chain"],
                    ]
                )
                if result.code:
                    raise RuntimeError("网络转发隔离入口缺失，已拒绝执行")

    async def _legacy_prepare(self, entry):
        if not shutil.which("iptables"):
            return
        hook = (
            "DOCKER-USER"
            if (await self.runner(["iptables", "-w", "5", "-S", "DOCKER-USER"])).code
            == 0
            else "FORWARD"
        )
        slot = entry.get("slot")
        chain_suffix = str(slot) if isinstance(slot, int) else str(max(0, len(self.networks) - 1))
        chain = "MS" + self.instance + chain_suffix
        entry["legacy"] = {"hook": hook, "chain": chain}
        await completed_thread(
            self._resource,
            "network",
            entry["name"],
            {**entry, "socket": self.settings.socket},
        )
        operations = [
            ["-N", chain],
            ["-A", chain, "-j", "ACCEPT"],
            ["-I", hook, "1", "-i", entry["bridge"], "-j", chain],
            ["-I", hook, "1", "-o", entry["bridge"], "-j", chain],
        ]
        for arguments in operations:
            if (await self.runner(["iptables", "-w", "5", *arguments])).code:
                raise RuntimeError("无法接入现有转发策略")

    async def _remove_network(self, entry):
        await self.docker("network", "rm", entry["name"], checked=False)
        remaining = await self.docker(
            "network",
            "ls",
            "--filter",
            "name=^" + entry["name"] + "$",
            "--format",
            "{{.Name}}",
            checked=False,
        )
        if remaining.code or remaining.output.strip():
            raise RuntimeError("沙箱网络清理未确认，保留隔离规则")
        legacy = entry.get("legacy")
        if legacy:
            for direction in ("-i", "-o"):
                await self.runner(
                    [
                        "iptables",
                        "-w",
                        "5",
                        "-D",
                        legacy["hook"],
                        direction,
                        entry["bridge"],
                        "-j",
                        legacy["chain"],
                    ]
                )
            await self.runner(["iptables", "-w", "5", "-F", legacy["chain"]])
            await self.runner(["iptables", "-w", "5", "-X", legacy["chain"]])
        for family in ("inet", "bridge"):
            await self.runner(["nft", "delete", "table", family, entry["table"]])
        remaining_rules = await self.runner(["nft", "list", "tables"])
        if remaining_rules.code or re.search(
            r"\btable\s+(?:inet|bridge)\s+" + re.escape(entry["table"]) + r"\b",
            remaining_rules.output,
        ):
            raise RuntimeError("隔离规则清理未确认，保留恢复记录")
        await completed_thread(self._forget, entry["name"])

    def _receipt(self, key, *, container="", status="", result=None):
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM executions WHERE thread_id=? AND revision=? AND call_id=?",
                key,
            ).fetchone()
            if status:
                db.execute(
                    "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(thread_id,revision,call_id) DO UPDATE SET container=CASE WHEN excluded.container!='' THEN excluded.container ELSE executions.container END, status=excluded.status, result=excluded.result",
                    (
                        *key,
                        container,
                        status,
                        json.dumps(result or {}, ensure_ascii=False),
                    ),
                )
            return dict(old) if old else None

    @staticmethod
    def _write_resolv_file(path: Path, dns_servers) -> None:
        path.write_text(
            "".join(f"nameserver {resolver}\n" for resolver in dns_servers),
            encoding="utf-8",
        )
        safe_chmod(path, 0o644)

    @staticmethod
    def _normalize_host_mappings(host_mappings, targets) -> list[tuple[str, str]]:
        if not host_mappings:
            return []
        if not isinstance(host_mappings, dict):
            raise ValueError("域名映射配置无效")
        allowed_ips = {
            str(ipaddress.ip_address(item[0]))
            for item in (targets or ())
            if isinstance(item, (tuple, list)) and len(item) >= 1
        }
        normalized: list[tuple[str, str]] = []
        for host, ip_raw in host_mappings.items():
            if (
                not isinstance(host, str)
                or not isinstance(ip_raw, str)
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host)
                or ".." in host
            ):
                raise ValueError("受限域名映射的主机名无效")
            addr = str(ipaddress.ip_address(ip_raw.strip()))
            if allowed_ips and addr not in allowed_ips:
                raise ValueError("受限域名映射的 IP 不在目标放行清单中")
            normalized.append((host.lower(), addr))
        return normalized

    def _own(self, work):
        def _ensure_owner(target: Path, *, follow_symlinks: bool) -> None:
            info = os.lstat(target)
            if info.st_uid == 10000 and info.st_gid == 10000:
                return
            try:
                os.chown(target, 10000, 10000, follow_symlinks=follow_symlinks)
            except PermissionError as error:
                raise PermissionError(
                    "无法将沙箱工作区权限调整为 10000:10000（当前宿主进程缺少 chown 权限）"
                ) from error
            except OSError as error:
                if error.errno == errno.EROFS:
                    return
                if error.errno == errno.EPERM:
                    raise PermissionError(
                        "无法将沙箱工作区权限调整为 10000:10000（当前宿主进程缺少 chown 权限）"
                    ) from error
                raise

        for directory, _children, files in os.walk(work.path):
            _ensure_owner(Path(directory), follow_symlinks=False)
            for name in files:
                _ensure_owner(Path(directory) / name, follow_symlinks=False)

    async def execute(
        self,
        row,
        work,
        command,
        *,
        call_id,
        timeout=None,
        mode="public",
        targets=(),
        secrets=None,
        entrypoint=None,
        step_index=None,
        host_mappings=None,
    ):
        if not self.ready or self.closing:
            raise RuntimeError(self.error or "沙箱正在关闭")
        if not isinstance(command, str) or not command.strip() or len(command) > 16000:
            raise ValueError("脚本命令无效")
        receipt_call_id = (
            f"step:{int(step_index)}:{call_id}" if step_index is not None else call_id
        )
        key = row["thread_id"], row["revision"], receipt_call_id
        async with self.slot:
            if not self.ready or self.closing:
                raise RuntimeError("执行器已停止")
            await completed_thread(self._admit)
            current = await asyncio.to_thread(
                self.store.get, row["thread_id"], row["chat_id"]
            )
            if (
                current["state"] == "closed"
                or mode == "restricted"
                and not await completed_thread(self.authorized, row)
            ):
                raise PermissionError("线程已停止或本次受限执行授权已失效")
            old = await asyncio.to_thread(self._receipt, key)
            if old:
                try:
                    old_result = json.loads(old["result"]) if old["result"] else {}
                except ValueError:
                    old_result = {}
                old_status = old["status"]
                if old_status in {
                    "uncertain",
                    "running",
                    "cleanup_unconfirmed",
                } or (
                    old_status == "completed"
                    and isinstance(old_result, dict)
                    and bool(old_result)
                ):
                    return {
                        "status": old_status,
                        **(old_result if isinstance(old_result, dict) else {}),
                        "repeated": True,
                    }
            validated_mappings = (
                self._normalize_host_mappings(host_mappings, targets)
                if mode == "restricted"
                else []
            )
            for handle in secrets or {}:
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", handle):
                    raise ValueError("凭据句柄无效")
            targets_arg = () if targets is None else targets
            network = await self._network(mode, targets_arg, True)
            active_network_ref = network if network != "none" else ""
            container = "maisya-" + self.instance + "-" + uuid.uuid4().hex[:12]
            entered_container_try = False
            try:
                await completed_thread(self._own, work)
                arguments = [
                    "create",
                    "--pull",
                    "never",
                    "--name",
                    container,
                    "--label",
                    "maisya.instance=" + self.instance,
                    "--runtime",
                    self.settings.runtime,
                    "--network",
                    network,
                    "--read-only",
                    "--user",
                    "10000:10000",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "128",
                    "--ulimit",
                    "core=0:0",
                    "--ulimit",
                    "nofile=256:256",
                    "--cpus",
                    "1",
                    "--memory",
                    str(self.settings.memory_mib) + "m",
                    "--memory-swap",
                    str(self.settings.memory_mib) + "m",
                    "--log-driver",
                    "none",
                    "--workdir",
                    "/work/project",
                    "--env",
                    "HOME=/work/cache",
                    "--env",
                    "PYTHONNOUSERSITE=1",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=64m,uid=10000,gid=10000",
                ]
                for name in ("project", "in", "out", "cache"):
                    arguments += [
                        "--mount",
                        f"type=bind,src={work.path / name},dst=/work/{name}"
                        + (",readonly" if name == "in" else ""),
                    ]
                if mode == "restricted":
                    for host, addr in validated_mappings:
                        arguments += ["--add-host", f"{host}:{addr}"]
                elif network != "none":
                    for resolver in self.settings.dns:
                        arguments += ["--dns", resolver]
                    # gVisor 沙箱够不到宿主的 127.0.0.11 embedded resolver（gVisor issue #7469），
                    # user-defined bridge 下 --dns 只作上游不写 nameserver；直接挂一个可达的 resolv.conf。
                    resolv = work.path / ".resolv.conf"
                    await asyncio.to_thread(
                        self._write_resolv_file, resolv, self.settings.dns
                    )
                    arguments += [
                        "--mount",
                        f"type=bind,src={resolv},dst=/etc/resolv.conf,readonly",
                    ]
                for handle, path in (secrets or {}).items():
                    arguments += [
                        "--mount",
                        f"type=bind,src={path},dst=/run/secrets/{handle},readonly",
                    ]
                if entrypoint:
                    arguments += [
                        "--entrypoint",
                        entrypoint[0],
                        self.settings.image,
                        *entrypoint[1:],
                    ]
                else:
                    arguments += [
                        "--entrypoint",
                        "/bin/sh",
                        self.settings.image,
                        "-c",
                        command,
                    ]
                await asyncio.to_thread(
                    self._receipt, key, container=container, status="preparing"
                )
                self.containers[row["thread_id"]] = container
                if active_network_ref:
                    self._container_networks[container] = active_network_ref
                await completed_thread(
                    self._resource,
                    "container",
                    container,
                    {"socket": self.settings.socket, "thread_id": row["thread_id"]},
                )
            except BaseException:
                if active_network_ref and not entered_container_try:
                    self._container_networks.pop(container, None)
                    remaining = max(0, self._active_networks.get(active_network_ref, 0) - 1)
                    if remaining:
                        self._active_networks[active_network_ref] = remaining
                    else:
                        self._active_networks.pop(active_network_ref, None)
                raise
            entered_container_try = True
            monitor = None
            attached_task = None
            prior_quarantined = bool(work.quarantined)
            terminated_ok = False
            cleanup_safe = False
            needs_rollback = False
            cancel_during_cleanup = False
            try:
                await self.docker(*arguments)
                await asyncio.to_thread(self._receipt, key, status="running")
                attached_task = asyncio.create_task(
                    self.docker(
                        "start",
                        "--attach",
                        container,
                        checked=False,
                        timeout=min(
                            timeout or self.settings.timeout, self.settings.timeout
                        ),
                        output_bytes=self.settings.output_bytes,
                    )
                )
                if network != "none":
                    entry = next(
                        value
                        for value in self.networks.values()
                        if value["name"] == network
                    )

                    async def watch():
                        while True:
                            await asyncio.sleep(1)
                            await self._check_network(entry)

                    monitor = asyncio.create_task(watch())
                    done, _pending = await asyncio.wait(
                        {attached_task, monitor}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if monitor in done:
                        await monitor
                attached = await attached_task
                inspection = json.loads(
                    (
                        await self.docker(
                            "inspect", "--format", "{{json .State}}", container
                        )
                    ).output
                )
                if inspection.get("Running"):
                    raise RuntimeError("容器未完全退出")
                if inspection.get("OOMKilled"):
                    raise RuntimeError("容器达到内存上限，文件修改已回退")
                if network != "none":
                    entry = next(
                        value
                        for value in self.networks.values()
                        if value["name"] == network
                    )
                    await self._check_network(entry)
                current = await asyncio.to_thread(
                    self.store.get, row["thread_id"], row["chat_id"]
                )
                if current["state"] == "closed":
                    raise RuntimeError("线程已停止，执行文件未提交")
                version = None
                if not secrets:
                    commit = asyncio.create_task(asyncio.to_thread(work.commit))
                    try:
                        version = await asyncio.shield(commit)
                    except asyncio.CancelledError:
                        await _uninterruptible_await(commit)
                        raise
                result = {
                    "exit_code": inspection.get("ExitCode", attached.code),
                    "output": self.redact(attached.output)[:12000],
                    "truncated": len(attached.output) > 12000,
                    "checkpoint": version,
                }
                await asyncio.to_thread(
                    self._receipt, key, status="completed", result=result
                )
                cleanup_safe = True
                return result
            except BaseException:
                needs_rollback = True
                work.quarantined = True
                terminated_ok, c_term = await _uninterruptible_await(
                    self._terminate(container)
                )
                if c_term:
                    cancel_during_cleanup = True
                if terminated_ok:
                    _, c_rb = await _uninterruptible_await(
                        asyncio.to_thread(work.rollback)
                    )
                    if c_rb:
                        cancel_during_cleanup = True
                    cleanup_safe = True
                _, c_rec = await _uninterruptible_await(
                    asyncio.to_thread(self._receipt, key, status="interrupted")
                )
                if c_rec:
                    cancel_during_cleanup = True
                raise
            finally:
                for task in (monitor, attached_task):
                    if task and not task.done():
                        task.cancel()
                pending_bg = [task for task in (monitor, attached_task) if task]
                if pending_bg:
                    _, c_bg = await _uninterruptible_await(
                        asyncio.gather(*pending_bg, return_exceptions=True)
                    )
                    if c_bg:
                        cancel_during_cleanup = True
                if not terminated_ok:
                    terminated_ok, c_term2 = await _uninterruptible_await(
                        self._terminate(container)
                    )
                    if c_term2:
                        cancel_during_cleanup = True
                if terminated_ok:
                    self.containers.pop(row["thread_id"], None)
                    if active_network_ref:
                        self._container_networks.pop(container, None)
                        remaining = max(
                            0, self._active_networks.get(active_network_ref, 0) - 1
                        )
                        if remaining:
                            self._active_networks[active_network_ref] = remaining
                        else:
                            self._active_networks.pop(active_network_ref, None)
                    _, c_fgt = await _uninterruptible_await(
                        asyncio.to_thread(self._forget, container)
                    )
                    if c_fgt:
                        cancel_during_cleanup = True
                    if cleanup_safe and not prior_quarantined:
                        work.quarantined = False
                else:
                    work.quarantined = True
                    self.ready, self.error = False, "容器清理未确认，已停止后续执行"
                    _, c_cu = await _uninterruptible_await(
                        asyncio.to_thread(
                            self._receipt, key, status="cleanup_unconfirmed"
                        )
                    )
                    if c_cu:
                        cancel_during_cleanup = True
                    raise RuntimeError(self.error)
                if cancel_during_cleanup and not needs_rollback:
                    raise asyncio.CancelledError

    async def _terminate(self, container):
        try:
            await self.docker("rm", "--force", container, checked=False)
            result = await self.docker(
                "ps",
                "--all",
                "--filter",
                "name=^/" + container + "$",
                "--format",
                "{{.Names}}",
                checked=False,
            )
            return result.code == 0 and not result.output.strip()
        except (OSError, RuntimeError, TimeoutError, ValueError):
            return False

    async def close(self):
        self.closing = True
        for container in list(self.containers.values()):
            if not await self._terminate(container):
                raise RuntimeError("容器仍在运行，保留工作副本和网络隔离")
            await completed_thread(self._forget, container)
        self.containers.clear()
        for entry in list(self.networks.values()):
            await self._remove_network(entry)
        self.networks.clear()
        if self.instance_lock is not None:
            release_lock_handle(self.instance_lock)
            self.instance_lock = None
