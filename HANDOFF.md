# Maisya Handoff

写给下一位接手者（人或 agent）。只记录需要上手才知道的状态、事实、坑和上线前核查；设计与规则的权威来源是根目录 `AGENTS.md`、`codemap.md` 和 `docs/` 文档。

最后更新：2026-11-13（完成全量设计期望对齐 + 工作流自由化升级：无容器工作区与 `render.html2png` 出图、自由 `web_provider` 映射、按档案自定义模型/参数与删除、管理员可写 MCP/跨插件 `@API`/自定义模板与 `sandbox_targets`、`person_read`/`recall_read`、`daily_proactive_limit`/`heartbeat_interval_minutes`/`heartbeat_profile`、控制台直起工作流与周期工作流调度；通过 `203` 项本地单测/UI 测试、`22` 项真实宿主 SDK 契约与未篡改宿主配置的隔离实模联网冒烟）。

## 这是什么

MaiBot 插件 `chartyr.maisya`。不改 MaiBot 宿主源码，给 MaiBot 提供可调度的 Agent 能力。仓库根目录就是插件根目录，可直接用 MaiBot 后台的仓库地址安装。

核心设计：MaiBot 是唯一主体；四个核心概念组合出全部能力——能力档案（Profile）、Agent 线程（Thread）、意图（Intent）、记忆层（Memory Layers）。planner 可见工具只有两个：`maisya_agent`、`maisya_recall`；延迟工具 `maisya_later`、`maisya_shape`。

## 当前真实状态

| 范围 | 状态 | 依据 |
|---|---|---|
| M0 基座 / M1 线程 / M2 回接 | 已实现、加固、对齐并验证 | 203 项本地单测/UI 测试 + 22 项宿主 SDK 契约（含 `web_provider` 多源回退/映射、`send.hybrid` `file` 拆条与 `before_planner` 精简） |
| M3 maker + 工作区 + 沙箱 + 凭据 + `@API` | **真实 runsc 验收通过；工作流已放开** | `tests_sandbox/run_isolated.py --execute` 全绿，`host_unchanged` / `backup_cleaned` 复核通过；支持无容器 `workspace_file`/`html_render`/`result_attach`、管理员可写 MCP/跨插件 `@API`/自定义模板与 `sandbox_targets` |
| M4 记忆与塑形 | 已实现并验证 | 本地单测 + `Learning._learn` 真实 LLM 冒烟 (`applied`)；含 `HEARTBEAT.md` 脾性/方向拆分、`allow_custom` 放开自定义正文与 `delete_profile`/`delete_skill` |
| M5 意图 / Pulse / 克制主动性 | 已实现并对齐 | `daily_proactive_limit`（0..50）、`heartbeat_interval_minutes`（5..1440）、`heartbeat_profile`、管理员 `every_seconds >= 300` 周期工作流、`session_routes` 活跃统计 + `chat_proactive_levels` + `acquaintance_users` + `longing_quiet_hours` + `serendipity` |
| M6 四页管理台与三路配置合并 | 已实现并验证 | `webui/build.py --check` + 2 项扩展 Chromium UI 测试（现场直接发起新工作流、能力增删与宿主 `discovery` 只读参考、设置自定义 `web_provider`、新参数与计划调度） |
| 真实模型 + 第三方搜索插件 / EXA 联网冒烟 | **隔离全链通过** | `tests_host/run_live_smoke.py --execute`（独立 `MAIBOT_INSTANCE_DIR`，未开启宿主 `web_search`）：`scout` 调 `web.search` 后经 `config.get_plugin` 自动回退 `google-search-plugin`（EXA）返回真实来源链接、`web_fetch` 抓取 `example.com` 成功、`maker` 走 `planner` 槽位、`Learning._learn` 回执 `applied` |
| 部署到线上 `plugins/` | **未部署（红线未放行）** | `plugins/`、`src/`、`config/`、`data/` 零改动；上线部署需用户当次授权与先备份 |

本地验证命令：

```bash
python3 webui/build.py --check
PYTHONPATH=.:.local-checks/deps ~/.venvs/maiwork/bin/python -m pytest tests -q -rs -p no:cacheprovider
```

依赖安装（如 `.local-checks/deps` 未就绪）：`python -m pip install --target .local-checks/deps 'cryptography>=46,<51' 'jsonschema>=4.23,<5'`。

真实宿主隔离契约（22 项）与隔离实模联网冒烟：

```bash
python3 tests_host/run_contract.py
python3 tests_host/run_live_smoke.py --execute
```

M3 真实容器与 gVisor 运行时验收（启动独立 Docker/containerd、建隔离 bridge/nft 规则，并通过后自动清理临时防火墙备份）：

```bash
python3 tests_sandbox/run_isolated.py --execute
```

沙箱镜像构建：

```bash
./sandbox/build_image.sh            # 打印计划（dry-run）
./sandbox/build_image.sh --execute  # 构建 maisya-sandbox:py313
```

## 接手加固清单（2026-11-13 已落地）

