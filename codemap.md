# Maisya 代码地图（Codemap）

> 描述 `Maisya v0.3` 插件仓（`chartyr.maisya v0.3.0`）的真实文件布局、职责边界、Tool / API / Hook 入口、WebUI 架构与运行期 29 张 SQLite 表映射（均对照工作区源码核实，最后校验：`2026-11-18`）。

---

## 1. 仓库顶层结构

```text
Maisya/
├── .github/workflows/tests.yml   # 3-OS（Ubuntu / macOS / Windows）Python 3.13 CI，锁定 SDK <= 2.23.99
├── __init__.py                   # 插件包导出（export plugin_class）
├── _manifest.json                # 宿主 manifest v2：chartyr.maisya v0.3.0、宿主 1.0.0~1.4.99、SDK 2.8.1~2.23.99、26 项 capabilities
├── plugin.py                     # 宿主装配根（7,740 行）：生命周期、4 个 @Tool、5 个 Hook、1 个 @Command、6 个静态 @API
├── webui.json                    # 宿主 WebUI v2 单页声明：id="now" 指向 webui/now.html，3 组查询 + 3 组写入 RPC
├── config.example.toml           # 最简默认配置模板（admins=[]、permission_mode="ask_risky"、enabled=false）
├── LICENSE                       # GNU GPL v3 开源协议（GPL-3.0-or-later）
├── logo.svg                      # 插件矢量图标（千禧机械边框 + 小鱼剪影）
├── README.md                     # 面向用户与管理员的项目主页与极速上手
├── AGENTS.md                     # 工程红线、10 条架构不变式与发布边界
├── HANDOFF.md                    # 当前收敛交付快照、验收状态与接手核查项
├── codemap.md                    # 本代码地图
├── maisya/                       # 36 个核心 Python 运行时模块 + viz.css 样式资产
├── presets/                      # 默认内置 default / scout / digest 档案、4 个共享技能、心跳模板与调度手册模板
├── sandbox/                      # 可选 Docker 隔离镜像定义（Dockerfile、build_image.sh）
├── webui/                        # 五页双主题工作台源码模板与唯一编译产物 now.html
├── docs/                         # 工程文档（00/01/02/04/06）、用户指南（guide/）、截图（images/）与历史阶段归档（archive/）
├── tests/                        # 本地单元、v0.3 专项与 Playwright Chromium UI 测试（81 个 test_*.py 模块 + conftest.py）
├── tests_host/                   # 独立隔离目录宿主 SDK 契约与实模联网冒烟脚本（run_contract.py、run_live_smoke.py）
└── tests_sandbox/                # Docker / runsc 运行时体检与隔离验收脚本（check_runtime.py、run_isolated.py）
```

---

## 2. `plugin.py` 宿主挂载面（源码已核）

- **生命周期、模型脱敏与头像缓存**：
  - `MaisyaPlugin.on_load()`：初始化 `maisya.db`、工作区、`MemoryLayers`、`Vault`、`PermissionGrantStore`、`HostWorkspaceRunner`、`DockerRunner`、`OnboardingManager`、`Bridge` / `Share` / `Pulse` 与 `Scout` / `Maker` 引擎；执行 `recover_pending_transactions` 启动崩溃恢复；将宿主 `[[models]]` 脱敏清单同步至 `host_models.json`（只保留模型名称、标识与提供商，绝不落 `api_key` / `base_url`）；合并宿主 `plugin.permission` 与 `settings.admins` 计算有效管理员集合。
  - `MaisyaPlugin.on_config_update()`：响应 `("bot", "model", "self")` 宿主配置热更新，重算管理员与模型名录，同步线程、意图与 `Pulse` 参数并刷新缓存；若管理员被移除则调用 `pause_delegations_for_revoked_admins` 冻结其高权委托。
  - `MaisyaPlugin.on_unload()`：排空头像/流缓存，有序关闭 `Management`、`Share`、`Pulse`、`ThreadEngine`、`AgentRuntime` 并清空 300s 内存引导缓存。
  - `_resolve_chat_sources()`：查询群头像与会话元信息，兼容 MaiBot 1.4.6 `chat.get_avatar` 顶层返回 `status="available"|"unsupported"` 结构并按 TTL 缓存。
