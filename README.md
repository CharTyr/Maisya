<div align="center">

<img src="logo.svg" width="96" alt="Maisya Logo" />

# Maisya

**以 MaiBot 自身决策模型为唯一调度中枢，通过全能默认能力与可渐进定制的 Agent 档案，为 MaiBot 提供可信赖的探索、创作、执行与条件盯梢能力**

[![Version](https://img.shields.io/badge/version-v0.3.3-5ec4b8.svg)](https://github.com/CharTyr/Maisya)
[![MaiBot](https://img.shields.io/badge/MaiBot-1.0.0--1.4.99-green.svg)](https://github.com/Mai-with-u/MaiBot)
[![SDK](https://img.shields.io/badge/插件%20SDK-2.8.1--2.23.99-green.svg)](https://github.com/Mai-with-u/MaiBot)
[![License](https://img.shields.io/badge/license-GPL--3.0-orange.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/CharTyr/Maisya)](https://github.com/CharTyr/Maisya/stargazers)

[项目特色](#项目特色) • [使用范例](#使用范例) • [与 MaiWork 怎么选](#与-maiwork-怎么选) • [快速开始](#快速开始) • [管理工作台](#管理工作台) • [详细文档](#详细文档)

</div>

> Maisya 是 MaiBot 插件（ID `chartyr.maisya`）。它**不另起第二套机器人，也不抢日常聊天的话筒**——**MaiBot 自己的决策模型（Planner）就是唯一的调度者与发言人**。平时闲聊一切照旧；遇到查证、作图做网页、跑脚本、跨群运维或定时盯梢时，MaiBot 自主调起全能默认能力或专属定制档案，完成后仍用自己的语气和群聊习惯交回对话。

---

## 项目特色

- **开箱三位专长角色与渐进定制，越用越顺手**：开箱提供 **`default`**（全能默认助手，整合查证、图文对比长卡与工作区持续产出）、**`scout`**（查证助手）与 **`digest`**（资讯简报）三位角色；每位角色都有独立长期记忆 `profiles/<id>/MEMORY.md` 与实战经验 `NOTES.md`，任务完成后自动复盘、首次提炼新技能先由你预览保存，后续同类任务自动补强规则。
- **直接发密码就能干活，保险箱只是可选项**：平时直接发密码、API Token 或私钥委托 MaiBot 做事时，直接交给线程脱敏执行、绝不拒绝或强求先存保险箱；只在你想给常用密码起别名重复用时，才按需放进「设置 → 密码保险箱（仅存别名 + 密码，查看只显示别名和日期）」。
- **管理员直通与双重安全边界**：支持宿主管理员与插件管理员双源合并，开启插件后**已核验管理员私聊无需先加群白名单即可直接调用全部能力**与跨群调度；一句对话即可开启群聊、设早报与各群主动分寸，还能立即试跑预览发回私聊。
- **单一收敛心跳 + 条件盯梢（Watch）**：不再维护脆弱的外部 Webhook；由统一的心跳检查搭配条件盯梢，没变化静默记账不刷屏，真正达到阈值或条件才唤醒汇报；详细微调参数也可以直接跟 MaiBot 对话让它自调整。
- **五页双主题工作台与真实 `.maisya.zip` 分享**：零额外端口直嵌 MaiBot WebUI，提供 **现场 / 产物 / 能力（角色 | 技能 | 外部连接） / 手账 / 设置** 五页（千禧机械风 / 现代极简风 / 自动跟随）；外部连接支持粘贴常见 `mcpServers` JSON、直接填写密码/Token、远程 HTTP 与本机程序一键试连取工具；角色与能力支持导出脱敏 `.maisya.zip`（含公开 `https` 直链导入与推荐定时绑定），并支持随时切换开启 5 步首次引导。

---

## 使用范例

### 1. 轻型快查与直接作图：一句话从核实到成片
- **怎么触发**：`「@MaiBot 刚聊到的那个新开源库实测性能怎么样？帮我核实下顺便拉张对比图发群里」`
- **怎么运转**：MaiBot 调起 **`default`** 能力检索原文、核对数据并在工作区渲染成图卡或报告页交付；收到图后再追一句 `「把许可证和内存占用那栏补上」`，会在同一线程原位改图重出。

### 2. 并行调研与多路汇合
- **怎么触发**：`「@MaiBot 帮社团盘点本周国内外独立游戏新品和开发工具更新，分成两路先查再合成完整网页报告发群文件」`
- **怎么运转**：MaiBot 在同一轮**并行开启多条 `default` 线程**分头检索；后台完成后自动汇合唤醒 MaiBot，再接力整合成网页报告和封面图卡一次发回群里。

### 3. 专属定制档案与常驻工作区
- **怎么触发**：为某个群定制一个「克苏鲁守秘人」或「项目日报助手」档案（绑定需要用到的技能、连接器或小鱼头像），后续在该群提起它时，会自动接上同会话的常驻主线程与持久化工作区进度，不需每次手动记线程号。

### 4. 管理员私聊全局调度 & 条件盯梢（Watch）
- **私聊一句话跨群安排**：管理员直接私聊 MaiBot 即可让它给指定群跑试稿、调整记忆或定时任务，并可指定 `deliver_to` 先把成稿发回自己私聊预览，确认满意后再发进目标群。
- **按条件安静盯梢**：
  > 「每隔半小时帮我看一次仓库 Release 页面，如果发布了新的正式版就来提醒我，没有更新就保持安静。」

---

## 与 MaiWork 怎么选？

作者同时维护了 [MaiWork](https://github.com/CharTyr/MaiWork) 与 Maisya 两个插件，定位侧重不同，按自己的使用习惯选即可：

- **[MaiWork](https://github.com/CharTyr/MaiWork)**：偏向**预先定制好的群生产力栏目（资讯 / 构想 / 长期目标）+ 偏重型、工单式的任务审批与验收交付**，自带独立管理端口网页与一次性云端/本机重型执行环境。
- **Maisya**：**以 MaiBot 自身决策模型（Planner）作为唯一调度者与发言人**，平时完全保持 MaiBot 原有聊天人设，需要查证、作图、跑脚本或条件盯梢时由全能默认底座与按需定制的档案接管执行；支持免容器工作区直跑与对话式高风险确认，也可按需挂载可选 Docker 隔离层。

| 对比维度 | Maisya | [MaiWork](https://github.com/CharTyr/MaiWork) |
|:-----|:-----|:-----|
| **定位与模式** | 类似 Grok Bot 的灵活、可编排多 Agent 原生实现 | 预先定制好的群生产力栏目 + 工单式审批交付 |
| **调度与发言主体** | **MaiBot Planner 本身**唯一调度并按原人设开口 | 独立主模型调度、审单、验收并对外交付 |
| **能力装配与扩展** | 开箱全能 `default` 底座 + Hermes 渐进定制档案（5 大工具集 / 技能 / 连接器 / `.zip` 分享） | 围绕既定群栏目沉淀群理解、个人画像与任务流 |
| **执行与安全边界** | 免容器受控工作区直开 + 关键操作确认闸，群友执行可选轻量 Docker 隔离 | 独立重型/一次性云端或本机隔离执行环境 |
| **界面与入口** | 零新端口直嵌 MaiBot WebUI（五页工作台 + 5 步首次引导） | 独立管理端口网页 |

---

## 快速开始

### 第一步：安装插件（三种方式任选）

1. **从 MaiBot 插件市场直接安装（推荐）**
   打开 **MaiBot WebUI → 插件市场**，搜索 **「Maisya」** 点击安装。

2. **填入仓库地址安装**
   在 **MaiBot WebUI → 插件管理** 页面填入仓库地址 `https://github.com/CharTyr/Maisya.git` 安装。

3. **命令行克隆**
   在 MaiBot 的 `plugins/` 目录下执行：
   ```bash
   git clone https://github.com/CharTyr/Maisya.git CharTyr_Maisya
   ```

### 第二步：跟着 5 步引导完成初始化

打开 **MaiBot WebUI → Maisya**：

1. 首次安装会自动弹出 **5 步引导向导**，无需手动编辑 TOML 即可完成：
   - 确认宿主继承或补充插件侧管理员账号（设置后**管理员私聊默认直接全能力可用**）；
   - 一键打开插件总开关并勾选要服务的群或私聊（支持 `<platform>:<group|private>:<id>`、QQ 官方区分大小写 OpenID 与 Telegram 论坛话题）；
   - 选择全局默认模型、测试模型连通与工具调用能力、选定高风险操作确认模式与工作台主题；
   - 查看 7 项能力就绪清单并可直接复制开箱试跑语句发给 MaiBot。
2. 默认 `enabled_chats = []` 下，未放行群聊与非管理员私聊依然保持零读取、零调用、零打扰。

#### 跨平台运行说明（Windows / macOS / Linux / Docker）

- **全平台免容器开箱即用**：直接复用 MaiBot 已配好的模型和联网来源；管理员触发时默认在独立线程工作区安全执行脚本与工具。
- **缺浏览器内核时补装一次**：若精简镜像或新系统首次做图卡提示缺少 Chromium，在**运行 MaiBot 的宿主**执行 `playwright install chromium` 即可（Linux 环境按需补充 `playwright install-deps chromium`）。
- **Docker + NapCat 跨容器传群文件**：若 MaiBot 与 NapCat 不共享磁盘目录，群文件发送会自动触发 `base64://` 兜底上传。
- **可选 Docker 沙箱按需开启**：若希望群友也能在群聊安全运行自由 Python/Node 脚本，再按 [运行环境、沙箱与连接器指南](docs/guide/sandbox-and-services.md) 开启轻量 Docker 隔离层（系统会自动探测 `runsc`，无 `runsc` 时使用无特权隔离容器）。升级用户可参考 [更新日志](CHANGELOG.md) 的升级须知，老配置里的空镜像与旧运行时无需手改。

---

## 管理工作台

无需另开端口或额外密码，登录 MaiBot 控制台后在扩展页打开 **Maisya**，即可使用自包含五页工作台（**现场 / 产物 / 能力 / 手账 / 设置**）：

- **双主题切换**：默认采用协调宿主的**千禧机械风（`millennium`）**，随时可一键切换**现代极简风（`modern`）**或跟随宿主（`auto`）；
- **现场**：左侧查看「预定与盯梢（`schedule` / `watch` 巡检状态与启停）」与「活动时间线」，右侧查看垂直步骤时间线（支持点开每一步脱敏命令与输出）联动上下文 Token 方块图；发起新线程时可下拉选角色、会话与已有/新建工作区；
- **产物**：统一集中浏览图文、网页报告、代码、图表与交付结论（支持分类统计、来源会话筛选与预览）；
- **能力（角色 | 技能 | 外部连接）**：管理 `default` / `scout` / `digest` 及定制角色的小鱼外形、`profiles/<id>/MEMORY.md` 与经验笔记，预览确认自动复盘出的新技能并编辑附属文件，导出/导入脱敏 `.maisya.zip`；在「外部连接」支持粘贴 `mcpServers` JSON、直接填密码/环境变量、远程 HTTP / 本机程序一键试连获取工具，以及登记宿主跨插件接口；
- **手账**：统一查看今日主动搭话额度与未回锁状态，集中编辑并回滚会话 `MEMORY.md`、自身近况 `JOURNAL.md` 与 `HEARTBEAT.md`，以及一键撤回自动经验回执；
- **设置**：直观管理会话开关、管理员与审批策略、只存「别名 + 密码」的可选密码保险箱，以及直观的常规心跳/免打扰/主题/沙箱设定。

---

## 详细文档

- **用户与配置指南**：[核心功能与交互详解](docs/guide/features.md) · [配置、定时盯梢与私聊管理说明](docs/guide/configuration.md)
- **环境与安全边界**：[运行环境、沙箱、安全存储与连接器](docs/guide/sandbox-and-services.md)
- **开发与架构参考**：[开发入门与防跑偏红线](docs/00-开发入门.md) · [架构蓝图](docs/01-架构蓝图.md) · [管理台与接入](docs/04-管理台与接入.md) · [宿主接口事实](docs/06-宿主接口事实.md) · [极简高权收敛计划（归档）](docs/archive/07-极简高权收敛计划.md) · [代码地图](codemap.md)

---

## 许可证与致谢

本项目基于 **GPL-3.0-or-later** 开源（详见 [LICENSE](LICENSE)）。感谢 [MaiBot](https://github.com/Mai-with-u/MaiBot) 与 [gVisor](https://gvisor.dev)。

[@CharTyr](https://github.com/CharTyr)
