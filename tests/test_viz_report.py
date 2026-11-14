import asyncio
import base64
from dataclasses import asdict
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import maisya.abilities as abilities  # noqa: E402
from maisya.abilities import Profile, Profiles  # noqa: E402
from maisya.maker import AgentRuntime  # noqa: E402
from maisya.memory import MemoryLayers  # noqa: E402
from maisya.report import MAX_IMAGE_BYTES, render_report_artifact  # noqa: E402
from maisya.sandbox import SandboxSettings  # noqa: E402
import maisya.scout as scout  # noqa: E402
from maisya.threads import ThreadStore  # noqa: E402
from maisya.vault import Vault  # noqa: E402
from maisya.viz_spec import (  # noqa: E402
    CATALOG,
    SpecError,
    check_numbers,
    check_spec_numbers,
    load_viz_css,
    numbers_in,
    render,
    texts_of,
    validate,
)
from maisya.workspaces import Workspaces  # noqa: E402


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_JPEG_MIN = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
_GIF_MIN = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
_WEBP_MIN = b"RIFF\x18\x00\x00\x00WEBPVP8 \x0c\x00\x00\x000\x01\x00\x9d\x01*\x01\x00\x01\x00\x02\x00"


class _FakeSandbox:
    ready = False


def _ensure_render_report_registered(monkeypatch):
    """Temporary fallback when running in isolated wt before admin-ux merges scout/abilities."""
    if "render_report" not in scout.TOOL_DEFINITIONS:
        defs = dict(scout.TOOL_DEFINITIONS)
        defs["render_report"] = scout.definition(
            "render_report",
            "渲染结构化 Intelligent UI 调研报告或卡片 HTML 到 out/*.html",
            {
                "path": {"type": "string", "minLength": 0, "maxLength": 256},
                "title": {"type": "string", "minLength": 0, "maxLength": 200},
                "subtitle": {"type": "string", "minLength": 0, "maxLength": 300},
                "blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "minItems": 1,
                    "maxItems": 64,
                },
                "theme": {"type": "string", "enum": ["light", "dark"]},
                "layout": {"type": "string", "enum": ["page", "card"]},
                "source_text": {"type": "string", "minLength": 0, "maxLength": 32768},
            },
            ["blocks"],
        )
        monkeypatch.setattr(scout, "TOOL_DEFINITIONS", defs)
    if "render_report" not in abilities.WORKSPACE_TOOLS:
        monkeypatch.setattr(
            abilities, "WORKSPACE_TOOLS", abilities.WORKSPACE_TOOLS | {"render_report"}
        )
    if "render_report" not in abilities.SANDBOX_TOOLS:
        monkeypatch.setattr(
            abilities, "SANDBOX_TOOLS", abilities.SANDBOX_TOOLS | {"render_report"}
        )
    if "render_report" not in abilities.HOST_TOOLS:
        monkeypatch.setattr(
            abilities, "HOST_TOOLS", abilities.HOST_TOOLS | {"render_report"}
        )


