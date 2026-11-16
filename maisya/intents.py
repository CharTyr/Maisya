"""意图存储与社交分寸状态：提醒、主动搭话与自身活动统一经 Pulse 过闸。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import re
import sqlite3
import time
import uuid

from .trust import STREAM_SPEC_RE, format_stream_spec, parse_stream_spec

KINDS = {"return", "proactive", "silent"}
MODES = {"schedule", "once", "recurring", "afterthought", "watch"}
SOURCES = {
    "thread",
    "afterglow",
    "serendipity",
    "longing",
    "later",
    "webhook",
    "workflow",
    "watch",
}
CHAT_TARGET = re.compile(
    r"^chat:(?:[^\s:][^\s]{0,255}|[a-z][a-z0-9_-]{0,31}:(?:group|private):[^\s:][^\s]{0,199})$"
)
DAILY_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_payload_strict(raw) -> tuple[dict, bool]:
    if isinstance(raw, dict):
        return raw, True
    if raw is None or raw == "":
        return {}, True
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}, False
    if not isinstance(loaded, dict):
        return {}, False
    return loaded, True


def _parse_payload(raw) -> dict:
    return _parse_payload_strict(raw)[0]


def is_profileless_daily(source: str, payload: dict | None) -> bool:
    if not isinstance(payload, dict) or not payload.get("daily_at"):
        return False
    return (
        str(source or "") != "workflow"
        and not str(payload.get("profile") or "").strip()
        and not bool(payload.get("scheduled_admin"))
    )


def next_daily_due(
    now_ts: float, hhmm: str, tz_offset_minutes: int = 480
) -> tuple[float, str]:
    """按给定每日钟点（HH:MM）和时区偏移计算下一次触发时间戳（至少在 now+5s 之后）。"""
    match = DAILY_TIME_RE.fullmatch(str(hhmm or ""))
    if not match:
        raise ValueError("每日时刻格式应为 24 小时制 HH:MM（如 08:30）")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("每日时刻小时应为 00-23、分钟应为 00-59")
    offset = int(tz_offset_minutes)
    if not -720 <= offset <= 840:
        raise ValueError("时区偏移应在 -720 到 840 分钟之间")
    tz = timezone(timedelta(minutes=offset))
    now_f = float(now_ts)
    now_dt = datetime.fromtimestamp(now_f, tz=tz)
    candidate = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now_f + 5:
        candidate = candidate + timedelta(days=1)
    return candidate.timestamp(), f"{hour:02d}:{minute:02d}"


_SLEEP_ENDPOINT_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")


def _validate_tz_offset_minutes(tz_offset_minutes: object) -> int:
    if isinstance(tz_offset_minutes, bool) or not isinstance(tz_offset_minutes, int):
        raise ValueError("时区偏移必须是整数分钟")
    if not -720 <= tz_offset_minutes <= 840:
        raise ValueError("时区偏移应在 -720 到 840 分钟之间")
    return tz_offset_minutes


def _parse_sleep_endpoint(raw: str) -> int | None:
    match = _SLEEP_ENDPOINT_RE.fullmatch(str(raw or "").strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) is not None else 0
    if hour == 24:
        return 1440 if minute == 0 else None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


class Intents:
    def __init__(
        self,
        store,
        *,
        clock=time.time,
        daily_proactive_limit=1,
        tz_offset_minutes=480,
    ):
        self.store, self.clock = store, clock
        self.daily_proactive_limit = int(daily_proactive_limit)
        if tz_offset_minutes is None:
            self.tz_offset_minutes = 480
        elif callable(tz_offset_minutes):
            _validate_tz_offset_minutes(tz_offset_minutes())
            self.tz_offset_minutes = tz_offset_minutes
        else:
            self.tz_offset_minutes = _validate_tz_offset_minutes(tz_offset_minutes)
        with store.connect() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS intents ("
                "intent_id TEXT PRIMARY KEY, kind TEXT, source TEXT, target TEXT, "
                "due_at REAL, reason TEXT, thread_id TEXT, payload TEXT, "
                "status TEXT, wake_status TEXT, created REAL, handled REAL, "
                "bypass_limits INTEGER NOT NULL DEFAULT 0, "
                "mode TEXT NOT NULL DEFAULT 'schedule', "
                "condition TEXT NOT NULL DEFAULT '', "
                "last_checked_at REAL, last_triggered_at REAL)"
            )
            intent_cols = {
                row[1] for row in db.execute("PRAGMA table_info(intents)").fetchall()
            }
            if "bypass_limits" not in intent_cols:
                db.execute(
                    "ALTER TABLE intents ADD COLUMN bypass_limits INTEGER NOT NULL DEFAULT 0"
                )
            if "mode" not in intent_cols:
                db.execute(
                    "ALTER TABLE intents ADD COLUMN mode TEXT NOT NULL DEFAULT 'schedule'"
                )
            if "condition" not in intent_cols:
                db.execute(
                    "ALTER TABLE intents ADD COLUMN condition TEXT NOT NULL DEFAULT ''"
                )
            if "last_checked_at" not in intent_cols:
                db.execute("ALTER TABLE intents ADD COLUMN last_checked_at REAL")
            if "last_triggered_at" not in intent_cols:
                db.execute("ALTER TABLE intents ADD COLUMN last_triggered_at REAL")
            db.execute(
                "CREATE INDEX IF NOT EXISTS intent_due ON intents(status, kind, due_at)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS social_state (chat_id TEXT PRIMARY KEY, "
                "locked INTEGER DEFAULT 0, last_proactive REAL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS session_routes ("
                "session_id TEXT PRIMARY KEY, platform TEXT NOT NULL, "
                "chat_type TEXT NOT NULL, group_id TEXT NOT NULL, user_id TEXT NOT NULL, "
                "account_id TEXT NOT NULL, scope TEXT NOT NULL, updated REAL NOT NULL, "
                "human_count INTEGER NOT NULL DEFAULT 0, last_human_at REAL NOT NULL DEFAULT 0)"
            )
            cols = {
                row[1]
                for row in db.execute("PRAGMA table_info(session_routes)").fetchall()
            }
            if "human_count" not in cols:
                db.execute(
                    "ALTER TABLE session_routes ADD COLUMN human_count INTEGER NOT NULL DEFAULT 0"
                )
            if "last_human_at" not in cols:
                db.execute(
                    "ALTER TABLE session_routes ADD COLUMN last_human_at REAL NOT NULL DEFAULT 0"
                )

    def _tz_offset_minutes(self, tz_offset_minutes=None) -> int:
        raw = (
            getattr(self, "tz_offset_minutes", 480)
            if tz_offset_minutes is None
            else tz_offset_minutes
        )
        if callable(raw):
            raw = raw()
        return _validate_tz_offset_minutes(raw)

    def _intent(self, row):
        result = dict(row)
        result["payload"] = _parse_payload(result.get("payload"))
        result["mode"] = str(result.get("mode") or "schedule") or "schedule"
        result["condition"] = str(result.get("condition") or "")
        result.setdefault("last_checked_at", None)
        result.setdefault("last_triggered_at", None)
        return result

    def get(self, intent_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        if row is None:
            raise ValueError("意图不存在")
        return self._intent(row)

    def list(self, target, *, limit=20, include_paused: bool = False):
        statuses = "('pending','paused')" if include_paused else "('pending')"
        with self.store.connect() as db:
            rows = db.execute(
                f"SELECT * FROM intents WHERE target=? AND status IN {statuses} "
                "ORDER BY due_at LIMIT ?",
                (target, min(max(int(limit), 1), 50)),
            ).fetchall()
        return [self._intent(row) for row in rows]

    def list_all(self, *, status: str = "pending", limit: int = 50) -> list[dict]:
        bounded = min(max(int(limit), 1), 200)
        with self.store.connect() as db:
            if status:
                rows = db.execute(
                    "SELECT * FROM intents WHERE status=? ORDER BY due_at LIMIT ?",
                    (status, bounded),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM intents ORDER BY due_at DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
        return [self._intent(row) for row in rows]

    def cancel_matching_workflows(
        self,
        target: str,
        *,
        profile: str,
        workspace: str = "",
        is_admin: bool | None = None,
    ) -> list[str]:
        if not target or not profile:
            return []
        now = self.clock()
        cancelled = []
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT intent_id, payload FROM intents WHERE target=? AND status IN ('pending', 'paused')",
                (target,),
            ).fetchall()
            for row in rows:
                payload = _parse_payload(row["payload"])
                if not payload:
                    continue
                if is_admin is False and payload.get("scheduled_admin") is True:
                    continue
                if not (payload.get("every_seconds") or payload.get("daily_at")):
                    continue
                if payload.get("profile") != profile:
                    continue
                if workspace and str(payload.get("workspace") or "") != workspace:
                    continue
                db.execute(
                    "UPDATE intents SET status='cancelled', handled=? WHERE intent_id=? AND status IN ('pending', 'paused')",
                    (now, row["intent_id"]),
                )
                cancelled.append(row["intent_id"])
        return cancelled

    def schedule(
        self,
        *,
        kind,
        source,
        target,
        due_at,
        reason,
        thread_id="",
        payload=None,
        bypass_limits=False,
        mode="",
        condition="",
        last_checked_at=None,
        last_triggered_at=None,
    ):
        if kind not in KINDS or source not in SOURCES or not CHAT_TARGET.fullmatch(target):
            raise ValueError("意图类型、来源或目标无效")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("意图需要一句 500 字符以内的理由")
        norm_mode = str(mode or ("watch" if source == "watch" else "schedule")).strip().lower()
        if norm_mode not in MODES:
            raise ValueError("意图模式无效")
        if condition is None:
            norm_condition = ""
        elif isinstance(condition, str):
            norm_condition = condition.strip()
        else:
            raise ValueError("盯梢条件必须是字符串")
        if len(norm_condition) > 500:
            raise ValueError("盯梢条件需在 500 字符以内")
        checked_ts = float(last_checked_at) if last_checked_at is not None else None
        triggered_ts = float(last_triggered_at) if last_triggered_at is not None else None
        now = self.clock()
        try:
            due_at = float(due_at)
        except (TypeError, ValueError):
            raise ValueError("到期时间无效") from None
        if not now - 5 <= due_at <= now + 365 * 86400:
            raise ValueError("到期时间应在现在到一年之间")
        intent_id = "in_" + uuid.uuid4().hex[:16]
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            payload_dict = payload if isinstance(payload, dict) else {}
            effective_bypass = bool(bypass_limits) and not is_profileless_daily(
                source, payload_dict
            )
            if kind == "proactive" and not effective_bypass and norm_mode != "watch":
                limit = int(self.daily_proactive_limit)
                start, end = self._day_bounds(due_at)
                arena = self._target_arena(db, target, payload_dict or None)
                counts = self._proactive_counts(db, start, end)
                if limit <= 0 or counts[arena] >= limit:
                    raise ValueError("普通惦记已达到每日上限")
            db.execute(
                "INSERT INTO intents (intent_id, kind, source, target, due_at, reason, thread_id, payload, status, wake_status, created, handled, bypass_limits, mode, condition, last_checked_at, last_triggered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, NULL, ?, ?, ?, ?, ?)",
                (
                    intent_id,
                    kind,
                    source,
                    target,
                    due_at,
                    reason.strip(),
                    thread_id,
                    encode(payload or {}),
                    now,
                    1 if effective_bypass else 0,
                    norm_mode,
                    norm_condition,
                    checked_ts,
                    triggered_ts,
                ),
            )
        return self.get(intent_id)

    def _require_modifiable_intent(self, db, intent_id: str, target: str, *, statuses: tuple[str, ...], is_admin: bool | None):
        placeholders = ",".join("?" for _ in statuses)
        row = db.execute(
            f"SELECT * FROM intents WHERE intent_id=? AND target=? AND status IN ({placeholders})",
            (intent_id, target, *statuses),
        ).fetchone()
        if row is None:
            raise ValueError("意图不存在或已处理")
        payload = _parse_payload(row["payload"])
        if is_admin is False and payload.get("scheduled_admin") is True:
            raise PermissionError("管理员排定的定时或盯梢工作流不可由非管理员修改或取消")
        return row

    def cancel(self, intent_id, target, *, is_admin: bool | None = None):
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_modifiable_intent(
                db, intent_id, target, statuses=("pending", "paused"), is_admin=is_admin
            )
            db.execute(
                "UPDATE intents SET status='cancelled', handled=? "
                "WHERE intent_id=? AND target=? AND status IN ('pending', 'paused')",
                (self.clock(), intent_id, target),
            )
        return {"intent_id": intent_id, "status": "cancelled"}

    def pause(self, intent_id, target, *, is_admin: bool | None = None):
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_modifiable_intent(
                db, intent_id, target, statuses=("pending",), is_admin=is_admin
            )
            db.execute(
                "UPDATE intents SET status='paused' WHERE intent_id=? AND target=? AND status='pending'",
                (intent_id, target),
            )
        return self.get(intent_id)

    def resume(self, intent_id, target, *, is_admin: bool | None = None):
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = self._require_modifiable_intent(
                db, intent_id, target, statuses=("paused",), is_admin=is_admin
            )
            due_at = max(float(row["due_at"] or 0.0), self.clock())
            db.execute(
                "UPDATE intents SET status='pending', due_at=? WHERE intent_id=? AND target=? AND status='paused'",
                (due_at, intent_id, target),
            )
        return self.get(intent_id)

    def record_watch_check(
        self,
        intent_id: str,
        *,
        checked_at: float | None = None,
        triggered: bool = False,
        triggered_at: float | None = None,
        next_due_at: float | None = None,
        wake_status: str = "",
        payload: dict | None = None,
        thread_id: str | None = None,
    ) -> dict:
        now = float(self.clock() if checked_at is None else checked_at)
        trig_val = (
            float(now if triggered_at is None else triggered_at) if triggered else None
        )
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if row is None:
                raise ValueError("意图不存在")
            final_trig = trig_val if triggered else row["last_triggered_at"]
            next_payload = (
                encode(payload) if payload is not None else row["payload"]
            )
            next_tid = row["thread_id"] if thread_id is None else thread_id
            if next_due_at is not None:
                db.execute(
                    "UPDATE intents SET status='pending', due_at=?, handled=NULL, "
                    "wake_status=?, last_checked_at=?, last_triggered_at=?, payload=?, thread_id=? "
                    "WHERE intent_id=?",
                    (
                        float(next_due_at),
                        wake_status or ("watch_triggered" if triggered else "watch_unmet"),
                        now,
                        final_trig,
                        next_payload,
                        next_tid,
                        intent_id,
                    ),
                )
            else:
                db.execute(
                    "UPDATE intents SET status='done', handled=?, "
                    "wake_status=?, last_checked_at=?, last_triggered_at=?, payload=?, thread_id=? "
                    "WHERE intent_id=?",
                    (
                        now,
                        wake_status or ("accepted" if triggered else "watch_unmet"),
                        now,
                        final_trig,
                        next_payload,
                        next_tid,
                        intent_id,
                    ),
                )
        return self.get(intent_id)

    def due(self, enabled, *, limit=8):
        now = self.clock()
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM intents WHERE status='pending' AND due_at<=? "
                "ORDER BY due_at LIMIT 64",
                (now,),
            ).fetchall()
            claimed, corrupt_ids, proactive_claimed = [], [], False
            for row in rows:
                payload, valid_payload = _parse_payload_strict(row["payload"])
                if not valid_payload:
                    corrupt_ids.append(row["intent_id"])
                    continue
                target = str(row["target"] or "")
                chat_id = target.split(":", 1)[1] if ":" in target else target
                platform = str(payload.get("platform") or "")
                if row["kind"] == "proactive":
                    if proactive_claimed or not enabled(chat_id, platform):
                        continue
                    proactive_claimed = True
                claimed.append(row["intent_id"])
                if len(claimed) >= limit:
                    break
            if corrupt_ids:
                db.execute(
                    "UPDATE intents SET status='failed', wake_status='corrupt_payload', handled=? WHERE intent_id IN ("
                    + ",".join("?" for _ in corrupt_ids)
                    + ")",
                    (now, *corrupt_ids),
                )
            if claimed:
                db.execute(
                    "UPDATE intents SET status='claimed' WHERE intent_id IN ("
                    + ",".join("?" for _ in claimed)
                    + ")",
                    claimed,
                )
            return [self._intent(row) for row in rows if row["intent_id"] in claimed]

    def update_thread(self, intent_id, thread_id, payload=None):
        with self.store.connect() as db, db:
            if payload is None:
                db.execute(
                    "UPDATE intents SET thread_id=? WHERE intent_id=?",
                    (thread_id, intent_id),
                )
            else:
                db.execute(
                    "UPDATE intents SET thread_id=?, payload=? WHERE intent_id=?",
                    (thread_id, encode(payload), intent_id),
                )

    def finish(self, intent_id, status, wake_status="", *, delay=900):
        if status not in {"done", "deferred", "failed"}:
            raise ValueError("意图处理结果无效")
        with self.store.connect() as db, db:
            if status == "deferred":
                db.execute(
                    "UPDATE intents SET status='pending', due_at=? "
                    "WHERE intent_id=? AND status='claimed'",
                    (self.clock() + delay, intent_id),
                )
            else:
                db.execute(
                    "UPDATE intents SET status=?, wake_status=?, handled=? "
                    "WHERE intent_id=? AND status='claimed'",
                    (status, wake_status, self.clock(), intent_id),
                )

    def requeue(self):
        with self.store.connect() as db, db:
            db.execute("UPDATE intents SET status='pending' WHERE status='claimed'")

    def _social_aliases(self, db, chat_id: str, extra=()) -> list[str]:
        seeds: list[str] = []
        for item in (chat_id, *(extra or ())):
            text = str(item or "").strip()
            if text.startswith("chat:"):
                text = text[5:].strip()
            if text and text not in seeds:
                seeds.append(text)
        seen: list[str] = []
        queue = list(seeds)
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.append(cur)
            parsed = parse_stream_spec(cur)
            if parsed is not None:
                plat, ctype, raw_id = parsed
                canon = format_stream_spec(plat, ctype, raw_id)
                if canon and canon not in seen and canon not in queue:
                    queue.append(canon)
                try:
                    col = "group_id" if ctype == "group" else "user_id"
                    rows = db.execute(
                        f"SELECT session_id FROM session_routes WHERE LOWER(platform)=? AND chat_type=? AND {col}=?",
                        (plat, ctype, raw_id),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = ()
                for r in rows:
                    sid = str(r["session_id"] or "").strip()
                    if sid and sid not in seen and sid not in queue:
                        queue.append(sid)
                continue
            try:
                route = db.execute(
                    "SELECT platform, chat_type, group_id, user_id FROM session_routes WHERE session_id=?",
                    (cur,),
                ).fetchone()
            except sqlite3.OperationalError:
                route = None
            if route:
                spec = format_stream_spec(
                    route["platform"],
                    route["chat_type"],
                    group_id=route["group_id"],
                    user_id=route["user_id"],
                )
                if spec and spec not in seen and spec not in queue:
                    queue.append(spec)
        return seen

    def note_received(self, chat_id, *, engaged: bool = True, aliases=()):
        if not engaged:
            return
        with self.store.connect() as db, db:
            alias_ids = self._social_aliases(db, chat_id, aliases)
            for alias in alias_ids:
                db.execute(
                    "INSERT INTO social_state (chat_id, locked) VALUES (?, 0) "
                    "ON CONFLICT(chat_id) DO UPDATE SET locked=0",
                    (alias,),
                )

    def in_sleep_window(self, sleep_window="", *, now=None, tz_offset_minutes=None):
        now = self.clock() if now is None else now
        offset_min = self._tz_offset_minutes(tz_offset_minutes)
        if not sleep_window or not isinstance(sleep_window, str):
            return False
        parts = sleep_window.split("-", 1)
        if len(parts) != 2:
            return False
        start = _parse_sleep_endpoint(parts[0])
        end = _parse_sleep_endpoint(parts[1])
        if start is None or end is None or start == end:
            return False
        minute_of_day = ((math.floor(float(now)) + offset_min * 60) % 86400) // 60
        return (
            start <= minute_of_day < end
            if start < end
            else minute_of_day >= start or minute_of_day < end
        )

    def social_gate(
        self,
        target,
        *,
        sleep_window="",
        now=None,
        aliases=(),
        tz_offset_minutes=None,
    ):
        if not CHAT_TARGET.fullmatch(target):
            return False, "invalid_target"
        chat_id = target.split(":", 1)[1]
        if self.in_sleep_window(
            sleep_window, now=now, tz_offset_minutes=tz_offset_minutes
        ):
            return False, "sleep_window"
        with self.store.connect() as db:
            alias_ids = self._social_aliases(db, chat_id, aliases)
            if alias_ids:
                placeholders = ",".join("?" for _ in alias_ids)
                state = db.execute(
                    f"SELECT MAX(locked) AS locked FROM social_state WHERE chat_id IN ({placeholders})",
                    alias_ids,
                ).fetchone()
            else:
                state = None
        if state and state["locked"]:
            return False, "unreplied_lock"
        return True, ""

    def known_chat(self, chat_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT 1 FROM trusted_sources WHERE session_id=? AND relayed=0 LIMIT 1",
                (chat_id,),
            ).fetchone()
            if row is not None:
                return True
            route = db.execute(
                "SELECT 1 FROM session_routes WHERE session_id=? LIMIT 1",
                (chat_id,),
            ).fetchone()
        return route is not None

    def _target_arena(self, db, target: str, payload: dict | None = None) -> str:
        raw_target = str(target or "").strip()
        chat_id = (
            raw_target.split(":", 1)[1]
            if raw_target.startswith("chat:")
            else raw_target
        )
        parsed = parse_stream_spec(chat_id)
        if parsed is not None:
            return parsed[1]
        if chat_id:
            try:
                route = db.execute(
                    "SELECT chat_type FROM session_routes WHERE session_id=?",
                    (chat_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                route = None
            if route and route["chat_type"] in {"group", "private"}:
                return str(route["chat_type"])
        if isinstance(payload, dict) and payload.get("chat_type") in {
            "group",
            "private",
        }:
            return str(payload["chat_type"])
        return "group"

    def _proactive_counts(
        self, db, start: float, end: float, *, exclude_intent_id: str = ""
    ) -> dict[str, int]:
        rows = db.execute(
            "SELECT intent_id, target, payload FROM intents WHERE kind='proactive' "
            "AND source IN ('afterglow','serendipity','longing','later') "
            "AND due_at>=? AND due_at<? AND status NOT IN ('cancelled','failed')",
            (start, end),
        ).fetchall()
        counts = {"group": 0, "private": 0}
        for row in rows:
            if exclude_intent_id and row["intent_id"] == exclude_intent_id:
                continue
            parsed_payload = _parse_payload(row["payload"])
            arena = self._target_arena(
                db,
                row["target"],
                parsed_payload or None,
            )
            counts[arena] = counts.get(arena, 0) + 1
        return counts

    def proactive_quota_available(
        self,
        target: str = "",
        *,
        now=None,
        exclude_intent_id: str = "",
        payload: dict | None = None,
        tz_offset_minutes=None,
    ) -> bool:
        limit = int(self.daily_proactive_limit)
        if limit <= 0:
            return False
        now = self.clock() if now is None else now
        start, end = self._day_bounds(now, tz_offset_minutes=tz_offset_minutes)
        with self.store.connect() as db:
            counts = self._proactive_counts(
                db, start, end, exclude_intent_id=exclude_intent_id
            )
            if target:
                arena = self._target_arena(
                    db, target, payload if isinstance(payload, dict) else None
                )
                return counts[arena] < limit
            return counts["group"] < limit or counts["private"] < limit

    def longing_ready(
        self, chat_id, *, now=None, cooldown=7 * 86400, aliases=()
    ) -> bool:
        now = self.clock() if now is None else now
        with self.store.connect() as db:
            alias_ids = self._social_aliases(db, chat_id, aliases)
            if alias_ids:
                a_ph = ",".join("?" for _ in alias_ids)
                states = db.execute(
                    f"SELECT locked, last_proactive FROM social_state WHERE chat_id IN ({a_ph})",
                    alias_ids,
                ).fetchall()
                for state in states:
                    if state["locked"] or (
                        state["last_proactive"]
                        and now - state["last_proactive"] < cooldown
                    ):
                        return False
                targets = ["chat:" + a for a in alias_ids]
                t_ph = ",".join("?" for _ in targets)
                active_proactive = db.execute(
                    f"SELECT 1 FROM intents WHERE target IN ({t_ph}) AND kind='proactive' "
                    "AND status IN ('pending','claimed') LIMIT 1",
                    targets,
                ).fetchone()
                if active_proactive is not None:
                    return False
                recent_longing = db.execute(
                    f"SELECT 1 FROM intents WHERE target IN ({t_ph}) AND source='longing' "
                    "AND status!='cancelled' "
                    "AND ((handled IS NOT NULL AND ? - handled < ?) OR ? - created < ?) "
                    "LIMIT 1",
                    (*targets, now, cooldown, now, cooldown),
                ).fetchone()
                return recent_longing is None
        return True

    def acquaintance_allowed(
        self, chat_id, *, admins=(), acquaintances=(), payload=None
    ) -> bool:
        trusted: set[str] = set()
        for raw in (*admins, *acquaintances):
            text = str(raw or "").strip()
            if ":" not in text:
                continue
            parsed = parse_stream_spec(text)
            if parsed is not None:
                plat_spec, kind_spec, tid_spec = parsed
                if kind_spec == "private":
                    trusted.add(f"{plat_spec}:{tid_spec}")
                continue
            p, u = text.split(":", 1)
            p_norm, u_norm = p.strip().lower(), u.strip()
            if p_norm and u_norm:
                trusted.add(f"{p_norm}:{u_norm}")
        with self.store.connect() as db:
            route = db.execute(
                "SELECT platform, chat_type, user_id, human_count "
                "FROM session_routes WHERE session_id=?",
                (chat_id,),
            ).fetchone()
            non_relayed = db.execute(
                "SELECT platform, user_id FROM trusted_sources "
                "WHERE session_id=? AND relayed=0 LIMIT 1",
                (chat_id,),
            ).fetchone()
        if route and route["chat_type"] == "group":
            return bool(route["human_count"] > 0 or non_relayed is not None)
        if (
            route
            and route["chat_type"] == "private"
            and (route["human_count"] > 0 or non_relayed is not None)
        ) or (route is None and non_relayed is not None):
            return True
        plat = str(
            (payload or {}).get("platform") or (route["platform"] if route else "")
        ).strip().lower()
        uid = str(
            (payload or {}).get("user_id") or (route["user_id"] if route else "")
        ).strip()
        return bool(plat and uid and f"{plat}:{uid}" in trusted)

    def mark_proactive(self, target, *, aliases=()):
        chat_id = target.split(":", 1)[1] if ":" in str(target or "") else str(target or "")
        now = self.clock()
        with self.store.connect() as db, db:
            alias_ids = self._social_aliases(db, chat_id, aliases)
            for alias in alias_ids:
                db.execute(
                    "INSERT INTO social_state (chat_id, locked, last_proactive) "
                    "VALUES (?, 1, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                    "locked=1, last_proactive=excluded.last_proactive",
                    (alias, now),
                )

    def _day_bounds(self, now, *, tz_offset_minutes=None):
        offset_sec = self._tz_offset_minutes(tz_offset_minutes) * 60
        now_sec = math.floor(float(now))
        start = now_sec - (now_sec + offset_sec) % 86400
        return start, start + 86400
