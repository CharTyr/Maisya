# Maisya 代码地图（Codemap）

> 描述 `Maisya v0.3` 插件仓的真实文件布局、职责边界、Tool / API / Hook 入口与数据存储映射（均对照工作区当前源码核实）。

---

## 1. 仓库顶层结构

```text
Maisya/
├── .github/workflows/tests.yml   # 3-OS（Ubuntu / macOS / Windows）Python 3.13 CI 工作流
├── __init__.py                   # 插件包导出
├── _manifest.json                # 宿主 manifest v2：chartyr.maisya v0.3.0、display.icon、25 项 capabilities
├── plugin.py                     # 宿主装配根：生命周期、4 个 Tool、5 个 Hook、1 个 Command、6 个 @API
├── webui.json                    # 宿主 WebUI v2 单页声明：id="now" 指向 webui/now.html，3 组查询/写操作 RPC
├── config.example.toml           # 最简默认配置模板（admins=[]、permission_mode="ask_risky"、enabled=false）
├── LICENSE                       # GNU GPL v3 开源协议
├── logo.svg                      # 插件矢量图标（千禧机械边框 + 小鱼剪影）
├── README.md                     # 面向用户的项目主页与极速上手
├── AGENTS.md                     # 工程红线与 10 条架构不变式
├── HANDOFF.md                    # 当前 v0.3 收敛交付快照、验证状态与接手核查项
├── codemap.md                    # 本代码地图
├── maisya/                       # 32 个核心 Python 运行时模块 + viz.css 样式资产
├── presets/                      # 默认内置 default / scout / digest 档案、4 个共享技能与心跳模板
├── sandbox/                      # 可选 Docker 镜像定义（Dockerfile、build_image.sh）
├── webui/                        # 五页双主题（现场 | 产物 | 能力 | 手账 | 设置）模板、编译脚本与产物
├── docs/                         # 工程文档（00~04、06~07）与用户手册（guide/）
├── tests/                        # 本地单元、v0.3 专项与 Playwright Chromium UI 测试套件
├── tests_host/                   # 独立隔离目录宿主 SDK 契约与真实工具冒烟脚本
└── tests_sandbox/                # Docker / runsc 运行时体检与隔离验收脚本
```

---

## 2. `plugin.py` 宿主挂载面（源码已核）

- **生命周期与配置联动**：
  - `MaisyaPlugin.on_load()`：初始化 SQLite 存储、工作区、`MemoryLayers`、`Vault`、`PermissionGrantStore`、`HostWorkspaceRunner`、`DockerRunner`、`OnboardingManager`、`Bridge` / `Share` / `Pulse` 与 `Scout` / `Maker` 引擎；将宿主 `[[models]]` 脱敏清单同步至 `host_models.json`（只保留模型名称、标识与提供商，不落 `api_key`/`base_url`）；合并宿主 `plugin.permission` 与插件 `settings.admins` 计算有效管理员集合。
  - `MaisyaPlugin.on_config_update()`：响应 `("bot", "model", "self")` 宿主配置热更新，重新解析模型与管理员，同步引擎/意图/Pulse 参数并刷新缓存。
  - `MaisyaPlugin.on_unload()`：排空异步头像/流缓存，有序关闭 `Management`、`Share`、`Pulse`、`ThreadEngine`、`AgentRuntime` 并清空内存引导状态。
- **Planner 工具（仅 3 个可见 + 1 个延迟）**：
  - `maisya_agent`（`visibility="visible"`）：`start` / `send` / `stop` / `reopen` / `list` / `approve` / `share`。默认 `profile="default"`；定制持久档案支持 `fresh=true` 归档主线程重开；`approve` 支持 `grant_tier="once|session|always|deny"` 对 `waiting` 审批续跑或拒绝；`target_chat` / `deliver_to` 支持管理员跨群调度与私聊预览；`from_threads` 遵守 `Private -> Group` 单向隔离墙。
  - `maisya_recall`（`visibility="visible"`）：按 `scope` 与 `kind` 检索过往线程、自身后台经历、`MEMORY.md` 与 `JOURNAL.md`，管理员支持 `target_chat="all"` 跨全部会话，执行 `Private <-> Group` 单向隐私壁垒。
  - `maisya_later`（`visibility="visible"`）：`schedule` / `list` / `pause` / `resume` / `run_now` / `cancel`，支持 `mode="once|recurring|watch"`（`watch` 配合 `condition` 静默盯梢检查、满足条件才汇报）、`kind="later|proactive|silent"`、`every_seconds >= 300`、`daily_at`（HH:MM 不漂移）、`replace` 覆盖去重、`run_now` 先置 `claimed` 防重复排期、管理员跨会话 `target_chat` 与 `scheduled_admin` 取消保护。
  - `maisya_shape`（**无 `visibility` 标记，保持延迟工具**）：管理员引导经宿主 `tool_search` 展开，支持 **8 类配置与塑形操作**：`overview` / `setup`（支持一站配好群与 `run_now=True, deliver_to='current'` 私聊试跑预览）/ `settings` / `profile`（含 `export` / `import` 真实 `.maisya.zip`）/ `file`（含 `role_memory`）/ `digest_sources` / `services` / `skill_proposal`（显式拒绝已永久移除的 `webhooks` / `recipe` 入口），强制执行防自锁死校验与明文密钥拦截。
