"""Wave 2 v0.3 WebUI 静态红线、双主题、四页交互、五步引导及移动端鱼轮播 Playwright 验证。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEBUI = ROOT / "webui"
NOW_HTML = WEBUI / "now.html"


def test_build_check_and_static_redlines():
    build_py = WEBUI / "build.py"
    res = subprocess.run([sys.executable, str(build_py), "--check"], capture_output=True, text=True, check=False)
    assert res.returncode == 0, f"build.py --check failed: {res.stdout}\n{res.stderr}"

    assert NOW_HTML.exists()
    raw = NOW_HTML.read_bytes()
    assert len(raw) < 2 * 1024 * 1024, f"webui/now.html 体积超限: {len(raw)} bytes"
    html = raw.decode("utf-8")

    # CSP 与红线禁用扫描（保留标准 W3C SVG XML 命名空间 http://www.w3.org/2000/svg）
    assert not re.search(r"<img\b", html, re.I), "发现禁用标签 <img"
    assert not re.search(r"<iframe\b", html, re.I), "发现禁用标签 <iframe"
    assert "localStorage" not in html, "发现禁用 API localStorage"
    assert "sessionStorage" not in html, "发现禁用 API sessionStorage"
    non_svg_urls = re.findall(r"https?://(?!www\.w3\.org/2000/svg)", html, re.I)
    assert not non_svg_urls, f"发现禁用外链: {non_svg_urls}"
    assert not re.search(r"transition\s*:\s*all\b", html, re.I), "发现禁用样式 transition: all"
    assert "她" not in html, "发现禁用代词「她」"
    assert "/Users/" not in html, "发现本地路径泄露 /Users/"

    # 默认千禧机械主题 + 4 栏结构 + 移动端鱼轮播标识保留
    assert 'data-theme="millennium"' in html
    assert 'data-ui-style="millennium"' in html
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in html
    for nav_id in ('data-nav="now"', 'data-nav="profiles"', 'data-nav="library"', 'data-nav="settings"'):
        assert nav_id in html, f"缺少顶栏导航 {nav_id}"
    for dock_id in ('data-mob-nav="now"', 'data-mob-nav="profiles"', 'data-mob-nav="library"', 'data-mob-nav="settings"'):
        assert dock_id in html, f"缺少移动底栏 {dock_id}"
    for fish_sel in ("ab-mob-head", "mob-role-carousel", "mob-role-dots", "ab-mob-role-card1", "data-mob-role-jump"):
        assert fish_sel in html, f"缺少移动端鱼轮播关键标识 {fish_sel}"

    assert (WEBUI / "fragments" / "profiles.html").exists()
    assert (WEBUI / "fragments" / "library.html").exists()
    assert (WEBUI / "fragments" / "settings.html").exists()
    for legacy in ("abilities.html", "memory.html", "outputs.html"):
        assert not (WEBUI / "fragments" / legacy).exists(), f"废旧 fragment 未清理: {legacy}"


def _make_mock_maibot_init_script(*, onboarding_show: bool = True, empty_host_admins: bool = False) -> str:
    host_admins = [] if empty_host_admins else ["qq:private:90001"]
    return f"""
