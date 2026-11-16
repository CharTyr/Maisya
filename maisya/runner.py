"""双轨执行通用协议、风险裁决与宿主机工作区执行器。"""

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import threading
from typing import Literal, Protocol

from .pathsafe import minimal_subprocess_env, path_is_link_like, rmtree_safe, safe_chmod
from .trust import PERMISSION_MODES, PermissionGrantStore
from .vault import Vault, VaultError, redact_secrets

RiskLevel = Literal["low", "risky"]
PermissionMode = Literal["ask_risky", "autonomous", "always_ask"]
RunnerStatus = Literal["ok", "error", "timeout", "approval_required", "denied"]

_DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:^|[;&|]\s*|\s)(?:rm\s+(?:-[A-Za-z]*[rf][A-Za-z]*|--recursive|--force))",
        re.IGNORECASE,
    ),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+-[A-Za-z]*[fd]", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+(?:--force|-f)\b", re.IGNORECASE),
    re.compile(r"\b(?:mkfs(?:\.[a-z0-9]+)?|fdisk|parted|wipefs)\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(
        r"\b(?:shutdown|reboot|poweroff|halt|systemctl|launchctl|service)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[;&|]\s*)(?:sudo|su|doas)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:chmod\s+(?:-R|777)|chown\s+-R)\b", re.IGNORECASE),
    re.compile(r"\b(?:killall|pkill|kill\s+-9)\b", re.IGNORECASE),
    re.compile(r"\|\s*(?:sh|bash|zsh)\b", re.IGNORECASE),
)

_EXTERNAL_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bcurl\b[^\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--request\s+(?:POST|PUT|PATCH|DELETE)|(?:^|\s)(?:-d|--data|--data-raw|--data-binary|-F|--form|--upload-file|-T)\b)",
        re.IGNORECASE,
    ),
    re.compile(r"\bwget\b[^\n]*(?:--post-data|--post-file|--method=(?:POST|PUT|DELETE|PATCH))", re.IGNORECASE),
    re.compile(r"\b(?:ssh|scp|sftp|rsync)\b", re.IGNORECASE),
)

_SECRET_PATH_RE = re.compile(r"(?:^|[\s'\"=(/])run/secrets(?:$|[\s'\")/])|/run/secrets", re.IGNORECASE)
_TRAVERSAL_RE = re.compile(r"(?:^|[\s'\"=(/\\])\.\.[/\\]")
_SENSITIVE_ABS_PREFIXES = (
    "/etc",
    "/root",
    "/boot",
    "/sbin",
    "/private/etc",
    "/var/root",
    "/run/secrets",
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
)
_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "PYTHONPATH",
        "PYTHONHOME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SHELL",
        "USER",
        "IFS",
        "ENV",
        "BASH_ENV",
    }
)
_FORBIDDEN_ENV_PREFIXES = ("LD_", "DYLD_")


@dataclass(frozen=True)
class ApprovalRequired:
    """高风险动作拦截后返回的结构化确认载荷。"""

    reason: str
    action_key: str
    risk_level: RiskLevel
    command: str | list[str]
    scope: str = ""
    category: str = "risky"
    targets: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "reason": self.reason,
            "action_key": self.action_key,
            "risk_level": self.risk_level,
            "command": list(self.command)
            if isinstance(self.command, list)
            else self.command,
            "scope": self.scope,
            "category": self.category,
            "targets": list(self.targets),
        }


@dataclass(frozen=True)
class ExecutionRequest:
    """跨执行器统一的命令执行请求。"""

    command: list[str] | str
    workspace: Path
    is_admin: bool = False
    actor: str = ""
    scope: str = ""
    session_id: str = ""
    permission_mode: PermissionMode = "ask_risky"
    secrets: tuple[str, ...] = ()
    extra_redactions: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    output_bytes: int = 2097152
    shell: bool = False
    action_key: str = ""
    risk_hint: str = ""
    targets: tuple[str, ...] = ()
    mount_secret_files: bool = False
    unisolated_host: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    """统一执行返回结构，出站文本已强制脱敏。"""

    status: RunnerStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    approval_required: ApprovalRequired | None = None
    error: str = ""
    runner_name: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.exit_code == 0

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "approval_required": self.approval_required.to_dict()
            if self.approval_required is not None
            else None,
            "error": self.error,
            "runner_name": self.runner_name,
        }


