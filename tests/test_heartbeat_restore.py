"""docs/08 心跳恢复方案的验收测试。"""

import pytest
from pydantic import ValidationError

from maisya.config import Settings, apply_heartbeat_compat


# ───────────── 第 1 步：档位、高级覆盖与旧配置迁移 ─────────────


def test_defaults_are_off_with_two_layer_quota():
    s = Settings()
    assert s.heartbeat_mode == "off"
    assert s.heartbeat_enabled is False
    assert s.heartbeat_interval_minutes == 0
    assert s.longing_quiet_hours == 0
    assert s.daily_proactive_limit == 3
    assert s.per_chat_daily_proactive_limit == 1
    dumped = s.model_dump()
    assert dumped["heartbeat_mode"] == "off"
    assert "heartbeat_enabled" not in dumped


@pytest.mark.parametrize(
    ("raw", "mode"),
    [("关", "off"), ("off", "off"), ("克制", "gentle"), ("on", "gentle"), ("活泼", "lively"), ("LIVELY", "lively")],
)
def test_mode_aliases(raw, mode):
    assert Settings(heartbeat_mode=raw).heartbeat_mode == mode


def test_invalid_mode_rejected():
    with pytest.raises(ValidationError):
        Settings(heartbeat_mode="sometimes")


@pytest.mark.parametrize("value", [0, 30, 1440])
def test_interval_override_accepts_zero_or_30_to_1440(value):
    assert Settings(heartbeat_interval_minutes=value).heartbeat_interval_minutes == value


@pytest.mark.parametrize("value", [5, 29, 1441])
def test_interval_override_rejects_out_of_range(value):
    with pytest.raises(ValidationError):
        Settings(heartbeat_interval_minutes=value)


def test_per_chat_limit_range():
    assert Settings(per_chat_daily_proactive_limit=0).per_chat_daily_proactive_limit == 0
    with pytest.raises(ValidationError):
        Settings(per_chat_daily_proactive_limit=11)


def test_mode_rhythm_and_overrides():
    assert Settings().heartbeat_base_minutes() == 0
    assert Settings(heartbeat_mode="gentle").heartbeat_base_minutes() == 180
    assert Settings(heartbeat_mode="lively").heartbeat_base_minutes() == 90
    assert Settings(heartbeat_mode="gentle", heartbeat_interval_minutes=45).heartbeat_base_minutes() == 45
    assert Settings(heartbeat_mode="gentle").longing_threshold_hours() == 72
    assert Settings(heartbeat_mode="lively").longing_threshold_hours() == 24
    assert Settings(heartbeat_mode="lively", longing_quiet_hours=48).longing_threshold_hours() == 48


def test_legacy_dump_migrates_to_off_and_new_quota():
    legacy = {
        "heartbeat_enabled": True,
        "heartbeat_interval_minutes": 60,
        "daily_proactive_limit": 1,
        "longing_quiet_hours": 0,
    }
    s = Settings.model_validate(legacy)
    assert s.heartbeat_mode == "off"
    assert s.heartbeat_interval_minutes == 0
    assert s.daily_proactive_limit == 3


def test_legacy_longing_user_lands_on_gentle_and_keeps_threshold():
    s = Settings.model_validate(
        {"heartbeat_enabled": True, "longing_quiet_hours": 48, "daily_proactive_limit": 5}
    )
    assert s.heartbeat_mode == "gentle"
    assert s.longing_quiet_hours == 48
    assert s.daily_proactive_limit == 5


def test_legacy_v030_full_dump_without_heartbeat_enabled_migrates():
    s = Settings.model_validate(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "agent_max_steps": 16,
            "heartbeat_profile": "default",
            "heartbeat_interval_minutes": 60,
            "daily_proactive_limit": 1,
            "longing_quiet_hours": 72,
        }
    )
    assert s.heartbeat_mode == "gentle"
    assert s.heartbeat_interval_minutes == 0
    assert s.daily_proactive_limit == 3
    assert s.longing_quiet_hours == 72


def test_non_legacy_partial_config_is_not_migrated():
    s = Settings.model_validate({"heartbeat_interval_minutes": 90, "daily_proactive_limit": 1})
    assert s.heartbeat_interval_minutes == 90
    assert s.daily_proactive_limit == 1


