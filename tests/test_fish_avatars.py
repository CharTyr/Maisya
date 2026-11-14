from pathlib import Path
import json

import pytest
from playwright.async_api import async_playwright, expect
from test_foundation import loaded

from maisya.recipes import (
    build_recipe_preview,
    export_recipe_archive,
    import_recipe_archive,
)

CSP_META = (
    "<head><meta http-equiv=\"Content-Security-Policy\" "
    "content=\"default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; connect-src 'none'; img-src data: blob:\">"
)


@pytest.mark.asyncio
async def test_profile_fish_seed_validation_and_management_action(tmp_path):
    plugin = await loaded(tmp_path)
    assert plugin.profiles.get("scout").fish_seed == ""

    bad = await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "scout", "fish_seed": "bad seed!"}),
    )
    assert bad["success"] is False

    saved = await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "scout", "fish_seed": "c-pebble-2-dots-demo42"}),
    )
    assert saved["success"] is True
    assert saved["fish_seed"] == "c-pebble-2-dots-demo42"
    assert saved["version"] >= 1
    assert plugin.profiles.get("scout").fish_seed == "c-pebble-2-dots-demo42"

    abilities = await plugin.manage_read("abilities")
    scout_entry = next(p for p in abilities["profiles"] if p["id"] == "scout")
    assert scout_entry["fish_seed"] == "c-pebble-2-dots-demo42"

    outputs = await plugin.manage_read("outputs")
    assert outputs["profile_fish"]["scout"] == "c-pebble-2-dots-demo42"

    snap = await plugin.console_query()
    scout_card = next(c for c in snap["profile_cards"] if c["id"] == "scout")
    assert scout_card["fish_seed"] == "c-pebble-2-dots-demo42"

    reset = await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "scout", "fish_seed": ""}),
    )
    assert reset["success"] is True
    assert plugin.profiles.get("scout").fish_seed == ""


@pytest.mark.asyncio
async def test_recipe_preserves_and_transfers_fish_seed(tmp_path):
    plugin = await loaded(tmp_path)
    custom_seed = "c-betta-6-happy-seed99"
    res = await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "scout", "fish_seed": custom_seed}),
    )
    assert res["success"] is True

    archive, _manifest = export_recipe_archive(
        plugin, "scout", {"include_skills": False, "include_routines": False}
    )

    _, pv = build_recipe_preview(plugin, archive)
    assert pv["profile"]["fish_seed"] == custom_seed

    copied = import_recipe_archive(
        plugin, archive, target="scout_copy", mode="copy"
    )
    assert copied["id"] == "scout_copy"
    assert copied["mode"] == "copy"
    assert plugin.profiles.get("scout_copy").fish_seed == custom_seed

    # Local scout gets a newer custom fish; an incoming recipe without fish_seed preserves local fish on update
    local_override = "c-eel-11-sleepy-local1"
    await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "scout", "fish_seed": local_override}),
    )
    await plugin.manage_write(
        "set_profile_fish",
        json.dumps({"target": "scout_copy", "fish_seed": ""}),
    )
    no_fish_archive, _ = export_recipe_archive(
        plugin, "scout_copy", {"include_skills": False, "include_routines": False}
    )
    updated = import_recipe_archive(
        plugin,
        no_fish_archive,
        target="scout",
        mode="update",
    )
    assert updated["id"] == "scout"
    assert updated["mode"] == "update"
    assert updated["preserved"]["fish_seed"] == local_override
    assert plugin.profiles.get("scout").fish_seed == local_override


