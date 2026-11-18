# Maisya Handoff

写给下一位接手者（人或 agent）。只记录需要上手才知道的状态、事实、红线、验证命令与上线前核查项；设计与规则的权威来源是根目录 [AGENTS.md](AGENTS.md)、[codemap.md](codemap.md)、[架构蓝图](docs/01-架构蓝图.md) 与 [管理台与接入](docs/04-管理台与接入.md)。

最后更新：2026-11-19（v0.3.1 沙箱老配置兼容，见第 7 节）。**架构收口方案（`B0`～`B7`）、GPT 4 项代码审查修复、`docs/archive/08` 心跳恢复、`docs/archive/09` 调度手册、`docs/archive/10` 多人持续活动底座、线上简报并行采编与定时私聊故障直报、跨会话私聊回传路由，以及 WebUI 12 处破坏性操作原生 `<dialog>` 统一二次确认（`2112bc6`）已全部落地、通过全量 + Playwright + 真实宿主隔离契约验收并上线运行。** 当前分支 `main`。

全量验收实测结果：`tests/` 共 **640 passed**（0 failed / 0 skipped）；`tests_host/run_contract.py` 共 **23 tests OK**（`host_unchanged: true`，`isolated_directory_cleaned: true`）；`python3 webui/build.py --check` 返回 `0`；`wc -l maisya/shaping.py` = **595**（`<= 600`）。

---

## 1. 当前核心产品形态与架构不变式

- **唯一主体**：MaiBot 是唯一对话主体，保留原有人设、日常闲聊节奏（原生 `reply`）与宿主 `A_Memorix` 记忆边界；Maisya 扩展行动空间，不抢话筒。
- **三档内置能力档案 + 渐进定制 + 三层真实包分享**：
  1. **能力档案（`Profile`）**：内置 [`presets/profiles/default/`](presets/profiles/default/profile.toml)（全能默认助手，开启全部 5 大 `TOOLSETS` 与 `render_report` 图文报告等）、[`scout/`](presets/profiles/scout/profile.toml)（轻量查证助手）与 [`digest/`](presets/profiles/digest/profile.toml)（资讯简报），支持渐进定制（`PROFILE.md <= 150` 行）与历史 `maker` 兼容别名。
  2. **真实包导出与导入（[`maisya/packages.py`](maisya/packages.py)）**：支持 `profile` / `capability_pack` / `template` 三层脱敏 `.maisya.zip` 本地导出、聊天工作区工具 `package_export` 打包后由 `share` 直接发回文件、公开 `https://` 直链导入及重名确认/另存副本（**已彻底移除虚假 `maisya://` 分享链接**）。非管理员导出公共档案严格隔离为「仅当前会话且非管理员创建的常规任务」；导入执行前置全量校验，并在任何阶段失败时事务回滚已安装技能与目标目录。
- **双轨线程与现场控制**：
  - `default` / `scout` 默认轻量按事建线程（`ephemeral`）；持久档案按 `(owner, chat_id, profile)` 自动续接同会话常驻主线（`primary_threads`，传 `fresh=true` 归档重开）。
  - `现场` 页区分「排入跟进」（`steer=false`）与运行中「立即转向」（`steer=true`）；从 `产物` 点击「查看生成过程」跳转回 `现场` 时自动重置筛选避免空白。
- **全局预定与条件盯梢（`Intent` / `Watch`）**：
  - `现场 -> 预定与盯梢` 展示**全局计划**（不随选中线程筛选消失）；`maisya_later` 支持 `schedule | list | pause | resume | run_now | cancel`（含 `mode="watch"` 静默盯梢），`run_now` / `intent_run_now` 在派发前先置 `'claimed'`（异常兜底转 `'deferred'`），防循环任务重复排期。
  - **外部签名推送接口（原 `incoming.py` / `maisya_incoming_event` / `webhooks.json`）确定永久移除，不再恢复**。
- **四本手账与双轨技能沉淀**：
  - `手账` 页集成 4 本手账：`会话备忘`（`MEMORY.md` ≤16,000 字）| `角色记忆`（`profiles/<id>/MEMORY.md` ≤8,000 字，与 `能力 -> 角色` 实时同步）| `近期经历`（`JOURNAL.md` ≤8,000 字）| `闲暇心跳`（`HEARTBEAT.md` ≤16,000 字）。
  - 自动学习（`Learning`，`max_tokens=500`，[`maisya/learning.py`](maisya/learning.py)）已移除无实证裸多步工具串合成；不论新增技能（`create`）或修改已有技能（`patch`），均先进入 `skill_proposals` 待确认队列并附带 `thread:<id>@r<rev>` 溯源引用（管理员可审核保存或忽略）；日常经验仅追加低风险短笔记。
