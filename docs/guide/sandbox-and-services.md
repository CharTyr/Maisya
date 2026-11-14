# 运行环境、沙箱与外部接入

[← 回到 README](../../README.md)

这里说明什么场景不需要容器、什么场景才需要开启 `Docker + gVisor` 隔离沙箱，以及怎么把外部工具、MCP、跨插件接口、凭据和外部事件安全接进来。

---

## 一、不用装容器先用起来：无容器轻量工作区模式

在很多部署环境里（例如 macOS 开发机、普通桌面 Linux，或者跑在容器里不想套娃挂载 Docker socket 的 MaiBot 实例），你并不一定要折腾 Docker 才能用工作区和做图。

当 `[sandbox] enabled = false`（默认值）时：

- **`scout` 与 `digest`**（`environment = "readonly"`）：联网检索、网页正文提取、人物与记忆只读查阅、`digest_feed` RSS 管理、以及调用宿主 `render.html2png` 离线把 HTML 渲染成高质量 PNG 海报图卡，全部直接可用。
- **`maker` 等不依赖私密凭据与受控网段的工作区角色**：自动降级为 **无容器轻量工作区模式（`light_workspace`）**——持久工作区目录 `in / project / out`、文件读写、上游接力 `from_threads`、HTML 渲染和 `result_attach` 交付完全正常，只是对模型隐藏并拦截 `sandbox_exec` 任意命令执行。
- **哪些角色仍要求容器沙箱就绪**：凡是在 `profile.toml` 中明确配置了 `secrets`（私有凭据单次挂载）、`sandbox_targets`（受控公网 `host:port` 白名单）或 `network = "restricted"` 的高权角色，在容器未开启前仍保持禁用锁定，防止边界降级。

---

## 二、什么时候开 Docker + gVisor 沙箱，以及怎么启用

当你需要让 Agent 在群聊或项目工作区里真正跑 Python/Node 脚本、安装第三方依赖包、运行受限白名单运维任务，或者把管理员凭据单次挂入 `/run/secrets/<handle>` 时，才需要开启完整沙箱。

### 1. 环境要求与设计原则

- 宿主机建议为带有 cgroup v2 的 Linux，安装好 Docker 与 **gVisor（`runsc`）** 运行时。
- Maisya 采用**「工作区持久，执行容器一次一建」**：
  - 每个线程把工作区安全副本映射成有字节和 inode 上限的独立隔离目录后才启动单次 `runsc` 容器；
  - 容器强制非 root 用户、只读根文件系统、`cap-drop=ALL`、`no-new-privileges`，屏蔽宿主网络、局域网私网、云元数据与相邻容器；
  - 命令退出或超时后确认全部进程结束、校验路径与大小配额（`openat2` / 目录句柄防软硬链接穿越）再原子提交检查点，随后立即销毁容器。

### 2. 构建沙箱镜像与启用步骤

仓库内附带了可复现镜像定义（[`sandbox/Dockerfile`](../../sandbox/Dockerfile)，基于 Python 3.13 + Node 22 + 非 root 运行用户）与构建脚本 [`sandbox/build_image.sh`](../../sandbox/build_image.sh)：

```bash
# 1. 先以预览模式确认将执行的 docker build 命令
./sandbox/build_image.sh

# 2. 实际构建固定镜像 maisya-sandbox:py313
./sandbox/build_image.sh --execute
```

构建完成后，在管理台 **`设置 → 高级设置 → 容器沙箱与并发`**（或 `config.toml` `[sandbox]`）开启 `enabled = true` 并填好 `image = "maisya-sandbox:py313"` 与 `max_concurrent`（默认 `2`，范围 `1..8`），然后在 **`设置 → 运行健康`** 点一下「刷新检查」确认沙箱状态变为就绪。

---

## 三、凭据保险箱（`/vault`）

不要把 API Key、访问令牌或签名密钥直接写进提示词、手账或技能文件。请统一放进凭据保险箱：