class WorkspaceRunner(Protocol):
    """统一工作区执行协议（HostWorkspaceRunner / DockerRunner）。"""

    name: str

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...


def _command_preview(command: list[str] | str) -> str:
    if isinstance(command, str):
        return command.strip()
    return " ".join(str(part) for part in command)


def _command_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _inspect_args_text(command: list[str] | str, *, shell: bool) -> str:
    if isinstance(command, str):
        return command
    if shell:
        return " ".join(command)
    # Exclude argv[0] binary path so host Python interpreter path does not false-trigger path escape.
    if len(command) <= 1:
        return ""
    return " ".join(str(part) for part in command[1:])


def _has_path_escape(command: list[str] | str, workspace: Path, *, shell: bool) -> bool:
    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        candidates = tokens
    else:
        candidates = [str(part) for part in command[1:]]

    inspected = _inspect_args_text(command, shell=shell)
    if _TRAVERSAL_RE.search(inspected):
        return True
    for prefix in _SENSITIVE_ABS_PREFIXES:
        if prefix in inspected:
            return True

    ws_root = workspace.resolve()
    for token in candidates:
        stripped = token.strip("'\"")
        if not stripped or stripped == "/dev/null":
            continue
        if ".." in Path(stripped).parts:
            return True
        if stripped.startswith("~"):
            return True
        if stripped.startswith("/") and not stripped.startswith("//"):
            try:
                resolved = Path(stripped).resolve()
            except OSError:
                return True
            if not resolved.is_relative_to(ws_root):
                return True
    return False


def assess_execution_risk(
    request: ExecutionRequest,
) -> tuple[RiskLevel, str, str, str]:
    """评估执行请求的风险级别，返回 (risk_level, category, action_key, reason)。"""
    cmd_text = _command_preview(request.command)
    inspected = _inspect_args_text(request.command, shell=request.shell)
    fp = _command_fingerprint(cmd_text)

    if request.risk_hint == "cross_chat":
        return (
            "risky",
            "cross_chat",
            request.action_key or f"chat:cross_send:{fp}",
            "跨会话消息投递需要管理员确认",
        )
    if request.risk_hint == "external_write":
        return (
            "risky",
            "external_write",
            request.action_key or f"net:external_write:{fp}",
            "非只读外部写操作需要管理员确认",
        )
    if request.risk_hint == "privileged_script":
        return (
            "risky",
            "privileged_script",
            request.action_key or f"exec:privileged:{fp}",
            "高权限脚本执行需要管理员确认",
        )
    if request.secrets:
        handles = ",".join(sorted(str(h) for h in request.secrets))
        return (
            "risky",
            "secret_injection",
            request.action_key or f"secret:{handles}",
            f"注入安全凭据 ({handles}) 需要管理员确认",
        )
    if _SECRET_PATH_RE.search(cmd_text) or _SECRET_PATH_RE.search(inspected):
        return (
            "risky",
            "secret_access",
            request.action_key or f"secret:path:{fp}",
            "读取凭据路径需要管理员确认",
        )
    for pattern in _DESTRUCTIVE_PATTERNS:
        if pattern.search(cmd_text):
            return (
                "risky",
                "destructive_shell",
                request.action_key or f"exec:destructive:{fp}",
                "高风险破坏性命令需要管理员确认",
            )
    if _has_path_escape(request.command, request.workspace, shell=request.shell):
        return (
            "risky",
            "path_escape",
            request.action_key or f"fs:outside:{fp}",
            "访问或修改工作区外路径需要管理员确认",
        )
    for pattern in _EXTERNAL_WRITE_PATTERNS:
        if pattern.search(cmd_text):
            return (
                "risky",
                "external_write",
                request.action_key or f"net:external_write:{fp}",
                "非只读外部网络写入或远端连接需要管理员确认",
            )
    return (
        "low",
        "workspace",
        request.action_key or f"exec:workspace:{fp}",
        "常规工作区操作",
    )


