# Maisya 代码地图（Codemap）

> 描述 `Maisya` 插件仓的真实文件布局、职责边界、Tool / API / Hook 入口与 SQLite 数据表映射。

---

## 1. 仓库顶层结构

```
Maisya/
├── .github/workflows/tests.yml   # 3-OS（Ubuntu / macOS / Windows）Python 3.13 CI 工作流
├── __init__.py                   # 插件包导出
├── _manifest.json                # 宿主 manifest v2：chartyr.maisya、display.icon、urls、24 项 capabilities
├── plugin.py                     # 宿主装配根：生命周期、4 个 Tool、5 个 Hook、1 个 Command、7 个 API
├── webui.json                    # 宿主 WebUI v2 声明：5 个工作区自定义页面（now/outputs/abilities/memory/settings）
├── config.example.toml           # 默认配置模板（enabled=false、enabled_chats=[]、sandbox.enabled=false）
├── LICENSE                       # GNU GPL v3 开源协议
├── logo.svg                      # 插件矢量图标（千禧机械边框 + 小鱼剪影）
├── README.md                     # 给用户看的项目主页与极速上手
├── AGENTS.md                     # 工程红线与 10 条架构不变式
├── HANDOFF.md                    # 当前交付快照、验收基线与接手核查项
├── codemap.md                    # 本代码地图
├── maisya/                       # 28 个核心 Python 运行时模块 + viz.css 样式资产
├── presets/                      # 预置灵魂、角色、技能与心跳模板
├── sandbox/                      # 容器镜像定义（Dockerfile、build_image.sh）
├── webui/                        # 双主题五页 WebUI 源码模板与编译产物
├── docs/                         # 工程文档（00~06）、用户手册（guide/）与脱敏演示截图（images/）
├── tests/                        # 本地单元与 Playwright Chromium UI 测试套件
├── tests_host/                   # 独立隔离目录宿主 SDK 契约与真实工具冒烟脚本（打包同步含 logo.svg 与 viz.css）
└── tests_sandbox/                # Docker + gVisor (runsc) 运行时体检与隔离验收脚本
```

---

## 2. `plugin.py` 宿主挂载面（实核）

- **生命周期与配置联动**：
  - `MaisyaPlugin.on_load()`：初始化 SQLite 存储、工作区、手账、保险箱、权限信任、服务/Webhook、`Bridge`/`Share`/`Pulse` 与 `Scout`/`Maker` 引擎，并同步宿主 `[[models]]` 脱敏清单至 `host_models.json`。
  - `MaisyaPlugin.on_config_update()`：响应 `("bot", "model")` 等宿主配置热更新。
  - `MaisyaPlugin.on_unload()`：有序排空线程、清理有界管理员引导缓存并关闭资源。
- **Planner 工具（仅 3 个可见 + 1 个延迟）**：
  - `maisya_agent`（`visibility="visible"`）：`start` / `send` / `stop` / `share`，支持并行 `wait_seconds=0`、接力 `from_threads`、工作区归一（`digest-feeds`→`digest`）、管理员跨会话 `target_chat` 与投递方式 `deliver_to`。
  - `maisya_recall`（`visibility="visible"`）：按 `scope` 与 `kind` 检索过往线程、后台经历、`MEMORY.md` 与 `JOURNAL.md`，支持管理员 `target_chat="all"` 跨全部会话。
  - `maisya_later`（`visibility="visible"`）：`schedule` / `list` / `cancel`，支持 `kind=later|proactive|silent`、`daily_at`（HH:MM 不漂移）、`digest` 空 prompt 自动补全、`replace` 覆盖与管理员跨会话 `target_chat`。
  - `maisya_shape`（**无 `visibility` 标记，保持延迟工具**）：管理员一站式配置中心（新增复合 `setup` 一站式开通/偏好/试订/定时/试跑，并保留 `overview`/`settings`/`profile`/`skill`/`memory`/`digest_sources`/`notes`/`journal`/`heartbeat`/`services`/`webhooks`/`recipe` 细粒度操作）。
- **宿主 Hooks & Command**：
  - `chat.receive.after_process`（`maisya_receive`）：只读记录可信消息、`new_input`、`note_received`；未启用会话的已核验管理员写入 300s 内存缓存。
  - `maisaka.planner.before_request`（`maisya_planner`）：剔除内置 `subagent*`，按启用态与管理员权限裁剪档案/工具、提示延迟展开 `maisya_shape`、注入线程索引与手账快照。
  - `maisaka.planner.after_response`（`maisya_planner_observe`）：关联本轮启动/补充调用到回接组并收尾权限快照。
  - `maisaka.replyer.before_request`（`maisya_replyer`）：单次垫入已核实线程结果，或因已由 `share` 精确投递而抑制重复回复。
  - `send_service.before_send`（`maisya_redact`）：出站凭据与私密路径脱敏，非管理员消息剥离全体提及。
  - `@Command("vault")`（`^/vault(?:\s|$)`）：私聊录入/删除凭据句柄。
