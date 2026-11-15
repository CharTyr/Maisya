"""管理台只读快照；只读取插件自己的数据。"""

import hashlib
import json
import mimetypes
import sqlite3
import time

from .abilities import Profile
from .context_budget import (
    DEFAULT_CONTEXT_TOKENS,
    estimate_message_tokens,
    estimate_tokens,
    resolve_context_budget,
)
from .threads import _is_junk_thread_row


def _safe_json_loads(raw, default):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        parsed = json.loads(raw or "")
        return parsed if parsed is not None else default
    except (TypeError, ValueError):
        return default


def _pending_approval_map(db, redact) -> dict[str, dict]:
    try:
        rows = db.execute(
            "SELECT request_id, thread_id, tool_call_id, action_key, tool, category, "
            "risk_level, command, reason, summary, scope, session_id, targets, created_at "
            "FROM pending_approvals WHERE status='pending' ORDER BY created_at DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        tid = str(r["thread_id"] or "")
        if not tid or tid in out:
            continue
        targets = _safe_json_loads(r["targets"], [])
        out[tid] = {
            "request_id": r["request_id"],
            "thread_id": tid,
            "action_key": r["action_key"],
            "tool": r["tool"],
            "category": r["category"],
            "risk_level": r["risk_level"],
            "command": str(redact(r["command"]))[:800],
            "reason": str(redact(r["reason"]))[:300],
            "summary": str(redact(r["summary"]))[:300],
            "scope": r["scope"],
            "session_id": r["session_id"],
            "targets": targets if isinstance(targets, list) else [],
            "created_at": float(r["created_at"] or 0.0),
        }
    return out


def _media_kind_for(name: str, kind: str = "") -> str:
    lower = str(name or "").lower()
    k = str(kind or "").lower()
    if k in {"image", "photo"} or lower.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
        return "image"
    if k in {"html", "page"} or lower.endswith((".html", ".htm")):
        return "html"
    if k in {"audio", "voice"} or lower.endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
        return "audio"
    if k in {"video"} or lower.endswith((".mp4", ".webm", ".mov", ".mkv")):
        return "video"
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith((".md", ".txt", ".json", ".toml", ".py", ".csv", ".log")):
        return "doc"
    return k or "file"


def _step_command_summary(tool_name: str, args: dict, redact) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "query", "url", "path", "action", "tool", "api"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return str(redact(val.strip()))[:320]
    try:
        return str(redact(json.dumps(args, ensure_ascii=False)))[:320]
    except (TypeError, ValueError):
        return ""


def _build_steps(db, thread_id: str, redact, *, limit: int = 120) -> list[dict]:
    records = db.execute(
        "SELECT seq, kind, revision, payload, at FROM thread_events "
        "WHERE thread_id=? ORDER BY seq ASC LIMIT ?",
        (thread_id, limit),
    ).fetchall()
    steps: list[dict] = []
    prev_ts: float | None = None
    for ev in records:
        seq = int(ev["seq"] or 0)
        kind = str(ev["kind"] or "")
        at = float(ev["at"] or 0.0)
        delta_ms = max(0, int((at - prev_ts) * 1000)) if prev_ts is not None and at >= prev_ts else 0
        payload_obj = _safe_json_loads(ev["payload"], None)
        if kind == "tools" and isinstance(payload_obj, dict):
            calls = payload_obj.get("calls") or []
            results_raw = payload_obj.get("results") or []
            res_by_id = {
                str(item.get("call_id") or ""): item.get("value")
                for item in results_raw
                if isinstance(item, dict)
            }
            for idx, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                call_id = str(call.get("id") or "")
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                tool_name = str(fn.get("name") or call.get("name") or "tool")
                raw_args = fn.get("arguments") if "arguments" in fn else call.get("arguments")
                args = _safe_json_loads(raw_args, {}) if isinstance(raw_args, str) else (raw_args if isinstance(raw_args, dict) else {})
                red_args = redact(args)
                val = res_by_id.get(call_id)
                val_dict = val if isinstance(val, dict) else {}
                status = "ok"
                if val_dict.get("pending_approval") or val_dict.get("waiting") or val_dict.get("status") == "approval_required":
                    status = "waiting"
                elif val_dict.get("error") or val_dict.get("status") in {"error", "denied", "timeout"}:
                    status = "error"
                raw_out = json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val or "")
                red_out = str(redact(raw_out))
                cmd_str = _step_command_summary(tool_name, args, redact)
                title = f"{tool_name}" + (f" · {cmd_str[:36]}" if cmd_str else "")
                steps.append(
                    {
                        "id": f"step-{seq}-{idx}",
                        "seq": seq,
                        "kind": "tool",
                        "tool": tool_name,
                        "title": title,
                        "command": cmd_str,
                        "args": red_args,
                        "status": status,
                        "duration_ms": int(val_dict.get("duration_ms") or (delta_ms if idx == 0 else 0)),
                        "output_preview": red_out[:400],
                        "redacted_output": red_out[:2400],
                        "at": at,
                        "ts": at,
                    }
                )
            prev_ts = at
            continue

        red_text = ""
        if isinstance(payload_obj, dict):
            summary_candidate = (
                payload_obj.get("text")
                or payload_obj.get("question")
                or payload_obj.get("error")
                or payload_obj.get("reason")
                or json.dumps(payload_obj, ensure_ascii=False)
            )
            red_text = str(redact(str(summary_candidate)))
        else:
            red_text = str(redact(str(ev["payload"] or "")))
        title_map = {
            "input": "收到触发输入",
            "waiting": "等待确认或回复",
            "finished": "生成最终结果",
            "failed": "执行出错",
            "stopped": "停止线程",
            "steer": "收到转向补充",
            "steer_interrupted": "中断并转向",
            "context_prune": "上下文压缩清理",
            "continued": "继续下一轮探索",
        }
        status = (
            "error"
            if kind == "failed"
            else ("waiting" if kind == "waiting" else ("stopped" if kind in {"stopped", "steer_interrupted"} else "ok"))
        )
        steps.append(
            {
                "id": f"step-{seq}",
                "seq": seq,
                "kind": kind or "event",
                "tool": "",
                "title": title_map.get(kind, kind or "事件"),
                "command": "",
                "args": redact(payload_obj) if isinstance(payload_obj, dict) else {},
                "status": status,
                "duration_ms": delta_ms,
                "output_preview": red_text[:400],
                "redacted_output": red_text[:2400],
                "at": at,
                "ts": at,
            }
        )
        prev_ts = at
    return steps


