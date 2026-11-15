"""宿主认证管理页的静态 API；正文与秘密走各自的执行边界。"""

import asyncio
import base64
import copy
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import tomllib
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from .abilities import (
    FISH_SEED_RE,
    PROFILE_ID_RE,
    TASK_RE,
    TOOLSETS,
    _TOOLSET_LABELS,
    _serialize_profile_dict,
    _validate_instructions,
)
from .config import Settings, canonical_ui_style
from .console import snapshot
from .memory import PRESETS_DIR, SKILL_ID_RE, MemoryLayers, parse_skill_meta
from .pathsafe import is_absolute_program_path, path_is_link_like
from .shaping import collect_known_chats, resolve_chat_target, resolve_stream_spec
from .scout import call_capability
from .share import links_in
from .services import (
    validate_api_entry,
    validate_web_bindings,
)
from .trust import format_stream_spec, merge_admins, parse_stream_spec, validate_admin_update
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


_SLEEP_WINDOW_VALUE_RE = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?-(\d{1,2})(?::(\d{2}))?$"
)


def _normalize_sleep_window_setting(value) -> str:
    """校验并规范设置页的免打扰时段，完整保留分钟精度与 00-24。"""
    if not isinstance(value, str):
        raise ValueError("sleep_window 必须是字符串（如 01-08、23:30-07:00 或 00-24）")
    text = value.strip()
    if not text:
        return ""
    match = _SLEEP_WINDOW_VALUE_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            "sleep_window 格式应为 HH-HH 或 HH:MM-HH:MM（如 01-08、23:30-07:00 或 00-24）"
        )
    sh, sm_raw, eh, em_raw = match.groups()
    start_h = int(sh)
    start_m = int(sm_raw or 0)
    end_h = int(eh)
    end_m = int(em_raw or 0)
    if (
        not (0 <= start_h <= 24 and 0 <= end_h <= 24)
        or not (0 <= start_m <= 59 and 0 <= end_m <= 59)
        or (start_h == 24 and start_m != 0)
        or (end_h == 24 and end_m != 0)
    ):
        raise ValueError("sleep_window 小时范围为 0-24（24 时仅限 24 或 24:00），分钟为 00-59")
    if sm_raw is not None or em_raw is not None:
        return f"{start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}"
    return f"{start_h:02d}-{end_h:02d}"