def _all_16_blocks() -> list[dict]:
    return [
        {"type": "heading", "level": 2, "text": "核心结论概览"},
        {"type": "text", "text": "第一段对比摘要，含说明文字。"},
        {"type": "note", "tone": "warn", "text": "口径说明：以下指标基于公开测试。"},
        {"type": "stat", "label": "吞吐提升", "value": "42%", "delta": "+12%", "trend": "up", "note": "同功耗下"},
        {
            "type": "stats",
            "title": "关键指标网格",
            "items": [
                {"label": "延迟 P95", "value": "18ms", "delta": "-5ms", "trend": "down"},
                {"label": "可用率", "value": "99.9%", "trend": "flat"},
            ],
        },
        {
            "type": "compare",
            "title": "方案对比",
            "items": [
                {"name": "方案 A", "value": "120 分", "badge": "推荐", "winner": True, "points": ["带宽高", "延迟稳"]},
                {"name": "方案 B", "value": "95 分", "points": ["成本低", "吞吐偏低"]},
            ],
        },
        {
            "type": "bars",
            "title": "实测吞吐",
            "unit": " tok/s",
            "items": [
                {"label": "模型甲超长标签测试换行情况", "value": 150},
                {"label": "模型乙", "value": 98, "tone": "teal"},
            ],
        },
        {
            "type": "line",
            "title": "季度趋势",
            "unit": "%",
            "x_label": "季度",
            "y_label": "准确率",
            "points": [
                {"x": "Q1", "y": 72},
                {"x": "Q2", "y": 81},
                {"x": "Q3", "y": 89},
            ],
        },
        {
            "type": "share",
            "title": "成本结构",
            "unit": "%",
            "items": [
                {"label": "算力", "value": 60},
                {"label": "存储", "value": 25},
                {"label": "带宽", "value": 15},
            ],
        },
        {
            "type": "timeline",
            "title": "演进时间线",
            "items": [
                {"time": "2025-11", "title": "立项", "detail": "完成首版评测基线"},
                {"time": "2026-04", "title": "发布", "detail": "全量上线"},
            ],
        },
        {
            "type": "table",
            "title": "规格对照",
            "columns": ["项目", "数值", "状态"],
            "align": ["left", "right", "center"],
            "rows": [["显存", 80, "在售"], ["功耗", 350, "满载"]],
        },
        {
            "type": "list",
            "title": "关键清单",
            "ordered": True,
            "items": ["第一项核验点", {"title": "第二项", "text": "支持双路并行"}],
        },
        {"type": "takeaway", "tone": "good", "text": "结论：综合性价比领先。"},
        {
            "type": "tabs",
            "title": "分维度视图",
            "tabs": [
                {
                    "label": "性能维度",
                    "blocks": [{"type": "stat", "label": "峰值", "value": "240"}],
                },
                {
                    "label": "成本维度",
                    "blocks": [{"type": "note", "tone": "info", "text": "单位算力降本显著"}],
                },
            ],
        },
        {"type": "image", "path": "in/sample.png", "alt": "样本示意图", "caption": "图 1：样本图"},
        {
            "type": "sources",
            "title": "参考出处",
            "items": [
                {"title": "官方白皮书", "date": "2026-04-01", "url": "https://example.org/paper"},
                "内部基准记录",
            ],
        },
    ]


def test_viz_catalog_and_all_16_components_render():
    assert len(CATALOG) == 16
    blocks = _all_16_blocks()
    spec = {"v": 1, "title": "年度硬件对比报告", "subtitle": "多维实测", "blocks": blocks}
    validate(spec, profile="report")

    out_html = render(
        spec,
        image_resolver=lambda p: f"data:image/png;base64,{base64.b64encode(_PNG_1X1).decode('ascii')}",
    )
    for expected_cls in (
        "vs-h",
        "vs-p",
        "vs-note",
        "vs-stat",
        "vs-stats",
        "vs-cmp",
        "vs-bars",
        "vs-line-svg",
        "vs-share",
        "vs-tl",
        "vs-tbl",
        "vs-list",
        "vs-take",
        "vs-tabs",
        "vs-fig",
        "vs-src",
    ):
        assert expected_cls in out_html
    assert "<script" not in out_html.lower()
    assert 'type="radio"' in out_html
    assert "data:image/png;base64," in out_html

    css = load_viz_css()
    assert ".vs-static" in css
    assert ".vs-tab-section" in css
    assert "overflow-wrap: anywhere" in css
    assert ".vs-bar-t" in css
    assert "transition: all" not in css.lower()
    assert "url(" not in css.lower()
    assert "@import" not in css.lower()