- **Planner 工具（仅 3 个可见 + 1 个延迟）**：
  - `maisya_agent`（`visibility="visible"`）：`start` / `send` / `stop` / `reopen` / `list` / `approve` / `share`。默认 `profile="default"`；定制持久档案支持 `fresh=true` 归档主线程重开；`approve` 支持 `grant_tier="once|session|always|deny"`；`target_chat` / `deliver_to` 支持管理员跨群调度与私聊预览；`from_threads` 遵守 `Private -> Group` 单向隔离墙。
  - `maisya_recall`（`visibility="visible"`）：基于 FTS5 检索过往线程、`JOURNAL.md` 与 `MEMORY.md`，管理员支持 `target_chat="all"` 跨全部会话，执行 `Private <-> Group` 单向隐私壁垒。
  - `maisya_later`（`visibility="visible"`）：`schedule` / `list` / `pause` / `resume` / `run_now` / `cancel`，支持 `mode="once|recurring|watch"`（`watch` 静默盯梢检查、命中条件才汇报）、`kind="later|proactive|silent"`、`every_seconds >= 300`、`daily_at`（HH:MM 不漂移）、`replace` 覆盖去重、`run_now` 先置 `claimed` 防重复排期并挂载 `run_now_notify` 异常直报发起管理员私聊、跨会话 `target_chat` 与 `scheduled_admin` 取消保护。
  - `maisya_shape`（**不设 `visibility` 标记，保持延迟工具**）：管理员引导经宿主 `tool_search` 展开，支持 **9 类配置与塑形操作**：`overview` / `setup`（支持一站配好群与 `run_now=True, deliver_to='current'` 私聊试跑预览）/ `settings` / `profile`（含 `export` / `import` `.maisya.zip`）/ `file`（含角色/技能附属文件与 `role_memory`）/ `planner`（读写回滚 `PLANNER.md`）/ `digest_sources` / `services` / `skill_proposal`（显式拒绝已永久移除的 `webhooks` / `recipe`），强制执行防自锁死校验与明文密钥拦截。
- **宿主 Hooks & Command**：
  - `chat.receive.after_process`（`maisya_receive`）：只读记录 `trusted_sources`（含 `sender_name`）与 `session_routes`、`note_received(engaged=not bool(group_id))`；未启用群聊普通消息零落盘与零注入；已核验管理员私聊在插件启用后直接全能力直通（无需进 `enabled_chats`），其余未启用群聊触发仅进 300s 有界内存缓存。
  - `maisaka.planner.before_request`（`maisya_planner`）：剔除宿主 `subagent*`、保留其余插件工具；按真实身份隐藏管理员工具与受限档案，保持字节稳定前置顺序组装统一工具契约（不点名档案）、`<maisya_planner_manual>` 与可用档案名册，后接动态线程索引（含 `state_note`）、回接结果、`MEMORY.md` 与 `JOURNAL.md`。
  - `maisaka.planner.after_response`（`maisya_planner_observe`）：关联同轮启动/补充调用到回接组（含管理员跨群 `issuer_chat_id`）；在 `not has_tool_call` 时触发 `end_turn` 收口，并按群参与解锁对应会话「未回不连戳」。
  - `maisaka.replyer.before_request`（`maisya_replyer`）：单次垫入已核实纯文字结果；若同轮已由 `share` 精确投递则调用 `action="abort"` 并填充 `custom_result={"reason": ...}` 拦截重复回复。
  - `send_service.before_send`（`maisya_redact`）：出站凭据与私密路径脱敏，非管理员消息剥离全体提及。
  - `@Command("vault")`（`^/vault(?:\s|$)`）：私聊录入或删除凭据句柄（仅限已核验管理员）。
