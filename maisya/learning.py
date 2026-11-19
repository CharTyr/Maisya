"""工具恢复后的短方法笔记；与用户结果交付独立。"""

import asyncio
from collections import Counter
import json
import logging
import re
import sqlite3

from .abilities import is_autonomous_snapshot
from .intents import extract_llm_tokens
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
    def __init__(self, host, memory, *, intents=None):
        self.host, self.memory, self.intents = host, memory, intents
        self.tasks = {}
        self.slot = asyncio.Semaphore(1)
        self.closed = False
        self.skip_stats: Counter[str] = Counter()
        self.scheduled_count: int = 0

    def diagnostics(self) -> dict:
        """返回自动经验提炼的调度与跳过原因计数，用于诊断触发阈值是否过严。"""
        return {
            "scheduled": self.scheduled_count,
            "active": len(self.tasks),
            "skipped": dict(self.skip_stats),
        }

    async def _record_llm_tokens(self, response: object) -> None:
        toks = extract_llm_tokens(response)
        rec_fn = (
            getattr(self.intents, "record_bg_usage", None)
            if self.intents is not None
            else None
        )
        if toks > 0 and callable(rec_fn):
            await completed_thread(
                lambda: rec_fn(source="learning", target="self", runs=0, tokens=toks)
            )

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
        trace, failed, recovered, success_tools = [], set(), False, []
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
                else:
                    success_tools.append(name)
                    if name in failed:
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
        if self.closed:
            self.skip_stats["closed"] += 1
            return
        if len(self.tasks) >= 8:
            self.skip_stats["queue_full"] += 1
            return
        if not is_autonomous_snapshot(profile):
            self.skip_stats["not_autonomous"] += 1
            logger.debug("learning skipped (not_autonomous): %s", row.get("thread_id"))
            return
        if not row.get("result", {}).get("text"):
            self.skip_stats["empty_text"] += 1
            logger.debug("learning skipped (empty_text): %s", row.get("thread_id"))
            return
        trace = await completed_thread(self._trace, row)
        if not trace:
            self.skip_stats["no_recovery_trace"] += 1
            logger.debug("learning skipped (no_recovery_trace): %s", row.get("thread_id"))
            return
        if not await completed_thread(self._receipt, row, "pending", reserve=True):
            self.skip_stats["already_processed"] += 1
            return
        self.scheduled_count += 1
        key = row["thread_id"], row["completed_revision"]
        task = asyncio.create_task(self._learn(row, trace))
        self.tasks[key] = task
        task.add_done_callback(lambda finished: self.tasks.pop(key, None))

    async def schedule_planner_signal(
        self,
        *,
        dedup_key: str,
        signal: str,
        actions: list[dict],
        profiles: list[dict],
    ):
        """从抽象调度轨迹（零聊天原文）提炼一条待确认的 PLANNER.md 调度经验草稿。"""
        if self.closed or len(self.tasks) >= 8 or not dedup_key:
            return
        row = {"thread_id": f"planner:{dedup_key}", "completed_revision": 1}
        if not await completed_thread(self._receipt, row, "pending", reserve=True):
            return
        key = (row["thread_id"], 1)
        safe_profiles = [
            {
                "id": str(p.get("id") or "")[:40],
                "description": str(p.get("description") or "")[:120],
            }
            for p in profiles[:24]
            if isinstance(p, dict) and p.get("id")
        ]
        safe_actions = [
            {str(k): str(v)[:80] if isinstance(v, str) else v for k, v in a.items()}
            for a in actions[:8]
            if isinstance(a, dict)
        ]
        task = asyncio.create_task(
            self._learn_planner(row, str(signal), safe_actions, safe_profiles)
        )
        self.tasks[key] = task
        task.add_done_callback(lambda finished: self.tasks.pop(key, None))

    async def _learn_planner(
        self, row: dict, signal: str, actions: list[dict], profiles: list[dict]
    ):
        try:
            async with self.slot, asyncio.timeout(45):
                cur_manual = await completed_thread(self.memory.read, "planner")
                user_payload = json.dumps(
                    {
                        "signal": signal,
                        "actions": actions,
                        "available_profiles": profiles,
                        "manual_headings": [
                            ln.strip()
                            for ln in str(cur_manual.get("content") or "").splitlines()
                            if ln.strip().startswith("## ")
                        ][:12],
                    },
                    ensure_ascii=False,
                )
                raw_resp = await call_capability(
                    self.host,
                    "llm.generate_with_tools",
                    timeout_ms=40000,
                    prompt=[
                        {
                            "role": "system",
                            "content": (
                                "你是 MaiBot 调度手册复盘提炼器。仅根据给定的抽象调度轨迹与可用角色描述，"
                                "提炼一条跨会话通用的调度经验草稿。严禁猜测或包含任何聊天原文、群号、人名、路径、具体数值或凭据。\n"
                                '仅输出 JSON：{"title":"4到12字短标题","lesson":"一句祈使句通用调度方法（无值得沉淀之处则留空字符串）","rationale":"一句话说明原因"}。'
                            ),
                        },
                        {"role": "user", "content": user_payload},
                    ],
                    tools=[],
                    task_name="utils",
                    model_name="",
                    max_tokens=500,
                )
                await self._record_llm_tokens(raw_resp)
                response = checked(raw_resp)
                candidate = _parse_json_object(response["response"])
                lesson = str(candidate.get("lesson") or "").strip()
                if not 1 <= len(lesson) <= 400:
                    await completed_thread(self._receipt, row, "empty")
                    return
                bullet = lesson if lesson.startswith("-") else f"- {lesson}"
                await completed_thread(
                    self.memory._guard, "planner", bullet, "", allow_custom=False
                )
                title = str(candidate.get("title") or "调度经验草稿").strip()[:60]
                rationale = str(
                    candidate.get("rationale") or f"调度信号：{signal}"
                ).strip()[:200]
                await completed_thread(
                    lambda: self.memory.save_skill_proposal(
                        thread_id=row["thread_id"],
                        revision=1,
                        profile_id="planner",
                        skill_id="planner-manual",
                        title=title,
                        description="MaiBot 调度手册经验草稿",
                        content=bullet,
                        signals=[signal],
                        rationale=rationale,
                    )
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
            logger.warning("调度经验提炼瞬态失败 %s: %s", row["thread_id"], exc)
            await completed_thread(self._receipt, row, "interrupted")
        except (ValueError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("调度经验提炼被拒绝 %s: %s", row["thread_id"], exc)
            await completed_thread(self._receipt, row, "rejected")
        except Exception as exc:
            logger.warning("调度经验提炼异常 %s: %s", row["thread_id"], exc)
            await completed_thread(self._receipt, row, "interrupted")

    async def _learn(self, row, trace):
        try:
            async with self.slot, asyncio.timeout(45):
                old = await completed_thread(
                    self.memory.read, "notes", target=row["profile"]
                )
                existing_skills = await completed_thread(self.memory.skills)
                raw_snap = row.get("profile_snapshot") or {}
                if isinstance(raw_snap, str):
                    try:
                        raw_snap = json.loads(raw_snap)
                    except json.JSONDecodeError:
                        raw_snap = {}
                snap_skills = (
                    raw_snap.get("skills")
                    if isinstance(raw_snap, dict)
                    else getattr(raw_snap, "skills", ())
                ) or ()
                bound_skills = [
                    str(s)
                    for s in snap_skills
                    if isinstance(s, str) and s in existing_skills
                ]
                preset_skills = await completed_thread(self.memory.preset_skills)
                custom_bound = [s for s in bound_skills if s not in preset_skills]
                skill_briefs = await completed_thread(
                    self.memory.skill_briefs, existing_skills
                )
                public_skills = [
                    {
                        "id": str(b.get("id") or ""),
                        "title": str(b.get("title") or "")[:40],
                        "desc": str(b.get("description") or "")[:64],
                    }
                    for b in skill_briefs[:16]
                    if b.get("id")
                ]
                if len(old["content"]) >= 1800 and not custom_bound and not public_skills:
                    await completed_thread(self._receipt, row, "full")
                    return
                if custom_bound or public_skills:
                    user_payload = json.dumps(
                        {
                            "profile": str(row.get("profile") or "default"),
                            "trace": trace,
                            "bound_skills": custom_bound,
                            "existing_skills": public_skills,
                        },
                        ensure_ascii=False,
                    )
                else:
                    user_payload = json.dumps(trace, ensure_ascii=False)
                raw_resp = await call_capability(
                    self.host,
                    "llm.generate_with_tools",
                    timeout_ms=40000,
                    prompt=[
                        {
                            "role": "system",
                            "content": (
                                "从工具执行与恢复轨迹提炼通用程序性经验。仅可使用给定的角色名、工具轨迹与已有技能公开摘要，严禁推测或包含人物、会话、主机、路径、资产、具体数值或凭据。\n"
                                "严禁把单一会话、群聊中特定群友的一次性业务上下文、题材偏好、兴趣领域或内容口味写入全局 note（NOTES.md）或 skill（SKILL.md）；全局笔记与技能只保留跨群、跨领域通用的工具用法与排障步骤。\n"
                                '1) 若 existing_skills 或 bound_skills 中已有同领域技能可承接新经验，优先返回 {"op":"patch","skill_id":"已有技能ID","lesson":"一句祈使句避坑规则","note":"通用方法"}；\n'
                                '2) 若确无覆盖技能且属全新可复用多步工作流，返回 {"op":"create","skill_id":"英文短横线ID","title":"类级中文名","description":"适用时机：...","rules":["规则1","规则2"],"pitfalls":["避坑1"],"rationale":"一句话说明为何新建"}（也可直接给 content）；\n'
                                '3) 若仅适合写角色短笔记，返回 {"note":"通用方法"}；无可提炼内容返回 {"note":""}。'
                            ),
                        },
                        {
                            "role": "user",
                            "content": user_payload,
                        },
                    ],
                    tools=[],
                    task_name="utils",
                    model_name="",
                    max_tokens=500,
                )
                await self._record_llm_tokens(raw_resp)
                response = checked(raw_resp)
                candidate = _parse_json_object(response["response"])
                did_work = False
                op = str(candidate.get("op") or "").strip().lower()
                cand_skill = str(candidate.get("skill_id") or "").strip()
                lesson = str(candidate.get("lesson") or "").strip()
                prov = f"thread:{row['thread_id']}@r{int(row.get('completed_revision') or 0)}"
                trace_signals = [prov]
                for item in trace:
                    if isinstance(item, dict) and item.get("signal"):
                        sig = str(item["signal"])
                        if sig not in trace_signals:
                            trace_signals.append(sig)
                    elif isinstance(item, dict) and item.get("tool") and "tool_recovery" not in trace_signals:
                        trace_signals.append("tool_recovery")

                if (
                    op == "patch" or (cand_skill and cand_skill in existing_skills and lesson)
                ) and (cand_skill in existing_skills or custom_bound):
                    target_sk = (
                        cand_skill if cand_skill in existing_skills else custom_bound[0]
                    )
                    if 1 <= len(lesson) <= 500:
                        cur_sk = await completed_thread(
                            self.memory.read, "skill", target=target_sk
                        )
                        if lesson not in cur_sk["content"] and len(cur_sk["content"]) < 24000:
                            bullet = lesson if lesson.startswith("-") else f"- {lesson}"
                            base_txt = cur_sk["content"].rstrip()
                            if "## 实战补充与避坑" in base_txt:
                                next_sk = f"{base_txt}\n{bullet}\n"
                            else:
                                next_sk = f"{base_txt}\n\n## 实战补充与避坑\n{bullet}\n"
                            await completed_thread(
                                self.memory._guard,
                                "skill",
                                next_sk,
                                row["chat_id"],
                                allow_custom=True,
                            )
                            await completed_thread(
                                lambda: self.memory.save_skill_proposal(
                                    thread_id=row["thread_id"],
                                    revision=int(row.get("completed_revision") or 0),
                                    profile_id=str(row.get("profile") or "default"),
                                    skill_id=target_sk,
                                    title=f"补充技能：{target_sk}",
                                    description=lesson[:180],
                                    content=next_sk,
                                    signals=trace_signals[:6],
                                    rationale=f"来自 {prov} 的实战避坑补充，确认后合并入技能",
                                )
                            )
                        did_work = True
                elif op == "create" and cand_skill and cand_skill not in existing_skills:
                    raw_md = str(candidate.get("content") or "").strip()
                    title = str(candidate.get("title") or cand_skill).strip()
                    desc = str(candidate.get("description") or "自动复盘提炼的可复用技能").strip()
                    rationale = str(
                        candidate.get("rationale") or "自动复盘识别到的新工作流草稿，预览确认后创建"
                    ).strip()
                    if not raw_md:
                        rules = [
                            str(r).strip()
                            for r in (candidate.get("rules") or [])
                            if str(r or "").strip()
                        ]
                        pitfalls = [
                            str(p).strip()
                            for p in (candidate.get("pitfalls") or [])
                            if str(p or "").strip()
                        ]
                        if rules or pitfalls:
                            lines = [
                                "---",
                                f"title: {json.dumps(title, ensure_ascii=False)}",
                                f"description: {json.dumps(desc, ensure_ascii=False)}",
                                "---",
                                "",
                                f"# {title}",
                                "",
                            ]
                            if rules:
                                lines.append("## 执行要点")
                                lines.extend(
                                    r if r.startswith("-") else f"- {r}" for r in rules
                                )
                                lines.append("")
                            if pitfalls:
                                lines.append("## 避坑提示")
                                lines.extend(
                                    p if p.startswith("-") else f"- {p}" for p in pitfalls
                                )
                                lines.append("")
                            raw_md = "\n".join(lines).strip() + "\n"
                    if raw_md:
                        await completed_thread(
                            self.memory._guard, "skill", raw_md, row["chat_id"]
                        )
                        await completed_thread(
                            lambda: self.memory.save_skill_proposal(
                                thread_id=row["thread_id"],
                                revision=int(row.get("completed_revision") or 0),
                                profile_id=str(row.get("profile") or "default"),
                                skill_id=cand_skill,
                                title=title,
                                description=desc,
                                content=raw_md,
                                signals=[prov, f"{len(trace)} 步恢复轨迹"],
                                rationale=rationale,
                            )
                        )
                        did_work = True

                note = candidate.get("note")
                if isinstance(note, str) and 1 <= len(note) <= 500:
                    if len(old["content"]) < 1800:
                        if note not in old["content"]:
                            content = (old["content"] + "\n\n" + note).strip()
                            await completed_thread(
                                self.memory.write,
                                "notes",
                                content,
                                target=row["profile"],
                                chat_id=row["chat_id"],
                                expected_version=old["version"],
                                author=prov,
                            )
                        did_work = True
                    elif not did_work:
                        await completed_thread(self._receipt, row, "full")
                        return

                if not did_work:
                    await completed_thread(self._receipt, row, "empty")
                    return
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
