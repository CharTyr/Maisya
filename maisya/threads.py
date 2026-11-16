"""持续 Agent 线程、事件记录与有界运行。"""

import asyncio
import hashlib
import html
import inspect
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing, nullcontext
from dataclasses import asdict
from pathlib import Path

from .abilities import Profile
from .bridge import record_return
from .data_scope import (
    STEP_METHODS_CONTEXT,
    can_read_scope,
    ensure_scope_tables,
    has_publication_grant as _has_publication_grant_db,
    initial_thread_scope,
    is_tainted_scope,
    merge_scopes,
    record_publication_grant as _record_publication_grant_db,
)
from .context_budget import (
    DEFAULT_CONTEXT_TOKENS,
    DEFAULT_MIN_PRUNE_CHARS,
    DEFAULT_TAIL_TOKENS,
    MIN_CONTEXT_TOKENS,
    PRUNE_MIN_RECLAIM_TOKENS,
    THREAD_READ_DEFAULT_MAX_CHARS,
    build_static_handoff,
    estimate_messages_tokens,
    estimate_tokens,
    estimate_tools_tokens,
    extract_exact_anchors,
    extract_verbatim_user_messages,
    format_thread_history_prompt,
    is_context_overflow_error,
    prune_tool_outputs,
    resolve_context_budget,
)
from .pathsafe import rmtree_safe, safe_chmod
from .scout import TOOL_DEFINITIONS, validate_arguments
from .workspaces import completed_thread, file_lock


_PRESERVED_GRANT_FLAGS = (
    "scheduled_admin",
    "console_admin",
    "admin",
    "admin_verified",
)


def _merge_grant_dict(existing_grant: dict, incoming_grant: dict) -> dict:
    """合并线程级 grant，保留权限标记与非空来源字段，同时支持显式 admin_direct=False 解除预览粘性。"""
    merged = dict(existing_grant) if isinstance(existing_grant, dict) else {}
    incoming = incoming_grant if isinstance(incoming_grant, dict) else {}
    for key, value in incoming.items():
        if key in _PRESERVED_GRANT_FLAGS:
            if merged.get(key) is True or value is True:
                merged[key] = True
            elif key not in merged:
                merged[key] = value
            continue
        if key == "admin_direct":
            if value is False:
                merged["admin_direct"] = False
            elif value is True or merged.get("admin_direct") is True:
                merged["admin_direct"] = True
            elif key not in merged:
                merged[key] = value
            continue
        if isinstance(key, str) and key.startswith("source_"):
            non_empty = (
                bool(value.strip()) if isinstance(value, str) else value is not None and bool(value)
            )
            if non_empty:
                merged[key] = value
            continue
        if value is not None and (
            not isinstance(value, str) or value != "" or key not in merged
        ):
            merged[key] = value
    return merged


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def public(row):
    return {
        key: row[key] for key in ("thread_id", "profile", "owner", "state", "result")
    }


_INPUT_TAG_RE = re.compile(
    r"\s*<(?:reference_ids|previous_result|maisya_temperament|recent_journal|previous_runs)>[\s\S]*?</(?:reference_ids|previous_result|maisya_temperament|recent_journal|previous_runs)>\s*"
)
_HEARTBEAT_TITLE_PREFIX = "按自身节奏做一次轻量公开探索并记录近况"
_HEARTBEAT_TRAILER_RE = re.compile(r"\s*以下为近期已记录的手账[\s\S]*$")


def _clean_input_title(raw_text: str) -> str:
    stripped = _INPUT_TAG_RE.sub("\n", str(raw_text or "")).strip()
    if stripped.startswith(_HEARTBEAT_TITLE_PREFIX):
        body = re.split(r"[:：]\s*", stripped, maxsplit=1)[1] if (":" in stripped or "：" in stripped) else ""
        body = _HEARTBEAT_TRAILER_RE.sub("", body)
        for line in body.splitlines():
            item = line.strip()
            if not item or item.startswith("#") or item.startswith("<!--"):
                continue
            item = re.sub(r"^(?:[-*]|\d+[.)])\s*", "", item).strip()
            item = item.rstrip("。；;")
            if item:
                return f"闲暇漫游 · {item[:48]}"
        return "闲暇漫游 · 公开兴趣探索"
    cleaned = re.sub(r"\s+", " ", stripped)
    return cleaned[:80]


_HOST_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:/])"
    r"(?:/(?:Users|root|home|tmp|var|etc|opt|mnt|srv|private|Volumes|usr|bin|lib)(?:/[^\s'\",;)\]]*)?"
    r"|[A-Za-z]:\\[^\s'\",;)\]]+)"
)


def _safe_tool_error_message(error: Exception, redact=None) -> str:
    """保留安全的工具业务报错，对系统底层 OS 路径与隐蔽凭据做兜底收敛。"""
    if isinstance(error, OSError) and not isinstance(error, PermissionError):
        if (
            getattr(error, "errno", None) is not None
            or getattr(error, "filename", None) is not None
        ):
            return "工具文件或系统操作未完成；请检查工作区路径与权限配置。"
    raw = str(error or "").strip()
    if redact is not None and raw:
        raw = str(redact(raw))
    scrubbed = _HOST_ABS_PATH_RE.sub("<path>", raw)
    scrubbed = re.sub(r"\s+", " ", scrubbed).strip()
    return scrubbed[:280] if scrubbed else "只读工具未完成；检查参数、来源范围或宿主能力配置。"


async def _await_uninterruptible_coro(coro):
    """在重复取消下保护清理协程跑完，返回 `(result, saw_cancel)`。"""
    task = asyncio.ensure_future(coro)
    saw_cancel = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            saw_cancel = True
    return task.result(), saw_cancel


def _summarize_tool_calls(calls: list) -> str:
    parts = []
    for call in (calls or [])[:3]:
        fn = (call.get("function") or {}) if isinstance(call, dict) else {}
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                args = {}
        except ValueError:
            args = {}
        detail = ""
        if name == "web_search" and args.get("query"):
            detail = str(args["query"])[:32]
        elif name == "web_fetch" and args.get("url"):
            detail = str(args["url"])[:42]
        elif name == "workspace_file":
            act = str(args.get("action") or "")
            p = str(args.get("path") or "")[:32]
            detail = f"{act} {p}".strip()
        elif name == "html_render" and args.get("output_path"):
            detail = str(args["output_path"])[:32]
        elif name == "sandbox_exec" and args.get("command"):
            detail = re.sub(r"\s+", " ", str(args["command"])).strip()[:36]
        elif name == "digest_feed" and args.get("action"):
            detail = str(args["action"])[:24]
        parts.append(f"{name}({detail})" if detail else name)
    return ", ".join(parts)


def _implicit_ws_has_out(
    workspace_root: Path | None, thread_id: str
) -> tuple[Path | None, bool]:
    """检查未绑定持久工作区的临时线程目录是否已产出过 out/ 文件，避免误删。"""
    if workspace_root is None or not thread_id:
        return None, False
    ws_id = "ws_" + hashlib.sha256(thread_id.encode()).hexdigest()[:24]
    ws_dir = workspace_root / ws_id
    if not ws_dir.exists() or ws_dir.is_symlink() or not ws_dir.is_dir():
        return None, False
    ptr = ws_dir / "current.json"
    if not ptr.is_file() or ptr.is_symlink():
        return ws_dir, False
    try:
        version = json.loads(ptr.read_text(encoding="utf-8")).get("version", "")
    except (OSError, TypeError, ValueError):
        return ws_dir, False
    if not isinstance(version, str) or not re.fullmatch(r"v_[a-f0-9]{32}", version):
        return ws_dir, False
    out_dir = ws_dir / version / "out"
    if not out_dir.exists() or out_dir.is_symlink() or not out_dir.is_dir():
        return ws_dir, False
    has_files = any(p.is_file() and not p.is_symlink() for p in out_dir.rglob("*"))
    return ws_dir, has_files


def _repair_dangling_tool_calls(history: list[dict]) -> list[dict]:
    """为历史中未收到 tool 回执的 tool_calls 补齐跳过回执，保证多轮上下文配对有效。"""
    if not isinstance(history, list) or not history:
        return []
    repaired: list[dict] = []
    idx = 0
    total = len(history)
    while idx < total:
        msg = history[idx]
        if not isinstance(msg, dict):
            idx += 1
            continue
        copied = dict(msg)
        repaired.append(copied)
        tool_calls = copied.get("tool_calls") if copied.get("role") == "assistant" else None
        idx += 1
        if not isinstance(tool_calls, list) or not tool_calls:
            continue
        seen_ids: set[str] = set()
        while idx < total and isinstance(history[idx], dict) and history[idx].get("role") == "tool":
            tool_msg = dict(history[idx])
            repaired.append(tool_msg)
            call_id = tool_msg.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                seen_ids.add(call_id)
            idx += 1
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id")
            if isinstance(tc_id, str) and tc_id and tc_id not in seen_ids:
                seen_ids.add(tc_id)
                repaired.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": encode(
                            {
                                "skipped": True,
                                "reason": "线程已中断或停止，该工具调用未完成，已自动跳过。",
                            }
                        ),
                    }
                )
    return repaired


def _has_unresumed_stop(db, thread_id: str) -> bool:
    stop_row = db.execute(
        'SELECT MAX(seq) AS s FROM thread_events WHERE thread_id=? AND kind="stopped"',
        (thread_id,),
    ).fetchone()
    stop_seq = int(stop_row["s"] or 0) if stop_row else 0
    if not stop_seq:
        return False
    active_row = db.execute(
        'SELECT MAX(seq) AS s FROM thread_events WHERE thread_id=? AND kind IN ("input", "reopened")',
        (thread_id,),
    ).fetchone()
    active_seq = int(active_row["s"] or 0) if active_row else 0
    return stop_seq > active_seq


def _was_stopped_or_terminal(db, thread_id: str, state: str) -> bool:
    if state in {"closed", "archived", "done"}:
        return True
    if state != "idle":
        return False
    return _has_unresumed_stop(db, thread_id)


def _is_junk_thread_row(
    row,
    *,
    delivered_ids: set[str],
    named_ws_ids: set[str],
    attachment_ids: set[str],
    workspace_root: Path | None = None,
) -> bool:
    """判断线程是否属于可批量清理的废记录（已停未交付、报错、空结果且无附件/工作区/out）。"""
    tid = row["thread_id"]
    state = str(row["state"] or "")
    if (
        state in {"active", "waiting", "archived"}
        or tid in named_ws_ids
        or tid in attachment_ids
        or tid in delivered_ids
    ):
        return False
    _ws_dir, has_out = _implicit_ws_has_out(workspace_root, tid)
    if has_out:
        return False
    if state == "closed":
        return True
    try:
        raw = row["result"]
        parsed = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return True
    if not isinstance(parsed, dict) or "error" in parsed:
        return True
    text = str(parsed.get("text") or "").strip()
    atts = parsed.get("attachments")
    has_atts = isinstance(atts, list) and len(atts) > 0
    return not (text or has_atts)