@pytest.mark.parametrize(
    ("raw", "current", "expected"),
    [
        ({"heartbeat_enabled": True}, "off", {"heartbeat_mode": "gentle"}),
        ({"heartbeat_enabled": True}, "lively", {"heartbeat_mode": "lively"}),
        ({"heartbeat_enabled": False}, "lively", {"heartbeat_mode": "off"}),
        ({"heartbeat_enabled": False, "heartbeat_mode": "lively"}, "off", {"heartbeat_mode": "lively"}),
        ({"sleep_window": "1-8"}, "off", {"sleep_window": "1-8"}),
    ],
)
def test_heartbeat_enabled_compat_on_write(raw, current, expected):
    assert apply_heartbeat_compat(raw, current) == expected


# ───────────── 第 2 步：两层额度 ─────────────

from maisya.intents import Intents  # noqa: E402
from maisya.threads import ThreadStore  # noqa: E402
from maisya.trust import MessageStore  # noqa: E402

NOW = 1795250000.0


def _intents(tmp_path, total=3, per_chat=1):
    db = tmp_path / "maisya.db"
    MessageStore(db)
    it = Intents(ThreadStore(db), daily_proactive_limit=total, per_chat_daily_proactive_limit=per_chat)
    it.clock = lambda: NOW
    return it


def _proactive(it, target, offset=60, source="afterglow", **extra):
    return it.schedule(kind="proactive", source=source, target=target, due_at=NOW + offset, reason="由头", **extra)


def test_per_chat_cap_blocks_second_in_same_chat(tmp_path):
    it = _intents(tmp_path, total=5, per_chat=1)
    _proactive(it, "chat:qq:group:111")
    with pytest.raises(ValueError, match="会话"):
        _proactive(it, "chat:qq:group:111", offset=90)
    _proactive(it, "chat:qq:group:222")


def test_global_total_blocks_across_chats(tmp_path):
    it = _intents(tmp_path, total=2, per_chat=5)
    _proactive(it, "chat:qq:group:111")
    _proactive(it, "chat:qq:private:Alice")
    assert not it.proactive_quota_available("chat:qq:group:333", now=NOW)
    with pytest.raises(ValueError, match="每日上限"):
        _proactive(it, "chat:qq:group:333")


def test_exempt_sources_neither_count_nor_block(tmp_path):
    it = _intents(tmp_path, total=1, per_chat=1)
    it.schedule(kind="proactive", source="workflow", target="chat:qq:group:111", due_at=NOW + 30, reason="定点工作流", bypass_limits=True)
    it.schedule(kind="return", source="thread", target="chat:qq:group:111", due_at=NOW + 30, reason="回接")
    assert it.proactive_quota_available("chat:qq:group:111", now=NOW)
    _proactive(it, "chat:qq:group:111")


def test_quota_state_reports_both_layers(tmp_path):
    it = _intents(tmp_path, total=3, per_chat=1)
    _proactive(it, "chat:qq:group:111")
    state = it.proactive_quota_state("chat:qq:group:111", now=NOW)
    assert state == {"used": 1, "limit": 3, "remaining": 2, "chat_used": 1, "chat_limit": 1, "chat_remaining": 0}
    assert it.proactive_quota_state(now=NOW)["remaining"] == 2


# ───────────── 第 3 步：一次心跳的流程 ─────────────

import asyncio  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from test_foundation import event, loaded  # noqa: E402

HOUR = 3600.0
T0 = 1_700_000_000.0
PLAN_HB = "## 气质与分寸\n温柔\n\n## 自主探索方向\n逛逛开源小工具\n"
EMPTY_HB = "## 气质与分寸\n温柔\n\n## 自主探索方向\n\n"


async def _hb_plugin(tmp_path, *, heartbeat=EMPTY_HB, silent_hours=100.0, decision=None):
    plugin = await loaded(tmp_path)
    now = [T0]
    plugin.intents.clock = lambda: now[0]
    await plugin.after_receive(message=event("你好", user="friend", group=False, platform="qq"))
    with plugin.threads.connect() as db, db:
        db.execute(
            "UPDATE session_routes SET last_human_at=?, human_count=5 WHERE session_id='chat'",
            (now[0] - silent_hours * HOUR,),
        )
    plugin.memory.write("memory", "暗号：青柠汽水", chat_id="chat")
    Path(plugin.memory.root, "memory/self/HEARTBEAT.md").write_text(heartbeat, encoding="utf-8")
    pulse = plugin.pulse
    pulse.acquaintance_users = ["qq:friend"]
    pulse.heartbeat_enabled = lambda: True
    pulse.heartbeat_interval = int(3 * HOUR)
    pulse.longing_quiet_hours = 72
    pulse.rng = lambda: 0.5
    pulse.invalidate_caches()
    calls = []

    async def capability(name, **kwargs):
        calls.append((name, kwargs))
        if name == "llm.generate_with_tools" and _is_decision(kwargs):
            if isinstance(decision, Exception):
                raise decision
            return {"response": decision if isinstance(decision, str) else json.dumps(decision or {"action": "ok"})}
        return {"success": True}

    plugin.ctx.call_capability = capability
    return plugin, now, calls