def evaluate_permission_gate(
    request: ExecutionRequest,
    grants: PermissionGrantStore | None = None,
) -> ApprovalRequired | None:
    """结合 permission_mode 与 permission_grants 判断是否需要挂起确认。"""
    if request.permission_mode not in PERMISSION_MODES:
        raise ValueError(f"无效的权限模式：{request.permission_mode!r}")

    risk_level, category, action_key, reason = assess_execution_risk(request)
    if request.permission_mode == "autonomous":
        return None
    if request.permission_mode == "ask_risky" and risk_level == "low":
        if not request.unisolated_host:
            return None
        scope = request.scope or "*"
        if grants is not None and grants.check_or_consume(
            scope=scope,
            action_key="host_exec:unisolated",
            session_id=request.session_id,
        ):
            return None
        return ApprovalRequired(
            reason="宿主机直通模式不隔离本机文件系统（cwd 不限制脚本访问工作区外路径），请确认授予宿主机高信任执行权限",
            action_key="host_exec:unisolated",
            risk_level="risky",
            command=request.command,
            scope=scope,
            category="host_unisolated",
            targets=request.targets,
        )

    scope = request.scope or "*"
    if grants is not None and grants.check_or_consume(
        scope=scope,
        action_key=action_key,
        session_id=request.session_id,
    ):
        return None

    return ApprovalRequired(
        reason=reason if risk_level == "risky" else "当前权限模式（always_ask）要求确认每次执行",
        action_key=action_key,
        risk_level=risk_level,
        command=request.command,
        scope=scope,
        category=category if risk_level == "risky" else "always_ask",
        targets=request.targets,
    )


def _clamp_timeout(timeout: float) -> float:
    return max(0.01, min(float(timeout), 180.0))


def _clamp_output_bytes(output_bytes: int) -> int:
    return max(1024, min(int(output_bytes), 8388608))


def _kill_process_tree(proc) -> None:
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _decode_and_redact(
    raw: bytes,
    *,
    limit: int,
    vault: Vault | None,
    secret_values: Iterable[str],
) -> tuple[str, bool]:
    truncated = len(raw) > limit
    chunk = raw[:limit]
    text = chunk.decode("utf-8", errors="replace")
    if vault is not None:
        text = vault.redact(text)
    values = list(secret_values)
    if values:
        text = redact_secrets(text, values)
    return text, truncated


