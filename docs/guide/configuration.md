# 配置说明

[← 回到 README](../../README.md)

日常使用中，首次安装引导与 MaiBot 后台嵌入的 **四页管理台** 已覆盖绝大多数设置；管理员也可以在任意私聊或启用群聊里用自然语言直接调整配置、档案与会话备忘。这里说明统一会话格式、双源管理员、对话塑形与底层字段速查。

---

## 一、统一会话写法：`平台:类型:id`

为了防止 QQ 群、QQ 私聊与 Telegram 群撞号，所有配置、档案作用域、定时/条件盯梢与网页管理台统一采用三段式标准写法：

| 会话类型 | 标准写法 | 示例 |
|:-----|:-----|:-----|
| QQ 群 | `qq:group:<群号>` | `qq:group:100246809` |
| QQ 私聊 | `qq:private:<QQ号>` | `qq:private:10001` |
| QQ 官方群 / 私聊 | `qq_official:<group\|private>:<OpenID>` | `qq_official:group:AbCdEf123456` |
| Telegram 群 | `telegram:group:<群ID>` | `telegram:group:-100123456` |
| Telegram 论坛话题 | `telegram:group:<群ID>::tg-topic::mt=<话题ID>` | `telegram:group:-100123456::tg-topic::mt=12` |
| Telegram 私聊 | `telegram:private:<用户ID>` | `telegram:private:804200` |

**格式注意点与向后兼容**：

- **Telegram 论坛话题（Forum Topic）**：第三段 ID 完整保留 `::tg-topic::mt=<话题ID>` 后缀，同一话题群内的不同话题可独立开关。
- **大小写敏感 OpenID**：`platform` 与 `group`/`private` 会规范为小写，但第三段会话/用户 ID **完整保留原始大小写**（适配 QQ 官方 OpenID 等区分大小写的账号）。
- **向后兼容**：历史手写的纯数字、`qq:<id>` 或宿主 32 位哈希 `session_id`，甚至对话里直接称呼群名，后台的双向别名解析都能互相识别对齐；同一群存在多份历史别名备忘时优先读取最新版本。

---

## 二、管理员直通与四块「设置」布局

### 1. 双源管理员与私聊默认直通

- **有效管理员来源**：运行时管理员名单由 **宿主 `plugin.permission`（只读继承） ∪ 插件 `admins`（引导与设置页可增删）** 合并生效，无需修改宿主 `bot_config.toml` 即可直接在管理台完成初始化。纯数字条目自动规范为 `qq:<号码>`，OpenID 保留大小写。
- **防自锁死校验**：不允许通过对话或管理接口将合并后的有效管理员删空，也不允许发起修改的管理员把自己从有效管理员中移除。
- **管理员私聊直通**：一旦 `plugin.enabled = true`，**已核验的管理员私聊无需加入 `enabled_chats` 即可直接调起全部能力、检索全部会话与跨群调度**；非管理员私聊与未加入白名单的群聊仍严格保持零响应、零写入。

### 2. 「设置」页面的四个区块

`v0.3` 将设置页收敛为四个结构化区块：

1. **会话开关**：`plugin.enabled` 总开关、管理员私聊直通提示，以及按 `<platform>:<group|private>:<id>` 逐条增删 `enabled_chats` 与单会话主动分寸（`quiet` / `care` / `lively`）。
2. **权限与管理员**：
   - 分区展示只读 `host_admins` 与可增删 `plugin_admins`；
   - **高风险操作确认策略（`permission_mode`）**：`ask_risky`（默认，仅危险/越界/首次注入凭据操作前询问确认）、`autonomous`（管理员全自主放行）、`always_ask`（所有执行动作均先确认）；
   - 查看或逐条撤销已记住的长期授权（`permission_grants`）；
   - 「重新打开首次安装引导」按钮。
