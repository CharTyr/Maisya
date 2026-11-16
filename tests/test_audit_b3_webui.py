import asyncio
import base64
import json
import re
import subprocess
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBUI_DIR = REPO_ROOT / "webui"

HOST_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'none'; img-src data: blob:; font-src data:; media-src data: blob:; "
    'object-src \'none\'; frame-src \'none\'; base-uri \'none\'; form-action \'none\'">'
)


def _wrap_csp(html: str) -> str:
    return html.replace("<head>", f"<head>{HOST_CSP_META}", 1)


@asynccontextmanager
async def _open_page(
    plugin,
    tab_name: str,
    *,
    viewport=None,
    on_rpc=None,
    reject_clipboard: bool = False,
):
    errors: list[str] = []
    rpc_log: list[tuple[str, str, dict]] = []
    active = 0
    peak = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = (
            await browser.new_context(viewport=viewport)
            if viewport
            else await browser.new_context()
        )
        page = await context.new_page()
        page.on("pageerror", lambda err: errors.append(f"pageerror:{err}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console:{msg.text}")
            if msg.type == "error"
            else None,
        )

        async def host_rpc(kind, name, arguments):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                args = dict(arguments or {})
                rpc_log.append((kind, name, args))
                if on_rpc is not None:
                    override = await on_rpc(kind, name, args)
                    if override is not None:
                        return override
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
            finally:
                active -= 1

        await page.expose_function("hostRpc", host_rpc)
        extra_clipboard = (
            "try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new Error('blocked'))}});}catch(_e){}"
            if reject_clipboard
            else ""
        )
        await page.evaluate(
            f"""() => {{
              {extra_clipboard}
              window.maibot = {{
                ready: Promise.resolve(),
                context: {{ theme: "light", dashboardStyle: "millennium", host: {{ plugins: {{}} }} }},
                page: {{ state: {{ tab: {json.dumps(tab_name)} }} }},
                query: (name, args) => window.hostRpc("query", name, args),
                action: (name, args) => window.hostRpc("action", name, args),
                navigate: () => Promise.resolve(),
              }};
            }}"""
        )
        raw_doc = (WEBUI_DIR / "now.html").read_text(encoding="utf-8")
        assert "localStorage" not in raw_doc
        assert "sessionStorage" not in raw_doc
        assert "transition: all" not in raw_doc
        await page.set_content(_wrap_csp(raw_doc), wait_until="domcontentloaded")
        try:
            yield page, errors, rpc_log, lambda: peak
            assert await page.locator("img").count() == 0
            assert await page.locator("iframe").count() == 0
        finally:
            await context.close()
            await browser.close()


