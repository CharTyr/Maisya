# Maisya Handoff

写给下一位接手者（人或 agent）。只记录需要上手才知道的状态、事实、红线、验证命令与上线前核查项；设计与规则的权威来源是根目录 [AGENTS.md](AGENTS.md)、[codemap.md](codemap.md)、[架构蓝图](docs/01-架构蓝图.md) 与 [管理台与接入](docs/04-管理台与接入.md)。

最后更新：2026-11-16（**`v0.3.3` 产品蓝图对齐与安全加固基线**，对应本地 `main` 已完成 4 个提交并通过全量验证的状态；未推送远端，未部署上线）。

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
  - 自动学习（`Learning`，`max_tokens=500`）首次抽象的新技能落入 `skill_proposals` 草稿队列（`能力 -> 技能` 与 `手账` 入口预览保存）；对已有技能的微调自动写入并在 `手账 -> 自动复盘与经验回执` 展示，支持按历史版本一键撤回（`undo_learning_note`）。
- **MCP 连接中心与凭据边界（`services.py` / `vault.py` / `management.py` / `shaping_setup.py`）**：
  - 支持直接粘贴标准 `mcpServers` JSON、表单/对话框直接填写 `env`/`headers` 密码（加密存入 `kind="mcp_inline"` 内部句柄，不污染用户可见密码保险箱）、一键试连发现工具、工具白名单通配符 `*`，以及远程 Streamable HTTP MCP（含公网与 DNS 重绑 SSRF 校验）。
  - `vault._mcp_secret_handle` 与 `purge_mcp_secrets` 使用**完整服务名 SHA-256 哈希与精确匹配**，消除长前缀与包含名称碰撞；配置保存先全量校验 `_validate_services` 再事务提交凭据（写盘失败自动恢复 `.enc` 与元数据）。
  - 用户在聊天中直接发密码/Token 让 Bot 办事时正常执行，依赖出站与上下文脱敏兜底，**绝不因未提前录入保险箱而拒单**；密码保险箱仅作长期复用凭据时使用。
- **Planner 工具、对话塑形与执行边界**：
  - 可见工具：`maisya_agent`（`start | send | stop | reopen | list | approve | share`）、`maisya_recall`、`maisya_later`；
  - 延迟工具：`maisya_shape`（**严禁设 `visibility` 标记**，`maisya/shaping.py` 严格 ≤ 600 行，现为 596 行），支持 `overview | setup | settings | profile | file | digest_sources | services | skill_proposal` 与 `setup(..., run_now=True, deliver_to='current')` 私聊试跑预览。
  - 双源管理员（`宿主 plugin.permission ∪ Settings.admins`）私聊直接核验直通；`HostWorkspaceRunner`（默认 `ask_risky` 审批挂起）与可选 `DockerRunner` 隔离协作。
- **五页双主题玻璃盒 WebUI（[`webui/now.html`](webui/now.html)）**：
  - [`webui.json`](webui.json) v2 暴露唯一入口 `webui/now.html`，内嵌 **`现场 | 产物 | 能力 | 手账 | 设置`** 五页与 5 步冷启动引导（引导逻辑保持不变），提供千禧机械（`millennium`，默认）、清朗手账（`modern`，兼容旧值 `"maiwork"`）与 `auto` 三档外观。
  - 界面层具备请求序号与交互脏标记保护（`bookLoadSeq`、`detailSeq`、`fishTouched`/`modelTouched`/`mdTouched`），避免后台异步加载覆盖用户刚打开的模型选择器、像素鱼定制抽屉或手账输入内容。

---

## 2. 最新验收实测结果（本地 `main` 最新提交后实测）

- **全量本地与 Playwright Chromium UI 单测**：
  - `PYTHONPATH=.:tests:.local-checks/deps ~/.venvs/maiwork/bin/python -m pytest tests -q -rs -p no:cacheprovider`：**`523 passed`**（`0 failed / 0 skipped`，耗时 `103.37s`）。
- **独立宿主隔离契约（真实 MaiBot SDK）**：
  - `python3 tests_host/run_contract.py`：**`Ran 22 tests` `OK`**（`host_unchanged: true`，`isolated_directory_cleaned: true`）。
- **前端构建与语法校验**：
  - `python3 webui/build.py --check` 返回退出码 `0`；[`webui/now.html`](webui/now.html) 两段内嵌脚本通过 `node --check`。
- **代码边界、格式与隐私扫描**：
  - `wc -l maisya/shaping.py` = **`596`**（`<= 600`）；
  - `git diff --check` 返回 `0`；
  - 全部 67 个变更文件的本地地址与凭据关键词扫描：`findings=0`。

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

1. **当前仓库与预览状态**：
   - 本轮所有设计对齐、GPT 审查修复与回归测试已按模块拆分提交在本地 `main` 分支（**尚未 `git push` 至 `origin/main`，也未部署到线上服务器**）；
   - 若本地开发预览服务仍在端口 `18765` 运行，测试结束后按需关闭或续用即可。
2. **已就绪的下一份设计方案（尚未编码）**：
   - 工作区保留了一份未跟踪新设计稿 [`docs/08-心跳恢复方案.md`](docs/08-心跳恢复方案.md)（将心跳与久违想念收口为 `heartbeat_mode = off | gentle | lively` 三档、全局+单会话两层每日主动额度、无由头零模型调用、有由头单次 `utils` 便宜 JSON 判断及 `heartbeat_ticks` 玻璃盒记录），可作为下一会话按其第 9 节 TDD 顺序直接推进的入口。
3. **部署红线（来自 [AGENTS.md](AGENTS.md)）**：
   - `AGENTS.local.md`、`config.toml`、`.local-checks/` 与 `.trellis/` 只留本地，严禁写入或提交进仓库；测试夹具统一使用 `qq:group:100246809`、`qq:private:10001`。
   - 任何涉及线上 `plugins/` 的放入、覆盖或删除（会触发插件热重载）前，**必须取得用户当次明确同意**；
   - 部署前必须先将既有线上插件文件与运行期数据（SQLite 必须用只读 `.backup()` 复制）备份至 `/root/backups/`，并保留线上 `config.toml`；
   - 严禁修改 MaiBot 宿主源码、严禁擅自更改 `config/bot_config.toml`、严禁重启 MaiBot 或适配器服务（重启由用户操作）。
