import asyncio
import io
import json
import logging
import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from maibot_sdk.context import PluginPaths

from maisya.abilities import TOOLSETS, Profile
from maisya.bridge import Bridge
from maisya.management import Management
from maisya.packages import export_package, import_package_bundle
from maisya.trust import Message, MessageStore
from plugin import create_plugin


async def _loaded_plugin(tmp_path, *, enabled_chats=(), admins=()):
    async def get(key, default=None):
        return None if key == "plugin.permission" else default

    plugin = create_plugin()
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=get),
        paths=PluginPaths(
            data_dir=tmp_path / "host/data/plugins/chartyr.maisya",
            runtime_dir=tmp_path / "runtime",
        ),
        logger=logging.getLogger("test.maisya"),
    )
    plugin.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "enabled_chats": list(enabled_chats),
            "admins": list(admins),
        }
    )
    await plugin.on_load()
    return plugin


def _patch_profile_toml(profiles, profile_id: str, **updates) -> None:
    toml_path = profiles.path / profile_id / "profile.toml"
    raw = toml_path.read_text(encoding="utf-8")
    lines = []
    seen = set()
    for line in raw.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in updates:
            val = updates[key]
            if isinstance(val, bool):
                lines.append(f"{key} = {'true' if val else 'false'}")
            elif isinstance(val, int):
                lines.append(f"{key} = {val}")
            elif isinstance(val, (list, tuple)):
                lines.append(f"{key} = {json.dumps(list(val), ensure_ascii=False)}")
            else:
                lines.append(f'{key} = "{val}"')
            seen.add(key)
        else:
            lines.append(line)
    for key, val in updates.items():
        if key not in seen:
            if isinstance(val, bool):
                lines.append(f"{key} = {'true' if val else 'false'}")
            elif isinstance(val, int):
                lines.append(f"{key} = {val}")
            elif isinstance(val, (list, tuple)):
                lines.append(f"{key} = {json.dumps(list(val), ensure_ascii=False)}")
            else:
                lines.append(f'{key} = "{val}"')
    toml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# M1: Speaker Attribution, Queue Coalescing, Permission Isolation & Window
# ---------------------------------------------------------------------------


def test_message_store_migrates_legacy_db_and_preserves_sender_name(tmp_path):
    db_path = tmp_path / "legacy_trust.db"
    with sqlite3.connect(db_path) as db, db:
        db.execute(
            "CREATE TABLE trusted_sources ("
            "message_id TEXT, alias TEXT, session_id TEXT, platform TEXT, "
            "user_id TEXT, relayed INTEGER, local_operator INTEGER, received REAL, "
            "PRIMARY KEY (message_id, session_id, platform))"
        )
    store = MessageStore(db_path, clock=lambda: 1000.0)
    event = {
        "message_id": "m-card-1",
        "session_id": "qq:group:100246809",
        "platform": "qq",
        "sender_name": "兜底昵称",
        "message_info": {
            "user_info": {
                "user_id": "20001",
                "user_nickname": "原昵称",
                "user_cardname": "阿诚",
            },
            "group_info": {"group_id": "100246809"},
        },
    }
    msg = Message.from_event(event)
    assert msg.sender_name == "阿诚"
    store.record(msg)
    found = store.lookup("m-card-1", "qq:group:100246809", "qq")
    assert found is not None
    assert found.sender_name == "阿诚"
    assert found.user_id == "20001"