def test_viz_news_vs_report_profile_boundaries_and_xss_escape():
    for rtype, block in (
        ("heading", {"type": "heading", "text": "标题"}),
        ("text", {"type": "text", "text": "正文"}),
        ("note", {"type": "note", "text": "注释"}),
        ("image", {"type": "image", "path": "in/a.png"}),
        ("sources", {"type": "sources", "items": ["出处"]}),
    ):
        with pytest.raises(SpecError, match=rtype):
            validate({"v": 1, "blocks": [block]}, profile="news")
        # Also disallowed inside news tabs
        with pytest.raises(SpecError, match=rtype):
            validate(
                {
                    "blocks": [
                        {
                            "type": "tabs",
                            "tabs": [
                                {"label": "A", "blocks": [block]},
                                {"label": "B", "blocks": [{"type": "stat", "label": "k", "value": "1"}]},
                            ],
                        }
                    ]
                },
                profile="news",
            )

    # Nested tabs forbidden in both profiles
    nested = {
        "blocks": [
            {
                "type": "tabs",
                "tabs": [
                    {
                        "label": "外层1",
                        "blocks": [
                            {
                                "type": "tabs",
                                "tabs": [
                                    {"label": "内1", "blocks": [{"type": "stat", "label": "x", "value": "1"}]},
                                    {"label": "内2", "blocks": [{"type": "stat", "label": "y", "value": "2"}]},
                                ],
                            }
                        ],
                    },
                    {"label": "外层2", "blocks": [{"type": "stat", "label": "z", "value": "3"}]},
                ],
            }
        ]
    }
    with pytest.raises(SpecError, match="嵌套"):
        validate(nested, profile="report")

    # News allows max 6 blocks; report allows up to 64
    seven_stats = {"blocks": [{"type": "stat", "label": f"k{i}", "value": str(i)} for i in range(7)]}
    with pytest.raises(SpecError):
        validate(seven_stats, profile="news")
    validate(seven_stats, profile="report")

    too_many = {"blocks": [{"type": "stat", "label": f"k{i}", "value": str(i)} for i in range(65)]}
    with pytest.raises(SpecError, match="64"):
        validate(too_many, profile="report")

    # XSS payloads escaped
    xss_spec = {
        "title": "<script>alert(1)</script>",
        "blocks": [{"type": "takeaway", "text": '<img src=x onerror="alert(2)">'}],
    }
    rendered = render(xss_spec)
    assert "<script>" not in rendered
    assert "<img src=x" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered


def test_viz_static_flattens_tabs_and_has_no_scripts():
    spec = {
        "blocks": [
            {
                "type": "tabs",
                "title": "分tab测试",
                "tabs": [
                    {"label": "第一页", "blocks": [{"type": "stat", "label": "营收", "value": "120亿"}]},
                    {"label": "第二页", "blocks": [{"type": "list", "items": ["结论一", "结论二"]}]},
                ],
            }
        ]
    }
    flat_html = render(spec, static=True)
    assert "vs-static" in flat_html
    assert flat_html.count("vs-tab-section") >= 2
    assert "第一页" in flat_html and "第二页" in flat_html
    assert 'type="radio"' not in flat_html
    assert "<script" not in flat_html.lower()