- **MCP 连接中心、外部副作用保护与凭据边界（`services.py` / `vault.py` / `management.py` / `tx_recovery.py` / `shaping_setup.py`）**：
  - 支持直接粘贴标准 `mcpServers` JSON、表单/对话框直接填写 `env`/`headers` 密码（加密存入 `kind="mcp_inline"` 内部句柄，不污染用户可见密码保险箱）、一键试连发现工具、工具白名单通配符 `*`，以及远程 Streamable HTTP MCP（含公网与 DNS 重绑 SSRF 校验）。
  - `vault._mcp_secret_handle` 与 `purge_mcp_secrets` 使用**完整服务名 SHA-256 哈希与精确匹配**，消除长前缀与包含名称碰撞；配置保存与包导入统一纳入 `tx_lock` 与启动崩溃恢复日志 [`maisya/tx_recovery.py`](maisya/tx_recovery.py)（`<data_root>/tx_journal/` 不记录明文密钥，`on_load()` 自动回滚残留孤立状态；先校验后提交，失败自动回滚 `.enc`、技能与目标目录）。
  - 外部服务调用（`Services.call` / `call_api`）支持 `classify_external_effect`（`read_only` / `idempotent_write` / `non_idempotent_write`）与独立于 `call_id` 的 `(thread_id, revision)` 级 `op_id`；支持时自动注入确定性 `idempotency_key`，对 `non_idempotent_write` 出现未知状态返回 `uncertain` / `reconcile_required` 阻断盲目重试。
  - 用户在聊天中直接发密码/Token 让 Bot 办事时正常执行，依赖出站与上下文脱敏兜底，**绝不因未提前录入保险箱而拒单**；密码保险箱仅作长期复用凭据时使用。
- **Planner 工具、权限数据域与执行边界**：
  - 可见工具：`maisya_agent`（`start | send | stop | reopen | list | approve | share`）、`maisya_recall`、`maisya_later`；
  - 延迟工具：`maisya_shape`（**严禁设 `visibility` 标记**，`maisya/shaping.py` 严格 ≤ 600 行，现为 592 行），支持 `overview | setup | settings | profile | file | planner | digest_sources | services | skill_proposal` 与 `setup(..., run_now=True, deliver_to='current')` 私聊试跑预览。
  - 委托授权生命周期（[`maisya/authz.py`](maisya/authz.py)）按真实 `source_msg_id` 反查生成权限快照，跨线程绑定 `thread_rev` 与 `scheduled_admin` 边界；数据读写与跨会话发布收口至统一 [`maisya/data_scope.py`](maisya/data_scope.py)（含 `publication_grants` 与管理员跨会话回传授权）。
  - 执行环境统一为 `execution_mode = auto | host | sandbox`（前端 `#pr-exec-mode` 同步更新）：`auto` 就绪优先隔离、`host` 直通工作区、`sandbox` 强制隔离；在 `host` 模式触及高危动作时引入 `host_exec:unisolated` 显式二次确认（区分于一般高风险动作审批）。
- **后台预算与 `return` 豁免（`config.py` / `intents.py` / `pulse.py`）**：
  - 新增 `daily_bg_runs_limit = 48`、`daily_bg_tokens_limit = 200000`、`bg_max_consecutive_failures = 3` 及 `bg_budget_usage` / `intents` 失败追踪字段；超限或连续失败分别切入 `budget_paused`（次日自动解冻或人工 `resume()` 立即恢复）与 `error_paused`，并在管理台概览/现场/状态接口完整可观测。
  - `Pulse._returns()` 与 `kind == "return"` 结果回接完全豁免后台预算及主动搭话额度限制，确保正常交代不中断。
