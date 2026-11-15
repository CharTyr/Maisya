# 运行环境、沙箱、安全存储与连接器

[← 回到 README](../../README.md)

这里说明 `Maisya v0.3` 如何在免容器环境下开箱执行任务、何时需要开启 `DockerRunner` 隔离沙箱，以及如何安全使用凭据保险箱与 `abilities/services.json` 连接器。

---

## 一、免容器开箱执行：`HostWorkspaceRunner` 与群友安全边界

在 Windows、macOS、普通 Linux 桌面或跑在容器里的 MaiBot 实例上，你不需要先配置 Docker 与 gVisor，即可直接使用联网查证、图卡报告制作和工作区脚本执行。

当 `[sandbox] enabled = false`（默认值）时，执行权限按**真实触发身份**自动裁决：

1. **已核验管理员轮次（默认直通 `HostWorkspaceRunner`）**：
   - 允许在当前线程工作区副本 `project/` 下直接运行 Python / Bash / CLI 命令（`cwd` 锁定工作区副本）；
   - 通过最小化环境变量构造（`minimal_subprocess_env()` + `PYTHONNOUSERSITE=1`）彻底剥离宿主机进程环境变量与敏感配置；
   - 独立进程组运行并强制执行超时上限（默认 `60s`，可设最高 `180s`）与输出上限（默认 `2 MiB`）；
   - 如档案启用受托凭据（`shared_secrets = true`），凭据仅在命令期间写入临时 `0o400` 只读目录 `MAISYA_SECRETS_DIR` 并于退出后立即销毁；标准输出与错误全量经过脱敏；
   - 在默认 **`permission_mode = "ask_risky"`** 下，命中写工作区外路径、高危破坏性命令（如 `rm -rf`、`git reset --hard`、磁盘/服务启停、`sudo`）、外部写操作（如 `curl -X POST` / `ssh` / `scp` / `rsync`）或未授权敏感路径/首次注入凭据时，线程会暂停并请求管理员确认，批准后才放行并支持按 `once` / `session` / `always` 记住授权。
2. **普通群友 / 非管理员轮次（无容器时自动锁定）**：
   - 未开启 Docker 时，**硬拦截任意 Shell 拼接与自由代码执行**，防止通过提示词注入读取宿主机文件；
   - 正常保留联网查证（`web`）、对话与记忆读取（`context`）、工作区文件读写及 HTML/图卡渲染（`workspace`）；若管理员在自定义档案的 `preset/scripts/` 下预置了只读 `.py` 脚本，则仅允许以无 Shell 参数数组直调固定脚本且零凭据注入。

### 跨平台实操注意点

- **HTML 渲染出图缺少浏览器内核**：`render.html2png` 复用宿主环境提供的 Chromium。首次作图如报错缺少 Playwright 浏览器，请在**运行 MaiBot 的宿主环境**执行 `playwright install chromium`（Linux 按需补充 `playwright install-deps chromium`），无需启用 Docker 沙箱。
- **Docker + NapCat 跨容器传群文件**：若 MaiBot 与 NapCat 跑在不同容器中、各自看不到对方磁盘目录，QQ 群文件路径投递失败时会自动降级走 `adapter.napcat.file.upload_group_file` `base64://` 兜底上传。

---

## 二、按需开启可选 `DockerRunner` 隔离层

当你想让**非管理员（群友）也能在群聊安全执行自由 Python / Node 脚本**，或想对某个高风险自定义档案显式指定容器隔离（`execution_mode = "sandbox"`）时，再开启 Docker 沙箱即可。

### 1. `v0.3` 降门槛隔离策略

- **不再强锁 `runsc` 与 `@sha256` 摘要**：默认配置下（`privileged_sandbox_mode = false`）支持普通镜像标签与常规 Docker 运行时；启动探测检测到宿主机安装了 gVisor（`runsc`）时自动附加 `--runtime=runsc`，未安装时平滑使用标准无特权容器（默认配置 `network_mode = "none"`、`readonly_rootfs = true`、`--cap-drop ALL`、`--security-opt no-new-privileges`、`--pids-limit 128`）。
- **群友轮次强制无秘密、无宿主挂载**：非管理员在容器中仅挂载线程工作区 `/work/project`，绝不挂入宿主目录或任何主人凭据。
- 若你仍需严格要求 `runsc` 与 `@sha256:` 不可变镜像摘要（如启用受限公网与专有容器网段隔离），可在 `config.toml` `[sandbox]` 中开启 `privileged_sandbox_mode = true`。

