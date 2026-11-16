"""TDD 验收套件：五页工作台（现场|产物|能力|手账|设置）、三预设角色与角色独立 MEMORY.md、
任务后自动复盘提炼技能（首次草稿预览确认 / 已有技能自动补经验）、极简别名密码保险箱与单一心跳对话塑形。
"""

from __future__ import annotations

import json
from pathlib import Path
import time

import pytest
from playwright.async_api import async_playwright, expect

from maisya.abilities import BUILTIN_PROFILES, Profiles
from maisya.learning import Learning
from maisya.shaping import execute_shape
from test_foundation import loaded


HOST_CSP_META = (
    "<head><meta http-equiv=\"Content-Security-Policy\" content=\""
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'none'; img-src data: blob:;\">"
)


def _wrap_csp(html_doc: str) -> str:
    return html_doc.replace("<head>", HOST_CSP_META, 1)


def _wire_rpc(plugin):
    inflight = 0
    peak = 0

    async def rpc(kind: str, name: str, args: dict | None):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            payload = dict(args or {})
            if kind == "query":
                if name == "read":
                    return await plugin.manage_read(**payload)
                if name == "event":
                    return await plugin.console_event(**payload)
                return await plugin.console_query(**payload)
            if name == "write":
                return await plugin.manage_write(**payload)
            if name == "note":
                return await plugin.console_send(**payload)
            return await plugin.console_stop(**payload)
        finally:
            inflight -= 1

    return rpc, lambda: peak


