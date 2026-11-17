"""三层角色与能力包导出、脱敏、对齐预览与导入（Profile / Capability Pack / Template）。"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import ipaddress
import json
import re
import shutil
import socket
import ssl
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .abilities import (
    PROFILE_ID_RE,
    TOOLSETS,
    _ALLOWED_PROFILE_TOML_KEYS,
    _TOOLSET_LABELS,
    _serialize_profile_dict,
    _validate_instructions,
)
from .authz import approved_envelope, record_delegation
from .memory import SKILL_ID_RE, MemoryLayers, parse_skill_meta
from .pathsafe import ensure_dirs, path_is_link_like
from .tx_recovery import TxJournal, tx_lock
from .trust import format_stream_spec, parse_stream_spec
from .vault import atomic_private_write

PACKAGE_FORMAT = "maisya.package.v1"
PACKAGE_KINDS: tuple[str, ...] = ("profile", "capability_pack", "template")
PACKAGE_KIND_LABELS: dict[str, str] = {
    "profile": "角色身份",
    "capability_pack": "能力包",
    "template": "分享模板",
}

_MAX_ZIP_BYTES = 4 * 1024 * 1024
_MAX_ZIP_FILES = 96

_PEM_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE)
_TOKEN_PREFIX_RE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bgh[pousr]_[A-Za-z0-9]{16,}\b|\bAKIA[0-9A-Z]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{12,}\b)"
)
_URL_USERINFO_RE = re.compile(r"(://)([^/\s:@]+:[^/\s@]+)(@)")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret|password|passwd|cookie)\b(\s*[:=]\s*)([^\s&\"'`,;]+)"
)
_HOST_PATH_SCRUB_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:/(?:U"
    r"sers|home|root|opt|var/folders|var/log|private/var|etc/mai)/[^\s\"'<>`]+|[A-Za-z]:\\(?:U"
    r"sers|Windows|Program Files)\\[^\s\"'<>`]+)"
)
_STREAM_SPEC_SCRUB_RE = re.compile(
    r"\b(?:qq|discord|telegram|tg|weixin|wechat|feishu|lark|onebot|napcat|sandbox|console):(?:group|private):[A-Za-z0-9_.-]+\b",
    re.IGNORECASE,
)
_EMAIL_SCRUB_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PRIVATE_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, port: int = 443, *, timeout: float = 8.0, context=None):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip
        self._orig_host = host

    def connect(self):
        raw_sock = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        try:
            ctx = self._context or ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=self._orig_host)
        except BaseException:
            raw_sock.close()
            raise


def _resolve_public_ip(host: str, port: int) -> str:
    clean_host = str(host or "").strip().lower()
    if not clean_host or clean_host in {"localhost", "metadata.google.internal"}:
        raise ValueError("远程压缩包地址禁止访问本机或内网地址")
    try:
        ip_obj = ipaddress.ip_address(clean_host)
        if not (ip_obj.is_global and not ip_obj.is_multicast):
            raise ValueError("远程压缩包地址禁止访问内网、回环或保留 IP")
        return str(ip_obj)
    except ValueError as err:
        if "禁止" in str(err):
            raise
    try:
        infos = socket.getaddrinfo(clean_host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"无法解析远程主机 {clean_host}") from exc
    picked = ""
    for info in infos:
        ip_str = info[4][0]
        ip_obj = ipaddress.ip_address(ip_str)
        if not (ip_obj.is_global and not ip_obj.is_multicast):
            raise ValueError("远程域名解析包含非公网地址，已拒绝")
        if not picked:
            picked = ip_str
    if not picked:
        raise ValueError(f"远程主机 {clean_host} 未返回可用公网地址")
    return picked


def fetch_remote_package_zip(url: str, *, timeout: float = 8.0, _hops: int = 2) -> bytes:
    """仅允许公开 https:// 压缩包直链拉取，校验公网 IP 防 SSRF 并限制 4 MiB。"""
    clean_url = str(url or "").strip()
    parsed = urllib.parse.urlparse(clean_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ch.isspace() for ch in clean_url)
    ):
        raise ValueError("远程直链必须使用公开的 https:// 地址，不支持本地或明文 http://")
    port = parsed.port or 443
    pinned_ip = _resolve_public_ip(parsed.hostname, port)
    path_qs = parsed.path or "/"
    if parsed.query:
        path_qs += "?" + parsed.query
    conn = _PinnedHTTPSConnection(parsed.hostname, pinned_ip, port=port, timeout=timeout)
    try:
        conn.request(
            "GET",
            path_qs,
            headers={
                "Host": parsed.netloc,
                "User-Agent": "Maisya-Package/1.0",
                "Accept": "application/zip, application/octet-stream, */*",
            },
        )
        resp = conn.getresponse()
        if resp.status in {301, 302, 303, 307, 308}:
            if _hops <= 0:
                raise ValueError("远程直链重定向次数过多")
            loc = resp.getheader("Location") or ""
            next_url = urllib.parse.urljoin(clean_url, loc)
            return fetch_remote_package_zip(next_url, timeout=timeout, _hops=_hops - 1)
        if resp.status != 200:
            raise ValueError(f"下载远程能力包失败（HTTP {resp.status}）")
        raw = resp.read(_MAX_ZIP_BYTES + 1)
        if len(raw) > _MAX_ZIP_BYTES:
            raise ValueError("远程压缩包超过 4 MiB 上限")
        if not raw.startswith(b"PK"):
            raise ValueError("远程地址返回的内容不是有效的 .zip 压缩包")
        return raw
    except OSError as exc:
        raise ValueError(f"无法连接远程地址：{exc}") from exc
    finally:
        conn.close()