- **管理台与外部 `@API`（共 7 个）**：
  - 实时现场：`maisya_now`、`maisya_event`、`maisya_note`、`maisya_stop`
  - 后台管理：`maisya_manage_read`、`maisya_manage_write`
  - 外部接入：`maisya_incoming_event`（`public=True`，HMAC 验签 Webhook）

---

## 3. `maisya/` 模块职责表（28 个 Python 模块 + `viz.css`）

| 模块 | 职责与核心类/函数 |
|---|---|
| [config.py](maisya/config.py) | `PluginSettings`、`Settings`（默认 `ui_style="millennium"`、`proactive_level="care"`）、三路合并（默认 < 文件 < 管理台已保存覆盖）与抗自锁死校验 |
| [trust.py](maisya/trust.py) | 可信来源消息 `Message` 校验、`session_routes` 双向映射、`<platform>:<group\|private>:<id>`（兼容 TG `::tg-topic::mt=` 话题与大小写敏感 OpenID）<-> MD5 `session_id` 解析、白名单与管理员鉴权 |
| [abilities.py](maisya/abilities.py) | `Profile` / `Profiles`（含 `"render_report"` 进 `WORKSPACE_TOOLS`、幂等预设升级、`export()` 回退预设技能、`model_name` + `fallback_models` 顺序回退链、`allowed_chats`、兼容 `<platform>:private:<id>` 与大小写敏感 `allow_users`、技能与工具挂载边界）与 `Skills` 校验管理 |
| [memory.py](maisya/memory.py) | `MemoryLayers`：全局技能、最新别名优先的会话 `MEMORY.md`、自视角 `JOURNAL.md`、`HEARTBEAT.md`、`memory_versions` 版本历史/回滚，以及 `learning_receipts`/`experience_receipts` |
| [threads.py](maisya/threads.py) | `ThreadStore`、`agent_threads` / `thread_events` / `agent_workspaces` / `thread_context`；支持 `chat_aliases` 与预策划冷流工作区无缝接管 |
| [workspaces.py](maisya/workspaces.py) | `Workspaces` / `WorkingCopy`：隔离临时拷贝 -> 验证后原子提交至 `in/`、`project/`、`out/`，附件清单（`attachments`）与防穿越文件打开 |
| [pathsafe.py](maisya/pathsafe.py) | 跨平台文件系统安全原语：根目录逃逸/UNC/符号链接校验、大小写一致路径规范化、跨平台文件锁，以及隔离最小子进程环境变量构造 |
| [scout.py](maisya/scout.py) | `Scout` 只读侦察与规划循环（注入实时北京时间防搜旧年份、`TOOL_DEFINITIONS` 含 `render_report` 与 `digest_feed.collect|seed`）、`model_list` 多模型异常顺次 fallback、`web.search` / `google-search-plugin` 自适应与阶段限步 |
| [maker.py](maisya/maker.py) | `Maker` 创作与多工具步进（整合文件编辑、`render_report` 调研图文排版、HTML 出图及缺浏览器诊断提示、附件声明、自定义 `services`、`sandbox_exec` 与 `digest_feed`） |
| [viz_spec.py](maisya/viz_spec.py) | Intelligent UI 共享组件目录（16 类模块）、`news`/`report` 档校验、`numbers_in` / `check_spec_numbers` 防幻觉数字核验，以及动态/静态 `render()` |
| [viz.css](maisya/viz.css) | Intelligent UI 共享明暗双模、表格/趋势/柱条/比较/时间线/静态展开样式表（`load_viz_css()` 内联进 HTML，无外部 URL） |
| [report.py](maisya/report.py) | `render_report_artifact`：为 `maker` 生成自包含 `out/*.html` 报告页（`layout="page"`）或 780px 零脚本长图卡（`layout="card"`），校验本地图片魔数并内联 base64 |
| [page_date.py](maisya/page_date.py) | HTML / JSON-LD / `<time>` / 元标签与相对时间的多层级文章发布时间抽取器（`published_from_html` / `published_from_text`） |
| [digest.py](maisya/digest.py) | `DigestWorkspace`、`STARTER_FEEDS`、`prefilter_search_item`、`collect`、`parse_feed_xml`、`run_digest_tool`：冷启动精选初订、RSS 发现/14 天试订/转正/退订、五维打分与内嵌 `viz_spec` 简报卡 |
| [sandbox.py](maisya/sandbox.py) | Docker + `runsc` (gVisor) 容器执行、`executions`/`execution_resources`、`secrets` 单次只读挂载，无容器自动降级 `light_workspace` |
| [services.py](maisya/services.py) | 联网源绑定校验（`validate_web_bindings`）、公网目标校验（`validate_endpoint_targets`、防 SSRF）、跨平台 MCP 绝对路径校验与动态模板调用 |
| [vault.py](maisya/vault.py) | 宿主加密保险箱，生成与管理不落明文的凭据句柄，全链路输出脱敏 `redact()` |
| [incoming.py](maisya/incoming.py) | `Incoming`：Webhook 验签、幂等去重（`incoming_events`）与 `<platform>:<group\|private>:<id>` 路由投递 |
| [bridge.py](maisya/bridge.py) | `Bridge`：`return_groups` / `return_members` / `return_intents` / `result_deliveries`；支持 `return_chat_id` 与同轮超时后自动转后台交付 |
| [share.py](maisya/share.py) | `Share`：使用宿主 `maisaka.reply.generate` 与 `send.hybrid` 精准发出链接/文件/图片/私聊预览，并在跨容器场景支持 `adapter.napcat.file.upload_group_file` `base64://` 兜底 |
| [intents.py](maisya/intents.py) | `Intent` 队列与 `social_state`：克制唤醒（睡眠免打扰、未回不连戳、熟人、每日额度）、`daily_at` 不漂移周期排班与 `scheduled_admin` 保护 |
| [pulse.py](maisya/pulse.py) | `Pulse`：后台到期扫描、`Bridge` 回接认领（含 `inspect.signature` 兼容与 `admin_direct`）、`<maisya_workflow>` 唤醒目标会话 `planner` |
| [learning.py](maisya/learning.py) | `Learning._learn()`：对复杂成功轨迹做异步经验提炼并回执，不阻断对话 |
| [shaping.py](maisya/shaping.py) | `execute_shape`、`handle_setup`、`build_overview`、`resolve_chat_target`、`handle_digest_sources`、`patch_settings`/`patch_profile`：管理员对话式全量配置、合规规范化与回滚引擎 |
| [recipes.py](maisya/recipes.py) | `1 + 2` 能力配方导入导出、隐私与凭据体检、公开 HTTPS 防 SSRF 拉取预览、缺件对齐、平滑升级与推荐例行任务一键挂载 |
| [management.py](maisya/management.py) | `Management`：五页控制台统一读写门面，驱动 `产物` 资料空间分类、角色名册模型与回退配置、手账读写、一般/高级设置、`ui_style` 切换、发现与健康检查 |
| [console.py](maisya/console.py) | `snapshot()` / `read_event()`：`现场` 单路游标快照、`01→04 FLOW` 拓扑、上下文分布、`TREND` 示波器读数及事件原文分页 |
| [web.py](maisya/web.py) | 多源联网搜索与抓取抽象（含 `WebRouter.search_broad` 多引擎并发检索、`published_at` 与 `<link rel="alternate">` 绝对 `feed_links` 抽取） |
| [mcp_worker.py](maisya/mcp_worker.py) | 独立子进程封装与超时受控调用高风险/外部 stdio MCP 工具（跨平台绝对路径校验、最小环境变量隔离、Linux 可选降权） |