@pytest.mark.asyncio
async def test_builtin_presets_scout_default_digest_and_per_role_memory(tmp_path):
    """开箱内置 default / scout / digest 三个预设角色（maker 映射至 default），且每个角色拥有独立 MEMORY.md。"""
    repo_presets = Path(__file__).resolve().parent.parent / "presets" / "profiles"
    assert BUILTIN_PROFILES == frozenset({"default", "scout", "digest"})
    for pid in ("default", "scout", "digest"):
        assert (repo_presets / pid / "profile.toml").is_file()
        assert (repo_presets / pid / "PROFILE.md").is_file()
    assert not (repo_presets / "maker").exists()

    profiles = Profiles(tmp_path / "profiles")
    assert [p.id for p in profiles.list_profiles()] == ["default", "digest", "scout"]
    assert profiles.get("maker").id == "default"
    assert profiles.get("scout").id == "scout"
    assert profiles.get("digest").id == "digest"
    for protected_id in ("default", "scout", "digest"):
        with pytest.raises(ValueError):
            profiles.delete(protected_id)

    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        # 角色独立 MEMORY.md 与 NOTES.md 互不干扰，且与会话 MEMORY.md 隔离
        r0 = plugin.memory.read("role_memory", target="digest")
        assert r0["version"] == 0 and r0["content"] == ""
        w1 = plugin.memory.write(
            "role_memory",
            "# 简报助手长期记忆\n- 重点关注独立游戏与 Rust 生态一手发布。\n",
            target="digest",
            expected_version=0,
        )
        assert w1["version"] == 1
        assert (plugin.profiles.path / "digest" / "MEMORY.md").is_file()

        plugin.memory.write(
            "notes",
            "- 抓取 RSS 超时时回退官方 GitHub Releases。",
            target="digest",
            expected_version=0,
            allow_custom=True,
        )
        plugin.memory.write(
            "memory",
            "本群每周五晚汇总讨论。",
            chat_id="qq:group:100246809",
            expected_version=0,
        )

        methods_txt = plugin.memory.methods(plugin.profiles.get("digest"))
        assert "简报助手长期记忆" in methods_txt
        assert "抓取 RSS 超时时回退" in methods_txt
        assert "本群每周五晚汇总讨论" not in methods_txt

        # 保险箱明文密钥禁止写入角色 MEMORY.md
        plugin.vault.set("leak_check", "sk-TestSecretValueForRoleMemory123456")
        with pytest.raises(ValueError, match="凭据"):
            plugin.memory.write(
                "role_memory",
                "泄露测试 sk-TestSecretValueForRoleMemory123456",
                target="digest",
                expected_version=1,
            )

        # 现场上下文方块图包含「角色长期记忆」条目
        row = plugin.threads.create(
            plugin.profiles.get("digest"),
            "测试简报上下文构成",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:10001",
        )
        snap = await plugin.console_query(thread_id=row["thread_id"])
        labels = [it["label"] for it in snap["selected"]["context"]["items"]]
        assert "角色长期记忆" in labels
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_auto_distillation_first_create_stages_draft_subsequent_patches_skill(tmp_path):
    """任务完成后默认自动复盘：无归口技能时生成待预览保存的新技能草稿；已有归口技能时自动更新补经验。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        llm_mode = "new_skill"

        async def capability(name, **kwargs):
            if name == "llm.generate_with_tools":
                if llm_mode == "new_skill":
                    return {
                        "success": True,
                        "response": json.dumps(
                            {
                                "op": "create",
                                "skill_id": "log-triage",
                                "title": "日志排障归档规范",
                                "description": "当需要排查多服务报错日志并归档根因时使用。",
                                "rationale": "首次出现多步日志定位与去重流程，建议创建类级技能。",
                                "note": "先按时间戳对齐网关与应用日志，再过滤重复健康检查噪点。",
                                "content": (
                                    "---\n"
                                    'title: "日志排障归档规范"\n'
                                    'description: "当需要排查多服务报错日志并归档根因时使用。"\n'
                                    "---\n\n"
                                    "# 日志排障归档规范\n\n"
                                    "## 执行步骤\n"
                                    "1. 对齐多服务日志时区与请求追踪号。\n"
                                    "2. 剔除健康检查噪点后汇总首个异常栈。\n"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                        "tool_calls": [],
                    }
                return {
                    "success": True,
                    "response": json.dumps(
                        {
                            "op": "patch",
                            "skill_id": "log-triage",
                            "lesson": "遇到压缩轮转日志时先读取索引尾部 200 行定位时间窗口，避免全量解压超时。",
                            "note": "先读轮转日志尾部时间窗口再定向解压。",
                        },
                        ensure_ascii=False,
                    ),
                    "tool_calls": [],
                }
            return {"success": True}

        plugin.ctx.call_capability = capability
        learning = Learning(plugin.ctx, plugin.memory)

        # 第 1 次：新领域复杂任务完成 -> 自动生成待确认草稿，尚未直接创建技能文件
        prof = plugin.profiles.get("default")
        t1 = plugin.threads.create(
            prof,
            "排查网关 502 日志并生成根因归档",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:10001",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                t1["thread_id"],
                "tools",
                1,
                {
                    "calls": [
                        {"id": "c1", "function": {"name": "workspace_file", "arguments": {}}},
                        {"id": "c2", "function": {"name": "workspace_file", "arguments": {}}},
                    ],
                    "results": [
                        {"call_id": "c1", "value": {"error": "encoding mismatch"}},
                        {"call_id": "c2", "value": {"ok": True, "text": "parsed"}},
                    ],
                },
            )
            db.execute(
                "UPDATE agent_threads SET state='done', completed_revision=1, result=? WHERE thread_id=?",
                (json.dumps({"text": "已完成 502 日志对齐与根因汇总。"}, ensure_ascii=False), t1["thread_id"]),
            )
        row1 = plugin.threads.get(t1["thread_id"], "qq:group:100246809")
        await learning.schedule(row1)
        await learning.drain()

        assert "log-triage" not in plugin.memory.skills()
        sk_view = await plugin.manage_read("skills")
        proposals = sk_view.get("pending_proposals") or []
        assert any(p["skill_id"] == "log-triage" for p in proposals)
        prop = next(p for p in proposals if p["skill_id"] == "log-triage")

        # 用户预览并确认保存首次创建的技能
        confirmed = await plugin.manage_write(
            "confirm_skill_proposal",
            json.dumps(
                {
                    "proposal_id": prop["proposal_id"],
                    "bind_profiles": ["default"],
                },
                ensure_ascii=False,
            ),
        )
        assert confirmed["success"], confirmed
        assert "log-triage" in plugin.memory.skills()
        assert "log-triage" in plugin.profiles.get("default").skills

        # 第 2 次：绑定该技能后再次完成同类排障 -> 补丁进入待确认草稿队列并保留线程来源，确认后才写入 log-triage
        llm_mode = "patch_existing"
        prof_updated = plugin.profiles.get("default")
        t2 = plugin.threads.create(
            prof_updated,
            "排查归档压缩轮转日志中的偶发超时",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:10001",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(
                db,
                t2["thread_id"],
                "tools",
                1,
                {
                    "calls": [
                        {"id": "c3", "function": {"name": "workspace_file", "arguments": {}}},
                        {"id": "c4", "function": {"name": "workspace_file", "arguments": {}}},
                    ],
                    "results": [
                        {"call_id": "c3", "value": {"error": "timeout"}},
                        {"call_id": "c4", "value": {"ok": True, "text": "tail ok"}},
                    ],
                },
            )
            db.execute(
                "UPDATE agent_threads SET state='done', completed_revision=1, result=? WHERE thread_id=?",
                (json.dumps({"text": "已定位压缩日志中的超时窗口。"}, ensure_ascii=False), t2["thread_id"]),
            )
        row2 = plugin.threads.get(t2["thread_id"], "qq:group:100246809")
        await learning.schedule(row2)
        await learning.drain()

        assert plugin.memory.read("skill", target="log-triage")["version"] == 1
        patch_props = [
            p
            for p in plugin.memory.list_skill_proposals(status="pending")
            if p["skill_id"] == "log-triage"
        ]
        assert patch_props and any(
            t2["thread_id"] in json.dumps(p, ensure_ascii=False) for p in patch_props
        )
        confirmed2 = await plugin.manage_write(
            "confirm_skill_proposal",
            json.dumps({"proposal_id": patch_props[0]["proposal_id"]}, ensure_ascii=False),
        )
        assert confirmed2["success"], confirmed2
        updated_skill = plugin.memory.read("skill", target="log-triage")
        assert "压缩轮转日志" in updated_skill["content"]
        assert updated_skill["version"] >= 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_vault_alias_password_only_no_secret_echo_and_mention_alias_injection(tmp_path):
    """密码保险箱仅用别名+密码保存，查看仅返回别名与保存时间，管理员提别名即可注入执行且自动脱敏。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        secret_val = "ghp_MyUltraSecretTokenValue99887766"
        res = await plugin.manage_write(
            "vault_set",
            json.dumps({"handle": "github_pat", "value": secret_val}),
        )
        assert res["success"], res
        raw_dump = json.dumps(res, ensure_ascii=False)
        assert secret_val not in raw_dump

        item_info = await plugin.manage_read("vault_item", json.dumps({"handle": "github_pat"}))
        assert item_info["success"] is True
        assert item_info["handle"] == "github_pat"
        assert float(item_info["updated_at"]) > 0
        assert secret_val not in json.dumps(item_info, ensure_ascii=False)
        assert "value" not in item_info and "secret" not in item_info

        # default 角色无需手改 profile.toml，管理员轮次仅在 exec_command 提到别名即自动挂载并脱敏输出
        cur_set = await plugin.manage_read("settings")
        await plugin.manage_write(
            "settings_update",
            json.dumps(
                {
                    "settings": {"permission_mode": "autonomous"},
                    "expected_version": cur_set["version"],
                }
            ),
        )
        prof = plugin.profiles.get("default")
        assert not prof.secrets
        row = plugin.threads.create(
            prof,
            "使用 github_pat 检查连通",
            chat_id="qq:private:10001",
            platform="qq",
            user_id="qq:10001",
        )
        exec_out = await plugin.runtime.call(
            "exec_command",
            {
                "command": "python3 -c \"import os; v=os.environ.get('MAISYA_SECRET_GITHUB_PAT',''); print('LEN=' + str(len(v)), v)\"",
                "secrets": ["github_pat"],
            },
            {
                **row,
                "grant": {"admin_direct": True},
            },
        )
        out_str = json.dumps(exec_out, ensure_ascii=False)
        assert f"LEN={len(secret_val)}" in out_str
        assert secret_val not in out_str
        assert "[REDACTED]" in out_str
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_single_heartbeat_and_conversational_shaping_user_scenarios(tmp_path):
    """单一心跳设计与管理员聊天一站式配置（setup / 心跳微调 / 角色记忆 / 资讯源 / 回滚）。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        plugin.messages.upsert_route(
            "sid_indie", platform="qq", chat_type="group", group_id="100246809"
        )

        # 1) 管理员私聊一句话 setup：开群白名单 + 写群备忘 + 配免打扰 + 订简报主题 + 排每日定时
        setup_res = await execute_shape(
            plugin,
            action="setup",
            target_chat="qq:group:100246809",
            patch={
                "memory": "本群关注独立游戏、像素银河城与合作射击，不看买量手游。",
                "sleep_window": "23:00-08:00",
                "heartbeat_enabled": True,
                "heartbeat_interval_minutes": 60,
                "heartbeat_append": "- 闲暇浏览独立游戏开发日志与 Steam 新品节动态",
                "digest_topics": ["独立游戏", "Steam 新品"],
                "daily_at": "09:00",
                "profile": "digest",
            },
            admin_verified=True,
        )
        assert setup_res["kind"] == "setup"
        assert "qq:group:100246809" in plugin.settings.enabled_chats
        assert plugin.settings.sleep_window == "23-08"
        assert plugin.settings.heartbeat_interval_minutes == 60
        assert "独立游戏开发日志" in plugin.memory.read("heartbeat")["content"]
        assert "不看买量手游" in plugin.memory.read("memory", chat_id="qq:group:100246809")["content"]
        assert plugin.profiles.get("digest").enabled is True
        intents = plugin.intents.list_all(status="pending", limit=20)
        assert any("digest" in json.dumps(it, ensure_ascii=False) for it in intents)

        # 2) 对话直接给角色 MEMORY.md 补一条长期记忆并支持回滚
        rm_res = await execute_shape(
            plugin,
            action="role_memory",
            op="patch",
            target="digest",
            patch={"append": "- 优先采用开发者一手日志与实机帧率数据。"},
            admin_verified=True,
        )
        assert "一手日志" in plugin.memory.read("role_memory", target="digest")["content"]
        await execute_shape(
            plugin,
            action="role_memory",
            op="rollback",
            target="digest",
            version=rm_res["previous_version"],
            expected_version=rm_res["version"],
            admin_verified=True,
        )
        assert "一手日志" not in plugin.memory.read("role_memory", target="digest")["content"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_five_page_webui_abilities_memory_vault_and_heartbeat_playwright(tmp_path):
    """Playwright 验证五页顶栏（现场|产物|能力|手账|设置）、能力三子栏、手账三册、极简密码保险箱与单一心跳。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        plugin.messages.upsert_route("sid_g1", platform="qq", chat_type="group", group_id="100246809")
        cur_set = await plugin.manage_read("settings")
        await plugin.manage_write(
            "settings_update",
            json.dumps(
                {
                    "settings": {"enabled_chats": ["qq:group:100246809"]},
                    "expected_version": cur_set["version"],
                }
            ),
        )
        # 预置一条自动复盘生成的新技能待确认草稿
        prop = plugin.memory.save_skill_proposal(
            thread_id="th_demo_1",
            revision=1,
            profile_id="default",
            skill_id="weekly-retro",
            title="周度复盘整理法",
            description="当需要汇总群聊一周重点议题时使用。",
            content="---\ntitle: \"周度复盘整理法\"\ndescription: \"当需要汇总群聊一周重点议题时使用。\"\n---\n\n# 周度复盘整理法\n\n- 按议题聚类并保留原消息时间点。\n",
            signals=["2 步工具链", "已产出结论"],
            rationale="新出现的周度汇总工作流，等待确认保存。",
        )
        assert prop["proposal_id"]

        rpc, get_peak = _wire_rpc(plugin)
        doc = _wrap_csp(Path("webui/now.html").read_text(encoding="utf-8"))
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                page = await browser.new_page(viewport={"width": 1320, "height": 900})
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "now" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")

                # 1) 顶栏与移动底栏严格为 5 项：现场 | 产物 | 能力 | 手账 | 设置
                nav_texts = [t.strip() for t in await page.locator(".ms-nav [data-nav]").all_inner_texts()]
                assert nav_texts == ["现场", "产物", "能力", "手账", "设置"]

                # 2) 能力页：角色（default/scout/digest + 角色记忆 MEMORY.md）| 技能（待确认草稿预览创建）| 外部连接
                await page.locator('[data-nav="abilities"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("能力")
                for pid in ("default", "scout", "digest"):
                    await expect(page.locator(f'#pr-roster [data-pick-profile-id="{pid}"]')).to_be_visible()
                await page.locator('#pr-roster [data-pick-profile-id="digest"]').click()
                await page.locator("#pr-adv-toggle").click()
                await page.locator('[data-pr-tab="role_memory"]').click()
                await page.locator("#pr-role-mem-md").fill("# 简报角色记忆\n- 偏好硬核独立游戏评测。")
                await page.locator("#pr-role-mem-save").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("角色记忆已保存")
                assert "硬核独立游戏评测" in plugin.memory.read("role_memory", target="digest")["content"]

                # 切到「能力 -> 技能」子栏，预览并保存自动复盘的新技能草稿
                await page.locator('[data-ab-sub="skills"]').click()
                await expect(page.locator("#sk-proposals-sec")).to_be_visible()
                await expect(page.locator("#sk-proposals-list")).to_contain_text("周度复盘整理法")
                await page.locator(f'[data-preview-proposal="{prop["proposal_id"]}"]').click()
                await expect(page.locator("#sk-drawer-scrim")).to_be_visible()
                await page.locator("#sk-drawer-save").click()
                await expect(page.locator("#sk-feedback")).to_contain_text("技能已保存")
                assert "weekly-retro" in plugin.memory.skills()

                # 3) 独立手账页：会话备忘 | 自身近况 | 心跳说明
                await page.locator('[data-nav="memory"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("手账")
                await expect(page.locator("#mem-editor")).to_be_visible()
                await page.locator("#mem-editor").fill("# 群聊备忘\n- 周五晚固定联机。")
                await page.locator("#mem-save").click()
                await expect(page.locator("#mem-feedback")).to_contain_text("已保存")
                assert "周五晚固定联机" in plugin.memory.read("memory", chat_id="qq:group:100246809")["content"]

                await page.locator('[data-mem-kind="heartbeat"]').click()
                await page.locator("#mem-hb-template").click()
                hb_val = await page.locator("#mem-editor").input_value()
                assert len(hb_val.strip()) > 10
                await page.locator("#mem-save").click()
                await expect(page.locator("#mem-feedback")).to_contain_text("已保存")

                # 4) 设置页：别名+密码极简保险箱（点击查看仅显示别名和保存日期）+ 单一心跳直观设置
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator("#ms-page-title")).to_have_text("设置")
                assert await page.locator("#set-vault-label").count() == 0
                assert await page.locator("#set-vault-domain").count() == 0
                await page.locator("#set-vault-handle").fill("demo_token")
                await page.locator("#set-vault-secret").fill("SecretVal_NeverShow_12345")
                await page.locator("#set-vault-save").click()
                await expect(page.locator("#set-vault-list")).to_contain_text("demo_token")
                assert await page.locator("#set-vault-secret").input_value() == ""

                await page.locator('[data-vault-inspect="demo_token"]').click()
                await expect(page.locator("#set-vault-detail")).to_be_visible()
                detail_txt = await page.locator("#set-vault-detail").inner_text()
                assert "demo_token" in detail_txt
                assert "保存" in detail_txt
                assert "SecretVal_NeverShow_12345" not in detail_txt

                # 单一心跳直观设置（三档选择 + 高级覆盖）
                await expect(page.locator("#set-hb-mode-seg")).to_be_visible()
                await expect(page.locator("#set-per-chat-limit")).to_be_visible()
                await page.locator("#set-hb-adv summary").click()
                await expect(page.locator("#set-hb-interval")).to_be_visible()
                await expect(page.locator("#set-longing")).to_be_visible()
                assert await page.locator("img, iframe").count() == 0
                assert not errors and get_peak() <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