- **管理台静态 `@API`（共 6 个，均 `public=False, version="1"`）**：
  - 实时现场：`maisya_now`、`maisya_event`、`maisya_note`、`maisya_stop`
  - 后台管理：`maisya_manage_read`、`maisya_manage_write`

---

## 3. `maisya/` 分层模块职责表（36 个 `.py` + `viz.css`）

> 已永久移除历史模块 `incoming.py` 与 `recipes.py`。按架构职责分为 5 层（核于 `wc -l maisya/*.py`，含 `shaping.py = 595 <= 600`）：

### 3.1 配置、身份权限与事务底座
| 模块 | 职责与核心类 / 函数 |
|---|---|
| [config.py](maisya/config.py) | `PluginSettings`、`Settings`（`extra="ignore"`、默认 `heartbeat_mode="off"`、`daily_proactive_limit=3`、`per_chat_daily_proactive_limit=1`、`daily_bg_runs_limit=48`、`daily_bg_tokens_limit=200000`、`bg_max_consecutive_failures=3`、`longing_quiet_hours=0` 跟随档位、`heartbeat_base_minutes()` / `longing_threshold_hours()`、`_migrate_legacy_heartbeat` / `apply_heartbeat_compat`，默认 `ui_style="millennium"`、`proactive_level="care"`）、三路合并与抗自锁死校验 |
| [trust.py](maisya/trust.py) | `MessageStore` / `trusted_sources`（含真实发言显示名 `sender_name`）、双文件 WAL 路由索引（`session_routes`）、`<platform>:<group\|private>:<id>` <-> MD5 `session_id` 解析、大小写敏感 OpenID 保留、`merge_admins` / `validate_admin_update` 防自锁死，与 `PermissionGrantStore`（`permission_grants`） |
| [authz.py](maisya/authz.py) | 统一委托授权生命周期：`delegations` 表、`approved_envelope` 权限包络、`check_hot_edit_expansion`（禁热改扩权逃逸）、`pause_delegations_for_revoked_admins` 与 `evaluate_activity_authority` |
| [data_scope.py](maisya/data_scope.py) | 数据归属与可见性原语：`initial_thread_scope` / `merge_scopes` 污染格交集、`is_tainted_scope` / `can_read_scope` 隔离裁决、`asset_scopes` 档案记忆隔离，以及按 `(thread_id, revision)` 绑定的 `publication_grants` 发布授权 |
| [tx_recovery.py](maisya/tx_recovery.py) | 进程内重入锁 `tx_lock`、无明文 `TxJournal`（`tx_journal/` 二阶段原子备份与回滚，失败显式抛错），与启动前置 `recover_pending_transactions` 崩溃恢复 |
| [vault.py](maisya/vault.py) | `Fernet` 加密保险箱（`0600`）与 `vault_meta.json` 非敏感目录；`mcp_inline` 凭据纳入完整服务名 SHA-256 哈希与精确 `purge_mcp_secrets` 清理（防名称碰撞），输出/上下文强制 `redact` |
| [pathsafe.py](maisya/pathsafe.py) | 跨平台文件系统安全原语：根逃逸 / UNC / 符号链接检查、无 `dir_fd` 回退、跨平台 `file_lock` 与 `minimal_subprocess_env` |
| [onboarding.py](maisya/onboarding.py) | `OnboardingManager`：5 步冷启动向导状态机（`hello -> admins -> chats -> runtime -> done`，`0600` 原子写）、存量静默识别、`(run_id, sequence)` 防竞态、`items` 7 项就绪清单与 8 秒限时无秘钥 `verify_model` |

