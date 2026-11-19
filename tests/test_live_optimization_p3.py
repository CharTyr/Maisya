"""针对线上 P3 存储膨胀治理、别名行收口与运维健壮性的 TDD 验收测试。"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maisya.abilities import Profile
from maisya.intents import Intents
from maisya.threads import ThreadStore
from maisya.trust import MessageStore, format_stream_spec
from maisya.workspaces import Workspaces
from plugin import MaisyaPlugin


TEST_GROUP_ID = "100246809"
TEST_GROUP_SID = "grp_p3_test_md5_0000000000000001"


@pytest.mark.asyncio
async def test_p3_1_drop_empty_incoming_events_prune_storage_and_compact_aged_events(tmp_path: Path):
    # 1a. 空的遗留 incoming_events 表在初始化时自动清理
    db_empty = tmp_path / "empty_legacy.db"
    with sqlite3.connect(db_empty) as conn:
        conn.execute("CREATE TABLE incoming_events (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.commit()
    ThreadStore(db_empty)
    with sqlite3.connect(db_empty) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incoming_events'"
        ).fetchone()
    assert exists is None, "空的遗留 incoming_events 表应被安全清理"

    # 1b. 非空的 incoming_events 表绝不误删
    db_nonempty = tmp_path / "nonempty_legacy.db"
    with sqlite3.connect(db_nonempty) as conn:
        conn.execute("CREATE TABLE incoming_events (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("INSERT INTO incoming_events (payload) VALUES ('keep-me')")
        conn.commit()
    ThreadStore(db_nonempty)
    with sqlite3.connect(db_nonempty) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM incoming_events").fetchone()[0]
    assert cnt == 1

    # 2. Workspaces.prune_storage 回收过期附件、孤儿附件文件、孤儿 ws_* 目录，但保留活跃与近 2 条
    now_ts = 1_800_000_000.0
    store = ThreadStore(tmp_path / "m.db")
    files = Workspaces(tmp_path / "ws", store)
    prof = Profile(id="default", tools=("workspace_file", "result_attach"))
    idle_row = store.create(prof, "旧归档任务", chat_id=TEST_GROUP_SID, platform="qq", user_id="u1", workspace="ws_keep")
    active_row = store.create(prof, "活跃任务", chat_id=TEST_GROUP_SID, platform="qq", user_id="u1", workspace="ws_act")
    store.finish(idle_row["thread_id"], TEST_GROUP_SID, {"text": "done"}, [], 1)
    with store.connect() as db, db:
        db.execute("UPDATE agent_threads SET state='active' WHERE thread_id=?", (active_row["thread_id"],))

    with files.activity(idle_row) as work:
        work.write("out/old1.txt", "A" * 2000)
        work.write("out/old2.txt", "B" * 2000)
        work.write("out/new1.txt", "C" * 2000)
        work.write("out/new2.txt", "D" * 2000)
        work.commit()
        atts_idle = files.attach(idle_row, work, ["out/old1.txt", "out/old2.txt", "out/new1.txt", "out/new2.txt"])

    with files.activity(active_row) as work_act:
        work_act.write("out/act_old.txt", "E" * 2000)
        work_act.commit()
        atts_act = files.attach(active_row, work_act, ["out/act_old.txt"])

    # 将 old1/old2 与 active 附件时间拨到 45 天前，new1/new2 保持当前
    old_created = now_ts - 45 * 86400
    with store.connect() as db, db:
        db.execute("UPDATE attachments SET created=? WHERE handle IN (?, ?, ?)", (old_created, atts_idle[0]["handle"], atts_idle[1]["handle"], atts_act[0]["handle"]))
        db.execute("UPDATE attachments SET created=? WHERE handle IN (?, ?)", (now_ts - 60, atts_idle[2]["handle"], atts_idle[3]["handle"]))

    # 制造孤儿附件文件与孤儿 ws_* 目录
    orphan_att = files.artifacts / "att_deadbeef000000000000000000000000"
    orphan_att.write_bytes(b"orphan" * 300)
    orphan_ws = files.root / "ws_deadbeef0000000000000001"
    orphan_ws.mkdir(parents=True)
    (orphan_ws / "junk.bin").write_bytes(b"x" * 1024)

    pruned = files.prune_storage(
        max_age_days=30,
        keep_recent_attachments=2,
        prune_orphan_dirs=True,
        min_orphan_age_seconds=0.0,
        now=now_ts,
    )
    assert pruned["pruned_attachments"] == 2, pruned
    assert pruned["pruned_orphan_files"] >= 1, pruned
    assert pruned["pruned_orphan_dirs"] == 1, pruned
    assert not (files.artifacts / atts_idle[0]["handle"]).exists()
    assert not (files.artifacts / atts_idle[1]["handle"]).exists()
    assert (files.artifacts / atts_idle[2]["handle"]).exists()
    assert (files.artifacts / atts_idle[3]["handle"]).exists()
    assert (files.artifacts / atts_act[0]["handle"]).exists(), "活跃线程的附件不得被误删"
    assert not orphan_att.exists()
    assert not orphan_ws.exists()

    # 3. ThreadStore.compact_aged_events 压缩超 14 天旧非活跃大载荷，保留近期与活跃线程，保留 FTS
    big_tools_payload = json.dumps(
        {
            "calls": [{"id": "c1", "function": {"name": "digest_feed", "arguments": "{}"}}],
            "results": [{"call_id": "c1", "value": {"blob": "X" * 10000}}],
        },
        ensure_ascii=False,
    )
    big_archive_payload = json.dumps(
        {"handoff": "摘要", "messages": [{"role": "tool", "content": "Y" * 10000}]},
        ensure_ascii=False,
    )
    with store.connect() as db, db:
        # 旧 idle 线程的超 14 天大载荷 -> 应被压缩
        db.execute(
            "INSERT INTO thread_events (thread_id, kind, revision, payload, at) VALUES (?, 'tools', 1, ?, ?)",
            (idle_row["thread_id"], big_tools_payload, now_ts - 20 * 86400),
        )
        db.execute(
            "INSERT INTO thread_events (thread_id, kind, revision, payload, at) VALUES (?, 'context_archive', 1, ?, ?)",
            (idle_row["thread_id"], big_archive_payload, now_ts - 20 * 86400),
        )
        # 同线程 3 天内的大载荷 -> 保留原文
        db.execute(
            "INSERT INTO thread_events (thread_id, kind, revision, payload, at) VALUES (?, 'tools', 2, ?, ?)",
            (idle_row["thread_id"], big_tools_payload, now_ts - 3 * 86400),
        )
        # 活跃线程 20 天前的大载荷 -> 保留原文
        db.execute(
            "INSERT INTO thread_events (thread_id, kind, revision, payload, at) VALUES (?, 'tools', 1, ?, ?)",
            (active_row["thread_id"], big_tools_payload, now_ts - 20 * 86400),
        )

    comp = store.compact_aged_events(max_age_days=14, max_payload_bytes=512, now=now_ts)
    assert comp["compacted_events"] == 2, comp
    assert comp["reclaimed_bytes"] > 15000, comp
    with store.connect() as db:
        old_rows = db.execute(
            "SELECT kind, payload FROM thread_events WHERE thread_id=? AND revision=1 AND kind IN ('tools', 'context_archive')",
            (idle_row["thread_id"],),
        ).fetchall()
        for r in old_rows:
            parsed = json.loads(r["payload"])
            assert parsed.get("compacted") is True
            assert len(r["payload"]) < 600
        recent_row = db.execute(
            "SELECT payload FROM thread_events WHERE thread_id=? AND revision=2 AND kind='tools'",
            (idle_row["thread_id"],),
        ).fetchone()
        assert len(recent_row["payload"]) > 9000
        act_ev = db.execute(
            "SELECT payload FROM thread_events WHERE thread_id=? AND kind='tools'",
            (active_row["thread_id"],),
        ).fetchone()
        assert len(act_ev["payload"]) > 9000
    # FTS 检索不受影响
    assert any(h["thread_id"] == idle_row["thread_id"] for h in store.search("旧归档", TEST_GROUP_SID))


@pytest.mark.asyncio
async def test_p3_2_canonical_alias_dedup_bounded_unload_avatar_cache_and_short_shape_desc(
    tmp_path: Path, caplog, monkeypatch
):
    # 1. social_state 与 publication_grants 写入收口为单条规范键，且读路径兼容全部别名
    store = ThreadStore(tmp_path / "m.db")
    msgs = MessageStore(tmp_path / "m.db")
    msgs.upsert_route(TEST_GROUP_SID, platform="qq", chat_type="group", group_id=TEST_GROUP_ID)
    aliases = [
        TEST_GROUP_SID,
        f"qq:group:{TEST_GROUP_ID}",
        f"qq:{TEST_GROUP_ID}",
        f"group:{TEST_GROUP_ID}",
        TEST_GROUP_ID,
    ]
    store.chat_aliases = lambda cid, plat="": aliases
    intents = Intents(store, clock=lambda: 1_800_000_000.0)

    intents.mark_proactive(f"chat:{TEST_GROUP_SID}", aliases=aliases)
    with store.connect() as db:
        social_rows = db.execute("SELECT chat_id, locked FROM social_state").fetchall()
    assert len(social_rows) == 1, f"social_state 应只写 1 条规范键，实际 {len(social_rows)} 条: {[dict(r) for r in social_rows]}"
    assert social_rows[0]["chat_id"] == f"qq:group:{TEST_GROUP_ID}"
    # 任意别名都能读到已锁状态
    ok_sid, reason_sid = intents.social_gate(f"chat:{TEST_GROUP_SID}", aliases=aliases)
    assert ok_sid is False and reason_sid == "unreplied_lock"

    intents.note_received(TEST_GROUP_SID, engaged=True, aliases=aliases)
    with store.connect() as db:
        social_rows_after = db.execute("SELECT chat_id, locked FROM social_state").fetchall()
    assert len(social_rows_after) == 1
    assert social_rows_after[0]["locked"] == 0
    ok_after, _ = intents.social_gate(f"chat:{TEST_GROUP_SID}", aliases=aliases)
    assert ok_after is True

    store.record_publication_grant("th_1111111111111111", 1, TEST_GROUP_SID, actor="admin:1")
    with store.connect() as db:
        grant_rows = db.execute("SELECT target_chat FROM publication_grants").fetchall()
    assert len(grant_rows) == 1, f"publication_grants 应只写 1 条规范键，实际 {len(grant_rows)} 条"
    assert store.has_publication_grant("th_1111111111111111", 1, TEST_GROUP_SID) is True
    assert store.has_publication_grant("th_1111111111111111", 1, f"qq:group:{TEST_GROUP_ID}") is True

    # 2. on_unload 限时保护 + chat.get_avatar E_CAPABILITY_FAILED 降级 DEBUG 且长负缓存
    data_dir = tmp_path / "data" / "plugins" / "chartyr.maisya"
    data_dir.mkdir(parents=True, exist_ok=True)
    avatar_calls: list[str] = []
    now_mono = [2000.0]
    monkeypatch.setattr("plugin.time.monotonic", lambda: now_mono[0])

    async def _cap(name: str, **kwargs):
        if name == "chat.get_all_streams":
            return {
                "success": True,
                "streams": [
                    {
                        "session_id": TEST_GROUP_SID,
                        "platform": "qq",
                        "group_id": TEST_GROUP_ID,
                        "group_name": "测试群",
                        "chat_type": "group",
                        "is_group_session": True,
                    }
                ],
            }
        if name == "chat.get_avatar":
            avatar_calls.append(str(kwargs.get("target_id") or ""))
            return {"success": False, "error": "E_CAPABILITY_FAILED: 无法获取头像"}
        if name in {"component.list_loaded_plugins", "api.list"}:
            return {"success": True, "plugins": [], "apis": []}
        return {"success": True}

    plugin = MaisyaPlugin()
    plugin._test_cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "enabled_chats": [format_stream_spec("qq", "group", TEST_GROUP_ID)],
        "admins": ["qq:10001"],
        "sandbox": {"enabled": False},
    }
    plugin.get_plugin_config_data = lambda: dict(plugin._test_cfg)
    plugin.set_plugin_config = lambda d: plugin._test_cfg.update(dict(d))
    test_logger = logging.getLogger("test.maisya.p3")
    plugin._ctx = SimpleNamespace(
        paths=SimpleNamespace(data_dir=data_dir, runtime_dir=tmp_path / "rt"),
        config=SimpleNamespace(get=lambda k, d=None: asyncio.sleep(0, result=["qq:10001"] if k == "plugin.permission" else d)),
        call_capability=_cap,
        logger=test_logger,
    )
    await plugin.on_load()

    with caplog.at_level(logging.WARNING, logger="test.maisya.p3"):
        await plugin.console_query()
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("E_CAPABILITY_FAILED" in m for m in warn_msgs), f"E_CAPABILITY_FAILED 不应刷 WARNING: {warn_msgs}"
    assert len(avatar_calls) == 1
    # 60 秒后仍在长负缓存内，不重复打 RPC
    now_mono[0] += 60.0
    await plugin.console_query()
    assert len(avatar_calls) == 1, f"E_CAPABILITY_FAILED 应进入长负缓存，实际 RPC 次数: {len(avatar_calls)}"

    # 恢复全局 time.monotonic，避免冻结 asyncio 事件循环时钟
    monkeypatch.undo()

    # 3. on_unload 遇到单个组件 close 挂死时在限期内优雅脱身并继续清理其余组件
    async def _hang_close():
        await asyncio.sleep(5.0)

    plugin._unload_component_timeout = 0.08
    plugin.sharing.close = _hang_close
    t0 = time.perf_counter()
    await plugin.on_unload()
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"on_unload 不应被单组件挂起卡死，耗时 {elapsed:.2f}s"
    assert plugin.pulse is None and plugin.engine is None and plugin.runtime is None

    # 4. maisya_shape 工具描述精简且保持无 visibility 延迟加载
    shape_meta = getattr(MaisyaPlugin.shape_tool, "_component_meta", None) or getattr(
        MaisyaPlugin.shape_tool, "__maibot_component__", None
    ) or {}
    # 从装饰器或源码属性验证描述长度 <= 80 且不包含 visibility
    src_plugin = (ROOT / "plugin.py").read_text(encoding="utf-8")
    idx = src_plugin.index('@Tool(\n        "maisya_shape",')
    snippet = src_plugin[idx : idx + 320]
    assert "visibility" not in snippet
    desc_line = next(ln for ln in snippet.splitlines() if "description=" in ln)
    desc_val = desc_line.split("description=", 1)[1].strip().rstrip(",").strip('"')
    assert len(desc_val) <= 80, f"maisya_shape description 过长 ({len(desc_val)} 字符): {desc_val}"
