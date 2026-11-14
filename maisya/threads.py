"""持续 Agent 线程、事件记录与有界运行。"""

import asyncio
import hashlib
import html
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from .abilities import Profile
from .bridge import record_return
from .pathsafe import safe_chmod
from .scout import validate_arguments
from .workspaces import completed_thread


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def public(row):
    return {
        key: row[key] for key in ("thread_id", "profile", "owner", "state", "result")
    }


_INPUT_TAG_RE = re.compile(
    r"\s*<(?:reference_ids|previous_result)>[\s\S]*?</(?:reference_ids|previous_result)>\s*"
)


def _clean_input_title(raw_text: str) -> str:
    cleaned = _INPUT_TAG_RE.sub(" ", str(raw_text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80]


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
        safe_chmod(path, 0o600)

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
        with self.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            workspace_id = ""
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
                if binding:
                    if binding["profile"] != profile.id:
                        raise ValueError("该工作区已绑定另一个角色")
                    previous = db.execute(
                        "SELECT state,profile_snapshot FROM agent_threads WHERE thread_id=?",
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
                    if previous and previous["state"] != "closed":
                        old = json.loads(
                            encode(
                                asdict(
                                    Profile(**json.loads(previous["profile_snapshot"]))
                                )
                            )
                        )
                        updated = json.loads(encode(asdict(profile)))
                        if any(
                            old[key] != updated[key]
                            for key in (
                                "access",
                                "environment",
                                "tools",
                                "network",
                                "mcp",
                                "apis",
                                "secrets",
                                "template",
                                "sandbox_targets",
                                "bound",
                                "allow_users",
                                "allow_chats",
                            )
                        ):
                            raise PermissionError(
                                "工作区绑定线程的权限快照不同，请先停止原线程再显式续接"
                            )
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
                        db.execute(
                            "UPDATE agent_threads SET profile_snapshot=? WHERE thread_id=?",
                            (encode(updated), binding["thread_id"]),
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
                            encode([owner, chat_id, workspace]).encode()
                        ).hexdigest()[:24]
                    )
                db.execute(
                    "INSERT INTO agent_workspaces VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(workspace_id) DO UPDATE SET chat_id=excluded.chat_id, thread_id=excluded.thread_id",
                    (workspace_id, owner, chat_id, workspace, profile.id, thread_id),
                )
            db.execute(
                "INSERT INTO agent_threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        row = self.get(thread_id, chat_id)
        if row["state"] == "closed":
            raise ValueError("线程已关闭；请通过 from_threads 引用旧结果开启新线程")
        revision = row["revision"] + 1
        with self.connect() as db, db:
            db.execute(
                'UPDATE agent_threads SET revision=?, state="active", questions=?, updated=? WHERE thread_id=?',
                (
                    revision,
                    0 if row["state"] == "idle" else row["questions"],
                    time.time(),
                    thread_id,
                ),
            )
            payload = {"text": message, "context": input_context or {}}
            if return_route:
                payload["return_route"] = return_route
                db.execute(
                    "INSERT INTO return_members VALUES (?, ?, ?)",
                    (return_route["group_id"], thread_id, revision),
                )
            self.event(db, thread_id, "input", revision, payload)
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
            return row is not None and row["state"] != "closed", False
        return row is not None and row["state"] != "closed", row is not None and row[
            "revision"
        ] > processed

    def checkpoint(self, thread_id, history, processed, kind, payload):
        with self.connect() as db, db:
            db.execute(
                'UPDATE agent_threads SET history=?, processed_revision=? WHERE thread_id=? AND state!="closed"',
                (encode(history), processed, thread_id),
            )
            self.event(db, thread_id, kind, processed, payload)

    def finish(self, thread_id, chat_id, result, history, processed, *, waiting=False):
        row = self.get(thread_id, chat_id)
        fifo = bool(row["workspace_id"])
        with self.connect() as db, db:
            cursor = db.execute(
                "UPDATE agent_threads SET result=?, history=?, processed_revision=?, completed_revision=?, "
                'state=CASE WHEN ? AND revision>? THEN "active" ELSE ? END, questions=questions+?, updated=? WHERE thread_id=? AND state!="closed" AND revision'
                + (">=?" if fifo else "=?"),
                (
                    encode(result),
                    encode(history),
                    processed,
                    processed,
                    int(fifo),
                    processed,
                    "waiting" if waiting else "idle",
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
        with self.connect() as db, db:
            db.execute(
                'UPDATE agent_threads SET state="closed", completed_revision=revision, updated=? WHERE thread_id=?',
                (time.time(), thread_id),
            )
            self.event(db, thread_id, "stopped", row["revision"], {})
        return self.get(thread_id, chat_id)

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
                'UPDATE agent_threads SET state="idle", result=? WHERE state="active"',
                (encode({"error": "宿主重载中断了运行；可以给原线程 send 继续。"}),),
            )

    def compact(self, thread_id, chat_id, *, max_chars=48000):
        row = self.get(thread_id, chat_id)
        history = row["history"]
        if len(encode(history)) <= max_chars:
            return row["summary"]
        blocks = []
        for message in history:
            if (
                message.get("role") == "tool"
                and blocks
                and blocks[-1][0].get("tool_calls")
            ):
                blocks[-1].append(message)
            else:
                blocks.append([message])
        user_blocks = [
            index
            for index, block in enumerate(blocks)
            if block[0].get("role") == "user"
        ]
        priority = ([user_blocks[-1], user_blocks[0]] if user_blocks else []) + list(
            reversed(range(len(blocks)))
        )
        selected = set()
        budget = max_chars - min(1000, max_chars // 4)
        size = 2
        for index in priority:
            block_size = len(encode(blocks[index])) + 2
            if index not in selected and size + block_size <= budget:
                selected.add(index)
                size += block_size
        retained = [
            message
            for index, block in enumerate(blocks)
            if index in selected
            for message in block
        ]
        with self.connect() as db, db:
            archive_seq = self.event(
                db,
                thread_id,
                "context_archive",
                row["processed_revision"],
                {"history": history, "previous_index": row["summary"]},
            )
            summary = encode(
                {
                    "archive_event": archive_seq,
                    "read_tool": "thread_read",
                    "note": "完整历史保存在本线程。需要更早的事实、决定、理由或出处时，按关键词回读；这是导航索引，不是事实摘要。",
                }
            )
            db.execute(
                'UPDATE agent_threads SET summary=?, history=? WHERE thread_id=? AND state!="closed"',
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
        max_chars=6000,
        allow_self=False,
    ):
        self.get(thread_id, chat_id, allow_self=allow_self)
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
                    'AND kind IN ("input", "result", "question", "tools", "context_archive") '
                    "AND instr(lower(payload), lower(?))>0 ORDER BY seq DESC LIMIT ?",
                    (thread_id, query, limit),
                ).fetchall()
        result = []
        chunk_size = max(100, max_chars // max(1, len(rows)))
        for record in rows:
            text = record["payload"]
            start = (
                offset
                if event_seq
                else max(0, text.lower().find(query.lower()) - 160)
                if query
                else 0
            )
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
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)[:12]
        match = " AND ".join('"' + token + '"*' for token in tokens)
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
                    [query, *parameters, limit],
                ).fetchall()
            elif re.search(r"[\u3400-\u9fff]", query):
                conditions = " AND ".join("f.text LIKE ?" for _ in tokens)
                rows = db.execute(
                    "SELECT t.* FROM agent_threads t JOIN thread_search f ON t.thread_id=f.thread_id WHERE "
                    + conditions
                    + " AND "
                    + scopes[scope]
                    + " ORDER BY t.updated DESC LIMIT ?",
                    [*["%" + token + "%" for token in tokens], *parameters, limit],
                ).fetchall()
            elif match:
                rows = db.execute(
                    "SELECT t.* FROM agent_threads t JOIN thread_search f ON t.thread_id=f.thread_id "
                    "WHERE thread_search MATCH ? AND "
                    + scopes[scope]
                    + " ORDER BY t.updated DESC LIMIT ?",
                    [match, *parameters, limit],
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT t.* FROM agent_threads t WHERE "
                    + scopes[scope]
                    + " ORDER BY t.updated DESC LIMIT ?",
                    [*parameters, limit],
                ).fetchall()
            found = []
            for row in rows:
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
                    chat_id,
                    query=query,
                    limit=2,
                    max_chars=1800,
                    allow_self=True,
                )
        return found


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
        context_chars=48000,
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
        self.context_chars = context_chars
        self.redact = redact
        self.intents = intents
        self.serendipity = serendipity
        self.tasks = {}
        self.chats = {}
        self.locks = {}
        self.waiters = {}
        self._starting = 0
        self._submission_lock = asyncio.Lock()
        self.closing = False

    async def recover(self):
        await asyncio.to_thread(self.store.recover)

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
    ):
        if self.closing or len(self.tasks) + self._starting >= self.max_active:
            raise ValueError("当前 Agent 线程已满，请稍后再试")
        self._validate_input(message, wait_seconds)
        self._starting += 1
        try:
            row = await asyncio.to_thread(
                self.store.create,
                profile,
                self.redact(message),
                chat_id=chat_id,
                platform=platform,
                user_id=user_id,
                owner=owner,
                workspace=workspace,
                grant=grant,
            )
            await self.send(
                row["thread_id"],
                message,
                chat_id=chat_id,
                wait_seconds=0,
                return_route=return_route,
                input_context={
                    "grant": grant or {},
                    "references": references or [],
                    "from_threads": from_threads or [],
                },
            )
        finally:
            self._starting -= 1
        waiters = self.waiters.get(row["thread_id"], [])
        if wait_seconds and waiters:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(waiters[0][1]), wait_seconds
                )
            except TimeoutError:
                pass
        return public(
            await asyncio.to_thread(self.store.get, row["thread_id"], chat_id)
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
    ):
        self._validate_input(message, wait_seconds)
        if type(steer) is not bool:
            raise ValueError("转向标志必须为布尔值")
        input_context = {**(input_context or {}), "steer": steer}
        if self.closing:
            raise ValueError("插件正在关闭")
        async with (
            self._submission_lock,
            self.locks.setdefault(thread_id, asyncio.Lock()),
        ):
            row = await asyncio.to_thread(self.store.get, thread_id, chat_id)
            if thread_id not in self.tasks and len(self.tasks) >= self.max_active:
                raise ValueError("当前 Agent 线程已满，请稍后再试")
            if row["revision"] - row["processed_revision"] >= 32:
                raise ValueError("该线程正在消化已有补充，请稍后再发")
            if (
                row["state"] in {"idle", "waiting"}
                and time.time() - row["updated"] > self.idle_seconds
            ):
                await asyncio.to_thread(
                    self.store.compact, thread_id, chat_id, max_chars=self.context_chars
                )
            revision = await asyncio.to_thread(
                self.store.enqueue,
                thread_id,
                self.redact(message),
                chat_id,
                return_route,
                input_context,
            )
            future = asyncio.get_running_loop().create_future()
            self.waiters.setdefault(thread_id, []).append((revision, future))
            if steer and row["state"] == "active" and thread_id in self.tasks:
                self.tasks[thread_id].cancel()
            if thread_id not in self.tasks:
                self.chats[thread_id] = chat_id
                self.tasks[thread_id] = asyncio.create_task(
                    self._drive(thread_id, chat_id)
                )
        if wait_seconds:
            try:
                return await asyncio.wait_for(asyncio.shield(future), wait_seconds)
            except TimeoutError:
                pass
        return public(await asyncio.to_thread(self.store.get, thread_id, chat_id))

    def _resolve(self, thread_id, row):
        remaining = []
        for revision, future in self.waiters.get(thread_id, []):
            if revision <= row["completed_revision"] or row["state"] == "closed":
                if not future.done():
                    future.set_result(public(row))
            else:
                remaining.append((revision, future))
        self.waiters[thread_id] = remaining

    async def _drive(self, thread_id, chat_id):
        row = await asyncio.to_thread(self.store.get, thread_id, chat_id)
        profile = Profile(**row["profile_snapshot"])
        history, processed = row["history"], row["processed_revision"]
        context_index = row["summary"]
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            async with asyncio.timeout(self.timeout):
                for _ in range(profile.max_steps or self.max_steps):
                    pending = await asyncio.to_thread(
                        self.store.pending, thread_id, processed
                    )
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
                    if len(encode(history)) > self.context_chars:
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
                            chat_id,
                            max_chars=self.context_chars,
                        )
                        history = (
                            await asyncio.to_thread(self.store.get, thread_id, chat_id)
                        )["history"]
                    request_history = history
                    if context_index:
                        request_history = [
                            {
                                "role": "user",
                                "content": "<thread_history>"
                                + html.escape(context_index)
                                + "</thread_history>",
                            },
                            *history,
                        ]
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
                        if memory_context:
                            request_history = [
                                {
                                    "role": "user",
                                    "content": "<memory_context>"
                                    + html.escape(encode(memory_context))
                                    + "</memory_context>",
                                },
                                *request_history,
                            ]
                    response = await self.runtime.step(profile, request_history)
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
                        tool_messages = []
                        tool_results = []
                        redirecting = False
                        question = None
                        current = await asyncio.to_thread(
                            self.store.get, thread_id, chat_id
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
                                if redirecting or changed:
                                    redirecting = True
                                    result = {
                                        "skipped": True,
                                        "reason": "收到新补充，先按新的方向继续；该工具尚未执行。",
                                    }
                                elif name == "thread_read":
                                    validate_arguments(name, arguments)
                                    result = await asyncio.to_thread(
                                        self.store.read_history,
                                        thread_id,
                                        chat_id,
                                        **arguments,
                                        max_chars=min(
                                            6000, max(200, self.context_chars // 3)
                                        ),
                                    )
                                elif name == "thread_later":
                                    validate_arguments(name, arguments)
                                    if self.intents is None:
                                        raise ValueError("意图存储不可用")
                                    due_at = self.intents.clock() + arguments["in_seconds"]
                                    if arguments.get("kind", "silent") == "serendipity":
                                        if (
                                            current["owner"] != "self"
                                            or not Profile(**current["profile_snapshot"]).autonomous
                                            or not callable(self.serendipity)
                                        ):
                                            raise PermissionError("仅自身公开探索线程可提议偶遇分享")
                                        share_reason = (
                                            arguments.get("share_reason")
                                            or arguments["next_step"]
                                        ).strip()
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
                                        {**current, "_tool_call_id": call["id"]},
                                    )
                            except (ValueError, RuntimeError, PermissionError, OSError):
                                result = {
                                    "error": "只读工具未完成；检查参数、来源范围或宿主能力配置。"
                                }
                            result = self.redact(result)
                            tool_results.append(
                                {"call_id": call["id"], "value": result}
                            )
                            tool_messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call["id"],
                                    "content": "<external_data>"
                                    + encode(result)
                                    .replace("<", "\\u003c")
                                    .replace(">", "\\u003e")
                                    + "</external_data>",
                                }
                            )
                        history += [
                            {"role": "assistant", "content": text, "tool_calls": calls},
                            *tool_messages,
                        ]
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
                            chat_id,
                            {"question": self.redact(question)},
                            history,
                            processed,
                            waiting=True,
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
                        chat_id,
                        result,
                        history,
                        processed,
                    ):
                        if hasattr(self.runtime, "finished"):
                            finished = await completed_thread(
                                self.store.get, thread_id, chat_id
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
            row = await completed_thread(self.store.get, thread_id, chat_id)
            if not self.closing and row["state"] == "active":
                history.append(
                    {
                        "role": "user",
                        "content": "<activity_interrupted>当前活动已显式转向。正在执行的脚本取消并回退未提交文件；已开始的外部操作须核查现场后再决定是否重试。</activity_interrupted>",
                    }
                )
                await completed_thread(
                    self.store.checkpoint,
                    thread_id,
                    history,
                    processed,
                    "steer_interrupted",
                    {"reason": "explicit_steering"},
                )
            raise
        except Exception as error:
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
            row = await asyncio.to_thread(self.store.get, thread_id, chat_id)
            await asyncio.to_thread(
                self.store.finish,
                thread_id,
                chat_id,
                {"error": error_text},
                history,
                processed,
            )
        finally:
            if hasattr(self.runtime, "end"):
                await self.runtime.end(thread_id)
            async with self.locks.setdefault(thread_id, asyncio.Lock()):
                self.tasks.pop(thread_id, None)
                row = await asyncio.to_thread(self.store.get, thread_id, chat_id)
                self._resolve(thread_id, row)
                if not self.closing and row["state"] == "active":
                    self.tasks[thread_id] = asyncio.create_task(
                        self._drive(thread_id, chat_id)
                    )

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
        async with self.locks.setdefault(thread_id, asyncio.Lock()):
            row = await asyncio.to_thread(self.store.stop, thread_id, chat_id)
            task = self.tasks.get(thread_id)
            if task:
                task.cancel()
            self._resolve(thread_id, row)
        if task:
            await asyncio.gather(task, return_exceptions=True)
            if self.tasks.get(thread_id) is task:
                self.tasks.pop(thread_id, None)
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
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def close(self):
        self.closing = True
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        self.tasks.clear()
        for waiters in self.waiters.values():
            for _, future in waiters:
                if not future.done():
                    future.cancel()
