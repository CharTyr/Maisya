# 更新日志

只记录面向使用者的行为变化。内部重构与文档整理不单列。

## v0.3.2

全面治理配置热更新稳定性、多步推理权限与 Token 成本，升级共享工作区与群聊上下文体验，并默认激活容器自动路由与长期存储留存回收。

### 修复

- **修改 `config.toml` 不再冲掉运行期设置**：宿主配置热更新（`on_config_update`）改为三路合并，仅合入 `config.toml` 相对上次基准实际改动的键，完整保留通过 WebUI 或对话调整过的 `enabled_chats`、模型、超时、联网源与睡眠时段；目标群未启用导致定时任务跳过时写入 `wake_status="chat_disabled"` 并向管理员发去重私聊提醒。
- **多步推理不再中途丢失管理员权限**：`after_planner` 不再凭未执行的 `share` 调用提前清空本轮管理员身份，改为 `share` 真实送达后消费；同步收紧 `is_pulse_wakeup` 判定，并为 `maisya_shape`/`maisya_agent` 的非法参数报错附带合法枚举、本轮可用 `source_msg_id` 与当前版本号提示。
- **统一后台预算账目与阻断周期历史滚雪球**：用户结果回接（`return`）豁免任务不再扣减 `bg_budget_usage` Token 额度，非豁免后台任务补齐真实耗时记账；周期工作流复用空闲线程前自动归档上一周期工具大历史，大幅降低重复推理消耗。

### 改进

- **共享工作区按事独立线程与群聊话题账本**：支持 `workspace` 会话模式（共享工作区文件与 `project/STATE.md`、按独立话题轻量起线程并注入 `<workspace_context>`），群聊 `<maisya_threads>` 带发起人与新鲜度分流、冷线程自动下桌、跨群管理员私聊预览严格隔离；管理台与日常提示词去掉 JSON 引号 `&quot;` 膨胀与未展开 `maisya_shape` 时的冗余说明。
- **联网负缓存、本地可读时间与图卡压缩去重**：宿主统一联网未启用时负缓存 300 秒避免重复报错刷屏；`chat_read`/`message_read` 新增可读本地时间 `time_local`；`html_render` 支持 `.jpg`/`.webp` 压缩及超 600KB PNG 无损优化（预设简报卡切为 `out/digest-card.jpg`），`result_attach` 同轮按 SHA-256 去重；支持角色级 `timeout_seconds`（`digest` 默认 300 秒）。
- **默认激活容器自动路由与自动进展便签**：补齐 `effective_mode()` 对 `"auto"` 的识别，预置 `default` 档案切为 `execution_mode = "auto"`（有沙箱自动走隔离容器、无沙箱平滑回退，未定制的旧 `default/profile.toml` 自动升级）；`digest` 筛选与出卡后程序化写入 `<=250` 字 `project/STATE.md` 同步 `state_note`；`Sandbox.probe()` 补充 `bridge`/`docker0` 排障提示；`Learning` 新增跳过原因诊断计数。
- **存储留存清理与运维健壮性**：启动时安全清理空遗留表 `incoming_events`、自动压缩超 14 天非活跃大载荷并回收超 30 天无引用附件与孤儿文件/孤儿工作区目录；`social_state` 与 `publication_grants` 写入收口为单条规范键；`on_unload` 单组件关闭增加 2.5 秒限时保护；`chat.get_avatar` 预期不可用降级 `DEBUG` 并 300 秒负缓存。

## v0.3.1

修掉「升级后打不开轻量 Docker 沙箱」的一类老配置问题，并让沙箱开不起来时能直接看懂原因。

### 修复

- **旧版 `[sandbox] image = ""` 不再挡住开关**：老 `config.toml` 里留空的 `image` 现在视同未配置，按默认镜像 `maisya-sandbox:latest` 处理。此前在管理台或首次引导里勾选沙箱开关会整体校验失败，报 `启用容器执行需要指定有效镜像`。
- **局部保存不再挤掉沙箱默认项**：管理台与引导只提交 `{enabled}` 这样的局部补丁时，嵌套的 `[sandbox]` 会按默认值补齐，`socket`、`max_concurrent`、`subnet` 等不会再跟着丢。

### 改进

- **沙箱探测提示可操作**：镜像不存在时直接说清镜像名与两条修法（跑 `./sandbox/build_image.sh --execute` 构建，或改 `[sandbox] image`）；指定的容器运行时没装时列出当前 Docker 实际可用的运行时，并指向要改的配置项。替换原来笼统的「容器运行时操作失败：image」。
- **设置页显示沙箱状态**：打开开关后，「设置 → 基础运行 → 轻量 Docker 沙箱」下方会直接显示已就绪、未探测到 Docker，或具体的未就绪原因。

### 升级须知

- 直接覆盖插件文件即可，**不需要手工改 `config.toml`**：空镜像会自动回落默认值，配置在下次保存时写回规范化后的内容。
- 如果日志里出现「指定的容器运行时 runsc 未安装」，说明这台机器没有装 gVisor。二选一：把 `[sandbox] runtime` 改成 `""` 或 `"runc"` 跟随默认运行时（其余无特权限制不变），或者装上 `runsc` 后保留原配置。
- 沙箱镜像需要事先在本机 Docker 里存在（`./sandbox/build_image.sh --execute` 构建，或自行 `docker tag`），插件不会联网拉取。默认镜像不存在时沙箱保持未就绪，不影响插件其他功能。

## v0.3.0

- 极简高权底座：双源管理员、权限授予、密码保险箱元数据、宿主机工作区执行器与降门槛 Docker 执行器。
- 双主题五页玻璃盒管理台，含全局原生二次确认弹窗。
- 三位开箱角色 `default` / `scout` / `digest`。
- 三档心跳恢复与调度手册，默认 `heartbeat_mode = "off"`。
