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

## 三、临时密码与可选密码保险箱（`/vault`）

- **用户直接发密码就能干活，不拒单**：在日常或管理员任务中，如果用户直接在对话里提供临时密码、API Token 或私钥块让 Bot 去完成具体事情，执行器会通过当次临时环境变量注入（或 300s 内存临时私钥缓存）完成任务，同时自动在日志与返回值中脱敏、不写进永久保险箱。
- **可选密码保险箱（别名 + 密码）**：当你希望把某个常用密钥起个固定别名、以后只提别名不重复发密码时，才需要存进保险箱：
  1. 在管理台 **`设置 → 密码保险箱`** 只填写「别名 + 密码」保存；列表与「查看」弹窗只显示**别名与保存日期**，绝不显示密码明文；
  2. 或使用已核验的管理员账号在**私聊**发送 `/vault set <别名> <密码>` 录入（进入插件时先脱敏原消息文本再存密文）。
- **外部连接密码支持直填**：添加外部 MCP 连接时，可直接把 `env` 或 `Authorization` 请求头填在连接配置里，后台会自动使用内部专用句柄加密，无需用户先跑到密码保险箱绕一圈。

---

## 四、外部连接中心：`abilities/services.json`

在管理台 **`能力 → 外部连接`**（持久化于 `<宿主根>/data/maisya/abilities/services.json`）统一接入各类外部工具（`v0.3` 已移除静态命令 `templates`）：

### 1. 本机 / 远程 MCP 服务（`servers`）

- **支持四种便捷用法**：① 一键粘贴常见 `mcpServers` JSON 配置自动填入；② 本机程序（自动解析 `npx`/`uvx`/`python3` 等裸命令路径）或远程网址（Streamable HTTP/HTTPS）；③ 配置时**直接填环境变量/请求头密码**；④ 点击 **`测试连接并获取工具`** 现场建连列出全部工具按需勾选（未勾选过滤时默认全开放 `*`）。
- 子进程仅透传最小必要环境变量（Linux 额外支持降权执行）；群友共享的只读查询工具设 `readonly = true, access = "everyone"`（群友可用但拿不到密钥），管理员私有可写 MCP 设 `readonly = false, access = "admins"`。

### 2. 跨插件 `@API`（`apis`）

在「外部连接」右栏可浏览同实例宿主下其他已安装插件公开的 `@API` 并一键登记到角色使用；普通群聊只开放 `readonly = true` 接口，可写接口仅限管理员档案与已核验管理员轮次，调用结果统一经过脱敏返回。

### 3. 自定义联网来源映射（`web`）

当宿主统一联网未开启、或需要把 `web_search` / `web_fetch` 固定接到自建服务时，在 `"web"` 节点定义 `providers` 与 `arg_map` / `defaults` 字段映射。

### 4. 平台文件实体读取器（`inputs`）

当平台消息只提供私有 `file_ref` 而没有公网下载链接时，在 `"inputs"` 按平台名（如 `"qq"`）绑定只读 MCP 读取器，将 `{message_id, file_ref}` 读回的二进制资源安全写入线程只读 `in/` 目录。

> **关于外部事件监控**：旧版外部 Webhook 推送接口（`webhooks.json` / `incoming.py`）已永久移除，不再恢复。持续状态监测与外部拉取告警请直接使用原生的 **`maisya_later(mode="watch", condition=..., every_seconds=...)`**（详见 [管理台与接入](../../docs/04-管理台与接入.md#七条件盯梢watch替代历史-webhook)）。