def test_numbers_anti_hallucination_chinese_and_rounding():
    # Chinese-adjacent digits extracted cleanly; identifiers like v2.0 / gpt4 skipped
    nums = numbers_in("涨了15%，同比提升12.5个点，共1,234.50台，v2.0及gpt4不计入")
    assert nums == [15.0, 12.5, 1234.5]

    source = "营收 120 亿元，同比增长 35.67%，转化率 12.5%，评分 99.96，共 2026 年样本"
    ok, reason = check_numbers(["营收120亿元，涨了36%", "转化率13%，约35.7%", "评分100", "前3名及8个地区"], source)
    assert ok, reason

    # <= 10 numbers never fail
    ok_small, _ = check_numbers(["共8项，同比9%，排名第2"], "完全没有数字的原文")
    assert ok_small

    # Hallucinated >10 number fails loudly
    bad_ok, bad_reason = check_numbers(["暴涨42%达到120亿元"], source)
    assert not bad_ok
    assert "42" in bad_reason

    # check_spec_numbers audits nested tabs/tables while skipping image paths and source URLs
    spec = {
        "title": "2026 基准分析",
        "blocks": [
            {"type": "table", "columns": ["指标", "数值"], "rows": [["同比", "35.7%"]]},
            {
                "type": "tabs",
                "tabs": [
                    {"label": "A", "blocks": [{"type": "stat", "label": "虚构值", "value": "888"}]},
                    {"label": "B", "blocks": [{"type": "stat", "label": "真实值", "value": "120"}]},
                ],
            },
            {"type": "image", "path": "in/chart_9999.png", "alt": "图表"},
            {"type": "sources", "items": [{"title": "报告", "url": "https://example.org/post/77777"}]},
        ],
    }
    spec_ok, spec_reason = check_spec_numbers(spec, source)
    assert not spec_ok
    assert "888" in spec_reason
    assert "9999" not in spec_reason
    assert "77777" not in spec_reason

    texts = texts_of(spec)
    assert any("2026 基准分析" in t for t in texts)
    assert all("77777" not in t for t in texts)


