# Maisya Handoff

写给下一位接手者（人或 agent）。只记录需要上手才知道的状态、事实、红线、验证命令与上线前核查项；设计与规则的权威来源是根目录 [AGENTS.md](AGENTS.md)、[codemap.md](codemap.md)、[架构蓝图](docs/01-架构蓝图.md) 与 [极简高权收敛计划](docs/07-极简高权收敛计划.md)。

最后更新：2026-11-16（**`v0.3.0` 极简高权收敛架构基线**，对应 `main` 合入 Wave 1～3 全部代码、测试与文档后的已验证形态；历史 `v0.2.0` 五页多预置方案已收敛取代）。

---

## 1. 当前核心收敛形态与不变式

- **唯一主体**：MaiBot 是唯一对话主体，保留原有人设、日常闲聊节奏与宿主 `A_Memorix` 记忆边界；Maisya 不抢话筒。
- **四个核心概念（`v0.3` 收敛后）**：
  1. **能力档案（`Profile`）**：收敛为单一开箱全能 [`presets/profiles/default/`](presets/profiles/default/profile.toml)（开启全部 5 大 `TOOLSETS`，并透明别名映射历史 `scout` / `maker` / `digest`），以及 Hermes 式渐进定制档案（首屏极简、5 大工具集 / 技能 / 连接器开关、`PROFILE.md <= 150` 行留空自动生成、单档案 `.zip` 导入导出；已移除旧版 `recipes.py` 配方系统）。
  2. **双轨 Agent 线程（`Thread`）**：`default` 按事建轻量线程（`ephemeral`，兼容 `"task"`）；定制档案默认按 `(owner, chat_id, profile)` 自动绑定/续接同会话常驻主线（`primary_threads`，传 `fresh=true` 归档重开）。热改档案配置实时生效不锁旧工作区；`stop` 取消当前轮并回归 `"idle"` 状态，可随时通过 `send` 或 `reopen` 续聊。
  3. **意图、Pulse 与条件盯梢（`Intent` / `Watch`）**：所有异步唤醒与定时统一落入 `intents`；新增 **`maisya_later(mode="watch", condition=...)` 原生条件盯梢**替代已移除的签名 Webhook（`incoming.py` / `maisya_incoming_event` / `webhooks.json`）。`watch` 未命中条件时静默重排（`[WATCH:NO_CHANGE]`），不唤醒聊天且不扣减每日主动额度。
  4. **记忆层**：`A_Memorix`（宿主群友印象）继续只读借调；Maisya 管理会话备忘 `MEMORY.md`（≤2,200 字）、7 天近况 `JOURNAL.md`（≤1,500 字）、探索节奏 `HEARTBEAT.md` 及 `SKILL.md` / `NOTES.md` / `PROFILE.md`。
- **双源管理员、私聊直通与两级执行器**：
  - 有效管理员 = `宿主 plugin.permission（只读继承） ∪ 插件 Settings.admins（支持引导/设置页/对话增删，防清空与防删己自锁死）`，完整保留 `user_id` 大小写；
  - `plugin.enabled = true` 后，**已核验管理员私聊默认免白名单直通全部能力与跨群调度**；普通群聊与非管理员私聊继续受 `enabled_chats` 严格门控；
  - 宿主机执行采用 [`maisya/runner.py`](maisya/runner.py) `HostWorkspaceRunner`（管理员态免容器开箱执行，最小化环境 + 进程组超时强杀 + `0o400` 临时 `MAISYA_SECRETS_DIR` 退出即焚 + 输出脱敏），默认配合 `permission_mode = "ask_risky"` 挂起高风险操作至 `pending_approvals`，经真实管理员 `maisya_agent(action="approve", grant_tier="once|session|always|deny")` 确认并按需记录 `permission_grants`；
  - 可选 [`maisya/sandbox.py`](maisya/sandbox.py) `DockerRunner` 降低门槛（不再强制要求 `runsc` 与 `@sha256:` 除非开启 `privileged_sandbox_mode`），主要用于非管理员代码隔离执行；未开启 Docker 时，普通用户严禁执行任意 Shell 或自由代码。
