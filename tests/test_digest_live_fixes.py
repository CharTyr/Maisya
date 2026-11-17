"""Regression tests for 4 live-reproduced digest & cross-chat planner issues."""

from __future__ import annotations

import asyncio
import json
import logging
from email.utils import formatdate
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

import maisya.digest as digest_mod
from maisya.digest import DigestWorkspace, _normalize_url_key, run_digest_tool
from plugin import create_plugin


class _MemoryWork:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.commits = 0

    def read(self, path: str, offset: int = 0) -> dict:
        if path not in self.files:
            raise FileNotFoundError(path)
        raw = self.files[path].encode("utf-8")
        chunk = raw[offset : offset + 12000]
        next_off = offset + len(chunk) if offset + len(chunk) < len(raw) else None
        return {
            "text": chunk.decode("utf-8"),
            "next_offset": next_off,
            "bytes": len(raw),
        }

    def write(self, path: str, content: str) -> dict:
        self.files[path] = content
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    def commit(self) -> str:
        self.commits += 1
        return f"v_{self.commits}"


async def _offline_fetch(url: str, *, timeout: float = 7.5, max_bytes: int = 2_097_152):
    if url.endswith(".xml"):
        return {
            "body": (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b"<rss version=\"2.0\"><channel><title>Offline Feed</title></channel></rss>"
            ),
            "content_type": "application/xml",
            "status": 200,
            "url": url,
            "error": "",
        }
    return {"body": None, "content_type": "", "status": 404, "url": url, "error": "offline"}


async def _boot_plugin(tmp_path, *, permissions=("qq:admin_1",), enabled_chats=()):
    async def get(key, default=None):
        return list(permissions) if key == "plugin.permission" else default

    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya.live_fixes"),
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": list(enabled_chats),
        }
    )
    await plugin.on_load()
    return plugin


def _enable_persistent_digest(plugin) -> None:
    if not (plugin.profiles.path / "digest").is_dir():
        plugin.profiles.clone("default", "digest")
    cur = plugin.memory.read("profile", target="digest", document="profile.toml", allow_custom=True)
    text = cur["content"].replace("enabled = false", "enabled = true")
    if "session_mode" not in text:
        text += '\nsession_mode = "persistent"\n'
    plugin.memory.write(
        "profile",
        text,
        target="digest",
        document="profile.toml",
        expected_version=cur["version"],
        allow_custom=True,
    )


def _private_msg(message_id: str, text: str, *, session_id: str, user_id: str) -> dict:
    return {
        "message_id": message_id,
        "session_id": session_id,
        "platform": "qq",
        "processed_plain_text": text,
        "raw_message": [{"type": "text", "data": text}],
        "message_info": {
            "user_info": {"user_id": user_id},
            "group_info": None,
            "additional_config": {"relayed_by_plugin": False},
        },
    }


