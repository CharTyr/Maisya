"""意图唤醒入口：回接用户线程结果、处理到期的提醒与主动意图。"""

import asyncio
import html
import inspect
import json
import logging
import random
import re
import sqlite3
import time
from pathlib import Path

from .abilities import is_autonomous_snapshot
from .bridge import _grant_admin_direct
from .intents import (
    _validate_tz_offset_minutes,
    is_profileless_daily,
    next_daily_due,
)
from .memory import MemoryLayers
from .scout import call_capability
from .trust import parse_stream_spec

logger = logging.getLogger(__name__)

# 心跳间隔的随机抖动范围（基准间隔的倍数），见 docs/08 第二节
HEARTBEAT_JITTER = (0.7, 1.4)

_SERENDIPITY_FILLERS = (
    "刚发现",
    "一个",
    "今天",
    "看看",
    "不错",
    "可以",
    "这个",
    "那个",
    "尝试",
)
_SERENDIPITY_FILLER_SET = set(_SERENDIPITY_FILLERS)


def _pending_approval_of(result_obj) -> dict | None:
    """Return pending approval payload when a waiting thread paused for admin confirmation."""
    if not isinstance(result_obj, dict):
        return None
    pa = result_obj.get("pending_approval")
    if isinstance(pa, dict) and pa.get("pending_approval"):
        return pa
    return None


def _extract_tokens(text):
    lower = str(text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_]{2,}", lower))
    cleaned = lower
    for filler in _SERENDIPITY_FILLERS:
        cleaned = cleaned.replace(filler, " ")
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned):
        for i in range(len(run) - 1):
            tokens.add(run[i : i + 2])
    return tokens - _SERENDIPITY_FILLER_SET


