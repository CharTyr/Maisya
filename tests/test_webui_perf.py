import asyncio
import hashlib
import json
import sqlite3
import time

import pytest

from maisya.abilities import Profile
from maisya.trust import MessageStore
from tests.test_foundation import loaded


def _hex_sid(seed: str) -> str:
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def _build_mixed_streams(count: int = 200) -> list[dict]:
    streams = []
    for idx in range(count):
        if idx % 2 == 0:
            gid = str(100200000 + idx)
            streams.append(
                {
                    "session_id": _hex_sid(f"qq:group:{gid}"),
                    "platform": "qq",
                    "group_id": gid,
                    "group_name": f"Group {idx}",
                    "chat_type": "group",
                    "is_group_session": True,
                    "last_active_time": 1794459000.0 + idx,
                }
            )
        else:
            uid = str(200300000 + idx)
            streams.append(
                {
                    "session_id": _hex_sid(f"telegram:private:{uid}"),
                    "platform": "telegram",
                    "user_id": uid,
                    "user_nickname": f"User {idx}",
                    "chat_type": "private",
                    "is_group_session": False,
                    "last_active_time": 1794459000.0 + idx,
                }
            )
    return streams


@pytest.mark.asyncio
async def test_200_streams_o1_routing_autoselect_and_external_db_invalidation(
    tmp_path, monkeypatch
):
    plugin = await loaded(tmp_path)
    try:
        raw_streams = _build_mixed_streams(200)
        first_sid = raw_streams[0]["session_id"]
        target_group_sid = raw_streams[4]["session_id"]
        target_gid = raw_streams[4]["group_id"]
        plugin.settings.enabled_chats = [first_sid, f"qq:group:{target_gid}"]
        plugin.settings.chat_proactive_levels = {
            f"telegram:private:{raw_streams[5]['user_id']}": "quiet"
        }

        async def capability(name, **arguments):
            if name == "chat.get_all_streams":
                return {"success": True, "streams": raw_streams}
            if name == "chat.get_avatar":
                return {"success": True, "avatar": {"status": "unavailable", "url": ""}}
            if name in ("component.list_loaded_plugins", "api.list"):
                return {"success": True, "plugins": [], "apis": []}
            if name == "statistics.local.models":
                return {"success": True, "models": []}
            return {"success": False}

        plugin.ctx.call_capability = capability
        thread_row = plugin.threads.create(
            Profile(), "Perf probe", chat_id=first_sid, platform="qq", user_id=""
        )

        # Seed stream/route caches once, simulating steady-state runtime.
        await plugin._ensure_stream_cache(force=True)
        await plugin.console_query(chat_id=first_sid, thread_id=thread_row["thread_id"])

        real_connect = sqlite3.connect
        trust_db_connects = 0
        total_db_connects = 0
        msg_db_path = str(plugin.messages.path)

        def counted_connect(database, *args, **kwargs):
            nonlocal trust_db_connects, total_db_connects
            if str(database) == msg_db_path:
                total_db_connects += 1
                import traceback

                if any(frame.filename.endswith("trust.py") for frame in traceback.extract_stack()):
                    trust_db_connects += 1
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr("maisya.trust.sqlite3.connect", counted_connect)

        t0 = time.monotonic()
        snap = await plugin.console_query()
        t_console = time.monotonic() - t0
        assert snap["success"] is True
        assert snap["selected"] is not None
        assert snap["selected"]["thread_id"] == thread_row["thread_id"]
        assert t_console < 0.08

        for section in ("settings", "abilities", "outputs", "memory"):
            start = time.monotonic()
            res = await plugin.manage_read(section)
            elapsed = time.monotonic() - start
            assert res["success"] is True
            assert elapsed < 0.08, f"{section} took {elapsed:.3f}s"
            if section == "settings":
                by_sid = {c["chat_id"]: c for c in res["known_chats"]}
                assert len(by_sid) >= 200
                assert by_sid[target_group_sid]["enabled"] is True

        assert trust_db_connects <= 3
        assert total_db_connects <= 25

        # External process writes directly to messages.db; cache must invalidate via (mtime_ns, size).
        ext_sid = _hex_sid("qq:private:9009001")
        ext_store = MessageStore(plugin.messages.path)
        ext_store.upsert_route(
            ext_sid,
            platform="qq",
            chat_type="private",
            user_id="9009001",
            account_id="bot-a",
            timestamp=1800000000.0,
        )
        monkeypatch.undo()
        loaded_route = plugin.messages.route_for(ext_sid)
        assert loaded_route is not None
        assert loaded_route["user_id"] == "9009001"
        assert ext_sid in {h["session_id"] for h in plugin.messages.find_sessions("qq:private:9009001")}

        ext_store.upsert_route(
            ext_sid,
            platform="qq",
            chat_type="private",
            user_id="9009001",
            account_id="bot-b",
            timestamp=1800000001.0,
        )
        assert plugin.messages.route_for(ext_sid)["account_id"] == "bot-b"
    finally:
        await plugin.on_unload()