- **五页双主题玻璃盒 WebUI（[`webui/now.html`](webui/now.html)）**：
  - [`webui.json`](webui.json) v2 暴露唯一入口 `webui/now.html`，内嵌 **`现场 | 产物 | 能力 | 手账 | 设置`** 五页与 5 步冷启动引导（引导逻辑保持不变），提供千禧机械（`millennium`，默认）、清朗手账（`modern`，兼容旧值 `"maiwork"`）与 `auto` 三档外观。
  - 界面层具备请求序号与交互脏标记保护（`bookLoadSeq`、`detailSeq`、`fishTouched`/`modelTouched`/`mdTouched`），避免后台异步加载覆盖用户刚打开的模型选择器、像素鱼定制抽屉或手账输入内容。

---

## 2. 最新全量验收状态（v0.3 + 08/09 + 架构收口 B0～B7）

- **全量本地与 Playwright Chromium UI 单测**：
  - `PYTHONPATH=.:tests:.local-checks/deps ~/.venvs/maiwork/bin/python -m pytest tests -q -rs -p no:cacheprovider`：**`640 passed`**（`0 failed / 0 skipped`）。
- **独立宿主隔离契约（真实 MaiBot SDK）**：
  - `python3 tests_host/run_contract.py`：**`Ran 23 tests` `OK`**（`host_unchanged: true`，`isolated_directory_cleaned: true`）。
- **前端构建与语法校验**：
  - `python3 webui/build.py --check` 返回退出码 `0`。
- **代码边界、格式与隐私扫描**：
  - `wc -l maisya/shaping.py` = **`595`**（`<= 600`）；
  - `git diff --check` 返回 `0`；
  - 受跟踪文件秘密/地址/群号关键词扫描：零泄漏。

---

## 3. 本地与隔离验证命令

```bash
# 1. WebUI 构建一致性检查
python3 webui/build.py --check

# 2. 全量本地与 Playwright Chromium UI 测试
PYTHONPATH=.:tests:.local-checks/deps ~/.venvs/maiwork/bin/python -m pytest tests -q -rs -p no:cacheprovider

# 3. 真实宿主 venv 隔离契约（自动校验 host_unchanged）
python3 tests_host/run_contract.py

# 4. 隔离实模联网冒烟（加 --execute 才真实调用）
python3 tests_host/run_live_smoke.py --execute

# 5. 独立 Docker / runsc 运行时验收（--execute 会建临时规则并在完成后自动清理）
python3 tests_sandbox/run_isolated.py --execute

# 6. 构建可选沙箱镜像（默认 dry-run；--execute 才执行构建）
./sandbox/build_image.sh
```

若 `.local-checks/deps` 未就绪：
`python -m pip install --target .local-checks/deps 'cryptography>=46,<51' 'jsonschema>=4.23,<5'`

---

## 4. 下一位接手待办与线上部署红线

1. **当前仓库与线上部署状态**：
   - 本地 `main` 分支已完成 `P0`（`13d0324`）、`P1/P2 + 文档`（`553d097`）、`4 项线上简报/跨会话引导修复`（`d85b5cd`）、`图卡质检与画像采编 3 项修复`（`f448526`）、`通用上下文抽样与领域中立预设`（`fe34d52`）、`眉标去重、漏存自动回补与高活跃群跨天补采`（`93a9119`、`2851113`）及 `多人持续活动 P1 通用底座`（`f6dc7a3`）与本交接提交，已收敛发布为 `v0.3.0` 并同步推送至 `origin/main`；
   - 线上已完成最新一次先备份后原子部署（`f6dc7a3`，61 个运行文件 + 保留线上 `config.toml` 与 38 个 `.pyc`；旧插件、运行期数据与 SQLite `.backup()` 只读 `quick_check: ok` 备份位于 `/root/backups/maisya-deploy-20261117-171120/`；前序备份保留在 `/root/backups/maisya-deploy-20261117-152729/`、`/root/backups/maisya-deploy-20261117-144307/` 等目录），线上 `config.toml`（`mode 600`，`[plugin] enabled = true`）逐字节原样保留，宿主核心代码零改动且未重启 MaiBot；
   - 轻触 `_manifest.json` 触发热重载后，日志确认 `chartyr.maisya v0.3.0` 加载、注入与注册全部成功且 `0` 报错；只读查库确认 `trusted_sources.sender_name`、`agent_threads.state_note`、`agent_workspaces.state_note` 已就绪且 `abilities/skills/ongoing-workspace/SKILL.md` 已同步。