---

## 4. `presets/`、`webui/` 与测试矩阵

- **预置资产（`presets/`）**：
  - 角色：`scout`（联网搜源与事实核对）、`maker`（创作、HTML 图卡、`render_report` 通用报告与隔离沙箱计算）、`digest`（资讯简报，默认停用，`max_steps = 20`）。
  - 技能：`source-checking`、`ongoing-workspace`、`news-digest`、`research-report`。
  - 心跳模板：`presets/memory/HEARTBEAT.md`（默认留空等待启用）。
- **WebUI 双主题模板体系（`webui/`）**：
  - 源码模板：`now.html.in`、`management.html.in`、`fragments/{outputs,abilities,memory,settings}.html`、`millennium.css`、`fish.js`。
  - 编译器：`python3 webui/build.py` 生成最终单页包 `webui/{now,outputs,abilities,memory,settings}.html`（零 `<img>`、零 `<iframe>`、零 `transition: all`、零 `localStorage`）。
  - 主题：`millennium`（默认，协调宿主千禧机械风）、`maiwork`（清朗手账风）与 `auto`（随 `ui_style` 持久化）。
- **测试目录**：
  - `tests/`：本地单元测试与 Chromium Playwright 端到端界面测试套件。
  - `tests_host/`：`run_contract.py`（隔离实例宿主 SDK 契约，打包同步包含 `logo.svg`）与 `run_live_smoke.py`（真模型+联网冒烟）。
  - `tests_sandbox/`：`check_runtime.py` 与 `run_isolated.py`（`runsc` 沙箱实测）。

---

## 5. SQLite 表结构汇总（均存 `data/maisya/maisya.db`）

- **对话与身份路线**：`trusted_sources`（`trust.py`）、`session_routes`（`trust.py` / `intents.py`）
- **线程与工作区**：`agent_threads`、`thread_events`、`agent_workspaces`、`thread_context`（`threads.py`）、`attachments`（`workspaces.py`）
- **手账与回执**：`memory_versions`、`learning_receipts`、`experience_receipts`（`memory.py`）
- **沙箱与资源**：`executions`、`execution_resources`（`sandbox.py`）
- **回接、交付与意图**：`return_groups`、`return_members`、`return_intents`、`result_deliveries`（`bridge.py`）、`intents`、`social_state`、`incoming_events`（`intents.py`）
