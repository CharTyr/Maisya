# Maisya Repository Map (`codemap.md`)

## 顶层目录职责（仓库根目录即插件根目录）

为直接兼容 MaiBot 官方后台「填仓库地址一键安装」与「一键点按钮更新」，本仓库采用扁平根目录结构；所有运行期用户数据（保险箱、派生专岗、技能库、记忆、心跳手账与工作区）统一存放在插件目录之外的 `<宿主根>/data/maisya/`，更新或重装插件绝不丢失数据。

```text
Maisya/
├── __init__.py                     # 仅导出 create_plugin（M0 建立）
├── _manifest.json                  # 插件清单 chartyr.maisya（M0 建立）
├── webui.json                      # MaiBot WebUI v2 扩展声明（mode="custom", workspace）
├── plugin.py                       # MaiBotPlugin 入口：钩子、@Tool、@API 胶水层
├── maisya/                         # 5 大精悍内核模块
│   ├── auth_vault.py               # 管理员名册继承、双道防串号验身、凭据保险箱与脱敏闸
│   ├── profiles.py                 # 预设双底座（researcher/worker）、派生专岗热加载、脱敏 .zip 配方包导入导出、MCP 池
│   ├── runner.py                   # 轻量运行器、cancel_run_id 热转向、NEEDS_INPUT 挂起反问、同批汇合闸（Batch Barrier）、沙箱 WorkBox
│   ├── memory_skills.py            # 2200字冻结 MEMORY.md、SQLite FTS5 runs 账本、SKILL.md 多卡组合与四闸复盘
│   └── pulse.py                    # 统一后台脉搏（HEARTBEAT.md 随机心跳 + JOURNAL.md 心境手账 + schedule + 按需 Webhook + 社交分寸硬闸）
├── presets/                        # 开箱预设资产（首次启动释放至 <宿主根>/data/maisya/）
│   ├── profiles/                   # researcher / worker 默认档案模板、HEARTBEAT.md 与 JOURNAL.md 模板
│   └── skills/                     # platform-delivery 等开箱预设 SKILL.md
├── webui/                          # 自包含单文件玻璃盒管理台（遵守宿主 connect-src 'none'）
│   ├── live.html                   # 1. 现场：流向圆点动画 + 专岗步骤流 + Context Map 方块地图 + 心跳与 JOURNAL 随笔
│   ├── profiles.html               # 2. 档案：专岗卡片、配方包 .zip 导入导出、HEARTBEAT.md、MEMORY.md 与持久 workspaces
│   ├── skills.html                 # 3. 技能与工具：技能库、草稿箱、补丁 Diff 回滚、MCP 池、定时与 Webhook
│   └── settings.html               # 4. 设置：服务群与熟人边界、免打扰时段、管理员、模型/保险箱与沙箱自检
├── docs/                           # 设计蓝图、开发入门、里程碑与宿主接口事实
│   ├── 00-开发入门.md
│   ├── 01-架构蓝图.md
│   ├── 02-开发里程碑.md
│   └── 06-宿主接口事实.md
├── tests/                          # 本地快速单测与浏览器沙箱 UI 验收测试
├── tests_host/                     # 真实宿主 venv 隔离契约测试（manifest / webui.json / 组件注册 / 生命周期）
├── AGENTS.md                       # 开发规则、线上红线、设计五铁律
└── README.md                       # 用户向项目简介
```

## 运行期外部数据目录（`<宿主根>/data/maisya/`）

```text
<宿主根>/data/maisya/
├── vault/                          # 加密凭据保险箱（0600，只进不出）
├── profiles/<id>/                  # 自定义派生专岗（AGENTS.md / NOTES.md / preset/，热生效）
├── skills/                         # 生效中 SKILL.md、历史版本与 pending 草稿箱
├── pulse/                          # HEARTBEAT.md 心跳守则 + JOURNAL.md 近 7 天心境手账（<=1500字环形滚动）
├── sessions/<session_id>/          # 每会话 2200 字 MEMORY.md + 持久命名 workspaces/
└── maisya.db                       # SQLite（WAL）：可信消息表、FTS5 runs 账本、schedule、未回不连戳状态锁
```