def _format_toml(data: dict[str, Any]) -> str:
    def _val(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(_val(x) for x in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)

    lines = [f"{k} = {_val(v)}" for k, v in data.items() if v is not None]
    return "\n".join(lines) + ("\n" if lines else "")


def _collect_sensitive_ids(plugin, extra_ids: tuple[str, ...] = ()) -> tuple[set[str], set[str]]:
    """收集已知会话/群/用户 ID 与管理员标识，供导出自动脱敏替换。"""
    chat_tokens: set[str] = set()
    id_tokens: set[str] = set()

    def _add_token(tok: str, *, is_chat: bool = False) -> None:
        clean = str(tok or "").strip()
        if not clean:
            return
        parsed = parse_stream_spec(clean)
        if parsed is not None:
            chat_tokens.add(clean)
            chat_tokens.add(format_stream_spec(*parsed))
            if len(parsed[2]) >= 5:
                id_tokens.add(parsed[2])
            return
        if ":" in clean:
            tail = clean.rsplit(":", 1)[-1].strip()
            if len(clean) >= 5:
                id_tokens.add(clean)
            if len(tail) >= 5:
                id_tokens.add(tail)
            return
        if len(clean) >= 5:
            (chat_tokens if is_chat else id_tokens).add(clean)

    settings = getattr(plugin, "settings", None)
    if settings is not None:
        for c in getattr(settings, "enabled_chats", ()) or ():
            _add_token(str(c), is_chat=True)
        for a in getattr(settings, "admins", ()) or ():
            _add_token(str(a), is_chat=False)

    admins_info = getattr(plugin, "admins_info", None)
    if isinstance(admins_info, dict):
        for key in ("host_admins", "plugin_admins", "effective_admins"):
            for a in admins_info.get(key) or ():
                _add_token(str(a), is_chat=False)

    messages = getattr(plugin, "messages", None)
    if messages is not None and callable(getattr(messages, "list_routes", None)):
        try:
            for r in messages.list_routes():
                if not isinstance(r, dict):
                    continue
                _add_token(str(r.get("session_id") or ""), is_chat=True)
                _add_token(str(r.get("group_id") or ""), is_chat=True)
                _add_token(str(r.get("user_id") or ""), is_chat=False)
                _add_token(str(r.get("account_id") or ""), is_chat=False)
        except Exception:
            pass

    for raw in extra_ids or ():
        _add_token(str(raw), is_chat=False)

    return chat_tokens, id_tokens


def sanitize_export_text(
    plugin, text: str, *, extra_ids: tuple[str, ...] = ()
) -> str:
    """对导出文本执行强脱敏：清理 Vault 秘密、Token、宿主路径、群号/私聊 ID 与内网 IP。"""
    cleaned = str(text or "")
    if not cleaned:
        return ""
    vault = getattr(plugin, "vault", None)
    if vault is not None:
        cleaned = str(vault.redact(cleaned))
    cleaned = _PEM_KEY_RE.sub("<PRIVATE_KEY>", cleaned)
    cleaned = _BEARER_RE.sub("Bearer <REDACTED>", cleaned)
    cleaned = _TOKEN_PREFIX_RE.sub("<REDACTED_TOKEN>", cleaned)
    cleaned = _URL_USERINFO_RE.sub(r"\1<REDACTED>\3", cleaned)
    cleaned = _KV_SECRET_RE.sub(r"\1\2<REDACTED>", cleaned)
    cleaned = _HOST_PATH_SCRUB_RE.sub("<PATH>", cleaned)
    cleaned = _STREAM_SPEC_SCRUB_RE.sub("<TARGET_CHAT>", cleaned)
    cleaned = _EMAIL_SCRUB_RE.sub("<EMAIL>", cleaned)
    cleaned = _PRIVATE_IP_RE.sub("<PRIVATE_IP>", cleaned)

    chat_tokens, id_tokens = _collect_sensitive_ids(plugin, extra_ids=extra_ids)
    for tok in sorted(chat_tokens, key=len, reverse=True):
        if len(tok) >= 5:
            cleaned = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])",
                "<TARGET_CHAT>",
                cleaned,
            )
    for tok in sorted(id_tokens, key=len, reverse=True):
        if len(tok) >= 5:
            cleaned = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])",
                "<REDACTED_ID>",
                cleaned,
            )

    if vault is not None and vault.contains_secret(cleaned.encode("utf-8")):
        raise ValueError("导出文本脱敏后仍命中已知凭据，已拒绝导出")
    return cleaned


def _validate_archive_bytes(plugin, content: bytes, label: str = "内容") -> bytes:
    vault = getattr(plugin, "vault", None)
    if vault is not None and vault.contains_secret(content):
        raise ValueError(f"{label}脱敏后仍含已知凭据，已中止导出")
    return content


def _sanitize_companion_bytes(
    plugin,
    raw_bytes: bytes,
    label: str,
    *,
    extra_ids: tuple[str, ...] = (),
) -> bytes:
    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _validate_archive_bytes(plugin, raw_bytes, label)
    clean_text = sanitize_export_text(plugin, decoded, extra_ids=extra_ids)
    return _validate_archive_bytes(plugin, clean_text.encode("utf-8"), label)


def _collect_profile_schedules(
    plugin,
    profile_id: str,
    *,
    extra_ids: tuple[str, ...],
    is_admin: bool = True,
    caller_chat_id: str = "",
    caller_platform: str = "",
) -> list[dict[str, Any]]:
    if not is_admin and not caller_chat_id:
        return []
    intents = getattr(plugin, "intents", None)
    if intents is None:
        return []
    allowed_targets: set[str] | None = None
    if caller_chat_id:
        aliases = {caller_chat_id}
        aliases_fn = getattr(plugin, "chat_aliases_for", None)
        if callable(aliases_fn):
            try:
                aliases.update(str(a or "").strip() for a in (aliases_fn(caller_chat_id, caller_platform) or ()) if a)
            except Exception:
                pass
        allowed_targets = {f"chat:{a}" for a in aliases if a}
        if caller_chat_id == "self":
            allowed_targets.add("self")
    out: list[dict[str, Any]] = []
    try:
        rows = intents.list_all(status="pending", limit=50)
    except Exception:
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if allowed_targets is not None and str(row.get("target") or "") not in allowed_targets:
            continue
        meta = (
            row.get("payload")
            if isinstance(row.get("payload"), dict)
            else (row.get("metadata") if isinstance(row.get("metadata"), dict) else {})
        )
        if not is_admin and bool(meta.get("scheduled_admin")):
            continue
        row_prof = str(row.get("profile") or meta.get("profile") or "").strip()
        if row_prof != profile_id:
            continue
        mode = str(row.get("mode") or "schedule").strip()
        if mode not in {"schedule", "watch", "wake"}:
            continue
        raw_prompt = str(
            meta.get("prompt")
            or meta.get("next_step")
            or row.get("condition")
            or row.get("reason")
            or ""
        ).strip()
        reason_clean = sanitize_export_text(
            plugin, str(row.get("reason") or ""), extra_ids=extra_ids
        )
        every = meta.get("every_seconds")
        cron = str(meta.get("cron") or "").strip()
        item: dict[str, Any] = {
            "mode": mode,
            "profile": profile_id,
            "target_chat": "<TARGET_CHAT>",
            "condition": sanitize_export_text(plugin, str(row.get("condition") or ""), extra_ids=extra_ids),
            "prompt": sanitize_export_text(plugin, raw_prompt, extra_ids=extra_ids),
        }
        if reason_clean:
            item["reason"] = reason_clean
        if isinstance(every, (int, float)) and int(every) > 0:
            item["every_seconds"] = int(every)
        if cron:
            item["cron"] = cron
        out.append(item)
    return out