(() => {{
  const calls = [];
  let inflight = 0;
  let peakInflight = 0;
  const state = {{
    plugin_enabled: false,
    ui_style: "millennium",
    permission_mode: "ask_risky",
    default_model: "gpt-main",
    daily_proactive_limit: 3,
    sleep_window: "23:30-08:00",
    sandbox_enabled: true,
    enabled_chats: ["qq:group:100246809"],
    known_chats: ["qq:group:100246809", "qq:group:200112233"],
    host_admins: {json.dumps(host_admins)},
    plugin_admins: ["qq:private:10001"],
    grants: [{{ grant_key: "g1", scope: "qq:group:100246809", action_key: "workspace:write", tier: "always" }}],
    vault_meta: [{{ handle: "gh_ro", label: "GitHub 只读", domain: "api.github.com", fields: ["TOKEN"] }}],
    models: [
      {{ name: "gpt-main", provider: "openai" }},
      {{ name: "claude-aux", provider: "anthropic" }}
    ],
    profiles: [
      {{
        id: "default",
        name: "默认助手",
        description: "通用探索与对话收口",
        access: "everyone",
        enabled: true,
        model_name: "gpt-main",
        execution_mode: "host",
        session_mode: "persistent",
        shared_secrets: true,
        toolsets: ["context", "web", "workspace", "exec", "schedule"],
        skills: ["summarizer"],
        mcp: ["github_ro"],
        apis: [],
        profile_md: "# 默认助手\\n简洁高效完成委派。",
        fish_seed: ""
      }},
      {{
        id: "analyst",
        name: "深度研判",
        description: "长报告与日志分析",
        access: "admins",
        enabled: true,
        model_name: "claude-aux",
        execution_mode: "sandbox",
        session_mode: "ephemeral",
        shared_secrets: false,
        toolsets: ["context", "web", "workspace"],
        skills: ["summarizer"],
        mcp: [],
        apis: ["search_api"],
        profile_md: "# 深度研判",
        fish_seed: "c-angler-2-wink-analyst"
      }}
    ],
    intents: [
      {{ intent_id: "i-watch-1", kind: "watch", mode: "watch", reason: "盯梢构建状态", condition: "ci_status == 'failed'", status: "pending", chat_id: "qq:group:100246809", profile: "default", due_at: 1763200000 }},
      {{ intent_id: "i-sched-1", kind: "schedule", mode: "schedule", reason: "每日日报提醒", status: "paused", chat_id: "qq:group:100246809", profile: "analyst", daily_at: "09:00" }}
    ],
    threads: [
      {{
        thread_id: "th-run-1",
        chat_id: "qq:group:100246809",
        profile: "default",
        title: "排查依赖兼容问题",
        excerpt: "等待确认执行诊断命令",
        state: "waiting",
        total_tokens: 18500,
        updated: 1763199900,
        pending_approval: {{
          request_id: "req-1",
          action_key: "exec:shell",
          tool: "exec",
          summary: "运行 pytest 核验兼容",
          command: "pytest -q tests/smoke.py",
          risk_level: "中风险",
          reason: "将在宿主机读取仓库测试"
        }}
      }},
      {{
        thread_id: "th-idle-2",
        chat_id: "qq:group:100246809",
        profile: "analyst",
        title: "上一轮梳理归档",
        excerpt: "已输出结论摘要",
        state: "idle",
        total_tokens: 6400,
        updated: 1763195000,
        pending_approval: null
      }}
    ],
    onboarding: {{
      show: {"true" if onboarding_show else "false"},
      run_id: "run-42",
      sequence: 1,
      current_step: "hello",
      model_verify: null,
      recent_dms: [{{ chat_id: "qq:private:10002", name: "Bob" }}],
      known_chats: ["qq:group:100246809", "qq:group:200112233"],
      enabled_chats: ["qq:group:100246809"],
      host_admins: {json.dumps(host_admins)},
      plugin_admins: ["qq:private:10001"],
      default_model: "gpt-main",
      permission_mode: "ask_risky",
      plugin_enabled: false,
      sandbox_enabled: true,
      models: [{{ name: "gpt-main", provider: "openai" }}, {{ name: "claude-aux", provider: "anthropic" }}],
      items: [
        {{ key: "admins", required: true, state: "ok", title: "管理员权限", detail: "已设定", step: "admins" }},
        {{ key: "chats", required: true, state: "ok", title: "会话范围", detail: "1 个群", step: "chats" }},
        {{ key: "models", required: true, state: "warn", title: "模型连通", detail: "待探针验证", step: "runtime" }},
        {{ key: "default_profile", required: true, state: "ok", title: "默认档案", detail: "default", step: "hello" }},
        {{ key: "toolsets_mcp", required: false, state: "ok", title: "工具集", detail: "5/5", step: "runtime" }},
        {{ key: "runtime", required: false, state: "ok", title: "沙箱环境", detail: "就绪", step: "runtime" }},
        {{ key: "safety", required: true, state: "ok", title: "安全守门", detail: "ask_risky", step: "runtime" }}
      ]
    }},
    fsFiles: {{
      "MEMORY.md": "# 会话记忆\\n- 群友偏好简洁回答",
      "JOURNAL.md": "# 自身近况\\n- 完成 v0.3 重构准备",
      "profiles/custom_probe/note.txt": "可删除的自定义档案附属笔记"
    }},
    probeCalls: 0
  }};

  const tinyPngB64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

  function buildSnap(tid) {{
    const pick = state.threads.find((t) => t.thread_id === tid) || state.threads[0];
    return {{
      success: true,
      ui_style: state.ui_style,
      permission_mode: state.permission_mode,
      default_model: state.default_model,
      enabled_chats: state.enabled_chats,
      known_chats: state.known_chats,
      chat_sources: {{ "qq:group:100246809": {{ name: "极客测试群", platform: "qq", chat_id: "qq:group:100246809" }} }},
      admins_info: {{ host_admins: state.host_admins, plugin_admins: state.plugin_admins }},
      docker: {{ enabled: state.sandbox_enabled, available: true }},
      sandbox_enabled: state.sandbox_enabled,
      onboarding: state.onboarding,
      scheduled_intents: state.intents.filter((x) => x.kind === "schedule"),
      watches: state.intents.filter((x) => x.kind === "watch"),
      profiles: state.profiles.map((p) => p.id),
      profile_cards: state.profiles,
      available_models: state.models.map((m) => m.name),
      model_catalog: state.models,
      threads: state.threads,
      selected: pick ? {{
        ...pick,
        summary: "已收集测试依赖信息并等待命令执行确认。",
        steps: [
          {{ id: "st-1", seq: 1, kind: "trigger", title: "收到调度委派", status: "done", duration_ms: 12, command: "trigger:qq:group:100246809", redacted_output: "来源消息已核验" }},
          {{ id: "st-2", seq: 2, kind: "command", title: "预检本地环境", tool: "exec", status: "done", duration_ms: 230, command: "python3 --version", redacted_output: "Python 3.12 (已脱敏 TOKEN=***)" }}
        ],
        artifacts: [{{ id: "art-1", title: "诊断截图", path: "workspaces/shot.png", mime: "image/png" }}],
        context: {{
          tokens: 18500,
          token_limit: 128000,
          trigger_tokens: 96000,
          token_roles: {{ system: 3200, memory: 2400, user: 1900, assistant: 4500, tool: 6500 }},
          items: [
            {{ id: "i1", cat: "system", label: "核心规则", tokens: 3200, preview: "SYSTEM PROMPT" }},
            {{ id: "i2", cat: "tool", label: "命令输出", tokens: 6500, preview: "CMD OUTPUT" }}
          ]
        }}
      }} : null
    }};
  }}

  window.__maibotCalls = calls;
  window.__getPeakInflight = () => peakInflight;
  window.maibot = {{
    ready: Promise.resolve(),
    context: {{ theme: "light", dashboardStyle: "millennium" }},
    page: {{ state: {{ tab: "now" }} }},
    async query(name, arg) {{
      inflight++; if (inflight > peakInflight) peakInflight = inflight;
      calls.push({{ type: "query", name, arg }});
      try {{
        if (name === "snapshot") return buildSnap(arg?.thread_id);
        if (name === "read") {{
          const sec = arg?.section;
          const p = arg?.payload ? JSON.parse(arg.payload) : {{}};
          if (sec === "now_detail") return {{ success: true, ...buildSnap(p.thread_id), selected: buildSnap(p.thread_id).selected }};
          if (sec === "onboarding") return {{ success: true, onboarding: state.onboarding }};
          if (sec === "profiles") return {{
            success: true,
            profiles: state.profiles,
            skills: ["summarizer"],
            skill_items: [{{ id: "summarizer", title: "摘要技能", description: "方法技能" }}],
            connectors: {{
              mcp: [{{ id: "github_ro", name: "github_ro", description: "GitHub 只读", readonly: true }}],
              plugin_apis: [{{ id: "search_api", name: "search_api", api: "search_api", description: "检索 API", readonly: true }}],
            }},
            available_models: state.models.map((m) => m.name),
            model_catalog: state.models,
            default_model: state.default_model
          }};
          if (sec === "profile_detail") {{
            const pr = state.profiles.find((x) => x.id === p.profile_id) || state.profiles[0];
            return {{
              success: true,
              profile: pr,
              profile_md: pr.profile_md || "",
              connectors: {{
                mcp: [{{ id: "github_ro", name: "github_ro", description: "GitHub 只读", readonly: true }}],
                plugin_apis: [{{ id: "search_api", name: "search_api", api: "search_api", description: "检索 API", readonly: true }}],
              }},
            }};
          }}
          if (sec === "library") return {{ success: true, items: [
            {{ id: "att:att_img1", handle: "att_img1", title: "封面海报图", media_kind: "image", kind: "attachment", mime: "image/png", data_uri: `data:image/png;base64,${{tinyPngB64}}`, chat_id: "qq:group:100246809", profile: "default", thread_id: "th-run-1" }},
            {{ id: "ws:ws_1:out/report.md", title: "周度排查报告", media_kind: "markdown", kind: "workspace_out", workspace_id: "ws_1", path: "out/report.md", mime: "text/markdown", excerpt: "工作区报告", chat_id: "qq:group:100246809", profile: "analyst", thread_id: "th-idle-2" }}
          ] }};
          if (sec === "artifact") {{
            if (p.kind === "attachment" && p.handle === "att_img1") return {{ success: true, kind: "attachment", handle: "att_img1", mime: "image/png", base64: tinyPngB64 }};
            if (p.kind === "workspace_file" && p.workspace_id === "ws_1" && p.path === "out/report.md") return {{ success: true, kind: "workspace_file", mime: "text/markdown", text: "# 报告正文\\n工作区文件分块读取" }};
            return {{ success: false, error: `未知产物请求: ${{JSON.stringify(p)}}` }};
          }}
          if (sec === "fs_list") return {{ success: true, path: p.path || "", entries: [
            {{ name: "MEMORY.md", path: "MEMORY.md", kind: "file", size: 42, writable: true }},
            {{ name: "JOURNAL.md", path: "JOURNAL.md", kind: "file", size: 38, writable: true }},
            {{ name: "note.txt", path: "profiles/custom_probe/note.txt", kind: "file", size: 24, writable: true }},
            {{ name: "profiles", path: "profiles", kind: "dir" }}
          ].filter((e) => state.fsFiles[e.path] !== undefined || e.kind === "dir") }};
          if (sec === "fs_read") return {{ success: true, path: p.path, content: state.fsFiles[p.path] || "", version: 1, writable: true }};
          if (sec === "settings") return {{
            success: true,
            version: 1,
            plugin_enabled: state.plugin_enabled,
            ui_style: state.ui_style,
            permission_mode: state.permission_mode,
            known_chats: state.known_chats.map((cid) => ({{ stream_spec: cid, chat_id: cid, name: cid }})),
            known_chat_specs: state.known_chats,
            enabled_chats: state.enabled_chats,
            admins_info: {{ host_admins: state.host_admins, plugin_admins: state.plugin_admins }},
            grants: state.grants,
            vault_meta: state.vault_meta,
            available_models: state.models,
            docker: {{ enabled: state.sandbox_enabled, available: true }},
            settings: {{
              plugin: {{ enabled: state.plugin_enabled }},
              ui_style: state.ui_style,
              default_model: state.default_model,
              daily_proactive_limit: state.daily_proactive_limit,
              sleep_window: state.sleep_window,
              sandbox_enabled: state.sandbox_enabled,
              enabled_chats: state.enabled_chats,
              admins: state.plugin_admins
            }}
          }};
          return {{ success: true }};
        }}
        return {{ success: true }};
      }} finally {{
        inflight--;
      }}
    }},
    async action(name, arg) {{
      inflight++; if (inflight > peakInflight) peakInflight = inflight;
      calls.push({{ type: "action", name, arg }});
      try {{
        if (name === "note") return {{ success: true, thread_id: arg?.thread_id || "th-run-1" }};
        if (name === "write") {{
          const act = arg?.action;
          const p = arg?.payload ? JSON.parse(arg.payload) : {{}};
          if (act === "ui_style") {{
            state.ui_style = p.ui_style;
            return {{ success: true, ui_style: p.ui_style }};
          }}
          if (act === "thread_approve") {{
            const th = state.threads.find((t) => t.thread_id === p.thread_id);
            if (th) {{ th.pending_approval = null; th.state = "done"; }}
            return {{ success: true }};
          }}
          if (act === "thread_stop") {{
            const th = state.threads.find((t) => t.thread_id === p.thread_id);
            if (th) th.state = "idle";
            return {{ success: true }};
          }}
          if (act === "intent_pause") {{
            const it = state.intents.find((x) => x.intent_id === p.intent_id);
            if (it) it.status = "paused";
            return {{ success: true }};
          }}
          if (act === "intent_resume") {{
            const it = state.intents.find((x) => x.intent_id === p.intent_id);
            if (it) it.status = "pending";
            return {{ success: true }};
          }}
          if (act === "onboarding_advance") {{
            if (p.data && typeof p.data.plugin_enabled === "boolean") {{
              state.plugin_enabled = p.data.plugin_enabled;
              state.onboarding.plugin_enabled = p.data.plugin_enabled;
            }} else if (p.data?.plugin && typeof p.data.plugin.enabled === "boolean") {{
              state.plugin_enabled = p.data.plugin.enabled;
              state.onboarding.plugin_enabled = p.data.plugin.enabled;
            }}
            state.onboarding.current_step = p.target_step || "done";
            state.onboarding.sequence = Math.max(state.onboarding.sequence + 1, p.sequence || 1);
            return {{ success: true, onboarding: state.onboarding }};
          }}
          if (act === "onboarding_verify_model") {{
            state.probeCalls++;
            const callIdx = state.probeCalls;
            if (callIdx === 1) {{
              await new Promise((r) => setTimeout(r, 180));
              return {{ success: false, run_id: p.run_id, sequence: p.sequence, probe_sequence: p.probe_sequence, error: "STALE_ERROR_FROM_FIRST_PROBE", model_verify: {{ run_id: p.run_id, sequence: p.sequence, state: "error", message: "STALE_ERROR_FROM_FIRST_PROBE" }} }};
            }}
            await new Promise((r) => setTimeout(r, 15));
            return {{ success: true, run_id: p.run_id, sequence: p.sequence, probe_sequence: p.probe_sequence, model_verify: {{ run_id: p.run_id, sequence: p.sequence, state: "ok", duration_ms: 22, message: "MODEL_OK_22ms" }} }};
          }}
          if (act === "onboarding_complete") {{
            state.onboarding.show = false;
            return {{ success: true, onboarding: state.onboarding }};
          }}
          if (act === "onboarding_reopen") {{
            state.onboarding.show = true;
            state.onboarding.run_id = "run-43";
            state.onboarding.current_step = "hello";
            return {{ success: true, onboarding: state.onboarding }};
          }}
          if (act === "profile_create") {{
            state.profiles.push({{ ...p }});
            return {{ success: true, profile: p }};
          }}
          if (act === "profile_update") {{
            const i = state.profiles.findIndex((x) => x.id === p.id);
            if (i >= 0) state.profiles[i] = {{ ...state.profiles[i], ...p }};
            return {{ success: true }};
          }}
          if (act === "profile_delete") {{
            if (p.profile_id === "default") return {{ success: false, error: "默认档案不可删除" }};
            state.profiles = state.profiles.filter((x) => x.id !== p.profile_id);
            return {{ success: true }};
          }}
          if (act === "profile_export_zip") {{
            return {{ success: true, filename: `${{p.profile_id}}.zip`, base64: "UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==" }};
          }}
          if (act === "fs_write") {{
            state.fsFiles[p.path] = p.content;
            return {{ success: true, version: 2 }};
          }}
          if (act === "fs_delete") {{
            delete state.fsFiles[p.path];
            return {{ success: true, deleted: true, path: p.path }};
          }}
          if (act === "settings_update") {{
            if (p.settings?.admins && p.settings.admins.length === 0 && state.host_admins.length === 0) {{
              return {{ success: false, error: "至少保留一位管理员" }};
            }}
            if (typeof p.settings?.plugin_enabled === "boolean") state.plugin_enabled = p.settings.plugin_enabled;
            else if (typeof p.settings?.plugin?.enabled === "boolean") state.plugin_enabled = p.settings.plugin.enabled;
            if (p.settings?.admins) state.plugin_admins = p.settings.admins;
            if (p.settings?.enabled_chats) state.enabled_chats = p.settings.enabled_chats;
            return {{ success: true, version: 2 }};
          }}
          if (act === "vault_set") {{
            const fieldNames = Array.isArray(p.fields) ? p.fields : Object.keys(p.fields || {{ TOKEN: 1 }});
            state.vault_meta.push({{ handle: p.handle, label: p.label, domain: p.domain, fields: fieldNames }});
            return {{ success: true }};
          }}
          return {{ success: true }};
        }}
        return {{ success: true }};
      }} finally {{
        inflight--;
      }}
    }}
  }};
}})();
"""


@pytest.mark.parametrize("reduced_motion", [False, True])
def test_desktop_4_pages_onboarding_race_guard_and_interactions(reduced_motion: bool):
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 960},
            reduced_motion="reduce" if reduced_motion else "no-preference",
        )
        ctx.add_init_script(_make_mock_maibot_init_script(onboarding_show=True, empty_host_admins=True))
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)

        page.goto(NOW_HTML.as_uri(), wait_until="domcontentloaded")

        # 1. 五步引导自动弹出 + 7 项检查清单 + Tab 焦点围栏 + Step 4 防竞态探测
        onb_scrim = page.locator("#onb-scrim")
        onb_scrim.wait_for(state="visible", timeout=5000)
        assert page.locator(".onb-item").count() == 7

        # Tab 围栏留在 #onb-card 内
        for _ in range(5):
            page.keyboard.press("Tab")
            inside = page.evaluate("() => document.getElementById('onb-card')?.contains(document.activeElement)")
            assert inside, "Tab 焦点逃逸出引导弹窗"

        # 推进到第 3 步 chats -> 初次冷启动即使后端 plugin_enabled=false 也默认勾选总开关；再推进到第 4 步 runtime
        page.locator("#onb-next").click()
        page.locator("#onb-next").click()
        page.locator("#onb-plugin-enabled").wait_for(state="visible")
        assert page.locator("#onb-plugin-enabled").is_checked()
        page.locator("#onb-next").click()
        page.locator("#onb-probe-btn").wait_for(state="visible")

        # 连续两次点击“测试模型连通”：第一次慢失败、第二次快成功 -> 必须保留第 2 次结果、丢弃过期失败！
        page.locator("#onb-probe-btn").click()
        page.locator("#onb-probe-btn").click()
        page.wait_for_timeout(280)
        probe_text = page.locator("#onb-probe-tag").inner_text()
        assert "MODEL_OK_22ms" in probe_text, f"模型连通结果异常: {probe_text}"
        assert "STALE_ERROR_FROM_FIRST_PROBE" not in probe_text, "未丢弃过期探测响应"

        # 进入第 5 步并完成引导（校验 runtime 步骤传出 sandbox: {enabled}）
        page.locator("#onb-next").click()
        assert "7 项就绪清单" in page.locator("#onb-stage").inner_text()
        adv_calls = page.evaluate(
            """() => window.__maibotCalls.filter(c => c.type === 'action' && c.name === 'write' && c.arg?.action === 'onboarding_advance').map(c => JSON.parse(c.arg.payload))"""
        )
        assert any(isinstance(c.get("data", {}).get("sandbox"), dict) for c in adv_calls)
        assert any(c.get("data", {}).get("plugin_enabled") is True and c.get("data", {}).get("plugin", {}).get("enabled") is True for c in adv_calls)
        page.locator("#onb-next").click()
        onb_scrim.wait_for(state="hidden")

        # 2. 双主题切换：millennium -> modern -> auto -> millennium
        assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "millennium"
        assert page.evaluate("() => document.documentElement.getAttribute('data-ui-style')") == "millennium"
        page.locator("#ui-style-toggle").click()
        assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "modern"
        assert page.evaluate("() => document.documentElement.getAttribute('data-ui-style')") == "modern"
        page.locator("#ui-style-toggle").click()
        assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "auto"
        page.locator("#ui-style-toggle").click()
        assert page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "millennium"

        # 3. 现场页：预定与盯梢 + 审批条 + 垂直步骤时间线 + Treemap 保留与详情切换
        assert "ci_status == 'failed'" in page.locator("#now-intents").inner_text()
        page.locator("#now-approval-bar").wait_for(state="visible")
        assert page.locator("#ctx-map .cm-tile").count() >= 2

        # 点选执行步骤 -> 查看命令与脱敏输出 -> 返回总览
        page.locator("#now-step-timeline .now-step-btn").nth(1).click()
        page.locator("#now-view-step").wait_for(state="visible")
        assert "python3 --version" in page.locator("#now-step-args").inner_text()
        assert "Python 3.12" in page.locator("#now-step-output").inner_text()
        page.locator("#now-back-overview").click()
        page.locator("#now-view-overview").wait_for(state="visible")

        # 点击“本轮会话允许”
        page.locator('[data-appr-decision="session"]').click()
        page.wait_for_timeout(80)
        appr_call = page.evaluate(
            """() => window.__maibotCalls.find(c => c.type === 'action' && c.name === 'write' && c.arg?.action === 'thread_approve')"""
        )
        assert appr_call and json.loads(appr_call["arg"]["payload"])["decision"] == "session"

        # 4. 档案页：Hermes 渐进式披露 + 150 行计数 + default 删除保护 + 新建/导出/删除
        page.locator('[data-nav="profiles"]').click()
        page.locator("#pr-id").wait_for(state="visible")
        assert page.locator("#pr-adv-body").is_hidden()
        assert page.locator("#pr-btn-delete").is_disabled()
        page.locator("#pr-adv-toggle").click()
        page.locator("#pr-adv-body").wait_for(state="visible")
        page.locator("#pr-md").fill("line1\nline2\nline3")
        assert "3 / 150 行" in page.locator("#pr-md-count").inner_text()
        page.locator('[data-pr-tab="abilities"]').click()
        ab_text = page.locator("#pr-pane-abilities").inner_text()
        assert "5 大工具集" in ab_text
        assert "github_ro" in ab_text and "search_api" in ab_text

        # 新建并删除自定义档案，校验对齐后端字段枚举（无 connectors/chats/thread）
        page.locator("#pr-btn-new").click()
        page.locator("#pr-id").fill("custom_probe")
        page.locator("#pr-name").fill("自定义探针")
        page.locator("#pr-desc").fill("测试新建能力")
        page.locator("#pr-btn-save").click()
        page.locator('[data-pick-profile-id="custom_probe"]').wait_for(state="visible")
        create_call = page.evaluate(
            """() => window.__maibotCalls.find(c => c.type === 'action' && c.name === 'write' && c.arg?.action === 'profile_create')"""
        )
        assert create_call
        create_payload = json.loads(create_call["arg"]["payload"])
        assert "connectors" not in create_payload
        assert create_payload["access"] == "everyone"
        assert create_payload["session_mode"] == "persistent"
        assert isinstance(create_payload["mcp_servers"], list)
        assert isinstance(create_payload["apis"], list)

        page.locator("#pr-btn-export").click()
        page.wait_for_timeout(50)
        page.locator("#pr-btn-delete").click()
        page.wait_for_timeout(80)

        # 5. 资料库页：无 <img>/<iframe> + CSS 图片舞台 + workspace_out -> workspace_file + 系统文件编辑删除
        page.locator('[data-nav="library"]').click()
        page.locator("#lib-gallery .lib-card").first.wait_for(state="visible")
        page.locator("#lib-gallery .lib-card").first.click()
        page.locator("#lib-drawer .lib-img-stage").wait_for(state="visible")
        assert page.locator("#lib-art-delete").is_hidden()
        bg = page.locator("#lib-drawer .lib-img-stage").evaluate("el => getComputedStyle(el).backgroundImage")
        assert "data:image/png;base64," in bg
        assert page.locator("img, iframe").count() == 0

        # 点击工作区产物 -> 验证传给 read("artifact") 的 kind == "workspace_file"
        page.locator("#lib-gallery .lib-card").nth(1).click()
        page.locator("#lib-stage pre").wait_for(state="visible")
        assert "工作区文件分块读取" in page.locator("#lib-stage pre").inner_text()

        page.locator('#lib-fs-tree [data-fs-path="MEMORY.md"]').click()
        page.locator("#lib-fs-editor").wait_for(state="visible")
        assert page.locator("#lib-fs-delete").is_hidden()
        page.locator("#lib-fs-editor").fill("# 更新记忆")
        page.locator("#lib-fs-save").click()
        page.wait_for_timeout(50)
        page.locator('#lib-fs-tree [data-fs-path="profiles/custom_probe/note.txt"]').click()
        page.locator("#lib-fs-delete").wait_for(state="visible")
        page.locator("#lib-fs-delete").click()
        page.wait_for_timeout(50)

        # 6. 设置页：4 块结构 + known_chat_specs/list[dict] 会话不显 [object Object] + 防自锁死报错 + Vault 只存不显
        page.locator('[data-nav="settings"]').click()
        for blk in ("#set-block-chats", "#set-block-auth", "#set-block-vault", "#set-block-runtime"):
            page.locator(blk).wait_for(state="visible")
        chat_list_txt = page.locator("#set-chat-list").inner_text()
        assert "qq:group:100246809" in chat_list_txt and "qq:group:200112233" in chat_list_txt
        assert "object Object" not in chat_list_txt
        assert page.locator("#set-plugin-enabled").is_checked()
        page.locator("#set-plugin-enabled").uncheck()
        page.wait_for_timeout(60)
        assert not page.locator("#set-plugin-enabled").is_checked()
        page.locator("#set-plugin-enabled").check()
        page.wait_for_timeout(60)
        assert page.locator("#set-plugin-enabled").is_checked()

        # 尝试移除最后一位管理员 -> 触发防自锁死错误提示
        page.locator('[data-set-del-admin="qq:private:10001"]').click()
        assert "管理员" in page.locator("#set-admin-err").inner_text()

        # 写入 Vault -> 秘密输入框立即清空、不回显且 fields 仅为字段名数组
        page.locator("#set-vault-handle").fill("demo_pat")
        page.locator("#set-vault-label").fill("演示凭据")
        page.locator("#set-vault-domain").fill("api.example.com")
        page.locator("#set-vault-secret").fill("TOKEN=secret-xyz-999")
        page.locator("#set-vault-save").click()
        page.wait_for_timeout(80)
        assert page.locator("#set-vault-secret").input_value() == ""
        assert "secret-xyz-999" not in page.locator("#set-vault-list").inner_text()
        assert "demo_pat" in page.locator("#set-vault-list").inner_text()
        vault_call = page.evaluate(
            """() => window.__maibotCalls.find(c => c.type === 'action' && c.name === 'write' && c.arg?.action === 'vault_set')"""
        )
        assert vault_call
        vault_payload = json.loads(vault_call["arg"]["payload"])
        assert vault_payload["fields"] == ["TOKEN"]

        assert page.evaluate("() => window.__getPeakInflight()") <= 2
        browser.close()

    assert not errors, f"检测到控制台或运行时报错: {errors}"


def test_mobile_narrow_viewport_fish_carousel_and_4col_dock():
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
        ctx.add_init_script(_make_mock_maibot_init_script(onboarding_show=False, empty_host_admins=False))
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)

        page.goto(NOW_HTML.as_uri(), wait_until="domcontentloaded")
        page.locator("#now-intents").wait_for(state="visible")

        # 移动底部 4 栏导航可见且无横向溢出
        dock = page.locator("#ms-mob-dock")
        dock.wait_for(state="visible")
        assert dock.locator(".ms-mob-dock-btn").count() == 4
        cols = dock.evaluate("el => getComputedStyle(el).gridTemplateColumns.trim().split(/\\s+/).length")
        assert cols == 4, f"移动端底栏列数应为 4，实测 {cols}"
        overflow = page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
        assert not overflow, "现场页窄屏出现横向溢出"

        # 切换到档案页 -> 校验保留的像素鱼横向轮播器 + 概览卡 + 底部弹层
        page.locator('[data-mob-nav="profiles"]').click()
        page.locator("#ab-mob-head").wait_for(state="visible")
        page.locator("#mob-role-carousel .mob-role-item").first.wait_for(state="visible")
        assert page.locator("#mob-role-carousel .mob-role-item").count() == 2
        assert page.locator("#mob-role-carousel svg.fish").count() == 2
        assert page.locator(".m-ab-roster").is_hidden()

        first_title = page.locator("#ab-mob-role-title").inner_text()
        page.locator(".mob-role-step.next").click()
        second_title = page.locator("#ab-mob-role-title").inner_text()
        assert first_title != second_title

        # 打开移动端鱼定制抽屉
        page.locator('[data-mob-role-jump="fish"]').click()
        page.locator("#profiles-mob-scrim .m-mob-sheet").wait_for(state="visible")
        page.locator("#profiles-mob-sheet-close").click()
        page.locator("#profiles-mob-scrim").wait_for(state="hidden")

        assert not page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
        assert page.evaluate("() => window.__getPeakInflight()") <= 2
        browser.close()

    assert not errors, f"检测到移动端控制台或运行时报错: {errors}"
