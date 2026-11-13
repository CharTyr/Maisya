"""管理台只读快照；只读取插件自己的数据。"""

import json
import sqlite3
import time


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
    redact=lambda value: value,
    memory=None,
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
            "(SELECT json_extract(e.payload,'$.text') FROM thread_events e WHERE e.thread_id=t.thread_id AND e.kind='input' ORDER BY seq LIMIT 1) AS title "
            "FROM agent_threads t "
            + ("WHERE chat_id=? " if chat_id else "")
            + "ORDER BY updated DESC LIMIT 60",
            (chat_id,) if chat_id else (),
        ).fetchall()
        try:
            ws_map = {
                r["thread_id"]: r["name"]
                for r in db.execute(
                    "SELECT thread_id,name FROM agent_workspaces"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            ws_map = {}
        try:
            delivered_ids = {
                r["thread_id"]
                for r in db.execute(
                    "SELECT DISTINCT thread_id FROM result_deliveries"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            delivered_ids = set()
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
                "title": redact(str(row["title"] or row["thread_id"]))[:120],
                "has_error": "error" in json.loads(row["result"]),
                "excerpt": _excerpt_of(row["result"], redact),
                "workspace": ws_map.get(row["thread_id"], ""),
                "delivered": row["thread_id"] in delivered_ids,
            }
            for row in rows
        ]
        selected = None
        events = []
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
            selected["result"] = {
                key: str(redact(value))[:16000] for key, value in row["result"].items()
            }
            profile_text = str(profile.get("instructions", ""))
            notes_text = ""
            mem_ctx = {}
            if memory is not None:
                try:
                    profile_obj = memory.profiles.get(row["profile"])
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
            context_items = []
            if profile_text.strip():
                context_items.append(
                    {
                        "id": "sys-profile",
                        "cat": "system",
                        "label": f"能力说明 ({row['profile']})",
                        "chars": len(profile_text),
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
                        "preview": _history_preview(item, redact),
                    }
                )
            rev_rows = db.execute(
                "SELECT revision,SUM(MIN(LENGTH(payload),24000)) AS span,MAX(at) AS ts "
                "FROM thread_events WHERE thread_id=? GROUP BY revision ORDER BY revision DESC LIMIT 24",
                (thread_id,),
            ).fetchall()
            running = system_chars + memory_chars
            history_points = []
            for rev_row in reversed(rev_rows):
                running += int(rev_row["span"] or 0)
                history_points.append(
                    {
                        "rev": rev_row["revision"],
                        "chars": min(running, sum(sizes.values()) or running),
                        "at": rev_row["ts"],
                    }
                )
            if not history_points and sum(sizes.values()) > 0:
                history_points = [
                    {
                        "rev": row["revision"],
                        "chars": sum(sizes.values()),
                        "at": row["updated"],
                    }
                ]
            elif history_points:
                history_points[-1]["chars"] = sum(sizes.values())
            selected["context"] = {
                "messages": len(row["history"]),
                "chars": sum(sizes.values()),
                "limit": context_limit,
                "roles": sizes,
                "archived": bool(row["summary"]),
                "items": context_items,
                "history_points": history_points,
            }
            selected["model"] = (
                profile.get("model") or profile.get("task") or "utils"
            )
            selected["tools"] = profile.get("tools", [])
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
                intent_where = "WHERE thread_id=? OR target=? "
                intent_params = (thread_id, "chat:" + selected["chat_id"])
            elif chat_id:
                intent_where = "WHERE target=? "
                intent_params = ("chat:" + chat_id,)
            else:
                intent_where = ""
                intent_params = ()
            scheduled_intents = [
                {
                    "intent_id": item["intent_id"],
                    "kind": item["kind"],
                    "source": item["source"],
                    "target": item["target"],
                    "due_at": item["due_at"],
                    "status": item["status"],
                    "reason": str(redact(item["reason"]))[:200],
                    "thread_id": item["thread_id"],
                }
                for item in db.execute(
                    "SELECT intent_id,kind,source,target,due_at,status,reason,thread_id "
                    "FROM intents "
                    + intent_where
                    + "ORDER BY created DESC LIMIT 12",
                    intent_params,
                ).fetchall()
            ]
        except sqlite3.OperationalError:
            scheduled_intents = []
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