2. **已收口文档基准**：
   - [`docs/01-架构蓝图.md`](docs/01-架构蓝图.md)、[`docs/guide/configuration.md`](docs/guide/configuration.md) 与 [`codemap.md`](codemap.md)（36 个 Python 模块 + 29 张 SQLite 表）已同步更新；[`docs/archive/03-M3执行方案.md`](docs/archive/03-M3执行方案.md)、[`docs/archive/07-极简高权收敛计划.md`](docs/archive/07-极简高权收敛计划.md)、[`docs/archive/08-心跳恢复方案.md`](docs/archive/08-心跳恢复方案.md)、[`docs/archive/09-调度手册方案.md`](docs/archive/09-调度手册方案.md) 与 [`docs/archive/10-多人持续活动底座方案.md`](docs/archive/10-多人持续活动底座方案.md) 已移入 `docs/archive/` 归档。
3. **部署红线（来自 [AGENTS.md](AGENTS.md)）**：
   - `AGENTS.local.md`、`config.toml`、`.local-checks/` 与 `.trellis/` 只留本地，严禁写入或提交进仓库；测试夹具统一使用 `qq:group:100246809`、`qq:private:10001`。
   - 任何涉及线上 `plugins/` 的放入、覆盖或删除（会触发插件热重载）前，**必须取得用户当次明确同意**；
   - 部署前必须先将既有线上插件文件与运行期数据（SQLite 必须用只读 `.backup()` 复制）备份至 `/root/backups/`，并保留线上 `config.toml`；
   - 严禁修改 MaiBot 宿主源码、严禁擅自更改 `config/bot_config.toml`、严禁重启 MaiBot 或适配器服务（重启由用户操作）。

---

## 5. 已完成：心跳恢复（docs/08）与调度手册（docs/09）落地（2026-11-17）

方案原文与已定决策见 [08 心跳恢复方案](docs/archive/08-心跳恢复方案.md)、[09 调度手册方案](docs/archive/09-调度手册方案.md)。两份方案全部步骤（含 08 第 1～7 步与 09 第 1～7 步）已全部完成、接入验证并准备提交上线。

### 5.1 已完成清单

| 方案步骤 | 状态 | 主要改动 |
|---|---|---|
| 09 第 1～3 步 | ✅ | `memory.py` 新增 `kind="planner"`（`abilities/planner/PLANNER.md`、初始化写入 `v0 preset` 版本记录、未改过的预设随版本升级）；`presets/planner/PLANNER.md`；`plugin.py` 字节稳定 `<maisya_planner_manual>` + 名册注入；`shaping.py` / `shaping_setup.py` 支持 `planner` 读写回滚与名册检查 |
| 09 第 4～5 步 UI 与接口 | ✅ | `management.py` `planner` 读写、回滚与稳定注入预览；`webui/fragments/abilities.html` 与 `webui/management.html.in`（含移动端轮播）固定首位「MaiBot（调度）」卡片、`PLANNER.md` 编辑回滚、Token 估算预览与调度草稿采纳/忽略 |
| 09 第 6～7 步 第二阶段与文档 | ✅ | `learning.py` + `plugin.py` 三类抽象调度信号（连续 steer ≥ 2 次、stop 后 10 分钟换档、同请求串行补派且不误报并发）提炼不含聊天原文的 `profile_id="planner"` 待确认草稿；`tests_host/test_contract.py` 实测宿主原样透传 `function.description` 及 KV 前缀缓存评估；文档全部同步 |
| 08 第 1～5 步 核心重构 | ✅ | `config.py` 三档 `heartbeat_mode`（默认 `off`）、`v0.3.0` 遗留全量配置（含无 `heartbeat_enabled` 落盘）兼容迁移、`intents.py` 全局+单会话两层额度与 `heartbeat_ticks` 表、`pulse.py` 三步心跳与抖动、对话调整与概览心跳块 |
| 08 第 6～7 步 WebUI 与文档 | ✅ | `management.py` / `console.py` / `webui/fragments/settings.html` / `webui/now.html.in` / `webui/management.html.in`（三段档位、两层每日上限、折叠 `0 = 跟随档位`、手账主动状态条、现场近期心跳记录）及全部文档与回归测试对齐 |

### 5.2 阶段验收结果（`08/09` 基准；最新全量见第 2 节）