def _skill_source_dir(plugin, skill_id: str, profile_id: str = "") -> tuple[Path, Path]:
    from .memory import PRESETS_DIR

    data_root = Path(plugin.data_root)
    candidates = [
        (data_root / "abilities", data_root / "abilities" / "skills" / skill_id),
        (data_root / "abilities", data_root / "abilities" / "profiles" / profile_id / "preset" / "skills" / skill_id)
        if profile_id
        else None,
        (PRESETS_DIR, PRESETS_DIR / "skills" / skill_id),
    ]
    for pair in candidates:
        if pair is None:
            continue
        base_root, skill_dir = pair
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists() or path_is_link_like(skill_md):
            return base_root, skill_dir
    raise ValueError(f"技能不存在：{skill_id}")


def export_package(
    plugin,
    profile_id: str = "",
    *,
    kind: str = "template",
    skill_ids: list[str] | tuple[str, ...] | None = None,
    is_admin: bool = True,
    caller_chat_id: str = "",
    caller_platform: str = "",
) -> dict[str, Any]:
    """按三层模型导出脱敏后的 .maisya.zip 与可选分享链接。"""
    pkg_kind = str(kind or "template").strip().lower()
    if pkg_kind not in PACKAGE_KINDS:
        raise ValueError(f"不支持的导出类型：{kind}（支持 {', '.join(PACKAGE_KINDS)}）")

    pid = str(profile_id or "").strip()
    if not pid and pkg_kind != "capability_pack":
        raise ValueError("导出角色或模板需要指定档案标识")

    extra_ids: list[str] = []
    profile = None
    if pid:
        profile = plugin.profiles.get(pid)
        pid = profile.id
        extra_ids.extend(profile.allow_chats)
        extra_ids.extend(profile.allow_users)
    extra_tuple = tuple(extra_ids)

    resolved_skills: list[str] = []
    if skill_ids is not None:
        for sid in skill_ids:
            clean_sid = str(sid or "").strip()
            if clean_sid and SKILL_ID_RE.fullmatch(clean_sid) and clean_sid not in resolved_skills:
                resolved_skills.append(clean_sid)
    elif profile is not None:
        resolved_skills = list(profile.skills)

    # 收集外部服务元数据（仅服务名称与说明/凭据句柄名，绝不包含秘密值）
    services_meta: list[dict[str, Any]] = []
    secret_handles: list[str] = list(profile.secrets) if profile is not None else []
    if profile is not None and getattr(plugin, "runtime", None) is not None:
        services_cfg = plugin.runtime.services.load() if getattr(plugin.runtime, "services", None) else {}
        servers = services_cfg.get("servers") if isinstance(services_cfg.get("servers"), dict) else {}
        apis = services_cfg.get("apis") if isinstance(services_cfg.get("apis"), dict) else {}
        for mcp_name in profile.mcp:
            spec = servers.get(mcp_name) if isinstance(servers.get(mcp_name), dict) else {}
            creds = spec.get("credentials") if isinstance(spec.get("credentials"), dict) else {}
            vault_obj = getattr(plugin, "vault", None)
            visible_handles: list[str] = []
            for env_name, handle in creds.items():
                h_str = str(handle or "").strip()
                if not h_str:
                    continue
                meta = vault_obj.get_meta(h_str) if vault_obj is not None else None
                if isinstance(meta, dict) and meta.get("kind") == "mcp_inline":
                    env_label = f"{mcp_name}.{env_name}"
                    if env_label not in visible_handles:
                        visible_handles.append(env_label)
                    continue
                visible_handles.append(h_str)
                if h_str not in secret_handles:
                    secret_handles.append(h_str)
            services_meta.append(
                {
                    "type": "mcp",
                    "id": mcp_name,
                    "description": sanitize_export_text(plugin, str(spec.get("description") or ""), extra_ids=extra_tuple),
                    "readonly": bool(spec.get("readonly", True)),
                    "credential_handles": sorted(set(visible_handles)),
                }
            )
        for api_name in profile.apis:
            spec = apis.get(api_name) if isinstance(apis.get(api_name), dict) else {}
            services_meta.append(
                {
                    "type": "plugin_api",
                    "id": api_name,
                    "description": sanitize_export_text(plugin, str(spec.get("description") or ""), extra_ids=extra_tuple),
                    "readonly": bool(spec.get("readonly", True)),
                }
            )

    schedules = (
        _collect_profile_schedules(
            plugin,
            pid,
            extra_ids=extra_tuple,
            is_admin=is_admin,
            caller_chat_id=caller_chat_id,
            caller_platform=caller_platform,
        )
        if pid
        else []
    )

    title = (
        sanitize_export_text(plugin, profile.title or pid, extra_ids=extra_tuple)
        if profile is not None
        else (resolved_skills[0] if len(resolved_skills) == 1 else "maisya-pack")
    )
    summary = (
        sanitize_export_text(plugin, profile.description, extra_ids=extra_tuple)
        if profile is not None
        else "可复用的 Maisya 能力包"
    )

    archive_files: dict[str, bytes] = {}
    if pkg_kind in {"profile", "template"} and profile is not None:
        profile_dir = plugin.profiles.path / pid
        md_raw = (profile_dir / "PROFILE.md").read_text(encoding="utf-8")
        md_clean = sanitize_export_text(plugin, md_raw, extra_ids=extra_tuple)
        _validate_instructions(md_clean)
        archive_files["profile/PROFILE.md"] = _validate_archive_bytes(
            plugin, md_clean.encode("utf-8"), "PROFILE.md"
        )
        safe_access = "admins" if profile.access == "listed" else profile.access
        profile_settings = {
            "id": pid,
            "title": sanitize_export_text(plugin, profile.title or pid, extra_ids=extra_tuple),
            "name": sanitize_export_text(plugin, profile.name or profile.title or pid, extra_ids=extra_tuple),
            "description": sanitize_export_text(plugin, profile.description, extra_ids=extra_tuple),
            "icon_shape": profile.icon_shape,
            "theme_color": profile.theme_color,
            "fish_seed": profile.fish_seed,
            "model_name": profile.model_name or profile.model,
            "fallback_models": list(profile.fallback_models),
            "task": profile.task,
            "environment": profile.environment,
            "execution_mode": profile.execution_mode,
            "permission_mode": profile.permission_mode,
            "session_mode": profile.session_mode,
            "access": safe_access,
            "enabled": bool(profile.enabled),
            "max_steps": int(profile.max_steps) if profile.max_steps is not None else None,
            "max_tokens": int(profile.max_tokens) if profile.max_tokens is not None else None,
            "context_tokens": (
                int(profile.context_tokens)
                if profile.context_tokens is not None
                else None
            ),
            "temperature": profile.temperature,
            **(
                {"coalesce_window_ms": int(profile.coalesce_window_ms)}
                if getattr(profile, "coalesce_window_ms", 0)
                else {}
            ),
        }
        toml_text = _format_toml(profile_settings)
        archive_files["profile/settings.toml"] = _validate_archive_bytes(
            plugin, toml_text.encode("utf-8"), "settings.toml"
        )
        if getattr(plugin, "memory", None) is not None and hasattr(
            plugin.memory, "read_profile_companions"
        ):
            for rel_comp, raw_comp in plugin.memory.read_profile_companions(pid):
                if not is_admin and rel_comp.startswith("references/"):
                    continue
                archive_files[f"profile_companions/{rel_comp}"] = (
                    _sanitize_companion_bytes(
                        plugin,
                        raw_comp,
                        f"profile/{rel_comp}",
                        extra_ids=extra_tuple,
                    )
                )

    skill_summaries: list[dict[str, str]] = []
    if pkg_kind in {"capability_pack", "template"}:
        system_tools = list(profile.toolsets) if profile is not None else []
        tools_explicit = list(profile.tools) if profile is not None else []
        pack_data = {
            "skills": resolved_skills,
            "system_tools": system_tools,
            "tools": tools_explicit,
            "mcp": list(profile.mcp) if profile is not None else [],
            "apis": list(profile.apis) if profile is not None else [],
            "required_permissions": {
                "environment": profile.environment if profile is not None else "readonly",
                "execution_mode": profile.execution_mode if profile is not None else "host",
                "access": (
                    "admins"
                    if (profile is not None and profile.access == "listed")
                    else (profile.access if profile is not None else "everyone")
                ),
                "network": profile.network if profile is not None else "public",
                "web_provider": profile.web_provider if profile is not None else "auto",
                "sandbox_targets": [
                    sanitize_export_text(plugin, t, extra_ids=extra_tuple)
                    for t in (profile.sandbox_targets if profile is not None else ())
                ],
                "secret_handles": secret_handles,
                "services_meta": services_meta,
            },
            "schedules": schedules,
        }
        archive_files["capabilities/pack.json"] = _validate_archive_bytes(
            plugin,
            json.dumps(pack_data, ensure_ascii=False, indent=2).encode("utf-8"),
            "pack.json",
        )
        for sid in resolved_skills:
            _, s_dir = _skill_source_dir(plugin, sid, profile_id=pid)
            skill_md_path = s_dir / "SKILL.md"
            raw_md = MemoryLayers._bytes(skill_md_path).decode("utf-8", errors="replace")
            clean_md = sanitize_export_text(plugin, raw_md, extra_ids=extra_tuple)
            meta = parse_skill_meta(sid, clean_md)
            skill_summaries.append(
                {
                    "id": sid,
                    "title": meta["title"],
                    "description": meta["description"],
                }
            )
            archive_files[f"skills/{sid}/SKILL.md"] = _validate_archive_bytes(
                plugin, clean_md.encode("utf-8"), f"{sid}/SKILL.md"
            )
            for extra_path in sorted(s_dir.rglob("*")):
                if extra_path == skill_md_path or extra_path.is_dir():
                    continue
                if path_is_link_like(extra_path):
                    continue
                rel_extra = extra_path.relative_to(s_dir)
                if any(part.startswith(".") for part in rel_extra.parts):
                    continue
                if not is_admin and rel_extra.parts[:1] == ("references",):
                    continue
                raw_extra = MemoryLayers._bytes(extra_path)
                archive_files[f"skills/{sid}/{rel_extra.as_posix()}"] = (
                    _sanitize_companion_bytes(
                        plugin,
                        raw_extra,
                        f"{sid}/{rel_extra.as_posix()}",
                        extra_ids=extra_tuple,
                    )
                )

    manifest = {
        "format": PACKAGE_FORMAT,
        "kind": pkg_kind,
        "kind_label": PACKAGE_KIND_LABELS[pkg_kind],
        "profile_id": pid,
        "title": title,
        "description": summary,
        "exported_at": int(time.time()),
        "desensitized": True,
        "skills": skill_summaries,
        "system_tools": list(profile.toolsets) if profile is not None else [],
        "required_secret_handles": secret_handles,
        "schedules_count": len(schedules),
    }
    archive_files["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2
    ).encode("utf-8")

    total_bytes = sum(len(v) for v in archive_files.values())
    if len(archive_files) > _MAX_ZIP_FILES or total_bytes > _MAX_ZIP_BYTES:
        raise ValueError("导出包体积超过安全限制（最大 4 MiB / 96 个文件）")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for arc_name in sorted(archive_files):
            zf.writestr(arc_name, archive_files[arc_name])
    zip_bytes = buf.getvalue()

    slug_base = pid or (resolved_skills[0] if resolved_skills else "maisya")
    safe_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", slug_base).strip("_-") or "maisya"
    filename = f"{safe_slug}-{pkg_kind}.maisya.zip"
    return {
        "kind": pkg_kind,
        "kind_label": PACKAGE_KIND_LABELS[pkg_kind],
        "profile_id": pid,
        "filename": filename,
        "zip_bytes": zip_bytes,
        "base64": base64.b64encode(zip_bytes).decode("ascii"),
        "bytes": len(zip_bytes),
        "sha256": hashlib.sha256(zip_bytes).hexdigest(),
        "manifest": manifest,
        "schedules": schedules,
        "files_count": len(archive_files),
        "desensitized": True,
    }


