from pathlib import Path
import base64
import json
import re

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded

from maisya.abilities import FISH_SEED_RE

CSP_META = (
    '<head><meta http-equiv="Content-Security-Policy" '
    "content=\"default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; connect-src 'none'; img-src data: blob:\">"
)


@pytest.mark.asyncio
async def test_profile_fish_seed_validation_and_management_action(tmp_path):
    plugin = await loaded(tmp_path)
    assert plugin.profiles.get("default").fish_seed == ""

    bad = await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "default", "fish_seed": "bad seed!"}),
    )
    assert bad["success"] is False

    seed = "c-pebble-2-dots-demo42"
    assert FISH_SEED_RE.fullmatch(seed)
    saved = await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "default", "fish_seed": seed}),
    )
    assert saved["success"] is True
    assert saved["fish_seed"] == seed
    assert saved["version"] >= 1
    assert plugin.profiles.get("default").fish_seed == seed

    profiles_payload = await plugin.manage_read("profiles")
    default_entry = next(
        p for p in profiles_payload["profiles"] if p["id"] == "default"
    )
    assert default_entry["fish_seed"] == seed

    abilities_alias = await plugin.manage_read("abilities")
    assert any(
        p["id"] == "default" and p["fish_seed"] == seed
        for p in abilities_alias["profiles"]
    )

    outputs = await plugin.manage_read("outputs")
    assert outputs["profile_fish"]["default"] == seed

    snap = await plugin.console_query()
    default_card = next(c for c in snap["profile_cards"] if c["id"] == "default")
    assert default_card["fish_seed"] == seed

    reset = await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "default", "fish_seed": ""}),
    )
    assert reset["success"] is True
    assert plugin.profiles.get("default").fish_seed == ""


@pytest.mark.asyncio
async def test_profile_zip_preserves_fish_seed(tmp_path):
    plugin = await loaded(tmp_path)
    custom_seed = "c-betta-6-happy-seed99"
    res = await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "default", "fish_seed": custom_seed}),
    )
    assert res["success"] is True

    exported = await plugin.manage_write(
        "profile_export_zip",
        json.dumps({"profile_id": "default"}),
    )
    assert exported["success"] is True, exported
    raw_zip = base64.b64decode(exported["base64"])

    imported = await plugin.manage_write(
        "profile_import_zip",
        json.dumps(
            {
                "profile_id": "default_copy",
                "base64": exported["base64"],
                "overwrite": False,
            }
        ),
    )
    assert imported["success"] is True, imported
    assert plugin.profiles.get("default_copy").fish_seed == custom_seed

    # 直接调用 abilities .zip 接口也保持一致
    roundtrip = plugin.profiles.export_profile_zip("default_copy")
    assert roundtrip == raw_zip or len(roundtrip) > 0
    restored = plugin.profiles.import_profile_zip(
        roundtrip, "default_copy2", overwrite=True
    )
    assert restored.fish_seed == custom_seed


def test_fish_js_assets_and_species_counts():
    fish_js = Path("webui/fish.js").read_text(encoding="utf-8")
    assert "FISH_SPECIES_SPEC" in fish_js
    assert "FISH_COLORS" in fish_js
    assert "FISH_EYES_SPEC" in fish_js
    assert "roleFishHtml" in fish_js
    assert "fishSvg" in fish_js


