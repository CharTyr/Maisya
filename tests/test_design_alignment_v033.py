"""产品设计初衷对齐、MCP 连接中心重构、直发密码不拒单、真实打包文件分享与四大原始场景查漏补缺验收测试。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from playwright.async_api import async_playwright, expect

from maisya.abilities import DEFAULT_INSTRUCTIONS, Profiles
from maisya.packages import (
    export_package,
    fetch_remote_package_zip,
    import_package_bundle,
    preview_export_package,
    preview_package,
)
from maisya.shaping import execute_shape
from maisya.trust import Message
from test_foundation import event as make_event, loaded


HOST_CSP_META = (
    "<head><meta http-equiv=\"Content-Security-Policy\" content=\""
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'none'; img-src data: blob:;\">"
)


def _wrap_csp(html_doc: str) -> str:
    return html_doc.replace("<head>", HOST_CSP_META, 1)


def _wire_rpc(plugin):
    inflight = 0
    peak = 0

    async def rpc(kind: str, name: str, args: dict | None):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        try:
            payload = dict(args or {})
            if kind == "query":
                if name == "read":
                    return await plugin.manage_read(**payload)
                if name == "event":
                    return await plugin.console_event(**payload)
                return await plugin.console_query(**payload)
            if name == "write":
                return await plugin.manage_write(**payload)
            if name == "note":
                return await plugin.console_send(**payload)
            return await plugin.console_stop(**payload)
        finally:
            inflight -= 1

    return rpc, lambda: peak


@pytest.mark.asyncio
async def test_direct_password_accepted_in_task_without_polluting_vault(tmp_path):
    """用户直接发密码给 Bot 干活绝不拒绝，exec_command 支持临时 env 并脱敏，Vault 保持纯净。"""
    default_md = (
        Path(__file__).resolve().parent.parent / "presets" / "profiles" / "default" / "PROFILE.md"
    ).read_text(encoding="utf-8")
    assert "受托句柄" not in default_md
    assert "切勿拒绝" in default_md or "直接用来完成" in default_md
    assert "受托句柄" not in DEFAULT_INSTRUCTIONS

    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        ev = make_event(text="用我发你的密码完成测试", user="10001", platform="qq", group=False)
        ev["message_id"] = "m-direct-pw"
        ev["session_id"] = "priv_10001"
        plugin.messages.record(Message.from_event(ev))

        plugin.profiles.update("default", permission_mode="autonomous")
        th = plugin.threads.create(
            plugin.profiles.get("default"),
            "执行带临时密码的命令",
            chat_id="priv_10001",
            platform="qq",
            user_id="10001",
            grant={"source_msg_id": "m-direct-pw"},
        )
        # 触发一次可执行脚本输出传入的环境变量，验证脱敏且不写入 vault
        secret_tok = "direct-user-pass-889900"
        import shlex
        cmd_str = f"{shlex.quote(sys.executable)} -c \"import os; print('GOT:' + os.environ['TMP_PASS'])\""
        res = await plugin.runtime.call(
            "exec_command",
            {
                "command": cmd_str,
                "env": {"TMP_PASS": secret_tok},
            },
            th,
        )
        assert res["status"] == "ok", res
        assert secret_tok not in (res.get("stdout") or "")
        assert "[REDACTED]" in (res.get("stdout") or "")
        # 用户直接发的临时密码不能写进密码保险箱
        assert plugin.vault.handles() == []
        assert plugin.vault.list_meta() == []

        # 用户直接发 PEM 私钥块（非 /vault）：聊天记录脱敏，但会话 300s 内存临时凭据保留可用
        pem = "-----BEGIN RSA PRIVATE KEY-----\nTEMP-INLINE-PEM-BODY\n-----END RSA PRIVATE KEY-----"
        pem_ev = make_event(text="用这个私钥部署 " + pem, user="10001", platform="qq", group=False)
        pem_ev["message_id"] = "m-pem-1"
        pem_ev["session_id"] = "priv_10001"
        hook_res = await plugin.after_receive(message=pem_ev)
        assert "TEMP-INLINE-PEM-BODY" not in str(hook_res)
        ephemeral = plugin.get_ephemeral_secrets("priv_10001")
        assert any("TEMP-INLINE-PEM-BODY" in item["value"] for item in ephemeral)
        assert plugin.vault.handles() == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_mcp_inline_secrets_json_parse_probe_wildcard_http_and_profile_binding(tmp_path):
    """MCP 连接中心：支持直接填密码、粘贴 mcpServers JSON、裸命令路径解析、试连取工具、HTTP MCP 及角色自动授权 mcp_call。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        # 1. 准备一个本地 stdio 测试 MCP 脚本
        mcp_script = tmp_path / "echo_mcp.py"
        mcp_script.write_text(
            """
import json, os, sys
for line in sys.stdin:
    req = json.loads(line)
    rid = req.get("id")
    m = req.get("method")
    if m == "initialize":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}}
    elif m == "notifications/initialized":
        continue
    elif m == "tools/list":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "ping_secret", "description": "Read API_TOKEN env", "inputSchema": {"type": "object", "properties": {"echo": {"type": "string"}}}},
            {"name": "sum_nums", "description": "Add two numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}}},
        ]}}
    elif m == "tools/call":
        tname = req["params"]["name"]
        args = req["params"].get("arguments") or {}
        if tname == "ping_secret":
            txt = f"{args.get('echo', '')}:{os.environ.get('API_TOKEN', '')}"
        else:
            txt = str(int(args.get("a", 0)) + int(args.get("b", 0)))
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": txt}]}}
    else:
        resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
""",
            encoding="utf-8",
        )

        # 2. 直接传 env 明文密钥 + 裸程序名（不事先存密码保险箱）进行试连发现工具
        svc0 = await plugin.manage_read("services")
        assert svc0["success"]
        probe = await plugin.manage_write(
            "probe_mcp_service",
            json.dumps(
                {
                    "name": "local_demo",
                    "transport": "stdio",
                    "command": [sys.executable, str(mcp_script)],
                    "env": {"API_TOKEN": "tok-mcp-direct-999"},
                }
            ),
        )
        assert probe["success"], probe
        tool_names = [t["name"] for t in probe["tools"]]
        assert tool_names == ["ping_secret", "sum_nums"]

        # 3. 保存含直接密码的 MCP（不传 tools 时默认 "*"，或支持 mcpServers JSON 导入）
        up = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "name": "local_demo",
                    "expected_version": svc0["version"],
                    "item": {
                        "command": ["python3", str(mcp_script)],
                        "env": {"API_TOKEN": "tok-mcp-direct-999"},
                        "description": "演示直填密码 MCP",
                        "readonly": True,
                        "access": "everyone",
                    },
                }
            ),
        )
        assert up["success"], up
        raw_services_disk = plugin.runtime.services.path.read_text(encoding="utf-8")
        assert "tok-mcp-direct-999" not in raw_services_disk
        # 用户可见密码保险箱不应被 MCP 内联密码污染
        assert plugin.vault.handles() == []
        assert plugin.vault.list_meta() == []

        # 使用真实测试脚本路径保存并绑定给角色（验证 toolsets 角色绑定 mcp 自动获得 mcp_call）
        up2 = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "name": "local_demo",
                    "expected_version": up["version"],
                    "item": {
                        "command": [sys.executable, str(mcp_script)],
                        "env": {"__KEEP__": "API_TOKEN"},
                        "tools": ["*"],
                        "readonly": True,
                        "access": "everyone",
                    },
                }
            ),
        )
        assert up2["success"], up2

        prof_upd = await plugin.manage_write(
            "profile_update",
            json.dumps(
                {
                    "id": "default",
                    "profile_id": "default",
                    "toolsets": ["context", "web", "workspace", "exec", "schedule"],
                    "mcp": ["local_demo"],
                }
            ),
        )
        assert prof_upd["success"], prof_upd
        loaded_prof = plugin.profiles.get("default")
        assert "mcp_call" in loaded_prof.tools

        # 执行 MCP 调用：实际进程拿到密码，输出经 vault 脱敏
        th = plugin.threads.create(
            loaded_prof,
            "调用本地 MCP",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="10001",
        )
        call1 = await plugin.runtime.call(
            "mcp_call",
            {"service": "local_demo", "tool": "ping_secret", "arguments": {"echo": "ok"}},
            th,
        )
        assert "ok:" in call1["text"]
        assert "tok-mcp-direct-999" not in call1["text"]
        assert "[REDACTED]" in call1["text"]

        # 4. 远程 Streamable HTTP MCP 试连与工具调用（含内联 Bearer Header 鉴权）
        seen_headers: list[str] = []

        class _McpHttpHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen_headers.append(self.headers.get("Authorization", ""))
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                rid = body.get("id")
                method = body.get("method")
                if method == "initialize":
                    res = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}}
                elif method == "notifications/initialized":
                    self.send_response(202)
                    self.end_headers()
                    return
                elif method == "tools/list":
                    res = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{"name": "remote_ping", "description": "Ping remote", "inputSchema": {"type": "object", "properties": {"msg": {"type": "string"}}}}]}}
                else:
                    msg = (body.get("params", {}).get("arguments") or {}).get("msg", "")
                    res = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"remote:{msg}:http-secret-xyz"}]}}
                payload = json.dumps(res).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "sess-http-1")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        srv = HTTPServer(("127.0.0.1", 0), _McpHttpHandler)
        srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
        srv_thread.start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/mcp"
            probe_http = await plugin.manage_write(
                "probe_mcp_service",
                json.dumps(
                    {
                        "name": "remote_demo",
                        "transport": "http",
                        "url": url,
                        "headers": {"Authorization": "Bearer http-secret-xyz"},
                    }
                ),
            )
            assert probe_http["success"], probe_http
            assert [t["name"] for t in probe_http["tools"]] == ["remote_ping"]

            svc_cur = await plugin.manage_read("services")
            up_http = await plugin.manage_write(
                "upsert_service_item",
                json.dumps(
                    {
                        "kind": "mcp",
                        "name": "remote_demo",
                        "expected_version": svc_cur["version"],
                        "item": {
                            "transport": "http",
                            "url": url,
                            "headers": {"Authorization": "Bearer http-secret-xyz"},
                            "tools": ["remote_ping"],
                            "readonly": True,
                            "access": "everyone",
                        },
                    }
                ),
            )
            assert up_http["success"], up_http
            assert "http-secret-xyz" not in plugin.runtime.services.path.read_text(encoding="utf-8")
            assert plugin.vault.handles() == []

            await plugin.manage_write(
                "profile_update",
                json.dumps({"id": "default", "profile_id": "default", "mcp": ["local_demo", "remote_demo"]}),
            )
            th2 = plugin.threads.create(
                plugin.profiles.get("default"),
                "调用远程 MCP",
                chat_id="qq:group:100246809",
                platform="qq",
                user_id="10001",
            )
            call_http = await plugin.runtime.call(
                "mcp_call",
                {"service": "remote_demo", "tool": "remote_ping", "arguments": {"msg": "hi"}},
                th2,
            )
            assert "remote:hi:" in call_http["text"]
            assert "http-secret-xyz" not in call_http["text"]
            assert "Bearer http-secret-xyz" in seen_headers

            # 删除 MCP 时同时清理关联的内部加密句柄
            svc_del_cur = await plugin.manage_read("services")
            del_res = await plugin.manage_write(
                "delete_service_item",
                json.dumps({"kind": "mcp", "name": "local_demo", "expected_version": svc_del_cur["version"]}),
            )
            assert del_res["success"]
            assert not plugin.vault.handles(include_internal=True) or all(
                not h.startswith("mcp_in_local_demo_") for h in plugin.vault.handles(include_internal=True)
            )
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_package_real_zip_remote_https_ssrf_and_schedule_binding(tmp_path):
    """打包分享去掉虚假 maisya:// 链接，保留真实 .zip 导出预览、公开 https 直链拉取防 SSRF 及推荐定时落地。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        plugin.vault.set("ext_token", "sk-real-secret-777888")
        plugin.profiles.clone("default", "game_curator", name="独立游戏策展")
        plugin.profiles.update(
            "game_curator",
            skills=["source-checking"],
            secrets=["ext_token"],
            mcp=["local_only"],
            instructions="帮 qq:group:100246809 追踪独立游戏资讯。",
        )
        plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target="chat:qq:group:100246809",
            due_at=plugin.intents.clock() + 3600,
            reason="每日独立游戏速递",
            payload={"profile": "game_curator", "every_seconds": 86400, "prompt": "整理今日独立游戏新闻", "scheduled_admin": True},
            bypass_limits=True,
            mode="schedule",
        )

        pv_exp = preview_export_package(plugin, "game_curator", kind="template")
        assert pv_exp["ok"] and pv_exp["desensitized"] is True
        assert pv_exp["schedules_count"] >= 1
        assert "ext_token" in pv_exp["required_secret_handles"]
        assert "100246809" not in json.dumps(pv_exp, ensure_ascii=False)

        exp = export_package(plugin, "game_curator", kind="template")
        assert not exp.get("share_link")
        zip_bytes = exp["zip_bytes"]

        # SSRF 拒绝内网/http 非加密链接
        for bad_url in ("http://127.0.0.1/pkg.zip", "https://127.0.0.1/pkg.zip", "https://192.168.1.10/pkg.zip"):
            with pytest.raises(ValueError):
                fetch_remote_package_zip(bad_url)

        # 公开 https 直链模拟拉取 -> 预览 -> 选择目标群并生效推荐定时
        with patch("maisya.packages.fetch_remote_package_zip", return_value=zip_bytes):
            pv_res = await plugin.manage_write(
                "package_preview",
                json.dumps({"remote_url": "https://example.org/game_curator-template.maisya.zip"}),
            )
            assert pv_res["success"], pv_res
            assert pv_res["schedules_count"] >= 1

            imp_res = await plugin.manage_write(
                "package_import",
                json.dumps(
                    {
                        "remote_url": "https://example.org/game_curator-template.maisya.zip",
                        "profile_id": "game_curator_v2",
                        "overwrite": True,
                        "schedule_target_chat": "qq:group:99887766",
                    }
                ),
            )
            assert imp_res["success"], imp_res
            assert imp_res["profile_id"] == "game_curator_v2"
            assert imp_res.get("scheduled_count", 0) >= 1
            pending = plugin.intents.list_all(status="pending", limit=20)
            assert any(
                (r.get("payload") or {}).get("profile") == "game_curator_v2"
                and r.get("target") == "chat:qq:group:99887766"
                for r in pending
            )

        # 对话式导出只提示发压缩包文件，绝不提 maisya:// 假链接
        ev = make_event(text="打包导出 game_curator", user="10001", platform="qq", group=False)
        ev["message_id"] = "m-pkg-exp"
        ev["session_id"] = "priv_10001"
        plugin.messages.record(Message.from_event(ev))
        shape_exp = await execute_shape(
            plugin,
            action="profile",
            op="export",
            target="game_curator",
            patch={"package_kind": "template"},
            current_chat_id="priv_10001",
            current_platform="qq",
            source_msg_id="m-pkg-exp",
        )
        assert shape_exp["desensitized"] is True
        assert shape_exp["filename"].endswith(".maisya.zip")
        assert Path(shape_exp["export_path"]).is_file()
        assert "share_link" not in shape_exp
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_setup_run_now_and_default_report_skills_and_proactive_tuning(tmp_path):
    """一句对话配好并当场试跑发私聊预览 + default 自带图文报告技能 + 群分寸对话调整 + shaping<=600 行。"""
    shaping_lines = len(Path("maisya/shaping.py").read_text(encoding="utf-8").splitlines())
    assert shaping_lines <= 600, f"shaping.py 超出 600 行红线: {shaping_lines}"

    default_prof = Profiles(tmp_path / "profiles").get("default")
    assert default_prof.skills == ()
    assert "render_report" in default_prof.tools
    assert "workspace_file" in default_prof.tools
    assert "result_attach" in default_prof.tools
    assert Path("presets/skills/research-report/SKILL.md").is_file()
    assert Path("presets/skills/ongoing-workspace/SKILL.md").is_file()

    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        ev = make_event(text="帮群 100246809 开早报并先试跑发我私聊", user="10001", platform="qq", group=False)
        ev["message_id"] = "m-setup-trial"
        ev["session_id"] = "priv_10001"
        plugin.messages.record(Message.from_event(ev))

        res = await execute_shape(
            plugin,
            action="setup",
            target_chat="qq:group:100246809",
            patch={
                "profile": "digest",
                "digest_topics": ["AI 工具", "独立游戏"],
                "daily_at": "08:30",
                "run_now": True,
                "deliver_to": "current",
                "chat_proactive_levels": {"qq:group:100246809": "lively"},
            },
            current_chat_id="priv_10001",
            current_platform="qq",
            source_msg_id="m-setup-trial",
        )
        assert "settings" in res["applied"]
        assert "digest_sources" in res["applied"]
        assert any(a.startswith("trial:") for a in res["applied"]), res
        assert res.get("trial", {}).get("thread_id")
        assert plugin.settings.chat_proactive_levels.get("qq:group:100246809") == "lively"
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_playwright_mcp_json_paste_probe_connectors_skill_companions_and_no_fake_link(tmp_path):
    """Playwright 端到端验证：MCP JSON 粘贴/直接密码/试连/编辑/角色勾选、技能附属文件编辑器、无假分享链接、手账主动状态条。"""
    plugin = await loaded(tmp_path, ["qq:10001"])
    await plugin.manage_write("onboarding_skip", "{}")
    try:
        mcp_script = tmp_path / "pw_mcp.py"
        mcp_script.write_text(
            """
