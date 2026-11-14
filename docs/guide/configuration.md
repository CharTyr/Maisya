# 配置说明

[← 回到 README](../../README.md)

日常使用中，绝大多数配置直接在 MaiBot 后台嵌入的 Maisya 五页管理台点一点就能生效，或者在私聊里用一句话让管理员助手替你改好；这里说明各项配置的协作关系、三路合并规则与底层配置字段速查。

---

## 一、统一会话写法：`平台:类型:id`

为了防止 QQ 群、QQ 私聊、Telegram 群撞号，所有配置、角色范围、定时安排、Webhook 与网页管理台统一采用三段式标准写法：

| 会话类型 | 标准写法 | 示例 |
|:-----|:-----|:-----|
| QQ 群 | `qq:group:<群号>` | `qq:group:100246809` |
| QQ 私聊 | `qq:private:<QQ号>` | `qq:private:10001` |
| QQ 官方群 / 私聊 | `qq_official:<group\|private>:<OpenID>` | `qq_official:group:AbCdEf123456` |
| Telegram 群 | `telegram:group:<群ID>` | `telegram:group:-100123456` |
| Telegram 论坛话题 | `telegram:group:<群ID>::tg-topic::mt=<话题ID>` | `telegram:group:-100123456::tg-topic::mt=12` |
| Telegram 私聊 | `telegram:private:<用户ID>` | `telegram:private:804200` |

**格式注意点与向后兼容**：

- **Telegram 论坛话题（Forum Topic）**：第三段 ID 完整保留 `::tg-topic::mt=<话题ID>` 后缀（三段式解析允许第三段内含冒号），同一个话题群里的不同话题可作为独立会话分别开关与编排。
- **QQ 官方等大小写敏感 OpenID**：`platform` 与 `group`/`private` 会自动小写规范化，但第三段会话/用户 ID **严格区分并保留原始大小写**（如 QQ 官方的 `OpenID`），手动填写 `allow_chats`、`allow_users` 或 `admins` 时请直接粘贴宿主实际显示的原始大小写。
- **向后兼容**：你在管理台里通过三段式下拉选单添加时会自动生成这种格式；如果你之前手写过 `100246809`、`qq:100246809` 或宿主内部的 32 位哈希 `session_id`，甚至对话里直接喊群名，后台的双向别名解析（`session_routes` + `chat_aliases`）都能互相识别对齐。

---

## 二、设置页面怎么分：「一般设置」与「高级设置」

### 1. 一般设置（日常开关与节奏）

- **界面设计风格（`ui_style`）**：默认 `millennium`（千禧机械风，与 MaiBot 宿主原味协调），可选 `maiwork`（清朗手账风）或 `auto`（跟随宿主当前切换）；顶栏按钮与此处保持实时同步并持久化保存。
- **启用插件与启用会话白名单（`enabled_chats`）**：用下拉卡片逐条添加或移除要服务的群与私聊，**每一条会话右侧都能独立指定主动分寸**（`安静 · 仅被动响应` / `平衡 · 适度主动关心` / `活泼 · 可分享自身偶遇` / 沿用全局默认）。
- **主动分寸与每日上限**：
  - 全局 `proactive_level`（`quiet` / `care` 默认 / `lively`）；
  - `daily_proactive_limit`（`0～50`，默认 `1`）：约束普通闲聊余韵与久违想念，避免刷屏；后台返还的用户请求结果和管理员定时工作流走独立队列。
- **免打扰时段（`sleep_window`）**：用整点范围选择器配置（如 `01:00 至 08:00`），时段内跳过所有无事主动唤醒并顺延。
- **联网搜索方式（`web_provider`）**：默认 `auto`，优先调宿主统一联网 `web.search` / `web.fetch`，宿主未开统一联网时自动兼容第三方搜索插件或自己在高级设置里登记的只读联网源。
- **自我探索心跳（`heartbeat_interval_minutes` + `heartbeat_profile`）**：默认每 `60` 分钟由指定公开角色结合 `HEARTBEAT.md` 做一次轻探索；设为 `0` 关闭。
- **常用定时安排**：支持 `单次 60s`、`每小时巡检`、`每日简报`、`资讯简报（预设）` 一键填入；在展开项里还可按**每天固定钟点（`daily_at`，如 `08:30, 19:30`）**不漂移调度，避免 24 小时叠加产生时间偏移。