1. **防长耗时 LLM / 联网调用踩中宿主 30s 默认 RPC 超时**：`scout` / `maker` / `learning` / `share` / `pulse` 统一经 `call_capability` 显式传递 `timeout_ms`（LLM `90000` / `40000` / `45000`、联网 `70000`、读消息 `30000` / `15000`、混合发送 `60000`）。
2. **防 `RPCError` 等非标准 `Exception` 导致线程无限重拉死循环**：真实宿主超时/断连抛 `RPCError(Exception)`；`call_capability` 将非标准 `Exception` 归一化为 `RuntimeError`，且 `ThreadEngine._drive`、`Learning._learn` 与 `Pulse._run` 兜底捕获 `Exception`（保留 `asyncio.CancelledError`），确保异常时必然 `finish` 转 `idle` 并记录错误。
3. **能力档案 `task` 槽位与 `maker` 路由对齐**：`Profile.task` 限定 `utils | planner | replyer | fast_model | vlm`；`presets/profiles/maker/profile.toml` 设 `task = "planner"`；`scout`、`maker` 与 `console` 均透传 `profile.task`。
4. **冷会话唤醒、主动额度与 `HEARTBEAT.md`**：`_manifest.json` 声明 `chat.open_session`；`trust.py` 持久化 `session_routes` 并支持 `strip_mention_all`（非管理员输出剥离 `@全体成员`/`@所有人`/`[CQ:at,qq=all]`）；`pulse._wake` 遇聊天流未建立先尝试 `chat.open_session` 一次，仍失败则 `deferred`；`later_tool(kind="proactive")` 受 `1 次/日` 主动额度和 `sleep_window` 约束；`pulse._heartbeat()` 按 `HEARTBEAT.md` 周期启动自身 `scout`（受 `proactive_level != "quiet"`、`sleep_window` 与冷却控制）。
5. **三路配置合并与沙箱并发上限**：`plugin.py` 维护 `data/maisya/settings.base.json`，在 `on_load` 与版本化 `on_config_update` 时对 `config.toml`、`settings.base.json`、`settings.json` 做三路合并；`SandboxSettings` 新增 `max_concurrent`（默认 `2`，范围 `1..8`）并同步 WebUI。
6. **回归设计期望对齐（2026-11-13）**：
   - 开箱即用预设：`presets/skills/source-checking/SKILL.md`、`presets/skills/ongoing-workspace/SKILL.md` 与 `presets/memory/HEARTBEAT.md`（区分脾性基线与空白自主方向）；优化 `scout`/`maker` 档案说明。
   - 跨插件只读 `@API`：`_manifest.json` 新增 `api.list` / `api.get` / `api.call`，`services.json["apis"]` 与 `plugin_api` 工具允许按白名单只读调用其他插件接口（适配 `api.call` 对 `result` 的 SDK 自动解包）。
   - 无缝对接 `planner` 与清晰投递：`before_planner` 输出 `summary`/`autonomous`、省略空上下文段、条件提示 `maisya_shape`（仅管理员轮）；`Share` 将 `file` 附件拆出于图文主表达之后单独 `send.hybrid` 跟发。
   - 克制主动性与事件先跑线程：新增 `chat_proactive_levels`（映射原子替换）、`acquaintance_users`、`longing_quiet_hours`、规则化 `_longing`、`self` 线程 `thread_later(kind="serendipity")` 偶遇路由、`<maisya_temperament>` / `<maisya_thread_result>` 注入与 `webhooks.json` 自主 `profile+workspace` 先跑线程再通知。
   - 玻璃盒管理台扩展：`console.py` 补全 `system`/`memory` 角色占比与 `scheduled_intents`；`now.html` 支持五段上下文柱状图与勾选 `steer` 立即转向递纸条；`settings.html` 暴露独立会话级别、熟人列表与静默小时阈值。

## 上线前人工核查清单（部署到线上前必看）

1. **宿主 `web_search` 与第三方联网兼容模式**：当前宿主 `[model_task_config.web_search].model_list` 为空（`live_web_assigned: false`）；Maisya 默认 `web_provider = "auto"` 会优先探测宿主统一联网，检测到未启用后自动通过 `config.get_plugin` 复用线上已启用的 `xxxxx7258.google-search-plugin`（EXA/Tavily/AnySearch/You/DeepSeek/DuckDuckGo 多引擎链与单次作用域脱敏），无须再改动宿主联网开关即可使用；若日后在 MaiBot WebUI「模型功能分配 → 统一联网」分配服务，`auto` 会自动切回宿主统一联网，也可在 Maisya 设置页手动固定 `web_provider`。
2. **宿主 `utils` 首选模型带工具报 400 回退**：实模冒烟日志显示 `[model_task_config.utils]` 首选 `gpt-6-luna` 在 `/v1/chat/completions` 上同时传 `function tools` 与 `reasoning_effort` 会报 `400 BadRequestError` 并回退到下一模型；建议调整宿主 `utils` 模型顺序或推理参数，避免每步多一次 400 重试延迟。
3. **沙箱镜像与防火墙准备**：若上线开启 `sandbox.enabled = true`，确认宿主已存在 `maisya-sandbox:py313`（必要时运行 `./sandbox/build_image.sh --execute`），并在用户授权后执行 `plugins/` 备份、放置与热重载。
4. **真实平台与私聊联调项**：`share` 在真实群/私聊中的 `send.hybrid` 表现、平台 `file_ref` 实际 MCP 读取器与真实群内 `pulse` 对话效果，均须在用户当次指定测试群后再行验收。

## 验收与清理纪律

- 隔离验收脚本均严格限制在 `/tmp/maisya-*` 或 `/var/tmp/maisya-sandbox-ci-*` 中运行，不触碰线上 `plugins/`、`src/`、`config/` 与活数据库。
- 实测通过后临时目录与 `/root/backups/maisya-sandbox-ci-*` 防火墙备份均自动清除；服务器连接与宿主路径保存在未入库的本地 `AGENTS.local.md`。

## 工作方式红线（摘自 `AGENTS.md`）

宿主代码零改动；不重启任何线上服务；开发/契约测试都在 `plugins/` 外的独立目录；往线上 `plugins/` 放改删 `.py`/`_manifest.json` 触发全量热重载需用户当次同意；密钥与服务器地址不落仓库、不打印；先备份再改。