- 新增测试：[`tests/test_planner_manual.py`](tests/test_planner_manual.py)（21 项）、[`tests/test_heartbeat_restore.py`](tests/test_heartbeat_restore.py)（49 项）；
- 当时阶段全量本地与 Playwright：`PYTHONPATH=.:tests:.local-checks/deps ~/.venvs/maiwork/bin/python -m pytest tests -q -rs -p no:cacheprovider` -> **`593 passed`**（0 failed / 0 skipped；叠加第 6 节收口与线上实测修复回归后现为 **`616 passed`**）；
- 宿主 SDK 隔离契约：`python3 tests_host/run_contract.py` -> **`Ran 23 tests` `OK`**（`host_unchanged: true`，`isolated_directory_cleaned: true`）；
- 构建与边界检查：`python3 webui/build.py --check` 返回 `0`，`wc -l maisya/shaping.py` 当时为 `588`（现为 `592`）。

新增入库文件清单（现已归档至 `docs/archive/`）：[`docs/archive/08-心跳恢复方案.md`](docs/archive/08-心跳恢复方案.md)、[`docs/archive/09-调度手册方案.md`](docs/archive/09-调度手册方案.md)、[`presets/planner/PLANNER.md`](presets/planner/PLANNER.md)、[`tests/test_heartbeat_restore.py`](tests/test_heartbeat_restore.py)、[`tests/test_planner_manual.py`](tests/test_planner_manual.py)。

### 5.3 已知坑

- `tests/test_takeover_hardening.py::test_later_proactive_daily_quota_and_non_admin_mention_all_strip` 用真实时钟排 60 秒 / 90 秒后的惦记，**在 23:58 前后跑会跨日失败**，与本次改动无关。
- `shaping.py` 有 `<= 600` 行的测试红线（当前 592 行），新增逻辑放 `shaping_setup.py`。
- 提示词里不要直接写 `<maisya_profiles>` 这类标签名：测试和模型都会按标签切块。

---

## 6. 已完成：架构收口 B0～B7 与 GPT 四项修复落地（2026-11-17）

