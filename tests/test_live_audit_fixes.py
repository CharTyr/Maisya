"""2026-11-20 线上排查 6 项问题专项回归测试（TDD）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from maisya.abilities import Profile, Profiles
from maisya.bridge import Bridge
from maisya.context_budget import prune_tool_outputs
from maisya.digest import DigestWorkspace, run_digest_tool
from maisya.intents import Intents
from maisya.learning import Learning
from maisya.memory import MemoryLayers
from maisya.pulse import Pulse
from maisya.threads import ThreadEngine, ThreadStore
from maisya.vault import Vault
from maisya.workspaces import Workspaces


# ─────────────────── Fix 1: 带附件定时工作流唤醒直达 share ───────────────────


@pytest.mark.asyncio
async def test_fix1_workflow_wake_with_attachments_gives_direct_share_directive(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    clock = [1795134600.0]
    intents = Intents(store, clock=lambda: clock[0])
    bridge = Bridge(store)
    prof = Profile(id="digest", tools=("digest_feed",))
    row = store.create(prof, "早报", chat_id="chat_grp_1", platform="qq", user_id="u1")
    with store.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='idle', revision=1, processed_revision=1, "
            "completed_revision=1, result=? WHERE thread_id=?",
            (
                json.dumps(
                    {
                        "text": "已生成并挂载附件，待发送。",
                        "attachments": [
                            {
                                "handle": "att_abc123456789",
                                "name": "digest-card.png",
                                "kind": "image",
                                "bytes": 123456,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                row["thread_id"],
            ),
        )

    woke_calls: list[dict] = []

    class FakeHost:
        async def call_capability(self, cap, **kwargs):
            woke_calls.append({"cap": cap, **kwargs})
            return {"success": True, "result": {"accepted": True}}

    pulse = Pulse(
        bridge,
        FakeHost(),
        lambda chat_id, platform="": True,
        intents,
        heartbeat_enabled=False,
    )
    intents.schedule(
        kind="return",
        source="workflow",
        target="chat:chat_grp_1",
        due_at=clock[0] + 1,
        reason="每日早间资讯简报图卡",
        thread_id=row["thread_id"],
        payload={"profile": "digest", "wait_thread": True},
        bypass_limits=True,
    )
    clock[0] += 5
    await pulse.tick()

    assert len(woke_calls) == 1
    wake_msg = str(woke_calls[0].get("message") or woke_calls[0].get("text") or woke_calls[0])
    assert row["thread_id"] in wake_msg
    assert "att_abc123456789" in wake_msg
    assert 'action="share"' in wake_msg or "action='share'" in wake_msg
    # 明确告知目标即当前会话、直接 share 勿绕路
    assert "当前会话" in wake_msg
    assert "原生 reply 无法发" in wake_msg or "勿先调用 reply" in wake_msg or "不要调用 reply" in wake_msg


# ─────────────────── Fix 2: 上下文减重（stage-fold & recent_pushed_titles） ───────────────────


def test_fix2_prune_tool_outputs_supports_only_tools_and_protect_last_turn():
    big_collect = json.dumps({"candidates": [{"title": "X", "source_text": "正文" * 3000}]}, ensure_ascii=False)
    big_chat = json.dumps([{"text": "群聊消息" * 1500}], ensure_ascii=False)
    history = [
        {"role": "user", "content": "生成简报"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "chat_read", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": f"<external_data>{big_chat}</external_data>"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c2", "type": "function", "function": {"name": "digest_feed", "arguments": '{"action":"collect"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "content": f"<external_data>{big_collect}</external_data>"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c3", "type": "function", "function": {"name": "digest_feed", "arguments": '{"action":"score_select"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c3", "content": '<external_data>{"selected": 5}</external_data>'},
    ]
    pruned, stats = prune_tool_outputs(
        history,
        tail_tokens=0,
        min_prune_chars=4000,
        only_tools={"digest_feed"},
        protect_last_turn=True,
        event_seq=99,
    )
    assert stats["reclaimed_tokens"] > 1000
    assert len(stats["pruned_entries"]) == 1
    assert stats["pruned_entries"][0]["tool_call_id"] == "c2"
    # chat_read 未被误折，最新一轮 score_select 未被误折
    assert "群聊消息" in pruned[2]["content"]
    assert "[已折叠旧工具输出(digest_feed)" in pruned[4]["content"]
    assert '{"selected": 5}' in pruned[6]["content"]


@pytest.mark.asyncio
async def test_fix2_engine_folds_prior_tool_output_on_stage_hint_and_collect_returns_recent_titles(tmp_path: Path):
    # Part A: DigestWorkspace.collect 返回 compact recent_pushed_titles，无需手读整份 digest_history.json
    store = ThreadStore(tmp_path / "m.db")
    files = Workspaces(tmp_path / "ws", store)
    prof = Profile(id="digest", tools=("digest_feed", "workspace_file"))
    row = store.create(prof, "早报", chat_id="c1", platform="qq", user_id="u1", workspace="digest")
    with files.activity(row) as work:
        dw = DigestWorkspace(work, clock=lambda: 1795134600.0)
        dw._save_history(
            {
                "items": [
                    {"id": f"it_{i}", "title": f"已推历史标题_{i}", "url": f"https://example.com/{i}", "created": 1795000000.0 + i * 100}
                    for i in range(15)
                ]
            }
        )
        work.commit()
        res = await dw.collect(web=None, queries=[], max_items=5)
        recent_titles = res.get("recent_pushed_titles")
        assert isinstance(recent_titles, list)
        assert len(recent_titles) <= 10
        assert "已推历史标题_14" in recent_titles
        assert "已推历史标题_0" not in recent_titles

    # Part B: ThreadEngine 在工具返回 _maisya_fold_prior_tools 时无损折叠早期大输出且剥离内部字段
    seen_histories: list[list[dict]] = []
    big_blob = ("前导摘要" * 80) + "RAW_COLLECT_TAIL_" + ("材料正文" * 2200)

    class FakeRuntime:
        def __init__(self):
            self.step_no = 0

        async def step(self, profile, history):
            seen_histories.append(json.loads(json.dumps(history, ensure_ascii=False)))
            self.step_no += 1
            if self.step_no == 1:
                return {
                    "response": "",
                    "tool_calls": [
                        {"id": "tc1", "function": {"name": "digest_feed", "arguments": '{"action":"collect"}'}}
                    ],
                }
            if self.step_no == 2:
                return {
                    "response": "",
                    "tool_calls": [
                        {"id": "tc2", "function": {"name": "digest_feed", "arguments": '{"action":"score_select"}'}}
                    ],
                }
            return {"response": "完成简报", "tool_calls": []}

        async def call(self, name, arguments, context):
            if arguments.get("action") == "collect":
                return {"candidates": [{"title": "T1", "source_text": big_blob}]}
            return {"selected": [{"title": "T1"}], "_maisya_fold_prior_tools": ["digest_feed"]}

    engine = ThreadEngine(store, FakeRuntime(), timeout=30)
    started = await engine.start(prof, "做简报", chat_id="c1", platform="qq", user_id="u1")
    await asyncio.wait_for(engine.drain(), timeout=5.0)
    assert len(seen_histories) == 3
    # 第 2 步还能看到完整 collect 正文
    assert "RAW_COLLECT_TAIL_" in json.dumps(seen_histories[1], ensure_ascii=False)
    # 第 3 步（score_select 之后）早期 collect 大输出已被无损折叠，且 _maisya_fold_prior_tools 未泄露给模型
    step3_dump = json.dumps(seen_histories[2], ensure_ascii=False)
    assert "RAW_COLLECT_TAIL_" not in step3_dump
    assert "_maisya_fold_prior_tools" not in step3_dump
    assert "已折叠旧工具输出(digest_feed)" in step3_dump


# ─────────────────── Fix 3: Learning 准确 tool_recovery & 不半提交 ───────────────────


@pytest.mark.asyncio
async def test_fix3_learning_no_false_tool_recovery_and_no_rejected_receipt_when_proposal_saved(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    profiles = Profiles(tmp_path / "profs")
    vault = Vault(tmp_path / "vault")
    mem = MemoryLayers(tmp_path / "mem", store, profiles, vault)

    # 创建一个带有纯成功多步交付（无任何报错）的 digest 线程
    prof = profiles.get("digest")
    row = store.create(prof, "做简报", chat_id="chat_secret_999", platform="qq", user_id="u1")
    with store.connect() as db, db:
        store.event(
            db,
            row["thread_id"],
            "tools",
            1,
            {
                "calls": [
                    {"id": "a1", "function": {"name": "digest_feed", "arguments": "{}"}},
                    {"id": "a2", "function": {"name": "html_render", "arguments": "{}"}},
                    {"id": "a3", "function": {"name": "result_attach", "arguments": "{}"}},
                ],
                "results": [
                    {"call_id": "a1", "value": {"candidates": []}},
                    {"call_id": "a2", "value": {"path": "out/card.png"}},
                    {"call_id": "a3", "value": {"attachments": [{"handle": "att_1"}]}},
                ],
            },
        )
        db.execute(
            "UPDATE agent_threads SET state='idle', completed_revision=1, result=? WHERE thread_id=?",
            (
                json.dumps(
                    {
                        "text": "已生成附件",
                        "attachments": [{"handle": "att_1", "name": "card.png", "kind": "image"}],
                    },
                    ensure_ascii=False,
                ),
                row["thread_id"],
            ),
        )

    # 模拟 LLM 返回合法的 skill patch + 含会话标识(触发隐私守卫)的 note
    class FakeHost:
        async def call_capability(self, cap, **kwargs):
            return {
                "success": True,
                "response": json.dumps(
                    {
                        "op": "patch",
                        "skill_id": "news-digest",
                        "lesson": "渲染简报卡前先确认精选结果非空再调用截图工具。",
                        "note": "在 chat_secret_999 群里需要多发显卡新闻。",
                    },
                    ensure_ascii=False,
                ),
            }

    learner = Learning(FakeHost(), mem)
    await learner.schedule(store.get(row["thread_id"], "chat_secret_999"))
    await learner.drain()

    # 1. skill_proposal 应保存成功，且 signals 不得误标 tool_recovery（全程零错误）
    props = mem.list_skill_proposals(status="pending")
    assert len(props) == 1
    assert "tool_recovery" not in props[0]["signals"]
    assert "multi_step_delivery" in props[0]["signals"]

    # 2. 不安全 note 被安全跳过，未写入 NOTES.md
    notes_content = mem.read("notes", target="digest")["content"]
    assert "chat_secret_999" not in notes_content

    # 3. 回执状态必须与产物一致：因为 proposal 已成功保存，回执应为 applied 而非 rejected
    with store.connect() as db:
        rec_status = db.execute(
            "SELECT status FROM learning_receipts WHERE thread_id=? AND revision=1",
            (row["thread_id"],),
        ).fetchone()[0]
    assert rec_status == "applied"


@pytest.mark.asyncio
async def test_fix3_learning_marks_tool_recovery_on_real_error_and_rejects_cleanly_when_all_fail_guard(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    profiles = Profiles(tmp_path / "profs")
    vault = Vault(tmp_path / "vault")
    mem = MemoryLayers(tmp_path / "mem", store, profiles, vault)

    prof = profiles.get("digest")
    row = store.create(prof, "排查", chat_id="chat_priv_777", platform="qq", user_id="u1")
    with store.connect() as db, db:
        store.event(
            db,
            row["thread_id"],
            "tools",
            1,
            {
                "calls": [
                    {"id": "e1", "function": {"name": "digest_feed", "arguments": "{}"}},
                    {"id": "e2", "function": {"name": "digest_feed", "arguments": "{}"}},
                ],
                "results": [
                    {"call_id": "e1", "value": {"error": "temporary timeout"}},
                    {"call_id": "e2", "value": {"candidates": []}},
                ],
            },
        )
        db.execute(
            "UPDATE agent_threads SET state='idle', completed_revision=1, result=? WHERE thread_id=?",
            (json.dumps({"text": "已恢复完成"}, ensure_ascii=False), row["thread_id"]),
        )

    # Case A: 全部含私密会话 ID -> 不写 proposal、不写 NOTES、回执为 rejected
    class RejectHost:
        async def call_capability(self, cap, **kwargs):
            return {
                "success": True,
                "response": json.dumps(
                    {
                        "op": "patch",
                        "skill_id": "news-digest",
                        "lesson": "在 chat_priv_777 重试前先等待。",
                        "note": "chat_priv_777 经常超时。",
                    },
                    ensure_ascii=False,
                ),
            }

    learner = Learning(RejectHost(), mem)
    await learner.schedule(store.get(row["thread_id"], "chat_priv_777"))
    await learner.drain()
    assert mem.list_skill_proposals(status="pending") == []
    with store.connect() as db:
        st_a = db.execute(
            "SELECT status FROM learning_receipts WHERE thread_id=? AND revision=1",
            (row["thread_id"],),
        ).fetchone()[0]
    assert st_a == "rejected"

    # Case B: 真实错误恢复且内容合规 -> 准确带上 tool_recovery 标签；
    # 且即使会话下存在同名内置工作区 "digest"，笔记提及工具名 "digest_feed" 也不应被误判泄密
    row2 = store.create(prof, "排查2", chat_id="chat_priv_778", platform="qq", user_id="u1", workspace="digest")
    with store.connect() as db, db:
        store.event(
            db,
            row2["thread_id"],
            "tools",
            1,
            {
                "calls": [
                    {"id": "e3", "function": {"name": "digest_feed", "arguments": "{}"}},
                    {"id": "e4", "function": {"name": "digest_feed", "arguments": "{}"}},
                ],
                "results": [
                    {"call_id": "e3", "value": {"error": "timeout"}},
                    {"call_id": "e4", "value": {"candidates": []}},
                ],
            },
        )
        db.execute(
            "UPDATE agent_threads SET state='idle', completed_revision=1, result=? WHERE thread_id=?",
            (json.dumps({"text": "恢复完成"}, ensure_ascii=False), row2["thread_id"]),
        )

    class OkHost:
        async def call_capability(self, cap, **kwargs):
            return {
                "success": True,
                "response": json.dumps(
                    {
                        "op": "patch",
                        "skill_id": "news-digest",
                        "lesson": "采集超时后缩小查询词数量并优先复用已有候选池。",
                        "note": "调用 digest_feed 超时后先缩短查询词再重试。",
                    },
                    ensure_ascii=False,
                ),
            }

    learner2 = Learning(OkHost(), mem)
    await learner2.schedule(store.get(row2["thread_id"], "chat_priv_778"))
    await learner2.drain()
    props = mem.list_skill_proposals(status="pending")
    assert len(props) == 1
    assert "tool_recovery" in props[0]["signals"]
    assert "digest_feed" in mem.read("notes", target="digest")["content"]


@pytest.mark.asyncio
async def test_fix2_run_digest_tool_score_select_emits_stage_fold_hint(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    files = Workspaces(tmp_path / "ws", store)
    prof = Profile(id="digest", tools=("digest_feed",))
    row = store.create(prof, "早报", chat_id="c1", platform="qq", user_id="u1", workspace="digest")
    with files.activity(row) as work:
        dw = DigestWorkspace(work, clock=lambda: 1795134600.0)
        pool_url = "https://example.com/news/stage-fold"
        dw._save_pool(
            {
                "total_discovered": 1,
                "by_url": {
                    "example.com/news/stage-fold": {
                        "url": pool_url,
                        "url_key": "example.com/news/stage-fold",
                        "title": "新版本发布",
                        "source": "DevBlog",
                        "published_at": "2026-11-19T08:00:00Z",
                        "snippet": "实测内存下降 35%",
                        "source_text": "新版本正式发布，实测内存占用下降 35%，现已开源可用。",
                    }
                },
            }
        )
        work.commit()
        res = await run_digest_tool(
            work,
            {
                "action": "score_select",
                "max_items": 5,
                "candidates": [
                    {
                        "url": pool_url,
                        "title": "开源引擎发布新版本，实测内存下降三成半",
                        "summary": "新版本正式发布，实测内存占用下降 35%，现已开源可用，适合中低配环境长期运行。",
                        "why": "内存直降三成半很实用",
                        "topic": "开源工具",
                        "quote": "实测内存占用下降 35%",
                        "scores": {"info": 5, "source": 4, "relevance": 5, "timeliness": 5, "chat": 4, "novelty": 5},
                    }
                ],
            },
            context={"thread_id": row["thread_id"], "revision": 1},
        )
        assert len(res["selected"]) == 1
        assert res.get("_maisya_fold_prior_tools") == ["digest_feed"]


# ─────────────────── Fix 4: RSS 展示/早熟转正 + state_note 不污染历史线程 ───────────────────


@pytest.mark.asyncio
async def test_fix4_rss_state_md_shows_trial_count_and_early_graduates_proven_feeds(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    files = Workspaces(tmp_path / "ws", store)
    prof = Profile(id="digest", tools=("workspace_file", "digest_feed"))
    now = 1795134600.0
    row = store.create(prof, "早报", chat_id="c1", platform="qq", user_id="u1", workspace="digest")
    with files.activity(row) as work:
        dw = DigestWorkspace(work, clock=lambda: now)
        dw._save_sources(
            {
                "feeds": [
                    {
                        "id": "rss_proven",
                        "url": "https://proven.example/feed.xml",
                        "host": "proven.example",
                        "title": "Proven Feed",
                        "origin": "starter",
                        "status": "trial",
                        "added_at": now - 5 * 86400,
                        "trial_until": now + 9 * 86400,
                        "candidates_14d": [now - 86400] * 5,
                        "kept_total": 3,
                        "up": 0,
                        "down": 0,
                        "reply": 0,
                        "fail_count": 0,
                        "first_fail_at": 0.0,
                        "last_polled_at": now - 3600,
                    },
                    {
                        "id": "rss_new_trial",
                        "url": "https://newtrial.example/feed.xml",
                        "host": "newtrial.example",
                        "title": "New Trial Feed",
                        "origin": "starter",
                        "status": "trial",
                        "added_at": now - 2 * 86400,
                        "trial_until": now + 12 * 86400,
                        "candidates_14d": [now - 3600],
                        "kept_total": 1,
                        "up": 0,
                        "down": 0,
                        "reply": 0,
                        "fail_count": 0,
                        "first_fail_at": 0.0,
                        "last_polled_at": now - 3600,
                    },
                ],
                "rejected": [],
            }
        )
        work.commit()

        # 1. poll 生命周期应让 kept_total>=3 且零报错的试用源提前毕业为 active
        async def fake_fetch(url):
            return {"ok": True, "status": 200, "text": "<rss><channel><title>T</title></channel></rss>"}

        dw.fetcher = fake_fetch
        poll_res = await dw.poll()
        assert any(g["feed_id"] == "rss_proven" for g in poll_res["graduated"])
        feeds_by_id = {f["id"]: f for f in dw.status()["feeds"]}
        assert feeds_by_id["rss_proven"]["status"] == "active"
        assert feeds_by_id["rss_new_trial"]["status"] == "trial"

        # 2. _write_state_note 应同时展示活跃数与试用数，不再误报 0 个
        dw._write_state_note([{"topic": "显卡硬件"}], stage="已出卡", tz_offset_minutes=480)
        state_md = (work.path / "project" / "STATE.md").read_text(encoding="utf-8")
        assert "活跃订阅源 1 个" in state_md
        assert "试用 1 个" in state_md or "试用中 1 个" in state_md


def test_fix4_update_state_note_does_not_overwrite_closed_or_archived_threads(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    prof = Profile(id="digest", tools=("digest_feed",), session_mode="workspace")
    t_old_closed = store.create(prof, "旧1", chat_id="c1", platform="qq", user_id="u1", workspace="digest")
    with store.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='closed', state_note='旧关闭摘要' WHERE thread_id=?",
            (t_old_closed["thread_id"],),
        )
    t_old_arch = store.create(prof, "旧2", chat_id="c1", platform="qq", user_id="u1", workspace="digest")
    with store.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='archived', state_note='旧归档摘要' WHERE thread_id=?",
            (t_old_arch["thread_id"],),
        )
    t_active = store.create(prof, "新", chat_id="c1", platform="qq", user_id="u1", workspace="digest")
    assert len({t_old_closed["thread_id"], t_old_arch["thread_id"], t_active["thread_id"]}) == 3

    with store.connect() as db:
        ws_id = db.execute(
            "SELECT workspace_id FROM thread_context WHERE thread_id=?", (t_active["thread_id"],)
        ).fetchone()[0]

    store.update_state_note(t_active["thread_id"], ws_id, "2026-11-20 最新出卡摘要")

    with store.connect() as db:
        notes = {
            r["thread_id"]: r["state_note"]
            for r in db.execute("SELECT thread_id, state_note FROM agent_threads").fetchall()
        }
    assert notes[t_active["thread_id"]] == "2026-11-20 最新出卡摘要"
    assert notes[t_old_closed["thread_id"]] == "旧关闭摘要"
    assert notes[t_old_arch["thread_id"]] == "旧归档摘要"


# ─────────────────── Fix 5: 豁免定时工作流独立记账 + Fix 6: failed 填 last_error ───────────────────


@pytest.mark.asyncio
async def test_fix5_exempt_return_workflow_tracked_separately_without_starving_global_budget(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    clock = [1795134600.0]
    intents = Intents(store, clock=lambda: clock[0], daily_bg_runs_limit=10, daily_bg_tokens_limit=100000)
    bridge = Bridge(store)
    prof = Profile(id="digest", tools=())
    profiles = Profiles(tmp_path / "profs")

    class FakeRuntime:
        async def step(self, profile, history):
            return {
                "response": "早报完成",
                "tool_calls": [],
                "usage": {"prompt_tokens": 180000, "completion_tokens": 2000, "total_tokens": 182000},
            }

    class FakeHost:
        async def call_capability(self, cap, **kwargs):
            return {"success": True, "result": {"accepted": True}}

    engine = ThreadEngine(store, FakeRuntime(), intents=intents, timeout=30)
    pulse = Pulse(
        bridge,
        FakeHost(),
        lambda chat_id, platform="": True,
        intents,
        engine=engine,
        profiles=profiles,
        heartbeat_enabled=False,
    )
    intents.schedule(
        kind="return",
        source="workflow",
        target="chat:c1",
        due_at=clock[0] + 1,
        reason="每日简报",
        payload={"profile": "default", "prompt": "出简报", "daily_at": "08:30"},
        bypass_limits=True,
    )
    clock[0] += 5
    await pulse.tick()
    await asyncio.wait_for(engine.drain(), timeout=5.0)
    clock[0] += 5
    await pulse.tick()

    budget = intents.bg_budget_state()
    # 非豁免全局预算保持 0 且不耗尽（不饿死 heartbeat / watch）
    assert budget["runs_used"] == 0
    assert budget["tokens_used"] == 0
    assert budget["exhausted"] is False
    # 豁免观测账完整记录了运行次数和 Token 消耗
    assert budget["exempt_runs_used"] == 1
    assert budget["exempt_tokens_used"] >= 182000


@pytest.mark.asyncio
async def test_fix6_failed_intent_records_last_error(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    clock = [1795134600.0]
    intents = Intents(store, clock=lambda: clock[0])
    bridge = Bridge(store)
    prof = Profile(id="digest", tools=())
    row = store.create(prof, "早报", chat_id="c1", platform="qq", user_id="u1")
    with store.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='idle', revision=1, processed_revision=1, "
            "completed_revision=1, result=? WHERE thread_id=?",
            (json.dumps({"error": "本轮探索超过运行时限。"}, ensure_ascii=False), row["thread_id"]),
        )

    class FakeHost:
        async def call_capability(self, cap, **kwargs):
            return {"success": True, "result": {"accepted": True}}

    pulse = Pulse(
        bridge,
        FakeHost(),
        lambda chat_id, platform="": True,
        intents,
        heartbeat_enabled=False,
    )
    it = intents.schedule(
        kind="return",
        source="workflow",
        target="chat:c1",
        due_at=clock[0] + 1,
        reason="每日简报",
        thread_id=row["thread_id"],
        payload={"profile": "digest", "wait_thread": True},
        bypass_limits=True,
    )
    clock[0] += 5
    await pulse.tick()

    with store.connect() as db:
        saved = dict(
            db.execute(
                "SELECT status, wake_status, last_error FROM intents WHERE intent_id=?",
                (it["intent_id"],),
            ).fetchone()
        )
    assert saved["status"] == "failed"
    assert saved["wake_status"] == "thread_error"
    assert "超过运行时限" in saved["last_error"]