def _thread_artifacts(db, row, selected, redact) -> list[dict]:
    tid = str(row["thread_id"] or "")
    ws_id = (
        "ws_" + hashlib.sha256(tid.encode()).hexdigest()[:24]
    )
    try:
        ws_row = db.execute(
            "SELECT workspace_id FROM agent_workspaces WHERE thread_id=? LIMIT 1",
            (tid,),
        ).fetchone()
        if ws_row and ws_row["workspace_id"]:
            ws_id = str(ws_row["workspace_id"])
        else:
            ctx_row = db.execute(
                "SELECT workspace_id FROM thread_context WHERE thread_id=? LIMIT 1",
                (tid,),
            ).fetchone()
            if ctx_row and ctx_row["workspace_id"]:
                ws_id = str(ctx_row["workspace_id"])
    except sqlite3.OperationalError:
        pass
    seen: set[str] = set()
    artifacts: list[dict] = []
    try:
        att_rows = db.execute(
            "SELECT handle, name, kind, size, created FROM attachments WHERE thread_id=? ORDER BY created DESC",
            (tid,),
        ).fetchall()
    except sqlite3.OperationalError:
        att_rows = []
    for a in att_rows:
        handle = str(a["handle"] or "")
        name = str(redact(str(a["name"] or handle)))[:160]
        seen.add(handle)
        mkind = _media_kind_for(name, str(a["kind"] or ""))
        mime, _ = mimetypes.guess_type(name)
        artifacts.append(
            {
                "id": handle or f"att:{tid}:{name}",
                "handle": handle,
                "name": name,
                "path": name,
                "workspace_id": ws_id,
                "thread_id": tid,
                "media_kind": mkind,
                "kind": str(a["kind"] or mkind),
                "size_bytes": int(a["size"] or 0),
                "bytes": int(a["size"] or 0),
                "mime": mime or "application/octet-stream",
                "deliverable": True,
                "created_at": float(a["created"] or 0.0),
            }
        )
    for item in selected.get("attachments") or []:
        handle = str(item.get("handle") or "")
        if handle and handle in seen:
            continue
        name = str(item.get("name") or handle)
        mkind = _media_kind_for(name, str(item.get("kind") or ""))
        mime, _ = mimetypes.guess_type(name)
        artifacts.append(
            {
                "id": handle or f"att:{tid}:{name}",
                "handle": handle,
                "name": name,
                "path": name,
                "workspace_id": ws_id,
                "thread_id": tid,
                "media_kind": mkind,
                "kind": str(item.get("kind") or mkind),
                "size_bytes": int(item.get("bytes") or 0),
                "bytes": int(item.get("bytes") or 0),
                "mime": mime or "application/octet-stream",
                "deliverable": True,
                "created_at": float(row["updated"] or 0.0),
            }
        )
    return artifacts