def preview_export_package(
    plugin,
    profile_id: str = "",
    *,
    kind: str = "template",
    skill_ids: list[str] | tuple[str, ...] | None = None,
    is_admin: bool = True,
    caller_chat_id: str = "",
    caller_platform: str = "",
) -> dict[str, Any]:
    """导出前的包内容与脱敏清单预览（不落盘）。"""
    pkg = export_package(
        plugin,
        profile_id,
        kind=kind,
        skill_ids=skill_ids,
        is_admin=is_admin,
        caller_chat_id=caller_chat_id,
        caller_platform=caller_platform,
    )
    manifest = pkg["manifest"]
    return {
        "ok": True,
        "kind": pkg["kind"],
        "kind_label": pkg["kind_label"],
        "profile_id": pkg["profile_id"],
        "filename": pkg["filename"],
        "bytes": pkg["bytes"],
        "files_count": pkg["files_count"],
        "title": manifest.get("title", ""),
        "description": manifest.get("description", ""),
        "has_profile": pkg["kind"] in {"profile", "template"},
        "has_capabilities": pkg["kind"] in {"capability_pack", "template"},
        "skills": manifest.get("skills") or [],
        "system_tools": manifest.get("system_tools") or [],
        "required_secret_handles": manifest.get("required_secret_handles") or [],
        "schedules": pkg.get("schedules") or [],
        "schedules_count": int(manifest.get("schedules_count") or 0),
        "desensitized": True,
    }


