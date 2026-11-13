# Maisya Repository Map (`codemap.md`)

## 顶层目录职责

仓库根目录就是插件根目录，可直接支持 MaiBot 官方后台填写仓库地址安装和点按钮更新。运行期用户资产全部存放在插件目录外的 `<宿主根>/data/maisya/`。

当前架构与实现已对齐产品设计期望：插件生命周期、外部数据目录与 `settings.base.json` 三路配置合并（`chat_proactive_levels` 等用户键映射整份原子替换）、可信消息与 `session_routes`（含 `human_count` / `last_human_at` 活跃统计）、管理员验身与 `@全体成员` 过滤、保险箱与输入/出站脱敏；`scout`/`maker`/`digest` 角色（`profile`，开箱内置 `source-checking` / `ongoing-workspace` / `news-digest` 技能与 `HEARTBEAT.md` 脾性）、按角色覆盖 `model_name` / `max_tokens` / `temperature` / `max_steps` / `web_provider` / `sandbox_targets`、开放无容器工作区与宿主 `render.html2png` 渲染出图、`person_read` / `recall_read`、管理员可写 MCP / 跨插件 `@API` / 自定义模板与完整 `SKILL.md` 生命周期（`skill_items` 元数据、`create_skill`、`reset_skill`、`bind_skill` 一键装配与删改角色/技能）、`web_provider` 多源自由映射（含 `plugin:<id>` / `web:<name>` / `mcp:<name>` / `plugin_api:<name>` 与 `arg_map` / `defaults`）、`call_capability` 显式超时与 `RPCError` 归一化；持久工作区、gVisor/Docker 沙箱与并发控制（`max_concurrent`）；分层记忆、版本回滚、可配置 `heartbeat_interval_minutes` / `heartbeat_profile` 定期探索与恢复后短方法学习；`bridge.py` / `share.py` 原生回接（`file` 附件独立拆条 `send.hybrid` 跟发）；`intents.py` / `pulse.py`（`daily_proactive_limit`、每会话独立主动级别 `chat_proactive_levels`、冷私聊 `acquaintance_users` 边界、`self` 线程 `thread_later(kind="serendipity")` 偶遇路由、规则化 `_longing`、`<maisya_temperament>` 与 `<maisya_thread_result>` 注入、`wait_thread` 5s 顺延、管理员定时/周期工作流 `every_seconds >= 300`）；`incoming.py`（支持 `profile` + 可选 `workspace` 先跑自主线程后通知）；以及五页玻璃盒管理台（现场直接唤醒角色与引导、产物集中检视与持续工作区浏览、能力页以技能库为核心与角色工作台/宿主发现只读参考、手账与探索节奏、设置与定时调度）。

