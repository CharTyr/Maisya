"""意图存储与社交分寸状态：提醒、主动搭话与自身活动统一经 Pulse 过闸。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import time
import uuid

from .trust import STREAM_SPEC_RE, format_stream_spec, parse_stream_spec

KINDS = {"return", "proactive", "silent"}
SOURCES = {
    "thread",
    "afterglow",
    "serendipity",
    "longing",
    "later",
    "webhook",
    "workflow",
}
CHAT_TARGET = re.compile(
    r"^chat:(?:[^\s:][^\s]{0,255}|[a-z][a-z0-9_-]{0,31}:(?:group|private):[^\s:][^\s]{0,199})$"
)
DAILY_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


class Intents:
    def __init__(self, store, *, clock=time.time, daily_proactive_limit=1):
        self.store, self.clock = store, clock
        self.daily_proactive_limit = int(daily_proactive_limit)
        with store.connect() as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS intents ("
                "intent_id TEXT PRIMARY KEY, kind TEXT, source TEXT, target TEXT, "
                "due_at REAL, reason TEXT, thread_id TEXT, payload TEXT, "
                "status TEXT, wake_status TEXT, created REAL, handled REAL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS intent_due ON intents(status, kind, due_at)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS social_state (chat_id TEXT PRIMARY KEY, "
                "locked INTEGER DEFAULT 0, last_proactive REAL DEFAULT 0)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS incoming_events (event_key TEXT PRIMARY KEY,digest TEXT,intent_id TEXT)"
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

    def _intent(self, row):
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def get(self, intent_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        if row is None:
            raise ValueError("意图不存在")
        return self._intent(row)

    def list(self, target, *, limit=20):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM intents WHERE target=? AND status='pending' "
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
        self, target: str, *, profile: str, workspace: str = ""
    ) -> list[str]:
        if not target or not profile:
            return []
        now = self.clock()
        cancelled = []
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT intent_id, payload FROM intents WHERE target=? AND status='pending'",
                (target,),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if not (payload.get("every_seconds") or payload.get("daily_at")):
                    continue
                if payload.get("profile") != profile:
                    continue
                if workspace and str(payload.get("workspace") or "") != workspace:
                    continue
                db.execute(
                    "UPDATE intents SET status='cancelled', handled=? WHERE intent_id=? AND status='pending'",
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
        event_key="",
        event_digest="",
    ):
        if kind not in KINDS or source not in SOURCES or not CHAT_TARGET.fullmatch(target):
            raise ValueError("意图类型、来源或目标无效")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("意图需要一句 500 字符以内的理由")
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
            if event_key:
                old = db.execute(
                    "SELECT digest,intent_id FROM incoming_events WHERE event_key=?",
                    (event_key,),
                ).fetchone()
                if old:
                    if old["digest"] != event_digest:
                        raise ValueError("同一事件编号已绑定另一份内容")
                    return self.get(old["intent_id"])
            if kind == "proactive" and not bypass_limits:
                limit = int(self.daily_proactive_limit)
                start, end = self._day_bounds(now)
                arena = self._target_arena(
                    db, target, payload if isinstance(payload, dict) else None
                )
                counts = self._proactive_counts(db, start, end)
                if limit <= 0 or counts[arena] >= limit:
                    raise ValueError("普通惦记已达到每日上限")
            db.execute(
                "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, NULL)",
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
                ),
            )
            if event_key:
                db.execute(
                    "INSERT INTO incoming_events VALUES (?,?,?)",
                    (event_key, event_digest, intent_id),
                )
        return self.get(intent_id)

    def cancel(self, intent_id, target):
        with self.store.connect() as db, db:
            cursor = db.execute(
                "UPDATE intents SET status='cancelled', handled=? "
                "WHERE intent_id=? AND target=? AND status='pending'",
                (self.clock(), intent_id, target),
            )
            if not cursor.rowcount:
                raise ValueError("意图不存在或已处理")
        return {"intent_id": intent_id, "status": "cancelled"}

    def due(self, enabled, *, limit=8):
        now = self.clock()
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM intents WHERE status='pending' AND due_at<=? "
                "ORDER BY due_at LIMIT 64",
                (now,),
            ).fetchall()
            claimed, proactive_claimed = [], False
            for row in rows:
                chat_id = row["target"].split(":", 1)[1]
                platform = json.loads(row["payload"]).get("platform", "")
                if row["kind"] == "proactive":
                    if proactive_claimed or not enabled(chat_id, platform):
                        continue
                    proactive_claimed = True
                claimed.append(row["intent_id"])
                if len(claimed) >= limit:
                    break
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

    def note_received(self, chat_id, *, engaged: bool = True):
        if not engaged:
            return
        with self.store.connect() as db, db:
            db.execute(
                "INSERT INTO social_state (chat_id, locked) VALUES (?, 0) "
                "ON CONFLICT(chat_id) DO UPDATE SET locked=0",
                (chat_id,),
            )

    def in_sleep_window(self, sleep_window="", *, now=None):
        now = self.clock() if now is None else now
        hour = (int(now) + 8 * 3600) % 86400 // 3600
        if not sleep_window:
            return False
        try:
            start, end = (int(part) for part in sleep_window.split("-", 1))
        except (TypeError, ValueError):
            return False
        return (
            0 <= start <= 24
            and 0 <= end <= 24
            and start != end
            and (start <= hour < end if start < end else hour >= start or hour < end)
        )

    def social_gate(self, target, *, sleep_window="", now=None):
        if not CHAT_TARGET.fullmatch(target):
            return False, "invalid_target"
        chat_id = target.split(":", 1)[1]
        if self.in_sleep_window(sleep_window, now=now):
            return False, "sleep_window"
        with self.store.connect() as db:
            state = db.execute(
                "SELECT locked FROM social_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
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

    def event_exists(self, event_key, event_digest):
        with self.store.connect() as db:
            old = db.execute(
                "SELECT digest, intent_id FROM incoming_events WHERE event_key=?",
                (event_key,),
            ).fetchone()
        if not old:
            return None
        if old["digest"] != event_digest:
            raise ValueError("同一事件编号已绑定另一份内容")
        return self.get(old["intent_id"])

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

    def _proactive_counts(self, db, start: float, end: float) -> dict[str, int]:
        rows = db.execute(
            "SELECT target, payload FROM intents WHERE kind='proactive' "
            "AND source IN ('afterglow','serendipity','longing','later') "
            "AND created>=? AND created<? AND status NOT IN ('cancelled','failed')",
            (start, end),
        ).fetchall()
        counts = {"group": 0, "private": 0}
        for row in rows:
            raw_payload = row["payload"]
            try:
                parsed_payload = json.loads(raw_payload) if raw_payload else None
            except (TypeError, ValueError):
                parsed_payload = None
            arena = self._target_arena(
                db,
                row["target"],
                parsed_payload if isinstance(parsed_payload, dict) else None,
            )
            counts[arena] = counts.get(arena, 0) + 1
        return counts

    def proactive_quota_available(self, target: str = "", *, now=None) -> bool:
        limit = int(self.daily_proactive_limit)
        if limit <= 0:
            return False
        now = self.clock() if now is None else now
        start, end = self._day_bounds(now)
        with self.store.connect() as db:
            counts = self._proactive_counts(db, start, end)
            if target:
                arena = self._target_arena(db, target)
                return counts[arena] < limit
            return counts["group"] < limit or counts["private"] < limit

    def longing_ready(self, chat_id, *, now=None, cooldown=7 * 86400) -> bool:
        now = self.clock() if now is None else now
        with self.store.connect() as db:
            state = db.execute(
                "SELECT locked, last_proactive FROM social_state WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            if state and (
                state["locked"]
                or (state["last_proactive"] and now - state["last_proactive"] < cooldown)
            ):
                return False
            active_proactive = db.execute(
                "SELECT 1 FROM intents WHERE target=? AND kind='proactive' "
                "AND status IN ('pending','claimed') LIMIT 1",
                ("chat:" + chat_id,),
            ).fetchone()
            if active_proactive is not None:
                return False
            recent_longing = db.execute(
                "SELECT 1 FROM intents WHERE target=? AND source='longing' "
                "AND status!='cancelled' "
                "AND ((handled IS NOT NULL AND ? - handled < ?) OR ? - created < ?) "
                "LIMIT 1",
                ("chat:" + chat_id, now, cooldown, now, cooldown),
            ).fetchone()
        return recent_longing is None

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

    def mark_proactive(self, target):
        chat_id = target.split(":", 1)[1]
        with self.store.connect() as db, db:
            db.execute(
                "INSERT INTO social_state (chat_id, locked, last_proactive) "
                "VALUES (?, 1, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "locked=1, last_proactive=excluded.last_proactive",
                (chat_id, self.clock()),
            )

    @staticmethod
    def _day_bounds(now):
        start = now - (int(now) + 8 * 3600) % 86400
        return start, start + 86400
