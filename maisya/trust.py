"""可信消息索引与纯权限判断。"""

import base64
import hashlib
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .pathsafe import safe_chmod

PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<kind>(?:[A-Z0-9]+ )*PRIVATE KEY)-----[\s\S]+?-----END (?P=kind)-----"
)
VAULT_COMMAND = re.compile(r"^/vault(?:\s|$)")
MENTION_ALL_TEXT = re.compile(
    r"@全体成员|@所有人|\[CQ:at,qq=all(?:,[^\]]*)?\]", re.IGNORECASE
)
MENTION_ALL_TARGETS = frozenset({"all", "全体成员", "所有人"})
STREAM_SPEC_RE = re.compile(
    r"^([a-z][a-z0-9_-]{0,31}):(group|private):([^\s:][^\s]{0,199})$"
)


def parse_stream_spec(raw: str | None) -> tuple[str, str, str] | None:
    text = str(raw or "").strip()
    if text.lower().startswith("chat:"):
        text = text[5:].strip()
    if ":" not in text:
        return None
    parts = text.split(":", 2)
    if len(parts) != 3:
        return None
    plat, kind, tid = parts[0].strip().lower(), parts[1].strip().lower(), parts[2].strip()
    candidate = f"{plat}:{kind}:{tid}"
    match = STREAM_SPEC_RE.fullmatch(candidate)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def format_stream_spec(
    platform: str | None,
    chat_type: str = "",
    raw_id: str = "",
    *,
    group_id: str = "",
    user_id: str = "",
) -> str:
    plat = str(platform or "").strip().lower()
    gid = str(group_id or "").strip()
    uid = str(user_id or "").strip()
    ctype = str(chat_type or "").strip().lower()
    if not ctype:
        ctype = "group" if gid else ("private" if uid else "")
    if ctype in {"user", "dm", "c2c"}:
        ctype = "private"
    tid = str(raw_id or (gid if ctype == "group" else uid) or gid or uid).strip()
    if not plat or ctype not in {"group", "private"} or not tid:
        return ""
    candidate = f"{plat}:{ctype}:{tid}"
    return candidate if STREAM_SPEC_RE.fullmatch(candidate) else ""


def alias(message_id: str) -> str:
    message_id = message_id.strip()
    if len(message_id) <= 12:
        return message_id
    return (
        "m"
        + base64.b32encode(hashlib.sha1(message_id.encode()).digest())
        .decode()
        .lower()[:6]
    )


def session_rank_key(entry: dict, *, preferred_account_id: str = "") -> tuple:
    """为同一逻辑会话的多条历史/多账号记录计算 canonical 排序键（越大越优先）。"""
    aid = str(entry.get("account_id") or "").strip()
    pref = str(preferred_account_id or "").strip()
    try:
        last_h = float(entry.get("last_human_at") or 0.0)
    except (TypeError, ValueError):
        last_h = 0.0
    try:
        h_cnt = int(entry.get("human_count") or 0)
    except (TypeError, ValueError):
        h_cnt = 0
    try:
        upd = float(entry.get("updated") or 0.0)
    except (TypeError, ValueError):
        upd = 0.0
    return (
        1 if (pref and aid and aid == pref) else 0,
        1 if last_h > 0 else 0,
        last_h,
        h_cnt,
        1 if aid else 0,
        upd,
    )


@dataclass(frozen=True)
class Message:
    message_id: str
    session_id: str
    platform: str
    user_id: str
    is_plugin_relayed: bool
    is_local_operator: bool = False
    group_id: str = ""
    account_id: str = ""
    scope: str = ""

    @classmethod
    def from_event(cls, event: dict) -> "Message":
        info = event.get("message_info") or {}
        config = info.get("additional_config") or {}
        group_info = info.get("group_info")
        platform = str(event.get("platform") or "").strip().lower()
        return cls(
            str(event.get("message_id") or "").strip(),
            str(event.get("session_id") or "").strip(),
            platform,
            str((info.get("user_info") or {}).get("user_id") or "").strip(),
            bool(config.get("relayed_by_plugin")),
            platform == "bot_console" and config.get("is_local_operator") is True,
            str((group_info or {}).get("group_id") or "").strip()
            if isinstance(group_info, dict)
            else "",
            str(info.get("account_id") or config.get("account_id") or "").strip(),
            str(info.get("scope") or config.get("scope") or "").strip(),
        )


