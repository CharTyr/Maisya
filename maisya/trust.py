"""可信消息索引与纯权限判断。"""

import base64
import hashlib
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

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
        if os.name != "nt":
            path.chmod(0o600)

    def record(self, message: Message) -> None:
        if not all(
            (message.message_id, message.session_id, message.platform, message.user_id)
        ):
            raise ValueError("incomplete message identity")
        now = time.time() if self.clock is None else self.clock()
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
                chat_type = "group" if message.group_id else "private"
                route_user = "" if message.group_id else message.user_id
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
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT DISTINCT platform FROM trusted_sources WHERE session_id=?",
                (session_id,),
            ).fetchall()
            if len(rows) == 1:
                return rows[0][0]
            route = db.execute(
                "SELECT platform FROM session_routes WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return route[0] if route else ""

    def route_for(self, session_id: str) -> dict | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT platform, chat_type, group_id, user_id, account_id, scope "
                "FROM session_routes WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "platform": row[0],
            "chat_type": row[1],
            "group_id": row[2],
            "user_id": row[3],
            "account_id": row[4],
            "scope": row[5],
        }

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
    ) -> None:
        sid = str(session_id or "").strip()
        plat = str(platform or "").strip().lower()
        ctype = str(chat_type or "").strip().lower()
        gid = str(group_id or "").strip()
        uid = "" if gid else str(user_id or "").strip()
        if not sid or not plat or ctype not in {"group", "private"}:
            return
        now = time.time() if self.clock is None else self.clock()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO session_routes VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "platform=excluded.platform, chat_type=excluded.chat_type, "
                "group_id=CASE WHEN excluded.group_id!='' THEN excluded.group_id ELSE session_routes.group_id END, "
                "user_id=CASE WHEN excluded.user_id!='' THEN excluded.user_id ELSE session_routes.user_id END, "
                "account_id=CASE WHEN excluded.account_id!='' THEN excluded.account_id ELSE session_routes.account_id END, "
                "scope=CASE WHEN excluded.scope!='' THEN excluded.scope ELSE session_routes.scope END, "
                "updated=excluded.updated",
                (
                    sid,
                    plat,
                    ctype,
                    gid,
                    uid,
                    str(account_id or "").strip(),
                    str(scope or "").strip(),
                    now,
                ),
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
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT session_id, platform, chat_type, group_id, user_id, account_id, scope "
                "FROM session_routes ORDER BY updated DESC LIMIT 512"
            ).fetchall()
        matched = []
        for sid, plat, ctype, gid, uid, aid, sc in rows:
            plat_l = str(plat or "").strip().lower()
            candidates = {sid}
            if gid:
                candidates.update({gid, f"group:{gid}"})
                if plat_l:
                    candidates.update({f"{plat_l}:{gid}", f"{plat_l}:group:{gid}"})
            elif uid:
                candidates.update({uid, f"private:{uid}"})
                if plat_l:
                    candidates.update({f"{plat_l}:{uid}", f"{plat_l}:private:{uid}"})
            if key in candidates or norm in candidates:
                matched.append(
                    {
                        "session_id": sid,
                        "platform": plat_l,
                        "chat_type": ctype,
                        "group_id": gid,
                        "user_id": uid,
                        "account_id": aid,
                        "scope": sc,
                    }
                )
        return matched

    def activity_for(self, session_id: str) -> dict:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT chat_type, human_count, last_human_at "
                "FROM session_routes WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if not row:
            return {"chat_type": "", "human_count": 0, "last_human_at": 0.0}
        return {
            "chat_type": row[0],
            "human_count": int(row[1]),
            "last_human_at": float(row[2]),
        }

    def list_routes(self, limit: int = 256) -> list[dict]:
        cap = max(1, min(int(limit), 1000))
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                "SELECT session_id, platform, chat_type, group_id, user_id, account_id, scope, "
                "human_count, last_human_at, updated "
                "FROM session_routes ORDER BY updated DESC LIMIT ?",
                (cap,),
            ).fetchall()
        return [
            {
                "session_id": sid,
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
            for sid, plat, ctype, gid, uid, aid, sc, hc, lha, upd in rows
        ]


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