def _is_decision(kwargs):
    return "MaiBot 的心跳" in json.dumps(kwargs.get("prompt"), ensure_ascii=False)


def _decision_calls(calls):
    return [kw for name, kw in calls if name == "llm.generate_with_tools" and _is_decision(kw)]


@pytest.mark.asyncio
async def test_off_mode_makes_no_calls_and_no_ticks(tmp_path):
    plugin, _, calls = await _hb_plugin(tmp_path, heartbeat=PLAN_HB)
    try:
        plugin.pulse.heartbeat_enabled = lambda: False
        await plugin.pulse._heartbeat()
        assert calls == []
        assert plugin.intents.heartbeat_ticks() == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_no_reason_records_ok_without_model_call(tmp_path):
    plugin, _, calls = await _hb_plugin(tmp_path, silent_hours=1.0)
    try:
        await plugin.pulse._heartbeat()
        assert _decision_calls(calls) == []
        assert [t["outcome"] for t in plugin.intents.heartbeat_ticks()] == ["ok:no_reason"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(("roll", "factor"), [(0.0, 0.7), (1.0, 1.4)])
async def test_next_tick_is_jittered(tmp_path, roll, factor):
    plugin, now, _ = await _hb_plugin(tmp_path, silent_hours=1.0)
    try:
        plugin.pulse.rng = lambda: roll
        await plugin.pulse._heartbeat()
        assert plugin.pulse._heartbeat_next_at == pytest.approx(now[0] + 3 * HOUR * factor)
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_sleep_window_skips_tick(tmp_path):
    plugin, _, calls = await _hb_plugin(tmp_path)
    try:
        plugin.pulse._in_sleep_window = lambda *a, **k: True
        await plugin.pulse._heartbeat()
        assert _decision_calls(calls) == []
        assert plugin.intents.heartbeat_ticks() == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_miss_schedules_longing_intent_without_private_memory_in_prompt(tmp_path):
    plugin, _, calls = await _hb_plugin(
        tmp_path, decision={"action": "miss", "ref": "c1", "reason": "好久没聊，问问近况"}
    )
    try:
        await plugin.pulse._heartbeat()
        decided = _decision_calls(calls)
        assert len(decided) == 1
        prompt_text = json.dumps(decided[0]["prompt"], ensure_ascii=False)
        assert "c1" in prompt_text and "温柔" in prompt_text
        assert "青柠汽水" not in prompt_text
        assert decided[0]["tools"] == []
        intents = plugin.intents.list("chat:chat")
        assert [(i["source"], i["reason"]) for i in intents] == [("longing", "好久没聊，问问近况")]
        assert plugin.intents.heartbeat_ticks()[0]["outcome"] == "miss"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [RuntimeError("model down"), "不是 JSON", {"action": "miss", "ref": "c9"}, {"action": "explore"}],
)
async def test_bad_decisions_fall_back_to_silence(tmp_path, decision):
    plugin, _, _ = await _hb_plugin(tmp_path, decision=decision)
    try:
        await plugin.pulse._heartbeat()
        assert plugin.intents.list("chat:chat") == []
        assert not plugin.engine.tasks
        assert plugin.intents.heartbeat_ticks()[0]["outcome"].startswith("ok:")
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_explore_starts_self_thread_with_topic(tmp_path):
    plugin, _, calls = await _hb_plugin(
        tmp_path, heartbeat=PLAN_HB, silent_hours=1.0, decision={"action": "explore", "topic": "终端排版工具"}
    )
    sent = []

    async def spy_step(profile, history, **_kwargs):
        sent.append(str(history))
        return {"response": "逛完了", "tool_calls": []}

    plugin.runtime.step = spy_step
    try:
        await plugin.pulse._heartbeat()
        await asyncio.gather(*list(plugin.engine.tasks.values()), return_exceptions=True)
        assert len(_decision_calls(calls)) == 1
        assert sent and "终端排版工具" in sent[-1] and "逛逛开源小工具" in sent[-1]
        assert plugin.intents.heartbeat_ticks()[0]["outcome"] == "explore"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_tick_log_keeps_last_50(tmp_path):
    plugin, _, _ = await _hb_plugin(tmp_path)
    try:
        for i in range(60):
            plugin.intents.record_heartbeat_tick("ok:no_reason", f"#{i}", at=T0 + i)
        ticks = plugin.intents.heartbeat_ticks(limit=100)
        assert len(ticks) == 50
        assert ticks[0]["detail"] == "#59"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_standalone_longing_loop_removed(tmp_path):
    plugin, _, _ = await _hb_plugin(tmp_path)
    try:
        assert not hasattr(plugin.pulse, "_longing")
    finally:
        await plugin.on_unload()


