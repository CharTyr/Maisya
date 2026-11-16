"""统一授权委托生命周期与共同裁决原语（B1）。"""

from contextlib import closing
import json
import sqlite3
import time
import uuid
from typing import Any

from .pathsafe import safe_chmod

DELEGATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS delegations ("
    "delegation_id TEXT PRIMARY KEY, "
    "subject_kind TEXT NOT NULL, "
    "subject_id TEXT NOT NULL, "
    "issuer TEXT NOT NULL, "
    "chat_id TEXT NOT NULL DEFAULT '', "
    "target_chat TEXT NOT NULL DEFAULT '', "
    "profile_id TEXT NOT NULL DEFAULT '', "
    "envelope_json TEXT NOT NULL DEFAULT '{}', "
    "status TEXT NOT NULL DEFAULT 'active', "
    "pause_reason TEXT NOT NULL DEFAULT '', "
    "created_at REAL NOT NULL, "
    "updated_at REAL NOT NULL, "
    "expires_at REAL, "
    "revoked_at REAL"
    ")"
)


def ensure_delegation_table(db: sqlite3.Connection) -> None:
    db.execute(DELEGATIONS_DDL)
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_delegations_subject "
        "ON delegations(subject_kind, subject_id, updated_at)"
    )


def _is_user_issuer(issuer: str) -> bool:
    text = str(issuer or "").strip()
    return ":" in text and not text.startswith(("console", "self"))


def approved_envelope(
    profile,
    services_cfg: dict | None = None,
    *,
    target_chat: str = "",
) -> dict[str, Any]:
    """冻结授权当刻的能力包络，防止后续热改静默扩权。"""
    if profile is None:
        return {"target_chat": str(target_chat or "")}
    cfg = services_cfg if isinstance(services_cfg, dict) else {}
    servers = cfg.get("servers") or {}
    apis = cfg.get("apis") or {}
    mcp_list = list(getattr(profile, "mcp", ()) or ())
    api_list = list(getattr(profile, "apis", ()) or ())
    writable_mcp = sorted(
        svc
        for svc in mcp_list
        if isinstance(servers.get(svc), dict) and servers[svc].get("readonly") is not True
    )
    writable_apis = sorted(
        api_id
        for api_id in api_list
        if isinstance(apis.get(api_id), dict) and apis[api_id].get("readonly") is not True
    )
    return {
        "execution_mode": str(getattr(profile, "execution_mode", "host") or "host"),
        "permission_mode": str(getattr(profile, "permission_mode", "ask_risky") or "ask_risky"),
        "secrets": list(getattr(profile, "secrets", ()) or ()),
        "shared_secrets": bool(getattr(profile, "shared_secrets", True)),
        "mcp": mcp_list,
        "writable_mcp": writable_mcp,
        "apis": api_list,
        "writable_apis": writable_apis,
        "target_chat": str(target_chat or ""),
    }


def record_delegation(
    store_or_path,
    *,
    subject_kind: str,
    subject_id: str,
    issuer: str,
    chat_id: str = "",
    target_chat: str = "",
    profile_id: str = "",
    envelope: dict | None = None,
    expires_at: float | None = None,
    delegation_id: str = "",
    clock=None,
) -> str:
    if store_or_path is None or not subject_id or not issuer:
        return ""
    now = float(clock()) if callable(clock) else time.time()
    did = str(delegation_id or f"del_{uuid.uuid4().hex[:16]}").strip()
    env_json = json.dumps(envelope or {}, ensure_ascii=False, sort_keys=True)
    db_path = getattr(store_or_path, "path", store_or_path)
    with closing(sqlite3.connect(db_path, timeout=3)) as db, db:
        ensure_delegation_table(db)
        db.execute(
            "UPDATE delegations SET status='superseded', updated_at=? "
            "WHERE subject_kind=? AND subject_id=? AND status='active'",
            (now, subject_kind, subject_id),
        )
        db.execute(
            "INSERT INTO delegations "
            "(delegation_id, subject_kind, subject_id, issuer, chat_id, target_chat, "
            "profile_id, envelope_json, status, pause_reason, created_at, updated_at, expires_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', '', ?, ?, ?, NULL) "
            "ON CONFLICT(delegation_id) DO UPDATE SET "
            "issuer=excluded.issuer, chat_id=excluded.chat_id, target_chat=excluded.target_chat, "
            "profile_id=excluded.profile_id, envelope_json=excluded.envelope_json, status='active', "
            "pause_reason='', updated_at=excluded.updated_at, expires_at=excluded.expires_at, revoked_at=NULL",
            (
                did,
                subject_kind,
                subject_id,
                str(issuer).strip(),
                str(chat_id or ""),
                str(target_chat or ""),
                str(profile_id or ""),
                env_json,
                now,
                now,
                expires_at,
            ),
        )
    safe_chmod(db_path, 0o600)
    return did


