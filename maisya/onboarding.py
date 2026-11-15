"""Maisya 五步启动引导与 7 项就绪清单管理。

纯后端状态机与防竞态模型探测模块，供 Wave 2 控制台 API 与插件初始化接入：
- 五步引导：`hello -> admins -> chats -> runtime -> done`
- 状态持久化在插件私有目录 `onboarding.json`（0600 原子写，拒绝符号链接）
- 使用单调递增 `(run_id, sequence)` 票据防并发/过期回调覆盖
- 严格过滤任何宿主或探测输出中的 `api_key`、`base_url` 与令牌片段
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .pathsafe import path_is_link_like
from .vault import atomic_private_write

STEPS: tuple[str, ...] = ("hello", "admins", "chats", "runtime", "done")
STATUSES: tuple[str, ...] = ("not_started", "in_progress", "completed", "skipped")
TOOLSETS: tuple[str, ...] = ("context", "web", "workspace", "exec", "schedule")

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{4,}\b")
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|base[_-]?url|authorization|secret|token|password)\s*[:=]\s*\S+"
)


def _sanitize_text(value: Any, *, max_len: int = 240) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=[已脱敏]", text)
    text = _BEARER_RE.sub("Bearer [已脱敏]", text)
    text = _SK_RE.sub("[已脱敏]", text)
    text = _URL_RE.sub("[URL已脱敏]", text)
    return text[:max_len]


def _sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return ""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if any(
                tok in key.lower()
                for tok in ("api_key", "apikey", "base_url", "baseurl", "secret", "token", "password", "auth")
            ):
                continue
            out[key] = _sanitize_payload(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_payload(item, depth=depth + 1) for item in value[:64]]
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _sanitize_text(value)


def _extract_settings_dict(settings: Any) -> dict[str, Any]:
    if settings is None:
        return {}
    if isinstance(settings, dict):
        return settings
    dump = getattr(settings, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _extract_safe_models(host_model_catalog: Any) -> list[dict[str, Any]]:
    if not host_model_catalog:
        return []
    raw_models = (
        host_model_catalog.get("models")
        if isinstance(host_model_catalog, dict)
        else host_model_catalog
    )
    if not isinstance(raw_models, (list, tuple)):
        return []
    safe_models: list[dict[str, Any]] = []
    for item in raw_models[:128]:
        if not isinstance(item, dict):
            continue
        name = _sanitize_text(item.get("name") or item.get("identifier") or "", max_len=120)
        ident = _sanitize_text(item.get("identifier") or item.get("name") or "", max_len=120)
        provider = _sanitize_text(item.get("provider") or "", max_len=80)
        tasks = item.get("assigned_tasks") or []
        safe_tasks = [_sanitize_text(t, max_len=40) for t in tasks[:16]] if isinstance(tasks, (list, tuple)) else []
        if name or ident:
            safe_models.append(
                {
                    "name": name or ident,
                    "identifier": ident or name,
                    "provider": provider,
                    "assigned_tasks": [t for t in safe_tasks if t],
                }
            )
    return safe_models


def _default_model_verify(*, run_id: str = "", sequence: int = 0) -> dict[str, Any]:
    return {
        "run_id": run_id or secrets.token_hex(8),
        "sequence": max(0, int(sequence)),
        "state": "idle",
        "model_name": "",
        "duration_ms": 0,
        "message": "",
        "error_code": "",
    }


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "status": "not_started",
        "current_step": "hello",
        "dismissed_at": None,
        "completed_steps": [],
        "admin_verify": {},
        "chat_scope": {},
        "runtime_verify": {},
        "model_verify": _default_model_verify(),
    }


def compute_readiness_checklist(
    *,
    settings: Any = None,
    host_model_catalog: Any = None,
    host_admins: set[str] | list[str] | tuple[str, ...] | None = None,
    mcp_servers: dict[str, Any] | list[Any] | None = None,
    docker_status: dict[str, Any] | None = None,
    default_profile_ready: bool | None = None,
    profiles_path: Path | None = None,
    model_verify: dict[str, Any] | None = None,
    runtime_verify: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算 7 项就绪清单与 `usable` 核心可用门槛。"""
    sdict = _extract_settings_dict(settings)
    plugin_cfg = sdict.get("plugin") if isinstance(sdict.get("plugin"), dict) else {}
    plugin_enabled = bool(plugin_cfg.get("enabled", True)) if sdict else False

    host_admin_set = {str(x).strip() for x in (host_admins or ()) if str(x).strip()}
    extra_admins = {str(x).strip() for x in (sdict.get("admins") or ()) if str(x).strip()}
    all_admins = host_admin_set | extra_admins
    admins_ok = bool(all_admins)

    enabled_chats = [str(c).strip() for c in (sdict.get("enabled_chats") or ()) if str(c).strip()]
    safe_models = _extract_safe_models(host_model_catalog)
    configured_default_model = _sanitize_text(sdict.get("default_model") or "", max_len=120)
    mv = model_verify if isinstance(model_verify, dict) else {}
    mv_state = str(mv.get("state") or "idle")
    rv = runtime_verify if isinstance(runtime_verify, dict) else {}
    perm_mode = str(sdict.get("permission_mode") or rv.get("permission_mode") or "ask_risky")

    # 1) 管理员已配置
    item_admins = {
        "key": "admins",
        "required": True,
        "state": "ok" if admins_ok else "off",
        "title": "管理员已配置",
        "detail": f"已识别 {len(all_admins)} 位管理员" if admins_ok else "至少需在宿主或配置中添加 1 位管理员",
        "step": "admins",
    }

    # 2) 允许会话已开启（至少 1 个启用会话，或插件开启时保留管理员私聊直通）
    if not plugin_enabled:
        chats_state, chats_detail = "off", "插件总开关未开启，且尚无可用会话"
    elif enabled_chats:
        chats_state, chats_detail = "ok", f"已开启 {len(enabled_chats)} 个会话（含管理员私聊直通）"
    elif admins_ok:
        chats_state, chats_detail = "warn", "插件已启用，当前仅允许管理员私聊直通；可在设置补充群聊"
    else:
        chats_state, chats_detail = "off", "尚未开启允许会话，且未配置管理员"
    item_chats = {
        "key": "chats",
        "required": True,
        "state": chats_state,
        "title": "允许会话已开启",
        "detail": chats_detail,
        "step": "chats",
    }

    # 3) 默认模型可用
    model_names = [m["name"] for m in safe_models]
    active_model = _sanitize_text(mv.get("model_name") or configured_default_model or (model_names[0] if model_names else ""))
    if mv_state == "failed":
        mod_state = "off"
        mod_detail = f"模型探测失败（{_sanitize_text(mv.get('error_code') or 'failed')}）：{_sanitize_text(mv.get('message') or '请重新探测')}"
    elif not safe_models and not configured_default_model and mv_state != "ok":
        mod_state = "off"
        mod_detail = "未检测到宿主可用模型，请先在宿主模型配置中设置主/任务模型"
    elif mv_state == "ok":
        mod_state = "ok"
        mod_detail = f"模型 {active_model or 'default'} 探测通过（{int(mv.get('duration_ms') or 0)} ms）"
    else:
        mod_state = "warn"
        mod_detail = f"已发现 {len(safe_models)} 个模型（当前：{active_model or '未指定'}），尚未执行连通探测"
    item_models = {
        "key": "models",
        "required": True,
        "state": mod_state,
        "title": "默认模型可用",
        "detail": mod_detail,
        "step": "runtime",
    }

    # 4) 默认档案就绪（default preset 可读）
    if default_profile_ready is not None:
        prof_ready: bool | None = bool(default_profile_ready)
    elif profiles_path is not None:
        p = Path(profiles_path)
        prof_ready = (p / "default" / "profile.toml").is_file() or (p / "scout" / "profile.toml").is_file()
    else:
        repo_presets = Path(__file__).resolve().parent.parent / "presets" / "profiles"
        if (repo_presets / "default" / "profile.toml").is_file() or (repo_presets / "scout" / "profile.toml").is_file():
            prof_ready = None
        else:
            prof_ready = False
    item_profile = {
        "key": "default_profile",
        "required": False,
        "state": "ok" if prof_ready is True else ("off" if prof_ready is False else "warn"),
        "title": "默认档案就绪",
        "detail": (
            "default 能力档案可读"
            if prof_ready is True
            else ("未找到 default 能力档案" if prof_ready is False else "待运行时确认 default 档案目录")
        ),
        "step": "runtime",
    }

    # 5) 工具组与 MCP 状态（5 大 Toolset + MCP）
    mcp_count = len(mcp_servers) if isinstance(mcp_servers, (dict, list, tuple)) else 0
    item_tools = {
        "key": "toolsets_mcp",
        "required": False,
        "state": "ok",
        "title": "工具组与 MCP 状态",
        "detail": f"5 大工具组（{', '.join(TOOLSETS)}）就绪，已登记 {mcp_count} 个只读 MCP 服务",
        "step": "runtime",
    }

    # 6) 宿主工具运行模式 / 沙箱状态
    ds = docker_status if isinstance(docker_status, dict) else {}
    docker_enabled = bool(ds.get("enabled", False))
    docker_ready = bool(ds.get("ready", False))
    host_runner_ready = bool(ds.get("host_runner_ready", True))
    if docker_enabled and docker_ready:
        rt_state, rt_detail = "ok", "Docker 隔离沙箱已就绪"
    elif docker_enabled and not docker_ready:
        rt_state = "warn" if host_runner_ready else "off"
        rt_detail = "Docker 已开启但暂未就绪，将回落宿主机受控工作区执行" if host_runner_ready else "Docker 与宿主工作区执行器均未就绪"
    else:
        rt_state = "ok" if host_runner_ready else "warn"
        rt_detail = "Docker 未启用，使用 HostWorkspaceRunner 宿主机受控工作区执行"
    item_runtime = {
        "key": "runtime",
        "required": False,
        "state": rt_state,
        "title": "运行模式与沙箱状态",
        "detail": rt_detail,
        "step": "runtime",
    }

    # 7) 安全提示确认
    safety_ack = bool(rv.get("safety_acknowledged", False))
    item_safety = {
        "key": "safety",
        "required": False,
        "state": "ok" if safety_ack else "warn",
        "title": "安全提示确认",
        "detail": (
            f"已确认 {perm_mode} 高风险确认门槛"
            if safety_ack
            else f"当前权限模式 {perm_mode}：建议在引导第 4 步确认高风险操作门槛"
        ),
        "step": "runtime",
    }

    items = [
        item_admins,
        item_chats,
        item_models,
        item_profile,
        item_tools,
        item_runtime,
        item_safety,
    ]
    missing = [it["key"] for it in items if (it["required"] and it["state"] == "off")]
    if item_profile["state"] == "off":
        missing.append("default_profile")
    return {
        "items": items,
        "usable": not missing,
        "missing_requirements": missing,
        "models": safe_models,
        "toolsets": list(TOOLSETS),
    }


