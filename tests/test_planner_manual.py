"""docs/09 调度手册：PLANNER.md 记忆层（预设复制、版本回滚、写入闸、预设跟随升级）。"""

import asyncio
from pathlib import Path

import pytest

from maisya.abilities import Profiles
from maisya.memory import MemoryLayers
from maisya.threads import ThreadStore
from maisya.vault import Vault

PRESET = Path(__file__).resolve().parents[1] / "presets/planner/PLANNER.md"


def _layers(root, clock=lambda: 1000.0):
    root.mkdir(parents=True, exist_ok=True)
    store = ThreadStore(root / "threads.db")
    profiles = Profiles(root / "abilities/profiles")
    vault = Vault(root / "vault")
    return MemoryLayers(root, store, profiles, vault, clock=clock), (store, profiles, vault)


def test_preset_seeded_on_first_load(tmp_path):
    memory, _ = _layers(tmp_path / "maisya")
    path = tmp_path / "maisya/abilities/planner/PLANNER.md"
    assert path.read_bytes() == PRESET.read_bytes()
    doc = memory.read("planner")
    assert doc["version"] == 0
    assert doc["document"] == "PLANNER.md"
    assert "## 什么时候派线程" in doc["content"]
    assert memory.history("planner")[0]["author"] == "preset"


def test_preset_has_no_profile_ids():
    text = PRESET.read_text(encoding="utf-8")
    for pid in ("scout", "default", "digest", "maker"):
        assert pid not in text


def test_write_history_and_rollback(tmp_path):
    memory, _ = _layers(tmp_path / "maisya")
    cur = memory.read("planner")
    memory.write("planner", "## 交付\n- 结论先说重点。\n", expected_version=cur["version"])
    assert memory.read("planner")["version"] == 1
    assert [h["version"] for h in memory.history("planner")] == [1, 0]
    memory.rollback("planner", 0)
    assert memory.read("planner")["content"] == PRESET.read_text(encoding="utf-8")


def test_stale_version_rejected(tmp_path):
    memory, _ = _layers(tmp_path / "maisya")
    memory.read("planner")
    with pytest.raises(ValueError, match="版本已变化"):
        memory.write("planner", "## 交付\n- 新规则\n", expected_version=7)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("本群的人喜欢短回复", "MEMORY.md"),
        ("遇到 100246809 先查资料", "MEMORY.md"),
        ("调用时带上 sk-" + "A" * 24, "凭据"),
    ],
)
def test_guard_rejects_chat_specific_or_secret(tmp_path, content, reason):
    memory, _ = _layers(tmp_path / "maisya")
    with pytest.raises(ValueError, match=reason):
        memory.write("planner", content)


def test_length_limit(tmp_path):
    memory, _ = _layers(tmp_path / "maisya")
    memory.write("planner", "调" * 6000)
    with pytest.raises(ValueError, match="长度"):
        memory.write("planner", "调" * 6001)


def test_unedited_preset_follows_upgrade_but_edits_are_kept(tmp_path):
    root = tmp_path / "maisya"
    memory, (store, profiles, vault) = _layers(root)
    path = root / "abilities/planner/PLANNER.md"
    memory.read("planner")
    path.write_text("旧版预设手册内容", encoding="utf-8")
    MemoryLayers(root, store, profiles, vault, clock=lambda: 2000.0)
    assert path.read_bytes() == PRESET.read_bytes()

    again = MemoryLayers(root, store, profiles, vault, clock=lambda: 3000.0)
    cur = again.read("planner")
    again.write("planner", "## 交付\n- 用户自己的规则\n", expected_version=cur["version"])
    MemoryLayers(root, store, profiles, vault, clock=lambda: 4000.0)
    assert "用户自己的规则" in path.read_text(encoding="utf-8")


# ───────────── 第 2 步：注入 ─────────────

import html  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

from test_foundation import loaded  # noqa: E402

