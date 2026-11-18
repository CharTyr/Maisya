"""在原有线程内组合只读工具、工作区、沙箱与受权能力。"""

import base64
import hashlib
import inspect
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .abilities import Profile, _has_relative_link
from .authz import check_hot_edit_expansion
from .data_scope import STEP_METHODS_CONTEXT, initial_thread_scope
from .pathsafe import minimal_subprocess_env, path_is_link_like
from .vault import redact_secrets
from .runner import (
    ApprovalRequired,
    ExecutionRequest,
    ExecutionResult,
    HostWorkspaceRunner,
    _command_preview,
    _decode_and_redact,
    _has_path_escape,
)
from .sandbox import DockerRunner, Sandbox, SandboxSettings
from .scout import Scout, call_capability, checked, safe_url, validate_arguments
from .trust import PermissionGrantStore, parse_stream_spec
from .workspaces import (
    Workspaces,
    completed_thread,
    regular_open,
    write_workspace_bytes,
)

_PLAYWRIGHT_ERROR_MARKERS = (
    "playwright",
    "chromium",
    "browser",
    "executable doesn't exist",
)

_ACCESS_RANK = {"everyone": 0, "listed": 1, "admins": 2}
_ENVIRONMENT_RANK = {"sandbox": 0, "readonly": 1}
_EXECUTION_RANK = {"host": 0, "auto": 1, "sandbox": 2}
_PERMISSION_RANK = {"autonomous": 0, "ask_risky": 1, "always_ask": 2}
_NETWORK_RANK = {"public": 0, "restricted": 1, "none": 2}
_SHELL_META_RE = re.compile(r"[\n\r|&;><$`\\]")
_TRUSTED_SCRIPT_BOOT = (
    "import os, runpy, socket, sys\n"
    "def _deny_net(*_a, **_kw):\n"
    "    raise PermissionError('无容器受控脚本禁止访问网络')\n"
    "socket.socket = _deny_net\n"
    "socket.socketpair = _deny_net\n"
    "socket.create_connection = _deny_net\n"
    "socket.create_server = _deny_net\n"
    "sys.argv = sys.argv[1:]\n"
    "sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def _stricter_choice(first: str, second: str, ranks: dict[str, int], *, default_rank: int) -> str:
    return first if ranks.get(first, default_rank) >= ranks.get(second, default_rank) else second


def _tighten_allowlist(snapshot_items: tuple[str, ...], live_items: tuple[str, ...]) -> tuple[str, ...]:
    """合并快照与实时白名单时只做单调收窄；非空交集为空则保留永不匹配的哨兵。"""
    if snapshot_items and live_items:
        live_set = set(live_items)
        narrowed = tuple(item for item in snapshot_items if item in live_set)
        return narrowed if narrowed else ("__disjoint__",)
    return live_items or snapshot_items


def _intersect_bound(snapshot_items: tuple[str, ...], live_items: tuple[str, ...]) -> tuple[str, ...]:
    """强能力绑定（mcp/apis/secrets）取原快照与实时配置的严格交集，空集即禁止，防止热改静默扩权。"""
    if not snapshot_items or not live_items:
        return ()
    live_set = set(live_items)
    return tuple(item for item in snapshot_items if item in live_set)


def _step_service_context(context: dict) -> dict:
    """为服务/跨插件 API 调用生成按 step_index 隔离的 _tool_call_id 浅副本。"""
    step_idx = context.get("_step_index")
    if step_idx is None:
        return context
    raw_call_id = str(context.get("_tool_call_id") or "")
    return {**context, "_tool_call_id": f"step:{int(step_idx)}:{raw_call_id}"}


def _playwright_render_hint(message: str) -> str:
    """若 html2png 报错涉及缺失 Playwright/Chromium 内核，附加安装与降级交付提示。"""
    lowered = message.lower()
    if any(marker in lowered for marker in _PLAYWRIGHT_ERROR_MARKERS):
        return (
            f"{message}（若宿主未安装浏览器内核，可在宿主 Python 环境执行 "
            "playwright install chromium，或先改用纯文字/网页文件交付）"
        )
    return ""


class AgentRuntime(Scout):
    def __init__(
        self,
        host,
        root,
        store,
        settings,
        vault,
        *,
        authorized,
        web_provider="auto",
        tz_offset_minutes=None,
        grants=None,
        host_runner=None,
        docker_runner=None,
        global_permission_mode="ask_risky",
    ):
        super().__init__(host, vault.redact, tz_offset_minutes=tz_offset_minutes)
        self.store, self.vault, self.authorized = store, vault, authorized
        self.sandbox_settings = settings if isinstance(settings, SandboxSettings) else SandboxSettings()
        if grants is not None:
            self.grants = grants
        elif getattr(store, "path", None) is not None:
            self.grants = PermissionGrantStore(store.path)
        else:
            self.grants = None
        self.host_runner = host_runner or HostWorkspaceRunner(
            vault=vault, grants=self.grants
        )
        self._custom_docker_runner = docker_runner is not None
        self.docker_runner = docker_runner or DockerRunner(
            self.sandbox_settings, vault=vault, grants=self.grants
        )
        self._global_permission_mode = global_permission_mode
        self._pending_pause_by_call = {}
        self.sandbox = Sandbox(
            settings, store, redact=vault.redact, authorized=authorized
        )
        self.files = Workspaces(
            root / "workspaces",
            store,
            volume=self.sandbox.volume if settings.enabled else None,
            contains_secret=vault.contains_secret,
            redact=vault.redact,
        )
        self.sessions = {}
        self.attachments = {}
        self._attachment_by_path = {}
        self.attachment_attempts = {}
        self.memory = None
        self.learning = None
        self.permitted = None
        from .services import Services
        from .web import WebRouter

        self.services = Services(root / "abilities/services.json", vault)
        self.web = WebRouter(
            host,
            redact=vault.redact,
            provider=web_provider,
            services=self.services,
            runtime=self,
        )
        self._ensure_pending_approvals_table()

    def _ensure_pending_approvals_table(self) -> None:
        connect = getattr(self.store, "connect", None)
        if not callable(connect):
            return
        with connect() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending_approvals ("
                "request_id TEXT PRIMARY KEY, "
                "thread_id TEXT NOT NULL, "
                "tool_call_id TEXT NOT NULL DEFAULT '', "
                "action_key TEXT NOT NULL, "
                "tool TEXT NOT NULL DEFAULT 'exec_command', "
                "category TEXT NOT NULL DEFAULT 'risky', "
                "risk_level TEXT NOT NULL DEFAULT 'risky', "
                "command TEXT NOT NULL DEFAULT '', "
                "reason TEXT NOT NULL DEFAULT '', "
                "summary TEXT NOT NULL DEFAULT '', "
                "scope TEXT NOT NULL DEFAULT '*', "
                "session_id TEXT NOT NULL DEFAULT '', "
                "targets TEXT NOT NULL DEFAULT '[]', "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "decision TEXT NOT NULL DEFAULT '', "
                "actor TEXT NOT NULL DEFAULT '', "
                "created_at REAL NOT NULL, "
                "resolved_at REAL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_approvals_thread "
                "ON pending_approvals (thread_id, status, created_at)"
            )

    def _get_global_permission_mode(self) -> str:
        raw = (
            self._global_permission_mode()
            if callable(self._global_permission_mode)
            else self._global_permission_mode
        )
        mode = str(raw or "ask_risky").strip()
        return mode if mode in _PERMISSION_RANK else "ask_risky"

    def _effective_permission_mode(self, profile: Profile) -> str:
        g_mode = self._get_global_permission_mode()
        p_mode = str(getattr(profile, "permission_mode", "") or "").strip()
        if g_mode == "always_ask" or p_mode == "always_ask":
            return "always_ask"
        if p_mode in {"autonomous", "ask_risky"} and p_mode != "ask_risky":
            return p_mode
        return g_mode

    def docker_available(self) -> bool:
        return bool(
            getattr(self.sandbox_settings, "enabled", False)
            and (self.sandbox.ready or self._custom_docker_runner)
        )

    def pending_approval_for_thread(self, thread_id: str) -> dict | None:
        connect = getattr(self.store, "connect", None)
        if not callable(connect) or not thread_id:
            return None
        with connect() as db:
            row = db.execute(
                "SELECT request_id, thread_id, tool_call_id, action_key, tool, category, "
                "risk_level, command, reason, summary, scope, session_id, targets, created_at "
                "FROM pending_approvals WHERE thread_id=? AND status='pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
        if not row:
            return None
        try:
            targets = json.loads(row["targets"] or "[]")
        except ValueError:
            targets = []
        return {
            "request_id": row["request_id"],
            "thread_id": row["thread_id"],
            "tool_call_id": row["tool_call_id"],
            "action_key": row["action_key"],
            "tool": row["tool"],
            "category": row["category"],
            "risk_level": row["risk_level"],
            "command": self.redact(row["command"]),
            "reason": self.redact(row["reason"]),
            "summary": self.redact(row["summary"]),
            "scope": row["scope"],
            "session_id": row["session_id"],
            "targets": targets if isinstance(targets, list) else [],
            "created_at": float(row["created_at"] or 0.0),
        }

    def record_pending_approval(
        self,
        *,
        thread_id: str,
        tool_call_id: str,
        tool: str,
        approval: ApprovalRequired,
        scope: str,
        session_id: str,
        extra_redactions: tuple[str, ...] = (),
    ) -> dict:
        request_id = "apr_" + uuid.uuid4().hex[:12]
        raw_cmd = self.redact(_command_preview(approval.command))
        raw_reason = self.redact(approval.reason)
        if extra_redactions:
            raw_cmd = redact_secrets(raw_cmd, extra_redactions)
            raw_reason = redact_secrets(raw_reason, extra_redactions)
        cmd_preview = raw_cmd
        reason = raw_reason
        summary = self.redact(f"{reason}：{cmd_preview[:180]}" if cmd_preview else reason)
        if extra_redactions:
            summary = redact_secrets(summary, extra_redactions)
        now = time.time()
        targets_list = [str(t) for t in (approval.targets or ())]
        item = {
            "request_id": request_id,
            "thread_id": thread_id,
            "tool_call_id": tool_call_id,
            "action_key": approval.action_key,
            "tool": tool,
            "category": approval.category,
            "risk_level": approval.risk_level,
            "command": cmd_preview,
            "reason": reason,
            "summary": summary,
            "scope": scope or approval.scope or "*",
            "session_id": session_id or thread_id,
            "targets": targets_list,
            "created_at": now,
        }
        connect = getattr(self.store, "connect", None)
        if callable(connect):
            with connect() as db, db:
                db.execute(
                    "UPDATE pending_approvals SET status='superseded', resolved_at=? "
                    "WHERE thread_id=? AND status='pending' AND action_key=?",
                    (now, thread_id, approval.action_key),
                )
                db.execute(
                    "INSERT INTO pending_approvals "
                    "(request_id, thread_id, tool_call_id, action_key, tool, category, "
                    "risk_level, command, reason, summary, scope, session_id, targets, "
                    "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        request_id,
                        thread_id,
                        tool_call_id,
                        approval.action_key,
                        tool,
                        approval.category,
                        approval.risk_level,
                        cmd_preview,
                        reason,
                        summary,
                        item["scope"],
                        item["session_id"],
                        json.dumps(targets_list, ensure_ascii=False),
                        now,
                    ),
                )
        if tool_call_id:
            self._pending_pause_by_call[tool_call_id] = item
        return item

    def resolve_pending_approval(
        self,
        thread_id: str,
        *,
        decision: str,
        request_id: str = "",
        action_key: str = "",
        actor: str = "",
        note: str = "",
    ) -> dict:
        clean_decision = str(decision or "").strip().lower()
        if clean_decision not in {"once", "session", "always", "deny"}:
            raise ValueError("decision 必须为 once、session、always 或 deny")
        connect = getattr(self.store, "connect", None)
        if not callable(connect):
            raise RuntimeError("当前存储不支持挂起审批记录")
        now = time.time()
        with connect() as db:
            if request_id:
                row = db.execute(
                    "SELECT * FROM pending_approvals WHERE thread_id=? AND request_id=? AND status='pending'",
                    (thread_id, request_id),
                ).fetchone()
            elif action_key:
                row = db.execute(
                    "SELECT * FROM pending_approvals WHERE thread_id=? AND action_key=? AND status='pending' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (thread_id, action_key),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM pending_approvals WHERE thread_id=? AND status='pending' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (thread_id,),
                ).fetchone()
            if row is None:
                raise ValueError("该线程当前没有待确认的高风险操作")
            row_data = dict(row)

        grant_key = ""
        if clean_decision in {"once", "session", "always"}:
            if self.grants is None:
                raise RuntimeError("权限授权存储尚未就绪")
            grant_key = self.grants.grant(
                scope=row_data["scope"] or "*",
                action_key=row_data["action_key"],
                tier=clean_decision,
                session_id=row_data["session_id"] or thread_id,
                kind=row_data["category"] or "exec",
                note=note or row_data["summary"] or row_data["reason"],
                actor=actor,
            )

        with connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            if clean_decision in {"once", "session", "always"}:
                cursor = db.execute(
                    "UPDATE pending_approvals SET status='approved', decision=?, actor=?, resolved_at=? "
                    "WHERE request_id=? AND status='pending'",
                    (clean_decision, actor, now, row_data["request_id"]),
                )
            else:
                cursor = db.execute(
                    "UPDATE pending_approvals SET status='denied', decision='deny', actor=?, resolved_at=? "
                    "WHERE request_id=? AND status='pending'",
                    (actor, now, row_data["request_id"]),
                )
            if not cursor.rowcount:
                raise ValueError("该线程当前没有待确认的高风险操作")
            db.execute(
                "UPDATE agent_threads SET questions=0 WHERE thread_id=?",
                (thread_id,),
            )
        return {
            "request_id": row_data["request_id"],
            "thread_id": thread_id,
            "action_key": row_data["action_key"],
            "decision": clean_decision,
            "status": "denied" if clean_decision == "deny" else "approved",
            "grant_key": grant_key,
            "summary": self.redact(row_data["summary"]),
            "resolved": True,
        }

    def resolve_profile(self, profile: Profile) -> Profile:
        """统一合成快照与实时角色档案：刷新运行参数且只允许安全边界单调收窄。"""
        profiles = getattr(self.memory, "profiles", None) if self.memory else None
        if profiles is None:
            return profile
        prof_path = getattr(profiles, "path", None)
        if prof_path is not None and not (prof_path / profile.id / "profile.toml").is_file():
            return profile
        live = profiles.get(profile.id)
        if live is None:
            return profile
        return replace(
            profile,
            enabled=bool(profile.enabled and live.enabled),
            bound=bool(profile.bound and live.bound),
            access=_stricter_choice(profile.access, live.access, _ACCESS_RANK, default_rank=2),
            environment=_stricter_choice(
                profile.environment, live.environment, _ENVIRONMENT_RANK, default_rank=1
            ),
            execution_mode=_stricter_choice(
                profile.execution_mode, live.execution_mode, _EXECUTION_RANK, default_rank=1
            ),
            permission_mode=_stricter_choice(
                profile.permission_mode or "ask_risky",
                live.permission_mode or "ask_risky",
                _PERMISSION_RANK,
                default_rank=1,
            ),
            session_mode=live.session_mode,
            network=_stricter_choice(profile.network, live.network, _NETWORK_RANK, default_rank=2),
            allow_users=_tighten_allowlist(tuple(profile.allow_users), tuple(live.allow_users)),
            allow_chats=_tighten_allowlist(tuple(profile.allow_chats), tuple(live.allow_chats)),
            instructions=live.instructions,
            description=live.description,
            icon_shape=live.icon_shape,
            theme_color=live.theme_color,
            model=live.model,
            model_name=live.model_name,
            fallback_models=live.fallback_models,
            task=live.task,
            max_tokens=live.max_tokens,
            context_tokens=live.context_tokens,
            temperature=live.temperature,
            max_steps=live.max_steps,
            web_provider=live.web_provider,
            sandbox_targets=live.sandbox_targets,
            shared_secrets=bool(profile.shared_secrets and live.shared_secrets),
            skills=live.skills,
            mcp=_intersect_bound(tuple(profile.mcp), tuple(live.mcp)),
            apis=_intersect_bound(tuple(profile.apis), tuple(live.apis)),
            secrets=_intersect_bound(tuple(profile.secrets), tuple(live.secrets)),
            fish_seed=live.fish_seed,
            coalesce_window_ms=live.coalesce_window_ms,
        )

    def begin_turn(self, thread_id: str) -> None:
        """在新一轮线程执行开始时清理上一轮残留的附件与重试计数。"""
        self.attachments.pop(thread_id, None)
        self._attachment_by_path.pop(thread_id, None)
        self.attachment_attempts.pop(thread_id, None)

    def effective_mode(self, profile):
        if getattr(profile, "execution_mode", "host") == "sandbox" or profile.environment == "sandbox":
            if self.docker_available():
                return "sandbox"
            return "sandbox" if profile.requires_container else "light_workspace"
        if getattr(profile, "uses_workspace", False) or "exec" in getattr(profile, "toolsets", ()):
            return "host"
        return "readonly"

    def available(self, profile):
        if not profile.enabled:
            raise PermissionError("角色已停用")
        live = None
        if (
            self.memory
            and (self.memory.profiles.path / profile.id / "profile.toml").is_file()
        ):
            live = self.memory.profiles.get(profile.id)
            if not live.enabled:
                raise PermissionError("角色已停用")
        if not profile.bound or (live is not None and not live.bound):
            raise PermissionError("角色的服务或凭据尚未绑定")
        needs_container = (
            profile.environment == "sandbox" and profile.requires_container
        ) or (
            live is not None
            and live.environment == "sandbox"
            and live.requires_container
        )
        if needs_container and not self.docker_available():
            raise RuntimeError(self.sandbox.error or "沙箱尚未就绪")
        req_secrets = set(profile.secrets) | (set(live.secrets) if live else set())
        req_mcp = set(profile.mcp) | (set(live.mcp) if live else set())
        req_apis = set(profile.apis) | (set(live.apis) if live else set())
        if (req_secrets or req_mcp or req_apis) and self.services is None:
            raise RuntimeError("能力服务尚未登记")
        if req_mcp or req_apis:
            configuration = self.services.load()
            if (
                any(
                    name not in configuration.get("servers", {}) for name in req_mcp
                )
                or any(
                    name not in configuration.get("apis", {}) for name in req_apis
                )
            ):
                raise PermissionError("角色引用的服务或只读 API 尚未登记")

    def _consume_pending_pause(self, history) -> dict | None:
        if not self._pending_pause_by_call or not isinstance(history, list):
            return None
        for item in reversed(history):
            if not isinstance(item, dict) or item.get("role") != "tool":
                break
            call_id = str(item.get("tool_call_id") or "")
            if call_id and call_id in self._pending_pause_by_call:
                return self._pending_pause_by_call.pop(call_id)
        return None

    async def step(self, profile, history, *, max_steps=None, tz_offset_minutes=None):
        pending_pause = self._consume_pending_pause(history)
        if pending_pause is not None:
            question = (
                f"执行前需要管理员确认：{pending_pause['summary']}\n"
                "请明确回复确认（once/session/always/deny），或在控制台批准后继续。"
            )
            return {
                "text": "",
                "calls": [
                    {
                        "id": f"apr_ask_{uuid.uuid4().hex[:10]}",
                        "function": {
                            "name": "ask_user",
                            "arguments": {
                                "question": question,
                                "reason": "missing_input",
                            },
                        },
                    }
                ],
            }
        profile = await completed_thread(self.resolve_profile, profile)
        await completed_thread(self.available, profile)
        if self.memory:
            step_ctx = STEP_METHODS_CONTEXT.get() or {}
            methods_sig = inspect.signature(self.memory.methods).parameters
            if "chat_id" in methods_sig or "is_admin" in methods_sig:
                methods = await completed_thread(
                    self.memory.methods,
                    profile,
                    chat_id=str(step_ctx.get("chat_id") or ""),
                    is_admin=bool(step_ctx.get("is_admin")),
                )
            else:
                methods = await completed_thread(self.memory.methods, profile)
            profile = replace(
                profile,
                instructions=profile.instructions
                + "\n<methods>"
                + methods
                + "</methods>",
            )
        docker_ok = self.docker_available()
        if not docker_ok:
            if profile.execution_mode == "sandbox":
                profile = replace(
                    profile,
                    tools=tuple(
                        name
                        for name in profile.tools
                        if name not in {"sandbox_exec", "exec_command"}
                    ),
                    instructions=profile.instructions
                    + "\n<runtime_mode>当前容器沙箱未就绪，自动运行于无容器轻量工作区模式：可使用 calc 进行精确计算与随机抽样，以及 workspace_file、html_render、result_attach 和已授权的只读/联网工具完成整理、排版与出图（开启 Docker 沙箱后恢复容器执行）。</runtime_mode>",
                )
            elif "sandbox_exec" in profile.tools:
                profile = replace(
                    profile,
                    tools=tuple(
                        name for name in profile.tools if name != "sandbox_exec"
                    ),
                )
        if (
            "workspace_file" not in profile.tools
            and await completed_thread(self._has_companions, profile)
        ):
            profile = replace(profile, tools=(*profile.tools, "workspace_file"))
        if self.services and profile.mcp:
            descriptions = await completed_thread(self.services.describe, profile)
            profile = replace(
                profile,
                instructions=profile.instructions
                + "\n已授权的能力服务："
                + json.dumps(descriptions, ensure_ascii=False),
            )
        if self.services and profile.apis:
            api_descriptions = await completed_thread(
                self.services.describe_apis, profile
            )
            profile = replace(
                profile,
                instructions=profile.instructions
                + "\n已授权的跨插件 API："
                + json.dumps(api_descriptions, ensure_ascii=False),
            )
        if tz_offset_minutes is not None:
            return await super().step(
                profile, history, tz_offset_minutes=tz_offset_minutes
            )
        return await super().step(profile, history)

    def before(self, context):
        profile = self.resolve_profile(Profile(**context["profile_snapshot"]))
        self.available(profile)
        if self.permitted and not self.permitted(context):
            raise PermissionError("当前真实来源或会话不在档案范围内")
        if profile.access == "admins" and not self.authorized(context):
            raise PermissionError("继续受限线程需要当前真实管理员授权")
        ctx_data = (
            self.memory.context(
                context["chat_id"] if context["owner"] == "chat" else ""
            )
            if self.memory
            else None
        )
        if (
            isinstance(ctx_data, dict)
            and context.get("owner") == "chat"
            and context.get("chat_id")
        ):
            plugin_ref = getattr(self, "_plugin_ref", None)
            if plugin_ref is not None and hasattr(plugin_ref, "chat_meta_for"):
                meta = plugin_ref.chat_meta_for(context["chat_id"])
                if meta:
                    ctx_data = {**ctx_data, **meta}
        return ctx_data

    def _has_companions(self, profile: Profile) -> bool:
        if self.memory is None:
            return False
        try:
            if self.memory.profile_files(profile.id):
                return True
        except (ValueError, OSError):
            pass
        for skill_id in profile.skills:
            try:
                if self.memory.skill_files(skill_id, profile_id=profile.id):
                    return True
            except (ValueError, OSError):
                continue
        return False

    @staticmethod
    def _record_protected_reference(work, rel_sub: str, raw_data: bytes) -> None:
        if not rel_sub.startswith("references/"):
            return
        refs = getattr(work, "protected_references", None)
        if not isinstance(refs, dict):
            refs = {"digests": set(), "texts": []}
            work.protected_references = refs
        refs["digests"].add(hashlib.sha256(raw_data).hexdigest())
        if b"\x00" not in raw_data[:4096]:
            compact = re.sub(
                r"\s+", "", raw_data.decode("utf-8", errors="ignore")
            )
            if len(compact) >= 32:
                refs["texts"].append(compact)

    def _sync_profile_companions(self, row, work):
        if self.memory is None:
            return
        try:
            companions = self.memory.read_profile_companions(str(row.get("profile") or ""))
        except (ValueError, OSError):
            return
        for rel_sub, raw_data in companions:
            rel = f"in/profile/{rel_sub}"
            write_workspace_bytes(work.path, rel, raw_data, overwrite=True)
            self._record_protected_reference(work, rel_sub, raw_data)

    def _sync_skill_companions(self, row, work):
        if self.memory is None:
            return
        try:
            live = self.memory.profiles.get(row["profile"])
            skills = live.skills
        except (ValueError, OSError):
            skills = tuple((row.get("profile_snapshot") or {}).get("skills") or ())
        for skill_id in skills:
            try:
                companions = self.memory.read_skill_companions(
                    skill_id, profile_id=str(row.get("profile") or "")
                )
            except (ValueError, OSError):
                continue
            for rel_sub, raw_data in companions:
                rel = f"in/skills/{skill_id}/{rel_sub}"
                write_workspace_bytes(work.path, rel, raw_data, overwrite=True)
                self._record_protected_reference(work, rel_sub, raw_data)

    async def _work(self, row):
        thread_id = row["thread_id"]
        if thread_id not in self.sessions:

            def enter():
                manager = self.files.activity(row)
                work = manager.__enter__()
                self._sync_profile_companions(row, work)
                self._sync_skill_companions(row, work)
                self.sessions[thread_id] = manager, work
                return work

            await completed_thread(enter)
        return self.sessions[thread_id][1]

    async def prepare(self, row, metadata):
        references = metadata.get("references", [])
        previous_threads = metadata.get("from_threads", [])
        if not references and not previous_threads:
            return []
        if (
            not isinstance(references, list)
            or len(references) > 10
            or not all(
                isinstance(value, str) and len(value) <= 256 for value in references
            )
        ):
            raise ValueError("引用输入无效")
        if not isinstance(previous_threads, list) or len(previous_threads) > 5:
            raise ValueError("引用线程无效")
        resolved_profile = await completed_thread(
            self.resolve_profile, Profile(**row["profile_snapshot"])
        )
        if not resolved_profile.uses_workspace:
            return []
        if row["owner"] == "self":
            raise PermissionError("自身活动不自动导入会话引用输入")
        await completed_thread(self.before, row)
        work = await self._work(row)
        imported = []
        for reference_index, message_id in enumerate(references):
            raw = checked(
                await call_capability(
                    self.host,
                    "message.get_by_id",
                    timeout_ms=30000,
                    message_id=message_id,
                    chat_id=row["chat_id"],
                    include_binary_data=True,
                ),
                "message",
            )
            if not isinstance(raw, dict) or raw.get("session_id") != row["chat_id"]:
                raise PermissionError("引用输入不属于本会话")
            for index, part in enumerate(raw.get("raw_message", [])):
                if part.get("type") not in {"image", "emoji", "voice", "file"}:
                    continue
                data = part.get("data") if isinstance(part.get("data"), dict) else {}
                filename = (
                    re.sub(
                        r"[^\w.-]", "_", str(data.get("name") or f"input-{index}.bin")
                    )[:80].rstrip(". ")
                    or f"input-{index}.bin"
                )
                reference_key = hashlib.sha256(message_id.encode()).hexdigest()[:12]
                relative = f"in/ref-{reference_key}-{index}-{filename}"
                binary = part.get("binary_data_base64") or data.get("base64")
                if binary:
                    if not isinstance(binary, str) or len(binary) > 8 * 1024**2:
                        raise ValueError("引用附件过大")
                    content = base64.b64decode(binary, validate=True)
                elif data.get("url"):
                    if not self.sandbox.ready:
                        raise RuntimeError("下载外部链接引用文件需要启用沙箱")
                    url = safe_url(data["url"])
                    cache_name = (
                        "reference-"
                        + hashlib.sha256(
                            (message_id + str(index)).encode()
                        ).hexdigest()[:16]
                    )
                    program = (
                        "import urllib.request\nurl="
                        + repr(url)
                        + "\npath="
                        + repr("/work/cache/" + cache_name)
                        + '\nwith urllib.request.urlopen(url,timeout=15) as response,open(path,"wb") as output:\n total=0\n while chunk:=response.read(65536):\n  total+=len(chunk)\n  if total>6*1024**2: raise ValueError("input too large")\n  output.write(chunk)\n'
                    )
                    result = await self.sandbox.execute(
                        row,
                        work,
                        "python -c " + shlex.quote(program),
                        call_id="reference-" + cache_name,
                        mode=row["profile_snapshot"].get("network", "public"),
                    )
                    if result.get("exit_code") != 0:
                        raise RuntimeError("引用文件下载失败")

                    def _read_cached_ref():
                        with regular_open(work.path, "cache/" + cache_name) as source:
                            return source.read(6 * 1024**2 + 1)

                    content = await completed_thread(_read_cached_ref)
                elif data.get("file_ref"):
                    content = await self.services.input_file(
                        Profile(**row["profile_snapshot"]),
                        message_id,
                        data["file_ref"],
                        row,
                        self,
                    )
                else:
                    raise ValueError(
                        "引用文件只有平台元数据，需要可读取的原文件或下载地址"
                    )
                await completed_thread(self._import_bytes, work, relative, content)
                imported.append(
                    {"path": relative, "message_id": message_id, "bytes": len(content)}
                )
        grant_obj = row.get("grant") or {}
        allow_any_chat = bool(
            grant_obj.get("scheduled_admin")
            or grant_obj.get("admin_direct")
            or grant_obj.get("console_admin")
            or (row.get("owner") == "self" and grant_obj.get("admin_verified"))
        )
        target_arena = (
            await completed_thread(
                self._chat_arena, row["chat_id"], row.get("platform", "")
            )
            if row.get("owner") == "chat"
            else "self"
        )
        admin_checked: bool | None = None
        for previous_id in previous_threads:
            previous = await completed_thread(
                self.store.get,
                previous_id,
                row["chat_id"],
                allow_self=True,
                allow_any_chat=allow_any_chat,
            )
            prev_vis = str(
                previous.get("visibility_scope")
                or initial_thread_scope(
                    owner=previous.get("owner") or "chat",
                    chat_id=previous.get("chat_id") or "",
                )
            )
            if target_arena == "group" and (
                (
                    previous.get("owner") == "chat"
                    and await completed_thread(
                        self._chat_arena,
                        previous["chat_id"],
                        previous.get("platform", ""),
                    )
                    == "private"
                )
                or prev_vis.startswith("private:")
            ):
                if admin_checked is None:
                    admin_checked = bool(await completed_thread(self.authorized, row))
                prev_rev = int(
                    previous.get("completed_revision") or previous.get("revision") or 0
                )
                published = bool(
                    hasattr(self.store, "has_publication_grant")
                    and await completed_thread(
                        self.store.has_publication_grant,
                        previous["thread_id"],
                        prev_rev,
                        row["chat_id"],
                    )
                )
                if not (admin_checked and published):
                    raise PermissionError("私聊线程结果默认不可直接注入群聊线程")
            if prev_vis == "admin_only":
                if admin_checked is None:
                    admin_checked = bool(await completed_thread(self.authorized, row))
                if not admin_checked:
                    raise PermissionError("引用受限线程需要当前管理员授权")
            if hasattr(self.store, "narrow_scope"):
                await completed_thread(
                    self.store.narrow_scope, row["thread_id"], prev_vis
                )
            prev_profiles = [Profile(**previous["profile_snapshot"])]
            if self.memory:
                try:
                    live_prev = await completed_thread(
                        self.memory.profiles.get, previous["profile"]
                    )
                    prev_profiles.append(live_prev)
                except (ValueError, OSError):
                    pass
            if any(prof.access == "admins" for prof in prev_profiles):
                if admin_checked is None:
                    admin_checked = bool(await completed_thread(self.authorized, row))
                if not admin_checked:
                    raise PermissionError("引用受限线程需要当前管理员授权")
            for prof in prev_profiles:
                if prof.access == "listed":
                    if admin_checked is None:
                        admin_checked = bool(await completed_thread(self.authorized, row))
                    if not await completed_thread(
                        self._listed_profile_permits,
                        prof,
                        row,
                        is_admin=admin_checked,
                    ):
                        raise PermissionError("引用受限线程不在当前会话或来源白名单内")
            prev_res = self.redact(previous.get("result") or {})
            prev_text = str(prev_res.get("text") or "").strip()
            prev_links = prev_res.get("links") or []
            if prev_text or prev_links:
                lines = [
                    f"# 上游线程 {previous_id} ({previous.get('profile', '')}) 结论",
                    "",
                ]
                if prev_text:
                    lines.append(prev_text)
                if prev_links:
                    lines.extend(["", "## 参考链接"])
                    for link_item in prev_links:
                        url_val = (
                            link_item.get("url", "")
                            if isinstance(link_item, dict)
                            else str(link_item)
                        )
                        if url_val:
                            lines.append(f"- {url_val}")
                summary_bytes = ("\n".join(lines).strip() + "\n").encode("utf-8")
                if not self.vault.contains_secret(summary_bytes):
                    rel_summary = f"in/previous-{previous_id}-result.md"
                    await completed_thread(
                        self._import_bytes,
                        work,
                        rel_summary,
                        summary_bytes,
                        overwrite=True,
                    )
                    imported.append(
                        {
                            "path": rel_summary,
                            "thread_id": previous_id,
                            "kind": "result_text",
                            "bytes": len(summary_bytes),
                        }
                    )
            attached_signatures = set()
            for index, attachment in enumerate(
                previous["result"].get("attachments", [])
            ):
                path = await completed_thread(
                    self.files.resolve, attachment["handle"], previous["chat_id"]
                )
                content = await completed_thread(path.read_bytes)
                attached_signatures.add(
                    (
                        str(attachment.get("name") or ""),
                        hashlib.sha256(content).hexdigest(),
                    )
                )
                safe_att_name = (
                    re.sub(r"[^\w.-]", "_", attachment["name"])[:80].rstrip(". ")
                    or f"attachment-{index}.bin"
                )
                relative = f"in/previous-{previous_id}-{index}-{safe_att_name}"
                await completed_thread(
                    self._import_bytes, work, relative, content, overwrite=True
                )
                imported.append(
                    {"path": relative, "thread_id": previous_id, "bytes": len(content)}
                )
            same_workspace = bool(
                previous.get("workspace_id")
                and previous.get("workspace_id") == row.get("workspace_id")
            )
            if not same_workspace:
                out_artifacts = await completed_thread(
                    self.files.read_checkpoint_outputs, previous
                )
                for subpath, out_bytes in out_artifacts:
                    leaf_name = subpath.rsplit("/", 1)[-1]
                    if (
                        leaf_name,
                        hashlib.sha256(out_bytes).hexdigest(),
                    ) in attached_signatures:
                        continue
                    rel_out = f"in/previous-{previous_id}-out/{subpath}"
                    try:
                        await completed_thread(
                            self._import_bytes,
                            work,
                            rel_out,
                            out_bytes,
                            overwrite=True,
                        )
                    except ValueError:
                        continue
                    imported.append(
                        {
                            "path": rel_out,
                            "thread_id": previous_id,
                            "kind": "workspace_out",
                            "bytes": len(out_bytes),
                        }
                    )
        await completed_thread(work.commit)
        return imported

    def _chat_arena(self, chat_id: str, platform: str = "") -> str:
        cid = str(chat_id or "").strip()
        if not cid:
            return ""
        if cid == "self":
            return "self"
        parsed = parse_stream_spec(cid)
        if parsed is not None:
            return parsed[1]
        candidates = [cid]
        aliases_fn = getattr(self.store, "chat_aliases", None)
        if callable(aliases_fn):
            try:
                for alias in sorted(set(aliases_fn(cid, platform)) - {cid}):
                    if isinstance(alias, str) and alias:
                        parsed_alias = parse_stream_spec(alias)
                        if parsed_alias is not None:
                            return parsed_alias[1]
                        candidates.append(alias)
            except (TypeError, ValueError):
                pass
        with self.store.connect() as db:
            has_routes = (
                db.execute(
                    'SELECT 1 FROM sqlite_master WHERE type="table" AND name="session_routes"'
                ).fetchone()
                is not None
            )
            if has_routes:
                placeholders = ",".join("?" for _ in candidates)
                rows = db.execute(
                    f"SELECT chat_type, group_id, user_id FROM session_routes WHERE session_id IN ({placeholders})",
                    tuple(candidates),
                ).fetchall()
                for route in rows:
                    ctype = str(route["chat_type"] or "").strip().lower()
                    if ctype in {"group", "private"}:
                        return ctype
                    if str(route["group_id"] or "").strip():
                        return "group"
                    if str(route["user_id"] or "").strip():
                        return "private"
        return ""

    def _listed_profile_permits(
        self, profile_obj: Profile, row: dict, *, is_admin: bool = False
    ) -> bool:
        chat_id = str(row.get("chat_id") or "")
        platform = str(row.get("platform") or "")
        plat = platform.strip().lower()
        chat_candidates = [chat_id] if chat_id else [""]
        aliases_fn = getattr(self.store, "chat_aliases", None)
        if chat_id and callable(aliases_fn):
            try:
                for alias in sorted(set(aliases_fn(chat_id, platform)) - {chat_id}):
                    if isinstance(alias, str) and alias:
                        chat_candidates.append(alias)
            except (TypeError, ValueError):
                pass
        raw_uid = str(row.get("user_id") or "")
        user_candidates = [raw_uid]
        if plat and raw_uid.startswith(f"{plat}:private:"):
            user_candidates.append(raw_uid.split(":", 2)[2])
        elif plat and raw_uid.startswith(f"{plat}:"):
            user_candidates.append(raw_uid.split(":", 1)[1])
        return any(
            profile_obj.permits(cid, platform, user_id=cand_uid, is_admin=is_admin)
            for cid in chat_candidates
            for cand_uid in user_candidates
        )

    def _import_bytes(self, work, relative, content, *, overwrite=False):
        existing_size = 0
        existed = False
        try:
            with regular_open(work.path, relative) as previous:
                existed = True
                old_bytes = previous.read(6 * 1024**2 + 1)
                if old_bytes == content:
                    return
                existing_size = len(old_bytes)
            if not overwrite:
                raise ValueError("引用附件内容已变化，请使用新的消息引用")
        except FileNotFoundError:
            pass
        files, used = self.files.inventory(work.path)
        if (
            not content
            or len(content) > 6 * 1024**2
            or used - existing_size + len(content) > self.files.max_bytes
            or (not existed and len(files) >= self.files.max_files)
        ):
            raise ValueError("引用文件为空或超过配额")
        write_workspace_bytes(work.path, relative, content, overwrite=overwrite)

    async def call(self, name, arguments, context):
        validate_arguments(name, arguments)
        profile = await completed_thread(
            self.resolve_profile, Profile(**context["profile_snapshot"])
        )
        await completed_thread(self.available, profile)
        if self.permitted and not await completed_thread(self.permitted, context):
            raise PermissionError("当前真实来源或会话不在档案范围内")
        if profile.access == "admins" and not await completed_thread(
            self.authorized, context
        ):
            raise PermissionError("本次活动的管理员授权已失效")
        if name == "calc":
            from .calc_engine import run_calc_tool

            return await completed_thread(run_calc_tool, arguments)
        if name in {
            "workspace_file",
            "html_render",
            "result_attach",
            "digest_feed",
            "render_report",
            "package_export",
        }:
            if not profile.uses_workspace:
                if name == "workspace_file" and await completed_thread(self._has_companions, profile):
                    ws_act = arguments.get("action")
                    ws_path = str(arguments.get("path") or "")
                    if ws_act != "list" and not (ws_act == "read" and ws_path.startswith("in/")):
                        raise PermissionError("当前角色未启用工作区工具，只读角色仅可查阅 in/ 下的附属资料")
                else:
                    raise PermissionError("当前角色未启用工作区工具")
            work = await self._work(context)
            if name == "package_export":
                from .packages import export_package

                plugin_ref = getattr(self, "_plugin_ref", None)
                if plugin_ref is None:
                    raise RuntimeError("导出插件实例未就绪")
                target_pid = str(arguments.get("profile_id") or "").strip() or profile.id
                target_card = await completed_thread(plugin_ref.profiles.get, target_pid)
                is_adm = bool(await completed_thread(self.authorized, context))
                if target_card.access == "admins" and not is_adm:
                    raise PermissionError("导出管理员专属角色需要已核验管理员触发")
                if target_card.access == "listed":
                    if not await completed_thread(
                        self._listed_profile_permits,
                        target_card,
                        context,
                        is_admin=is_adm,
                    ):
                        raise PermissionError("目标角色不在当前会话可用范围内")
                pkg_kind = str(arguments.get("kind") or "template").strip().lower()
                pkg = await completed_thread(
                    export_package,
                    plugin_ref,
                    target_pid,
                    kind=pkg_kind,
                    is_admin=is_adm,
                    caller_chat_id=str(context.get("chat_id") or ""),
                    caller_platform=str(context.get("platform") or ""),
                )
                rel_path = f"out/exports/{pkg['filename']}"
                await completed_thread(work.write_bytes, rel_path, pkg["zip_bytes"])
                await completed_thread(work.commit)
                handles = await completed_thread(
                    self.files.attach, context, work, [rel_path]
                )
                existing = list(self.attachments.get(context["thread_id"], ()))
                seen_keys = {
                    (item.get("handle"), item.get("path"))
                    for item in existing
                    if isinstance(item, dict)
                }
                for item in handles:
                    key = (item.get("handle"), item.get("path")) if isinstance(item, dict) else item
                    if key not in seen_keys:
                        seen_keys.add(key)
                        existing.append(item)
                self.attachments[context["thread_id"]] = existing
                return {
                    "ok": True,
                    "profile_id": pkg["profile_id"] or target_pid,
                    "kind": pkg["kind"],
                    "kind_label": pkg["kind_label"],
                    "filename": pkg["filename"],
                    "path": rel_path,
                    "attachment_handle": handles[0]["handle"] if handles else "",
                    "desensitized": True,
                    "delivery_hint": "请让主脑调用 maisya_agent(action='share') 将导出的 .maisya.zip 压缩包文件直接发回聊天",
                }
            if name == "digest_feed":
                from .digest import run_digest_tool

                params = inspect.signature(run_digest_tool).parameters
                accepts_kwargs = any(
                    param.kind == inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
                extra_kwargs = {}
                if accepts_kwargs or "web" in params:
                    extra_kwargs["web"] = self.web
                if accepts_kwargs or "context" in params:
                    extra_kwargs["context"] = context
                if inspect.iscoroutinefunction(run_digest_tool):
                    return await run_digest_tool(work, arguments, **extra_kwargs)
                result = await completed_thread(
                    run_digest_tool, work, arguments, **extra_kwargs
                )
                if inspect.isawaitable(result):
                    result = await result
                return result
            if name == "render_report":
                from .report import render_report_artifact

                return await completed_thread(render_report_artifact, work, arguments)
            if name == "workspace_file":
                action = arguments["action"]
                if action == "list":
                    return await completed_thread(work.listing)
                if action == "read":
                    return await completed_thread(
                        work.read, arguments.get("path", ""), arguments.get("offset", 0)
                    )
                result = await completed_thread(
                    work.write, arguments.get("path", ""), arguments.get("content", "")
                )
                await completed_thread(work.commit)
                return result
            if name == "html_render":
                raw_html = arguments.get("html")
                if raw_html is not None and not isinstance(raw_html, str):
                    raise ValueError("html 必须为字符串")
                html_text = raw_html if isinstance(raw_html, str) and raw_html.strip() else ""
                if not html_text and arguments.get("html_path"):
                    def _read_html():
                        with regular_open(work.path, arguments["html_path"]) as src:
                            raw = src.read(524289)
                        if len(raw) > 524288:
                            raise ValueError("HTML 源文件超过 512KB 上限")
                        return raw.decode("utf-8", errors="replace")

                    html_text = await completed_thread(_read_html)
                if not isinstance(html_text, str) or not html_text.strip():
                    raise ValueError("html_render 需要提供非空 html 或 html_path")
                output_path = str(
                    arguments.get("output_path") or f"out/render-{os.urandom(6).hex()}.png"
                )
                if not output_path.startswith("out/"):
                    raise ValueError("渲染图片输出路径必须位于 out/ 目录下")
                viewport = {
                    "width": int(arguments.get("width", 960)),
                    "height": int(arguments.get("height", 640)),
                }
                try:
                    raw_rendered = await call_capability(
                        self.host,
                        "render.html2png",
                        timeout_ms=30000,
                        html=html_text,
                        selector=str(arguments.get("selector", "body")),
                        viewport=viewport,
                        device_scale_factor=float(
                            arguments.get("device_scale_factor", 2.0)
                        ),
                        full_page=bool(arguments.get("full_page", True)),
                        allow_network=False,
                    )
                    if (
                        isinstance(raw_rendered, dict)
                        and raw_rendered.get("success") is False
                        and _playwright_render_hint(
                            str(raw_rendered.get("error") or "")
                        )
                    ):
                        raise RuntimeError(str(raw_rendered.get("error") or ""))
                    rendered = checked(raw_rendered, "result")
                except RuntimeError as error:
                    hinted = _playwright_render_hint(str(error))
                    if hinted:
                        raise RuntimeError(hinted) from error
                    raise
                image_b64 = rendered.get("image_base64")
                if not isinstance(image_b64, str) or len(image_b64) > 12 * 1024**2:
                    raise RuntimeError("宿主渲染未返回有效图片数据")
                png_bytes = base64.b64decode(image_b64, validate=True)
                write_res = await completed_thread(
                    work.write_bytes, output_path, png_bytes
                )
                await completed_thread(work.commit)
                return {
                    **write_res,
                    "mime_type": rendered.get("mime_type", "image/png"),
                    "width": rendered.get("width"),
                    "height": rendered.get("height"),
                }
            attempts = self.attachment_attempts.get(context["thread_id"], 0)
            if attempts >= 2:
                return {
                    "error": "本轮附件已尝试修正一次，请直接返回可用文字与具体附件错误"
                }
            raw_paths = arguments["paths"]
            dedup_paths = (
                list(
                    dict.fromkeys(
                        PurePosixPath(p).as_posix()
                        if isinstance(p, str) and p.startswith("out/")
                        else p
                        for p in raw_paths
                    )
                )
                if isinstance(raw_paths, list) and raw_paths
                else raw_paths
            )
            try:
                attachments = await completed_thread(
                    self.files.attach, context, work, dedup_paths
                )
            except (ValueError, OSError) as exc:
                next_attempts = attempts + 1
                self.attachment_attempts[context["thread_id"]] = next_attempts
                if next_attempts >= 2:
                    return {
                        "error": f"本轮附件已尝试修正一次（{exc}），请直接返回可用文字与具体附件错误"
                    }
                raise
            existing = list(self.attachments.get(context["thread_id"], ()))
            by_path = self._attachment_by_path.setdefault(context["thread_id"], {})
            for norm_path, item in zip(dedup_paths, attachments):
                if not isinstance(item, dict) or not isinstance(norm_path, str):
                    continue
                old_handle = by_path.get(norm_path)
                if old_handle:
                    existing = [
                        cur
                        for cur in existing
                        if not (
                            isinstance(cur, dict)
                            and cur.get("handle") == old_handle
                        )
                    ]
                handle = str(item.get("handle") or "")
                if handle:
                    by_path[norm_path] = handle
            seen_keys = {
                (item.get("handle"), item.get("path"))
                for item in existing
                if isinstance(item, dict)
            }
            for item in attachments:
                key = (
                    (item.get("handle"), item.get("path"))
                    if isinstance(item, dict)
                    else item
                )
                if key not in seen_keys:
                    seen_keys.add(key)
                    existing.append(item)
            self.attachments[context["thread_id"]] = existing
            return {"attachments": attachments}
        if name in {"exec_command", "sandbox_exec"}:
            return await self._run_exec_tool(name, arguments, context, profile)
        service_ctx = _step_service_context(context)
        if name == "mcp_call":
            if not self.services:
                raise RuntimeError("MCP 服务尚未登记")
            return await self.services.call(profile, arguments, service_ctx, self)
        if name == "plugin_api":
            if not self.services:
                raise RuntimeError("跨插件 API 尚未登记")
            return await self.services.call_api(profile, arguments, service_ctx, self)
        return await super().call(name, arguments, context)

    def _resolve_trusted_script_path(
        self, profile: Profile, script_arg: str
    ) -> Path | None:
        if (
            not script_arg
            or script_arg.startswith("-")
            or not script_arg.endswith(".py")
            or self.memory is None
        ):
            return None
        profiles = getattr(self.memory, "profiles", None)
        if profiles is None:
            return None
        candidate_rel = Path(script_arg)
        if (
            candidate_rel.is_absolute()
            or ".." in candidate_rel.parts
            or len(candidate_rel.parts) > 8
        ):
            return None

        roots_to_try: list[tuple[Path, Path]] = []
        parts = candidate_rel.parts
        if len(parts) >= 5 and parts[:2] == ("in", "skills") and parts[3] == "scripts":
            skill_id = parts[2]
            if skill_id in set(profile.skills):
                rel_sub = Path(*parts[4:])
                roots_to_try.append(
                    (self.memory.root / "abilities/skills" / skill_id / "scripts", rel_sub)
                )
                preset_sk = self.memory._resolve_preset_skill_dir(
                    skill_id, profile_id=profile.id
                )
                if preset_sk is not None:
                    roots_to_try.append((preset_sk / "scripts", rel_sub))
        elif len(parts) >= 4 and parts[:3] == ("in", "profile", "scripts"):
            rel_sub = Path(*parts[3:])
            roots_to_try.append((Path(profiles.path) / profile.id / "scripts", rel_sub))
            roots_to_try.append(
                (Path(profiles.path) / profile.id / "preset/scripts", rel_sub)
            )
        else:
            rel_sub = (
                Path(*parts[2:])
                if parts[:2] == ("preset", "scripts")
                else (Path(*parts[1:]) if parts[:1] == ("scripts",) else candidate_rel)
            )
            roots_to_try.append((Path(profiles.path) / profile.id / "scripts", rel_sub))
            roots_to_try.append(
                (Path(profiles.path) / profile.id / "preset/scripts", rel_sub)
            )

        for allowed_dir, sub_path in roots_to_try:
            if path_is_link_like(allowed_dir) or not allowed_dir.is_dir():
                continue
            target_file = allowed_dir / sub_path
            if path_is_link_like(target_file) or _has_relative_link(allowed_dir, target_file):
                continue
            try:
                allowed_root = allowed_dir.resolve()
                resolved_script = target_file.resolve()
            except OSError:
                continue
            if resolved_script.is_relative_to(allowed_root) and resolved_script.is_file():
                return resolved_script
        return None

    def _run_trusted_preset_script(
        self, profile: Profile, raw_cmd: str, work, timeout: float
    ) -> ExecutionResult | None:
        if not isinstance(raw_cmd, str) or not raw_cmd.strip() or _SHELL_META_RE.search(raw_cmd):
            return None
        try:
            tokens = shlex.split(raw_cmd)
        except ValueError:
            return None
        if len(tokens) < 2:
            return None
        exe = os.path.basename(tokens[0]).lower()
        if exe not in {"python", "python3"}:
            return None
        resolved_script = self._resolve_trusted_script_path(profile, tokens[1])
        if resolved_script is None:
            return None
        extra_args = tokens[2:]
        if _has_path_escape([sys.executable, *extra_args], work.path, shell=False):
            return None
        clamped_timeout = max(1.0, min(float(timeout), 60.0))
        proc = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-c",
                _TRUSTED_SCRIPT_BOOT,
                str(resolved_script),
                *extra_args,
            ],
            cwd=str(work.path),
            env=minimal_subprocess_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        try:
            out_raw, err_raw = proc.communicate(timeout=clamped_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out_raw, err_raw = proc.communicate(timeout=2.0)
            stdout, trunc_out = _decode_and_redact(
                out_raw, limit=65536, vault=self.vault, secret_values=()
            )
            stderr, trunc_err = _decode_and_redact(
                err_raw, limit=65536, vault=self.vault, secret_values=()
            )
            return ExecutionResult(
                status="timeout",
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                truncated=trunc_out or trunc_err,
                error=f"预置脚本超时（>{clamped_timeout}s）",
                runner_name="preset_script",
            )
        stdout, trunc_out = _decode_and_redact(
            out_raw, limit=65536, vault=self.vault, secret_values=()
        )
        stderr, trunc_err = _decode_and_redact(
            err_raw, limit=65536, vault=self.vault, secret_values=()
        )
        status = "ok" if proc.returncode == 0 else "error"
        return ExecutionResult(
            status=status,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=trunc_out or trunc_err,
            error="" if proc.returncode == 0 else f"预置脚本退出码 {proc.returncode}",
            runner_name="preset_script",
        )

    async def _run_exec_tool(
        self, name: str, arguments: dict, context: dict, profile: Profile
    ) -> dict:
        is_admin = bool(await completed_thread(self.authorized, context))
        docker_ok = self.docker_available()
        work = await self._work(context)
        command = arguments["command"]
        timeout = float(arguments.get("timeout") or 60.0)
        risk_hint = str(arguments.get("risk_hint") or "").strip()
        raw_targets = arguments.get("targets") or profile.sandbox_targets or ()
        targets = tuple(str(t).strip() for t in raw_targets if str(t).strip())
        thread_id = str(context.get("thread_id") or "")
        chat_id = str(context.get("chat_id") or "")
        scope = f"chat:{chat_id}" if chat_id else "*"
        eff_perm = self._effective_permission_mode(profile)

        if not is_admin:
            if not docker_ok:
                if name == "exec_command":
                    preset_res = await completed_thread(
                        self._run_trusted_preset_script, profile, command, work, timeout
                    )
                    if preset_res is not None:
                        if preset_res.status == "timeout":
                            raise TimeoutError(self.redact(preset_res.error))
                        return self.redact(preset_res.to_dict())
                raise PermissionError(
                    "当前未启用隔离 Docker 沙箱，普通会话禁止执行任意 Shell 或代码命令；仅可使用工作区文件或档案内建只读固定脚本"
                )
            # Non-admin with Docker enabled: strictly no secrets, no host mounts, no host network
            req = ExecutionRequest(
                command=command,
                workspace=work.path,
                is_admin=False,
                actor=str(context.get("user_id") or ""),
                scope=scope,
                session_id=thread_id,
                permission_mode=eff_perm,
                secrets=(),
                timeout=timeout,
                shell=isinstance(command, str),
                risk_hint=risk_hint,
                targets=targets,
                mount_secret_files=False,
            )
            exec_res = await self.docker_runner.execute(req)
        else:
            strict_sandbox = name == "sandbox_exec" or profile.execution_mode == "sandbox"
            if strict_sandbox and not docker_ok:
                raise RuntimeError(
                    "当前档案要求容器沙箱隔离执行，但 Docker 沙箱未启用或未就绪（拒绝降级为宿主机执行）"
                )
            use_sandbox = strict_sandbox or (
                profile.execution_mode == "auto" and docker_ok
            )
            requested_secrets: list[str] = []
            if getattr(profile, "shared_secrets", True):
                for h in profile.secrets or ():
                    if h and h not in requested_secrets:
                        requested_secrets.append(h)
                arg_secrets = arguments.get("secrets")
                if isinstance(arg_secrets, (list, tuple)):
                    for raw_h in arg_secrets:
                        h = str(raw_h or "").strip()
                        if h and h not in requested_secrets:
                            requested_secrets.append(h)
                if self.vault is not None:
                    known_handles = await completed_thread(self.vault.handles)
                    cmd_str = (
                        command
                        if isinstance(command, str)
                        else " ".join(str(x) for x in command)
                    )
                    for h in known_handles:
                        env_name = "MAISYA_SECRET_" + re.sub(r"[^A-Za-z0-9_]", "_", h).upper()
                        if (
                            env_name in cmd_str or f"/run/secrets/{h}" in cmd_str
                        ) and h not in requested_secrets:
                            requested_secrets.append(h)
            if (
                context.get("profile_snapshot")
                and self.memory is not None
                and getattr(self.memory, "profiles", None) is not None
            ):
                prof_id = str(context.get("profile") or profile.id)
                if (self.memory.profiles.path / prof_id / "profile.toml").is_file():
                    live_prof = await completed_thread(self.memory.profiles.get, prof_id)
                    services_cfg = self.services.load() if self.services else {}
                    err = check_hot_edit_expansion(
                        Profile(**context["profile_snapshot"]),
                        live_prof,
                        services_cfg=services_cfg,
                        envelope=(context.get("grant") or {}).get("envelope"),
                        requested_secrets=requested_secrets,
                    )
                    if err:
                        raise PermissionError(err)
            secrets_tuple = tuple(requested_secrets)
            clean_env: dict[str, str] = {}
            raw_env = arguments.get("env")
            if isinstance(raw_env, dict):
                blocked_env = {
                    "PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "TMPDIR",
                    "TEMP", "TMP", "SHELL", "USER", "IFS", "ENV", "BASH_ENV",
                }
                for ek, ev in list(raw_env.items())[:32]:
                    k_str = str(ek or "").strip()
                    if (
                        not k_str
                        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k_str)
                        or k_str.upper() in blocked_env
                        or k_str.upper().startswith(("LD_", "DYLD_"))
                        or not isinstance(ev, str)
                        or len(ev) > 8192
                    ):
                        continue
                    clean_env[k_str] = ev
            plugin_ref = getattr(self, "_plugin_ref", None)
            ephemeral_items = (
                plugin_ref.get_ephemeral_secrets(chat_id)
                if plugin_ref is not None and callable(getattr(plugin_ref, "get_ephemeral_secrets", None))
                else []
            )
            for idx, e_item in enumerate(ephemeral_items, 1):
                e_val = str((e_item or {}).get("value") or "")
                if e_val:
                    clean_env.setdefault(f"MAISYA_TEMP_KEY_{idx}", e_val)
            extra_redacts = tuple(
                v for v in clean_env.values() if isinstance(v, str) and len(v) >= 4
            )
            raw_uid = str(context.get("user_id") or "").strip()
            plat = str(context.get("platform") or "").strip().lower()
            actor_id = (
                raw_uid
                if (":" in raw_uid or not plat or not raw_uid)
                else f"{plat}:{raw_uid}"
            ) or str((context.get("grant") or {}).get("issuer_admin") or "admin")
            req = ExecutionRequest(
                command=command,
                workspace=work.path,
                is_admin=True,
                actor=actor_id,
                scope=scope,
                session_id=thread_id,
                permission_mode=eff_perm,
                secrets=secrets_tuple,
                extra_redactions=extra_redacts,
                env=clean_env,
                timeout=timeout,
                shell=isinstance(command, str),
                risk_hint=risk_hint,
                targets=targets,
                mount_secret_files=bool(secrets_tuple),
                unisolated_host=not use_sandbox,
            )
            if secrets_tuple and thread_id and hasattr(self.store, "narrow_scope"):
                await completed_thread(self.store.narrow_scope, thread_id, "admin_only")
            runner = self.docker_runner if use_sandbox else self.host_runner
            exec_res = await runner.execute(req)

        if (
            exec_res.status == "approval_required"
            and exec_res.approval_required is not None
        ):
            pending = await completed_thread(
                self.record_pending_approval,
                thread_id=thread_id,
                tool_call_id=str(context.get("_tool_call_id") or ""),
                tool=name,
                approval=exec_res.approval_required,
                scope=scope,
                session_id=thread_id,
                extra_redactions=extra_redacts if is_admin else (),
            )
            return self.redact(
                {
                    "status": "approval_required",
                    "pending_approval": True,
                    "request_id": pending["request_id"],
                    "action_key": pending["action_key"],
                    "risk_level": pending["risk_level"],
                    "category": pending["category"],
                    "command": pending["command"],
                    "reason": pending["reason"],
                    "summary": pending["summary"],
                }
            )
        if exec_res.status == "denied":
            raise PermissionError(self.redact(exec_res.error or "执行请求被安全边界拒绝"))
        return self.redact(exec_res.to_dict())

    def result(self, thread_id, result):
        attachments = self.attachments.get(thread_id)
        return {**result, **({"attachments": attachments} if attachments else {})}

    def ensure_learning(self):
        if self.learning is None and self.memory is not None:
            from .learning import Learning

            intents = getattr(getattr(self, "_plugin_ref", None), "intents", None)
            self.learning = Learning(self.host, self.memory, intents=intents)
        return self.learning

    async def finished(self, row):
        if not self.memory:
            return
        try:
            await completed_thread(self.memory.experience, row)
            learning = self.ensure_learning()
            if learning is not None:
                await learning.schedule(row)
        except (ValueError, RuntimeError, TypeError, OSError, sqlite3.Error):
            return

    async def end(self, thread_id):
        self.attachments.pop(thread_id, None)
        self.attachment_attempts.pop(thread_id, None)
        session = self.sessions.pop(thread_id, None)
        if session:
            manager, _work = session
            await completed_thread(manager.__exit__, None, None, None)

    async def stopped(self, thread_id):
        await self.services.close_thread(thread_id)
        if thread_id in self.sandbox.containers:
            raise RuntimeError(self.sandbox.error or "执行容器清理未确认")

    async def configure(self, settings):
        await self.sandbox.close()
        await completed_thread(self.files.release_quarantines)
        await self.services.release_quarantines()
        self.sandbox_settings = (
            settings if isinstance(settings, SandboxSettings) else SandboxSettings()
        )
        cli_runner = (
            self.docker_runner.cli_runner if self._custom_docker_runner else None
        )
        self.docker_runner = DockerRunner(
            self.sandbox_settings,
            vault=self.vault,
            grants=self.grants,
            cli_runner=cli_runner,
        )
        self.sandbox = Sandbox(
            settings, self.store, redact=self.vault.redact, authorized=self.authorized
        )
        await self.sandbox.probe()
        self.files.volume = self.sandbox.volume if settings.enabled else None

    async def close(self):
        if self.learning:
            await self.learning.close()
        await self.sandbox.close()
        await self.services.release_quarantines()
        if self.services:
            await self.services.close()
        for thread_id in list(self.sessions):
            await self.end(thread_id)
        await completed_thread(self.files.release_quarantines)
