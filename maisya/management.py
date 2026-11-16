"""宿主认证管理页的静态 API；正文与秘密走各自的执行边界。"""

import asyncio
import base64
import copy
import hashlib
import json
import mimetypes
import os
import re
import shlex
import shutil
import sqlite3
import sys
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
from .digest import DigestWorkspace
from .memory import PRESETS_DIR, SKILL_ID_RE, MemoryLayers, parse_skill_meta
from .packages import (
    export_package,
    import_package_bundle,
    preview_export_package,
    preview_package,
    sanitize_export_text,
)
from .pathsafe import is_absolute_program_path, path_is_link_like
from .shaping import collect_known_chats, resolve_chat_target, resolve_stream_spec
from .shaping_setup import handle_digest_sources
from .scout import call_capability
from .share import links_in
from .services import (
    validate_api_entry,
    validate_mcp_http_url,
    validate_web_bindings,
)
from .trust import (
    clean_display_name,
    format_stream_spec,
    merge_admins,
    parse_stream_spec,
    validate_admin_update,
)
from .vault import atomic_private_write, secret_variants
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


def resolve_mcp_command(raw_cmd) -> list[str]:
    """将简单裸命令名（如 npx/uvx/python3）尽量解析为本机绝对路径，显式相对路径保留以供校验拦截。"""
    if isinstance(raw_cmd, str):
        parts = shlex.split(raw_cmd) if raw_cmd.strip() else []
    elif isinstance(raw_cmd, (list, tuple)):
        parts = [str(x) for x in raw_cmd if str(x)]
    else:
        parts = []
    if not parts:
        return []
    prog = parts[0].strip()
    if is_absolute_program_path(prog) or "/" in prog or "\\" in prog:
        return [prog, *parts[1:]]
    extra_bins = [
        str(Path(sys.executable).parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        str(Path.home() / ".local" / "bin"),
        os.environ.get("PATH", ""),
    ]
    search_path = os.pathsep.join(p for p in extra_bins if p)
    resolved = shutil.which(prog, path=search_path)
    if not resolved and prog in {"python", "python3"} and sys.executable:
        resolved = sys.executable
    return [resolved or prog, *parts[1:]]


def parse_mcp_json_payload(raw) -> dict:
    """解析常见 mcpServers JSON 或单个服务配置，返回可直接回填表单的标准结构。"""
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ValueError("MCP JSON 必须是 JSON 对象")
    name = ""
    spec = data
    container = data.get("mcpServers") or data.get("servers")
    if isinstance(container, dict) and container:
        name, first = next(iter(container.items()))
        if not isinstance(first, dict):
            raise ValueError("mcpServers 中的服务项必须是对象")
        spec = first
    elif isinstance(data.get("name"), str) and any(k in data for k in ("command", "url", "serverUrl")):
        name = data["name"].strip()
    url = str(spec.get("url") or spec.get("serverUrl") or "").strip()
    transport = str(spec.get("transport") or ("http" if url else "stdio")).strip().lower()
    raw_cmd = spec.get("command")
    raw_args = spec.get("args")
    cmd_list: list[str] = []
    if isinstance(raw_cmd, list):
        cmd_list = [str(x) for x in raw_cmd if str(x)]
    elif isinstance(raw_cmd, str) and raw_cmd.strip():
        cmd_list = [raw_cmd.strip()]
    if isinstance(raw_args, list):
        cmd_list.extend(str(x) for x in raw_args if str(x))
    raw_env = spec.get("env") if isinstance(spec.get("env"), dict) else {}
    env_map = {str(k).strip(): str(v) for k, v in raw_env.items() if str(k).strip() and v is not None}
    raw_hdrs = spec.get("headers") if isinstance(spec.get("headers"), dict) else {}
    hdr_map = {str(k).strip(): str(v) for k, v in raw_hdrs.items() if str(k).strip() and v is not None}
    env_lines = [f"{k}={v}" for k, v in (hdr_map or env_map).items()] if transport == "http" else [f"{k}={v}" for k, v in env_map.items()]
    return {
        "name": re.sub(r"[^A-Za-z0-9_-]+", "_", str(name or "").strip()).strip("_-") or "custom_mcp",
        "transport": "http" if transport == "http" else "stdio",
        "command": cmd_list,
        "command_text": shlex.join(cmd_list),
        "url": url,
        "env": env_map,
        "headers": hdr_map,
        "env_text": "\n".join(env_lines),
        "description": str(spec.get("description") or "").strip(),
    }


def _collect_inline_secret_handles(
    vault,
    service_name: str,
    raw_pairs: object,
    *,
    keep_keys_in: object,
    prev_handles: dict,
    slot: str,
    pending_secrets: list[tuple[str, str, str, str]] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """加密表单直接填写的 env/headers 为内部 MCP 句柄，支持 __KEEP__ 沿用未改动的旧密钥。"""
    keep_keys: set[str] = set()
    if isinstance(keep_keys_in, (list, tuple, set)):
        keep_keys.update(str(x).strip() for x in keep_keys_in if str(x).strip())
    next_handles: dict[str, str] = {}
    inline_keys: list[str] = []
    if isinstance(raw_pairs, dict):
        if "__KEEP__" in raw_pairs:
            kv = raw_pairs["__KEEP__"]
            if isinstance(kv, str) and kv.strip():
                keep_keys.add(kv.strip())
            elif isinstance(kv, (list, tuple, set)):
                keep_keys.update(str(x).strip() for x in kv if str(x).strip())
        for raw_k, raw_v in raw_pairs.items():
            key = str(raw_k or "").strip()
            if not key or key == "__KEEP__":
                continue
            if raw_v == "__KEEP__":
                keep_keys.add(key)
                continue
            val = "" if raw_v is None else str(raw_v)
            if val != "":
                if len(val.encode("utf-8")) > 65536:
                    raise ValueError("凭据必须为非空文本，且不超过 64 KiB")
                if pending_secrets is not None:
                    next_handles[key] = vault._mcp_secret_handle(service_name, key, slot=slot)
                    pending_secrets.append((service_name, key, val, slot))
                else:
                    next_handles[key] = vault.set_mcp_secret(service_name, key, val, slot=slot)
                if key not in inline_keys:
                    inline_keys.append(key)
    for k in sorted(keep_keys):
        prev_h = (prev_handles or {}).get(k)
        if prev_h and k not in next_handles:
            next_handles[k] = prev_h
            if k not in inline_keys:
                inline_keys.append(k)
    return next_handles, inline_keys





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
        if kind in {"api", "apis", "plugin_api"}:
            return "api", "apis"
        raise ValueError("服务类型仅支持 mcp 或 api")

    def _normalize_mcp_spec(
        self,
        name: str,
        raw_spec: dict,
        *,
        prev_spec: dict | None = None,
        pending_secrets: list[tuple[str, str, str, str]] | None = None,
    ) -> dict:
        spec = dict(raw_spec)
        prev = prev_spec if isinstance(prev_spec, dict) else {}
        transport = "http" if (str(spec.get("transport") or "").strip().lower() == "http" or str(spec.get("url") or "").strip()) else "stdio"
        if transport == "http":
            spec["transport"] = "http"
            spec["url"] = validate_mcp_http_url(str(spec.get("url") or ""))
            spec.pop("command", None)
        else:
            spec.pop("transport", None)
            spec.pop("url", None)
            if "command" in spec:
                spec["command"] = resolve_mcp_command(spec.get("command") or [])

        raw_env = spec.pop("env", None)
        keep_env_in = spec.pop("keep_env_keys", None)
        if raw_env is not None or keep_env_in is not None:
            env_handles, env_keys = _collect_inline_secret_handles(
                self.plugin.vault,
                name,
                raw_env,
                keep_keys_in=keep_env_in,
                prev_handles=prev.get("credentials") or {},
                slot="env",
                pending_secrets=pending_secrets,
            )
            base_creds = dict(spec.get("credentials") or {}) if isinstance(spec.get("credentials"), dict) else {}
            merged_creds = {**base_creds, **env_handles}
            if merged_creds:
                spec["credentials"] = merged_creds
            else:
                spec.pop("credentials", None)
            if env_keys:
                spec["inline_env_keys"] = env_keys
            else:
                spec.pop("inline_env_keys", None)
        elif "credentials" not in spec and prev.get("credentials"):
            spec["credentials"] = dict(prev["credentials"])
            if prev.get("inline_env_keys"):
                spec["inline_env_keys"] = list(prev["inline_env_keys"])

        raw_hdrs = spec.pop("headers", None)
        keep_hdr_in = spec.pop("keep_header_keys", None)
        if raw_hdrs is not None or keep_hdr_in is not None:
            hdr_handles, hdr_keys = _collect_inline_secret_handles(
                self.plugin.vault,
                name,
                raw_hdrs,
                keep_keys_in=keep_hdr_in,
                prev_handles=prev.get("header_credentials") or {},
                slot="header",
                pending_secrets=pending_secrets,
            )
            base_hc = dict(spec.get("header_credentials") or {}) if isinstance(spec.get("header_credentials"), dict) else {}
            merged_hc = {**base_hc, **hdr_handles}
            if merged_hc:
                spec["header_credentials"] = merged_hc
            else:
                spec.pop("header_credentials", None)
            if hdr_keys:
                spec["inline_header_keys"] = hdr_keys
            else:
                spec.pop("inline_header_keys", None)
        elif "header_credentials" not in spec and prev.get("header_credentials"):
            spec["header_credentials"] = dict(prev["header_credentials"])
            if prev.get("inline_header_keys"):
                spec["inline_header_keys"] = list(prev["inline_header_keys"])

        if not spec.get("tools"):
            spec["tools"] = ["*"]
        return spec

    def _commit_services_with_secrets(
        self,
        next_config: dict,
        pending_secrets: list[tuple[str, str, str, str]],
    ) -> None:
        self._validate_services(next_config)
        content_str = json.dumps(next_config, ensure_ascii=False)
        content = content_str.encode("utf-8")
        for _, _, val, slot in pending_secrets:
            variants = set(secret_variants(val))
            if slot == "header" and isinstance(val, str):
                parts = val.strip().split(None, 1)
                if len(parts) == 2 and parts[0].lower() in {"bearer", "token", "basic", "apikey"} and len(parts[1].strip()) >= 6:
                    variants.update(secret_variants(parts[1].strip()))
            if any(variant.encode("utf-8") in content for variant in variants):
                raise ValueError("服务登记只接受结构化配置和凭据句柄")
        vault = self.plugin.vault
        prev_meta: dict = {}
        meta_existed = False
        enc_snapshots: dict[Path, bytes | None] = {}
        if pending_secrets and vault is not None:
            prev_meta = vault._load_meta()
            meta_existed = vault.meta_path.exists()
            for svc, key, _val, slot in pending_secrets:
                handle = vault._mcp_secret_handle(svc, key, slot=slot)
                enc_path = vault.path / f"{handle}.enc"
                if enc_path not in enc_snapshots:
                    enc_snapshots[enc_path] = enc_path.read_bytes() if enc_path.exists() else None
        try:
            for svc, key, val, slot in pending_secrets:
                vault.set_mcp_secret(svc, key, val, slot=slot)
            atomic_private_write(self.plugin.runtime.services.path, content)
        except BaseException:
            if pending_secrets and vault is not None:
                for enc_path, old_bytes in enc_snapshots.items():
                    if old_bytes is None:
                        enc_path.unlink(missing_ok=True)
                    else:
                        atomic_private_write(enc_path, old_bytes)
                if meta_existed or prev_meta:
                    vault._save_meta(prev_meta)
                elif vault.meta_path.exists():
                    vault.meta_path.unlink(missing_ok=True)
            raise

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
        next_config.setdefault("apis", {})
        next_config.pop("templates", None)
        prev_spec = copy.deepcopy(next_config.get(bucket, {}).get(orig_name or name) or {})
        if orig_name:
            _, orig_bucket = (
                self._service_bucket(orig_kind) if orig_kind else (kind, bucket)
            )
            if (orig_bucket, orig_name) != (bucket, name):
                next_config.get(orig_bucket, {}).pop(orig_name, None)

        pending_secrets: list[tuple[str, str, str, str]] = []
        if kind == "mcp":
            spec = self._normalize_mcp_spec(
                name,
                spec,
                prev_spec=prev_spec,
                pending_secrets=pending_secrets,
            )
        elif kind == "api":
            if "api_name" not in spec and spec.get("api"):
                spec["api_name"] = str(spec.pop("api") or "").strip()
            spec.pop("api", None)
        next_config[bucket][name] = spec
        self._commit_services_with_secrets(next_config, pending_secrets)
        if kind == "mcp":
            keep_h = set((spec.get("credentials") or {}).values()) | set((spec.get("header_credentials") or {}).values())
            self.plugin.vault.purge_mcp_secrets(name, keep_handles=keep_h)
            if orig_name and orig_name != name:
                self.plugin.vault.purge_mcp_secrets(orig_name, keep_handles=keep_h)
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
        if kind == "mcp":
            self.plugin.vault.purge_mcp_secrets(name)
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
        skill_id = str(options.get("skill_id") or "").strip()
        raw_hid = options.get("history_id")
        hid = str(raw_hid).strip() if raw_hid is not None else ""
        if skill_id:
            if not SKILL_ID_RE.fullmatch(skill_id):
                raise ValueError("技能编号格式无效")
            cur_sk = self.plugin.memory.read("skill", target=skill_id, allow_custom=True)
            self._require_expected_version(
                options,
                cur_sk["version"],
                message="技能版本已变化，请刷新后重试",
                require_int=True,
            )
            sk_hist = self.plugin.memory.history("skill", target=skill_id)
            tid_cand = hid[7:] if hid.startswith("thread:") else hid
            sk_match_idx = (
                next(
                    (
                        i
                        for i, h in enumerate(sk_hist)
                        if h.get("author") in {hid, tid_cand, f"thread:{tid_cand}"}
                    ),
                    None,
                )
                if hid
                else next(
                    (i for i, h in enumerate(sk_hist) if str(h.get("author") or "").startswith("thread:")),
                    None,
                )
            )
            if sk_match_idx is None or sk_match_idx + 1 >= len(sk_hist):
                raise ValueError("未找到可撤回的技能自动补充版本")
            prev_ver = int(sk_hist[sk_match_idx + 1]["version"])
            saved_sk = self.plugin.memory.rollback(
                "skill",
                prev_ver,
                target=skill_id,
                expected_version=cur_sk["version"],
                allow_custom=True,
            )
            reverted_tid = tid_cand or (
                str(sk_hist[sk_match_idx].get("author") or "")[7:]
                if str(sk_hist[sk_match_idx].get("author") or "").startswith("thread:")
                else ""
            )
            if profile_id and reverted_tid:
                try:
                    n_cur = self.plugin.memory.read("notes", target=profile_id)
                    n_hist = self.plugin.memory.history("notes", target=profile_id)
                    n_idx = next(
                        (
                            i
                            for i, h in enumerate(n_hist)
                            if h.get("author") in {reverted_tid, f"thread:{reverted_tid}"}
                        ),
                        None,
                    )
                    if n_idx is not None:
                        if n_idx + 1 < len(n_hist):
                            self.plugin.memory.rollback(
                                "notes",
                                n_hist[n_idx + 1]["version"],
                                target=profile_id,
                                expected_version=n_cur["version"],
                                allow_custom=True,
                            )
                        else:
                            self.plugin.memory.write(
                                "notes",
                                "",
                                target=profile_id,
                                expected_version=n_cur["version"],
                                author="rollback",
                                allow_custom=True,
                            )
                except (ValueError, OSError, sqlite3.Error):
                    pass
            if reverted_tid and getattr(self.plugin, "threads", None) is not None:
                with self.plugin.threads.connect() as db, db:
                    db.execute(
                        "UPDATE learning_receipts SET status = 'reverted' WHERE thread_id = ? AND status = 'applied'",
                        (reverted_tid,),
                    )
            return {
                "skill_id": skill_id,
                "profile_id": profile_id,
                "target": skill_id,
                "content": saved_sk["content"],
                "version": saved_sk["version"],
                "restored_from": prev_ver,
                "history": self.plugin.memory.history("skill", target=skill_id),
            }
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
                    "name": clean_display_name(
                        s_info.get("name") or "", gid or uid or spec or sid
                    ),
                    "platform": plat,
                    "chat_type": ctype,
                    "group_id": gid,
                    "user_id": uid,
                    "avatar": str(s_info.get("avatar") or ""),
                    "avatar_data": str(s_info.get("avatar_data") or ""),
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
        hb_has_plan = False
        if getattr(self.plugin, "memory", None) is not None:
            try:
                hb_path = self.plugin.memory.root / "memory/self/HEARTBEAT.md"
                hb_raw = hb_path.read_text(encoding="utf-8") if hb_path.is_file() else ""
                _, hb_plan = MemoryLayers.heartbeat_parts(hb_raw)
                hb_has_plan = bool(hb_plan.strip())
            except OSError:
                hb_has_plan = False
        return {
            "settings": settings,
            "version": self._version(settings),
            "admins": admins_info["effective_admins"],
            "admins_info": admins_info,
            "permission_mode": settings.get("permission_mode", "ask_risky"),
            "ui_style": settings["ui_style"],
            "plugin_enabled": plugin_enabled,
            "heartbeat_has_plan": hb_has_plan,
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
        unreplied_raw: list[str] = []
        intents_mgr = getattr(plugin, "intents", None)
        same_intents_db = bool(
            intents_mgr is not None
            and getattr(intents_mgr, "store", None) is not None
            and (
                intents_mgr.store is plugin.threads
                or getattr(intents_mgr.store, "path", None)
                == getattr(plugin.threads, "path", None)
            )
        )
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
            pending_props = plugin.memory.list_skill_proposals(
                status="pending", limit=10, db=db
            )
            if same_intents_db:
                try:
                    s_rows = db.execute(
                        "SELECT DISTINCT chat_id FROM social_state WHERE locked = 1 ORDER BY updated DESC LIMIT 20"
                    ).fetchall()
                    for sr in s_rows:
                        rcid = str(sr["chat_id"] if hasattr(sr, "keys") else sr[0]).strip()
                        if rcid:
                            unreplied_raw.append(rcid)
                except sqlite3.Error:
                    unreplied_raw = []
        chats.update(plugin.settings.enabled_chats)
        raw_nonempty = {str(c).strip() for c in chats if str(c or "").strip()}
        deduped = {
            ("self" if c == "self" else (plugin.canonical_stream_spec(c) or c))
            for c in raw_nonempty
        }
        source_ids = sorted(deduped | raw_nonempty)
        raw_chat_id = str(options.get("chat_id", "") or "").strip()
        chat_id = self._resolve_memory_chat_id(raw_chat_id)
        proactive_quota = self._proactive_quota()
        if not same_intents_db and intents_mgr is not None and getattr(intents_mgr, "store", None) is not None:
            try:
                with intents_mgr.store.connect() as sdb:
                    s_rows = sdb.execute(
                        "SELECT DISTINCT chat_id FROM social_state WHERE locked = 1 ORDER BY updated DESC LIMIT 20"
                    ).fetchall()
                    for sr in s_rows:
                        rcid = str(sr["chat_id"] if hasattr(sr, "keys") else sr[0]).strip()
                        if rcid:
                            unreplied_raw.append(rcid)
            except sqlite3.Error:
                unreplied_raw = []
        unreplied_chats: list[str] = []
        for rcid in unreplied_raw:
            canon = plugin.canonical_stream_spec(rcid) or rcid
            if canon not in unreplied_chats:
                unreplied_chats.append(canon)
        mem_ctx = plugin.memory.context(chat_id)
        _, hb_plan = MemoryLayers.heartbeat_parts(str((mem_ctx or {}).get("heartbeat") or ""))
        proactive_status = {
            **proactive_quota,
            "unreplied_chats": unreplied_chats,
            "heartbeat_enabled": bool(getattr(plugin.settings, "heartbeat_enabled", True)),
            "heartbeat_has_plan": bool(hb_plan.strip()),
            "sleep_window": str(getattr(plugin.settings, "sleep_window", "") or ""),
        }
        return {
            "chats": sorted(deduped),
            "_source_chat_ids": source_ids,
            "context": mem_ctx,
            "results": plugin.memory.search(options.get("query", ""), chat_id)
            if chat_id
            else [],
            "experiences": plugin.threads.search(
                options.get("query", ""), chat_id, scope="self"
            ),
            "workspaces": workspaces,
            "proactive_quota": proactive_quota,
            "proactive_status": proactive_status,
            "pending_skill_proposals": pending_props,
            "profiles": [
                {
                    "id": p.id,
                    "title": p.title or p.id,
                    "fish_seed": getattr(p, "fish_seed", "") or "",
                }
                for p in (
                    plugin.profiles.list_profiles()
                    if getattr(plugin, "profiles", None) is not None
                    else ()
                )
            ],
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
        skill_by_author: dict[str, tuple[str, dict, dict | None]] = {}
        try:
            sk_rows = db.execute(
                "SELECT target, version, content, author, updated FROM memory_versions "
                "WHERE kind='skill' AND document='SKILL.md' ORDER BY target ASC, version DESC"
            ).fetchall()
            sk_hist_map: dict[str, list[dict]] = {}
            for sr in sk_rows:
                sk_hist_map.setdefault(str(sr["target"] or ""), []).append(dict(sr))
            for sk_target, sk_list in sk_hist_map.items():
                for idx, entry in enumerate(sk_list):
                    auth = str(entry.get("author") or "")
                    if auth.startswith("thread:") and auth not in skill_by_author:
                        prev_entry = sk_list[idx + 1] if idx + 1 < len(sk_list) else None
                        skill_by_author[auth] = (sk_target, entry, prev_entry)
        except sqlite3.Error:
            skill_by_author = {}

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

            sk_hit = skill_by_author.get(f"thread:{tid}") or skill_by_author.get(tid)
            sk_target = ""
            sk_title = ""
            sk_matched = None
            summary = self._note_diff_excerpt(profile, matched, prev, thread_summary)
            if sk_hit is not None:
                sk_target, sk_matched, sk_prev = sk_hit
                sk_meta = parse_skill_meta(sk_target, str(sk_matched.get("content") or ""))
                sk_title = sk_meta.get("title") or sk_target
                prev_lines = (
                    {ln.strip() for ln in str(sk_prev.get("content") or "").splitlines() if ln.strip()}
                    if sk_prev is not None
                    else set()
                )
                cur_lines = [ln.strip() for ln in str(sk_matched.get("content") or "").splitlines() if ln.strip()]
                added_lines = [
                    ln.lstrip("-*• ").strip()
                    for ln in cur_lines
                    if ln not in prev_lines and not ln.startswith("#")
                ]
                sk_added = added_lines[0] if added_lines else thread_summary
                summary = self._compact_receipt_text(
                    self._safe_redact(f"已补入技能「{sk_title}」：{sk_added}"), limit=100
                )

            raw_topic = self._compact_receipt_text(self._safe_redact(input_text), limit=48)
            prof_label = title_cache.get(profile, profile)
            topic = raw_topic or summary or (f"{prof_label} 笔记修订" if prof_label else "自动笔记回执")

            raw_at = (sk_matched or matched or {}).get("updated") or row["updated"] or 0
            at = float(raw_at) if raw_at else 0.0
            can_undo = bool(status == "applied" and ((profile and matched is not None) or sk_matched is not None))

            items.append(
                {
                    "thread_id": tid,
                    "revision": int(row["revision"] or 0),
                    "status": status,
                    "profile": profile,
                    "chat_id": chat_id,
                    "topic": topic,
                    "summary": summary,
                    "version": (sk_matched or matched)["version"] if (sk_matched or matched) is not None else None,
                    "history_id": f"thread:{tid}" if can_undo else "",
                    "skill_id": sk_target,
                    "skill_title": sk_title,
                    "asset_kind": "skill" if sk_matched is not None else "notes",
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
        mcp_items = []
        for name, spec in sorted(servers.items()):
            if not isinstance(spec, dict):
                continue
            transport = "http" if (spec.get("transport") == "http" or spec.get("url")) else "stdio"
            env_keys = list(
                spec.get("inline_env_keys")
                or (list((spec.get("credentials") or {}).keys()) if isinstance(spec.get("credentials"), dict) else [])
            )
            header_keys = list(
                spec.get("inline_header_keys")
                or (list((spec.get("header_credentials") or {}).keys()) if isinstance(spec.get("header_credentials"), dict) else [])
            )
            mcp_items.append(
                {
                    "id": name,
                    "name": name,
                    "transport": transport,
                    "command": list(spec.get("command") or []),
                    "url": str(spec.get("url") or ""),
                    "description": str(spec.get("description") or ""),
                    "readonly": bool(spec.get("readonly", True)),
                    "access": str(spec.get("access") or "admins"),
                    "tools": list(spec.get("tools") or []),
                    "credentials": sorted((spec.get("credentials") or {}).values())
                    if isinstance(spec.get("credentials"), dict)
                    else [],
                    "inline_env_keys": env_keys,
                    "inline_header_keys": header_keys,
                }
            )
        api_items = [
            {
                "id": name,
                "name": name,
                "api": str(spec.get("api_name") or spec.get("api") or name),
                "api_name": str(spec.get("api_name") or spec.get("api") or name),
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
            visual: bool = False,
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
                    "visual": bool(visual),
                    "assigned_tasks": tasks_clean,
                }
            else:
                if identifier and not existing["identifier"]:
                    existing["identifier"] = identifier.strip()
                if provider and not existing["provider"]:
                    existing["provider"] = provider.strip()
                if visual:
                    existing["visual"] = True
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
                        visual=bool(item.get("visual", False)),
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
                                    "visual": bool(val.get("visual", False)),
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
                for item in stat_items[:512]:
                    if isinstance(item, str) and item.strip():
                        probe_entries.append(
                            {
                                "name": item.strip(),
                                "identifier": "",
                                "provider": "",
                                "visual": False,
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
                                    "visual": bool(item.get("visual", False)),
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
                        visual=bool(item.get("visual", False)),
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
            "chat_sources",
        }
    )

    def _distilled_threads_path(self) -> Path:
        return Path(self.plugin.data_root) / "distilled_threads.json"

    def _load_distilled_threads(self) -> set[str]:
        path = self._distilled_threads_path()
        if not path.is_file() or path_is_link_like(path):
            return set()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        items = raw.get("threads") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return set()
        return {str(x).strip() for x in items if str(x).strip()}

    def _record_distilled_thread(self, thread_id: str) -> None:
        tid = str(thread_id or "").strip()
        if not tid:
            return
        path = self._distilled_threads_path()
        if path_is_link_like(path):
            return
        cur = sorted(self._load_distilled_threads() | {tid})[-256:]
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_private_write(
            path, json.dumps({"threads": cur}, ensure_ascii=False).encode("utf-8")
        )

    def _match_umbrella_skill_sync(
        self, text_blob: str, bound_skills: list[str]
    ) -> tuple[str, str]:
        mem = getattr(self.plugin, "memory", None)
        if mem is None:
            return "", ""
        all_ids = list(mem.skills())
        briefs = {b["id"]: b for b in mem.skill_briefs(all_ids)}
        hay = text_blob.lower()
        best_id, best_score = "", 0
        for sid in all_ids:
            info = briefs.get(sid) or {}
            title = str(info.get("title") or sid)
            desc = str(info.get("description") or "")
            score = 0
            if sid in bound_skills:
                score += 1
            tokens = [
                t
                for t in re.split(r"[^a-z0-9\u4e00-\u9fff]+", f"{sid} {title} {desc}".lower())
                if len(t) >= 2 and t not in {"工作", "规范", "指南", "方法", "使用", "进行", "流程", "任务", "skill"}
            ]
            hits = sum(1 for tok in set(tokens) if tok in hay)
            score += hits * 2
            if score > best_score and hits >= 2:
                best_id, best_score = sid, score
        if best_id:
            return best_id, str((briefs.get(best_id) or {}).get("title") or best_id)
        return "", ""

    def _extract_thread_skill_signals_sync(self, db, tid: str, full_row: dict) -> dict:
        history = full_row.get("history") if isinstance(full_row.get("history"), list) else []
        res = full_row.get("result") if isinstance(full_row.get("result"), dict) else {}
        res_text = str(res.get("text") or "").strip()
        snap = full_row.get("profile_snapshot") if isinstance(full_row.get("profile_snapshot"), dict) else {}
        bound_skills = [str(s) for s in (snap.get("skills") or ()) if str(s).strip()]

        ev_rows = db.execute(
            "SELECT kind, payload FROM thread_events WHERE thread_id=? ORDER BY seq LIMIT 160",
            (tid,),
        ).fetchall()
        tool_names: list[str] = []
        failed_tools: set[str] = set()
        recovered = False
        steered = False
        for ev in ev_rows:
            ek = str(ev["kind"] or "")
            if ek in {"steer", "steer_interrupted"}:
                steered = True
            try:
                ep = json.loads(ev["payload"] or "{}")
            except Exception:
                ep = {}
            if not isinstance(ep, dict):
                continue
            if ek == "tool" and ep.get("tool"):
                tn = str(ep["tool"]).strip()
                if tn and tn not in tool_names:
                    tool_names.append(tn)
            elif ek == "tools":
                cmap = {
                    c.get("id"): (c.get("function") or {}).get("name")
                    for c in (ep.get("calls") or ())
                    if isinstance(c, dict)
                }
                for r in ep.get("results") or ():
                    if not isinstance(r, dict):
                        continue
                    tn = str(cmap.get(r.get("call_id")) or "").strip()
                    if tn and tn not in tool_names:
                        tool_names.append(tn)
                    val = r.get("value")
                    is_err = isinstance(val, dict) and bool(
                        val.get("error") or val.get("exit_code", 0) != 0 or val.get("ok") is False
                    )
                    if is_err and tn:
                        failed_tools.add(tn)
                    elif tn and tn in failed_tools:
                        recovered = True

        user_msgs = [
            str(m.get("content") or "").strip()
            for m in history
            if isinstance(m, dict) and m.get("role") == "user" and str(m.get("content") or "").strip()
        ]
        for msg in history:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or ():
                    if isinstance(tc, dict):
                        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                        tn = str((fn or {}).get("name") or "").strip()
                        if tn and tn not in tool_names:
                            tool_names.append(tn)

        signals: list[str] = []
        if steered or len(user_msgs) >= 2:
            signals.append("包含转向修正")
        if recovered:
            signals.append("排错恢复经验")
        if len(tool_names) >= 2:
            signals.append(f"{len(tool_names)} 项工具链")
        elif tool_names:
            signals.append(f"调用 {tool_names[0]}")
        if res_text:
            signals.append("已产出结论")

        worth = bool(recovered or steered or tool_names or (user_msgs and res_text))
        blob = " ".join([*user_msgs[:3], res_text[:300], *tool_names])
        matched_id, matched_title = self._match_umbrella_skill_sync(blob, bound_skills)
        return {
            "worth": worth,
            "signals": signals,
            "tool_names": tool_names,
            "steered": steered,
            "recovered": recovered,
            "bound_skills": bound_skills,
            "matched_skill": matched_id,
            "matched_skill_title": matched_title,
            "recommended_op": "patch" if matched_id else "create",
        }

    def _distill_candidates_sync(self) -> list[dict]:
        threads_mgr = getattr(self.plugin, "threads", None)
        if threads_mgr is None:
            return []
        handled = self._load_distilled_threads()
        rows = threads_mgr.list_threads(limit=30)
        out: list[dict] = []
        with threads_mgr.connect() as db:
            for row in rows:
                tid = str(row.get("thread_id") or "").strip()
                if not tid or tid in handled:
                    continue
                state = str(row.get("state") or "").strip().lower()
                if state in {"active", "running", "waiting"}:
                    continue
                try:
                    full_row = threads_mgr.get(tid, "", allow_self=True, allow_any_chat=True)
                except Exception:
                    continue
                sig_info = self._extract_thread_skill_signals_sync(db, tid, full_row)
                if not sig_info["worth"]:
                    continue
                res = full_row.get("result") if isinstance(full_row.get("result"), dict) else {}
                res_text = str(res.get("text") or "").strip()
                raw_title = str(row.get("title") or "").strip()
                if not raw_title and not res_text:
                    continue
                title = sanitize_export_text(self.plugin, raw_title or tid)[:80]
                summary = sanitize_export_text(
                    self.plugin, res_text or full_row.get("summary") or raw_title
                ).replace("\n", " ")[:180]
                out.append(
                    {
                        "thread_id": tid,
                        "title": title,
                        "summary": summary,
                        "profile_id": str(row.get("profile") or "default"),
                        "chat_id": str(row.get("chat_id") or ""),
                        "state": state,
                        "updated": float(row.get("updated") or 0.0),
                        "signals": sig_info["signals"],
                        "recommended_op": sig_info["recommended_op"],
                        "matched_skill": sig_info["matched_skill"],
                        "matched_skill_title": sig_info["matched_skill_title"],
                    }
                )
                if len(out) >= 6:
                    break
        return out

    async def _distill_skill_from_thread(self, options: dict) -> dict:
        tid = str(options.get("thread_id") or "").strip()
        if not tid:
            raise ValueError("请指定要提炼的线程 ID")
        if bool(options.get("dismiss")):
            await completed_thread(self._record_distilled_thread, tid)
            return {"dismissed": True, "thread_id": tid}
        preview_only = bool(options.get("preview_only"))
        row = await completed_thread(
            self.plugin.threads.get, tid, "", allow_self=True, allow_any_chat=True
        )
        def _inspect_sig():
            with self.plugin.threads.connect() as db:
                return self._extract_thread_skill_signals_sync(db, tid, row)

        sig_info = await completed_thread(_inspect_sig)
        history = row.get("history") if isinstance(row.get("history"), list) else []
        res = row.get("result") if isinstance(row.get("result"), dict) else {}
        user_prompts: list[str] = []
        assistant_notes: list[str] = []
        for msg in history:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = str(msg.get("content") or "").strip()
            if role == "user" and content:
                user_prompts.append(sanitize_export_text(self.plugin, content)[:500])
            elif role == "assistant" and content:
                assistant_notes.append(sanitize_export_text(self.plugin, content)[:600])
        tool_names = list(sig_info["tool_names"])
        raw_res_text = sanitize_export_text(self.plugin, str(res.get("text") or "").strip())[:800]

        forced_mode = str(options.get("op") or options.get("mode") or "").strip().lower()
        matched_skill = str(options.get("matched_skill") or sig_info["matched_skill"] or "").strip()
        existing_skills = await completed_thread(self.plugin.memory.skills)
        existing_briefs = await completed_thread(self.plugin.memory.skill_briefs, existing_skills)
        existing_md = ""
        matched_meta: dict = {}
        if matched_skill and matched_skill in existing_skills:
            read_cur = await completed_thread(self.plugin.memory.read, "skill", target=matched_skill)
            existing_md = str(read_cur.get("content") or "")
            matched_meta = next((b for b in existing_briefs if b["id"] == matched_skill), {})

        op_mode = (
            forced_mode
            if forced_mode in {"patch", "create"}
            else ("patch" if (matched_skill and existing_md and preview_only) else "create")
        )

        raw_req_title = str(options.get("title") or "").strip()
        clean_goal = (user_prompts[0].splitlines()[0][:40] if user_prompts else "").strip()
        fallback_title = sanitize_export_text(
            self.plugin, raw_req_title or (matched_meta.get("title") if op_mode == "patch" else "") or clean_goal or "通用作业规范"
        ).strip() or "通用作业规范"
        fallback_desc = sanitize_export_text(
            self.plugin,
            str(options.get("description") or "").strip()
            or (
                str(matched_meta.get("description") or "")
                if op_mode == "patch" and matched_meta.get("description")
                else f"适用时机：处理与「{fallback_title[:24]}」同类任务时启用。按步骤核验并交付结构化结果。"
            ),
        )[:200]

        llm_op = ""
        llm_slug = ""
        llm_title, llm_desc, llm_rules, llm_pitfalls, llm_ref_path, llm_ref_body, llm_rationale = (
            "",
            "",
            [],
            [],
            "",
            "",
            "",
        )
        ctx = getattr(self.plugin, "ctx", None)
        if ctx is not None and not options.get("offline") and not options.get("content"):
            try:
                digest_input = json.dumps(
                    {
                        "user_requests": user_prompts[:3],
                        "tools_used": tool_names[:12],
                        "signals": sig_info["signals"],
                        "outcome": raw_res_text or "\n".join(assistant_notes[-2:]),
                        "existing_skills": [
                            {"id": b["id"], "title": b.get("title"), "description": b.get("description", "")[:100]}
                            for b in existing_briefs[:16]
                        ],
                        "matched_skill": matched_skill,
                    },
                    ensure_ascii=False,
                )
                sys_prompt = (
                    "你是参考 Hermes 规范的技能复盘提炼器。请把本次活动提炼为跨会话可复用的类级程序性记忆（写祈使句规则与避坑指南，严禁写成单次任务流水账，严禁保留群号/账号/路径/密钥）。\n"
                    "优先级：1. 若 existing_skills 中已有同领域技能，选 op='patch' 并给出要增补的祈使句规则与避坑项；2. 若无覆盖，选 op='create' 创建类级技能（slug 用英文小写连字符如 network-rule-check，禁止用今天具体事件命名）。\n"
                    "仅输出 JSON：{\"op\":\"patch|create\",\"target_skill\":\"已有ID或新英文slug\",\"title\":\"类级中文名\",\"description\":\"适用时机：... 执行行为：...\",\"rules\":[\"祈使句规则1\",\"规则2\"],\"pitfalls\":[\"避坑要点与原因\"],\"rationale\":\"一句话说明为何合并到已有技能或新建类级技能\"}"
                )
                llm_res = await asyncio.wait_for(
                    call_capability(
                        ctx,
                        "llm.generate_with_tools",
                        timeout_ms=12000,
                        messages=[
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": digest_input},
                        ],
                        tools=[],
                    ),
                    timeout=12.0,
                )
                raw_resp = (
                    str(llm_res.get("response") or llm_res.get("content") or "")
                    if isinstance(llm_res, dict)
                    else ""
                ).strip()
                m = re.search(r"\{[\s\S]*\}", raw_resp)
                if m:
                    parsed_llm = json.loads(m.group(0))
                    if isinstance(parsed_llm, dict):
                        cand_op = str(parsed_llm.get("op") or "").strip().lower()
                        cand_target = str(parsed_llm.get("target_skill") or "").strip()
                        if cand_op in {"patch", "create"} and not forced_mode:
                            if cand_op == "patch" and cand_target in existing_skills:
                                llm_op = "patch"
                                matched_skill = cand_target
                                read_cur = await completed_thread(self.plugin.memory.read, "skill", target=matched_skill)
                                existing_md = str(read_cur.get("content") or "")
                                matched_meta = next((b for b in existing_briefs if b["id"] == matched_skill), {})
                            elif cand_op == "create":
                                llm_op = "create"
                                llm_slug = cand_target
                        llm_title = sanitize_export_text(self.plugin, str(parsed_llm.get("title") or ""))[:60]
                        llm_desc = sanitize_export_text(self.plugin, str(parsed_llm.get("description") or ""))[:200]
                        llm_rules = [
                            sanitize_export_text(self.plugin, str(r)).strip()
                            for r in (parsed_llm.get("rules") or parsed_llm.get("steps") or ())
                            if str(r).strip()
                        ]
                        llm_pitfalls = [
                            sanitize_export_text(self.plugin, str(p)).strip()
                            for p in (parsed_llm.get("pitfalls") or parsed_llm.get("tips") or ())
                            if str(p).strip()
                        ]
                        llm_rationale = sanitize_export_text(self.plugin, str(parsed_llm.get("rationale") or ""))[:160]
            except Exception:
                pass

        if llm_op and not forced_mode:
            op_mode = llm_op
        if op_mode == "patch" and (not matched_skill or not existing_md):
            op_mode = "create"

        final_title = llm_title or (str(matched_meta.get("title") or "") if op_mode == "patch" else "") or fallback_title
        final_desc = llm_desc or fallback_desc
        if not llm_rules:
            tool_hint = "、".join(tool_names) if tool_names else "会话检索与工作区工具"
            llm_rules = [
                f"先核清输入边界与目标格式（典型触发：{user_prompts[0][:80] if user_prompts else final_title}），缺省细节自行按常规补齐，不反复追问。",
                f"优先按顺序组合 {tool_hint} 获取一手依据，遇到失败或空结果时切换检索词重试后再下结论。",
                "输出结构化可复核结果，严格剥离单次会话群号、宿主机私密路径与凭据。",
            ]
        if not llm_pitfalls:
            llm_pitfalls = [
                "禁止把单次对话里的临时数值或私密上下文硬编码进通用流程；只保留可迁移的判断规则。"
            ]

        explicit_content = str(options.get("content") or "").strip()
        if explicit_content:
            full_md = sanitize_export_text(self.plugin, explicit_content)
        elif op_mode == "patch" and existing_md:
            addition_lines = [
                "",
                "## 实战增补规则与避坑要点",
                *[f"- {r}" for r in llm_rules[:5]],
                *[f"- 注意：{p}" for p in llm_pitfalls[:4]],
            ]
            addition_block = "\n".join(addition_lines)
            if "## 实战增补规则与避坑要点" in existing_md:
                full_md = existing_md.rstrip() + "\n" + "\n".join([f"- {r}" for r in llm_rules[:4]]) + "\n"
            else:
                full_md = existing_md.rstrip() + "\n" + addition_block + "\n"
        else:
            safe_yaml_title = final_title.replace('"', "'")
            safe_yaml_desc = final_desc.replace('"', "'")
            body_lines = [
                "## 适用时机与边界",
                final_desc,
                "",
                "## 核心执行规则",
                *[f"{idx}. {r}" for idx, r in enumerate(llm_rules[:6], 1)],
                "",
                "## 常见陷阱与避坑指南",
                *[f"- {p}" for p in llm_pitfalls[:5]],
            ]
            full_md = (
                f'---\nname: "{safe_yaml_title}"\ndescription: "{safe_yaml_desc}"\n---\n\n'
                f"# {final_title}\n\n" + "\n".join(body_lines) + "\n"
            )

        if op_mode == "patch":
            skill_id = matched_skill
            rationale = llm_rationale or f"检测到现有技能「{matched_meta.get('title') or matched_skill}」已覆盖该任务领域，按 Hermes 规范优先向原技能增补实战规则与避坑项，避免产生碎片化单次技能。"
        else:
            req_slug = str(options.get("target") or llm_slug or "").strip()
            if req_slug and SKILL_ID_RE.fullmatch(req_slug):
                base_slug = req_slug
            else:
                ascii_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", final_title).strip("_-").lower()[:32]
                if ascii_slug and SKILL_ID_RE.fullmatch(ascii_slug):
                    base_slug = ascii_slug
                else:
                    base_slug = f"workflow-{tid.replace('th_', '')[:8] or uuid.uuid4().hex[:6]}"
            existing_set = set(existing_skills)
            skill_id = base_slug
            if not options.get("overwrite"):
                suffix = 2
                while skill_id in existing_set:
                    skill_id = f"{base_slug[:54]}-{suffix}"
                    suffix += 1
            rationale = llm_rationale or "当前技能库尚无覆盖此类任务的主干技能，提炼为类级通用规范（仅保留祈使句规则与避坑指南，不记单次流水账）。"

        default_bind = list(
            dict.fromkeys(
                [
                    str(row.get("profile") or "default"),
                    *(
                        b_pid
                        for b_pid in (options.get("profile_ids") or ())
                        if str(b_pid).strip()
                    ),
                ]
            )
        )

        if preview_only:
            return {
                "preview": True,
                "thread_id": tid,
                "op": op_mode,
                "id": skill_id,
                "skill_id": skill_id,
                "matched_skill": matched_skill,
                "title": final_title,
                "description": final_desc,
                "existing_content": existing_md if op_mode == "patch" else "",
                "content": full_md,
                "signals": sig_info["signals"],
                "rationale": rationale,
                "suggested_profiles": default_bind,
                "version": (
                    (await completed_thread(self.plugin.memory.read, "skill", target=skill_id)).get("version")
                    if op_mode == "patch"
                    else None
                ),
            }

        if op_mode == "patch" and skill_id in set(existing_skills):
            cur_ver = (await completed_thread(self.plugin.memory.read, "skill", target=skill_id)).get("version")
            saved = await completed_thread(
                self.plugin.memory.write,
                "skill",
                full_md,
                target=skill_id,
                expected_version=cur_ver,
                author=f"distill:{tid}",
                allow_custom=True,
            )
            saved_ver = saved["version"]
        else:
            if skill_id in set(existing_skills) and options.get("overwrite"):
                cur_ver = (await completed_thread(self.plugin.memory.read, "skill", target=skill_id)).get("version")
                saved = await completed_thread(
                    self.plugin.memory.write,
                    "skill",
                    full_md,
                    target=skill_id,
                    expected_version=cur_ver,
                    author=f"distill:{tid}",
                    allow_custom=True,
                )
                saved_ver = saved["version"]
            else:
                created = await completed_thread(
                    self.plugin.memory.create_skill,
                    skill_id,
                    title=final_title,
                    description=final_desc,
                    content=full_md,
                    allow_custom=True,
                )
                skill_id = created["target"]
                saved_ver = created["version"]

        comp_path = str(options.get("companion_path") or llm_ref_path or "").strip()
        comp_body = str(options.get("companion_content") or llm_ref_body or "").strip()
        if comp_path and comp_body:
            await completed_thread(
                self.plugin.memory.write_skill_file,
                skill_id,
                comp_path,
                sanitize_export_text(self.plugin, comp_body),
                author=f"distill:{tid}",
            )

        await completed_thread(self._record_distilled_thread, tid)

        bind_pids = options.get("profile_ids")
        if isinstance(bind_pids, list):
            for pid in bind_pids:
                clean_pid = str(pid or "").strip()
                if clean_pid:
                    try:
                        await completed_thread(
                            self._set_profile_skill_binding, clean_pid, skill_id, True
                        )
                    except (ValueError, OSError):
                        pass

        return {
            "op": op_mode,
            "id": skill_id,
            "skill_id": skill_id,
            "title": final_title,
            "description": final_desc,
            "version": saved_ver,
            "content": full_md,
            "rationale": rationale,
            "signals": sig_info["signals"],
        }

    def _bind_skill_profiles_sync(self, options: dict) -> dict:
        skill_id = str(options.get("skill") or options.get("target") or "").strip()
        if not skill_id or skill_id not in self.plugin.memory.skills():
            raise ValueError("技能不存在")
        raw_pids = options.get("profile_ids")
        if not isinstance(raw_pids, list):
            raise ValueError("bind_skill_profiles 需要提供 profile_ids 列表")
        wanted = {str(p).strip() for p in raw_pids if str(p).strip()}
        bound_out: list[str] = []
        for path in sorted(self.plugin.profiles.path.iterdir()):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                prof = self.plugin.profiles.get(path.name)
            except (ValueError, OSError):
                continue
            should_bind = prof.id in wanted
            if should_bind != (skill_id in prof.skills):
                self._set_profile_skill_binding(prof.id, skill_id, should_bind)
            if should_bind:
                bound_out.append(prof.id)
        return {"skill": skill_id, "bound_profiles": bound_out}

    def _digest_overview_sync(self) -> list[dict]:
        rows_out: list[dict] = []
        seen: set[str] = set()
        if getattr(self.plugin, "threads", None) is None or getattr(self.plugin, "runtime", None) is None:
            return rows_out
        ws_root = getattr(getattr(getattr(self.plugin, "runtime", None), "files", None), "root", None)
        if ws_root is not None and not (ws_root / "workspaces").is_dir():
            return rows_out
        try:
            with self.plugin.threads.connect() as db:
                ws_rows = db.execute(
                    "SELECT thread_id, chat_id, owner FROM agent_workspaces WHERE name='digest' ORDER BY rowid DESC LIMIT 40"
                ).fetchall()
        except sqlite3.Error:
            ws_rows = []
        for ws in ws_rows:
            cid = str(ws["chat_id"] or "").strip()
            canon = "self" if cid == "self" else (self.plugin.canonical_stream_spec(cid) or cid)
            if not canon or canon in seen:
                continue
            try:
                t_row = self.plugin.threads.get(
                    ws["thread_id"], ws["chat_id"], allow_self=True, allow_any_chat=True
                )
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                continue
            if not t_row:
                continue
            try:
                with self.plugin.runtime.files.activity(t_row) as work:
                    src = DigestWorkspace(work)._load_sources()
            except (OSError, ValueError, RuntimeError):
                continue
            seen.add(canon)
            rows_out.append(
                {
                    "chat_id": canon,
                    "topics": [str(t) for t in (src.get("topics") or ()) if str(t or "").strip()],
                    "feeds": [
                        {
                            "id": str(f.get("id") or ""),
                            "title": str(f.get("title") or f.get("url") or ""),
                            "url": str(f.get("url") or ""),
                            "state": str(f.get("state") or "trial"),
                        }
                        for f in (src.get("feeds") or ())
                        if isinstance(f, dict) and (f.get("url") or f.get("id"))
                    ][:24],
                }
            )
        return rows_out

    async def _read_profiles_payload(self, *, include_chat_sources: bool = False) -> dict:
        result = await completed_thread(self._assets)
        known_chats = await completed_thread(self._known_chats_sync)
        result["known_chats"] = known_chats
        host_models = await self._collect_host_models()
        result["available_models"] = host_models["names"]
        result["model_catalog"] = host_models["models"]
        result["model_tasks"] = host_models["tasks"]
        result["default_model"] = str(self.plugin.settings.default_model or "")
        result["connectors"] = await completed_thread(self._connectors_info)
        result["docker"] = await completed_thread(self._docker_status)
        result["admins_info"] = await completed_thread(self._admins_info)
        result["permission_mode"] = str(self.plugin.settings.permission_mode or "ask_risky")
        result["digest_overview"] = await completed_thread(self._digest_overview_sync)
        if include_chat_sources:
            src_ids = list(
                dict.fromkeys(
                    cid
                    for p in result.get("profiles") or ()
                    for cid in (p.get("allow_chats") or ())
                    if cid
                )
            )
            result["chat_sources"] = (
                await self.plugin._resolve_chat_sources(src_ids) if src_ids else {}
            )
        else:
            result["chat_sources"] = {}
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
            result = await self._read_profiles_payload(
                include_chat_sources=(section == "profiles")
            )
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
            connectors = await completed_thread(self._connectors_info)
            profile_cards = [
                {
                    "id": p["id"],
                    "name": p.get("title") or p["id"],
                    "fish_seed": p.get("fish_seed") or "",
                    "skills": list(p.get("skills") or ()),
                    "toolsets": list(p.get("toolsets") or ()),
                }
                for p in assets["profiles"]
            ]
            pmap = {p["id"]: p for p in profile_cards}
            enriched_skills = []
            for sk in assets["skill_items"]:
                used_by = list(sk.get("used_by") or ())
                enriched_skills.append(
                    {
                        **sk,
                        "category": "system" if sk.get("is_preset") else "personal",
                        "modified_preset": bool(sk.get("is_modified")),
                        "bound_profiles": [
                            {"id": pid, "name": pmap[pid]["name"]}
                            for pid in used_by
                            if pid in pmap
                        ],
                    }
                )
            sys_titles = {
                "context": "会话与记忆",
                "web": "联网搜索",
                "workspace": "文件读写",
                "exec": "命令运行",
                "schedule": "定时提醒",
            }
            system_tools = [
                {
                    "id": ts["id"],
                    "title": sys_titles.get(ts["id"], ts["id"]),
                    "description": ts["label"].split("：", 1)[-1]
                    if "：" in ts["label"]
                    else ts["label"],
                    "tools": list(ts["tools"]),
                    "bound_profiles": [
                        {"id": p["id"], "name": p["name"]}
                        for p in profile_cards
                        if ts["id"] in p["toolsets"]
                    ],
                }
                for ts in assets["toolsets"]
            ]
            external_services = [
                *[{"kind": "mcp", **item} for item in (connectors.get("mcp") or ())],
                *[
                    {"kind": "plugin_api", **item}
                    for item in (connectors.get("plugin_apis") or ())
                ],
            ]
            candidates = await completed_thread(self._distill_candidates_sync)
            cand_cids = list(
                dict.fromkeys(
                    c["chat_id"] for c in candidates if isinstance(c, dict) and c.get("chat_id")
                )
            )
            chat_sources = (
                await self.plugin._resolve_chat_sources(cand_cids)
                if cand_cids
                else {}
            )
            pending_proposals = await completed_thread(
                self.plugin.memory.list_skill_proposals, status="pending"
            )
            result = {
                "skills": enriched_skills,
                "skill_map": assets["skills"],
                "preset_skills": assets["preset_skills"],
                "system_tools": system_tools,
                "external_services": external_services,
                "pending_proposals": pending_proposals,
                "candidates": candidates,
                "profiles": [p["id"] for p in assets["profiles"]],
                "profile_cards": profile_cards,
                "chat_sources": chat_sources,
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
            recent_dms = (
                await completed_thread(self.plugin._recent_dms_sync, 16)
                if callable(getattr(self.plugin, "_recent_dms_sync", None))
                else []
            )
            admins_block = result.get("admins_info") or {}
            admin_specs = []
            if callable(getattr(self.plugin, "_admin_to_stream_spec", None)):
                admin_specs = [
                    self.plugin._admin_to_stream_spec(a)
                    for a in (
                        *(admins_block.get("host_admins") or ()),
                        *(admins_block.get("plugin_admins") or ()),
                        *(result.get("admins") or ()),
                    )
                ]
            src_ids = list(
                dict.fromkeys(
                    [
                        *(
                            ((result.get("settings") or {}).get("enabled_chats"))
                            or result.get("enabled_chats")
                            or ()
                        ),
                        *(
                            dm.get("chat_id")
                            for dm in recent_dms
                            if isinstance(dm, dict) and dm.get("chat_id")
                        ),
                        *(s for s in admin_specs if s),
                        *(
                            c.get("stream_spec") or c.get("chat_id")
                            for c in known_chat_rows[:24]
                            if isinstance(c, dict)
                            and (c.get("stream_spec") or c.get("chat_id"))
                        ),
                    ]
                )
            )
            sources = await self.plugin._resolve_chat_sources(src_ids)
            if callable(getattr(self.plugin, "_apply_sources_to_dms", None)):
                self.plugin._apply_sources_to_dms(recent_dms, sources)
            result["recent_dms"] = recent_dms
            result["chat_sources"] = sources
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
        elif section == "vault_item":
            handle = str(options.get("handle") or "").strip()
            meta = await completed_thread(self.plugin.vault.get_meta, handle)
            if meta is None:
                raise ValueError(f"未找到密码别名：{handle}")
            result = {
                "handle": meta["handle"],
                "updated_at": float(meta.get("updated_at") or 0.0),
            }
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
        elif section == "chat_sources":
            raw_ids = options.get("chat_ids")
            chat_ids = (
                [str(x).strip() for x in raw_ids if str(x).strip()][:48]
                if isinstance(raw_ids, list)
                else []
            )
            result = {
                "sources": await self.plugin._resolve_chat_sources(chat_ids),
            }
        elif section == "package_export_preview":
            pid = str(
                options.get("profile_id") or options.get("target") or ""
            ).strip()
            pkg_kind = str(
                options.get("package_kind") or options.get("kind") or "template"
            ).strip().lower()
            raw_skills = options.get("skill_ids")
            skill_ids = (
                [str(s).strip() for s in raw_skills if str(s).strip()]
                if isinstance(raw_skills, list)
                else None
            )
            result = await completed_thread(
                preview_export_package,
                self.plugin,
                pid,
                kind=pkg_kind,
                skill_ids=skill_ids,
            )
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
        self,
        stream,
        name: str,
        offset: int,
        *,
        chunk_size: int | None = None,
        total_hint: int | None = None,
        extra: dict | None = None,
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
            bin_win = max(1024, min(int(chunk_size), 196608)) if isinstance(chunk_size, int) else 196608
            chunk = raw[offset : offset + bin_win]
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
        txt_win = max(1024, min(int(chunk_size), 196608)) if isinstance(chunk_size, int) else 16000
        end = min(len(decoded), offset + txt_win)
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
        raw_chunk_sz = opts.get("chunk_size")
        req_chunk_sz = int(raw_chunk_sz) if type(raw_chunk_sz) is int and 1024 <= raw_chunk_sz <= 196608 else None

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
                    chunk_size=req_chunk_sz,
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
                    chunk_size=req_chunk_sz,
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
            ob_src_ids = list(
                dict.fromkeys(
                    [
                        *(view.get("enabled_chats") or ()),
                        *(
                            dm.get("chat_id")
                            for dm in (view.get("recent_dms") or ())
                            if isinstance(dm, dict) and dm.get("chat_id")
                        ),
                        *((view.get("known_chats") or ())[:16]),
                    ]
                )
            )
            if ob_src_ids and callable(getattr(self.plugin, "_resolve_chat_sources", None)):
                sources = await self.plugin._resolve_chat_sources(ob_src_ids)
                view["chat_sources"] = sources
                if callable(getattr(self.plugin, "_apply_sources_to_dms", None)):
                    self.plugin._apply_sources_to_dms(view.get("recent_dms"), sources)
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
        elif action == "package_export":
            pid = str(
                options.get("profile_id") or options.get("target") or ""
            ).strip()
            pkg_kind = str(
                options.get("package_kind") or options.get("kind") or "template"
            ).strip().lower()
            raw_skills = options.get("skill_ids")
            skill_ids = (
                [str(s).strip() for s in raw_skills if str(s).strip()]
                if isinstance(raw_skills, list)
                else None
            )
            pkg = await completed_thread(
                export_package, plugin, pid, kind=pkg_kind, skill_ids=skill_ids
            )
            result = {
                "profile_id": pkg["profile_id"],
                "kind": pkg["kind"],
                "kind_label": pkg["kind_label"],
                "filename": pkg["filename"],
                "base64": pkg["base64"],
                "mime": "application/zip",
                "bytes": pkg["bytes"],
                "sha256": pkg["sha256"],
                "manifest": pkg["manifest"],
                "desensitized": True,
            }
        elif action == "package_upload_chunk":
            result = await completed_thread(self._stage_package_chunk, options)
        elif action == "package_preview":
            tok = str(options.get("transfer_token") or "").strip()
            staged_bytes = None
            if tok:
                tr = self.transfers.get(tok)
                if not tr or tr.get("direction") != "package_stage":
                    raise ValueError("导入包传输令牌无效或已过期")
                staged_bytes = bytes(tr["data"])
            result = await completed_thread(
                preview_package,
                plugin,
                raw_bytes=staged_bytes,
                base64_data=str(options.get("base64") or options.get("data") or ""),
                remote_url=str(options.get("remote_url") or options.get("url") or ""),
                share_link=str(options.get("share_link") or ""),
            )
            result = {
                **result,
                "enabled_chats": list(plugin.settings.enabled_chats),
                **({"transfer_token": tok} if tok else {}),
            }
        elif action == "package_import":
            tok = str(options.get("transfer_token") or "").strip()
            staged_bytes = None
            if tok:
                tr = self.transfers.pop(tok, None)
                if not tr or tr.get("direction") != "package_stage":
                    raise ValueError("导入包传输令牌无效或已过期")
                staged_bytes = bytes(tr["data"])
            imported = await completed_thread(
                import_package_bundle,
                plugin,
                raw_bytes=staged_bytes,
                base64_data=str(options.get("base64") or options.get("data") or ""),
                remote_url=str(options.get("remote_url") or options.get("url") or ""),
                share_link=str(options.get("share_link") or ""),
                target_profile_id=str(
                    options.get("profile_id") or options.get("target") or ""
                ).strip(),
                overwrite=bool(options.get("overwrite", False)),
                schedule_target_chat=str(
                    options.get("schedule_target_chat") or ""
                ).strip(),
            )
            prof_out = (
                self._serialize_profile(plugin.profiles.get(imported["profile_id"]))
                if imported.get("profile_id")
                else None
            )
            result = {
                **imported,
                "id": imported.get("profile_id") or "",
                "profile": prof_out,
                "done": True,
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
        elif action == "bind_skill_profiles":
            result = await completed_thread(self._bind_skill_profiles_sync, options)
        elif action == "distill_skill_from_thread":
            result = await self._distill_skill_from_thread(options)
        elif action == "confirm_skill_proposal":
            prop_id = str(options.get("proposal_id") or "").strip()
            prop = await completed_thread(plugin.memory.get_skill_proposal, prop_id)
            if not prop:
                raise ValueError("未找到待确认的技能草稿")
            sk_id = str(options.get("skill_id") or prop["skill_id"]).strip()
            sk_title = str(options.get("title") or prop["title"]).strip()
            sk_desc = str(options.get("description") or prop["description"]).strip()
            sk_md = str(options.get("content") or prop["content"])
            if sk_id in await completed_thread(plugin.memory.skills):
                cur_sk = await completed_thread(plugin.memory.read, "skill", target=sk_id)
                saved = await completed_thread(
                    plugin.memory.write,
                    "skill",
                    sk_md,
                    target=sk_id,
                    expected_version=cur_sk["version"],
                    allow_custom=True,
                )
            else:
                saved = await completed_thread(
                    lambda: plugin.memory.create_skill(
                        sk_id,
                        title=sk_title,
                        description=sk_desc,
                        content=sk_md,
                        allow_custom=True,
                    )
                )
            bind_profiles = options.get("bind_profiles")
            if bind_profiles is None and prop.get("profile_id"):
                bind_profiles = [prop["profile_id"]]
            if isinstance(bind_profiles, list):
                for pid in bind_profiles:
                    p_str = str(pid or "").strip()
                    if p_str:
                        await completed_thread(
                            self._set_profile_skill_binding, p_str, sk_id, True
                        )
            await completed_thread(plugin.memory.resolve_skill_proposal, prop_id, "created")
            result = {
                "proposal_id": prop_id,
                "skill_id": sk_id,
                "version": saved.get("version", 1),
                "bound_profiles": list(bind_profiles or []),
            }
        elif action == "dismiss_skill_proposal":
            prop_id = str(options.get("proposal_id") or "").strip()
            ok = await completed_thread(
                plugin.memory.resolve_skill_proposal, prop_id, "dismissed"
            )
            result = {"proposal_id": prop_id, "dismissed": ok}
        elif action == "digest_sources_op":
            result = await handle_digest_sources(
                plugin,
                op=str(options.get("op") or "add").strip().lower(),
                target_chat=str(options.get("target_chat") or options.get("chat_id") or "").strip(),
                patch={
                    **({"topics": options["topics"]} if "topics" in options else {}),
                    **({"urls": options["urls"]} if "urls" in options else {}),
                    **({"feed_id": options["feed_id"]} if "feed_id" in options else {}),
                    **({"url": options["url"]} if "url" in options else {}),
                },
                current_chat_id="",
                current_platform="qq",
                resolve_chat_target_fn=resolve_chat_target,
                clean_str_list_fn=lambda v, label="列表": [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x or "").strip()],
            )
        elif action == "export_skill_zip":
            skill_id = str(options.get("skill") or options.get("target") or "").strip()
            if not skill_id:
                raise ValueError("export_skill_zip 需要指定技能编号")
            pkg = await completed_thread(
                export_package, plugin, "", kind="capability_pack", skill_ids=[skill_id]
            )
            result = {
                "skill_id": skill_id,
                "filename": pkg["filename"],
                "base64": pkg["base64"],
                "mime": "application/zip",
                "bytes": pkg["bytes"],
                "manifest": pkg["manifest"],
                "desensitized": True,
            }
        elif action == "save_skill_file":
            sk_id = str(options.get("target") or options.get("skill") or "").strip()
            rel_path = str(options.get("path") or "").strip()
            if not sk_id or not rel_path:
                raise ValueError("save_skill_file 需要提供 target 与 path")
            wr = await completed_thread(
                plugin.memory.write_skill_file,
                sk_id,
                rel_path,
                str(options.get("content") or ""),
            )
            files = await completed_thread(plugin.memory.skill_files, sk_id)
            comp_paths = [c["path"] for c in files[:20] if isinstance(c, dict) and c.get("path")]
            result = {**wr, "target": sk_id, "path": wr.get("file_path", rel_path), "companion_files": comp_paths}
        elif action == "delete_skill_file":
            sk_id = str(options.get("target") or options.get("skill") or "").strip()
            rel_path = str(options.get("path") or "").strip()
            if not sk_id or not rel_path:
                raise ValueError("delete_skill_file 需要提供 target 与 path")
            rm = await completed_thread(plugin.memory.remove_skill_file, sk_id, rel_path)
            files = await completed_thread(plugin.memory.skill_files, sk_id)
            comp_paths = [c["path"] for c in files[:20] if isinstance(c, dict) and c.get("path")]
            result = {**rm, "target": sk_id, "path": rm.get("removed", rel_path), "companion_files": comp_paths}
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
            if row["status"] not in {"pending", "paused"}:
                raise ValueError("意图不存在或已处理")
            if row["status"] == "paused":
                await completed_thread(
                    plugin.intents.resume, iid, row["target"], is_admin=True
                )

            has_pulse = getattr(plugin, "pulse", None) is not None

            def _mark_due_now():
                now_ts = plugin.intents.clock() - 1.0
                next_st = "claimed" if has_pulse else "pending"
                with plugin.intents.store.connect() as db, db:
                    db.execute(
                        "UPDATE intents SET due_at=?, status=? WHERE intent_id=?",
                        (now_ts, next_st, iid),
                    )
                return plugin.intents.get(iid)

            fresh = await completed_thread(_mark_due_now)
            if has_pulse:
                try:
                    await plugin.pulse._dispatch(fresh)
                except Exception:
                    await completed_thread(plugin.intents.finish, iid, "deferred")
                    raise
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
        elif action == "parse_mcp_json":
            raw_json = options.get("json_text")
            if raw_json is None:
                raw_json = options.get("payload")
            if raw_json is None:
                raise ValueError("请先粘贴 mcpServers 配置 JSON")
            result = await completed_thread(parse_mcp_json_payload, raw_json)
        elif action == "probe_mcp_service":
            raw_json = options.get("json_text")
            parsed_cfg = (
                await completed_thread(parse_mcp_json_payload, raw_json)
                if raw_json and not options.get("command") and not options.get("url")
                else None
            )
            svc_name = str(
                options.get("name") or (parsed_cfg or {}).get("name") or ""
            ).strip()
            svc_cfg = (
                (await completed_thread(plugin.runtime.services.load)).get("servers", {}).get(svc_name) or {}
                if svc_name
                else {}
            )
            raw_url = str(options.get("url") or (parsed_cfg or {}).get("url") or "").strip()
            transport = str(
                options.get("transport")
                or (parsed_cfg or {}).get("transport")
                or ("http" if raw_url else "stdio")
            ).strip().lower()

            def _extract_inline(raw_map: object) -> dict[str, str]:
                out: dict[str, str] = {}
                if isinstance(raw_map, dict):
                    for k, v in raw_map.items():
                        ks = str(k or "").strip()
                        if ks and ks != "__KEEP__" and v not in (None, "", "__KEEP__"):
                            out[ks] = str(v)
                return out

            inline_env = _extract_inline(options.get("env") or (parsed_cfg or {}).get("env"))
            inline_headers = _extract_inline(options.get("headers") or (parsed_cfg or {}).get("headers"))
            if transport == "http":
                probe_spec = {
                    "transport": "http",
                    "url": validate_mcp_http_url(raw_url),
                    "header_credentials": dict(svc_cfg.get("header_credentials") or {}),
                }
            else:
                raw_cmd = options.get("command") or (parsed_cfg or {}).get("command") or []
                resolved_cmd = resolve_mcp_command(raw_cmd)
                if not resolved_cmd or not is_absolute_program_path(resolved_cmd[0]):
                    raise ValueError(
                        f"未能找到可执行程序「{(resolved_cmd or [''])[0]}」，请填写本机程序绝对路径"
                    )
                probe_spec = {
                    "command": resolved_cmd,
                    "credentials": dict(svc_cfg.get("credentials") or {}),
                }
            probed = await plugin.runtime.services.probe_server(
                probe_spec,
                inline_env=inline_env or None,
                inline_headers=inline_headers or None,
                timeout=8.0,
            )
            result = {
                **probed,
                "name": svc_name,
                "resolved_command": probe_spec.get("command") or [],
            }
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
                "transport",
                "url",
                "headers",
                "credentials",
                "header_credentials",
                "inline_env_keys",
                "inline_header_keys",
                "tools",
                "readonly",
                "access",
                "memory_mib",
                "description",
            }:
                raise ValueError("MCP 服务含未知配置项")
            is_http = str(server.get("transport") or "").strip().lower() == "http" or bool(
                str(server.get("url") or "").strip()
            )
            if is_http:
                validate_mcp_http_url(str(server.get("url") or ""))
                if "headers" in server:
                    hdrs = server["headers"]
                    if not isinstance(hdrs, dict) or any(
                        not isinstance(k, str)
                        or not isinstance(v, str)
                        or any(bad in k or bad in v for bad in ("\r", "\n", "\x00"))
                        for k, v in hdrs.items()
                    ):
                        raise ValueError("远程 MCP 请求头配置无效")
            else:
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
            for handle in (server.get("credentials") or {}).values():
                self.plugin.vault._validate_handle(handle)
            for handle in (server.get("header_credentials") or {}).values():
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

    def _stage_package_chunk(self, options):
        self.transfers = {
            key: value
            for key, value in self.transfers.items()
            if value["expires"] > time.time()
        }
        token = str(options.get("token") or "").strip()
        if not token:
            if len(self.transfers) >= 4:
                raise ValueError("传输较多，请稍后再试")
            token = uuid.uuid4().hex
            self.transfers[token] = {
                "data": bytearray(),
                "expires": time.time() + 300,
                "direction": "package_stage",
            }
        transfer = self.transfers.get(token)
        if (
            not transfer
            or transfer.get("direction") != "package_stage"
            or options.get("offset", 0) != len(transfer["data"])
        ):
            raise ValueError("导入位置无效或传输过期")
        chunk = base64.b64decode(str(options.get("data") or ""), validate=True)
        if len(chunk) > 24576 or len(transfer["data"]) + len(chunk) > 4 * 1024**2:
            self.transfers.pop(token, None)
            raise ValueError("导入包超过 4 MiB 上限")
        transfer["data"].extend(chunk)
        return {
            "token": token,
            "offset": len(transfer["data"]),
            "done": bool(options.get("done")),
        }
