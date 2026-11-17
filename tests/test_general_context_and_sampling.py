"""Regression tests for stratified chat sampling, universal context metadata, and domain-neutral presets."""

from __future__ import annotations

from dataclasses import asdict
import inspect
import time
from pathlib import Path

import pytest

from maisya import digest
from maisya.abilities import Profile, Profiles
from maisya.learning import Learning
from maisya.memory import MemoryLayers
from maisya.scout import Scout
from maisya.threads import ThreadStore
from maisya.vault import Vault
from test_foundation import loaded

PRESETS_ROOT = Path(__file__).resolve().parents[1] / "presets"


def _msg(
    idx: int,
    text: str,
    *,
    ts: float,
    session_id: str = "chat-a",
    relayed: bool = False,
) -> dict:
    return {
        "message_id": f"m-{idx}",
        "session_id": session_id,
        "platform": "qq",
        "sender_name": f"user_{idx % 5}",
        "user_id": f"u_{idx % 5}",
        "timestamp": ts,
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": f"u_{idx % 5}"},
            "group_info": {"group_id": "100246809"},
            "additional_config": {"relayed_by_plugin": relayed},
        },
    }


@pytest.mark.asyncio
async def test_chat_read_forwards_hours_and_searches_entire_window_before_limit():
    now = time.time()
    # 120 messages spanning 5 days; earlier ones discuss "生化危机", recent 80 discuss lunch
    raw_messages = []
    for i in range(120):
        ts = now - (5 * 86400) + i * 3500
        if i < 10:
            txt = f"之前聊《生化危机》与 MyGO 的机制 {i}"
        elif i < 40:
            txt = f"中段聊聊硬件与桌游 {i}"
        else:
            txt = f"今天中午吃面条 {i}"
        raw_messages.append(_msg(i, txt, ts=ts))

    recorded: list[dict] = []

    class _Host:
        async def call_capability(self, name, **kwargs):
            recorded.append({"name": name, **kwargs})
            assert name == "message.get_recent"
            lim = int(kwargs.get("limit", 100))
            hrs = kwargs.get("hours")
            items = raw_messages
            if hrs is not None:
                cutoff = now - float(hrs) * 3600
                items = [m for m in items if m["timestamp"] >= cutoff]
            return {"success": True, "messages": items[-lim:]}

    scout = Scout(_Host())
    hits = await scout.call(
        "chat_read",
        {"hours": 168, "limit": 5, "query": "生化危机"},
        {"chat_id": "chat-a", "owner": "chat"},
    )
    assert recorded[-1]["hours"] == 168
    assert recorded[-1]["limit"] >= 400
    assert len(hits) == 5
    assert all("生化危机" in m["text"] for m in hits)

    recorded.clear()
    default_msgs = await scout.call(
        "chat_read",
        {},
        {"chat_id": "chat-a", "owner": "chat"},
    )
    assert recorded[-1]["limit"] == 20
    assert "hours" not in recorded[-1]
    assert len(default_msgs) == 20