@pytest.mark.asyncio
async def test_playwright_fish_avatars_4_pages_and_mobile_carousel(tmp_path):
    plugin = await loaded(tmp_path)
    await plugin.manage_write("onboarding_skip", "{}")
    cloned = await plugin.manage_write(
        "profile_clone",
        json.dumps(
            {"source_id": "default", "target_id": "custom_fish", "name": "小鱼测试档"}
        ),
    )
    assert cloned["success"] is True, cloned

    profile = plugin.profiles.get("default")
    row = plugin.threads.create(
        profile,
        "小鱼头像联合验证",
        chat_id="qq:group:100246809",
        platform="qq",
        user_id="qq:10001",
        workspace="ws_fish",
    )
    with plugin.runtime.files.activity(row) as work:
        work.write("out/report.md", "# 报告\n已完成汇总。")
        work.commit()
    with plugin.threads.connect() as db, db:
        db.execute(
            "UPDATE agent_threads SET state='waiting', result=?, completed_revision=1 WHERE thread_id=?",
            (json.dumps({"text": "已整理完成"}, ensure_ascii=False), row["thread_id"]),
        )

    profile_reads = []
    profile_saves = []

    async def maibot_query(name, arguments):
        args = arguments or {}
        if name == "read":
            res = await plugin.manage_read(**args)
            if args.get("section") == "profile_detail":
                profile_reads.append(args.get("options"))
            return res
        if name == "event":
            return await plugin.console_event(**args)
        return await plugin.console_query(**args)

    async def maibot_action(name, arguments):
        args = arguments or {}
        if name == "write":
            res = await plugin.manage_write(**args)
            if args.get("action") == "profile_update":
                profile_saves.append(res)
            return res
        if name == "note":
            return await plugin.console_send(**args)
        if name == "stop":
            return await plugin.console_stop(**args)
        return {"success": True}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 880})
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))

            await page.expose_function("hostQuery", maibot_query)
            await page.expose_function("hostAction", maibot_action)
            await page.evaluate(
                """() => {
                    window.maibot = {
                        ready: Promise.resolve(),
                        context: { theme: 'light', host: { plugins: {} } },
                        query: (name, args) => window.hostQuery(name, args || {}),
                        action: (name, args) => window.hostAction(name, args || {}),
                        navigate: () => Promise.resolve(),
                    };
                }"""
            )
            now_html = (
                Path("webui/now.html")
                .read_text(encoding="utf-8")
                .replace("<head>", CSP_META, 1)
            )
            await page.set_content(now_html, wait_until="domcontentloaded")

            # 1) 现场页：线程列表与选中标题旁显示像素鱼，标题内无 svg，全局无 <img>
            await page.wait_for_selector("#now-threads svg.fish")
            await page.wait_for_selector("#now-sel-fish svg.fish")
            await expect(page.locator("#now-sel-title")).to_have_text("小鱼头像联合验证")
            assert await page.locator("#now-sel-title svg").count() == 0
            assert await page.locator("img").count() == 0

            # 2) 能力页：桌面列表、锁外形定制（12 物种 + 18 配色）、随机与自动恢复
            await page.click('[data-nav="abilities"]')
            await page.wait_for_selector(
                '#pr-roster [data-pick-profile-id="default"] svg.fish'
            )
            details_before = len(profile_reads)
            await page.click('#pr-roster [data-pick-profile-id="default"]')
            await expect(page.locator("#pr-id")).to_have_value("default")
            while len(profile_reads) <= details_before:
                await page.wait_for_timeout(20)
            await page.wait_for_selector("#pr-head-fish svg.fish")
            await page.wait_for_selector("#pr-fish-preview svg.fish")

            await page.click('#pr-fish-mode [data-fish-mode="lock"]')
            await expect(page.locator("#pr-fish-drawer")).to_be_visible()
            assert await page.locator("#pr-fish-species [data-fish-species]").count() == 12
            assert await page.locator("#pr-fish-colors [data-fish-color]").count() == 18

            await page.click('#pr-fish-species [data-fish-species="disc"]')
            await page.click('#pr-fish-colors [data-fish-color="5"]')
            saves_before = len(profile_saves)
            details_before = len(profile_reads)
            await page.click("#pr-btn-save")
            while len(profile_saves) <= saves_before or len(profile_reads) <= details_before:
                await page.wait_for_timeout(20)
            await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")
            saved_seed = plugin.profiles.get("default").fish_seed
            assert re.match(r"^c-disc-5-", saved_seed)

            await expect(page.locator("#pr-fish-box")).not_to_contain_text("形象标识小鱼")
            await expect(page.locator("#pr-fish-box")).not_to_contain_text("自动按档案名稳定生成像素鱼")
            await page.click('#pr-fish-mode [data-fish-mode="random"]')
            help_random = await page.locator("#pr-fish-help").get_attribute("data-help")
            assert help_random and "随机" in help_random
            saves_before = len(profile_saves)
            details_before = len(profile_reads)
            await page.click("#pr-btn-save")
            while len(profile_saves) <= saves_before or len(profile_reads) <= details_before:
                await page.wait_for_timeout(20)
            rerolled = plugin.profiles.get("default").fish_seed
            assert rerolled and not rerolled.startswith("c-")

            await page.click('#pr-fish-mode [data-fish-mode="auto"]')
            help_auto = await page.locator("#pr-fish-help").get_attribute("data-help")
            assert help_auto and "自动" in help_auto
            saves_before = len(profile_saves)
            details_before = len(profile_reads)
            await page.click("#pr-btn-save")
            while len(profile_saves) <= saves_before or len(profile_reads) <= details_before:
                await page.wait_for_timeout(20)
            assert plugin.profiles.get("default").fish_seed == ""

            # 3) 切换产物页与设置页确认零 <img> 与零报错
            await page.click('[data-nav="library"]')
            await expect(page.locator("#lib-gallery-pane")).to_be_visible()
            assert await page.locator("img").count() == 0

            await page.click('[data-nav="settings"]')
            await expect(page.locator("#set-block-chats")).to_be_visible()
            assert await page.locator("img").count() == 0

            # 4) 窄屏移动端：#mob-role-carousel 像素鱼轮播 + #ab-mob-role-card1 回归检查
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.click('.ms-mob-dock [data-mob-nav="abilities"]')
            await page.wait_for_selector("#mob-role-carousel svg.fish")
            await expect(page.locator("#mob-role-carousel")).to_be_visible()
            await expect(page.locator("#ab-mob-role-card1")).to_be_visible()
            assert await page.locator("#mob-role-carousel svg.fish").count() >= 2
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )

            await page.click('#mob-role-carousel [data-mob-role="custom_fish"]')
            await expect(page.locator("#ab-mob-role-title")).to_contain_text("小鱼测试档")
            await page.click('[data-mob-role-jump="fish"]')
            await expect(page.locator("#profiles-mob-scrim")).to_be_visible()
            await page.wait_for_selector("#profiles-mob-sheet-body svg.fish")
            await page.click("#profiles-mob-sheet-body [data-mob-fish-rand]")
            await page.click("#profiles-mob-sheet-close")
            await expect(page.locator("#profiles-mob-scrim")).to_be_hidden()

            await page.click('.ms-mob-dock [data-mob-nav="now"]')
            await page.wait_for_selector("#now-threads svg.fish")
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= window.innerWidth"
            )

            assert not errors
        finally:
            await browser.close()
