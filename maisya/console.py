"""管理台只读快照；只读取插件自己的数据。"""

import json
import sqlite3


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
            selected["result"] = {
                key: str(redact(value))[:16000] for key, value in row["result"].items()
            }
            system_chars = len(str(profile.get("instructions", "")))
            if memory is not None:
                try:
                    profile_obj = memory.profiles.get(row["profile"])
                    system_chars = len(profile_obj.instructions) + len(
                        memory.methods(profile_obj)
                    )
                except (ValueError, OSError, RuntimeError, sqlite3.Error):
                    pass
            memory_chars = len(row["summary"])
            if memory is not None:
                try:
                    mem_ctx = memory.context(
                        "" if row["owner"] == "self" else row["chat_id"]
                    )
                    memory_chars += (
                        len(mem_ctx.get("memory", ""))
                        + len(mem_ctx.get("journal", ""))
                        + len(mem_ctx.get("heartbeat", ""))
                    )
                except (ValueError, OSError, RuntimeError, sqlite3.Error):
                    pass
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
            selected["context"] = {
                "messages": len(row["history"]),
                "chars": sum(sizes.values()),
                "limit": context_limit,
                "roles": sizes,
                "archived": bool(row["summary"]),
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
    return {
        "success": True,
        "threads": threads,
        "selected": selected,
        "events": events,
        "cursor": cursor,
        "intents": intents,
        "scheduled_intents": scheduled_intents,
        "deliveries": deliveries,
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