def _decode_package_bytes(
    *,
    raw_bytes: bytes | None = None,
    base64_data: str = "",
    remote_url: str = "",
    share_link: str = "",
) -> tuple[bytes, str]:
    remote = str(remote_url or share_link or "").strip()
    if remote:
        return fetch_remote_package_zip(remote), ""
    if isinstance(raw_bytes, (bytes, bytearray)) and len(raw_bytes) > 0:
        return bytes(raw_bytes), ""
    b64 = str(base64_data or "").strip()
    if b64:
        raw = base64.b64decode(b64, validate=True)
        if len(raw) > _MAX_ZIP_BYTES:
            raise ValueError("包文件超过 4 MiB 上限")
        return raw, ""
    raise ValueError("请上传 .maisya.zip / .zip 文件或提供公开 https:// 压缩包直链")


def _extract_safe_zip_map(plugin, raw_zip: bytes) -> dict[str, bytes]:
    if len(raw_zip) > _MAX_ZIP_BYTES:
        raise ValueError("导入包超过 4 MiB 上限")
    vault = getattr(plugin, "vault", None)
    files: dict[str, bytes] = {}
    total_size = 0
    with zipfile.ZipFile(io.BytesIO(raw_zip), "r") as zf:
        entries = zf.infolist()
        if len(entries) > _MAX_ZIP_FILES:
            raise ValueError("导入包内文件过多（最多 96 个）")
        seen: set[str] = set()
        for item in entries:
            name = item.filename
            if (
                not name
                or name.startswith("/")
                or "\\" in name
                or ":" in name
                or ".." in Path(name).parts
                or name in seen
                or (item.external_attr >> 16 & 0o170000) not in {0, 0o100000, 0o040000}
            ):
                raise ValueError("导入包包含非法路径或符号链接")
            seen.add(name)
            if item.is_dir():
                continue
            if item.file_size > _MAX_ZIP_BYTES:
                raise ValueError("导入包单个文件过大")
            total_size += item.file_size
            if total_size > _MAX_ZIP_BYTES:
                raise ValueError("导入包解压后超过 4 MiB 上限")
            data = zf.read(item)
            if len(data) > _MAX_ZIP_BYTES:
                raise ValueError("导入包单个文件实际大小超过限制")
            if vault is not None and vault.contains_secret(data):
                raise ValueError("导入包包含本机已知凭据明文，已拒绝")
            files[Path(name).as_posix()] = data
    return files