@pytest.mark.asyncio
async def test_playwright_fish_avatars_abilities_outputs_and_now(tmp_path):
    plugin = await loaded(tmp_path)
    profile = plugin.profiles.get("scout")
    row = plugin.threads.create(
        profile,
        "小鱼头像联合验证",
        chat_id="chat",
        platform="qq",
        user_id="qq:Alice",
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

    async def mgmt_backend(mode, arguments):
        return await (
            plugin.manage_read(**arguments)
            if mode == "read"
            else plugin.manage_write(**arguments)
        )

    async def now_query(name, arguments):
        return await (
            plugin.console_event(**arguments)
            if name == "event"
            else plugin.console_query(**arguments)
        )

    async def now_action(name, arguments):
        return await (
            plugin.console_send(**arguments)
            if name == "note"
            else plugin.console_stop(**arguments)
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1200, "height": 880})
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))

            # 1) abilities.html: roster fish, customizer, reroll, reset, recipe preview
            await page.expose_function("backend", mgmt_backend)
            await page.evaluate(
                "window.maibot={ready:Promise.resolve(),context:{theme:'light'},query:(_,args)=>window.backend('read',args),action:(_,args)=>window.backend('write',args),navigate:()=>Promise.resolve()}"
            )
            abilities_html = Path("webui/abilities.html").read_text(encoding="utf-8").replace("<head>", CSP_META, 1)
            await page.set_content(abilities_html, wait_until="domcontentloaded")
            await page.wait_for_selector('#profiles [data-profile="scout"] svg.fish')
            assert await page.locator("img").count() == 0
            assert await page.locator("#skill-scope-bar svg.fish").count() >= 2

            await page.click('#profiles [data-profile="scout"]')
            await expect(page.locator("#role-fish-bar")).to_be_visible()

            await page.click("#role-fish-custom")
            await page.wait_for_selector("#role-fish-drawer .fish-opts")
            assert await page.locator('#role-fish-drawer [data-fish-pick="species"]').count() == 12
            assert await page.locator('#role-fish-drawer [data-fish-pick="color"]').count() == 18
            assert await page.locator('#role-fish-drawer [data-fish-pick="eyes"]').count() == 11

            await page.click('#role-fish-drawer [data-fish-pick="species"][data-fish-val="disc"]')
            await page.click('#role-fish-drawer [data-fish-pick="color"][data-fish-val="5"]')
            await page.click('#role-fish-drawer [data-fish-pick="eyes"][data-fish-val="wink"]')
            await page.click("#role-fish-shape")
            await page.click("#role-fish-save")
            await expect(page.locator("#feedback")).to_contain_text("已保存自定义小鱼头像")
            saved_seed = plugin.profiles.get("scout").fish_seed
            assert saved_seed.startswith("c-disc-5-wink-")

            # Export recipe & preview with saved custom fish
            await page.click("#export")
            await expect(page.locator("#package-text")).not_to_have_value("")
            await page.click("#rcp-preview-btn")
            await page.wait_for_selector("#rcp-pv-summary svg.fish")

            # Reroll & reset back to default
            await page.click("#role-fish-reroll")
            await expect(page.locator("#feedback")).to_contain_text("已随机更换一条新小鱼头像")
            rerolled = plugin.profiles.get("scout").fish_seed
            assert rerolled and not rerolled.startswith("c-")

            await page.click("#role-fish-reset")
            await expect(page.locator("#feedback")).to_contain_text("已恢复该角色的默认小鱼头像")
            assert plugin.profiles.get("scout").fish_seed == ""

            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            await page.set_viewport_size({"width": 1200, "height": 880})

            # 2) outputs.html: deliverable list, workspace chips, metadata bar
            outputs_html = Path("webui/outputs.html").read_text(encoding="utf-8").replace("<head>", CSP_META, 1)
            await page.set_content(outputs_html, wait_until="domcontentloaded")
            await page.wait_for_selector("#out-list svg.fish")
            await page.wait_for_selector("#workspaces svg.fish")
            await page.wait_for_selector("#out-meta svg.fish")
            assert await page.locator("img").count() == 0

            # 3) now.html: flow nodes, threads list, inspector fish outside h2, wake chips
            now_page = await browser.new_page(viewport={"width": 1200, "height": 880})
            now_page.on("pageerror", lambda err: errors.append(str(err)))
            await now_page.expose_function("nowQuery", now_query)
            await now_page.expose_function("nowAction", now_action)
            await now_page.evaluate(
                "window.maibot={ready:Promise.resolve(),context:{theme:'light'},query:(name,args)=>window.nowQuery(name,args),action:(name,args)=>window.nowAction(name,args),navigate:()=>Promise.resolve()}"
            )
            now_html = Path("webui/now.html").read_text(encoding="utf-8").replace("<head>", CSP_META, 1)
            await now_page.set_content(now_html, wait_until="domcontentloaded")
            await now_page.wait_for_selector("#fl-profiles svg.fish")
            await now_page.wait_for_selector("#threads svg.fish")
            await now_page.click("#launch-panel > summary")
            await now_page.wait_for_selector("#launch-profile-chips svg.fish")

            await expect(now_page.locator("h2")).to_have_text(row["thread_id"])
            assert await now_page.locator("h2 svg").count() == 0
            assert await now_page.locator("#ins-role-fish svg.fish").count() == 1
            assert await now_page.locator("img").count() == 0

            await now_page.set_viewport_size({"width": 390, "height": 844})
            assert await now_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            assert not errors
        finally:
            await browser.close()