- **宿主 Hooks & Command**：
  - `chat.receive.after_process`（`maisya_receive`）：只读记录可信消息与 `session_routes`、`note_received(engaged=not bool(group_id))`；未启用会话的普通消息保持零落盘与零注入；已核验管理员私聊在插件启用后直接全能力直通（即使未加入 `enabled_chats`），其余未启用群聊触发仅进 300s 有界内存管理员引导缓存。
  - `maisaka.planner.before_request`（`maisya_planner`）：剔除宿主内置 `subagent*`，按真实身份隐藏管理员工具与受限档案，在尾部注入可用档案（含定制档案已绑定的 `active_thread_id`）、最近线程索引、会话 `MEMORY.md` 与 `JOURNAL.md`。
  - `maisaka.planner.after_response`（`maisya_planner_observe`）：关联本轮启动/补充调用到回接组，并在真实人类群轮次促成有效发言/调度时解锁群「未回不连戳」。
  - `maisaka.replyer.before_request`（`maisya_replyer`）：单次垫入已核实纯文字结果，或因同轮已由 `share` 精确投递而抑制重复回复。
  - `send_service.before_send`（`maisya_redact`）：出站凭据与私密路径脱敏，非管理员消息剥离全体提及。
  - `@Command("vault")`（`^/vault(?:\s|$)`）：私聊录入或删除凭据句柄。
- **管理台静态 `@API`（共 6 个，均 `public=False, version="1"`）**：
  - 实时现场：`maisya_now`、`maisya_event`、`maisya_note`、`maisya_stop`
  - 后台管理：`maisya_manage_read`、`maisya_manage_write`（已移除 `maisya_incoming_event`）

---

## 3. `maisya/` 模块职责表（32 个 Python 模块 + `viz.css`）

> 注：已永久移除历史模块 `incoming.py` 与 `recipes.py`，新增 `runner.py`、`onboarding.py`、`packages.py` 与 `shaping_setup.py`。

