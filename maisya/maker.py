"""在原有线程内组合只读工具、工作区、沙箱与受权能力。"""

import base64
import hashlib
import inspect
import json
import os
import re
import shlex
import sqlite3
from dataclasses import replace

from .abilities import Profile
from .sandbox import Sandbox
from .scout import Scout, call_capability, checked, safe_url, validate_arguments
from .trust import parse_stream_spec
from .workspaces import (
    Workspaces,
    completed_thread,
    regular_open,
    write_workspace_bytes,
)

_PLAYWRIGHT_ERROR_MARKERS = (
    "playwright",
    "chromium",
    "browser",
    "executable doesn't exist",
)

_ACCESS_RANK = {"everyone": 0, "listed": 1, "admins": 2}
_ENVIRONMENT_RANK = {"sandbox": 0, "readonly": 1}
_NETWORK_RANK = {"public": 0, "restricted": 1, "none": 2}


def _stricter_choice(first: str, second: str, ranks: dict[str, int], *, default_rank: int) -> str:
    return first if ranks.get(first, default_rank) >= ranks.get(second, default_rank) else second


def _tighten_allowlist(snapshot_items: tuple[str, ...], live_items: tuple[str, ...]) -> tuple[str, ...]:
    """合并快照与实时白名单时只做单调收窄；非空交集为空则保留永不匹配的哨兵。"""
    if snapshot_items and live_items:
        live_set = set(live_items)
        narrowed = tuple(item for item in snapshot_items if item in live_set)
        return narrowed if narrowed else ("__disjoint__",)
    return live_items or snapshot_items


def _step_service_context(context: dict) -> dict:
    """为服务/跨插件 API 调用生成按 step_index 隔离的 _tool_call_id 浅副本。"""
    step_idx = context.get("_step_index")
    if step_idx is None:
        return context
    raw_call_id = str(context.get("_tool_call_id") or "")
    return {**context, "_tool_call_id": f"step:{int(step_idx)}:{raw_call_id}"}