def _parse_package_map(plugin, files: dict[str, bytes], hint_kind: str = "") -> dict[str, Any]:
    if "manifest.json" in files:
        manifest = json.loads(files["manifest.json"].decode("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("format") != PACKAGE_FORMAT:
            raise ValueError("包清单格式不兼容")
        pkg_kind = str(manifest.get("kind") or hint_kind or "template").strip().lower()
        if pkg_kind not in PACKAGE_KINDS:
            raise ValueError(f"不支持的包类型：{pkg_kind}")
        profile_md = (
            files["profile/PROFILE.md"].decode("utf-8")
            if "profile/PROFILE.md" in files
            else ""
        )
        profile_settings = (
            tomllib.loads(files["profile/settings.toml"].decode("utf-8"))
            if "profile/settings.toml" in files
            else {}
        )
        pack_data = (
            json.loads(files["capabilities/pack.json"].decode("utf-8"))
            if "capabilities/pack.json" in files
            else {}
        )
        skills: dict[str, dict[str, Any]] = {}
        profile_companions: dict[str, bytes] = {}
        for arc, raw in files.items():
            parts = Path(arc).parts
            if len(parts) >= 3 and parts[0] == "skills":
                sid = parts[1]
                if not SKILL_ID_RE.fullmatch(sid):
                    raise ValueError(f"非法技能编号：{sid}")
                rel = "/".join(parts[2:])
                bucket = skills.setdefault(sid, {"skill_md": "", "companions": {}})
                if rel == "SKILL.md":
                    bucket["skill_md"] = raw.decode("utf-8")
                else:
                    bucket["companions"][rel] = raw
            elif len(parts) >= 3 and parts[0] == "profile_companions":
                profile_companions["/".join(parts[1:])] = raw
        pid = str(manifest.get("profile_id") or profile_settings.get("id") or "").strip()
        return {
            "legacy": False,
            "kind": pkg_kind,
            "kind_label": PACKAGE_KIND_LABELS[pkg_kind],
            "manifest": manifest,
            "profile_id": pid,
            "title": str(manifest.get("title") or profile_settings.get("title") or pid),
            "description": str(manifest.get("description") or profile_settings.get("description") or ""),
            "profile_md": profile_md,
            "profile_settings": profile_settings,
            "profile_companions": profile_companions,
            "pack": pack_data if isinstance(pack_data, dict) else {},
            "skills": skills,
            "raw_files": files,
        }

    # 兼容旧版 profile_export_zip / preset/skills 结构
    if "profile.toml" in files and "PROFILE.md" in files:
        cfg = tomllib.loads(files["profile.toml"].decode("utf-8"))
        md = files["PROFILE.md"].decode("utf-8")
        skills = {}
        for arc, raw in files.items():
            parts = Path(arc).parts
            if len(parts) >= 4 and parts[0] == "preset" and parts[1] == "skills":
                sid = parts[2]
                rel = "/".join(parts[3:])
                if SKILL_ID_RE.fullmatch(sid):
                    bucket = skills.setdefault(sid, {"skill_md": "", "companions": {}})
                    if rel == "SKILL.md":
                        bucket["skill_md"] = raw.decode("utf-8")
                    else:
                        bucket["companions"][rel] = raw
        for sid in cfg.get("skills") or ():
            skills.setdefault(str(sid), {"skill_md": "", "companions": {}})
        pid = str(cfg.get("id") or cfg.get("name") or "").strip()
        return {
            "legacy": True,
            "kind": "template",
            "kind_label": PACKAGE_KIND_LABELS["template"],
            "manifest": {},
            "profile_id": pid,
            "title": str(cfg.get("title") or cfg.get("name") or pid or "导入角色"),
            "description": str(cfg.get("description") or ""),
            "profile_md": md,
            "profile_settings": cfg,
            "pack": {
                "skills": list(cfg.get("skills") or list(skills.keys())),
                "system_tools": list(cfg.get("toolsets") or ()),
                "tools": list(cfg.get("tools") or ()),
                "required_permissions": {
                    "environment": str(cfg.get("environment") or "readonly"),
                    "access": str(cfg.get("access") or "everyone"),
                    "secret_handles": list(cfg.get("secrets") or ()),
                },
                "schedules": [],
            },
            "skills": skills,
            "raw_files": files,
        }

    raise ValueError("无法识别的导入包格式：缺少 manifest.json 或 profile.toml")


def preview_package(
    plugin,
    *,
    raw_bytes: bytes | None = None,
    base64_data: str = "",
    remote_url: str = "",
    share_link: str = "",
) -> dict[str, Any]:
    """解析导入包并返回与当前宿主的对齐预览。"""
    raw_zip, hint_kind = _decode_package_bytes(
        raw_bytes=raw_bytes,
        base64_data=base64_data,
        remote_url=remote_url,
        share_link=share_link,
    )
    file_map = _extract_safe_zip_map(plugin, raw_zip)
    parsed = _parse_package_map(plugin, file_map, hint_kind=hint_kind)

    pack = parsed["pack"]
    req_perms = pack.get("required_permissions") if isinstance(pack.get("required_permissions"), dict) else {}
    req_handles = [
        str(h).strip()
        for h in (req_perms.get("secret_handles") or ())
        if str(h).strip()
    ]
    existing_handles = set(plugin.vault.handles()) if getattr(plugin, "vault", None) else set()
    missing_handles = [h for h in req_handles if h not in existing_handles]

    local_skills = set(plugin.memory.skills()) if getattr(plugin, "memory", None) else set()
    skill_preview = []
    for sid, sdata in parsed["skills"].items():
        meta = parse_skill_meta(sid, sdata.get("skill_md") or "")
        skill_preview.append(
            {
                "id": sid,
                "title": meta["title"],
                "description": meta["description"],
                "already_exists": sid in local_skills,
            }
        )

    sys_tools = [
        {
            "id": ts,
            "label": _TOOLSET_LABELS.get(ts, ts),
        }
        for ts in (pack.get("system_tools") or ())
        if ts in TOOLSETS
    ]
    pid = parsed["profile_id"]
    existing_profiles = set()
    if getattr(plugin, "profiles", None) and plugin.profiles.path.is_dir():
        existing_profiles = {
            p.name
            for p in plugin.profiles.path.iterdir()
            if p.is_dir() and not p.is_symlink() and (p / "profile.toml").is_file()
        }
    schedules = [s for s in (pack.get("schedules") or ()) if isinstance(s, dict)]

    return {
        "ok": True,
        "kind": parsed["kind"],
        "kind_label": parsed["kind_label"],
        "profile_id": pid,
        "title": parsed["title"],
        "description": parsed["description"],
        "has_profile": bool(parsed["profile_md"] or parsed["profile_settings"]),
        "skills": skill_preview,
        "system_tools": sys_tools,
        "schedules": schedules,
        "schedules_count": len(schedules),
        "required_secret_handles": req_handles,
        "missing_secret_handles": missing_handles,
        "existing_conflict": bool(pid and pid in existing_profiles),
        "model_name": str((parsed["profile_settings"] or {}).get("model_name") or ""),
    }


def _apply_imported_schedules(
    plugin,
    profile_id: str,
    schedules: list[dict[str, Any]],
    target_chat: str,
    *,
    tx: TxJournal | None = None,
) -> int:
    clean_chat = str(target_chat or "").strip()
    if not clean_chat or not schedules or getattr(plugin, "intents", None) is None:
        return 0
    parsed = parse_stream_spec(clean_chat)
    resolved_cid = (
        plugin.resolve_known_session_id(clean_chat)
        if hasattr(plugin, "resolve_known_session_id")
        else clean_chat
    )
    target_plat = parsed[0] if parsed else "qq"
    target_key = "self" if resolved_cid == "self" else f"chat:{resolved_cid}"
    count = 0
    now_ts = plugin.intents.clock()
    svc_cfg = (
        plugin.runtime.services.load()
        if (getattr(plugin, "runtime", None) and getattr(plugin.runtime, "services", None))
        else {}
    )
    sched_prof = plugin.profiles.get(profile_id)
    sched_env = approved_envelope(sched_prof, svc_cfg, target_chat=resolved_cid)
    for item in schedules[:12]:
        if not isinstance(item, dict):
            continue
        every_sec = int(item.get("every_seconds") or 0)
        if every_sec and every_sec < 300:
            every_sec = 300
        mode = str(item.get("mode") or "schedule").strip()
        if mode not in {"schedule", "watch", "wake"}:
            mode = "schedule"
        prompt = str(item.get("prompt") or item.get("reason") or "执行推荐定时任务").strip()
        reason = str(item.get("reason") or prompt[:120]).strip()
        cond = str(item.get("condition") or "").strip()
        created = plugin.intents.schedule(
            kind="proactive",
            source="workflow",
            target=target_key,
            due_at=now_ts + (every_sec or 3600),
            reason=reason[:500],
            condition=cond[:500],
            mode=mode,
            payload={
                "platform": target_plat,
                "profile": profile_id,
                "prompt": prompt[:2000],
                **({"every_seconds": every_sec} if every_sec else {}),
                "scheduled_admin": True,
                "issuer_admin": "console",
                "envelope": sched_env,
            },
            bypass_limits=True,
        )
        if created.get("intent_id") and tx is not None:
            tx.record_created_intent(created["intent_id"])
        if getattr(plugin, "grants", None) is not None and created.get("intent_id"):
            record_delegation(
                plugin.grants,
                subject_kind="intent",
                subject_id=created["intent_id"],
                issuer="console",
                chat_id=resolved_cid,
                target_chat=resolved_cid,
                profile_id=profile_id,
                envelope=sched_env,
                clock=plugin.intents.clock,
            )
        count += 1
    return count


def import_package_bundle(
    plugin,
    *,
    raw_bytes: bytes | None = None,
    base64_data: str = "",
    remote_url: str = "",
    share_link: str = "",
    target_profile_id: str = "",
    overwrite: bool = False,
    schedule_target_chat: str = "",
) -> dict[str, Any]:
    """导入三层包（Profile / Capability Pack / Template），保留脱敏边界并对齐本地环境。"""
    raw_zip, hint_kind = _decode_package_bytes(
        raw_bytes=raw_bytes,
        base64_data=base64_data,
        remote_url=remote_url,
        share_link=share_link,
    )
    file_map = _extract_safe_zip_map(plugin, raw_zip)
    parsed = _parse_package_map(plugin, file_map, hint_kind=hint_kind)

    pkg_kind = parsed["kind"]
    if parsed["legacy"]:
        target_id = str(target_profile_id or parsed["profile_id"] or f"imp_{uuid.uuid4().hex[:6]}").strip()
        prof = plugin.profiles.import_profile_zip(raw_zip, target_id, overwrite=overwrite)
        plugin.memory.seed_profile(prof.id)
        return {
            "ok": True,
            "kind": pkg_kind,
            "profile_id": prof.id,
            "imported_skills": list(prof.skills),
            "missing_secret_handles": [],
        }

    # 1. 提交任何写入前先完成全部静态、目标与权限校验
    existing_skills = set(plugin.memory.skills())
    valid_skill_items: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []
    for sid, sdata in parsed["skills"].items():
        if not SKILL_ID_RE.fullmatch(sid):
            raise ValueError(f"技能编号格式无效：{sid}")
        skill_md = str(sdata.get("skill_md") or "").strip()
        if not skill_md:
            continue
        plugin.memory._guard("skill", skill_md, "", "", allow_custom=True)
        meta = parse_skill_meta(sid, skill_md)
        sdir = Path(plugin.data_root) / "abilities" / "skills" / sid
        if path_is_link_like(sdir):
            raise ValueError("技能目录不能是符号链接")
        companions = sdata.get("companions") or {}
        if companions:
            for rel_sub, comp_bytes in companions.items():
                rel_parts = Path(rel_sub).parts
                if not rel_parts or any(p in {"", ".", ".."} or p.startswith(".") for p in rel_parts):
                    continue
                plugin.memory._guard_companion_bytes(rel_sub, comp_bytes)
                if path_is_link_like(sdir.joinpath(*rel_parts)):
                    raise ValueError("技能附属文件路径不能包含符号链接")
        valid_skill_items.append((sid, sdata, meta, skill_md))

    candidate_skills = existing_skills | {sid for sid, _, _, _ in valid_skill_items}
    pack = parsed["pack"]
    pack_skills = [str(s) for s in (pack.get("skills") or ()) if str(s) in (candidate_skills | set(parsed["skills"]))]
    pack_toolsets = [str(t) for t in (pack.get("system_tools") or ()) if t in TOOLSETS]
    req_perms = pack.get("required_permissions") if isinstance(pack.get("required_permissions"), dict) else {}
    req_handles = [str(h) for h in (req_perms.get("secret_handles") or ()) if str(h)]
    existing_handles = set(plugin.vault.handles()) if getattr(plugin, "vault", None) else set()
    missing_handles = [h for h in req_handles if h not in existing_handles]

    bind_pid = ""
    existing_prof = None
    raw_pid = ""
    target_dir: Path | None = None
    md_text = ""
    toml_text = ""
    if pkg_kind == "capability_pack":
        bind_pid = str(target_profile_id or "").strip()
        if bind_pid:
            existing_prof = plugin.profiles.get(bind_pid)
    else:
        raw_pid = str(target_profile_id or parsed["profile_id"] or f"tpl_{uuid.uuid4().hex[:6]}").strip()
        if not PROFILE_ID_RE.fullmatch(raw_pid):
            raw_pid = re.sub(r"[^A-Za-z0-9_-]+", "_", raw_pid).strip("_-") or f"tpl_{uuid.uuid4().hex[:6]}"
            if not raw_pid[0].isalpha():
                raw_pid = "p_" + raw_pid
        target_dir = plugin.profiles.path / raw_pid
        if path_is_link_like(target_dir):
            raise ValueError("角色目录不能是符号链接")
        if target_dir.exists() and not overwrite:
            raise ValueError(f"角色 {raw_pid} 已存在；请改名或勾选覆盖已有角色")

        settings_cfg = dict(parsed["profile_settings"] or {})
        settings_cfg.pop("id", None)
        settings_cfg.pop("allow_chats", None)
        settings_cfg.pop("enabled_chats", None)
        settings_cfg.pop("allow_users", None)
        if settings_cfg.get("access") == "listed":
            settings_cfg["access"] = "admins"
        if pkg_kind == "template":
            if pack_skills:
                settings_cfg["skills"] = list(dict.fromkeys(pack_skills))
            if pack_toolsets:
                settings_cfg["toolsets"] = pack_toolsets
            if pack.get("tools"):
                settings_cfg["tools"] = list(pack["tools"])
            if req_perms:
                if req_perms.get("environment"):
                    settings_cfg["environment"] = req_perms["environment"]
                if req_perms.get("execution_mode"):
                    settings_cfg["execution_mode"] = req_perms["execution_mode"]
                if req_perms.get("network"):
                    settings_cfg["network"] = req_perms["network"]
                if req_perms.get("web_provider"):
                    settings_cfg["web_provider"] = req_perms["web_provider"]
        if target_dir.exists() and overwrite:
            old_toml = target_dir / "profile.toml"
            if old_toml.is_file():
                old_cfg = tomllib.loads(old_toml.read_text(encoding="utf-8"))
                for key in ("allow_chats", "allow_users"):
                    if key in old_cfg:
                        settings_cfg[key] = old_cfg[key]

        settings_cfg = {
            k: v for k, v in settings_cfg.items() if k in _ALLOWED_PROFILE_TOML_KEYS
        }
        md_text = str(parsed["profile_md"] or f"# {settings_cfg.get('title') or raw_pid}\n").strip() + "\n"
        _validate_instructions(md_text)
        toml_text = _serialize_profile_dict(settings_cfg)
        tomllib.loads(toml_text)
        valid_prof_companions: dict[str, bytes] = {}
        for rel_sub, comp_bytes in (parsed.get("profile_companions") or {}).items():
            clean_sub = plugin.memory._validate_profile_companion_rel(rel_sub)
            plugin.memory._guard_companion_bytes(clean_sub, comp_bytes)
            valid_prof_companions[clean_sub] = comp_bytes

    # 2. 按事务日志提交技能与角色目录/能力包，任何一步或崩溃均支持原子恢复并暴露回滚故障
    imported_skills: list[str] = []
    skipped_skills: list[str] = []
    stage: Path | None = None
    backup_dir: Path | None = None
    with tx_lock(plugin.data_root):
        tx = TxJournal(plugin.data_root, kind="package_import")
        try:
            for sid, sdata, meta, skill_md in valid_skill_items:
                sdir = Path(plugin.data_root) / "abilities" / "skills" / sid
                if sid in existing_skills:
                    tx.backup_tree(f"skill_{sid}", sdir)
                    if overwrite:
                        cur = plugin.memory.read("skill", target=sid, document="SKILL.md")
                        tx.record_skill_overwrite(sid, int(cur["version"]))
                        plugin.memory.write(
                            "skill",
                            skill_md,
                            expected_version=cur["version"],
                            target=sid,
                            document="SKILL.md",
                            author="package_import",
                        )
                        imported_skills.append(sid)
                    else:
                        skipped_skills.append(sid)
                else:
                    tx.backup_tree(f"skill_{sid}", sdir)
                    tx.record_skill_create(sid)
                    plugin.memory.create_skill(
                        sid,
                        title=meta["title"],
                        description=meta["description"],
                        content=skill_md,
                        allow_custom=True,
                    )
                    imported_skills.append(sid)
                companions = sdata.get("companions") or {}
                if companions and not path_is_link_like(sdir):
                    for rel_sub, comp_bytes in companions.items():
                        rel_parts = Path(rel_sub).parts
                        if not rel_parts or any(p in {"", ".", ".."} or p.startswith(".") for p in rel_parts):
                            continue
                        parent = ensure_dirs(sdir, rel_parts[:-1], create=True, mode=0o700)
                        dest = parent / rel_parts[-1]
                        if path_is_link_like(dest):
                            continue
                        existed_dest = dest.exists()
                        if overwrite or not existed_dest:
                            atomic_private_write(dest, comp_bytes)

            if pkg_kind == "capability_pack":
                if existing_prof is not None and bind_pid:
                    tx.backup_tree(f"prof_{bind_pid}", plugin.profiles.path / bind_pid)
                    merged_skills = list(dict.fromkeys([*existing_prof.skills, *pack_skills, *imported_skills]))
                    merged_toolsets = list(dict.fromkeys([*existing_prof.toolsets, *pack_toolsets]))
                    plugin.profiles.update(
                        bind_pid,
                        toolsets=merged_toolsets or list(existing_prof.toolsets),
                        skills=merged_skills,
                    )
                scheduled_count = _apply_imported_schedules(
                    plugin,
                    bind_pid or (existing_prof.id if existing_prof is not None else "default"),
                    [s for s in (pack.get("schedules") or ()) if isinstance(s, dict)],
                    schedule_target_chat,
                    tx=tx,
                )
                tx.commit()
                return {
                    "ok": True,
                    "kind": pkg_kind,
                    "profile_id": bind_pid,
                    "imported_skills": imported_skills,
                    "skipped_skills": skipped_skills,
                    "missing_secret_handles": missing_handles,
                    "scheduled_count": scheduled_count,
                }

            assert target_dir is not None
            tx.backup_tree(f"prof_{raw_pid}", target_dir)
            stage = Path(tempfile.mkdtemp(prefix="pkg-import-", dir=plugin.profiles.path))
            atomic_private_write(stage / "PROFILE.md", md_text.encode("utf-8"))
            atomic_private_write(stage / "profile.toml", toml_text.encode("utf-8"))
            for rel_sub, comp_bytes in valid_prof_companions.items():
                rel_parts = Path(rel_sub).parts
                parent = ensure_dirs(stage, rel_parts[:-1], create=True, mode=0o700)
                dest = parent / rel_parts[-1]
                if not path_is_link_like(dest):
                    atomic_private_write(dest, comp_bytes)
            if target_dir.exists():
                backup_dir = Path(tempfile.mkdtemp(prefix="pkg-backup-", dir=plugin.profiles.path))
                shutil.rmtree(backup_dir)
                target_dir.rename(backup_dir)
            stage.rename(target_dir)
            prof = plugin.profiles.get(raw_pid)
            plugin.memory.seed_profile(prof.id)
            scheduled_count = _apply_imported_schedules(
                plugin,
                prof.id,
                [s for s in (pack.get("schedules") or ()) if isinstance(s, dict)],
                schedule_target_chat,
                tx=tx,
            )
            if backup_dir is not None and backup_dir.exists():
                shutil.rmtree(backup_dir)
            tx.commit()
            return {
                "ok": True,
                "kind": pkg_kind,
                "profile_id": prof.id,
                "imported_skills": imported_skills,
                "skipped_skills": skipped_skills,
                "missing_secret_handles": missing_handles,
                "scheduled_count": scheduled_count,
            }
        except BaseException:
            if backup_dir is not None and backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            tx.rollback(
                store_path=getattr(getattr(plugin, "threads", None), "path", None),
                memory=plugin.memory,
            )
            raise
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
