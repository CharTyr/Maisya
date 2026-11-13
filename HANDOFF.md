# Maisya Handoff

写给下一位接手者（人或 agent）。只记录需要上手才知道的状态、事实、坑和下一步；设计与规则的权威来源是根目录 `AGENTS.md`、`codemap.md` 和 `docs/` 六份文档。

最后更新：2026-11-13（M3 独立运行时验收补测通过）。

## 这是什么

MaiBot 插件 `chartyr.maisya`。不改 MaiBot 宿主源码，给 MaiBot 提供可调度的 Agent 能力。仓库根目录就是插件根目录，可直接用 MaiBot 后台的仓库地址安装。

核心设计：MaiBot 是唯一主体；四个核心概念组合出全部能力——能力档案（Profile）、Agent 线程（Thread）、意图（Intent）、记忆层（Memory Layers）。planner 可见工具只有两个：`maisya_agent`、`maisya_recall`；延迟工具 `maisya_later`、`maisya_shape`。

## 当前真实状态

| 范围 | 状态 | 依据 |
|---|---|---|
| M0 基座 / M1 线程 / M2 回接 | 已实现并通过本地与宿主契约 | 本地 168 项非浏览器测试 |
| M3 maker + 工作区 + 沙箱 + 凭据 | **真实 runsc 验收通过** | `tests_sandbox/run_isolated.py --execute` 全绿，`production_checks` 复核宿主未变 |
| M4 记忆与塑形 | 已实现，本地与宿主契约通过 | 本地测试 + 19 项真实宿主 SDK 契约 |
| M5 意图/Pulse | 已实现，`silent` 唤醒已修会话查找缺陷 | 本地测试覆盖；真实模型 silent 推进未联调 |
| M6 四页管理台 | 已实现 | 本地测试 + 2 项 Chromium UI 测试（本机跑，沙箱环境起不来 Chromium） |
| 真实模型/平台/外部投递 | **未联调** | 模型、联网、发消息全部走测试替身；平台真实上传与群消息效果未验 |
| 部署 | **未部署到线上 `plugins/`** | `sandbox.enabled` 默认 false；触发热重载需用户当次同意 |

本地测试命令：

```bash
source "$HOME/.venvs/maiwork/bin/activate"
cd /Users/char/Documents/Projects/Maisya
PYTHONPATH=.:.local-checks/deps python -m pytest tests -q -rs -p no:cacheprovider
```

依赖只装一次：`python -m pip install --target .local-checks/deps 'cryptography>=46,<51' 'jsonschema>=4.23,<5'`。

真实宿主契约（改了 `_manifest.json`/`webui.json`/生命周期/组件注册必跑）：

```bash
python3 tests_host/run_contract.py
```

M3 真实运行时验收（需用户当次授权，会起独立 Docker/containerd、建临时 bridge/nft 规则、备份防火墙到 `/root/backups/`）：

```bash
python3 tests_sandbox/run_isolated.py --execute
```

## 本轮 M3 验收新增覆盖（2026-11-13）

`tests_sandbox/check_runtime.py` 补上了第 9 节清单里此前只有本地 mock 的第 2、3、4 行：

- `workspace_concurrency_reload_traversal_quota_passed`：同名续接、持锁拒第二写入者、跨会话隔离、`../`/`..\\`/符号链接/`in/`/超配额拒绝、`commit` 后新 `activity` 读回检查点。
- `input_entities_and_immutable_attachments_passed`：base64 文件/图片字节/公网 URL 沙箱内下载进 `in/`；`att_` 句柄限定会话、源文件改写后附件仍读原字节、非 `out/` 拒绝声明。
- `real_stdio_mcp_sessions_and_revocation_passed`：真实 stdio MCP initialize/tools/list/tools/call；同服务按 `(chat_id, thread_id)` 独立会话；未登记工具/非法参数/撤权后调用全拒。
- `profile_clone_export_import_passed`：克隆、导出包解压仅含句柄名、导入含绑定的档案自动 `bound=false` 不执行代码。

