# Maisya Repository Map (`codemap.md`)

## 顶层目录职责

仓库根目录就是插件根目录，可直接支持 MaiBot 官方后台填写仓库地址安装和点按钮更新。运行期用户资产全部存放在插件目录外的 `<宿主根>/data/maisya/`。

当前已实现 M0 基座、M1 线程核心与 M2 回接桥：插件生命周期、外部数据目录、可信消息与管理员验身、保险箱、输入与出站脱敏；`scout` 档案、自主只读调用、线程消息队列、模型与工具边界转向、短窗等待、停止、完整历史归档与回读、FTS5 检索；`bridge.py` 的 `return` 意图与回接组、`share` 原生表达准确载荷出口、`console.py` 与现场页。

M5 已实现通用意图、统一 Pulse 分发与社交分寸闸；M3 执行代码已接入 maker、持久工作区、容器控制、stdio MCP、凭据模板和档案包，真实 gVisor/网络/资源边界等待独立环境验收。M4 已接入分层记忆、版本历史、管理员塑形与轻量学习；M6 四页管理台、分块档案包、持久设置和事件通道已接入。自身活动与指定用户/会话范围已贯通工具入口，真实模型、平台与外部投递等待配置联调。

```text
Maisya/
├── __init__.py                     # 只导出 create_plugin
├── _manifest.json                  # 插件清单 chartyr.maisya
├── webui.json                      # MaiBot WebUI v2 扩展声明
├── plugin.py                       # 宿主入口：生命周期、Hook、@Tool、@API 胶水层
├── config.example.toml             # 默认关闭的配置示例
├── maisya/
│   ├── config.py                   # SDK 配置模型与插件目录外数据初始化
│   ├── abilities.py                # scout/maker 档案、工具白名单、热加载、克隆与安全档案包
│   ├── maker.py                    # 在原线程内组合工作区、沙箱、输入实体、附件与服务
│   ├── workspaces.py               # 安全文件访问、串行写入、原子检查点、配额与不可变附件
│   ├── sandbox.py                  # 专用 Docker/gVisor、出站规则、资源参数、取消与清理恢复
│   ├── services.py                 # 按线程隔离的 stdio MCP 池、工具 schema 与私有固定模板
│   ├── mcp_worker.py               # MCP 独立子进程的资源限制、cgroup 与降权
│   ├── memory.py                   # 分层文件、版本/回滚、上下文、隐私匹配检查与隔离 FTS5
│   ├── learning.py                 # 同一工具失败后恢复时提炼短方法，状态轨迹输入与有界后台调用
│   ├── management.py               # 四页管理 API、持久设置、版本编辑、分块档案包、服务与事件登记
│   ├── incoming.py                 # 宿主认证入口的 HMAC 校验、时效、去重与 Intent 转换
│   ├── share.py                    # 原生表达与准确链接、文件和媒体的单次出口
│   ├── scout.py                    # 宿主原生模型、联网、当前会话消息/记忆、识图与线程内回读工具定义
│   ├── threads.py                  # 线程状态、输入队列、边界转向、事件、短窗等待、停止、上下文归档/回读与 FTS5
│   ├── bridge.py                   # 结果事件引用、回接组、意图认领、Replyer 事实与重试
│   ├── intents.py                  # 意图存储：return / proactive / silent、社交状态、每日额度与到期认领
│   ├── pulse.py                    # 统一意图分发：return 回接、later 提醒、proactive 过闸唤醒、silent 线程推进
│   ├── trust.py                    # 有界可信消息映射、真实发言人验证、工具过滤与明确凭据输入识别
│   ├── vault.py                    # Fernet 加密保险箱、原子写入与已知凭据脱敏
│   └── console.py                  # 玻璃盒 @API、游标事件读取与管理操作
├── presets/
│   ├── profiles/                   # scout 与 maker，首次加载释放后保留用户修改
│   ├── skills/                     # 开箱方法技能
│   └── memory/                     # HEARTBEAT.md 与 JOURNAL.md 模板
├── webui/
│   ├── now.html                    # 现场：线程、意图、步骤流、上下文方块图、递纸条与停止
│   ├── abilities.html              # 能力：档案、技能、笔记、MCP、执行环境与档案包
│   ├── memory.html                 # 记忆：近况、经历、会话记忆、工作区与统一检索
│   ├── settings.html               # 设置：会话、边界、主动度、管理员、模型、保险箱与自检
│   ├── management.html.in          # 三个管理页共用的内联脚本与样式源码
│   ├── fragments/                  # 三页各自的正文源码
│   └── build.py                    # 无依赖生成自包含 HTML，--check 检查产物
├── docs/
│   ├── 00-开发入门.md
│   ├── 01-架构蓝图.md
│   ├── 02-开发里程碑.md
│   ├── 03-M3执行方案.md             # 执行决策、资源预算、实施记录与运行时验收状态
│   ├── 04-管理台与接入.md           # 四页使用、自身活动、范围权限、平台文件读取器与签名事件协议
│   └── 06-宿主接口事实.md
├── tests/                          # 本地单测与浏览器 UI 测试
├── tests_host/                     # 真实宿主 venv 隔离契约测试
├── tests_sandbox/                   # 独立 Docker/runsc 运行时验收，执行需当次线上授权
├── AGENTS.md                       # 开发规则、线上红线与设计中心
└── README.md                       # 用户向项目简介
```

## 模块关系

```text
plugin.py
  ├── trust.py        # 每条宿主事件先确认会话与真实发言人
  ├── abilities.py    # 提供可调度能力档案
  ├── threads.py      # 用能力档案运行 Agent 线程
  ├── bridge.py       # 把线程结果交回 MaiBot
  ├── intents.py      # 意图与社交分寸状态
  ├── pulse.py        # 处理意图并唤醒会话
  └── console.py      # 给玻璃盒读取事件和执行管理操作
```

## 运行期外部数据目录

```text
<宿主根>/data/maisya/
├── maisya.db
├── settings.json                  # 管理页持久配置；宿主 self 配置更新时同步
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