| 批次 | 状态 | 主要改动与文件 |
|---|---|---|
| `B1` 委托授权生命周期（`13d0324`） | ✅ | 新增 [`maisya/authz.py`](maisya/authz.py)，按 `source_msg_id` 反查生成请求权限快照并绑定 `thread_rev` / `scheduled_admin` 边界（`[plugin].admins=[]` 时控制台线程保留豁免） |
| `B2` 统一数据域与发布授权（`13d0324`） | ✅ | 新增 [`maisya/data_scope.py`](maisya/data_scope.py)，收口角色/工作区/共享读取范围校验，并落实 `publication_grants` 跨会话授权与管理员直达豁免 |
| `B3` 执行模式与高危直通确认（`13d0324`） | ✅ | 统一 `execution_mode = auto \| host \| sandbox` 并在 [`webui/fragments/abilities.html`](webui/fragments/abilities.html) / [`webui/management.html.in`](webui/management.html.in) 更新三档选项；引入 `host_exec:unisolated` 高危直通确认 |
| `B4` 技能提案审慎门槛（`553d097`） | ✅ | [`maisya/learning.py`](maisya/learning.py) 移除无据工具串合成，现有技能 `patch` 进入 `skill_proposals` 待确认队列并附 `thread:<id>@r<rev>` 溯源 |
| `B5` 崩溃恢复事务日志（`553d097`） | ✅ | 新增 [`maisya/tx_recovery.py`](maisya/tx_recovery.py)，以 `tx_lock` + 无明文 `TxJournal` 包裹 MCP 凭据提交与包/模板导入，`plugin.py` `on_load()` 自动恢复 |
| `B6` 外部副作用幂等与对账（`553d097`） | ✅ | [`maisya/services.py`](maisya/services.py) 引入 `classify_external_effect`、`(thread_id, revision)` 级 `op_id`、`idempotency_key` 与非幂等 `uncertain`/`reconcile_required` 阻断；保留 `read_only` 单步回执去重 |
| `B7` 后台预算与回归稳态（`553d097`） | ✅ | [`maisya/config.py`](maisya/config.py)、[`maisya/intents.py`](maisya/intents.py)、[`maisya/pulse.py`](maisya/pulse.py) 增加日常运行/Token 预算、`bg_max_consecutive_failures` 退避暂停与控制台可观测状态，`return` 完全豁免 |
| 回归测试与文档同步 | ✅ | 新增 [`tests/test_arch_consolidation_p0.py`](tests/test_arch_consolidation_p0.py)、[`tests/test_arch_consolidation_p1_p2.py`](tests/test_arch_consolidation_p1_p2.py)，同步 `docs/01`、`docs/guide/configuration.md` 与 `codemap.md` |
| 简报与跨会话调度实测 4 项修复 | ✅（`d85b5cd`，已部署） | [`maisya/digest.py`](maisya/digest.py) 合并 6 小时内多次 `collect` 池与未成卡 `score_select` 草稿（防自锁重与空增量清空）；[`maisya/threads.py`](maisya/threads.py) 屏蔽运行中持久线程上一轮旧 `result`（保留 `_resolve` 对刚完成轮次的精确交付）；[`plugin.py`](plugin.py) 强化未启用私聊管理员 `source_msg_id` 引导与报错提示；新增 [`tests/test_digest_live_fixes.py`](tests/test_digest_live_fixes.py) 并固定 [`tests/test_audit_b2_entry.py`](tests/test_audit_b2_entry.py) 晨间时钟（已备份并原子热重载上线） |
| 简报图卡质检与画像采编 3 项修复 | ✅（`f448526`，已部署） | [`maisya/viz_spec.py`](maisya/viz_spec.py) 补齐 `stat`/`stats` 块级与单项 `unit`/`display` 渲染、防重复后缀及核数覆盖；[`maisya/digest.py`](maisya/digest.py) 过滤单指标 `stats`/`bars` 与无 `delta` `stat`（保留对比与 `report` 档案）；[`presets/skills/news-digest/SKILL.md`](presets/skills/news-digest/SKILL.md) 与 [`presets/profiles/digest/PROFILE.md`](presets/profiles/digest/PROFILE.md) 明确 `chat_read(limit=80, hours=168)` 群画像抽样、仅无封面配图且 `>= 2` 项可比数据时加 `viz` 与纯正中文标题规范；[`tests/test_digest_live_fixes.py`](tests/test_digest_live_fixes.py) 新增 2 项回归测试（已备份并原子热重载上线） |
| 通用上下文抽样、会话元数据与记忆层防偏收口 | ✅（`fe34d52`，已部署并清旧残留） | [`maisya/scout.py`](maisya/scout.py) 透传 `chat_read` 的 `hours`/全域搜索与跨天抗刷屏抽样（`mode` `auto`/`recent`/`sample`），并在线程系统提示写明 `<memory_context>`/`<methods>` 非工作区文件与实时查证优先；[`plugin.py`](plugin.py) 与 [`maisya/maker.py`](maisya/maker.py) 被动缓存群名称并仅向 `owner="chat"` 注入 `chat_type`/`chat_title`；[`maisya/learning.py`](maisya/learning.py)、[`presets/planner/PLANNER.md`](presets/planner/PLANNER.md)、[`presets/profiles/digest/PROFILE.md`](presets/profiles/digest/PROFILE.md)、[`presets/skills/news-digest/SKILL.md`](presets/skills/news-digest/SKILL.md)、[`maisya/shaping.py`](maisya/shaping.py) 与 [`maisya/digest.py`](maisya/digest.py) 强化记忆层隔离、领域中立多兴趣拼盘及跨领域精选源；[`docs/06-宿主接口事实.md`](docs/06-宿主接口事实.md) 补实 `message.get_recent` 参数事实，新增 [`tests/test_general_context_and_sampling.py`](tests/test_general_context_and_sampling.py)（已备份热重载并清理线上旧独立游戏测试残留） |
| 眉标去重、漏存自动回补与高活跃群跨天补采 | ✅（`93a9119`，已部署） | [`maisya/digest_card.py`](maisya/digest_card.py) 新增 `_dedupe_eyebrow_slot_label` 自动剥离 `slot_label` 中重复的当天日期/星期与时段词；[`maisya/digest.py`](maisya/digest.py) 为 `score_select` 增设 `backfill_uncached_candidates` 自动抓存未入池公开链接原文并给出更清晰的缺正文报错，同步更新 [`presets/skills/news-digest/SKILL.md`](presets/skills/news-digest/SKILL.md) 与 [`presets/profiles/digest/PROFILE.md`](presets/profiles/digest/PROFILE.md)；[`_manifest.json`](_manifest.json)、[`maisya/scout.py`](maisya/scout.py) 与 [`docs/06-宿主接口事实.md`](docs/06-宿主接口事实.md) 声明并使用 `message.get_by_time_in_chat` 为高活跃大群分桶补采跨天样本，新增 3 项回归用例并补充 [`tests_host/test_contract.py`](tests_host/test_contract.py) 契约断言 |
| 多人持续活动 P1 通用底座（4 模块，`docs/archive/10`） | ✅（`f6dc7a3`，已部署） | 设计文档 [`docs/archive/10-多人持续活动底座方案.md`](docs/archive/10-多人持续活动底座方案.md) + 7 项端到端测试 [`tests/test_multi_party_foundation.py`](tests/test_multi_party_foundation.py)：① [`maisya/trust.py`](maisya/trust.py)、[`plugin.py`](plugin.py)、[`maisya/abilities.py`](maisya/abilities.py) 与 [`maisya/threads.py`](maisya/threads.py) 增加群 `<speaker>` 发言归属信封、`coalesce_window_ms` 与同权并发队列合并批处理及跨权限边界拆批；② [`maisya/workspaces.py`](maisya/workspaces.py)、[`maisya/threads.py`](maisya/threads.py)、[`maisya/bridge.py`](maisya/bridge.py)、[`maisya/console.py`](maisya/console.py)、[`webui/now.html.in`](webui/now.html.in) 与 [`maisya/scout.py`](maisya/scout.py) 落地 `project/STATE.md` 脱敏 `state_note`、`<=600` 字动态 `<maisya_threads>` 注入（保持 KV 稳定前缀不变）与结果数字归属保真提示；③ [`maisya/memory.py`](maisya/memory.py)、[`maisya/maker.py`](maisya/maker.py)、[`maisya/packages.py`](maisya/packages.py)、[`maisya/shaping.py`](maisya/shaping.py)、[`maisya/management.py`](maisya/management.py) 与 [`webui/management.html.in`](webui/management.html.in) 落地角色附属目录（`references/templates/scripts/assets`）、只读 `in/` 开放、`references/` 外发硬锁与非管理员打包剥离；④ 新增 [`maisya/calc_engine.py`](maisya/calc_engine.py) 注册 `calc`（`TOOLSETS["exec"]`）提供无容器 `Decimal` 运算、数组统计与 `secrets.SystemRandom` 掷骰/抽签，并将无网只读 `python -I` 脚本扩展至角色与已绑定技能 `scripts/` |
| 实机瓶颈、私聊回传与退避熔断（`85775b1` / `98f4a99` / `f65178a` / `f8760cc`） | ✅（已部署） | 修复跨群私聊回传路由（`issuer_chat_id`）与陈旧投递 `share` 语音重发校验；新增 `reclaim_workspace_for_profile` 回收被异常占用的工作区、`Pulse` `record_bg_failure` 指数退避、`run_now` 超时/失败直报管理员私聊（`run_now_notify`）、`digest.collect` 并发检索抓取（`Semaphore(8)` + 池缓存复用）、`maisaka.replyer.before_request` 中止填充 `custom_result.reason` 及宿主 1.4.6 `chat.get_avatar` 顶层 `status` 兼容 |
| WebUI 破坏性操作统一二次确认（`2112bc6`） | ✅（已同步线上 `webui/`） | 在 [`webui/now.html.in`](webui/now.html.in) 与 [`webui/millennium.css`](webui/millennium.css) 实现原生 `<dialog id="ms-confirm-dialog">` Top-Layer 共享确认组件（默认聚焦安全「取消」键、兼容 `form-action 'none'` CSP 与 `Escape` 防穿透），覆盖现场/能力/外部连接/保险箱/授权撤销全部 12 处删除与重置入口；顺手修复 `#now-prune-btn` 清理后若当前线程已被批删的选中态残留 |

