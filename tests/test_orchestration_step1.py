import asyncio
import base64
import html
import json
import re
import time
from pathlib import Path

import pytest
from test_foundation import event, loaded

from maisya.trust import Message

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _seed_maker_profile(profiles, *, name="maker"):
    if name not in {p.id for p in profiles.list()}:
        profiles.clone("default", name)
    (profiles.path / name / "profile.toml").write_text(
        'title = "创作工坊"\n'
        'enabled = true\n'
        'bound = true\n'
        'access = "everyone"\n'
        'environment = "sandbox"\n'
        'execution_mode = "sandbox"\n'
        'permission_mode = "autonomous"\n'
        'network = "public"\n'
        'skills = ["research-report"]\n'
        'tools = ["person_read", "recall_read", "workspace_file", "html_render", "render_report", "result_attach", "sandbox_exec"]\n',
        encoding="utf-8",
    )
    return profiles.get(name)


def test_presets_complete_builtin_tools_and_silent_out_of_box_heartbeat(tmp_path):
    root = Path(__file__).resolve().parents[1]
    default_raw = (root / "presets/profiles/default/profile.toml").read_text("utf-8")
    assert not re.search(r"(?m)^\s*skills\s*=", default_raw), (
        "default/profile.toml must not predefine skills = []"
    )
    assert (root / "presets/skills/research-report/SKILL.md").is_file()
    assert (root / "presets/skills/news-digest/SKILL.md").is_file()

    from maisya.abilities import Profiles
    from maisya.memory import MemoryLayers
    from maisya.threads import ThreadStore
    from maisya.vault import Vault

    profiles = Profiles(tmp_path / "profiles")
    assert [p.id for p in profiles.list()] == ["default", "digest", "scout"]
    default_prof = profiles.get("default")
    assert "person_read" in default_prof.tools
    assert "recall_read" in default_prof.tools
    assert "html_render" in default_prof.tools
    assert "workspace_file" in default_prof.tools
    assert "result_attach" in default_prof.tools
    assert "render_report" in default_prof.tools
    assert not default_prof.requires_container

    memory = MemoryLayers(
        tmp_path, ThreadStore(tmp_path / "state.db"), profiles, Vault(tmp_path / "vault")
    )
    hb = memory.read("heartbeat")["content"]
    temperament, plan = MemoryLayers.heartbeat_parts(hb)
    assert temperament
    assert plan == ""
    assert memory.read("journal")["content"] == ""