def test_upsert_routes_batching_and_noop_skip(tmp_path, monkeypatch):
    store = MessageStore(tmp_path / "batch_routes.db")
    entries = []
    for idx in range(200):
        entries.append(
            {
                "session_id": _hex_sid(f"batch:{idx}"),
                "platform": "qq" if idx % 2 == 0 else "telegram",
                "chat_type": "group" if idx % 2 == 0 else "private",
                "group_id": str(500000 + idx) if idx % 2 == 0 else "",
                "user_id": "" if idx % 2 == 0 else str(600000 + idx),
                "account_id": "bot-1",
                "scope": "main",
                "timestamp": 1794459000.0 + idx,
            }
        )

    real_connect = sqlite3.connect
    connect_calls = 0

    def counting_connect(database, *args, **kwargs):
        nonlocal connect_calls
        if str(database) == str(store.path):
            connect_calls += 1
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr("maisya.trust.sqlite3.connect", counting_connect)

    store.upsert_routes(entries)
    assert connect_calls <= 1
    assert len(store.list_routes(limit=300)) == 200
    assert store.route_for(entries[42]["session_id"])["group_id"] == "500042"
    assert store.route_for(entries[43]["session_id"])["user_id"] == "600043"

    # Re-upserting identical routes must not open SQLite for writes.
    connect_calls = 0
    store.upsert_routes(entries)
    assert connect_calls == 0

    # Updating one entry writes in a single transaction.
    updated_entries = [dict(item) for item in entries]
    updated_entries[0]["account_id"] = "bot-2"
    connect_calls = 0
    store.upsert_routes(updated_entries)
    assert connect_calls <= 1
    assert store.route_for(entries[0]["session_id"])["account_id"] == "bot-2"