@pytest.mark.asyncio
async def test_chat_read_multi_day_window_stratifies_and_filters_sticker_noise():
    now = time.time()
    messages = []
    # Day 1-2: 20 msgs
    for i in range(20):
        messages.append(_msg(i, f"主机联机 《战争机器》 通关感想 {i}", ts=now - 6 * 86400 + i * 3600))
    # Day 3-4: 20 msgs
    for i in range(20, 40):
        messages.append(_msg(i, f"乐队专辑 与 番剧音乐讨论 {i}", ts=now - 4 * 86400 + (i - 20) * 3600))
    # Day 5: 20 msgs
    for i in range(40, 60):
        messages.append(_msg(i, f"周末线下桌游 规则拆解 {i}", ts=now - 2 * 86400 + (i - 40) * 1800))
    # Day 6: 20 msgs
    for i in range(60, 80):
        messages.append(_msg(i, f"手机系统降级 备份恢复教程 {i}", ts=now - 1 * 86400 + (i - 60) * 1800))
    # Latest 80 msgs: 65 sticker/empty noise + 15 lunch msgs at the tail
    for i in range(80, 145):
        noise = "" if i % 3 == 0 else ("[image]" if i % 3 == 1 else "[表情包: 委屈,破防]")
        messages.append(_msg(i, noise, ts=now - 7200 + (i - 80) * 40))
    for i in range(145, 160):
        messages.append(_msg(i, f"中午吃挂面和咸菜 {i}", ts=now - 600 + (i - 145) * 30))

    class _Host:
        async def call_capability(self, name, **kwargs):
            assert name == "message.get_recent"
            lim = int(kwargs.get("limit", 100))
            return {"success": True, "messages": messages[-lim:]}

    scout = Scout(_Host())
    sampled = await scout.call(
        "chat_read",
        {"hours": 168, "limit": 24},
        {"chat_id": "chat-a", "owner": "chat"},
    )
    assert len(sampled) == 24
    # Must remain in chronological order
    timestamps = [m["timestamp"] for m in sampled]
    assert timestamps == sorted(timestamps)
    joined = "\n".join(m["text"] for m in sampled)
    for topic in ("主机联机", "乐队专辑", "线下桌游", "系统降级"):
        assert topic in joined, f"Missing early/mid topic {topic} in sampled window"
    assert sampled[-1]["message_id"] == "m-159"
    # Sampled earlier portion must not pick pure sticker/empty noise when real messages exist
    head = sampled[:-6]
    assert all(m["text"].strip() and not m["text"].startswith("[表情包:") and m["text"] != "[image]" for m in head)

    recent_only = await scout.call(
        "chat_read",
        {"hours": 168, "limit": 24, "mode": "recent"},
        {"chat_id": "chat-a", "owner": "chat"},
    )
    assert [m["message_id"] for m in recent_only] == [m["message_id"] for m in messages[-24:]]