def _excerpt_of(raw_result, redact, limit=140):
    try:
        parsed = json.loads(raw_result or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    text = parsed.get("text") or parsed.get("error") or ""
    return str(redact(str(text))).strip()[:limit]


def _history_preview(item, redact):
    role = str(item.get("role") or "")
    content = str(item.get("content") or "").strip()
    tool_calls = item.get("tool_calls") or []
    parts = []
    if content:
        parts.append(content)
    if isinstance(tool_calls, list) and tool_calls:
        names = [
            str(((tc.get("function") or {}).get("name") or tc.get("name") or "tool"))
            for tc in tool_calls
            if isinstance(tc, dict)
        ]
        if names:
            parts.append("调用工具: " + ", ".join(names))
        parts.append(json.dumps(tool_calls, ensure_ascii=False))
    if role == "tool" and item.get("name"):
        parts.insert(0, f"[{item['name']}]")
    return str(redact("\n".join(parts) or json.dumps(item, ensure_ascii=False)))[:800]


def _history_label(item, index):
    role = str(item.get("role") or "")
    if role == "user":
        return f"输入 #{index}"
    if role == "tool":
        name = item.get("name") or item.get("tool_call_id") or f"#{index}"
        return f"工具结果 · {name}"
    if role == "assistant":
        calls = item.get("tool_calls") or []
        if isinstance(calls, list) and calls:
            first = ((calls[0].get("function") or {}).get("name") or calls[0].get("name") or "tool")
            return f"决策调用 · {first}" if not str(item.get("content") or "").strip() else f"助手推理 #{index}"
        return f"助手输出 #{index}"
    return f"消息 #{index}"


def snapshot(
    store,
    *,
    thread_id="",
    chat_id="",
    cursor=0,
    context_limit=48000,
    context_tokens=DEFAULT_CONTEXT_TOKENS,
    model_catalog=None,
    redact=lambda value: value,
    memory=None,
    workspace_root=None,
):
    if (
        not isinstance(thread_id, str)
        or len(thread_id) > 64
        or not isinstance(chat_id, str)
        or len(chat_id) > 256
    ):
        raise ValueError("线程或会话筛选无效")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("事件游标无效")
    with store.connect() as db:
        rows = db.execute(
            "SELECT t.thread_id,t.chat_id,t.profile,t.owner,t.state,t.result,t.updated,"
            "COALESCE(length(t.history), 0) AS hist_len,"
            "(SELECT MIN(e0.at) FROM thread_events e0 WHERE e0.thread_id=t.thread_id) AS created_at,"
            "(SELECT json_extract(e.payload,'$.text') FROM thread_events e WHERE e.thread_id=t.thread_id AND e.kind='input' ORDER BY seq LIMIT 1) AS title "
            "FROM agent_threads t "
            + ("WHERE chat_id=? " if chat_id else "")
            + "ORDER BY updated DESC LIMIT 60",
            (chat_id,) if chat_id else (),
        ).fetchall()
        pending_map = _pending_approval_map(db, redact)
        try:
            ws_map = {
                r["thread_id"]: r["name"]
                for r in db.execute(
                    "SELECT thread_id,name FROM agent_workspaces WHERE thread_id IS NOT NULL "
                    "UNION SELECT c.thread_id,w.name FROM thread_context c "
                    "JOIN agent_workspaces w ON w.workspace_id=c.workspace_id "
                    "WHERE c.thread_id IS NOT NULL"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            ws_map = {}
        named_ws_ids = set(ws_map.keys())
        try:
            delivered_ids = {
                r["thread_id"]
                for r in db.execute(
                    "SELECT DISTINCT thread_id FROM result_deliveries"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            delivered_ids = set()
        try:
            attachment_ids = {
                r["thread_id"]
                for r in db.execute(
                    "SELECT DISTINCT thread_id FROM attachments WHERE thread_id IS NOT NULL"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            attachment_ids = set()
        threads = [
            {
                key: row[key]
                for key in (
                    "thread_id",
                    "chat_id",
                    "profile",
                    "owner",
                    "state",
                    "updated",
                )
            }
            | {
                "created": float(row["created_at"] or row["updated"] or 0.0),
                "title": redact(str(row["title"] or row["thread_id"]))[:120],
                "has_error": "error" in json.loads(row["result"]),
                "excerpt": _excerpt_of(row["result"], redact),
                "workspace": ws_map.get(row["thread_id"], ""),
                "delivered": row["thread_id"] in delivered_ids,
                "pending_approval": pending_map.get(row["thread_id"]),
                "total_tokens": max(0, int(row["hist_len"] or 0) // 3),
                "token_limit": context_tokens,
                "junk": _is_junk_thread_row(
                    row,
                    delivered_ids=delivered_ids,
                    named_ws_ids=named_ws_ids,
                    attachment_ids=attachment_ids,
                    workspace_root=workspace_root,
                ),
                "can_delete": (
                    row["state"] not in {"active", "waiting"}
                    and row["thread_id"] not in named_ws_ids
                ),
            }
            for row in rows
        ]
        suppress_fallback = thread_id == "__none__"
        if suppress_fallback:
            thread_id = ""
        selected = None
        events = []
        if not suppress_fallback and not thread_id and threads:
            preferred = (
                next((t for t in threads if t["state"] in {"active", "waiting"}), None)
                or next((t for t in threads if not t["junk"]), None)
                or threads[0]
            )
            thread_id = preferred["thread_id"]
        if thread_id:
            target = db.execute(
                "SELECT chat_id FROM agent_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if target is None or chat_id and target["chat_id"] != chat_id:
                raise ValueError("所选线程不存在于该会话")
            row = store.get(thread_id, target["chat_id"])
            profile = row["profile_snapshot"]
            selected = {
                key: row[key]
                for key in (
                    "thread_id",
                    "chat_id",
                    "profile",
                    "state",
                    "revision",
                    "processed_revision",
                )
            }
            selected["workspace"] = ws_map.get(thread_id, "")
            selected["can_delete"] = (
                row["state"] not in {"active", "waiting"}
                and thread_id not in named_ws_ids
            )
            selected["result"] = {
                key: str(redact(value))[:16000] for key, value in row["result"].items()
            }
            raw_atts = row["result"].get("attachments") if isinstance(row["result"], dict) else None
            selected["attachments"] = [
                {
                    "handle": str(a.get("handle") or "")[:80],
                    "name": str(redact(str(a.get("name") or "")))[:160],
                    "kind": str(a.get("kind") or "file")[:32],
                    "bytes": int(a.get("bytes") or a.get("size") or 0),
                }
                for a in (raw_atts if isinstance(raw_atts, list) else [])
                if isinstance(a, dict) and str(a.get("handle") or "").startswith("att_")
            ][:16]
            profile_text = str(profile.get("instructions", ""))
            notes_text = ""
            mem_ctx = {}
            profile_for_budget = None
            if memory is not None:
                try:
                    profile_obj = memory.profiles.get(row["profile"])
                    profile_for_budget = profile_obj
                    profile_text = profile_obj.instructions
                    notes_text = memory.methods(profile_obj)
                except (ValueError, OSError, RuntimeError, sqlite3.Error):
                    pass
                try:
                    mem_ctx = memory.context(
                        "" if row["owner"] == "self" else row["chat_id"]
                    )
                except (ValueError, OSError, RuntimeError, sqlite3.Error):
                    mem_ctx = {}
            if profile_for_budget is None and isinstance(profile, dict):
                try:
                    valid_fields = set(Profile.__dataclass_fields__)
                    profile_for_budget = Profile(
                        **{k: v for k, v in profile.items() if k in valid_fields}
                    )
                except (TypeError, ValueError):
                    profile_for_budget = None
            budget = resolve_context_budget(
                profile_for_budget,
                default_context_tokens=context_tokens,
                model_catalog=model_catalog,
            )
            system_chars = len(profile_text) + len(notes_text)
            memory_chars = (
                len(row["summary"])
                + len(mem_ctx.get("memory", ""))
                + len(mem_ctx.get("journal", ""))
                + len(mem_ctx.get("heartbeat", ""))
            )
            sizes = {
                "system": system_chars,
                "memory": memory_chars,
                **{
                    role: sum(
                        len(json.dumps(item, ensure_ascii=False))
                        for item in row["history"]
                        if item.get("role") == role
                    )
                    for role in ("user", "assistant", "tool")
                },
            }
            system_tokens = estimate_tokens(profile_text) + estimate_tokens(notes_text)
            memory_tokens = (
                estimate_tokens(row["summary"])
                + estimate_tokens(mem_ctx.get("memory", ""))
                + estimate_tokens(mem_ctx.get("journal", ""))
                + estimate_tokens(mem_ctx.get("heartbeat", ""))
            )
            token_sizes = {
                "system": system_tokens,
                "memory": memory_tokens,
                "user": 0,
                "assistant": 0,
                "tool": 0,
            }
            for item in row["history"]:
                cat_role = (
                    item.get("role")
                    if item.get("role") in {"user", "assistant", "tool"}
                    else "assistant"
                )
                token_sizes[cat_role] += estimate_message_tokens(item)
            context_items = []
            if profile_text.strip():
                context_items.append(
                    {
                        "id": "sys-profile",
                        "cat": "system",
                        "label": f"能力说明 ({row['profile']})",
                        "chars": len(profile_text),
                        "tokens": estimate_tokens(profile_text),
                        "preview": str(redact(profile_text))[:800],
                    }
                )
            if notes_text.strip():
                context_items.append(
                    {
                        "id": "sys-notes",
                        "cat": "system",
                        "label": "方法笔记 (NOTES.md)",
                        "chars": len(notes_text),
                        "tokens": estimate_tokens(notes_text),
                        "preview": str(redact(notes_text))[:800],
                    }
                )
            if row["summary"]:
                context_items.append(
                    {
                        "id": "mem-summary",
                        "cat": "memory",
                        "label": "早期历史归档摘要",
                        "chars": len(row["summary"]),
                        "tokens": estimate_tokens(row["summary"]),
                        "preview": str(redact(row["summary"]))[:800],
                    }
                )
            for mem_key, mem_label in (
                ("memory", "会话备忘 (MEMORY.md)"),
                ("journal", "近期手账 (JOURNAL.md)"),
                ("heartbeat", "探索节奏 (HEARTBEAT.md)"),
            ):
                mem_val = str(mem_ctx.get(mem_key) or "")
                if mem_val.strip():
                    context_items.append(
                        {
                            "id": f"mem-{mem_key}",
                            "cat": "memory",
                            "label": mem_label,
                            "chars": len(mem_val),
                            "tokens": estimate_tokens(mem_val),
                            "preview": str(redact(mem_val))[:800],
                        }
                    )
            history_slice = row["history"][-30:]
            base_idx = max(0, len(row["history"]) - len(history_slice))
            for offset_idx, item in enumerate(history_slice):
                if len(context_items) >= 40:
                    break
                role = str(item.get("role") or "")
                cat = role if role in {"user", "assistant", "tool"} else "assistant"
                context_items.append(
                    {
                        "id": f"hist-{base_idx + offset_idx}",
                        "cat": cat,
                        "label": _history_label(item, base_idx + offset_idx + 1),
                        "chars": len(json.dumps(item, ensure_ascii=False)),
                        "tokens": estimate_message_tokens(item),
                        "preview": _history_preview(item, redact),
                    }
                )
            rev_rows = db.execute(
                "SELECT revision,SUM(MIN(LENGTH(payload),24000)) AS span,MAX(at) AS ts "
                "FROM thread_events WHERE thread_id=? GROUP BY revision ORDER BY revision DESC LIMIT 24",
                (thread_id,),
            ).fetchall()
            has_pruned = (
                db.execute(
                    'SELECT 1 FROM thread_events WHERE thread_id=? AND kind="context_prune" LIMIT 1',
                    (thread_id,),
                ).fetchone()
                is not None
            )
            running = system_chars + memory_chars
            running_tokens = system_tokens + memory_tokens
            total_chars = sum(sizes.values())
            total_tokens = sum(token_sizes.values())
            history_points = []
            for rev_row in reversed(rev_rows):
                span = int(rev_row["span"] or 0)
                running += span
                running_tokens += max(1, span // 3) if span > 0 else 0
                history_points.append(
                    {
                        "rev": rev_row["revision"],
                        "chars": min(running, total_chars or running),
                        "tokens": min(running_tokens, total_tokens or running_tokens),
                        "at": rev_row["ts"],
                    }
                )
            if not history_points and (total_chars > 0 or total_tokens > 0):
                history_points = [
                    {
                        "rev": row["revision"],
                        "chars": total_chars,
                        "tokens": total_tokens,
                        "at": row["updated"],
                    }
                ]
            elif history_points:
                history_points[-1]["chars"] = total_chars
                history_points[-1]["tokens"] = total_tokens
            selected["context"] = {
                "messages": len(row["history"]),
                "chars": total_chars,
                "limit": context_limit,
                "roles": sizes,
                "tokens": total_tokens,
                "token_limit": budget["window_tokens"],
                "trigger_tokens": budget["trigger_tokens"],
                "prune_tokens": budget["prune_tokens"],
                "budget_source": budget["source"],
                "token_roles": token_sizes,
                "pruned": has_pruned,
                "archived": bool(row["summary"]),
                "items": context_items,
                "history_points": history_points,
            }
            explicit_models = list(
                dict.fromkeys(
                    m.strip()
                    for m in (
                        profile.get("model_name") or profile.get("model") or "",
                        *(profile.get("fallback_models") or ()),
                    )
                    if isinstance(m, str) and m.strip()
                )
            )
            task_slot = profile.get("task") or "utils"
            selected["model_chain"] = explicit_models
            if explicit_models:
                selected["model"] = (
                    f"{explicit_models[0]} (+{len(explicit_models) - 1}备选)"
                    if len(explicit_models) > 1
                    else explicit_models[0]
                )
            else:
                selected["model"] = task_slot
            selected["tools"] = profile.get("tools", [])
            selected["pending_approval"] = pending_map.get(thread_id)
            selected["steps"] = _build_steps(db, thread_id, redact)
            selected["artifacts"] = _thread_artifacts(db, row, selected, redact)
            for t_item in threads:
                if t_item["thread_id"] == thread_id:
                    t_item["total_tokens"] = int(total_tokens)
                    t_item["token_limit"] = int(budget["window_tokens"])
                    break
            records = db.execute(
                "SELECT seq,kind,revision,payload,at FROM thread_events WHERE thread_id=? AND seq>? ORDER BY seq LIMIT 30",
                (thread_id, cursor),
            ).fetchall()
            for event in records:
                text = redact(event["payload"])
                events.append(
                    {key: event[key] for key in ("seq", "kind", "revision", "at")}
                    | {"text": text[:2000], "truncated": len(text) > 2000}
                )
            if records:
                cursor = records[-1]["seq"]
        intents = [
            dict(item)
            for item in db.execute(
                "SELECT i.thread_id,i.revision,i.origin,i.consumed,g.wake_status FROM return_intents i JOIN return_groups g ON g.group_id=i.group_id "
                + ("WHERE i.thread_id=? " if thread_id else "")
                + "ORDER BY i.event_seq DESC LIMIT 20",
                (thread_id,) if thread_id else (),
            ).fetchall()
        ]
        try:
            if thread_id:
                intent_where = "WHERE status IN ('pending','paused') AND (thread_id=? OR target=?) "
                intent_params = (thread_id, "chat:" + selected["chat_id"])
            elif chat_id:
                intent_where = "WHERE status IN ('pending','paused') AND target=? "
                intent_params = ("chat:" + chat_id,)
            else:
                intent_where = "WHERE status IN ('pending','paused') "
                intent_params = ()
            try:
                intent_rows = db.execute(
                    "SELECT intent_id,kind,source,target,due_at,status,wake_status,reason,thread_id,"
                    "payload,mode,condition,last_checked_at,last_triggered_at "
                    "FROM intents "
                    + intent_where
                    + "ORDER BY due_at ASC, created DESC LIMIT 32",
                    intent_params,
                ).fetchall()
            except sqlite3.OperationalError:
                intent_rows = db.execute(
                    "SELECT intent_id,kind,source,target,due_at,status,reason,thread_id,payload "
                    "FROM intents "
                    + intent_where
                    + "ORDER BY due_at ASC, created DESC LIMIT 32",
                    intent_params,
                ).fetchall()
            scheduled_intents = []
            for item in intent_rows:
                keys = set(item.keys())
                payload_obj = _safe_json_loads(item["payload"] if "payload" in keys else "{}", {})
                if not isinstance(payload_obj, dict):
                    payload_obj = {}
                mode = str((item["mode"] if "mode" in keys else "") or payload_obj.get("mode") or ("watch" if item["source"] == "watch" else ("recurring" if (payload_obj.get("every_seconds") or payload_obj.get("daily_at")) else "schedule")))
                target_str = str(item["target"] or "")
                chat_target = target_str.split(":", 1)[1] if ":" in target_str else target_str
                scheduled_intents.append(
                    {
                        "intent_id": item["intent_id"],
                        "kind": item["kind"],
                        "mode": mode,
                        "source": item["source"],
                        "target": target_str,
                        "chat_id": chat_target,
                        "due_at": item["due_at"],
                        "condition": str(redact((item["condition"] if "condition" in keys else "") or payload_obj.get("condition") or ""))[:300],
                        "last_checked_at": item["last_checked_at"] if "last_checked_at" in keys else None,
                        "last_triggered_at": item["last_triggered_at"] if "last_triggered_at" in keys else None,
                        "status": item["status"],
                        "wake_status": item["wake_status"] if "wake_status" in keys else "",
                        "reason": str(redact(item["reason"]))[:200],
                        "thread_id": item["thread_id"],
                        "workspace": str(payload_obj.get("workspace") or ""),
                        "profile": str(payload_obj.get("profile") or ""),
                        "every_seconds": payload_obj.get("every_seconds"),
                        "daily_at": payload_obj.get("daily_at") or "",
                        "protected": bool(payload_obj.get("scheduled_admin")),
                        "kind_label": "条件盯梢" if mode == "watch" else ("周期计划" if (payload_obj.get("every_seconds") or payload_obj.get("daily_at")) else "单次安排"),
                    }
                )
        except sqlite3.OperationalError:
            scheduled_intents = []
        watches = [item for item in scheduled_intents if item.get("mode") == "watch"]
        if thread_id:
            deliveries = [
                dict(item)
                for item in db.execute(
                    "SELECT route,status,message_id,at FROM result_deliveries WHERE thread_id=? ORDER BY revision DESC LIMIT 5",
                    (thread_id,),
                ).fetchall()
            ]
        else:
            deliveries = []
        since = time.time() - 86400
        try:
            active_count = db.execute(
                "SELECT COUNT(*) AS c FROM agent_threads WHERE state IN ('active','waiting')"
            ).fetchone()["c"]
            today_threads = db.execute(
                "SELECT COUNT(*) AS c FROM agent_threads WHERE updated>=?", (since,)
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            active_count = sum(1 for item in threads if item["state"] in {"active", "waiting"})
            today_threads = len(threads)
        try:
            today_deliveries = db.execute(
                "SELECT COUNT(*) AS c FROM result_deliveries WHERE at>=?", (since,)
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            today_deliveries = len(deliveries)
        try:
            pending_intents = db.execute(
                "SELECT COUNT(*) AS c FROM intents WHERE status='pending'"
            ).fetchone()["c"]
        except sqlite3.OperationalError:
            pending_intents = sum(1 for item in scheduled_intents if item.get("status") == "pending")
        stats = {
            "active_count": active_count,
            "today_threads": today_threads,
            "today_deliveries": today_deliveries,
            "pending_intents": pending_intents,
            "error_count": sum(1 for item in threads if item["has_error"]),
        }
    return {
        "success": True,
        "threads": threads,
        "selected": selected,
        "events": events,
        "cursor": cursor,
        "intents": intents,
        "scheduled_intents": scheduled_intents,
        "watches": watches,
        "deliveries": deliveries,
        "stats": stats,
    }


def read_event(store, thread_id, event_seq, offset, redact):
    if (
        not isinstance(thread_id, str)
        or len(thread_id) > 64
        or type(event_seq) is not int
        or event_seq < 1
        or type(offset) is not int
        or offset < 0
    ):
        raise ValueError("事件回读参数无效")
    with store.connect() as db:
        row = db.execute(
            "SELECT payload FROM thread_events WHERE thread_id=? AND seq=?",
            (thread_id, event_seq),
        ).fetchone()
    if not row:
        raise ValueError("事件不属于所选线程")
    text = redact(row["payload"])
    end = min(len(text), offset + 6000)
    return {
        "success": True,
        "text": text[offset:end],
        "next_offset": end if end < len(text) else None,
        "total_chars": len(text),
    }