- **planner 工具与接口收敛**：
  - 可见工具：`maisya_agent`（`start | send | stop | reopen | list | approve | share`）、`maisya_recall`、`maisya_later`（`schedule | list | cancel`，含 `mode="watch"`）；
  - 延迟工具：`maisya_shape`（**严禁设 `visibility` 标记**；收敛为 `overview` / `settings` / `profile` / `file` 4 类正交操作）；
  - 静态 `@API` 共 **6 个**（`maisya_now`、`maisya_event`、`maisya_note`、`maisya_stop`、`maisya_manage_read`、`maisya_manage_write`）。
- **四页双主题 WebUI 与 5 步启动引导**：
  - [`webui.json`](webui.json) v2 仅暴露单入口 [`webui/now.html`](webui/now.html)，内部渲染 **现场 | 档案 | 资料库 | 设置** 四页与 **5 步冷启动引导浮层**（状态存 `onboarding.json`，含 `(run_id, sequence)` 防竞态票据与 7 项只读就绪清单）；
  - 默认主题 `millennium`（千禧机械），可选 `modern`（现代极简，自动兼容旧值 `"maiwork"`）与 `auto`；
  - 移动端（`<= 720px` 主布局及底栏 / `<= 760px` 档案轮播头）保留 4 栏底栏（`.ms-mob-dock`）与无框横向游动像素鱼轮播开关（`#ab-mob-head` / `#mob-role-carousel` / `#ab-mob-role-card1`）；
  - 修改界面仅编辑 `webui/*.in`、`webui/fragments/*.html`、`webui/millennium.css`、`webui/icons.js`、`webui/fish.js` 后执行 `python3 webui/build.py`，严格遵守零 `<img>`、零 `<iframe>`、零 `localStorage`、零 `transition: all` 与峰值并发 RPC `<= 2`。

---

## 2. 最新验收实测结果（`main` Wave 3 合流基线）

- **前端编译一致性**：`python3 webui/build.py --check` 返回退出码 `0`（[`webui/now.html`](webui/now.html) 与源码模板一致）。
- **全量本地与 Playwright UI 单测**：
  - `PYTHONPATH=.:tests:.local-checks/deps ~/.venvs/maiwork/bin/python -m pytest tests -q -rs -p no:cacheprovider`：`505 passed`（`0 failed / 0 errors / 0 skipped`，加 `--rootdir=tests` 同为全量通过）；
  - 已完整覆盖后端与运行期收口（`stop -> "idle"` 取消守卫联动、无管理员状态下配置回写防误拦、`maisya_shape` 4 动作防自锁死与 `chat_proactive_levels` 写入/清空、`set_profile_models` 落盘、`chat.open_session` 热写回路由、空 `account_id` 会话升级、`start` 解除 `admin_direct` 粘性、`ui_style` 版本号同步）以及 4 页双主题 Playwright Chromium UI 验收。
- **真实宿主 venv 隔离契约**：`python3 tests_host/run_contract.py`：`Ran 22 tests` `OK`（`host_unchanged: true`，`isolated_directory_cleaned: true`）。
- **文档校验基线**：所有文档内部链接、模块清单与接口名称已逐一对照当前工作区代码校准，移除失效模块引用（`incoming.py`、`recipes.py`、`maisya_incoming_event`、`webhooks.json`、`templates` 等）。

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

## 4. 接手维护与线上部署指引

1. **本地隐私文件仅留本地**：`AGENTS.local.md`、`config.toml`、`.local-checks/` 与 `.trellis/` 不应写入或提交进代码正文；测试夹具统一使用虚拟标识如 `qq:group:100246809`、`qq:private:10001`。
2. **`v0.3` 线上清零整洁替换约定**：
   - 本次 `v0.3` 收敛重构完成验收后，**支持对线上既有 Maisya 插件文件与运行期数据执行清零整洁替换**，以干净状态完成首次 5 步启动引导与权限/模型初始化；
   - 如需保留存量数据迁移，需核对 `<宿主根>/data/maisya/` 下的三路配置与 `vault/`、历史 `maisya.db` 的兼容读取（旧 `webhooks.json` 自动忽略）。
3. **部署红线（来自 [AGENTS.md](AGENTS.md)）**：
   - 任何涉及线上 `plugins/` 的放入、覆盖或删除（会触发插件热重载）前，**必须取得用户当次明确同意**；
   - 部署前必须先将既有线上插件文件与运行期数据（SQLite 必须用只读 `.backup()` 复制）备份至 `/root/backups/`；
   - 严禁修改 MaiBot 宿主源码、严禁擅自更改 `config/bot_config.toml`、严禁重启 MaiBot 或适配器服务（重启由用户操作）。