## 7. 已完成：v0.3.1 沙箱老配置兼容与开箱可诊断（2026-11-19）

线上反馈「开轻量 Docker 沙箱报 `1 validation error for Settings sandbox ... 启用容器执行需要指定有效镜像`」，根因是线上 `config.toml` 保留了早期版本的 `image = ""`（以及 `runtime = "runsc"`），而 v0.3 把默认镜像改成 `maisya-sandbox:latest` 却没兼容空串；管理台与首次引导只提交 `{sandbox: {enabled}}` 局部补丁，于是整份配置校验失败。

| 改动 | 文件 | 说明 |
|---|---|---|
| 空镜像回落 | [`maisya/sandbox.py`](maisya/sandbox.py) | `DEFAULT_SANDBOX_IMAGE` 常量 + `image` 字段 `mode="before"` 校验，空串 / 纯空白 / `None` 视同未配置 |
| 沙箱嵌套补齐 | [`maisya/config.py`](maisya/config.py) | `Settings._migrate_legacy` 对 `sandbox` 用默认值打底，局部补丁不再挤掉 `socket` / `max_concurrent` / `subnet` 等 |
| 探测提示可操作 | [`maisya/sandbox.py`](maisya/sandbox.py) | 运行时缺失时列出 Docker 实际可用运行时；镜像缺失时改用 `checked=False` 自查并给出 `./sandbox/build_image.sh --execute` 与改写 `image` 两条修法 |
| 设置页状态行 | [`webui/fragments/settings.html`](webui/fragments/settings.html)、[`webui/management.html.in`](webui/management.html.in)、[`webui/millennium.css`](webui/millennium.css) | `#set-sandbox-hint` 显示已就绪 / 未探测到 Docker / 具体未就绪原因；开关保存成功后重新拉取状态 |
| 回归测试 | [`tests/test_sandbox.py`](tests/test_sandbox.py)、[`tests/test_management.py`](tests/test_management.py)、[`tests/test_v03_runner_and_permissions.py`](tests/test_v03_runner_and_permissions.py) | 空镜像回落、局部补丁保字段、运行时/镜像缺失提示、探测就绪共 5 项 |