def test_b3_webui_schema_ci_pillow_css_and_dead_skeleton_cleanup():
    webui_cfg = json.loads((REPO_ROOT / "webui.json").read_text(encoding="utf-8"))
    assert len(webui_cfg["pages"]) == 1
    assert webui_cfg["pages"][0]["entry"] == "webui/now.html"
    note_chat_id = webui_cfg["pages"][0]["actions"]["note"]["parameters"]["chat_id"]
    assert note_chat_id["max_length"] == 256

    ci_yml = (REPO_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "pillow" in ci_yml.lower()

    mgmt_in = (WEBUI_DIR / "management.html.in").read_text(encoding="utf-8")
    assert "activeSlotCount" not in mgmt_in
    assert '<body data-page="{{PAGE_ID}}">' not in mgmt_in
    assert ".m-model-rank" not in mgmt_in
    assert ".lib-img-stage" in mgmt_in

    now_in = (WEBUI_DIR / "now.html.in").read_text(encoding="utf-8")
    assert "while (activeQueue >= 2)" in now_in
    assert 'id="ms-mob-dock"' in now_in

    now_built = (WEBUI_DIR / "now.html").read_text(encoding="utf-8")
    assert "localStorage" not in now_built
    assert "sessionStorage" not in now_built
    assert "transition: all" not in now_built

    build_check = subprocess.run(
        [sys.executable, str(WEBUI_DIR / "build.py"), "--check"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert build_check.returncode == 0, build_check.stdout + build_check.stderr


@pytest.mark.asyncio
async def test_b3_toggle_profile_expected_version_zero_not_bypassed(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        cur = await plugin.manage_read(
            "asset", json.dumps({"kind": "profile", "target": "default", "document": "profile.toml"})
        )
        assert cur["success"] and cur["version"] == 0

        ok_v0 = await plugin.manage_write(
            "toggle_profile",
            json.dumps(
                {
                    "target": "default",
                    "enabled": False,
                    "expected_version": 0,
                }
            ),
        )
        assert ok_v0["success"] and ok_v0["version"] == 1
        assert plugin.profiles.get("default").enabled is False

        stale = await plugin.manage_write(
            "toggle_profile",
            json.dumps({"target": "default", "enabled": True, "expected_version": 0}),
        )
        assert not stale["success"], "expected_version=0 must not bypass conflict check"
        assert plugin.profiles.get("default").enabled is False

        ok_v1 = await plugin.manage_write(
            "toggle_profile",
            json.dumps({"target": "default", "enabled": True, "expected_version": 1}),
        )
        assert ok_v1["success"] and ok_v1["version"] == 2
        assert plugin.profiles.get("default").enabled is True
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b3_management_settings_tz_offset_and_sleep_window_validation(tmp_path):
    from maisya.management import _normalize_sleep_window_setting

    assert _normalize_sleep_window_setting("") == ""
    assert _normalize_sleep_window_setting(" 00-24 ") == "00-24"
    assert _normalize_sleep_window_setting("23:30-07:00") == "23:30-07:00"
    assert _normalize_sleep_window_setting("00:00-24:00") == "00:00-24:00"
    for bad in ("25-07", "24:30-07:00", "12:60-14:00", "abc-def"):
        with pytest.raises(ValueError):
            _normalize_sleep_window_setting(bad)

    plugin = await loaded(tmp_path)
    try:
        cur = await plugin.manage_read("settings")
        assert cur["success"] and cur["settings"]["tz_offset_minutes"] == 480

        saved = await plugin.manage_write(
            "settings",
            json.dumps(
                {
                    "settings": {
                        **cur["settings"],
                        "sleep_window": " 00-24 ",
                        "tz_offset_minutes": 540,
                    },
                    "expected_version": cur["version"],
                }
            ),
        )
        assert saved["success"], saved
        assert saved["settings"]["sleep_window"] == "00-24"
        assert saved["settings"]["tz_offset_minutes"] == 540
        assert plugin.settings.tz_offset_minutes == 540

        # Omitting tz_offset_minutes in a partial update must preserve current tz_offset_minutes
        partial = dict(saved["settings"])
        partial.pop("tz_offset_minutes", None)
        partial["proactive_level"] = "quiet"
        preserved = await plugin.manage_write(
            "settings",
            json.dumps({"settings": partial, "expected_version": saved["version"]}),
        )
        assert preserved["success"], preserved
        assert preserved["settings"]["tz_offset_minutes"] == 540
        assert plugin.settings.tz_offset_minutes == 540

        for bad_tz in (True, 480.5, -721, 841):
            bad_res = await plugin.manage_write(
                "settings",
                json.dumps(
                    {
                        "settings": {**preserved["settings"], "tz_offset_minutes": bad_tz},
                        "expected_version": preserved["version"],
                    }
                ),
            )
            assert not bad_res["success"]

        bad_sleep = await plugin.manage_write(
            "settings",
            json.dumps(
                {
                    "settings": {**preserved["settings"], "sleep_window": "25-08"},
                    "expected_version": preserved["version"],
                }
            ),
        )
        assert not bad_sleep["success"]
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b3_settings_longing_bounds_sleep_window_and_tz_offset_e2e(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await expect(page.locator("#set-sleep-start")).not_to_have_value("")
            await expect(page.locator("#set-daily-limit")).to_be_visible()

            # 非法免打扰时段应被拒绝且不覆写原配置
            prev_sleep = plugin.settings.sleep_window
            await page.locator("#set-sleep-start").fill("25:00")
            await page.locator("#set-save-runtime").click()
            await expect(page.locator("#set-feedback")).to_have_class(
                re.compile(r"mil-feedback--err")
            )
            assert plugin.settings.sleep_window == prev_sleep
            # 等待失败保存触发的 loadSettings() 回填完毕，避免后续输入被异步重绘覆盖
            await expect(page.locator("#set-sleep-start")).not_to_have_value("25:00")

            # 设置每日搭话额度（0..50 边界）与 00:00-24:00 全天免打扰
            await page.locator("#set-daily-limit").fill("2")
            await page.locator("#set-sleep-start").fill("00:00")
            await page.locator("#set-sleep-end").fill("24:00")
            await page.evaluate("document.getElementById('set-feedback').textContent = ''")
            await page.locator("#set-save-runtime").click()
            await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
            assert plugin.settings.daily_proactive_limit == 2
            assert plugin.settings.sleep_window == "00:00-24:00"
            assert not errors and get_peak() <= 2

        # 重开设置页应正确回填 00:00-24:00 与额度 2
        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, _peak):
            await expect(page.locator("#set-sleep-start")).to_have_value("00:00")
            await expect(page.locator("#set-sleep-end")).to_have_value("24:00")
            await expect(page.locator("#set-daily-limit")).to_have_value("2")
            assert not errors

        # 分钟级 sleep_window ("23:30-07:00") 在无关设置更新时不得丢失
        captured_writes: list[dict] = []
        second_read_done = asyncio.Event()
        settings_reads = 0

        async def minute_sleep_rpc(kind, name, args):
            nonlocal settings_reads
            if kind == "query" and name == "read":
                sec = args.get("section")
                if sec == "settings":
                    res = await plugin.manage_read(**args)
                    res["settings"]["sleep_window"] = "23:30-07:00"
                    settings_reads += 1
                    if settings_reads >= 2:
                        second_read_done.set()
                    return res
            if kind == "action" and name == "write" and args.get("action") == "settings_update":
                payload = json.loads(args.get("payload") or "{}")
                captured_writes.append(payload)
                return {
                    "success": True,
                    "settings": payload["settings"],
                    "version": "v-minute-test",
                }
            return None

        async with _open_page(
            plugin,
            "settings",
            viewport={"width": 1440, "height": 900},
            on_rpc=minute_sleep_rpc,
        ) as (page, errors, _rpc_log, _peak):
            await expect(page.locator("#set-sleep-start")).to_have_value("23:30")
            await expect(page.locator("#set-sleep-end")).to_have_value("07:00")
            await page.locator("#set-save-runtime").click()
            await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
            assert captured_writes
            assert captured_writes[-1]["settings"]["sleep_window"] == "23:30-07:00"

            await asyncio.wait_for(second_read_done.wait(), timeout=5.0)
            await page.evaluate("() => new Promise(r => setTimeout(r, 0))")
            await page.locator("#set-sleep-start").fill("22:45")
            await page.locator("#set-sleep-end").fill("06:15")
            await page.evaluate("document.getElementById('set-feedback').textContent = ''")
            await page.locator("#set-save-runtime").click()
            await expect(page.locator("#set-feedback")).to_contain_text("设置已保存")
            assert len(captured_writes) == 2
            assert captured_writes[-1]["settings"]["sleep_window"] == "22:45-06:15"
            assert not errors
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b3_abilities_multiline_toml_subtable_model_num_and_task_clear(tmp_path):
    plugin = await loaded(tmp_path)
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        cloned = await plugin.manage_write(
            "profile_clone",
            json.dumps({"source_id": "default", "target_id": "custom_b3", "name": "B3自定义助手"}),
        )
        assert cloned["success"], cloned

        plugin.memory.write(
            "profile",
            (
                'name = "B3自定义助手"\n'
                "enabled = true\n"
                'task = "planner"\n'
                "toolsets = [\n"
                '  "web", # 网页工具\n'
                "]\n"
            ),
            target="custom_b3",
            document="profile.toml",
            allow_custom=True,
        )

        # v0.3 档案页对多行 TOML 档案的解析渲染（Hermes 首屏 + 高级开关）
        async with _open_page(
            plugin, "profiles", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await page.locator('#pr-roster [data-pick-profile-id="custom_b3"]').click()
            await expect(page.locator("#pr-name")).to_have_value("B3自定义助手")
            await expect(page.locator("#pr-enabled")).to_be_checked()
            await page.locator("#pr-adv-toggle").click()
            await expect(page.locator("#pr-adv-body")).to_be_visible()
            await page.locator('[data-pr-tab="abilities"]').click()
            web_cb = page.locator('#pr-ability-cards input[data-ab-toggle-kind="toolsets"][data-ab-toggle-id="web"]')
            await expect(web_cb).to_be_checked()
            await page.locator('[data-pr-tab="general"]').click()
            await page.locator("#pr-btn-save").click()
            await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
            assert not errors and get_peak() <= 2

        # 后端 set_profile_models 清理 task 键并写回有效 TOML
        clear_res = await plugin.manage_write(
            "set_profile_models",
            json.dumps({"profile_id": "custom_b3", "models": ["primary-model", "backup-model"], "fallback_slot": ""}),
        )
        assert clear_res["success"], clear_res
        saved_toml = plugin.memory.read("profile", target="custom_b3", document="profile.toml")[
            "content"
        ]
        parsed = tomllib.loads(saved_toml)
        assert parsed["model_name"] == "primary-model"
        assert parsed["fallback_models"] == ["backup-model"]
        assert "task" not in parsed
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b3_critical_e2e_paths_and_strict_concurrency_gate(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    await plugin.manage_write("onboarding_skip", "{}")
    chunk_bin = (b"\x89B3_HDR\x00\xff" + bytes(range(256)) * 780)[:210000]
    expected_b64 = base64.b64encode(chunk_bin).decode("ascii")

    try:
        # Prepare undo_learning note + scheduled_admin intent + workspace/binary output
        plugin.memory.write("notes", "初始规则", target="default", author="user")
        th = plugin.threads.create(
            plugin.profiles.get("default"),
            "B3 端到端闭环活动",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:1001",
            workspace="b3ws",
        )
        with plugin.threads.connect() as db, db:
            plugin.threads.event(db, th["thread_id"], "input", 1, {"text": "总结产物与规则"})
        plugin.memory.write(
            "notes",
            "初始规则\n- 自动补充条文",
            target="default",
            author=f"thread:{th['thread_id']}",
        )
        with plugin.threads.connect() as db, db:
            db.execute(
                "INSERT INTO learning_receipts (thread_id, revision, status) VALUES (?, 1, 'applied')",
                (th["thread_id"],),
            )
        with plugin.runtime.files.activity(th) as work:
            (work.path / "out").mkdir(parents=True, exist_ok=True)
            (work.path / "out/payload.bin").write_bytes(chunk_bin)
            work.commit()
            attached = plugin.runtime.files.attach(th, work, ["out/payload.bin"])
        with plugin.threads.connect() as db, db:
            db.execute(
                "UPDATE agent_threads SET state='waiting', result=?, completed_revision=1 WHERE thread_id=?",
                (
                    json.dumps(
                        {"text": "二进制附件已完成。", "attachments": attached},
                        ensure_ascii=False,
                    ),
                    th["thread_id"],
                ),
            )

        sched = await plugin.manage_write(
            "schedule_intent",
            json.dumps(
                {
                    "chat_id": "qq:group:100246809",
                    "profile": "default",
                    "in_seconds": 3600,
                    "every_seconds": 86400,
                    "prompt": "B3 受保护巡检计划",
                }
            ),
        )
        assert sched["success"]
        sched_id = sched["intent"]["intent_id"]

        # 1) undo_learning_note 后端撤回 + 现场页 #now-intents 取消 scheduled_admin
        undo = await plugin.manage_write(
            "undo_learning_note", json.dumps({"profile": "default"})
        )
        assert undo["success"], undo
        assert plugin.memory.read("notes", target="default")["content"].strip() == "初始规则"

        async with _open_page(plugin, "now", viewport={"width": 1440, "height": 900}) as (
            page,
            errors,
            _rpc_log,
            get_peak,
        ):
            cancel_btn = page.locator(
                f'#now-intents [data-intent-act="intent_delete"][data-intent-id="{sched_id}"]'
            )
            await expect(cancel_btn).to_be_visible()
            await cancel_btn.click()
            await expect(page.locator('[data-feedback-slot="now"]')).to_contain_text("状态已更新")
            assert plugin.intents.get(sched_id)["status"] == "cancelled"
            assert not errors and get_peak() <= 2

        # 2) payload.bin 后端 196608B 分片读取 + 资料库桌面/移动端抽屉预览
        c1 = await plugin.manage_read(
            "artifact",
            json.dumps({"kind": "attachment", "handle": attached[0]["handle"], "offset": 0}),
        )
        assert c1["success"] and c1["next_offset"] == 196608
        c2 = await plugin.manage_read(
            "artifact",
            json.dumps({"kind": "attachment", "handle": attached[0]["handle"], "offset": c1["next_offset"]}),
        )
        assert c2["success"] and not c2.get("next_offset")
        full_b64 = base64.b64encode(base64.b64decode(c1["base64"]) + base64.b64decode(c2["base64"])).decode("ascii")
        assert full_b64 == expected_b64

        async with _open_page(
            plugin,
            "library",
            viewport={"width": 1440, "height": 900},
            reject_clipboard=True,
        ) as (page, errors, _rpc_log, get_peak):
            item = page.locator("#lib-gallery [data-lib-idx]", has_text="payload.bin").first
            await expect(item).to_be_visible()
            await item.click()
            await expect(page.locator("#lib-drawer")).to_be_visible()
            await page.locator("#lib-drawer-close").click()
            assert not errors and get_peak() <= 2

        async with _open_page(plugin, "library", viewport={"width": 390, "height": 844}) as (
            page,
            errors,
            _rpc_log,
            _peak,
        ):
            item = page.locator("#lib-gallery [data-lib-idx]", has_text="payload.bin").first
            await expect(item).to_be_visible()
            await item.click()
            await expect(page.locator("#lib-drawer")).to_be_visible()
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )
            await page.locator("#lib-drawer-close").click()
            assert not errors

        # 3) upsert_service_item MCP 服务原子改名 + Vault 脱敏展示
        await plugin.manage_write(
            "vault", json.dumps({"handle": "hook_key", "value": "sec_val_123456"})
        )
        up1 = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "name": "old_mcp_svc",
                    "item": {"command": ["/usr/bin/python3", "-m", "http.server"], "tools": ["fetch"]},
                }
            ),
        )
        assert up1["success"], up1
        up2 = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "orig_kind": "mcp",
                    "orig_name": "old_mcp_svc",
                    "name": "new_mcp_svc",
                    "item": {"command": ["/usr/bin/python3", "-m", "http.server"], "tools": ["fetch"]},
                }
            ),
        )
        assert up2["success"], up2
        svcs = (await plugin.manage_read("services"))["configuration"]
        assert "old_mcp_svc" not in (svcs.get("servers") or {})
        assert "new_mcp_svc" in (svcs.get("servers") or {})

        async with _open_page(plugin, "settings", viewport={"width": 1440, "height": 900}) as (
            page,
            errors,
            _rpc_log,
            get_peak,
        ):
            await expect(page.locator("#set-vault-list")).to_contain_text("hook_key")
            assert "sec_val_123456" not in await page.content()
            assert not errors and get_peak() <= 2

        # 4) 重叠快速切页严格遵守 inflight <= 2（while activeQueue >= 2）
        async def delayed_rpc(kind, name, args):
            await asyncio.sleep(0.06)
            return None

        async with _open_page(
            plugin, "now", viewport={"width": 1440, "height": 900}, on_rpc=delayed_rpc
        ) as (page, errors, rpc_log, get_peak):
            await expect(page.locator("#now-threads")).to_be_visible()
            await page.evaluate(
                """() => {
                  document.querySelector('[data-nav="library"]')?.click();
                  setTimeout(() => document.querySelector('[data-nav="settings"]')?.click(), 10);
                  setTimeout(() => document.querySelector('[data-nav="abilities"]')?.click(), 20);
                  setTimeout(() => document.querySelector('[data-nav="now"]')?.click(), 30);
                }"""
            )
            await page.wait_for_timeout(450)
            assert len(rpc_log) >= 3
            assert get_peak() <= 2
            assert not errors
    finally:
        await plugin.on_unload()