### 3.2 能力档案、记忆层与能力包
| 模块 | 职责与核心类 / 函数 |
|---|---|
| [abilities.py](maisya/abilities.py) | `Profile` / `Profiles` / `Skills`：`BUILTIN_PROFILES={"default", "scout", "digest"}`、历史 `maker` 别名映射、5 大 `TOOLSETS`（`exec` 含 `calc`）、`coalesce_window_ms` 突发合并、`MAX_PROFILE_MD_LINES=150` 自动生成、`execution_mode` / `session_mode` 与导出脱敏 |
| [memory.py](maisya/memory.py) | `MemoryLayers`：角色/技能附属目录（`references/templates/scripts/assets`，`methods()` 自动声明保密边界）、`skill_proposals`、`MEMORY.md`（按最新别名优先）、角色 `MEMORY.md`、7 天 `JOURNAL.md`、`HEARTBEAT.md`、`PLANNER.md`、`memory_versions`、`memory_search`（FTS5）与复盘回执 |
| [packages.py](maisya/packages.py) | 三层 `.maisya.zip` 导入导出（`profile` / `capability_pack` / `template`、`preview_export_package` / `import_package_bundle`、公开 `https://` 直链 SSRF 校验）；支持 `profile_companions/`（非管理员导出自动剥离 `references/`）与定时任务按权过滤 |
| [learning.py](maisya/learning.py) | `Learning._learn()`（`max_tokens=500`）与 `schedule_planner_signal()`：对成功轨迹复盘并带 `thread:<id>@r<rev>` 溯源写入 `skill_proposals` 草稿或角色短笔记；对连续转向/停后换档/同请求串行补派生成不含原文的 `planner` 草稿 |
| [shaping.py](maisya/shaping.py) | 轻量管理员塑形路由（595 行，`<= 600` 行红线）：分发 `overview` / `setup` / `settings` / `profile` / `file` / `planner` / `digest_sources` / `services` / `skill_proposal` 读写、`patch`、回滚、防自锁死与密钥拦截 |
| [shaping_setup.py](maisya/shaping_setup.py) | `shaping.py` 扩展逻辑：`setup` 一站开通与私聊试跑预览、`export` / `import`、事务式 `handle_services`、`handle_digest_sources`、`handle_skill_proposal`、`PLANNER_CONTRACT_LINES`（含 `state_note`）与字节稳定 `build_planner_preview` |