### 2. 高级设置（边界、执行预算、保险箱与外部接入）

- **单轮活动预算与并发限制**：`agent_max_steps`（默认 `16` 步）、`agent_timeout`（默认 `180` 秒）、`agent_max_active`（最多并发活动线程数，默认 `8`）、`thread_idle_seconds`（闲置 `900` 秒）、`thread_context_chars`（单线程上下文字符预算 `48,000`，超出部分自动完整归档并保留索引回读）。
- **熟人边界与久违阈值**：`acquaintance_users`（填允许主动问候的私聊账号清单）与 `longing_quiet_hours`（默认 `72` 小时；设为 `0` 关闭久违触发）。
- **容器沙箱与并发槽位**、**凭据保险箱**、**外部服务与只读接口（MCP / `@API` / 联网源）**、**外部 Webhook 事件通道**与 **运行自检诊断**：按需启用与排障。

---

## 三、多模型分配与顺序 Fallback

宿主把真实模型清单通过配置更新与模型统计能力同步给 Maisya，同步时只保留模型名称、标识与提供商名字，**宿主的 API 地址与密钥绝不进入 Maisya 目录与网页接口**。

你可以在两个层级指定模型：

1. **按角色槽位回落**：`profile.toml` 中的 `task`（默认为 `utils` 或 `planner`，支持 `utils | planner | replyer | fast_model | vlm` 或自定义宿主任务名）。
2. **按角色精细串联 Fallback**：在角色配置的「角色模型分配与回退顺序」里有序加入多个具体模型（写入 `model_name` 与 `fallback_models`）。每次模型步骤会按 `#1 → #2 → #3 …` 依次尝试，上一位遇到 `RuntimeError` / `ValueError` / `OSError` / `TimeoutError` 时立刻无缝顶上，同时记住实际成功的那一个 `used_model`。

---

## 四、记忆分册与宿主 `A_Memorix` 的边界

| 存储位置 | 记什么 | 谁来管、怎么生效 |
|:-----|:-----|:-----|
| **宿主 `A_Memorix`** | 群友长期聊天印象、人物关系、聊过什么日常话题 | 依然由 MaiBot 宿主管理；Maisya 线程只在需要时通过 `memory_read` / `person_read` 只读检索 |
| **会话备忘（`MEMORY.md`）** | 仅限该会话（单个群或私聊）的办事约定、格式偏好、常驻选题、重要资产说明 | 管理员在 **`手账`** 页或一句话用 `maisya_shape` 编辑（每会话限 `2,200` 字），支持版本历史一键回滚 |
| **近期手账（`JOURNAL.md`）** | 过去 7 天以 MaiBot 自身视角积累的公开探索经历（限 `1,500` 字） | 自身线程公开成果或管理员整理 |
| **探索脾性（`HEARTBEAT.md`）** | 自我探索的性格基线与可选自主兴趣方向 | 在 **`手账 → 探索节奏`** 编辑 |
| **方法笔记与技能（`NOTES.md` / `SKILL.md`）** | 跨会话通用的做事规范，以及同一工具报错恢复或连续转向两次后自动沉淀的短经验（≤500 token 摘要，过密钥隐私检查） | 技能库与角色笔记共管，同样支持历史版本回滚 |

---

## 五、三路配置合并：`config.toml` 和网页设置怎么共存

所有运行期数据（数据库、手账、技能、角色、保险箱、工作区）放在插件目录之外的 `<宿主根>/data/maisya/`。配置保存采用三路合并策略：

- `config.toml`：插件初始启动与手动改文件入口；
- `<宿主根>/data/maisya/settings.base.json`：记录上次从 `config.toml` 同步时的快照基线；
- `<宿主根>/data/maisya/settings.json`：你在网页管理台或通过私聊 `maisya_shape` 保存的当前生效配置。

