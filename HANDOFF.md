# Maisya Handoff

写给下一位接手者（人或 agent）。只记录需要上手才知道的状态、事实、红线、验证方式与上线前待办；设计与规则的权威来源是根目录 [AGENTS.md](AGENTS.md)、[codemap.md](codemap.md) 与 [docs/](docs/01-架构蓝图.md)。

最后更新：2026-11-15（正式发版 **`v0.2.0`**，合入 **Always-On & Dual-Arena Waves 1–4**、**Hermes 式自适应 256K Token 上下文预算与两级压缩**、**WebUI `<100ms` 响应性能优化**与 **`现场` FLOW `03` 角色联动筛选、`04` 活跃/有效产出净视图与废记录清理/删除保护**）。已完成 M0–M6 全量架构落地、工作流自由化、Maisya × MaiWork **WebUI v2.6 双主题五页工作台**（`现场 | 产物 | 能力 | 手账 | 设置`，桌面端与移动端底栏/抽屉、SVG 图标体系与单入口侧边栏，默认协调 MaiBot 宿主的原味千禧机械风 `millennium` + 可选清朗手账风 `maiwork`，支持顶栏即时切换与 `ui_style` 配置持久化；移除全部口号并把说明文字收进 `?` 帮助图标弹窗；完整保留 `现场` `FLOW → STATUS/TELEMETRY → CONTEXT → ACTIVITY/LIVE → 折叠唤醒` 架构并将 FLOW `02` 列更正为 `02 主脑调度 · MaiBot Planner`；`产物` 升级为统一 `资料空间` 分类导航/列表网格/最近 5 项视图；`设置` 与角色面板区分「一般设置 / 高级设置」，角色支持宿主机密脱敏后的多模型顺次分配与回退 `model_name + fallback_models`）、**全系统消息流统一 `<platform>:<group|private>:<id>` 格式与普通用户产品化体验**（所有配置、白名单、角色作用域、定时/Webhook 与五页 WebUI 统一显示及提交如 `qq:group:100246809`，后台双向兼容解析 MaiBot 32 位哈希 `session_id`；`设置` 白名单改为条目卡片 + 每条会话独立分寸选择器 + 「从已发现会话挑选 / 平台·类型·群号账号」三段式添加；免打扰改为整点时间段开关；心跳角色与定时目标改为下拉选择；`运行状态速览` / `宿主探测` / `自动笔记回执` / `现场` 动态步骤均由原始 JSON 与内部代号改为结构化中文卡片）、**管理员一段话对话式全量配置与跨群定时工作流（`maisya/shaping.py`：`maisya_shape` 支持 `overview` 一览及配置/角色/技能/手账/服务/Webhook 增改、版本回滚、防明文与防自锁死；群号/群名/MD5 `session_id` 别名互通与预置冷群路由；未启用会话管理员有界内存引导与私聊 `/vault set`；`maisya_later` `daily_at` 不漂移每日钟点、`replace` 覆盖去重与 `scheduled_admin` 保护；`maisya_agent` `target_chat` 跨群试跑投递唤醒）**、**角色（`profile`）启停开关 + 角色专属程序化内联 SVG 小鱼头像（12 鱼种 × 18 色 × 11 眼神，支持定制与随机换鱼，`webui/fish.js`）+ 对齐 MaiWork 视觉品质的 `digest` 资讯简报预设工作流（`maisya/digest_card.py` 5 套天空主题与 `600px@2x=1200px` 浅色图卡、`judge_cover`、内联可视化 `viz`、默认关闭按群配好再启用）**、**围绕 `SKILL.md` 的画廊优先工作台与 `.zip`/`.skill`/`.tar.*` 技能包导入导出**、**`planner` 多 Agent 编排与灵魂缝合（无容器轻量创作降级、`from_threads` 结论+成品全量接力、`Share` `<planner_intent>` 语境传达、闲聊精简注入、`maisya_later` 自然语言周期工作流）**、**`1 + 2` Grok 式能力配方模板分享（本地 `.zip`/`.tar.gz`/包文本 + 后端防 SSRF 公开链接安全拉取预览、草稿隐私体检、本机环境缺件对齐与平滑升级回滚）**、**Wave 1 插件市场化、与 MaiWork 选型文档与跨平台兼容**、**Always-On & Dual-Arena Waves 1–4（Inbox-first Webhook 落盘排队与交互/后台双并发车道 `kick_queued`、非阻塞 `Pulse._dispatch` 与周期定时单次失败续排 + `<previous_runs>` 继承、零空闲 I/O `HEARTBEAT.md` 缓存与 30s `_longing` 节流、多轮结构化压缩目录与同日 `JOURNAL.md` 合并、`longing_quiet_hours` 默认 `0` 关闭/`12..720` 开启、私聊严格 `enabled_chats` 白名单与大小写敏感熟人判断、群/私分舞台主动额度与 `failed` 不扣额、群参与式 `unreplied_lock` 解锁、群聊 `stop`/`steer`/个人委托归属保护、Private<->Group 单向隐私壁垒、群滚动 `<maisya_return>` 锚定，以及结合线程/记忆语料的双舞台 `serendipity`）**、**Hermes 式自适应 256K Token 上下文预算与两级压缩（[maisya/context_budget.py](maisya/context_budget.py)）**、**WebUI `<100ms` 响应性能优化（[maisya/trust.py](maisya/trust.py) 双文件 WAL `(mtime_ns, size)` 签名内存路由索引与单事务 `upsert_routes`、[plugin.py](plugin.py) `O(1)` 别名索引与 `0.25s` 有界非阻塞头像解析、[maisya/console.py](maisya/console.py) 首屏默认选中首线程、[maisya/management.py](maisya/management.py) `batch` 并行聚合读取与并行 `discovery` + `60s` 模型目录缓存、[webui/now.html.in](webui/now.html.in) / [webui/management.html.in](webui/management.html.in) 去重 `queryInFlight` 轮询与启动瀑布流）**，以及 **`现场` FLOW `03` 角色联动筛选、`04` 活跃/有效产出净视图与废记录清理/删除保护（[maisya/threads.py](maisya/threads.py) `delete_thread` / `prune_junk_threads`、[maisya/console.py](maisya/console.py) `junk` / `can_delete`、[maisya/management.py](maisya/management.py) 清理写操作、[webui/now.html.in](webui/now.html.in) / [webui/management.html.in](webui/management.html.in) 角色联动、空状态直达发起与配置跳转、废记录清理与单条删除，同时保留附件产物并拦截运行中及持久工作区记录）**。当前本地主分支已通过 **350 项**本地与 Playwright Chromium UI 测试、**22 项**真实宿主 SDK（`2.23.0` / `MaiBot 1.4.6`）隔离契约，经 170 活跃会话只读实机尺度基准验证全读路径热中位 `<85ms`，正准备执行 **`v0.2.0`** 发版与线上同步。