def _strip_text_mentions(text: str) -> str:
    if not MENTION_ALL_TEXT.search(text):
        return text
    lines = []
    for line in text.splitlines(keepends=True):
        if MENTION_ALL_TEXT.search(line):
            suffix = "\n" if line.endswith("\n") else ""
            body = line[:-1] if suffix else line
            body = re.sub(r"[ \t]{2,}", " ", MENTION_ALL_TEXT.sub(" ", body)).strip(
                " \t"
            )
            lines.append(body + suffix)
        else:
            lines.append(line)
    return "".join(lines)


def _is_mention_all_segment(item) -> bool:
    if not isinstance(item, dict) or item.get("type") != "at":
        return False
    data = item.get("data")
    if isinstance(data, dict):
        target = str(
            data.get("target_user_id")
            or data.get("user_id")
            or data.get("qq")
            or data.get("id")
            or ""
        )
    else:
        target = str(data or "")
    return target.strip().lower() in MENTION_ALL_TARGETS


def strip_mention_all(value):
    if isinstance(value, str):
        return _strip_text_mentions(value)
    if isinstance(value, list):
        cleaned = []
        changed = False
        for item in value:
            if _is_mention_all_segment(item):
                changed = True
                continue
            updated = strip_mention_all(item)
            if updated is not item and updated != item:
                changed = True
            cleaned.append(updated)
        return cleaned if changed else value
    if isinstance(value, dict):
        updated = dict(value)
        changed = False
        for key in (
            "processed_plain_text",
            "display_message",
            "text",
            "content",
            "data",
            "segments",
            "raw_message",
            "message_sequence",
        ):
            if key in updated:
                nxt = strip_mention_all(updated[key])
                if nxt != updated[key]:
                    updated[key] = nxt
                    changed = True
        return updated if changed else value
    return value