TOOLS = [{"name": "reply"}, {"name": "maisya_agent"}, {"name": "maisya_shape"}]


async def _prompt(plugin, tools=TOOLS):
    hook = await plugin.before_planner(session_id="chat", tool_definitions=tools, items=[])
    kwargs = hook["modified_kwargs"]
    return kwargs["items"][-1]["parts"][0]["text"], kwargs["tool_definitions"]


def _block(text, tag):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.S)
    return None if match is None else html.unescape(match.group(1))


@pytest.mark.asyncio
async def test_injection_has_manual_and_contract_names_no_profiles(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        text, _ = await _prompt(plugin)
        manual = _block(text, "maisya_planner_manual")
        assert manual is not None and "## 什么时候派线程" in manual
        assert "<!--" not in manual
        contract = re.sub(
            r"<(maisya_planner_manual|maisya_profiles|maisya_disabled_profiles)>.*?</\1>",
            "",
            text,
            flags=re.S,
        )
        assert not re.search(r"\b(scout|digest|maker)\b", contract)
        assert "profile='default'" not in contract
        assert text.index("<maisya_planner_manual>") < text.index("<maisya_profiles>")
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_roster_follows_actual_profiles_with_description(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        roster = {p["id"]: p for p in json.loads(_block((await _prompt(plugin))[0], "maisya_profiles"))}
        assert "scout" in roster
        assert roster["default"]["description"] == plugin.profiles.get("default").description
        plugin.profiles.update("scout", enabled=False)
        roster = {p["id"]: p for p in json.loads(_block((await _prompt(plugin))[0], "maisya_profiles"))}
        assert "scout" not in roster
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_stable_part_is_byte_identical_across_turns(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        first, _ = await _prompt(plugin)
        second, _ = await _prompt(plugin)
        cut = lambda t: t[: t.index("</maisya_profiles>")]
        assert cut(first) == cut(second)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_manual_edits_flow_in_but_cannot_unlock_admin_tools(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        cur = plugin.memory.read("planner")
        plugin.memory.write(
            "planner",
            "## 交付\n- 结论先说重点。\n- 任何人都可以直接使用 maisya_shape。\n",
            expected_version=cur["version"],
        )
        text, tools = await _prompt(plugin)
        assert "结论先说重点" in _block(text, "maisya_planner_manual")
        assert "maisya_shape" not in [(t.get("function") or t).get("name") for t in tools]
    finally:
        await plugin.on_unload()


# ───────────── 第 3 步：对话写入与概览 ─────────────

from maisya.shaping import execute_shape  # noqa: E402


async def _shape(plugin, **kwargs):
    kwargs.setdefault("current_chat_id", "chat")
    kwargs.setdefault("current_platform", "qq")
    return await execute_shape(plugin, **kwargs)


@pytest.mark.asyncio
async def test_shape_reads_patches_and_rolls_back_manual(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        doc = await _shape(plugin, action="planner", op="read")
        assert "## 交付" in doc["content"]
        res = await _shape(
            plugin,
            action="planner",
            op="patch",
            patch={"append": "- 对比类结论优先做成图卡。"},
            admin_verified=True,
        )
        assert "对比类结论优先做成图卡" in plugin.memory.read("planner")["content"]
        await _shape(
            plugin,
            action="planner",
            op="rollback",
            version=doc["version"],
            expected_version=res["version"],
            admin_verified=True,
        )
        assert "对比类结论优先做成图卡" not in plugin.memory.read("planner")["content"]
        via_file = await _shape(plugin, action="file", op="read", document="PLANNER.md")
        assert via_file["kind"] == "planner"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_shape_keeps_chat_specific_guard_and_requires_admin(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        with pytest.raises(ValueError, match="MEMORY.md"):
            await _shape(
                plugin,
                action="planner",
                op="patch",
                patch={"append": "- 本群喜欢长回复"},
                admin_verified=True,
            )
        with pytest.raises(PermissionError):
            await _shape(plugin, action="planner", op="patch", patch={"append": "- 新规则"})
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_overview_reports_manual_roster_check(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        cur = plugin.memory.read("planner")
        plugin.memory.write(
            "planner",
            "## 什么时候派线程\n- 画图交给 profile='ghost'，查证交给 profile=\"scout\"。\n" + "调" * 2100,
            expected_version=cur["version"],
        )
        overview = await _shape(plugin, action="overview")
        manual = overview["planner_manual"]
        assert manual["unknown_profiles"] == ["ghost"]
        assert manual["over_suggested_length"] is True
        assert manual["version"] == cur["version"] + 1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_admin_guidance_points_corrections_to_manual(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        plugin.authorize_admin_source = lambda *a, **k: True
        plugin._turn_sources["chat"] = ["m-admin"]
        plugin._pending_sources["chat"] = ["m-admin"]
        text, _ = await _prompt(plugin)
        assert "PLANNER.md" in text
    finally:
        await plugin.on_unload()


# ───────────── 第 4 步：管理接口与写闸校验 ─────────────


@pytest.mark.asyncio
async def test_management_planner_read_write_rollback_and_guard(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        prof_res = await plugin.manage_read("profiles", "{}")
        assert prof_res["success"] is True
        assert prof_res["planner_summary"]["version"] == 0
        assert prof_res["planner_summary"]["unknown_profiles"] == []

        res = await plugin.manage_read("planner", "{}")
        assert res["success"] is True
        assert res["manual"]["version"] == 0
        assert "## 什么时候派线程" in res["manual"]["content"]
        assert isinstance(res["manual"]["history"], list)
        roster_ids = {p["id"] for p in res["roster"]}
        assert {"default", "scout"} <= roster_ids
        assert "digest" not in roster_ids
        preview = res["injection_preview"]
        assert "<maisya_planner_manual>" in preview["text"]
        assert "<maisya_profiles>" in preview["text"]
        assert preview["chars"] == len(preview["text"])
        assert preview["estimated_tokens"] > 0
        assert res["check"]["unknown_profiles"] == []
        assert res["pending_proposals"] == []

        # 启用 digest 与停用 scout 后，名册与注入预览即时同步
        plugin.profiles.update("digest", enabled=True)
        plugin.profiles.update("scout", enabled=False)
        after_disable = await plugin.manage_read("planner", "{}")
        after_ids = {p["id"] for p in after_disable["roster"]}
        assert "digest" in after_ids
        assert "scout" not in after_ids
        assert "scout" not in after_disable["injection_preview"]["text"]

        # WebUI 写 PLANNER.md 必须走非 allow_custom 写入闸，拒绝具体会话信息
        rejected = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "planner",
                    "content": "## 交付\n- 本群 100246809 的固定习惯。",
                    "expected_version": 0,
                }
            ),
        )
        assert rejected["success"] is False
        assert "MEMORY.md" in rejected["error"]

        # 正常编辑、版本冲突检查与回滚
        saved = await plugin.manage_write(
            "asset",
            json.dumps(
                {
                    "kind": "planner",
                    "content": "## 交付\n- 结论控制在三句内。",
                    "expected_version": 0,
                }
            ),
        )
        assert saved["success"] is True
        assert saved["version"] == 1

        stale = await plugin.manage_write(
            "asset",
            json.dumps({"kind": "planner", "content": "## 交付\n- 旧版本覆盖", "expected_version": 0}),
        )
        assert stale["success"] is False

        rb = await plugin.manage_write(
            "rollback",
            json.dumps({"kind": "planner", "version": 0, "expected_version": 1}),
        )
        assert rb["success"] is True
        assert rb["content"] == PRESET.read_text(encoding="utf-8")
    finally:
        await plugin.on_unload()


# ───────────── 第 5 步：WebUI 首位固定「MaiBot（调度）」卡片 ─────────────


@pytest.mark.asyncio
async def test_webui_planner_first_card_edit_rollback_and_proposals_playwright(tmp_path):
    from playwright.async_api import async_playwright, expect

    plugin = await loaded(tmp_path, ["qq:10001"])
    await plugin.manage_write("onboarding_skip", "{}")
    prop = plugin.memory.save_skill_proposal(
        thread_id="planner:sig1",
        revision=1,
        profile_id="planner",
        skill_id="planner-manual",
        title="先平行后汇合",
        description="MaiBot 调度手册经验草稿",
        content="- 多路互不依赖查证同轮并行派出，结果汇总后统一交付。",
        signals=["same_turn_followup_dispatch"],
        rationale="同一请求先串行再补派，建议改用并行",
    )

    async def request(kind, name, arguments):
        args = dict(arguments or {})
        if kind == "query":
            if name == "read":
                return await plugin.manage_read(**args)
            if name == "event":
                return await plugin.console_event(**args)
            return await plugin.console_query(**args)
        if name == "write":
            return await plugin.manage_write(**args)
        if name == "note":
            return await plugin.console_send(**args)
        return await plugin.console_stop(**args)

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            errors: list[str] = []
            try:
                page = await browser.new_page(viewport={"width": 1360, "height": 900})
                page.on("pageerror", lambda err: errors.append(str(err)))
                await page.expose_function("hostRpc", request)
                doc = Path("webui/now.html").read_text(encoding="utf-8")
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "abilities" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")

                # 首位固定「MaiBot（调度）」，但默认仍停留在 default 表单以免破坏既有习惯
                first_card = page.locator("#pr-roster .ab-card").first
                await expect(first_card).to_have_attribute("data-pick-profile-id", "__planner__")
                await expect(first_card).to_contain_text("MaiBot（调度）")
                await expect(page.locator("#pr-id")).to_have_value("default")

                # 点击「MaiBot（调度）」：切到调度手册视图、禁用删/导、展示名册与注入预览
                await first_card.click()
                await expect(page.locator("#pr-planner-panel")).to_be_visible()
                await expect(page.locator("#pr-btn-delete")).to_be_disabled()
                await expect(page.locator("#pr-btn-export")).to_be_disabled()
                await expect(page.locator("#pl-md")).to_have_value(re.compile(r"## 什么时候派线程"))
                await expect(page.locator("#pl-roster-preview")).to_contain_text("scout")
                await expect(page.locator("#pl-inject-preview")).to_contain_text("<maisya_planner_manual>")
                await expect(page.locator("#pl-token-tag")).to_contain_text("tokens")

                # 确认待审调度草稿：直接写入 PLANNER.md，不建技能目录
                await expect(page.locator("#pl-proposals-list")).to_contain_text("先平行后汇合")
                await page.locator(f'[data-pl-prop-confirm="{prop["proposal_id"]}"]').click()
                await expect(page.locator("#pr-feedback")).to_contain_text("已采纳调度经验")
                assert "多路互不依赖查证同轮并行派出" in plugin.memory.read("planner")["content"]
                assert not (plugin.data_root / "abilities/skills/planner-manual").exists()

                # 直接编辑保存与版本回滚
                await page.evaluate("document.getElementById('pr-feedback').textContent = ''")
                await page.locator("#pl-md").fill("## 交付\n- 手动修订测试版本。")
                await page.locator("#pr-btn-save").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("已保存调度手册")
                assert plugin.memory.read("planner")["version"] == 2
                await expect(page.locator('#pl-hist-list [data-pl-rb="0"]')).to_be_visible()
                await page.locator('#pl-hist-list [data-pl-rb="0"]').click()
                await expect(page.locator("#pr-feedback")).to_contain_text("已回滚")
                assert plugin.memory.read("planner")["content"] == PRESET.read_text(encoding="utf-8")

                # 调度草稿不得混入「能力 -> 技能」待确认技能列表
                plugin.memory.save_skill_proposal(
                    thread_id="planner:sig2",
                    revision=1,
                    profile_id="planner",
                    skill_id="planner-manual",
                    title="二号调度草稿",
                    description="MaiBot 调度手册经验草稿",
                    content="- 长数据先做结论卡片。",
                    signals=["steer_twice"],
                    rationale="两次转向",
                )
                await page.locator('[data-ab-sub="skills"]').click()
                await expect(page.locator("#sk-proposals-list")).not_to_contain_text("二号调度草稿")

                assert not errors
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


# ───────────── 第 7 步：第二阶段调度信号自动沉淀 ─────────────


@pytest.mark.asyncio
async def test_planner_signal_steer_twice_distills_abstract_draft_without_auto_apply(tmp_path):
    plugin = await loaded(tmp_path, ["qq:10001"])
    captured_prompts = []

    async def capability(name, **kwargs):
        if name == "llm.generate_with_tools":
            prompt_str = json.dumps(kwargs.get("prompt"), ensure_ascii=False)
            if "调度手册" in prompt_str or "调度轨迹" in prompt_str:
                captured_prompts.append(kwargs)
                return {
                    "success": True,
                    "response": json.dumps(
                        {
                            "title": "复杂调研先确认约束",
                            "lesson": "复杂检索前先收敛时间窗与信源口径，减少执行中途多次转向。",
                            "rationale": "同一线程在一次委托中被连续转向两次",
                        },
                        ensure_ascii=False,
                    ),
                    "tool_calls": [],
                }
            return {"success": True, "response": "已完成初始检索。", "tool_calls": []}
        return {"success": True}

    plugin.ctx.call_capability = capability
    plugin.memory.write("memory", "私密群梗：老王每周五只喝乌龙茶", chat_id="chat", allow_custom=True)
    try:
        started = await plugin.agent_tool(
            action="start",
            profile="default",
            message="请查技术动态（秘语口令：老王喝乌龙茶）",
            wait_seconds=10,
            chat_id="chat",
            platform="qq",
        )
        tid = started["thread_id"]
        # 连续两次显式 steer
        await plugin.agent_tool(
            action="send", thread_id=tid, message="改查本周前端框架动态", steer=True, wait_seconds=0, chat_id="chat", platform="qq"
        )
        await plugin.agent_tool(
            action="send", thread_id=tid, message="再缩小到打包构建工具", steer=True, wait_seconds=0, chat_id="chat", platform="qq"
        )
        await plugin.engine.drain()
        assert plugin.runtime.learning is not None
        await plugin.runtime.learning.drain()

        assert len(captured_prompts) == 1
        prompt_dump = json.dumps(captured_prompts[0]["prompt"], ensure_ascii=False)
        assert "steer_twice" in prompt_dump
        assert "老王" not in prompt_dump and "乌龙茶" not in prompt_dump
        assert "秘语口令" not in prompt_dump
        assert captured_prompts[0]["tools"] == []
        assert captured_prompts[0]["max_tokens"] <= 500

        # 绝不自动修改 PLANNER.md
        assert plugin.memory.read("planner")["version"] == 0
        pending = plugin.memory.list_skill_proposals(status="pending")
        assert len(pending) == 1
        assert pending[0]["profile_id"] == "planner"
        assert "收敛时间窗" in pending[0]["content"]

        # 管理员在对话确认后写入 PLANNER.md
        confirmed = await _shape(
            plugin,
            action="skill_proposal",
            op="confirm",
            target=pending[0]["proposal_id"],
            admin_verified=True,
        )
        assert confirmed["version"] == 1
        assert "收敛时间窗" in plugin.memory.read("planner")["content"]
        assert not (plugin.data_root / "abilities/skills/planner-manual").exists()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_planner_signal_stop_switch_and_sequential_followup_without_parallel_false_positive(tmp_path):
    plugin = await loaded(tmp_path, ["qq:10001"])
    now = [1_700_000_000.0]
    plugin.intents.clock = lambda: now[0]
    captured = []
    gate = asyncio.Event()

    async def capability(name, **kwargs):
        if name == "llm.generate_with_tools":
            prompt_str = json.dumps(kwargs.get("prompt"), ensure_ascii=False)
            if "调度手册" in prompt_str or "调度轨迹" in prompt_str:
                captured.append(json.loads(kwargs["prompt"][-1]["content"]))
                return {
                    "success": True,
                    "response": json.dumps(
                        {
                            "title": "先核名册再指派 / 独立任务并行",
                            "lesson": "联网核查类需求直接指派查证助手，互不依赖的多路子任务同轮并行派出。",
                            "rationale": "识别到换档或同请求串行补派",
                        },
                        ensure_ascii=False,
                    ),
                    "tool_calls": [],
                }
            if "并行阻塞测试" in prompt_str:
                await gate.wait()
            return {"success": True, "response": "done", "tool_calls": []}
        return {"success": True}

    plugin.ctx.call_capability = capability
    try:
        # 1. stop 后 10 分钟内换档案重新 start -> 触发 stop_and_switch_profile
        s1 = await plugin.agent_tool(
            action="start", profile="default", message="去查一个冷门库", wait_seconds=10, chat_id="chat", platform="qq"
        )
        await plugin.agent_tool(action="stop", thread_id=s1["thread_id"], chat_id="chat", platform="qq")
        now[0] += 120
        s2 = await plugin.agent_tool(
            action="start", profile="scout", message="改由查证助手查这个库", wait_seconds=10, chat_id="chat", platform="qq"
        )
        await plugin.engine.drain()
        assert plugin.runtime.learning is not None
        await plugin.runtime.learning.drain()
        assert any(c.get("signal") == "stop_and_switch_profile" for c in captured)

        # 2. 同一请求串行补派（前序已完成再对同 source_msg_id 补派）-> 触发 same_turn_followup_dispatch
        from test_foundation import event as make_ev

        ev_req = make_ev("一次查 A 和 B", user="10001", group=False, platform="qq")
        ev_req["message_id"] = "msg-seq-followup-1"
        await plugin.after_receive(message=ev_req)
        req_msg_id = ev_req["message_id"]
        now[0] += 10
        r_seq1 = await plugin.agent_tool(
            action="start", profile="scout", message="先查 A", wait_seconds=10, source_msg_id=req_msg_id, chat_id="chat", platform="qq"
        )
        assert r_seq1["success"] is True
        now[0] += 5
        r_seq2 = await plugin.agent_tool(
            action="start", profile="default", message="忘了还要 B，再派一次", wait_seconds=10, source_msg_id=req_msg_id, chat_id="chat", platform="qq"
        )
        assert r_seq2["success"] is True
        await plugin.engine.drain()
        assert plugin.runtime.learning is not None
        await plugin.runtime.learning.drain()
        assert any(c.get("signal") == "same_turn_followup_dispatch" for c in captured)

        # 3. 并行 fan-out（wait_seconds=0 同时挂起两个 active 线程）绝不误报 same_turn_followup_dispatch
        before_count = len(captured)
        ev_par = make_ev("同时查 C 和 D", user="10001", group=False, platform="qq")
        ev_par["message_id"] = "msg-par-fanout-2"
        await plugin.after_receive(message=ev_par)
        now[0] += 20
        await plugin.agent_tool(
            action="start", profile="default", message="并行阻塞测试 C", wait_seconds=0, source_msg_id=ev_par["message_id"], chat_id="chat", platform="qq"
        )
        await plugin.agent_tool(
            action="start", profile="scout", message="并行阻塞测试 D", wait_seconds=0, source_msg_id=ev_par["message_id"], chat_id="chat", platform="qq"
        )
        gate.set()
        await plugin.engine.drain()
        assert plugin.runtime.learning is not None
        await plugin.runtime.learning.drain()
        assert len(captured) == before_count
    finally:
        gate.set()
        await plugin.on_unload()