class HostWorkspaceRunner:
    """仅面向已核验管理员的本机工作区直通执行器。"""

    name = "host_workspace"

    def __init__(
        self,
        *,
        vault: Vault | None = None,
        grants: PermissionGrantStore | None = None,
    ):
        self.vault = vault
        self.grants = grants
        self._active_lock = threading.Lock()
        self._active_procs: list[tuple[str, str, object]] = []
        self._terminated_procs: set[int] = set()

    def _register_proc(self, session_id: str, actor: str, proc: object) -> tuple[str, str, object]:
        entry = (str(session_id or ""), str(actor or ""), proc)
        with self._active_lock:
            self._active_procs.append(entry)
        return entry

    def _unregister_proc(self, entry: tuple[str, str, object] | None) -> None:
        if entry is None:
            return
        proc = entry[2]
        with self._active_lock:
            if entry in self._active_procs:
                self._active_procs.remove(entry)
            self._terminated_procs.discard(id(proc))

    def _consume_terminated(self, proc: object | None) -> bool:
        if proc is None:
            return False
        with self._active_lock:
            pid_key = id(proc)
            if pid_key in self._terminated_procs:
                self._terminated_procs.discard(pid_key)
                return True
            return False

    def active_count(self, *, session_id: str = "", actor: str = "") -> int:
        sid = str(session_id or "")
        act = str(actor or "")
        with self._active_lock:
            return sum(
                1
                for s, a, proc in self._active_procs
                if getattr(proc, "returncode", None) is None
                and (not sid or s == sid)
                and (not act or a == act)
            )

    def terminate_for(self, *, session_id: str = "", actor: str = "") -> int:
        sid = str(session_id or "")
        act = str(actor or "")
        with self._active_lock:
            targets = [
                proc
                for s, a, proc in self._active_procs
                if getattr(proc, "returncode", None) is None
                and (not sid or s == sid)
                and (not act or a == act)
            ]
            for proc in targets:
                self._terminated_procs.add(id(proc))
        killed = 0
        for proc in targets:
            _kill_process_tree(proc)
            killed += 1
        return killed

    def _finish_result(
        self,
        proc: object,
        out_raw: bytes,
        err_raw: bytes,
        *,
        output_limit: int,
        secret_values: list[str],
    ) -> ExecutionResult:
        stdout, trunc_out = _decode_and_redact(
            out_raw,
            limit=output_limit,
            vault=self.vault,
            secret_values=secret_values,
        )
        stderr, trunc_err = _decode_and_redact(
            err_raw,
            limit=output_limit,
            vault=self.vault,
            secret_values=secret_values,
        )
        rc = int(getattr(proc, "returncode", 0) or 0)
        if self._consume_terminated(proc):
            return ExecutionResult(
                status="denied",
                exit_code=rc or -9,
                stdout=stdout,
                stderr=stderr or "授权已撤销，本地子进程已终止",
                truncated=trunc_out or trunc_err,
                error="授权已撤销，本地子进程已终止",
                runner_name=self.name,
            )
        return ExecutionResult(
            status="ok" if rc == 0 else "error",
            exit_code=rc,
            stdout=stdout,
            stderr=stderr,
            truncated=trunc_out or trunc_err,
            error="" if rc == 0 else f"进程退出码 {rc}",
            runner_name=self.name,
        )

    def _prepare(
        self, request: ExecutionRequest
    ) -> tuple[ExecutionResult | None, dict[str, str], list[str], Path | None]:
        if not request.is_admin:
            return (
                ExecutionResult(
                    status="denied",
                    error="仅已核验管理员可使用宿主机工作区执行器",
                    runner_name=self.name,
                ),
                {},
                [],
                None,
            )

        ws = Path(request.workspace)
        if path_is_link_like(ws) or not ws.exists() or not ws.is_dir():
            return (
                ExecutionResult(
                    status="denied",
                    error="工作区路径无效或包含符号链接",
                    runner_name=self.name,
                ),
                {},
                [],
                None,
            )

        approval = evaluate_permission_gate(request, self.grants)
        if approval is not None:
            return (
                ExecutionResult(
                    status="approval_required",
                    approval_required=approval,
                    error=approval.reason,
                    runner_name=self.name,
                ),
                {},
                [],
                None,
            )

        env = minimal_subprocess_env()
        env["PYTHONNOUSERSITE"] = "1"
        for key, value in (request.env or {}).items():
            key_u = str(key).upper()
            if key_u in _FORBIDDEN_ENV_KEYS or any(
                key_u.startswith(pre) for pre in _FORBIDDEN_ENV_PREFIXES
            ):
                continue
            env[str(key)] = str(value)

        secret_values: list[str] = list(request.extra_redactions)
        secrets_dir: Path | None = None
        if request.secrets:
            if self.vault is None:
                return (
                    ExecutionResult(
                        status="error",
                        error="未配置凭据保险箱，无法注入凭据",
                        runner_name=self.name,
                    ),
                    {},
                    [],
                    None,
                )
            try:
                env = self.vault.inject_env_secrets(request.secrets, env=env)
                for handle in request.secrets:
                    secret_values.append(self.vault.for_execution(handle))
                if request.mount_secret_files:
                    secrets_dir = Path(tempfile.mkdtemp(prefix="maisya-sec-"))
                    safe_chmod(secrets_dir, 0o700)
                    for handle in request.secrets:
                        secret_path = secrets_dir / handle
                        secret_path.write_text(
                            self.vault.for_execution(handle), encoding="utf-8"
                        )
                        safe_chmod(secret_path, 0o400)
                    env["MAISYA_SECRETS_DIR"] = str(secrets_dir)
            except (VaultError, ValueError, OSError) as exc:
                if secrets_dir is not None:
                    rmtree_safe(secrets_dir)
                return (
                    ExecutionResult(
                        status="error",
                        error=str(exc),
                        runner_name=self.name,
                    ),
                    {},
                    [],
                    None,
                )

        return None, env, secret_values, secrets_dir

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        blocked, env, secret_values, secrets_dir = self._prepare(request)
        if blocked is not None:
            return blocked

        ws_resolved = Path(request.workspace).resolve()
        timeout = _clamp_timeout(request.timeout)
        output_limit = _clamp_output_bytes(request.output_bytes)
        use_shell = isinstance(request.command, str) or request.shell
        cmd = (
            _command_preview(request.command)
            if use_shell and not isinstance(request.command, str)
            else request.command
        )
        popen_kwargs: dict = {
            "cwd": str(ws_resolved),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": use_shell,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            popen_kwargs["start_new_session"] = True

        proc: subprocess.Popen[bytes] | None = None
        entry: tuple[str, str, object] | None = None
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
            entry = self._register_proc(request.session_id, request.actor, proc)
            try:
                out_raw, err_raw = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    out_raw, err_raw = proc.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    out_raw, err_raw = b"", b""
                stdout, trunc_out = _decode_and_redact(
                    out_raw,
                    limit=output_limit,
                    vault=self.vault,
                    secret_values=secret_values,
                )
                stderr, trunc_err = _decode_and_redact(
                    err_raw,
                    limit=output_limit,
                    vault=self.vault,
                    secret_values=secret_values,
                )
                return ExecutionResult(
                    status="timeout",
                    exit_code=None,
                    stdout=stdout,
                    stderr=stderr,
                    truncated=trunc_out or trunc_err,
                    error=f"命令执行超时（>{timeout}s）",
                    runner_name=self.name,
                )
            return self._finish_result(
                proc,
                out_raw,
                err_raw,
                output_limit=output_limit,
                secret_values=secret_values,
            )
        except OSError as exc:
            return ExecutionResult(
                status="error",
                error=f"执行失败：{exc}",
                runner_name=self.name,
            )
        finally:
            self._unregister_proc(entry)
            if secrets_dir is not None:
                rmtree_safe(secrets_dir)

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        blocked, env, secret_values, secrets_dir = self._prepare(request)
        if blocked is not None:
            return blocked

        ws_resolved = Path(request.workspace).resolve()
        timeout = _clamp_timeout(request.timeout)
        output_limit = _clamp_output_bytes(request.output_bytes)
        use_shell = isinstance(request.command, str) or request.shell
        spawn_kwargs: dict = {
            "cwd": str(ws_resolved),
            "env": env,
            "stdin": asyncio.subprocess.DEVNULL,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if os.name == "nt":
            spawn_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            spawn_kwargs["start_new_session"] = True

        proc = None
        entry: tuple[str, str, object] | None = None
        try:
            if use_shell:
                cmd_str = _command_preview(request.command)
                proc = await asyncio.create_subprocess_shell(cmd_str, **spawn_kwargs)
            else:
                proc = await asyncio.create_subprocess_exec(
                    *[str(part) for part in request.command], **spawn_kwargs
                )
            entry = self._register_proc(request.session_id, request.actor, proc)
            try:
                out_raw, err_raw = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                _kill_process_tree(proc)
                try:
                    out_raw, err_raw = await asyncio.wait_for(
                        proc.communicate(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    out_raw, err_raw = b"", b""
                stdout, trunc_out = _decode_and_redact(
                    out_raw,
                    limit=output_limit,
                    vault=self.vault,
                    secret_values=secret_values,
                )
                stderr, trunc_err = _decode_and_redact(
                    err_raw,
                    limit=output_limit,
                    vault=self.vault,
                    secret_values=secret_values,
                )
                return ExecutionResult(
                    status="timeout",
                    exit_code=None,
                    stdout=stdout,
                    stderr=stderr,
                    truncated=trunc_out or trunc_err,
                    error=f"命令执行超时（>{timeout}s）",
                    runner_name=self.name,
                )
            except asyncio.CancelledError:
                _kill_process_tree(proc)
                try:
                    await proc.wait()
                except Exception:
                    pass
                raise
            return self._finish_result(
                proc,
                out_raw,
                err_raw,
                output_limit=output_limit,
                secret_values=secret_values,
            )
        except OSError as exc:
            return ExecutionResult(
                status="error",
                error=f"执行失败：{exc}",
                runner_name=self.name,
            )
        finally:
            self._unregister_proc(entry)
            if secrets_dir is not None:
                rmtree_safe(secrets_dir)


__all__ = [
    "ApprovalRequired",
    "ExecutionRequest",
    "ExecutionResult",
    "HostWorkspaceRunner",
    "PermissionMode",
    "RiskLevel",
    "RunnerStatus",
    "WorkspaceRunner",
    "assess_execution_risk",
    "evaluate_permission_gate",
]