@pytest.mark.asyncio
async def test_thread_prompt_guidance_and_chat_metadata_injection(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        # 1) Passive title capture via after_receive even when _stream_cache is empty
        await plugin.after_receive(
            message={
                "message_id": "m_passive_1",
                "session_id": "chat",
                "platform": "qq",
                "timestamp": "1794890000.0",
                "processed_plain_text": "大家好",
                "raw_message": [{"type": "text", "data": "大家好"}],
                "message_info": {
                    "user_info": {"user_id": "u_alice"},
                    "group_info": {"group_id": "100246809", "group_name": "同好研究群"},
                    "additional_config": {"relayed_by_plugin": ""},
                },
            }
        )
        snap = asdict(plugin.profiles.get("scout"))
        passive_ctx = plugin.runtime.before(
            {"profile": "scout", "chat_id": "chat", "platform": "qq", "owner": "chat", "profile_snapshot": snap}
        )
        assert passive_ctx["chat_type"] == "group"
        assert passive_ctx["chat_title"] == "同好研究群"

        # 2) Explicit _stream_cache entry overrides with authoritative stream name
        plugin._stream_cache = {
            "expires": time.monotonic() + 60.0,
            "streams": {
                "chat": {
                    "chat_id": "chat",
                    "name": "周末茶话会",
                    "chat_type": "group",
                    "group_id": "100246809",
                    "user_id": "",
                    "platform": "qq",
                    "stream_spec": "qq:group:100246809",
                }
            },
            "by_alias": {},
            "_streams_id": 1,
        }
        chat_ctx = plugin.runtime.before(
            {"profile": "scout", "chat_id": "chat", "platform": "qq", "owner": "chat", "profile_snapshot": snap}
        )
        assert chat_ctx["chat_type"] == "group"
        assert chat_ctx["chat_title"] == "周末茶话会"

        self_ctx = plugin.runtime.before(
            {"profile": "scout", "chat_id": "chat", "platform": "qq", "owner": "self", "profile_snapshot": snap}
        )
        assert "chat_type" not in self_ctx
        assert "chat_title" not in self_ctx

        captured_prompts = []

        class _Host:
            async def call_capability(self, name, **kwargs):
                if name == "llm.generate_with_tools":
                    captured_prompts.append(kwargs["prompt"][0]["content"])
                    return {"success": True, "response": "ok", "tool_calls": []}
                raise AssertionError(name)

        await Scout(_Host()).step(Profile(), [{"role": "user", "content": "摸清当前动态"}])
        sys_txt = captured_prompts[0]
        assert "<memory_context>" in sys_txt
        assert "<methods>" in sys_txt
        assert "workspace_file" in sys_txt
        assert "chat_read" in sys_txt
    finally:
        await plugin.on_unload()


def test_all_presets_and_learning_are_domain_neutral_and_pass_guard(tmp_path):
    root = tmp_path / "maisya"
    root.mkdir(parents=True, exist_ok=True)
    store = ThreadStore(root / "threads.db")
    profiles = Profiles(root / "abilities/profiles")
    vault = Vault(root / "vault")
    memory = MemoryLayers(root, store, profiles, vault, clock=lambda: 1000.0)

    planner_txt = (PRESETS_ROOT / "planner/PLANNER.md").read_text(encoding="utf-8")
    memory._guard("planner", planner_txt, "")
    assert "开放探索" in planner_txt
    assert "fresh=True" in planner_txt

    for prof_md in sorted((PRESETS_ROOT / "profiles").glob("*/PROFILE.md")):
        txt = prof_md.read_text(encoding="utf-8")
        memory._guard("profile", txt, "")

    digest_prof = (PRESETS_ROOT / "profiles/digest/PROFILE.md").read_text(encoding="utf-8")
    assert "独立游戏" not in digest_prof

    for skill_md in sorted((PRESETS_ROOT / "skills").glob("*/SKILL.md")):
        txt = skill_md.read_text(encoding="utf-8")
        memory._guard("skill", txt, "")

    learn_src = inspect.getsource(Learning._learn)
    assert "单一会话" in learn_src and "题材偏好" in learn_src

    categories = {str(entry.get("category") or "") for entry in digest.STARTER_FEEDS}
    assert {"acg", "consumer-tech", "science", "tabletop"} <= categories


@pytest.mark.asyncio
async def test_chat_read_high_traffic_group_backfills_earlier_day_buckets_via_get_by_time():
    now = time.time()
    all_messages = []
    # Days 1-6 (now - 6d to now - 24h): 1600 earlier messages across 6 days
    for i in range(1600):
        ts = (now - 6 * 86400) + i * ((5 * 86400) / 1600.0)
        if 100 <= i < 150:
            txt = f"第一天主机讨论 与 战争机器联机 {i}"
        elif 700 <= i < 760:
            txt = f"第三天番剧音乐 MyGO 专辑吐槽 {i}"
        elif 1250 <= i < 1310:
            txt = f"第五天开源工具 SQLite 升级排障 {i}"
        else:
            txt = f"日常工作摸鱼闲聊 {i}"
        all_messages.append(_msg(i, txt, ts=ts))

    # Latest 18 hours: 800 high-traffic spam messages saturating get_recent(limit=800)
    for j in range(800):
        idx = 1600 + j
        ts = (now - 18 * 3600) + j * ((18 * 3600 - 10) / 800.0)
        all_messages.append(_msg(idx, f"今日刷屏吃啥午饭 {idx}", ts=ts))

    bucket_calls = []

    class _Host:
        async def call_capability(self, name, **kwargs):
            if name == "message.get_recent":
                lim = int(kwargs.get("limit", 100))
                hrs = kwargs.get("hours")
                items = all_messages
                if hrs is not None:
                    cutoff = now - float(hrs) * 3600
                    items = [m for m in items if m["timestamp"] >= cutoff]
                return {"success": True, "messages": items[-lim:]}
            if name == "message.get_by_time_in_chat":
                bucket_calls.append(kwargs)
                st = float(kwargs.get("start_time", 0.0))
                et = float(kwargs.get("end_time", now))
                lim = int(kwargs.get("limit", 150) or 150)
                windowed = [m for m in all_messages if st <= m["timestamp"] <= et]
                return {"success": True, "messages": windowed[-lim:]}
            raise AssertionError(name)

    scout = Scout(_Host())
    sampled = await scout.call(
        "chat_read",
        {"hours": 168, "limit": 64},
        {"chat_id": "chat-a", "owner": "chat"},
    )
    assert 1 <= len(bucket_calls) <= 4
    assert len(sampled) == 64
    mids = [m["message_id"] for m in sampled]
    assert len(set(mids)) == len(mids)
    tss = [m["timestamp"] for m in sampled]
    assert tss == sorted(tss)
    joined = "\n".join(m["text"] for m in sampled)
    for phrase in ("第一天主机讨论", "第三天番剧音乐", "第五天开源工具", "今日刷屏"):
        assert phrase in joined, f"Missing {phrase} in high-traffic stratified read"

    bucket_calls.clear()
    hits = await scout.call(
        "chat_read",
        {"hours": 168, "limit": 10, "query": "第三天番剧音乐"},
        {"chat_id": "chat-a", "owner": "chat"},
    )
    assert hits and all("第三天番剧音乐" in m["text"] for m in hits)