@pytest.mark.asyncio
async def test_second_collect_merges_pool_preserving_first_collect_source_text(monkeypatch):
    monkeypatch.setattr(digest_mod, "fetch_public_bytes", _offline_fetch)
    clock_ref = [1_794_880_000.0]
    work = _MemoryWork()
    dw = DigestWorkspace(work, fetcher=_offline_fetch, clock=lambda: clock_ref[0])
    await dw.add_feed("https://local.example/rss.xml", title="Local Empty", auto=False)

    tail_quote_a = "Solo developer confirmed 128000 copies sold in first week on Steam."
    body_a = ("Indie roguelite Slotbound builds turn-based combos on a 3x3 board. " * 25) + tail_quote_a
    assert len(body_a) > 1000
    body_b = "Airship physics postmortem cuts mesh vertices by 95% with deterministic wind simulation."

    url_a = "https://gamedev.example/posts/slotbound-128k"
    url_b = "https://engine.example/posts/airship-physics"
    url_c = "https://studio.example/posts/turn-based-tactics"

    round_state = {"batch": 1}

    class _FakeWeb:
        async def search_broad(self, q, max_results=8, context=None):
            b = round_state["batch"]
            if b == 1:
                items = [{"url": url_a, "title": "Slotbound Reaches 128K Copies", "published_at": formatdate(clock_ref[0] - 3600, usegmt=True)}]
            elif b == 2:
                items = [{"url": url_b, "title": "Airship Physics Overhaul", "published_at": formatdate(clock_ref[0] - 3600, usegmt=True)}]
            else:
                items = [{"url": url_c, "title": "Turn-Based Tactics Devlog", "published_at": formatdate(clock_ref[0] - 3600, usegmt=True)}]
            return {"results": items}

        async def fetch(self, url, context=None):
            pub = formatdate(clock_ref[0] - 3600, usegmt=True)
            if url == url_a:
                return {"url": url, "title": "Slotbound", "content": body_a, "published_at": pub}
            if url == url_b:
                return {"url": url, "title": "Airship", "content": body_b, "published_at": pub}
            return {
                "url": url,
                "title": "Tactics",
                "content": "Tactics studio shared grid combat telemetry and turn pacing curves across 40k sessions.",
                "published_at": pub,
            }

    col1 = await dw.collect(web=_FakeWeb(), queries=["indie sales"], max_items=10)
    assert [c["url"] for c in col1["candidates"]] == [url_a]
    assert len(col1["candidates"][0]["source_text"]) <= 850

    clock_ref[0] += 60.0
    round_state["batch"] = 2
    col2 = await dw.collect(web=_FakeWeb(), queries=["airship physics"], max_items=10)
    assert [c["url"] for c in col2["candidates"]] == [url_b]

    # Submit both A (relying on cached tail beyond 850 chars!) and B from two collects
    sel = dw.score_select(
        [
            {
                "title": "《Slotbound》首周卖出 12.8 万份：3x3 棋盘自动战斗构筑",
                "url": url_a,
                "quote": tail_quote_a,
                "brief": "独立肉鸽《Slotbound》首周在 Steam 卖出了 12.8 万份，核心机制围绕 3x3 棋盘触发的联动决策展开。",
                "why": "销量数字硬且玩法拆解清晰。",
                "topic": "销量口碑",
                "source": "gamedev.example",
                "info": 5,
                "source_score": 5,
                "relevance": 5,
                "timeliness": 5,
                "chat": 4,
            },
            {
                "title": "飞艇物理重构：确定性风场模拟与 95% 网格瘦身",
                "url": url_b,
                "quote": "cuts mesh vertices by 95% with deterministic wind simulation",
                "brief": "工程日志复盘了确定性风场物理同步思路，并把飞艇渲染网格顶点削减了 95%。",
                "why": "渲染与物理工程干货密集。",
                "topic": "引擎工具",
                "source": "engine.example",
                "info": 5,
                "source_score": 5,
                "relevance": 4,
                "timeliness": 5,
                "chat": 4,
            },
        ],
        max_items=5,
    )
    assert {it["url"] for it in sel["selected"]} == {url_a, url_b}, sel

    # Advance past 6h retention window -> collect #3 prunes stale A/B
    clock_ref[0] += 6 * 3600 + 10.0
    round_state["batch"] = 3
    await dw.collect(web=_FakeWeb(), queries=["tactics devlog"], max_items=10)
    pool_doc = json.loads(work.read("project/digest_pool.json")["text"])
    assert set(pool_doc["by_url"]) == {_normalize_url_key(url_c)}
    assert pool_doc["total_discovered"] == 1