@pytest.mark.asyncio
async def test_group_speaker_envelope_and_private_unchanged(tmp_path):
    plugin = await _loaded_plugin(
        tmp_path,
        enabled_chats=["qq:group:100246809", "qq:private:10001"],
        admins=["qq:10001"],
    )
    prompts: list[tuple[str, list[dict]]] = []

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            prompt_list = list(arguments.get("prompt") or [])
            user_turns = [
                m["content"]
                for m in prompt_list
                if m.get("role") == "user" and not str(m.get("content") or "").startswith(("<memory_context>", "<thread_history>"))
            ]
            prompts.append((prompt_list[0].get("content", "") if prompt_list else "", user_turns))
            last_u = user_turns[-1] if user_turns else ""
            return {"response": f"收到:{len(last_u)}", "tool_calls": []}
        raise RuntimeError(f"unexpected rpc {name}")

    plugin.ctx.call_capability = capability
    try:
        plugin.messages.record(
            Message.from_event(
                {
                    "message_id": "m-u1",
                    "session_id": "qq:group:100246809",
                    "platform": "qq",
                    "message_info": {
                        "user_info": {"user_id": "20001", "user_cardname": "阿诚"},
                        "group_info": {"group_id": "100246809"},
                    },
                }
            )
        )
        plugin.messages.record(
            Message.from_event(
                {
                    "message_id": "m-dm-1",
                    "session_id": "qq:private:10001",
                    "platform": "qq",
                    "message_info": {
                        "user_info": {"user_id": "10001", "user_nickname": "管理员"}
                    },
                }
            )
        )
        res_grp = await plugin.agent_tool(
            action="start",
            profile="default",
            source_msg_id="m-u1",
            message="我推开东侧木门",
            wait_seconds=5,
            chat_id="qq:group:100246809",
            platform="qq",
        )
        assert res_grp["success"], res_grp
        grp_user_msgs = prompts[-1][1]
        assert '<speaker id="20001" name="阿诚" msg_id="m-u1" />' in grp_user_msgs[-1]
        assert "我推开东侧木门" in grp_user_msgs[-1]

        res_dm = await plugin.agent_tool(
            action="start",
            profile="default",
            source_msg_id="m-dm-1",
            message="仅看私聊指令",
            wait_seconds=5,
            chat_id="qq:private:10001",
            platform="qq",
        )
        assert res_dm["success"], res_dm
        dm_user_msgs = prompts[-1][1]
        assert "<speaker " not in dm_user_msgs[-1]
        assert dm_user_msgs[-1] == "仅看私聊指令"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_workspace_batch_drain_coalesces_waiters_and_respects_permission_boundaries(tmp_path):
    plugin = await _loaded_plugin(
        tmp_path,
        enabled_chats=["qq:group:100246809"],
        admins=["qq:99999"],
    )
    plugin.profiles.clone("default", "board")
    _patch_profile_toml(
        plugin.profiles,
        "board",
        session_mode="persistent",
        access="everyone",
        permission_mode="autonomous",
    )

    entered_rev1 = asyncio.Event()
    release_rev1 = asyncio.Event()
    step_batches: list[list[str]] = []

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            msgs = [
                m["content"]
                for m in arguments["prompt"]
                if m.get("role") == "user" and not str(m.get("content") or "").startswith(("<memory_context>", "<thread_history>"))
            ]
            if len(step_batches) == 0:
                step_batches.append(msgs)
                entered_rev1.set()
                await release_rev1.wait()
                return {"response": "第一轮完成", "tool_calls": []}
            step_batches.append(msgs)
            return {"response": f"批量汇总:{len(msgs)}", "tool_calls": []}
        raise RuntimeError(f"unexpected rpc {name}")

    plugin.ctx.call_capability = capability
    try:
        for mid, uid, uname in (
            ("m-1", "20001", "玩家A"),
            ("m-2", "20002", "玩家B"),
            ("m-3", "20003", "玩家C"),
            ("m-4", "20004", "玩家D"),
            ("m-adm", "99999", "管理员"),
            ("m-5", "20005", "玩家E"),
        ):
            plugin.messages.record(
                Message.from_event(
                    {
                        "message_id": mid,
                        "session_id": "qq:group:100246809",
                        "platform": "qq",
                        "message_info": {
                            "user_info": {"user_id": uid, "user_cardname": uname},
                            "group_info": {"group_id": "100246809"},
                        },
                    }
                )
            )

        t1 = asyncio.create_task(
            plugin.agent_tool(
                action="start",
                profile="board",
                source_msg_id="m-1",
                message="动作1",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        await asyncio.wait_for(entered_rev1.wait(), timeout=5.0)
        tid = plugin.threads.find_primary_thread("board", "qq:group:100246809")["thread_id"]

        async def _wait_rev(target_rev: int) -> None:
            for _ in range(50):
                if plugin.threads.get(tid, "qq:group:100246809")["revision"] >= target_rev:
                    return
                await asyncio.sleep(0.01)

        t2 = asyncio.create_task(
            plugin.agent_tool(
                action="send",
                thread_id=tid,
                source_msg_id="m-2",
                message="动作2",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        await _wait_rev(2)
        t3 = asyncio.create_task(
            plugin.agent_tool(
                action="send",
                thread_id=tid,
                source_msg_id="m-3",
                message="动作3",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        await _wait_rev(3)
        t4 = asyncio.create_task(
            plugin.agent_tool(
                action="send",
                thread_id=tid,
                source_msg_id="m-4",
                message="动作4",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        await _wait_rev(4)

        release_rev1.set()
        r1, r2, r3, r4 = await asyncio.gather(t1, t2, t3, t4)
        assert r1["result"]["text"] == "第一轮完成"
        assert r2.get("coalesced_into") == 4 and r2.get("superseded") is True
        assert "text" not in (r2.get("result") or {})
        assert r3.get("coalesced_into") == 4 and r3.get("superseded") is True
        assert r4["result"]["text"].startswith("批量汇总:")
        # Total 2 LLM steps: rev1 + coalesced (rev2, rev3, rev4)
        assert len(step_batches) == 2
        assert "玩家B" in step_batches[1][-3] and "玩家C" in step_batches[1][-2] and "玩家D" in step_batches[1][-1]

        # Only latest revision (rev=4) offered by bridge, intermediate coalesced returns retired
        offered = await plugin.bridge.offer("qq:group:100246809")
        assert [item["revision"] for item in offered] == [4]
        with plugin.threads.connect() as db:
            unconsumed = [
                r["revision"]
                for r in db.execute(
                    "SELECT revision FROM return_intents WHERE thread_id=? AND consumed=0",
                    (tid,),
                ).fetchall()
            ]
        assert 2 not in unconsumed and 3 not in unconsumed
        await plugin.bridge.end_turn("qq:group:100246809")

        # Permission isolation boundary: admin rev followed by non-admin rev MUST NOT coalesce
        entered_rev1.clear()
        release_rev1.clear()
        step_batches.clear()
        # Re-use block gate on first call
        async def capability_split(name, **arguments):
            if name == "llm.generate_with_tools":
                msgs = [
                    m["content"]
                    for m in arguments["prompt"]
                    if m.get("role") == "user" and not str(m.get("content") or "").startswith(("<memory_context>", "<thread_history>"))
                ]
                step_batches.append(msgs[-1:])
                if len(step_batches) == 1:
                    entered_rev1.set()
                    await release_rev1.wait()
                return {"response": f"split-{len(step_batches)}", "tool_calls": []}
            raise RuntimeError(name)

        plugin.ctx.call_capability = capability_split
        ta = asyncio.create_task(
            plugin.agent_tool(
                action="send",
                thread_id=tid,
                source_msg_id="m-2",
                message="垫底轮",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        await asyncio.wait_for(entered_rev1.wait(), timeout=5.0)
        await _wait_rev(5)
        tb = asyncio.create_task(
            plugin.agent_tool(
                action="send",
                thread_id=tid,
                source_msg_id="m-adm",
                message="管理员操作",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        await _wait_rev(6)
        tc = asyncio.create_task(
            plugin.agent_tool(
                action="send",
                thread_id=tid,
                source_msg_id="m-5",
                message="普通成员紧随",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        await _wait_rev(7)
        release_rev1.set()
        await asyncio.gather(ta, tb, tc)
        assert len(step_batches) == 3
    finally:
        release_rev1.set()
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_coalesce_window_ms_merges_burst_group_messages_into_first_step(tmp_path):
    plugin = await _loaded_plugin(
        tmp_path,
        enabled_chats=["qq:group:100246809"],
    )
    plugin.profiles.clone("default", "bursty")
    _patch_profile_toml(
        plugin.profiles,
        "bursty",
        session_mode="persistent",
        coalesce_window_ms=140,
    )
    seen_calls: list[list[str]] = []

    async def capability(name, **arguments):
        if name == "llm.generate_with_tools":
            seen_calls.append(
                [
                    m["content"]
                    for m in arguments["prompt"]
                    if m.get("role") == "user" and not str(m.get("content") or "").startswith(("<memory_context>", "<thread_history>"))
                ]
            )
            return {"response": "一并答复", "tool_calls": []}
        raise RuntimeError(name)

    plugin.ctx.call_capability = capability
    try:
        for mid, uid, uname in (("m-b1", "20001", "甲"), ("m-b2", "20002", "乙")):
            plugin.messages.record(
                Message.from_event(
                    {
                        "message_id": mid,
                        "session_id": "qq:group:100246809",
                        "platform": "qq",
                        "message_info": {
                            "user_info": {"user_id": uid, "user_cardname": uname},
                            "group_info": {"group_id": "100246809"},
                        },
                    }
                )
            )
        t1 = asyncio.create_task(
            plugin.agent_tool(
                action="start",
                profile="bursty",
                source_msg_id="m-b1",
                message="第一句",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        await asyncio.sleep(0.03)
        t2 = asyncio.create_task(
            plugin.agent_tool(
                action="start",
                profile="bursty",
                source_msg_id="m-b2",
                message="第二句",
                wait_seconds=5,
                chat_id="qq:group:100246809",
                platform="qq",
            )
        )
        r1, r2 = await asyncio.gather(t1, t2)
        assert len(seen_calls) == 1
        assert "甲" in seen_calls[0][0] and "第一句" in seen_calls[0][0]
        assert "乙" in seen_calls[0][1] and "第二句" in seen_calls[0][1]
        assert r1.get("coalesced_into") == 2 and r2["result"]["text"] == "一并答复"
    finally:
        await plugin.on_unload()


# ---------------------------------------------------------------------------
# M2: project/STATE.md Live Note, Stable KV Prefix Preservation & Bridge Fidelity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_state_note_flows_to_planner_and_console_preserving_stable_prefix(tmp_path):
    plugin = await _loaded_plugin(
        tmp_path,
        enabled_chats=["qq:group:100246809"],
    )
    plugin.vault.set("state_secret", "super-secret-state-token-99")
    plugin.profiles.clone("default", "tracker")
    _patch_profile_toml(
        plugin.profiles,
        "tracker",
        session_mode="persistent",
        permission_mode="autonomous",
    )

    step_n = 0

    async def capability(name, **arguments):
        nonlocal step_n
        if name == "llm.generate_with_tools":
            step_n += 1
            if step_n == 1:
                note = "状态：第3轮接龙已收4/6人；token=super-secret-state-token-99。" + ("A" * 700)
                return {
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "c_w1",
                            "function": {
                                "name": "workspace_file",
                                "arguments": json.dumps(
                                    {"action": "write", "path": "project/STATE.md", "content": note},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                }
            if step_n == 2:
                return {"response": "已登记状态便签", "tool_calls": []}
            if step_n == 3:
                return {
                    "response": "",
                    "tool_calls": [
                        {
                            "id": "c_w2",
                            "function": {
                                "name": "workspace_file",
                                "arguments": json.dumps(
                                    {"action": "write", "path": "project/STATE.md", "content": ""},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                }
            return {"response": "已清空便签", "tool_calls": []}
        raise RuntimeError(name)

    plugin.ctx.call_capability = capability
    try:
        async def _planner_block():
            res = await plugin.before_planner(
                session_id="qq:group:100246809",
                tool_definitions=[
                    {"type": "function", "function": {"name": "reply"}},
                    {"type": "function", "function": {"name": "maisya_agent"}},
                ],
                items=[],
            )
            return res["modified_kwargs"]["items"][-1]["parts"][0]["text"]

        before_txt = await _planner_block()
        stable_before = before_txt.split("</maisya_profiles>")[0] + "</maisya_profiles>"

        started = await plugin.agent_tool(
            action="start",
            profile="tracker",
            message="更新一次当前进度",
            wait_seconds=5,
            chat_id="qq:group:100246809",
            platform="qq",
        )
        assert started["success"]
        tid = started["thread_id"]

        searched = plugin.threads.search("", "qq:group:100246809", limit=5)
        hit = next(x for x in searched if x["thread_id"] == tid)
        assert "第3轮接龙已收4/6人" in hit["state_note"]
        assert "super-secret-state-token-99" not in hit["state_note"]
        assert len(hit["state_note"]) <= 600

        after_txt = await _planner_block()
        stable_after = after_txt.split("</maisya_profiles>")[0] + "</maisya_profiles>"
        assert stable_before == stable_after
        assert "第3轮接龙已收4/6人" in after_txt
        assert "super-secret-state-token-99" not in after_txt

        snap = await plugin.console_query(thread_id=tid, chat_id="qq:group:100246809")
        assert "第3轮接龙已收4/6人" in snap["selected"]["state_note"]

        # Clear STATE.md clears state_note
        await plugin.agent_tool(
            action="send",
            thread_id=tid,
            message="清空当前状态便签",
            wait_seconds=5,
            chat_id="qq:group:100246809",
            platform="qq",
        )
        searched2 = plugin.threads.search("", "qq:group:100246809", limit=5)
        hit2 = next(x for x in searched2 if x["thread_id"] == tid)
        assert "state_note" not in hit2

        # Bridge _facts contains numeric/attribution fidelity instruction
        bridge = Bridge(plugin.threads)
        facts_str = bridge._facts([{"thread_id": tid, "revision": 1, "profile": "tracker", "source_msg_id": "", "result": {"text": "总计 42 元"}}])
        assert "具体数字" in facts_str or "保留" in facts_str
    finally:
        await plugin.on_unload()


# ---------------------------------------------------------------------------
# M3: Profile Companion Files, references/ Anti-Leak Lock & Export Stripping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_companions_readonly_references_leak_lock_and_export_stripping(tmp_path):
    plugin = await _loaded_plugin(
        tmp_path,
        enabled_chats=["qq:group:100246809"],
        admins=["qq:10001"],
    )
    plugin.vault.set("companion_secret", "vault-companion-secret-88")
    try:
        # Clone a readonly profile (no workspace toolset!) and mount a reference file
        plugin.profiles.clone("scout", "ref_reader")
        _patch_profile_toml(
            plugin.profiles,
            "ref_reader",
            toolsets=["context", "web"],
            skills=["shared_helper"],
            access="everyone",
            permission_mode="autonomous",
        )
        plugin.memory.create_skill("shared_helper", title="共享辅助", description="带题库底稿", content="# 共享辅助\n按需查阅底稿。\n")
        skill_ref_body = "这是技能内部标准答案与禁止直接发送给用户的完整参考底稿正文内容，包含核心秘密答案四十二号与全部细节描述。" * 3
        plugin.memory.write_skill_file("shared_helper", "references/answer_key.md", skill_ref_body)

        prof_ref_body = "这是角色内部模组底牌真相：幕后黑手就是酒馆老板本人，地下室密道口就在壁炉正后方第三块砖处。" * 3
        plugin.memory.write_profile_file("ref_reader", "references/secret_plot.md", prof_ref_body)
        plugin.memory.write_profile_file("ref_reader", "templates/card.html", "<div>模板</div>")

        with pytest.raises(ValueError, match="目录|references"):
            plugin.memory.write_profile_file("ref_reader", "invalid/bad.txt", "nope")
        with pytest.raises(ValueError, match="穿越|越界|路径"):
            plugin.memory.read_profile_file("ref_reader", "../profile.toml")
        with pytest.raises(ValueError, match="凭据"):
            plugin.memory.write_profile_file("ref_reader", "references/leak.txt", "vault-companion-secret-88")

        files = plugin.memory.profile_files("ref_reader")
        assert {f["path"] for f in files} == {"references/secret_plot.md", "templates/card.html"}

        # Check methods() boundary prompt & in/profile listing
        prof = plugin.profiles.get("ref_reader")
        methods_txt = plugin.memory.methods(prof)
        assert "in/profile/" in methods_txt
        assert "references/" in methods_txt and ("禁止" in methods_txt or "外发" in methods_txt)

        # Readonly profile without workspace toolset can list and read in/ via workspace_file, but cannot write
        captured_tools = []

        async def capability(name, **arguments):
            if name == "llm.generate_with_tools":
                captured_tools.append([t["function"]["name"] for t in arguments.get("tools") or []])
                if len(captured_tools) == 1:
                    return {
                        "response": "",
                        "tool_calls": [
                            {
                                "id": "ro_read",
                                "function": {
                                    "name": "workspace_file",
                                    "arguments": json.dumps({"action": "read", "path": "in/profile/references/secret_plot.md"}),
                                },
                            }
                        ],
                    }
                return {"response": "已查阅内部资料并作答", "tool_calls": []}
            raise RuntimeError(name)

        plugin.ctx.call_capability = capability
        row_ro = await plugin.agent_tool(
            action="start",
            profile="ref_reader",
            message="查一下内部设定",
            wait_seconds=5,
            chat_id="qq:group:100246809",
            platform="qq",
        )
        assert row_ro["success"], row_ro
        assert "workspace_file" in captured_tools[0]

        t_ro = plugin.threads.get(row_ro["thread_id"], "qq:group:100246809")
        with pytest.raises(PermissionError, match="未启用工作区|只读"):
            await plugin.runtime.call(
                "workspace_file",
                {"action": "write", "path": "project/hack.txt", "content": "x"},
                t_ro,
            )

        # Workspace-enabled profile: cannot overwrite in/, cannot attach references/ verbatim or >200 char excerpt
        plugin.profiles.clone("default", "host_ref")
        _patch_profile_toml(
            plugin.profiles,
            "host_ref",
            toolsets=["context", "workspace", "exec"],
            skills=["shared_helper"],
            access="everyone",
            permission_mode="autonomous",
        )
        plugin.memory.write_profile_file("host_ref", "references/secret_plot.md", prof_ref_body)
        plugin.memory.write_profile_file("host_ref", "templates/public_tpl.html", "<h1>公开海报</h1>")

        t_ws = plugin.threads.create(plugin.profiles.get("host_ref"), "工作区测试", chat_id="qq:group:100246809", platform="qq", user_id="20001")
        with pytest.raises(PermissionError, match="只读"):
            await plugin.runtime.call(
                "workspace_file",
                {"action": "write", "path": "in/profile/references/secret_plot.md", "content": "tamper"},
                t_ws,
            )
        await plugin.runtime.call(
            "workspace_file",
            {"action": "write", "path": "out/leak_prof.md", "content": prof_ref_body},
            t_ws,
        )
        with pytest.raises(ValueError, match="references|内部参考资料|禁止"):
            await plugin.runtime.call(
                "result_attach",
                {"paths": ["out/leak_prof.md"]},
                t_ws,
            )
        t_ws2 = plugin.threads.create(plugin.profiles.get("host_ref"), "技能泄露检查", chat_id="qq:group:100246809", platform="qq", user_id="20001")
        await plugin.runtime.call(
            "workspace_file",
            {"action": "write", "path": "out/leak_skill.md", "content": "前置说明\n" + skill_ref_body[:240]},
            t_ws2,
        )
        with pytest.raises(ValueError, match="references|内部参考资料|禁止"):
            await plugin.runtime.call(
                "result_attach",
                {"paths": ["out/leak_skill.md"]},
                t_ws2,
            )
        await plugin.runtime.call(
            "workspace_file",
            {"action": "write", "path": "out/poster.html", "content": "<h1>基于公开模板制作的成果</h1>"},
            t_ws,
        )
        ok_att = await plugin.runtime.call("result_attach", {"paths": ["out/poster.html"]}, t_ws)
        assert ok_att["attachments"]

        # Package export: non-admin strips references/*; admin preserves & re-imports
        user_zip = export_package(plugin, "host_ref", kind="template", is_admin=False)["zip_bytes"]
        with zipfile.ZipFile(io.BytesIO(user_zip)) as zf:
            names = set(zf.namelist())
            assert not any("/references/" in n or n.startswith("profile_companions/references/") for n in names)
            assert "skills/shared_helper/SKILL.md" in names
            assert "profile_companions/templates/public_tpl.html" in names

        adm_zip = export_package(plugin, "host_ref", kind="template", is_admin=True)["zip_bytes"]
        with zipfile.ZipFile(io.BytesIO(adm_zip)) as zf:
            names = set(zf.namelist())
            assert "profile_companions/references/secret_plot.md" in names
            assert "skills/shared_helper/references/answer_key.md" in names

        imp = import_package_bundle(plugin, raw_bytes=adm_zip, target_profile_id="host_ref_copy", overwrite=True)
        assert imp["ok"]
        assert plugin.memory.read_profile_file("host_ref_copy", "references/secret_plot.md")["content"] == prof_ref_body

        # Admin shaping file op + management RPCs
        plugin.messages.record(
            Message.from_event(
                {
                    "message_id": "m-adm-f",
                    "session_id": "qq:private:10001",
                    "platform": "qq",
                    "message_info": {"user_info": {"user_id": "10001"}},
                }
            )
        )
        shaped = await plugin.shape_tool(
            action="file",
            op="write",
            document="profiles/host_ref/references/new_ref.md",
            content="新增内部设定",
            source_msg_id="m-adm-f",
            chat_id="qq:private:10001",
            platform="qq",
        )
        assert shaped["success"]
        assert plugin.memory.read_profile_file("host_ref", "references/new_ref.md")["content"] == "新增内部设定"

        # Management backend APIs
        mgmt = Management(plugin)
        saved_pf = await mgmt.write("save_profile_file", json.dumps({"target": "host_ref", "path": "references/ui.md", "content": "后台参考"}))
        assert "references/ui.md" in saved_pf["companion_files"]
        read_pf = await mgmt.read("profile_file", json.dumps({"target": "host_ref", "path": "references/ui.md"}))
        assert read_pf["content"] == "后台参考"
        detail = await mgmt.read("profile_detail", json.dumps({"profile_id": "host_ref"}))
        assert "references/ui.md" in detail["companion_files"] and detail["companion_count"] >= 3
        del_pf = await mgmt.write("delete_profile_file", json.dumps({"target": "host_ref", "path": "references/ui.md"}))
        assert "references/ui.md" not in del_pf["companion_files"]
    finally:
        await plugin.on_unload()


# ---------------------------------------------------------------------------
# M4: Containerless Deterministic calc Tool & Offline Trusted Scripts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_containerless_calc_and_trusted_scripts_for_non_admin(tmp_path):
    assert "calc" in TOOLSETS["exec"]
    plugin = await _loaded_plugin(
        tmp_path,
        enabled_chats=["qq:group:100246809"],
    )
    plugin.profiles.clone("default", "calc_role")
    _patch_profile_toml(
        plugin.profiles,
        "calc_role",
        toolsets=["context", "workspace", "exec"],
        execution_mode="host",
        permission_mode="autonomous",
        skills=["dice_kit"],
    )
    plugin.memory.create_skill("dice_kit", title="计算与随机工具", description="提供扩展离线脚本", content="# 脚本\n")
    plugin.memory.write_profile_file(
        "calc_role",
        "scripts/tally.py",
        "import sys\nprint('PROFILE_OK:' + ':'.join(sys.argv[1:]))\n",
    )
    plugin.memory.write_skill_file(
        "dice_kit",
        "scripts/net_test.py",
        "import socket, sys\nprint('MODE:' + (sys.argv[1] if len(sys.argv) > 1 else 'plain'))\nif 'net' in sys.argv:\n    socket.create_connection(('1.1.1.1', 80), timeout=1)\n",
    )
    # Unbound skill must NOT be executable
    plugin.memory.create_skill("other_kit", title="未绑定脚本", description="未授权", content="# 未绑定\n")
    plugin.memory.write_skill_file("other_kit", "scripts/hidden.py", "print('SHOULD_NOT_RUN')\n")

    try:
        # Docker disabled in default loaded() test harness
        assert not plugin.runtime.docker_available()
        prof = plugin.profiles.get("calc_role")
        assert "calc" in prof.tools

        # Simulate step tool filtering for non-admin without Docker: calc stays, exec_command only for trusted scripts
        filtered_tools: list[str] = []

        async def capability(name, **arguments):
            if name == "llm.generate_with_tools":
                filtered_tools[:] = [t["function"]["name"] for t in (arguments.get("tools") or [])]
                return {"response": "完成", "tool_calls": []}
            raise RuntimeError(name)

        plugin.ctx.call_capability = capability
        await plugin.agent_tool(
            action="start",
            profile="calc_role",
            message="先探探可用工具",
            wait_seconds=5,
            chat_id="qq:group:100246809",
            platform="qq",
        )
        assert "calc" in filtered_tools
        assert "sandbox_exec" not in filtered_tools

        t_row = plugin.threads.create(prof, "计算测试", chat_id="qq:group:100246809", platform="qq", user_id="20001")

        # 1. Exact Decimal + variables + precision
        r_math = await plugin.runtime.call(
            "calc",
            {"expression": "(a + b) * 3 / 2", "variables": {"a": 0.1, "b": 0.2}, "precision": 2},
            t_row,
        )
        assert r_math["result_str"] == "0.45" and r_math["result"] == pytest.approx(0.45)

        # 2. Array statistics
        r_stats = await plugin.runtime.call("calc", {"numbers": [10, 20, 30, 40]}, t_row)
        assert r_stats["stats"]["count"] == 4
        assert r_stats["stats"]["sum"] == 100
        assert r_stats["stats"]["mean"] == 25
        assert r_stats["stats"]["median"] == 25

        # 3. True random roll expressions
        r_roll = await plugin.runtime.call("calc", {"roll": ["2d20kh1+5", "4d6dl1"]}, t_row)
        rolls = r_roll["rolls"]
        assert len(rolls) == 2
        assert rolls[0]["modifier"] == 5 and len(rolls[0]["rolls"]) == 2 and len(rolls[0]["kept"]) == 1
        assert rolls[0]["total"] == max(rolls[0]["rolls"]) + 5
        assert len(rolls[1]["rolls"]) == 4 and len(rolls[1]["kept"]) == 3
        assert rolls[1]["total"] == sum(rolls[1]["kept"])

        # 4. True random draw (pick / shuffle / weighted)
        r_draw = await plugin.runtime.call("calc", {"draw": {"mode": "pick", "items": ["红", "蓝", "绿"], "count": 2}}, t_row)
        assert len(r_draw["draw"]["picked"]) == 2 and len(set(r_draw["draw"]["picked"])) == 2

        # 5. Malicious AST blocked
        for bad_expr in ("__import__('os').system('id')", "(1).__class__", "10**1000000"):
            with pytest.raises(ValueError):
                await plugin.runtime.call("calc", {"expression": bad_expr}, t_row)

        # 6. Trusted profile & bound-skill scripts run for non-admin without Docker, network blocked
        res_prof = await plugin.runtime.call(
            "exec_command",
            {"command": "python3 in/profile/scripts/tally.py arg1 arg2"},
            t_row,
        )
        assert res_prof["status"] == "ok" and "PROFILE_OK:arg1:arg2" in res_prof["stdout"]

        res_skill = await plugin.runtime.call(
            "exec_command",
            {"command": "python3 in/skills/dice_kit/scripts/net_test.py safe"},
            t_row,
        )
        assert res_skill["status"] == "ok" and "MODE:safe" in res_skill["stdout"]

        res_net = await plugin.runtime.call(
            "exec_command",
            {"command": "python3 in/skills/dice_kit/scripts/net_test.py net"},
            t_row,
        )
        assert res_net["status"] == "error" and "PermissionError" in res_net["stderr"]

        # Unbound skill script, inline -c, or shell metacharacters are rejected
        for bad_cmd in (
            "python3 in/skills/other_kit/scripts/hidden.py",
            "python3 -c \"print(1)\"",
            "python3 in/profile/scripts/tally.py; id",
        ):
            with pytest.raises(PermissionError):
                await plugin.runtime.call("exec_command", {"command": bad_cmd}, t_row)
    finally:
        await plugin.on_unload()