def test_render_report_artifact_page_card_paths_and_images(tmp_path):
    store = ThreadStore(tmp_path / "threads.db")
    maker_profile = Profile(
        id="maker",
        title="创作者",
        tools=("render_report", "workspace_file", "result_attach"),
        access="everyone",
    )
    row = store.create(
        maker_profile,
        "生成调研报告",
        chat_id="chat-report",
        platform="qq",
        user_id="user-1",
        workspace="testreport",
    )
    ws = Workspaces(tmp_path / "ws", store)
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_1X1)
    with ws.activity(row) as work:
        (work.path / "in" / "a.png").write_bytes(_PNG_1X1)
        (work.path / "project" / "b.jpg").write_bytes(_JPEG_MIN)
        (work.path / "out" / "c.webp").write_bytes(_WEBP_MIN)
        (work.path / "in" / "d.gif").write_bytes(_GIF_MIN)
        (work.path / "in" / "fake.png").write_bytes(b"NOT A REAL PNG IMAGE")
        (work.path / "cache" / "secret.png").write_bytes(_PNG_1X1)

        # 1. layout="page" with tabs, dark theme, relative path normalized -> out/*.html
        blocks = [
            {"type": "heading", "text": "GPU 算力对比"},
            {"type": "stat", "label": "算力提升", "value": "36%", "trend": "up"},
            {"type": "image", "path": "in/a.png", "caption": "PNG 图"},
            {"type": "image", "path": "project/b.jpg", "caption": "JPG 图"},
            {"type": "image", "path": "out/c.webp", "caption": "WebP 图"},
            {"type": "image", "path": "in/d.gif", "caption": "GIF 图"},
            {
                "type": "tabs",
                "tabs": [
                    {"label": "吞吐", "blocks": [{"type": "stat", "label": "QPS", "value": "1200"}]},
                    {"label": "延迟", "blocks": [{"type": "note", "text": "P95 为 18ms"}]},
                ],
            },
        ]
        res_page = render_report_artifact(
            work,
            {
                "path": "gpu-compare.html",
                "title": "GPU 算力调研",
                "blocks": blocks,
                "theme": "dark",
                "layout": "page",
                "source_text": "算力提升 35.7%，吞吐 QPS 1200，P95 延迟 18ms",
            },
        )
        assert res_page["path"] == "out/gpu-compare.html"
        assert res_page["block_count"] == len(blocks)
        assert res_page["layout"] == "page"
        assert res_page["bytes"] > 500
        page_html = (work.path / "out" / "gpu-compare.html").read_text(encoding="utf-8")
        assert 'data-theme="dark"' in page_html
        assert (
            "default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
            in page_html
        )
        assert "data:image/png;base64," in page_html
        assert "data:image/jpeg;base64," in page_html
        assert "data:image/webp;base64," in page_html
        assert "data:image/gif;base64," in page_html
        assert "<script>" in page_html
        assert 'class="report-page"' in page_html

        # 2. layout="card" -> static=True, 780px, ZERO <script>, CSP without script-src
        res_card = render_report_artifact(
            work,
            {
                "path": "out/gpu-card.html",
                "blocks": blocks[:2] + [blocks[-1]],
                "theme": "light",
                "layout": "card",
                "source_text": "算力提升 35.7%，QPS 1200，P95 18ms",
            },
        )
        assert res_card["path"] == "out/gpu-card.html"
        assert res_card["layout"] == "card"
        card_html = (work.path / "out" / "gpu-card.html").read_text(encoding="utf-8")
        assert 'data-theme="light"' in card_html
        assert "default-src 'none'; img-src data:; style-src 'unsafe-inline'" in card_html
        assert "script-src" not in card_html
        assert "<script" not in card_html.lower()
        assert "vs-static" in card_html
        assert "vs-tab-section" in card_html
        assert "width=780" in card_html

        # 3. Source hallucination fails loudly
        with pytest.raises(ValueError, match="999"):
            render_report_artifact(
                work,
                {
                    "path": "hallucinated.html",
                    "blocks": [{"type": "stat", "label": "异常值", "value": "999%"}],
                    "source_text": "实测增长 15%",
                },
            )
        assert not (work.path / "out" / "hallucinated.html").exists()

        # 4. Path & image security rejections
        for bad_path in (
            "../escape.html",
            "/tmp/abs.html",
            "in/evil.html",
            "project/evil.html",
            "cache/evil.html",
            "out/bad.js",
            "out/.html",
            "CON.html",
        ):
            with pytest.raises(ValueError):
                render_report_artifact(
                    work,
                    {
                        "path": bad_path,
                        "blocks": [{"type": "note", "text": "test"}],
                    },
                )

        bad_images = [
            "https://example.com/a.png",
            "http://example.com/a.png",
            "in/../secret.png",
            "cache/secret.png",
            "in/fake.png",
            "in/missing.png",
        ]
        try:
            (work.path / "in" / "sym.png").symlink_to(outside)
            has_symlink = True
        except OSError:
            has_symlink = False
        if has_symlink:
            bad_images.append("in/sym.png")
        try:
            for img_path in bad_images:
                with pytest.raises(ValueError):
                    render_report_artifact(
                        work,
                        {
                            "blocks": [{"type": "image", "path": img_path}],
                        },
                    )
        finally:
            if has_symlink and (work.path / "in" / "sym.png").is_symlink():
                (work.path / "in" / "sym.png").unlink()

        # Oversized single image (> 3 MiB) and cumulative (> 8 MiB)
        big_png = _PNG_1X1 + b"\x00" * (MAX_IMAGE_BYTES + 10)
        (work.path / "in" / "too-big.png").write_bytes(big_png)
        with pytest.raises(ValueError, match="3 MiB"):
            render_report_artifact(work, {"blocks": [{"type": "image", "path": "in/too-big.png"}]})

        ok_2_9_mb = _PNG_1X1 + b"\x00" * (2_900_000)
        (work.path / "in" / "chunk.png").write_bytes(ok_2_9_mb)
        with pytest.raises(ValueError, match="8 MiB"):
            render_report_artifact(
                work,
                {
                    "blocks": [
                        {"type": "image", "path": "in/chunk.png"},
                        {"type": "image", "path": "in/chunk.png"},
                        {"type": "image", "path": "in/chunk.png"},
                    ]
                },
            )