- **录入方式**：
  1. 在管理台 **`设置 → 高级设置 → 凭据保险箱`** 填写句柄名与密钥明文保存（回显列表中永远只列句柄名，看不到内容）；
  2. 或用核验过的管理员账号在**私聊**发送 `/vault set <句柄名> <密钥>`（命令进入插件时消息正文即先行替换为脱敏文本，不会把明文留进聊天历史库）。
- **物理存储与自动脱敏**：使用 `Fernet` 加密并设 `0600` 权限存放在 `<宿主根>/data/maisya/vault/`；线程控制台输出、错误堆栈、学习摘要及能力配方导出前均会扫描所有已知凭据与常见密钥正则并脱敏拦截。

---

## 四、外部服务登记：`abilities/services.json`

在管理台 **`设置 → 高级设置 → 外部服务与接口`**（对应外部数据目录 `<宿主根>/data/maisya/abilities/services.json`）可以统一登记四类外部能力，角色通过对应名字或开关授权使用：

### 1. stdio MCP 服务（`servers`）

由管理员固定好可执行程序路径、白名单工具名与环境变量绑定的保险箱句柄，按需以隔离进程启动并在 Linux 上降权执行：

- 群友共享的带 Key 查询工具可设 `readonly = true, access = "everyone"`，模型与群友只能看到查回的结果，拿不到环境变量里的 Key。
- 管理员私有可写 MCP（`readonly = false, access = "admins"`）仅允许同时设为 `access = "admins"` 的角色在当前已核验管理员授权下调用。

### 2. 跨插件 `@API`（`apis`）

无需手写转接脚本，直接把同一个 MaiBot 实例里其他已安装插件公开的 `@API` 登记为白名单：

- 设定 `plugin_id`、`api_name`、参数 JSON `schema` 与 `readonly` 标志；
- 在角色配置中勾选工具 `plugin_api` 并填写授权调用的 API 别名即可。

### 3. 自定义联网来源映射（`web`）

当宿主统一联网没开启，且你想把 `web_search` / `web_fetch` 固定接到自己的搜索引擎（或想单独为某个角色指定 `web:<别名>`）时，可以在 `"web"` 节点定义 `providers` 和 `arg_map` / `defaults` 字段映射。

### 4. 平台文件实体读取器（`inputs`）

当聊天适配器仅在消息里给出平台私有 `file_ref` 而未附带可下载公网 URL 时，可在 `"inputs"` 中按平台（如 `"qq"`）绑定一个专门把 `{message_id, file_ref}` 转成二进制资源的只读 MCP 工具，读回的字节安全写入工作区只读 `in/` 目录供后续分析处理。

---

## 五、签名外部事件 Webhook（`webhooks.json`）

如果你想让外部脚本、GitHub 通知、监控告警或 CI 完成信号去触发 MaiBot 某个群里或某个自主角色的后续工作流，可以直接复用宿主已鉴权的静态 WebUI Action 通道（**不在主机新开任何未保护端口**）：

1. 先用 `/vault set my_event_key <你的签名密钥>` 在保险箱存好 HMAC 密钥。
2. 在 **`设置 → 高级设置 → 外部事件通道`** 添加通道：填目标会话（`qq:group:12345678`）、类型（`proactive` 过社交分寸闸唤醒群聊，或 `silent` 只在后台跑完更新工作区）、可选绑定自主 `profile` + `workspace`（接到事件后先跑线程整理分析再通知），并绑定刚才的凭据句柄。
3. 推送端按 HMAC-SHA256 规范对 `{channel, event_id, timestamp, payload}` 计算签名后，携带已登录的宿主 `maibot_session` Cookie 调用宿主路由：
   ```text
   POST /api/webui/plugins/runtime/webui/chartyr.maisya/settings/actions/incoming_event
   ```
   接收端严格校验证书/登录态、5 分钟时间窗、16 KiB 载荷上限与 `event_id` 幂等防重放。