### 3.3 线程引擎、上下文预算与双级执行
| 模块 | 职责与核心类 / 函数 |
|---|---|
| [threads.py](maisya/threads.py) | `ThreadStore` / `ThreadEngine`：`agent_threads`、`thread_events`、`thread_search`（FTS5）、`agent_workspaces`、`thread_context`、`primary_threads`；`<speaker>` 归属信封、`coalesce_window_ms` 同权批合并（跨权限拆批）、`reclaim_workspace_for_profile` 工作区回收与 `stop()` / `reopen()` |
| [context_budget.py](maisya/context_budget.py) | Token 估算、`MODEL_FAMILY_WINDOWS` 窗口封顶、默认 256K 自适应预算、`prune_tool_outputs` Tier-1 折叠、精确锚点提取、静态交班回退与厂商溢出错误识别 |
| [workspaces.py](maisya/workspaces.py) | `Workspaces` / `WorkingCopy`：临时拷贝校验后原子提交 `in/` / `project/` / `out/`（提取 `project/STATE.md` 至 `state_note`）、`in/` 只读锁定、`result_attach` 对 `references/` 整文与 `>=200` 字摘录硬阻断，及 `attachments` 表 |
| [scout.py](maisya/scout.py) | `Scout` 规划循环：多模型 `fallback_models` 顺序回退、实时北京时间提示、`chat_read` 跨天分桶均匀抽样、`TOOL_DEFINITIONS` 与只读工作区循环 |
| [maker.py](maisya/maker.py) | `Maker` 执行引擎：整合文件读写（无工作区工具但挂载附属文件时开放只读 `in/`）、`render_report`、`digest_feed`、`calc`、`package_export`、`HostWorkspaceRunner` / `DockerRunner` 路由、群友 `in/*/scripts/` 无网 `python -I` 固定脚本执行，及 `pending_approvals` |
| [runner.py](maisya/runner.py) | 通用执行协议 `WorkspaceRunner`、`ExecutionRequest` / `ExecutionResult` / `ApprovalRequired`、`assess_execution_risk` / `evaluate_permission_gate`（含 `host_exec:unisolated`）、`HostWorkspaceRunner`（进程组超时强杀、`0o400` `MAISYA_SECRETS_DIR` 退出即焚、输出脱敏） |
| [sandbox.py](maisya/sandbox.py) | `SandboxSettings`（默认 `enabled=False`）与降门槛 `DockerRunner`：自动探测 `runsc` 择优附加、`--cap-drop ALL --security-opt no-new-privileges --pids-limit 128`、工作区隔离与 `executions` / `execution_resources` 记录 |
| [calc_engine.py](maisya/calc_engine.py) | 无容器确定性计算与密码学真随机引擎：受限 AST `Decimal` 运算（`eval_expression`）、数组统计（`compute_stats`）与 `secrets.SystemRandom` 掷骰（`roll_dice_expr`）/ 抽签洗牌加权抽样（`draw_items`） |
| [services.py](maisya/services.py) | `Services`（`abilities/services.json`）：本地 `stdio` MCP、远程 HTTP MCP（公网与 DNS SSRF 校验）、工具清单探测、`apis`、`web`、`inputs`，与外部副作用分级（`classify_external_effect`、`op_id` / `idempotency_key`、`uncertain` 阻断） |
| [mcp_worker.py](maisya/mcp_worker.py) | 独立受控子进程执行 `stdio` 与 Streamable HTTP MCP 工具调用（绝对路径、公网/DNS SSRF 与最小环境变量隔离） |
| [web.py](maisya/web.py) | `WebRouter`：多源联网搜索与正文抓取抽象（优先宿主 `web.search` / `web.fetch`，自适应第三方搜索插件与 `services.json["web"]`） |

### 3.4 结果回接、节律唤醒与专题产物
| 模块 | 职责与核心类 / 函数 |
|---|---|
| [bridge.py](maisya/bridge.py) | `Bridge`：`return_groups` / `return_members` / `return_intents` / `result_deliveries`；同步垫入与异步汇合、跨群私聊回传 `issuer_chat_id` 路由、语音重发校验与数值/归属保真提示 |
| [share.py](maisya/share.py) | `Share`：调用 `maisaka.reply.generate` 与 `send.hybrid` 交付准确链接、媒体与文件，支持语音后状态核对与 Docker+NapCat `base64://` 群文件兜底 |
| [intents.py](maisya/intents.py) | `Intents` 队列与社交/预算状态：`intents`（`schedule`/`watch`）、`bg_budget_usage`、`social_state`、`heartbeat_ticks`（保留 50 行）；两层每日额度、免打扰、`daily_at` 不漂移、`scheduled_admin` 保护与连续失败 `record_bg_failure` 指数退避熔断 |
| [pulse.py](maisya/pulse.py) | `Pulse` 后台调度循环：驱动 `return`（预算与额度豁免）/ `proactive` / `silent` / `watch`（无变化 `[WATCH:NO_CHANGE]` 静默）、`run_now` 故障管理员私聊唤醒、三档抖动 + `utils` 三步心跳、`serendipity` 与零空闲 I/O |
| [digest.py](maisya/digest.py) | `DigestWorkspace`、`STARTER_FEEDS`、`collect`、`parse_feed_xml`、`run_digest_tool`：RSS 与搜索并发抓取（`Semaphore(8)` + `COLLECT_QUERY_TIMEOUT_SECS=18s` / `COLLECT_FETCH_TIMEOUT_SECS=6s` + `project/digest_pool.json` 缓存复用）、漏存 URL 自动回补与五维筛选 |
| [digest_card.py](maisya/digest_card.py) | `600px@2x=1200px` 资讯简报天空主题浅色图卡渲染、眉标重复日期剥离（`_dedupe_eyebrow_slot_label`）与封面筛选 |
| [report.py](maisya/report.py) | `render_report_artifact`：生成自包含 `out/*.html` 网页报告（内联 `viz.css`）与 780px 静态长图卡，校验本地图片魔数并内联 base64 |
| [viz_spec.py](maisya/viz_spec.py) | Intelligent UI 共享组件目录（16 类模块）、`news`/`report` 档校验（过滤单指标退化图表）、`numbers_in` / `check_spec_numbers` 防幻觉数字核验与静态/动态渲染 |
| [viz.css](maisya/viz.css) | Intelligent UI 共享样式表（`load_viz_css()` 内联进 HTML，零外链） |
| [page_date.py](maisya/page_date.py) | HTML / JSON-LD / `<time>` / 元标签与相对时间的多层级文章发布时间抽取器 |