| 模块 | 职责与核心类/函数 |
|---|---|
| [config.py](maisya/config.py) | `PluginSettings`、`Settings`（`extra="ignore"`、新增 `admins` / `permission_mode` / `default_model`，`canonical_ui_style` 将历史 `"maiwork"` 归一为 `"modern"`，默认 `ui_style="millennium"`、`proactive_level="care"`、`longing_quiet_hours=0`）、三路合并与抗自锁死校验 |
| [trust.py](maisya/trust.py) | 可信消息映射（`trusted_sources`）、双文件 WAL 路由索引（`session_routes`）、`<platform>:<group\|private>:<id>` <-> MD5 `session_id` 解析、大小写敏感 OpenID 保留、`merge_admins` 双源管理员合并、`validate_admin_update` 防删空自锁死，以及 `PermissionGrantStore`（`permission_grants` 表） |
| [runner.py](maisya/runner.py) | 通用执行协议与宿主工作区执行器：`ExecutionRequest` / `ExecutionResult` / `ApprovalRequired`、`assess_execution_risk` / `evaluate_permission_gate`、`HostWorkspaceRunner`（`minimal_subprocess_env`、`PYTHONNOUSERSITE=1`、进程组超时强杀、`0o400` 临时 `MAISYA_SECRETS_DIR` 挂载与退出即焚、输出强制 `redact`） |
| [sandbox.py](maisya/sandbox.py) | `SandboxSettings`（默认 `enabled=False, image="maisya-sandbox:latest"`）与降门槛 `DockerRunner`：自动探测 `runsc` 择优附加（`privileged_sandbox_mode=False` 时兼容常规镜像标签与默认无特权 `runc`）、`--cap-drop ALL --security-opt no-new-privileges --pids-limit 128`、工作区隔离副本及单次只读 `/run/secrets/<handle>` 挂载、`executions`/`execution_resources` 记录 |
| [abilities.py](maisya/abilities.py) | `Profile` / `Profiles` / `Skills`：`BUILTIN_PROFILES={"default", "scout", "digest"}`、历史 `maker` 别名映射、5 大 `TOOLSETS`（`context` / `web` / `workspace` / `exec` / `schedule`）、`MAX_PROFILE_MD_LINES=150` 自动生成、`export_profile_zip` / `import_profile_zip`（含秘密扫描阻断）、`execution_mode`（`host\|sandbox`）与 `session_mode`（`persistent\|ephemeral`，兼容 `"task"`） |
| [packages.py](maisya/packages.py) | 三层能力包导出与导入（`profile` / `capability_pack` / `template` `.maisya.zip`、`preview_export_package` / `import_package_bundle`、公开 `https://` 直链拉取与 SSRF 校验）；按调用者身份与会话过滤导出定时任务（非管理员屏蔽管理员与外群任务）；导入执行前置校验与失败事务回滚，已移除虚假分享链接 |
| [onboarding.py](maisya/onboarding.py) | `OnboardingManager`：5 步首次引导状态机（`hello -> admins -> chats -> runtime -> done`，`STATUSES=("not_started","in_progress","completed","skipped")`）、`onboarding.json` `0600` 原子写、存量静默升级识别、单调递增 `(run_id, sequence)` 防竞态、`items` 7 项只读就绪清单与 `usable` 门禁，以及限时 `verify_model`（绝不泄露宿主 `api_key`/`base_url`） |
| [memory.py](maisya/memory.py) | `MemoryLayers`：全局技能与 `skill_proposals` 草稿队列、最新别名优先会话 `MEMORY.md`、角色 `MEMORY.md`、7 天自视角 `JOURNAL.md`（同日合并）、`HEARTBEAT.md`、`memory_versions` 版本历史与回滚、`learning_receipts` / `experience_receipts` |
| [context_budget.py](maisya/context_budget.py) | Token 估算、`MODEL_FAMILY_WINDOWS` 家族窗口封顶、默认 256K 自适应上下文预算、`prune_tool_outputs` Tier-1 折叠、精确锚点提取、静态交班回退与厂商溢出错误识别 |
| [threads.py](maisya/threads.py) | `ThreadStore` / `ThreadEngine`：`agent_threads` / `thread_events` / `agent_workspaces` / `thread_context` / `primary_threads`；定制档案同会话常驻主线程绑定与 `fresh` 归档重开、热改档案动态加载不锁死、`stop()` 回归 `idle`、`reopen()` 修复悬空调用并唤醒历史/归档/已停线程，以及双车道并发调度与两级上下文压缩 |
| [workspaces.py](maisya/workspaces.py) | `Workspaces` / `WorkingCopy`：临时拷贝校验后原子提交 `in/` / `project/` / `out/`、附件登记（`attachments`）与防穿越文件打开 |
| [pathsafe.py](maisya/pathsafe.py) | 跨平台文件系统安全原语：根逃逸/UNC/符号链接检查、跨平台文件锁与最小子进程环境变量构造（`minimal_subprocess_env`） |
| [scout.py](maisya/scout.py) | `Scout` 侦察规划循环：多模型顺序 `fallback`、实时北京时间提示、宿主/第三方 `web.search` 自适应与只读受控工作区循环 |
| [maker.py](maisya/maker.py) | `Maker` 工具执行引擎：整合文件编辑、HTML 渲染、`render_report`、`digest_feed`、`package_export`（传递调用者管理员状态与会话作用域）、连接器（MCP/`plugin_api`）、`HostWorkspaceRunner` 与 `DockerRunner` 路由、群友非容器只允许预置只读脚本校验，以及 `pending_approvals` 风险挂起与 `approve` 授权记录 |
| [viz_spec.py](maisya/viz_spec.py) | Intelligent UI 共享组件目录（16 类模块）、`news`/`report` 档校验、`numbers_in` / `check_spec_numbers` 防幻觉数字核验与静态/动态渲染 |
| [viz.css](maisya/viz.css) | Intelligent UI 共享样式表（`load_viz_css()` 内联进 HTML，零外链） |
| [report.py](maisya/report.py) | `render_report_artifact`：自包含 `out/*.html` 报告页与 780px 静态图卡生成，校验本地图片魔数并内联 base64 |
| [page_date.py](maisya/page_date.py) | HTML / JSON-LD / `<time>` / 元标签与相对时间的多层级文章发布时间抽取器 |
| [digest.py](maisya/digest.py) | `DigestWorkspace`、`STARTER_FEEDS`、`collect`、`parse_feed_xml`、`run_digest_tool`：RSS 订阅/试订/淘汰、五维筛选与图解简报卡辅助能力 |
| [digest_card.py](maisya/digest_card.py) | `600px@2x=1200px` 资讯简报天空主题浅色图卡渲染与封面筛选 |
| [services.py](maisya/services.py) | `Services`（`abilities/services.json`）：支持本地 `stdio` MCP、远程 Streamable HTTP MCP（公网与 DNS 重绑 SSRF 校验）、工具清单探测、`apis`（跨插件 `@API`）、`web`（自定义联网映射）与 `inputs`（平台文件读取器），已移除旧版静态执行 `templates` |
| [vault.py](maisya/vault.py) | 宿主 `Fernet` 加密保险箱（`0600`）与 `vault_meta.json` 脱敏元数据索引（`handle` / `kind` / `domain` / `fields` / `note`）；内联 `mcp_inline` 凭据句柄纳入完整服务名 SHA-256 哈希与精确 `purge_mcp_secrets` 清理，防长前缀与包含名称碰撞；明文不落日志、上下文与提示词 |
| [bridge.py](maisya/bridge.py) | `Bridge`：`return_groups` / `return_members` / `return_intents` / `result_deliveries`；同步单次垫入与异步回接组汇合，附带 `chat_type` 与群滚动锚点提示 |
| [share.py](maisya/share.py) | `Share`：调用 `maisaka.reply.generate` 与 `send.hybrid` 交付准确链接、媒体与文件，支持 Docker+NapCat `base64://` 群文件兜底 |
| [intents.py](maisya/intents.py) | `Intent` 队列与 `social_state`：含 `mode`（`schedule` / `watch`）、`condition`、`last_checked_at` / `last_triggered_at`、睡眠免打扰、按会话参与式解锁、分舞台每日额度、`daily_at` 不漂移周期与 `scheduled_admin` 保护 |
| [pulse.py](maisya/pulse.py) | `Pulse` 后台调度循环：统一驱动 `return` / `proactive` / `silent` / `watch`（静默盯梢巡检，仅触发条件才生成回接与报告；无变化输出 `[WATCH:NO_CHANGE]` 且不扣每日主动额度）、零空闲 I/O 缓存与 `serendipity` 提议 |
| [learning.py](maisya/learning.py) | `Learning._learn()`（`max_tokens=500`）：对复杂成功轨迹做复盘——首次新技能写进 `skill_proposals` 草稿、已有技能微调直接写入并在 `手账` 留回执支持按版本撤回（受隐私与密钥检查保护） |
| [shaping.py](maisya/shaping.py) | 轻量管理员塑形路由（`596` 行，红线 `<= 600` 行）：分发 `overview` / `setup` / `settings` / `profile` / `file` / `digest_sources` / `services` / `skill_proposal` 读写与局部 `patch`、版本历史回滚、抗自锁死与明文拦截 |
| [shaping_setup.py](maisya/shaping_setup.py) | 管理员对话塑形扩展：一站式 `setup`（配好即通过 `run_now=True, deliver_to='current'` 私聊试跑预览）、`profile(op='export'|'import')`、定时源 `digest_sources`、事务式 `handle_services`（先校验后写入）与 `handle_skill_proposal` |
| [management.py](maisya/management.py) | `Management`：五页工作台与 5 步引导后端统一读写门面（`now_detail` / `profile_detail` / `skills` / `connectors` / `library` / `memory` / `intents` / `settings` / `onboarding` / `models` / `grants` / `vault_list`），含标准 `mcpServers` JSON 解析、`upsert_service_item` 先校验后事务写保险箱（失败自动回滚 `.enc` 与元数据）、`.maisya.zip` 导入导出、`undo_learning_note`、`watch`/定时暂停恢复 `intent_run_now` 与模型连通探针 |
| [console.py](maisya/console.py) | `snapshot()` / `read_event()`：提供 `现场` 单路游标快照、全局预定与盯梢列表（`scheduled_intents` / `watches`）、步骤详情（`steps`）、待审批载荷（`pending_approval`）与上下文 Token 方块图数据 |
| [web.py](maisya/web.py) | `WebRouter`：多源联网搜索与抓取抽象（优先宿主 `web.search` / `web.fetch`，自适应第三方搜索插件与 `services.json["web"]`） |
| [mcp_worker.py](maisya/mcp_worker.py) | 独立受控子进程调用 `stdio` 与 Streamable HTTP MCP 工具（跨平台绝对路径校验、公网/DNS SSRF 校验、最小环境变量隔离） |

