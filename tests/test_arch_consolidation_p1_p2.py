"""P1/P2 architecture consolidation TDD suite (B4, B5, B6, B7)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from maisya.config import Settings
from maisya.intents import Intents
from maisya.learning import Learning
from maisya.packages import export_package, import_package_bundle
from maisya.services import Services
from test_foundation import event as make_ev
from test_foundation import loaded as base_loaded


async def _setup_plugin(tmp_path: Path, admins=None, *, extra_cfg=None):
    plugin = await base_loaded(tmp_path, list(admins or ["qq:10001"]))
    cfg = {
        "plugin": {"enabled": True, "config_version": "0.1.0"},
        "enabled_chats": ["qq:group:100246809", "qq:private:10001"],
        "admins": list(admins or ["qq:10001"]),
        "permission_mode": "ask_risky",
        "sandbox": {"enabled": False},
        **(extra_cfg or {}),
    }
    await plugin.on_config_update("self", cfg, 1)
    return plugin


# ──────────────────── B4: 学习晋升规则与来源溯源 ────────────────────


@pytest.mark.asyncio
async def test_b4_bare_tool_chain_does_not_mutate_global_skills_and_patch_creates_proposal_with_provenance(tmp_path):
    plugin = await _setup_plugin(tmp_path)
    try:
        llm_calls = []

        async def _llm(name, **kwargs):
            if name == "llm.generate_with_tools":
                llm_calls.append(kwargs)
                return {
                    "success": True,
                    "response": json.dumps(
                        {
                            "op": "patch",
                            "skill_id": "deploy-guard",
                            "lesson": "发布前务必先校验回滚快照完整性。",
                            "note": "先校验快照再切目录。",
                        },
                        ensure_ascii=False,
                    ),
                    "tool_calls": [],
                }
            return {"success": True}

        plugin.ctx.call_capability = _llm
        plugin.memory.create_skill(
            "deploy-guard",
            title="发布护栏",
            description="安全发布检查",
            content="---\ntitle: 发布护栏\n---\n\n# 发布护栏\n- 检查依赖。\n",
            allow_custom=True,
        )
        plugin.profiles.update("default", skills=["deploy-guard"])
        learning = Learning(plugin.ctx, plugin.memory)

        # 1. 纯“多步工具成功”且无报错恢复 / 无 user steer：不应触发提炼或写入全局技能
        t_bare = plugin.threads.create(
            plugin.profiles.get("default"),
            "多步只读检查",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="10001",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                t_bare["thread_id"],
                "tools",
                1,
                {
                    "calls": [
                        {"id": "a1", "function": {"name": "web_search", "arguments": {}}},
                        {"id": "a2", "function": {"name": "web_fetch", "arguments": {}}},
                        {"id": "a3", "function": {"name": "workspace_file", "arguments": {}}},
                    ],
                    "results": [
                        {"call_id": "a1", "value": {"ok": True}},
                        {"call_id": "a2", "value": {"ok": True}},
                        {"call_id": "a3", "value": {"ok": True}},
                    ],
                },
            )
            db.execute(
                "UPDATE agent_threads SET state='done', completed_revision=1, result=? WHERE thread_id=?",
                (json.dumps({"text": "完成多步查看"}, ensure_ascii=False), t_bare["thread_id"]),
            )
        await learning.schedule(plugin.threads.get(t_bare["thread_id"], "qq:group:100246809"))
        await learning.drain()
        assert llm_calls == []
        assert plugin.memory.read("skill", target="deploy-guard")["version"] == 1
        assert plugin.memory.list_skill_proposals(status="pending") == []

        # 2. 真实报错后恢复：自动写入档案短笔记并附带来源；全局技能修改进草稿队列待确认，确认后才升级版本
        t_rec = plugin.threads.create(
            plugin.profiles.get("default"),
            "处理原子部署异常恢复",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="10001",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                t_rec["thread_id"],
                "tools",
                1,
                {
                    "calls": [
                        {"id": "r1", "function": {"name": "workspace_file", "arguments": {}}},
                        {"id": "r2", "function": {"name": "workspace_file", "arguments": {}}},
                    ],
                    "results": [
                        {"call_id": "r1", "value": {"error": "snapshot missing"}},
                        {"call_id": "r2", "value": {"ok": True, "text": "verified"}},
                    ],
                },
            )
            db.execute(
                "UPDATE agent_threads SET state='done', completed_revision=1, result=? WHERE thread_id=?",
                (json.dumps({"text": "已校验并完成。"}, ensure_ascii=False), t_rec["thread_id"]),
            )
        await learning.schedule(plugin.threads.get(t_rec["thread_id"], "qq:group:100246809"))
        await learning.drain()

        # 档案短笔记自动落盘且保留版本来源
        assert "先校验快照再切目录" in plugin.memory.read("notes", target="default")["content"]
        note_hist = plugin.memory.history("notes", target="default")
        assert any(t_rec["thread_id"] in str(h.get("author") or "") for h in note_hist)

        # 全局 SKILL.md 尚未被静默改动，必须先进入 pending skill_proposals 并含线程@版本来源
        sk_before = plugin.memory.read("skill", target="deploy-guard")
        assert sk_before["version"] == 1
        assert "发布前务必先校验回滚快照完整性" not in sk_before["content"]

        proposals = plugin.memory.list_skill_proposals(status="pending")
        patch_props = [p for p in proposals if p["skill_id"] == "deploy-guard"]
        assert len(patch_props) == 1
        prop = patch_props[0]
        prov = f"thread:{t_rec['thread_id']}@r1"
        assert prov in json.dumps(prop, ensure_ascii=False)

        # 管理员确认草稿后才升级 deploy-guard 到 v2
        confirmed = await plugin.manage_write(
            "confirm_skill_proposal",
            json.dumps({"proposal_id": prop["proposal_id"]}, ensure_ascii=False),
        )
        assert confirmed["success"] is True, confirmed
        sk_after = plugin.memory.read("skill", target="deploy-guard")
        assert sk_after["version"] >= 2
        assert "发布前务必先校验回滚快照完整性" in sk_after["content"]
    finally:
        await plugin.on_unload()


# ──────────────────── B5: 多写端持久事务与崩溃恢复 ────────────────────


@pytest.mark.asyncio
async def test_b5_tx_journal_crash_recovery_rolls_back_vault_services_and_package_imports_without_secret_leak(tmp_path):
    from maisya.tx_recovery import TxJournal, recover_pending_transactions

    plugin = await _setup_plugin(tmp_path)
    try:
        # 1. 服务配置 + 加密保险箱多步写：崩溃与故障注入下，不落明文、回滚或启动恢复完整还原
        old_secret = "sk-old-mcp-secret-9988"
        new_secret = "sk-new-mcp-secret-7766"
        res0 = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "name": "svc_alpha",
                    "expected_version": plugin.management._version(plugin.runtime.services.load()),
                    "item": {
                        "command": ["python3", "-m", "demo_mcp"],
                        "env": {"API_TOKEN": old_secret},
                        "readonly": True,
                        "access": "everyone",
                    },
                },
                ensure_ascii=False,
            ),
        )
        assert res0["success"] is True, res0
        cfg_before = plugin.runtime.services.load()
        handle_alpha = cfg_before["servers"]["svc_alpha"]["credentials"]["API_TOKEN"]
        assert plugin.vault.for_execution(handle_alpha) == old_secret

        # 注入写入 services.json 时崩溃，且校验 tx_journal 落盘绝无明文秘密
        orig_services_path = plugin.runtime.services.path
        orig_atomic = __import__("maisya.management", fromlist=["atomic_private_write"]).atomic_private_write
        import maisya.management as mgmt_mod

        def _boom_on_services(path, content, *args, **kwargs):
            if Path(path) == orig_services_path:
                raise OSError("simulated power loss during services.json swap")
            return orig_atomic(path, content, *args, **kwargs)

        mgmt_mod.atomic_private_write = _boom_on_services
        try:
            res_fail = await plugin.manage_write(
                "upsert_service_item",
                json.dumps(
                    {
                        "kind": "mcp",
                        "name": "svc_alpha",
                        "orig_name": "svc_alpha",
                        "expected_version": res0["version"],
                        "item": {
                            "command": ["python3", "-m", "demo_mcp"],
                            "env": {"API_TOKEN": new_secret},
                            "readonly": True,
                            "access": "everyone",
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            assert res_fail["success"] is False
            assert "simulated power loss" in res_fail["error"]
        finally:
            mgmt_mod.atomic_private_write = orig_atomic

        assert plugin.vault.for_execution(handle_alpha) == old_secret
        assert plugin.runtime.services.load()["servers"]["svc_alpha"]["credentials"]["API_TOKEN"] == handle_alpha

        # 模拟进程硬杀：已准备 TxJournal + 修改了 vault/meta 与 services.json，重启恢复必须还原旧版本
        tx = TxJournal(plugin.data_root, kind="services_vault")
        tx.backup_file("services", orig_services_path)
        tx.backup_file("vault_meta", plugin.vault.meta_path)
        enc_file = plugin.vault.path / f"{handle_alpha}.enc"
        tx.backup_file("enc_alpha", enc_file)
        plugin.vault.set_mcp_secret("svc_alpha", "API_TOKEN", new_secret, slot="env")
        orig_services_path.write_text('{"servers":{"svc_alpha":{"command":["broken"],"tools":["*"]}},"apis":{}}', encoding="utf-8")

        # 检查 tx_journal 目录绝不包含明文秘密
        journal_dir = Path(plugin.data_root) / "tx_journal"
        assert journal_dir.exists()
        for f in journal_dir.rglob("*"):
            if f.is_file():
                blob = f.read_bytes()
                assert new_secret.encode("utf-8") not in blob
                assert old_secret.encode("utf-8") not in blob

        recovered = recover_pending_transactions(
            plugin.data_root, store_path=plugin.threads.path, memory=plugin.memory
        )
        assert len(recovered) >= 1
        assert plugin.vault.for_execution(handle_alpha) == old_secret
        assert plugin.runtime.services.load()["servers"]["svc_alpha"]["command"] == cfg_before["servers"]["svc_alpha"]["command"]

        # 2. 能力包导入中途失败（含覆盖已有技能+附属文件+更新角色+部分排期已写入）必须完整回滚且不吞错误
        plugin.memory.create_skill(
            "existing-pack-sk",
            title="旧技能",
            description="旧描述",
            content="---\ntitle: 旧技能\n---\n\n# 旧技能\n- 原始规则\n",
            allow_custom=True,
        )
        comp_path = Path(plugin.data_root) / "abilities" / "skills" / "existing-pack-sk" / "extra.txt"
        comp_path.write_text("OLD_COMPANION", encoding="utf-8")
        plugin.profiles.create(
            "pack_host_role",
            name="包绑定角色",
            description="原角色",
            skills=["existing-pack-sk"],
            enabled=True,
        )
        # 用 exporter 生成合法 zip 能力包并附加覆盖内容与排期
        pkg_res = export_package(plugin, skill_ids=["existing-pack-sk"], kind="capability_pack")
        pkg_bytes = pkg_res["zip_bytes"]
        import io, zipfile
        buf_in = zipfile.ZipFile(io.BytesIO(pkg_bytes), "r")
        buf_out = io.BytesIO()
        with zipfile.ZipFile(buf_out, "w") as zout:
            for item in buf_in.infolist():
                if item.filename in {"skills/existing-pack-sk/extra.txt", "pack.toml"}:
                    continue
                data = buf_in.read(item.filename)
                if item.filename == "skills/existing-pack-sk/SKILL.md":
                    data = "---\ntitle: 新技能覆盖\ndescription: 新描述\n---\n\n# 新技能覆盖\n- 被中断的新规则\n".encode("utf-8")
                zout.writestr(item, data)
            zout.writestr("skills/existing-pack-sk/extra.txt", b"NEW_COMPANION")
            zout.writestr(
                "pack.toml",
                (
                    'id = "demo-pack"\n'
                    'name = "Demo Pack"\n'
                    'skills = ["existing-pack-sk"]\n'
                    'toolsets = ["workspace"]\n\n'
                    '[[schedules]]\n'
                    'mode = "schedule"\n'
                    'every_seconds = 600\n'
                    'reason = "包排期"\n'
                    'prompt = "执行检查"\n'
                ).encode("utf-8"),
            )
        pack_zip = buf_out.getvalue()

        # 注入 capability_pack 在更新目标档案后写入部分排期并中途崩溃
        import maisya.packages as pkg_mod
        orig_apply_sched = pkg_mod._apply_imported_schedules

        def _broken_sched(*args, **kwargs):
            orig_apply_sched(*args, **kwargs)
            raise RuntimeError("injected schedule failure after partial schedule")

        pkg_mod._apply_imported_schedules = _broken_sched
        try:
            with pytest.raises(RuntimeError):
                import_package_bundle(
                    plugin,
                    raw_bytes=pack_zip,
                    target_profile_id="pack_host_role",
                    overwrite=True,
                    schedule_target_chat="qq:group:100246809",
                )
        finally:
            pkg_mod._apply_imported_schedules = orig_apply_sched

        # 旧技能内容、版本号、附属文件、绑定角色完整复原，且部分写入的排期已清理
        assert "原始规则" in plugin.memory.read("skill", target="existing-pack-sk")["content"]
        assert comp_path.read_text(encoding="utf-8") == "OLD_COMPANION"
        assert "existing-pack-sk" in plugin.profiles.get("pack_host_role").skills
        assert all(it.get("reason") != "包排期" for it in plugin.intents.list_all(status=""))

        # 若回滚步骤本身失败，不能静默吞异常（必须抛 RuntimeError 暴露未完全回滚状态）
        tx_fail = TxJournal(plugin.data_root, kind="package_import")
        tx_fail.record_created_intent("in_nonexistent_test")
        bad_target = tmp_path / "conflict_target.txt"
        bad_target.write_text("v1", encoding="utf-8")
        tx_fail.backup_file("bad", bad_target)
        tx_fail._manifest["operations"][-1]["backup_rel"] = "backups/missing.bin"
        tx_fail._save_manifest()
        with pytest.raises(RuntimeError, match="回滚"):
            tx_fail.rollback(store_path=plugin.threads.path, memory=plugin.memory)
    finally:
        await plugin.on_unload()


# ──────────────────── B6: 外部副作用分级、稳定操作 ID 与幂等键 ────────────────────


@pytest.mark.asyncio
async def test_b6_external_side_effect_default_uncertain_blocks_regenerated_call_id_retry_and_injects_idempotency_key(tmp_path):
    plugin = await _setup_plugin(tmp_path)
    try:
        # 1. 注册一个含可写动作 & 声明可幂等动作 & 只读动作的跨插件 API，检验分类与默认 non_idempotent_write 防重复
        api_calls = []

        async def _api_cap(name, **kwargs):
            if name == "api.call":
                api_calls.append(kwargs)
                api_name = kwargs.get("api_name", "")
                if api_name == "ext.write.create":
                    if len(api_calls) == 1:
                        raise TimeoutError("gateway timed out after commit")
                    return {"success": True, "result": {"created": True}}
                if api_name == "ext.idem.upsert":
                    if kwargs.get("args", {}).get("_fail_once"):
                        # Fail on exact first invocation of idem
                        raise TimeoutError("transient timeout on idempotent call")
                    return {"success": True, "result": {"ok": True, "key": kwargs.get("args", {}).get("idempotency_key")}}
                return {"success": True, "result": {"items": [1]}}
            return {"success": True}

        plugin.ctx.call_capability = _api_cap
        svc_payload = {
            "servers": {
                "ext_mcp": {
                    "command": ["python3", "-m", "fake_mcp"],
                    "tools": ["*", "mcp_create_ticket", "mcp_read_ticket"],
                    "readonly": False,
                    "readonly_tools": ["mcp_read_ticket"],
                    "idempotent_tools": ["mcp_upsert_ticket"],
                    "access": "everyone",
                }
            },
            "apis": {
                "create_order": {
                    "api_name": "ext.write.create",
                    "readonly": False,
                    "access": "everyone",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "idempotency_key": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                },
                "upsert_item": {
                    "api_name": "ext.idem.upsert",
                    "readonly": False,
                    "idempotent": True,
                    "access": "everyone",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "item": {"type": "string"},
                            "idempotency_key": {"type": "string"},
                        },
                        "required": ["item"],
                    },
                },
                "list_items": {
                    "api_name": "ext.read.list",
                    "readonly": True,
                    "access": "everyone",
                },
            },
        }
        plugin.runtime.services.path.write_text(
            json.dumps(svc_payload, ensure_ascii=False), encoding="utf-8"
        )
        plugin.runtime.services.load()
        prof = plugin.profiles.create(
            "ext_worker",
            name="外部对接角色",
            description="测试外部副作用防重",
            access="admins",
            mcp=["ext_mcp"],
            apis=["create_order", "upsert_item", "list_items"],
            enabled=True,
        )
        row = plugin.threads.create(
            prof,
            "创建外部工单",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="10001",
            grant={"console_admin": True, "issuer_admin": "console"},
        )
        plugin.threads.activate(row["thread_id"])
        ctx1 = {**row, "_tool_call_id": "llm_call_001", "_step_index": 0}
        # 首次调用 non_idempotent_write：发送时注入 idempotency_key，但返回丢失超时 -> uncertain
        with pytest.raises(TimeoutError):
            await plugin.runtime.services.call_api(
                prof,
                {"action": "call", "api": "create_order", "arguments": {"title": "Order-42"}},
                ctx1,
                plugin.runtime,
            )
        assert len(api_calls) == 1
        sent_key_1 = api_calls[0]["args"].get("idempotency_key")
        assert isinstance(sent_key_1, str) and sent_key_1.startswith("maisya-")

        # 同一 (thread_id, revision) 内，模型更换全新 _tool_call_id 与 _step_index 重试：必须被阻止且不得再调用 api.call
        ctx2 = {**row, "_tool_call_id": "llm_call_REGENERATED_999", "_step_index": 1}
        blocked = await plugin.runtime.services.call_api(
            prof,
            {"action": "call", "api": "create_order", "arguments": {"title": "Order-42"}},
            ctx2,
            plugin.runtime,
        )
        assert blocked["status"] == "uncertain"
        assert blocked["reconcile_required"] is True
        assert str(blocked.get("op_id") or "").startswith("ext:")
        assert len(api_calls) == 1  # 未重发外部请求

        # read_only API 连续调用可正常运行不锁死
        r1 = await plugin.runtime.services.call_api(
            prof,
            {"action": "call", "api": "list_items", "arguments": {}},
            {**row, "_tool_call_id": "r1", "_step_index": 2},
            plugin.runtime,
        )
        assert r1["result"] == {"items": [1]}

        # 声明 idempotent=True 的写入工具，透传相同确定性 idempotency_key 且允许安全重试
        r_idem = await plugin.runtime.services.call_api(
            prof,
            {"action": "call", "api": "upsert_item", "arguments": {"item": "k1"}},
            {**row, "_tool_call_id": "i1", "_step_index": 3},
            plugin.runtime,
        )
        assert str(r_idem["result"]["key"]).startswith("maisya-")

        # 2. MCP 可写工具同理：未标注 readonly_tools/idempotent_tools 时默认 non_idempotent_write，更换 call_id 禁止盲目重发
        mcp_sent = []

        class _FakeSession:
            def __init__(self):
                self.tools = {
                    "mcp_create_ticket": {
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "subject": {"type": "string"},
                                "idempotency_key": {"type": "string"},
                            },
                            "required": ["subject"],
                        }
                    },
                    "mcp_read_ticket": {
                        "inputSchema": {"type": "object", "properties": {}}
                    },
                }

            async def request(self, method, params):
                mcp_sent.append(params)
                if params["name"] == "mcp_create_ticket":
                    raise TimeoutError("mcp remote timeout after apply")
                return {"content": [{"type": "text", "text": '{"ok": true}'}]}

            async def close(self):
                return None

        async def _fake_session(service, context, config):
            return _FakeSession()

        plugin.runtime.services._session = _fake_session
        with pytest.raises(TimeoutError):
            await plugin.runtime.services.call(
                prof,
                {
                    "service": "ext_mcp",
                    "tool": "mcp_create_ticket",
                    "arguments": {"subject": "Incident-1"},
                },
                {**row, "_tool_call_id": "m_call_1", "_step_index": 4},
                plugin.runtime,
            )
        assert len(mcp_sent) == 1
        assert str(mcp_sent[0]["arguments"].get("idempotency_key") or "").startswith("maisya-")

        # 模型重新生成全新 _tool_call_id 重试同操作 -> 拦截并要求核对
        mcp_blocked = await plugin.runtime.services.call(
            prof,
            {
                "service": "ext_mcp",
                "tool": "mcp_create_ticket",
                "arguments": {"subject": "Incident-1"},
            },
            {**row, "_tool_call_id": "m_call_REGEN_2", "_step_index": 5},
            plugin.runtime,
        )
        assert mcp_blocked["status"] == "uncertain"
        assert mcp_blocked["reconcile_required"] is True
        assert str(mcp_blocked.get("op_id") or "").startswith("ext:")
        assert len(mcp_sent) == 1
    finally:
        await plugin.on_unload()


# ──────────────────── B7: 运行预算、连续失败退避与 return 豁免 ────────────────────


@pytest.mark.asyncio
async def test_b7_background_budgets_token_accounting_and_exponential_backoff_preserve_return_intents(tmp_path):
    now = [1_794_500_000.0]
    plugin = await _setup_plugin(
        tmp_path,
        extra_cfg={
            "daily_bg_runs_limit": 2,
            "daily_bg_tokens_limit": 50000,
            "bg_max_consecutive_failures": 3,
            "proactive_level": "lively",
            "daily_proactive_limit": 0,
            "per_chat_daily_proactive_limit": 0,
        },
    )
    plugin.intents.clock = lambda: now[0]
    plugin.messages.clock = lambda: now[0]
    plugin.bridge.clock = lambda: now[0]
    try:
        # 注册一个测试群让 pulse enabled 且可投递
        ev_init = make_ev(text="大家好", user="20002", platform="qq", group=True)
        ev_init["message_id"] = "m-init"
        ev_init["session_id"] = "qq:group:100246809"
        ev_init["timestamp"] = str(now[0])
        ev_init["message_info"]["group_info"] = {"group_id": "100246809"}
        from maisya.trust import Message
        plugin.messages.record(Message.from_event(ev_init))

        waked = []
        fail_mode = [False]

        async def _cap(name, **kwargs):
            if name in {"reply.wake", "maisaka.proactive.trigger"} or "wake" in name:
                waked.append((name, kwargs))
                return {"success": True}
            if name == "llm.generate_with_tools":
                if fail_mode[0]:
                    raise RuntimeError("upstream model down")
                return {
                    "success": True,
                    "response": "后台巡检正常",
                    "tool_calls": [],
                    "usage": {"total_tokens": 350},
                }
            return {"success": True}

        plugin.ctx.call_capability = _cap
        # 让 watch/workflow 直接通过 watch_evaluator 或 engine 跑
        watch_runs = [0]

        async def _watch_eval(intent_row, ctx):
            watch_runs[0] += 1
            if fail_mode[0]:
                raise RuntimeError("watch source 503")
            return {"triggered": False, "summary": "no diff"}

        plugin.pulse.watch_evaluator = _watch_eval
        sched = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target="chat:qq:group:100246809",
            due_at=now[0] + 10,
            reason="后台盯梢预算测试",
            mode="watch",
            condition="发现新版本",
            payload={
                "platform": "qq",
                "profile": "default",
                "every_seconds": 300,
                "scheduled_admin": True,
                "issuer_admin": "qq:10001",
            },
            bypass_limits=True,
        )
        iid = sched["intent_id"]

        # 1. 跑满每日 2 次后台执行预算：第 1 次正常消耗并记录次数与可观测时长/token；第 2 次后预算耗尽
        now[0] += 15
        await plugin.pulse.tick()
        budget_after_1 = plugin.intents.bg_budget_state(
            runs_limit=plugin.settings.daily_bg_runs_limit,
            tokens_limit=plugin.settings.daily_bg_tokens_limit,
            now=now[0],
        )
        assert budget_after_1["runs_used"] == 1
        assert "cost" not in budget_after_1 and "usd" not in budget_after_1  # 不虚构金钱计价

        now[0] += 305
        await plugin.pulse.tick()
        assert watch_runs[0] == 2

        # 第 3 次到达触发时间时，日预算已耗尽 -> 转入 budget_paused，且不真正执行 watch_evaluator
        now[0] += 305
        await plugin.pulse.tick()
        assert watch_runs[0] == 2
        paused_row = plugin.intents.get(iid)
        assert paused_row["status"] == "budget_paused"

        # 概览暴露预算余量与耗尽状态
        from maisya.shaping import execute_shape
        now_snap = await plugin.manage_read("now_detail")
        assert "bg_budget" in now_snap
        ov = await execute_shape(plugin, action="overview", op="read", target="", patch=None)
        assert "bg_budget" in ov["heartbeat"]

        # CRITICAL: 用户触发的 kind="return" 线程回传绝不能被 daily_proactive_limit=0 或 bg_budget 耗尽吞掉！
        ev_req = make_ev(text="帮我查一下这个问题", user="20002", platform="qq", group=True)
        ev_req["message_id"] = "m-user-req"
        ev_req["session_id"] = "qq:group:100246809"
        ev_req["timestamp"] = str(now[0])
        ev_req["message_info"]["group_info"] = {"group_id": "100246809"}
        plugin.messages.record(Message.from_event(ev_req))
        started_u = await plugin.agent_tool(
            action="start",
            profile="default",
            message="用户主动提问结果回传",
            source_msg_id="m-user-req",
            chat_id="qq:group:100246809",
            platform="qq",
            wait_seconds=0,
        )
        assert started_u["success"] is True, started_u
        u_tid = started_u["thread_id"]
        for _ in range(50):
            cur_u = plugin.threads.get(u_tid, "qq:group:100246809")
            if int(cur_u.get("completed_revision") or 0) >= 1:
                break
            await asyncio.sleep(0.02)
        assert int(plugin.threads.get(u_tid, "qq:group:100246809").get("completed_revision") or 0) >= 1
        now[0] += 65
        await plugin.pulse.tick()
        assert any(kw.get("reason") == "maisya:return" for _, kw in waked)

        # 2. 放宽预算，验证连续失败指数退避与达阈值自动 error_paused + 恢复清零
        await plugin.on_config_update(
            "self",
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "enabled_chats": ["qq:group:100246809", "qq:private:10001"],
                "admins": ["qq:10001"],
                "daily_bg_runs_limit": 20,
                "bg_max_consecutive_failures": 3,
                "sandbox": {"enabled": False},
            },
            2,
        )
        plugin.intents.resume(iid, "chat:qq:group:100246809", is_admin=True)
        fail_mode[0] = True

        # 第 1 次失败 -> 退避重排，failure_count=1
        now[0] += 10
        await plugin.pulse.tick()
        r_f1 = plugin.intents.get(iid)
        assert r_f1["status"] == "pending"
        assert r_f1["failure_count"] == 1
        assert "watch source 503" in r_f1["last_error"]
        delay1 = r_f1["due_at"] - now[0]

        # 第 2 次失败 -> 退避间隔严格翻倍递增
        now[0] = r_f1["due_at"] + 1
        await plugin.pulse.tick()
        r_f2 = plugin.intents.get(iid)
        assert r_f2["status"] == "pending"
        assert r_f2["failure_count"] == 2
        delay2 = r_f2["due_at"] - now[0]
        assert delay2 >= delay1 * 1.8

        # 第 3 次失败 -> 达到 bg_max_consecutive_failures=3，转入 error_paused 不再空转
        now[0] = r_f2["due_at"] + 1
        await plugin.pulse.tick()
        r_f3 = plugin.intents.get(iid)
        assert r_f3["status"] == "error_paused"
        assert r_f3["failure_count"] == 3

        # 恢复后 failure_count 清零且可再次正常跑通
        fail_mode[0] = False
        resumed = plugin.intents.resume(iid, "chat:qq:group:100246809", is_admin=True)
        assert resumed["status"] == "pending"
        assert resumed["failure_count"] == 0
    finally:
        await plugin.on_unload()
