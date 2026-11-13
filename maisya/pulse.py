"""意图唤醒入口：回接用户线程结果、处理到期的提醒与主动意图。"""

import asyncio
import html
import json
import logging
import sqlite3


class Pulse:
    def __init__(
        self,
        bridge,
        host,
        enabled,
        intents=None,
        engine=None,
        *,
        sleep_window="",
        proactive_level="care",
        redact=lambda v: v,
    ):
        self.bridge, self.host, self.enabled = bridge, host, enabled
        self.intents, self.engine = intents, engine
        self.sleep_window, self.redact = sleep_window, redact
        self.proactive_level = proactive_level
        self.task = None
        self.closing = False

    async def tick(self):
        if self.closing:
            return
        await self._returns()
        if self.intents is not None:
            await self._intents()

    async def _returns(self):
        batches = await asyncio.to_thread(self.bridge.claim_ready, self.enabled)
        for chat_id, rows in batches.items():
            rows = await asyncio.to_thread(self.bridge.current, rows, pending=True)
            if not rows:
                continue
            if self.closing or not all(
                self.enabled(chat_id, row["platform"]) for row in rows
            ):
                continue
            facts = html.escape(
                json.dumps(
                    self.bridge.redact(self.bridge.material(rows)), ensure_ascii=False
                )
            )
            intent = (
                "之前由本会话真实用户消息触发的 Agent 线程已有结果。结合当前对话判断是否开口；"
                "纯文字通过原生 reply 表达，事实将单次交给 Replyer，也可以静默 stop。"
                "结果仍可通过 maisya_recall 查阅。\n<maisya_return>"
                + facts
                + "</maisya_return>"
            )
            status = await self._wake(
                chat_id,
                intent,
                reason="maisya:return",
                metadata={
                    "kind": "return",
                    "groups": sorted({row["group_id"] for row in rows}),
                },
            )
            await asyncio.to_thread(self.bridge.wake_status, rows, status)

    async def _intents(self):
        rows = await asyncio.to_thread(self.intents.due, self.enabled)
        for row in rows:
            if self.closing:
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred"
                )
                continue
            try:
                await self._dispatch(row)
            except (RuntimeError, OSError, ValueError, TypeError, sqlite3.Error):
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred"
                )

    async def _dispatch(self, row):
        target = row["target"]
        chat_id = target.split(":", 1)[1]
        platform = row["payload"].get("platform", "")
        if row["kind"] == "silent":
            await self._silent(row)
            return
        if not self.enabled(chat_id, platform):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        if row["kind"] == "proactive":
            allowed, reason = self._proactive_gate(row)
            if not allowed:
                status = "deferred" if reason == "sleep_window" else "failed"
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], status, reason
                )
                return
        text = html.escape(self.redact(row["reason"]))
        if row["kind"] == "proactive":
            intent = (
                "这是你自己之前想对会话提起的惦记，或计划中的一次自然冒泡。"
                "结合当前对话判断是否值得开口；可以自然提起，也可以静默 stop。"
                "\n<maisya_proactive>" + text + "</maisya_proactive>"
            )
        else:
            intent = (
                "这是你之前通过 maisya_later 安排的提醒，现在到期。"
                "结合当前对话用一两句自然提起；如果已无意义可静默 stop。"
                "\n<maisya_reminder>" + text + "</maisya_reminder>"
            )
        status = await self._wake(
            chat_id,
            intent
            + (
                "\n<external_event>"
                + html.escape(
                    json.dumps(self.redact(row["payload"]["event"]), ensure_ascii=False)
                )
                + "</external_event>"
                if row["source"] == "webhook" and "event" in row["payload"]
                else ""
            ),
            reason="maisya:" + row["source"],
            metadata={
                "kind": row["kind"],
                "source": row["source"],
                "intent_id": row["intent_id"],
            },
        )
        if status == "accepted" and row["kind"] == "proactive":
            await asyncio.to_thread(self.intents.mark_proactive, target)
        done = "done" if status == "accepted" else "failed"
        await asyncio.to_thread(self.intents.finish, row["intent_id"], done, status)

    async def _silent(self, row):
        thread_id = row["thread_id"]
        if not thread_id:
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")
            return
        try:
            target_chat = row["target"].split(":", 1)[1]
            thread = await asyncio.to_thread(
                self.bridge.store.get,
                thread_id,
                target_chat,
                allow_self=target_chat == "self",
            )
        except (ValueError, PermissionError):
            thread = None
        if thread is None or thread["state"] == "closed":
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "done", "thread_closed"
            )
            return
        profile = thread["profile_snapshot"]
        if (
            profile.get("access") not in {"everyone", "listed"}
            or profile.get("allow_users")
            or profile.get("secrets")
            or profile.get("template")
        ):
            await asyncio.to_thread(
                self.intents.finish,
                row["intent_id"],
                "failed",
                "fresh_authorization_required",
            )
            return
        if thread["owner"] != "self" and not self.enabled(
            thread["chat_id"], thread["platform"]
        ):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        if self.engine is None:
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        if self.engine.tasks.get(thread_id) or thread["state"] == "active":
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        try:
            chat_id = thread["chat_id"] if thread["owner"] != "self" else "self"
            prompt = row["payload"].get("next_step") or row["reason"]
            if row["source"] == "webhook" and "event" in row["payload"]:
                prompt += (
                    "\n<external_event>"
                    + html.escape(
                        json.dumps(
                            self.redact(row["payload"]["event"]), ensure_ascii=False
                        )
                    )
                    + "</external_event>"
                )
            await self.engine.send(
                thread_id,
                prompt,
                chat_id=chat_id,
                wait_seconds=0,
                input_context={"grant": {}},
            )
        except (ValueError, PermissionError, RuntimeError, sqlite3.Error):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")

    def _proactive_gate(self, row):
        if self.proactive_level == "quiet":
            return False, "quiet_level"
        source = row["source"]
        if self.proactive_level == "care" and source == "serendipity":
            return False, "care_blocks_serendipity"
        allowed, reason = self.intents.social_gate(
            row["target"], sleep_window=self.sleep_window
        )
        if not allowed:
            return allowed, reason
        chat_id = row["target"].split(":", 1)[1]
        if not self.intents.known_chat(chat_id):
            return False, "unfamiliar_chat"
        return True, ""

    async def _wake(self, chat_id, intent, *, reason, metadata):
        try:
            result = await self.host.call_capability(
                "maisaka.proactive.trigger",
                stream_id=chat_id,
                intent=intent,
                reason=reason,
                metadata=metadata,
            )
            accepted = bool(result) and (
                not isinstance(result, dict) or result.get("success", True)
            )
            return "accepted" if accepted else "rejected"
        except (RuntimeError, OSError, ValueError, TypeError):
            return "uncertain"

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while not self.closing:
            await asyncio.sleep(1)
            try:
                await self.tick()
            except (sqlite3.Error, OSError, RuntimeError):
                logging.getLogger(__name__).warning(
                    "Maisya 回接检查暂未完成，结果保留在线程中。"
                )

    async def close(self):
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
