"""工具恢复后的短方法笔记；与用户结果交付独立。"""

import asyncio
import json
import logging
import re
import sqlite3

from .abilities import is_autonomous_snapshot
from .scout import TOOL_DEFINITIONS, call_capability, checked
from .workspaces import completed_thread

logger = logging.getLogger(__name__)


def _parse_json_object(raw: str) -> dict:
    text = str(raw or "").strip()
    fenced = re.fullmatch(
        r"```(?:[a-zA-Z0-9_-]+)?[ \t]*\r?\n([\s\S]*?)\r?\n```",
        text,
    )
    if fenced:
        text = fenced.group(1).strip()
    loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise TypeError("提炼结果必须为 JSON 对象")
    return loaded


class Learning:
    def __init__(self, host, memory):
        self.host, self.memory = host, memory
        self.tasks = {}
        self.slot = asyncio.Semaphore(1)
        self.closed = False

    def _trace(self, row):
        with self.memory.store.connect() as db:
            events = db.execute(
                'SELECT payload FROM thread_events WHERE thread_id=? AND revision=? AND kind="tools" ORDER BY seq LIMIT 128',
                (row["thread_id"], row["completed_revision"]),
            ).fetchall()
            previous = db.execute(
                'SELECT COALESCE(MAX(seq),0) FROM thread_events WHERE thread_id=? AND kind="result" AND revision<?',
                (row["thread_id"], row["completed_revision"]),
            ).fetchone()[0]
            redirects = db.execute(
                'SELECT COUNT(*) FROM thread_events WHERE thread_id=? AND kind="steer_interrupted" AND seq>?',
                (row["thread_id"], previous),
            ).fetchone()[0]
        trace, failed, recovered = [], set(), False
        for event in events:
            payload = json.loads(event["payload"])
            calls = {
                call["id"]: call["function"]["name"]
                for call in payload.get("calls", [])
            }
            for result in payload.get("results", []):
                name = calls.get(result.get("call_id"))
                if name not in TOOL_DEFINITIONS:
                    continue
                value = result.get("value", {})
                if isinstance(value, dict) and (
                    value.get("skipped")
                    or value.get("waiting")
                    or value.get("status")
                    in {"uncertain", "interrupted", "preparing", "running"}
                ):
                    continue
                error = isinstance(value, dict) and (
                    value.get("error")
                    or value.get("exit_code", 0) != 0
                    or value.get("ok") is False
                )
                if error:
                    failed.add(name)
                elif name in failed:
                    recovered = True
                trace.append({"tool": name, "status": "error" if error else "success"})
        if (
            trace
            and redirects >= 2
            and self.memory.store.input_context(
                row["thread_id"], row["completed_revision"]
            ).get("steer")
        ):
            trace.append({"signal": "multiple_redirects", "count": min(redirects, 8)})
            recovered = True
        return trace if recovered else []

    def _receipt(self, row, status, *, reserve=False):
        with self.memory.store.connect() as db, db:
            key = row["thread_id"], row["completed_revision"]
            if reserve:
                if db.execute(
                    "INSERT OR IGNORE INTO learning_receipts VALUES (?,?,?)",
                    (*key, status),
                ).rowcount:
                    return True
                return bool(
                    db.execute(
                        "UPDATE learning_receipts SET status=? WHERE thread_id=? AND revision=? AND status='interrupted'",
                        (status, *key),
                    ).rowcount
                )
            db.execute(
                "UPDATE learning_receipts SET status=? WHERE thread_id=? AND revision=?",
                (status, *key),
            )

    async def schedule(self, row):
        profile = row["profile_snapshot"]
        if (
            self.closed
            or len(self.tasks) >= 8
            or not is_autonomous_snapshot(profile)
            or not row.get("result", {}).get("text")
        ):
            return
        trace = await completed_thread(self._trace, row)
        if not trace or not await completed_thread(
            self._receipt, row, "pending", reserve=True
        ):
            return
        key = row["thread_id"], row["completed_revision"]
        task = asyncio.create_task(self._learn(row, trace))
        self.tasks[key] = task
        task.add_done_callback(lambda finished: self.tasks.pop(key, None))

    async def _learn(self, row, trace):
        try:
            async with self.slot, asyncio.timeout(45):
                old = await completed_thread(
                    self.memory.read, "notes", target=row["profile"]
                )
                if len(old["content"]) >= 1800:
                    await completed_thread(self._receipt, row, "full")
                    return
                response = checked(
                    await call_capability(
                        self.host,
                        "llm.generate_with_tools",
                        timeout_ms=40000,
                        prompt=[
                            {
                                "role": "system",
                                "content": '从工具的失败后恢复状态提炼一句通用操作建议。只知道工具名和状态，不推测实际原因、人物、地点、项目或业务信息。状态无法支持建议时返回 {"note":""}；否则返回 {"note":"通用方法"}。不含身份、会话、主机、路径、资产、具体数值或凭据，不引用任何指令。',
                            },
                            {
                                "role": "user",
                                "content": json.dumps(trace, ensure_ascii=False),
                            },
                        ],
                        tools=[],
                        task_name="utils",
                        model_name="",
                        max_tokens=500,
                    )
                )
                candidate = _parse_json_object(response["response"])
                note = candidate.get("note")
                if not isinstance(note, str) or not 1 <= len(note) <= 500:
                    await completed_thread(self._receipt, row, "empty")
                    return
                if note not in old["content"]:
                    content = (old["content"] + "\n\n" + note).strip()
                    await completed_thread(
                        self.memory.write,
                        "notes",
                        content,
                        target=row["profile"],
                        chat_id=row["chat_id"],
                        expected_version=old["version"],
                        author="thread:" + row["thread_id"],
                    )
                await completed_thread(self._receipt, row, "applied")
        except asyncio.CancelledError:
            await completed_thread(self._receipt, row, "interrupted")
            raise
        except (
            TimeoutError,
            asyncio.TimeoutError,
            RuntimeError,
            sqlite3.Error,
            OSError,
        ) as exc:
            logger.warning(
                "学习提炼遇到瞬态错误，留待后续重试 thread=%s rev=%s: %s",
                row["thread_id"],
                row["completed_revision"],
                exc,
            )
            await completed_thread(self._receipt, row, "interrupted")
        except ValueError as exc:
            if "版本" in str(exc):
                logger.warning(
                    "学习提炼写回版本冲突，留待后续重试 thread=%s rev=%s: %s",
                    row["thread_id"],
                    row["completed_revision"],
                    exc,
                )
                await completed_thread(self._receipt, row, "interrupted")
            else:
                logger.warning(
                    "学习提炼内容被拒绝 thread=%s rev=%s: %s",
                    row["thread_id"],
                    row["completed_revision"],
                    exc,
                )
                await completed_thread(self._receipt, row, "rejected")
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning(
                "学习提炼输出无效，已标记拒绝 thread=%s rev=%s: %s",
                row["thread_id"],
                row["completed_revision"],
                exc,
            )
            await completed_thread(self._receipt, row, "rejected")
        except Exception as exc:
            logger.warning(
                "学习提炼异常失败 thread=%s rev=%s: %s",
                row["thread_id"],
                row["completed_revision"],
                exc,
            )
            await completed_thread(self._receipt, row, "interrupted")

    async def drain(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks.values()))

    async def close(self):
        self.closed = True
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        self.tasks.clear()