@pytest.mark.asyncio
async def test_score_select_retry_before_render_does_not_self_dedupe_or_wipe_draft(monkeypatch):
    monkeypatch.setattr(digest_mod, "fetch_public_bytes", _offline_fetch)
    clock_ref = [1_794_880_000.0]
    work = _MemoryWork()
    dw = DigestWorkspace(work, fetcher=_offline_fetch, clock=lambda: clock_ref[0])
    added = await dw.add_feed("https://indie.example/rss.xml", title="IndieWire", auto=True)
    feed_id = added["feed"]["id"]
    pub_iso = formatdate(clock_ref[0] - 1800, usegmt=True)

    def _cand(slug: str, title: str, quote: str, src: str, *, topic: str, fid: str = "") -> dict:
        d = {
            "title": title,
            "url": f"https://news.example/{slug}",
            "published": pub_iso,
            "source_text": src,
            "quote": quote,
            "brief": f"{title} 的具体实机与数值进展说明，包含核心设计与机制改动详情，便于直接了解看点。",
            "why": "一手来源且机制细节清楚。",
            "topic": topic,
            "source": "news.example",
            "info": 5,
            "source_score": 5,
            "relevance": 5,
            "timeliness": 5,
            "chat": 4,
        }
        if fid:
            d["feed_id"] = fid
        return d

    item_a = _cand("a", "《赤色发条》重构弹反循环", "combat loop rebuilt around strict parry windows", "Author note: combat loop rebuilt around strict parry windows for bosses.", topic="玩法拆解", fid=feed_id)
    item_b = _cand("b", "《星标边境》首周破十万销量", "crossed 100k units with 92% positive reviews", "Steam data: crossed 100k units with 92% positive reviews in seven days.", topic="销量口碑")
    item_c = _cand("c", "《冬至港湾》水面着色器拆解", "planar reflection shader runs under 0.4ms on integrated GPU", "Tech breakdown: planar reflection shader runs under 0.4ms on integrated GPU.", topic="引擎工具")
    item_d_bad = _cand("d", "《宿之掟》定格守夜机制", "wrong translated quote text", "公式説明：部屋を見張り違和感を探す心理ホラー。", topic="新作试玩")
    item_d_fixed = _cand("d", "《宿之掟》定格守夜机制", "部屋を見張り違和感を探す心理ホラー", "公式説明：部屋を見張り違和感を探す心理ホラー。", topic="新作试玩")
    item_e_bad = _cand("e", "《虚构测试》误填引文条目", "nonexistent phrase", "Actual page body mentions tile grid rendering only.", topic="开发日志")

    res1 = dw.score_select([item_a, item_b, item_c, item_d_bad], max_items=5, run_key="th_demo:8")
    assert len(res1["selected"]) == 3
    assert len(res1["rejected"]) == 1
    hist1 = dw._load_history()
    assert len(hist1["items"]) == 3
    assert len(hist1.get("unrendered_ids") or []) == 3

    # Retry within same unrendered run: resubmitting A/B/C + fixed D must NOT self-dedupe or double-count kept_total
    clock_ref[0] += 40.0
    res2 = dw.score_select([item_a, item_b, item_c, item_d_fixed], max_items=5, run_key="th_demo:8")
    assert len(res2["selected"]) == 4, res2["rejected"]
    assert all("重复" not in str(r.get("reject_reason") or "") for r in res2["rejected"])
    hist2 = dw._load_history()
    urls_in_hist2 = [it["url"] for it in hist2["items"]]
    assert len(urls_in_hist2) == 4 and len(set(urls_in_hist2)) == 4
    feeds2 = {f["id"]: f for f in dw._load_sources()["feeds"]}
    assert feeds2[feed_id]["kept_total"] == 1

    # Delta retry with 0 valid items BEFORE render must preserve the 4 validated draft items (not wipe to [])
    clock_ref[0] += 20.0
    res3 = dw.score_select([item_e_bad], max_items=5, run_key="th_demo:8")
    assert len(res3["selected"]) == 4
    assert len(dw._load_history()["last_selected"]) == 4

    # Rendering commits the draft and clears unrendered_ids
    card_res = await run_digest_tool(work, {"action": "render_card_html", "output_path": "out/digest-card.html"})
    assert card_res["count"] == 4
    assert dw._load_history().get("unrendered_ids") == []

    # Subsequent revision AFTER render SHOULD reject A as 14-day duplicate
    res4 = dw.score_select([item_a], max_items=5, run_key="th_demo:9")
    assert res4["selected"] == []
    assert res4["rejected"][0]["reject_reason"] == "与近 14 天已记录资讯链接或主题重复"

    # Explicit empty candidates=[] clears last_selected cleanly
    res5 = dw.score_select([], max_items=5, run_key="th_demo:9")
    assert res5["selected"] == []
    assert dw._load_history()["last_selected"] == []
    empty_card = await run_digest_tool(work, {"action": "render_card_html", "output_path": "out/digest-card.html"})
    assert empty_card["count"] == 0


