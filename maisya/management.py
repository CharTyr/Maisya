"""宿主认证管理页的静态 API；正文与秘密走各自的执行边界。"""

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from .config import Settings
from .memory import MemoryLayers
from .scout import call_capability
from .share import links_in
from .services import (
    validate_api_entry,
    validate_endpoint_targets,
    validate_web_bindings,
)
from .vault import atomic_private_write
from .workspaces import completed_thread, regular_open


_TEXT_EXTS = {
    ".txt",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".csv",
    ".tsv",
    ".sh",
    ".sql",
    ".css",
    ".xml",
    ".log",
    ".ini",
    ".cfg",
}

_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _media_kind(name: str) -> tuple[str, str]:
    suffix = Path(name).suffix.lower()
    if suffix in _IMAGE_MIME:
        return ("image", _IMAGE_MIME[suffix])
    if suffix in {".html", ".htm"}:
        return ("html", "text/html")
    if suffix in {".md", ".markdown"}:
        return ("markdown", "text/markdown")
    if suffix in _TEXT_EXTS:
        return ("text", "text/plain")
    return ("binary", "application/octet-stream")


def _safe_links(result: dict) -> list[str]:
    try:
        return links_in(result)
    except ValueError:
        out = []
        raw_text = str(result.get("text", ""))
        for candidate in re.findall(r"https?://[^\s<>\"'`]+", raw_text):
            cleaned = candidate.rstrip("。，、；！!.,;")
            if cleaned not in out:
                out.append(cleaned)
            if len(out) >= 40:
                break
        return out