def latest_delegation(store_or_path, subject_kind: str, subject_id: str) -> dict | None:
    if store_or_path is None or not subject_id:
        return None
    db_path = getattr(store_or_path, "path", store_or_path)
    with closing(sqlite3.connect(db_path, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delegations'"
        ).fetchone()
        if not exists:
            return None
        row = db.execute(
            "SELECT * FROM delegations WHERE subject_kind=? AND subject_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (subject_kind, subject_id),
        ).fetchone()
    return dict(row) if row is not None else None


def pause_delegation(
    store_or_path, *, subject_kind: str, subject_id: str, reason: str, clock=None
) -> int:
    if store_or_path is None or not subject_id:
        return 0
    now = float(clock()) if callable(clock) else time.time()
    db_path = getattr(store_or_path, "path", store_or_path)
    with closing(sqlite3.connect(db_path, timeout=3)) as db, db:
        ensure_delegation_table(db)
        cur = db.execute(
            "UPDATE delegations SET status='paused', pause_reason=?, updated_at=? "
            "WHERE subject_kind=? AND subject_id=? AND status='active'",
            (str(reason or "委托已暂停")[:200], now, subject_kind, subject_id),
        )
        return int(cur.rowcount or 0)


def pause_delegations_for_revoked_admins(
    store_or_path, active_admins: set[str], *, clock=None
) -> int:
    """管理员名单缩减或清空后，冻结不再有效的个人高权委托（保留现任管理员接管通道）。"""
    if store_or_path is None:
        return 0
    now = float(clock()) if callable(clock) else time.time()
    db_path = getattr(store_or_path, "path", store_or_path)
    with closing(sqlite3.connect(db_path, timeout=3)) as db, db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='delegations'"
        ).fetchone()
        if not exists:
            return 0
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT delegation_id, issuer, subject_kind FROM delegations WHERE status='active'"
        ).fetchall()
        paused = 0
        for row in rows:
            issuer = str(row["issuer"] or "").strip()
            subj_kind = str(row["subject_kind"] or "").strip()
            is_console_thread = issuer == "console" and subj_kind == "thread"
            revoked = (not active_admins and not is_console_thread) or (
                _is_user_issuer(issuer) and issuer not in active_admins
            )
            if revoked:
                db.execute(
                    "UPDATE delegations SET status='paused', "
                    "pause_reason='原授权管理员已失效，需现任管理员接管', updated_at=? "
                    "WHERE delegation_id=?",
                    (now, row["delegation_id"]),
                )
                paused += 1
        return paused


def check_hot_edit_expansion(
    snapshot_profile,
    live_profile,
    *,
    services_cfg: dict | None = None,
    envelope: dict | None = None,
    requested_secrets: tuple[str, ...] | list[str] = (),
    requested_mcp: str = "",
    requested_api: str = "",
) -> str:
    """检查热改是否比原快照/批准包络扩权；若本次调用命中新增高权资源则返回拒绝原因。"""
    snap_secrets = set(getattr(snapshot_profile, "secrets", ()) or ())
    live_secrets = set(getattr(live_profile, "secrets", ()) or ()) if live_profile else set()
    env_secrets = set((envelope or {}).get("secrets") or ()) if isinstance(envelope, dict) and "secrets" in envelope else snap_secrets
    newly_bound_secrets = live_secrets - (snap_secrets | env_secrets)
    req_sec = set(requested_secrets or ())
    if req_sec & newly_bound_secrets:
        return "档案凭据配置在委派后已扩大（超出原批准包络），需重新核验授权"

    snap_mcp = set(getattr(snapshot_profile, "mcp", ()) or ())
    live_mcp = set(getattr(live_profile, "mcp", ()) or ()) if live_profile else set()
    env_mcp = set((envelope or {}).get("mcp") or ()) if isinstance(envelope, dict) and "mcp" in envelope else snap_mcp
    if requested_mcp and requested_mcp in (live_mcp - (snap_mcp | env_mcp)):
        return "档案 MCP 绑定在委派后已扩大（超出原批准包络），需重新核验授权"

    snap_apis = set(getattr(snapshot_profile, "apis", ()) or ())
    live_apis = set(getattr(live_profile, "apis", ()) or ()) if live_profile else set()
    env_apis = set((envelope or {}).get("apis") or ()) if isinstance(envelope, dict) and "apis" in envelope else snap_apis
    if requested_api and requested_api in (live_apis - (snap_apis | env_apis)):
        return "档案跨插件 API 绑定在委派后已扩大（超出原批准包络），需重新核验授权"

    if isinstance(services_cfg, dict) and isinstance(envelope, dict):
        if requested_mcp and "writable_mcp" in envelope:
            svc = (services_cfg.get("servers") or {}).get(requested_mcp)
            if (
                isinstance(svc, dict)
                and svc.get("readonly") is not True
                and requested_mcp not in set(envelope.get("writable_mcp") or ())
            ):
                return "MCP 服务权限由只读变为可写（超出原批准包络），需重新核验授权"
        if requested_api and "writable_apis" in envelope:
            spec = (services_cfg.get("apis") or {}).get(requested_api)
            if (
                isinstance(spec, dict)
                and spec.get("readonly") is not True
                and requested_api not in set(envelope.get("writable_apis") or ())
            ):
                return "跨插件 API 权限由只读变为可写（超出原批准包络），需重新核验授权"

    if (
        isinstance(envelope, dict)
        and envelope.get("execution_mode") == "sandbox"
        and live_profile is not None
        and getattr(live_profile, "execution_mode", "") == "host"
    ):
        return "执行环境由沙箱放宽为宿主机直通（超出原批准包络），需重新核验授权"
    return ""


