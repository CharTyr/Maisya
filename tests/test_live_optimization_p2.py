"""针对线上 P2 激活已建底座能力（auto 沙箱、digest STATE.md、Learning 观测）的 TDD 验收测试。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from maisya.abilities import Profile, Profiles
from maisya.digest import DigestWorkspace
from maisya.learning import Learning
from maisya.maker import AgentRuntime
from maisya.memory import MemoryLayers
from maisya.sandbox import ProcessResult, Sandbox, SandboxSettings
from maisya.threads import ThreadStore
from maisya.vault import Vault
from maisya.workspaces import Workspaces


@pytest.mark.asyncio
async def test_p2_1_effective_mode_auto_default_preset_and_bridge_probe_hint(tmp_path: Path):
    store = ThreadStore(tmp_path / "m.db")
    vault = Vault(tmp_path / "vault")
    runtime = AgentRuntime(
        SimpleNamespace(),
        tmp_path,
        store,
        SandboxSettings(enabled=False),
        vault,
        authorized=lambda *a, **k: True,
    )
    auto_prof = Profile(
        id="default",
        execution_mode="auto",
        toolsets=("context", "web", "workspace", "exec"),
    )

    # 1. effective_mode 识别 execution_mode="auto"
    runtime.docker_available = lambda: True
    assert runtime.effective_mode(auto_prof) == "sandbox"
    runtime.docker_available = lambda: False
    assert runtime.effective_mode(auto_prof) == "host"

    # 2. 预设 default 档案已切为 execution_mode = "auto"
    presets = Profiles(ROOT / "presets" / "profiles")
    assert presets.get("default").execution_mode == "auto"

    # 3. 存量未改动的旧 default/profile.toml 自动迁移到 execution_mode = "auto"，已定制文件保持不动
    old_default_toml = (
        'title = "全能默认助手"\n'
        'name = "全能默认助手"\n'
        'description = "MaiBot 默认全能探索与创作执行助手，整合联网查证、图文对比卡片与报告、工作区持续修改、脚本运行与延后自唤醒。"\n'
        'icon_shape = "compass"\n'
        'theme_color = "#2F6FED"\n'
        "enabled = true\n"
        "bound = true\n"
        'access = "everyone"\n'
        'session_mode = "ephemeral"\n'
        'execution_mode = "host"\n'
        'permission_mode = "ask_risky"\n'
        "shared_secrets = true\n"
        'task = "utils"\n'
        'environment = "readonly"\n'
        'network = "public"\n'
        'toolsets = ["context", "web", "workspace", "exec", "schedule"]\n'
    )
    pdir = tmp_path / "profiles_upgrade"
    (pdir / "default").mkdir(parents=True)
    (pdir / "default" / "profile.toml").write_text(old_default_toml, encoding="utf-8")
    (pdir / "default" / "PROFILE.md").write_text("# default\n", encoding="utf-8")
    upgraded = Profiles(pdir)
    assert upgraded.get("default").execution_mode == "auto"

    pdir_custom = tmp_path / "profiles_custom"
    (pdir_custom / "default").mkdir(parents=True)
    (pdir_custom / "default" / "profile.toml").write_text(
        old_default_toml.replace('task = "utils"', 'task = "planner"'),
        encoding="utf-8",
    )
    (pdir_custom / "default" / "PROFILE.md").write_text("# default\n", encoding="utf-8")
    kept_custom = Profiles(pdir_custom)
    assert kept_custom.get("default").execution_mode == "host"

    # 4. Sandbox.probe() 在显式 network_mode="bridge" 且 bridge 网络不可用时给出排障提示
    sb_settings = SandboxSettings(enabled=True, network_mode="bridge")
    sb = Sandbox(sb_settings, store, authorized=lambda *a, **k: True)

    async def _fake_runner(args, **kwargs):
        cmd = " ".join(args)
        if "info" in cmd:
            return ProcessResult(0, '{"Runtimes":{"runc":{}},"CgroupVersion":"2"}')
        if "image inspect" in cmd:
            return ProcessResult(0, "[]")
        if "network inspect bridge" in cmd:
            return ProcessResult(1, "Error: No such network: bridge")
        if "ps -aq" in cmd:
            return ProcessResult(0, "")
        return ProcessResult(0, "")

    sb.runner = _fake_runner
    orig_which = __import__("shutil").which
    import maisya.sandbox as sb_mod
    sb_mod.shutil.which = lambda name: "/usr/bin/docker" if name == "docker" else orig_which(name)
    try:
        ready = await sb.probe()
        assert ready is False
        assert "bridge" in sb.error and "network_mode" in sb.error
    finally:
        sb_mod.shutil.which = orig_which


@pytest.mark.asyncio
async def test_p2_2_digest_auto_state_md_and_learning_skip_diagnostics(tmp_path: Path):
    # 1. digest score_select & render_card_html 自动写入 <=250 字 project/STATE.md 并同步到 state_note
    store = ThreadStore(tmp_path / "m.db")
    files = Workspaces(tmp_path / "ws", store)
    prof = Profile(id="digest", tools=("workspace_file", "digest_feed"))
    row = store.create(prof, "早报", chat_id="c1", platform="qq", user_id="u1", workspace="digest")
    with files.activity(row) as work:
        dw = DigestWorkspace(work, clock=lambda: 1794900000.0)
        dw.tz_offset_minutes = 480

        # 预先写入候选池让 score_select 命中一条有效新闻
        pool_url = "https://example.com/news/ai-chip"
        dw._save_pool(
            {
                "total_discovered": 1,
                "by_url": {
                    "example.com/news/ai-chip": {
                        "url": pool_url,
                        "url_key": "example.com/news/ai-chip",
                        "title": "新一代 AI 芯片发布",
                        "source": "TechDaily",
                        "published_at": "2026-11-18T08:00:00Z",
                        "snippet": "全新架构能效提升显著",
                        "source_text": "全新架构能效提升显著，实测功耗降低 40%，将于下季度量产交付。",
                    }
                },
            }
        )
        sel = dw.score_select(
            [
                {
                    "url": pool_url,
                    "title": "新一代 AI 芯片正式发布，功耗降低四成",
                    "summary": "全新架构能效提升显著，实测功耗降低 40%，将于下季度量产交付，适合端侧推理部署。",
                    "why": "能效跃升直接利好本地模型部署",
                    "topic": "AI硬件",
                    "quote": "实测功耗降低 40%",
                    "scores": {"info": 5, "source": 4, "relevance": 5, "timeliness": 5, "chat": 4, "novelty": 5},
                }
            ],
            max_items=5,
        )
        assert len(sel["selected"]) == 1
        state_file = work.path / "project" / "STATE.md"
        assert state_file.is_file(), "score_select 应自动写入 project/STATE.md"
        state_text = state_file.read_text(encoding="utf-8")
        assert len(state_text) <= 250
        assert "AI硬件" in state_text
        assert "1 条" in state_text or "入选 1" in state_text

        # 同步到 SQLite state_note
        with store.connect() as db:
            ws_note = db.execute("SELECT state_note FROM agent_workspaces LIMIT 1").fetchone()[0]
        assert ws_note.strip() != ""

        # render_card_html 亦刷新 STATE.md 且保持 <=250 字
        dw.render_card_html(sel["selected"], group_title="早间简报", slot_label="早间")
        state_text2 = state_file.read_text(encoding="utf-8")
        assert len(state_text2) <= 250
        assert "已出卡" in state_text2 or "已渲染" in state_text2

    # 2. Learning 跳过原因统计（诊断可观测，不削弱审核门）
    mem = MemoryLayers(
        tmp_path / "mem", store, Profiles(tmp_path / "profs"), Vault(tmp_path / "v2")
    )
    learner = Learning(SimpleNamespace(), mem)
    # 非自主角色 -> not_autonomous
    await learner.schedule(
        {
            "thread_id": "th_0000000000000001",
            "completed_revision": 1,
            "profile": "admin_only",
            "profile_snapshot": {"access": "admins", "bound": True, "enabled": True},
            "result": {"text": "done"},
        }
    )
    # 空文本结果 -> empty_text
    await learner.schedule(
        {
            "thread_id": "th_0000000000000002",
            "completed_revision": 1,
            "profile": "default",
            "profile_snapshot": {"access": "everyone", "bound": True, "enabled": True},
            "result": {"text": ""},
        }
    )
    # 无失败恢复轨迹 -> no_recovery_trace
    await learner.schedule(
        {
            "thread_id": "th_0000000000000003",
            "completed_revision": 1,
            "profile": "default",
            "profile_snapshot": {"access": "everyone", "bound": True, "enabled": True},
            "result": {"text": "一次就成功了"},
        }
    )
    diag = learner.diagnostics()
    assert diag["skipped"]["not_autonomous"] == 1
    assert diag["skipped"]["empty_text"] == 1
    assert diag["skipped"]["no_recovery_trace"] == 1
