"""P0 architecture consolidation tests (B1 authorization lifecycle, B2 data ownership, B3 execution trust)."""

import asyncio
import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from maisya.abilities import Profile
from maisya.runner import ExecutionRequest, ExecutionResult, HostWorkspaceRunner
from maisya.shaping import execute_shape
from maisya.trust import Message, PermissionGrantStore
from maisya.workspaces import Workspaces
from test_foundation import event as make_ev
from test_foundation import loaded as base_loaded


async def _setup_plugin(tmp_path: Path, admins=None, *, host_admins=None, extra_cfg=None):
    host_list = list(admins or ["qq:10001"]) if host_admins is None else list(host_admins)
    plugin = await base_loaded(tmp_path, host_list)
    cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "enabled_chats": ["qq:group:100246809", "qq:group:100246810", "qq:private:10001"],
        "admins": list(admins or ["qq:10001"]),
        "permission_mode": "ask_risky",
        "sandbox": {"enabled": False},
        **(extra_cfg or {}),
    }
    await plugin.on_config_update("self", cfg, 1)
    return plugin


def _record_msg(plugin, *, msg_id: str, text: str, user: str, chat_id: str, group: bool, ts: float):
    ev = make_ev(text=text, user=user, platform="qq", group=group)
    ev["message_id"] = msg_id
    ev["session_id"] = chat_id
    ev["timestamp"] = str(ts)
    if group:
        ev["message_info"]["group_info"] = {"group_id": chat_id.split(":")[-1]}
    plugin.messages.record(Message.from_event(ev))
    return ev


# ──────────────────── B1: 授权生命周期与统一裁决 ────────────────────