class Pulse:
    def __init__(
        self,
        bridge,
        host,
        enabled,
        intents=None,
        engine=None,
        *,
        sleep_window="",
        proactive_level="care",
        chat_proactive_levels=None,
        acquaintance_users=None,
        admins=lambda: set(),
        enabled_chats=lambda: [],
        longing_quiet_hours=0,
        activity=None,
        platform_for=None,
        redact=lambda v: v,
        routes=None,
        memory=None,
        profiles=None,
        plugin_enabled=lambda: True,
        heartbeat_enabled=True,
        heartbeat_interval=3600,
        heartbeat_profile="default",
        chat_aliases=lambda chat_id, platform="": {chat_id},
        resolve_session=None,
        tz_offset_minutes=None,
        watch_evaluator=None,
        authority_check=None,
        daily_bg_runs_limit=48,
        daily_bg_tokens_limit=200000,
        bg_max_consecutive_failures=3,
    ):
        self.bridge, self.host, self.enabled = bridge, host, enabled
        self.intents, self.engine = intents, engine
        self.watch_evaluator = watch_evaluator
        self.authority_check = authority_check
        self.daily_bg_runs_limit = max(0, int(daily_bg_runs_limit))
        self.daily_bg_tokens_limit = max(0, int(daily_bg_tokens_limit))
        self.bg_max_consecutive_failures = max(1, int(bg_max_consecutive_failures))
        self.sleep_window, self.redact = sleep_window, redact
        if tz_offset_minutes is not None and not callable(tz_offset_minutes):
            tz_offset_minutes = _validate_tz_offset_minutes(tz_offset_minutes)
        self.tz_offset_minutes = tz_offset_minutes
        self.proactive_level = proactive_level
        self.chat_proactive_levels = dict(chat_proactive_levels or {})
        self.acquaintance_users = list(acquaintance_users or [])
        self.admins = admins
        self.enabled_chats = enabled_chats
        self.longing_quiet_hours = longing_quiet_hours
        self.activity = activity
        self.platform_for = platform_for
        self.routes = routes
        self.memory = memory
        self.profiles = profiles
        self.plugin_enabled = plugin_enabled
        self.heartbeat_enabled = heartbeat_enabled
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_profile = heartbeat_profile
        self.chat_aliases = chat_aliases
        self.resolve_session = resolve_session
        self.task = None
        self.closing = False
        self._heartbeat_next_at = 0.0
        self._heartbeat_last_clock = 0.0
        self._heartbeat_cached_mtime = None
        self._heartbeat_cached_size = None
        self._heartbeat_cached_parts = ("", "")
        self._heartbeat_interval_used = heartbeat_interval
        self._queued_heartbeat_tid = ""
        self.rng = random.random
        self._tick_lock = None
        self._tick_lock_loop = None

    def _get_tick_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._tick_lock is None or self._tick_lock_loop is not loop:
            self._tick_lock = asyncio.Lock()
            self._tick_lock_loop = loop
        return self._tick_lock

    def invalidate_caches(self):
        """清空心跳文件与节律冷却缓存，便于配置更新或测试时钟调整后立即重读。"""
        self._heartbeat_next_at = 0.0
        self._heartbeat_last_clock = 0.0
        self._heartbeat_cached_mtime = None
        self._heartbeat_cached_size = None
        self._heartbeat_cached_parts = ("", "")
        self._heartbeat_interval_used = self.heartbeat_interval

    async def _engine_call(self, method_name, *args, **kwargs):
        if self.engine is None:
            return None
        fn = getattr(self.engine, method_name, None)
        if fn is None:
            return None
        params = inspect.signature(fn).parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            filtered = kwargs
        else:
            filtered = {k: v for k, v in kwargs.items() if k in params}
        result = fn(*args, **filtered)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _thread_state_after_dispatch(
        self,
        tid: str,
        chat_id: str,
        started: dict | None = None,
    ) -> tuple[bool, bool, dict | None]:
        in_tasks = bool(
            tid
            and self.engine is not None
            and getattr(self.engine, "tasks", None)
            and self.engine.tasks.get(tid)
        )
        thread = None
        store = getattr(self.bridge, "store", None) if self.bridge is not None else None
        if tid and store is not None and hasattr(store, "get"):
            try:
                thread = await asyncio.to_thread(
                    store.get, tid, chat_id, allow_self=chat_id == "self"
                )
            except (ValueError, PermissionError, sqlite3.Error):
                thread = None
        if in_tasks:
            return True, False, thread
        if thread is not None:
            is_queued = (
                int(thread.get("processed_revision") or 0)
                < int(thread.get("revision") or 0)
                or thread.get("state") == "active"
            )
            return False, is_queued, thread
        is_queued = isinstance(started, dict) and started.get("state") == "active"
        return False, is_queued, None

    def _configured_tz_offset_minutes(self) -> int | None:
        raw = getattr(self, "tz_offset_minutes", None)
        if raw is None:
            return None
        if callable(raw):
            raw = raw()
            if raw is None:
                return None
        return _validate_tz_offset_minutes(raw)

    def _effective_tz_offset_minutes(self) -> int:
        configured = self._configured_tz_offset_minutes()
        if configured is not None:
            return configured
        for obj in (self.intents, self.memory):
            raw = getattr(obj, "tz_offset_minutes", None)
            if raw is None:
                continue
            if callable(raw):
                raw = raw()
            if raw is not None:
                try:
                    return _validate_tz_offset_minutes(raw)
                except ValueError:
                    continue
        return 480

    def _tz_kwarg_for(self, fn, params=None) -> dict[str, int]:
        tz_val = self._configured_tz_offset_minutes()
        if tz_val is None:
            return {}
        sig_params = inspect.signature(fn).parameters if params is None else params
        if "tz_offset_minutes" in sig_params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig_params.values()
        ):
            return {"tz_offset_minutes": tz_val}
        return {}

    def _in_sleep_window(self, sleep_window=None, *, now=None) -> bool:
        if self.intents is None:
            return False
        fn = self.intents.in_sleep_window
        params = inspect.signature(fn).parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        kwargs = self._tz_kwarg_for(fn, params)
        if now is not None and ("now" in params or has_var_kw):
            kwargs["now"] = now
        sw = self.sleep_window if sleep_window is None else sleep_window
        return bool(fn(sw, **kwargs))

    def _social_gate(self, target, *, sleep_window=None, now=None, aliases=()):
        fn = self.intents.social_gate
        params = inspect.signature(fn).parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        sw = self.sleep_window if sleep_window is None else sleep_window
        kwargs = {"sleep_window": sw, **self._tz_kwarg_for(fn, params)}
        if "aliases" in params or has_var_kw:
            kwargs["aliases"] = aliases
        if now is not None and ("now" in params or has_var_kw):
            kwargs["now"] = now
        return fn(target, **kwargs)

    def _proactive_quota_available(
        self,
        target: str = "",
        *,
        now=None,
        exclude_intent_id: str = "",
        payload: dict | None = None,
    ) -> bool:
        if self.intents is None:
            return False
        fn = self.intents.proactive_quota_available
        params = inspect.signature(fn).parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        extra_kw = self._tz_kwarg_for(fn, params)
        for key, val in (
            ("now", now),
            ("exclude_intent_id", exclude_intent_id),
            ("payload", payload),
        ):
            if val is not None and (key != "exclude_intent_id" or val) and (has_var_kw or key in params):
                extra_kw[key] = val
        target_param = params.get("target")
        if target_param is not None and target_param.kind == inspect.Parameter.KEYWORD_ONLY:
            return bool(fn(target=target, **extra_kw))
        if any(
            p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            )
            for p in params.values()
        ):
            return bool(fn(target, **extra_kw))
        if has_var_kw:
            return bool(fn(target=target, **extra_kw))
        return bool(fn(**extra_kw))

    def _chat_arena(self, chat_id: str) -> str:
        if callable(self.routes):
            try:
                route = self.routes(chat_id)
            except (ValueError, RuntimeError, sqlite3.Error):
                route = None
            if isinstance(route, dict):
                ctype = str(route.get("chat_type") or "").strip().lower()
                if ctype in {"group", "private"}:
                    return ctype
        parsed = parse_stream_spec(chat_id)
        if parsed is not None and parsed[1] in {"group", "private"}:
            return parsed[1]
        return ""

    def _with_previous_runs_prompt(self, prompt: str, payload: dict) -> str:
        if str(payload.get("workspace") or "").strip():
            return str(prompt)
        recent_runs = payload.get("recent_runs")
        if not recent_runs:
            return str(prompt)
        if isinstance(recent_runs, list):
            safe_runs = [
                {
                    **item,
                    "summary": self.redact(str(item.get("summary") or "")),
                }
                if isinstance(item, dict)
                else self.redact(str(item))
                for item in recent_runs
            ]
            serialized = json.dumps(safe_runs, ensure_ascii=False)
        else:
            serialized = self.redact(json.dumps(recent_runs, ensure_ascii=False))
        return (
            str(prompt)
            + "\n<previous_runs>"
            + html.escape(serialized)
            + "</previous_runs>\n请对比前序巡检结论，聚焦新增变化，避免重复播报相同旧闻。"
        )

    def _runtime_chat_id(self, chat_id, *, allow_unresolved=True):
        token = str(chat_id or "").strip()
        if not token or token == "self":
            return token
        if callable(self.resolve_session):
            try:
                resolved = self.resolve_session(token, allow_unresolved=allow_unresolved)
            except TypeError:
                resolved = self.resolve_session(token)
            if resolved:
                return resolved
            if not allow_unresolved:
                return ""
        return token

    async def _ensure_runtime_chat_id(self, chat_id):
        resolved = await asyncio.to_thread(
            self._runtime_chat_id, chat_id, allow_unresolved=True
        )
        parsed = parse_stream_spec(resolved)
        if parsed is None:
            return resolved
        opened_sid = await self._reopen_session_id(resolved)
        if opened_sid:
            return opened_sid
        rechecked = await asyncio.to_thread(
            self._runtime_chat_id, chat_id, allow_unresolved=False
        )
        return rechecked or resolved

    def level_for(self, chat_id):
        if chat_id in self.chat_proactive_levels:
            return self.chat_proactive_levels[chat_id]
        if callable(self.chat_aliases):
            for alias_id in self.chat_aliases(chat_id):
                if alias_id in self.chat_proactive_levels:
                    return self.chat_proactive_levels[alias_id]
        return self.proactive_level

    def _admins(self):
        return self.admins() if callable(self.admins) else (self.admins or ())

    def _temperament_xml(self):
        if self.memory is None:
            return ""
        try:
            heartbeat = self.memory.read("heartbeat")
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            return ""
        temperament, _ = MemoryLayers.heartbeat_parts(heartbeat.get("content") or "")
        if not temperament:
            return ""
        return (
            "\n<maisya_temperament>"
            + html.escape(self.redact(temperament))
            + "</maisya_temperament>"
        )

    async def tick(self, *, background_tick: bool = False):
        if self.closing:
            return
        lock = self._get_tick_lock()
        if background_tick and lock.locked():
            return
        async with lock:
            if self.closing:
                return
            await self._returns()
            if self.intents is not None:
                await self._intents()
            await self._heartbeat()

    async def _returns(self):
        params = inspect.signature(self.bridge.claim_ready).parameters
        if "plugin_enabled" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            batches = await asyncio.to_thread(
                self.bridge.claim_ready,
                self.enabled,
                plugin_enabled=self.plugin_enabled,
            )
        else:
            batches = await asyncio.to_thread(self.bridge.claim_ready, self.enabled)
        for chat_id, rows in batches.items():
            rows = await asyncio.to_thread(self.bridge.current, rows, pending=True)
            if not rows:
                continue
            if (
                self.closing
                or not (
                    self.plugin_enabled() if callable(self.plugin_enabled) else True
                )
                or not all(
                    self.enabled(chat_id, row["platform"])
                    or _grant_admin_direct(row.get("raw_grant"))
                    for row in rows
                )
            ):
                continue
            material_rows = self.bridge.material(rows)
            facts = html.escape(
                json.dumps(self.bridge.redact(material_rows), ensure_ascii=False)
            )
            has_pending_apr = any(
                _pending_approval_of(row.get("result"))
                for row in rows
                if isinstance(row, dict)
            )
            admin_direct = any(_grant_admin_direct(row.get("raw_grant")) for row in rows)
            if has_pending_apr:
                intent = (
                    "之前发起的 Agent 线程因高风险操作暂停等待管理员审批（尚未完成产出）。"
                    "请立即调用 reply 向管理员说明触发审批的具体命令与风险详情，并请对方确认是否批准放行或改用安全方式继续。\n<maisya_return>"
                    + facts
                    + "</maisya_return>"
                )
            elif admin_direct:
                intent = (
                    "之前由本会话真实用户消息触发的 Agent 线程已有结果。"
                    "这是管理员最新发起的后台试跑预览（晚于此前历史闲聊或旧取消语，请勿因旧消息静默跳过）："
                    "含图片/文件附件或准确链接的结果请直接调用 maisya_agent(action='share', thread_id=...)（无需传 source_msg_id）发送后结束本轮；"
                    "纯文字结论可直接用 reply 回复。\n<maisya_return>"
                    + facts
                    + "</maisya_return>"
                )
            else:
                scroll_note = ""
                if any(
                    isinstance(item, dict)
                    and item.get("chat_type") == "group"
                    and int(item.get("messages_since_trigger") or 0) >= 5
                    and item.get("source_msg_id")
                    for item in (material_rows or [])
                ):
                    scroll_note = (
                        "当前群聊自原提问起已滚过多条新消息：若开口回应，请结合 source_msg_id "
                        "引用锚定原消息或点名提问人（share 可传 ref_msg_ids=[source_msg_id]）；"
                        "若群内话题已翻篇且无需打断，可简短收尾或静默 stop。"
                    )
                intent = (
                    "之前由本会话真实用户消息触发的 Agent 线程已有结果。结合当前对话判断是否开口；"
                    "含图片/文件附件或准确链接请调用 maisya_agent(action='share', thread_id=...) 交付，纯文字通过原生 reply 表达，事实将单次交给 Replyer，也可以静默 stop。"
                    + scroll_note
                    + "结果仍可通过 maisya_recall 查阅。\n<maisya_return>"
                    + facts
                    + "</maisya_return>"
                )
            self.bridge.new_input(chat_id)
            if not all(self.enabled(chat_id, row["platform"]) for row in rows):
                await self.bridge.offer(chat_id)
            status = await self._wake(
                chat_id,
                intent,
                reason="maisya:return",
                metadata={
                    "kind": "return",
                    "groups": sorted({row["group_id"] for row in rows}),
                },
            )
            await asyncio.to_thread(self.bridge.wake_status, rows, status)
            if status == "accepted":
                inline_fn = getattr(self.bridge, "inline", None)
                if callable(inline_fn):
                    seen_tids: set[str] = set()
                    for r in rows:
                        tid = str(r.get("thread_id") or "") if isinstance(r, dict) else ""
                        if tid and tid not in seen_tids:
                            seen_tids.add(tid)
                            res = inline_fn(tid, chat_id)
                            if inspect.isawaitable(res):
                                await res

    async def _intents(self):
        rows = await asyncio.to_thread(self.intents.due, self.enabled)
        for row in rows:
            if self.closing:
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred"
                )
                continue
            try:
                await self._dispatch(row)
            except (RuntimeError, OSError, ValueError, TypeError, sqlite3.Error):
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred"
                )

    async def _reschedule_recurring(
        self, row, *, thread=None, record_run: bool = True, error: str = ""
    ):
        payload = row.get("payload") or {}
        every = int(payload.get("every_seconds") or 0)
        daily_at = str(payload.get("daily_at") or "").strip()
        if (every < 300 and not daily_at) or self.intents is None:
            return
        next_payload = dict(payload)
        next_payload.pop("wait_thread", None)
        next_payload.pop("already_started", None)
        next_payload.pop("return_routed_queued", None)
        now = self.intents.clock()
        result_obj = thread.get("result") if isinstance(thread, dict) else None
        if (
            record_run
            and not str(payload.get("workspace") or "").strip()
            and isinstance(result_obj, dict)
            and not result_obj.get("error")
        ):
            result_text = str(result_obj.get("text") or "").strip()
            if result_text:
                prev_runs = [
                    dict(item)
                    for item in (payload.get("recent_runs") or [])
                    if isinstance(item, dict)
                ]
                prev_runs.append(
                    {
                        "at": int(now),
                        "summary": self.redact(result_text)[:360],
                    }
                )
                prev_runs = prev_runs[-3:]
                while len(prev_runs) > 1 and len(
                    json.dumps(prev_runs, ensure_ascii=False)
                ) > 1200:
                    prev_runs.pop(0)
                while prev_runs and len(
                    json.dumps(prev_runs, ensure_ascii=False)
                ) > 1200:
                    last = prev_runs[-1]
                    summary = str(last.get("summary") or "")
                    if len(summary) <= 20:
                        break
                    last["summary"] = summary[:-40]
                next_payload["recent_runs"] = prev_runs
        if daily_at:
            tz_offset = (
                int(payload["tz_offset_minutes"])
                if payload.get("tz_offset_minutes") is not None
                else self._effective_tz_offset_minutes()
            )
            due_at, norm = next_daily_due(now, daily_at, tz_offset)
            next_payload["daily_at"] = norm
            next_payload["tz_offset_minutes"] = tz_offset
            next_payload["every_seconds"] = 86400
        else:
            due_at = now + every
        due_at = max(now + 60, due_at)
        fails = int(row.get("failure_count") or 0) + (1 if error else 0)
        if error:
            backoff = min(3600.0, 60.0 * (2 ** max(0, fails - 1)))
            due_at = max(due_at, now + backoff)
        bypass = bool(
            row.get("source") == "workflow"
            or str(next_payload.get("profile") or "").strip()
            or next_payload.get("scheduled_admin")
        )
        try:
            created = await asyncio.to_thread(
                self.intents.schedule,
                kind=row["kind"],
                source=row["source"],
                target=row["target"],
                due_at=due_at,
                reason=row["reason"],
                thread_id="",
                payload=next_payload,
                bypass_limits=bypass,
                mode=row.get("mode", "schedule"),
                condition=row.get("condition", ""),
            )
            if error and created and created.get("intent_id"):
                new_id = created["intent_id"]
                err_txt = str(error)[:300]
                pause_err = fails >= max(1, int(getattr(self, "bg_max_consecutive_failures", 3)))
                def _mark_fail():
                    with self.intents.store.connect() as db, db:
                        if pause_err:
                            db.execute(
                                "UPDATE intents SET status='error_paused', wake_status='error_paused', failure_count=?, last_error=? WHERE intent_id=?",
                                (fails, err_txt, new_id),
                            )
                        else:
                            db.execute(
                                "UPDATE intents SET failure_count=?, last_error=? WHERE intent_id=?",
                                (fails, err_txt, new_id),
                            )
                await asyncio.to_thread(_mark_fail)
        except ValueError as exc:
            logger.warning(
                "循环意图续排跳过 intent=%s target=%s: %s",
                row.get("intent_id"),
                row.get("target"),
                exc,
            )

    def _next_watch_due(self, row, now: float) -> float | None:
        payload = row.get("payload") or {}
        every = int(payload.get("every_seconds") or 0)
        daily_at = str(payload.get("daily_at") or "").strip()
        if every < 300 and not daily_at:
            return None
        if daily_at:
            tz_offset = (
                int(payload["tz_offset_minutes"])
                if payload.get("tz_offset_minutes") is not None
                else self._effective_tz_offset_minutes()
            )
            due_at, _ = next_daily_due(now, daily_at, tz_offset)
        else:
            due_at = now + every
        return max(now + 60, float(due_at))

    def _watch_prompt(self, row: dict, condition: str) -> str:
        payload = row.get("payload") or {}
        base = str(payload.get("prompt") or row.get("reason") or condition).strip()
        recent_runs = payload.get("recent_runs")
        history_block = ""
        if recent_runs:
            history_block = (
                "\n<previous_runs>"
                + html.escape(self.redact(json.dumps(recent_runs, ensure_ascii=False)))
                + "</previous_runs>"
            )
        return (
            base
            + "\n<watch_contract>"
            + f"\n条件：{html.escape(condition or row.get('reason') or '')}"
            + "\n仅当满足触发条件或发现需要汇报的新变化时输出结论；"
            + "若条件未满足或无变化，请仅输出 [WATCH:NO_CHANGE]，不要输出多余文字或声明附件。"
            + "\n</watch_contract>"
            + history_block
        )

    def _append_watch_summary(self, payload: dict, summary: str, now: float) -> dict:
        next_payload = dict(payload or {})
        next_payload.pop("wait_thread", None)
        next_payload.pop("already_started", None)
        next_payload.pop("return_routed_queued", None)
        clean = self.redact(str(summary or "").strip())
        if clean:
            next_payload["last_watch_summary"] = clean[:360]
            runs = [
                dict(item)
                for item in (next_payload.get("recent_runs") or [])
                if isinstance(item, dict)
            ]
            runs.append({"at": int(now), "summary": clean[:360]})
            runs = runs[-3:]
            while len(runs) > 1 and len(json.dumps(runs, ensure_ascii=False)) > 1200:
                runs.pop(0)
            next_payload["recent_runs"] = runs
        return next_payload

    @staticmethod
    def _delegated_grant(payload: dict | None, *, base: dict | None = None) -> dict:
        src = payload if isinstance(payload, dict) else {}
        grant = dict(base or {})
        if src.get("scheduled_admin"):
            grant["scheduled_admin"] = True
        if src.get("admin_direct"):
            grant["admin_direct"] = True
        if src.get("issuer_admin"):
            grant["issuer_admin"] = str(src["issuer_admin"])
        if isinstance(src.get("envelope"), dict):
            grant["envelope"] = dict(src["envelope"])
        return grant

    async def _check_intent_authority(self, row: dict) -> bool:
        if not callable(self.authority_check):
            return True
        ok, auth_err = await asyncio.to_thread(self.authority_check, row)
        if not ok:
            await asyncio.to_thread(
                self.intents.pause_for_authority, row["intent_id"], auth_err
            )
            return False
        return True

    def _bg_budget_exhausted(self, now=None) -> tuple[bool, str]:
        fn = getattr(self.intents, "bg_budget_state", None)
        if not callable(fn):
            return False, ""
        state = fn(
            runs_limit=getattr(self, "daily_bg_runs_limit", 48),
            tokens_limit=getattr(self, "daily_bg_tokens_limit", 200000),
            now=now,
        )
        return bool(state.get("exhausted")), str(state.get("exhausted_reason") or "")

    def _record_bg_usage_sync(
        self, *, source: str, target: str = "", runs: int = 1, tokens: int = 0, duration_ms: int = 0, now=None
    ) -> None:
        fn = getattr(self.intents, "record_bg_usage", None)
        if callable(fn):
            fn(
                source=source,
                target=target,
                runs=runs,
                tokens=tokens,
                duration_ms=duration_ms,
                now=now,
            )

    async def _handle_watch(self, row):
        if not await self._check_intent_authority(row):
            return
        target = row["target"]
        raw_chat_id = target.split(":", 1)[1]
        payload = dict(row.get("payload") or {})
        platform = str(payload.get("platform") or "")
        if not platform and parse_stream_spec(raw_chat_id):
            platform = parse_stream_spec(raw_chat_id)[0]
        chat_id = await self._ensure_runtime_chat_id(raw_chat_id)
        plugin_ok = self.plugin_enabled() if callable(self.plugin_enabled) else True
        if not (
            self.enabled(chat_id, platform)
            or (plugin_ok and bool(payload.get("admin_direct")))
        ):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        if row.get("kind") != "return" and not (
            row.get("thread_id") and payload.get("wait_thread")
        ):
            exhausted, b_reason = await asyncio.to_thread(self._bg_budget_exhausted)
            if exhausted:
                pause_fn = getattr(self.intents, "pause_for_budget", None)
                if callable(pause_fn):
                    await asyncio.to_thread(pause_fn, row["intent_id"], b_reason)
                else:
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], "deferred"
                    )
                return

        condition = str(
            row.get("condition") or payload.get("condition") or row.get("reason") or ""
        ).strip()
        thread = None
        triggered = False
        summary = ""
        attachments: list = []
        links: list = []

        if callable(self.watch_evaluator):
            ctx = {
                "chat_id": chat_id,
                "platform": platform,
                "condition": condition,
                "now": self.intents.clock(),
            }
            t0 = time.monotonic()
            params = inspect.signature(self.watch_evaluator).parameters
            try:
                ev_res = (
                    self.watch_evaluator(row)
                    if len(params) == 1
                    else self.watch_evaluator(row, ctx)
                )
                if inspect.isawaitable(ev_res):
                    ev_res = await ev_res
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                dur_ms = int((time.monotonic() - t0) * 1000)
                await asyncio.to_thread(
                    self._record_bg_usage_sync,
                    source="watch",
                    target=target,
                    runs=1,
                    duration_ms=dur_ms,
                )
                fail_fn = getattr(self.intents, "record_bg_failure", None)
                if callable(fail_fn):
                    await asyncio.to_thread(
                        fail_fn,
                        row["intent_id"],
                        str(exc),
                        max_failures=getattr(self, "bg_max_consecutive_failures", 3),
                    )
                else:
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], "deferred"
                    )
                return
            dur_ms = int((time.monotonic() - t0) * 1000)
            toks = int(ev_res.get("tokens_used") or 0) if isinstance(ev_res, dict) else 0
            await asyncio.to_thread(
                self._record_bg_usage_sync,
                source="watch",
                target=target,
                runs=1,
                tokens=toks,
                duration_ms=dur_ms,
            )
            if isinstance(ev_res, bool):
                triggered = ev_res
            elif isinstance(ev_res, dict):
                triggered = bool(ev_res.get("triggered"))
                summary = str(
                    ev_res.get("summary") or ev_res.get("message") or ev_res.get("text") or ""
                ).strip()
                attachments = list(ev_res.get("attachments") or [])
                links = list(ev_res.get("links") or [])
        elif self.engine is not None and (
            payload.get("profile") or self.profiles is not None
        ):
            if row.get("thread_id"):
                try:
                    thread = await asyncio.to_thread(
                        self.bridge.store.get, row["thread_id"], chat_id, allow_self=True
                    )
                except (ValueError, PermissionError):
                    thread = None
                if thread is not None and (
                    (self.engine.tasks.get(row["thread_id"]))
                    or thread.get("state") == "active"
                ):
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], "deferred", delay=2
                    )
                    return
            else:
                prof_id = str(payload.get("profile") or "").strip()
                ability = None
                if prof_id and self.profiles is not None:
                    try:
                        ability = await asyncio.to_thread(self.profiles.get, prof_id)
                    except FileNotFoundError:
                        await asyncio.to_thread(
                            self.intents.finish,
                            row["intent_id"],
                            "failed",
                            "profile_missing",
                        )
                        return
                elif self.profiles is not None:
                    for fallback_id in ("default", "scout"):
                        try:
                            ability = await asyncio.to_thread(
                                self.profiles.get, fallback_id
                            )
                            break
                        except FileNotFoundError:
                            continue
                if ability is None:
                    await asyncio.to_thread(
                        self.intents.finish,
                        row["intent_id"],
                        "failed",
                        "profile_missing",
                    )
                    return
                if not ability.enabled:
                    now_ts = self.intents.clock()
                    await asyncio.to_thread(
                        self.intents.record_watch_check,
                        row["intent_id"],
                        checked_at=now_ts,
                        triggered=False,
                        next_due_at=self._next_watch_due(row, now_ts),
                        wake_status="profile_disabled",
                    )
                    return
                if not ability.autonomous and not payload.get("scheduled_admin"):
                    now_ts = self.intents.clock()
                    await asyncio.to_thread(
                        self.intents.record_watch_check,
                        row["intent_id"],
                        checked_at=now_ts,
                        triggered=False,
                        next_due_at=self._next_watch_due(row, now_ts),
                        wake_status="fresh_authorization_required",
                    )
                    return
                workspace = str(
                    payload.get("workspace") or f"watch_{row['intent_id']}"
                ).strip()
                grant = self._delegated_grant(payload)
                started = await self._engine_call(
                    "start",
                    ability,
                    self._watch_prompt(row, condition),
                    chat_id=chat_id,
                    platform=platform or "qq",
                    user_id="",
                    owner="chat",
                    wait_seconds=0,
                    workspace=workspace,
                    grant=grant,
                    background=True,
                    defer_if_full=True,
                )
                started_tid = str((started or {}).get("thread_id") or "")
                _, is_queued, thread = await self._thread_state_after_dispatch(
                    started_tid, chat_id, started
                )
                if started_tid and (
                    is_queued
                    or (self.engine.tasks.get(started_tid))
                    or (thread and thread.get("state") == "active")
                ):
                    updated_payload = {**payload, "wait_thread": True}
                    await asyncio.to_thread(
                        self.intents.update_thread,
                        row["intent_id"],
                        started_tid,
                        updated_payload,
                    )
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], "deferred", delay=2
                    )
                    return
            result_obj = (
                thread.get("result")
                if isinstance(thread, dict) and isinstance(thread.get("result"), dict)
                else {}
            )
            await asyncio.to_thread(
                self._record_bg_usage_sync, source="watch", target=target, runs=1
            )
            if result_obj.get("error"):
                now_ts = self.intents.clock()
                logger.warning(
                    "盯梢线程执行失败 intent=%s thread=%s error=%s",
                    row["intent_id"],
                    thread.get("thread_id") if isinstance(thread, dict) else "",
                    result_obj.get("error"),
                )
                fail_fn = getattr(self.intents, "record_bg_failure", None)
                if callable(fail_fn):
                    await asyncio.to_thread(
                        fail_fn,
                        row["intent_id"],
                        str(result_obj.get("error")),
                        max_failures=getattr(self, "bg_max_consecutive_failures", 3),
                    )
                else:
                    await asyncio.to_thread(
                        self.intents.record_watch_check,
                        row["intent_id"],
                        checked_at=now_ts,
                        triggered=False,
                        next_due_at=self._next_watch_due(row, now_ts),
                        wake_status="thread_error",
                        thread_id="",
                    )
                return
            summary = str(result_obj.get("text") or "").strip()
            attachments = list(result_obj.get("attachments") or [])
            from .share import links_in

            try:
                links = links_in(result_obj)
            except ValueError:
                links = []
            explicit = result_obj.get("triggered")
            if isinstance(explicit, bool):
                triggered = explicit
            elif (
                not attachments
                and not links
                and (
                    "[WATCH:NO_CHANGE]" in summary.upper()
                    or summary.strip().upper() == "[NO_CHANGE]"
                )
            ):
                triggered = False
            else:
                triggered = bool(summary or attachments or links)
        else:
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return

        now_ts = self.intents.clock()
        next_due = self._next_watch_due(row, now_ts)
        next_payload = self._append_watch_summary(
            payload, "" if not triggered and "[WATCH:NO_CHANGE]" in summary.upper() else summary, now_ts
        )
        if not triggered:
            await asyncio.to_thread(
                self.intents.record_watch_check,
                row["intent_id"],
                checked_at=now_ts,
                triggered=False,
                next_due_at=next_due,
                wake_status="watch_unmet",
                payload=next_payload,
                thread_id="",
            )
            return

        if thread is not None and (summary or attachments or links):
            await asyncio.to_thread(
                self.bridge.stage,
                thread,
                chat_id,
                archived=thread.get("owner") == "self",
            )
        wake_intent = (
            "你安排的条件盯梢任务已命中触发条件，请结合当前对话自然汇报结论："
            "纯文字结论可直接用 reply 提起；含图片、文件附件或准确链接请调用 maisya_agent(action=\"share\") 交付。\n"
            "<maisya_watch>"
            + html.escape(self.redact(condition or row["reason"]))
            + "</maisya_watch>"
        )
        if summary:
            wake_intent += (
                "\n<watch_finding>"
                + html.escape(self.redact(summary))
                + "</watch_finding>"
            )
        if thread and (attachments or links):
            hint_info: dict = {"thread_id": str(thread["thread_id"])}
            handles = [
                str(item.get("handle"))
                for item in attachments
                if isinstance(item, dict) and item.get("handle")
            ]
            if handles:
                hint_info["attachment_handles"] = handles
            if links:
                hint_info["links"] = links[:12]
            wake_intent += (
                "\n<maisya_delivery_hint>"
                + html.escape(json.dumps(hint_info, ensure_ascii=False))
                + "</maisya_delivery_hint>"
            )
        status = await self._wake(
            chat_id,
            wake_intent,
            reason="maisya:watch",
            metadata={
                "kind": row["kind"],
                "mode": "watch",
                "source": row["source"],
                "intent_id": row["intent_id"],
                "condition": condition,
            },
        )
        if status in {"session_missing", "uncertain"}:
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "deferred"
            )
            return
        await asyncio.to_thread(
            self.intents.record_watch_check,
            row["intent_id"],
            checked_at=now_ts,
            triggered=True,
            triggered_at=now_ts,
            next_due_at=next_due,
            wake_status=status,
            payload=next_payload,
            thread_id="",
        )

    async def _resolve_approval_notify_chat(
        self,
        *,
        source_chat_id: str,
        issuer_admin: str,
        fallback_chat_id: str,
    ) -> str:
        clean_src = str(source_chat_id or "").strip()
        if clean_src:
            resolved = await self._ensure_runtime_chat_id(clean_src)
            if resolved:
                return resolved
        clean_issuer = str(issuer_admin or "").strip()
        if (
            clean_issuer
            and clean_issuer != "console"
            and not clean_issuer.startswith("admin:")
            and ":" in clean_issuer
        ):
            plat, uid = clean_issuer.split(":", 1)
            if plat.strip() and uid.strip():
                spec = f"{plat.strip().lower()}:private:{uid.strip()}"
                resolved = await self._ensure_runtime_chat_id(spec)
                if resolved:
                    return resolved
        if fallback_chat_id and self._chat_arena(fallback_chat_id) == "private":
            return fallback_chat_id
        return ""

    async def _dispatch(self, row):
        if not await self._check_intent_authority(row):
            return
        if str(row.get("mode") or "") == "watch" or row.get("source") == "watch":
            await self._handle_watch(row)
            return
        target = row["target"]
        raw_chat_id = target.split(":", 1)[1]
        platform = row["payload"].get("platform", "")
        if not platform and parse_stream_spec(raw_chat_id):
            platform = parse_stream_spec(raw_chat_id)[0]
        if row["kind"] == "silent":
            await self._silent(row)
            return
        chat_id = await self._ensure_runtime_chat_id(raw_chat_id)
        plugin_ok = (
            self.plugin_enabled() if callable(self.plugin_enabled) else True
        )
        if not (
            self.enabled(chat_id, platform)
            or (plugin_ok and bool((row.get("payload") or {}).get("admin_direct")))
        ):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        if row["kind"] == "proactive":
            allowed, reason = self._proactive_gate(row)
            if not allowed:
                status = "deferred" if reason == "sleep_window" else "failed"
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], status, reason
                )
                if status == "failed":
                    await self._reschedule_recurring(row, record_run=False)
                return
        elif row["payload"].get("profile") and self._in_sleep_window():
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "deferred", "sleep_window"
            )
            return
        if bool(row["payload"].get("return_routed_queued")) and row.get("thread_id"):
            tid = str(row["thread_id"])
            raw_thread_chat = str(row["payload"].get("thread_chat_id") or "").strip()
            thread_chat_id = (
                (await self._ensure_runtime_chat_id(raw_thread_chat))
                if raw_thread_chat
                else chat_id
            ) or chat_id
            in_tasks, is_queued, thread = await self._thread_state_after_dispatch(
                tid, thread_chat_id
            )
            if is_queued:
                kicked = await self._engine_call(
                    "kick_queued", tid, thread_chat_id, background=True
                )
                if kicked is False:
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], "deferred", delay=2
                    )
                    return
                if getattr(self.engine, "kick_queued", None) is not None:
                    in_tasks, is_queued, thread = (
                        await self._thread_state_after_dispatch(tid, thread_chat_id)
                    )
                    if not in_tasks and is_queued:
                        await asyncio.to_thread(
                            self.intents.finish, row["intent_id"], "deferred", delay=2
                        )
                        return
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "done", "return_routed"
            )
            await self._reschedule_recurring(row, thread=thread)
            return
        if (
            row["payload"].get("profile")
            and not row.get("thread_id")
            and self.engine is not None
            and self.profiles is not None
        ):
            if row.get("kind") != "return":
                exhausted, b_reason = await asyncio.to_thread(self._bg_budget_exhausted)
                if exhausted:
                    pause_fn = getattr(self.intents, "pause_for_budget", None)
                    if callable(pause_fn):
                        await asyncio.to_thread(pause_fn, row["intent_id"], b_reason)
                    else:
                        await asyncio.to_thread(
                            self.intents.finish, row["intent_id"], "deferred"
                        )
                    return
            try:
                ability = await asyncio.to_thread(
                    self.profiles.get, row["payload"]["profile"]
                )
            except FileNotFoundError:
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "profile_missing",
                )
                return
            if not ability.enabled:
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "profile_disabled",
                )
                await self._reschedule_recurring(row, record_run=False)
                return
            if not ability.autonomous and not row["payload"].get("scheduled_admin"):
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "fresh_authorization_required",
                )
                await self._reschedule_recurring(row, record_run=False)
                return
            prompt = self._with_previous_runs_prompt(
                row["payload"].get("prompt") or row["reason"],
                row["payload"],
            )
            raw_thread_chat = str(row["payload"].get("thread_chat_id") or "").strip()
            thread_chat_id = (
                (await self._ensure_runtime_chat_id(raw_thread_chat))
                if raw_thread_chat
                else chat_id
            ) or chat_id
            if bool(row["payload"].get("admin_direct")) and thread_chat_id != chat_id:
                source_msg_id = str(row["payload"].get("source_msg_id") or "admin-preview")
                route = await self.bridge.route(chat_id, source_msg_id, 0)
                try:
                    started = await self._engine_call(
                        "start",
                        ability,
                        prompt,
                        chat_id=thread_chat_id,
                        platform=platform or "qq",
                        user_id="",
                        owner="chat",
                        wait_seconds=0,
                        return_route=route,
                        workspace=row["payload"].get("workspace", ""),
                        grant=self._delegated_grant(
                            row["payload"],
                            base={
                                "scheduled_admin": True,
                                "admin_direct": True,
                                "source_chat_id": chat_id,
                                "source_platform": platform or "qq",
                                "source_msg_id": source_msg_id,
                            },
                        ),
                        background=True,
                        defer_if_full=True,
                    )
                finally:
                    await self.bridge.submitted(route)
                started_tid = str((started or {}).get("thread_id") or "")
                _, is_queued, thread = await self._thread_state_after_dispatch(
                    started_tid, thread_chat_id, started
                )
                if started_tid and is_queued:
                    updated_payload = {
                        **row["payload"],
                        "return_routed_queued": True,
                    }
                    await asyncio.to_thread(
                        self.intents.update_thread,
                        row["intent_id"],
                        started_tid,
                        updated_payload,
                    )
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], "deferred", delay=2
                    )
                    return
                await asyncio.to_thread(
                    self.intents.update_thread,
                    row["intent_id"],
                    started_tid,
                    row["payload"],
                )
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "done", "return_routed"
                )
                await self._reschedule_recurring(row, thread=thread)
                return
            started = await self._engine_call(
                "start",
                ability,
                prompt,
                chat_id="self" if chat_id == "self" else chat_id,
                platform="self" if chat_id == "self" else (platform or "qq"),
                user_id="",
                owner="self" if chat_id == "self" else "chat",
                wait_seconds=0,
                workspace=row["payload"].get("workspace", ""),
                grant=self._delegated_grant(row["payload"]),
                background=True,
                defer_if_full=True,
            )
            updated_payload = {**row["payload"], "wait_thread": True}
            row = {**row, "thread_id": started["thread_id"], "payload": updated_payload}
            await asyncio.to_thread(
                self.intents.update_thread,
                row["intent_id"],
                started["thread_id"],
                updated_payload,
            )
        thread = None
        if row.get("thread_id"):
            try:
                thread = await asyncio.to_thread(
                    self.bridge.store.get, row["thread_id"], chat_id, allow_self=True
                )
            except (ValueError, PermissionError):
                thread = None
            if row["payload"].get("wait_thread"):
                tid = row["thread_id"]
                if (
                    self.engine is not None
                    and tid not in self.engine.tasks
                    and thread is not None
                    and int(thread.get("processed_revision") or 0)
                    < int(thread.get("revision") or 0)
                ):
                    await self._engine_call("kick_queued", tid, chat_id, background=True)
                if (self.engine and self.engine.tasks.get(tid)) or (
                    thread and thread["state"] == "active"
                ):
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], "deferred", delay=2
                    )
                    return
        is_workflow = bool(
            row["source"] == "workflow"
            or row["payload"].get("profile")
            or thread is not None
        )
        text = html.escape(self.redact(row["reason"]))
        if is_workflow:
            intent = (
                "预设或定时安排的角色工作流已完成本轮运行。结合当前对话与产出内容判断是否交付："
                "纯文字结论可直接用 reply 自然提起；若含准确链接、图片或文件附件，请调用 maisya_agent(action=\"share\") 交付后结束本轮；"
                "若 <maisya_thread_result> 与前序运行相比无新增变化或无值得打扰的新内容，请直接静默 stop。"
                "\n<maisya_workflow>" + text + "</maisya_workflow>"
            )
            if row["kind"] == "proactive":
                intent += await asyncio.to_thread(self._temperament_xml)
        elif row["kind"] == "proactive":
            intent = (
                "这是你自己之前想对会话提起的惦记，或计划中的一次自然冒泡。"
                "结合当前对话判断是否值得开口；可以自然提起，也可以静默 stop。"
                "\n<maisya_proactive>" + text + "</maisya_proactive>"
            )
            intent += await asyncio.to_thread(self._temperament_xml)
        else:
            intent = (
                "这是你之前通过 maisya_later 安排的提醒，现在到期。"
                "结合当前对话用一两句自然提起；如果已无意义可静默 stop。"
                "\n<maisya_reminder>" + text + "</maisya_reminder>"
            )
        if thread and isinstance(thread.get("result"), dict):
            result_obj = thread["result"]
            payload_obj = row.get("payload") or {}
            t_grant = (thread.get("grant") or {}) if isinstance(thread, dict) else {}
            src_chat = str(
                payload_obj.get("source_chat_id")
                or t_grant.get("source_chat_id")
                or ""
            ).strip()
            issuer = str(
                payload_obj.get("issuer_admin")
                or t_grant.get("issuer_admin")
                or ""
            ).strip()
            if row.get("kind") != "return" and (
                row.get("source") == "workflow"
                or row["payload"].get("profile")
                or row["payload"].get("every_seconds")
                or row["payload"].get("daily_at")
            ):
                await asyncio.to_thread(
                    self._record_bg_usage_sync,
                    source=row["source"],
                    target=target,
                    runs=1,
                )
            if result_obj.get("error"):
                err_text = str(result_obj.get("error") or "thread_error")
                logger.warning(
                    "线程执行失败，终止本次唤醒 intent=%s thread=%s error=%s",
                    row["intent_id"],
                    thread.get("thread_id"),
                    result_obj.get("error"),
                )
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "failed", "thread_error"
                )
                await self._reschedule_recurring(
                    row,
                    thread=thread,
                    record_run=False,
                    error=err_text,
                )
                if (
                    row.get("kind") == "return"
                    and payload_obj.get("already_started")
                    and (src_chat or issuer)
                ):
                    err_notify = await self._resolve_approval_notify_chat(
                        source_chat_id=src_chat,
                        issuer_admin=issuer,
                        fallback_chat_id="",
                    )
                    if (
                        err_notify
                        and err_notify != chat_id
                        and self._chat_arena(err_notify) == "private"
                    ):
                        await asyncio.to_thread(
                            self.bridge.stage,
                            thread,
                            err_notify,
                            archived=False,
                            admin_direct=True,
                        )
                        err_wake = (
                            "之前发起的跨会话后台 Agent 线程执行失败（未向目标会话发送任何消息）。"
                            "请立即调用 reply 向管理员如实说明失败原因，再决定是否重派或改向。\n"
                            "<maisya_workflow>"
                            + text
                            + "</maisya_workflow>\n<maisya_thread_error>"
                            + html.escape(self.redact(err_text))
                            + "</maisya_thread_error>"
                        )
                        await self._wake(
                            err_notify,
                            err_wake,
                            reason="maisya:workflow_error",
                            metadata={
                                "kind": row["kind"],
                                "source": "workflow",
                                "intent_id": row["intent_id"],
                                "thread_id": str(thread.get("thread_id") or ""),
                            },
                        )
                return
            pending_apr = (
                _pending_approval_of(result_obj)
                if thread.get("state") == "waiting"
                else None
            )
            if pending_apr is not None:
                notify_chat = await self._resolve_approval_notify_chat(
                    source_chat_id=src_chat,
                    issuer_admin=issuer,
                    fallback_chat_id=chat_id,
                )
                if notify_chat and (
                    notify_chat != chat_id or self._chat_arena(chat_id) == "private"
                ):
                    await asyncio.to_thread(
                        self.bridge.stage,
                        thread,
                        notify_chat,
                        archived=False,
                        admin_direct=True,
                    )
                    apr_body = json.dumps(
                        self.redact(
                            {
                                "thread_id": str(thread["thread_id"]),
                                "target_chat": chat_id,
                                "pending_approval": pending_apr,
                            }
                        ),
                        ensure_ascii=False,
                    )
                    wake_intent = (
                        "之前发起的后台 Agent 线程因高风险操作暂停等待管理员审批（尚未完成交付或发群）。"
                        "请立即调用 reply 向管理员说明触发审批的具体命令与风险详情，询问是批准放行还是改用工作区文件安全重做。\n"
                        "<maisya_approval_required>"
                        + html.escape(apr_body)
                        + "</maisya_approval_required>"
                    )
                    status = await self._wake(
                        notify_chat,
                        wake_intent,
                        reason="maisya:approval",
                        metadata={
                            "kind": row["kind"],
                            "source": "workflow",
                            "intent_id": row["intent_id"],
                            "thread_id": str(thread["thread_id"]),
                        },
                    )
                    done_st = (
                        "done"
                        if status == "accepted"
                        else (
                            "deferred"
                            if status in {"session_missing", "uncertain"}
                            else "failed"
                        )
                    )
                    wake_st = (
                        "approval_notified" if status == "accepted" else status
                    )
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], done_st, wake_st
                    )
                else:
                    await asyncio.to_thread(
                        self.intents.finish,
                        row["intent_id"],
                        "done",
                        "approval_waiting",
                    )
                return
            result_text = str(result_obj.get("text") or "")
            attachments = result_obj.get("attachments") or []
            from .share import links_in

            try:
                result_links = links_in(result_obj)
            except ValueError:
                result_links = []
            if result_text or attachments or result_links:
                has_att = any(
                    isinstance(item, dict)
                    and str(item.get("handle") or "").startswith("att_")
                    for item in attachments
                )
                if (
                    row.get("kind") == "return"
                    and payload_obj.get("already_started")
                    and not payload_obj.get("explicit_deliver_target")
                    and not has_att
                    and (src_chat or issuer)
                ):
                    src_notify = await self._resolve_approval_notify_chat(
                        source_chat_id=src_chat,
                        issuer_admin=issuer,
                        fallback_chat_id="",
                    )
                    if (
                        src_notify
                        and src_notify != chat_id
                        and self._chat_arena(src_notify) == "private"
                    ):
                        await asyncio.to_thread(
                            self.bridge.stage,
                            thread,
                            src_notify,
                            archived=False,
                            admin_direct=True,
                        )
                        src_intent = (
                            "你在管理员私聊发起的跨会话后台 Agent 线程已完成本轮运行（不含需要直接发往目标群的媒体/文件附件，未向目标群发送消息）。"
                            "请立即调用 reply（reply_style=\"长回复\"）向管理员如实汇报查证结论、进展或卡点；如需补发群消息或附件，再显式确认。\n"
                            "<maisya_workflow>"
                            + text
                            + "</maisya_workflow>\n<maisya_thread_result>"
                            + html.escape(self.redact(result_text))
                            + "</maisya_thread_result>"
                        )
                        if result_links:
                            src_intent += (
                                "\n<maisya_delivery_hint>"
                                + html.escape(
                                    json.dumps(
                                        {
                                            "thread_id": str(thread["thread_id"]),
                                            "links": result_links[:12],
                                        },
                                        ensure_ascii=False,
                                    )
                                )
                                + "</maisya_delivery_hint>"
                            )
                        src_status = await self._wake(
                            src_notify,
                            src_intent,
                            reason="maisya:workflow",
                            metadata={
                                "kind": row["kind"],
                                "source": "workflow",
                                "intent_id": row["intent_id"],
                                "thread_id": str(thread["thread_id"]),
                            },
                        )
                        done_src = (
                            "done"
                            if src_status == "accepted"
                            else (
                                "deferred"
                                if src_status in {"session_missing", "uncertain"}
                                else "failed"
                            )
                        )
                        wake_src = (
                            "return_routed_source"
                            if src_status == "accepted"
                            else src_status
                        )
                        await asyncio.to_thread(
                            self.intents.finish, row["intent_id"], done_src, wake_src
                        )
                        return
                await asyncio.to_thread(
                    self.bridge.stage,
                    thread,
                    chat_id,
                    archived=thread.get("owner") == "self",
                )
                intent += (
                    "\n<maisya_thread_result>"
                    + html.escape(self.redact(result_text))
                    + "</maisya_thread_result>"
                )
                hint_info = {"thread_id": str(thread["thread_id"])}
                if attachments:
                    handles = [
                        str(item.get("handle"))
                        for item in attachments
                        if isinstance(item, dict) and item.get("handle")
                    ]
                    if handles:
                        hint_info["attachment_handles"] = handles
                if result_links:
                    hint_info["links"] = result_links[:12]
                if attachments or result_links:
                    intent += (
                        "\n<maisya_delivery_hint>"
                        + html.escape(json.dumps(hint_info, ensure_ascii=False))
                        + "</maisya_delivery_hint>"
                    )
        status = await self._wake(
            chat_id,
            intent
            + (
                "\n<external_event>"
                + html.escape(
                    json.dumps(self.redact(row["payload"]["event"]), ensure_ascii=False)
                )
                + "</external_event>"
                if row["source"] == "webhook" and "event" in row["payload"]
                else ""
            ),
            reason="maisya:" + row["source"],
            metadata={
                "kind": row["kind"],
                "source": row["source"],
                "intent_id": row["intent_id"],
            },
        )
        if status == "accepted" and row["kind"] == "proactive":
            mp_fn = self.intents.mark_proactive
            mp_aliases = tuple(
                x
                for x in (chat_id, (row.get("payload") or {}).get("stream_spec") or "")
                if x
            )
            if "aliases" in inspect.signature(mp_fn).parameters:
                await asyncio.to_thread(mp_fn, target, aliases=mp_aliases)
            else:
                await asyncio.to_thread(mp_fn, target)
        done = (
            "done"
            if status == "accepted"
            else ("deferred" if status in {"session_missing", "uncertain"} else "failed")
        )
        await asyncio.to_thread(self.intents.finish, row["intent_id"], done, status)
        if done in {"done", "failed"}:
            await self._reschedule_recurring(
                row, thread=thread, record_run=(done == "done")
            )

    async def _silent(self, row):
        raw_target_chat = row["target"].split(":", 1)[1]
        target_chat = (
            "self"
            if raw_target_chat == "self"
            else await self._ensure_runtime_chat_id(raw_target_chat)
        )
        if row["payload"].get("already_started"):
            tid = row.get("thread_id") or ""
            thread = None
            if tid and self.engine is not None and tid not in self.engine.tasks:
                try:
                    thread = await asyncio.to_thread(
                        self.bridge.store.get,
                        tid,
                        target_chat,
                        allow_self=target_chat == "self",
                    )
                except (ValueError, PermissionError):
                    thread = None
                if (
                    thread is not None
                    and int(thread.get("processed_revision") or 0)
                    < int(thread.get("revision") or 0)
                ):
                    kicked = await self._engine_call(
                        "kick_queued", tid, target_chat, background=True
                    )
                    if kicked is False:
                        await asyncio.to_thread(
                            self.intents.finish,
                            row["intent_id"],
                            "deferred",
                            delay=15,
                        )
                        return
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")
            await self._reschedule_recurring(row, thread=thread)
            return
        if (
            row["payload"].get("profile")
            and not row.get("thread_id")
            and self.engine is not None
            and self.profiles is not None
        ):
            exhausted, b_reason = await asyncio.to_thread(self._bg_budget_exhausted)
            if exhausted:
                pause_fn = getattr(self.intents, "pause_for_budget", None)
                if callable(pause_fn):
                    await asyncio.to_thread(pause_fn, row["intent_id"], b_reason)
                else:
                    await asyncio.to_thread(
                        self.intents.finish, row["intent_id"], "deferred"
                    )
                return
            platform = row["payload"].get("platform", "")
            if not platform and parse_stream_spec(raw_target_chat):
                platform = parse_stream_spec(raw_target_chat)[0]
            if target_chat != "self" and not self.enabled(target_chat, platform):
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred"
                )
                return
            try:
                ability = await asyncio.to_thread(
                    self.profiles.get, row["payload"]["profile"]
                )
            except FileNotFoundError:
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "profile_missing",
                )
                return
            if not ability.enabled:
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "profile_disabled",
                )
                await self._reschedule_recurring(row, record_run=False)
                return
            if not ability.autonomous and not row["payload"].get("scheduled_admin"):
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "fresh_authorization_required",
                )
                await self._reschedule_recurring(row, record_run=False)
                return
            prompt = self._with_previous_runs_prompt(
                row["payload"].get("prompt") or row["reason"],
                row["payload"],
            )
            thread_chat = "self" if target_chat == "self" else target_chat
            started = await self._engine_call(
                "start",
                ability,
                prompt,
                chat_id=thread_chat,
                platform="self" if target_chat == "self" else (platform or "qq"),
                user_id="",
                owner="self" if target_chat == "self" else "chat",
                wait_seconds=0,
                workspace=row["payload"].get("workspace", ""),
                grant=self._delegated_grant(row["payload"]),
                background=True,
                defer_if_full=True,
            )
            started_tid = str((started or {}).get("thread_id") or "")
            _, is_queued, started_thread = await self._thread_state_after_dispatch(
                started_tid, thread_chat, started
            )
            if started_tid and is_queued:
                updated_payload = {**row["payload"], "already_started": True}
                await asyncio.to_thread(
                    self.intents.update_thread,
                    row["intent_id"],
                    started_tid,
                    updated_payload,
                )
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred", delay=15
                )
                return
            await asyncio.to_thread(
                self._record_bg_usage_sync,
                source=row["source"],
                target=row["target"],
                runs=1,
            )
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")
            await self._reschedule_recurring(row, thread=started_thread)
            return
        thread_id = row["thread_id"]
        if not thread_id:
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "failed", "thread_missing"
            )
            return
        try:
            thread = await asyncio.to_thread(
                self.bridge.store.get,
                thread_id,
                target_chat,
                allow_self=target_chat == "self",
            )
        except (ValueError, PermissionError):
            thread = None
        if thread is None or thread["state"] == "closed":
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "done", "thread_closed"
            )
            if str((row.get("payload") or {}).get("profile") or "").strip():
                await self._reschedule_recurring(row, thread=thread)
            return
        scheduled_admin = bool((row.get("payload") or {}).get("scheduled_admin"))
        snapshot_ok = is_autonomous_snapshot(thread.get("profile_snapshot"))
        prof_id = str(
            (row.get("payload") or {}).get("profile")
            or thread.get("profile")
            or thread.get("profile_id")
            or ""
        ).strip()
        if prof_id and self.profiles is not None:
            try:
                live_ability = await asyncio.to_thread(self.profiles.get, prof_id)
            except FileNotFoundError:
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "profile_missing",
                )
                return
            if not live_ability.enabled:
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "profile_disabled",
                )
                await self._reschedule_recurring(row, thread=thread, record_run=False)
                return
            if not scheduled_admin and (
                not snapshot_ok or not live_ability.autonomous
            ):
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "fresh_authorization_required",
                )
                await self._reschedule_recurring(row, thread=thread, record_run=False)
                return
        elif not scheduled_admin and not snapshot_ok:
            await asyncio.to_thread(
                self.intents.finish,
                row["intent_id"],
                "failed",
                "fresh_authorization_required",
            )
            await self._reschedule_recurring(row, thread=thread, record_run=False)
            return
        if thread["owner"] != "self" and not self.enabled(
            thread["chat_id"], thread["platform"]
        ):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        if self.engine is None:
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        if (
            thread_id not in self.engine.tasks
            and int(thread.get("processed_revision") or 0)
            < int(thread.get("revision") or 0)
        ):
            kicked = await self._engine_call(
                "kick_queued", thread_id, target_chat, background=True
            )
            if kicked is False:
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred", delay=15
                )
                return
        if self.engine.tasks.get(thread_id) or thread["state"] == "active":
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "deferred", delay=15
            )
            return
        try:
            chat_id = thread["chat_id"] if thread["owner"] != "self" else "self"
            prompt = row["payload"].get("next_step") or row["reason"]
            if row["source"] == "webhook" and "event" in row["payload"]:
                prompt += (
                    "\n<external_event>"
                    + html.escape(
                        json.dumps(
                            self.redact(row["payload"]["event"]), ensure_ascii=False
                        )
                    )
                    + "</external_event>"
                )
            sent = await self._engine_call(
                "send",
                thread_id,
                prompt,
                chat_id=chat_id,
                wait_seconds=0,
                input_context={
                    "grant": self._delegated_grant(row.get("payload")),
                },
                background=True,
                defer_if_full=True,
            )
        except (ValueError, PermissionError, RuntimeError, sqlite3.Error):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        _, is_queued, updated_thread = await self._thread_state_after_dispatch(
            thread_id, chat_id, sent
        )
        if is_queued:
            updated_payload = {**row["payload"], "already_started": True}
            await asyncio.to_thread(
                self.intents.update_thread,
                row["intent_id"],
                thread_id,
                updated_payload,
            )
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "deferred", delay=15
            )
            return
        await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")
        await self._reschedule_recurring(row, thread=updated_thread or thread)

    def _proactive_gate(self, row):
        chat_id = row["target"].split(":", 1)[1]
        level = self.level_for(chat_id)
        if level == "quiet":
            return False, "quiet_level"
        source = row["source"]
        if level == "care" and source == "serendipity" and self._chat_arena(chat_id) != "private":
            return False, "care_blocks_serendipity"
        payload = row.get("payload") or {}
        aliases = tuple(
            x
            for x in (
                payload.get("stream_spec") if isinstance(payload, dict) else "",
                payload.get("chat_id") if isinstance(payload, dict) else "",
            )
            if x
        )
        allowed, reason = self._social_gate(
            row["target"], sleep_window=self.sleep_window, aliases=aliases
        )
        if not allowed:
            return allowed, reason
        if not payload.get("scheduled_admin") and not self.intents.acquaintance_allowed(
            chat_id,
            admins=self._admins(),
            acquaintances=self.acquaintance_users,
            payload=payload,
        ):
            return False, "unfamiliar_chat"
        row_bypass = bool(row.get("bypass_limits"))
        can_bypass = (
            source == "workflow"
            or bool(str(payload.get("profile") or "").strip())
            or bool(payload.get("scheduled_admin"))
            or (row_bypass and not is_profileless_daily(source, payload))
        )
        if not can_bypass and not self._proactive_quota_available(
            row["target"],
            now=self.intents.clock(),
            exclude_intent_id=row.get("intent_id", ""),
            payload=payload if isinstance(payload, dict) else None,
        ):
            return False, "daily_proactive_limit"
        return True, ""

    def _recent_thread_corpus(self, chat_id: str, raw_chat_id: str = "") -> str:
        parts = []
        recent_fn = getattr(self, "recent_topics", None)
        if callable(recent_fn):
            try:
                topics = recent_fn(chat_id)
            except (ValueError, OSError, RuntimeError, sqlite3.Error, TypeError):
                topics = None
            if isinstance(topics, str):
                parts.append(topics)
            elif isinstance(topics, (list, tuple, set)):
                parts.extend(str(item) for item in topics if item)
        store = getattr(self.bridge, "store", None) if self.bridge is not None else None
        if store is not None and callable(getattr(store, "connect", None)):
            lookup_ids = {str(chat_id)}
            if raw_chat_id:
                lookup_ids.add(str(raw_chat_id))
            if callable(self.chat_aliases):
                try:
                    lookup_ids.update(
                        str(alias) for alias in self.chat_aliases(chat_id) if alias
                    )
                except (ValueError, RuntimeError, sqlite3.Error, TypeError):
                    pass
            clean_ids = sorted(x for x in lookup_ids if x)
            if clean_ids:
                placeholders = ",".join("?" for _ in clean_ids)
                try:
                    with store.connect() as db:
                        rows = db.execute(
                            f"SELECT t.summary, t.history, ts.text "
                            f"FROM agent_threads t "
                            f"LEFT JOIN thread_search ts ON ts.thread_id = t.thread_id "
                            f"WHERE t.chat_id IN ({placeholders}) "
                            f"ORDER BY t.updated DESC LIMIT 12",
                            tuple(clean_ids),
                        ).fetchall()
                    for row in rows:
                        keys = set(row.keys()) if hasattr(row, "keys") else set()
                        for key in ("summary", "text"):
                            if key in keys and row[key]:
                                parts.append(str(row[key]))
                        if "history" in keys and row["history"]:
                            try:
                                hist = json.loads(row["history"])
                            except (ValueError, TypeError):
                                hist = None
                            if isinstance(hist, list):
                                for msg in hist[-6:]:
                                    if isinstance(msg, dict) and msg.get("content"):
                                        parts.append(str(msg["content"]))
                except (sqlite3.Error, OSError, ValueError):
                    pass
        return "\n".join(parts)

    def propose_serendipity(self, *, thread_id, due_at, reason):
        if self.intents is None or self.memory is None:
            return {"scheduled": False}
        tokens = _extract_tokens(reason)
        if not tokens:
            return {"scheduled": False}
        try:
            due_ts = float(due_at)
        except (TypeError, ValueError):
            return {"scheduled": False}
        if not self._proactive_quota_available("", now=due_ts):
            return {"scheduled": False}
        admins = self._admins()
        best = None
        seen_sids = set()
        for raw_chat_id in self.enabled_chats():
            chat_id = self._runtime_chat_id(raw_chat_id, allow_unresolved=False)
            if not chat_id or chat_id in seen_sids:
                continue
            seen_sids.add(chat_id)
            arena = self._chat_arena(chat_id) or self._chat_arena(str(raw_chat_id or ""))
            level = self.level_for(chat_id)
            if arena == "private":
                if level not in {"care", "lively"}:
                    continue
            elif level != "lively":
                continue
            platform = self.platform_for(chat_id) if callable(self.platform_for) else ""
            if not self.enabled(chat_id, platform):
                continue
            if not self.intents.acquaintance_allowed(
                chat_id,
                admins=admins,
                acquaintances=self.acquaintance_users,
                payload={"platform": platform},
            ):
                continue
            if not self._social_gate("chat:" + chat_id, sleep_window="")[0]:
                continue
            candidate_payload = {"platform": platform}
            if arena:
                candidate_payload["chat_type"] = arena
            if not self._proactive_quota_available(
                "chat:" + chat_id,
                now=due_ts,
                payload=candidate_payload,
            ):
                continue
            try:
                mem_content = self.memory.read("memory", chat_id=chat_id).get("content") or ""
            except (ValueError, OSError, RuntimeError, sqlite3.Error):
                mem_content = ""
            thread_content = self._recent_thread_corpus(chat_id, str(raw_chat_id or ""))
            corpus_tokens = _extract_tokens(mem_content + "\n" + thread_content)
            score = len(tokens & corpus_tokens)
            if score > 0 and (best is None or score > best[0]):
                best = (score, chat_id, platform)
        if best is None:
            return {"scheduled": False}
        _, best_chat, best_platform = best
        try:
            row = self.intents.schedule(
                kind="proactive",
                source="serendipity",
                target="chat:" + best_chat,
                due_at=due_at,
                reason=reason,
                thread_id=thread_id,
                payload={"platform": best_platform},
            )
        except ValueError:
            return {"scheduled": False}
        return {"scheduled": True, "intent_id": row["intent_id"], "due_at": row["due_at"]}

    def _longing_candidates(self, now) -> list[dict]:
        """心跳第 1 步的想念候选：只用代码判断，见 docs/08 第三节。"""
        if self.longing_quiet_hours <= 0 or not self._proactive_quota_available("", now=now):
            return []
        threshold = self.longing_quiet_hours * 3600
        admins = self._admins()
        seen_sids, found = set(), []
        for raw_chat_id in list(self.enabled_chats()):
            chat_id = self._runtime_chat_id(raw_chat_id, allow_unresolved=False)
            if not chat_id or chat_id in seen_sids:
                continue
            seen_sids.add(chat_id)
            if self.level_for(chat_id) == "quiet":
                continue
            platform = self.platform_for(chat_id) if callable(self.platform_for) else ""
            if not self.enabled(chat_id, platform):
                continue
            act = self.activity(chat_id) if callable(self.activity) else {}
            silent = now - float(act.get("last_human_at") or 0.0)
            if (
                int(act.get("human_count") or 0) < 3
                or float(act.get("last_human_at") or 0.0) <= 0
                or silent < threshold
            ):
                continue
            if not self.intents.acquaintance_allowed(
                chat_id,
                admins=admins,
                acquaintances=self.acquaintance_users,
                payload={"platform": platform},
            ):
                continue
            target = "chat:" + chat_id
            if not self._social_gate(target, sleep_window="")[0]:
                continue
            if not self.intents.longing_ready(chat_id, now=now):
                continue
            if not self._proactive_quota_available(target, now=now, payload={"platform": platform}):
                continue
            found.append(
                {
                    "chat_id": chat_id,
                    "platform": platform,
                    "type": self._chat_arena(chat_id) or "group",
                    "silent_hours": int(silent // 3600),
                }
            )
        found.sort(key=lambda item: -item["silent_hours"])
        for index, item in enumerate(found[:5], start=1):
            item["ref"] = f"c{index}"
        return found[:5]

    async def _heartbeat_decide(self, temperament, plan, journal_tail, candidates):
        """心跳第 2 步：一次便宜判断，返回 (action, 参数, outcome)。任何异常都按静默处理。"""
        public = [
            {"ref": c["ref"], "type": c["type"], "silent_hours": c["silent_hours"]}
            for c in candidates
        ]
        system = (
            "你是 MaiBot 的心跳：隔一段时间醒来一次，决定这次要不要做点什么。默认静默：拿不准、没有自然由头或最近做过类似的事，就选 ok。\n"
            '只能三选一，只输出 JSON：{"action":"ok"}；'
            '{"action":"miss","ref":"候选 ref","reason":"一句话由头"}（想起一个久未聊天的熟人会话，之后由那个会话里的 MaiBot 自己决定说不说）；'
            '{"action":"explore","topic":"一句话方向"}（按探索方向去公开冲浪，只围绕公开主题）。'
            "没有候选就不能选 miss，没有探索方向就不能选 explore。"
        )
        if temperament:
            system += "\n<maisya_temperament>" + html.escape(temperament) + "</maisya_temperament>"
        user = json.dumps(
            {"candidates": public, "explore_directions": plan, "recent_journal": journal_tail},
            ensure_ascii=False,
        )
        try:
            async with asyncio.timeout(20):
                response = await call_capability(
                    self.host,
                    "llm.generate_with_tools",
                    timeout_ms=20000,
                    prompt=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    tools=[],
                    task_name="utils",
                    model_name="",
                    max_tokens=200,
                )
            raw = str((response or {}).get("response") or "")
            match = re.search(r"\{[\s\S]*\}", raw)
            decision = json.loads(match.group(0)) if match else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return "ok", None, "ok:fallback", type(exc).__name__
        if not isinstance(decision, dict):
            return "ok", None, "ok:fallback", "invalid_json"
        action = str(decision.get("action") or "").strip().lower()
        if action == "miss":
            ref = str(decision.get("ref") or "").strip()
            hit = next((c for c in candidates if c["ref"] == ref), None)
            reason = str(decision.get("reason") or "").strip()[:200]
            if hit is None or not reason:
                return "ok", None, "ok:fallback", "invalid_miss"
            return "miss", (hit, reason), "miss", reason
        if action == "explore":
            topic = str(decision.get("topic") or "").strip()[:200]
            if not plan or not topic:
                return "ok", None, "ok:fallback", "invalid_explore"
            return "explore", topic, "explore", topic
        return "ok", None, "ok:model", ""

    async def _wake(self, chat_id, intent, *, reason, metadata):
        try:
            result = await call_capability(
                self.host,
                "maisaka.proactive.trigger",
                timeout_ms=30000,
                stream_id=chat_id,
                intent=intent,
                reason=reason,
                metadata=metadata,
            )
            if (
                isinstance(result, dict)
                and not result.get("success", True)
                and "未找到已存在的聊天流" in str(result.get("error", ""))
            ):
                reopened = await self._reopen_session(chat_id)
                if reopened:
                    result = await call_capability(
                        self.host,
                        "maisaka.proactive.trigger",
                        timeout_ms=30000,
                        stream_id=chat_id,
                        intent=intent,
                        reason=reason,
                        metadata=metadata,
                    )
                if (
                    isinstance(result, dict)
                    and not result.get("success", True)
                    and "未找到已存在的聊天流" in str(result.get("error", ""))
                ):
                    return "session_missing"
            accepted = bool(result) and (
                not isinstance(result, dict) or result.get("success", True)
            )
            return "accepted" if accepted else "rejected"
        except (RuntimeError, OSError, ValueError, TypeError):
            return "uncertain"

    async def _reopen_session_id(self, chat_id):
        args = None
        if callable(self.routes):
            route = await asyncio.to_thread(self.routes, chat_id)
            if route and route.get("platform"):
                if route["chat_type"] == "group" and route.get("group_id"):
                    args = {
                        "platform": route["platform"],
                        "chat_type": "group",
                        "user_id": route.get("user_id", ""),
                        "group_id": route.get("group_id", ""),
                    }
                elif route["chat_type"] == "private" and route.get("user_id"):
                    args = {
                        "platform": route["platform"],
                        "chat_type": "private",
                        "user_id": route.get("user_id", ""),
                        "group_id": "",
                    }
                if args is not None:
                    if route.get("account_id"):
                        args["account_id"] = route["account_id"]
                    if route.get("scope"):
                        args["scope"] = route["scope"]
        if args is None:
            parsed = parse_stream_spec(chat_id)
            if parsed is not None:
                plat, ctype, tid = parsed
                args = {
                    "platform": plat,
                    "chat_type": ctype,
                    "user_id": tid if ctype == "private" else "",
                    "group_id": tid if ctype == "group" else "",
                }
        if args is None:
            return ""
        opened = await call_capability(
            self.host, "chat.open_session", timeout_ms=15000, **args
        )
        if not opened or (isinstance(opened, dict) and not opened.get("success", True)):
            return ""
        if isinstance(opened, dict):
            return str(opened.get("session_id") or opened.get("stream_id") or "").strip()
        return ""

    async def _reopen_session(self, chat_id):
        if not callable(self.routes) and parse_stream_spec(chat_id) is None:
            return False
        args = None
        if callable(self.routes):
            route = await asyncio.to_thread(self.routes, chat_id)
            if route and route.get("platform"):
                if route["chat_type"] == "group" and not route.get("group_id"):
                    return False
                if route["chat_type"] == "private" and not route.get("user_id"):
                    return False
                args = {
                    "platform": route["platform"],
                    "chat_type": route["chat_type"],
                    "user_id": route.get("user_id", ""),
                    "group_id": route.get("group_id", ""),
                }
                if route.get("account_id"):
                    args["account_id"] = route["account_id"]
                if route.get("scope"):
                    args["scope"] = route["scope"]
        if args is None:
            parsed = parse_stream_spec(chat_id)
            if parsed is None:
                return False
            plat, ctype, tid = parsed
            args = {
                "platform": plat,
                "chat_type": ctype,
                "user_id": tid if ctype == "private" else "",
                "group_id": tid if ctype == "group" else "",
            }
        opened = await call_capability(
            self.host, "chat.open_session", timeout_ms=15000, **args
        )
        return bool(opened) and (
            not isinstance(opened, dict) or opened.get("success", True)
        )

    def _heartbeat_file_path(self):
        if self.memory is None:
            return None
        paths = getattr(self.memory, "paths", None)
        if isinstance(paths, dict) and paths.get("heartbeat"):
            return Path(paths["heartbeat"])
        if callable(paths):
            try:
                resolved = paths("heartbeat")
                if resolved:
                    return Path(resolved)
            except TypeError:
                pass
        if paths is not None and hasattr(paths, "heartbeat"):
            resolved = getattr(paths, "heartbeat")
            if resolved:
                return Path(resolved)
        root = getattr(self.memory, "root", None)
        if root:
            return Path(root) / "memory/self/HEARTBEAT.md"
        return None

    def _claim_heartbeat(self, now):
        with self.intents.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT last_proactive FROM social_state WHERE chat_id='__self_heartbeat__'"
            ).fetchone()
            last_run = row["last_proactive"] if row else 0.0
            min_gap = self.heartbeat_interval * HEARTBEAT_JITTER[0]
            if now - last_run < min_gap:
                self._heartbeat_next_at = float(last_run) + min_gap
                return False
            db.execute(
                "INSERT INTO social_state (chat_id, locked, last_proactive) "
                "VALUES ('__self_heartbeat__', 0, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "last_proactive=excluded.last_proactive",
                (now,),
            )
        self._schedule_next_heartbeat(now)
        return True

    def _schedule_next_heartbeat(self, now):
        low, high = HEARTBEAT_JITTER
        factor = low + (high - low) * min(1.0, max(0.0, float(self.rng())))
        self._heartbeat_next_at = float(now) + self.heartbeat_interval * factor

    def _find_queued_heartbeat_tid(self) -> str:
        if self._queued_heartbeat_tid:
            return self._queued_heartbeat_tid
        store = getattr(self.bridge, "store", None) if self.bridge is not None else None
        if store is None or not hasattr(store, "connect"):
            return ""
        try:
            with store.connect() as db:
                row = db.execute(
                    "SELECT t.thread_id FROM agent_threads t "
                    "LEFT JOIN thread_context tc ON tc.thread_id = t.thread_id "
                    "LEFT JOIN agent_workspaces w ON w.workspace_id = tc.workspace_id "
                    "WHERE t.owner='self' AND t.chat_id='self' "
                    "AND (t.processed_revision < t.revision OR t.state='active') "
                    "AND w.name='heartbeat' "
                    "ORDER BY t.updated DESC LIMIT 1"
                ).fetchone()
                return str(row["thread_id"]) if row else ""
        except sqlite3.Error:
            return ""

    async def _handle_queued_heartbeat(self, tid: str) -> bool:
        if not tid:
            return False
        in_tasks, is_queued, _ = await self._thread_state_after_dispatch(tid, "self")
        if in_tasks:
            self._queued_heartbeat_tid = tid
            return True
        if is_queued:
            kicked = await self._engine_call(
                "kick_queued", tid, "self", background=True
            )
            if kicked is False:
                self._queued_heartbeat_tid = tid
                return True
            if getattr(self.engine, "kick_queued", None) is not None:
                in_tasks2, is_queued2, _ = await self._thread_state_after_dispatch(
                    tid, "self"
                )
                if not in_tasks2 and is_queued2:
                    self._queued_heartbeat_tid = tid
                    return True
            self._queued_heartbeat_tid = ""
            return True
        self._queued_heartbeat_tid = ""
        return False

    def _is_heartbeat_enabled(self) -> bool:
        val = self.heartbeat_enabled
        return bool(val() if callable(val) else val)

    async def _heartbeat(self):
        if (
            self.closing
            or not self.plugin_enabled()
            or not self._is_heartbeat_enabled()
            or self.heartbeat_interval <= 0
            or self.memory is None
            or self.profiles is None
            or self.engine is None
            or self.intents is None
            or (
                self.proactive_level == "quiet"
                and not any(
                    v in {"care", "lively"} for v in self.chat_proactive_levels.values()
                )
            )
            or self._in_sleep_window()
            or any(
                self.engine.chats.get(tid) == "self" for tid in self.engine.tasks
            )
        ):
            return
        try:
            if self._queued_heartbeat_tid and await self._handle_queued_heartbeat(
                self._queued_heartbeat_tid
            ):
                return
            now = self.intents.clock()
            if self.heartbeat_interval != self._heartbeat_interval_used:
                self._heartbeat_next_at = 0.0
                self._heartbeat_interval_used = self.heartbeat_interval
            if now < self._heartbeat_last_clock:
                self._heartbeat_next_at = 0.0
            self._heartbeat_last_clock = now
            if now < self._heartbeat_next_at:
                return

            hb_path = self._heartbeat_file_path()
            if hb_path is not None:
                try:
                    st = hb_path.stat()
                except OSError:
                    st = None
                if st is not None:
                    mtime_ns = st.st_mtime_ns
                    size = st.st_size
                    if (
                        self._heartbeat_cached_mtime == mtime_ns
                        and self._heartbeat_cached_size == size
                    ):
                        temperament, plan = self._heartbeat_cached_parts
                    else:
                        heartbeat = await asyncio.to_thread(
                            self.memory.read, "heartbeat"
                        )
                        temperament, plan = MemoryLayers.heartbeat_parts(
                            heartbeat.get("content") or ""
                        )
                        self._heartbeat_cached_mtime = mtime_ns
                        self._heartbeat_cached_size = size
                        self._heartbeat_cached_parts = (temperament, plan)
                elif self._heartbeat_cached_mtime == -1:
                    temperament, plan = self._heartbeat_cached_parts
                else:
                    heartbeat = await asyncio.to_thread(self.memory.read, "heartbeat")
                    temperament, plan = MemoryLayers.heartbeat_parts(
                        heartbeat.get("content") or ""
                    )
                    self._heartbeat_cached_mtime = -1
                    self._heartbeat_cached_size = 0
                    self._heartbeat_cached_parts = (temperament, plan)
            else:
                heartbeat = await asyncio.to_thread(self.memory.read, "heartbeat")
                temperament, plan = MemoryLayers.heartbeat_parts(
                    heartbeat.get("content") or ""
                )
            ability = None
            if plan:
                try:
                    ability = await asyncio.to_thread(
                        self.profiles.get, self.heartbeat_profile
                    )
                except (ValueError, PermissionError, OSError) as exc:
                    logger.warning(
                        "自主探索档案不可用 profile=%s: %s", self.heartbeat_profile, exc
                    )
                if ability is not None and (not ability.enabled or not ability.autonomous):
                    ability = None
            if ability is not None:
                queued_tid = await asyncio.to_thread(self._find_queued_heartbeat_tid)
                if queued_tid and await self._handle_queued_heartbeat(queued_tid):
                    return
            # 第 1 步：代码收集由头，没有由头就原地静默、不调模型
            candidates = await asyncio.to_thread(self._longing_candidates, now)
            if ability is None and not candidates:
                self._schedule_next_heartbeat(now)
                await asyncio.to_thread(
                    self.intents.record_heartbeat_tick, "ok:no_reason", "", at=now
                )
                return
            exhausted, b_reason = await asyncio.to_thread(self._bg_budget_exhausted, now)
            if exhausted:
                self._schedule_next_heartbeat(now)
                await asyncio.to_thread(
                    self.intents.record_heartbeat_tick, "budget_paused", b_reason, at=now
                )
                return
            if not await asyncio.to_thread(self._claim_heartbeat, now):
                return
            hb_t0 = time.monotonic()
            journal_obj = await asyncio.to_thread(self.memory.read, "journal")
            journal_text = str((journal_obj or {}).get("content") or "").strip()
            recent_journal = self.redact(journal_text)[-600:] if journal_text else ""
            # 第 2 步：一次便宜判断
            action, arg, outcome, detail = await self._heartbeat_decide(
                temperament,
                plan if ability is not None else "",
                recent_journal[-400:],
                candidates,
            )
            await asyncio.to_thread(
                self._record_bg_usage_sync,
                source="heartbeat",
                target="self",
                runs=1,
                duration_ms=int((time.monotonic() - hb_t0) * 1000),
                now=now,
            )
            # 第 3 步：落地
            if action == "miss":
                hit, reason = arg
                try:
                    await asyncio.to_thread(
                        self.intents.schedule,
                        kind="proactive",
                        source="longing",
                        target="chat:" + hit["chat_id"],
                        due_at=now,
                        reason=reason,
                        payload={"platform": hit["platform"]},
                    )
                except ValueError as exc:
                    outcome, detail = "skip:schedule", str(exc)
            await asyncio.to_thread(
                self.intents.record_heartbeat_tick, outcome, detail, at=now
            )
            if action != "explore":
                return
            prompt = (
                "按自身节奏做一次轻量公开探索并记录近况（不涉及任何具体会话私有信息）：\n"
                + plan
                + "\n本次方向："
                + arg
            )
            if temperament:
                prompt += "\n<maisya_temperament>" + html.escape(temperament) + "</maisya_temperament>"
            if recent_journal:
                prompt += (
                    "\n以下为近期已记录的手账，请避开与最近手账重复的主题，不要重复写进 JOURNAL.md 或再次汇报：\n"
                    "<recent_journal>"
                    + html.escape(recent_journal)
                    + "</recent_journal>"
                )
            started = await self._engine_call(
                "start",
                ability,
                prompt,
                chat_id="self",
                platform="self",
                user_id="",
                owner="self",
                wait_seconds=0,
                workspace="heartbeat",
                background=True,
                defer_if_full=True,
            )
            started_tid = str((started or {}).get("thread_id") or "")
            _, is_queued, _ = await self._thread_state_after_dispatch(
                started_tid, "self", started
            )
            if started_tid and is_queued:
                self._queued_heartbeat_tid = started_tid
        except (ValueError, PermissionError, RuntimeError, OSError, sqlite3.Error) as exc:
            logger.warning(
                "自主探索调度跳过 profile=%s: %s", self.heartbeat_profile, exc
            )
            return

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while not self.closing:
            await asyncio.sleep(1)
            try:
                await self.tick(background_tick=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "Maisya 回接检查暂未完成，结果保留在线程中。"
                )

    async def close(self):
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