class ThreadStore:
    def __init__(self, path: Path):
        self.path = path
        self.chat_aliases = None
        with self.connect() as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS agent_threads ("
                "thread_id TEXT PRIMARY KEY, profile TEXT, profile_snapshot TEXT, chat_id TEXT, platform TEXT, "
                "user_id TEXT, owner TEXT, state TEXT, result TEXT, history TEXT, summary TEXT, "
                "revision INTEGER, processed_revision INTEGER, completed_revision INTEGER, questions INTEGER, updated REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS thread_events ("
                "seq INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, kind TEXT, revision INTEGER, payload TEXT, at REAL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS thread_event_lookup ON thread_events(thread_id, revision)"
            )
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS thread_search USING fts5(thread_id UNINDEXED, text)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS agent_workspaces (workspace_id TEXT PRIMARY KEY, owner TEXT, chat_id TEXT, name TEXT, profile TEXT, thread_id TEXT, UNIQUE(owner,chat_id,name))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS thread_context (thread_id TEXT PRIMARY KEY, workspace_id TEXT, grant TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS primary_threads (owner TEXT, chat_id TEXT, profile TEXT, thread_id TEXT, updated REAL, PRIMARY KEY (owner, chat_id, profile))"
            )
            for tbl in ("agent_threads", "agent_workspaces"):
                cols = {r[1] for r in db.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "visibility_scope" not in cols:
                    db.execute(
                        f"ALTER TABLE {tbl} ADD COLUMN visibility_scope TEXT NOT NULL DEFAULT ''"
                    )
            ensure_scope_tables(db)
        safe_chmod(path, 0o600)

    def narrow_scope(self, thread_id: str, *extra_scopes: str) -> str:
        clean = [str(s or "").strip() for s in extra_scopes if str(s or "").strip()]
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT owner, chat_id, visibility_scope FROM agent_threads WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if row is None:
                return ""
            cur = str(row["visibility_scope"] or "").strip() or initial_thread_scope(
                owner=row["owner"] or "chat",
                chat_id=row["chat_id"] or "",
            )
            merged = merge_scopes([cur, *clean])
            db.execute(
                "UPDATE agent_threads SET visibility_scope=? WHERE thread_id=?",
                (merged, thread_id),
            )
            ctx_row = db.execute(
                "SELECT workspace_id FROM thread_context WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            ws_id = str(ctx_row["workspace_id"] or "").strip() if ctx_row else ""
            if ws_id:
                ws_row = db.execute(
                    "SELECT visibility_scope FROM agent_workspaces WHERE workspace_id=?",
                    (ws_id,),
                ).fetchone()
                ws_cur = str(ws_row["visibility_scope"] or "").strip() if ws_row else ""
                ws_merged = merge_scopes([ws_cur, merged]) if ws_cur else merged
                db.execute(
                    "UPDATE agent_workspaces SET visibility_scope=? WHERE workspace_id=?",
                    (ws_merged, ws_id),
                )
            return merged

    def record_publication_grant(
        self, thread_id: str, revision: int, target_chat: str, *, actor: str = ""
    ) -> None:
        targets = {str(target_chat or "").strip()}
        if target_chat and callable(getattr(self, "chat_aliases", None)):
            try:
                targets.update(
                    str(a).strip() for a in self.chat_aliases(target_chat, "") if a
                )
            except (TypeError, ValueError):
                pass
        for cand in sorted(t for t in targets if t):
            _record_publication_grant_db(
                self,
                thread_id=thread_id,
                revision=int(revision or 0),
                target_chat=cand,
                actor=actor,
            )

    def has_publication_grant(
        self, thread_id: str, revision: int, target_chat: str
    ) -> bool:
        targets = [str(target_chat or "").strip()]
        if target_chat and callable(getattr(self, "chat_aliases", None)):
            try:
                for alias in sorted(set(self.chat_aliases(target_chat, ""))):
                    if isinstance(alias, str) and alias and alias not in targets:
                        targets.append(alias)
            except (TypeError, ValueError):
                pass
        return _has_publication_grant_db(
            self,
            thread_id=thread_id,
            revision=int(revision or 0),
            target_chats=targets,
        )

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=3)
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def create(
        self,
        profile,
        message,
        *,
        chat_id,
        platform,
        user_id,
        owner="chat",
        workspace="",
        grant=None,
        fresh_workspace: bool = False,
    ):
        if owner not in {"chat", "self"}:
            raise ValueError("线程归属无效")
        if (
            not isinstance(workspace, str)
            or workspace
            and not re.fullmatch(r"[\w-]{1,64}", workspace)
        ):
            raise ValueError("工作区名称只能含文字、数字、下划线或横线")
        thread_id = "th_" + uuid.uuid4().hex[:16]
        grant_dict = grant if isinstance(grant, dict) else {}
        base_vis = initial_thread_scope(owner=owner, chat_id=chat_id)
        vis_scope = merge_scopes(
            [
                base_vis,
                "admin_only"
                if (
                    getattr(profile, "access", "everyone") == "admins"
                    or bool(getattr(profile, "secrets", ()))
                )
                else "public",
                grant_dict.get("visibility_scope"),
            ]
        )
        is_admin_caller = bool(
            grant_dict.get("admin_verified")
            or grant_dict.get("scheduled_admin")
            or grant_dict.get("console_admin")
            or grant_dict.get("admin_direct")
            or grant_dict.get("issuer_admin")
        )
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            workspace_id = ""
            ws_name_to_bind = workspace
            if workspace:
                binding = db.execute(
                    "SELECT * FROM agent_workspaces WHERE owner=? AND chat_id=? AND name=?",
                    (owner, chat_id, workspace),
                ).fetchone()
                if (
                    binding is None
                    and owner == "chat"
                    and callable(getattr(self, "chat_aliases", None))
                ):
                    try:
                        aliases = sorted(
                            set(self.chat_aliases(chat_id, platform)) - {chat_id}
                        )
                    except (TypeError, ValueError):
                        aliases = []
                    for alias in aliases:
                        if not isinstance(alias, str) or not alias:
                            continue
                        binding = db.execute(
                            "SELECT * FROM agent_workspaces WHERE owner=? AND chat_id=? AND name=?",
                            (owner, alias, workspace),
                        ).fetchone()
                        if binding is not None:
                            break
                if binding and not is_admin_caller:
                    b_vis = str(
                        (binding["visibility_scope"] if "visibility_scope" in binding.keys() else "")
                        or initial_thread_scope(owner=binding["owner"], chat_id=binding["chat_id"])
                    )
                    ws_aliases = [chat_id]
                    if callable(getattr(self, "chat_aliases", None)):
                        try:
                            ws_aliases = list(self.chat_aliases(chat_id, platform))
                        except (TypeError, ValueError):
                            pass
                    if is_tainted_scope(b_vis, current_chat_id=chat_id, aliases=ws_aliases):
                        binding = None
                        ws_name_to_bind = f"{workspace}_clean_{uuid.uuid4().hex[:6]}"
                if binding and fresh_workspace:
                    archived_name = f"{binding['name']}#archived_{str(binding['thread_id'])[-6:]}"
                    db.execute(
                        "UPDATE agent_workspaces SET name=? WHERE workspace_id=?",
                        (archived_name, binding["workspace_id"]),
                    )
                    binding = None
                if binding:
                    if binding["profile"] != profile.id:
                        raise ValueError("该工作区已绑定另一个角色")
                    previous = db.execute(
                        "SELECT state,profile_snapshot,visibility_scope FROM agent_threads WHERE thread_id=?",
                        (binding["thread_id"],),
                    ).fetchone()
                    if previous:
                        previous_scope = json.loads(previous["profile_snapshot"])
                        widened = (
                            previous_scope["access"] == "admins"
                            and profile.access != "admins"
                        )
                        old_users = previous_scope.get("allow_users", [])
                        if old_users and (
                            not profile.allow_users
                            or not set(profile.allow_users) <= set(old_users)
                        ):
                            widened = True
                        if widened:
                            raise PermissionError(
                                "受限工作区不能随档案权限修改向其他用户开放"
                            )
                    if previous and not _was_stopped_or_terminal(
                        db, binding["thread_id"], str(previous["state"] or "")
                    ):
                        updated = json.loads(encode(asdict(profile)))
                        prev_vis = str(
                            (previous["visibility_scope"] if "visibility_scope" in previous.keys() else "")
                            or base_vis
                        )
                        next_vis = merge_scopes([prev_vis, vis_scope])
                        if (
                            binding["chat_id"] != chat_id
                            and previous["state"] == "active"
                        ):
                            raise RuntimeError("工作区正在运行中，请稍候再试")
                        if (
                            binding["chat_id"] != chat_id
                            and previous["state"] != "active"
                        ):
                            db.execute(
                                "UPDATE agent_threads SET chat_id=?, platform=? WHERE thread_id=?",
                                (chat_id, platform, binding["thread_id"]),
                            )
                            db.execute(
                                "UPDATE agent_workspaces SET chat_id=? WHERE workspace_id=?",
                                (chat_id, binding["workspace_id"]),
                            )
                            if (
                                db.execute(
                                    'SELECT 1 FROM sqlite_master WHERE type="table" AND name="attachments"'
                                ).fetchone()
                                is not None
                            ):
                                db.execute(
                                    "UPDATE attachments SET chat_id=? "
                                    "WHERE thread_id=? OR thread_id IN ("
                                    "SELECT thread_id FROM thread_context WHERE workspace_id=?)",
                                    (
                                        chat_id,
                                        binding["thread_id"],
                                        binding["workspace_id"],
                                    ),
                                )
                        db.execute(
                            "UPDATE agent_threads SET profile_snapshot=?, visibility_scope=? WHERE thread_id=?",
                            (encode(updated), next_vis, binding["thread_id"]),
                        )
                        db.execute(
                            "UPDATE agent_workspaces SET visibility_scope=? WHERE workspace_id=?",
                            (next_vis, binding["workspace_id"]),
                        )
                        db.execute(
                            "UPDATE thread_context SET grant=? WHERE thread_id=?",
                            (encode(grant or {}), binding["thread_id"]),
                        )
                        db.commit()
                        return self.get(
                            binding["thread_id"],
                            chat_id,
                            allow_any_chat=binding["chat_id"] != chat_id,
                        )
                    workspace_id = binding["workspace_id"]
                else:
                    workspace_id = (
                        "ws_"
                        + hashlib.sha256(
                            encode([owner, chat_id, ws_name_to_bind]).encode()
                        ).hexdigest()[:24]
                    )
                    while db.execute(
                        "SELECT 1 FROM agent_workspaces WHERE workspace_id=?",
                        (workspace_id,),
                    ).fetchone():
                        workspace_id = (
                            "ws_"
                            + hashlib.sha256(
                                encode([owner, chat_id, ws_name_to_bind, uuid.uuid4().hex]).encode()
                            ).hexdigest()[:24]
                        )
                b_vis_old = str(
                    (binding["visibility_scope"] if binding and "visibility_scope" in binding.keys() else "")
                    or vis_scope
                )
                ws_vis = merge_scopes([b_vis_old, vis_scope]) if binding else vis_scope
                db.execute(
                    "INSERT INTO agent_workspaces (workspace_id, owner, chat_id, name, profile, thread_id, visibility_scope) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(workspace_id) DO UPDATE SET chat_id=excluded.chat_id, thread_id=excluded.thread_id, visibility_scope=excluded.visibility_scope",
                    (workspace_id, owner, chat_id, ws_name_to_bind, profile.id, thread_id, ws_vis),
                )
            db.execute(
                "INSERT INTO agent_threads "
                "(thread_id, profile, profile_snapshot, chat_id, platform, user_id, owner, state, result, history, summary, revision, processed_revision, completed_revision, questions, updated, visibility_scope) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    profile.id,
                    encode(asdict(profile)),
                    chat_id,
                    platform,
                    user_id,
                    owner,
                    "idle",
                    "{}",
                    "[]",
                    "",
                    0,
                    0,
                    0,
                    0,
                    time.time(),
                    vis_scope,
                ),
            )
            db.execute("INSERT INTO thread_search VALUES (?, ?)", (thread_id, message))
            db.execute(
                "INSERT INTO thread_context VALUES (?, ?, ?)",
                (thread_id, workspace_id, encode(grant or {})),
            )
            self.event(
                db,
                thread_id,
                "opened",
                0,
                {"origin": "user" if user_id else "maibot", "owner": owner},
            )
        return self.get(thread_id, chat_id)

    def get(self, thread_id, chat_id, *, allow_self=False, allow_any_chat=False):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM agent_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            context = db.execute(
                "SELECT workspace_id,grant FROM thread_context WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        if row is None:
            raise ValueError("线程不存在")
        result = dict(row)
        result["workspace_id"] = context["workspace_id"] if context else ""
        result["grant"] = json.loads(context["grant"]) if context else {}
        if not str(result.get("visibility_scope") or "").strip():
            result["visibility_scope"] = initial_thread_scope(
                owner=result.get("owner") or "chat",
                chat_id=result.get("chat_id") or "",
            )
        if (
            not allow_any_chat
            and result["chat_id"] != chat_id
            and not (allow_self and result["owner"] == "self")
        ):
            raise PermissionError("线程不属于当前会话")
        for key in ("result", "history", "profile_snapshot"):
            result[key] = json.loads(result[key])
        return result

    def enqueue(
        self, thread_id, message, chat_id, return_route=None, input_context=None
    ):
        self.get(thread_id, chat_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute(
                    "SELECT revision, state, questions, history FROM agent_threads WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
                if cur is None:
                    raise ValueError("线程不存在")
                revision = int(cur["revision"]) + 1
                existing_history = json.loads(cur["history"] or "[]")
                repaired_history = _repair_dangling_tool_calls(existing_history)
                reset_questions = cur["state"] in {"idle", "done", "archived", "closed"}
                if repaired_history != existing_history:
                    db.execute(
                        'UPDATE agent_threads SET revision=?, state="active", questions=?, history=?, updated=? WHERE thread_id=?',
                        (
                            revision,
                            0 if reset_questions else cur["questions"],
                            encode(repaired_history),
                            time.time(),
                            thread_id,
                        ),
                    )
                else:
                    db.execute(
                        'UPDATE agent_threads SET revision=?, state="active", questions=?, updated=? WHERE thread_id=?',
                        (
                            revision,
                            0 if reset_questions else cur["questions"],
                            time.time(),
                            thread_id,
                        ),
                    )
                if cur["state"] in {"done", "archived", "closed"}:
                    self.event(
                        db,
                        thread_id,
                        "reopened",
                        revision - 1,
                        {"from_state": cur["state"]},
                    )
                payload = {"text": message, "context": input_context or {}}
                if (
                    isinstance(input_context, dict)
                    and "grant" in input_context
                    and isinstance(input_context["grant"], dict)
                ):
                    grant_row = db.execute(
                        "SELECT grant FROM thread_context WHERE thread_id=?",
                        (thread_id,),
                    ).fetchone()
                    existing_grant = {}
                    if grant_row and grant_row["grant"]:
                        try:
                            parsed_grant = json.loads(grant_row["grant"])
                            if isinstance(parsed_grant, dict):
                                existing_grant = parsed_grant
                        except (TypeError, ValueError):
                            existing_grant = {}
                    merged_grant = _merge_grant_dict(
                        existing_grant, input_context["grant"]
                    )
                    if grant_row is None:
                        db.execute(
                            'INSERT INTO thread_context (thread_id, workspace_id, grant) VALUES (?, "", ?)',
                            (thread_id, encode(merged_grant)),
                        )
                    else:
                        db.execute(
                            "UPDATE thread_context SET grant=? WHERE thread_id=?",
                            (encode(merged_grant), thread_id),
                        )
                if return_route:
                    payload["return_route"] = return_route
                    db.execute(
                        "INSERT INTO return_members VALUES (?, ?, ?)",
                        (return_route["group_id"], thread_id, revision),
                    )
                self.event(db, thread_id, "input", revision, payload)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return revision

    def input_context(self, thread_id, revision):
        with self.connect() as db:
            row = db.execute(
                'SELECT payload FROM thread_events WHERE thread_id=? AND revision=? AND kind="input"',
                (thread_id, revision),
            ).fetchone()
        return json.loads(row["payload"]).get("context", {}) if row else {}

    @staticmethod
    def event(db, thread_id, kind, revision, payload):
        cursor = db.execute(
            "INSERT INTO thread_events(thread_id, kind, revision, payload, at) VALUES (?, ?, ?, ?, ?)",
            (thread_id, kind, revision, encode(payload), time.time()),
        )
        return cursor.lastrowid

    def pending(self, thread_id, processed):
        with self.connect() as db:
            rows = db.execute(
                'SELECT revision, payload FROM thread_events WHERE thread_id=? AND kind="input" '
                "AND revision>? ORDER BY revision",
                (thread_id, processed),
            ).fetchall()
        return [(row["revision"], json.loads(row["payload"])["text"]) for row in rows]

    def boundary(self, thread_id, processed):
        with self.connect() as db:
            row = db.execute(
                "SELECT revision, state FROM agent_threads WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            context = db.execute(
                "SELECT workspace_id FROM thread_context WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        if context and context["workspace_id"]:
            return row is not None and row["state"] == "active", False
        return row is not None and row["state"] == "active", row is not None and row[
            "revision"
        ] > processed

    def checkpoint(self, thread_id, history, processed, kind, payload):
        with self.connect() as db, db:
            cursor = db.execute(
                'UPDATE agent_threads SET history=?, processed_revision=? WHERE thread_id=? AND state="active"',
                (encode(history), processed, thread_id),
            )
            if cursor.rowcount:
                self.event(db, thread_id, kind, processed, payload)

    def finish(
        self,
        thread_id,
        chat_id,
        result,
        history,
        processed,
        *,
        waiting=False,
        state=None,
    ):
        row = self.get(thread_id, chat_id)
        if state == "done":
            with self.connect() as db, db:
                cursor = db.execute(
                    'UPDATE agent_threads SET result=?, history=?, processed_revision=?, completed_revision=?, state="done", updated=? '
                    'WHERE thread_id=? AND state IN ("active", "idle", "waiting")',
                    (
                        encode(result),
                        encode(history),
                        processed,
                        processed,
                        time.time(),
                        thread_id,
                    ),
                )
                if not cursor.rowcount:
                    return False
                event_seq = self.event(db, thread_id, "result", processed, result)
                record_return(db, thread_id, processed, event_seq)
                records = db.execute(
                    'SELECT payload FROM thread_events WHERE thread_id=? AND kind IN ("input", "result", "question") ORDER BY seq',
                    (thread_id,),
                ).fetchall()
                text = "\n".join(item["payload"] for item in records)
                db.execute("DELETE FROM thread_search WHERE thread_id=?", (thread_id,))
                db.execute("INSERT INTO thread_search VALUES (?, ?)", (thread_id, text))
            return True
        fifo = bool(row["workspace_id"])
        target_state = state if state in {"idle", "waiting", "done"} else ("waiting" if waiting else "idle")
        with self.connect() as db, db:
            cursor = db.execute(
                "UPDATE agent_threads SET result=?, history=?, processed_revision=?, completed_revision=?, "
                'state=CASE WHEN ? AND revision>? THEN "active" ELSE ? END, questions=questions+?, updated=? WHERE thread_id=? AND state="active" AND revision'
                + (">=?" if fifo else "=?"),
                (
                    encode(result),
                    encode(history),
                    processed,
                    processed,
                    int(fifo),
                    processed,
                    target_state,
                    int(waiting),
                    time.time(),
                    thread_id,
                    processed,
                ),
            )
            if not cursor.rowcount:
                return False
            event_seq = self.event(
                db, thread_id, "question" if waiting else "result", processed, result
            )
            record_return(db, thread_id, processed, event_seq)
            records = db.execute(
                'SELECT payload FROM thread_events WHERE thread_id=? AND kind IN ("input", "result", "question") ORDER BY seq',
                (thread_id,),
            ).fetchall()
            text = "\n".join(item["payload"] for item in records)
            db.execute("DELETE FROM thread_search WHERE thread_id=?", (thread_id,))
            db.execute("INSERT INTO thread_search VALUES (?, ?)", (thread_id, text))
        return True

    def stop(self, thread_id, chat_id):
        row = self.get(thread_id, chat_id)
        repaired_history = _repair_dangling_tool_calls(row["history"])
        with self.connect() as db, db:
            db.execute(
                'UPDATE agent_threads SET state="idle", history=?, processed_revision=revision, updated=? WHERE thread_id=?',
                (encode(repaired_history), time.time(), thread_id),
            )
            has_intents = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='return_intents'"
            ).fetchone()
            if has_intents:
                db.execute(
                    "UPDATE return_intents SET consumed=1 WHERE thread_id=? AND consumed=0",
                    (thread_id,),
                )
            self.event(db, thread_id, "stopped", row["revision"], {})
        return self.get(thread_id, chat_id)

    def is_stopped(self, thread_id: str, *, state: str | None = None) -> bool:
        """判断线程是否处于未恢复的停止态（idle/archived/done 下最近停止事件晚于恢复事件）。"""
        if state in {"active", "waiting"}:
            return False
        if state == "closed":
            return True
        with self.connect() as db:
            if state is None:
                row = db.execute(
                    "SELECT state FROM agent_threads WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
                if not row:
                    return True
                resolved_state = row["state"]
                if resolved_state in {"active", "waiting"}:
                    return False
                if resolved_state == "closed":
                    return True
            return _has_unresumed_stop(db, thread_id)

    def save_stopped_history(self, thread_id, history, processed):
        repaired = _repair_dangling_tool_calls(history)
        with self.connect() as db, db:
            db.execute(
                'UPDATE agent_threads SET history=? WHERE thread_id=? AND state="idle" AND revision=?',
                (encode(repaired), thread_id, processed),
            )

    def cleanup_error(self, thread_id, chat_id, message):
        row = self.get(thread_id, chat_id)
        with self.connect() as db, db:
            result = {**row["result"], "error": message}
            db.execute(
                "UPDATE agent_threads SET result=?,updated=? WHERE thread_id=?",
                (encode(result), time.time(), thread_id),
            )
            self.event(
                db, thread_id, "cleanup_error", row["revision"], {"error": message}
            )

    def recover(self):
        with self.connect() as db, db:
            db.execute(
                'UPDATE agent_threads SET state="idle", '
                'result=CASE WHEN processed_revision < revision AND result != "{}" THEN result ELSE ? END '
                'WHERE state="active"',
                (encode({"error": "宿主重载中断了运行；可以给原线程 send 继续。"}),),
            )

    def activate(self, thread_id):
        with self.connect() as db, db:
            cursor = db.execute(
                'UPDATE agent_threads SET state="active", updated=? WHERE thread_id=? AND state IN ("idle", "waiting", "done", "archived", "closed")',
                (time.time(), thread_id),
            )
            return cursor.rowcount > 0

    def reopen(self, thread_id, chat_id=None, *, allow_self=False, allow_any_chat=False):
        row = self.get(
            thread_id,
            chat_id or "",
            allow_self=allow_self,
            allow_any_chat=(allow_any_chat or chat_id is None),
        )
        repaired = _repair_dangling_tool_calls(row["history"])
        with self.connect() as db, db:
            db.execute(
                'UPDATE agent_threads SET state="idle", history=?, updated=? WHERE thread_id=?',
                (encode(repaired), time.time(), thread_id),
            )
            self.event(
                db,
                thread_id,
                "reopened",
                row["revision"],
                {"from_state": row["state"]},
            )
        return self.get(
            thread_id,
            row["chat_id"],
            allow_self=(row["owner"] == "self"),
        )

    def find_primary_thread(self, profile_id, chat_id, *, owner="chat", is_admin: bool = True):
        target_chat = "self" if owner == "self" else chat_id
        with self.connect() as db:
            primary = db.execute(
                "SELECT thread_id FROM primary_threads WHERE owner=? AND chat_id=? AND profile=?",
                (owner, target_chat, profile_id),
            ).fetchone()
            if (
                primary is None
                and owner == "chat"
                and callable(getattr(self, "chat_aliases", None))
            ):
                try:
                    aliases = sorted(set(self.chat_aliases(chat_id, "")) - {chat_id})
                except (TypeError, ValueError):
                    aliases = []
                for alias in aliases:
                    primary = db.execute(
                        "SELECT thread_id FROM primary_threads WHERE owner=? AND chat_id=? AND profile=?",
                        (owner, alias, profile_id),
                    ).fetchone()
                    if primary is not None:
                        break
            if primary is None:
                return None
            tid = primary["thread_id"]
            row = db.execute(
                "SELECT state, chat_id, owner FROM agent_threads WHERE thread_id=?",
                (tid,),
            ).fetchone()
            if row is None or row["state"] in {"archived", "closed", "done"}:
                return None
            resolved = self.get(
                tid,
                row["chat_id"],
                allow_self=(row["owner"] == "self"),
                allow_any_chat=(row["chat_id"] != chat_id and callable(getattr(self, "chat_aliases", None))),
            )
            if not is_admin:
                check_aliases = [chat_id]
                if callable(getattr(self, "chat_aliases", None)):
                    try:
                        check_aliases = list(self.chat_aliases(chat_id, ""))
                    except (TypeError, ValueError):
                        pass
                if is_tainted_scope(
                    resolved.get("visibility_scope"),
                    current_chat_id=chat_id,
                    aliases=check_aliases,
                ):
                    return None
            return resolved

    def bind_primary_thread(self, profile_id, chat_id, thread_id, *, owner="chat"):
        target_chat = "self" if owner == "self" else chat_id
        with self.connect() as db, db:
            db.execute(
                "INSERT OR REPLACE INTO primary_threads VALUES (?, ?, ?, ?, ?)",
                (owner, target_chat, profile_id, thread_id, time.time()),
            )

    def archive_thread(self, thread_id, chat_id=None, *, allow_self=False):
        row = self.get(
            thread_id,
            chat_id or "",
            allow_self=allow_self,
            allow_any_chat=(chat_id is None),
        )
        with self.connect() as db, db:
            db.execute(
                'UPDATE agent_threads SET state="archived", updated=? WHERE thread_id=?',
                (time.time(), thread_id),
            )
            db.execute(
                "DELETE FROM primary_threads WHERE thread_id=?",
                (thread_id,),
            )
            ws_rows = db.execute(
                "SELECT workspace_id, name FROM agent_workspaces WHERE thread_id=?",
                (thread_id,),
            ).fetchall()
            for ws_row in ws_rows:
                ws_name = str(ws_row["name"] or "")
                suffix = f"#archived_{thread_id[-6:]}"
                if ws_name and "#archived_" not in ws_name:
                    db.execute(
                        "UPDATE agent_workspaces SET name=? WHERE workspace_id=?",
                        (ws_name + suffix, ws_row["workspace_id"]),
                    )
            self.event(db, thread_id, "archived", row["revision"], {})
        return self.get(
            thread_id,
            row["chat_id"],
            allow_self=(row["owner"] == "self"),
        )

    def update_profile_snapshot(self, thread_id, profile, chat_id=None):
        row = self.get(
            thread_id,
            chat_id or "",
            allow_self=True,
            allow_any_chat=(chat_id is None),
        )
        prev_scope = row["profile_snapshot"] if isinstance(row["profile_snapshot"], dict) else {}
        widened = prev_scope.get("access") == "admins" and profile.access != "admins"
        old_users = prev_scope.get("allow_users", [])
        if old_users and (
            not profile.allow_users or not set(profile.allow_users) <= set(old_users)
        ):
            widened = True
        if widened:
            raise PermissionError("受限工作区不能随档案权限修改向其他用户开放")
        updated = json.loads(encode(asdict(profile)))
        with self.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET profile_snapshot=? WHERE thread_id=?",
                (encode(updated), thread_id),
            )

    @staticmethod
    def _named_workspace_thread_ids(db) -> set[str]:
        return {
            row["thread_id"]
            for row in db.execute(
                "SELECT thread_id FROM agent_workspaces WHERE thread_id IS NOT NULL "
                "UNION SELECT c.thread_id FROM thread_context c "
                "JOIN agent_workspaces w ON w.workspace_id=c.workspace_id "
                "WHERE c.thread_id IS NOT NULL"
            ).fetchall()
            if row["thread_id"]
        }

    @staticmethod
    def _delete_threads_tx(db, thread_ids: list[str]) -> None:
        if not thread_ids:
            return
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        core_tables = ("thread_events", "thread_search", "thread_context", "agent_threads")
        opt_tables = (
            "result_deliveries",
            "executions",
            "learning_receipts",
            "experience_receipts",
        )
        for tid in thread_ids:
            for tbl in core_tables:
                db.execute(f"DELETE FROM {tbl} WHERE thread_id=?", (tid,))
            for tbl in opt_tables:
                if tbl in tables:
                    db.execute(f"DELETE FROM {tbl} WHERE thread_id=?", (tid,))
            if "intents" in tables:
                db.execute(
                    "UPDATE intents SET status='cancelled' WHERE thread_id=? AND status='pending'",
                    (tid,),
                )
            if "return_intents" in tables:
                db.execute(
                    "UPDATE return_intents SET consumed=1 WHERE thread_id=? AND consumed=0",
                    (tid,),
                )

    def delete_thread(
        self, thread_id: str, *, workspace_root: Path | None = None
    ) -> dict:
        """删除单条非运行且未绑定持久工作区的活动记录，保留 attachments 产物。"""
        if not isinstance(thread_id, str) or not re.fullmatch(
            r"th_[a-f0-9]{16}", thread_id
        ):
            raise ValueError("线程编号无效")
        lock_ctx = (
            file_lock(workspace_root / ".capacity.lock")
            if workspace_root is not None
            else nullcontext()
        )
        with lock_ctx:
            with self.connect() as db, db:
                row = db.execute(
                    "SELECT thread_id,chat_id,state FROM agent_threads WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("活动记录不存在")
                if row["state"] in {"active", "waiting"}:
                    raise ValueError("运行中或等待中的活动不能直接删除，请先停止线程")
                if thread_id in self._named_workspace_thread_ids(db):
                    raise ValueError("该活动绑定了持久工作区，不能直接删除记录")
                self._delete_threads_tx(db, [thread_id])
            ws_dir, has_out = _implicit_ws_has_out(workspace_root, thread_id)
            if ws_dir is not None and not has_out:
                rmtree_safe(ws_dir, ignore_errors=True)
        return {"deleted": thread_id, "chat_id": row["chat_id"]}

    def prune_junk_threads(
        self,
        *,
        chat_ids: set[str] | list[str] | None = None,
        workspace_root: Path | None = None,
    ) -> dict:
        """批量清理废记录（已停止未交付、报错结束、空结果且无附件/持久工作区/out）。"""
        scope = [c for c in (chat_ids or []) if isinstance(c, str) and c]
        lock_ctx = (
            file_lock(workspace_root / ".capacity.lock")
            if workspace_root is not None
            else nullcontext()
        )
        with lock_ctx:
            with self.connect() as db, db:
                if scope:
                    placeholders = ",".join("?" * len(scope))
                    rows = db.execute(
                        f"SELECT thread_id,chat_id,state,result FROM agent_threads WHERE chat_id IN ({placeholders})",
                        tuple(scope),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT thread_id,chat_id,state,result FROM agent_threads"
                    ).fetchall()
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                named_ws_ids = self._named_workspace_thread_ids(db)
                delivered_ids = (
                    {
                        row["thread_id"]
                        for row in db.execute(
                            "SELECT DISTINCT thread_id FROM result_deliveries"
                        ).fetchall()
                    }
                    if "result_deliveries" in tables
                    else set()
                )
                attachment_ids = (
                    {
                        row["thread_id"]
                        for row in db.execute(
                            "SELECT DISTINCT thread_id FROM attachments WHERE thread_id IS NOT NULL"
                        ).fetchall()
                    }
                    if "attachments" in tables
                    else set()
                )
                targets = [
                    row["thread_id"]
                    for row in rows
                    if _is_junk_thread_row(
                        row,
                        delivered_ids=delivered_ids,
                        named_ws_ids=named_ws_ids,
                        attachment_ids=attachment_ids,
                        workspace_root=workspace_root,
                    )
                ]
                self._delete_threads_tx(db, targets)
            for tid in targets:
                ws_dir, has_out = _implicit_ws_has_out(workspace_root, tid)
                if ws_dir is not None and not has_out:
                    rmtree_safe(ws_dir, ignore_errors=True)
        return {"deleted": targets, "count": len(targets)}

    @staticmethod
    def _truncate_tool_block(block, *, target_chars: int, archive_seq: int):
        assistant_msg = dict(block[0])
        if isinstance(assistant_msg.get("tool_calls"), list):
            copied_calls = []
            for call in assistant_msg["tool_calls"]:
                if isinstance(call, dict):
                    copied = dict(call)
                    if isinstance(copied.get("function"), dict):
                        copied["function"] = dict(copied["function"])
                    copied_calls.append(copied)
                else:
                    copied_calls.append(call)
            assistant_msg["tool_calls"] = copied_calls
        asst_content = str(assistant_msg.get("content") or "")
        if len(asst_content) > 400:
            assistant_msg["content"] = asst_content[:400] + "..."
        tool_msgs = [dict(m) for m in block[1:]]
        notice = (
            f" ...[工具输出过长已截断，完整内容已归档至 event_seq={archive_seq}；"
            "请直接基于已有结果继续推进，切勿重复调用同一工具，"
            f"如需查阅后续片段可调 thread_read(event_seq={archive_seq}, offset=...)]"
        )
        parsed = []
        for m in tool_msgs:
            raw = str(m.get("content") or "")
            if raw.startswith("<external_data>") and raw.endswith("</external_data>"):
                prefix, suffix = "<external_data>", "</external_data>"
                inner = raw[len(prefix) : -len(suffix)]
            else:
                prefix, suffix = "", ""
                inner = raw
            parsed.append([dict(m), prefix, suffix, inner, raw])

        def _render(caps: list[int | None]) -> list[dict]:
            out = [assistant_msg]
            for idx, (base_msg, prefix, suffix, inner, raw) in enumerate(parsed):
                cap = caps[idx]
                msg = dict(base_msg)
                if cap is None:
                    msg["content"] = raw
                else:
                    trimmed_notice = notice if cap >= len(notice) else notice[:cap]
                    body_cap = max(0, cap - len(trimmed_notice))
                    msg["content"] = f"{prefix}{inner[:body_cap]}{trimmed_notice}{suffix}"
                out.append(msg)
            return out

        caps: list[int | None] = [None] * len(parsed)
        encoded_len = len(encode(_render(caps)))
        if encoded_len <= target_chars:
            return _render(caps)

        # 若 assistant tool_calls arguments 本身过大导致无法在 target_chars 内保留工具输出，
        # 在保持 call id / function.name / tool_call_id 配对不变的前提下把超大 arguments 折叠为有效 JSON。
        calls_list = assistant_msg.get("tool_calls")
        if isinstance(calls_list, list) and len(encode(_render([0] * len(parsed)))) > target_chars:
            indexed_calls = []
            for c_idx, call in enumerate(calls_list):
                if isinstance(call, dict) and isinstance(call.get("function"), dict):
                    raw_args = call["function"].get("arguments")
                    args_str = raw_args if isinstance(raw_args, str) else encode(raw_args)
                    indexed_calls.append((len(args_str), c_idx, call["function"]))
            indexed_calls.sort(key=lambda item: item[0], reverse=True)
            for args_len, _, fn_dict in indexed_calls:
                if len(encode(_render([0] * len(parsed)))) <= target_chars:
                    break
                if args_len > 120:
                    fn_dict["arguments"] = encode(
                        {"_truncated": True, "archive_seq": archive_seq}
                    )
            if len(encode(_render([0] * len(parsed)))) > target_chars:
                cur_asst_text = str(assistant_msg.get("content") or "")
                if len(cur_asst_text) > 80:
                    assistant_msg["content"] = cur_asst_text[:80] + "..."

        # Estimate available raw inner chars after assistant_msg, per-message overhead, and notices
        skeleton_caps = [0] * len(parsed)
        skeleton_len = len(encode(_render(skeleton_caps)))
        avail_inner = max(0, target_chars - skeleton_len)
        # Water-fill across oversized messages proportional/uniformly
        raw_lengths = [len(entry[3]) + len(notice) for entry in parsed]
        n = len(parsed)
        if n > 0:
            # Start with generous per-item allocation and water-fill unused budget
            caps = [min(raw_lengths[i], max(len(notice), avail_inner // n + len(notice))) for i in range(n)]
            unused = max(0, avail_inner - sum(max(0, c - len(notice)) for c in caps))
            if unused > 0:
                for i in range(n):
                    need = max(0, raw_lengths[i] - (caps[i] or 0))
                    add = min(unused, need)
                    caps[i] = (caps[i] or 0) + add
                    unused -= add

        # Refine caps so encode(rendered) <= target_chars even with JSON escaping
        for _ in range(12):
            rendered = _render(caps)
            cur_len = len(encode(rendered))
            if cur_len <= target_chars:
                return rendered
            excess = cur_len - target_chars
            # Shrink the largest capped/uncapped tool entry
            largest_idx = max(range(n), key=lambda i: len(rendered[1 + i].get("content", "")))
            cur_raw_len = len(parsed[largest_idx][3]) + len(notice)
            prev_cap = caps[largest_idx] if caps[largest_idx] is not None else cur_raw_len
            step = max(16, (excess + 1) // 2 if excess > 64 else excess)
            new_cap = max(0, prev_cap - step)
            if new_cap == prev_cap:
                break
            caps[largest_idx] = new_cap
        return _render(caps)

    def prune_history(
        self,
        thread_id,
        chat_id,
        *,
        tail_tokens=DEFAULT_TAIL_TOKENS,
        min_prune_chars=DEFAULT_MIN_PRUNE_CHARS,
        min_reclaim_tokens=PRUNE_MIN_RECLAIM_TOKENS,
    ) -> bool:
        """Tier-1 零成本裁剪：归档并折叠保护区之前的重复或超大旧工具输出。"""
        row = self.get(thread_id, chat_id)
        history = row["history"]
        _, stats = prune_tool_outputs(
            history,
            tail_tokens=tail_tokens,
            min_prune_chars=min_prune_chars,
            event_seq="__EVENT_SEQ__",
        )
        if (
            not stats["pruned_entries"]
            or stats["reclaimed_tokens"] < min_reclaim_tokens
        ):
            return False
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute(
                "SELECT history, processed_revision, state FROM agent_threads WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if cur is None or cur["state"] in {"closed", "archived"}:
                return False
            cur_history = json.loads(cur["history"] or "[]")
            _, cur_stats = prune_tool_outputs(
                cur_history,
                tail_tokens=tail_tokens,
                min_prune_chars=min_prune_chars,
                event_seq="__EVENT_SEQ__",
            )
            if (
                not cur_stats["pruned_entries"]
                or cur_stats["reclaimed_tokens"] < min_reclaim_tokens
            ):
                return False
            archive_seq = self.event(
                db,
                thread_id,
                "context_prune",
                cur["processed_revision"],
                {
                    "reclaimed_tokens": cur_stats["reclaimed_tokens"],
                    "protect_from": cur_stats["protect_from"],
                    "items": cur_stats["pruned_entries"],
                },
            )
            final_history, _ = prune_tool_outputs(
                cur_history,
                tail_tokens=tail_tokens,
                min_prune_chars=min_prune_chars,
                event_seq=archive_seq,
            )
            db.execute(
                'UPDATE agent_threads SET history=? WHERE thread_id=? AND state NOT IN ("closed", "archived")',
                (encode(final_history), thread_id),
            )
        return True

    @staticmethod
    def _group_history_blocks(history: list[dict]) -> list[list[dict]]:
        blocks: list[list[dict]] = []
        for message in history:
            if (
                message.get("role") == "tool"
                and blocks
                and blocks[-1][0].get("tool_calls")
            ):
                blocks[-1].append(message)
            else:
                blocks.append([message])
        return blocks

    @staticmethod
    def _parse_prior_summary(summary_raw: str | None) -> tuple[list[int], list[dict], str, dict, list[str]]:
        prior_events: list[int] = []
        prior_runs: list[dict] = []
        prior_handoff = ""
        prior_anchors: dict = {}
        prior_user_msgs: list[str] = []
        if isinstance(summary_raw, str) and summary_raw.strip():
            try:
                prev_summary = json.loads(summary_raw)
            except ValueError:
                prev_summary = None
            if isinstance(prev_summary, dict):
                raw_events = prev_summary.get("archive_events")
                if isinstance(raw_events, list):
                    for item in raw_events:
                        if isinstance(item, int) and not isinstance(item, bool):
                            prior_events.append(item)
                elif isinstance(prev_summary.get("archive_event"), int) and not isinstance(
                    prev_summary.get("archive_event"), bool
                ):
                    prior_events.append(prev_summary["archive_event"])
                raw_runs = prev_summary.get("archived_runs")
                if isinstance(raw_runs, list):
                    prior_runs = [item for item in raw_runs if isinstance(item, dict)]
                if isinstance(prev_summary.get("handoff"), str):
                    prior_handoff = prev_summary["handoff"]
                if isinstance(prev_summary.get("anchors"), dict):
                    prior_anchors = prev_summary["anchors"]
                if isinstance(prev_summary.get("user_messages"), list):
                    prior_user_msgs = [
                        str(x) for x in prev_summary["user_messages"] if isinstance(x, str)
                    ]
        return prior_events, prior_runs, prior_handoff, prior_anchors, prior_user_msgs

    def _collect_run_digest(self, blocks: list[list[dict]], *, skip_indices: set[int]) -> tuple[list[str], list[str], list[str]]:
        goals: list[str] = []
        tools: list[str] = []
        highlights: list[str] = []
        for index, block in enumerate(blocks):
            if index in skip_indices:
                continue
            for msg in block:
                role = msg.get("role")
                if role == "user":
                    raw_text = str(msg.get("content") or "")
                    if raw_text.startswith(
                        (
                            "<thread_inputs>",
                            "<thread_history>",
                            "<memory_context>",
                            "<activity_interrupted>",
                            "<previous_runs>",
                        )
                    ):
                        continue
                    snippet = _clean_input_title(raw_text)
                    if snippet and snippet not in goals and len(goals) < 3:
                        goals.append(snippet)
                elif role == "assistant":
                    for call in msg.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        fn = call.get("function") or {}
                        if not isinstance(fn, dict):
                            fn = {}
                        tool_name = str(
                            fn.get("name") or call.get("func_name") or ""
                        ).strip()[:64]
                        if tool_name and tool_name not in tools and len(tools) < 8:
                            tools.append(tool_name)
                    asst_text = re.sub(
                        r"\s+", " ", str(msg.get("content") or "")
                    ).strip()
                    if asst_text:
                        highlights.append(asst_text[:100])
        return goals, tools, highlights[-2:]

    def _compact_by_tokens(
        self,
        row: dict,
        thread_id: str,
        *,
        max_tokens: int,
        tail_tokens: int | None,
        handoff_summary: str | None,
        redact,
        force: bool,
    ) -> str:
        history = row["history"]
        cur_tokens = estimate_messages_tokens(history)
        if not force and cur_tokens <= max_tokens:
            return row["summary"]
        blocks = self._group_history_blocks(history)
        if not blocks:
            return row["summary"]

        base_tail = (
            int(tail_tokens)
            if tail_tokens is not None
            else min(24_000, max(1_200, int(max_tokens * 0.15)))
        )
        if force:
            tail_budget = min(base_tail, max(200, cur_tokens // 3))
        else:
            tail_budget = max(400, base_tail)

        # 计算末尾保留区：仅保留 tail_budget 内的尾部消息块（并始终保留最后一块，紧急超限且仅 1 块时下文兜底裁剪）
        tail_start = len(blocks)
        acc = 0
        for idx in range(len(blocks) - 1, -1, -1):
            blk_tokens = estimate_messages_tokens(blocks[idx])
            if idx < len(blocks) - 1 and acc + blk_tokens > tail_budget:
                break
            acc += blk_tokens
            tail_start = idx
        if force and tail_start == 0 and len(blocks) > 1:
            tail_start = len(blocks) - 1

        selected = set(range(tail_start, len(blocks)))
        archived_blocks = [blocks[idx] for idx in range(tail_start)]
        archived_messages = [msg for blk in archived_blocks for msg in blk]

        with self.connect() as db, db:
            archive_seq = self.event(
                db,
                thread_id,
                "context_archive",
                row["processed_revision"],
                {"history": history, "previous_index": row["summary"]},
            )
            truncated_last = None
            last_idx = len(blocks) - 1
            retained_tokens_limit = max(512, tail_budget)
            if (
                len(selected) == 1
                and last_idx in selected
                and estimate_messages_tokens(blocks[last_idx]) > retained_tokens_limit
            ):
                has_tools = bool(
                    blocks[last_idx][0].get("tool_calls")
                    and any(m.get("role") == "tool" for m in blocks[last_idx])
                )
                if has_tools:
                    truncated_last = self._truncate_tool_block(
                        blocks[last_idx],
                        target_chars=max(512, retained_tokens_limit * 2),
                        archive_seq=archive_seq,
                    )
                else:
                    per_block_chars = max(512, retained_tokens_limit * 2)
                    msg_count = max(1, len(blocks[last_idx]))
                    per_msg_budget = max(240, (per_block_chars - 120 * msg_count) // msg_count)
                    half = min(600, max(100, per_msg_budget // 2))
                    min_truncate_len = half * 2 + 120
                    tag = "紧急" if force else "上下文"
                    trimmed_blk = []
                    for msg in blocks[last_idx]:
                        copied = dict(msg)
                        raw_c = copied.get("content")
                        if isinstance(raw_c, str) and len(raw_c) > min_truncate_len:
                            copied["content"] = (
                                raw_c[:half]
                                + f"\n...[{tag}压缩已截断，完整原文见 event_seq={archive_seq}]...\n"
                                + raw_c[-half:]
                            )
                        trimmed_blk.append(copied)
                    truncated_last = trimmed_blk

            retained = []
            for idx in range(tail_start, len(blocks)):
                if idx == last_idx and truncated_last is not None:
                    retained.extend(truncated_last)
                else:
                    retained.extend(blocks[idx])

            (
                prior_events,
                prior_runs,
                prior_handoff,
                prior_anchors,
                prior_user_msgs,
            ) = self._parse_prior_summary(row.get("summary"))

            goals, tools, highlights = self._collect_run_digest(
                blocks, skip_indices=selected
            )
            merged_anchors = extract_exact_anchors(
                archived_messages, prior_anchors=prior_anchors, redact=redact
            )
            merged_user_msgs = extract_verbatim_user_messages(
                archived_messages, prior_messages=prior_user_msgs, redact=redact
            )
            if isinstance(handoff_summary, str) and handoff_summary.strip():
                final_handoff = str(
                    redact(handoff_summary.strip()) if redact else handoff_summary.strip()
                )[:3000]
            else:
                final_handoff = build_static_handoff(
                    archived_messages,
                    previous_handoff=prior_handoff,
                    anchors=merged_anchors,
                    user_messages=merged_user_msgs,
                    tools=tools,
                    highlights=highlights,
                    redact=redact,
                )

            run_entry = {
                "archive_event": archive_seq,
                "revision": row["processed_revision"],
                "goals": goals,
                "tools": tools,
                "highlights": highlights,
            }
            archive_events = [x for x in prior_events if x != archive_seq] + [
                archive_seq
            ]
            archive_events = archive_events[-20:]
            archived_runs = [
                r for r in prior_runs if r.get("archive_event") != archive_seq
            ] + [run_entry]
            summary_obj = {
                "mode": "token_v2",
                "archive_event": archive_seq,
                "archive_events": archive_events,
                "read_tool": "thread_read",
                "handoff": final_handoff,
                "anchors": merged_anchors,
                "user_messages": merged_user_msgs,
                "note": "这是交班摘要与精确锚点；完整历史保存在本线程，核对全文或未折叠细节时使用 thread_read。",
                "archived_runs": archived_runs,
            }
            while len(encode(summary_obj)) > 5600 and len(summary_obj["archived_runs"]) > 0:
                summary_obj["archived_runs"].pop(0)
            summary = encode(summary_obj)
            db.execute(
                'UPDATE agent_threads SET summary=?, history=? WHERE thread_id=? AND state NOT IN ("closed", "archived")',
                (summary, encode(retained), thread_id),
            )
        return summary

    def compact(
        self,
        thread_id,
        chat_id,
        *,
        max_chars=48000,
        max_tokens=None,
        tail_tokens=None,
        handoff_summary=None,
        redact=None,
        force=False,
    ):
        row = self.get(thread_id, chat_id)
        if max_tokens is not None:
            return self._compact_by_tokens(
                row,
                thread_id,
                max_tokens=max_tokens,
                tail_tokens=tail_tokens,
                handoff_summary=handoff_summary,
                redact=redact,
                force=force,
            )
        history = row["history"]
        if len(encode(history)) <= max_chars:
            return row["summary"]
        blocks = self._group_history_blocks(history)
        user_blocks = [
            index
            for index, block in enumerate(blocks)
            if block[0].get("role") == "user"
        ]
        last_idx = len(blocks) - 1
        has_trailing_tool_block = bool(
            blocks
            and blocks[last_idx][0].get("tool_calls")
            and any(msg.get("role") == "tool" for msg in blocks[last_idx])
        )
        if has_trailing_tool_block:
            priority = (
                ([user_blocks[-1]] if user_blocks else [])
                + [last_idx]
                + ([user_blocks[0]] if user_blocks else [])
                + list(reversed(range(len(blocks))))
            )
        else:
            priority = ([user_blocks[-1], user_blocks[0]] if user_blocks else []) + list(
                reversed(range(len(blocks)))
            )
        selected = set()
        budget = max_chars - min(1000, max_chars // 4)
        size = 2
        stop_optional_blocks = False
        for index in priority:
            if stop_optional_blocks and index not in selected:
                continue
            block_size = len(encode(blocks[index])) + 2
            if index not in selected and size + block_size <= budget:
                selected.add(index)
                size += block_size
            elif has_trailing_tool_block and index == last_idx and last_idx not in selected:
                # Preserve remaining budget for the truncated trailing tool block
                stop_optional_blocks = True
        with self.connect() as db, db:
            archive_seq = self.event(
                db,
                thread_id,
                "context_archive",
                row["processed_revision"],
                {"history": history, "previous_index": row["summary"]},
            )
            truncated_last = None
            if has_trailing_tool_block and last_idx not in selected:
                other_retained = [
                    message
                    for index, block in enumerate(blocks)
                    if index in selected and index != last_idx
                    for message in block
                ]
                other_len = len(encode(other_retained))
                # If user_blocks[-1] itself nearly exhausted budget, drop earlier blocks
                # (keep only user_blocks[-1] or empty if necessary) to guarantee tool block fits
                if budget - other_len < 400 and selected:
                    selected.clear()
                    other_retained = []
                    other_len = 2
                avail_for_block = max(128, max(budget, max_chars - 64) - other_len - 2)
                truncated_last = self._truncate_tool_block(
                    blocks[last_idx],
                    target_chars=avail_for_block,
                    archive_seq=archive_seq,
                )
            retained = []
            for index, block in enumerate(blocks):
                if index == last_idx and truncated_last is not None:
                    retained.extend(truncated_last)
                elif index in selected:
                    retained.extend(block)
            prior_events, prior_runs, _, _, _ = self._parse_prior_summary(
                row.get("summary")
            )
            skip_indices = set(selected)
            if truncated_last is not None:
                skip_indices.add(last_idx)
            goals, tools, highlights = self._collect_run_digest(
                blocks, skip_indices=skip_indices
            )
            run_entry = {
                "archive_event": archive_seq,
                "revision": row["processed_revision"],
                "goals": goals,
                "tools": tools,
                "highlights": highlights,
            }
            archive_events = [x for x in prior_events if x != archive_seq] + [
                archive_seq
            ]
            archive_events = archive_events[-20:]
            archived_runs = [
                r for r in prior_runs if r.get("archive_event") != archive_seq
            ] + [run_entry]
            summary_obj = {
                "archive_event": archive_seq,
                "archive_events": archive_events,
                "read_tool": "thread_read",
                "note": "完整历史保存在本线程。需要更早的事实、决定、理由或出处时，按关键词回读；这是导航索引，不是事实摘要。",
                "archived_runs": archived_runs,
            }
            while (
                len(encode(summary_obj)) > 2400
                and len(summary_obj["archived_runs"]) > 0
            ):
                summary_obj["archived_runs"].pop(0)
            summary = encode(summary_obj)
            db.execute(
                'UPDATE agent_threads SET summary=?, history=? WHERE thread_id=? AND state NOT IN ("closed", "archived")',
                (summary, encode(retained), thread_id),
            )
        return summary

    def read_history(
        self,
        thread_id,
        chat_id,
        *,
        query="",
        event_seq=0,
        offset=0,
        limit=5,
        max_chars=16000,
        allow_self=False,
        allow_any_chat=False,
    ):
        self.get(
            thread_id,
            chat_id,
            allow_self=allow_self,
            allow_any_chat=allow_any_chat,
        )
        if (
            not isinstance(query, str)
            or len(query) > 500
            or type(event_seq) is not int
            or event_seq < 0
            or type(offset) is not int
            or offset < 0
        ):
            raise ValueError("线程回读参数无效")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("回读数量必须在 1 到 10 之间")
        with self.connect() as db:
            if event_seq:
                rows = db.execute(
                    "SELECT seq, kind, payload FROM thread_events WHERE thread_id=? AND seq=?",
                    (thread_id, event_seq),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT seq, kind, payload FROM thread_events WHERE thread_id=? "
                    'AND kind IN ("input", "result", "question", "tools", "context_archive", "context_prune") '
                    "AND instr(lower(payload), lower(?))>0 ORDER BY seq DESC LIMIT ?",
                    (thread_id, query, limit),
                ).fetchall()
        result = []
        chunk_size = max(100, max_chars // max(1, len(rows)))
        for record in rows:
            text = record["payload"]
            start = (
                offset
                if event_seq or offset > 0
                else max(0, text.lower().find(query.lower()) - 160)
                if query
                else 0
            )
            start = min(len(text), start)
            end = min(len(text), start + chunk_size)
            result.append(
                {
                    "event_seq": record["seq"],
                    "kind": record["kind"],
                    "text": text[start:end],
                    "offset": start,
                    "next_offset": end if end < len(text) else None,
                    "total_chars": len(text),
                }
            )
        return result

    def search(
        self, query, chat_id, *, scope="available", limit=5, all_chats=False, chat_ids=None
    ):
        if (
            scope not in {"chat", "self", "available"}
            or not isinstance(query, str)
            or len(query) > 500
        ):
            raise ValueError("检索参数无效")
        limit = max(1, min(int(limit), 10))
        fetch_limit = min(50, limit * 5)
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)[:12]
        match = " AND ".join('"' + token + '"*' for token in tokens)
        viewer_aliases = [c for c in {chat_id, *(chat_ids or ())} if isinstance(c, str) and c]
        if not all_chats and chat_id and callable(getattr(self, "chat_aliases", None)):
            try:
                for alias in self.chat_aliases(chat_id, ""):
                    if isinstance(alias, str) and alias and alias not in viewer_aliases:
                        viewer_aliases.append(alias)
            except (TypeError, ValueError):
                pass
        if all_chats:
            scopes = {
                "chat": 't.owner="chat"',
                "self": 't.owner="self"',
                "available": "1=1",
            }
            parameters = []
        elif chat_ids:
            cids = sorted(
                {c for c in [chat_id, *(chat_ids or ())] if isinstance(c, str) and c}
            )
            if len(cids) > 1:
                placeholders = ",".join("?" for _ in cids)
                scopes = {
                    "chat": f'(t.owner="chat" AND t.chat_id IN ({placeholders}))',
                    "self": 't.owner="self"',
                    "available": f'((t.owner="chat" AND t.chat_id IN ({placeholders})) OR t.owner="self")',
                }
                parameters = [] if scope == "self" else list(cids)
            else:
                target_cid = cids[0] if cids else chat_id
                scopes = {
                    "chat": '(t.owner="chat" AND t.chat_id=?)',
                    "self": 't.owner="self"',
                    "available": '((t.owner="chat" AND t.chat_id=?) OR t.owner="self")',
                }
                parameters = [] if scope == "self" else [target_cid]
        else:
            scopes = {
                "chat": '(t.owner="chat" AND t.chat_id=?)',
                "self": 't.owner="self"',
                "available": '((t.owner="chat" AND t.chat_id=?) OR t.owner="self")',
            }
            parameters = [] if scope == "self" else [chat_id]
        with self.connect() as db:
            if query and not match:
                return []
            if re.fullmatch(r"th_[a-f0-9]{16}", query):
                rows = db.execute(
                    "SELECT t.* FROM agent_threads t WHERE t.thread_id=? AND "
                    + scopes[scope]
                    + " LIMIT ?",
                    [query, *parameters, fetch_limit],
                ).fetchall()
            elif re.search(r"[\u3400-\u9fff]", query):
                conditions = " AND ".join("f.text LIKE ?" for _ in tokens)
                rows = db.execute(
                    "SELECT t.* FROM agent_threads t JOIN thread_search f ON t.thread_id=f.thread_id WHERE "
                    + conditions
                    + " AND "
                    + scopes[scope]
                    + " ORDER BY t.updated DESC LIMIT ?",
                    [*["%" + token + "%" for token in tokens], *parameters, fetch_limit],
                ).fetchall()
            elif match:
                rows = db.execute(
                    "SELECT t.* FROM agent_threads t JOIN thread_search f ON t.thread_id=f.thread_id "
                    "WHERE thread_search MATCH ? AND "
                    + scopes[scope]
                    + " ORDER BY t.updated DESC LIMIT ?",
                    [match, *parameters, fetch_limit],
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT t.* FROM agent_threads t WHERE "
                    + scopes[scope]
                    + " ORDER BY t.updated DESC LIMIT ?",
                    [*parameters, fetch_limit],
                ).fetchall()
            found = []
            for row in rows:
                r_vis = str(
                    (row["visibility_scope"] if "visibility_scope" in row.keys() else "")
                    or initial_thread_scope(owner=row["owner"], chat_id=row["chat_id"])
                )
                if not can_read_scope(
                    r_vis,
                    viewer_chat=chat_id,
                    is_admin=all_chats,
                    aliases=viewer_aliases,
                ):
                    continue
                if len(found) >= limit:
                    break
                tid = row["thread_id"]
                ws_row = db.execute(
                    "SELECT w.name FROM thread_context c JOIN agent_workspaces w "
                    "ON w.workspace_id=c.workspace_id WHERE c.thread_id=?",
                    (tid,),
                ).fetchone()
                ws_name = str(ws_row["name"]) if ws_row and ws_row["name"] else ""
                first_in = db.execute(
                    'SELECT payload FROM thread_events WHERE thread_id=? AND kind="input" '
                    "ORDER BY seq ASC LIMIT 1",
                    (tid,),
                ).fetchone()
                title = ""
                if first_in:
                    try:
                        title = _clean_input_title(
                            json.loads(first_in["payload"]).get("text", "")
                        )
                    except ValueError:
                        title = ""
                if not title:
                    fts_row = db.execute(
                        "SELECT text FROM thread_search WHERE thread_id=? LIMIT 1",
                        (tid,),
                    ).fetchone()
                    if fts_row and fts_row["text"]:
                        title = _clean_input_title(str(fts_row["text"]))
                item = {
                    "thread_id": tid,
                    "profile": row["profile"],
                    "owner": row["owner"],
                    "chat_id": row["chat_id"],
                    "state": row["state"],
                    "title": title,
                    "workspace": ws_name,
                    "result": json.loads(row["result"]),
                }
                if row["state"] in {"active", "waiting"}:
                    latest_ev = db.execute(
                        "SELECT kind, revision, payload FROM thread_events WHERE thread_id=? "
                        'AND kind IN ("tools", "question", "redirect", "input") '
                        "ORDER BY seq DESC LIMIT 1",
                        (tid,),
                    ).fetchone()
                    latest_step = ""
                    if latest_ev:
                        ev_kind = latest_ev["kind"]
                        try:
                            ev_payload = json.loads(latest_ev["payload"])
                        except ValueError:
                            ev_payload = {}
                        if ev_kind == "tools":
                            step_count = db.execute(
                                'SELECT COUNT(*) AS n FROM thread_events WHERE thread_id=? AND revision=? AND kind="tools"',
                                (tid, latest_ev["revision"]),
                            ).fetchone()["n"]
                            summary_calls = _summarize_tool_calls(
                                ev_payload.get("calls") if isinstance(ev_payload, dict) else []
                            )
                            latest_step = (
                                f"第 {step_count} 步：{summary_calls}"
                                if summary_calls
                                else f"第 {step_count} 步执行中"
                            )
                        elif ev_kind == "question":
                            q_text = (
                                str(ev_payload.get("question") or "")[:60]
                                if isinstance(ev_payload, dict)
                                else ""
                            )
                            latest_step = f"等待补充：{q_text}" if q_text else "等待补充核心输入"
                        elif ev_kind == "redirect":
                            latest_step = "已收到转向补充，正在按新方向推进"
                        elif ev_kind == "input":
                            latest_step = "已启动，正在规划第一步"
                    if latest_step:
                        item["latest_step"] = latest_step
                found.append(item)
        if query and not re.fullmatch(r"th_[a-f0-9]{16}", query):
            for item in found:
                item["matches"] = self.read_history(
                    item["thread_id"],
                    item["chat_id"] if item["owner"] == "chat" else chat_id,
                    query=query,
                    limit=2,
                    max_chars=1800,
                    allow_self=True,
                    allow_any_chat=True,
                )
        return found

    def list_threads(
        self,
        *,
        chat_id: str = "",
        profile: str = "",
        state: str | tuple[str, ...] | list[str] | set[str] = "",
        keyword: str = "",
        since: float | None = None,
        until: float | None = None,
        owner: str = "",
        scope: str = "chat",
        limit: int = 50,
    ) -> list[dict]:
        """按会话、档案、状态、关键词与时间窗口检索线程及归档记录。"""
        limit = max(1, min(int(limit or 50), 200))
        where: list[str] = []
        params: list[object] = []

        if scope == "self":
            where.append('t.owner="self"')
        elif chat_id:
            cids = [chat_id]
            if callable(getattr(self, "chat_aliases", None)):
                try:
                    cids = sorted(set(self.chat_aliases(chat_id, "")) | {chat_id})
                except (TypeError, ValueError):
                    cids = [chat_id]
            if scope == "available":
                if len(cids) > 1:
                    placeholders = ",".join("?" for _ in cids)
                    where.append(f'((t.owner="chat" AND t.chat_id IN ({placeholders})) OR t.owner="self")')
                    params.extend(cids)
                else:
                    where.append('((t.owner="chat" AND t.chat_id=?) OR t.owner="self")')
                    params.append(cids[0])
            else:
                if len(cids) > 1:
                    placeholders = ",".join("?" for _ in cids)
                    where.append(f'(t.owner="chat" AND t.chat_id IN ({placeholders}))')
                    params.extend(cids)
                else:
                    where.append('(t.owner="chat" AND t.chat_id=?)')
                    params.append(cids[0])

        if owner in {"chat", "self"}:
            where.append("t.owner=?")
            params.append(owner)
        if profile:
            where.append("t.profile=?")
            params.append(profile)
        if state:
            states = (
                [state]
                if isinstance(state, str)
                else [str(s) for s in state if str(s)]
            )
            if len(states) == 1:
                where.append("t.state=?")
                params.append(states[0])
            elif states:
                placeholders = ",".join("?" for _ in states)
                where.append(f"t.state IN ({placeholders})")
                params.extend(states)
        if since is not None:
            where.append("t.updated>=?")
            params.append(float(since))
        if until is not None:
            where.append("t.updated<=?")
            params.append(float(until))

        kw = str(keyword or "").strip()
        join_clause = ""
        if kw:
            if re.fullmatch(r"th_[a-f0-9]{16}", kw):
                where.append("t.thread_id=?")
                params.append(kw)
            else:
                join_clause = " LEFT JOIN thread_search f ON t.thread_id=f.thread_id"
                tokens = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", kw)
                if tokens:
                    for token in tokens:
                        like_val = f"%{token}%"
                        where.append(
                            "(COALESCE(f.text, '') LIKE ? OR t.history LIKE ? OR t.result LIKE ?)"
                        )
                        params.extend([like_val, like_val, like_val])
                else:
                    like_val = f"%{kw}%"
                    where.append(
                        "(COALESCE(f.text, '') LIKE ? OR t.history LIKE ? OR t.result LIKE ?)"
                    )
                    params.extend([like_val, like_val, like_val])

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            f"SELECT t.* FROM agent_threads t{join_clause}{where_sql} "
            "ORDER BY t.updated DESC LIMIT ?"
        )
        params.append(limit)
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
            items: list[dict] = []
            for row in rows:
                tid = row["thread_id"]
                ws_row = db.execute(
                    "SELECT w.name FROM thread_context c JOIN agent_workspaces w "
                    "ON w.workspace_id=c.workspace_id WHERE c.thread_id=?",
                    (tid,),
                ).fetchone()
                ws_name = str(ws_row["name"]) if ws_row and ws_row["name"] else ""
                first_in = db.execute(
                    'SELECT payload FROM thread_events WHERE thread_id=? AND kind="input" ORDER BY seq ASC LIMIT 1',
                    (tid,),
                ).fetchone()
                title = ""
                if first_in:
                    try:
                        title = _clean_input_title(
                            json.loads(first_in["payload"]).get("text", "")
                        )
                    except ValueError:
                        title = ""
                if not title:
                    search_row = db.execute(
                        "SELECT text FROM thread_search WHERE thread_id=?",
                        (tid,),
                    ).fetchone()
                    if search_row and search_row["text"]:
                        first_line = str(search_row["text"]).splitlines()[0]
                        title = _clean_input_title(first_line)
                items.append(
                    {
                        "thread_id": tid,
                        "profile": row["profile"],
                        "owner": row["owner"],
                        "chat_id": row["chat_id"],
                        "state": row["state"],
                        "title": title,
                        "workspace": ws_name,
                        "result": json.loads(row["result"]),
                        "updated": row["updated"],
                    }
                )
        return items


class ThreadEngine:
    def __init__(
        self,
        store,
        runtime,
        *,
        idle_seconds=900,
        max_steps=16,
        timeout=180,
        max_active=8,
        context_chars=None,
        context_tokens=None,
        model_catalog=None,
        redact=lambda value: value,
        intents=None,
        serendipity=None,
    ):
        self.store = store
        self.runtime = runtime
        self.idle_seconds = idle_seconds
        self.max_steps = max_steps
        self.timeout = timeout
        self.max_active = max_active
        self._legacy_char_budget = (
            int(context_chars)
            if context_chars is not None and context_tokens is None
            else None
        )
        self.context_chars = int(context_chars) if context_chars is not None else 48000
        self.context_tokens = (
            int(context_tokens)
            if context_tokens is not None
            else (
                max(MIN_CONTEXT_TOKENS, self._legacy_char_budget // 3)
                if self._legacy_char_budget is not None
                else DEFAULT_CONTEXT_TOKENS
            )
        )
        self.model_catalog = model_catalog
        self.redact = redact
        self.intents = intents
        self.serendipity = serendipity
        self.tasks = {}
        self.background_tasks: set[str] = set()
        self.chats = {}
        self.locks = {}
        self.waiters = {}
        self._finishing: set[str] = set()
        self._deferred_queue: list[tuple[str, str, bool]] = []
        self._starting = 0
        self._starting_bg = 0
        self._submission_lock = asyncio.Lock()
        self.closing = False

    def _background_limit(self) -> int:
        if self.max_active >= 4:
            return self.max_active - 2
        if self.max_active == 3:
            return 2
        return self.max_active

    def _has_capacity(
        self,
        *,
        background: bool,
        free_total: int = 0,
        free_bg: int = 0,
    ) -> bool:
        total = max(0, len(self.tasks) - free_total) + self._starting
        if total >= self.max_active:
            return False
        if background:
            bg_active = max(0, len(self.background_tasks) - free_bg) + self._starting_bg
            return bg_active < self._background_limit() and total < self.max_active
        return total < self.max_active

    def _enqueue_deferred(
        self, thread_id: str, run_chat: str, *, background: bool
    ) -> None:
        for idx, (tid, cur_chat, cur_bg) in enumerate(self._deferred_queue):
            if tid == thread_id:
                self._deferred_queue[idx] = (
                    tid,
                    run_chat or cur_chat,
                    cur_bg and background,
                )
                return
        self._deferred_queue.append((thread_id, run_chat, background))

    def _drop_deferred(self, thread_id: str) -> None:
        self._deferred_queue = [
            entry for entry in self._deferred_queue if entry[0] != thread_id
        ]

    def _cleanup_waiter(self, thread_id: str, target_future) -> None:
        items = self.waiters.get(thread_id)
        if items:
            remaining = [
                (rev, fut)
                for rev, fut in items
                if fut is not target_future and not fut.done()
            ]
            if remaining:
                self.waiters[thread_id] = remaining
                return
            self.waiters.pop(thread_id, None)
        self._prune_idle_state_locked(thread_id)

    def _prune_idle_state_locked(self, thread_id: str) -> None:
        """清理已完全空闲线程的残余内存字典条目（无运行、延迟或等待任务时生效）。"""
        if thread_id in self.tasks:
            return
        if any(tid == thread_id for tid, _, _ in self._deferred_queue):
            return
        if self.waiters.get(thread_id):
            return
        self.waiters.pop(thread_id, None)
        self.chats.pop(thread_id, None)
        lock_obj = self.locks.get(thread_id)
        if lock_obj is not None and not lock_obj.locked():
            self.locks.pop(thread_id, None)

    async def _drain_deferred_locked(
        self, *, free_total: int = 0, free_bg: int = 0
    ) -> None:
        if self.closing or not self._deferred_queue:
            return
        remaining = []
        for idx, (tid, queued_chat, entry_bg) in enumerate(self._deferred_queue):
            if self.closing:
                remaining.extend(self._deferred_queue[idx:])
                break
            if tid in self.tasks:
                if not entry_bg:
                    self.background_tasks.discard(tid)
                continue
            if not self._has_capacity(
                background=entry_bg, free_total=free_total, free_bg=free_bg
            ):
                remaining.append((tid, queued_chat, entry_bg))
                continue
            try:
                row = await asyncio.to_thread(
                    self.store.get,
                    tid,
                    queued_chat,
                    allow_self=True,
                    allow_any_chat=True,
                )
            except (ValueError, PermissionError):
                continue
            if (
                row["state"] in {"closed", "archived"}
                or row["processed_revision"] >= row["revision"]
            ):
                continue
            run_chat = row["chat_id"] if row["owner"] != "self" else "self"
            if row["state"] != "active":
                activated = await asyncio.to_thread(self.store.activate, tid)
                if not activated:
                    continue
            self.chats[tid] = run_chat
            if entry_bg:
                self.background_tasks.add(tid)
            self.tasks[tid] = asyncio.create_task(self._drive(tid, run_chat))
        self._deferred_queue = remaining

    async def recover(self):
        await asyncio.to_thread(self.store.recover)

    async def _send_locked(
        self,
        thread_id,
        message,
        *,
        chat_id,
        create_waiter: bool = True,
        return_route=None,
        input_context=None,
        steer: bool = False,
        background: bool = False,
        defer_if_full: bool = False,
        auto_drain: bool = False,
    ):
        row = await asyncio.to_thread(self.store.get, thread_id, chat_id)
        deferred = False
        if thread_id not in self.tasks and not self._has_capacity(
            background=background
        ):
            if not defer_if_full:
                raise ValueError("当前 Agent 线程已满，请稍后再试")
            deferred = True
        if row["revision"] - row["processed_revision"] >= 32:
            raise ValueError("该线程正在消化已有补充，请稍后再发")
        revision = await asyncio.to_thread(
            self.store.enqueue,
            thread_id,
            self.redact(message),
            chat_id,
            return_route,
            input_context,
        )
        if deferred:
            if auto_drain:
                run_chat = row["chat_id"] if row["owner"] != "self" else "self"
                self._enqueue_deferred(thread_id, run_chat, background=background)
            return public(await asyncio.to_thread(self.store.get, thread_id, chat_id)), None
        self._drop_deferred(thread_id)
        future = None
        if create_waiter:
            future = asyncio.get_running_loop().create_future()
            self.waiters.setdefault(thread_id, []).append((revision, future))
        if (
            steer
            and row["state"] == "active"
            and thread_id in self.tasks
            and thread_id not in self._finishing
        ):
            self.tasks[thread_id].cancel()
        if not background and thread_id in self.tasks:
            self.background_tasks.discard(thread_id)
        if thread_id not in self.tasks:
            self.chats[thread_id] = chat_id
            if background:
                self.background_tasks.add(thread_id)
            self.tasks[thread_id] = asyncio.create_task(
                self._drive(thread_id, chat_id)
            )
        return public(await asyncio.to_thread(self.store.get, thread_id, chat_id)), future

    async def start(
        self,
        profile,
        message,
        *,
        chat_id,
        platform,
        user_id,
        owner="chat",
        wait_seconds=20,
        return_route=None,
        workspace="",
        grant=None,
        references=None,
        from_threads=None,
        background: bool = False,
        defer_if_full: bool = False,
        auto_drain: bool = False,
        fresh: bool = False,
        reuse_primary: bool | None = None,
    ):
        self._validate_input(message, wait_seconds)
        is_persistent = (
            bool(reuse_primary)
            if reuse_primary is not None
            else getattr(profile, "session_mode", "ephemeral") == "persistent"
        )
        grant_dict = grant if isinstance(grant, dict) else {}
        is_admin_caller = bool(
            grant_dict.get("admin_verified")
            or grant_dict.get("scheduled_admin")
            or grant_dict.get("console_admin")
            or grant_dict.get("admin_direct")
            or grant_dict.get("issuer_admin")
        )
        effective_grant = dict(grant_dict)
        if from_threads:
            inherited_scopes = []
            allow_cross = bool(
                effective_grant.get("scheduled_admin")
                or effective_grant.get("admin_direct")
                or effective_grant.get("console_admin")
                or (owner == "self" and effective_grant.get("admin_verified"))
            )
            for prev_id in from_threads:
                try:
                    prev_row = await asyncio.to_thread(
                        self.store.get,
                        prev_id,
                        chat_id,
                        allow_self=True,
                        allow_any_chat=allow_cross,
                    )
                except (KeyError, PermissionError, ValueError):
                    continue
                inherited_scopes.append(
                    str(
                        prev_row.get("visibility_scope")
                        or initial_thread_scope(
                            owner=prev_row.get("owner") or "chat",
                            chat_id=prev_row.get("chat_id") or "",
                        )
                    )
                )
            if inherited_scopes:
                base_scope = str(
                    effective_grant.get("visibility_scope")
                    or initial_thread_scope(owner=owner, chat_id=chat_id)
                )
                effective_grant["visibility_scope"] = merge_scopes(
                    [base_scope, *inherited_scopes]
                )
        if is_persistent and fresh:
            existing_pre = await asyncio.to_thread(
                self.store.find_primary_thread,
                profile.id,
                chat_id,
                owner=owner,
                is_admin=is_admin_caller,
            )
            if existing_pre is not None and existing_pre["thread_id"] in self.tasks:
                await self.stop(
                    existing_pre["thread_id"],
                    chat_id=existing_pre["chat_id"],
                )
        row = None
        future = None
        result = None
        reused_existing = False
        async with self._submission_lock:
            if self.closing:
                raise ValueError("当前 Agent 线程已满，请稍后再试")
            existing = (
                await asyncio.to_thread(
                    self.store.find_primary_thread,
                    profile.id,
                    chat_id,
                    owner=owner,
                    is_admin=is_admin_caller,
                )
                if is_persistent
                else None
            )
            if existing is not None and fresh:
                await asyncio.to_thread(
                    self.store.archive_thread,
                    existing["thread_id"],
                    existing["chat_id"],
                    allow_self=(owner == "self"),
                )
                existing = None

            if existing is not None:
                reused_existing = True
                row = existing
                tid = row["thread_id"]
                await asyncio.to_thread(
                    self.store.update_profile_snapshot, tid, profile, row["chat_id"]
                )
                if effective_grant.get("visibility_scope"):
                    await asyncio.to_thread(
                        self.store.narrow_scope,
                        tid,
                        effective_grant["visibility_scope"],
                    )
                try:
                    async with self.locks.setdefault(tid, asyncio.Lock()):
                        result, future = await self._send_locked(
                            tid,
                            message,
                            chat_id=row["chat_id"],
                            create_waiter=bool(wait_seconds),
                            return_route=return_route,
                            input_context={
                                "grant": effective_grant,
                                "references": references or [],
                                "from_threads": from_threads or [],
                            },
                            background=background,
                            defer_if_full=defer_if_full,
                            auto_drain=auto_drain,
                        )
                finally:
                    self._prune_idle_state_locked(tid)
            else:
                if not defer_if_full and not self._has_capacity(background=background):
                    raise ValueError("当前 Agent 线程已满，请稍后再试")
                target_workspace = workspace or (
                    f"profile_{profile.id}" if is_persistent else ""
                )
                row = await asyncio.to_thread(
                    self.store.create,
                    profile,
                    self.redact(message),
                    chat_id=chat_id,
                    platform=platform,
                    user_id=user_id,
                    owner=owner,
                    workspace=target_workspace,
                    grant=effective_grant,
                    fresh_workspace=bool(is_persistent and fresh),
                )
                tid = row["thread_id"]
                if is_persistent:
                    await asyncio.to_thread(
                        self.store.bind_primary_thread,
                        profile.id,
                        row["chat_id"] if owner != "self" else "self",
                        tid,
                        owner=owner,
                    )
                try:
                    async with self.locks.setdefault(tid, asyncio.Lock()):
                        result, future = await self._send_locked(
                            tid,
                            message,
                            chat_id=row["chat_id"],
                            create_waiter=bool(wait_seconds),
                            return_route=return_route,
                            input_context={
                                "grant": effective_grant,
                                "references": references or [],
                                "from_threads": from_threads or [],
                            },
                            background=background,
                            defer_if_full=defer_if_full,
                            auto_drain=auto_drain,
                        )
                except BaseException:
                    self._prune_idle_state_locked(tid)
                    if not reused_existing:
                        try:
                            await asyncio.to_thread(self.store.delete_thread, tid)
                        except Exception:
                            pass
                    raise
                self._prune_idle_state_locked(tid)
        if wait_seconds and future is not None:
            try:
                return await asyncio.wait_for(asyncio.shield(future), wait_seconds)
            except TimeoutError:
                self._cleanup_waiter(row["thread_id"], future)
            except BaseException:
                self._cleanup_waiter(row["thread_id"], future)
                raise
        return result

    async def resume_thread(
        self,
        thread_id: str,
        message: str = "",
        *,
        chat_id: str,
        wait_seconds=20,
        return_route=None,
        input_context=None,
        steer: bool = False,
        background: bool = False,
        defer_if_full: bool = False,
        auto_drain: bool = False,
    ):
        """从 done / stopped / archived / closed 原地唤醒并可选追加新输入。"""
        if not message or not str(message).strip():
            row = await asyncio.to_thread(self.store.reopen, thread_id, chat_id)
            return public(row)
        return await self.send(
            thread_id,
            message,
            chat_id=chat_id,
            wait_seconds=wait_seconds,
            return_route=return_route,
            input_context=input_context,
            steer=steer,
            background=background,
            defer_if_full=defer_if_full,
            auto_drain=auto_drain,
        )

    @staticmethod
    def _validate_input(message, wait_seconds):
        if not isinstance(message, str) or not message.strip() or len(message) > 16000:
            raise ValueError("线程消息必须为非空文本，且不超过 16000 字符")
        if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 30:
            raise ValueError("等待窗口必须在 0 到 30 秒之间")

    async def send(
        self,
        thread_id,
        message,
        *,
        chat_id,
        wait_seconds=20,
        return_route=None,
        input_context=None,
        steer=False,
        background: bool = False,
        defer_if_full: bool = False,
        auto_drain: bool = False,
    ):
        self._validate_input(message, wait_seconds)
        if type(steer) is not bool:
            raise ValueError("转向标志必须为布尔值")
        input_context = {**(input_context or {}), "steer": steer}
        if self.closing:
            raise ValueError("插件正在关闭")
        future = None
        result = None
        async with self._submission_lock:
            try:
                async with self.locks.setdefault(thread_id, asyncio.Lock()):
                    result, future = await self._send_locked(
                        thread_id,
                        message,
                        chat_id=chat_id,
                        create_waiter=bool(wait_seconds),
                        return_route=return_route,
                        input_context=input_context,
                        steer=steer,
                        background=background,
                        defer_if_full=defer_if_full,
                        auto_drain=auto_drain,
                    )
            finally:
                self._prune_idle_state_locked(thread_id)
        if wait_seconds and future is not None:
            try:
                return await asyncio.wait_for(asyncio.shield(future), wait_seconds)
            except TimeoutError:
                self._cleanup_waiter(thread_id, future)
            except BaseException:
                self._cleanup_waiter(thread_id, future)
                raise
        return result

    async def kick_queued(
        self, thread_id: str, chat_id: str, *, background: bool = True
    ) -> bool:
        async with self._submission_lock:
            self._drop_deferred(thread_id)
            try:
                async with self.locks.setdefault(thread_id, asyncio.Lock()):
                    if self.closing or thread_id in self.tasks:
                        return False
                    try:
                        row = await asyncio.to_thread(
                            self.store.get,
                            thread_id,
                            chat_id,
                            allow_self=True,
                            allow_any_chat=True,
                        )
                    except (ValueError, PermissionError):
                        return False
                    if (
                        row["state"] in {"closed", "archived"}
                        or row["processed_revision"] >= row["revision"]
                    ):
                        return False
                    if not self._has_capacity(background=background):
                        return False
                    run_chat = row["chat_id"] if row["owner"] != "self" else "self"
                    if row["state"] != "active":
                        activated = await asyncio.to_thread(self.store.activate, thread_id)
                        if not activated:
                            return False
                    self.chats[thread_id] = run_chat
                    if background:
                        self.background_tasks.add(thread_id)
                    self.tasks[thread_id] = asyncio.create_task(
                        self._drive(thread_id, run_chat)
                    )
                    return True
            finally:
                self._prune_idle_state_locked(thread_id)

    def _resolve(self, thread_id, row, *, stopped: bool = False):
        waiters = self.waiters.get(thread_id, [])
        if not waiters:
            return
        stopped_now = stopped or (
            row["state"] in {"idle", "archived", "done"}
            and self.store.is_stopped(thread_id, state=row["state"])
        )
        remaining = []
        for revision, future in waiters:
            if stopped_now or revision <= row["completed_revision"] or row["state"] in {"closed", "archived"}:
                if not future.done():
                    future.set_result(public(row))
            else:
                remaining.append((revision, future))
        if remaining:
            self.waiters[thread_id] = remaining
        else:
            self.waiters.pop(thread_id, None)

    def _current_model_catalog(self):
        return self.model_catalog() if callable(self.model_catalog) else self.model_catalog

    async def _run_tier2_compact(
        self,
        thread_id,
        store_chat,
        profile,
        history,
        processed,
        budget,
        *,
        overhead_tokens=0,
        force=False,
    ):
        cur_row = await asyncio.to_thread(self.store.get, thread_id, store_chat)
        cur_tokens = estimate_messages_tokens(history)
        effective_max_tokens = max(
            256, int(budget["trigger_tokens"]) - max(0, int(overhead_tokens or 0))
        )
        if not force and cur_tokens <= effective_max_tokens:
            return cur_row.get("summary") or "", history
        tail_tok = (
            min(budget["tail_tokens"], max(200, cur_tokens // 3))
            if force
            else budget["tail_tokens"]
        )
        blocks = ThreadStore._group_history_blocks(history)
        if not blocks:
            return cur_row.get("summary") or "", history
        tail_start = len(blocks)
        acc = 0
        for idx in range(len(blocks) - 1, -1, -1):
            bt = estimate_messages_tokens(blocks[idx])
            if idx < len(blocks) - 1 and acc + bt > tail_tok:
                break
            acc += bt
            tail_start = idx
        if force and tail_start == 0 and len(blocks) > 1:
            tail_start = len(blocks) - 1
        archived_turns = [m for blk in blocks[:tail_start] for m in blk]
        retained_limit = max(512, max(400, int(tail_tok)) if not force else int(tail_tok))
        can_truncate_single = (
            len(blocks) == 1
            and estimate_messages_tokens(blocks[0]) > retained_limit
        )
        if not archived_turns and not can_truncate_single:
            return cur_row.get("summary") or "", history
        retained_tokens = sum(
            estimate_messages_tokens(blk) for blk in blocks[tail_start:]
        )
        if not can_truncate_single and retained_tokens >= cur_tokens:
            return cur_row.get("summary") or "", history
        await asyncio.to_thread(
            self.store.checkpoint,
            thread_id,
            history,
            processed,
            "context_checkpoint",
            {},
        )
        _, _, prev_handoff, _, _ = ThreadStore._parse_prior_summary(cur_row.get("summary"))
        handoff_summary = None
        if archived_turns and hasattr(self.runtime, "summarize_compaction"):
            try:
                handoff_summary = await self.runtime.summarize_compaction(
                    archived_turns,
                    previous_handoff=prev_handoff,
                    profile=profile,
                )
            except Exception:
                handoff_summary = None
        context_index = await asyncio.to_thread(
            self.store.compact,
            thread_id,
            store_chat,
            max_tokens=effective_max_tokens,
            tail_tokens=tail_tok,
            handoff_summary=handoff_summary,
            redact=self.redact,
            force=force,
        )
        new_history = (
            await asyncio.to_thread(self.store.get, thread_id, store_chat)
        )["history"]
        return context_index, new_history

    @staticmethod
    def _assemble_request_history(
        history,
        *,
        context_index="",
        memory_context="",
        legacy_escape=False,
    ):
        req = list(history)
        if context_index:
            block_text = (
                "<thread_history>" + html.escape(context_index) + "</thread_history>"
                if legacy_escape
                else format_thread_history_prompt(context_index)
            )
            req = [{"role": "user", "content": block_text}, *req]
        if memory_context:
            req = [
                {
                    "role": "user",
                    "content": "<memory_context>"
                    + html.escape(encode(memory_context))
                    + "</memory_context>",
                },
                *req,
            ]
        return req

    @staticmethod
    def _effective_tool_schemas(profile: Profile) -> list[dict]:
        names: list[str] = []
        for name in (
            *(getattr(profile, "tools", ()) or ()),
            "thread_read",
            "ask_user",
            "thread_later",
        ):
            if isinstance(name, str) and name and name not in names:
                names.append(name)
        schemas: list[dict] = []
        for name in names:
            schema = TOOL_DEFINITIONS.get(name)
            if schema is not None:
                schemas.append(schema)
            else:
                schemas.append({"type": "function", "function": {"name": name}})
        return schemas

    @staticmethod
    def _estimate_prompt_overhead_tokens(
        profile: Profile,
        context_index: str,
        *,
        memory_context="",
        methods_text="",
    ) -> int:
        instructions = getattr(profile, "instructions", "") or ""
        if methods_text:
            methods_str = (
                methods_text
                if isinstance(methods_text, str)
                else encode(methods_text)
            )
            instructions += "\n<methods>" + methods_str + "</methods>"
        tokens = (
            estimate_tokens(instructions)
            + estimate_tokens(context_index or "")
            + estimate_tools_tokens(ThreadEngine._effective_tool_schemas(profile))
        )
        if memory_context:
            tokens += estimate_messages_tokens(
                [
                    {
                        "role": "user",
                        "content": "<memory_context>"
                        + html.escape(encode(memory_context))
                        + "</memory_context>",
                    }
                ]
            )
        return tokens

    async def _drive(self, thread_id, chat_id):
        current_task = asyncio.current_task()
        row = None
        store_chat = chat_id
        history = []
        processed = 0
        had_error = False
        try:
            row = await asyncio.to_thread(
                self.store.get, thread_id, chat_id, allow_self=(chat_id == "self")
            )
            store_chat = row["chat_id"]
            raw_profile = Profile.from_snapshot(row["profile_snapshot"])
            profile = (
                await asyncio.to_thread(self.runtime.resolve_profile, raw_profile)
                if hasattr(self.runtime, "resolve_profile")
                else raw_profile
            )
            history, processed = _repair_dangling_tool_calls(row["history"]), row["processed_revision"]
            in_flight_assistant = None
            in_flight_tools = []
            context_index = row["summary"]
            deadline = asyncio.get_running_loop().time() + self.timeout
            overflow_retried = False
            async with asyncio.timeout(self.timeout):
                for step_index in range(profile.max_steps or self.max_steps):
                    pending = await asyncio.to_thread(
                        self.store.pending, thread_id, processed
                    )
                    if pending:
                        if hasattr(self.runtime, "resolve_profile"):
                            profile = await asyncio.to_thread(
                                self.runtime.resolve_profile, raw_profile
                            )
                        if hasattr(self.runtime, "begin_turn"):
                            self.runtime.begin_turn(thread_id)
                    if row["workspace_id"]:
                        steering = []
                        for index, (revision, _text) in enumerate(pending):
                            metadata = await completed_thread(
                                self.store.input_context, thread_id, revision
                            )
                            if metadata.get("steer"):
                                steering.append(index)
                        selected = steering[-1] if steering else 0
                        for revision, text in pending[:selected]:
                            history.append(
                                {
                                    "role": "user",
                                    "content": "<superseded_input>"
                                    + html.escape(text)
                                    + "</superseded_input>",
                                }
                            )
                            processed = revision
                        pending = pending[selected : selected + 1]
                    for revision, text in pending:
                        history.append({"role": "user", "content": text})
                        processed = revision
                        metadata = await asyncio.to_thread(
                            self.store.input_context, thread_id, processed
                        )
                        if hasattr(self.runtime, "prepare"):
                            prepared = await self.runtime.prepare(
                                {
                                    **row,
                                    "revision": processed,
                                    "grant": metadata.get("grant", row["grant"]),
                                },
                                metadata,
                            )
                            if prepared:
                                history.append(
                                    {
                                        "role": "user",
                                        "content": "<thread_inputs>"
                                        + html.escape(encode(prepared))
                                        + "</thread_inputs>",
                                    }
                                )
                    budget = None
                    memory_context = ""
                    methods_text = ""
                    if self._legacy_char_budget is not None:
                        if len(encode(history)) > self._legacy_char_budget:
                            await asyncio.to_thread(
                                self.store.checkpoint,
                                thread_id,
                                history,
                                processed,
                                "context_checkpoint",
                                {},
                            )
                            context_index = await asyncio.to_thread(
                                self.store.compact,
                                thread_id,
                                store_chat,
                                max_chars=self._legacy_char_budget,
                            )
                            history = (
                                await asyncio.to_thread(
                                    self.store.get, thread_id, store_chat
                                )
                            )["history"]
                        alive, changed = await self._boundary(
                            thread_id, processed, deadline
                        )
                        if not alive:
                            return
                        if changed:
                            continue
                        if hasattr(self.runtime, "before"):
                            metadata = await asyncio.to_thread(
                                self.store.input_context, thread_id, processed
                            )
                            memory_context = await completed_thread(
                                self.runtime.before,
                                {
                                    **row,
                                    "revision": processed,
                                    "grant": metadata.get("grant", row["grant"]),
                                },
                            )
                    else:
                        alive, changed = await self._boundary(
                            thread_id, processed, deadline
                        )
                        if not alive:
                            return
                        if changed:
                            continue
                        if hasattr(self.runtime, "before"):
                            metadata = await asyncio.to_thread(
                                self.store.input_context, thread_id, processed
                            )
                            memory_context = (
                                await completed_thread(
                                    self.runtime.before,
                                    {
                                        **row,
                                        "revision": processed,
                                        "grant": metadata.get("grant", row["grant"]),
                                    },
                                )
                            ) or ""
                        memory_obj = getattr(self.runtime, "memory", None)
                        methods_fn = getattr(memory_obj, "methods", None)
                        step_ctx_meta = {
                            "chat_id": row["chat_id"] if row.get("owner") != "self" else "",
                            "is_admin": False,
                        }
                        cur_grant = (metadata.get("grant") if isinstance(locals().get("metadata"), dict) else None) or row.get("grant") or {}
                        if (
                            cur_grant.get("admin_verified")
                            or cur_grant.get("scheduled_admin")
                            or cur_grant.get("console_admin")
                            or cur_grant.get("admin_direct")
                        ):
                            step_ctx_meta["is_admin"] = True
                        elif callable(getattr(self.runtime, "authorized", None)):
                            try:
                                step_ctx_meta["is_admin"] = bool(
                                    await completed_thread(
                                        self.runtime.authorized,
                                        {**row, "grant": cur_grant},
                                    )
                                )
                            except (AttributeError, KeyError, TypeError, ValueError):
                                pass
                        if callable(methods_fn):
                            m_params = inspect.signature(methods_fn).parameters
                            if "chat_id" in m_params or "is_admin" in m_params:
                                methods_text = (
                                    await completed_thread(
                                        methods_fn,
                                        profile,
                                        chat_id=step_ctx_meta["chat_id"],
                                        is_admin=step_ctx_meta["is_admin"],
                                    )
                                ) or ""
                            else:
                                methods_text = (
                                    await completed_thread(methods_fn, profile)
                                ) or ""
                        budget = resolve_context_budget(
                            profile,
                            default_context_tokens=self.context_tokens,
                            model_catalog=self._current_model_catalog(),
                        )
                        overhead_tokens = self._estimate_prompt_overhead_tokens(
                            profile,
                            context_index,
                            memory_context=memory_context,
                            methods_text=methods_text,
                        )
                        prompt_tokens = overhead_tokens + estimate_messages_tokens(
                            history
                        )
                        if prompt_tokens >= budget["prune_tokens"]:
                            await asyncio.to_thread(
                                self.store.checkpoint,
                                thread_id,
                                history,
                                processed,
                                "context_checkpoint",
                                {},
                            )
                            pruned = await asyncio.to_thread(
                                self.store.prune_history,
                                thread_id,
                                store_chat,
                                tail_tokens=budget["tail_tokens"],
                            )
                            if pruned:
                                refreshed = await asyncio.to_thread(
                                    self.store.get, thread_id, store_chat
                                )
                                history = refreshed["history"]
                                processed = refreshed["processed_revision"]
                                prompt_tokens = overhead_tokens + estimate_messages_tokens(
                                    history
                                )
                        if prompt_tokens >= budget["trigger_tokens"]:
                            context_index, history = await self._run_tier2_compact(
                                thread_id,
                                store_chat,
                                profile,
                                history,
                                processed,
                                budget,
                                overhead_tokens=overhead_tokens,
                                force=False,
                            )
                        alive, changed = await self._boundary(
                            thread_id, processed, deadline
                        )
                        if not alive:
                            return
                        if changed:
                            continue
                    request_history = self._assemble_request_history(
                        history,
                        context_index=context_index,
                        memory_context=memory_context,
                        legacy_escape=(self._legacy_char_budget is not None),
                    )
                    step_grant_cur = (
                        metadata.get("grant") if isinstance(locals().get("metadata"), dict) else None
                    ) or row.get("grant") or {}
                    step_meta = {
                        "chat_id": row["chat_id"] if row.get("owner") != "self" else "",
                        "is_admin": bool(
                            step_grant_cur.get("admin_verified")
                            or step_grant_cur.get("scheduled_admin")
                            or step_grant_cur.get("console_admin")
                            or step_grant_cur.get("admin_direct")
                        ),
                    }
                    tok_step = STEP_METHODS_CONTEXT.set(step_meta)
                    try:
                        response = await self.runtime.step(profile, request_history)
                    except (
                        RuntimeError,
                        ValueError,
                        TypeError,
                        KeyError,
                        OSError,
                        TimeoutError,
                    ) as step_exc:
                        if (
                            not overflow_retried
                            and self._legacy_char_budget is None
                            and is_context_overflow_error(step_exc)
                        ):
                            overflow_retried = True
                            if budget is None:
                                budget = resolve_context_budget(
                                    profile,
                                    default_context_tokens=self.context_tokens,
                                    model_catalog=self._current_model_catalog(),
                                )
                            retry_overhead = self._estimate_prompt_overhead_tokens(
                                profile,
                                context_index,
                                memory_context=memory_context,
                                methods_text=methods_text,
                            )
                            context_index, history = await self._run_tier2_compact(
                                thread_id,
                                store_chat,
                                profile,
                                history,
                                processed,
                                budget,
                                overhead_tokens=retry_overhead,
                                force=True,
                            )
                            request_history = self._assemble_request_history(
                                history,
                                context_index=context_index,
                                memory_context=memory_context,
                                legacy_escape=False,
                            )
                            response = await self.runtime.step(
                                profile, request_history
                            )
                        else:
                            raise
                    finally:
                        STEP_METHODS_CONTEXT.reset(tok_step)
                    alive, changed = await self._boundary(
                        thread_id, processed, deadline
                    )
                    if not alive:
                        return
                    if changed:
                        await asyncio.to_thread(
                            self.store.checkpoint,
                            thread_id,
                            history,
                            processed,
                            "redirect",
                            {"discarded": "unexecuted_model_response"},
                        )
                        continue
                    calls = self._calls(response.get("tool_calls", []))
                    text = self.redact(str(response.get("response") or ""))
                    if calls:
                        in_flight_assistant = {
                            "role": "assistant",
                            "content": text,
                            "tool_calls": calls,
                        }
                        in_flight_tools = []
                        tool_messages = []
                        tool_results = []
                        redirecting = False
                        question = None
                        approval_pending = None
                        current = await asyncio.to_thread(
                            self.store.get, thread_id, store_chat
                        )
                        current["revision"] = processed
                        metadata = await asyncio.to_thread(
                            self.store.input_context, thread_id, processed
                        )
                        current["grant"] = metadata.get("grant", current["grant"])
                        for call in calls:
                            name, arguments = (
                                call["function"]["name"],
                                json.loads(call["function"]["arguments"]),
                            )
                            alive, changed = await self._boundary(
                                thread_id, processed, deadline
                            )
                            if not alive:
                                return
                            try:
                                if approval_pending is not None:
                                    result = {
                                        "skipped": True,
                                        "reason": "前置高风险操作正在等待管理员确认，本工具暂未执行。",
                                    }
                                elif redirecting or changed:
                                    redirecting = True
                                    result = {
                                        "skipped": True,
                                        "reason": "收到新补充，先按新的方向继续；该工具尚未执行。",
                                    }
                                elif name == "thread_read":
                                    validate_arguments(name, arguments)
                                    read_cap = (
                                        min(6000, max(200, self._legacy_char_budget // 3))
                                        if self._legacy_char_budget is not None
                                        else min(
                                            THREAD_READ_DEFAULT_MAX_CHARS,
                                            max(2000, budget["window_tokens"] // 4),
                                        )
                                    )
                                    result = await asyncio.to_thread(
                                        self.store.read_history,
                                        thread_id,
                                        store_chat,
                                        **arguments,
                                        max_chars=read_cap,
                                    )
                                elif name == "thread_later":
                                    validate_arguments(name, arguments)
                                    if self.intents is None:
                                        raise ValueError("意图存储不可用")
                                    due_at = self.intents.clock() + arguments["in_seconds"]
                                    if arguments.get("kind", "silent") == "serendipity":
                                        if (
                                            current["owner"] != "self"
                                            or not Profile.from_snapshot(current["profile_snapshot"]).autonomous
                                            or not callable(self.serendipity)
                                        ):
                                            raise PermissionError("仅自身公开探索线程可提议偶遇分享")
                                        share_reason = (
                                            arguments.get("share_reason")
                                            or arguments["next_step"]
                                        ).strip()
                                        if inspect.iscoroutinefunction(
                                            self.serendipity
                                        ) or inspect.iscoroutinefunction(
                                            getattr(self.serendipity, "__call__", None)
                                        ):
                                            result = await self.serendipity(
                                                thread_id=thread_id,
                                                due_at=due_at,
                                                reason=share_reason,
                                            )
                                        else:
                                            result = await asyncio.to_thread(
                                                self.serendipity,
                                                thread_id=thread_id,
                                                due_at=due_at,
                                                reason=share_reason,
                                            )
                                    else:
                                        target = (
                                            "chat:" + current["chat_id"]
                                            if current["owner"] != "self"
                                            else "chat:self"
                                        )
                                        scheduled_row = await asyncio.to_thread(
                                            self.intents.schedule,
                                            kind="silent",
                                            source="thread",
                                            target=target,
                                            due_at=due_at,
                                            reason=arguments["next_step"],
                                            thread_id=thread_id,
                                            payload={
                                                "next_step": arguments["next_step"],
                                                "platform": current["platform"],
                                            },
                                            bypass_limits=True,
                                        )
                                        result = {
                                            "intent_id": scheduled_row["intent_id"],
                                            "due_at": scheduled_row["due_at"],
                                        }
                                elif name == "ask_user":
                                    validate_arguments(name, arguments)
                                    if current["questions"] or len(calls) != 1:
                                        raise ValueError(
                                            "本轮已经问过一次，或应单独提出核心输入问题；请合理决定并完成"
                                        )
                                    question = arguments["question"]
                                    result = {"question": question, "waiting": True}
                                else:
                                    if name not in profile.tools:
                                        raise ValueError("当前角色未授权此工具")
                                    result = await self.runtime.call(
                                        name,
                                        arguments,
                                        {
                                            **current,
                                            "_tool_call_id": call["id"],
                                            "_step_index": step_index,
                                        },
                                    )
                                    if (
                                        isinstance(result, dict)
                                        and result.get("status") == "approval_required"
                                        and result.get("pending_approval")
                                    ):
                                        approval_pending = dict(result)
                            except (ValueError, RuntimeError, PermissionError, OSError) as error:
                                result = {
                                    "error": _safe_tool_error_message(error, self.redact)
                                }
                            result = self.redact(result)
                            tool_results.append(
                                {"call_id": call["id"], "value": result}
                            )
                            tool_msg = {
                                "role": "tool",
                                "tool_call_id": call["id"],
                                "content": "<external_data>"
                                + encode(result)
                                .replace("<", "\\u003c")
                                .replace(">", "\\u003e")
                                + "</external_data>",
                            }
                            tool_messages.append(tool_msg)
                            in_flight_tools.append(tool_msg)
                        history += [
                            {"role": "assistant", "content": text, "tool_calls": calls},
                            *tool_messages,
                        ]
                        in_flight_assistant = None
                        in_flight_tools = []
                        await asyncio.to_thread(
                            self.store.checkpoint,
                            thread_id,
                            history,
                            processed,
                            "tools",
                            {"calls": calls, "results": tool_results},
                        )
                        if question and await asyncio.to_thread(
                            self.store.finish,
                            thread_id,
                            store_chat,
                            {"question": self.redact(question)},
                            history,
                            processed,
                            waiting=True,
                        ):
                            return
                        if approval_pending is not None and await asyncio.to_thread(
                            self.store.finish,
                            thread_id,
                            store_chat,
                            self.redact(
                                {
                                    "pending_approval": approval_pending,
                                    "text": str(
                                        approval_pending.get("summary")
                                        or approval_pending.get("reason")
                                        or "高风险操作等待管理员确认"
                                    ),
                                }
                            ),
                            history,
                            processed,
                            state="waiting",
                        ):
                            return
                        continue
                    if not text:
                        raise RuntimeError("模型没有给出结果")
                    history.append({"role": "assistant", "content": text})
                    result = {"text": text}
                    if hasattr(self.runtime, "result"):
                        result = self.runtime.result(thread_id, result)
                    if await asyncio.to_thread(
                        self.store.finish,
                        thread_id,
                        store_chat,
                        result,
                        history,
                        processed,
                    ):
                        if hasattr(self.runtime, "finished"):
                            finished = await completed_thread(
                                self.store.get, thread_id, store_chat
                            )
                            await self.runtime.finished(finished)
                        return
                    await asyncio.to_thread(
                        self.store.checkpoint,
                        thread_id,
                        history,
                        processed,
                        "continued",
                        {},
                    )
                raise RuntimeError("线程达到本轮探索步数上限")
        except asyncio.CancelledError:
            had_error = True
            if row is not None:
                latest, _ = await _await_uninterruptible_coro(
                    asyncio.to_thread(
                        self.store.get,
                        thread_id,
                        store_chat,
                        allow_self=(chat_id == "self"),
                    )
                )
                combined_history = list(history)
                if locals().get("in_flight_assistant") is not None:
                    combined_history.append(in_flight_assistant)
                    combined_history.extend(locals().get("in_flight_tools") or [])
                repaired = _repair_dangling_tool_calls(combined_history)
                if not self.closing and latest["state"] == "active":
                    repaired.append(
                        {
                            "role": "user",
                            "content": "<activity_interrupted>当前活动已显式转向。正在执行的脚本取消并回退未提交文件；已开始的外部操作须核查现场后再决定是否重试。</activity_interrupted>",
                        }
                    )
                    await _await_uninterruptible_coro(
                        asyncio.to_thread(
                            self.store.checkpoint,
                            thread_id,
                            repaired,
                            processed,
                            "steer_interrupted",
                            {"reason": "explicit_steering"},
                        )
                    )
                elif latest["state"] == "idle" and (
                    locals().get("in_flight_assistant") is not None or repaired != history
                ):
                    await _await_uninterruptible_coro(
                        asyncio.to_thread(
                            self.store.save_stopped_history,
                            thread_id,
                            repaired,
                            processed,
                        )
                    )
            raise
        except Exception as error:
            had_error = True
            safe_errors = {
                "当前角色的模型均未返回可用结果",
                "线程达到本轮探索步数上限",
                "模型没有给出结果",
            }
            reason = (
                "本轮探索超过运行时限"
                if isinstance(error, TimeoutError)
                else (
                    self.redact(str(error))
                    if str(error) in safe_errors or isinstance(error, PermissionError)
                    else "本轮 Agent 活动中断，请检查模型、能力配置与执行环境"
                )
            )
            error_text = reason + "。可给原线程补充消息后继续。"
            if row is None:
                try:
                    latest = await asyncio.to_thread(
                        self.store.get,
                        thread_id,
                        chat_id,
                        allow_self=(chat_id == "self"),
                    )
                except Exception:
                    latest = None
                if latest is not None:
                    store_chat = latest["chat_id"]
                    history = latest["history"]
                    processed = latest["processed_revision"]
            else:
                latest = row
            if latest is not None:
                await asyncio.to_thread(
                    self.store.finish,
                    thread_id,
                    store_chat,
                    {"error": error_text},
                    history,
                    processed,
                )
        finally:
            self._finishing.add(thread_id)

            async def _finalize():
                try:
                    if hasattr(self.runtime, "end"):
                        try:
                            await self.runtime.end(thread_id)
                        except Exception:
                            pass
                finally:
                    async with self._submission_lock:
                        try:
                            thread_lock = self.locks.setdefault(
                                thread_id, asyncio.Lock()
                            )
                            async with thread_lock:
                                self._finishing.discard(thread_id)
                                was_background = thread_id in self.background_tasks
                                is_current = self.tasks.get(thread_id) is current_task
                                latest = None
                                try:
                                    latest = await asyncio.to_thread(
                                        self.store.get,
                                        thread_id,
                                        store_chat,
                                        allow_self=(chat_id == "self"),
                                    )
                                except Exception:
                                    latest = None
                                if latest is not None:
                                    self._resolve(thread_id, latest)
                                else:
                                    for _rev, fut in self.waiters.pop(thread_id, []):
                                        if not fut.done():
                                            fut.set_result(
                                                {
                                                    "thread_id": thread_id,
                                                    "state": "closed",
                                                    "result": {},
                                                }
                                            )
                                if (
                                    latest is not None
                                    and not self.closing
                                    and latest["state"] == "active"
                                    and (is_current or thread_id not in self.tasks)
                                ):
                                    self._drop_deferred(thread_id)
                                    self.chats[thread_id] = chat_id
                                    if was_background:
                                        self.background_tasks.add(thread_id)
                                    else:
                                        self.background_tasks.discard(thread_id)
                                    self.tasks[thread_id] = asyncio.create_task(
                                        self._drive(thread_id, chat_id)
                                    )
                                elif is_current:
                                    await self._drain_deferred_locked(
                                        free_total=1,
                                        free_bg=(1 if was_background else 0),
                                    )
                                    self.tasks.pop(thread_id, None)
                                    self.background_tasks.discard(thread_id)
                                else:
                                    self.background_tasks.discard(thread_id)
                                    await self._drain_deferred_locked()
                        finally:
                            self._prune_idle_state_locked(thread_id)

            _, cancelled_in_finally = await _await_uninterruptible_coro(_finalize())
            if cancelled_in_finally and not had_error:
                raise asyncio.CancelledError

    async def _boundary(self, thread_id, processed, deadline):
        state = await asyncio.to_thread(self.store.boundary, thread_id, processed)
        if self.closing:
            return False, False
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        return state

    @staticmethod
    def _calls(calls):
        result = []
        if not isinstance(calls, list) or len(calls) > 8:
            raise ValueError("模型工具调用格式无效或过多")
        for call in calls:
            function = call.get("function") or {
                "name": call.get("func_name"),
                "arguments": call.get("args", {}),
            }
            arguments = function.get("arguments", {})
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise TypeError("工具调用参数必须为对象")
            result.append(
                {
                    "id": str(call.get("id") or uuid.uuid4().hex),
                    "type": "function",
                    "function": {
                        "name": str(function.get("name") or ""),
                        "arguments": encode(arguments),
                    },
                }
            )
        return result

    async def stop(self, thread_id, *, chat_id):
        async with self._submission_lock:
            self._drop_deferred(thread_id)
            task = None
            try:
                async with self.locks.setdefault(thread_id, asyncio.Lock()):
                    row = await asyncio.to_thread(self.store.stop, thread_id, chat_id)
                    task = self.tasks.get(thread_id)
                    if task:
                        task.cancel()
                    self._resolve(thread_id, row, stopped=True)
                if not task:
                    await self._drain_deferred_locked()
            finally:
                self._prune_idle_state_locked(thread_id)
        if task:
            await asyncio.gather(task, return_exceptions=True)
            async with self._submission_lock:
                try:
                    if self.tasks.get(thread_id) is task:
                        was_background = thread_id in self.background_tasks
                        await self._drain_deferred_locked(
                            free_total=1,
                            free_bg=(1 if was_background else 0),
                        )
                        self.tasks.pop(thread_id, None)
                        self.background_tasks.discard(thread_id)
                    else:
                        await self._drain_deferred_locked()
                    row = await asyncio.to_thread(self.store.get, thread_id, chat_id)
                finally:
                    self._prune_idle_state_locked(thread_id)
        if hasattr(self.runtime, "stopped"):
            try:
                await self.runtime.stopped(thread_id)
            except (RuntimeError, OSError) as error:
                await completed_thread(
                    self.store.cleanup_error,
                    thread_id,
                    chat_id,
                    self.redact(str(error)),
                )
                raise
        return public(row)

    async def drain(self):
        while self.tasks:
            pending = [t for t in self.tasks.values() if not t.done()]
            if not pending:
                async with self._submission_lock:
                    for tid in [k for k, v in list(self.tasks.items()) if v.done()]:
                        self.tasks.pop(tid, None)
                        self.background_tasks.discard(tid)
                        self._prune_idle_state_locked(tid)
                continue
            await asyncio.gather(*pending, return_exceptions=True)
        async with self._submission_lock:
            for tid in list(set(self.chats) | set(self.locks)):
                self._prune_idle_state_locked(tid)

    async def close(self):
        self.closing = True
        self._deferred_queue.clear()
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        self.tasks.clear()
        self.background_tasks.clear()
        self.chats.clear()
        self.locks.clear()
        for waiters in self.waiters.values():
            for _, future in waiters:
                if not future.done():
                    future.cancel()
        self.waiters.clear()