def evaluate_activity_authority(
    plugin,
    row_or_payload: dict,
    *,
    subject_kind: str = "thread",
    subject_id: str = "",
) -> tuple[bool, str]:
    """统一校验高权后台活动/定时委托的当前授权生命周期。"""
    if not isinstance(row_or_payload, dict):
        return False, "委托载荷无效"
    if "grant" in row_or_payload and isinstance(row_or_payload.get("grant"), dict):
        grant = row_or_payload["grant"]
    elif "payload" in row_or_payload and isinstance(row_or_payload.get("payload"), dict):
        grant = row_or_payload["payload"]
    else:
        grant = row_or_payload

    is_admin_delegated = bool(
        grant.get("scheduled_admin") is True
        or grant.get("console_admin") is True
        or grant.get("admin_direct") is True
    )
    if not is_admin_delegated:
        return True, ""

    active_admins = set(getattr(plugin, "admins", set()) or ())
    if not active_admins and not (
        grant.get("console_admin") is True
        and str(grant.get("issuer_admin") or "console") == "console"
        and subject_kind == "thread"
    ):
        return False, "当前没有有效管理员，高权委托已暂停"

    sid = subject_id or str(
        row_or_payload.get("thread_id") or row_or_payload.get("intent_id") or ""
    ).strip()
    now = float(
        plugin.intents.clock()
        if getattr(plugin, "intents", None) is not None
        else time.time()
    )
    store = getattr(plugin, "grants", None) or getattr(plugin, "threads", None)
    del_row = latest_delegation(store, subject_kind, sid) if sid else None
    if del_row is not None:
        status = str(del_row.get("status") or "active")
        if status != "active":
            return False, str(del_row.get("pause_reason") or "原授权委托已暂停或撤销，需现任管理员接管")
        exp = del_row.get("expires_at")
        if exp is not None and float(exp) <= now:
            pause_delegation(
                store,
                subject_kind=subject_kind,
                subject_id=sid,
                reason="委托授权已过期，需现任管理员接管",
                clock=lambda: now,
            )
            return False, "委托授权已过期，需现任管理员接管"
        issuer = str(del_row.get("issuer") or "").strip()
        if _is_user_issuer(issuer) and issuer not in active_admins:
            pause_delegation(
                store,
                subject_kind=subject_kind,
                subject_id=sid,
                reason="原授权管理员已失效，需现任管理员接管",
                clock=lambda: now,
            )
            return False, "原授权管理员已失效，需现任管理员接管"

    issuer_admin = str(grant.get("issuer_admin") or "").strip()
    if _is_user_issuer(issuer_admin) and issuer_admin not in active_admins:
        if del_row is not None:
            pause_delegation(
                store,
                subject_kind=subject_kind,
                subject_id=sid,
                reason="原授权管理员已失效，需现任管理员接管",
                clock=lambda: now,
            )
        return False, "原授权管理员已失效，需现任管理员接管"

    src_msg_id = str(grant.get("source_msg_id") or "").strip()
    msg_store = getattr(plugin, "messages", None)
    if src_msg_id and msg_store is not None and not issuer_admin:
        src_chat = str(grant.get("source_chat_id") or row_or_payload.get("chat_id") or "")
        src_plat = str(grant.get("source_platform") or row_or_payload.get("platform") or "")
        msg = msg_store.lookup(src_msg_id, src_chat, src_plat)
        if msg is not None and not msg.is_local_operator:
            scoped_sender = f"{str(msg.platform or '').strip().lower()}:{msg.user_id}"
            if msg.is_plugin_relayed or scoped_sender not in active_admins:
                return False, "原授权来源管理员已失效，需现任管理员接管"

    return True, ""