@pytest.mark.asyncio
async def test_b1_removed_admin_or_empty_admins_pauses_scheduled_admin_and_allows_takeover(tmp_path):
    now = [1_794_500_000.0]
    plugin = await _setup_plugin(tmp_path, ["qq:10001", "qq:10002"], host_admins=["qq:10002"])
    plugin.intents.clock = lambda: now[0]
    plugin.messages.clock = lambda: now[0]
    try:
        _record_msg(
            plugin,
            msg_id="m-adm1",
            text="每天跑报告",
            user="10001",
            chat_id="qq:group:100246809",
            group=True,
            ts=now[0],
        )
        sched = await plugin.later_tool(
            action="schedule",
            source_msg_id="m-adm1",
            mode="recurring",
            every_seconds=600,
            profile="default",
            reason="生成日报",
            prompt="生成日报",
            chat_id="qq:group:100246809",
            platform="qq",
        )
        assert sched["success"] is True, sched
        intent_id = sched["intent_id"]

        # 超过 300s 消息窗口后，现役管理员创建的 scheduled_admin 仍可正常执行
        now[0] += 3600.0
        dispatched = []

        async def _ok_cap(name, **kwargs):
            if name == "llm.generate_with_tools":
                dispatched.append(kwargs)
                return {"success": True, "response": "日报已完成", "tool_calls": []}
            return {"success": True}

        plugin.ctx.call_capability = _ok_cap

        row = plugin.intents.get(intent_id)
        legacy_thread = plugin.threads.create(
            plugin.profiles.get("default"),
            "管理员历史后台线程",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="10001",
            grant={"scheduled_admin": True, "issuer_admin": "qq:10001"},
        )
        assert plugin._activity_authorized(legacy_thread) is True
        assert plugin._activity_permitted(legacy_thread) is True

        # 移除发起人 qq:10001（尽管 qq:10002 仍在管理员列表中）
        await plugin.on_config_update(
            "self",
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": ["qq:group:100246809", "qq:private:10001"],
                "admins": ["qq:10002"],
            },
            2,
        )
        # 在旧授权校验中被清空/撤销时立刻拒绝并暂停
        assert plugin._activity_authorized(legacy_thread) is False
        assert plugin._activity_permitted(legacy_thread) is False

        # 清空管理员（即使携带历史 scheduled_admin=True，也不能再放行）
        plugin.admins.clear()
        legacy_no_issuer = {
            **legacy_thread,
            "grant": {"scheduled_admin": True},
        }
        assert plugin._activity_authorized(legacy_no_issuer) is False
        assert plugin._activity_permitted(legacy_no_issuer) is False

        # 恢复 qq:10002 为管理员，验证 Pulse 到期调度已被撤销的 qq:10001 定时任务时会自动 paused 并等待接管
        await plugin.on_config_update(
            "self",
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": ["qq:group:100246809", "qq:private:10001"],
                "admins": ["qq:10002"],
            },
            3,
        )
        with plugin.intents.store.connect() as db, db:
            db.execute("UPDATE intents SET due_at=? WHERE intent_id=?", (now[0] - 5, intent_id))
        await plugin.pulse._intents()
        after_pause = plugin.intents.get(intent_id)
        assert after_pause["status"] == "paused"
        assert "接管" in after_pause.get("error", "") or after_pause.get("wake_status") == "authority_paused"

        # 现任管理员 qq:10002 接管 resume 后可恢复执行
        _record_msg(
            plugin,
            msg_id="m-adm2",
            text="我来接管这个定时",
            user="10002",
            chat_id="qq:group:100246809",
            group=True,
            ts=now[0],
        )
        resumed = await plugin.later_tool(
            action="resume",
            intent_id=intent_id,
            source_msg_id="m-adm2",
            chat_id="qq:group:100246809",
            platform="qq",
        )
        assert resumed["success"] is True, resumed
        assert plugin.intents.get(intent_id)["payload"]["issuer_admin"] == "qq:10002"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b1_envelope_prevents_silent_escalation_on_profile_hot_edit_and_narrowing_applies(tmp_path):
    now = [1_794_500_000.0]
    plugin = await _setup_plugin(tmp_path, ["qq:10001"])
    plugin.messages.clock = lambda: now[0]
    try:
        plugin.vault.set("top_sec", "sec-val-123456", label="Top", domain="example.com")
        _record_msg(
            plugin,
            msg_id="m-env-1",
            text="按当前无密钥配置启动线程",
            user="10001",
            chat_id="qq:private:10001",
            group=False,
            ts=now[0],
        )
        # 先启动线程时，default 没有 secrets 也没有该 mcp
        res = await plugin.agent_tool(
            action="start",
            profile="default",
            message="先只读分析",
            source_msg_id="m-env-1",
            chat_id="qq:private:10001",
            platform="qq",
            wait_seconds=10,
        )
        assert res["success"] is True
        tid = res["thread_id"]
        row = plugin.threads.get(tid, "qq:private:10001")

        # 随后热改 default 档案追加 secrets & 写入式 execution_mode，不应悄然扩大旧线程委派包络
        plugin.profiles.update("default", secrets=["top_sec"])
        with pytest.raises(PermissionError, match="包络|授权|凭据|扩大"):
            await plugin.runtime.call(
                "exec_command",
                {"command": "echo hi", "secrets": ["top_sec"]},
                row,
            )

        # 收窄配置（停用档案）必须立即拦截旧线程
        plugin.profiles.update("default", enabled=False)
        assert plugin._activity_permitted(row) is False
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b1_revoke_grant_blocks_subsequent_calls_and_terminates_active_process(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    grants = PermissionGrantStore(tmp_path / "maisya.db")
    runner = HostWorkspaceRunner(grants=grants)
    grants.grant(
        scope="chat:qq:private:10001",
        session_id="th-kill-1",
        action_key="host_exec:unisolated",
        tier="session",
        actor="qq:10001",
    )

    started = asyncio.Event()
    script = "import time, sys; print('STARTED', flush=True); time.sleep(30)"
    req = ExecutionRequest(
        command=[sys.executable, "-c", script],
        workspace=ws,
        is_admin=True,
        actor="qq:10001",
        scope="chat:qq:private:10001",
        session_id="th-kill-1",
        permission_mode="ask_risky",
        unisolated_host=True,
        timeout=60.0,
    )
    task = asyncio.create_task(runner.execute(req))
    for _ in range(50):
        if runner.active_count(session_id="th-kill-1") > 0:
            started.set()
            break
        await asyncio.sleep(0.05)
    assert started.is_set(), "subprocess should be tracked as active"

    # 撤销该线程的授权并终止存活本地子进程
    grants.revoke_scope("chat:qq:private:10001", session_id="th-kill-1")
    killed = runner.terminate_for(session_id="th-kill-1")
    assert killed >= 1
    res = await task
    assert res.status in {"denied", "error", "timeout"}

    # 后续同动作必须被重新拦截
    next_res = await runner.execute(
        ExecutionRequest(
            command=[sys.executable, "-c", "print('after-revoke')"],
            workspace=ws,
            is_admin=True,
            actor="qq:10001",
            scope="chat:qq:private:10001",
            session_id="th-kill-1",
            permission_mode="ask_risky",
            unisolated_host=True,
        )
    )
    assert next_res.status == "approval_required"


# ──────────────────── B2: 统一数据归属与共享档案边界 ────────────────────


@pytest.mark.asyncio
async def test_b2_role_memory_chat_scoping_and_legacy_quarantine(tmp_path):
    plugin = await _setup_plugin(tmp_path, ["qq:10001"])
    try:
        # 1. 从私聊写 role_memory(default)，应落入私聊专属 role_memory，不污染全局及群聊
        res = await execute_shape(
            plugin,
            action="role_memory",
            op="patch",
            target="default",
            patch={"append": "- 私聊主人喜欢在结果末尾附原网页链接。"},
            current_chat_id="qq:private:10001",
            current_platform="qq",
            admin_verified=True,
        )
        assert "version" in res and res["kind"] == "role_memory"

        default_prof = plugin.profiles.get("default")
        assert "原网页链接" not in plugin.memory.methods(default_prof)
        assert "原网页链接" not in plugin.memory.methods(default_prof, chat_id="qq:group:100246809")
        assert "原网页链接" in plugin.memory.methods(default_prof, chat_id="qq:private:10001")
        assert "原网页链接" not in plugin.memory.read("role_memory", target="default")["content"]

        # 2. 尝试把带群标识/私聊信息写入全局 role_memory 应被写入闸拦截
        with pytest.raises(ValueError, match="MEMORY.md"):
            plugin.memory.write("role_memory", "本群的人只看短摘要 100246809", target="default")

        # 3. 存量未归属且包含会话 clues 的全局 profiles/<id>/MEMORY.md 重新加载时必须被隔离为 admin_only
        global_rm = plugin.data_root / "abilities/profiles/scout/MEMORY.md"
        global_rm.write_text("本群固定口头禅不要改", encoding="utf-8")
        from maisya.memory import MemoryLayers

        reloaded = MemoryLayers(
            plugin.data_root, plugin.threads, plugin.profiles, plugin.vault
        )
        scout_prof = plugin.profiles.get("scout")
        assert "固定口头禅" not in reloaded.methods(
            scout_prof, chat_id="qq:group:100246809", is_admin=False
        )
        assert "固定口头禅" in reloaded.methods(
            scout_prof, chat_id="qq:group:100246809", is_admin=True
        )
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b2_scope_propagation_self_from_threads_recall_attachments_and_persistent_taint(tmp_path):
    now = [1_794_500_000.0]
    plugin = await _setup_plugin(tmp_path, ["qq:10001"])
    plugin.messages.clock = lambda: now[0]
    try:
        # 1. 创建私聊线程与附件
        t_priv = plugin.threads.create(
            plugin.profiles.get("default"),
            "整理私聊加密备忘",
            chat_id="qq:private:10001",
            platform="qq",
            user_id="10001",
        )
        plugin.threads.activate(t_priv["thread_id"])
        with plugin.runtime.files.activity(t_priv) as work:
            work.write("out/priv_note.md", "私聊保密内容 PRIVATE_TOKEN_ALPHA")
            work.commit()
            atts = plugin.runtime.files.attach(t_priv, work, ["out/priv_note.md"])
        att_handle = atts[0]["handle"]
        assert plugin.threads.finish(
            t_priv["thread_id"],
            "qq:private:10001",
            {"text": "私聊结果：PRIVATE_TOKEN_ALPHA", "attachments": atts},
            [{"role": "assistant", "content": "ok"}],
            t_priv["revision"],
        ) is True

        # 群聊不可访问私聊附件与私聊线程检索
        with pytest.raises(PermissionError):
            plugin.runtime.files.resolve(att_handle, "qq:group:100246809")

        _record_msg(
            plugin,
            msg_id="m-grp-other",
            text="查一下 PRIVATE_TOKEN_ALPHA",
            user="20002",
            chat_id="qq:group:100246809",
            group=True,
            ts=now[0],
        )
        rec = await plugin.recall_tool(
            query="PRIVATE_TOKEN_ALPHA",
            source_msg_id="m-grp-other",
            chat_id="qq:group:100246809",
            platform="qq",
        )
        assert "PRIVATE_TOKEN_ALPHA" not in json.dumps(rec, ensure_ascii=False)

        # 2. 管理员将私聊结果通过 from_threads 带入另一私聊预览或受限自建线程时，范围传播阻止流入普通群聊
        _record_msg(
            plugin,
            msg_id="m-dm-adm",
            text="整理私聊结果供复核",
            user="10001",
            chat_id="qq:private:10001",
            group=False,
            ts=now[0],
        )
        async def _simple_cap(name, **kwargs):
            if name == "llm.generate_with_tools":
                return {"success": True, "response": "衍生结论 PRIVATE_DERIVED_OK", "tool_calls": []}
            return {"success": True}
        plugin.ctx.call_capability = _simple_cap

        derived = await plugin.agent_tool(
            action="start",
            profile="default",
            message="基于旧结果生成衍生摘要",
            from_threads=[t_priv["thread_id"]],
            source_msg_id="m-dm-adm",
            chat_id="qq:private:10001",
            platform="qq",
            wait_seconds=10,
        )
        assert derived["success"] is True
        derived_row = plugin.threads.get(derived["thread_id"], "qq:private:10001")
        assert derived_row["visibility_scope"].startswith("private:")

        # 3. 管理员明确按 (thread_id, revision) 定向发布私聊线程结果到指定群 A（仅该版本放行，群 B 仍隔离）
        sent_to = []
        async def _share_cap(name, **kwargs):
            sent_to.append((name, kwargs))
            return {"success": True, "text": "已整理好摘要", "delivered": True}
        plugin.ctx.call_capability = _share_cap
        share_ok = await plugin.agent_tool(
            action="share",
            thread_id=t_priv["thread_id"],
            message="分享这一条给测试群",
            attachment_handles=[att_handle],
            target_chat="qq:group:100246809",
            deliver_to="target",
            source_msg_id="m-dm-adm",
            chat_id="qq:private:10001",
            platform="qq",
        )
        assert share_ok["success"] is True, share_ok
        # 群 A 获得了该 (thread_id, revision) 的显式发布许可，群 B 仍无权访问
        assert plugin.threads.has_publication_grant(
            t_priv["thread_id"], t_priv["revision"], "qq:group:100246809"
        ) is True
        assert plugin.threads.has_publication_grant(
            t_priv["thread_id"], t_priv["revision"], "qq:group:100246810"
        ) is False

        # 4. 持久档案在群内由管理员绑定私密作用域后，普通成员后续发言不得继承受污染的持久主线与工作区
        plugin.profiles.create(
            "ops_digest",
            name="受污染测试持久角色",
            description="持久线程隔离测试",
            session_mode="persistent",
            access="everyone",
            enabled=True,
        )
        plugin.ctx.call_capability = _simple_cap
        _record_msg(
            plugin,
            msg_id="m-grp-adm",
            text="在群里结合私聊线索整理",
            user="10001",
            chat_id="qq:group:100246809",
            group=True,
            ts=now[0],
        )
        adm_p = await plugin.agent_tool(
            action="start",
            profile="ops_digest",
            message="汇总刚才私聊细节",
            from_threads=[t_priv["thread_id"]],
            source_msg_id="m-grp-adm",
            chat_id="qq:group:100246809",
            platform="qq",
            wait_seconds=10,
        )
        assert adm_p["success"] is True
        # 该主线因引用私聊已被窄化，普通群友继续调用 ops_digest 必须开新干净线程，绝不继承原线程上下文
        mem_p = await plugin.agent_tool(
            action="start",
            profile="ops_digest",
            message="帮我看今天的公开新闻",
            source_msg_id="m-grp-other",
            chat_id="qq:group:100246809",
            platform="qq",
            wait_seconds=10,
        )
        assert mem_p["success"] is True
        assert mem_p["thread_id"] != adm_p["thread_id"]

        # 5. owner="self" 引用私聊线程时同样传播私有范围，不可被普通群召回或未经发布注入未授权群
        self_via_tool = await plugin.agent_tool(
            action="start",
            owner="self",
            profile="default",
            message="记录私聊线索到自我经历",
            from_threads=[t_priv["thread_id"]],
            source_msg_id="m-dm-adm",
            chat_id="qq:private:10001",
            platform="qq",
            wait_seconds=10,
        )
        assert self_via_tool["success"] is False
        self_tainted = await plugin.engine.start(
            plugin.profiles.get("default"),
            "自我思考 PRIVATE_SELF_TAINTED",
            chat_id="self",
            platform="self",
            user_id="",
            owner="self",
            wait_seconds=10,
            grant={"admin_verified": True},
            from_threads=[t_priv["thread_id"]],
        )
        self_tainted_row = plugin.threads.get(
            self_tainted["thread_id"], "self", allow_self=True
        )
        assert self_tainted_row["visibility_scope"].startswith("private:")
        assert self_tainted_row.get("result", {}).get("error")
        t_self_done = plugin.threads.create(
            plugin.profiles.get("default"),
            "私聊关联的自我沉淀 PRIVATE_SELF_TAINTED",
            chat_id="self",
            platform="self",
            user_id="",
            owner="self",
            grant={"visibility_scope": "private:qq:private:10001"},
        )
        plugin.threads.activate(t_self_done["thread_id"])
        assert plugin.threads.finish(
            t_self_done["thread_id"],
            "self",
            {"text": "PRIVATE_SELF_TAINTED"},
            [{"role": "assistant", "content": "ok"}],
            t_self_done["revision"],
        ) is True
        _record_msg(
            plugin,
            msg_id="m-grp2-other",
            text="查 PRIVATE_SELF_TAINTED",
            user="20003",
            chat_id="qq:group:100246810",
            group=True,
            ts=now[0],
        )
        rec2 = await plugin.recall_tool(
            query="PRIVATE_SELF_TAINTED",
            scope="self",
            source_msg_id="m-grp2-other",
            chat_id="qq:group:100246810",
            platform="qq",
        )
        assert "PRIVATE_SELF_TAINTED" not in json.dumps(rec2, ensure_ascii=False)
    finally:
        await plugin.on_unload()


# ──────────────────── B3: 宿主高信任 vs 容器隔离 ────────────────────


@pytest.mark.asyncio
async def test_b3_gpt_repro1_unisolated_host_exec_requires_confirmation_and_session_grant(tmp_path):
    now = [1_794_500_000.0]
    plugin = await _setup_plugin(tmp_path, ["qq:10001"])
    plugin.messages.clock = lambda: now[0]
    escaped_target = tmp_path / "escaped_by_script.txt"
    try:
        plugin.profiles.update("default", execution_mode="host", permission_mode="ask_risky")
        _record_msg(
            plugin,
            msg_id="m-b3-adm",
            text="运行工作区脚本",
            user="10001",
            chat_id="qq:private:10001",
            group=False,
            ts=now[0],
        )

        # GPT 复现场景 1：看似常规的 `python3 project/task.py` 实际上会通过脚本内部相对父目录逃逸写入，
        # 宿主机无容器隔离时绝不能当低风险直接执行
        tool_seq = [
            {
                "id": "call-w1",
                "type": "function",
                "function": {
                    "name": "workspace_file",
                    "arguments": json.dumps(
                        {
                            "action": "write",
                            "path": "project/task.py",
                            "content": "import pathlib; pathlib.Path('../escaped_by_script.txt').write_text('pwned', encoding='utf-8'); print('OK')\n",
                        }
                    ),
                },
            },
            {
                "id": "call-e1",
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "arguments": json.dumps({"command": "python3 project/task.py"}),
                },
            },
        ]
        idx = {"i": 0}

        async def _llm_ordered(name, **kwargs):
            if name != "llm.generate_with_tools":
                return {"success": True}
            if idx["i"] < len(tool_seq):
                tc = tool_seq[idx["i"]]
                idx["i"] += 1
                return {"success": True, "response": "", "tool_calls": [tc]}
            return {"success": True, "response": "任务已执行结束", "tool_calls": []}

        plugin.ctx.call_capability = _llm_ordered
        started = await plugin.agent_tool(
            action="start",
            profile="default",
            message="写并运行脚本 task.py",
            source_msg_id="m-b3-adm",
            chat_id="qq:private:10001",
            platform="qq",
            wait_seconds=10,
        )
        assert started["success"] is True
        assert started["state"] == "waiting", started
        assert not list(tmp_path.rglob("escaped_by_script.txt"))
        pending = plugin.runtime.pending_approval_for_thread(started["thread_id"])
        assert pending is not None
        assert pending["action_key"] == "host_exec:unisolated"
        assert pending["category"] == "host_unisolated"

        # 授予 session（同线程）宿主机高信任后，续跑执行并通过
        approved = await plugin.agent_tool(
            action="approve",
            thread_id=started["thread_id"],
            grant_tier="session",
            source_msg_id="m-b3-adm",
            chat_id="qq:private:10001",
            platform="qq",
            wait_seconds=10,
        )
        assert approved["success"] is True
        assert approved["resolved"] is True

        # 同线程再次执行常规命令不再重复弹窗询问
        row = plugin.threads.get(started["thread_id"], "qq:private:10001")
        ok2 = await plugin.runtime.call(
            "exec_command",
            {"command": "python3 -c \"print('SECOND_OK')\""},
            row,
        )
        assert ok2["status"] == "ok" and "SECOND_OK" in ok2["stdout"]

        # 但破坏性命令 rm -rf 仍必须拦截确认
        risky_rm = await plugin.runtime.call("exec_command", {"command": "rm -rf project/task.py"}, row)
        assert risky_rm["status"] == "approval_required"
        assert risky_rm["category"] == "destructive_shell"

        # 当档案设为 execution_mode="sandbox" 且 Docker 未就绪时，绝不悄然降级为宿主机执行
        plugin.profiles.update("default", execution_mode="sandbox")
        row_sb = {**row, "profile_snapshot": {**row["profile_snapshot"], "execution_mode": "sandbox", "environment": "sandbox"}}
        with pytest.raises(RuntimeError, match="Docker|沙箱"):
            await plugin.runtime.call("exec_command", {"command": "python3 -c \"print('no')\""}, row_sb)

        # 当 Docker 就绪且 execution_mode="auto" 时，自动优先路由到隔离 DockerRunner 而非 unisolated 宿主机
        docker_calls = []

        async def _fake_docker(req: ExecutionRequest) -> ExecutionResult:
            docker_calls.append(req)
            return ExecutionResult(status="ok", exit_code=0, stdout="docker-ok", stderr="", runner_name="docker")

        plugin.settings.sandbox.enabled = True
        plugin.runtime.sandbox.ready = True
        plugin.docker_runner.execute = _fake_docker  # type: ignore[method-assign]
        plugin.profiles.update("default", execution_mode="auto")
        row_auto = {**row, "profile_snapshot": {**row["profile_snapshot"], "execution_mode": "auto", "environment": "readonly"}}
        docker_res = await plugin.runtime.call("exec_command", {"command": "python3 -c \"print('ok')\""}, row_auto)
        assert docker_res["status"] == "ok" and len(docker_calls) == 1
    finally:
        await plugin.on_unload()