class OnboardingManager:
    """持久化管理五步启动引导与防竞态模型连通探测。"""

    def __init__(self, path: Path, *, clock: Callable[[], float] | None = None):
        self.path = Path(path)
        self.clock = clock or time.time
        self._lock = threading.RLock()
        if path_is_link_like(self.path):
            raise ValueError("onboarding.json 不能是符号链接")

    def _normalize_loaded(self, raw: Any) -> dict[str, Any]:
        base = _default_state()
        if not isinstance(raw, dict):
            return base
        status = str(raw.get("status") or "not_started")
        base["status"] = status if status in STATUSES else "not_started"
        step = str(raw.get("current_step") or "hello")
        base["current_step"] = step if step in STEPS else "hello"
        dismissed = raw.get("dismissed_at")
        base["dismissed_at"] = float(dismissed) if isinstance(dismissed, (int, float)) else None
        completed = [str(s) for s in (raw.get("completed_steps") or ()) if str(s) in STEPS]
        base["completed_steps"] = list(dict.fromkeys(completed))
        for k in ("admin_verify", "chat_scope", "runtime_verify"):
            val = raw.get(k)
            base[k] = _sanitize_payload(val) if isinstance(val, dict) else {}
        mv_raw = raw.get("model_verify")
        if isinstance(mv_raw, dict):
            mv = _default_model_verify(
                run_id=str(mv_raw.get("run_id") or "") or secrets.token_hex(8),
                sequence=int(mv_raw.get("sequence") or 0),
            )
            state = str(mv_raw.get("state") or "idle")
            mv["state"] = state if state in {"idle", "running", "ok", "failed"} else "idle"
            mv["model_name"] = _sanitize_text(mv_raw.get("model_name") or "", max_len=120)
            mv["duration_ms"] = max(0, int(mv_raw.get("duration_ms") or 0))
            mv["message"] = _sanitize_text(mv_raw.get("message") or "")
            mv["error_code"] = _sanitize_text(mv_raw.get("error_code") or "", max_len=64)
            base["model_verify"] = mv
        return base

    def _read_locked(self) -> dict[str, Any]:
        if path_is_link_like(self.path):
            raise ValueError("onboarding.json 不能是符号链接")
        if not self.path.is_file():
            data = _default_state()
            self._write_locked(data)
            return data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        return self._normalize_loaded(raw)

    def _write_locked(self, data: dict[str, Any]) -> None:
        if path_is_link_like(self.path):
            raise ValueError("onboarding.json 不能是符号链接")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._normalize_loaded(data), ensure_ascii=False, indent=2).encode("utf-8")
        atomic_private_write(self.path, payload)

    def read(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._read_locked())

    def _build_view(
        self,
        state_data: dict[str, Any],
        *,
        settings: Any = None,
        host_model_catalog: Any = None,
        host_admins: set[str] | list[str] | tuple[str, ...] | None = None,
        mcp_servers: dict[str, Any] | list[Any] | None = None,
        docker_status: dict[str, Any] | None = None,
        existing_threads_count: int = 0,
        is_admin_view: bool = True,
        default_profile_ready: bool | None = None,
        profiles_path: Path | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        checklist = compute_readiness_checklist(
            settings=settings,
            host_model_catalog=host_model_catalog,
            host_admins=host_admins,
            mcp_servers=mcp_servers,
            docker_status=docker_status,
            default_profile_ready=default_profile_ready,
            profiles_path=profiles_path,
            model_verify=state_data.get("model_verify"),
            runtime_verify=state_data.get("runtime_verify"),
        )
        sdict = _extract_settings_dict(settings)
        plugin_cfg = sdict.get("plugin") if isinstance(sdict.get("plugin"), dict) else {}
        plugin_enabled = bool(plugin_cfg.get("enabled", False)) if sdict else False
        status = state_data["status"]
        is_existing_install = (int(existing_threads_count or 0) > 0) or (
            plugin_enabled and bool(checklist["usable"])
        )
        if not is_admin_view:
            show = False
        elif status == "in_progress":
            show = True
        elif status == "not_started":
            show = not is_existing_install
        else:
            show = False

        out = {
            **copy.deepcopy(state_data),
            "steps": list(STEPS),
            "show": show,
            "usable": checklist["usable"],
            "missing_requirements": checklist["missing_requirements"],
            "items": checklist["items"],
            "models": checklist["models"],
            "toolsets": checklist["toolsets"],
        }
        if extra:
            out.update(extra)
        return out

    def view(self, **view_kwargs) -> dict[str, Any]:
        with self._lock:
            return self._build_view(self._read_locked(), **view_kwargs)

    @staticmethod
    def _matches_ticket(state_data: dict[str, Any], run_id: str, sequence: int | None) -> bool:
        mv = state_data.get("model_verify") or {}
        cur_run = str(mv.get("run_id") or "")
        cur_seq = int(mv.get("sequence") or 0)
        if run_id and str(run_id) != cur_run:
            return False
        if sequence is not None and int(sequence) < cur_seq:
            return False
        return True

    def advance(
        self,
        step: str | None = None,
        *,
        run_id: str = "",
        sequence: int | None = None,
        data: dict[str, Any] | None = None,
        **view_kwargs,
    ) -> dict[str, Any]:
        with self._lock:
            state = self._read_locked()
            if not self._matches_ticket(state, run_id, sequence):
                return self._build_view(state, **view_kwargs, extra={"stale": True, "applied": False})

            prev_step = state["current_step"]
            if prev_step in STEPS and prev_step not in state["completed_steps"]:
                state["completed_steps"].append(prev_step)

            target_step = str(step or "").strip()
            if target_step:
                if target_step not in STEPS:
                    raise ValueError(f"未知引导步骤：{target_step}")
                state["current_step"] = target_step
            else:
                idx = STEPS.index(prev_step) if prev_step in STEPS else 0
                state["current_step"] = STEPS[min(idx + 1, len(STEPS) - 1)]

            if isinstance(data, dict):
                clean = _sanitize_payload(data)
                if target_step == "admins" or prev_step == "admins":
                    state["admin_verify"] = {**state.get("admin_verify", {}), **clean}
                elif target_step == "chats" or prev_step == "chats":
                    state["chat_scope"] = {**state.get("chat_scope", {}), **clean}
                elif target_step == "runtime" or prev_step == "runtime":
                    state["runtime_verify"] = {**state.get("runtime_verify", {}), **clean}

            state["status"] = "in_progress"
            state["dismissed_at"] = None
            self._write_locked(state)
            return self._build_view(state, **view_kwargs, extra={"stale": False, "applied": True})

    def skip(self, *, run_id: str = "", sequence: int | None = None, **view_kwargs) -> dict[str, Any]:
        with self._lock:
            state = self._read_locked()
            if not self._matches_ticket(state, run_id, sequence):
                return self._build_view(state, **view_kwargs, extra={"stale": True, "applied": False})
            state["status"] = "skipped"
            state["dismissed_at"] = float(self.clock())
            self._write_locked(state)
            return self._build_view(state, **view_kwargs, extra={"stale": False, "applied": True})

    def complete(self, *, run_id: str = "", sequence: int | None = None, **view_kwargs) -> dict[str, Any]:
        with self._lock:
            state = self._read_locked()
            if not self._matches_ticket(state, run_id, sequence):
                return self._build_view(state, **view_kwargs, extra={"stale": True, "applied": False, "completed": False})

            preview = self._build_view(state, **view_kwargs)
            state["current_step"] = "done"
            state["dismissed_at"] = None
            if preview["usable"]:
                if "done" not in state["completed_steps"]:
                    state["completed_steps"].append("done")
                state["status"] = "completed"
                completed = True
            else:
                state["status"] = "in_progress"
                completed = False
            self._write_locked(state)
            return self._build_view(state, **view_kwargs, extra={"stale": False, "applied": True, "completed": completed})

    def reset(self, **view_kwargs) -> dict[str, Any]:
        with self._lock:
            prev = self._read_locked()
            next_seq = int((prev.get("model_verify") or {}).get("sequence") or 0) + 1
            state = _default_state()
            state["status"] = "in_progress"
            state["current_step"] = "hello"
            state["model_verify"] = _default_model_verify(run_id=secrets.token_hex(8), sequence=next_seq)
            self._write_locked(state)
            return self._build_view(state, **view_kwargs, extra={"stale": False, "applied": True})

    def reopen(self, step: str | None = None, **view_kwargs) -> dict[str, Any]:
        with self._lock:
            state = self._read_locked()
            target = str(step or "").strip()
            if target:
                if target not in STEPS:
                    raise ValueError(f"未知引导步骤：{target}")
                state["current_step"] = target
            elif state["current_step"] not in STEPS:
                state["current_step"] = "hello"
            state["status"] = "in_progress"
            state["dismissed_at"] = None
            mv = state.get("model_verify") or {}
            mv["run_id"] = secrets.token_hex(8)
            mv["sequence"] = int(mv.get("sequence") or 0) + 1
            state["model_verify"] = mv
            self._write_locked(state)
            return self._build_view(state, **view_kwargs, extra={"stale": False, "applied": True})

    async def verify_model(
        self,
        probe_fn: Callable[[], Any] | None = None,
        *,
        model_name: str = "",
        timeout_seconds: float = 8.0,
        **view_kwargs,
    ) -> dict[str, Any]:
        started = time.monotonic()
        with self._lock:
            state = self._read_locked()
            mv = dict(state["model_verify"])
            ticket_run = str(mv.get("run_id") or "") or secrets.token_hex(8)
            ticket_seq = int(mv.get("sequence") or 0) + 1
            mv.update(
                {
                    "run_id": ticket_run,
                    "sequence": ticket_seq,
                    "state": "running",
                    "model_name": _sanitize_text(model_name, max_len=120),
                    "duration_ms": 0,
                    "message": "正在探测模型连通性",
                    "error_code": "",
                }
            )
            state["model_verify"] = mv
            self._write_locked(state)

        outcome_state = "failed"
        outcome_msg = ""
        outcome_err = ""
        outcome_model = _sanitize_text(model_name, max_len=120)

        if probe_fn is None:
            outcome_err = "no_probe"
            outcome_msg = "未提供模型探测回调"
        else:
            try:
                async def _run_probe():
                    res = probe_fn()
                    if inspect.isawaitable(res):
                        return await res
                    return res

                raw = await asyncio.wait_for(_run_probe(), timeout=max(0.01, float(timeout_seconds)))
                if isinstance(raw, dict):
                    ok = bool(raw.get("ok", False))
                    outcome_model = _sanitize_text(raw.get("model_name") or outcome_model, max_len=120)
                    outcome_msg = _sanitize_text(raw.get("message") or ("模型探测成功" if ok else "模型探测未通过"))
                    outcome_err = "" if ok else _sanitize_text(raw.get("error_code") or "probe_failed", max_len=64)
                    outcome_state = "ok" if ok else "failed"
                elif raw is True:
                    outcome_state = "ok"
                    outcome_msg = "模型探测成功"
                else:
                    outcome_state = "failed"
                    outcome_err = "probe_failed"
                    outcome_msg = _sanitize_text(raw or "模型探测未通过")
            except (asyncio.TimeoutError, TimeoutError):
                outcome_state = "failed"
                outcome_err = "timeout"
                outcome_msg = f"模型探测超时（>{float(timeout_seconds):.1f}s）"
            except Exception as exc:
                outcome_state = "failed"
                outcome_err = "probe_error"
                outcome_msg = f"模型探测失败：{_sanitize_text(exc)}"

        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        with self._lock:
            latest = self._read_locked()
            cur_mv = latest.get("model_verify") or {}
            if str(cur_mv.get("run_id") or "") != ticket_run or int(cur_mv.get("sequence") or 0) != ticket_seq:
                return self._build_view(latest, **view_kwargs, extra={"stale": True, "applied": False})

            latest["model_verify"] = {
                "run_id": ticket_run,
                "sequence": ticket_seq,
                "state": outcome_state,
                "model_name": outcome_model,
                "duration_ms": elapsed_ms,
                "message": outcome_msg,
                "error_code": outcome_err,
            }
            self._write_locked(latest)
            return self._build_view(latest, **view_kwargs, extra={"stale": False, "applied": True})
