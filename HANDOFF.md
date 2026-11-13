# Maisya Handoff

写给下一位接手者（人或 agent）。只记录需要上手才知道的状态、事实、红线、验证方式与上线前待办；设计与规则的权威来源是根目录 [AGENTS.md](AGENTS.md)、[codemap.md](codemap.md) 与 [docs/](docs/01-架构蓝图.md)。

最后更新：2026-11-13（`main` 提交 `dfdd896` 之后收尾交接）。已完成 M0–M6 全量架构落地、工作流自由化、MaiBot 千禧机械风 × Aisya 实时流 **五页玻璃盒管理台**（`现场 | 产物 | 能力 | 手账 | 设置`）、**角色（`profile`）启停开关 + 默认关闭的 `digest` 资讯简报预设工作流**、以及 **围绕 `SKILL.md` 的画廊优先工作台与 `.zip`/`.skill`/`.tar.*` 技能包导入导出**；当前累计通过 **210 项**本地与 Playwright Chromium UI 测试、**22 项**真实宿主 SDK 隔离契约（`host_unchanged: true`）、隔离实模 + EXA 第三方联网冒烟与独立 Docker/`runsc` gVisor 运行时验收。

## 1. 一句话定位与核心不变式

- **唯一主体**：MaiBot 是唯一对话主体，保留原有人设、日常 RP 闲聊节奏与表达方式；Maisya 不抢话筒。
- **四个核心概念**：
  1. **角色（`Profile`）**：可调度的 Agent 执行能力（`scout` / `maker` / 默认关闭的 `digest` 及派生角色），支持 `enabled` 一键启停。
  2. **Agent 线程（`Thread`）**：可启动、续接、立即转向（`steer`）、停止与绑定持久工作区的执行上下文。
  3. **意图（`Intent`）**：所有异步唤醒（后台结果 `return`、事后惦记、`self` 偶遇 `serendipity`、久违 `longing`、定时、外部事件）统一落入 `intents`，由 `Pulse` 过社交分寸闸唤醒。
  4. **手账与方法（`Memory Layers`）**：
     - 宿主 `A_Memorix` 负责群友印象与长期聊天记忆（线程内按需只读检索 `memory_read`）；
     - Maisya「手账」只存本会话办事约定（`MEMORY.md`）、近 7 天自身经历（`JOURNAL.md`）、探索脾性与方向（`HEARTBEAT.md`）以及 `SKILL.md` / `NOTES.md`。