当插件重载或 `config.toml` 更新时，只有**你在 `config.toml` 里新改动的字段**会覆盖进运行态，而你之前在网页上改过的其他字段（只要 `config.toml` 对应字段没动）会完整保留。**注意：为了防止管理员在网页或私聊把自己锁在外面，对话式修改拒绝直接把 `[plugin] enabled` 设为 `false`。**

---

## 六、`config.toml` 与 `profile.toml` 常用项速查

完整可复制的初始配置示例见仓库根目录 [`config.example.toml`](../../config.example.toml)。

### `config.toml` 常用顶层配置

| 配置项 | 类型 | 默认值 | 说明 |
|:-----|:-----|:-----|:-----|
| `plugin.enabled` | bool | `false` | 插件总开关；`true` 后管理台与功能就绪 |
| `enabled_chats` | list | `[]` | 启用 Maisya 的会话白名单，建议写成 `"qq:group:12345678"` |
| `ui_style` | string | `"millennium"` | 管理台默认主题：`millennium` / `maiwork` / `auto` |
| `proactive_level` | string | `"care"` | 全局主动分寸：`quiet` / `care` / `lively` |
| `chat_proactive_levels` | table | `{}` | 按会话覆盖主动分寸，例如 `{"qq:group:12345678" = "lively"}` |
| `daily_proactive_limit` | int | `1` | 普通闲聊主动唤醒每日全局上限（`0..50`） |
| `sleep_window` | string | `""` | 免打扰整点区间，例如 `"01-08"` 或 `"23-7"` |
| `web_provider` | string | `"auto"` | 联网路由：`auto` / `host` / `google_search_plugin` / `plugin:<ID>` / `web:<别名>` / `mcp:<名>` / `plugin_api:<名>` |
| `heartbeat_interval_minutes` | int | `60` | 自我探索心跳间隔分钟数，`0` 为关闭 |
| `heartbeat_profile` | string | `"scout"` | 承担自主轻探索的角色编号 |
| `acquaintance_users` | list | `[]` | 允许冷私聊主动打招呼的熟识账号列表（`platform:user_id`） |
| `longing_quiet_hours` | int | `72` | 熟识会话静默多久触发久违想念，`0` 为关闭 |
| `agent_max_steps` / `agent_timeout` / `agent_max_active` | int | `16` / `180` / `8` | 单个活动每轮步数、单轮超时秒数、全插件最大并发活动线程数 |
| `thread_idle_seconds` / `thread_context_chars` | int | `900` / `48000` | 闲置超时与线程原文归档前的上下文字符预算 |

### 角色 `profile.toml` 核心字段

存放在 `<宿主根>/data/maisya/abilities/profiles/<profile_id>/profile.toml`：

| 字段 | 说明 |
|:-----|:-----|
| `title` / `enabled` | 角色显示名称，以及启用/停用开关（`digest` 默认 `false`） |
| `task` / `model_name` / `fallback_models` | 默认宿主模型槽位，以及有顺序的多模型主备列表 |
| `environment` / `network` | `"readonly"` 或 `"sandbox"`；容器网络策略（`"public"` / `"restricted"` / `"none"`） |
| `tools` / `skills` | 授权内置工具清单，以及绑定的技能编号列表 |
| `access` / `allow_chats` / `allow_users` | 可用范围：`everyone`、`admins` 或 `listed`（白名单，同时配会话和账号时取交集；`allow_users` 同时支持 `<platform>:<id>` 与 `<platform>:private:<id>`，并保留 OpenID 大小写） |
| `mcp` / `apis` / `secrets` / `sandbox_targets` | 绑定的 MCP 服务、跨插件 API、保险箱句柄和仅 `admins` 可用的受控出站白名单 |
| `web_provider` / `max_steps` / `max_tokens` / `temperature` | 按角色单独覆盖联网来源与采样/预算参数 |
| `fish_seed` | 可选：绑定该角色小鱼头像样式种子 |
