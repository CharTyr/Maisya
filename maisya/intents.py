"""意图存储与社交分寸状态：提醒、主动搭话与自身活动统一经 Pulse 过闸。"""

import json
import re
import time
import uuid

KINDS = {"return", "proactive", "silent"}
SOURCES = {"thread", "afterglow", "serendipity", "longing", "later", "webhook"}
CHAT_TARGET = re.compile(r"^chat:[^\s:]{1,256}$")


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Intents:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock
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
                "account_id TEXT NOT NULL, scope TEXT NOT NULL, updated REAL NOT NULL)"
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
        if kind not in KINDS or source not in SOURCES or not CHAT_TARGET.match(target):
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
                start, end = self._day_bounds(now)
                used = db.execute(
                    "SELECT COUNT(*) AS count FROM intents WHERE kind='proactive' "
                    "AND source IN ('afterglow','serendipity','longing','later') "
                    "AND created>=? AND created<? AND status!='cancelled'",
                    (start, end),
                ).fetchone()["count"]
                if used >= 1:
                    raise ValueError("普通惦记每天最多一条，今天已用完")
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

    def finish(self, intent_id, status, wake_status=""):
        if status not in {"done", "deferred", "failed"}:
            raise ValueError("意图处理结果无效")
        with self.store.connect() as db, db:
            if status == "deferred":
                db.execute(
                    "UPDATE intents SET status='pending', due_at=? "
                    "WHERE intent_id=? AND status='claimed'",
                    (self.clock() + 900, intent_id),
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

    def note_received(self, chat_id):
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
        if not CHAT_TARGET.match(target):
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