```text
Maisya/
├── __init__.py                     # 只导出 create_plugin
├── _manifest.json                  # 插件清单 chartyr.maisya（含 23 项宿主能力：discovery、person.*、render.html2png 等）
├── webui.json                      # MaiBot WebUI v2 扩展声明（含 note 可选 thread_id、profile/chat_id/workspace 与 steer）
├── plugin.py                       # 宿主入口：生命周期、三路配置合并、Hook（before_planner 含 summary/autonomous 与条件提示）、@Tool、@API 胶水层
├── config.example.toml             # 默认关闭的配置示例（含 web_provider / chat_proactive_levels / acquaintance_users / longing_quiet_hours 等）
├── maisya/
│   ├── config.py                   # SDK 配置模型、web_provider/per-chat 主动级别/熟人/久违静默阈值校验与外部数据初始化
│   ├── abilities.py                # scout/maker/digest 角色（profile，含 enabled 开关）、apis 只读接口字段、task 槽位白名单、热加载、克隆与安全角色包
│   ├── digest.py                   # SSRF/XXE 防护 RSS 试订/退订状态机、硬过滤与五维打分精选、离线天光简报卡 HTML 生成与 digest_feed 工具
│   ├── maker.py                    # 在原线程内组合工作区、沙箱、输入实体、附件、联网路由、MCP/模板/跨插件 API 与 planner 路由
│   ├── workspaces.py               # 安全文件访问、串行写入、原子检查点、配额与不可变附件
│   ├── sandbox.py                  # 专用 Docker/gVisor、max_concurrent 信号量、出站规则、资源参数、取消与清理恢复
│   ├── services.py                 # 按线程隔离的 stdio MCP 池、私有固定模板、跨插件只读 @API 与 services.json["web"] 只读联网绑定
│   ├── mcp_worker.py               # MCP 独立子进程的资源限制、cgroup 与降权
│   ├── memory.py                   # 分层文件、parse_skill_meta()、预设技能与 HEARTBEAT.md 释放（create_skill/reset_skill）、heartbeat_parts() 解析、版本/回滚与 FTS5
│   ├── learning.py                 # 同一工具失败后恢复时提炼短方法，状态轨迹输入与有界后台调用
│   ├── management.py               # 五页管理 API（含产物 outputs / artifact 检视）、持久设置、版本编辑、技能目录与角色装配（skill_items/create_skill/reset_skill/bind_skill）、分块角色包、服务（含 apis/web）、执行健康（含联网状态）与事件通道校验
│   ├── incoming.py                 # 宿主认证入口的 HMAC 校验、并发锁去重、可选 profile+workspace 线程启动与 Intent 转换
│   ├── share.py                    # 原生表达（含非管理员全员艾特过滤）与链接/图片发送、file 附件独立拆条 send.hybrid 交付
│   ├── scout.py                    # 宿主能力 RPC 包装（显式 timeout_ms 与非标准异常归一化）、WebRouter 委托、plugin_api 与 thread_later 定义
│   ├── web.py                      # 统一联网路由：宿主 web.search/fetch 预设优先，未启用时自动兼容 services.web 与第三方搜索插件（SSRF 防护+单次脱敏）
│   ├── threads.py                  # 线程状态、输入队列、边界转向、serendipity 提议、异常防无限重拉兜底与 FTS5
│   ├── bridge.py                   # 结果事件引用、回接组、意图认领、Replyer 事实与重试
│   ├── intents.py                  # 意图存储：return/proactive/silent、社交闸、acquaintance_allowed、longing_ready、事件去重与延期延迟
│   ├── pulse.py                    # 统一意图分发：return、per-chat 主动级别与熟人边界、serendipity、_longing、wait_thread、HEARTBEAT
│   ├── trust.py                    # 有界可信消息与持久化 session_routes（含 human_count / last_human_at）、真实发言人验证与凭据识别
│   ├── vault.py                    # Fernet 加密保险箱、原子写入与已知凭据脱敏
│   └── console.py                  # 玻璃盒 @API：五段上下文统计（system/memory/user/assistant/tool）、scheduled_intents、游标事件回读
├── presets/
│   ├── profiles/                   # scout（utils）、maker（planner）与默认关闭的 digest（资讯简报），首次加载释放后保留用户修改
│   ├── skills/                     # 开箱方法技能（source-checking / ongoing-workspace / news-digest）
│   └── memory/                     # HEARTBEAT.md（含脾性基线与空白自主探索区）与 JOURNAL.md 模板
├── sandbox/
│   ├── Dockerfile                  # maisya-sandbox:py313 可复现镜像定义（Python 3.13 + Node 22 + 非 root 用户）
│   └── build_image.sh              # 默认 dry-run、--execute 构建沙箱镜像脚本
├── webui/
│   ├── now.html                      # 现场：线程、五段上下文条、待唤醒意图、步骤流、勾选立即转向引导与停止
│   ├── outputs.html                  # 产物：已声明附件与图片预览、工作区成品安全阅读、结论出处与持续工作区目录树
│   ├── abilities.html                # 能力：技能库（搜索/新建/导入/重置/一键装配角色）、角色名册、方法笔记与角色包
│   ├── memory.html                   # 手账：会话备忘、近期手账、探索节奏与意图回执
│   ├── settings.html                 # 设置：会话、按会话主动级别、熟识账号、久违阈值、沙箱、凭据、MCP/API 与事件通道
│   ├── management.html.in            # 四个管理页共用的内联脚本（含 data-type="map" 解析）与样式源码
│   ├── fragments/                    # 四个管理页各自的正文源码
│   └── build.py                      # 无依赖生成自包含 HTML，--check 检查产物
├── docs/
│   ├── 00-开发入门.md
│   ├── 01-架构蓝图.md
│   ├── 02-开发里程碑.md
│   ├── 03-M3执行方案.md             # 执行决策、资源预算、实施记录与运行时验收状态
│   ├── 04-管理台与接入.md           # 四页使用、三路配置合并、自身活动、范围权限、跨插件 API、平台文件与签名事件协议
│   └── 06-宿主接口事实.md           # 基于 MaiCore 1.3.20 / SDK 2.21.0 的源码核查与实测契约
├── tests/                          # 本地单测、架构对齐回归与 Playwright Chromium UI 测试
├── tests_host/                     # 真实宿主 venv 隔离契约（test_contract.py）与实模联网冒烟（run_live_smoke.py）
├── tests_sandbox/                  # 独立 Docker/runsc 运行时验收，执行需当次线上授权
├── AGENTS.md                       # 开发规则、线上红线与设计中心
├── HANDOFF.md                      # 交接状态、验收结果与上线前核查清单
└── README.md                       # 用户向项目简介
```

## 模块关系

```text
plugin.py
  ├── trust.py        # 每条宿主事件确认会话路由、真实发言人与输出脱敏/全员艾特过滤
  ├── abilities.py    # 提供可调度角色（profile）与合法 task 槽位
  ├── threads.py      # 用角色（profile）运行 Agent 线程并兜底异常防重拉
  ├── bridge.py       # 把线程结果交回 MaiBot
  ├── intents.py      # 意图、睡眠时段与社交分寸状态
  ├── pulse.py        # 处理意图、chat.open_session 唤醒与 HEARTBEAT 探索
  └── console.py      # 给玻璃盒读取事件和执行管理操作
```

## 运行期外部数据目录

```text
<宿主根>/data/maisya/
├── maisya.db                      # 含 threads/intents/trusted_messages/session_routes 等表
├── settings.json                  # 管理页持久配置
├── settings.base.json             # 上次同步的 config.toml 基线，用于重启与配置更新三路合并
├── webhooks.json                  # 事件通道固定目标与签名密钥句柄
├── vault/
├── abilities/
│   ├── profiles/<profile_id>/
│   ├── skills/<skill_id>/
│   └── services.json              # 管理员登记的程序/模板；凭据只保存句柄绑定
├── memory/
│   ├── self/HEARTBEAT.md
│   ├── self/JOURNAL.md
│   └── chats/<会话哈希>/MEMORY.md   # SHA-256 前 32 位，SQLite 保留原会话归属
└── workspaces/
    ├── ws_<内部编号>/             # SQLite 记录所属会话/自身、名称和线程绑定
    │   ├── current.json
    │   └── v_<版本编号>/          # project/in/out 的普通文件
    ├── runtime/                   # 活动期间的 tmpfs 工作副本；清理未确认则保留隔离
    └── attachments/               # 不可变 att_ 文件
```