### 3.5 玻璃盒控制台与管理门面
| 模块 | 职责与核心类 / 函数 |
|---|---|
| [console.py](maisya/console.py) | `snapshot()` / `read_event()`：提供 `现场` 单路游标快照、`state_note`、近期心跳 `heartbeat_ticks`、全局预定/盯梢、步骤事件、待审批载荷与上下文 Token 方块图数据 |
| [management.py](maisya/management.py) | `Management`（6,000 行）：五页工作台与 5 步引导后端统一读写门面，覆盖档案/附属文件/`PLANNER.md`/技能/草稿/连接器/产物/手账/意图/授权/保险箱/`.maisya.zip` 导入导出与无秘钥模型探针 |

---

## 4. `presets/`、`webui/` 与测试矩阵

- **预置资产（`presets/`）**：
  - 档案：[`default`](presets/profiles/default/profile.toml)（全能默认助手，含 `render_report` 与 5 大工具集）、[`scout`](presets/profiles/scout/profile.toml)（轻量查证助手）与 [`digest`](presets/profiles/digest/profile.toml)（资讯简报）。
  - 技能：[`source-checking`](presets/skills/source-checking/SKILL.md)、[`ongoing-workspace`](presets/skills/ongoing-workspace/SKILL.md)、[`news-digest`](presets/skills/news-digest/SKILL.md)、[`research-report`](presets/skills/research-report/SKILL.md)。
  - 心跳与调度模板：[`presets/memory/HEARTBEAT.md`](presets/memory/HEARTBEAT.md)、[`presets/planner/PLANNER.md`](presets/planner/PLANNER.md)。
- **WebUI 五页双主题工作台（`webui/`）**：
  - 源码模板：`now.html.in`、`management.html.in`、`fragments/{abilities,library,memory,settings}.html`、`millennium.css`、`icons.js`、`fish.js`（已内联 5 步冷启动引导浮层）。
  - 编译产物：`python3 webui/build.py` 编译生成唯一宿主单入口 [`webui/now.html`](webui/now.html)（含 `现场 | 产物 | 能力 | 手账 | 设置` 五页、移动端五栏底栏 `.ms-mob-dock`、横向像素鱼轮播、请求序号与脏标记防覆盖，以及 `#ms-confirm-dialog` 原生 `<dialog>` Top-Layer 二次确认弹窗，全面守护 12 处删除/重置/撤销操作并兼容沙盒 `form-action 'none'` CSP 与 `Escape` 防穿透）；`python3 webui/build.py --check` 校验编译一致性与 `< 2 MiB` 体积上限。
  - 外观主题：`millennium`（默认，千禧机械风）、`modern`（现代极简风，向后兼容旧值 `"maiwork"`）与 `auto`（跟随宿主风格）。