### 2. 构建镜像与开启方法

仓库提供了 [`sandbox/Dockerfile`](../../sandbox/Dockerfile) 与辅助构建脚本 [`sandbox/build_image.sh`](../../sandbox/build_image.sh)（包含 Python 3.13、Node 24 与非 root 运行用户 `65532:65532`）：

```bash
# 1. 先预览构建计划（默认 dry-run）
./sandbox/build_image.sh

# 2. 实际构建本地镜像 maisya-sandbox:local（也可按需自行打 tag 如 maisya-sandbox:latest）
./sandbox/build_image.sh --execute
```

构建完成后，在管理台 **`设置 → 基础运行`** 打开 Docker 隔离开关，或在 `config.toml` `[sandbox]` 中设置 `enabled = true` 并填写你构建好的 `image`（如 `"maisya-sandbox:local"`）及 `max_concurrent`（默认 `2`，范围 `1..8`）。

---

## 三、安全存储与凭据保险箱（`/vault`）

不要把 API Key、Token 或 SSH 私钥写进提示词、手账或技能文件。请统一放进凭据保险箱：

- **录入方式**：
  1. 在管理台 **`设置 → 安全存储`** 填写句柄名、类型/域名备注与密钥明文保存（界面与列表只展示元数据名录，不回显明文）；
  2. 或使用已核验的管理员账号在**私聊**发送 `/vault set <句柄名> <密钥>`（进入插件时先脱敏原消息文本再存密文）。
- **存储与脱敏**：使用 `Fernet` 加密并设 `0600` 权限存于 `<宿主根>/data/maisya/vault/`；同步维护非敏感索引 `vault_meta.json` 供管理员线程按句柄名引用，所有控制台、日志、输出与 `.zip` 档案导出均强制触发脱敏与秘密扫描拦截。

---

## 四、外部连接器登记：`abilities/services.json`

在 `<宿主根>/data/maisya/abilities/services.json`（也可经 `设置` / 管理 API 维护）统一登记四类连接器节点（`v0.3` 已移除旧版静态命令 `templates`，命令行任务统一交由工作区执行器处理）：

### 1. stdio MCP 服务（`servers`）

由管理员固定可执行程序绝对路径（拒绝 UNC 路径）、允许工具名与保险箱环境变量绑定：

- 子进程默认仅透传最小必要系统环境变量（Linux 额外支持降权执行）；
- 群友共享的带 Key 查询工具可设 `readonly = true, access = "everyone"`，群友只见查询结果、拿不到 Key；
- 管理员私有可写 MCP（`readonly = false, access = "admins"`）仅对 `admins` 档案及已核验管理员轮次开放。

### 2. 跨插件 `@API`（`apis`）

将同实例下其他已安装插件公开的 `@API` 登记为白名单：

- 设 `plugin_id`、`api_name`、参数 JSON `schema` 与 `readonly` 标志；
- 在自定义档案的连接器（`connectors`）中开启对应接口即可调用，底层通过宿主 `api.list` / `api.get` / `api.call` 执行并统一脱敏返回。

### 3. 自定义联网来源映射（`web`）

当宿主统一联网未开启、或需要把 `web_search` / `web_fetch` 固定接到自建服务时，在 `"web"` 节点定义 `providers` 与 `arg_map` / `defaults` 字段映射。

### 4. 平台文件实体读取器（`inputs`）

当平台消息只提供私有 `file_ref` 而没有公网下载链接时，在 `"inputs"` 按平台名（如 `"qq"`）绑定只读 MCP 读取器，将 `{message_id, file_ref}` 读回的二进制资源安全写入线程只读 `in/` 目录。

> **关于外部事件监控**：`v0.3` 已移除高维护成本的 `webhooks.json` 与签名接收接口。持续状态监测与外部拉取告警请直接使用原生的 **`maisya_later(mode="watch", condition=..., every_seconds=...)`**（详见 [管理台与接入](../../docs/04-管理台与接入.md#七条件盯梢watch替代历史-webhook)）。