**发布与上线（2026-11-19）**：

- 版本 `0.3.0 -> 0.3.1`（[`_manifest.json`](_manifest.json)），新增 [`CHANGELOG.md`](CHANGELOG.md) 与升级须知；本地 `main` 已推送 `origin/main`（`a239f61`）。
- 备份：`/root/backups/maisya-0.3.0-20261119-011432/`（整个插件目录 + `settings.json` + `settings.base.json` + SQLite 只读 `.backup()`，共 16M；另有 `config.toml.before-runtime-fix` / `.before-image-fix` / `.before-enable` 三份快照）。
- 部署：61 个运行文件原子覆盖，线上 `config.toml` 内容调整仅限 `[sandbox]` 段（`image` 由 `""` 显式写成 `maisya-sandbox:latest`、`runtime` 由 `runsc` 改为 `""`、`enabled` 改为 `true`，其余字节不动）；文件属主统一 `root:root`，`__pycache__` 已清理。
- 热重载：日志确认 `chartyr.maisya v0.3.1 加载成功` → `注入 PluginContext` → `注册完成`，`plugin_on_load` `outcome=completed`（1.909s），0 报错；`settings.base.json` / `settings.json` 已回写规范化后的沙箱配置。
- 镜像：`maisya-sandbox:local` 与 `maisya-sandbox:latest` 均构建完成（377MB，`python 3.13.18` + `node v24.22.3`，非 root `10000:10000`），`--network none --read-only --cap-drop ALL` 下实测可运行。
- 真实探测：在插件 venv 中对线上 `Docker` 跑 `Sandbox.probe()`，`probe_ready = True`、`probe_error = ''`。

**上线后补记（同一晚修复）**：真机冒烟发现 `DockerRunner`（`exec_command`/`sandbox_exec` 的容器路径）没有把宿主工作区交给容器执行用户，工作区是 `700 root:root` 而容器以 `UID 10000` 运行，容器内读工作区会 `Permission denied`。现已把 `Sandbox._own` 的逻辑提取为模块级 `own_container_tree(root, *, strict=True)`，`DockerRunner.execute` 建容器前以 `strict=False` 调用（Docker Desktop 这类宿主/容器 uid 不共享映射的环境里 chown 失败不阻断），并改用真实容器实测 `cat a.txt` / `touch b.txt` / `python3 --version` 全部 `ok`、宿主侧可见新文件、工作区属主变为 `10000`。

**本机新发现（待后续处理）**：该宿主 `docker0` 网桥不存在（`ip -br addr show docker0` 报 `Device "docker0" does not exist`），默认 `bridge` 网络 `docker run` 会报 `adding interface veth... to bridge docker0 failed: Device does not exist`，因此镜像构建改用 `docker build --network host` 才通过。沙箱默认 `network_mode = "none"` 不受影响；若要把 `network_mode` 调成 `bridge`，需先修复/重建 `docker0`（属宿主 Docker 服务层面，需用户另行授权）。