- **测试矩阵（全量实测：`640 passed` + `23 tests OK`）**：
  - `tests/`（81 个 `test_*.py` 模块）：涵盖内核与权限隔离（`test_foundation.py`、`test_trust.py`、`test_vault.py`、`test_arch_consolidation_p0.py`、`test_arch_consolidation_p1_p2.py`、`test_multi_party_foundation.py`）、线程/预算/执行器/服务（`test_threads.py`、`test_context_budget_and_compaction.py`、`test_workspaces.py`、`test_scout.py`、`test_maker.py`、`test_sandbox.py`、`test_services.py`、`test_cross_platform.py`）、回接/节律/专题/实测回归（`test_bridge.py`、`test_share.py`、`test_intents.py`、`test_heartbeat_restore.py`、`test_planner_manual.py`、`test_digest*.py`、`test_live_task_bottlenecks.py`）及 Chromium CSP Playwright 端到端测试（`test_management_ui.py`、`test_now_ui.py`、`test_v03_webui_smoke.py`、`test_webui_v26_redesign.py`、`test_audit_b{1,2,3}_webui.py`、`test_webui_perf.py` 等）。
  - `tests_host/`：[`run_contract.py`](tests_host/run_contract.py)（真实宿主 SDK 23 项契约验证，校验 `host_unchanged: true`）与 [`run_live_smoke.py`](tests_host/run_live_smoke.py)（独立隔离实模联网冒烟）。
  - `tests_sandbox/`：[`check_runtime.py`](tests_sandbox/check_runtime.py) 与 [`run_isolated.py`](tests_sandbox/run_isolated.py)（Docker / `runsc` 运行时验收）。

---

## 5. 运行期存储映射（均存 `<宿主根>/data/maisya/`）

- **SQLite 数据库（`data/maisya/maisya.db`，共 29 张表：27 张普通表 + 2 张 FTS5 虚表）**：
  - **对话、身份与授权**：`trusted_sources`、`session_routes`（`trust.py` / `intents.py`）、`permission_grants`（`trust.py`）、`delegations`（`authz.py`）
  - **作用域与发布裁决**：`asset_scopes`、`publication_grants`（`data_scope.py`）
  - **线程、上下文、审批与工作区**：`agent_threads`、`thread_events`、`thread_search`（FTS5）、`agent_workspaces`、`thread_context`、`primary_threads`（`threads.py`）、`attachments`（`workspaces.py`）、`pending_approvals`（`maker.py`）
  - **记忆、版本与学习回执**：`memory_versions`、`memory_search`（FTS5）、`learning_receipts`、`experience_receipts`、`skill_proposals`（`memory.py`）
  - **执行与容器资源**：`executions`、`execution_resources`（`sandbox.py`）
  - **回接、交付、预算与意图**：`return_groups`、`return_members`、`return_intents`、`result_deliveries`（`bridge.py`）、`intents`、`bg_budget_usage`、`social_state`、`heartbeat_ticks`（`intents.py`）
- **私密配置、凭据与资产目录（`0600` / `0700` 权限管理）**：
  - `settings.json` / `settings.base.json` / `settings_history.json`（三路合并生效配置与回滚历史）
  - `onboarding.json`（5 步冷启动状态与脱敏模型探测结果，严禁存宿主密钥或 URL）
  - `host_models.json`（脱敏后的宿主模型名称、标识与提供商目录）
  - `tx_journal/`（无明文二阶段崩溃恢复事务日志）
  - `vault/` + `vault/vault_meta.json`（`Fernet` 加密凭据与脱敏索引，含 `mcp_inline` 句柄）
  - `abilities/`：`planner/PLANNER.md`、`profiles/<id>/`（含 `PROFILE.md`、`profile.toml`、`MEMORY.md`、`NOTES.md`、`references/templates/scripts/assets/`）、`skills/<id>/SKILL.md` 与 `services.json`（`servers` / `apis` / `web` / `inputs`）
  - `memory/`：`self/{HEARTBEAT.md, JOURNAL.md}` 与 `chats/<chat_hash>/MEMORY.md`
  - `workspaces/`：`ws_<编号>/`（含 `project/STATE.md`、`project/rss_sources.json`、`project/digest_history.json`、`project/digest_pool.json`、`in/`、`out/`）与 `attachments/`
