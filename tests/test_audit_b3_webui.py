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
    page_name: str,
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
                context: {{ theme: "light", dashboardStyle: "millennium" }},
                page: {{ state: {{}} }},
                query: (name, args) => window.hostRpc("query", name, args),
                action: (name, args) => window.hostRpc("action", name, args),
                navigate: () => Promise.resolve(),
              }};
            }}"""
        )
        doc = _wrap_csp((WEBUI_DIR / f"{page_name}.html").read_text(encoding="utf-8"))
        await page.set_content(doc)
        try:
            yield page, errors, rpc_log, lambda: peak
        finally:
            await context.close()
            await browser.close()


def test_b3_webui_schema_ci_pillow_css_and_dead_skeleton_cleanup():
    webui_cfg = json.loads((REPO_ROOT / "webui.json").read_text(encoding="utf-8"))
    note_chat_id = webui_cfg["pages"][0]["actions"]["note"]["parameters"]["chat_id"]
    assert note_chat_id["max_length"] == 256

    ci_yml = (REPO_ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "pillow" in ci_yml.lower()

    mgmt_in = (WEBUI_DIR / "management.html.in").read_text(encoding="utf-8")
    assert "activeSlotCount" not in mgmt_in
    assert '<body data-page="{{PAGE_ID}}">' not in mgmt_in
    assert ".m-model-rank" not in mgmt_in
    assert "#ms-mgmt-view .m-model-num" in mgmt_in
    assert "#ms-mgmt-view .m-model-row.is-primary .m-model-num" in mgmt_in

    now_in = (WEBUI_DIR / "now.html.in").read_text(encoding="utf-8")
    assert "while (activeQueue >= 2)" in now_in

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
            "asset", json.dumps({"kind": "profile", "target": "scout", "document": "profile.toml"})
        )
        assert cur["success"] and cur["version"] == 0

        ok_v0 = await plugin.manage_write(
            "toggle_profile",
            json.dumps(
                {
                    "target": "scout",
                    "enabled": False,
                    "expected_version": 0,
                }
            ),
        )
        assert ok_v0["success"] and ok_v0["version"] == 1
        assert plugin.profiles.get("scout").enabled is False

        stale = await plugin.manage_write(
            "toggle_profile",
            json.dumps({"target": "scout", "enabled": True, "expected_version": 0}),
        )
        assert not stale["success"], "expected_version=0 must not bypass conflict check"
        assert plugin.profiles.get("scout").enabled is False

        ok_v1 = await plugin.manage_write(
            "toggle_profile",
            json.dumps({"target": "scout", "enabled": True, "expected_version": 1}),
        )
        assert ok_v1["success"] and ok_v1["version"] == 2
        assert plugin.profiles.get("scout").enabled is True
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
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    try:
        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, rpc_log, get_peak):
            await page.locator('[data-settings-mode="advanced"]').click()
            await page.locator('[data-set-rail="runtime"]').click()

            longing = page.locator("#set-longing")
            await expect(longing).to_be_visible()

            before_writes = sum(
                1 for _, name, a in rpc_log if name == "write" and a.get("action") == "settings"
            )
            for bad_val in ("5", "-1", "721", "12.5"):
                await longing.fill(bad_val)
                await expect(longing).to_have_attribute("aria-invalid", "true")
                await expect(page.locator("#set-longing-err")).to_be_visible()
                await page.locator("#save-settings-adv").click()
                await expect(page.locator("#feedback")).to_contain_text("12")
                after_writes = sum(
                    1
                    for _, name, a in rpc_log
                    if name == "write" and a.get("action") == "settings"
                )
                assert after_writes == before_writes, (
                    f"invalid longing_quiet_hours={bad_val} should be blocked client-side"
                )

            await longing.fill("24")
            await expect(longing).not_to_have_attribute("aria-invalid", "true")
            await expect(page.locator("#set-longing-err")).to_be_hidden()

            # Switch back to general settings and configure timezone + 00-24 full-day sleep_window
            await page.locator('[data-settings-mode="general"]').click()
            tz_preset = page.locator("#set-tz-preset")
            tz_input = page.locator("#set-tz-offset")
            await expect(tz_input).to_have_value("480")
            await tz_preset.select_option("540")
            await expect(tz_input).to_have_value("540")
            await tz_input.fill("-300")
            await expect(tz_preset).to_have_value("-300")

            # Invalid timezone offset blocked client-side
            await tz_input.fill("-900")
            await page.locator("#save-settings").click()
            await expect(page.locator("#feedback")).to_contain_text("-720")
            assert plugin.settings.tz_offset_minutes == 480

            await tz_input.fill("-300")

            # Enable 00-24 sleep window
            await page.locator("label.mil-switch", has=page.locator("#sleep-enabled")).click()
            await page.locator("#sleep-start").select_option("00")
            await page.locator("#sleep-end").select_option("24")
            assert await page.locator("#set-sleep").input_value() == "00-24"

            await page.locator("#save-settings").click()
            await expect(page.locator("#feedback")).to_contain_text("已保存")
            assert plugin.settings.sleep_window == "00-24"
            assert plugin.settings.tz_offset_minutes == -300
            assert plugin.settings.longing_quiet_hours == 24
            assert not errors and get_peak() <= 2

        # Re-open settings page to confirm hydration preserves 00-24 and -300
        async with _open_page(
            plugin, "settings", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, _peak):
            await expect(page.locator("#sleep-enabled")).to_be_checked()
            await expect(page.locator("#sleep-start")).to_have_value("00")
            await expect(page.locator("#sleep-end")).to_have_value("24")
            assert await page.locator("#set-sleep").input_value() == "00-24"
            await expect(page.locator("#set-tz-offset")).to_have_value("-300")
            assert not errors

        # Minute-precision sleep_window ("23:30-07:00") hydration & preservation on unrelated save
        captured_writes: list[dict] = []

        async def minute_sleep_rpc(kind, name, args):
            if kind == "query" and name == "read":
                sec = args.get("section")
                if sec == "settings":
                    res = await plugin.manage_read(**args)
                    res["settings"]["sleep_window"] = "23:30-07:00"
                    return res
                if sec == "batch":
                    res = await plugin.manage_read(**args)
                    if res.get("sections", {}).get("settings", {}).get("settings"):
                        res["sections"]["settings"]["settings"]["sleep_window"] = "23:30-07:00"
                    return res
            if kind == "action" and name == "write" and args.get("action") == "settings":
                payload = json.loads(args.get("payload") or "{}")
                captured_writes.append(payload)
                # Return updated settings snapshot without failing if host config regex is pre-merge
                return {
                    "success": True,
                    "settings": payload["settings"],
                    "version": "v-minute-test",
                    "proactive_quota": {"used": 0, "limit": 1, "remaining": 1},
                }
            return None

        async with _open_page(
            plugin,
            "settings",
            viewport={"width": 1440, "height": 900},
            on_rpc=minute_sleep_rpc,
        ) as (page, errors, _rpc_log, _peak):
            await expect(page.locator("#sleep-enabled")).to_be_checked()
            await expect(page.locator("#sleep-start")).to_have_value("23")
            await expect(page.locator("#sleep-start-min")).to_have_value("30")
            await expect(page.locator("#sleep-end")).to_have_value("07")
            await expect(page.locator("#sleep-end-min")).to_have_value("00")
            assert await page.locator("#set-sleep").input_value() == "23:30-07:00"

            await page.locator("#proactive_level").select_option("lively")
            await page.locator("#save-settings").click()
            await expect(page.locator("#feedback")).to_contain_text("已保存")
            assert captured_writes
            assert captured_writes[-1]["settings"]["sleep_window"] == "23:30-07:00"

            # Interactive minute adjustment
            await page.locator("#sleep-start").select_option("22")
            await page.locator("#sleep-start-min").select_option("45")
            await page.locator("#sleep-end").select_option("06")
            await page.locator("#sleep-end-min").select_option("15")
            assert await page.locator("#set-sleep").input_value() == "22:45-06:15"
            await page.locator("#save-settings").click()
            assert captured_writes[-1]["settings"]["sleep_window"] == "22:45-06:15"
            assert not errors
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b3_abilities_multiline_toml_subtable_model_num_and_task_clear(tmp_path):
    plugin = await loaded(tmp_path)
    try:
        plugin.memory.write(
            "profile",
            (
                'title = "探索者"\n'
                "enabled = true\n"
                'task = "planner"\n'
                "tools = [\n"
                '  "web_search", # 网页检索\n'
                '  "web_fetch",\n'
                "]\n"
            ),
            target="scout",
            document="profile.toml",
            allow_custom=True,
        )

        async with _open_page(
            plugin, "abilities", viewport={"width": 1440, "height": 900}
        ) as (page, errors, _rpc_log, get_peak):
            await page.locator('#profiles [data-profile="scout"]').click()
            await page.locator('[data-doc-tab="config"]').click()

            # Multiline array with inline comment & trailing comma must hydrate visual checkboxes
            await expect(page.locator("#cfg-enabled")).to_be_checked()
            await expect(page.locator('[data-cfg-tool="web_search"]')).to_be_checked()
            await expect(page.locator('[data-cfg-tool="web_fetch"]')).to_be_checked()

            # Check subtable isolation when raw TOML in editor includes [subtable]
            subtable_toml = (
                'title = "探索者"\n'
                "enabled = true\n"
                'task = "planner"\n'
                "tools = [\n"
                '  "web_search", # 网页检索\n'
                '  "web_fetch",\n'
                "]\n\n"
                "[sandbox]\n"
                "enabled = false\n"
                'model_name = "subtable-model"\n'
                "max_steps = 99\n"
            )
            await page.locator("#content").fill(subtable_toml)
            await page.locator("#content").dispatch_event("input")

            await expect(page.locator("#cfg-enabled")).to_be_checked()
            await expect(page.locator("#cfg-steps")).to_have_value("")
            await expect(page.locator(".m-model-row")).to_have_count(0)

            await page.locator('[data-cfg-tool="image_read"]').check()
            await page.locator("#cfg-model-name").fill("primary-model")
            await page.locator("#cfg-model-add").click()
            await page.locator("#cfg-model-name").fill("backup-model")
            await page.locator("#cfg-model-add").click()
            await page.locator("#cfg-steps").fill("14")

            toml_with_subtable = await page.locator("#content").input_value()
            parsed_sub = tomllib.loads(toml_with_subtable)
            assert parsed_sub["enabled"] is True
            assert parsed_sub["max_steps"] == 14
            assert parsed_sub["model_name"] == "primary-model"
            assert parsed_sub["fallback_models"] == ["backup-model"]
            assert parsed_sub["tools"] == ["web_search", "web_fetch", "image_read"]
            assert parsed_sub["sandbox"] == {
                "enabled": False,
                "model_name": "subtable-model",
                "max_steps": 99,
            }

            # Verify .m-model-num badge styling actually took effect in Chromium
            num_styles = await page.evaluate(
                """() => {
                  const rows = [...document.querySelectorAll('#cfg-model-chain .m-model-num')];
                  return rows.map((el) => {
                    const cs = getComputedStyle(el);
                    return { bg: cs.backgroundColor, color: cs.color, padL: parseFloat(cs.paddingLeft), weight: Number(cs.fontWeight) };
                  });
                }"""
            )
            assert len(num_styles) == 2
            assert num_styles[0]["padL"] > 0 and num_styles[0]["weight"] >= 700
            assert num_styles[0]["bg"] != "rgba(0, 0, 0, 0)"
            assert num_styles[0]["bg"] != num_styles[1]["bg"]

            # Drop synthetic [sandbox] block before saving to Profiles.get, keep multiline array test intact
            await page.locator("#content").fill(toml_with_subtable.split("\n[sandbox]")[0] + "\n")
            await page.locator("#content").dispatch_event("input")
            await page.locator("#save").click()
            await expect(page.locator("#feedback")).to_contain_text("已保存")

            await page.locator("#cfg-task").select_option("")
            await page.locator("#cfg-model-apply").click()
            await expect(page.locator("#feedback")).to_contain_text("已应用")
            saved_toml = plugin.memory.read("profile", target="scout", document="profile.toml")[
                "content"
            ]
            assert "task" not in tomllib.loads(saved_toml)
            assert not errors and get_peak() <= 2
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_b3_critical_e2e_paths_and_strict_concurrency_gate(tmp_path):
    plugin = await loaded(tmp_path, ["qq:group:100246809"])
    chunk_bin = (b"\x89B3_HDR\x00\xff" + bytes(range(256)) * 780)[:210000]
    expected_b64 = base64.b64encode(chunk_bin).decode("ascii")

    try:
        # Prepare undo_learning note + scheduled_admin intent + workspace/binary output
        plugin.memory.write("notes", "初始规则", target="scout", author="user")
        th = plugin.threads.create(
            plugin.profiles.get("scout"),
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
            target="scout",
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
                    "profile": "scout",
                    "in_seconds": 3600,
                    "every_seconds": 86400,
                    "prompt": "B3 受保护巡检计划",
                }
            ),
        )
        assert sched["success"]
        sched_id = sched["intent"]["intent_id"]

        # 1) [data-undo-learning] + scheduled_admin WebUI cancel on memory.html
        async with _open_page(plugin, "memory", viewport={"width": 1440, "height": 900}) as (
            page,
            errors,
            _rpc_log,
            get_peak,
        ):
            await page.locator('[data-kind-tab="heartbeat"]').click()
            undo_btn = page.locator('#learning [data-undo-learning="scout"]').first
            await expect(undo_btn).to_be_visible()
            await undo_btn.click()
            await expect(page.locator("#feedback")).to_contain_text("撤回")
            assert plugin.memory.read("notes", target="scout")["content"].strip() == "初始规则"

            cancel_btn = page.locator(f'#intent-list [data-cancel-intent="{sched_id}"]')
            await expect(cancel_btn).to_be_visible()
            assert "受保护" in await page.locator("#intent-list").inner_text()
            await cancel_btn.click()
            await expect(page.locator("#feedback")).to_contain_text("取消")
            assert plugin.intents.get(sched_id)["status"] == "cancelled"
            assert not errors and get_peak() <= 2

        # 2) #out-more-btn binary pagination + mobile [data-ws-path] inspect on outputs.html
        async with _open_page(
            plugin,
            "outputs",
            viewport={"width": 1440, "height": 900},
            reject_clipboard=True,
        ) as (page, errors, _rpc_log, get_peak):
            item = page.locator("#out-list [data-out-idx]", has_text="payload.bin").first
            await expect(item).to_be_visible()
            await item.click()
            await expect(page.locator("#out-more-btn")).to_be_visible()
            await page.locator("#out-more-btn").click()
            await expect(page.locator("#out-more-btn")).to_be_hidden()
            await page.locator("#out-copy-btn").click()
            assert await page.locator("#out-copy-box").input_value() == expected_b64
            assert not errors and get_peak() <= 2

        async with _open_page(plugin, "outputs", viewport={"width": 390, "height": 844}) as (
            page,
            errors,
            _rpc_log,
            _peak,
        ):
            await page.locator('[data-out-space="workspaces"]').click()
            await page.locator("#workspaces [data-ws]").first.click()
            ws_file = page.locator('[data-ws-path="out/payload.bin"]')
            await expect(ws_file).to_be_visible()
            await ws_file.click()
            await expect(page.locator("#out-shell")).to_have_class(re.compile(r"is-mob-inspect"))
            await expect(page.locator("#out-inspector")).to_be_visible()
            assert not errors

        # 3) #svc-d-save (atomic rename + mcp -> template kind switch) & #wh-b-* on settings.html
        await plugin.manage_write(
            "vault", json.dumps({"handle": "hook_key", "value": "sec_val_123456"})
        )
        async with _open_page(plugin, "settings", viewport={"width": 1440, "height": 900}) as (
            page,
            errors,
            _rpc_log,
            get_peak,
        ):
            await page.locator('[data-settings-mode="advanced"]').click()
            await page.locator('[data-set-rail="services"]').click()
            await page.locator("#svc-new-btn").click()
            await page.locator("#svc-d-name").fill("old_mcp_svc")
            await page.locator("#svc-d-cmd").fill("/usr/bin/python3 -m http.server")
            await page.locator("#svc-d-save").click()
            await expect(page.locator("#feedback")).to_contain_text("old_mcp_svc")

            # Rename & switch mcp -> template atomically
            await page.locator('[data-svc-edit="old_mcp_svc"][data-svc-kind="mcp"]').click()
            await page.locator("#svc-d-kind").select_option("template")
            await page.locator("#svc-d-name").fill("new_tpl_svc")
            await page.locator("#svc-d-targets").fill("93.184.216.34:443")
            await page.locator("#svc-d-save").click()
            await expect(page.locator("#feedback")).to_contain_text("new_tpl_svc")
            svcs = (await plugin.manage_read("services"))["configuration"]
            assert "old_mcp_svc" not in (svcs.get("servers") or {})
            assert "new_tpl_svc" in (svcs.get("templates") or {})

            # Webhook builder #wh-b-*
            await page.locator('[data-set-rail="webhooks"]').click()
            await page.locator("#wh-b-name").fill("ci_push")
            await page.locator("#wh-b-target").fill("qq:group:100246809")
            await page.locator("#wh-b-profile").select_option("scout")
            await page.locator("#wh-b-kind").select_option("silent")
            await page.locator("#wh-b-secret").fill("hook_key")
            await page.locator("#wh-b-add").click()
            await page.locator("#save-webhooks").click()
            await expect(page.locator("#feedback")).to_contain_text("事件通道")
            hooks = (await plugin.manage_read("webhooks"))["configuration"]
            assert hooks["ci_push"]["kind"] == "silent"
            assert hooks["ci_push"]["secret_handle"] == "hook_key"
            assert not errors and get_peak() <= 2

        # 4) Concurrent overlapping requests strictly enforce inflight <= 2 on now.html
        async def delayed_rpc(kind, name, args):
            await asyncio.sleep(0.06)
            return None

        async with _open_page(
            plugin, "now", viewport={"width": 1440, "height": 900}, on_rpc=delayed_rpc
        ) as (page, errors, rpc_log, get_peak):
            await page.locator("#flow-all-chats").wait_for(state="visible")
            # Overlap multiple concurrent reads & tab-switch hydration bursts
            await page.evaluate(
                """() => {
                  document.getElementById('flow-all-chats')?.click();
                  document.querySelector('[data-nav="outputs"]')?.click();
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