@pytest.mark.asyncio
async def test_active_persistent_thread_hides_previous_revision_result_from_planner(tmp_path):
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    plugin = await _boot_plugin(
        tmp_path, permissions=("qq:admin_1",), enabled_chats=(f"qq:group:100246809", group_sid)
    )
    plugin.settings.sandbox.enabled = False
    _enable_persistent_digest(plugin)

    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    phase = {"rev": 1, "attach_done": False}
    rev2_started = asyncio.Event()
    rev2_release = asyncio.Event()

    async def capability(name, **arguments):
        if name == "chat.get_all_streams":
            return {"success": True, "streams": []}
        if name == "render.html2png":
            return {"success": True, "result": {"image_base64": png_b64, "mime_type": "image/png", "width": 1, "height": 1}}
        if name in {"llm.generate", "llm.generate_with_tools"}:
            if phase["rev"] == 1:
                if not phase["attach_done"]:
                    phase["attach_done"] = True
                    return {
                        "success": True,
                        "response": "",
                        "tool_calls": [
                            {"id": "c1", "type": "function", "function": {"name": "html_render", "arguments": json.dumps({"html": "<div>old</div>", "output_path": "out/digest-card.png"})}},
                            {"id": "c2", "type": "function", "function": {"name": "result_attach", "arguments": json.dumps({"paths": ["out/digest-card.png"]})}},
                        ],
                    }
                return {"success": True, "response": "早间旧简报", "tool_calls": []}
            rev2_started.set()
            await rev2_release.wait()
            return {"success": True, "response": "新版简报", "tool_calls": []}
        return {"success": True}

    plugin.ctx.call_capability = capability
    try:
        r1 = await plugin.agent_tool(
            action="start",
            profile="digest",
            message="早间简报",
            wait_seconds=10,
            chat_id=group_sid,
            platform="qq",
        )
        assert r1["state"] == "idle", r1
        old_att_handles = [a["handle"] for a in (r1["result"].get("attachments") or [])]
        assert old_att_handles

        phase["rev"] = 2
        r2 = await plugin.agent_tool(
            action="start",
            profile="digest",
            message="新一期简报",
            wait_seconds=0.05,
            chat_id=group_sid,
            platform="qq",
        )
        await asyncio.wait_for(rev2_started.wait(), timeout=2.0)
        assert r2["state"] == "active"
        assert r2["result"] == {}
        assert "早间旧简报" not in r2["content"]
        for h in old_att_handles:
            assert h not in r2["content"]

        listed = await plugin.agent_tool(action="list", profile="digest", chat_id=group_sid, platform="qq")
        assert listed["threads"] and listed["threads"][0]["state"] == "active"
        assert listed["threads"][0]["result"] == {}

        searched = await asyncio.to_thread(plugin.threads.search, "", group_sid)
        assert searched and searched[0]["state"] == "active"
        assert searched[0]["result"] == {}

        rev2_release.set()
        await plugin.engine.drain()
        done_list = plugin.threads.list_threads(chat_id=group_sid, profile="digest")
        done_search = plugin.threads.search("", group_sid)
        assert done_list[0]["result"]["text"] == "新版简报"
        assert done_search[0]["result"]["text"] == "新版简报"
    finally:
        rev2_release.set()
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_unenabled_admin_dm_bootstrap_and_missing_source_msg_id_guidance(tmp_path):
    group_sid = "c0300b39c80870992aca16d9fcb724c5"
    priv_sid = "priv_admin_session"
    plugin = await _boot_plugin(
        tmp_path, permissions=("qq:admin_1",), enabled_chats=(f"qq:group:100246809", group_sid)
    )
    plugin.ctx.call_capability = lambda name, **kw: asyncio.sleep(
        0,
        result={
            "success": True,
            "streams": [
                {
                    "session_id": group_sid,
                    "platform": "qq",
                    "is_group_session": True,
                    "chat_type": "group",
                    "group_id": "100246809",
                    "group_name": "测试群A",
                }
            ],
        },
    ) if name == "chat.get_all_streams" else asyncio.sleep(0, result={"success": True})

    try:
        await plugin.after_receive(
            message=_private_msg("m_adm_101", "帮我试跑简报", session_id=priv_sid, user_id="admin_1")
        )
        plan = await plugin.before_planner(
            session_id=priv_sid,
            tool_definitions=[
                {"name": "maisya_agent"},
                {"name": "maisya_recall"},
                {"name": "maisya_shape"},
                {"name": "maisya_later"},
            ],
            items=[],
        )
        boot_text = "\n".join(
            p.get("text", "")
            for it in plan.get("modified_kwargs", {}).get("items", [])
            for p in it.get("parts", [])
        )
        assert "<maisya_admin_bootstrap>" in boot_text
        assert 'source_msg_id="m_adm_101"' in boot_text or "source_msg_id='m_adm_101'" in boot_text

        recall_no_id = await plugin.recall_tool(
            query="简报", target_chat="qq:group:100246809", chat_id=priv_sid, platform="qq"
        )
        assert recall_no_id["success"] is False
        assert "跨会话检索需要真实管理员消息授权" in recall_no_id["error"]
        assert "source_msg_id" in recall_no_id["error"]

        agent_no_id = await plugin.agent_tool(
            action="start",
            profile="digest",
            message="试跑",
            deliver_to="current",
            chat_id=priv_sid,
            platform="qq",
        )
        assert agent_no_id["success"] is False
        assert "当前会话没有启用 Maisya" in agent_no_id["error"]
        assert "source_msg_id" in agent_no_id["error"]
        assert "target_chat" in agent_no_id["error"]

        # Non-admin stranger with explicit source_msg_id remains rejected without privilege escalation
        await plugin.after_receive(
            message=_private_msg("m_stranger", "我想越权", session_id="priv_stranger", user_id="stranger_1")
        )
        stranger_res = await plugin.agent_tool(
            action="start",
            profile="digest",
            target_chat="qq:group:100246809",
            deliver_to="current",
            message="越权",
            source_msg_id="m_stranger",
            chat_id="priv_stranger",
            platform="qq",
        )
        assert stranger_res["success"] is False
    finally:
        await plugin.on_unload()
