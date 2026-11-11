# Maisya Repository Map (`codemap.md`)

## 顶层目录职责

```text
Maisya/
├── AGENTS.md                       # 开发规则、线上红线、设计五铁律
├── README.md                       # 用户向项目简介
├── codemap.md                      # 仓库结构与模块边界速查（本文件）
├── docs/                           # 设计蓝图、开发入门、里程碑与宿主接口事实
│   ├── 00-开发入门.md
│   ├── 01-架构蓝图.md
│   ├── 02-开发里程碑.md
│   └── 06-宿主接口事实.md
├── plugin/
│   └── CharTyr_Maisya/             # 纯净可部署插件根目录（零测试文件、零开发杂物）
│       ├── __init__.py             # 仅导出 create_plugin（M0 建立）
│       ├── _manifest.json          # 插件清单 chartyr.maisya（M0 建立）
│       ├── webui.json              # MaiBot WebUI v2 扩展声明（mode="custom", workspace）
│       ├── plugin.py               # MaiBotPlugin 入口：钩子、@Tool、@API 胶水层
│       ├── maisya/                 # 5 大精悍内核模块
│       │   ├── auth_vault.py       # 管理员名册继承、双道防串号验身、凭据保险箱与脱敏闸
│       │   ├── profiles.py         # 预设双底座（researcher/worker）与派生专岗热加载、MCP 池
│       │   ├── runner.py           # 轻量运行器、同批并发汇合闸（Batch Barrier）、附件本地装载、沙箱 WorkBox
│       │   ├── memory_skills.py    # 2200字冻结 MEMORY.md、SQLite FTS5 runs 账本、SKILL.md 多卡组合与四闸复盘
│       │   └── pulse.py            # 统一后台脉搏（HEARTBEAT.md 随机心跳 + schedule 惦记巡检 + 按需 Webhook + 社交分寸硬闸）
│       ├── presets/                # 开箱预设资产
│       │   ├── profiles/           # researcher / worker 默认档案模板与 HEARTBEAT.md 模板
│       │   └── skills/             # platform-delivery 等开箱预设 SKILL.md
│       └── webui/                  # 自包含单文件玻璃盒管理台（遵守宿主 connect-src 'none'）
│           ├── live.html           # 1. 现场：流向圆点动画 + 专岗步骤流 + Context Map 方块地图 + 心跳记录
│           ├── profiles.html       # 2. 档案：角色/派生专岗卡片、HEARTBEAT.md、MEMORY.md 与持久 workspaces
│           ├── skills.html         # 3. 技能与工具：技能库、草稿箱、补丁 Diff 回滚、MCP 池、定时与 Webhook
│           └── settings.html       # 4. 设置：服务群与熟人边界、免打扰时段、管理员、模型/保险箱与沙箱自检
├── tests/                          # 本地快速单测与浏览器沙箱 UI 验收测试
└── tests_host/                     # 真实宿主 venv 隔离契约测试（manifest / webui.json / 组件注册 / 生命周期）
```

## 核心数据流概览

1. **会话入站（`chat.receive.after_process`）**：`< 5ms` 记录可信 `(msg_id, session_id, user_id)` 映射并解锁该用户「未回不连戳」状态；管理员私聊发凭据时入箱换句柄；永远 `continue`。
2. **原生调度（`maisaka.planner.before_request` + `@Tool`）**：启用会话剔除宿主 `subagent*`，挂载字节恒定说明块与冻结 `MEMORY.md`；无管理员发言时隐藏管理员工具；`maisya_dispatch` 秒回 `run_id`，同轮并发子任务经 **Batch Barrier** 全部收尾后合并唯一一次唤醒。
3. **专岗执行（`runner.py`）**：`researcher` 免容器纯只读；`worker` 及派生沙箱专岗在受限公网容器 `/work`（或会话专属持久 `workspace`）执行，媒体与密钥句柄全程旁路不进模型上下文。
4. **人设交付（`maisaka.replyer.before_request` + `send.hybrid`）**：单次垫入核实结论小抄（发完即焚），媒体/文件句柄以 `guided_reply` 旁路挂段直发。
5. **活人脉搏（`pulse.py`）**：空闲随机抖动读取 `HEARTBEAT.md`、巡检到期 `schedule` 或收按需 `Webhook`；无事 `HEARTBEAT_OK` / `[NO_CHANGE]` 静默，有事过三道社交分寸硬闸后经 `chat.open_session` + `maisaka.proactive.trigger` 唤醒目标会话。
6. **玻璃盒观测（`webui.json` + `@API`）**：宿主 iframe 内通过 `window.maibot` 单路串行游标轮询（严守 `inflight <= 2`），零独立端口展示实时流向与上下文构成。