def _validate_tz_offset_minutes_setting(value) -> int:
    """校验时区偏移（分钟），拒绝布尔与浮点并约束在 -720..840。"""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("tz_offset_minutes 必须是 -720 到 840 之间的整数分钟偏移")
    if isinstance(value, str):
        stripped = value.strip()
        if not re.fullmatch(r"[+-]?\d+", stripped):
            raise ValueError("tz_offset_minutes 必须是 -720 到 840 之间的整数分钟偏移")
        parsed = int(stripped)
    elif isinstance(value, int):
        parsed = value
    else:
        raise ValueError("tz_offset_minutes 必须是 -720 到 840 之间的整数分钟偏移")
    if not -720 <= parsed <= 840:
        raise ValueError("tz_offset_minutes 必须在 -720 到 840 分钟之间")
    return parsed


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

    @staticmethod
    def _serialize_profile(profile) -> dict:
        allowed_keys = {
            "id",
            "title",
            "name",
            "description",
            "icon_shape",
            "theme_color",
            "clone_from",
            "environment",
            "execution_mode",
            "permission_mode",
            "session_mode",
            "access",
            "model",
            "task",
            "model_name",
            "fallback_models",
            "tools",
            "toolsets",
            "web_provider",
            "network",
            "max_steps",
            "max_tokens",
            "context_tokens",
            "temperature",
            "bound",
            "enabled",
            "skills",
            "mcp",
            "apis",
            "secrets",
            "shared_secrets",
            "sandbox_targets",
            "allow_users",
            "allow_chats",
            "fish_seed",
        }
        data = {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in asdict(profile).items()
            if key in allowed_keys
        }
        mcp_list = list(profile.mcp)
        api_list = list(profile.apis)
        chats_list = list(profile.allow_chats)
        data["mcp"] = mcp_list
        data["mcp_servers"] = mcp_list
        data["apis"] = api_list
        data["connectors"] = list(dict.fromkeys([*mcp_list, *api_list]))
        data["allow_chats"] = chats_list
        data["enabled_chats"] = chats_list
        data["allowed_chats"] = chats_list
        data["model_list"] = list(profile.model_list)
        data["resolved_tools"] = list(profile.resolved_tools)
        return data

    def _assets(self):
        plugin = self.plugin
        profiles = []
        for path in sorted(plugin.profiles.path.iterdir()):
            if path.is_dir() and not path.is_symlink():
                try:
                    profile = plugin.profiles.get(path.name)
                    profiles.append(self._serialize_profile(profile))
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
        toolsets_meta = [
            {
                "id": ts_id,
                "label": _TOOLSET_LABELS.get(ts_id, ts_id),
                "tools": list(members),
            }
            for ts_id, members in TOOLSETS.items()
        ]
        handles = plugin.vault.handles()
        return {
            "profiles": profiles[:100],
            "toolsets": toolsets_meta,
            "skills": skill_ids,
            "skill_items": skill_items,
            "preset_skills": sorted(preset_set),
            "handles": handles,
            "vault_handles": handles,
            "vault_meta": plugin.vault.list_meta(),
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

    @staticmethod
    def _require_expected_version(
        options: dict, current_version, *, message: str, require_int: bool = False
    ) -> None:
        if "expected_version" not in options or options["expected_version"] is None:
            return
        expected = options["expected_version"]
        if require_int and type(expected) is not int:
            raise ValueError(message)
        if expected != current_version:
            raise ValueError(message)

    def _set_profile_models(self, options: dict) -> dict:
        profile_id = str(
            options.get("profile_id")
            or options.get("profile")
            or options.get("target")
            or ""
        ).strip()
        if not profile_id:
            raise ValueError("请指定要更新模型配置的角色")
        raw_models = options.get("models")
        if not isinstance(raw_models, list) or len(raw_models) > 16:
            raise ValueError("模型列表必须为最多 16 项的列表")
        cleaned: list[str] = []
        for entry in raw_models:
            if not isinstance(entry, str) or any(ch in entry for ch in ("\r", "\n", "\x00")):
                raise ValueError("模型名称格式无效")
            name = entry.strip()
            if not name or len(name) > 256:
                raise ValueError("模型名称不能为空且不超过 256 个字符")
            if self.plugin.vault.contains_secret(name.encode()):
                raise ValueError("模型名称不能包含凭据明文")
            if name not in cleaned:
                cleaned.append(name)
        slot: str | None = None
        if "fallback_slot" in options and options["fallback_slot"] is not None:
            raw_slot = options["fallback_slot"]
            if not isinstance(raw_slot, str):
                raise ValueError("默认回落模型类别格式无效")
            slot = raw_slot.strip()
            if slot and (
                not TASK_RE.fullmatch(slot)
                or self.plugin.vault.contains_secret(slot.encode())
            ):
                raise ValueError("默认回落模型类别格式无效")

        self.plugin.profiles.get(profile_id)
        toml_doc = self.plugin.memory.read(
            "profile", target=profile_id, document="profile.toml", allow_custom=True
        )
        self._require_expected_version(
            options,
            toml_doc["version"],
            message="角色配置版本已变化，请刷新后再保存",
            require_int=True,
        )
        cfg = tomllib.loads(toml_doc["content"]) if toml_doc["content"].strip() else {}
        cfg.pop("model", None)
        cfg.pop("model_list", None)
        if cleaned:
            cfg["model_name"] = cleaned[0]
            cfg["fallback_models"] = cleaned[1:]
        else:
            cfg["model_name"] = ""
            cfg["fallback_models"] = []
        if slot == "":
            cfg.pop("task", None)
        elif slot is not None:
            cfg["task"] = slot
        rendered = _serialize_profile_dict(cfg)
        saved = self.plugin.memory.write(
            "profile",
            rendered,
            target=profile_id,
            document="profile.toml",
            expected_version=toml_doc["version"],
            allow_custom=True,
        )
        try:
            updated = self.plugin.profiles.get(profile_id)
        except Exception:
            self.plugin.memory.write(
                "profile",
                toml_doc["content"],
                target=profile_id,
                document="profile.toml",
                expected_version=saved["version"],
                allow_custom=True,
            )
            raise
        models_out = list(updated.model_list)
        return {
            "profile_id": updated.id,
            "models": models_out,
            "model_list": models_out,
            "model_name": updated.model_name,
            "fallback_models": list(updated.fallback_models),
            "fallback_slot": updated.task,
            "task": updated.task,
            "version": saved["version"],
        }

    @staticmethod
    def _service_bucket(raw_kind) -> tuple[str, str]:
        kind = str(raw_kind or "").strip().lower()
        if kind == "mcp":
            return "mcp", "servers"
        raise ValueError("服务类型仅支持 mcp")

    def _upsert_service_item_sync(self, options: dict) -> dict:
        kind, bucket = self._service_bucket(options.get("kind") or "mcp")
        raw_item = options.get("item")
        if not isinstance(raw_item, dict):
            raise ValueError("服务项必须为结构化配置对象")
        name = str(
            options.get("name") or raw_item.get("name") or raw_item.get("id") or ""
        ).strip()
        self.plugin.vault._validate_handle(name)
        orig_name = str(options.get("orig_name") or "").strip()
        orig_kind = str(options.get("orig_kind") or "").strip()
        if orig_name:
            self.plugin.vault._validate_handle(orig_name)
        spec = {k: v for k, v in raw_item.items() if k not in {"name", "id"}}
        if isinstance(raw_item.get("spec"), dict) and set(spec) == {"spec"}:
            spec = dict(raw_item["spec"])

        current = self.plugin.runtime.services.load()
        self._require_expected_version(
            options,
            self._version(current),
            message="服务登记已变化，请刷新后重试",
        )
        next_config = copy.deepcopy(current)
        next_config.setdefault("servers", {})
        next_config.pop("templates", None)
        if orig_name:
            _, orig_bucket = (
                self._service_bucket(orig_kind) if orig_kind else (kind, bucket)
            )
            if (orig_bucket, orig_name) != (bucket, name):
                next_config.get(orig_bucket, {}).pop(orig_name, None)
        next_config[bucket][name] = spec
        self._validate_services(next_config)
        content = json.dumps(next_config, ensure_ascii=False).encode()
        atomic_private_write(self.plugin.runtime.services.path, content)
        return {
            "kind": kind,
            "name": name,
            "item": spec,
            "configuration": next_config,
            "version": self._version(next_config),
        }

    def _delete_service_item_sync(self, options: dict) -> dict:
        kind, bucket = self._service_bucket(options.get("kind") or "mcp")
        name = str(options.get("name") or options.get("id") or "").strip()
        if not name:
            raise ValueError("请指定要删除的服务名称")
        current = self.plugin.runtime.services.load()
        self._require_expected_version(
            options,
            self._version(current),
            message="服务登记已变化，请刷新后重试",
        )
        next_config = copy.deepcopy(current)
        next_config.pop("templates", None)
        entries = next_config.setdefault(bucket, {})
        if name not in entries:
            raise ValueError("指定的服务项不存在")
        entries.pop(name)
        self._validate_services(next_config)
        content = json.dumps(next_config, ensure_ascii=False).encode()
        atomic_private_write(self.plugin.runtime.services.path, content)
        return {
            "kind": kind,
            "name": name,
            "deleted": True,
            "configuration": next_config,
            "version": self._version(next_config),
        }

    def _undo_learning_note(self, options: dict) -> dict:
        profile_id = str(
            options.get("profile_id")
            or options.get("target")
            or options.get("profile")
            or ""
        ).strip()
        if not profile_id:
            raise ValueError("请指定要撤回笔记的角色")
        self.plugin.profiles.get(profile_id)
        current = self.plugin.memory.read("notes", target=profile_id)
        self._require_expected_version(
            options,
            current["version"],
            message="角色方法笔记版本已变化，请刷新后重试",
            require_int=True,
        )
        history = self.plugin.memory.history("notes", target=profile_id)
        raw_hid = options.get("history_id")
        hid = str(raw_hid).strip() if raw_hid is not None else ""

        restore_version: int | None = None
        reverted_tid: str | None = None
        if hid:
            ver_match = re.fullmatch(r"v?(\d+)", hid, re.IGNORECASE)
            if ver_match:
                ver = int(ver_match.group(1))
                if ver not in {h["version"] for h in history}:
                    raise ValueError("未找到指定的笔记历史版本")
                if ver == current["version"]:
                    raise ValueError("当前已是该历史版本，请选择更早的版本")
                restore_version = ver
                if (
                    len(history) >= 2
                    and history[1]["version"] == ver
                    and str(history[0].get("author") or "").startswith("thread:")
                ):
                    reverted_tid = str(history[0]["author"])[7:]
            else:
                matched_idx = next(
                    (
                        i
                        for i, h in enumerate(history)
                        if h["author"] in {hid, f"thread:{hid}"}
                    ),
                    None,
                )
                if matched_idx is None:
                    raise ValueError("未找到对应的学习笔记记录")
                author = str(history[matched_idx].get("author") or "")
                if author.startswith("thread:"):
                    reverted_tid = author[7:]
                if matched_idx + 1 < len(history):
                    restore_version = history[matched_idx + 1]["version"]
                elif author.startswith("thread:"):
                    restore_version = 0
                else:
                    raise ValueError("没有更早的笔记版本可撤回")
        elif len(history) >= 2:
            restore_version = history[1]["version"]
            top_author = str(history[0].get("author") or "")
            if top_author.startswith("thread:"):
                reverted_tid = top_author[7:]
        elif len(history) == 1 and str(history[0].get("author") or "").startswith("thread:"):
            restore_version = 0
            reverted_tid = str(history[0]["author"])[7:]
        else:
            raise ValueError("没有可撤回的笔记历史版本")

        if restore_version == 0:
            saved = self.plugin.memory.write(
                "notes",
                "",
                target=profile_id,
                expected_version=current["version"],
                author="rollback",
                allow_custom=True,
            )
        else:
            saved = self.plugin.memory.rollback(
                "notes",
                restore_version,
                target=profile_id,
                expected_version=current["version"],
                allow_custom=True,
            )
        if reverted_tid and getattr(self.plugin, "threads", None) is not None:
            with self.plugin.threads.connect() as db, db:
                db.execute(
                    "UPDATE learning_receipts SET status = 'reverted' WHERE thread_id = ? AND status = 'applied'",
                    (reverted_tid,),
                )
        return {
            "profile_id": profile_id,
            "target": profile_id,
            "content": saved["content"],
            "version": saved["version"],
            "restored_from": restore_version,
            "history": self.plugin.memory.history("notes", target=profile_id),
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
        known_aliases: set[str] = set()
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
            known_aliases.update(aliases)
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
            if token in known_aliases or norm in known_aliases:
                continue
            if parsed is not None:
                plat, ctype, tid = parsed
                new_aliases = {token, norm}
                known_aliases.update(new_aliases)
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
                        "aliases": new_aliases,
                    }
                )
            else:
                known_aliases.add(token)
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

    def _proactive_quota(self) -> dict[str, int]:
        limit = max(0, int(self.plugin.settings.daily_proactive_limit))
        used = 0
        intents = getattr(self.plugin, "intents", None)
        if intents is not None:
            now = intents.clock()
            start, end = intents._day_bounds(now)
            counts = {"group": 0, "private": 0}
            with intents.store.connect() as db:
                rows = db.execute(
                    "SELECT target, payload FROM intents WHERE kind='proactive' "
                    "AND source IN ('afterglow','serendipity','longing','later') "
                    "AND COALESCE(handled, due_at, created)>=? "
                    "AND COALESCE(handled, due_at, created)<? "
                    "AND status NOT IN ('cancelled','failed')",
                    (start, end),
                ).fetchall()
                arena_fn = getattr(intents, "_target_arena", None)
                for row in rows:
                    target = str(row["target"] if hasattr(row, "keys") else row[0])
                    raw_payload = row["payload"] if hasattr(row, "keys") else row[1]
                    try:
                        parsed_payload = (
                            json.loads(raw_payload) if raw_payload else None
                        )
                    except (TypeError, ValueError):
                        parsed_payload = None
                    if callable(arena_fn):
                        arena = arena_fn(
                            db,
                            target,
                            parsed_payload if isinstance(parsed_payload, dict) else None,
                        )
                    else:
                        arena = "private" if ":private:" in target else "group"
                    counts[arena if arena in counts else "group"] += 1
            used = max(counts.values())
        return {
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
        }

    @staticmethod
    def _intent_kind_label(
        kind: str, source: str, *, protected: bool, payload: dict
    ) -> str:
        is_recurring = bool(payload.get("every_seconds") or payload.get("daily_at"))
        if protected:
            return "管理员周期计划" if is_recurring else "管理员定时计划"
        source_labels = {
            "afterglow": "事后惦记",
            "serendipity": "冲浪偶遇",
            "longing": "久违问候",
            "later": "稍后提醒",
            "webhook": "外部事件",
            "workflow": "周期计划" if is_recurring else "定时任务",
            "thread": "线程回接" if kind == "return" else "线程跟进",
        }
        if source in source_labels:
            return source_labels[source]
        return {
            "return": "定时回接",
            "proactive": "主动关怀",
            "silent": "静默探索",
        }.get(kind, "待处理计划")

    def _admins_info(self) -> dict:
        info = getattr(self.plugin, "admins_info", None)
        if isinstance(info, dict):
            host_admins = list(info.get("host_admins") or [])
            settings_admins = list(info.get("settings_admins") or self.plugin.settings.admins)
        else:
            host_admins = []
            settings_admins = list(self.plugin.settings.admins)
        effective_admins = sorted(merge_admins(host_admins, settings_admins))
        return {
            "host_admins": sorted(host_admins),
            "settings_admins": list(settings_admins),
            "plugin_admins": list(settings_admins),
            "effective_admins": effective_admins,
        }

    def _docker_status(self) -> dict:
        runtime = getattr(self.plugin, "runtime", None)
        sb_cfg = getattr(self.plugin.settings, "sandbox", None)
        enabled = bool(getattr(sb_cfg, "enabled", False))
        image = str(getattr(sb_cfg, "image", "maisya-sandbox:latest") or "maisya-sandbox:latest")
        net_mode = str(getattr(sb_cfg, "network_mode", "none") or "none")
        available = bool(runtime.docker_available()) if runtime is not None else False
        err = ""
        if runtime is not None and getattr(runtime, "sandbox", None) is not None:
            err = str(getattr(runtime.sandbox, "error", "") or "")
        return {
            "enabled": enabled,
            "available": available,
            "ready": available,
            "image": image,
            "network_mode": net_mode,
            "error": err,
        }

    def _settings(self):
        settings = self.plugin.settings.model_dump()
        settings["ui_style"] = canonical_ui_style(settings.get("ui_style"))
        admins_info = self._admins_info()
        grants_store = getattr(self.plugin, "grants", None) or getattr(
            getattr(self.plugin, "runtime", None), "grants", None
        )
        grants_list = grants_store.list_grants() if grants_store is not None else []
        handles = self.plugin.vault.handles()
        vault_meta = self.plugin.vault.list_meta()
        docker = self._docker_status()
        plugin_enabled = bool(
            getattr(getattr(self.plugin.settings, "plugin", None), "enabled", False)
        )
        return {
            "settings": settings,
            "version": self._version(settings),
            "admins": admins_info["effective_admins"],
            "admins_info": admins_info,
            "permission_mode": settings.get("permission_mode", "ask_risky"),
            "ui_style": settings["ui_style"],
            "plugin_enabled": plugin_enabled,
            "sandbox_enabled": bool(docker.get("enabled", False)),
            "handles": handles,
            "vault_handles": handles,
            "vault_meta": vault_meta,
            "grants": grants_list,
            "docker": docker,
            "proactive_quota": self._proactive_quota(),
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
            "proactive_quota": self._proactive_quota(),
        }

    def _intents(self):
        with self.plugin.threads.connect() as db:
            intents = []
            for row in db.execute(
                "SELECT intent_id,kind,source,target,due_at,reason,payload,status,wake_status FROM intents ORDER BY created DESC LIMIT 60"
            ):
                item = dict(row)
                raw_payload = item.pop("payload", None)
                payload_obj: dict = {}
                if isinstance(raw_payload, str) and raw_payload.strip():
                    try:
                        parsed = json.loads(raw_payload)
                        if isinstance(parsed, dict):
                            payload_obj = parsed
                    except json.JSONDecodeError:
                        payload_obj = {}
                elif isinstance(raw_payload, dict):
                    payload_obj = raw_payload
                protected = bool(payload_obj.get("scheduled_admin") is True)
                item["protected"] = protected
                item["kind_label"] = self._intent_kind_label(
                    str(item.get("kind") or ""),
                    str(item.get("source") or ""),
                    protected=protected,
                    payload=payload_obj,
                )
                intents.append(item)
            return {
                "intents": intents,
                "learning": self._learning_receipts(db),
            }

    @staticmethod
    def _compact_receipt_text(text: str, limit: int = 80) -> str:
        """将换行与多余空白折叠为单行预览文本。"""
        compact = " ".join(str(text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: max(1, limit - 1)].rstrip() + "…"

    def _safe_redact(self, text: str) -> str:
        """经由凭据保险箱脱敏展示文本。"""
        if not text:
            return ""
        vault = getattr(self.plugin, "vault", None)
        return vault.redact(text) if vault is not None else text

    def _note_diff_excerpt(
        self,
        profile: str,
        matched: dict | None,
        prev: dict | None,
        fallback_summary: str,
    ) -> str:
        """提炼该次学习修订新增的笔记要点，失败时回落线程摘要。"""
        candidate = ""
        if profile and matched is not None and getattr(self.plugin, "memory", None) is not None:
            try:
                cur_text = str(
                    self.plugin.memory.read(
                        "notes", target=profile, version=matched["version"]
                    ).get("content")
                    or ""
                )
                prev_lines: set[str] = set()
                if prev is not None:
                    prev_text = str(
                        self.plugin.memory.read(
                            "notes", target=profile, version=prev["version"]
                        ).get("content")
                        or ""
                    )
                    prev_lines = {ln.strip() for ln in prev_text.splitlines() if ln.strip()}
                cur_lines = [ln.strip() for ln in cur_text.splitlines() if ln.strip()]
                added = [ln for ln in cur_lines if ln not in prev_lines]
                chosen = added[0] if added else (cur_lines[-1] if cur_lines else "")
                candidate = chosen.lstrip("-*• ").strip()
            except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error):
                candidate = ""
        text = candidate or fallback_summary
        return self._compact_receipt_text(self._safe_redact(text), limit=100)

    def _learning_receipts(self, db) -> list[dict]:
        """关联线程、消息与笔记版本，返回可展示且可撤回的学习回执。"""
        rows = db.execute(
            """
            SELECT
                lr.thread_id,
                lr.revision,
                lr.status,
                COALESCE(at.profile, '') AS profile,
                COALESCE(at.chat_id, '') AS chat_id,
                COALESCE(at.summary, '') AS thread_summary,
                COALESCE(at.updated, 0) AS updated,
                COALESCE(
                    (
                        SELECT te.payload
                        FROM thread_events te
                        WHERE te.thread_id = lr.thread_id AND te.kind = 'input'
                        ORDER BY te.seq ASC
                        LIMIT 1
                    ),
                    ''
                ) AS input_json
            FROM learning_receipts lr
            LEFT JOIN agent_threads at ON at.thread_id = lr.thread_id
            ORDER BY lr.rowid DESC
            LIMIT 30
            """
        ).fetchall()

        history_cache: dict[str, list[dict]] = {}
        title_cache: dict[str, str] = {}
        memory = getattr(self.plugin, "memory", None)
        profiles = getattr(self.plugin, "profiles", None)

        items: list[dict] = []
        for row in rows:
            tid = str(row["thread_id"] or "")
            status = str(row["status"] or "")
            profile = str(row["profile"] or "")
            chat_id = str(row["chat_id"] or "")
            thread_summary = str(row["thread_summary"] or "").strip()

            input_text = ""
            raw_json = str(row["input_json"] or "").strip()
            if raw_json:
                try:
                    parsed = json.loads(raw_json)
                    if isinstance(parsed, dict):
                        input_text = str(parsed.get("text") or "").strip()
                except ValueError:
                    input_text = ""

            if profile and profile not in history_cache:
                if memory is not None:
                    try:
                        history_cache[profile] = list(memory.history("notes", target=profile))
                    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error):
                        history_cache[profile] = []
                else:
                    history_cache[profile] = []
                prof_title = profile
                if profiles is not None:
                    try:
                        prof_title = profiles.get(profile).title or profile
                    except (ValueError, KeyError):
                        prof_title = profile
                title_cache[profile] = prof_title

            history = history_cache.get(profile, []) if profile else []
            matched_idx = next(
                (
                    i
                    for i, h in enumerate(history)
                    if h.get("author") in {tid, f"thread:{tid}"}
                ),
                None,
            )
            matched = history[matched_idx] if matched_idx is not None else None
            prev = (
                history[matched_idx + 1]
                if matched_idx is not None and matched_idx + 1 < len(history)
                else None
            )

            summary = self._note_diff_excerpt(profile, matched, prev, thread_summary)
            raw_topic = self._compact_receipt_text(self._safe_redact(input_text), limit=48)
            prof_label = title_cache.get(profile, profile)
            topic = raw_topic or summary or (f"{prof_label} 笔记修订" if prof_label else "自动笔记回执")

            raw_at = (matched or {}).get("updated") or row["updated"] or 0
            at = float(raw_at) if raw_at else 0.0
            can_undo = bool(profile and status == "applied" and matched is not None)

            items.append(
                {
                    "thread_id": tid,
                    "revision": int(row["revision"] or 0),
                    "status": status,
                    "profile": profile,
                    "chat_id": chat_id,
                    "topic": topic,
                    "summary": summary,
                    "version": matched["version"] if matched is not None else None,
                    "history_id": f"thread:{tid}" if can_undo else "",
                    "at": at,
                    "can_undo": can_undo,
                }
            )
        return items

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

    def _connectors_info(self) -> dict:
        config = self.plugin.runtime.services.load()
        servers = config.get("servers") if isinstance(config.get("servers"), dict) else {}
        apis = config.get("apis") if isinstance(config.get("apis"), dict) else {}
        mcp_items = [
            {
                "id": name,
                "name": name,
                "description": str(spec.get("description") or ""),
                "readonly": bool(spec.get("readonly", True)),
                "access": str(spec.get("access") or "admins"),
                "tools": list(spec.get("tools") or []),
                "credentials": sorted((spec.get("credentials") or {}).values())
                if isinstance(spec.get("credentials"), dict)
                else [],
            }
            for name, spec in sorted(servers.items())
            if isinstance(spec, dict)
        ]
        api_items = [
            {
                "id": name,
                "name": name,
                "api": str(spec.get("api") or name),
                "description": str(spec.get("description") or ""),
                "readonly": bool(spec.get("readonly", True)),
                "access": str(spec.get("access") or "admins"),
            }
            for name, spec in sorted(apis.items())
            if isinstance(spec, dict)
        ]
        web_items = [
            {"id": "auto", "label": "自动择优（宿主 / 内置）"},
            {"id": "host", "label": "宿主联网能力"},
            {"id": "google_search_plugin", "label": "Google 搜索插件"},
        ]
        return {
            "mcp": mcp_items,
            "plugin_apis": api_items,
            "web_providers": web_items,
            "web_provider": str(self.plugin.settings.web_provider or "auto"),
            "docker": self._docker_status(),
        }

    def _profile_detail_sync(self, profile_id: str) -> dict:
        pid = str(profile_id or "").strip()
        if not pid:
            raise ValueError("请指定档案标识 profile_id")
        prof = self.plugin.profiles.get(pid)
        md_res = self.plugin.memory.read("profile", target=prof.id, document="PROFILE.md")
        md_text = str(md_res.get("content") or "")
        notes_text = ""
        try:
            notes_res = self.plugin.memory.read("notes", target=prof.id)
            notes_text = str(notes_res.get("content") or "")
        except (ValueError, KeyError, OSError):
            notes_text = ""
        history = self.plugin.memory.history("profile", target=prof.id, document="PROFILE.md")
        assets = self._assets()
        return {
            "profile": self._serialize_profile(prof),
            "profile_md": md_text,
            "profile_md_lines": len(md_text.splitlines()),
            "profile_md_version": md_res.get("version"),
            "notes": notes_text,
            "history": history,
            "toolsets": assets["toolsets"],
            "skills": assets["skill_items"],
            "connectors": self._connectors_info(),
            "vault_handles": assets["vault_handles"],
        }

    def _library_sync(self, options: dict) -> dict:
        out = self._outputs(
            {
                "chat_id": str(options.get("chat_id") or ""),
                "kind": str(options.get("kind") or "all")
                if options.get("kind") in {None, "", "all", "attachment", "workspace_out", "conclusion"}
                else "all",
                "query": str(options.get("query") or ""),
            }
        )
        media_filter = str(options.get("media_kind") or "").strip().lower()
        profile_filter = str(options.get("profile") or "").strip()
        raw_limit = options.get("limit", 100)
        limit = max(1, min(int(raw_limit) if isinstance(raw_limit, int) else 100, 200))
        filtered = []
        for item in out.get("items", []):
            mk = str(item.get("media_kind") or "").lower()
            if media_filter and media_filter != "all":
                if media_filter == "html":
                    if mk not in {"html", "page"}:
                        continue
                elif media_filter == "doc":
                    if mk not in {"doc", "markdown", "text", "pdf", "conclusion"}:
                        continue
                elif mk != media_filter:
                    continue
            if profile_filter and str(item.get("profile") or "") != profile_filter:
                continue
            enriched = {
                **item,
                "size_bytes": int(item.get("bytes") or item.get("size_bytes") or 0),
                "filename": str(item.get("name") or item.get("path") or ""),
                "deliverable": bool(item.get("kind") == "attachment"),
            }
            filtered.append(enriched)
            if len(filtered) >= limit:
                break
        return {
            **out,
            "items": filtered,
            "total": len(filtered),
            "filters": {
                "chat_id": str(options.get("chat_id") or ""),
                "media_kind": media_filter or "all",
                "kind": str(options.get("kind") or "all"),
                "profile": profile_filter,
                "query": str(options.get("query") or ""),
            },
        }

    @staticmethod
    def _clean_virtual_path(raw_path: object) -> list[str]:
        text = str(raw_path or "").strip().replace("\\", "/")
        if not text:
            return []
        if text.startswith("/") or "\x00" in text:
            raise ValueError("路径格式无效，仅允许插件受管虚拟相对路径")
        parts = [p for p in text.split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            raise ValueError("路径不允许包含 .. 逃逸")
        if parts and ":" in parts[0] and len(parts) == 2 and parts[1] == "MEMORY.md":
            # Support convenience alias `<stream_spec>/MEMORY.md` -> `sessions/<stream_spec>/MEMORY.md`
            parts = ["sessions", parts[0], "MEMORY.md"]
        return parts

    def _safe_fs_resolve(self, root: Path, rel_parts: list[str]) -> Path:
        root_resolved = root.resolve()
        if path_is_link_like(root):
            raise ValueError("受管根目录不能是符号链接")
        cur = root
        for part in rel_parts:
            if not part or part in {".", ".."} or "\x00" in part:
                raise ValueError("路径包含非法片段")
            cur = cur / part
            if path_is_link_like(cur):
                raise ValueError("不允许经过或读取符号链接路径")
        resolved = cur.resolve()
        if not resolved.is_relative_to(root_resolved):
            raise ValueError("路径超出受管目录范围")
        return cur

    def _known_session_dirs(self) -> list[tuple[str, str]]:
        plugin = self.plugin
        chats: set[str] = set(plugin.settings.enabled_chats)
        with plugin.threads.connect() as db:
            for r in db.execute('SELECT DISTINCT chat_id FROM agent_threads WHERE owner="chat"'):
                if r[0]:
                    chats.add(str(r[0]))
            for r in db.execute('SELECT DISTINCT chat_id FROM memory_versions WHERE kind="memory"'):
                if r[0]:
                    chats.add(str(r[0]))
        seen: dict[str, str] = {}
        for raw_cid in sorted(chats):
            canon = plugin.canonical_stream_spec(raw_cid) or raw_cid
            if canon and canon != "self":
                seen.setdefault(canon, raw_cid)
        return sorted(seen.items())

    def _fs_list_sync(self, options: dict) -> dict:
        plugin = self.plugin
        parts = self._clean_virtual_path(options.get("path", ""))
        vpath = "/".join(parts)
        entries: list[dict] = []
        if not parts:
            for fname, kind in (("JOURNAL.md", "journal"), ("HEARTBEAT.md", "heartbeat")):
                fp = plugin.memory._locate(kind)[1]
                st = fp.stat() if fp.is_file() and not path_is_link_like(fp) else None
                entries.append(
                    {
                        "name": fname,
                        "path": fname,
                        "kind": "file",
                        "size": int(st.st_size) if st else 0,
                        "updated": float(st.st_mtime) if st else 0.0,
                        "writable": True,
                        "deletable": False,
                    }
                )
            chat_id = str(options.get("chat_id") or "").strip()
            if chat_id:
                resolved_cid = self._resolve_memory_chat_id(chat_id)
                mfp = plugin.memory._locate("memory", chat_id=resolved_cid)[1]
                mst = mfp.stat() if mfp.is_file() and not path_is_link_like(mfp) else None
                entries.append(
                    {
                        "name": "MEMORY.md",
                        "path": "MEMORY.md",
                        "kind": "file",
                        "size": int(mst.st_size) if mst else 0,
                        "updated": float(mst.st_mtime) if mst else 0.0,
                        "writable": True,
                        "deletable": False,
                    }
                )
            for dname in ("sessions", "profiles", "skills", "workspaces"):
                entries.append(
                    {
                        "name": dname,
                        "path": dname,
                        "kind": "dir",
                        "size": 0,
                        "updated": 0.0,
                        "writable": dname in {"profiles", "skills", "sessions"},
                        "deletable": False,
                    }
                )
            return {"path": "", "entries": entries}

        top = parts[0]
        if top == "sessions":
            if len(parts) == 1:
                for canon, raw_cid in self._known_session_dirs():
                    entries.append(
                        {
                            "name": canon,
                            "path": f"sessions/{canon}",
                            "kind": "dir",
                            "size": 0,
                            "updated": 0.0,
                            "writable": True,
                            "deletable": False,
                        }
                    )
                return {"path": vpath, "entries": entries}
            if len(parts) == 2:
                resolved_cid = self._resolve_memory_chat_id(parts[1])
                mfp = plugin.memory._locate("memory", chat_id=resolved_cid)[1]
                mst = mfp.stat() if mfp.is_file() and not path_is_link_like(mfp) else None
                entries.append(
                    {
                        "name": "MEMORY.md",
                        "path": f"sessions/{parts[1]}/MEMORY.md",
                        "kind": "file",
                        "size": int(mst.st_size) if mst else 0,
                        "updated": float(mst.st_mtime) if mst else 0.0,
                        "writable": True,
                        "deletable": False,
                    }
                )
                return {"path": vpath, "entries": entries}
            raise ValueError("目录不存在")

        root_map = {
            "profiles": plugin.profiles.path,
            "skills": plugin.data_root / "abilities/skills",
            "workspaces": plugin.runtime.files.root,
        }
        if top not in root_map:
            raise ValueError("不支持列举的系统目录")
        base_root = root_map[top]
        target_dir = self._safe_fs_resolve(base_root, parts[1:])
        if not target_dir.is_dir():
            raise ValueError("目标目录不存在")
        preset_skills = plugin.memory.preset_skills()
        for child in sorted(target_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith(".") or path_is_link_like(child):
                continue
            rel = f"{vpath}/{child.name}"
            is_dir = child.is_dir()
            st = child.stat()
            deletable = False
            if not is_dir:
                if top == "profiles" and len(parts) >= 2 and parts[1] != "default" and child.name not in {"PROFILE.md", "profile.toml"}:
                    deletable = True
                elif top == "skills" and len(parts) >= 2 and parts[1] not in preset_skills and child.name != "SKILL.md":
                    deletable = True
            entries.append(
                {
                    "name": child.name,
                    "path": rel,
                    "kind": "dir" if is_dir else "file",
                    "size": 0 if is_dir else int(st.st_size),
                    "updated": float(st.st_mtime),
                    "writable": top in {"profiles", "skills"} and not is_dir,
                    "deletable": deletable,
                }
            )
        return {"path": vpath, "entries": entries}

    def _fs_read_sync(self, options: dict) -> dict:
        plugin = self.plugin
        parts = self._clean_virtual_path(options.get("path", ""))
        if not parts:
            raise ValueError("请指定要读取的文件路径 path")
        vpath = "/".join(parts)
        if parts == ["JOURNAL.md"] or parts == ["memory", "self", "JOURNAL.md"]:
            res = plugin.memory.read("journal")
            return {
                "path": vpath,
                "content": str(res.get("content") or ""),
                "size": len(str(res.get("content") or "").encode("utf-8")),
                "updated": float(res.get("updated") or 0.0),
                "version": res.get("version"),
                "expected_version": res.get("version"),
                "writable": True,
                "truncated": False,
            }
        if parts == ["HEARTBEAT.md"] or parts == ["memory", "self", "HEARTBEAT.md"]:
            res = plugin.memory.read("heartbeat")
            return {
                "path": vpath,
                "content": str(res.get("content") or ""),
                "size": len(str(res.get("content") or "").encode("utf-8")),
                "updated": float(res.get("updated") or 0.0),
                "version": res.get("version"),
                "expected_version": res.get("version"),
                "writable": True,
                "truncated": False,
            }
        if parts == ["MEMORY.md"]:
            raw_cid = str(options.get("chat_id") or "").strip()
            if not raw_cid and plugin.settings.enabled_chats:
                raw_cid = str(plugin.settings.enabled_chats[0])
            if not raw_cid:
                raise ValueError("读取 MEMORY.md 请指定 chat_id 或使用 sessions/<chat>/MEMORY.md")
            cid = self._resolve_memory_chat_id(raw_cid)
            res = plugin.memory.read("memory", chat_id=cid)
            return {
                "path": vpath,
                "chat_id": cid,
                "content": str(res.get("content") or ""),
                "size": len(str(res.get("content") or "").encode("utf-8")),
                "updated": float(res.get("updated") or 0.0),
                "version": res.get("version"),
                "expected_version": res.get("version"),
                "writable": True,
                "truncated": False,
            }
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "MEMORY.md":
            cid = self._resolve_memory_chat_id(parts[1])
            res = plugin.memory.read("memory", chat_id=cid)
            return {
                "path": vpath,
                "chat_id": cid,
                "content": str(res.get("content") or ""),
                "size": len(str(res.get("content") or "").encode("utf-8")),
                "updated": float(res.get("updated") or 0.0),
                "version": res.get("version"),
                "expected_version": res.get("version"),
                "writable": True,
                "truncated": False,
            }
        if len(parts) == 3 and parts[0] == "profiles" and parts[2] in {"PROFILE.md", "profile.toml", "NOTES.md"}:
            if parts[2] == "NOTES.md":
                res = plugin.memory.read("notes", target=parts[1])
            else:
                res = plugin.memory.read("profile", target=parts[1], document=parts[2])
            return {
                "path": vpath,
                "content": str(res.get("content") or ""),
                "size": len(str(res.get("content") or "").encode("utf-8")),
                "updated": float(res.get("updated") or 0.0),
                "version": res.get("version"),
                "expected_version": res.get("version"),
                "writable": True,
                "truncated": False,
            }
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            res = plugin.memory.read("skill", target=parts[1])
            return {
                "path": vpath,
                "content": str(res.get("content") or ""),
                "size": len(str(res.get("content") or "").encode("utf-8")),
                "updated": float(res.get("updated") or 0.0),
                "version": res.get("version"),
                "expected_version": res.get("version"),
                "writable": True,
                "truncated": False,
            }

        root_map = {
            "profiles": plugin.profiles.path,
            "skills": plugin.data_root / "abilities/skills",
            "workspaces": plugin.runtime.files.root,
        }
        top = parts[0]
        if top not in root_map or len(parts) < 2:
            raise ValueError("不支持读取该系统路径")
        fpath = self._safe_fs_resolve(root_map[top], parts[1:])
        if not fpath.is_file():
            raise ValueError("文件不存在")
        raw = fpath.read_bytes()
        truncated = len(raw) > 65536
        text = plugin.vault.redact(raw[:65536].decode("utf-8", errors="replace"))
        st = fpath.stat()
        ver = hashlib.sha256(raw).hexdigest()[:16]
        return {
            "path": vpath,
            "content": text,
            "size": int(st.st_size),
            "updated": float(st.st_mtime),
            "version": ver,
            "expected_version": ver,
            "writable": top in {"profiles", "skills"} and not truncated,
            "truncated": truncated,
        }

    def _fs_write_sync(self, options: dict) -> dict:
        plugin = self.plugin
        parts = self._clean_virtual_path(options.get("path", ""))
        content = options.get("content")
        if not parts or not isinstance(content, str):
            raise ValueError("写入系统文件需要 path 与文本 content")
        vpath = "/".join(parts)
        raw_bytes = content.encode("utf-8")
        if len(raw_bytes) > 65536 or plugin.vault.contains_secret(raw_bytes):
            raise ValueError("文件内容过大或包含已知敏感凭据，禁止明文写入")
        expected = options.get("expected_version")

        def _write_memory(kind, **kw):
            cur = plugin.memory.read(kind, **kw)
            exp = expected if isinstance(expected, int) else cur["version"]
            saved = plugin.memory.write(
                kind, content, expected_version=exp, allow_custom=True, **kw
            )
            return {
                "path": vpath,
                "size": len(raw_bytes),
                "version": saved["version"],
                "updated": float(time.time()),
            }

        if parts == ["JOURNAL.md"] or parts == ["memory", "self", "JOURNAL.md"]:
            return _write_memory("journal")
        if parts == ["HEARTBEAT.md"] or parts == ["memory", "self", "HEARTBEAT.md"]:
            return _write_memory("heartbeat")
        if parts == ["MEMORY.md"]:
            raw_cid = str(options.get("chat_id") or "").strip()
            if not raw_cid and plugin.settings.enabled_chats:
                raw_cid = str(plugin.settings.enabled_chats[0])
            if not raw_cid:
                raise ValueError("写入 MEMORY.md 请指定 chat_id 或使用 sessions/<chat>/MEMORY.md")
            cid = self._resolve_memory_chat_id(raw_cid)
            return _write_memory("memory", chat_id=cid)
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "MEMORY.md":
            cid = self._resolve_memory_chat_id(parts[1])
            return _write_memory("memory", chat_id=cid)
        if len(parts) == 3 and parts[0] == "profiles" and parts[2] in {"PROFILE.md", "profile.toml", "NOTES.md"}:
            if parts[2] == "PROFILE.md":
                _validate_instructions(content)
            if parts[2] == "NOTES.md":
                return _write_memory("notes", target=parts[1])
            return _write_memory("profile", target=parts[1], document=parts[2])
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            return _write_memory("skill", target=parts[1])

        root_map = {
            "profiles": plugin.profiles.path,
            "skills": plugin.data_root / "abilities/skills",
        }
        top = parts[0]
        if top not in root_map or len(parts) < 3:
            raise ValueError("该系统路径为只读或不允许直接覆盖写入")
        if top == "profiles" and not PROFILE_ID_RE.fullmatch(parts[1]):
            raise ValueError("档案编号无效")
        if top == "skills" and not SKILL_ID_RE.fullmatch(parts[1]):
            raise ValueError("技能编号无效")
        fpath = self._safe_fs_resolve(root_map[top], parts[1:])
        fpath.parent.mkdir(parents=True, exist_ok=True)
        atomic_private_write(fpath, raw_bytes)
        ver = hashlib.sha256(raw_bytes).hexdigest()[:16]
        return {
            "path": vpath,
            "size": len(raw_bytes),
            "version": ver,
            "updated": float(time.time()),
        }

    def _fs_delete_sync(self, options: dict) -> dict:
        plugin = self.plugin
        parts = self._clean_virtual_path(options.get("path", ""))
        if len(parts) < 2:
            raise ValueError("系统核心记忆目录或顶层文件不可删除")
        vpath = "/".join(parts)
        top = parts[0]
        if top == "profiles":
            pid = parts[1]
            if pid == "default":
                raise ValueError("内置默认档案不可删除")
            if len(parts) == 2:
                plugin.profiles.delete(pid)
                plugin.memory.purge_profile(pid)
                return {"deleted": True, "path": vpath}
            if parts[2] in {"PROFILE.md", "profile.toml"}:
                raise ValueError("不可单独删除档案核心文件")
            target = self._safe_fs_resolve(plugin.profiles.path, parts[1:])
            if not target.exists():
                raise ValueError("目标文件不存在")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return {"deleted": True, "path": vpath}
        if top == "skills":
            sid = parts[1]
            preset_skills = plugin.memory.preset_skills()
            if sid in preset_skills and (len(parts) == 2 or parts[2] == "SKILL.md"):
                raise ValueError("预设技能核心文件不可删除")
            if len(parts) == 2:
                plugin.memory.delete_skill(sid)
                return {"deleted": True, "path": vpath}
            if parts[2] == "SKILL.md":
                raise ValueError("不可单独删除 SKILL.md")
            target = self._safe_fs_resolve(plugin.data_root / "abilities/skills", parts[1:])
            if not target.exists():
                raise ValueError("目标文件不存在")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            return {"deleted": True, "path": vpath}
        raise ValueError("仅支持删除非预设技能附属文件或自定义档案文件")

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
                    return await asyncio.wait_for(
                        call_capability(ctx, capability, timeout_ms=2500, **kwargs),
                        timeout=3.0,
                    )
                except Exception:
                    return default

            probe_entries: list[dict] = []
            probe_tasks: dict[str, list[str]] = {}

            raw_models, raw_stats = await asyncio.gather(
                probe("llm.get_available_models", {}),
                probe("statistics.local.models", {}, top_models=100),
            )
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
            self._model_catalog_cache = {"expires": now + 60.0, "probe_data": cached_probe}

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

    _BATCHABLE_SECTIONS = frozenset(
        {
            "now_detail",
            "abilities",
            "profiles",
            "profile_detail",
            "skills",
            "connectors",
            "library",
            "fs_list",
            "fs_read",
            "asset",
            "settings",
            "services",
            "memory",
            "outputs",
            "intents",
            "discovery",
            "health",
            "vault_list",
            "grants",
            "onboarding",
            "models",
        }
    )

    async def _read_profiles_payload(self) -> dict:
        result = await completed_thread(self._assets)
        result["known_chats"] = await completed_thread(self._known_chats_sync)
        host_models = await self._collect_host_models()
        result["available_models"] = host_models["names"]
        result["model_catalog"] = host_models["models"]
        result["model_tasks"] = host_models["tasks"]
        result["default_model"] = str(self.plugin.settings.default_model or "")
        result["connectors"] = await completed_thread(self._connectors_info)
        result["docker"] = await completed_thread(self._docker_status)
        result["admins_info"] = await completed_thread(self._admins_info)
        result["permission_mode"] = str(self.plugin.settings.permission_mode or "ask_risky")
        return result

    def _enrich_onboarding_view(self, raw_view: dict | None) -> dict:
        if hasattr(self.plugin, "enrich_onboarding_view"):
            return self.plugin.enrich_onboarding_view(raw_view)
        out = dict(raw_view or {})
        mv = out.get("model_verify") if isinstance(out.get("model_verify"), dict) else {}
        out["run_id"] = str(out.get("run_id") or mv.get("run_id") or "")
        raw_seq = out.get("sequence")
        out["sequence"] = int(raw_seq if raw_seq is not None else (mv.get("sequence") or 0))
        admins_info = self._admins_info()
        out["host_admins"] = list(admins_info.get("host_admins") or [])
        out["plugin_admins"] = list(admins_info.get("plugin_admins") or [])
        out["settings_admins"] = list(admins_info.get("settings_admins") or [])
        out["effective_admins"] = list(admins_info.get("effective_admins") or [])
        out["enabled_chats"] = list(self.plugin.settings.enabled_chats)
        out["known_chats"] = [
            str(item.get("stream_spec") or item.get("chat_id") or "")
            for item in self._known_chats_sync()
            if item.get("stream_spec") or item.get("chat_id")
        ]
        out["recent_dms"] = []
        out["default_model"] = str(self.plugin.settings.default_model or "")
        out["permission_mode"] = str(self.plugin.settings.permission_mode or "ask_risky")
        out["plugin_enabled"] = bool(
            getattr(getattr(self.plugin.settings, "plugin", None), "enabled", False)
        )
        out["sandbox_enabled"] = bool(getattr(self.plugin.settings.sandbox, "enabled", False))
        models = out.get("models")
        out["available_models"] = (
            [
                str(m.get("name") if isinstance(m, dict) else m or "")
                for m in models
                if (m.get("name") if isinstance(m, dict) else m)
            ]
            if isinstance(models, list)
            else []
        )
        return out

    async def _onboarding_view_payload(self) -> dict:
        if hasattr(self.plugin, "onboarding_view"):
            return await self.plugin.onboarding_view(is_admin_view=True)
        mgr = getattr(self.plugin, "onboarding", None)
        if mgr is None:
            return self._enrich_onboarding_view({"show": False, "usable": True})
        host_models = await self._collect_host_models()
        admins_info = await completed_thread(self._admins_info)
        services_cfg = await completed_thread(self.plugin.runtime.services.load)
        docker = await completed_thread(self._docker_status)
        with self.plugin.threads.connect() as db:
            row = db.execute("SELECT COUNT(*) AS n FROM agent_threads").fetchone()
            thread_count = int(row["n"] if row else 0)
        raw_view = await completed_thread(
            mgr.view,
            settings=self.plugin.settings,
            host_model_catalog=host_models["models"],
            host_admins=admins_info["host_admins"],
            mcp_servers=services_cfg.get("servers") or {},
            docker_status=docker,
            existing_threads_count=thread_count,
            is_admin_view=True,
            default_profile_ready=(self.plugin.profiles.path / "default" / "profile.toml").is_file(),
            profiles_path=self.plugin.profiles.path,
        )
        return await completed_thread(self._enrich_onboarding_view, raw_view)

    async def _discovery(self, options=None):
        ctx = getattr(self.plugin, "ctx", None)

        async def probe(capability, default, **kwargs):
            if ctx is None:
                return default
            try:
                return await call_capability(ctx, capability, **kwargs)
            except Exception:
                return default

        force_refresh = bool((options or {}).get("force"))
        raw_streams, host_models, raw_plugins, raw_apis = await asyncio.gather(
            probe("chat.get_all_streams", [], platform="all_platforms"),
            self._collect_host_models(force=force_refresh, probe_host=True),
            probe("component.list_loaded_plugins", []),
            probe("api.list", []),
        )
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
        plugins = (
            raw_plugins.get("plugins") or []
            if isinstance(raw_plugins, dict)
            else raw_plugins
        )
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

    async def _read_section(self, section: str, options: dict) -> dict:
        if section == "now_detail":
            tid = str(options.get("thread_id") or "").strip()
            cid = str(options.get("chat_id") or "").strip()
            cursor = int(options.get("cursor") or 0)
            filter_chat = (
                await completed_thread(self.plugin.resolve_known_session_id, cid)
                if cid and cid != "self"
                else cid
            )
            snap = await completed_thread(
                snapshot,
                self.plugin.threads,
                thread_id=tid,
                chat_id=filter_chat,
                cursor=cursor,
                context_limit=self.plugin.settings.thread_context_chars,
                context_tokens=self.plugin.settings.thread_context_tokens,
                model_catalog=self.plugin.get_model_catalog(),
                redact=self.plugin.vault.redact,
                memory=self.plugin.memory,
                workspace_root=self.plugin.data_root / "workspaces",
            )
            sel = snap.get("selected") if isinstance(snap, dict) else None
            chat_ids = list(
                dict.fromkeys(
                    [
                        cid,
                        *(t.get("chat_id") for t in snap.get("threads", []) if t.get("chat_id")),
                        sel.get("chat_id") if isinstance(sel, dict) else "",
                    ]
                )
            )
            chat_sources = await self.plugin._resolve_chat_sources([c for c in chat_ids if c])
            result = {
                **snap,
                "chat_sources": chat_sources,
                "chat_meta": chat_sources,
                "default_model": str(self.plugin.settings.default_model or ""),
                "plugin_enabled": bool(
                    getattr(getattr(self.plugin.settings, "plugin", None), "enabled", False)
                ),
                "sandbox_enabled": bool(getattr(self.plugin.settings.sandbox, "enabled", False)),
                "thread_id": tid or (sel.get("thread_id") if isinstance(sel, dict) else ""),
                "thread": sel,
                "steps": sel.get("steps", []) if isinstance(sel, dict) else [],
                "pending_approval": sel.get("pending_approval") if isinstance(sel, dict) else None,
                "artifacts": sel.get("artifacts", []) if isinstance(sel, dict) else [],
                "context": sel.get("context") if isinstance(sel, dict) else None,
            }
        elif section in {"abilities", "profiles"}:
            result = await self._read_profiles_payload()
        elif section == "profile_detail":
            pid = str(
                options.get("profile_id")
                or options.get("id")
                or options.get("target")
                or ""
            ).strip()
            result = await completed_thread(self._profile_detail_sync, pid)
            host_models = await self._collect_host_models()
            result["models"] = host_models["names"]
            result["available_models"] = host_models["names"]
            result["model_catalog"] = host_models["models"]
            result["model_tasks"] = host_models["tasks"]
        elif section == "skills":
            assets = await completed_thread(self._assets)
            result = {
                "skills": assets["skill_items"],
                "skill_map": assets["skills"],
                "preset_skills": assets["preset_skills"],
                "profiles": [p["id"] for p in assets["profiles"]],
            }
        elif section == "connectors":
            result = await completed_thread(self._connectors_info)
        elif section == "library":
            result = await completed_thread(self._library_sync, options)
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
        elif section == "fs_list":
            result = await completed_thread(self._fs_list_sync, options)
        elif section == "fs_read":
            result = await completed_thread(self._fs_read_sync, options)
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
            known_chat_rows = await collect_known_chats(self.plugin)
            result["known_chats"] = known_chat_rows
            result["known_chat_specs"] = [
                str(c.get("stream_spec") or c.get("chat_id") or "").strip()
                for c in known_chat_rows
                if isinstance(c, dict) and (c.get("stream_spec") or c.get("chat_id"))
            ]
            assets = await completed_thread(self._assets)
            result["profiles"] = [p["id"] for p in assets["profiles"]]
            result["profile_cards"] = assets["profiles"]
            host_models = await self._collect_host_models()
            result["available_models"] = host_models["names"]
            result["model_catalog"] = host_models["models"]
            result["model_tasks"] = host_models["tasks"]
        elif section == "services":
            config = await completed_thread(self.plugin.runtime.services.load)
            if isinstance(config, dict):
                config.pop("templates", None)
            await completed_thread(self._validate_services, config)
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
            result = await self._discovery(options)
        elif section == "health":
            docker = await completed_thread(self._docker_status)
            sb = getattr(self.plugin.runtime, "sandbox", None)
            containers = getattr(sb, "containers", None) if sb is not None else None
            result = {
                "enabled": docker["enabled"],
                "ready": docker["ready"],
                "available": docker["available"],
                "error": docker["error"],
                "running": len(containers) if isinstance(containers, (dict, list, set, tuple)) else 0,
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
        elif section == "package":
            async with self.lock:
                result = await completed_thread(self._transfer, options)
        elif section == "vault_list":
            handles = await completed_thread(self.plugin.vault.handles)
            items = await completed_thread(self.plugin.vault.list_meta)
            result = {"items": items, "handles": handles}
        elif section == "grants":
            scope = str(options.get("scope") or "").strip()
            grants_store = getattr(self.plugin, "grants", None) or getattr(
                getattr(self.plugin, "runtime", None), "grants", None
            )
            items = (
                await completed_thread(grants_store.list_grants, scope=scope or None)
                if grants_store is not None
                else []
            )
            result = {"items": items}
        elif section == "onboarding":
            view = await self._onboarding_view_payload()
            result = {**view, "onboarding": view}
        elif section == "models":
            force_refresh = bool(options.get("force"))
            host_models = await self._collect_host_models(
                force=force_refresh, probe_host=True
            )
            result = {
                "models": host_models["models"],
                "available_models": host_models["names"],
                "tasks": host_models["tasks"],
            }
        else:
            raise ValueError("未知管理查询")
        return result

    async def _read_batch_section(self, key: str, opts: dict) -> dict:
        try:
            value = await self._read_section(key, opts)
        except Exception as exc:
            return {"success": False, "error": str(exc) or "读取失败"}
        if isinstance(value, dict):
            return {"success": True, **value}
        return {"success": True, "data": value}

    async def _read(self, section, payload):
        options = self.arguments(payload)
        if section == "batch":
            sections_map = options.get("sections")
            if not isinstance(sections_map, dict) or not (1 <= len(sections_map) <= 6):
                raise ValueError("非法的批量读取请求")
            ordered_items: list[tuple[str, dict]] = []
            for key, value in sections_map.items():
                if not isinstance(key, str) or key not in self._BATCHABLE_SECTIONS:
                    raise ValueError("非法的批量读取请求")
                if value is not None and not isinstance(value, dict):
                    raise ValueError("非法的批量读取请求")
                ordered_items.append((key, dict(value or {})))
            values = await asyncio.gather(
                *(self._read_batch_section(key, opts) for key, opts in ordered_items)
            )
            result = {
                "sections": {
                    key: val for (key, _), val in zip(ordered_items, values)
                }
            }
        else:
            result = await self._read_section(section, options)
        return {
            "success": True,
            "ui_style": canonical_ui_style(self.plugin.settings.ui_style),
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
        att_by_key: dict[tuple[str, str, int], dict] = {}
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
            t_info = thread_rows.get(row["thread_id"], {})
            media_kind, mime = _media_kind(row["name"])
            entry = {
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
            att_by_key[(row["thread_id"], row["name"], row["size"])] = entry
            items.append(entry)

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
                            existing_att = att_by_key.get(
                                (meta["thread_id"], base_name, size)
                            )
                            if existing_att is not None:
                                existing_att["in_workspace_out"] = True
                                existing_att.setdefault("workspace_id", ws_id)
                                existing_att.setdefault("workspace_name", meta["name"])
                                existing_att.setdefault("path", rel)
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
            "workspace_out": sum(
                1
                for i in chat_items
                if i["kind"] == "workspace_out" or i.get("in_workspace_out")
            ),
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
        if kind_filter == "workspace_out":
            filtered = [
                i
                for i in filtered
                if i["kind"] == "workspace_out" or i.get("in_workspace_out")
            ]
        elif kind_filter != "all":
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
        if kind_filter == "workspace_out":
            filtered.sort(
                key=lambda item: (
                    0 if item.get("kind") == "workspace_out" else 1,
                    -float(item.get("at") or 0.0),
                )
            )
        else:
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
        opts = dict(options or {})
        kind = str(opts.get("kind") or "").strip()
        if kind == "workspace_out":
            kind = "workspace_file"
        art_id = str(opts.get("artifact_id") or opts.get("id") or "").strip()
        if not kind:
            if opts.get("handle") or art_id.startswith("att:") or art_id.startswith("att_"):
                kind = "attachment"
            elif (opts.get("workspace_id") and opts.get("path")) or (
                art_id.startswith("ws:") and len(art_id.split(":", 2)) == 3
            ):
                kind = "workspace_file"
            elif opts.get("thread_id") or art_id.startswith("con:"):
                kind = "conclusion"
        if kind == "attachment" and not opts.get("handle") and art_id:
            opts["handle"] = art_id[4:] if art_id.startswith("att:") else art_id
        elif (
            kind == "workspace_file"
            and (not opts.get("workspace_id") or not opts.get("path"))
            and art_id.startswith("ws:")
        ):
            parts = art_id.split(":", 2)
            if len(parts) == 3:
                opts.setdefault("workspace_id", parts[1])
                opts.setdefault("path", parts[2])
        elif kind == "conclusion" and not opts.get("thread_id") and art_id.startswith("con:"):
            opts["thread_id"] = art_id[4:]

        offset = opts.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 128 * 1024**2:
            raise ValueError("预览偏移量无效")

        if kind == "attachment":
            handle = opts.get("handle", "")
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
            vdir, ws_meta = self._resolve_workspace_dir(opts.get("workspace_id", ""))
            if vdir is None:
                raise ValueError("工作区尚无已提交文件")
            rel_path = opts.get("path", "")
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
            thread_id = opts.get("thread_id", "")
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

    async def _persist_settings(self, raw_settings) -> Settings:
        settings = Settings.model_validate(raw_settings)
        await self.plugin.on_config_update("self", settings.model_dump(), 0)
        await completed_thread(
            atomic_private_write,
            self.plugin.data_root / "settings.json",
            json.dumps(settings.model_dump(), ensure_ascii=False).encode(),
        )
        return settings

    _PROFILE_EDITABLE_KEYS = frozenset(
        {
            "name",
            "title",
            "description",
            "icon_shape",
            "theme_color",
            "clone_from",
            "base",
            "model_name",
            "model",
            "fallback_models",
            "task",
            "toolsets",
            "tools",
            "skills",
            "mcp_servers",
            "mcp",
            "connectors",
            "apis",
            "secret_handles",
            "secrets",
            "shared_secrets",
            "share_credentials",
            "execution_mode",
            "environment",
            "permission_mode",
            "session_mode",
            "enabled_chats",
            "allow_chats",
            "allowed_chats",
            "allow_users",
            "access",
            "instructions",
            "enabled",
            "bound",
            "network",
            "web_provider",
            "max_tokens",
            "context_tokens",
            "temperature",
            "max_steps",
            "fish_seed",
            "template",
            "sandbox_targets",
        }
    )

    @staticmethod
    def _str_list(raw: object) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            return [tok.strip() for tok in re.split(r"[\s,]+", raw) if tok.strip()]
        if not isinstance(raw, (list, tuple, set)):
            return []
        out: list[str] = []
        for item in raw:
            val = (
                (item.get("id") or item.get("name") or "")
                if isinstance(item, dict)
                else item
            )
            tok = str(val or "").strip()
            if tok and tok not in out:
                out.append(tok)
        return out

    @classmethod
    def _extract_profile_fields(
        cls, options: dict, *, services_cfg: dict | None = None
    ) -> dict:
        nested = options.get("profile") if isinstance(options.get("profile"), dict) else {}
        merged = {**nested, **options}
        if "profile_md" in merged and "instructions" not in merged:
            merged["instructions"] = merged["profile_md"]
        fields: dict = {}
        for key in cls._PROFILE_EDITABLE_KEYS:
            if key in merged and merged[key] is not None:
                fields[key] = merged[key]

        if "base" in fields:
            base_val = fields.pop("base")
            fields.setdefault("clone_from", base_val)

        if "share_credentials" in fields:
            sc_val = fields.pop("share_credentials")
            fields.setdefault("shared_secrets", bool(sc_val))

        if "environment" in fields:
            env_val = str(fields.pop("environment") or "").strip().lower()
            fields.setdefault(
                "execution_mode", "sandbox" if env_val == "sandbox" else "host"
            )

        if "access" in fields:
            acc = str(fields["access"] or "").strip().lower()
            if acc == "chats":
                fields["access"] = "listed"
            elif acc == "all":
                fields["access"] = "everyone"

        if "session_mode" in fields:
            sm = str(fields["session_mode"] or "").strip().lower()
            if sm in {"thread", "task"}:
                fields["session_mode"] = "ephemeral"

        if "allowed_chats" in fields:
            allowed_list = cls._str_list(fields.pop("allowed_chats"))
            if "allow_chats" not in fields and "enabled_chats" not in fields:
                fields["allow_chats"] = allowed_list

        has_mcp_input = any(
            k in fields for k in ("mcp_servers", "mcp", "connectors")
        )
        has_api_input = "apis" in fields
        if has_mcp_input or has_api_input:
            mcp_acc: list[str] = []
            api_acc: list[str] = []
            for tok in cls._str_list(fields.pop("mcp_servers", None)) + cls._str_list(
                fields.pop("mcp", None)
            ):
                if tok not in mcp_acc:
                    mcp_acc.append(tok)
            for tok in cls._str_list(fields.pop("apis", None)):
                if tok not in api_acc:
                    api_acc.append(tok)
            raw_conn = fields.pop("connectors", None)
            if raw_conn is not None:
                cfg = services_cfg if isinstance(services_cfg, dict) else {}
                known_apis = set((cfg.get("apis") or {}).keys())
                known_mcps = set((cfg.get("servers") or {}).keys())
                if isinstance(raw_conn, dict):
                    for tok in cls._str_list(
                        raw_conn.get("mcp_servers") or raw_conn.get("mcp")
                    ):
                        if tok not in mcp_acc:
                            mcp_acc.append(tok)
                    dict_apis = raw_conn.get("plugin_apis")
                    if dict_apis is None:
                        dict_apis = raw_conn.get("apis")
                    if dict_apis is not None:
                        has_api_input = True
                        for tok in cls._str_list(dict_apis):
                            if tok not in api_acc:
                                api_acc.append(tok)
                else:
                    has_api_input = True
                    for tok in cls._str_list(raw_conn):
                        if tok in known_apis and tok not in known_mcps:
                            if tok not in api_acc:
                                api_acc.append(tok)
                        else:
                            if tok not in mcp_acc:
                                mcp_acc.append(tok)
            if has_mcp_input:
                fields["mcp_servers"] = mcp_acc
            if has_api_input:
                fields["apis"] = api_acc
        return fields

    async def _apply_settings_patch(
        self, raw_settings: dict, *, expected_version: object = None
    ) -> dict:
        if expected_version is not None and expected_version != self._settings()["version"]:
            raise ValueError("设置已变化，请刷新后再保存")
        if not isinstance(raw_settings, dict):
            raise ValueError("settings 必须是对象")
        base_dump = self.plugin.settings.model_dump()
        merged = {**base_dump, **raw_settings}
        if isinstance(raw_settings.get("plugin"), dict) or "plugin_enabled" in raw_settings:
            pl_merged = {
                **dict(base_dump.get("plugin") or {}),
                **(
                    dict(raw_settings["plugin"])
                    if isinstance(raw_settings.get("plugin"), dict)
                    else {}
                ),
            }
            if raw_settings.get("plugin_enabled") is not None:
                pl_merged["enabled"] = bool(raw_settings["plugin_enabled"])
            merged["plugin"] = pl_merged
        merged.pop("plugin_enabled", None)
        if isinstance(raw_settings.get("sandbox"), dict) or "sandbox_enabled" in raw_settings:
            sb_merged = {
                **dict(base_dump.get("sandbox") or {}),
                **(
                    dict(raw_settings["sandbox"])
                    if isinstance(raw_settings.get("sandbox"), dict)
                    else {}
                ),
            }
            if raw_settings.get("sandbox_enabled") is not None:
                sb_merged["enabled"] = bool(raw_settings["sandbox_enabled"])
            merged["sandbox"] = sb_merged
        merged.pop("sandbox_enabled", None)
        if "admins" in raw_settings and (
            not isinstance(raw_settings["admins"], list)
            or bool(raw_settings["admins"])
            or bool(self.plugin.admins)
            or set(raw_settings) == {"admins"}
        ):
            host_admins = list(self._admins_info().get("host_admins") or [])
            merged["admins"] = validate_admin_update(
                raw_settings["admins"], actor="", host_admins=host_admins
            )
        if isinstance(raw_settings.get("enabled_chats"), list):
            normalized_chats = []
            for item in raw_settings["enabled_chats"]:
                token = str(item or "").strip()
                if not token:
                    continue
                try:
                    token = (
                        await resolve_stream_spec(
                            self.plugin, token, allow_unknown_ascii=True
                        )
                        or token
                    )
                except ValueError:
                    pass
                if token not in normalized_chats:
                    normalized_chats.append(token)
            merged["enabled_chats"] = normalized_chats
        if isinstance(raw_settings.get("chat_proactive_levels"), dict):
            normalized_levels = {}
            for k, v in raw_settings["chat_proactive_levels"].items():
                key = str(k or "").strip()
                if not key:
                    continue
                try:
                    key = (
                        await resolve_stream_spec(
                            self.plugin, key, allow_unknown_ascii=True
                        )
                        or key
                    )
                except ValueError:
                    pass
                normalized_levels[key] = v
            merged["chat_proactive_levels"] = normalized_levels
        if "tz_offset_minutes" in raw_settings:
            if raw_settings.get("tz_offset_minutes") is None:
                merged["tz_offset_minutes"] = self.plugin.settings.tz_offset_minutes
            else:
                merged["tz_offset_minutes"] = _validate_tz_offset_minutes_setting(
                    raw_settings["tz_offset_minutes"]
                )
        if "sleep_window" in raw_settings:
            if raw_settings.get("sleep_window") is None:
                merged["sleep_window"] = self.plugin.settings.sleep_window
            else:
                merged["sleep_window"] = _normalize_sleep_window_setting(
                    raw_settings["sleep_window"]
                )
        if "ui_style" in raw_settings and raw_settings["ui_style"] is not None:
            merged["ui_style"] = canonical_ui_style(raw_settings["ui_style"])
        await self._persist_settings(merged)
        return self._settings()

    async def _write_thread_approve(self, options: dict) -> dict:
        tid = str(options.get("thread_id") or "").strip()
        if not tid:
            raise ValueError("thread_approve 需要提供 thread_id")
        decision = str(options.get("decision") or options.get("grant_tier") or "once").strip().lower()
        request_id = str(options.get("request_id") or "").strip()
        action_key = str(options.get("action_key") or "").strip()
        note = str(options.get("note") or "").strip()
        resolved = await completed_thread(
            self.plugin.runtime.resolve_pending_approval,
            tid,
            decision=decision,
            request_id=request_id,
            action_key=action_key,
            actor="console:admin",
            note=note,
        )
        row = await completed_thread(
            self.plugin.threads.get,
            tid,
            "",
            allow_self=True,
            allow_any_chat=True,
        )
        engine = getattr(self.plugin, "engine", None)
        resumed = False
        if engine is not None:
            summary = resolved.get("summary") or resolved.get("action_key") or ""
            if decision == "deny":
                resume_msg = (
                    f"管理员通过控制台拒绝了该操作：{summary}。"
                    "请不要再执行该操作，改用安全方式继续或说明限制。"
                )
            else:
                resume_msg = (
                    f"管理员已通过控制台批准操作（{decision}）：{summary}。"
                    "请继续执行并完成任务。"
                )
            if note:
                resume_msg += f" 管理员附注：{note}"
            await engine.send(
                tid,
                resume_msg,
                chat_id=row["chat_id"],
                wait_seconds=0,
                background=True,
            )
            resumed = True
        return {**resolved, "resumed": resumed}

    async def _write_onboarding(self, action: str, options: dict) -> dict:
        mgr = getattr(self.plugin, "onboarding", None)
        if mgr is None:
            raise RuntimeError("首次引导管理器未初始化")
        base_view = await self._onboarding_view_payload()
        run_id = str(options.get("run_id") or "").strip()
        raw_seq = options.get("sequence")
        sequence = int(raw_seq) if raw_seq is not None else None
        ticket_ok = (not run_id or run_id == str(base_view.get("run_id") or "")) and (
            sequence is None or sequence >= int(base_view.get("sequence") or 0)
        )
        services_cfg = await completed_thread(self.plugin.runtime.services.load)
        view_kwargs = {
            "settings": self.plugin.settings,
            "host_model_catalog": (await self._collect_host_models())["models"],
            "host_admins": self._admins_info()["host_admins"],
            "mcp_servers": (services_cfg or {}).get("servers") or {},
            "docker_status": await completed_thread(self._docker_status),
            "existing_threads_count": len(self.plugin.threads.list_threads(limit=50)),
            "is_admin_view": True,
            "default_profile_ready": (
                self.plugin.profiles.path / "default" / "profile.toml"
            ).is_file(),
            "profiles_path": self.plugin.profiles.path,
        }

        async def _finish_ob(
            raw_view: dict, *, probe_sequence: int | None = None
        ) -> dict:
            view = await completed_thread(self._enrich_onboarding_view, raw_view)
            out = {**view, "onboarding": view}
            if probe_sequence is not None:
                out["probe_sequence"] = probe_sequence
            return out

        if action == "onboarding_advance":
            data = options.get("data")
            if ticket_ok and isinstance(data, dict):
                patch_keys = {
                    k: data[k]
                    for k in (
                        "admins",
                        "enabled_chats",
                        "default_model",
                        "permission_mode",
                        "sandbox",
                        "plugin",
                        "ui_style",
                        "sleep_window",
                    )
                    if k in data and data[k] is not None
                }
                if data.get("plugin_enabled") is not None:
                    pl_patch = dict(
                        patch_keys.get("plugin")
                        if isinstance(patch_keys.get("plugin"), dict)
                        else {}
                    )
                    pl_patch["enabled"] = bool(data["plugin_enabled"])
                    patch_keys["plugin"] = pl_patch
                if data.get("sandbox_enabled") is not None:
                    sb_patch = dict(
                        patch_keys.get("sandbox")
                        if isinstance(patch_keys.get("sandbox"), dict)
                        else {}
                    )
                    sb_patch["enabled"] = bool(data["sandbox_enabled"])
                    patch_keys["sandbox"] = sb_patch
                if patch_keys:
                    await self._apply_settings_patch(patch_keys)
                    view_kwargs["settings"] = self.plugin.settings
                    view_kwargs["docker_status"] = await completed_thread(
                        self._docker_status
                    )
            raw_step = options.get("target_step") or options.get("step")
            raw_view = await completed_thread(
                mgr.advance,
                str(raw_step).strip() if raw_step else None,
                run_id=run_id,
                sequence=sequence,
                data=data if isinstance(data, dict) else None,
                **view_kwargs,
            )
            return await _finish_ob(raw_view)
        if action == "onboarding_skip":
            raw_view = await completed_thread(
                mgr.skip, run_id=run_id, sequence=sequence, **view_kwargs
            )
            return await _finish_ob(raw_view)
        if action == "onboarding_complete":
            raw_view = await completed_thread(
                mgr.complete, run_id=run_id, sequence=sequence, **view_kwargs
            )
            return await _finish_ob(raw_view)
        if action == "onboarding_reset":
            raw_view = await completed_thread(mgr.reset, **view_kwargs)
            return await _finish_ob(raw_view)
        if action == "onboarding_reopen":
            step = options.get("step")
            raw_view = await completed_thread(
                mgr.reopen, str(step).strip() if step else None, **view_kwargs
            )
            return await _finish_ob(raw_view)
        if action == "onboarding_verify_model":
            raw_probe_seq = options.get("probe_sequence")
            probe_seq = int(raw_probe_seq) if raw_probe_seq is not None else None
            if not ticket_ok:
                return await _finish_ob(
                    {**base_view, "stale": True, "applied": False},
                    probe_sequence=probe_seq,
                )
            model_name = str(
                options.get("model_name") or self.plugin.settings.default_model or ""
            ).strip()
            probe_fn = getattr(self.plugin, "probe_model", None)
            cb = (
                (lambda: probe_fn(model_name=model_name))
                if callable(probe_fn)
                else None
            )
            raw_view = await mgr.verify_model(
                probe_fn=cb,
                model_name=model_name,
                timeout_seconds=10.0,
                **view_kwargs,
            )
            return await _finish_ob(raw_view, probe_sequence=probe_seq)
        raise ValueError("未知引导操作")

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
        elif action == "thread_stop":
            tid = str(options.get("thread_id") or "").strip()
            if not tid:
                raise ValueError("thread_stop 需要提供 thread_id")
            row = await completed_thread(
                plugin.threads.get, tid, "", allow_self=True, allow_any_chat=True
            )
            if getattr(plugin, "engine", None) is not None:
                await plugin.engine.stop(tid, chat_id=row["chat_id"])
            else:
                await completed_thread(plugin.threads.stop, tid, row["chat_id"])
            updated_row = await completed_thread(
                plugin.threads.get, tid, "", allow_self=True, allow_any_chat=True
            )
            result = {"thread_id": tid, "state": updated_row["state"]}
        elif action == "thread_reopen":
            tid = str(options.get("thread_id") or "").strip()
            if not tid:
                raise ValueError("thread_reopen 需要提供 thread_id")
            reopened = await completed_thread(
                plugin.threads.reopen, tid, allow_self=True, allow_any_chat=True
            )
            prompt = str(options.get("prompt") or options.get("message") or "").strip()
            if prompt and getattr(plugin, "engine", None) is not None:
                await plugin.engine.send(
                    tid,
                    prompt,
                    chat_id=reopened["chat_id"],
                    wait_seconds=0,
                    background=True,
                )
                reopened = await completed_thread(
                    plugin.threads.get, tid, "", allow_self=True, allow_any_chat=True
                )
            result = {"thread_id": tid, "state": reopened["state"]}
        elif action == "thread_approve":
            result = await self._write_thread_approve(options)
        elif action == "profile_create":
            pid = str(
                options.get("profile_id")
                or options.get("id")
                or options.get("target")
                or ""
            ).strip()
            services_cfg = (
                await completed_thread(plugin.runtime.services.load)
                if getattr(plugin, "runtime", None) is not None
                and getattr(plugin.runtime, "services", None) is not None
                else {}
            )
            fields = self._extract_profile_fields(options, services_cfg=services_cfg)

            def _create_prof():
                prof = plugin.profiles.create(pid, **fields)
                plugin.memory.seed_profile(prof.id)
                return prof

            profile = await completed_thread(_create_prof)
            result = {"id": profile.id, "profile": self._serialize_profile(profile)}
        elif action == "profile_update":
            pid = str(
                options.get("profile_id")
                or options.get("id")
                or options.get("target")
                or ""
            ).strip()
            if not pid:
                raise ValueError("profile_update 需要提供 profile_id")
            services_cfg = (
                await completed_thread(plugin.runtime.services.load)
                if getattr(plugin, "runtime", None) is not None
                and getattr(plugin.runtime, "services", None) is not None
                else {}
            )
            fields = self._extract_profile_fields(options, services_cfg=services_cfg)
            raw_expected = options.get("expected_version")
            if raw_expected is not None and type(raw_expected) is int and "instructions" in fields:
                cur_md = await completed_thread(
                    plugin.memory.read, "profile", target=pid, document="PROFILE.md"
                )
                if cur_md.get("version") != raw_expected:
                    raise ValueError("角色正文已被其他编辑更新，请刷新后再保存")
            profile = await completed_thread(
                lambda: plugin.profiles.update(pid, **fields)
            )
            result = {"id": profile.id, "profile": self._serialize_profile(profile)}
        elif action in {"profile_clone", "clone"}:
            source_id = str(
                options.get("source_id") or options.get("base") or "default"
            ).strip()
            target_id = str(
                options.get("target_id") or options.get("target") or ""
            ).strip()
            clone_title = options.get("title") or options.get("name")

            def _clone_and_seed():
                prof = plugin.profiles.clone(
                    source_id,
                    target_id,
                    name=str(clone_title).strip() if clone_title else None,
                )
                plugin.memory.seed_profile(prof.id)
                return prof

            profile = await completed_thread(_clone_and_seed)
            result = {"id": profile.id, "profile": self._serialize_profile(profile)}
        elif action in {"profile_delete", "delete_profile"}:
            target = str(
                options.get("profile_id") or options.get("target") or ""
            ).strip()

            def _delete_and_purge():
                plugin.profiles.delete(target)
                plugin.memory.purge_profile(target)

            await completed_thread(_delete_and_purge)
            result = {"id": target, "deleted": True}
        elif action == "profile_export_zip":
            pid = str(
                options.get("profile_id") or options.get("target") or ""
            ).strip()
            raw_zip = await completed_thread(plugin.profiles.export_profile_zip, pid)
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", pid).strip("._") or "profile"
            result = {
                "profile_id": pid,
                "filename": f"{safe_name}.zip",
                "base64": base64.b64encode(raw_zip).decode("ascii"),
                "mime": "application/zip",
                "bytes": len(raw_zip),
            }
        elif action == "profile_import_zip":
            b64_data = str(options.get("base64") or options.get("data") or "").strip()
            if not b64_data:
                raise ValueError("profile_import_zip 需要提供 base64 压缩包内容")
            raw_zip = base64.b64decode(b64_data, validate=True)
            if len(raw_zip) > 8 * 1024**2:
                raise ValueError("角色压缩包超过 8 MiB 上限")
            pid = str(
                options.get("profile_id") or options.get("target") or ""
            ).strip()
            if not pid:
                fname = str(options.get("filename") or "").strip()
                stem = Path(fname).stem if fname else ""
                pid = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_-").lower() or (
                    f"imp_{uuid.uuid4().hex[:6]}"
                )
            overwrite = bool(options.get("overwrite", False))

            def _import_zip():
                prof = plugin.profiles.import_profile_zip(
                    raw_zip, pid, overwrite=overwrite
                )
                plugin.memory.seed_profile(prof.id)
                return prof

            profile = await completed_thread(_import_zip)
            result = {
                "id": profile.id,
                "profile": self._serialize_profile(profile),
                "done": True,
            }
        elif action == "fs_write":
            result = await completed_thread(self._fs_write_sync, options)
        elif action == "fs_delete":
            result = await completed_thread(self._fs_delete_sync, options)
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
        elif action in {"vault_set", "vault"}:
            if action == "vault" and options.get("delete") is True:
                await completed_thread(plugin.vault.delete, options["handle"])
            else:
                await completed_thread(
                    lambda: plugin.vault.set(
                        options["handle"],
                        options["value"],
                        label=options.get("label"),
                        domain=options.get("domain"),
                        fields=options.get("fields"),
                        kind=options.get("kind"),
                        note=options.get("note"),
                    )
                )
            result = {
                "handles": await completed_thread(plugin.vault.handles),
                "items": await completed_thread(plugin.vault.list_meta),
            }
        elif action == "vault_delete":
            await completed_thread(plugin.vault.delete, options["handle"])
            result = {
                "handles": await completed_thread(plugin.vault.handles),
                "items": await completed_thread(plugin.vault.list_meta),
            }
        elif action == "grant_revoke":
            grants_store = getattr(plugin, "grants", None) or getattr(
                getattr(plugin, "runtime", None), "grants", None
            )
            if grants_store is None:
                raise RuntimeError("权限授权存储尚未就绪")
            grant_key = str(options.get("grant_key") or "").strip()
            scope = str(options.get("scope") or "").strip()
            action_key = str(options.get("action_key") or "").strip()
            if grant_key:
                await completed_thread(grants_store.revoke, grant_key)
            elif scope and action_key:
                await completed_thread(
                    grants_store.revoke,
                    f"always|{scope}||{action_key}",
                )
            elif scope:
                await completed_thread(grants_store.revoke_scope, scope)
            else:
                raise ValueError("grant_revoke 需要提供 grant_key 或 scope")
            result = {"items": await completed_thread(grants_store.list_grants)}
        elif action in {"settings_update", "settings"}:
            raw_settings = (
                dict(options["settings"])
                if isinstance(options.get("settings"), dict)
                else {
                    k: v
                    for k, v in options.items()
                    if k not in {"action", "expected_version"}
                }
            )
            result = await self._apply_settings_patch(
                raw_settings,
                expected_version=options.get("expected_version"),
            )
        elif action == "ui_style":
            raw_style = str(options.get("ui_style") or "").strip()
            if raw_style not in {"millennium", "modern", "maiwork", "auto"}:
                raise ValueError("界面风格仅支持 millennium、modern（maiwork）或 auto")
            style = canonical_ui_style(raw_style)
            raw_settings = plugin.settings.model_dump()
            raw_settings["ui_style"] = style
            settings = await self._persist_settings(raw_settings)
            result = {
                "ui_style": settings.ui_style,
                "version": self._settings()["version"],
            }
        elif action == "services":
            current = await completed_thread(plugin.runtime.services.load)
            if isinstance(current, dict):
                current.pop("templates", None)
            if options.get("expected_version") != self._version(current):
                raise ValueError("服务登记已变化，请刷新后重试")
            content = json.dumps(options["configuration"], ensure_ascii=False)
            await completed_thread(self._validate_services, options["configuration"])
            await completed_thread(
                atomic_private_write, plugin.runtime.services.path, content.encode()
            )
            await plugin.runtime.services.close_sessions()
            result = {"version": self._version(options["configuration"])}
        elif action == "intent_pause":
            row = await completed_thread(plugin.intents.get, options["intent_id"])
            await completed_thread(
                plugin.intents.pause, row["intent_id"], row["target"], is_admin=True
            )
            result = {"intent_id": row["intent_id"], "status": "paused"}
        elif action == "intent_resume":
            row = await completed_thread(plugin.intents.get, options["intent_id"])
            await completed_thread(
                plugin.intents.resume, row["intent_id"], row["target"], is_admin=True
            )
            result = {"intent_id": row["intent_id"], "status": "pending"}
        elif action == "intent_run_now":
            iid = str(options.get("intent_id") or "").strip()
            row = await completed_thread(plugin.intents.get, iid)
            if row["status"] == "paused":
                await completed_thread(
                    plugin.intents.resume, iid, row["target"], is_admin=True
                )

            def _mark_due_now():
                now_ts = plugin.intents.clock() - 1.0
                with plugin.intents.store.connect() as db, db:
                    db.execute(
                        "UPDATE intents SET due_at=?, status='pending' WHERE intent_id=?",
                        (now_ts, iid),
                    )
                return plugin.intents.get(iid)

            fresh = await completed_thread(_mark_due_now)
            if getattr(plugin, "pulse", None) is not None:
                await plugin.pulse._dispatch(fresh)
            try:
                latest = await completed_thread(plugin.intents.get, iid)
            except (ValueError, KeyError):
                latest = fresh
            result = {"intent_id": iid, "intent": latest}
        elif action in {"intent_delete", "cancel_intent"}:
            row = await completed_thread(plugin.intents.get, options["intent_id"])
            await completed_thread(
                plugin.intents.cancel, row["intent_id"], row["target"], is_admin=True
            )
            result = {"intent_id": row["intent_id"], "status": "cancelled"}
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
            mode = str(options.get("mode") or ("watch" if options.get("condition") else "")).strip()
            condition = str(options.get("condition") or "").strip()
            kind = str(options.get("kind") or "return").strip() or "return"
            payload = {
                "platform": explicit_platform or resolved_platform or "qq",
                "profile": profile_name or str(plugin.settings.heartbeat_profile or "default"),
                "workspace": str(options.get("workspace") or "").strip(),
                "prompt": prompt or reason,
                "every_seconds": every_seconds,
                "scheduled_admin": True,
            }
            due_at = plugin.intents.clock() + in_seconds
            row = await completed_thread(
                plugin.intents.schedule,
                kind=kind,
                source="watch" if mode == "watch" else "workflow",
                target=f"chat:{chat_id}",
                due_at=due_at,
                reason=reason,
                payload=payload,
                bypass_limits=True,
                mode=mode,
                condition=condition,
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
            raw_expected = options.get("expected_version")
            if raw_expected is not None and type(raw_expected) is not int:
                raise ValueError("角色配置版本格式无效")
            expected = current["version"] if raw_expected is None else raw_expected
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
                raise ValueError("需要提供角色标识与有效的头像种子")
            result = await completed_thread(
                self._set_profile_fish, target, raw_seed.strip()
            )
        elif action == "set_profile_models":
            result = await completed_thread(self._set_profile_models, options)
        elif action == "upsert_service_item":
            result = await completed_thread(self._upsert_service_item_sync, options)
            await plugin.runtime.services.close_sessions()
        elif action == "delete_service_item":
            result = await completed_thread(self._delete_service_item_sync, options)
            await plugin.runtime.services.close_sessions()
        elif action == "undo_learning_note":
            result = await completed_thread(self._undo_learning_note, options)
        elif action == "import_skill":
            result = await completed_thread(self._upload_skill, options)
        elif action == "import":
            result = await completed_thread(self._upload, options)
        elif action in {
            "onboarding_advance",
            "onboarding_skip",
            "onboarding_complete",
            "onboarding_reset",
            "onboarding_reopen",
            "onboarding_verify_model",
        }:
            result = await self._write_onboarding(action, options)
        elif action == "delete_thread":
            tid = str(options.get("thread_id") or "").strip()
            ws_root = (
                plugin.runtime.files.root
                if getattr(plugin, "runtime", None) is not None
                else None
            )
            result = await completed_thread(
                plugin.threads.delete_thread, tid, workspace_root=ws_root
            )
            locks = getattr(getattr(plugin, "engine", None), "locks", None)
            if isinstance(locks, dict):
                lock = locks.get(tid)
                if lock is not None and not lock.locked():
                    locks.pop(tid, None)
        elif action == "prune_threads":
            raw_chat = str(options.get("chat_id") or "").strip()

            def _prune():
                chat_ids = None
                if raw_chat:
                    resolved = (
                        plugin.resolve_known_session_id(raw_chat)
                        if raw_chat != "self"
                        else "self"
                    )
                    chat_ids = (
                        {raw_chat, resolved}
                        | plugin.chat_aliases_for(raw_chat)
                        | plugin.chat_aliases_for(resolved)
                    )
                ws_root = (
                    plugin.runtime.files.root
                    if getattr(plugin, "runtime", None) is not None
                    else None
                )
                return plugin.threads.prune_junk_threads(
                    chat_ids=chat_ids, workspace_root=ws_root
                )

            result = await completed_thread(_prune)
            locks = getattr(getattr(plugin, "engine", None), "locks", None)
            if isinstance(locks, dict):
                for dead in result.get("deleted") or ():
                    lock = locks.get(dead)
                    if lock is not None and not lock.locked():
                        locks.pop(dead, None)
        else:
            raise ValueError("未知管理操作")
        return {"success": True, **plugin.vault.redact(result)}

    def _validate_services(self, config):
        raw = json.dumps(config).encode()
        if (
            self.plugin.vault.contains_secret(raw)
            or len(raw) > 48000
            or not isinstance(config, dict)
            or set(config) - {"servers", "inputs", "apis", "web"}
        ):
            raise ValueError("服务登记只接受结构化配置和凭据句柄")
        if not isinstance(config.get("servers", {}), dict):
            raise TypeError("服务登记需要 servers 对象")
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