def _playwright_render_hint(message: str) -> str:
    """若 html2png 报错涉及缺失 Playwright/Chromium 内核，附加安装与降级交付提示。"""
    lowered = message.lower()
    if any(marker in lowered for marker in _PLAYWRIGHT_ERROR_MARKERS):
        return (
            f"{message}（若宿主未安装浏览器内核，可在宿主 Python 环境执行 "
            "playwright install chromium，或先改用纯文字/网页文件交付）"
        )
    return ""


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

    def resolve_profile(self, profile: Profile) -> Profile:
        """统一合成快照与实时角色档案：刷新运行参数且只允许安全边界单调收窄。"""
        profiles = getattr(self.memory, "profiles", None) if self.memory else None
        if profiles is None:
            return profile
        prof_path = getattr(profiles, "path", None)
        if prof_path is not None and not (prof_path / profile.id / "profile.toml").is_file():
            return profile
        live = profiles.get(profile.id)
        if live is None:
            return profile
        return replace(
            profile,
            enabled=bool(profile.enabled and live.enabled),
            bound=bool(profile.bound and live.bound),
            access=_stricter_choice(profile.access, live.access, _ACCESS_RANK, default_rank=2),
            environment=_stricter_choice(
                profile.environment, live.environment, _ENVIRONMENT_RANK, default_rank=1
            ),
            network=_stricter_choice(profile.network, live.network, _NETWORK_RANK, default_rank=2),
            allow_users=_tighten_allowlist(tuple(profile.allow_users), tuple(live.allow_users)),
            allow_chats=_tighten_allowlist(tuple(profile.allow_chats), tuple(live.allow_chats)),
            instructions=live.instructions,
            model=live.model,
            model_name=live.model_name,
            fallback_models=live.fallback_models,
            task=live.task,
            max_tokens=live.max_tokens,
            context_tokens=live.context_tokens,
            temperature=live.temperature,
            max_steps=live.max_steps,
            web_provider=live.web_provider,
            sandbox_targets=live.sandbox_targets,
            skills=live.skills,
            mcp=live.mcp,
            apis=live.apis,
            secrets=live.secrets,
            template=live.template,
            fish_seed=live.fish_seed,
        )

    def begin_turn(self, thread_id: str) -> None:
        """在新一轮线程执行开始时清理上一轮残留的附件与重试计数。"""
        self.attachments.pop(thread_id, None)
        self.attachment_attempts.pop(thread_id, None)

    def effective_mode(self, profile):
        if profile.environment != "sandbox":
            return "readonly"
        if self.sandbox.ready:
            return "sandbox"
        return "sandbox" if profile.requires_container else "light_workspace"

    def available(self, profile):
        if not profile.enabled:
            raise PermissionError("角色已停用")
        live = None
        if (
            self.memory
            and (self.memory.profiles.path / profile.id / "profile.toml").is_file()
        ):
            live = self.memory.profiles.get(profile.id)
            if not live.enabled:
                raise PermissionError("角色已停用")
        if not profile.bound or (live is not None and not live.bound):
            raise PermissionError("角色的服务或凭据尚未绑定")
        needs_container = (
            profile.environment == "sandbox" and profile.requires_container
        ) or (
            live is not None
            and live.environment == "sandbox"
            and live.requires_container
        )
        if needs_container and not self.sandbox.ready:
            raise RuntimeError(self.sandbox.error or "沙箱尚未就绪")
        req_secrets = set(profile.secrets) | (set(live.secrets) if live else set())
        req_mcp = set(profile.mcp) | (set(live.mcp) if live else set())
        req_apis = set(profile.apis) | (set(live.apis) if live else set())
        req_templates = {
            t for t in (profile.template, live.template if live else "") if t
        }
        if (
            req_secrets or req_templates or req_mcp or req_apis
        ) and self.services is None:
            raise RuntimeError("能力服务尚未登记")
        if req_mcp or req_templates or req_apis:
            configuration = self.services.load()
            if (
                any(
                    name not in configuration.get("servers", {}) for name in req_mcp
                )
                or any(
                    name not in configuration.get("apis", {}) for name in req_apis
                )
                or any(
                    tpl not in configuration.get("templates", {})
                    for tpl in req_templates
                )
            ):
                raise PermissionError("角色引用的服务、模板或只读 API 尚未登记")

    async def step(self, profile, history, *, max_steps=None):
        profile = await completed_thread(self.resolve_profile, profile)
        await completed_thread(self.available, profile)
        if self.memory:
            methods = await completed_thread(self.memory.methods, profile)
            profile = replace(
                profile,
                instructions=profile.instructions
                + "\n<methods>"
                + methods
                + "</methods>",
            )
        if profile.environment == "sandbox" and not self.sandbox.ready:
            profile = replace(
                profile,
                tools=tuple(
                    name
                    for name in profile.tools
                    if name not in {"sandbox_exec", "credential_call"}
                ),
                instructions=profile.instructions
                + "\n<runtime_mode>当前容器沙箱未就绪，自动运行于无容器轻量工作区模式：可使用 workspace_file、html_render、result_attach 及已授权的只读/联网工具完成整理、排版与出图；sandbox_exec 暂不可用（开启容器沙箱后自动恢复）。</runtime_mode>",
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
        profile = self.resolve_profile(Profile(**context["profile_snapshot"]))
        self.available(profile)
        if self.permitted and not self.permitted(context):
            raise PermissionError("当前真实来源或会话不在档案范围内")
        if profile.access == "admins" and not self.authorized(context):
            raise PermissionError("继续受限线程需要当前真实管理员授权")
        return (
            self.memory.context(
                context["chat_id"] if context["owner"] == "chat" else ""
            )
            if self.memory
            else None
        )

    def _sync_skill_companions(self, row, work):
        if self.memory is None:
            return
        try:
            live = self.memory.profiles.get(row["profile"])
            skills = live.skills
        except (ValueError, OSError):
            skills = tuple((row.get("profile_snapshot") or {}).get("skills") or ())
        for skill_id in skills:
            try:
                companions = self.memory.read_skill_companions(
                    skill_id, profile_id=str(row.get("profile") or "")
                )
            except (ValueError, OSError):
                continue
            for rel_sub, raw_data in companions:
                rel = f"in/skills/{skill_id}/{rel_sub}"
                write_workspace_bytes(work.path, rel, raw_data, overwrite=True)

    async def _work(self, row):
        thread_id = row["thread_id"]
        if thread_id not in self.sessions:

            def enter():
                manager = self.files.activity(row)
                work = manager.__enter__()
                self._sync_skill_companions(row, work)
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
        resolved_profile = await completed_thread(
            self.resolve_profile, Profile(**row["profile_snapshot"])
        )
        if not resolved_profile.uses_workspace:
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
                filename = (
                    re.sub(
                        r"[^\w.-]", "_", str(data.get("name") or f"input-{index}.bin")
                    )[:80].rstrip(". ")
                    or f"input-{index}.bin"
                )
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

                    def _read_cached_ref():
                        with regular_open(work.path, "cache/" + cache_name) as source:
                            return source.read(6 * 1024**2 + 1)

                    content = await completed_thread(_read_cached_ref)
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
        allow_any_chat = bool(
            (row.get("grant") or {}).get("scheduled_admin")
            or (row.get("grant") or {}).get("admin_direct")
        )
        target_arena = (
            await completed_thread(
                self._chat_arena, row["chat_id"], row.get("platform", "")
            )
            if row.get("owner") == "chat"
            else "self"
        )
        admin_checked: bool | None = None
        for previous_id in previous_threads:
            previous = await completed_thread(
                self.store.get,
                previous_id,
                row["chat_id"],
                allow_self=True,
                allow_any_chat=allow_any_chat,
            )
            if target_arena == "group" and previous.get("owner") == "chat":
                prev_arena = await completed_thread(
                    self._chat_arena,
                    previous["chat_id"],
                    previous.get("platform", ""),
                )
                if prev_arena == "private":
                    raise PermissionError("私聊线程结果默认不可直接注入群聊线程")
            prev_profiles = [Profile(**previous["profile_snapshot"])]
            if self.memory:
                try:
                    live_prev = await completed_thread(
                        self.memory.profiles.get, previous["profile"]
                    )
                    prev_profiles.append(live_prev)
                except (ValueError, OSError):
                    pass
            if any(prof.access == "admins" for prof in prev_profiles):
                if admin_checked is None:
                    admin_checked = bool(await completed_thread(self.authorized, row))
                if not admin_checked:
                    raise PermissionError("引用受限线程需要当前管理员授权")
            for prof in prev_profiles:
                if prof.access == "listed":
                    if admin_checked is None:
                        admin_checked = bool(await completed_thread(self.authorized, row))
                    if not await completed_thread(
                        self._listed_profile_permits,
                        prof,
                        row,
                        is_admin=admin_checked,
                    ):
                        raise PermissionError("引用受限线程不在当前会话或来源白名单内")
            prev_res = self.redact(previous.get("result") or {})
            prev_text = str(prev_res.get("text") or "").strip()
            prev_links = prev_res.get("links") or []
            if prev_text or prev_links:
                lines = [
                    f"# 上游线程 {previous_id} ({previous.get('profile', '')}) 结论",
                    "",
                ]
                if prev_text:
                    lines.append(prev_text)
                if prev_links:
                    lines.extend(["", "## 参考链接"])
                    for link_item in prev_links:
                        url_val = (
                            link_item.get("url", "")
                            if isinstance(link_item, dict)
                            else str(link_item)
                        )
                        if url_val:
                            lines.append(f"- {url_val}")
                summary_bytes = ("\n".join(lines).strip() + "\n").encode("utf-8")
                if not self.vault.contains_secret(summary_bytes):
                    rel_summary = f"in/previous-{previous_id}-result.md"
                    await completed_thread(
                        self._import_bytes,
                        work,
                        rel_summary,
                        summary_bytes,
                        overwrite=True,
                    )
                    imported.append(
                        {
                            "path": rel_summary,
                            "thread_id": previous_id,
                            "kind": "result_text",
                            "bytes": len(summary_bytes),
                        }
                    )
            attached_signatures = set()
            for index, attachment in enumerate(
                previous["result"].get("attachments", [])
            ):
                path = await completed_thread(
                    self.files.resolve, attachment["handle"], previous["chat_id"]
                )
                content = await completed_thread(path.read_bytes)
                attached_signatures.add(
                    (
                        str(attachment.get("name") or ""),
                        hashlib.sha256(content).hexdigest(),
                    )
                )
                safe_att_name = (
                    re.sub(r"[^\w.-]", "_", attachment["name"])[:80].rstrip(". ")
                    or f"attachment-{index}.bin"
                )
                relative = f"in/previous-{previous_id}-{index}-{safe_att_name}"
                await completed_thread(
                    self._import_bytes, work, relative, content, overwrite=True
                )
                imported.append(
                    {"path": relative, "thread_id": previous_id, "bytes": len(content)}
                )
            same_workspace = bool(
                previous.get("workspace_id")
                and previous.get("workspace_id") == row.get("workspace_id")
            )
            if not same_workspace:
                out_artifacts = await completed_thread(
                    self.files.read_checkpoint_outputs, previous
                )
                for subpath, out_bytes in out_artifacts:
                    leaf_name = subpath.rsplit("/", 1)[-1]
                    if (
                        leaf_name,
                        hashlib.sha256(out_bytes).hexdigest(),
                    ) in attached_signatures:
                        continue
                    rel_out = f"in/previous-{previous_id}-out/{subpath}"
                    try:
                        await completed_thread(
                            self._import_bytes,
                            work,
                            rel_out,
                            out_bytes,
                            overwrite=True,
                        )
                    except ValueError:
                        continue
                    imported.append(
                        {
                            "path": rel_out,
                            "thread_id": previous_id,
                            "kind": "workspace_out",
                            "bytes": len(out_bytes),
                        }
                    )
        await completed_thread(work.commit)
        return imported

    def _chat_arena(self, chat_id: str, platform: str = "") -> str:
        cid = str(chat_id or "").strip()
        if not cid:
            return ""
        if cid == "self":
            return "self"
        parsed = parse_stream_spec(cid)
        if parsed is not None:
            return parsed[1]
        candidates = [cid]
        aliases_fn = getattr(self.store, "chat_aliases", None)
        if callable(aliases_fn):
            try:
                for alias in sorted(set(aliases_fn(cid, platform)) - {cid}):
                    if isinstance(alias, str) and alias:
                        parsed_alias = parse_stream_spec(alias)
                        if parsed_alias is not None:
                            return parsed_alias[1]
                        candidates.append(alias)
            except (TypeError, ValueError):
                pass
        with self.store.connect() as db:
            has_routes = (
                db.execute(
                    'SELECT 1 FROM sqlite_master WHERE type="table" AND name="session_routes"'
                ).fetchone()
                is not None
            )
            if has_routes:
                placeholders = ",".join("?" for _ in candidates)
                rows = db.execute(
                    f"SELECT chat_type, group_id, user_id FROM session_routes WHERE session_id IN ({placeholders})",
                    tuple(candidates),
                ).fetchall()
                for route in rows:
                    ctype = str(route["chat_type"] or "").strip().lower()
                    if ctype in {"group", "private"}:
                        return ctype
                    if str(route["group_id"] or "").strip():
                        return "group"
                    if str(route["user_id"] or "").strip():
                        return "private"
        return ""

    def _listed_profile_permits(
        self, profile_obj: Profile, row: dict, *, is_admin: bool = False
    ) -> bool:
        chat_id = str(row.get("chat_id") or "")
        platform = str(row.get("platform") or "")
        plat = platform.strip().lower()
        chat_candidates = [chat_id] if chat_id else [""]
        aliases_fn = getattr(self.store, "chat_aliases", None)
        if chat_id and callable(aliases_fn):
            try:
                for alias in sorted(set(aliases_fn(chat_id, platform)) - {chat_id}):
                    if isinstance(alias, str) and alias:
                        chat_candidates.append(alias)
            except (TypeError, ValueError):
                pass
        raw_uid = str(row.get("user_id") or "")
        user_candidates = [raw_uid]
        if plat and raw_uid.startswith(f"{plat}:private:"):
            user_candidates.append(raw_uid.split(":", 2)[2])
        elif plat and raw_uid.startswith(f"{plat}:"):
            user_candidates.append(raw_uid.split(":", 1)[1])
        return any(
            profile_obj.permits(cid, platform, user_id=cand_uid, is_admin=is_admin)
            for cid in chat_candidates
            for cand_uid in user_candidates
        )

    def _import_bytes(self, work, relative, content, *, overwrite=False):
        existing_size = 0
        existed = False
        try:
            with regular_open(work.path, relative) as previous:
                existed = True
                old_bytes = previous.read(6 * 1024**2 + 1)
                if old_bytes == content:
                    return
                existing_size = len(old_bytes)
            if not overwrite:
                raise ValueError("引用附件内容已变化，请使用新的消息引用")
        except FileNotFoundError:
            pass
        files, used = self.files.inventory(work.path)
        if (
            not content
            or len(content) > 6 * 1024**2
            or used - existing_size + len(content) > self.files.max_bytes
            or (not existed and len(files) >= self.files.max_files)
        ):
            raise ValueError("引用文件为空或超过配额")
        write_workspace_bytes(work.path, relative, content, overwrite=overwrite)

    async def call(self, name, arguments, context):
        validate_arguments(name, arguments)
        profile = await completed_thread(
            self.resolve_profile, Profile(**context["profile_snapshot"])
        )
        await completed_thread(self.available, profile)
        if self.permitted and not await completed_thread(self.permitted, context):
            raise PermissionError("当前真实来源或会话不在档案范围内")
        if profile.access == "admins" and not await completed_thread(
            self.authorized, context
        ):
            raise PermissionError("本次活动的管理员授权已失效")
        if name in {
            "workspace_file",
            "html_render",
            "result_attach",
            "digest_feed",
            "render_report",
        }:
            if not profile.uses_workspace:
                raise PermissionError("当前角色未启用工作区工具")
            work = await self._work(context)
            if name == "digest_feed":
                from .digest import run_digest_tool

                params = inspect.signature(run_digest_tool).parameters
                accepts_kwargs = any(
                    param.kind == inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
                extra_kwargs = {}
                if accepts_kwargs or "web" in params:
                    extra_kwargs["web"] = self.web
                if accepts_kwargs or "context" in params:
                    extra_kwargs["context"] = context
                if inspect.iscoroutinefunction(run_digest_tool):
                    return await run_digest_tool(work, arguments, **extra_kwargs)
                result = await completed_thread(
                    run_digest_tool, work, arguments, **extra_kwargs
                )
                if inspect.isawaitable(result):
                    result = await result
                return result
            if name == "render_report":
                from .report import render_report_artifact

                return await completed_thread(render_report_artifact, work, arguments)
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
                try:
                    raw_rendered = await call_capability(
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
                    )
                    if (
                        isinstance(raw_rendered, dict)
                        and raw_rendered.get("success") is False
                        and _playwright_render_hint(
                            str(raw_rendered.get("error") or "")
                        )
                    ):
                        raise RuntimeError(str(raw_rendered.get("error") or ""))
                    rendered = checked(raw_rendered, "result")
                except RuntimeError as error:
                    hinted = _playwright_render_hint(str(error))
                    if hinted:
                        raise RuntimeError(hinted) from error
                    raise
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
            except (ValueError, OSError) as exc:
                next_attempts = attempts + 1
                self.attachment_attempts[context["thread_id"]] = next_attempts
                if next_attempts >= 2:
                    return {
                        "error": f"本轮附件已尝试修正一次（{exc}），请直接返回可用文字与具体附件错误"
                    }
                raise
            existing = list(self.attachments.get(context["thread_id"], ()))
            seen_keys = {
                (item.get("handle"), item.get("path"))
                for item in existing
                if isinstance(item, dict)
            }
            for item in attachments:
                key = (
                    (item.get("handle"), item.get("path"))
                    if isinstance(item, dict)
                    else item
                )
                if key not in seen_keys:
                    seen_keys.add(key)
                    existing.append(item)
            self.attachments[context["thread_id"]] = existing
            return {"attachments": attachments}
        if name == "sandbox_exec":
            if profile.environment != "sandbox":
                raise PermissionError("当前角色未启用容器沙箱执行")
            if not self.sandbox.ready:
                raise RuntimeError(
                    self.sandbox.error
                    or "当前处于无容器轻量工作区模式，sandbox_exec 暂不可用；请改用 workspace_file、html_render、result_attach，或在设置中启用容器沙箱"
                )
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
            host_mappings = {}
            if profile.network == "restricted" and profile.sandbox_targets:
                from ipaddress import ip_address
                from .services import validate_endpoint_targets

                def _resolve_targets():
                    resolved_endpoints = []
                    resolved_hosts = {}
                    for raw_target in profile.sandbox_targets:
                        raw_text = str(raw_target or "").strip()
                        pairs = validate_endpoint_targets([raw_text])
                        resolved_endpoints.extend(pairs)
                        host_part = raw_text
                        if raw_text.startswith("[") and "]:" in raw_text:
                            host_part = raw_text[1 : raw_text.index("]:")]
                        elif ":" in raw_text:
                            host_part = raw_text.rsplit(":", 1)[0]
                        host_part = host_part.strip().lower()
                        if host_part and pairs:
                            try:
                                ip_address(host_part)
                            except ValueError:
                                resolved_hosts[host_part] = str(pairs[0][0])
                    return tuple(dict.fromkeys(resolved_endpoints)), resolved_hosts

                endpoints, host_mappings = await completed_thread(_resolve_targets)
            exec_kwargs = {
                "call_id": context["_tool_call_id"],
                "step_index": context.get("_step_index"),
                "timeout": arguments.get("timeout"),
                "mode": profile.network,
            }
            if endpoints:
                exec_kwargs["targets"] = endpoints
            if host_mappings:
                exec_kwargs["host_mappings"] = host_mappings
            if profile.secrets:
                async with self.services.mount_secrets(
                    self, profile.secrets, work=work
                ) as secrets:
                    return self.redact(
                        await self.sandbox.execute(
                            context,
                            work,
                            arguments["command"],
                            secrets=secrets,
                            **exec_kwargs,
                        )
                    )
            return self.redact(
                await self.sandbox.execute(
                    context,
                    work,
                    arguments["command"],
                    **exec_kwargs,
                )
            )
        service_ctx = _step_service_context(context)
        if name == "mcp_call":
            if not self.services:
                raise RuntimeError("MCP 服务尚未登记")
            return await self.services.call(profile, arguments, service_ctx, self)
        if name == "plugin_api":
            if not self.services:
                raise RuntimeError("跨插件 API 尚未登记")
            return await self.services.call_api(profile, arguments, service_ctx, self)
        if name == "credential_call":
            if profile.environment == "sandbox" and not self.sandbox.ready:
                raise RuntimeError(self.sandbox.error or "固定凭据容器执行需要启用容器沙箱")
            if not self.services or not await completed_thread(
                self.authorized, context
            ):
                raise PermissionError("固定凭据模板需要本次真实管理员授权")
            return await self.services.credential(
                profile, arguments["arguments"], service_ctx, self
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
