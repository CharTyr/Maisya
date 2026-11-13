"""在原有线程内组合只读工具、工作区、沙箱与受权能力。"""

import base64
import hashlib
import json
import os
import re
import shlex
import sqlite3
from dataclasses import replace

from .abilities import Profile
from .sandbox import Sandbox
from .scout import Scout, call_capability, checked, safe_url, validate_arguments
from .workspaces import Workspaces, completed_thread, parent_fd, regular_open


class AgentRuntime(Scout):
    def __init__(
        self, host, root, store, settings, vault, *, authorized, web_provider="auto"
    ):
        super().__init__(host, vault.redact)
        self.store, self.vault, self.authorized = store, vault, authorized
        self.sandbox = Sandbox(
            settings, store, redact=vault.redact, authorized=authorized
        )
        self.files = Workspaces(
            root / "workspaces",
            store,
            volume=self.sandbox.volume if settings.enabled else None,
            contains_secret=vault.contains_secret,
        )
        self.sessions = {}
        self.attachments = {}
        self.attachment_attempts = {}
        self.memory = None
        self.learning = None
        self.permitted = None
        from .services import Services
        from .web import WebRouter

        self.services = Services(root / "abilities/services.json", vault)
        self.web = WebRouter(
            host,
            redact=vault.redact,
            provider=web_provider,
            services=self.services,
            runtime=self,
        )

    def available(self, profile):
        if not profile.enabled:
            raise PermissionError("角色已停用")
        if (
            self.memory
            and (self.memory.profiles.path / profile.id / "profile.toml").is_file()
        ):
            live = self.memory.profiles.get(profile.id)
            if not live.enabled:
                raise PermissionError("角色已停用")
        if not profile.bound:
            raise PermissionError("角色的服务或凭据尚未绑定")
        if profile.environment == "sandbox" and not self.sandbox.ready:
            raise RuntimeError(self.sandbox.error or "沙箱尚未就绪")
        if (
            profile.secrets or profile.template or profile.mcp or profile.apis
        ) and self.services is None:
            raise RuntimeError("能力服务尚未登记")
        if profile.mcp or profile.template or profile.apis:
            configuration = self.services.load()
            if (
                any(
                    name not in configuration.get("servers", {}) for name in profile.mcp
                )
                or any(
                    name not in configuration.get("apis", {}) for name in profile.apis
                )
                or profile.template
                and profile.template not in configuration.get("templates", {})
            ):
                raise PermissionError("角色引用的服务、模板或只读 API 尚未登记")

    async def step(self, profile, history, *, max_steps=None):
        self.available(profile)
        if self.memory:
            live = await completed_thread(self.memory.profiles.get, profile.id)
            profile = replace(
                profile,
                enabled=live.enabled,
                instructions=live.instructions,
                model=live.model,
                model_name=live.model_name,
                fallback_models=live.fallback_models,
                task=live.task,
                max_tokens=live.max_tokens,
                temperature=live.temperature,
                max_steps=live.max_steps,
                web_provider=live.web_provider,
                sandbox_targets=live.sandbox_targets,
                skills=live.skills,
                mcp=live.mcp,
                apis=live.apis,
                secrets=live.secrets,
                template=live.template,
            )
            methods = await completed_thread(self.memory.methods, profile)
            profile = replace(
                profile,
                instructions=profile.instructions
                + "\n<methods>"
                + methods
                + "</methods>",
            )
        if self.services and profile.mcp:
            descriptions = await completed_thread(self.services.describe, profile)
            profile = replace(
                profile,
                instructions=profile.instructions
                + "\n已授权的能力服务："
                + json.dumps(descriptions, ensure_ascii=False),
            )
        if self.services and profile.apis:
            api_descriptions = await completed_thread(
                self.services.describe_apis, profile
            )
            profile = replace(
                profile,
                instructions=profile.instructions
                + "\n已授权的跨插件 API："
                + json.dumps(api_descriptions, ensure_ascii=False),
            )
        return await super().step(profile, history)

    def before(self, context):
        profile = Profile(**context["profile_snapshot"])
        self.available(profile)
        if self.permitted and not self.permitted(context):
            raise PermissionError("当前真实来源或会话不在档案范围内")
        restricted = (
            profile.access == "admins"
            or self.memory
            and self.memory.profiles.get(profile.id).access == "admins"
        )
        if restricted and not self.authorized(context):
            raise PermissionError("继续受限线程需要当前真实管理员授权")
        return (
            self.memory.context(
                context["chat_id"] if context["owner"] == "chat" else ""
            )
            if self.memory
            else None
        )

    async def _work(self, row):
        thread_id = row["thread_id"]
        if thread_id not in self.sessions:

            def enter():
                manager = self.files.activity(row)
                work = manager.__enter__()
                self.sessions[thread_id] = manager, work
                return work

            await completed_thread(enter)
        return self.sessions[thread_id][1]

    async def prepare(self, row, metadata):
        references = metadata.get("references", [])
        previous_threads = metadata.get("from_threads", [])
        if not references and not previous_threads:
            return []
        if (
            not isinstance(references, list)
            or len(references) > 10
            or not all(
                isinstance(value, str) and len(value) <= 256 for value in references
            )
        ):
            raise ValueError("引用输入无效")
        if not isinstance(previous_threads, list) or len(previous_threads) > 5:
            raise ValueError("引用线程无效")
        if not Profile(**row["profile_snapshot"]).uses_workspace:
            return []
        if row["owner"] == "self":
            raise PermissionError("自身活动不自动导入会话引用输入")
        await completed_thread(self.before, row)
        work = await self._work(row)
        imported = []
        for reference_index, message_id in enumerate(references):
            raw = checked(
                await call_capability(
                    self.host,
                    "message.get_by_id",
                    timeout_ms=30000,
                    message_id=message_id,
                    chat_id=row["chat_id"],
                    include_binary_data=True,
                ),
                "message",
            )
            if not isinstance(raw, dict) or raw.get("session_id") != row["chat_id"]:
                raise PermissionError("引用输入不属于本会话")
            for index, part in enumerate(raw.get("raw_message", [])):
                if part.get("type") not in {"image", "emoji", "voice", "file"}:
                    continue
                data = part.get("data") if isinstance(part.get("data"), dict) else {}
                filename = re.sub(
                    r"[^\w.-]", "_", str(data.get("name") or f"input-{index}.bin")
                )[:80]
                reference_key = hashlib.sha256(message_id.encode()).hexdigest()[:12]
                relative = f"in/ref-{reference_key}-{index}-{filename}"
                binary = part.get("binary_data_base64") or data.get("base64")
                if binary:
                    if not isinstance(binary, str) or len(binary) > 8 * 1024**2:
                        raise ValueError("引用附件过大")
                    content = base64.b64decode(binary, validate=True)
                elif data.get("url"):
                    if not self.sandbox.ready:
                        raise RuntimeError("下载外部链接引用文件需要启用沙箱")
                    url = safe_url(data["url"])
                    cache_name = (
                        "reference-"
                        + hashlib.sha256(
                            (message_id + str(index)).encode()
                        ).hexdigest()[:16]
                    )
                    program = (
                        "import urllib.request\nurl="
                        + repr(url)
                        + "\npath="
                        + repr("/work/cache/" + cache_name)
                        + '\nwith urllib.request.urlopen(url,timeout=15) as response,open(path,"wb") as output:\n total=0\n while chunk:=response.read(65536):\n  total+=len(chunk)\n  if total>6*1024**2: raise ValueError("input too large")\n  output.write(chunk)\n'
                    )
                    result = await self.sandbox.execute(
                        row,
                        work,
                        "python -c " + shlex.quote(program),
                        call_id="reference-" + cache_name,
                        mode=row["profile_snapshot"].get("network", "public"),
                    )
                    if result.get("exit_code") != 0:
                        raise RuntimeError("引用文件下载失败")
                    with regular_open(work.path, "cache/" + cache_name) as source:
                        content = source.read(6 * 1024**2 + 1)
                elif data.get("file_ref"):
                    content = await self.services.input_file(
                        Profile(**row["profile_snapshot"]),
                        message_id,
                        data["file_ref"],
                        row,
                        self,
                    )
                else:
                    raise ValueError(
                        "引用文件只有平台元数据，需要可读取的原文件或下载地址"
                    )
                await completed_thread(self._import_bytes, work, relative, content)
                imported.append(
                    {"path": relative, "message_id": message_id, "bytes": len(content)}
                )
        for previous_id in previous_threads:
            previous = await completed_thread(
                self.store.get, previous_id, row["chat_id"], allow_self=True
            )
            if previous["profile_snapshot"][
                "access"
            ] == "admins" and not await completed_thread(self.authorized, row):
                raise PermissionError("引用受限线程需要当前管理员授权")
            for index, attachment in enumerate(
                previous["result"].get("attachments", [])
            ):
                path = await completed_thread(
                    self.files.resolve, attachment["handle"], previous["chat_id"]
                )
                content = await completed_thread(path.read_bytes)
                relative = (
                    f"in/previous-{previous_id}-{index}-"
                    + re.sub(r"[^\w.-]", "_", attachment["name"])[:80]
                )
                await completed_thread(self._import_bytes, work, relative, content)
                imported.append(
                    {"path": relative, "thread_id": previous_id, "bytes": len(content)}
                )
        await completed_thread(work.commit)
        return imported

    def _import_bytes(self, work, relative, content):
        try:
            with regular_open(work.path, relative) as previous:
                if previous.read(6 * 1024**2 + 1) == content:
                    return
            raise ValueError("引用附件内容已变化，请使用新的消息引用")
        except FileNotFoundError:
            pass
        files, used = self.files.inventory(work.path)
        if (
            not content
            or len(content) > 6 * 1024**2
            or used + len(content) > self.files.max_bytes
            or len(files) >= self.files.max_files
        ):
            raise ValueError("引用文件为空或超过配额")
        with parent_fd(work.path, relative, create=True) as (directory, name):
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)

    async def call(self, name, arguments, context):
        validate_arguments(name, arguments)
        profile = Profile(**context["profile_snapshot"])
        self.available(profile)
        if self.permitted and not await completed_thread(self.permitted, context):
            raise PermissionError("当前真实来源或会话不在档案范围内")
        live_restricted = (
            self.memory
            and (await completed_thread(self.memory.profiles.get, profile.id)).access
            == "admins"
        )
        if (
            profile.access == "admins" or live_restricted
        ) and not await completed_thread(self.authorized, context):
            raise PermissionError("本次活动的管理员授权已失效")
        if name in {"workspace_file", "html_render", "result_attach", "digest_feed"}:
            if not profile.uses_workspace:
                raise PermissionError("当前角色未启用工作区工具")
            work = await self._work(context)
            if name == "digest_feed":
                from .digest import run_digest_tool

                return await run_digest_tool(work, arguments)
            if name == "workspace_file":
                action = arguments["action"]
                if action == "list":
                    return await completed_thread(work.listing)
                if action == "read":
                    return await completed_thread(
                        work.read, arguments.get("path", ""), arguments.get("offset", 0)
                    )
                result = await completed_thread(
                    work.write, arguments.get("path", ""), arguments.get("content", "")
                )
                await completed_thread(work.commit)
                return result
            if name == "html_render":
                html_text = arguments.get("html") or ""
                if not html_text and arguments.get("html_path"):
                    def _read_html():
                        with regular_open(work.path, arguments["html_path"]) as src:
                            raw = src.read(524289)
                        if len(raw) > 524288:
                            raise ValueError("HTML 源文件超过 512KB 上限")
                        return raw.decode("utf-8", errors="replace")

                    html_text = await completed_thread(_read_html)
                if not isinstance(html_text, str) or not html_text.strip():
                    raise ValueError("html_render 需要提供非空 html 或 html_path")
                output_path = str(
                    arguments.get("output_path") or f"out/render-{os.urandom(6).hex()}.png"
                )
                if not output_path.startswith("out/"):
                    raise ValueError("渲染图片输出路径必须位于 out/ 目录下")
                viewport = {
                    "width": int(arguments.get("width", 960)),
                    "height": int(arguments.get("height", 640)),
                }
                rendered = checked(
                    await call_capability(
                        self.host,
                        "render.html2png",
                        timeout_ms=30000,
                        html=html_text,
                        selector=str(arguments.get("selector", "body")),
                        viewport=viewport,
                        device_scale_factor=float(
                            arguments.get("device_scale_factor", 2.0)
                        ),
                        full_page=bool(arguments.get("full_page", True)),
                        allow_network=False,
                    ),
                    "result",
                )
                image_b64 = rendered.get("image_base64")
                if not isinstance(image_b64, str) or len(image_b64) > 12 * 1024**2:
                    raise RuntimeError("宿主渲染未返回有效图片数据")
                png_bytes = base64.b64decode(image_b64, validate=True)
                write_res = await completed_thread(
                    work.write_bytes, output_path, png_bytes
                )
                await completed_thread(work.commit)
                return {
                    **write_res,
                    "mime_type": rendered.get("mime_type", "image/png"),
                    "width": rendered.get("width"),
                    "height": rendered.get("height"),
                }
            attempts = self.attachment_attempts.get(context["thread_id"], 0)
            if attempts >= 2:
                return {
                    "error": "本轮附件已尝试修正一次，请直接返回可用文字与具体附件错误"
                }
            try:
                attachments = await completed_thread(
                    self.files.attach, context, work, arguments["paths"]
                )
            except (ValueError, OSError):
                self.attachment_attempts[context["thread_id"]] = attempts + 1
                raise
            self.attachments[context["thread_id"]] = attachments
            return {"attachments": attachments}
        if name == "sandbox_exec":
            if profile.environment != "sandbox":
                raise PermissionError("当前角色未启用容器沙箱执行")
            if (
                profile.secrets
                or profile.network == "restricted"
                or profile.sandbox_targets
            ) and (
                profile.access != "admins"
                or not await completed_thread(self.authorized, context)
            ):
                raise PermissionError("带私有凭据或受限网络的沙箱执行仅限当前管理员授权")
            work = await self._work(context)
            endpoints = ()
            if profile.network == "restricted" and profile.sandbox_targets:
                from .services import validate_endpoint_targets

                endpoints = await completed_thread(
                    validate_endpoint_targets, profile.sandbox_targets
                )
            if profile.secrets:
                async with self.services.mount_secrets(
                    self, profile.secrets, work=work
                ) as secrets:
                    return self.redact(
                        await self.sandbox.execute(
                            context,
                            work,
                            arguments["command"],
                            call_id=context["_tool_call_id"],
                            timeout=arguments.get("timeout"),
                            mode=profile.network,
                            **({"targets": endpoints} if endpoints else {}),
                            secrets=secrets,
                        )
                    )
            return self.redact(
                await self.sandbox.execute(
                    context,
                    work,
                    arguments["command"],
                    call_id=context["_tool_call_id"],
                    timeout=arguments.get("timeout"),
                    mode=profile.network,
                    **({"targets": endpoints} if endpoints else {}),
                )
            )
        if name == "mcp_call":
            if not self.services:
                raise RuntimeError("MCP 服务尚未登记")
            return await self.services.call(profile, arguments, context, self)
        if name == "plugin_api":
            if not self.services:
                raise RuntimeError("跨插件 API 尚未登记")
            return await self.services.call_api(profile, arguments, context, self)
        if name == "credential_call":
            if not self.services or not await completed_thread(
                self.authorized, context
            ):
                raise PermissionError("固定凭据模板需要本次真实管理员授权")
            return await self.services.credential(
                profile, arguments["arguments"], context, self
            )
        return await super().call(name, arguments, context)

    def result(self, thread_id, result):
        attachments = self.attachments.get(thread_id)
        return {**result, **({"attachments": attachments} if attachments else {})}

    async def finished(self, row):
        if not self.memory:
            return
        try:
            await completed_thread(self.memory.experience, row)
            if self.learning is None:
                from .learning import Learning

                self.learning = Learning(self.host, self.memory)
            await self.learning.schedule(row)
        except (ValueError, RuntimeError, TypeError, OSError, sqlite3.Error):
            return

    async def end(self, thread_id):
        self.attachments.pop(thread_id, None)
        self.attachment_attempts.pop(thread_id, None)
        session = self.sessions.pop(thread_id, None)
        if session:
            manager, _work = session
            await completed_thread(manager.__exit__, None, None, None)

    async def stopped(self, thread_id):
        await self.services.close_thread(thread_id)
        if thread_id in self.sandbox.containers:
            raise RuntimeError(self.sandbox.error or "执行容器清理未确认")

    async def configure(self, settings):
        await self.sandbox.close()
        await completed_thread(self.files.release_quarantines)
        await self.services.release_quarantines()
        self.sandbox = Sandbox(
            settings, self.store, redact=self.vault.redact, authorized=self.authorized
        )
        await self.sandbox.probe()
        self.files.volume = self.sandbox.volume if settings.enabled else None

    async def close(self):
        if self.learning:
            await self.learning.close()
        await self.sandbox.close()
        await self.services.release_quarantines()
        if self.services:
            await self.services.close()
        for thread_id in list(self.sessions):
            await self.end(thread_id)
        await completed_thread(self.files.release_quarantines)