@pytest.mark.asyncio
async def test_console_avatar_nonblocking_timeout_and_negative_cache(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        plugin.settings.enabled_chats = ["group_fast", "group_slow", "group_fail"]
        avatar_calls = []

        async def capability(name, **arguments):
            if name == "chat.get_all_streams":
                return {
                    "success": True,
                    "streams": [
                        {
                            "session_id": "group_fast",
                            "platform": "qq",
                            "group_id": "101",
                            "group_name": "Fast",
                            "chat_type": "group",
                            "is_group_session": True,
                        },
                        {
                            "session_id": "group_slow",
                            "platform": "qq",
                            "group_id": "102",
                            "group_name": "Slow",
                            "chat_type": "group",
                            "is_group_session": True,
                        },
                        {
                            "session_id": "group_fail",
                            "platform": "qq",
                            "group_id": "103",
                            "group_name": "Fail",
                            "chat_type": "group",
                            "is_group_session": True,
                        },
                    ],
                }
            if name == "chat.get_avatar":
                tid = arguments.get("target_id")
                avatar_calls.append(tid)
                if tid == "101":
                    return {
                        "success": True,
                        "avatar": {"status": "available", "url": "data:image/png;base64,iVBORw0KGgo="},
                    }
                if tid == "102":
                    await asyncio.sleep(1.5)
                    return {
                        "success": True,
                        "avatar": {"status": "available", "url": "data:image/png;base64,QUJD"},
                    }
                raise RuntimeError("avatar upstream down")
            return {"success": False}

        plugin.ctx.call_capability = capability
        plugin.threads.create(Profile(), "Fast", chat_id="group_fast", platform="qq", user_id="")
        plugin.threads.create(Profile(), "Slow", chat_id="group_slow", platform="qq", user_id="")
        plugin.threads.create(Profile(), "Fail", chat_id="group_fail", platform="qq", user_id="")

        t0 = time.monotonic()
        snap = await plugin.console_query()
        elapsed = time.monotonic() - t0
        assert elapsed < 0.45
        assert snap["chat_sources"]["group_fast"]["avatar"] == "data:image/png;base64,iVBORw0KGgo="
        assert snap["chat_sources"]["group_slow"]["avatar"] == ""
        assert snap["chat_sources"]["group_fail"]["avatar"] == ""

        # Immediate second query must reuse negative cache and deduplicate inflight task.
        snap_again = await plugin.console_query()
        assert snap_again["chat_sources"]["group_fail"]["avatar"] == ""
        assert avatar_calls.count("103") == 1
        assert avatar_calls.count("102") == 1

        # Wait for the 1.5s inflight background fetch to complete, then verify cached result arrives inline.
        await asyncio.sleep(1.65)
        calls_before = len(avatar_calls)
        snap_after = await plugin.console_query()
        assert snap_after["chat_sources"]["group_slow"]["avatar"] == "data:image/png;base64,QUJD"
        assert len(avatar_calls) == calls_before
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_manage_read_batch_aggregates_and_redacts_secrets(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        plugin.settings.ui_style = "maiwork"
        secret_val = "super-secret-batch-token-987654"
        plugin.vault.set("perf_key", secret_val)

        async def capability(name, **arguments):
            if name == "chat.get_all_streams":
                return {
                    "success": True,
                    "streams": [
                        {
                            "session_id": "chat",
                            "platform": "qq",
                            "group_id": "100246809",
                            "group_name": f"Room {secret_val}",
                            "chat_type": "group",
                            "is_group_session": True,
                        }
                    ],
                }
            if name in ("component.list_loaded_plugins", "api.list"):
                return {"success": True, "plugins": [], "apis": []}
            return {"success": False}

        plugin.ctx.call_capability = capability

        res = await plugin.manage_read(
            "batch",
            json.dumps(
                {
                    "sections": {
                        "settings": {},
                        "services": {},
                        "health": {},
                        "intents": {},
                        "memory": {},
                    }
                }
            ),
        )
        assert res["success"] is True
        assert res["ui_style"] == "modern"
        assert res["sections"]["settings"]["ui_style"] == "modern"
        assert set(res["sections"].keys()) == {"settings", "services", "health", "intents", "memory"}
        assert "known_chats" in res["sections"]["settings"]
        assert "running" in res["sections"]["health"]
        assert "configuration" in res["sections"]["services"]
        assert "intents" in res["sections"]["intents"]
        assert secret_val not in json.dumps(res, ensure_ascii=False)

        bad_sec = await plugin.manage_read("batch", json.dumps({"sections": {"batch": {}}}))
        assert bad_sec["success"] is False

        too_many = await plugin.manage_read(
            "batch",
            json.dumps(
                {
                    "sections": {
                        "settings": {},
                        "services": {},
                        "health": {},
                        "intents": {},
                        "abilities": {},
                        "asset": {},
                        "memory": {},
                    }
                }
            ),
        )
        assert too_many["success"] is False
    finally:
        await plugin.on_unload()