class Management:
    def __init__(self, plugin):
        self.plugin = plugin
        self.transfers = {}
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.closed = False

    @asynccontextmanager
    async def request(self):
        if self.closed:
            raise RuntimeError("插件正在卸载")
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            yield
        finally:
            self.tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.transfers.clear()

    @staticmethod
    def arguments(payload):
        if not isinstance(payload, str) or len(payload.encode()) > 60000:
            raise ValueError("请求过大")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise TypeError("请求需要对象")
        return result

    def _version(self, value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def _assets(self):
        plugin = self.plugin
        profiles = []
        for path in sorted(plugin.profiles.path.iterdir()):
            if path.is_dir() and not path.is_symlink():
                try:
                    profile = plugin.profiles.get(path.name)
                    profiles.append(
                        {
                            key: value
                            for key, value in asdict(profile).items()
                            if key
                            in {
                                "id",
                                "title",
                                "environment",
                                "access",
                                "model",
                                "task",
                                "model_name",
                                "tools",
                                "web_provider",
                                "max_steps",
                                "bound",
                                "skills",
                            }
                        }
                    )
                except (ValueError, OSError) as error:
                    profiles.append(
                        {"id": path.name, "title": path.name, "error": str(error)}
                    )
        skills = plugin.data_root / "abilities/skills"
        return {
            "profiles": profiles[:100],
            "skills": [
                p.name
                for p in sorted(skills.iterdir())
                if p.is_dir() and not p.is_symlink()
            ][:100],
            "handles": plugin.vault.handles(),
        }

    def _settings(self):
        settings = self.plugin.settings.model_dump()
        return {
            "settings": settings,
            "version": self._version(settings),
            "admins": sorted(self.plugin.admins),
            "handles": self.plugin.vault.handles(),
        }

    def _memory(self, options):
        plugin = self.plugin
        with plugin.threads.connect() as db:
            chats = {
                row[0]
                for row in db.execute(
                    'SELECT DISTINCT chat_id FROM agent_threads WHERE owner="chat"'
                )
            }
            chats.update(
                row[0]
                for row in db.execute(
                    'SELECT DISTINCT chat_id FROM memory_versions WHERE kind="memory"'
                )
            )
            workspaces = [
                dict(row)
                for row in db.execute(
                    "SELECT workspace_id,owner,chat_id,name,profile,thread_id FROM agent_workspaces ORDER BY rowid DESC LIMIT 60"
                )
            ]
        chats.update(plugin.settings.enabled_chats)
        chat_id = options.get("chat_id", "")
        return {
            "chats": sorted(chats),
            "context": plugin.memory.context(chat_id),
            "results": plugin.memory.search(options.get("query", ""), chat_id)
            if chat_id
            else [],
            "experiences": plugin.threads.search(
                options.get("query", ""), chat_id, scope="self"
            ),
            "workspaces": workspaces,
        }

    def _intents(self):
        with self.plugin.threads.connect() as db:
            return {
                "intents": [
                    dict(row)
                    for row in db.execute(
                        "SELECT intent_id,kind,source,target,due_at,reason,status,wake_status FROM intents ORDER BY created DESC LIMIT 60"
                    )
                ],
                "learning": [
                    dict(row)
                    for row in db.execute(
                        "SELECT thread_id,revision,status FROM learning_receipts ORDER BY rowid DESC LIMIT 30"
                    )
                ],
            }

    def _transfer(self, options):
        self.transfers = {
            key: value
            for key, value in self.transfers.items()
            if value["expires"] > time.time()
        }
        token = options.get("token", "")
        if not token:
            if len(self.transfers) >= 4:
                raise ValueError("传输较多，请稍后再试")
            token = uuid.uuid4().hex
            data = self.plugin.profiles.export(options["target"])
            self.transfers[token] = {
                "data": data,
                "expires": time.time() + 300,
                "direction": "download",
            }
        transfer = self.transfers.get(token)
        if not transfer or transfer["direction"] != "download":
            raise ValueError("导出已过期，请重新导出")
        offset = options.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(transfer["data"]):
            raise ValueError("传输位置无效")
        data = transfer["data"][offset : offset + 24576]
        return {
            "token": token,
            "data": base64.b64encode(data).decode(),
            "offset": offset + len(data),
            "total": len(transfer["data"]),
        }

    async def _discovery(self):
        ctx = getattr(self.plugin, "ctx", None)

        async def probe(capability, default, **kwargs):
            if ctx is None:
                return default
            try:
                return await call_capability(ctx, capability, **kwargs)
            except Exception:
                return default

        raw_streams = await probe("chat.get_all_streams", [], platform="all_platforms")
        streams = (
            raw_streams.get("streams") or raw_streams.get("data") or []
            if isinstance(raw_streams, dict)
            else raw_streams
        )
        raw_models = await probe("llm.get_available_models", {})
        if isinstance(raw_models, dict):
            models = (
                raw_models["models"]
                if isinstance(raw_models.get("models"), dict)
                else {k: v for k, v in raw_models.items() if k != "success"}
            )
        else:
            models = {}
        raw_plugins = await probe("component.list_loaded_plugins", [])
        plugins = (
            raw_plugins.get("plugins") or []
            if isinstance(raw_plugins, dict)
            else raw_plugins
        )
        raw_apis = await probe("api.list", [])
        apis = raw_apis.get("apis") or [] if isinstance(raw_apis, dict) else raw_apis
        return {
            "streams": streams if isinstance(streams, list) else [],
            "models": models if isinstance(models, dict) else {},
            "plugins": plugins if isinstance(plugins, list) else [],
            "apis": apis if isinstance(apis, list) else [],
        }

    async def read(self, section, payload):
        async with self.request():
            return await self._read(section, payload)

    async def _read(self, section, payload):
        options = self.arguments(payload)
        if section == "abilities":
            result = await completed_thread(self._assets)
        elif section == "asset":
            allowed = {
                key: value
                for key, value in options.items()
                if key in {"target", "chat_id", "document", "version"}
            }
            kind = options["kind"]
            result = await completed_thread(self.plugin.memory.read, kind, **allowed)
            result["history"] = await completed_thread(
                self.plugin.memory.history,
                kind,
                **{key: value for key, value in allowed.items() if key != "version"},
            )
        elif section == "settings":
            result = self._settings()
        elif section == "services":
            config = await completed_thread(self.plugin.runtime.services.load)
            await completed_thread(self._validate_services, config)
            result = {"configuration": config, "version": self._version(config)}
        elif section == "webhooks":
            config = await completed_thread(self.plugin.incoming.load)
            result = {"configuration": config, "version": self._version(config)}
        elif section == "memory":
            result = await completed_thread(self._memory, options)
            result["chat_sources"] = await self.plugin._resolve_chat_sources(
                result.get("chats", [])
            )
        elif section == "outputs":
            result = await completed_thread(self._outputs, options)
            result["chat_sources"] = await self.plugin._resolve_chat_sources(
                result.get("chats", [])
            )
        elif section == "artifact":
            result = await completed_thread(self._artifact, options)
        elif section == "workspace":
            result = await completed_thread(self._workspace, options)
        elif section == "intents":
            result = await completed_thread(self._intents)
        elif section == "discovery":
            result = await self._discovery()
        elif section == "health":
            sandbox = self.plugin.runtime.sandbox
            result = {
                "enabled": sandbox.settings.enabled,
                "ready": sandbox.ready,
                "error": sandbox.error,
                "running": len(sandbox.containers),
                "quarantined": len(self.plugin.runtime.files.quarantines),
                "bytes": await completed_thread(self.plugin.runtime.files.usage),
                "limit": self.plugin.runtime.files.total_bytes,
                "web": await self.plugin.runtime.web.status(),
            }
        elif section == "package":
            async with self.lock:
                result = await completed_thread(self._transfer, options)
        else:
            raise ValueError("未知管理查询")
        return {"success": True, **self.plugin.vault.redact(result)}

    def _resolve_workspace_dir(self, workspace_id: str) -> tuple[Path | None, dict]:
        if not isinstance(workspace_id, str) or not re.fullmatch(
            r"ws_[a-f0-9]{24}", workspace_id
        ):
            raise ValueError("工作区编号无效")
        meta = {}
        with self.plugin.threads.connect() as db:
            ws_row = db.execute(
                "SELECT workspace_id,owner,chat_id,name,profile,thread_id "
                "FROM agent_workspaces WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()
            if ws_row:
                meta = dict(ws_row)
            else:
                matched = None
                for t_row in db.execute(
                    "SELECT thread_id,owner,chat_id,profile,updated FROM agent_threads"
                ):
                    implicit = (
                        "ws_"
                        + hashlib.sha256(t_row["thread_id"].encode()).hexdigest()[:24]
                    )
                    if implicit == workspace_id:
                        matched = dict(t_row)
                        break
                if not matched:
                    raise ValueError("工作区不存在")
                meta = {
                    "workspace_id": workspace_id,
                    "owner": matched["owner"],
                    "chat_id": matched["chat_id"],
                    "name": f"临时 · {matched['thread_id'][:10]}",
                    "profile": matched["profile"],
                    "thread_id": matched["thread_id"],
                }
        root = self.plugin.runtime.files.root / workspace_id
        pointer = MemoryLayers._bytes(root / "current.json")
        if not pointer:
            return None, meta
        version = json.loads(pointer)["version"]
        if not re.fullmatch(r"v_[a-f0-9]{32}", version):
            raise ValueError("工作区版本无效")
        vdir = root / version
        if vdir.is_symlink() or not vdir.is_dir():
            raise ValueError("工作区目录无效")
        return vdir, meta

    def _workspace(self, options):
        path, _meta = self._resolve_workspace_dir(options["workspace_id"])
        if path is None:
            return {"files": [], "text": ""}
        files, _size = self.plugin.runtime.files.inventory(path)
        text = ""
        if options.get("path"):
            with regular_open(path, options["path"]) as source:
                text = source.read(24000).decode("utf-8", errors="replace")
        return {
            "files": [{"path": name, "bytes": size} for name, size in files[:200]],
            "text": text,
        }

    def _outputs(self, options):
        chat_id = options.get("chat_id", "")
        kind_filter = options.get("kind", "all")
        query = options.get("query", "")
        if not isinstance(chat_id, str) or len(chat_id) > 256:
            raise ValueError("会话筛选无效")
        if kind_filter not in {"all", "attachment", "workspace_out", "conclusion"}:
            raise ValueError("产物类型筛选无效")
        if not isinstance(query, str) or len(query) > 200:
            raise ValueError("检索关键词无效")

        files_api = self.plugin.runtime.files
        items = []
        chats = set(self.plugin.settings.enabled_chats)
        attached_keys = set()
        ws_entries = {}
        workspaces = []

        with self.plugin.threads.connect() as db:
            thread_rows = {
                r["thread_id"]: dict(r)
                for r in db.execute(
                    "SELECT thread_id,profile,chat_id,owner,state,result,updated "
                    "FROM agent_threads ORDER BY updated DESC LIMIT 200"
                )
            }
            delivered = {
                r["thread_id"]
                for r in db.execute("SELECT DISTINCT thread_id FROM result_deliveries")
            }
            att_rows = [
                dict(r)
                for r in db.execute(
                    "SELECT handle,chat_id,thread_id,revision,name,kind,size,digest,created "
                    "FROM attachments ORDER BY created DESC LIMIT 160"
                )
            ]
            named_ws = [
                dict(r)
                for r in db.execute(
                    "SELECT workspace_id,owner,chat_id,name,profile,thread_id "
                    "FROM agent_workspaces ORDER BY rowid DESC LIMIT 80"
                )
            ]

        for row in thread_rows.values():
            chats.add(row["chat_id"])

        for row in att_rows:
            fpath = files_api.artifacts / row["handle"]
            if fpath.is_symlink() or not fpath.is_file():
                continue
            chats.add(row["chat_id"])
            attached_keys.add((row["thread_id"], row["name"], row["size"]))
            t_info = thread_rows.get(row["thread_id"], {})
            media_kind, mime = _media_kind(row["name"])
            items.append(
                {
                    "id": f"att:{row['handle']}",
                    "kind": "attachment",
                    "media_kind": media_kind,
                    "mime": mime,
                    "handle": row["handle"],
                    "name": row["name"],
                    "title": row["name"],
                    "chat_id": row["chat_id"],
                    "thread_id": row["thread_id"],
                    "profile": t_info.get("profile", ""),
                    "bytes": row["size"],
                    "at": float(row["created"] or 0.0),
                    "delivered": row["thread_id"] in delivered,
                    "excerpt": f"已声明附件 · {row['size']} 字节",
                }
            )

        for row in named_ws:
            ws_entries[row["workspace_id"]] = dict(row)
            chats.add(row["chat_id"])

        for tid, t_info in thread_rows.items():
            implicit = "ws_" + hashlib.sha256(tid.encode()).hexdigest()[:24]
            if implicit not in ws_entries and (files_api.root / implicit / "current.json").exists():
                ws_entries[implicit] = {
                    "workspace_id": implicit,
                    "owner": t_info["owner"],
                    "chat_id": t_info["chat_id"],
                    "name": f"临时 · {tid[:10]}",
                    "profile": t_info["profile"],
                    "thread_id": tid,
                }

        for ws_id, meta in ws_entries.items():
            root = files_api.root / ws_id
            pointer = MemoryLayers._bytes(root / "current.json")
            file_count = 0
            out_count = 0
            total_bytes = 0
            if pointer:
                try:
                    version = json.loads(pointer)["version"]
                except (ValueError, KeyError, TypeError):
                    version = ""
                if re.fullmatch(r"v_[a-f0-9]{32}", str(version)):
                    vdir = root / version
                    if not vdir.is_symlink() and vdir.is_dir():
                        inv, total_bytes = files_api.inventory(vdir)
                        file_count = len(inv)
                        t_info = thread_rows.get(meta["thread_id"], {})
                        for rel, size in inv:
                            if not rel.startswith("out/"):
                                continue
                            out_count += 1
                            base_name = PurePosixPath(rel).name
                            if (meta["thread_id"], base_name, size) in attached_keys:
                                continue
                            media_kind, mime = _media_kind(base_name)
                            mtime = 0.0
                            try:
                                mtime = (vdir / rel).lstat().st_mtime
                            except OSError:
                                pass
                            items.append(
                                {
                                    "id": f"ws:{ws_id}:{rel}",
                                    "kind": "workspace_out",
                                    "media_kind": media_kind,
                                    "mime": mime,
                                    "workspace_id": ws_id,
                                    "workspace_name": meta["name"],
                                    "path": rel,
                                    "name": base_name,
                                    "title": base_name,
                                    "chat_id": meta["chat_id"],
                                    "thread_id": meta["thread_id"],
                                    "profile": meta["profile"],
                                    "bytes": size,
                                    "at": float(t_info.get("updated") or mtime),
                                    "delivered": meta["thread_id"] in delivered,
                                    "excerpt": f"工作区 {meta['name']} / {rel}",
                                }
                            )
            workspaces.append(
                {
                    **meta,
                    "file_count": file_count,
                    "out_count": out_count,
                    "total_bytes": total_bytes,
                }
            )

        for tid, row in thread_rows.items():
            try:
                parsed = json.loads(row["result"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(parsed, dict) or "error" in parsed:
                continue
            text = str(parsed.get("text") or "").strip()
            links = _safe_links(parsed)
            atts = [a for a in (parsed.get("attachments") or []) if isinstance(a, dict)]
            if not text and not links:
                continue
            excerpt = text.splitlines()[0][:140] if text else (links[0] if links else "")
            items.append(
                {
                    "id": f"con:{tid}",
                    "kind": "conclusion",
                    "media_kind": "conclusion",
                    "mime": "text/plain",
                    "name": excerpt[:48] or tid,
                    "title": excerpt[:64] or tid,
                    "chat_id": row["chat_id"],
                    "thread_id": tid,
                    "profile": row["profile"],
                    "bytes": len(text.encode("utf-8")),
                    "at": float(row["updated"] or 0.0),
                    "delivered": tid in delivered,
                    "links_count": len(links),
                    "attachments_count": len(atts),
                    "excerpt": excerpt,
                }
            )

        chat_items = [i for i in items if not chat_id or i["chat_id"] == chat_id]
        chat_workspaces = [w for w in workspaces if not chat_id or w["chat_id"] == chat_id]
        stats = {
            "total": len(chat_items),
            "attachments": sum(1 for i in chat_items if i["kind"] == "attachment"),
            "workspace_out": sum(1 for i in chat_items if i["kind"] == "workspace_out"),
            "conclusions": sum(1 for i in chat_items if i["kind"] == "conclusion"),
            "workspaces": len(chat_workspaces),
        }

        filtered = chat_items
        if kind_filter != "all":
            filtered = [i for i in filtered if i["kind"] == kind_filter]
        needle = query.strip().lower()
        if needle:
            filtered = [
                i
                for i in filtered
                if needle
                in " ".join(
                    str(
                        i.get(k) or ""
                    )
                    for k in (
                        "name",
                        "title",
                        "path",
                        "excerpt",
                        "profile",
                        "workspace_name",
                        "thread_id",
                    )
                ).lower()
            ]
        filtered.sort(key=lambda item: item.get("at", 0.0), reverse=True)
        return {
            "items": filtered[:80],
            "workspaces": chat_workspaces[:60],
            "chats": sorted(c for c in chats if c),
            "stats": stats,
        }

    def _read_artifact_stream(
        self, stream, name: str, offset: int, *, total_hint: int | None = None, extra: dict | None = None
    ) -> dict:
        media_kind, mime = _media_kind(name)
        meta = dict(extra or {})
        if media_kind in {"image", "binary"}:
            max_preview = 8 * 1024 * 1024
            raw = stream.read(max_preview + 1)
            if len(raw) > max_preview:
                raise ValueError("二进制产物超过 8 MiB 在线预览上限")
            if self.plugin.vault.contains_secret(raw):
                raise PermissionError("产物包含已登记凭据，已拒绝预览")
            total = total_hint if total_hint is not None else len(raw)
            chunk = raw[offset : offset + 196608]
            next_off = offset + len(chunk) if offset + len(chunk) < len(raw) else None
            return {
                **meta,
                "name": name,
                "media_kind": media_kind,
                "mime": mime,
                "bytes": len(raw),
                "total": total,
                "base64": base64.b64encode(chunk).decode("ascii"),
                "next_offset": next_off,
            }
        raw = stream.read(2 * 1024 * 1024)
        decoded = self.plugin.vault.redact(raw.decode("utf-8", errors="replace"))
        end = min(len(decoded), offset + 16000)
        return {
            **meta,
            "name": name,
            "media_kind": media_kind,
            "mime": mime,
            "bytes": total_hint if total_hint is not None else len(raw),
            "total": len(decoded),
            "text": decoded[offset:end],
            "next_offset": end if end < len(decoded) else None,
        }

    def _artifact(self, options):
        kind = options.get("kind", "")
        offset = options.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 128 * 1024**2:
            raise ValueError("预览偏移量无效")

        if kind == "attachment":
            handle = options.get("handle", "")
            if (
                not isinstance(handle, str)
                or not handle.startswith("att_")
                or not handle[4:].isalnum()
            ):
                raise ValueError("附件句柄无效")
            with self.plugin.threads.connect() as db:
                row = db.execute(
                    "SELECT * FROM attachments WHERE handle=?", (handle,)
                ).fetchone()
            if not row:
                raise ValueError("附件不存在")
            fpath = self.plugin.runtime.files.artifacts / handle
            if (
                fpath.is_symlink()
                or not fpath.is_file()
                or fpath.stat().st_size != row["size"]
                or hashlib.sha256(fpath.read_bytes()).hexdigest() != row["digest"]
            ):
                raise ValueError("附件已损坏")
            with open(fpath, "rb") as source:
                return self._read_artifact_stream(
                    source,
                    row["name"],
                    offset,
                    total_hint=row["size"],
                    extra={
                        "kind": "attachment",
                        "handle": handle,
                        "chat_id": row["chat_id"],
                        "thread_id": row["thread_id"],
                    },
                )

        if kind == "workspace_file":
            vdir, ws_meta = self._resolve_workspace_dir(options.get("workspace_id", ""))
            if vdir is None:
                raise ValueError("工作区尚无已提交文件")
            rel_path = options.get("path", "")
            if not isinstance(rel_path, str) or not rel_path:
                raise ValueError("文件路径无效")
            with regular_open(vdir, rel_path) as source:
                size = os.fstat(source.fileno()).st_size
                return self._read_artifact_stream(
                    source,
                    PurePosixPath(rel_path).name,
                    offset,
                    total_hint=size,
                    extra={
                        "kind": "workspace_file",
                        "workspace_id": ws_meta["workspace_id"],
                        "path": rel_path,
                        "chat_id": ws_meta["chat_id"],
                        "thread_id": ws_meta["thread_id"],
                    },
                )

        if kind == "conclusion":
            thread_id = options.get("thread_id", "")
            if not isinstance(thread_id, str) or len(thread_id) > 64:
                raise ValueError("线程编号无效")
            with self.plugin.threads.connect() as db:
                row = db.execute(
                    "SELECT thread_id,chat_id,profile,state,result,updated "
                    "FROM agent_threads WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
            if not row:
                raise ValueError("线程不存在")
            parsed = json.loads(row["result"] or "{}")
            text = self.plugin.vault.redact(str(parsed.get("text") or ""))
            links = _safe_links(parsed)[:40]
            atts = [a for a in (parsed.get("attachments") or []) if isinstance(a, dict)][:8]
            end = min(len(text), offset + 16000)
            return {
                "kind": "conclusion",
                "media_kind": "conclusion",
                "thread_id": row["thread_id"],
                "chat_id": row["chat_id"],
                "profile": row["profile"],
                "state": row["state"],
                "text": text[offset:end],
                "next_offset": end if end < len(text) else None,
                "total": len(text),
                "links": links,
                "attachments": atts,
            }

        raise ValueError("未知产物类型")

    async def write(self, action, payload):
        async with self.request(), self.lock:
            return await self._write(action, payload)

    async def _write(self, action, payload):
        options = self.arguments(payload)
        plugin = self.plugin
        if action in {"asset", "rollback"}:
            kind = options.pop("kind")
            allowed = {
                key: value
                for key, value in options.items()
                if key in {"target", "chat_id", "document", "expected_version"}
            }
            if type(allowed.get("expected_version")) is not int:
                raise ValueError("请先读取当前版本")
            function = (
                plugin.memory.write if action == "asset" else plugin.memory.rollback
            )
            result = await completed_thread(
                function,
                kind,
                options["content"] if action == "asset" else options["version"],
                allow_custom=True,
                **allowed,
            )
        elif action == "clone":
            profile = await completed_thread(
                plugin.profiles.clone, options.get("base", "scout"), options["target"]
            )
            result = {"id": profile.id}
        elif action == "delete_profile":
            target = str(options.get("target") or "").strip()
            await completed_thread(plugin.profiles.delete, target)
            result = {"id": target}
        elif action == "delete_skill":
            target = str(options.get("target") or "").strip()
            await completed_thread(plugin.memory.delete_skill, target)
            result = {"target": target}
        elif action == "vault":
            if options.get("delete") is True:
                await completed_thread(plugin.vault.delete, options["handle"])
            else:
                await completed_thread(
                    plugin.vault.set, options["handle"], options["value"]
                )
            result = {"handles": await completed_thread(plugin.vault.handles)}
        elif action == "settings":
            if options.get("expected_version") != self._settings()["version"]:
                raise ValueError("设置已变化，请刷新后再保存")
            settings = Settings.model_validate(options["settings"])
            await plugin.on_config_update("self", settings.model_dump(), 0)
            await completed_thread(
                atomic_private_write,
                plugin.data_root / "settings.json",
                json.dumps(settings.model_dump(), ensure_ascii=False).encode(),
            )
            result = self._settings()
        elif action == "services":
            current = await completed_thread(plugin.runtime.services.load)
            if options.get("expected_version") != self._version(current):
                raise ValueError("服务登记已变化，请刷新后重试")
            content = json.dumps(options["configuration"], ensure_ascii=False)
            await completed_thread(self._validate_services, options["configuration"])
            await completed_thread(
                atomic_private_write, plugin.runtime.services.path, content.encode()
            )
            await plugin.runtime.services.close_sessions()
            result = {"version": self._version(options["configuration"])}
        elif action == "webhooks":
            current = await completed_thread(plugin.incoming.load)
            if options.get("expected_version") != self._version(current):
                raise ValueError("事件通道已变化，请刷新后重试")
            config = options["configuration"]
            await completed_thread(plugin.incoming.validate, config)
            content = json.dumps(config, ensure_ascii=False).encode()
            if plugin.vault.contains_secret(content) or len(content) > 48000:
                raise ValueError("事件通道只保存凭据句柄")
            await completed_thread(atomic_private_write, plugin.incoming.path, content)
            result = {"version": self._version(config)}
        elif action == "cancel_intent":
            row = await completed_thread(plugin.intents.get, options["intent_id"])
            await completed_thread(
                plugin.intents.cancel, row["intent_id"], row["target"]
            )
            result = {"intent_id": row["intent_id"]}
        elif action == "schedule_intent":
            chat_id = str(options.get("chat_id") or "").strip()
            if not chat_id:
                raise ValueError("请填写目标会话 chat_id")
            profile_name = str(options.get("profile") or "").strip()
            if profile_name:
                await completed_thread(plugin.profiles.get, profile_name)
            prompt = str(options.get("prompt") or "").strip()
            reason = str(options.get("reason") or prompt).strip()
            if not reason:
                raise ValueError("请填写计划原因或任务提示词")
            in_seconds = int(options.get("in_seconds", 60))
            if in_seconds < 1:
                raise ValueError("延迟秒数必须大于 0")
            every_seconds = int(options.get("every_seconds", 0) or 0)
            if every_seconds and every_seconds < 300:
                raise ValueError("周期执行间隔至少 300 秒")
            kind = str(options.get("kind") or "return").strip() or "return"
            payload = {
                "platform": str(options.get("platform") or "qq").strip() or "qq",
                "profile": profile_name or str(plugin.settings.heartbeat_profile or "scout"),
                "workspace": str(options.get("workspace") or "").strip(),
                "prompt": prompt or reason,
                "every_seconds": every_seconds,
                "scheduled_admin": True,
            }
            due_at = plugin.intents.clock() + in_seconds
            row = await completed_thread(
                plugin.intents.schedule,
                kind=kind,
                source="workflow",
                target=f"chat:{chat_id}",
                due_at=due_at,
                reason=reason,
                payload=payload,
                bypass_limits=True,
            )
            result = {"intent": row}
        elif action == "import":
            result = await completed_thread(self._upload, options)
        else:
            raise ValueError("未知管理操作")
        return {"success": True, **plugin.vault.redact(result)}

    def _validate_services(self, config):
        raw = json.dumps(config).encode()
        if (
            self.plugin.vault.contains_secret(raw)
            or len(raw) > 48000
            or not isinstance(config, dict)
            or set(config) - {"servers", "templates", "inputs", "apis", "web"}
        ):
            raise ValueError("服务登记只接受结构化配置和凭据句柄")
        if not isinstance(config.get("servers", {}), dict) or not isinstance(
            config.get("templates", {}), dict
        ):
            raise TypeError("服务登记需要 servers 和 templates 对象")
        apis = config.get("apis", {})
        if not isinstance(apis, dict) or len(apis) > 32:
            raise ValueError("跨插件 API 登记数量或格式无效")
        for name, spec in apis.items():
            validate_api_entry(name, spec)
        inputs = config.get("inputs", {})
        if not isinstance(inputs, dict) or len(inputs) > 16:
            raise ValueError("平台文件读取器登记无效")
        for binding in inputs.values():
            if (
                not isinstance(binding, dict)
                or set(binding) != {"service", "tool"}
                or binding["service"] not in config.get("servers", {})
                or binding["tool"]
                not in config["servers"][binding["service"]].get("tools", [])
            ):
                raise ValueError("平台文件读取器需要已登记的服务和工具")
        for name, server in config.get("servers", {}).items():
            self.plugin.vault._validate_handle(name)
            if not isinstance(server, dict) or set(server) - {
                "command",
                "credentials",
                "tools",
                "readonly",
                "access",
                "memory_mib",
                "description",
            }:
                raise ValueError("MCP 服务含未知配置项")
            command = server.get("command", [])
            if (
                not isinstance(command, list)
                or not command
                or not all(
                    isinstance(part, str) and "\x00" not in part for part in command
                )
                or not command[0].startswith("/")
            ):
                raise ValueError("MCP 程序使用绝对路径与固定参数")
            for handle in server.get("credentials", {}).values():
                self.plugin.vault._validate_handle(handle)
        for template in config.get("templates", {}).values():
            if not isinstance(template, dict) or set(template) - {
                "argv",
                "schema",
                "secrets",
                "targets",
                "description",
                "access",
            }:
                raise ValueError("固定模板含未知配置项")
            raw_targets = template.get("targets", [])
            if not isinstance(raw_targets, list):
                raise ValueError("固定模板 targets 必须为列表")
            normalized_targets = []
            for entry in raw_targets:
                if isinstance(entry, str):
                    normalized_targets.append(entry)
                elif isinstance(entry, dict) and "address" in entry and "port" in entry:
                    normalized_targets.append(f"{entry['address']}:{entry['port']}")
                else:
                    raise ValueError("固定模板 targets 仅支持 host:port 字符串或 {address, port}")
            if normalized_targets:
                validate_endpoint_targets(normalized_targets)
            for handle in template.get("secrets", []):
                self.plugin.vault._validate_handle(handle)
        validate_web_bindings(config)

    def _upload(self, options):
        self.transfers = {
            key: value
            for key, value in self.transfers.items()
            if value["expires"] > time.time()
        }
        token = options.get("token", "")
        if not token:
            if len(self.transfers) >= 4:
                raise ValueError("传输较多，请稍后再试")
            token = uuid.uuid4().hex
            self.transfers[token] = {
                "data": bytearray(),
                "target": options["target"],
                "expires": time.time() + 300,
                "direction": "upload",
            }
        transfer = self.transfers.get(token)
        if (
            not transfer
            or transfer["direction"] != "upload"
            or options.get("offset", 0) != len(transfer["data"])
        ):
            raise ValueError("导入位置无效或传输过期")
        data = base64.b64decode(options["data"], validate=True)
        if len(data) > 24576 or len(transfer["data"]) + len(data) > 8 * 1024**2:
            self.transfers.pop(token, None)
            raise ValueError("档案包过大")
        transfer["data"].extend(data)
        if options.get("done"):
            self.transfers.pop(token)
            profile = self.plugin.profiles.import_package(
                bytes(transfer["data"]), transfer["target"]
            )
            return {"id": profile.id, "done": True}
        return {"token": token, "offset": len(transfer["data"])}