class MessageStore:
    def __init__(
        self, path: Path, *, max_messages: int = 10000, max_age: float = 86400, clock=None
    ):
        self.path = path
        self.max_messages = max_messages
        self.max_age = max_age
        self.clock = clock
        self._routes_lock = threading.RLock()
        self._routes_sig: tuple[int, int, int, int] | None = None
        self._routes_by_sid: dict[str, dict] = {}
        self._sids_by_alias: dict[str, list[dict]] = {}
        if path.is_symlink():
            raise ValueError("消息索引不能是符号链接")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS trusted_sources ("
                "message_id TEXT, alias TEXT, session_id TEXT, platform TEXT, "
                "user_id TEXT, relayed INTEGER, local_operator INTEGER, received REAL, "
                "PRIMARY KEY (message_id, session_id, platform))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS trusted_alias ON trusted_sources(alias, session_id, platform)"
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
            initial_rows = db.execute(
                "SELECT session_id, platform, chat_type, group_id, user_id, account_id, scope, "
                "human_count, last_human_at, updated FROM session_routes"
            ).fetchall()
        safe_chmod(path, 0o600)
        with self._routes_lock:
            self._set_routes_from_rows_locked(initial_rows)
            self._routes_sig = self._db_sig()

    def _db_sig(self) -> tuple[int, int, int, int]:
        def _stat(target: Path | str) -> tuple[int, int]:
            try:
                st = os.stat(target)
                return (st.st_mtime_ns, st.st_size)
            except OSError:
                return (0, 0)

        return (*_stat(self.path), *_stat(f"{self.path}-wal"))

    @staticmethod
    def _normalize_route_row(row) -> dict:
        sid, plat, ctype, gid, uid, aid, sc, hc, lha, upd = row
        return {
            "session_id": str(sid or "").strip(),
            "platform": str(plat or "").strip().lower(),
            "chat_type": str(ctype or "").strip().lower(),
            "group_id": str(gid or "").strip(),
            "user_id": str(uid or "").strip(),
            "account_id": str(aid or "").strip(),
            "scope": str(sc or "").strip(),
            "human_count": int(hc or 0),
            "last_human_at": float(lha or 0.0),
            "updated": float(upd or 0.0),
        }

    def _set_routes_from_rows_locked(self, rows) -> None:
        by_sid: dict[str, dict] = {}
        for row in rows:
            entry = self._normalize_route_row(row)
            if entry["session_id"]:
                by_sid[entry["session_id"]] = entry
        self._routes_by_sid = by_sid
        self._rebuild_route_indexes_locked()

    def _rebuild_route_indexes_locked(self) -> None:
        by_alias: dict[str, list[dict]] = {}
        for entry in self._routes_by_sid.values():
            sid = entry["session_id"]
            plat_l = entry["platform"]
            gid = entry["group_id"]
            uid = entry["user_id"]
            candidates = {sid}
            if gid:
                candidates.update({gid, f"group:{gid}"})
                if plat_l:
                    candidates.update({f"{plat_l}:{gid}", f"{plat_l}:group:{gid}"})
            elif uid:
                candidates.update({uid, f"private:{uid}"})
                if plat_l:
                    candidates.update({f"{plat_l}:{uid}", f"{plat_l}:private:{uid}"})
            for tok in candidates:
                by_alias.setdefault(tok, []).append(entry)
        for hits in by_alias.values():
            if len(hits) > 1:
                hits.sort(key=session_rank_key, reverse=True)
        self._sids_by_alias = by_alias

    def _ensure_routes_loaded(self) -> None:
        with self._routes_lock:
            sig = self._db_sig()
            if self._routes_sig is not None and sig == self._routes_sig:
                return
            sig_before = sig
            with closing(sqlite3.connect(self.path)) as db:
                rows = db.execute(
                    "SELECT session_id, platform, chat_type, group_id, user_id, account_id, scope, "
                    "human_count, last_human_at, updated FROM session_routes"
                ).fetchall()
            self._set_routes_from_rows_locked(rows)
            self._routes_sig = sig_before

    def record(self, message: Message) -> None:
        if not all(
            (message.message_id, message.session_id, message.platform, message.user_id)
        ):
            raise ValueError("incomplete message identity")
        now = time.time() if self.clock is None else self.clock()
        with self._routes_lock:
            sig_before = self._db_sig()
            chat_type = "group" if message.group_id else "private"
            route_user = "" if message.group_id else message.user_id
            inc = 0
            with closing(sqlite3.connect(self.path)) as db, db:
                seen_human = (
                    db.execute(
                        "SELECT relayed FROM trusted_sources "
                        "WHERE message_id=? AND session_id=? AND platform=?",
                        (message.message_id, message.session_id, message.platform),
                    ).fetchone()
                    if not message.is_plugin_relayed
                    else None
                )
                db.execute(
                    "INSERT INTO trusted_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(message_id, session_id, platform) DO UPDATE SET "
                    "relayed = CASE WHEN trusted_sources.user_id != excluded.user_id "
                    "OR trusted_sources.local_operator != excluded.local_operator "
                    "THEN 1 ELSE MAX(trusted_sources.relayed, excluded.relayed) END",
                    (
                        message.message_id,
                        alias(message.message_id),
                        message.session_id,
                        message.platform,
                        message.user_id,
                        message.is_plugin_relayed,
                        message.is_local_operator,
                        now,
                    ),
                )
                if not message.is_plugin_relayed:
                    inc = 0 if (seen_human is not None and seen_human[0] == 0) else 1
                    db.execute(
                        "INSERT INTO session_routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(session_id) DO UPDATE SET "
                        "platform=excluded.platform, chat_type=excluded.chat_type, "
                        "group_id=excluded.group_id, user_id=excluded.user_id, "
                        "account_id=CASE WHEN excluded.account_id!='' THEN excluded.account_id ELSE session_routes.account_id END, "
                        "scope=CASE WHEN excluded.scope!='' THEN excluded.scope ELSE session_routes.scope END, "
                        "updated=excluded.updated, "
                        "human_count=session_routes.human_count + excluded.human_count, "
                        "last_human_at=excluded.last_human_at",
                        (
                            message.session_id,
                            message.platform,
                            chat_type,
                            message.group_id,
                            route_user,
                            message.account_id,
                            message.scope,
                            now,
                            inc,
                            now,
                        ),
                    )
                db.execute(
                    "DELETE FROM trusted_sources WHERE received < ?",
                    (now - self.max_age,),
                )
                db.execute(
                    "DELETE FROM trusted_sources WHERE rowid IN "
                    "(SELECT rowid FROM trusted_sources ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
                    (self.max_messages,),
                )
            if self._routes_sig is not None and self._routes_sig == sig_before:
                if not message.is_plugin_relayed:
                    cur = self._routes_by_sid.get(message.session_id) or {}
                    self._routes_by_sid[message.session_id] = {
                        "session_id": message.session_id,
                        "platform": str(message.platform or "").strip().lower(),
                        "chat_type": chat_type,
                        "group_id": str(message.group_id or "").strip(),
                        "user_id": str(route_user or "").strip(),
                        "account_id": str(message.account_id or "").strip() or cur.get("account_id", ""),
                        "scope": str(message.scope or "").strip() or cur.get("scope", ""),
                        "human_count": int(cur.get("human_count") or 0) + inc,
                        "last_human_at": float(now),
                        "updated": float(now),
                    }
                    self._rebuild_route_indexes_locked()
                self._routes_sig = self._db_sig()
            else:
                self._routes_sig = None

    def lookup(
        self, message_id: str, session_id: str, platform: str, *, max_age=None
    ) -> Message | None:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT message_id, session_id, platform, user_id, relayed, local_operator "
                "FROM trusted_sources WHERE session_id=? AND platform=? "
                "AND (message_id=? OR alias=?) AND received >= ?",
                (
                    session_id,
                    platform,
                    message_id,
                    message_id,
                    (time.time() if self.clock is None else self.clock())
                    - (self.max_age if max_age is None else min(max_age, self.max_age)),
                ),
            ).fetchall()
        if len(rows) != 1:
            return None
        raw, session, platform, user, relayed, local = rows[0]
        return Message(raw, session, platform, user, bool(relayed), bool(local))

    def platform_for(self, session_id: str) -> str:
        route = self.route_for(session_id)
        if route and route.get("platform"):
            return str(route["platform"])
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT DISTINCT platform FROM trusted_sources WHERE session_id=?",
                (session_id,),
            ).fetchall()
            if len(rows) == 1:
                return rows[0][0]
            row = db.execute(
                "SELECT platform FROM session_routes WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return row[0] if row else ""

    def route_for(self, session_id: str, *, include_stats: bool = False) -> dict | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        with self._routes_lock:
            self._ensure_routes_loaded()
            row = self._routes_by_sid.get(sid)
            if not row:
                return None
            route = {
                "platform": row["platform"],
                "chat_type": row["chat_type"],
                "group_id": row["group_id"],
                "user_id": row["user_id"],
                "account_id": row["account_id"],
                "scope": row["scope"],
            }
            if include_stats:
                route["human_count"] = int(row.get("human_count") or 0)
                route["last_human_at"] = float(row.get("last_human_at") or 0.0)
                route["updated"] = float(row.get("updated") or 0.0)
            return route

    def upsert_routes(
        self,
        entries: dict[str, dict] | Iterable[dict],
        *,
        timestamp: float | None = None,
    ) -> int:
        if not entries:
            return 0
        items = entries.items() if isinstance(entries, dict) else ((None, item) for item in entries)
        default_now = float(
            timestamp if timestamp is not None else (time.time() if self.clock is None else self.clock())
        )
        with self._routes_lock:
            self._ensure_routes_loaded()
            sig_before = self._routes_sig
            pending: list[tuple[dict, tuple]] = []
            staged: dict[str, dict] = {}
            for key_sid, raw in items:
                if not isinstance(raw, dict):
                    continue
                sid = str(raw.get("session_id") or key_sid or "").strip()
                plat = str(raw.get("platform") or "").strip().lower()
                ctype = str(raw.get("chat_type") or "").strip().lower()
                gid = str(raw.get("group_id") or "").strip()
                uid = "" if gid else str(raw.get("user_id") or "").strip()
                if not sid or not plat or ctype not in {"group", "private"}:
                    continue
                aid = str(raw.get("account_id") or "").strip()
                sc = str(raw.get("scope") or "").strip()
                raw_ts = raw.get("timestamp")
                entry_ts = (
                    float(raw_ts)
                    if raw_ts is not None
                    else (float(timestamp) if timestamp is not None else None)
                )
                now = entry_ts if entry_ts is not None else default_now

                cur = staged.get(sid) or self._routes_by_sid.get(sid)
                eff_gid = gid or (cur["group_id"] if cur else "")
                eff_uid = uid or (cur["user_id"] if cur else "")
                eff_aid = aid or (cur["account_id"] if cur else "")
                eff_sc = sc or (cur["scope"] if cur else "")
                if cur is not None:
                    same_meta = (
                        plat == cur["platform"]
                        and ctype == cur["chat_type"]
                        and eff_gid == cur["group_id"]
                        and eff_uid == cur["user_id"]
                        and eff_aid == cur["account_id"]
                        and eff_sc == cur["scope"]
                    )
                    same_ts = entry_ts is None or entry_ts == cur["updated"]
                    if same_meta and same_ts:
                        continue
                new_entry = {
                    "session_id": sid,
                    "platform": plat,
                    "chat_type": ctype,
                    "group_id": eff_gid,
                    "user_id": eff_uid,
                    "account_id": eff_aid,
                    "scope": eff_sc,
                    "human_count": int(cur.get("human_count") or 0) if cur else 0,
                    "last_human_at": float(cur.get("last_human_at") or 0.0) if cur else 0.0,
                    "updated": now,
                }
                staged[sid] = new_entry
                pending.append((new_entry, (sid, plat, ctype, gid, uid, aid, sc, now)))

            if not pending:
                return 0
            pre_write_sig = self._db_sig()
            with closing(sqlite3.connect(self.path)) as db, db:
                db.executemany(
                    "INSERT INTO session_routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0) "
                    "ON CONFLICT(session_id) DO UPDATE SET "
                    "platform=excluded.platform, chat_type=excluded.chat_type, "
                    "group_id=CASE WHEN excluded.group_id!='' THEN excluded.group_id ELSE session_routes.group_id END, "
                    "user_id=CASE WHEN excluded.user_id!='' THEN excluded.user_id ELSE session_routes.user_id END, "
                    "account_id=CASE WHEN excluded.account_id!='' THEN excluded.account_id ELSE session_routes.account_id END, "
                    "scope=CASE WHEN excluded.scope!='' THEN excluded.scope ELSE session_routes.scope END, "
                    "updated=excluded.updated",
                    [params for _, params in pending],
                )
            if sig_before is not None and sig_before == pre_write_sig:
                self._routes_by_sid.update(staged)
                self._rebuild_route_indexes_locked()
                self._routes_sig = self._db_sig()
            else:
                self._routes_sig = None
            return len(pending)

    def upsert_route(
        self,
        session_id: str,
        *,
        platform: str,
        chat_type: str,
        group_id: str = "",
        user_id: str = "",
        account_id: str = "",
        scope: str = "",
        timestamp: float | None = None,
    ) -> None:
        self.upsert_routes(
            [
                {
                    "session_id": session_id,
                    "platform": platform,
                    "chat_type": chat_type,
                    "group_id": group_id,
                    "user_id": user_id,
                    "account_id": account_id,
                    "scope": scope,
                }
            ],
            timestamp=timestamp,
        )

    def aliases_for(
        self,
        session_id: str,
        platform: str = "",
        *,
        group_id: str = "",
        user_id: str = "",
    ) -> set[str]:
        aliases: set[str] = set()
        sid = str(session_id or "").strip()
        if sid:
            aliases.add(sid)
        route = self.route_for(sid) if sid else None
        plat = (
            str(platform or "").strip()
            or str((route or {}).get("platform") or "").strip()
        ).lower()
        gid = str(group_id or "").strip() or str((route or {}).get("group_id") or "").strip()
        uid = "" if gid else (str(user_id or "").strip() or str((route or {}).get("user_id") or "").strip())
        if gid:
            aliases.update({gid, f"group:{gid}"})
            if plat:
                aliases.update({f"{plat}:{gid}", f"{plat}:group:{gid}"})
        elif uid:
            aliases.add(f"private:{uid}")
            if plat:
                aliases.update({f"{plat}:{uid}", f"{plat}:private:{uid}"})
        return aliases

    def find_sessions(self, token: str) -> list[dict]:
        key = str(token or "").strip()
        if not key:
            return []
        parsed = parse_stream_spec(key)
        if parsed is not None:
            norm = f"{parsed[0]}:{parsed[1]}:{parsed[2]}"
        elif ":" in key:
            head, tail = key.split(":", 1)
            norm = f"{head.strip().lower()}:{tail.strip()}"
        else:
            norm = key
        with self._routes_lock:
            self._ensure_routes_loaded()
            hits_a = self._sids_by_alias.get(key, ())
            hits_b = self._sids_by_alias.get(norm, ()) if norm != key else ()
            if not hits_a and not hits_b:
                return []
            if not hits_b:
                return [dict(item) for item in hits_a[:512]]
            if not hits_a:
                return [dict(item) for item in hits_b[:512]]
            seen: set[str] = set()
            matched: list[dict] = []
            for item in (*hits_a, *hits_b):
                sid = item["session_id"]
                if sid not in seen:
                    seen.add(sid)
                    matched.append(dict(item))
            matched.sort(key=session_rank_key, reverse=True)
            return matched[:512]

    def activity_for(self, session_id: str) -> dict:
        sid = str(session_id or "").strip()
        with self._routes_lock:
            self._ensure_routes_loaded()
            row = self._routes_by_sid.get(sid)
            if not row:
                return {"chat_type": "", "human_count": 0, "last_human_at": 0.0}
            return {
                "chat_type": row["chat_type"],
                "human_count": int(row["human_count"]),
                "last_human_at": float(row["last_human_at"]),
            }

    def list_routes(self, limit: int = 256) -> list[dict]:
        cap = max(1, min(int(limit), 1000))
        with self._routes_lock:
            self._ensure_routes_loaded()
            ordered = sorted(
                self._routes_by_sid.values(),
                key=lambda item: float(item.get("updated") or 0.0),
                reverse=True,
            )
            return [dict(item) for item in ordered[:cap]]


def normalize_admins(permissions) -> set[str]:
    if not isinstance(permissions, (list, tuple, set)):
        return set()
    result = set()
    for value in permissions:
        if not isinstance(value, str) or ":" not in value:
            continue
        parsed = parse_stream_spec(value)
        if parsed is not None:
            plat, kind, tid = parsed
            if kind == "private" and tid:
                result.add(f"{plat}:{tid}")
            continue
        platform, user = value.split(":", 1)
        plat = platform.strip().lower()
        uid = user.strip()
        if uid.lower().startswith("private:"):
            uid = uid[8:].strip()
        if plat and uid:
            result.add(f"{plat}:{uid}")
    return result


def allowed(
    store: MessageStore,
    source_msg_id: str,
    session_id: str,
    platform: str,
    admins: set[str],
) -> bool:
    msg = store.lookup(source_msg_id, session_id, platform, max_age=300)
    return bool(
        msg
        and not msg.is_plugin_relayed
        and (msg.is_local_operator or f"{msg.platform.lower()}:{msg.user_id}" in admins)
    )


def visible_tools(tools: list[str], restricted: set[str], is_admin: bool) -> list[str]:
    return [tool for tool in tools if is_admin or tool not in restricted]


def filter_tools(
    tools: list[dict], *, is_admin: bool, restricted=frozenset({"maisya_shape"})
) -> list[dict]:
    return [
        tool
        for tool in tools
        if is_admin or (tool.get("function") or tool).get("name") not in restricted
    ]


def is_vault_input(text: str, *, private: bool) -> bool:
    return private and bool(
        VAULT_COMMAND.match(text.strip()) or PRIVATE_KEY.search(text)
    )
