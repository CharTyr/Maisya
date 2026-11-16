"""统一数据归属、可见范围与版本绑定发布原语（B2）。"""

from collections.abc import Iterable
from contextlib import closing
import contextvars
import sqlite3
import time

STEP_METHODS_CONTEXT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "maisya_step_methods_ctx", default=None
)

ASSET_SCOPES_DDL = (
    "CREATE TABLE IF NOT EXISTS asset_scopes ("
    "asset_key TEXT PRIMARY KEY, "
    "visibility_scope TEXT NOT NULL DEFAULT 'public', "
    "source_chat_id TEXT NOT NULL DEFAULT '', "
    "provenance TEXT NOT NULL DEFAULT '', "
    "updated_at REAL NOT NULL"
    ")"
)

PUBLICATION_GRANTS_DDL = (
    "CREATE TABLE IF NOT EXISTS publication_grants ("
    "thread_id TEXT NOT NULL, "
    "revision INTEGER NOT NULL, "
    "target_chat TEXT NOT NULL, "
    "actor TEXT NOT NULL DEFAULT '', "
    "created_at REAL NOT NULL, "
    "PRIMARY KEY (thread_id, revision, target_chat)"
    ")"
)


def ensure_scope_tables(db: sqlite3.Connection) -> None:
    db.execute(ASSET_SCOPES_DDL)
    db.execute(PUBLICATION_GRANTS_DDL)


def initial_thread_scope(
    *,
    owner: str = "chat",
    chat_id: str = "",
    arena: str = "",
) -> str:
    if owner == "self" or chat_id == "self":
        return "public"
    cid = str(chat_id or "").strip()
    if not cid:
        return "admin_only"
    kind = arena or ("private" if ":private:" in cid else "group")
    if kind not in {"private", "group"}:
        kind = "private" if ":private:" in cid else "group"
    return f"{kind}:{cid}"


def normalize_scope(scope: str | None) -> str:
    raw = str(scope or "").strip()
    if not raw or raw == "public":
        return "public"
    if raw == "admin_only":
        return "admin_only"
    if raw.startswith(("private:", "group:")):
        return raw
    return "admin_only"


def merge_scopes(scopes: Iterable[str | None]) -> str:
    """多来源作用域收敛（格交集）：跨私聊/群聊混源直接收敛为 admin_only。"""
    distinct: set[str] = set()
    for raw in scopes:
        norm = normalize_scope(raw)
        if norm == "admin_only":
            return "admin_only"
        if norm != "public":
            distinct.add(norm)
    if not distinct:
        return "public"
    if len(distinct) == 1:
        return next(iter(distinct))
    return "admin_only"


def is_tainted_scope(scope: str | None, *, current_chat_id: str = "", aliases: Iterable[str] = ()) -> bool:
    """判断作用域相对当前普通会话是否带有更高敏感或跨会话污染。"""
    norm = normalize_scope(scope)
    if norm == "public":
        return False
    if norm == "admin_only" or norm.startswith("private:"):
        if norm == "admin_only":
            return True
        owner_chat = norm.split(":", 1)[1]
        allowed = {str(current_chat_id or ""), *(str(a) for a in aliases if a)}
        return owner_chat not in allowed
    owner_chat = norm.split(":", 1)[1]
    allowed = {str(current_chat_id or ""), *(str(a) for a in aliases if a)}
    return bool(current_chat_id) and owner_chat not in allowed


def can_read_scope(
    scope: str | None,
    *,
    viewer_chat: str = "",
    is_admin: bool = False,
    aliases: Iterable[str] = (),
) -> bool:
    norm = normalize_scope(scope)
    if norm == "public":
        return True
    if norm == "admin_only":
        return bool(is_admin)
    if is_admin:
        return True
    if not viewer_chat:
        return False
    owner_chat = norm.split(":", 1)[1]
    allowed = {str(viewer_chat), *(str(a) for a in aliases if a)}
    return owner_chat in allowed


def set_asset_scope(
    db: sqlite3.Connection,
    asset_key: str,
    visibility_scope: str,
    *,
    source_chat_id: str = "",
    provenance: str = "",
    now: float | None = None,
) -> None:
    ensure_scope_tables(db)
    db.execute(
        "INSERT INTO asset_scopes (asset_key, visibility_scope, source_chat_id, provenance, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(asset_key) DO UPDATE SET "
        "visibility_scope=excluded.visibility_scope, source_chat_id=excluded.source_chat_id, "
        "provenance=excluded.provenance, updated_at=excluded.updated_at",
        (
            str(asset_key),
            normalize_scope(visibility_scope),
            str(source_chat_id or ""),
            str(provenance or ""),
            float(time.time() if now is None else now),
        ),
    )


def get_asset_scope(db: sqlite3.Connection, asset_key: str) -> dict | None:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_scopes'"
    ).fetchone()
    if not exists:
        return None
    row = db.execute(
        "SELECT asset_key, visibility_scope, source_chat_id, provenance, updated_at "
        "FROM asset_scopes WHERE asset_key=?",
        (str(asset_key),),
    ).fetchone()
    return dict(row) if row is not None else None


def record_publication_grant(
    store_or_path,
    *,
    thread_id: str,
    revision: int,
    target_chat: str,
    actor: str = "",
    now: float | None = None,
) -> None:
    if store_or_path is None or not thread_id or not target_chat:
        return
    db_path = getattr(store_or_path, "path", store_or_path)
    with closing(sqlite3.connect(db_path, timeout=3)) as db, db:
        ensure_scope_tables(db)
        db.execute(
            "INSERT OR REPLACE INTO publication_grants VALUES (?, ?, ?, ?, ?)",
            (
                str(thread_id),
                int(revision),
                str(target_chat),
                str(actor or ""),
                float(time.time() if now is None else now),
            ),
        )


def has_publication_grant(
    store_or_path,
    *,
    thread_id: str,
    revision: int,
    target_chats: Iterable[str],
) -> bool:
    chats = [str(c).strip() for c in target_chats if str(c or "").strip()]
    if store_or_path is None or not thread_id or not chats:
        return False
    db_path = getattr(store_or_path, "path", store_or_path)
    with closing(sqlite3.connect(db_path, timeout=3)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_grants'"
        ).fetchone()
        if not exists:
            return False
        ph = ",".join("?" for _ in chats)
        row = db.execute(
            f"SELECT 1 FROM publication_grants WHERE thread_id=? AND revision=? AND target_chat IN ({ph}) LIMIT 1",
            [str(thread_id), int(revision), *chats],
        ).fetchone()
    return row is not None