## 1. 一句话定位与核心不变式

- **唯一主体**：MaiBot 是唯一对话主体，保留原有人设、日常 RP 闲聊节奏与表达方式；Maisya 不抢话筒。
- **四个核心概念**：
  1. **角色（`Profile`）**：可调度的 Agent 执行能力（`scout` / `maker` / 默认关闭的 `digest` 及派生角色），支持 `enabled` 一键启停。
  2. **Agent 线程（`Thread`）**：可启动、续接、立即转向（`steer`）、停止与绑定持久工作区的执行上下文。
  3. **意图（`Intent`）**：所有异步唤醒（后台结果 `return`、事后惦记、`self` 偶遇 `serendipity`、久违 `longing`、定时、外部事件）统一落入 `intents`，由 `Pulse` 过社交分寸闸唤醒。
  4. **手账与方法（`Memory Layers`）**：
     - 宿主 `A_Memorix` 负责群友印象与长期聊天记忆（线程内按需只读检索 `memory_read`）；
     - Maisya「手账」只存本会话办事约定（`MEMORY.md`）、近 7 天自身经历（`JOURNAL.md`）、探索脾性与方向（`HEARTBEAT.md`）以及 `SKILL.md` / `NOTES.md`。
- **planner 工具边界**：可见工具为 `maisya_agent`（支持已核验管理员 `target_chat` 跨群触发、`deliver_to='current'` 私聊回传预览、仅传 `thread_id` 跨群续接/停止与 `share` 预览后推群）、`maisya_recall`（支持管理员 `target_chat=.../'all'` 跨全部会话召回）、`maisya_later`（支持自然语言安排单次提醒、跨群 `daily_at` 不漂移每日工作流与 `replace` 去重，以及未启用管理员私聊个人提醒）；延迟工具为 `maisya_shape`（管理员核验，支持先 `tool_search` 展开后读取 `overview(target_chat=...)` 及在任意未加白名单私聊按任意顺序修改/回滚配置、角色、技能、跨群手账、`digest_sources` RSS 源订退、服务与配方）；未启用会话仅对已核验管理员开放有界内存引导、跨群操作与个人直达投递，非服务群与非管理员维持零落盘。
- **无容器轻量工作区降级**：当 `sandbox.enabled = false` 或容器运行时未就绪时，不含私有凭据/受限出站/固定凭据模板的创作角色（如预设 `maker`）自动降级为 `light_workspace` 模式可用（保留 `workspace_file`、`html_render`、`result_attach` 及宿主/联网工具，仅隐去并拦截 `sandbox_exec`；容器就绪后自动恢复完整沙箱能力）；含 `secrets`、`network = "restricted"` 或 `sandbox_targets` 的严格沙箱角色仍强制要求容器就绪。
- **WebUI 与预设硬约束**：
  - 严禁把「现场」做成工单/派单视角（底部「唤醒能力」为折叠抽屉，主操作为线程实时流 +「引导」/`steer`）。
  - `webui/` 与 `presets/` 下禁止出现「她」指代 MaiBot；遵守宿主严格 CSP（零 `<img>`、零 `<iframe>`、零 `localStorage`/`sessionStorage`、零 `transition: all`、峰值并发 RPC `<= 2`）。
  - 修改界面必须编辑 [webui/now.html.in](webui/now.html.in)、[webui/management.html.in](webui/management.html.in)、[webui/fragments/*.html](webui/fragments/) 与 [webui/millennium.css](webui/millennium.css)，再用 `python3 webui/build.py` 生成单文件 HTML，勿直接手改生成的 `webui/*.html`。
  - **预设文件注意**：不要往 `presets/profiles/scout/profile.toml` 添加 `skills = []`（`tests/test_memory.py` 会直接向 `scout/profile.toml` 追加 `skills = ["fonts"]`）。`presets/profiles/maker/profile.toml` 已内置 `render_report` 工具与 `skills = ["research-report"]`，`Profiles.export()` 在本地工作区未铺开 `skills/` 时会自动回退到 `presets/skills/`；`presets/profiles/digest/profile.toml` 默认 `max_steps = 20`。
  - **角色 `enabled` 规则**：`Incoming.load()` 使用 `strict_enabled=False` 校验既有 `webhooks.json`，确保后续停用某角色不会卡死插件加载；实际执行入口（`AgentRuntime.available()`、`Incoming.receive()`、`Pulse`、`console_send`、`schedule_intent`）强制要求 `enabled = True`。

## 2. 当前里程碑与验证状态

| 范围 | 状态 | 依据 |
|---|---|---|
| M0 基座 / M1 线程 / M2 回接 | 已实现、加固并验证 | **350 项**全量本地与 UI 测试 + **22 项**宿主 SDK 隔离契约（含 `web_provider` 多源映射、`send.hybrid` `file` 拆条、`Share` `<planner_intent>` 语境传达、`from_threads` 结论+`out/*`+附件全量接力、闲聊精简 `before_planner`、角色多模型顺次 fallback、统一 `<platform>:<group|private>:<id>` 消息流格式与 `session_id` 双向别名映射、以及未启用会话管理员内存自助引导） |
| M3 `maker` + 工作区 + 沙箱 + `@API` | **真实 `runsc` 验收通过；无容器自动降级可用** | `tests_sandbox/run_isolated.py --execute` 全绿（`host_unchanged` / `backup_cleaned`）；`maker` 未开容器时自动以 `light_workspace` 模式保留 `workspace_file`/`html_render`/`result_attach`，开启容器自动恢复 `sandbox_exec`；支持管理员可写 MCP/跨插件 `@API`/自定义模板与 `sandbox_targets` |
| M4 手账 / Skill 画廊 / 配方分享 / 塑形 | 已实现并验证 | 画廊优先 `能力` 工作台；技能包分块导入导出与伴随文件挂载；`maisya/shaping.py` 对话式全量塑形（`overview`、配置抗自锁死与历史回滚、角色/技能/跨群手账/服务/Webhook、`Vault` 凭据句柄与防明文拦截）；`maisya/recipes.py` `1 + 2` 能力配方模板分享（草稿资产勾选与隐私/凭据体检、公开 GitHub/归档链接防 SSRF 拉取预览、对照宿主 `discovery`/保险箱/服务/沙箱缺件对齐、平滑升级保留白名单/启停/工作区并可回滚、推荐例行任务一键挂载）；`Learning._learn` 真实 LLM 冒烟 `applied` |
| M5 意图 / `Pulse` / 克制主动性 / `digest` 预设 | 已实现并验证 | `maisya_later` `daily_at` 钟点不漂移每日工作流、`replace` 覆盖去重与 `scheduled_admin` 非管理员取消保护；跨群预先建流 `session_routes` 与试跑投递唤醒；`Pulse` `<maisya_workflow>` 唤醒与纯链接/正文自动 `bridge.stage` 预装载；`daily_proactive_limit`、`chat_proactive_levels`、`acquaintance_users`、`longing_quiet_hours`、`serendipity`；默认关闭的 `digest` 角色 + `news-digest` 技能 + `digest_feed` |
| M6 五页玻璃盒 WebUI 与三路配置合并 | 已实现并验证 | 双主题体系（`millennium` 默认协调 MaiBot 宿主的 0px 质感千禧机械主题 + `maiwork` 可选清朗手账主题，顶栏 `#ui-style-toggle` 即时切换与 `ui_style` 持久化）；全页零口号并用 `?` 帮助弹窗收纳说明文字；`现场`（顶部 `01→04 FLOW` 拓扑、状态与读数条、上下文窗口+示波器趋势、近期活动+活动现场引导、底部折叠唤醒抽屉）、`产物`（交付物/图片预览/持续工作区与角色小鱼）、`能力`（Skill 画廊/工作台 + 角色名册与默认折叠高级覆盖字段 + 配方分享导入面板）、`手账`（分册徽章与 `?` 指引 + 直达意图队列与自动笔记回执 + `A_Memorix` 边界）、`设置`（一般设置 vs 高级设置分级、预设快捷调度、动态自定义联网展开） |
| 真实模型 + 第三方搜索插件 / EXA 冒烟 | **隔离全链通过** | `tests_host/run_live_smoke.py --execute`（独立 `MAIBOT_INSTANCE_DIR`，未开宿主 `web_search`）：`scout` 自动兼容 `google-search-plugin`（EXA）返回真实来源链接、`web_fetch` 成功、`maker` 走 `planner` 槽位、`Learning._learn` 回执 `applied` |
| Wave 1 跨平台、市场元数据与 3-OS CI | **已合流并验证（`v0.1.0`）** | `_manifest.json` 规范 `0.1.0` / `GPL-3.0-or-later` / `display.icon` / `urls` 并在宿主测试包纳入 `logo.svg`；新增 `.github/workflows/tests.yml`（Ubuntu/macOS/Windows Python 3.13）；`README.md` 补充市场安装与 MaiWork 对比；新增 `maisya/pathsafe.py`，打通 TG `::tg-topic::mt=` 话题流、QQ 官方大小写 OpenID、Docker+NapCat `base64://` 群文件兜底与 `render.html2png` 缺浏览器提示 |
| Always-On & Dual-Arena Waves 1–4 | **已合流验证并同步线上** | **+24 项**专项测试（`test_always_on_engine.py` 10 / `test_always_on_dual_arena.py` 9 / `test_always_on_pulse.py` 5）与全量 **350 passed** + **22/22** 宿主契约：Inbox-first 持久队列与交互/后台并发车道、非阻塞 `Pulse` 调度与单次失败续排、零空闲磁盘/SQLite 轮询、多轮压缩目录与同日 `JOURNAL.md` 合并、`longing_quiet_hours=0` 默认关闭、私聊严格 `enabled_chats` 白名单、群/私分舞台主动额度与 `failed` 不扣额、群参与式解锁与个人委托/`steer`/`stop` 归属保护、Private<->Group 单向隐私隔离、群滚动 `<maisya_return>` 锚定及多语料双舞台 `serendipity` |
| Hermes 式自适应 256K Token 上下文预算与两级无损压缩 | **已合流验证并同步线上** | **+7 项**专项测试（`test_context_budget_and_compaction.py`）与全量 **350 passed** + **22/22** 宿主契约：`thread_context_tokens=262144` 默认 256K 窗口与 `Profile.context_tokens` 覆盖、混合中英文 Token 估算、55% 水位 Tier 1 零成本旧工具输出去重折叠、75% 水位 Tier 2 滚动结构化交班单与正则精确锚点本 + 单次溢出恢复、`thread_read` 16K 原文回读、取消 `idle_seconds` 强制截断，以及 `现场` / `设置` Token 计量视图 |
| WebUI `<100ms` 响应性能优化 | **已合流验证并同步线上** | **+4 项**专项性能与并发测试（`test_webui_perf.py`）+ 全量 **350 passed** + **22/22** 宿主契约；只读复制线上 `data/maisya` 与全部 `170` 条 `chat_sessions` 的隔离实机尺度基准显示：`cold_console=115.78ms`，热中位（10 次）`console 71.29ms`、`outputs 6.92ms`、`abilities 71.83ms`、`memory 5.99ms`、`settings 74.69ms`、`batch_settings 81.08ms`、`manage_discovery_warm 3.47ms`（修复前为 N+1 SQLite 扫描 + 阻塞式 `chat.get_avatar` 瀑布流 + `abilities` / `settings` 串行首屏请求） |
| `现场` FLOW 角色联动、净视图与废记录清理 | **已合流验证（`v0.2.0`）** | **+2 项**专项测试（`test_now_flow_and_cleanup.py`）+ 全量 **350 passed** + **22/22** 宿主契约：`FLOW 03 角色` 对称点击筛选、无在场活动时展示直达发起与跳去配置操作卡；`FLOW 04` 默认过滤已停未交付/报错/空结果废记录并保留底部活动追溯与 `maisya_recall`；一键「清理废记录 (`prune_threads`)」及单条「删除 (`delete_thread`)」严格跳过 `active`/`waiting` 与持久工作区，批量清理保护附件与临时工作区 `out/`，单条显式删除仍完整保留 `attachments` 产物 |
| **部署到线上 `plugins/` 与热重载验证** | **前版 `main@45dd8fc` 已在线；待同步 `v0.2.0`** | 最近线上快照位于服务端备份目录 `maisya-deploy-20261115-154408`，当前将执行 `v0.2.0` 备份、原子同步与热重载验证 |

## 3. 本地与隔离验证命令

```bash
# 1. WebUI 构建一致性检查
python3 webui/build.py --check

# 2. 全量本地与 Chromium UI 测试（实测 350 passed；隔离宿主契约 22/22 OK）
PYTHONPATH=.:tests:.local-checks/deps ~/.venvs/maiwork/bin/python -m pytest tests -q -rs -p no:cacheprovider

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

## 4. 接手维护、线上核查与真群开群前指引

截至 2026-11-15（`v0.2.0`），最新代码已准备发版并同步至线上插件目录，线上 `digest` 角色维持按需启用；后续维护与按需开群请遵循以下核查项：

1. **本地隐私文件仅留本地**：`AGENTS.local.md`、`config.toml` 与 `.local-checks/` 已在 `.gitignore` 屏蔽（绝不将服务器 IP、SSH 路径、密钥或真实群号写入受 Git 跟踪的文件或聊天输出）。
2. **线上默认安全开关与核查点**：
   - **默认静默待命**：线上保持 `enabled = true`、`enabled_chats = []`、`[sandbox] enabled = false`；五页管理台已可用且不主动读取或接入任何未启用群。
   - **联网回退**：当宿主统一 `web_search` 未开启或未配模型时，`web_provider = "auto"` 会自动复用宿主侧已加载的第三方搜索插件或已登记服务；若宿主后续开启统一联网，`auto` 会自动优先切换。
   - **宿主 `utils` 槽位提示**：此前隔离实模冒烟曾观测到若宿主某模型槽位首选模型不支持 `tools + reasoning_effort`，会触发一次 400 回退延迟；建议在 `能力 -> 角色管理` 为常用角色显式配好 `model_name` + `fallback_models` 顺序链。
   - **沙箱镜像**：仅当准备开启 `[sandbox] enabled = true` 时，才需在宿主构建 `maisya-sandbox:py313`（`./sandbox/build_image.sh --execute`）；未开启时 `maker` 与 `digest` 默认以 `light_workspace` 模式完成持久工作区、HTML 渲染与文件交付。若无容器精简环境首次做图提示缺少 Chromium，需在**运行 MaiBot 的宿主**执行 `playwright install chromium`（Linux 按需补充 `install-deps chromium`）。
   - **多平台与跨容器适配**：`allow_chats` / `allow_users` 兼容 `telegram:group:<chat_id>::tg-topic::mt=<topic_id>` 话题流，并原样保留 QQ 官方 OpenID 大小写；Docker + NapCat 跨容器路径隔离时，QQ 群文件投递自动降级走 `adapter.napcat.file.upload_group_file` `base64://` 兜底。
3. **正式拉入真群与每日资讯启用方式**：
   - 在 Maisya 管理台 `设置 -> 一般设置 -> 启用会话白名单` 用条目添加目标流（如 `qq:group:12345678`），或由管理员在私聊发一段话经 `maisya_shape` 一站式开启；
   - 启用「每日资讯」可直接在私聊交代启用 `digest`、配好 `digest_sources`、用 `maisya_later(daily_at=..., target_chat=...)` 挂每日排班，并可用 `deliver_to="current"` 在私聊试跑验收后再推往目标群。
4. **若需再次同步线上插件**：必须先取得用户当次明确授权，将旧插件与活数据（SQLite 必须用只读 `.backup()`）归档到服务端备份目录，再原子更新 `plugins/CharTyr_Maisya/` 并保留线上 `config.toml`，严禁改动宿主代码或重启线上服务。

- **角色模型清单完整性**：`MaisyaPlugin` 启动及刷新能力/概览时按 mtime/size 增量只读解析宿主 `config/model_config.toml` 中的全量 `[[models]]`（含已配置但未分配任务槽位、尚无统计用量的模型，不读取 `api_providers`/`api_key`/`base_url`，且不将 `web_search` 混入模型选项），配合热重载订阅并在管理台「模型与权限配置」提供「同步模型列表」按钮。
- **线上已发布修复汇总（`maisya-deploy-20261114-162806`）**：已全量部署同名多 `session_id` 按活跃账号对齐与空 `account_id` 补齐（`trust.py` / `shaping.py` / `plugin.py`）、`digest_feed` 数值 `source` 评分解析与 `render_card_html` 自动回读本轮入选条目（`digest.py`）、`maisaka.planner.before_request` 在追加 `items` 时保留 `item_schema_version`（`plugin.py`），以及全量宿主 `[[models]]` 清单读取与 WebUI「同步模型列表」（待在测试群下一次自然对话或 `19:30` 每日简报触发时验收真实出图投递效果）。

## 5. 工作方式红线（摘自 `AGENTS.md`）

宿主代码零改动；不重启任何线上服务；开发/契约测试在 `plugins/` 外的独立目录进行；往线上 `plugins/` 放改删触发热重载需用户当次同意；密钥与服务器地址不落仓库、不打印；先备份再改；不要使用 `git add .`（避免带入 `AGENTS.local.md` 与 `.local-checks/`）。