- **planner 工具边界**：可见工具仅 `maisya_agent`、`maisya_recall`；延迟工具仅 `maisya_later`、`maisya_shape`（管理员核验）。
- **WebUI 与预设硬约束**：
  - 严禁把「现场」做成工单/派单视角（底部「唤醒能力」为折叠抽屉，主操作为线程实时流 +「引导」/`steer`）。
  - `webui/` 与 `presets/` 下禁止出现「她」指代 MaiBot；遵守宿主严格 CSP（零 `<img>`、零 `<iframe>`、零 `localStorage`/`sessionStorage`、零 `transition: all`、峰值并发 RPC `<= 2`）。
  - 修改界面必须编辑 [webui/now.html.in](webui/now.html.in)、[webui/management.html.in](webui/management.html.in)、[webui/fragments/*.html](webui/fragments/) 与 [webui/millennium.css](webui/millennium.css)，再用 `python3 webui/build.py` 生成单文件 HTML，勿直接手改生成的 `webui/*.html`。
  - **预设文件注意**：不要往 `presets/profiles/scout/profile.toml` 或 `maker/profile.toml` 添加 `skills = []`（`tests/test_memory.py` 会直接向 `scout/profile.toml` 追加 `skills = ["fonts"]`）。
  - **角色 `enabled` 规则**：`Incoming.load()` 使用 `strict_enabled=False` 校验既有 `webhooks.json`，确保后续停用某角色不会卡死插件加载；实际执行入口（`AgentRuntime.available()`、`Incoming.receive()`、`Pulse`、`console_send`、`schedule_intent`）强制要求 `enabled = True`。

## 2. 当前里程碑与验证状态

| 范围 | 状态 | 依据 |
|---|---|---|
| M0 基座 / M1 线程 / M2 回接 | 已实现、加固并验证 | **210 项**本地与 UI 测试 + **22 项**宿主 SDK 隔离契约（含 `web_provider` 多源映射、`send.hybrid` `file` 拆条与 `before_planner` 精简） |
| M3 `maker` + 工作区 + 沙箱 + `@API` | **真实 `runsc` 验收通过；无容器可用** | `tests_sandbox/run_isolated.py --execute` 全绿（`host_unchanged` / `backup_cleaned`）；支持无容器 `workspace_file`/`html_render`/`result_attach`、管理员可写 MCP/跨插件 `@API`/自定义模板与 `sandbox_targets` |
| M4 手账 / Skill 画廊与压缩包 / 塑形 | 已实现并验证 | 画廊优先 `能力` 工作台；`.zip`/`.skill`/`.tar.gz`/`.tgz`/`.tar`/`.tar.bz2`/`.tar.xz` 24 KiB 分块导入导出；保留 `references/`·`scripts/`·`assets/` 伴随文件并在运行时只读挂载到 `in/skills/<id>/`；`Learning._learn` 真实 LLM 冒烟 `applied` |
| M5 意图 / `Pulse` / 克制主动性 / `digest` 预设 | 已实现并验证 | `daily_proactive_limit`、`chat_proactive_levels`、`acquaintance_users`、`longing_quiet_hours`、`serendipity`、管理员周期工作流；默认关闭的 `digest` 角色 + `news-digest` 技能 + `digest_feed`（SSRF/XXE 防护、14 天试订/退订状态机、五维打分硬筛与离线天光简报卡） |
| M6 五页玻璃盒 WebUI 与三路配置合并 | 已实现并验证 | `现场`（跨平台会话头像/名称、FLOW 拓扑、五段上下文、引导 `steer`）、`产物`（交付物/图片预览/持续工作区）、`能力`（Skill 画廊/工作台 + 角色名册与启停开关）、`手账`（`A_Memorix` 边界清晰）、`设置`（按会话主动度、快捷调度预设） |
| 真实模型 + 第三方搜索插件 / EXA 冒烟 | **隔离全链通过** | `tests_host/run_live_smoke.py --execute`（独立 `MAIBOT_INSTANCE_DIR`，未开宿主 `web_search`）：`scout` 自动兼容 `google-search-plugin`（EXA）返回真实来源链接、`web_fetch` 成功、`maker` 走 `planner` 槽位、`Learning._learn` 回执 `applied` |
| **部署到线上 `plugins/` 与真群联调** | **唯一未执行（红线保护）** | `plugins/`、`src/`、`config/`、`data/` 零改动；需下一会话取得用户当次部署授权并指定测试群 |

## 3. 本地与隔离验证命令

```bash
# 1. WebUI 构建一致性检查
python3 webui/build.py --check

# 2. 全量本地与 Chromium UI 测试（当前 210 passed）
PYTHONPATH=.:.local-checks/deps ~/.venvs/maiwork/bin/python -m pytest tests -q -rs -p no:cacheprovider

# 3. 真实宿主 venv 隔离契约（22 tests，自动校验 host_unchanged）
python3 tests_host/run_contract.py

# 4. 隔离实模联网冒烟（--execute 才真实调用）
python3 tests_host/run_live_smoke.py --execute

# 5. 独立 Docker/gVisor 运行时验收（--execute 会建临时规则并在完成后自动清理）
python3 tests_sandbox/run_isolated.py --execute

# 6. 构建沙箱镜像（默认 dry-run）
./sandbox/build_image.sh
```

若 `.local-checks/deps` 未就绪：
`python -m pip install --target .local-checks/deps 'cryptography>=46,<51' 'jsonschema>=4.23,<5'`

## 4. 下一会话接手与上线前核查清单

按线上红线，**当前本地已无需再补核心功能**，下一会话重点是**线上部署前核查与真群联调**：

1. **读取本地隐私配置**：先读取未入库的本地 `AGENTS.local.md` 获取 SSH 连接方式、宿主根目录路径与用户指定测试群（**绝不将 IP、路径或密钥写入受 Git 跟踪的文件或聊天输出**）。
2. **取得用户当次授权再动线上**：每次往线上 `plugins/` 放入/更新代码会触发全插件热重载；执行前先向用户确认部署授权与本次测试群号，并将旧插件与相关数据库备份至 `/root/backups/`。
3. **核查线上运行条件**：
   - **联网回退**：当前宿主 `web_search` 未分配模型时，Maisya 默认 `web_provider = "auto"` 会自动复用线上 `xxxxx7258.google-search-plugin`（EXA 等多引擎链）；若宿主后续开启统一联网，`auto` 会自动切回。
   - **宿主 `utils` 首选模型顺序**：先前实模冒烟曾观测到 `[model_task_config.utils]` 首选 `gpt-6-luna` 同时带 `function tools` 与 `reasoning_effort` 会触发 `400 BadRequestError` 并回退下一模型；上线前建议核查宿主 `utils` 模型顺序，避免每步多一次 400 重试延迟。
   - **沙箱镜像**：仅当开启 `sandbox.enabled = true` 时，确认宿主已就绪 `maisya-sandbox:py313`（必要时运行 `./sandbox/build_image.sh --execute`）。
4. **部署后与真群端到端验收**：
   - 确认热重载后宿主其他插件与 `A_Memorix` 工作正常，无报错卡死；
   - 在用户指定的测试群中逐项实测：日常闲聊/RP 是否原味不受干扰、`scout` 同步返回与异步回接、含链接/图片/群文件的 `share` 拆条交付（`send.hybrid`）、`Pulse` 社交分寸闸表现，以及真实宿主控制台打开 5 个自定义页面是否正常；
   - 测试收尾后按要求清理测试状态并回读确认。

## 5. 工作方式红线（摘自 `AGENTS.md`）

宿主代码零改动；不重启任何线上服务；开发/契约测试在 `plugins/` 外的独立目录进行；往线上 `plugins/` 放改删触发热重载需用户当次同意；密钥与服务器地址不落仓库、不打印；先备份再改；不要使用 `git add .`（避免带入 `AGENTS.local.md` 与 `.local-checks/`）。