3. **安全存储**：展示保险箱句柄脱敏索引（句柄名、类别、域名说明），支持新增与删除，绝不回显明文。
4. **基础运行**：全局主模型覆盖（`default_model`）、每日主动上限（`daily_proactive_limit`，`0..50`）、整点免打扰时段（`sleep_window`）、可选 `DockerRunner` 容器开关，以及外观主题切换（`millennium` 千禧机械默认 / `modern` 现代极简，兼容旧值 `maiwork` / `auto` 跟随宿主）。

---

## 三、对话式轻量管理（`maisya_shape`）

除网页管理台外，核验身份后的管理员也可以在任意私聊或启用群聊中让 MaiBot 自行调整配置：

- `maisya_shape` 保持为延迟工具，由 MaiBot 按需先通过 `tool_search` 展开，并收敛为 4 类正交操作：
  1. `overview`：读取全局或指定会话当前启用态、档案摘要、待触发定时/盯梢任务与系统文件状态；
  2. `settings`：局部 `patch`、全写、历史查看与回滚核心配置（含 `enabled_chats`、`admins`、`permission_mode`、`default_model`、`sleep_window`、`proactive_level`、`daily_proactive_limit`、`ui_style`、`sandbox`，禁止从对话把 `plugin.enabled` 设为 `false`）；
  3. `profile`：创建、读取、局部修改、启停或删除自定义能力档案（`PROFILE.md` 留空可自动生成初稿）；
  4. `file`：安全读写与版本回滚 `MEMORY.md`、`JOURNAL.md`、`HEARTBEAT.md` 或技能文档（自动拦截疑似明文密钥）。

---

## 四、多模型分配与顺序 Fallback

宿主把可用模型名录脱敏同步至 `host_models.json`（只保留模型名称、标识与提供商，**宿主的 `api_key`、`base_url` 与供应商配置绝不进入 Maisya**）。

模型解析顺序：
1. 若某档案配置了 `model_name` 与 `fallback_models`，按 `#1 → #2 → #3 …` 顺次尝试，前一位遇到错误时线程保留已有成果立即切换下一位；
2. 档案未指定具体模型时，回落使用全局 `default_model`（如留空则继续沿用 `profile.toml` 指定的宿主任务槽位 `task`，默认 `utils`）。

---

## 五、记忆分册与宿主 `A_Memorix` 的边界

| 存储位置 | 记什么 | 谁来管、怎么生效 |
|:-----|:-----|:-----|
| **宿主 `A_Memorix`** | 群友长期聊天印象、人物关系、聊过什么日常话题 | 依然由 MaiBot 宿主管理；Maisya 线程只在需要时通过 `memory_read` / `person_read` 只读检索 |
| **会话备忘（`MEMORY.md`）** | 仅限该会话的办事约定、格式偏好、常驻选题与资产说明（上限 `2,200` 字） | 管理员在 **`资料库 → 系统文件`** 或用 `maisya_shape` 编辑，支持版本历史回滚 |
| **近期手账（`JOURNAL.md`）** | 近 7 天以 MaiBot 自身视角积累的公开探索经历（上限 `1,500` 字，同日自动合并） | 自身线程公开成果或管理员整理 |
| **探索脾性（`HEARTBEAT.md`）** | 自我探索的兴趣方向与基线；留空且所有会话为 `quiet` 时不发起自主探索 | 在 **`资料库 → 系统文件`** 编辑 |
| **档案说明与笔记（`PROFILE.md` / `NOTES.md` / `SKILL.md`）** | 自定义档案约束（`PROFILE.md` 上限 `150` 行）、跨会话技能规范与自动提炼短笔记 | 档案与资料库共管，支持版本回滚 |

---

## 六、三路配置合并与平滑升级

运行期状态统一存放在插件目录之外的 `<宿主根>/data/maisya/`，配置采用三路合并（并设 `Settings` `extra="ignore"` 以忽略升级残留字段）：

- `config.toml`：启动入口与手工配置文件；
- `<宿主根>/data/maisya/settings.base.json`：上次同步 `config.toml` 的基线快照；
- `<宿主根>/data/maisya/settings.json`：管理台或 `maisya_shape` 保存的运行态配置（含历史记录 `settings_history.json`）。

