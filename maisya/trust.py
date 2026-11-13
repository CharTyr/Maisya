"""可信消息索引与纯权限判断。"""

import base64
import hashlib
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

    @classmethod
    def from_event(cls, event: dict) -> "Message":
        info = event.get("message_info") or {}
        config = info.get("additional_config") or {}
        platform = str(event.get("platform") or "").strip().lower()
        return cls(
            str(event.get("message_id") or "").strip(),
            str(event.get("session_id") or "").strip(),
            platform,
            str((info.get("user_info") or {}).get("user_id") or "").strip(),
            bool(config.get("relayed_by_plugin")),
            platform == "bot_console" and config.get("is_local_operator") is True,
        )


class MessageStore:
    def __init__(
        self, path: Path, *, max_messages: int = 10000, max_age: float = 86400
    ):
        self.path = path
        self.max_messages = max_messages
        self.max_age = max_age
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
        path.chmod(0o600)

    def record(self, message: Message) -> None:
        if not all(
            (message.message_id, message.session_id, message.platform, message.user_id)
        ):
            raise ValueError("incomplete message identity")
        with closing(sqlite3.connect(self.path)) as db, db:
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
                    time.time(),
                ),
            )
            db.execute(
                "DELETE FROM trusted_sources WHERE received < ?",
                (time.time() - self.max_age,),
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
                    time.time()
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
        return rows[0][0] if len(rows) == 1 else ""


def normalize_admins(permissions) -> set[str]:
    if not isinstance(permissions, (list, tuple, set)):
        return set()
    result = set()
    for value in permissions:
        if isinstance(value, str) and ":" in value:
            platform, user = value.split(":", 1)
            if platform.strip() and user.strip():
                result.add(f"{platform.strip().lower()}:{user.strip()}")
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
        and (msg.is_local_operator or f"{msg.platform}:{msg.user_id}" in admins)
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