# ───────────── 第 4～5 步：接线与对话调整面 ─────────────

from maisya.shaping import execute_shape  # noqa: E402


async def _settings_update(plugin, patch):
    cur = await plugin.manage_read("settings")
    return await plugin.manage_write(
        "settings_update", json.dumps({"settings": patch, "expected_version": cur["version"]})
    )


@pytest.mark.asyncio
async def test_settings_hot_update_reaches_pulse_and_intents(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        assert plugin.pulse._is_heartbeat_enabled() is False
        assert plugin.pulse.heartbeat_interval == 0
        await _settings_update(plugin, {"heartbeat_mode": "lively", "per_chat_daily_proactive_limit": 2})
        assert plugin.settings.heartbeat_mode == "lively"
        assert plugin.pulse._is_heartbeat_enabled() is True
        assert plugin.pulse.heartbeat_interval == 90 * 60
        assert plugin.pulse.longing_quiet_hours == 24
        assert plugin.intents.per_chat_daily_proactive_limit == 2
        await _settings_update(plugin, {"heartbeat_interval_minutes": 45, "longing_quiet_hours": 48})
        assert plugin.pulse.heartbeat_interval == 45 * 60
        assert plugin.pulse.longing_quiet_hours == 48
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_legacy_heartbeat_enabled_write_maps_to_mode(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        await _settings_update(plugin, {"heartbeat_enabled": True})
        assert plugin.settings.heartbeat_mode == "gentle"
        await _settings_update(plugin, {"heartbeat_enabled": False})
        assert plugin.settings.heartbeat_mode == "off"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_shape_accepts_mode_words_and_compat(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        kw = dict(current_chat_id="chat", current_platform="qq", admin_verified=True)
        await execute_shape(plugin, action="settings", op="patch", patch={"heartbeat_mode": "活泼"}, **kw)
        assert plugin.settings.heartbeat_mode == "lively"
        await execute_shape(plugin, action="settings", op="patch", patch={"heartbeat_enabled": False}, **kw)
        assert plugin.settings.heartbeat_mode == "off"
        await execute_shape(plugin, action="settings", op="patch", patch={"per_chat_daily_proactive_limit": 3}, **kw)
        assert plugin.settings.per_chat_daily_proactive_limit == 3
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_overview_heartbeat_block(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        kw = dict(current_chat_id="chat", current_platform="qq")
        hb = (await execute_shape(plugin, action="overview", **kw))["heartbeat"]
        assert hb["mode"] == "off" and hb["effective"] == "关闭"
        await _settings_update(plugin, {"heartbeat_mode": "gentle", "longing_quiet_hours": 48})
        plugin.intents.record_heartbeat_tick("ok:no_reason", "")
        hb = (await execute_shape(plugin, action="overview", **kw))["heartbeat"]
        assert hb["mode"] == "gentle"
        assert hb["effective"] == "只会想念（未填逛逛方向）"
        assert hb["base_interval_minutes"] == {"value": 180, "override": False}
        assert hb["longing_threshold_hours"] == {"value": 48, "override": True}
        assert hb["quota"]["limit"] == 3 and hb["quota"]["chat_limit"] == 1
        assert hb["recent_ticks"][0]["outcome"] == "ok:no_reason"
        Path(plugin.memory.root, "memory/self/HEARTBEAT.md").write_text(PLAN_HB, encoding="utf-8")
        hb = (await execute_shape(plugin, action="overview", **kw))["heartbeat"]
        assert hb["effective"] == "想念 + 逛逛"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_admin_guidance_maps_phrases_to_mode(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        plugin.authorize_admin_source = lambda *a, **k: True
        plugin._pending_sources["chat"] = ["m-admin"]
        hook = await plugin.before_planner(
            session_id="chat",
            tool_definitions=[
                {"name": "reply"},
                {"name": "maisya_agent"},
                {"name": "maisya_shape"},
            ],
            items=[],
        )
        text = hook["modified_kwargs"]["items"][-1]["parts"][0]["text"]
        assert "heartbeat_mode" in text and "per_chat_daily_proactive_limit" in text
        assert "heartbeat_enabled" not in text
    finally:
        await plugin.on_unload()


# ───────────── 第 6 步：WebUI 与现场近期心跳 ─────────────


@pytest.mark.asyncio
async def test_console_settings_and_memory_expose_heartbeat_status_and_ticks(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        plugin.intents.record_heartbeat_tick("ok:no_reason", "", at=T0)
        plugin.intents.record_heartbeat_tick("explore", "终端排版工具", at=T0 + 60)

        now_snap = await plugin.console_query()
        assert now_snap["pulse"]["heartbeat_mode"] == "off"
        assert now_snap["pulse"]["heartbeat_effective"] == "关闭"
        assert now_snap["pulse"]["per_chat_daily_limit"] == 1
        assert [t["outcome"] for t in now_snap["heartbeat_ticks"][:2]] == ["explore", "ok:no_reason"]

        st_read = await plugin.manage_read("settings")
        assert st_read["heartbeat_effective"] == "关闭"

        mem_read = await plugin.manage_read("memory")
        pst = mem_read["proactive_status"]
        assert pst["heartbeat_mode"] == "off"
        assert pst["heartbeat_effective"] == "关闭"
        assert pst["global_limit"] == 3
        assert pst["per_chat_limit"] == 1
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_webui_settings_memory_and_now_heartbeat_step6_playwright(tmp_path):
    from playwright.async_api import async_playwright, expect

    plugin = await loaded(tmp_path, ["qq:10001"])
    await plugin.manage_write("onboarding_skip", "{}")
    plugin.intents.record_heartbeat_tick("miss", "好久没聊，问候近况", at=T0 + 10)
    plugin.intents.record_heartbeat_tick("explore", "独立游戏开发日志", at=T0 + 20)

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
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("pageerror", lambda err: errors.append(str(err)))
                await page.expose_function("hostRpc", request)
                doc = Path("webui/now.html").read_text(encoding="utf-8")
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

                # 现场展示最近心跳记录
                await expect(page.locator("#now-hb-ticks")).to_be_visible()
                await expect(page.locator("#now-hb-ticks")).to_contain_text("独立游戏开发日志")

                # 设置页：三档切换、全局与单会话上限、高级覆盖（0 = 跟随档位）
                await page.locator('[data-nav="settings"]').click()
                await expect(page.locator("#set-hb-mode-seg")).to_be_visible()
                await expect(page.locator('#set-hb-mode-seg [data-hb-mode="off"]')).to_have_attribute("aria-pressed", "true")
                await expect(page.locator("#set-hb-effective-txt")).to_contain_text("关闭")
                await expect(page.locator("#set-per-chat-limit")).to_have_value("1")

                await page.locator('#set-hb-mode-seg [data-hb-mode="lively"]').click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                await expect(page.locator('#set-hb-mode-seg [data-hb-mode="lively"]')).to_have_attribute("aria-pressed", "true")
                await expect(page.locator("#set-hb-effective-txt")).to_contain_text("只会想念")
                assert plugin.settings.heartbeat_mode == "lively"

                await page.evaluate("document.getElementById('set-feedback').textContent = ''")
                await page.locator("#set-daily-limit").fill("4")
                await page.locator("#set-per-chat-limit").fill("2")
                await page.locator("#set-hb-adv summary").click()
                await expect(page.locator("#set-hb-interval")).to_have_value("0")
                await expect(page.locator("#set-longing")).to_have_value("0")
                await page.locator("#set-hb-interval").fill("45")
                await page.locator("#set-longing").fill("36")
                await page.locator("#set-save-runtime").click()
                await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
                assert plugin.settings.daily_proactive_limit == 4
                assert plugin.settings.per_chat_daily_proactive_limit == 2
                assert plugin.settings.heartbeat_interval_minutes == 45
                assert plugin.settings.longing_quiet_hours == 36

                # 手账页：顶部主动状态栏显示档位、生效状态与两层额度
                await page.locator('[data-nav="memory"]').click()
                await expect(page.locator("#mem-proactive-bar")).to_contain_text("活泼")
                await expect(page.locator("#mem-proactive-bar")).to_contain_text("只会想念")
                await expect(page.locator("#mem-proactive-bar")).to_contain_text("单会话上限 2")

                assert not errors
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()