---

## 七、`config.toml` 与 `profile.toml` 核心字段速查

最简初始模板见仓库根目录 [`config.example.toml`](../../config.example.toml)。

### `config.toml` 常用项

| 配置项 | 类型 | 默认值 | 说明 |
|:-----|:-----|:-----|:-----|
| `plugin.enabled` | bool | `false` | 插件总开关；`true` 后管理台与管理员私聊直通生效 |
| `admins` | list | `[]` | 插件侧管理员账号（与宿主 `plugin.permission` 合并，保留大小写敏感 OpenID） |
| `permission_mode` | string | `"ask_risky"` | 高风险执行确认策略：`ask_risky` / `autonomous` / `always_ask` |
| `enabled_chats` | list | `[]` | 启用 Maisya 的会话白名单，统一写成 `<platform>:<group\|private>:<id>` |
| `default_model` | string | `""` | 全局主模型覆盖（留空则跟随档案或宿主默认任务槽位） |
| `ui_style` | string | `"millennium"` | 管理台主题：`millennium` / `modern`（兼容旧值 `maiwork`）/ `auto` |
| `proactive_level` | string | `"care"` | 全局主动分寸：`quiet` / `care` / `lively` |
| `chat_proactive_levels` | table | `{}` | 按会话覆盖主动分寸 |
| `daily_proactive_limit` | int | `1` | 普通闲聊主动唤醒每日全局上限（`0..50`） |
| `sleep_window` | string | `""` | 免打扰区间（如 `"01-08"` 或 `"23:00-07:30"`） |
| `heartbeat_interval_minutes` | int | `60` | 自我探索心跳检查间隔（`5..1440` 分钟；`HEARTBEAT.md` 留空时不额外打扰） |
| `acquaintance_users` | list | `[]` | 允许冷私聊主动打招呼的熟识账号列表 |
| `longing_quiet_hours` | int | `0` | 熟识会话静默多久触发久违想念（`0` 关闭，或 `12..720` 小时） |
| `agent_max_steps` / `agent_timeout` / `agent_max_active` | int | `16` / `180` / `8` | 单个活动步数预算、单轮超时秒数、全插件最大并发活动线程数 |
| `thread_context_tokens` | int | `262144` | 自适应 Token 上下文窗口上限（小窗口模型自动向下封顶） |
| `sandbox.enabled` / `sandbox.image` | bool / string | `false` / `"maisya-sandbox:latest"` | 可选 Docker 隔离执行开关与镜像名 |

### 定制档案 `profile.toml` 核心字段

存放在 `<宿主根>/data/maisya/abilities/profiles/<profile_id>/profile.toml`（内建仅有 `default`，定制档案通常从页面引导或 `default` 派生）：

| 字段 | 说明 |
|:-----|:-----|
| `title` / `description` / `enabled` | 档案显示名称、一句话简介，以及启用/停用开关 |
| `base` / `session_mode` | 继承基线（默认 `"default"`）；会话模式 `"persistent"`（定制档案默认：同会话常驻主线）或 `"ephemeral"`（按事建线，兼容 `"task"`） |
| `execution_mode` / `permission_mode` | `"host"`（管理员默认工作区直跑）或 `"sandbox"`；留空则继承全局确认策略 |
| `access` / `allow_chats` / `allow_users` | 可用范围：`everyone`、`admins` 或 `listed` |
| `model_name` / `fallback_models` / `task` | 第 1 主选模型、顺次备选列表，及留空时的宿主默认槽位 |
| `toolsets` / `skills` / `connectors` | 5 大工具集开关（`context` / `web` / `workspace` / `exec` / `schedule`）、挂载技能与连接器列表 |
| `shared_secrets` / `secrets` / `sandbox_targets` | 是否向管理员运行轮次注入受托保险箱句柄（非管理员恒隔离）及受限出站白名单 |
| `fish_seed` | 档案专属像素小鱼头像样式种子 |