---

## 4. `presets/`、`webui/` 与测试矩阵

- **预置资产（`presets/`）**：
  - 档案：内置 [`presets/profiles/default/`](presets/profiles/default/profile.toml)（全能默认助手，开启 `render_report` 图文报告与全部 5 大工具集）、[`presets/profiles/scout/`](presets/profiles/scout/profile.toml)（轻量查证助手）与 [`presets/profiles/digest/`](presets/profiles/digest/profile.toml)（资讯简报）。
  - 技能：[`presets/skills/`](presets/skills/) 保留 `source-checking`、`ongoing-workspace`、`news-digest`、`research-report`。
  - 心跳模板：[`presets/memory/HEARTBEAT.md`](presets/memory/HEARTBEAT.md)（默认留空等待启用）。
- **WebUI 五页双主题模板体系（`webui/`）**：
  - 源码模板：`now.html.in`、`management.html.in`、`fragments/{abilities,library,memory,settings}.html`、`millennium.css`、`icons.js`、`fish.js`（5 步引导浮层已内联于入口模板中）。
  - 编译器：`python3 webui/build.py` 编译生成唯一宿主入口单文件 [`webui/now.html`](webui/now.html)（含五页视图 `现场 | 产物 | 能力 | 手账 | 设置`、移动端四栏底栏与横向像素鱼轮播；内置 `bookLoadSeq` / `detailSeq` 与交互脏标记保护；严格保持零外链、零 `<img>`、零 `<iframe>`、零 `transition: all`、零 `localStorage`）。`python3 webui/build.py --check` 校验构建一致性。
  - 主题：`millennium`（默认，千禧机械风）、`modern`（清朗手账风，向后兼容旧值 `"maiwork"`）与 `auto`（跟随宿主风格）。
