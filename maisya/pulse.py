"""意图唤醒入口：回接用户线程结果、处理到期的提醒与主动意图。"""

import asyncio
import html
import json
import logging
import re
import sqlite3

from .memory import MemoryLayers
from .scout import call_capability


def _extract_tokens(text):
    lower = str(text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_]{2,}", lower))
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", lower):
        for i in range(len(run) - 1):
            tokens.add(run[i : i + 2])
    return tokens


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
        chat_proactive_levels=None,
        acquaintance_users=None,
        admins=lambda: set(),
        enabled_chats=lambda: [],
        longing_quiet_hours=72,
        activity=None,
        platform_for=None,
        redact=lambda v: v,
        routes=None,
        memory=None,
        profiles=None,
        plugin_enabled=lambda: True,
        heartbeat_interval=3600,
        heartbeat_profile="scout",
    ):
        self.bridge, self.host, self.enabled = bridge, host, enabled
        self.intents, self.engine = intents, engine
        self.sleep_window, self.redact = sleep_window, redact
        self.proactive_level = proactive_level
        self.chat_proactive_levels = dict(chat_proactive_levels or {})
        self.acquaintance_users = list(acquaintance_users or [])
        self.admins = admins
        self.enabled_chats = enabled_chats
        self.longing_quiet_hours = longing_quiet_hours
        self.activity = activity
        self.platform_for = platform_for
        self.routes = routes
        self.memory = memory
        self.profiles = profiles
        self.plugin_enabled = plugin_enabled
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_profile = heartbeat_profile
        self.task = None
        self.closing = False

    def level_for(self, chat_id):
        return self.chat_proactive_levels.get(chat_id, self.proactive_level)

    def _admins(self):
        return self.admins() if callable(self.admins) else (self.admins or ())

    def _temperament_xml(self):
        if self.memory is None:
            return ""
        try:
            heartbeat = self.memory.read("heartbeat")
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            return ""
        temperament, _ = MemoryLayers.heartbeat_parts(heartbeat.get("content") or "")
        if not temperament:
            return ""
        return (
            "\n<maisya_temperament>"
            + html.escape(self.redact(temperament))
            + "</maisya_temperament>"
        )

    async def tick(self):
        if self.closing:
            return
        await self._returns()
        if self.intents is not None:
            await self._longing()
            await self._intents()
        await self._heartbeat()

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

    async def _reschedule_recurring(self, row):
        every = int((row.get("payload") or {}).get("every_seconds") or 0)
        if every < 300 or self.intents is None:
            return
        next_payload = dict(row["payload"])
        next_payload.pop("wait_thread", None)
        next_payload.pop("already_started", None)
        await asyncio.to_thread(
            self.intents.schedule,
            kind=row["kind"],
            source=row["source"],
            target=row["target"],
            due_at=self.intents.clock() + every,
            reason=row["reason"],
            thread_id="",
            payload=next_payload,
            bypass_limits=True,
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
        elif row["payload"].get("profile") and self.intents.in_sleep_window(self.sleep_window):
            await asyncio.to_thread(
                self.intents.finish, row["intent_id"], "deferred", "sleep_window"
            )
            return
        if (
            row["payload"].get("profile")
            and not row.get("thread_id")
            and self.engine is not None
            and self.profiles is not None
        ):
            ability = await asyncio.to_thread(self.profiles.get, row["payload"]["profile"])
            if not ability.enabled:
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "profile_disabled",
                )
                return
            if not ability.autonomous and not row["payload"].get("scheduled_admin"):
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "fresh_authorization_required",
                )
                return
            prompt = row["payload"].get("prompt") or row["reason"]
            started = await self.engine.start(
                ability,
                prompt,
                chat_id="self" if chat_id == "self" else chat_id,
                platform="self" if chat_id == "self" else (platform or "qq"),
                user_id="",
                owner="self" if chat_id == "self" else "chat",
                wait_seconds=5,
                workspace=row["payload"].get("workspace", ""),
                grant={"scheduled_admin": True}
                if row["payload"].get("scheduled_admin")
                else {},
            )
            updated_payload = {**row["payload"], "wait_thread": True}
            row = {**row, "thread_id": started["thread_id"], "payload": updated_payload}
            await asyncio.to_thread(
                self.intents.update_thread,
                row["intent_id"],
                started["thread_id"],
                updated_payload,
            )
        thread = None
        if row.get("thread_id"):
            try:
                thread = await asyncio.to_thread(
                    self.bridge.store.get, row["thread_id"], chat_id, allow_self=True
                )
            except (ValueError, PermissionError):
                thread = None
            if row["payload"].get("wait_thread") and (
                (self.engine and self.engine.tasks.get(row["thread_id"]))
                or (thread and thread["state"] == "active")
            ):
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred", delay=5
                )
                return
        is_workflow = bool(
            row["source"] == "workflow"
            or row["payload"].get("profile")
            or thread is not None
        )
        text = html.escape(self.redact(row["reason"]))
        if is_workflow:
            intent = (
                "预设或定时安排的角色工作流已完成本轮运行。结合当前对话与产出内容判断是否交付："
                "纯文字结论可直接用 reply 自然提起；若含准确链接、图片或文件附件，请调用 maisya_agent(action=\"share\") 交付后结束本轮；若无值得打扰的新内容可静默 stop。"
                "\n<maisya_workflow>" + text + "</maisya_workflow>"
            )
            if row["kind"] == "proactive":
                intent += await asyncio.to_thread(self._temperament_xml)
        elif row["kind"] == "proactive":
            intent = (
                "这是你自己之前想对会话提起的惦记，或计划中的一次自然冒泡。"
                "结合当前对话判断是否值得开口；可以自然提起，也可以静默 stop。"
                "\n<maisya_proactive>" + text + "</maisya_proactive>"
            )
            intent += await asyncio.to_thread(self._temperament_xml)
        else:
            intent = (
                "这是你之前通过 maisya_later 安排的提醒，现在到期。"
                "结合当前对话用一两句自然提起；如果已无意义可静默 stop。"
                "\n<maisya_reminder>" + text + "</maisya_reminder>"
            )
        if thread and isinstance(thread.get("result"), dict):
            result_obj = thread["result"]
            result_text = str(result_obj.get("text") or "")
            attachments = result_obj.get("attachments") or []
            from .share import links_in

            try:
                result_links = links_in(result_obj)
            except ValueError:
                result_links = []
            if result_text or attachments or result_links:
                await asyncio.to_thread(
                    self.bridge.stage,
                    thread,
                    chat_id,
                    archived=thread.get("owner") == "self",
                )
                intent += (
                    "\n<maisya_thread_result>"
                    + html.escape(self.redact(result_text))
                    + "</maisya_thread_result>"
                )
                hint_info = {"thread_id": str(thread["thread_id"])}
                if attachments:
                    handles = [
                        str(item.get("handle"))
                        for item in attachments
                        if isinstance(item, dict) and item.get("handle")
                    ]
                    if handles:
                        hint_info["attachment_handles"] = handles
                if result_links:
                    hint_info["links"] = result_links[:12]
                if attachments or result_links:
                    intent += (
                        "\n<maisya_delivery_hint>"
                        + html.escape(json.dumps(hint_info, ensure_ascii=False))
                        + "</maisya_delivery_hint>"
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
        done = (
            "done"
            if status == "accepted"
            else ("deferred" if status in {"session_missing", "uncertain"} else "failed")
        )
        await asyncio.to_thread(self.intents.finish, row["intent_id"], done, status)
        if done == "done":
            await self._reschedule_recurring(row)

    async def _silent(self, row):
        if row["payload"].get("already_started"):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")
            return
        target_chat = row["target"].split(":", 1)[1]
        if (
            row["payload"].get("profile")
            and not row.get("thread_id")
            and self.engine is not None
            and self.profiles is not None
        ):
            platform = row["payload"].get("platform", "")
            if target_chat != "self" and not self.enabled(target_chat, platform):
                await asyncio.to_thread(
                    self.intents.finish, row["intent_id"], "deferred"
                )
                return
            ability = await asyncio.to_thread(
                self.profiles.get, row["payload"]["profile"]
            )
            if not ability.enabled:
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "profile_disabled",
                )
                return
            if not ability.autonomous and not row["payload"].get("scheduled_admin"):
                await asyncio.to_thread(
                    self.intents.finish,
                    row["intent_id"],
                    "failed",
                    "fresh_authorization_required",
                )
                return
            await self.engine.start(
                ability,
                row["payload"].get("prompt") or row["reason"],
                chat_id="self" if target_chat == "self" else target_chat,
                platform="self" if target_chat == "self" else (platform or "qq"),
                user_id="",
                owner="self" if target_chat == "self" else "chat",
                wait_seconds=0,
                workspace=row["payload"].get("workspace", ""),
                grant={"scheduled_admin": True}
                if row["payload"].get("scheduled_admin")
                else {},
            )
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")
            await self._reschedule_recurring(row)
            return
        thread_id = row["thread_id"]
        if not thread_id:
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")
            return
        try:
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
        ) and not row["payload"].get("scheduled_admin"):
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
                input_context={
                    "grant": {"scheduled_admin": True}
                    if row["payload"].get("scheduled_admin")
                    else {}
                },
            )
        except (ValueError, PermissionError, RuntimeError, sqlite3.Error):
            await asyncio.to_thread(self.intents.finish, row["intent_id"], "deferred")
            return
        await asyncio.to_thread(self.intents.finish, row["intent_id"], "done")
        await self._reschedule_recurring(row)

    def _proactive_gate(self, row):
        chat_id = row["target"].split(":", 1)[1]
        level = self.level_for(chat_id)
        if level == "quiet":
            return False, "quiet_level"
        source = row["source"]
        if level == "care" and source == "serendipity":
            return False, "care_blocks_serendipity"
        allowed, reason = self.intents.social_gate(
            row["target"], sleep_window=self.sleep_window
        )
        if not allowed:
            return allowed, reason
        if not self.intents.acquaintance_allowed(
            chat_id,
            admins=self._admins(),
            acquaintances=self.acquaintance_users,
            payload=row.get("payload") or {},
        ):
            return False, "unfamiliar_chat"
        return True, ""

    def propose_serendipity(self, *, thread_id, due_at, reason):
        if self.intents is None or self.memory is None:
            return {"scheduled": False}
        tokens = _extract_tokens(reason)
        if not tokens:
            return {"scheduled": False}
        admins = self._admins()
        best = None
        for chat_id in self.enabled_chats():
            if self.level_for(chat_id) != "lively":
                continue
            platform = self.platform_for(chat_id) if callable(self.platform_for) else ""
            if not self.enabled(chat_id, platform):
                continue
            if not self.intents.acquaintance_allowed(
                chat_id,
                admins=admins,
                acquaintances=self.acquaintance_users,
                payload={"platform": platform},
            ):
                continue
            if not self.intents.social_gate("chat:" + chat_id, sleep_window="")[0]:
                continue
            try:
                content = (self.memory.read("memory", chat_id=chat_id).get("content") or "").lower()
            except (ValueError, OSError, RuntimeError, sqlite3.Error):
                continue
            score = sum(1 for tok in tokens if tok in content)
            if score > 0 and (best is None or score > best[0]):
                best = (score, chat_id, platform)
        if best is None:
            return {"scheduled": False}
        _, best_chat, best_platform = best
        try:
            row = self.intents.schedule(
                kind="proactive",
                source="serendipity",
                target="chat:" + best_chat,
                due_at=due_at,
                reason=reason,
                thread_id=thread_id,
                payload={"platform": best_platform},
            )
        except ValueError:
            return {"scheduled": False}
        return {"scheduled": True, "intent_id": row["intent_id"], "due_at": row["due_at"]}

    async def _longing(self):
        if (
            self.closing
            or not self.plugin_enabled()
            or self.intents is None
            or not callable(self.activity)
            or self.longing_quiet_hours <= 0
            or self.intents.in_sleep_window(self.sleep_window)
            or not await asyncio.to_thread(self.intents.proactive_quota_available)
        ):
            return
        now = self.intents.clock()
        threshold = self.longing_quiet_hours * 3600
        admins = self._admins()
        for chat_id in list(self.enabled_chats()):
            if self.level_for(chat_id) == "quiet":
                continue
            platform = (
                await asyncio.to_thread(self.platform_for, chat_id)
                if callable(self.platform_for)
                else ""
            )
            if not self.enabled(chat_id, platform):
                continue
            act = await asyncio.to_thread(self.activity, chat_id)
            if (
                act["human_count"] < 3
                or act["last_human_at"] <= 0
                or now - act["last_human_at"] < threshold
            ):
                continue
            allowed = await asyncio.to_thread(
                self.intents.acquaintance_allowed,
                chat_id,
                admins=admins,
                acquaintances=self.acquaintance_users,
                payload={"platform": platform},
            )
            if not allowed:
                continue
            ready = await asyncio.to_thread(
                self.intents.longing_ready, chat_id, now=now
            )
            if not ready:
                continue
            hours = int((now - act["last_human_at"]) // 3600)
            try:
                await asyncio.to_thread(
                    self.intents.schedule,
                    kind="proactive",
                    source="longing",
                    target="chat:" + chat_id,
                    due_at=now,
                    reason=f"这个熟识会话已经安静了约 {hours} 小时，看看是否自然问候一句。",
                    payload={"platform": platform},
                )
            except ValueError:
                pass
            break

    async def _wake(self, chat_id, intent, *, reason, metadata):
        try:
            result = await call_capability(
                self.host,
                "maisaka.proactive.trigger",
                timeout_ms=30000,
                stream_id=chat_id,
                intent=intent,
                reason=reason,
                metadata=metadata,
            )
            if (
                isinstance(result, dict)
                and not result.get("success", True)
                and "未找到已存在的聊天流" in str(result.get("error", ""))
            ):
                reopened = await self._reopen_session(chat_id)
                if reopened:
                    result = await call_capability(
                        self.host,
                        "maisaka.proactive.trigger",
                        timeout_ms=30000,
                        stream_id=chat_id,
                        intent=intent,
                        reason=reason,
                        metadata=metadata,
                    )
                if (
                    isinstance(result, dict)
                    and not result.get("success", True)
                    and "未找到已存在的聊天流" in str(result.get("error", ""))
                ):
                    return "session_missing"
            accepted = bool(result) and (
                not isinstance(result, dict) or result.get("success", True)
            )
            return "accepted" if accepted else "rejected"
        except (RuntimeError, OSError, ValueError, TypeError):
            return "uncertain"

    async def _reopen_session(self, chat_id):
        if not callable(self.routes):
            return False
        route = await asyncio.to_thread(self.routes, chat_id)
        if not route or not route.get("platform"):
            return False
        if route["chat_type"] == "group" and not route.get("group_id"):
            return False
        if route["chat_type"] == "private" and not route.get("user_id"):
            return False
        args = {
            "platform": route["platform"],
            "chat_type": route["chat_type"],
            "user_id": route.get("user_id", ""),
            "group_id": route.get("group_id", ""),
        }
        if route.get("account_id"):
            args["account_id"] = route["account_id"]
        if route.get("scope"):
            args["scope"] = route["scope"]
        opened = await call_capability(
            self.host, "chat.open_session", timeout_ms=15000, **args
        )
        return bool(opened) and (
            not isinstance(opened, dict) or opened.get("success", True)
        )

    def _claim_heartbeat(self, now):
        with self.intents.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT last_proactive FROM social_state WHERE chat_id='__self_heartbeat__'"
            ).fetchone()
            last_run = row["last_proactive"] if row else 0.0
            if now - last_run < self.heartbeat_interval:
                return False
            db.execute(
                "INSERT INTO social_state (chat_id, locked, last_proactive) "
                "VALUES ('__self_heartbeat__', 0, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "last_proactive=excluded.last_proactive",
                (now,),
            )
        return True

    async def _heartbeat(self):
        if (
            self.closing
            or not self.plugin_enabled()
            or self.memory is None
            or self.profiles is None
            or self.engine is None
            or self.intents is None
            or (
                self.proactive_level == "quiet"
                and not any(
                    v in {"care", "lively"} for v in self.chat_proactive_levels.values()
                )
            )
            or self.intents.in_sleep_window(self.sleep_window)
            or any(
                self.engine.chats.get(tid) == "self" for tid in self.engine.tasks
            )
        ):
            return
        try:
            heartbeat = await asyncio.to_thread(self.memory.read, "heartbeat")
            temperament, plan = MemoryLayers.heartbeat_parts(
                heartbeat.get("content") or ""
            )
            if not plan:
                return
            now = self.intents.clock()
            if not await asyncio.to_thread(self._claim_heartbeat, now):
                return
            ability = await asyncio.to_thread(
                self.profiles.get, self.heartbeat_profile
            )
            if not ability.autonomous:
                return
            prompt = (
                "按自身节奏做一次轻量公开探索并记录近况（不涉及任何具体会话私有信息）：\n"
                + plan
            )
            if temperament:
                prompt += "\n<maisya_temperament>" + html.escape(temperament) + "</maisya_temperament>"
            await self.engine.start(
                ability,
                prompt,
                chat_id="self",
                platform="self",
                user_id="",
                owner="self",
                wait_seconds=0,
                workspace="heartbeat",
            )
        except (ValueError, PermissionError, RuntimeError, OSError, sqlite3.Error):
            return

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def _run(self):
        while not self.closing:
            await asyncio.sleep(1)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "Maisya 回接检查暂未完成，结果保留在线程中。"
                )

    async def close(self):
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