## 实机踩过的坑（都写进了 `docs/03-M3执行方案.md`）

1. **daemon umask 继承**：远端脚本 `os.umask(0o077)` 后 fork 的 dockerd/containerd 把 snapshot 根建成 0700，容器 `/` 0700 → `failed to find initial working directory: permission denied`。修法：daemon `Popen` 用 `preexec_fn` 恢复 `os.setsid() + os.umask(0o022)`。
2. **gVisor 够不到 embedded DNS**：user-defined bridge 下 `--dns` 只作 embedded DNS 上游，gVisor 沙箱访问不到宿主 `127.0.0.11`（gVisor issue #7469）。修法：`sandbox.execute` 给有网执行 bind-mount 手写只读 `resolv.conf`（`nameserver` 取 `settings.dns`）。这是 `maisya/sandbox.py` 生产代码改动。
3. **降权 MCP worker 读不到私有路径**：`mcp_worker.py` 在 root 下 `setuid(2000000+)` 后 `execvpe` 登记的 `command`；`command` 指宿主 venv 解释器（`/root/...` 经 0700 目录）或脚本文件 0600 → `Permission denied`，worker stderr 是 DEVNULL 看不见。**MCP 登记的程序与脚本必须对降权 uid 可执行/可读**，用公共解释器（如 `/usr/bin/python3`），脚本 chmod 0644+、目录 0755。这是部署约束也是安全边界。
4. **REMOTE 脚本 umask 077 影响 check 自建文件**：check 进程 umask 077，写出的测试脚本默认 0600——凡是要被降权进程读的文件都要显式 chmod。

排障技巧：`services._session` 里 worker `stderr=DEVNULL` 会吞掉真实错误；临时把 stderr 继承出来（对 `asyncio.create_subprocess_exec` 打 patch）或直接手动复现 worker spawn 路径。

## 验收/清理纪律

- 隔离验收脚本只在 `/var/tmp/maisya-sandbox-ci-*` 放候选代码与经校验 runsc/官方镜像；启动独立 dockerd/containerd（不碰宿主 daemon 配置）；建专用 bridge + 只匹配该桥的临时 nft/iptables 规则；备份防火墙到 `/root/backups/`；结束后清理并核对宿主源码、原运行时、防火墙表名不变。
- 手工清理服务器残留时**不要 `pkill -f` 匹配模式**——会连 SSH 会话自己的 cmdline 一起杀。按 `/proc/*/cmdline` 里含 `maisya-sandbox-ci` 的进程精确杀；`findmnt` 找挂载后 `umount -l`；`nft list tables`/`iptables -S`/`ip link` 核对 `ms_*`/`MS*`/`br-ms*` 全空。
- 服务器连接与宿主路径写在本地不入库的 `AGENTS.local.md`；宿主 venv python 3.13 含插件依赖。

## 还没做（真实未尽项，不是缺陷）

- **Pulse silent 唤醒的真实模型验证**：`silent` 意图需要真模型调用产生新输入版本，check 环境无模型，属联调范围（`docs/03-M3执行方案.md` 第 9 节第 6 行）。
- **平台真实发消息/文件上传**：`share` 与 `file_ref` 读取器的真实适配器接口还没只读核实完，要补进 `docs/06-宿主接口事实.md`；真实上传需用户当次授权指定会话。
- **部署上线**：`sandbox.enabled=false` 默认；放到线上 `plugins/` 触发热重载、启用 maker 的 runsc/镜像/bridge/nft 都要用户当次同意并先备份。

## 工作方式红线（摘自 AGENTS.md，违反先停）

宿主代码零改动；不重启任何线上服务；开发/契约测试都在 `plugins/` 外的独立目录；往线上 `plugins/` 放改删 `.py`/`_manifest.json` 触发全量热重载需用户当次同意；密钥不落仓库、不打印；先备份再改。

TDD：先写失败测试再实现；测试入口找不到插件/依赖直接报错，不 skip 造假绿。