- **测试目录**：
  - `tests/`：本地单元测试、`v0.3` 专项与对齐测试（含 `test_v03_core_wiring.py`、`test_ux_restoration_v031.py`、`test_product_blueprint_v032.py`、`test_design_alignment_v033.py` 等）与 Playwright Chromium 端到端测试（共 523 项）。
  - `tests_host/`：`run_contract.py`（独立隔离目录宿主 SDK 契约验证，共 22 项含 `host_unchanged` 校验）与 `run_live_smoke.py`（实模联网冒烟）。
  - `tests_sandbox/`：`check_runtime.py` 与 `run_isolated.py`（Docker / `runsc` 运行时验收）。

---

## 5. 运行期存储映射（均存 `<宿主根>/data/maisya/`）

- **SQLite 数据库（`data/maisya/maisya.db`）**：
  - **对话与身份授权**：`trusted_sources`、`session_routes`（`trust.py` / `intents.py`）、`permission_grants`（`trust.py`）
  - **线程、审批与工作区**：`agent_threads`、`thread_events`、`agent_workspaces`、`thread_context`、`primary_threads`（`threads.py`）、`attachments`（`workspaces.py`）、`pending_approvals`（`maker.py`）
  - **记忆、版本与回执**：`memory_versions`、`memory_search`、`skill_proposals`、`learning_receipts`、`experience_receipts`（`memory.py`）
  - **执行与容器资源**：`executions`、`execution_resources`（`sandbox.py`）
  - **回接、交付与意图**：`return_groups`、`return_members`、`return_intents`、`result_deliveries`（`bridge.py`）、`intents`、`social_state`（`intents.py`；`incoming_events` 已移除）
- **私密配置与索引文件（`0600` 权限管理）**：
  - `settings.json` / `settings.base.json` / `settings_history.json`（三路合并生效配置与回滚历史）
  - `onboarding.json`（5 步引导状态与模型探测摘要，严禁存宿主密钥或 URL）
  - `host_models.json`（脱敏后的宿主模型名称与提供商目录）
  - `vault/` + `vault/vault_meta.json`（`Fernet` 加密凭据及非敏感元数据目录，含 `mcp_inline` 内部句柄）
  - `abilities/services.json`（管理员登记的 `servers` / `apis` / `web` / `inputs` 连接器清单）