import json, os, sys
for line in sys.stdin:
    req = json.loads(line)
    rid = req.get("id")
    m = req.get("method")
    if m == "initialize":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}}}
    elif m == "notifications/initialized":
        continue
    elif m == "tools/list":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": "fetch_docs", "description": "Search docs", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "list_tags", "description": "List tags", "inputSchema": {"type": "object", "properties": {}}},
        ]}}
    else:
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": "ok"}]}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
""",
            encoding="utf-8",
        )
        plugin.memory.create_skill(
            "guide_writer",
            title="指南编写",
            description="带参考文件的编写技能",
            content="# 指南编写\n\n按附带规范输出。\n",
            allow_custom=True,
        )
        plugin.memory.write_skill_file("guide_writer", "references/style.md", "# 样式要点\n- 短句直白\n")
        th_sk_patch = plugin.threads.create(
            plugin.profiles.get("default"),
            "整理指南输出规范",
            chat_id="qq:group:100246809",
            platform="qq",
            user_id="qq:10001",
        )
        with plugin.threads.connect() as db, db:
            db.execute(
                "INSERT OR REPLACE INTO learning_receipts VALUES (?, 1, 'applied')",
                (th_sk_patch["thread_id"],),
            )
        cur_gw = plugin.memory.read("skill", target="guide_writer")
        plugin.memory.write(
            "skill",
            cur_gw["content"].rstrip() + "\n\n## 实战补充与避坑\n- 每条规范前附适用前提。\n",
            target="guide_writer",
            expected_version=cur_gw["version"],
            author="thread:" + th_sk_patch["thread_id"],
            allow_custom=True,
        )

        rpc, get_peak = _wire_rpc(plugin)
        doc = _wrap_csp(Path("webui/now.html").read_text(encoding="utf-8"))

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            errors: list[str] = []
            try:
                page = await browser.new_page(viewport={"width": 1360, "height": 900})
                page.set_default_timeout(10000)
                page.on("pageerror", lambda e: errors.append(str(e)))
                await page.expose_function("hostRpc", rpc)
                await page.evaluate(
                    """window.maibot = {
                      ready: Promise.resolve(),
                      context: { theme: "light", host: { plugins: {} } },
                      page: { state: { tab: "abilities" } },
                      query: (name, args) => window.hostRpc("query", name, args),
                      action: (name, args) => window.hostRpc("action", name, args),
                      navigate: () => Promise.resolve(),
                    };"""
                )
                await page.set_content(doc, wait_until="domcontentloaded")

                # 1. 能力 -> 外部连接：粘贴 JSON -> 填入表单 -> 试连发现工具 -> 保存
                await page.locator('[data-ab-sub="connectors"]').click()
                await page.locator('[data-conn-input-mode="json"]').click()
                cfg_json = json.dumps(
                    {
                        "mcpServers": {
                            "docs_mcp": {
                                "command": sys.executable,
                                "args": [str(mcp_script)],
                                "env": {"DOCS_TOKEN": "tok-pw-direct-555"},
                            }
                        }
                    }
                )
                await page.locator("#conn-json-input").fill(cfg_json)
                await page.locator("#conn-json-parse").click()
                await expect(page.locator("#conn-name")).to_have_value("docs_mcp")
                await expect(page.locator("#conn-env")).to_contain_text("DOCS_TOKEN=tok-pw-direct-555")
                await page.locator("#conn-probe-btn").click()
                await expect(page.locator("#conn-tools-box")).to_be_visible()
                await expect(page.locator("#conn-tools-list")).to_contain_text("fetch_docs")
                await expect(page.locator("#conn-tools-list")).to_contain_text("list_tags")
                await page.locator("#conn-save-btn").click()
                await expect(page.locator("#conn-mcp-list")).to_contain_text("docs_mcp")
                await expect(page.locator("#conn-mcp-list")).to_contain_text("DOCS_TOKEN")

                # 2. 能力 -> 角色：展开外部连接勾选新添加的 docs_mcp 并验证打包弹窗已移除假分享链接
                await page.locator('[data-ab-sub="profiles"]').click()
                await page.locator("#pr-adv-toggle").click()
                await page.locator("#pr-tab-abilities").click()
                await expect(page.locator('[data-ab-kind="connectors"]')).to_be_visible()
                await page.locator('[data-ab-kind="connectors"]').click()
                await expect(page.locator('[data-ab-toggle-id="docs_mcp"]')).to_be_visible()
                await page.locator('[data-ab-toggle-id="docs_mcp"]').check()
                await page.locator("#pr-btn-save").click()
                await expect(page.locator("#pr-feedback")).to_contain_text("档案已保存")

                await page.locator("#pr-btn-export").click()
                await expect(page.locator("#pkg-scrim")).to_be_visible()
                assert await page.locator("#pkg-export-link").count() == 0
                assert await page.locator("#pkg-share-url").count() == 0
                await expect(page.locator("#pkg-export-preview")).to_contain_text("identity" if False else "身份设定")
                await page.locator("#pkg-close").click()

                # 3. 能力 -> 技能：编辑技能抽屉内查看与修改附属参考文件
                await page.locator('[data-ab-sub="skills"]').click()
                await page.locator('[data-open-sk-kind="skill"][data-open-sk-id="guide_writer"]').click()
                await expect(page.locator("#sk-companions-box")).to_be_visible()
                await page.locator("#sk-companions-box summary").click()
                await expect(page.locator("#sk-companions-list")).to_contain_text("references/style.md")
                await page.locator('[data-sk-comp-open="references/style.md"]').click()
                await expect(page.locator("#sk-comp-editor")).to_have_value("# 样式要点\n- 短句直白\n")
                await page.locator("#sk-drawer-close").click()

                # 4. 手账页：今日主动状态条 + 四分册（含角色记忆）+ 自动补入技能回执与撤回
                await page.locator('[data-nav="memory"]').click()
                await expect(page.locator("#mem-proactive-bar")).to_be_visible()
                await expect(page.locator("#mem-proactive-bar")).to_contain_text("今日主动")
                tab_labels = [
                    t.strip()
                    for t in await page.locator("#mem-book-tabs [data-mem-kind]").all_inner_texts()
                ]
                assert tab_labels == ["会话备忘", "角色记忆", "自身近况", "心跳说明"]

                await page.locator("#mem-tab-role-memory").click()
                await expect(page.locator("#mem-role-select")).to_be_visible()
                await page.locator("#mem-role-select").select_option("digest")
                await page.locator("#mem-editor").fill("# 简报独立记忆\n- 只收一手开发者日志。")
                await page.locator("#mem-save").click()
                await expect(page.locator("#mem-feedback")).to_contain_text("手账已保存")
                assert "只收一手开发者日志" in plugin.memory.read("role_memory", target="digest")["content"]

                await expect(page.locator("#mem-learning-list")).to_contain_text("补入技能：指南编写")
                await page.locator('#mem-learning-list [data-undo-skill="guide_writer"]').first.click()
                await expect(page.locator("#mem-feedback")).to_contain_text("已撤回技能")
                assert "每条规范前附适用前提" not in plugin.memory.read("skill", target="guide_writer")["content"]

                assert not errors
                assert get_peak() <= 2
            finally:
                await browser.close()
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_conversational_import_conflict_later_controls_and_scout_routing(tmp_path):
    """验证对话式导入（重名先确认/另存副本）、maisya_later 暂停恢复试跑、以及 scout 分工提示。"""
    grp = "qq:group:100246809"
    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        wakes = []

        async def capability(name, **kwargs):
            if name == "maisaka.proactive.trigger":
                wakes.append(kwargs)
                return {"success": True}
            return {"success": True}

        plugin.ctx.call_capability = capability
        plugin.messages.upsert_route("sid_g_100246809", platform="qq", chat_type="group", group_id="100246809")
        cur_set = await plugin.manage_read("settings")
        await plugin.manage_write("settings_update", json.dumps({"settings": {"enabled_chats": [grp]}, "expected_version": cur_set["version"]}))
        ev = make_event(text="导入包与排期", user="10001", platform="qq", group=True)
        ev["message_info"]["group_info"] = {"group_id": "100246809"}
        ev["message_id"] = "m-conv-import"
        ev["session_id"] = grp
        plugin.messages.record(Message.from_event(ev))

        exported = await execute_shape(
            plugin,
            action="profile",
            op="export",
            target="scout",
            current_chat_id=grp,
            current_platform="qq",
            source_msg_id="m-conv-import",
        )
        pkg_path = exported["export_path"]

        conflict_res = await execute_shape(
            plugin,
            action="profile",
            op="import",
            patch={"path": pkg_path},
            current_chat_id=grp,
            current_platform="qq",
            source_msg_id="m-conv-import",
        )
        assert conflict_res["imported"] is False
        assert conflict_res["needs_decision"] is True
        assert conflict_res["conflict_profile_id"] == "scout"

        fork_res = await execute_shape(
            plugin,
            action="profile",
            op="import",
            target="scout_fork",
            patch={"path": pkg_path},
            current_chat_id=grp,
            current_platform="qq",
            source_msg_id="m-conv-import",
        )
        assert fork_res["imported"] is True
        assert (plugin.profiles.path / "scout_fork" / "profile.toml").is_file()

        sched = await plugin.later_tool(
            action="schedule",
            mode="recurring",
            every_seconds=600,
            reason="每周汇总",
            source_msg_id="m-conv-import",
            chat_id=grp,
            platform="qq",
        )
        assert sched["success"] is True
        iid = sched["intent_id"]

        paused = await plugin.later_tool(
            action="pause",
            intent_id=iid,
            source_msg_id="m-conv-import",
            chat_id=grp,
            platform="qq",
        )
        assert paused["success"] is True and paused["status"] == "paused"

        listed = await plugin.later_tool(
            action="list",
            source_msg_id="m-conv-import",
            chat_id=grp,
            platform="qq",
        )
        assert any(it["intent_id"] == iid and it["status"] == "paused" for it in listed["intents"])

        resumed = await plugin.later_tool(
            action="resume",
            intent_id=iid,
            source_msg_id="m-conv-import",
            chat_id=grp,
            platform="qq",
        )
        assert resumed["success"] is True and resumed["status"] == "pending"

        ran = await plugin.later_tool(
            action="run_now",
            intent_id=iid,
            source_msg_id="m-conv-import",
            chat_id=grp,
            platform="qq",
        )
        assert ran["success"] is True
        assert ran["intent"]["status"] == "done"
        assert wakes, f"run_now 应立即触发唤醒: {ran}"
        after_run = await plugin.later_tool(
            action="list",
            source_msg_id="m-conv-import",
            chat_id=grp,
            platform="qq",
        )
        pending_ids = [it["intent_id"] for it in after_run["intents"] if it["status"] == "pending"]
        assert len(pending_ids) == 1 and pending_ids[0] != iid, after_run["intents"]

        tools = [
            {"type": "function", "function": {"name": name, "description": name}}
            for name in ("reply", "maisya_agent", "maisya_later", "maisya_recall")
        ]
        plan = await plugin.before_planner(session_id=grp, items=[], tool_definitions=tools)
        injected_txt = "\n".join(
            part.get("text", "")
            for item in plan["modified_kwargs"]["items"]
            for part in (item.get("parts") or ())
        )
        # 路由不再写死在提示词里（docs/09），由档案名册的描述承担
        import html as _html
        import re as _re

        roster_match = _re.search(r"<maisya_profiles>(.*?)</maisya_profiles>", injected_txt, flags=_re.S)
        assert roster_match is not None
        roster = {p["id"]: p for p in json.loads(_html.unescape(roster_match.group(1)))}
        assert roster["scout"]["description"] and roster["default"]["description"]
        assert "<maisya_planner_manual>" in injected_txt
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_gpt_review_security_and_transactional_fixes(tmp_path):
    """回归验证 GPT 审核发现的 4 项问题：导出任务隔离、MCP 句柄防碰撞、配置失败不污染凭据、模板导入失败回滚技能。"""
    import shutil

    grp = "qq:group:100246809"
    plugin = await loaded(tmp_path, ["qq:10001"])
    try:
        plugin.messages.upsert_route("sid_g_100246809", platform="qq", chat_type="group", group_id="100246809")
        cur_set = await plugin.manage_read("settings")
        await plugin.manage_write(
            "settings_update",
            json.dumps({"settings": {"enabled_chats": [grp, "qq:group:999888", "priv_admin_secret"]}, "expected_version": cur_set["version"]}),
        )

        # [P1 #1] 普通用户导出公共档案 default，不得夹带管理员任务或其他会话任务提示词
        now_ts = plugin.intents.clock()
        plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target="chat:priv_admin_secret",
            due_at=now_ts + 1200,
            reason="管理员私聊机密日报",
            payload={"profile": "default", "prompt": "SECRET_ADMIN_DM_PROMPT", "scheduled_admin": True},
            bypass_limits=True,
            mode="schedule",
        )
        plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target="chat:qq:group:999888",
            due_at=now_ts + 1200,
            reason="外群任务",
            payload={"profile": "default", "prompt": "OTHER_GROUP_PROMPT", "scheduled_admin": False},
            bypass_limits=True,
            mode="schedule",
        )
        plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{grp}",
            due_at=now_ts + 1200,
            reason="本群管理员保密工作流",
            payload={"profile": "default", "prompt": "SAME_GROUP_ADMIN_PROMPT", "scheduled_admin": True},
            bypass_limits=True,
            mode="schedule",
        )
        plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=f"chat:{grp}",
            due_at=now_ts + 1200,
            reason="公开群常规提醒",
            payload={"profile": "default", "prompt": "公开群常规提醒内容", "scheduled_admin": False},
            bypass_limits=True,
            mode="schedule",
        )
        ev_user = make_event(text="导出默认档案模板", user="20002", platform="qq", group=True)
        ev_user["message_info"]["group_info"] = {"group_id": "100246809"}
        ev_user["message_id"] = "m-user-export"
        ev_user["session_id"] = grp
        plugin.messages.record(Message.from_event(ev_user))

        default_prof = plugin.profiles.get("default")
        user_th = plugin.threads.create(
            default_prof,
            "普通成员导出",
            chat_id=grp,
            platform="qq",
            user_id="qq:20002",
        )
        export_res = await plugin.runtime.call(
            "package_export",
            {"profile_id": "default", "kind": "template"},
            user_th,
        )
        assert export_res["ok"] is True
        work = await plugin.runtime._work(user_th)
        exported_zip = (work.path / export_res["path"]).read_bytes()
        with zipfile.ZipFile(io.BytesIO(exported_zip), "r") as zf:
            all_txt = "\n".join(zf.read(n).decode("utf-8", errors="ignore") for n in zf.namelist())
        assert "SECRET_ADMIN_DM_PROMPT" not in all_txt
        assert "OTHER_GROUP_PROMPT" not in all_txt
        assert "SAME_GROUP_ADMIN_PROMPT" not in all_txt
        assert "公开群常规提醒内容" in all_txt

        # [P1 #2] 长前缀相同 MCP 名称（>24字符）与前缀包含名称（github vs github_enterprise）不碰撞、不误删
        svc_items = [
            ("shared_prefix_twentyfour_alpha", "SECRET_ALPHA_01"),
            ("shared_prefix_twentyfour_bravo", "SECRET_BRAVO_02"),
            ("github", "GH_PERSONAL_03"),
            ("github_enterprise", "GH_ENT_SECRET_OK"),
        ]
        ver = (await plugin.manage_read("services"))["version"]
        for svc_name, secret_val in svc_items:
            res = await plugin.manage_write(
                "upsert_service_item",
                json.dumps(
                    {
                        "kind": "mcp",
                        "name": svc_name,
                        "item": {
                            "command": [sys.executable, "-c", "print('ok')"],
                            "env": {"API_KEY": secret_val},
                            "tools": ["*"],
                        },
                        "expected_version": ver,
                    }
                ),
            )
            assert res["success"], res
            ver = res["version"]

        svc_cfg = plugin.runtime.services.load()["servers"]
        handles = {n: svc_cfg[n]["credentials"]["API_KEY"] for n, _ in svc_items}
        assert len(set(handles.values())) == 4
        for svc_name, secret_val in svc_items:
            assert plugin.vault.for_execution(handles[svc_name]) == secret_val

        for del_name in ("shared_prefix_twentyfour_alpha", "github"):
            del_res = await plugin.manage_write(
                "delete_service_item",
                json.dumps({"kind": "mcp", "name": del_name, "expected_version": ver}),
            )
            assert del_res["success"], del_res
            ver = del_res["version"]

        assert plugin.vault.for_execution(handles["shared_prefix_twentyfour_bravo"]) == "SECRET_BRAVO_02"
        assert plugin.vault.for_execution(handles["github_enterprise"]) == "GH_ENT_SECRET_OK"

        # [P2 #3] 服务配置校验失败时，不覆盖已有凭据（控制台入口与对话塑形入口均保护）
        bad_res = await plugin.manage_write(
            "upsert_service_item",
            json.dumps(
                {
                    "kind": "mcp",
                    "name": "github_enterprise",
                    "item": {
                        "command": [sys.executable, "-c", "print('ok')"],
                        "env": {"API_KEY": "NEW_BROKEN_SECRET"},
                        "tools": ["*"],
                        "invalid_extra_option": True,
                    },
                    "expected_version": ver,
                }
            ),
        )
        assert bad_res["success"] is False
        assert plugin.vault.for_execution(handles["github_enterprise"]) == "GH_ENT_SECRET_OK"

        ev_adm = make_event(text="改配置", user="10001", platform="qq", group=True)
        ev_adm["message_info"]["group_info"] = {"group_id": "100246809"}
        ev_adm["message_id"] = "m-adm-svc-fail"
        ev_adm["session_id"] = grp
        plugin.messages.record(Message.from_event(ev_adm))
        with pytest.raises(ValueError):
            await execute_shape(
                plugin,
                action="services",
                op="patch",
                target="github_enterprise",
                patch={
                    "command": [sys.executable, "-c", "print('ok')"],
                    "env": {"API_KEY": "SHAPE_BROKEN_SECRET"},
                    "invalid_extra_option": True,
                },
                current_chat_id=grp,
                current_platform="qq",
                source_msg_id="m-adm-svc-fail",
            )
        assert plugin.vault.for_execution(handles["github_enterprise"]) == "GH_ENT_SECRET_OK"

        # [P2 #4] 模板导入遭遇重名 overwrite=False 时先拦截，不把包内新技能写入全局技能库
        plugin.memory.create_skill(
            "temp_pkg_only_skill",
            title="临时包技能",
            description="仅用于导入失败回滚验证",
            allow_custom=True,
        )
        plugin.profiles.create("tpl_src", instructions="临时源角色", skills=["temp_pkg_only_skill"])
        tpl_pkg = export_package(plugin, "tpl_src", kind="template")
        shutil.rmtree(plugin.profiles.path / "tpl_src")
        shutil.rmtree(plugin.data_root / "abilities" / "skills" / "temp_pkg_only_skill")
        asset_id = "skill:temp_pkg_only_skill:SKILL.md"
        with plugin.memory.store.connect() as db, db:
            db.execute("DELETE FROM memory_versions WHERE kind='skill' AND target=?", ("temp_pkg_only_skill",))
            db.execute("DELETE FROM memory_search WHERE asset=?", (asset_id,))
        assert "temp_pkg_only_skill" not in plugin.memory.skills()

        with pytest.raises(ValueError, match="已存在"):
            import_package_bundle(
                plugin,
                raw_bytes=tpl_pkg["zip_bytes"],
                target_profile_id="scout",
                overwrite=False,
            )
        assert "temp_pkg_only_skill" not in plugin.memory.skills()
        assert not (plugin.data_root / "abilities" / "skills" / "temp_pkg_only_skill").exists()

        # 中途写入失败（如排期写入异常）时也完整回滚包内新技能与目标角色目录
        with patch("maisya.packages._apply_imported_schedules", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                import_package_bundle(
                    plugin,
                    raw_bytes=tpl_pkg["zip_bytes"],
                    target_profile_id="tpl_rollback_test",
                    overwrite=False,
                )
        assert "temp_pkg_only_skill" not in plugin.memory.skills()
        assert not (plugin.data_root / "abilities" / "skills" / "temp_pkg_only_skill").exists()
        assert not (plugin.profiles.path / "tpl_rollback_test").exists()
    finally:
        await plugin.on_unload()
