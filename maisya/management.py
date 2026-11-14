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

from .abilities import FISH_SEED_RE
from .config import Settings
from .memory import PRESETS_DIR, MemoryLayers, parse_skill_meta
from .pathsafe import is_absolute_program_path
from .shaping import collect_known_chats, resolve_chat_target, resolve_stream_spec
from .recipes import (
    build_recipe_draft,
    build_recipe_preview,
    export_recipe_archive,
    fetch_public_recipe_bytes,
    import_recipe_archive,
)
from .scout import call_capability
from .share import links_in
from .services import (
    validate_api_entry,
    validate_endpoint_targets,
    validate_web_bindings,
)
from .trust import format_stream_spec, parse_stream_spec
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
        self.recipe_previews = {}
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.closed = False
        self._model_catalog_cache = {"expires": 0.0, "data": None}

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
        self.recipe_previews.clear()

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
                                "fallback_models",
                                "tools",
                                "web_provider",
                                "max_steps",
                                "bound",
                                "enabled",
                                "skills",
                                "allow_users",
                                "allow_chats",
                                "fish_seed",
                            }
                        }
                        | {"model_list": list(profile.model_list)}
                    )
                except (ValueError, OSError) as error:
                    profiles.append(
                        {"id": path.name, "title": path.name, "error": str(error)}
                    )
        skills_dir = plugin.data_root / "abilities/skills"
        skill_ids = (
            [
                p.name
                for p in sorted(skills_dir.iterdir())
                if p.is_dir() and not p.is_symlink()
            ][:100]
            if skills_dir.is_dir()
            else []
        )
        preset_set = plugin.memory.preset_skills()
        with plugin.threads.connect() as db:
            skill_versions = {
                row["target"]: (row["version"], row["updated"])
                for row in db.execute(
                    "SELECT v.target, v.version, v.updated FROM memory_versions v "
                    "JOIN (SELECT asset, MAX(version) AS version FROM memory_versions WHERE kind='skill' GROUP BY asset) latest "
                    "ON v.asset=latest.asset AND v.version=latest.version"
                )
            }
        skill_items = []
        for skill_id in skill_ids:
            skill_file = skills_dir / skill_id / "SKILL.md"
            raw = (
                MemoryLayers._bytes(skill_file).decode("utf-8", errors="replace")
                if skill_file.exists() and not skill_file.is_symlink()
                else ""
            )
            meta = parse_skill_meta(skill_id, raw)
            is_preset = skill_id in preset_set
            preset_raw = (
                (PRESETS_DIR / "skills" / skill_id / "SKILL.md").read_text("utf-8")
                if is_preset
                else ""
            )
            ver, updated = skill_versions.get(skill_id, (0, 0.0))
            companions = plugin.memory.skill_files(skill_id)
            skill_items.append(
                {
                    "id": skill_id,
                    "name": meta["name"],
                    "title": meta["title"],
                    "description": meta["description"],
                    "has_frontmatter": meta["has_frontmatter"],
                    "is_preset": is_preset,
                    "is_modified": bool(is_preset and raw.strip() != preset_raw.strip()),
                    "used_by": [
                        p["id"] for p in profiles if skill_id in (p.get("skills") or [])
                    ],
                    "companion_count": len(companions),
                    "companion_files": [c["path"] for c in companions[:20]],
                    "chars": len(raw),
                    "version": ver,
                    "updated": updated,
                }
            )
        return {
            "profiles": profiles[:100],
            "skills": skill_ids,
            "skill_items": skill_items,
            "handles": plugin.vault.handles(),
        }

    def _set_profile_skill_binding(
        self, profile_id: str, skill_id: str, bound: bool
    ) -> dict:
        if bound and skill_id not in self.plugin.memory.skills():
            raise ValueError("技能不存在")
        profile = self.plugin.profiles.get(profile_id)
        current = self.plugin.memory.read(
            "profile", target=profile_id, document="profile.toml"
        )
        existing = list(profile.skills)
        if bound:
            next_skills = existing if skill_id in existing else [*existing, skill_id]
        else:
            next_skills = [item for item in existing if item != skill_id]
        formatted = "skills = " + json.dumps(next_skills, ensure_ascii=False)
        text = current["content"]
        if re.search(r"(?ms)^skills\s*=\s*\[.*?\]", text):
            updated = re.sub(
                r"(?ms)^skills\s*=\s*\[.*?\]", formatted, text, count=1
            )
        else:
            updated = (text.rstrip("\n") + "\n" + formatted + "\n").lstrip("\n")
        saved = self.plugin.memory.write(
            "profile",
            updated,
            target=profile_id,
            document="profile.toml",
            expected_version=current["version"],
            allow_custom=True,
        )
        return {
            "profile": profile_id,
            "skill": skill_id,
            "bound": bound,
            "skills": next_skills,
            "version": saved["version"],
        }

    def _set_profile_fish(self, profile_id: str, fish_seed: str) -> dict:
        if not isinstance(fish_seed, str) or not FISH_SEED_RE.fullmatch(fish_seed):
            raise ValueError("角色小鱼头像种子格式无效")
        self.plugin.profiles.get(profile_id)
        current = self.plugin.memory.read(
            "profile", target=profile_id, document="profile.toml", allow_custom=True
        )
        text = current["content"]
        if fish_seed == "":
            updated = re.sub(r"(?m)^fish_seed\s*=.*\r?\n?", "", text)
        else:
            formatted = "fish_seed = " + json.dumps(fish_seed, ensure_ascii=False)
            if re.search(r"(?m)^fish_seed\s*=.*$", text):
                updated = re.sub(r"(?m)^fish_seed\s*=.*$", formatted, text, count=1)
            elif re.search(r"(?m)^title\s*=.*$", text):
                updated = re.sub(
                    r"(?m)^title\s*=.*$",
                    lambda m: m.group(0) + "\n" + formatted,
                    text,
                    count=1,
                )
            else:
                updated = (text.rstrip("\n") + "\n" + formatted + "\n").lstrip("\n")
        if updated and not updated.endswith("\n"):
            updated += "\n"
        saved = self.plugin.memory.write(
            "profile",
            updated,
            target=profile_id,
            document="profile.toml",
            expected_version=current["version"],
            allow_custom=True,
        )
        return {
            "target": profile_id,
            "fish_seed": fish_seed,
            "version": saved["version"],
        }

    def _profile_fish_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        root = getattr(getattr(self.plugin, "profiles", None), "path", None)
        if not root or not root.is_dir():
            return mapping
        for path in sorted(root.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                mapping[path.name] = self.plugin.profiles.get(path.name).fish_seed
            except (ValueError, OSError):
                mapping[path.name] = ""
        return mapping

    def _known_chats_sync(self) -> list[dict]:
        plugin = self.plugin
        streams = (getattr(plugin, "_stream_cache", {}) or {}).get("streams", {}) or {}
        routes = (
            {r["session_id"]: r for r in plugin.messages.list_routes()}
            if getattr(plugin, "messages", None) is not None
            else {}
        )
        enabled_set = set(plugin.settings.enabled_chats)
        records: list[dict] = []
        seen_sids: set[str] = set()
        for sid in [*streams.keys(), *routes.keys()]:
            if not sid or sid in seen_sids:
                continue
            seen_sids.add(sid)
            s_info = streams.get(sid) or {}
            r_info = routes.get(sid) or {}
            plat = str(s_info.get("platform") or r_info.get("platform") or "").strip().lower()
            gid = str(s_info.get("group_id") or r_info.get("group_id") or "").strip()
            uid = str(s_info.get("user_id") or r_info.get("user_id") or "").strip()
            ctype = str(
                s_info.get("chat_type")
                or r_info.get("chat_type")
                or ("group" if gid else ("private" if uid else ""))
            ).strip().lower()
            spec = str(s_info.get("stream_spec") or "").strip()
            if not spec and plat and (gid or uid):
                spec = format_stream_spec(plat, "group" if gid else "private", gid or uid)
            aliases = plugin.chat_aliases_for(sid, plat) | {sid}
            if spec:
                aliases.add(spec)
            records.append(
                {
                    "chat_id": sid,
                    "stream_spec": spec,
                    "stream_id": spec or sid,
                    "name": str(s_info.get("name") or gid or uid or spec or sid),
                    "platform": plat,
                    "chat_type": ctype,
                    "group_id": gid,
                    "user_id": uid,
                    "enabled": bool(aliases & enabled_set),
                    "aliases": aliases,
                }
            )
        for raw in plugin.settings.enabled_chats:
            token = str(raw or "").strip()
            if not token:
                continue
            parsed = parse_stream_spec(token)
            norm = format_stream_spec(*parsed) if parsed else token
            if any(token in rec["aliases"] or norm in rec["aliases"] for rec in records):
                continue
            if parsed is not None:
                plat, ctype, tid = parsed
                records.append(
                    {
                        "chat_id": norm,
                        "stream_spec": norm,
                        "stream_id": norm,
                        "name": tid or norm,
                        "platform": plat,
                        "chat_type": ctype,
                        "group_id": tid if ctype == "group" else "",
                        "user_id": tid if ctype == "private" else "",
                        "enabled": True,
                        "aliases": {token, norm},
                    }
                )
            else:
                records.append(
                    {
                        "chat_id": token,
                        "stream_spec": "",
                        "stream_id": token,
                        "name": token,
                        "platform": "",
                        "chat_type": "",
                        "group_id": "",
                        "user_id": "",
                        "enabled": True,
                        "aliases": {token},
                    }
                )
        return [{k: v for k, v in rec.items() if k != "aliases"} for rec in records]

    def _resolve_memory_chat_id(self, raw_chat_id: str) -> str:
        raw = str(raw_chat_id or "").strip()
        if not raw:
            return raw
        sid = self.plugin.resolve_known_session_id(raw)
        if sid != raw:
            with self.plugin.threads.connect() as db:
                has_sid = db.execute(
                    'SELECT 1 FROM memory_versions WHERE kind="memory" AND chat_id=? LIMIT 1',
                    (sid,),
                ).fetchone()
                if has_sid:
                    return sid
                has_raw = db.execute(
                    'SELECT 1 FROM memory_versions WHERE kind="memory" AND chat_id=? LIMIT 1',
                    (raw,),
                ).fetchone()
                if has_raw:
                    return raw
        return sid

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
        raw_nonempty = {str(c).strip() for c in chats if str(c or "").strip()}
        deduped = {
            ("self" if c == "self" else (plugin.canonical_stream_spec(c) or c))
            for c in raw_nonempty
        }
        source_ids = sorted(deduped | raw_nonempty)
        raw_chat_id = str(options.get("chat_id", "") or "").strip()
        chat_id = self._resolve_memory_chat_id(raw_chat_id)
        return {
            "chats": sorted(deduped),
            "_source_chat_ids": source_ids,
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

    def _transfer_skill(self, options):
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
            data = self.plugin.memory.export_skill_archive(
                str(options.get("target") or "").strip()
            )
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

    def _transfer_recipe(self, options):
        self.transfers = {
            key: value
            for key, value in self.transfers.items()
            if value["expires"] > time.time()
        }
        token = options.get("token", "")
        meta = None
        if not token:
            if len(self.transfers) >= 4:
                raise ValueError("传输较多，请稍后再试")
            token = uuid.uuid4().hex
            data, meta = export_recipe_archive(
                self.plugin,
                str(options.get("target") or "").strip(),
                options,
            )
            self.transfers[token] = {
                "data": data,
                "meta": meta,
                "expires": time.time() + 300,
                "direction": "download_recipe",
            }
        transfer = self.transfers.get(token)
        if not transfer or transfer["direction"] != "download_recipe":
            raise ValueError("导出已过期，请重新导出")
        offset = options.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(transfer["data"]):
            raise ValueError("传输位置无效")
        data = transfer["data"][offset : offset + 24576]
        out = {
            "token": token,
            "data": base64.b64encode(data).decode(),
            "offset": offset + len(data),
            "total": len(transfer["data"]),
        }
        if meta is not None:
            out["meta"] = meta
        return out

    async def _collect_host_models(
        self, *, force: bool = False, probe_host: bool = False
    ) -> dict:
        if hasattr(self.plugin, "refresh_host_model_catalog"):
            refreshed = await completed_thread(self.plugin.refresh_host_model_catalog)
            if refreshed:
                self._model_catalog_cache["expires"] = 0.0
        now = time.monotonic()
        cached_probe = self._model_catalog_cache.get("probe_data")
        probe_valid = (
            not force
            and isinstance(cached_probe, dict)
            and now < float(self._model_catalog_cache.get("expires") or 0.0)
        )

        entries: dict[str, dict] = {}
        task_map: dict[str, list[str]] = {}

        def add_entry(
            name: str,
            *,
            identifier: str = "",
            provider: str = "",
            source: str = "host",
            assigned_tasks=(),
        ) -> None:
            cleaned = (name or "").strip()
            if not cleaned or len(cleaned) > 256:
                return
            tasks_clean = list(
                dict.fromkeys(
                    str(t).strip()
                    for t in (assigned_tasks or ())
                    if isinstance(t, str) and str(t).strip()
                )
            )
            existing = entries.get(cleaned)
            if existing is None:
                entries[cleaned] = {
                    "name": cleaned,
                    "identifier": identifier.strip(),
                    "provider": provider.strip(),
                    "source": source,
                    "assigned_tasks": tasks_clean,
                }
            else:
                if identifier and not existing["identifier"]:
                    existing["identifier"] = identifier.strip()
                if provider and not existing["provider"]:
                    existing["provider"] = provider.strip()
                cur_tasks = existing.setdefault("assigned_tasks", [])
                for t in tasks_clean:
                    if t not in cur_tasks:
                        cur_tasks.append(t)

        host_catalog = getattr(self.plugin, "_host_model_catalog", None)
        if isinstance(host_catalog, dict):
            for item in host_catalog.get("models") or ():
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    add_entry(
                        item["name"],
                        identifier=str(item.get("identifier") or ""),
                        provider=str(item.get("provider") or ""),
                        source=str(item.get("source") or "host_config"),
                        assigned_tasks=item.get("assigned_tasks") or (),
                    )
            for task_key, assigned in (host_catalog.get("tasks") or {}).items():
                if isinstance(task_key, str) and isinstance(assigned, list):
                    cleaned_assigned = [
                        str(m).strip()
                        for m in assigned
                        if isinstance(m, str) and str(m).strip()
                    ]
                    if cleaned_assigned:
                        task_map[task_key] = cleaned_assigned
                        if task_key != "web_search":
                            for model_name in cleaned_assigned:
                                add_entry(
                                    model_name,
                                    source="task_config",
                                    assigned_tasks=[task_key],
                                )

        if probe_host and not probe_valid:
            ctx = getattr(self.plugin, "ctx", None)

            async def probe(capability, default, **kwargs):
                if ctx is None:
                    return default
                try:
                    return await call_capability(ctx, capability, **kwargs)
                except Exception:
                    return default

            probe_entries: list[dict] = []
            probe_tasks: dict[str, list[str]] = {}

            raw_models = await probe("llm.get_available_models", {})
            if isinstance(raw_models, list):
                for item in raw_models:
                    if isinstance(item, str) and item.strip():
                        probe_tasks.setdefault(item.strip(), [])
            elif isinstance(raw_models, dict):
                unpacked = raw_models.get("models")
                if isinstance(unpacked, list):
                    for item in unpacked:
                        if isinstance(item, str) and item.strip():
                            probe_tasks.setdefault(item.strip(), [])
                else:
                    model_dict = (
                        unpacked
                        if isinstance(unpacked, dict)
                        else {k: v for k, v in raw_models.items() if k != "success"}
                    )
                    for key, val in model_dict.items():
                        if not isinstance(key, str) or not key.strip():
                            continue
                        if isinstance(val, list):
                            cleaned = [
                                str(m).strip()
                                for m in val
                                if isinstance(m, str) and str(m).strip()
                            ]
                            probe_tasks[key] = cleaned
                            if key != "web_search":
                                for model_name in cleaned:
                                    probe_entries.append(
                                        {
                                            "name": model_name,
                                            "identifier": "",
                                            "provider": "",
                                            "source": "available_models",
                                            "assigned_tasks": [key],
                                        }
                                    )
                        elif isinstance(val, dict):
                            name = str(
                                val.get("name") or val.get("model_name") or key
                            ).strip()
                            probe_entries.append(
                                {
                                    "name": name,
                                    "identifier": str(val.get("model_identifier") or ""),
                                    "provider": str(
                                        val.get("api_provider") or val.get("provider") or ""
                                    ),
                                    "source": "available_models",
                                    "assigned_tasks": [key] if key != "web_search" else [],
                                }
                            )
                            probe_tasks[key] = [name]
                        else:
                            probe_tasks.setdefault(key, [])

            raw_stats = await probe("statistics.local.models", {}, top_models=100)
            stat_items = (
                raw_stats.get("models")
                if isinstance(raw_stats, dict)
                else raw_stats
            )
            if isinstance(stat_items, list):
                for item in stat_items[:128]:
                    if isinstance(item, str) and item.strip():
                        probe_entries.append(
                            {
                                "name": item.strip(),
                                "identifier": "",
                                "provider": "",
                                "source": "statistics",
                            }
                        )
                    elif isinstance(item, dict):
                        name = str(
                            item.get("model_name") or item.get("name") or ""
                        ).strip()
                        if name:
                            probe_entries.append(
                                {
                                    "name": name,
                                    "identifier": str(item.get("model_identifier") or ""),
                                    "provider": str(
                                        item.get("provider")
                                        or item.get("api_provider")
                                        or ""
                                    ),
                                    "source": "statistics",
                                }
                            )

            cached_probe = {"entries": probe_entries, "tasks": probe_tasks}
            self._model_catalog_cache = {"expires": now + 15.0, "probe_data": cached_probe}

        if isinstance(cached_probe, dict):
            for item in cached_probe.get("entries") or ():
                if isinstance(item, dict):
                    add_entry(
                        str(item.get("name") or ""),
                        identifier=str(item.get("identifier") or ""),
                        provider=str(item.get("provider") or ""),
                        source=str(item.get("source") or "host"),
                        assigned_tasks=item.get("assigned_tasks") or (),
                    )
            for key, val in (cached_probe.get("tasks") or {}).items():
                task_map.setdefault(key, val)

        profiles_mgr = getattr(self.plugin, "profiles", None)
        if (
            profiles_mgr
            and getattr(profiles_mgr, "path", None)
            and profiles_mgr.path.exists()
        ):
            for path in sorted(profiles_mgr.path.iterdir()):
                if path.is_dir() and not path.is_symlink():
                    try:
                        prof = profiles_mgr.get(path.name)
                        for m in prof.model_list:
                            add_entry(m, source="profile")
                    except (ValueError, OSError):
                        continue

        catalog_list = list(entries.values())
        return {
            "models": catalog_list,
            "names": [item["name"] for item in catalog_list],
            "tasks": task_map,
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
        raw_stream_list = (
            raw_streams.get("streams") or raw_streams.get("data") or []
            if isinstance(raw_streams, dict)
            else raw_streams
        )
        streams = []
        for s in raw_stream_list if isinstance(raw_stream_list, list) else []:
            if isinstance(s, dict):
                plat_s = str(s.get("platform") or "").strip().lower()
                is_grp = bool(
                    s.get("is_group_session")
                    or s.get("chat_type") == "group"
                    or s.get("group_id")
                )
                gid_s = str(s.get("group_id") or "").strip()
                uid_s = str(s.get("user_id") or "").strip()
                tid_s = gid_s if is_grp else uid_s
                spec_s = (
                    format_stream_spec(plat_s, "group" if is_grp else "private", tid_s)
                    if (plat_s and tid_s)
                    else ""
                )
                streams.append(
                    {
                        **s,
                        "platform": plat_s or str(s.get("platform") or ""),
                        "stream_spec": spec_s,
                    }
                )
            else:
                streams.append(s)
        host_models = await self._collect_host_models(force=True, probe_host=True)
        raw_plugins = await probe("component.list_loaded_plugins", [])
        plugins = (
            raw_plugins.get("plugins") or []
            if isinstance(raw_plugins, dict)
            else raw_plugins
        )
        raw_apis = await probe("api.list", [])
        apis = raw_apis.get("apis") or [] if isinstance(raw_apis, dict) else raw_apis
        return {
            "streams": streams,
            "models": host_models["tasks"],
            "available_models": host_models["names"],
            "model_catalog": host_models["models"],
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
            result["known_chats"] = await completed_thread(self._known_chats_sync)
            host_models = await self._collect_host_models()
            result["available_models"] = host_models["names"]
            result["model_catalog"] = host_models["models"]
            result["model_tasks"] = host_models["tasks"]
        elif section == "asset":
            allowed = {
                key: value
                for key, value in options.items()
                if key in {"target", "chat_id", "document", "version"}
            }
            kind = options["kind"]
            if kind == "memory" and allowed.get("chat_id"):
                allowed["chat_id"] = await completed_thread(
                    self._resolve_memory_chat_id, str(allowed["chat_id"])
                )
            result = await completed_thread(self.plugin.memory.read, kind, **allowed)
            result["history"] = await completed_thread(
                self.plugin.memory.history,
                kind,
                **{key: value for key, value in allowed.items() if key != "version"},
            )
        elif section == "settings":
            result = await completed_thread(self._settings)
            result["known_chats"] = await collect_known_chats(self.plugin)
            assets = await completed_thread(self._assets)
            result["profiles"] = [p["id"] for p in assets["profiles"]]
            result["profile_cards"] = assets["profiles"]
        elif section == "services":
            config = await completed_thread(self.plugin.runtime.services.load)
            await completed_thread(self._validate_services, config)
            result = {"configuration": config, "version": self._version(config)}
        elif section == "webhooks":
            config = await completed_thread(self.plugin.incoming.load)
            result = {"configuration": config, "version": self._version(config)}
        elif section == "memory":
            result = await completed_thread(self._memory, options)
            source_ids = result.pop("_source_chat_ids", None) or result.get("chats", [])
            result["chat_sources"] = await self.plugin._resolve_chat_sources(source_ids)
        elif section == "outputs":
            result = await completed_thread(self._outputs, options)
            source_ids = list(
                dict.fromkeys(
                    [
                        *result.get("chats", []),
                        *(
                            i.get("chat_id")
                            for i in [*result.get("items", []), *result.get("recent", [])]
                            if i.get("chat_id")
                        ),
                    ]
                )
            )
            result["chat_sources"] = await self.plugin._resolve_chat_sources(source_ids)
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
        elif section == "skill_file":
            result = await completed_thread(
                self.plugin.memory.read_skill_file,
                str(options.get("target") or "").strip(),
                str(options.get("path") or "").strip(),
            )
            if isinstance(result.get("content"), str):
                result["content"] = self.plugin.vault.redact(result["content"])
        elif section == "skill_package":
            async with self.lock:
                result = await completed_thread(self._transfer_skill, options)
        elif section == "recipe_draft":
            result = await completed_thread(
                build_recipe_draft,
                self.plugin,
                str(options.get("target") or "").strip(),
                options,
            )
        elif section == "recipe_package":
            async with self.lock:
                result = await completed_thread(self._transfer_recipe, options)
        elif section == "package":
            async with self.lock:
                result = await completed_thread(self._transfer, options)
        else:
            raise ValueError("未知管理查询")
        return {
            "success": True,
            "ui_style": self.plugin.settings.ui_style,
            **self.plugin.vault.redact(result),
        }

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

        filter_aliases: set[str] = set()
        if chat_id:
            resolved_filter = self.plugin.resolve_known_session_id(chat_id)
            filter_aliases = (
                {chat_id, resolved_filter}
                | self.plugin.chat_aliases_for(chat_id)
                | self.plugin.chat_aliases_for(resolved_filter)
            )
        chat_items = [i for i in items if not filter_aliases or i["chat_id"] in filter_aliases]
        chat_workspaces = [w for w in workspaces if not filter_aliases or w["chat_id"] in filter_aliases]
        stats = {
            "total": len(chat_items),
            "attachments": sum(1 for i in chat_items if i["kind"] == "attachment"),
            "workspace_out": sum(1 for i in chat_items if i["kind"] == "workspace_out"),
            "conclusions": sum(1 for i in chat_items if i["kind"] == "conclusion"),
            "pages": sum(1 for i in chat_items if i.get("media_kind") == "html"),
            "images": sum(1 for i in chat_items if i.get("media_kind") == "image"),
            "docs": sum(
                1
                for i in chat_items
                if i.get("media_kind") in {"markdown", "text", "binary"}
            ),
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
        recent = sorted(
            chat_items, key=lambda item: item.get("at", 0.0), reverse=True
        )[:5]
        for entry in (*filtered[:80], *recent):
            cid = str(entry.get("chat_id") or "")
            parsed_c = parse_stream_spec(cid)
            entry["stream_spec"] = (
                self.plugin.canonical_stream_spec(cid)
                or (format_stream_spec(*parsed_c) if parsed_c else "")
            )
        deduped_chats = {
            (
                "self"
                if str(c).strip() == "self"
                else (self.plugin.canonical_stream_spec(str(c).strip()) or str(c).strip())
            )
            for c in chats
            if str(c or "").strip()
        }
        return {
            "items": filtered[:80],
            "recent": recent,
            "workspaces": chat_workspaces[:60],
            "chats": sorted(deduped_chats),
            "stats": stats,
            "profile_fish": self._profile_fish_map(),
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
            if kind == "memory" and allowed.get("chat_id"):
                allowed["chat_id"] = await completed_thread(
                    self._resolve_memory_chat_id, str(allowed["chat_id"])
                )
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
            def _clone_and_seed():
                prof = plugin.profiles.clone(
                    options.get("base", "scout"), options["target"]
                )
                plugin.memory.seed_profile(prof.id)
                return prof

            profile = await completed_thread(_clone_and_seed)
            result = {"id": profile.id}
        elif action == "delete_profile":
            target = str(options.get("target") or "").strip()

            def _delete_and_purge():
                plugin.profiles.delete(target)
                plugin.memory.purge_profile(target)

            await completed_thread(_delete_and_purge)
            result = {"id": target}
        elif action == "create_skill":
            target = str(options.get("target") or "").strip()
            created = await completed_thread(
                plugin.memory.create_skill,
                target,
                base=str(options.get("base") or "").strip(),
                title=str(options.get("title") or "").strip(),
                description=str(options.get("description") or "").strip(),
                content=str(options.get("content") or ""),
                allow_custom=True,
            )
            result = {"id": created["target"], "version": created["version"]}
        elif action == "reset_skill":
            target = str(options.get("target") or "").strip()
            reset = await completed_thread(plugin.memory.reset_skill, target)
            result = {"target": target, "version": reset["version"]}
        elif action == "bind_skill":
            skill_id = str(options.get("skill") or "").strip()
            profile_id = str(options.get("profile") or "").strip()
            bound = options.get("bound")
            if not skill_id or not profile_id or type(bound) is not bool:
                raise ValueError("bind_skill 需要提供 skill、profile 与布尔值 bound")
            result = await completed_thread(
                self._set_profile_skill_binding, profile_id, skill_id, bound
            )
        elif action == "delete_skill":
            target = str(options.get("target") or "").strip()
            if target in plugin.memory.preset_skills():
                raise ValueError("预设技能不可删除，可点击「重置预设」恢复默认内容")

            def _unbind_and_delete():
                unbound = []
                for path in sorted(plugin.profiles.path.iterdir()):
                    if not path.is_dir() or path.is_symlink():
                        continue
                    try:
                        prof = plugin.profiles.get(path.name)
                    except (ValueError, OSError):
                        continue
                    if target in prof.skills:
                        self._set_profile_skill_binding(prof.id, target, False)
                        unbound.append(prof.id)
                plugin.memory.delete_skill(target)
                return unbound

            unbound = await completed_thread(_unbind_and_delete)
            result = {"target": target, "unbound": unbound}
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
            raw_settings = dict(options["settings"]) if isinstance(options.get("settings"), dict) else options["settings"]
            if isinstance(raw_settings, dict):
                if isinstance(raw_settings.get("enabled_chats"), list):
                    normalized_chats = []
                    for item in raw_settings["enabled_chats"]:
                        token = str(item or "").strip()
                        if not token:
                            continue
                        try:
                            token = (
                                await resolve_stream_spec(
                                    plugin, token, allow_unknown_ascii=True
                                )
                                or token
                            )
                        except ValueError:
                            pass
                        if token not in normalized_chats:
                            normalized_chats.append(token)
                    raw_settings["enabled_chats"] = normalized_chats
                if isinstance(raw_settings.get("chat_proactive_levels"), dict):
                    normalized_levels = {}
                    for k, v in raw_settings["chat_proactive_levels"].items():
                        key = str(k or "").strip()
                        if not key:
                            continue
                        try:
                            key = (
                                await resolve_stream_spec(
                                    plugin, key, allow_unknown_ascii=True
                                )
                                or key
                            )
                        except ValueError:
                            pass
                        normalized_levels[key] = v
                    raw_settings["chat_proactive_levels"] = normalized_levels
            settings = Settings.model_validate(raw_settings)
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
            await completed_thread(
                plugin.incoming.validate, config, strict_enabled=True
            )
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
            raw_chat_id = str(options.get("chat_id") or "").strip()
            if not raw_chat_id:
                raise ValueError("请填写目标会话 chat_id")
            explicit_platform = str(options.get("platform") or "").strip().lower()
            if raw_chat_id == "self":
                chat_id = "self"
                resolved_platform = "self"
            else:
                chat_id, resolved_platform = await resolve_chat_target(
                    plugin,
                    raw_chat_id,
                    current_platform=explicit_platform,
                    allow_unknown_ascii=True,
                )
            profile_name = str(options.get("profile") or "").strip()
            if profile_name:
                ability = await completed_thread(plugin.profiles.get, profile_name)
                if not ability.enabled:
                    raise ValueError("角色已停用，请先在「能力」页开启后再加入计划")
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
                "platform": explicit_platform or resolved_platform or "qq",
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
        elif action == "toggle_profile":
            target = str(options.get("target") or "").strip()
            enabled = options.get("enabled")
            if not target or type(enabled) is not bool:
                raise ValueError("toggle_profile 需要提供 target 与布尔值 enabled")
            current = await completed_thread(
                plugin.memory.read,
                "profile",
                target=target,
                document="profile.toml",
            )
            text = current["content"]
            replacement = f"enabled = {'true' if enabled else 'false'}"
            if re.search(r"(?m)^enabled\s*=.*$", text):
                updated = re.sub(r"(?m)^enabled\s*=.*$", replacement, text, count=1)
            else:
                updated = (text.rstrip("\n") + "\n" + replacement + "\n").lstrip("\n")
            expected = options.get("expected_version") or current["version"]
            saved = await completed_thread(
                plugin.memory.write,
                "profile",
                updated,
                target=target,
                document="profile.toml",
                expected_version=expected,
                allow_custom=True,
            )
            result = {
                "target": target,
                "enabled": enabled,
                "version": saved["version"],
            }
        elif action == "set_profile_fish":
            target = str(options.get("target") or options.get("profile") or "").strip()
            raw_seed = options.get("fish_seed", "")
            if not target or not isinstance(raw_seed, str):
                raise ValueError("set_profile_fish 需要提供 target 与字符串 fish_seed")
            result = await completed_thread(
                self._set_profile_fish, target, raw_seed.strip()
            )
        elif action == "import_skill":
            result = await completed_thread(self._upload_skill, options)
        elif action == "recipe_preview":
            result = await self._preview_recipe(options)
        elif action == "recipe_import":
            result = await completed_thread(self._import_recipe, options)
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
                or not is_absolute_program_path(command[0])
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
            self.plugin.memory.seed_profile(profile.id)
            return {"id": profile.id, "done": True}
        return {"token": token, "offset": len(transfer["data"])}

    def _upload_skill(self, options):
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
                "target": str(options.get("target") or "").strip(),
                "filename": str(options.get("filename") or "").strip(),
                "overwrite": options.get("overwrite", True) is not False,
                "expires": time.time() + 300,
                "direction": "upload_skill",
            }
        transfer = self.transfers.get(token)
        if (
            not transfer
            or transfer["direction"] != "upload_skill"
            or options.get("offset", 0) != len(transfer["data"])
        ):
            raise ValueError("导入位置无效或传输过期")
        data = base64.b64decode(options["data"], validate=True)
        if len(data) > 24576 or len(transfer["data"]) + len(data) > 8 * 1024**2:
            self.transfers.pop(token, None)
            raise ValueError("技能包过大")
        transfer["data"].extend(data)
        if options.get("done"):
            self.transfers.pop(token)
            imported = self.plugin.memory.import_skill_archive(
                bytes(transfer["data"]),
                target=transfer["target"],
                filename=transfer["filename"],
                overwrite=transfer["overwrite"],
            )
            return {**imported, "done": True}
        return {"token": token, "offset": len(transfer["data"])}

    def _stage_recipe_preview(self, clean_zip: bytes) -> str:
        now = time.time()
        self.recipe_previews = {
            k: v for k, v in self.recipe_previews.items() if v["expires"] > now
        }
        while len(self.recipe_previews) >= 6:
            oldest = min(self.recipe_previews, key=lambda k: self.recipe_previews[k]["expires"])
            self.recipe_previews.pop(oldest, None)
        ptoken = "rpv_" + uuid.uuid4().hex
        self.recipe_previews[ptoken] = {
            "data": clean_zip,
            "expires": now + 600,
        }
        return ptoken

    async def _preview_recipe(self, options):
        target_hint = str(options.get("target") or "").strip()
        url = str(options.get("url") or "").strip()
        if url:
            raw_bytes, fname, sub_hint = await completed_thread(
                fetch_public_recipe_bytes, url
            )
            disc = await self._discovery()
            clean_zip, preview = await completed_thread(
                build_recipe_preview,
                self.plugin,
                raw_bytes,
                filename=fname,
                subpath_hint=sub_hint,
                target_hint=target_hint,
                discovery=disc,
                source_url=url,
            )
            ptoken = self._stage_recipe_preview(clean_zip)
            return {"done": True, "preview_token": ptoken, **preview}

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
                "target": target_hint,
                "filename": str(options.get("filename") or "").strip(),
                "expires": time.time() + 300,
                "direction": "upload_recipe",
            }
        transfer = self.transfers.get(token)
        if (
            not transfer
            or transfer["direction"] != "upload_recipe"
            or options.get("offset", 0) != len(transfer["data"])
        ):
            raise ValueError("导入位置无效或传输过期")
        data = base64.b64decode(options["data"], validate=True)
        if len(data) > 24576 or len(transfer["data"]) + len(data) > 8 * 1024**2:
            self.transfers.pop(token, None)
            raise ValueError("配方包过大")
        transfer["data"].extend(data)
        if options.get("done"):
            self.transfers.pop(token, None)
            disc = await self._discovery()
            clean_zip, preview = await completed_thread(
                build_recipe_preview,
                self.plugin,
                bytes(transfer["data"]),
                filename=transfer["filename"],
                target_hint=transfer["target"],
                discovery=disc,
            )
            ptoken = self._stage_recipe_preview(clean_zip)
            return {"done": True, "preview_token": ptoken, **preview}
        return {"token": token, "offset": len(transfer["data"])}

    def _import_recipe(self, options):
        now = time.time()
        self.recipe_previews = {
            k: v for k, v in self.recipe_previews.items() if v["expires"] > now
        }
        ptoken = str(options.get("preview_token") or "").strip()
        if ptoken:
            staged = self.recipe_previews.get(ptoken)
            if not staged:
                raise ValueError("配方预览已过期，请重新点击「预览与对齐检查」")
            raw_bytes = staged["data"]
        elif options.get("data"):
            raw_bytes = base64.b64decode(str(options["data"]), validate=True)
        else:
            raise ValueError("请先执行配方预览或提供有效的配方包数据")
        res = import_recipe_archive(
            self.plugin,
            raw_bytes,
            target=str(options.get("target") or "").strip(),
            mode=str(options.get("mode") or "copy").strip(),
            install_skills=options.get("install_skills", True) is not False,
            overwrite_skills=options.get("overwrite_skills", False) is True,
            mount_routines=options.get("mount_routines")
            if isinstance(options.get("mount_routines"), list)
            else [],
            routine_chat_id=str(options.get("routine_chat_id") or "").strip(),
            routine_platform=str(options.get("routine_platform") or "qq").strip() or "qq",
        )
        if ptoken:
            self.recipe_previews.pop(ptoken, None)
        return {"done": True, **res}