def test_agent_runtime_calls_render_report_and_digest_feed_forward(tmp_path, monkeypatch):
    _ensure_render_report_registered(monkeypatch)
    store = ThreadStore(tmp_path / "threads.db")
    maker_profile = Profile(
        id="maker",
        title="创作者",
        tools=("render_report", "workspace_file", "result_attach"),
        access="everyone",
    )
    row = store.create(
        maker_profile,
        "运行时报告测试",
        chat_id="chat-rt",
        platform="qq",
        user_id="user-1",
        workspace="maker01",
    )
    vault = Vault(tmp_path / "secrets.vault")
    runtime = AgentRuntime(
        None,
        tmp_path / "data",
        store,
        SandboxSettings(enabled=False),
        vault,
        authorized=lambda r: True,
    )
    ctx = {
        **row,
        "profile_snapshot": asdict(maker_profile),
    }

    async def _run():
        try:
            res = await runtime.call(
                "render_report",
                {
                    "path": "summary.html",
                    "title": "运行时调用验证",
                    "blocks": [{"type": "stat", "label": "通过率", "value": "100%"}],
                    "source_text": "通过率 100%",
                },
                ctx,
            )
            assert res["path"] == "out/summary.html"
            assert res["layout"] == "page"

            # Non-workspace profile denied
            no_ws_profile = Profile(id="reader", title="只读", tools=("chat_read",), access="everyone")
            with pytest.raises(PermissionError):
                await runtime.call(
                    "render_report",
                    {"blocks": [{"type": "note", "text": "x"}]},
                    {
                        "thread_id": "t-agent-denied",
                        "workspace_id": "ws_denied01",
                        "profile_snapshot": asdict(no_ws_profile),
                    },
                )

            # digest_feed signature forwarding compatibility (both 2-arg and web/context-aware)
            import maisya.digest as digest_mod

            seen = {}

            async def fake_legacy(work, arguments):
                seen["legacy"] = (work.path.exists(), dict(arguments))
                return {"ok": True, "mode": "legacy"}

            async def fake_modern(work, arguments, *, web=None, context=None):
                seen["modern"] = (web is runtime.web, context is ctx)
                return {"ok": True, "mode": "modern"}

            monkeypatch.setattr(digest_mod, "run_digest_tool", fake_legacy)
            assert (await runtime.call("digest_feed", {"action": "status"}, ctx))["mode"] == "legacy"
            assert seen["legacy"][0] is True

            monkeypatch.setattr(digest_mod, "run_digest_tool", fake_modern)
            assert (await runtime.call("digest_feed", {"action": "status"}, ctx))["mode"] == "modern"
            assert seen["modern"] == (True, True)
        finally:
            await runtime.end(ctx["thread_id"])
            await runtime.close()

    asyncio.run(_run())
    outputs = runtime.files.read_checkpoint_outputs(row)
    assert any(path == "summary.html" for path, _data in outputs)


def test_presets_and_skill_no_forbidden_pronoun_and_maker_config(tmp_path, monkeypatch):
    _ensure_render_report_registered(monkeypatch)
    presets_root = ROOT / "presets"
    skill_path = presets_root / "skills" / "research-report" / "SKILL.md"
    profile_toml = presets_root / "profiles" / "maker" / "profile.toml"
    profile_md = presets_root / "profiles" / "maker" / "PROFILE.md"

    assert skill_path.is_file()
    for target in (skill_path, profile_toml, profile_md):
        content = target.read_text(encoding="utf-8")
        assert "她" not in content, f"禁止在 {target} 中出现'她'"

    assert "render_report" in profile_toml.read_text(encoding="utf-8")
    assert "research-report" in profile_toml.read_text(encoding="utf-8")
    assert "render_report" in profile_md.read_text(encoding="utf-8")
    assert "source_text" in skill_path.read_text(encoding="utf-8")
    assert 'layout="card"' in skill_path.read_text(encoding="utf-8")
    assert 'layout="page"' in skill_path.read_text(encoding="utf-8")

    store = ThreadStore(tmp_path / "threads.db")
    vault = Vault(tmp_path / "secrets.vault")
    profiles = Profiles(tmp_path / "profiles", contains_secret=vault.contains_secret)
    layers = MemoryLayers(tmp_path / "mem", store, profiles, vault)
    maker = layers.profiles.get("maker")
    assert "render_report" in maker.tools
    assert "research-report" in maker.skills
    assert "research-report" in layers.skills()