@pytest.mark.asyncio
async def test_containerless_maker_light_workspace_runs_and_auto_restores_sandbox(tmp_path):
    plugin = await loaded(tmp_path)
    seen_tool_sets = []
    calls = 0

    async def capability(name, **arguments):
        nonlocal calls
        if name == "render.html2png":
            return {"image_base64": base64.b64encode(PNG_1X1).decode("ascii")}
        assert name == "llm.generate_with_tools"
        calls += 1
        tool_names = [
            t.get("name") or t.get("function", {}).get("name")
            for t in (arguments.get("tools") or [])
        ]
        seen_tool_sets.append((tool_names, arguments["prompt"][0]["content"]))
        if calls == 1:
            return {
                "tool_calls": [
                    {
                        "id": "wf-1",
                        "function": {
                            "name": "workspace_file",
                            "arguments": {
                                "action": "write",
                                "path": "out/card.html",
                                "content": "<h1>Light Card</h1>",
                            },
                        },
                    }
                ]
            }
        if calls == 2:
            return {
                "tool_calls": [
                    {
                        "id": "hr-1",
                        "function": {
                            "name": "html_render",
                            "arguments": {
                                "html_path": "out/card.html",
                                "output_path": "out/card.png",
                            },
                        },
                    }
                ]
            }
        if calls == 3:
            return {
                "tool_calls": [
                    {
                        "id": "ra-1",
                        "function": {
                            "name": "result_attach",
                            "arguments": {"paths": ["out/card.png"]},
                        },
                    }
                ]
            }
        return {"response": "无容器轻量卡片已渲染完成。", "tool_calls": []}

    plugin.ctx.call_capability = capability
    try:
        assert plugin.runtime.sandbox.ready is False
        maker = _seed_maker_profile(plugin.profiles)
        assert plugin.runtime.effective_mode(maker) == "light_workspace"

        res = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="light_card",
            message="制作一张轻量信息卡",
            chat_id="chat",
            platform="qq",
        )
        assert res["success"] and res["state"] == "idle", res
        assert "error" not in res["result"]
        assert res["result"]["attachments"][0]["name"] == "card.png"
        first_tools, first_sys = seen_tool_sets[0]
        assert "sandbox_exec" not in first_tools
        assert "workspace_file" in first_tools
        assert "html_render" in first_tools
        assert "轻量工作区模式" in first_sys

        # Flip container ready -> sandbox_exec immediately restored without profile edits
        plugin.runtime.sandbox_settings.enabled = True
        plugin.runtime.sandbox.ready = True
        assert plugin.runtime.effective_mode(maker) == "sandbox"
        seen_tool_sets.clear()

        async def sandbox_cap(name, **arguments):
            assert name == "llm.generate_with_tools"
            tool_names = [
                t.get("name") or t.get("function", {}).get("name")
                for t in (arguments.get("tools") or [])
            ]
            seen_tool_sets.append(tool_names)
            return {"response": "容器已恢复。", "tool_calls": []}

        plugin.ctx.call_capability = sandbox_cap
        res2 = await plugin.agent_tool(
            action="start",
            profile="maker",
            message="检查完整沙箱工具",
            chat_id="chat",
            platform="qq",
        )
        assert res2["success"]
        assert "sandbox_exec" in seen_tool_sets[0]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_before_planner_compact_idle_and_enriched_profiles_and_threads(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        _seed_maker_profile(plugin.profiles)
        plugin.messages.record(Message.from_event(event("hi", user="Alice", platform="qq")))
        plugin.vault.set("leak_check", "top-secret-token-998877")
        await plugin.manage_write(
            action="bind_skill",
            payload=json.dumps({"skill": "source-checking", "profile": "default", "bound": True}),
        )

        # 1. Idle session: compact prompt, enriched <maisya_profiles>, no <maisya_threads> noise
        idle_hook = await plugin.before_planner(
            session_id="chat",
            tool_definitions=[{"name": "reply"}, {"name": "maisya_agent"}, {"name": "maisya_shape"}],
            items=[],
        )
        idle_prompt = idle_hook["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "<maisya_threads>" not in idle_prompt
        assert "<maisya_returns>" not in idle_prompt
        profiles_match = re.search(
            r"<maisya_profiles>(.*?)</maisya_profiles>", idle_prompt, flags=re.S
        )
        assert profiles_match is not None
        profiles_payload = json.loads(html.unescape(profiles_match.group(1)))
        by_id = {p["id"]: p for p in profiles_payload}
        assert "default" in by_id and "maker" in by_id
        assert by_id["maker"]["mode"] == "light_workspace"
        assert "html_render" in by_id["maker"]["capabilities"]
        assert any(
            s["id"] == "source-checking" and s["summary"]
            for s in by_id["default"]["skills"]
        )

        # 2. Active thread with workspace & step event: <maisya_threads> exposes title, workspace, redacted latest_step
        maker = plugin.profiles.get("maker")
        row = plugin.threads.create(
            maker,
            "整理季度图表 top-secret-token-998877",
            chat_id="chat",
            platform="qq",
            user_id="qq:Alice",
            workspace="q_charts",
        )
        plugin.threads.enqueue(
            row["thread_id"], "整理季度图表 top-secret-token-998877", "chat"
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                row["thread_id"],
                "tools",
                1,
                {
                    "calls": [
                        {
                            "function": {
                                "name": "workspace_file",
                                "arguments": json.dumps(
                                    {
                                        "action": "write",
                                        "path": "out/report.md top-secret-token-998877",
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        }
                    ]
                },
            )
        active_hook = await plugin.before_planner(
            session_id="chat",
            tool_definitions=[{"name": "reply"}, {"name": "maisya_agent"}],
            items=[],
        )
        active_prompt = active_hook["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "top-secret-token-998877" not in active_prompt
        t_match = re.search(
            r"<maisya_threads>(.*?)</maisya_threads>", active_prompt, flags=re.S
        )
        assert t_match is not None
        threads_payload = json.loads(html.unescape(t_match.group(1)))
        t_entry = next(item for item in threads_payload if item["thread_id"] == row["thread_id"])
        assert t_entry["workspace"] == "q_charts"
        assert "整理季度图表" in t_entry["title"]
        assert "workspace_file" in t_entry.get("latest_step", "")
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_parallel_wait_zero_and_from_threads_handoff_results_and_out_files(tmp_path):
    plugin = await loaded(tmp_path)
    plugin.vault.set("upstream_key", "secret-upstream-value-12345")
    gate = asyncio.Event()

    async def slow_capability(name, **arguments):
        if name == "llm.generate_with_tools":
            await gate.wait()
            msgs_txt = json.dumps(arguments.get("prompt") or [], ensure_ascii=False)
            if "分支A" in msgs_txt:
                return {
                    "response": "分支A结论 https://example.org/a secret-upstream-value-12345",
                    "tool_calls": [],
                }
            return {"response": "分支B结论 https://example.org/b", "tool_calls": []}
        return {}

    plugin.ctx.call_capability = slow_capability
    try:
        maker = _seed_maker_profile(plugin.profiles)
        # Launch two parallel threads with wait_seconds=0
        r_a = await plugin.agent_tool(
            action="start",
            profile="default",
            message="查证分支A",
            wait_seconds=0,
            chat_id="chat",
            platform="qq",
        )
        r_b = await plugin.agent_tool(
            action="start",
            profile="default",
            message="查证分支B",
            wait_seconds=0,
            chat_id="chat",
            platform="qq",
        )
        assert r_a["state"] == "active" and r_b["state"] == "active"
        gate.set()
        for tid in (r_a["thread_id"], r_b["thread_id"]):
            for _ in range(50):
                if plugin.threads.get(tid, "chat")["state"] != "active":
                    break
                await asyncio.sleep(0.02)

        # Also prepare an upstream maker workspace with both unattached out/data.json and attached out/chart.png
        up_maker = plugin.threads.create(
            maker, "生成上游数据", chat_id="chat", platform="qq", user_id="Alice", workspace="up_ws"
        )
        with plugin.runtime.files.activity(up_maker) as work:
            work.write("out/data.json", '{"score": 98}')
            (work.path / "out/chart.png").write_bytes(PNG_1X1)
            work.commit()
            attached = plugin.runtime.files.attach(up_maker, work, ["out/chart.png"])
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='idle', result=?, completed_revision=1 WHERE thread_id=?",
                (
                    json.dumps(
                        {"text": "上游制图已完成", "attachments": attached},
                        ensure_ascii=False,
                    ),
                    up_maker["thread_id"],
                ),
            )

        captured_downstream = {}

        async def downstream_cap_stepped(name, **arguments):
            if "step1" not in captured_downstream:
                captured_downstream["step1"] = arguments["prompt"]
                return {
                    "tool_calls": [
                        {
                            "id": "ls-1",
                            "function": {
                                "name": "workspace_file",
                                "arguments": {"action": "list"},
                            },
                        }
                    ]
                }
            captured_downstream["step2"] = arguments["prompt"]
            return {"response": "下游接力完成。", "tool_calls": []}

        plugin.ctx.call_capability = downstream_cap_stepped
        down = await plugin.agent_tool(
            action="start",
            profile="maker",
            workspace="down_ws",
            from_threads=[r_a["thread_id"], up_maker["thread_id"]],
            message="结合上游查证与数据制作海报",
            chat_id="chat",
            platform="qq",
        )
        assert down["success"] and down["state"] == "idle", down
        assert "error" not in down["result"]
        step1_txt = json.dumps(captured_downstream["step1"], ensure_ascii=False)
        step2_txt = json.dumps(captured_downstream["step2"], ensure_ascii=False)
        assert "<previous_result" in step1_txt
        assert "https://example.org/a" in step1_txt
        assert "secret-upstream-value-12345" not in step1_txt

        # Verify in/ contains upstream result markdown, unattached out/data.json, and attached chart.png
        assert f"in/previous-{r_a['thread_id']}-result.md" in step2_txt
        assert f"in/previous-{up_maker['thread_id']}-out/data.json" in step2_txt
        assert f"in/previous-{up_maker['thread_id']}-0-chart.png" in step2_txt

        # Re-importing on send overwrites cleanly without FileExistsError
        captured_downstream.clear()
        res_send = await plugin.agent_tool(
            action="send",
            thread_id=down["thread_id"],
            from_threads=[r_a["thread_id"], up_maker["thread_id"]],
            message="再刷新一次上游产物",
            chat_id="chat",
            platform="qq",
        )
        assert res_send["success"] and "error" not in res_send["result"], res_send

        # Cross-chat isolation check
        other_row = plugin.threads.create(
            plugin.profiles.get("default"),
            "其他群任务",
            chat_id="other_chat",
            platform="qq",
            user_id="Bob",
        )
        cross = await plugin.agent_tool(
            action="start",
            profile="maker",
            from_threads=[other_row["thread_id"]],
            message="越权引用",
            chat_id="chat",
            platform="qq",
        )
        assert not cross["success"] and "不属于当前会话" in cross["error"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_share_forwards_sanitized_planner_intent_to_replyer(tmp_path):
    plugin = await loaded(tmp_path)
    plugin.vault.set("tok", "secret-share-tok-777")
    row = plugin.threads.create(
        plugin.profiles.get("default"),
        "查证更新日志",
        chat_id="chat",
        platform="qq",
        user_id="Alice",
    )
    with plugin.threads.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='idle', result=?, revision=1, completed_revision=1 WHERE thread_id=?",
            (
                json.dumps(
                    {"text": "官方已发布 2.0 版本 https://example.org/v2"},
                    ensure_ascii=False,
                ),
                row["thread_id"],
            ),
        )
    plugin.bridge.stage(plugin.threads.get(row["thread_id"], "chat"), "chat")

    captured = {}

    async def capability(name, **arguments):
        if name == "maisaka.reply.generate":
            captured["reply_args"] = arguments
            return "2.0 已经发布啦，详情见 https://example.org/v2"
        if name == "send.hybrid":
            captured["sent_text"] = arguments.get("processed_plain_text")
            return {"sent": True, "message_id": "m_share_1"}
        return {"success": True}

    plugin.ctx.call_capability = capability
    try:
        out = await plugin.agent_tool(
            action="share",
            thread_id=row["thread_id"],
            message="顺着刚才的话题轻松告诉大家，别带 secret-share-tok-777 或 https://unverified.invalid/x",
            chat_id="chat",
            platform="qq",
        )
        assert out["success"], out
        ref = captured["reply_args"]["reference"]
        assert "<planner_intent>" in ref
        assert "顺着刚才的话题轻松告诉大家" in ref
        assert "secret-share-tok-777" not in ref
        assert "unverified.invalid" not in ref
        assert "https://example.org/v2" in ref
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_visible_maisya_later_workflow_wake_wording_and_link_staging(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        components = {item["name"]: item for item in plugin.get_components()}
        assert components["maisya_later"]["metadata"]["visibility"] == "visible"

        # Create a restricted admin-only role to verify non-admin cannot schedule it via maisya_later
        plugin.profiles.clone("default", "ops_audit")
        t_cur = plugin.memory.read("profile", target="ops_audit", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "管理员审计"\nenvironment = "readonly"\naccess = "admins"\ntools = ["web_search"]\n',
            target="ops_audit",
            document="profile.toml",
            expected_version=t_cur["version"],
            allow_custom=True,
        )

        bob_ev = event("帮我每小时跑管理员审计", user="Bob")
        bob_ev["message_id"] = "msg_bob_1"
        plugin.messages.record(Message.from_event(bob_ev))
        denied = await plugin.later_tool(
            action="schedule",
            reason="每小时审计",
            in_seconds=60,
            every_seconds=3600,
            profile="ops_audit",
            prompt="查热点",
            chat_id="chat",
            platform="qq",
            source_msg_id="msg_bob_1",
        )
        assert not denied["success"] and "管理员" in denied["error"]

        # Schedule natural-language recurring workflow on default -> promoted to source="workflow"
        alice_ev = event("帮我们每天早上巡检一次开源发布", user="Alice")
        alice_ev["message_id"] = "msg_alice_1"
        plugin.messages.record(Message.from_event(alice_ev))
        ok = await plugin.later_tool(
            action="schedule",
            reason="每日开源巡检",
            in_seconds=60,
            every_seconds=3600,
            profile="default",
            prompt="汇总今日开源新版本",
            chat_id="chat",
            platform="qq",
            source_msg_id="msg_alice_1",
        )
        assert ok["success"], ok
        scheduled = plugin.intents.list("chat:chat")
        wf_intent = next(i for i in scheduled if i["intent_id"] == ok["intent_id"])
        assert wf_intent["source"] == "workflow"

        # Run Pulse: scout returns text + verified link (no binary attachments)
        wakes = []
        shared_texts = []

        async def capability(name, **arguments):
            if name == "llm.generate_with_tools":
                return {
                    "response": "今日重点发布：ProjectX v1.4 https://example.org/projectx-1.4",
                    "tool_calls": [],
                }
            if name == "maisaka.proactive.trigger":
                wakes.append(arguments)
                return {"success": True}
            if name == "maisaka.reply.generate":
                return "早安，今天 ProjectX 发了 1.4 版本！"
            if name == "send.hybrid":
                shared_texts.append(arguments.get("processed_plain_text", ""))
                return {"sent": True, "message_id": "m_wf_1"}
            return {"success": True}

        plugin.ctx.call_capability = capability
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE intents SET due_at=0 WHERE intent_id=?", (ok["intent_id"],)
            )
        now_box = [time.time()]
        plugin.intents.clock = lambda: now_box[0]
        await plugin.pulse.tick()
        await plugin.engine.drain()
        now_box[0] += 3
        await plugin.pulse.tick()

        assert len(wakes) == 1
        wake_intent = wakes[0]["intent"]
        assert "<maisya_workflow>" in wake_intent
        assert "<maisya_reminder>" not in wake_intent
        assert "<maisya_thread_result>" in wake_intent
        assert "<maisya_delivery_hint>" in wake_intent
        assert "https://example.org/projectx-1.4" in wake_intent

        # Verify bridge.stage was called even without attachments so share works immediately!
        wf_threads = plugin.threads.search("ProjectX", "chat", scope="chat")
        assert wf_threads
        wf_tid = wf_threads[0]["thread_id"]
        assert wf_tid in plugin.bridge._manual.get("chat", {})
        share_res = await plugin.agent_tool(
            action="share",
            thread_id=wf_tid,
            message="把早间巡检的发布消息自然发到群里",
            chat_id="chat",
            platform="qq",
        )
        assert share_res["success"], share_res
        assert any("https://example.org/projectx-1.4" in t for t in shared_texts)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_strict_sandbox_role_never_downgrades_and_direct_sandbox_exec_rejected(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        assert plugin.runtime.sandbox.ready is False
        alice_ev = event("直测命令", user="Alice")
        alice_ev["message_id"] = "m-admin-step1"
        plugin.messages.record(Message.from_event(alice_ev))
        # 1. Direct sandbox_exec call on light_workspace maker is explicitly rejected
        maker = _seed_maker_profile(plugin.profiles)
        maker_row = plugin.threads.create(
            maker,
            "直接测命令",
            chat_id="chat",
            platform="qq",
            user_id="Alice",
            grant={"admin": True, "source_msg_id": "m-admin-step1"},
        )
        with pytest.raises(RuntimeError, match="轻量工作区|沙箱"):
            await plugin.runtime.call(
                "sandbox_exec",
                {"command": "echo hi"},
                {**maker_row, "_tool_call_id": "c1"},
            )

        # 2. Strict container-only sandbox profile (with secrets / restricted network) NEVER downgrades
        plugin.vault.set("ssh_prod", "prod-secret-value-99")
        plugin.profiles.clone("maker", "strict_ops")
        t_cur = plugin.memory.read("profile", target="strict_ops", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "受控运维"\n'
            'environment = "sandbox"\n'
            'access = "admins"\n'
            'network = "restricted"\n'
            'secrets = ["ssh_prod"]\n'
            'tools = ["sandbox_exec", "workspace_file"]\n',
            target="strict_ops",
            document="profile.toml",
            expected_version=t_cur["version"],
            allow_custom=True,
        )
        strict_prof = plugin.profiles.get("strict_ops")
        assert strict_prof.requires_container is True
        assert plugin.runtime.effective_mode(strict_prof) == "sandbox"
        with pytest.raises(RuntimeError, match="沙箱"):
            plugin.runtime.available(strict_prof)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_active_thread_live_rebind_guard_later_edge_cases_and_dedup(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        # 1. Active thread snapshot must respect live.bound=False and readonly->strict_sandbox upgrades!
        plugin.profiles.clone("default", "live_guard")
        snap_prof = plugin.profiles.get("live_guard")
        t_cur = plugin.memory.read("profile", target="live_guard", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "待重绑角色"\n'
            'environment = "readonly"\n'
            'access = "admins"\n'
            'bound = false\n'
            'secrets = ["unregistered_secret"]\n'
            'tools = ["web_search"]\n',
            target="live_guard",
            document="profile.toml",
            expected_version=t_cur["version"],
            allow_custom=True,
        )
        with pytest.raises(PermissionError, match="绑定"):
            plugin.runtime.available(snap_prof)

        t_cur2 = plugin.memory.read("profile", target="live_guard", document="profile.toml")
        plugin.memory.write(
            "profile",
            'title = "升级为严格容器角色"\n'
            'environment = "sandbox"\n'
            'network = "restricted"\n'
            'access = "admins"\n'
            'bound = true\n'
            'tools = ["sandbox_exec"]\n',
            target="live_guard",
            document="profile.toml",
            expected_version=t_cur2["version"],
            allow_custom=True,
        )
        with pytest.raises(RuntimeError, match="沙箱"):
            plugin.runtime.available(snap_prof)

        # 2. maisya_later catches RuntimeError cleanly (no uncaught exception) & enforces allow_chats
        alice_ev = event("安排受控容器任务", user="Alice")
        alice_ev["message_id"] = "msg_alice_strict"
        plugin.messages.record(Message.from_event(alice_ev))
        unavail = await plugin.later_tool(
            action="schedule",
            reason="跑严格沙箱角色",
            in_seconds=60,
            profile="live_guard",
            prompt="执行检查",
            chat_id="chat",
            platform="qq",
            source_msg_id="msg_alice_strict",
        )
        assert unavail["success"] is False and "沙箱" in unavail["error"]

        plugin.profiles.clone("default", "other_chat_only")
        oc_cur = plugin.memory.read(
            "profile", target="other_chat_only", document="profile.toml"
        )
        plugin.memory.write(
            "profile",
            'title = "仅限别群"\nenvironment = "readonly"\naccess = "listed"\nallow_chats = ["other_group"]\ntools = ["web_search"]\n',
            target="other_chat_only",
            document="profile.toml",
            expected_version=oc_cur["version"],
            allow_custom=True,
        )
        blocked_chat = await plugin.later_tool(
            action="schedule",
            reason="越权安排别群角色",
            in_seconds=60,
            profile="other_chat_only",
            prompt="查资料",
            chat_id="chat",
            platform="qq",
            source_msg_id="msg_alice_strict",
        )
        assert blocked_chat["success"] is False

        # 3. Plain recurring reminder without profile or in_seconds defaults in_seconds & wakes as <maisya_reminder>, NOT <maisya_workflow>
        wakes = []

        async def capability(name, **arguments):
            if name == "maisaka.proactive.trigger":
                wakes.append(arguments)
                return {"success": True}
            return {"success": True}

        plugin.ctx.call_capability = capability
        rem = await plugin.later_tool(
            action="schedule",
            reason="每小时提醒大家起身活动",
            every_seconds=3600,
            chat_id="chat",
            platform="qq",
            source_msg_id="msg_alice_strict",
        )
        assert rem["success"], rem
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE intents SET due_at=0 WHERE intent_id=?", (rem["intent_id"],)
            )
        await plugin.pulse.tick()
        assert len(wakes) == 1
        assert "<maisya_reminder>" in wakes[0]["intent"]
        assert "<maisya_workflow>" not in wakes[0]["intent"]
        assert any(
            row["intent_id"] != rem["intent_id"]
            and row["payload"].get("every_seconds") == 3600
            for row in plugin.intents.list("chat:chat")
        )

        # 4. from_threads deduplicates declared attachment vs out/* and _import_bytes overwrites 0-byte file
        maker = _seed_maker_profile(plugin.profiles)
        up_row = plugin.threads.create(
            maker, "上游出图", chat_id="chat", platform="qq", user_id="qq:Alice"
        )
        with plugin.runtime.files.activity(up_row) as up_work:
            up_work.write_bytes("out/card.png", b"\x89PNG\r\n\x1a\ncarddata")
            up_work.write("out/extra.csv", "col1,col2\n1,2\n")
            up_work.commit()
            attached = plugin.runtime.files.attach(
                up_row, up_work, ["out/card.png"]
            )
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='idle', result=?, completed_revision=1 WHERE thread_id=?",
                (
                    json.dumps(
                        {
                            "text": "已生成海报与表格",
                            "attachments": attached,
                            "links": [],
                        },
                        ensure_ascii=False,
                    ),
                    up_row["thread_id"],
                ),
            )
        up_row = plugin.threads.get(up_row["thread_id"], "chat")

        down_row = plugin.threads.create(
            maker, "下游接力", chat_id="chat", platform="qq", user_id="qq:Alice"
        )
        imported = await plugin.runtime.prepare(
            down_row, {"from_threads": [up_row["thread_id"]]}
        )
        imported_paths = [item["path"] for item in imported]
        assert any(p.endswith("-card.png") for p in imported_paths)
        assert f"in/previous-{up_row['thread_id']}-out/extra.csv" in imported_paths
        assert f"in/previous-{up_row['thread_id']}-out/card.png" not in imported_paths

        down_work = await plugin.runtime._work(down_row)
        zero_target = down_work.path / "in/zero.txt"
        zero_target.write_bytes(b"")
        plugin.runtime._import_bytes(
            down_work, "in/zero.txt", b"non-empty", overwrite=True
        )
        assert zero_target.read_bytes() == b"non-empty"
        await plugin.runtime.end(down_row["thread_id"])
    finally:
        await plugin.on_unload()
