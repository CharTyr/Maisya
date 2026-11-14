"""管理员对话内配置与整形（settings / profile / skill / memory / overview / services）。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sqlite3
import time
import tomllib
from pathlib import Path

from .config import Settings
from .digest import DigestWorkspace
from .memory import MemoryLayers, parse_skill_meta
from .recipes import build_recipe_draft, serialize_profile_toml
from .scout import call_capability
from .trust import format_stream_spec, parse_stream_spec, session_rank_key
from .vault import atomic_private_write
from .workspaces import completed_thread

_SETTINGS_HISTORY_FILE = "settings_history.json"
_MAX_SETTINGS_HISTORY = 50
_CURRENT_CHAT_ALIASES = {"", "current", "here", "this", "当前", "本群", "当前会话"}
_LOCKOUT_MESSAGE = (
    "拒绝在聊天内关闭总开关（plugin.enabled=false 会锁死所有管理入口，请前往 WebUI「设置」操作）"
)
_PROFILE_TOML_KEYS = {
    "title",
    "model",
    "model_name",
    "fallback_models",
    "task",
    "max_tokens",
    "temperature",
    "max_steps",
    "web_provider",
    "environment",
    "tools",
    "access",
    "network",
    "mcp",
    "apis",
    "secrets",
    "template",
    "sandbox_targets",
    "bound",
    "enabled",
    "skills",
    "allow_users",
    "allow_chats",
    "fish_seed",
}
_MEMORY_KINDS = {"profile", "notes", "skill", "memory", "journal", "heartbeat"}


def _shaping_lock(plugin) -> asyncio.Lock:
    lock = getattr(plugin, "_shaping_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(plugin, "_shaping_lock", lock)
    return lock


def _redact(plugin, value):
    vault = getattr(plugin, "vault", None)
    return vault.redact(value) if vault is not None else value


def _assert_no_secret(plugin, value, label: str = "输入内容") -> None:
    vault = getattr(plugin, "vault", None)
    if vault is None:
        return
    raw = value if isinstance(value, (str, bytes)) else json.dumps(value, ensure_ascii=False)
    raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if vault.contains_secret(raw_bytes):
        raise ValueError(f"{label}包含已知凭据明文，已拒绝写入；凭据请用 /vault set 存入并通过句柄名引用")


def _clean_str_list(value, *, label: str = "列表") -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        raise ValueError(f"{label}必须是字符串或字符串列表")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _normalize_colon_token(raw: str) -> str:
    if ":" not in raw:
        return raw
    head, tail = raw.split(":", 1)
    return f"{head.strip().lower()}:{tail.strip()}"


def _stream_aliases(session_id: str, info: dict) -> set[str]:
    plat = str(info.get("platform") or "").strip().lower()
    gid = str(info.get("group_id") or "").strip()
    uid = str(info.get("user_id") or "").strip()
    aliases = {session_id}
    spec = str(info.get("stream_spec") or "").strip()
    if spec:
        aliases.add(spec)
    if gid:
        aliases.update({gid, f"group:{gid}"})
        if plat:
            aliases.update({f"{plat}:{gid}", format_stream_spec(plat, "group", gid)})
    elif uid:
        aliases.update({uid, f"private:{uid}"})
        if plat:
            aliases.update({f"{plat}:{uid}", format_stream_spec(plat, "private", uid)})
    return aliases


def _bootstrap_route_for(plugin, session_id: str) -> dict | None:
    sid = str(session_id or "").strip()
    if not sid:
        return None
    boot = getattr(plugin, "_bootstrap_messages", None)
    if not isinstance(boot, dict):
        return None
    latest: tuple[float, object] | None = None
    for (msg_sid, _, _), (ts, msg) in boot.items():
        if msg_sid != sid:
            continue
        if latest is None or ts >= latest[0]:
            latest = (ts, msg)
    if latest is None:
        return None
    msg = latest[1]
    plat = str(getattr(msg, "platform", "") or "").strip().lower()
    gid = str(getattr(msg, "group_id", "") or "").strip()
    uid = str(getattr(msg, "user_id", "") or "").strip()
    if not plat or not (gid or uid):
        return None
    ctype = "group" if gid else "private"
    return {
        "platform": plat,
        "chat_type": ctype,
        "group_id": gid,
        "user_id": "" if ctype == "group" else uid,
        "account_id": str(getattr(msg, "account_id", "") or "").strip(),
    }


def _persist_bootstrap_route(plugin, session_id: str) -> None:
    sid = str(session_id or "").strip()
    messages = getattr(plugin, "messages", None)
    if not sid or messages is None or messages.route_for(sid):
        return
    route = _bootstrap_route_for(plugin, sid)
    if not route:
        return
    try:
        messages.upsert_route(
            sid,
            platform=route["platform"],
            chat_type=route["chat_type"],
            user_id=route["user_id"],
            group_id=route["group_id"],
        )
    except Exception:
        pass


async def _open_session_for_spec(plugin, plat: str, kind: str, tid: str) -> str:
    ctx = getattr(plugin, "ctx", None)
    if ctx is None:
        return ""
    args = {
        "platform": plat,
        "chat_type": kind,
        "user_id": tid if kind == "private" else "",
        "group_id": tid if kind == "group" else "",
    }
    try:
        opened = await call_capability(ctx, "chat.open_session", timeout_ms=15000, **args)
    except (RuntimeError, OSError, ValueError, TypeError):
        return ""
    if not isinstance(opened, dict) or not opened.get("success", True):
        return ""
    sid = str(opened.get("session_id") or opened.get("stream_id") or "").strip()
    messages = getattr(plugin, "messages", None)
    if sid and messages is not None:
        try:
            messages.upsert_route(
                sid,
                platform=plat,
                chat_type=kind,
                user_id=args["user_id"],
                group_id=args["group_id"],
            )
        except Exception:
            pass
    return sid


def _logical_chat_key(entry: dict) -> tuple[str, str, str] | None:
    plat_l = str(entry.get("platform") or "").strip().lower()
    gid = str(entry.get("group_id") or "").strip()
    uid = str(entry.get("user_id") or "").strip()
    ctype_l = str(
        entry.get("chat_type") or ("group" if gid else ("private" if uid else ""))
    ).strip().lower()
    tid = gid if ctype_l == "group" else uid
    if not plat_l or ctype_l not in ("group", "private") or not tid:
        return None
    return plat_l, ctype_l, tid


def _pick_match(
    matches: list[dict],
    raw: str,
    current_platform: str,
    *,
    preferred_account_id: str = "",
) -> tuple[str, str]:
    deduped_by_sid: dict[str, dict] = {}
    for item in matches:
        sid = str(item.get("session_id") or "").strip()
        if not sid:
            continue
        prev = deduped_by_sid.get(sid)
        if prev is None:
            deduped_by_sid[sid] = dict(item)
        else:
            merged = dict(prev)
            for k, v in item.items():
                if v not in (None, "", 0, 0.0) and merged.get(k) in (None, "", 0, 0.0):
                    merged[k] = v
            deduped_by_sid[sid] = merged

    grouped: dict[tuple, list[dict]] = {}
    for item in deduped_by_sid.values():
        lkey = _logical_chat_key(item)
        bucket_key = ("logical", lkey) if lkey is not None else ("sid", item["session_id"])
        grouped.setdefault(bucket_key, []).append(item)

    collapsed: list[dict] = []
    for group in grouped.values():
        best = max(
            group,
            key=lambda m: session_rank_key(m, preferred_account_id=preferred_account_id),
        )
        best = dict(best)
        if not best.get("name"):
            for cand in group:
                if cand.get("name"):
                    best["name"] = cand["name"]
                    break
        collapsed.append(best)

    if len(collapsed) == 1:
        item = collapsed[0]
        return item["session_id"], str(item.get("platform") or current_platform or "qq").strip().lower()
    unique_pairs = {(m["session_id"], str(m.get("platform") or "").strip().lower()) for m in collapsed}
    if len(unique_pairs) == 1:
        sid, plat = next(iter(unique_pairs))
        return sid, plat or current_platform or "qq"
    plat_l = current_platform.strip().lower()
    if plat_l:
        filtered = [m for m in collapsed if str(m.get("platform") or "").strip().lower() == plat_l]
        if len({m["session_id"] for m in filtered}) == 1 and filtered:
            return filtered[0]["session_id"], str(filtered[0].get("platform") or plat_l).strip().lower()
    preview_parts = []
    for m in collapsed[:4]:
        lkey = _logical_chat_key(m)
        spec = format_stream_spec(*lkey) if lkey is not None else str(m.get("stream_spec") or "")
        label = str(m.get("name") or m.get("group_id") or m.get("user_id") or m["session_id"])
        detail = f"{spec}, session_id={m['session_id']}" if spec else f"session_id={m['session_id']}"
        preview_parts.append(f"{label}({detail})")
    preview = ", ".join(preview_parts)
    raise ValueError(f"找到多个与「{raw}」匹配的会话，请改用更精确的 平台:类型:id 或 session_id：{preview}")


def _merge_session_records(
    streams: dict[str, dict], routes_list: list[dict]
) -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for sid, info in (streams or {}).items():
        if sid and isinstance(info, dict):
            merged[sid] = {"session_id": sid, **info}
    for r in routes_list or []:
        sid = str(r.get("session_id") or "").strip()
        if not sid:
            continue
        base = dict(merged.get(sid) or {"session_id": sid})
        for field in (
            "platform",
            "chat_type",
            "group_id",
            "user_id",
            "account_id",
            "scope",
        ):
            if not base.get(field) and r.get(field):
                base[field] = r[field]
        for num_field in ("human_count", "last_human_at", "updated"):
            val = r.get(num_field)
            if val and not base.get(num_field):
                base[num_field] = val
        if not base.get("stream_spec"):
            lkey = _logical_chat_key(base)
            if lkey is not None:
                base["stream_spec"] = format_stream_spec(*lkey)
        merged[sid] = base
    return merged


async def resolve_chat_target(
    plugin,
    raw_target: str | None,
    *,
    current_chat_id: str = "",
    current_platform: str = "",
    allow_unknown_ascii: bool = False,
) -> tuple[str, str]:
    """将 平台:类型:id、群号、群名或 session_id 归一化为宿主 canonical (session_id, platform)。"""
    raw = str(raw_target or "").strip()
    cur_plat = str(current_platform or "").strip().lower()
    if raw in _CURRENT_CHAT_ALIASES or raw.lower() in {"current", "here", "this"}:
        return current_chat_id, cur_plat or "qq"

    streams = await plugin._resolve_chat_sources()
    messages = getattr(plugin, "messages", None)
    routes_list = (
        messages.list_routes(limit=1000)
        if messages is not None and hasattr(messages, "list_routes")
        else []
    )
    merged_records = _merge_session_records(streams, routes_list)

    preferred_account_id = ""
    if current_chat_id:
        cur_meta = merged_records.get(current_chat_id)
        if cur_meta and cur_meta.get("account_id"):
            preferred_account_id = str(cur_meta["account_id"]).strip()
        if not preferred_account_id and messages is not None:
            cur_route = messages.route_for(current_chat_id)
            if cur_route and cur_route.get("account_id"):
                preferred_account_id = str(cur_route["account_id"]).strip()
        if not preferred_account_id:
            boot_cur = _bootstrap_route_for(plugin, current_chat_id)
            if boot_cur and boot_cur.get("account_id"):
                preferred_account_id = str(boot_cur["account_id"]).strip()

    def _upgrade_empty_account_sibling(sid: str, entry: dict | None) -> tuple[str, str]:
        base = dict(merged_records.get(sid) or entry or {"session_id": sid})
        plat = str(base.get("platform") or cur_plat or "qq").strip().lower()
        if str(base.get("account_id") or "").strip():
            return sid, plat
        lkey = _logical_chat_key(base)
        if lkey is None:
            return sid, plat
        siblings = [
            rec
            for rec in merged_records.values()
            if _logical_chat_key(rec) == lkey and str(rec.get("account_id") or "").strip()
        ]
        if siblings:
            best = max(
                siblings,
                key=lambda m: session_rank_key(m, preferred_account_id=preferred_account_id),
            )
            return best["session_id"], str(best.get("platform") or plat or "qq").strip().lower()
        return sid, plat

    if raw in streams:
        return _upgrade_empty_account_sibling(raw, streams[raw])

    if raw in merged_records:
        return _upgrade_empty_account_sibling(raw, merged_records[raw])

    parsed = parse_stream_spec(raw)
    norm_spec = format_stream_spec(*parsed) if parsed else ""
    norm = norm_spec or _normalize_colon_token(raw)

    hits: list[dict] = []
    if messages is not None:
        route = messages.route_for(raw)
        if route:
            return _upgrade_empty_account_sibling(raw, route)
        for f in messages.find_sessions(raw):
            f_sid = str(f.get("session_id") or "").strip()
            enriched = dict(merged_records.get(f_sid) or {})
            enriched.update({k: v for k, v in f.items() if v not in (None, "", 0, 0.0)})
            enriched["session_id"] = f_sid
            hits.append(enriched)

    for sid, info in merged_records.items():
        cands = _stream_aliases(sid, info)
        if raw in cands or norm in cands or (norm_spec and norm_spec in cands):
            hits.append(info)
    if hits:
        return _pick_match(
            hits, raw, cur_plat, preferred_account_id=preferred_account_id
        )

    raw_lower = raw.lower()
    exact_name_hits = [
        info
        for info in merged_records.values()
        if str(info.get("name") or "").strip().lower() == raw_lower
    ]
    if exact_name_hits:
        return _pick_match(
            exact_name_hits, raw, cur_plat, preferred_account_id=preferred_account_id
        )

    partial_name_hits = [
        info
        for info in merged_records.values()
        if info.get("name") and raw_lower in str(info.get("name") or "").strip().lower()
    ]
    if partial_name_hits:
        return _pick_match(
            partial_name_hits, raw, cur_plat, preferred_account_id=preferred_account_id
        )

    if parsed is not None:
        plat, kind, tid = parsed
        opened_sid = await _open_session_for_spec(plugin, plat, kind, tid)
        if opened_sid:
            return opened_sid, plat
        return norm_spec, plat

    enabled_chats = set(getattr(plugin.settings, "enabled_chats", ()) or ())
    if raw in enabled_chats or raw == current_chat_id:
        return raw, cur_plat or "qq"
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", raw) and (allow_unknown_ascii or not streams):
        return raw, cur_plat or "qq"

    raise ValueError(
        f"未找到匹配的会话「{raw}」，请使用 平台:类型:id（如 qq:group:12345678）或先调用 read(kind='overview') 查看 known_chats"
    )


async def resolve_stream_spec(
    plugin,
    raw_target: str | None,
    *,
    current_chat_id: str = "",
    current_platform: str = "",
    allow_unknown_ascii: bool = True,
) -> str:
    """将任意会话标识归一化为标准 平台:类型:id 字符串（无路由元数据的新测试短 ID 原样保留）。"""
    raw = str(raw_target or "").strip()
    cur_plat = str(current_platform or "").strip().lower()
    if raw in _CURRENT_CHAT_ALIASES or raw.lower() in {"current", "here", "this"}:
        _persist_bootstrap_route(plugin, current_chat_id)
        canon = (
            plugin.canonical_stream_spec(current_chat_id, cur_plat)
            if hasattr(plugin, "canonical_stream_spec")
            else ""
        )
        return canon or current_chat_id

    parsed = parse_stream_spec(raw)
    if parsed is not None:
        return format_stream_spec(*parsed)

    cid, plat = await resolve_chat_target(
        plugin,
        raw,
        current_chat_id=current_chat_id,
        current_platform=current_platform,
        allow_unknown_ascii=allow_unknown_ascii,
    )
    if cid == current_chat_id:
        _persist_bootstrap_route(plugin, current_chat_id)
    canon = plugin.canonical_stream_spec(cid, plat) if hasattr(plugin, "canonical_stream_spec") else ""
    return canon or cid


async def collect_known_chats(plugin) -> list[dict]:
    """合并宿主群聊列表、已记录路由与已配置白名单，返回去重且带 平台:类型:id 的会话清单。"""
    streams = await plugin._resolve_chat_sources()
    messages = getattr(plugin, "messages", None)
    routes = {
        r["session_id"]: r for r in (messages.list_routes() if messages is not None else [])
    }

    enabled_set = set(plugin.settings.enabled_chats)
    for item in list(plugin.settings.enabled_chats):
        parsed = parse_stream_spec(item)
        if parsed is not None:
            enabled_set.add(format_stream_spec(*parsed))

    level_map = dict(plugin.settings.chat_proactive_levels or {})
    records: list[dict] = []
    seen_sids: set[str] = set()

    def _spec_for(sid: str, plat: str, ctype: str, gid: str, uid: str, s_info: dict) -> str:
        spec = str(s_info.get("stream_spec") or "").strip()
        if spec:
            return spec
        if plat and gid:
            return format_stream_spec(plat, "group", gid)
        if plat and uid:
            return format_stream_spec(plat, "private", uid)
        if plat and ctype in ("group", "private") and (gid or uid):
            return format_stream_spec(plat, ctype, gid or uid)
        parsed = parse_stream_spec(sid)
        return format_stream_spec(*parsed) if parsed else ""

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
        spec = _spec_for(sid, plat, ctype, gid, uid, s_info)
        aliases = set(plugin.chat_aliases_for(sid, plat)) | {sid}
        if spec:
            aliases.add(spec)
        records.append(
            {
                "sid": sid,
                "spec": spec,
                "raw_name": str(s_info.get("name") or "").strip(),
                "name": str(s_info.get("name") or gid or uid or spec or sid),
                "plat": plat,
                "ctype": ctype,
                "gid": gid,
                "uid": uid,
                "account_id": str(s_info.get("account_id") or r_info.get("account_id") or "").strip(),
                "human_count": int(s_info.get("human_count") or r_info.get("human_count") or 0),
                "last_human_at": float(s_info.get("last_human_at") or r_info.get("last_human_at") or 0.0),
                "updated": float(s_info.get("updated") or r_info.get("updated") or 0.0),
                "avatar": str(s_info.get("avatar") or ""),
                "aliases": aliases,
            }
        )

    for raw in plugin.settings.enabled_chats:
        token = str(raw or "").strip()
        if not token:
            continue
        parsed = parse_stream_spec(token)
        norm_tok = format_stream_spec(*parsed) if parsed else token
        if any(token in rec["aliases"] or norm_tok in rec["aliases"] for rec in records):
            continue
        if parsed is not None:
            plat, ctype, tid = parsed
            records.append(
                {
                    "sid": norm_tok,
                    "spec": norm_tok,
                    "raw_name": "",
                    "name": tid or norm_tok,
                    "plat": plat,
                    "ctype": ctype,
                    "gid": tid if ctype == "group" else "",
                    "uid": tid if ctype == "private" else "",
                    "account_id": "",
                    "human_count": 0,
                    "last_human_at": 0.0,
                    "updated": 0.0,
                    "avatar": "",
                    "aliases": {token, norm_tok},
                }
            )
        else:
            records.append(
                {
                    "sid": token,
                    "spec": "",
                    "raw_name": "",
                    "name": token,
                    "plat": "",
                    "ctype": "",
                    "gid": "",
                    "uid": "",
                    "account_id": "",
                    "human_count": 0,
                    "last_human_at": 0.0,
                    "updated": 0.0,
                    "avatar": "",
                    "aliases": {token},
                }
            )

    grouped_records: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        key = ("spec", rec["spec"]) if rec["spec"] else ("sid", rec["sid"])
        grouped_records.setdefault(key, []).append(rec)

    collapsed_records: list[dict] = []
    for group in grouped_records.values():
        if len(group) == 1:
            collapsed_records.append(group[0])
            continue
        best = max(group, key=session_rank_key)
        merged = dict(best)
        merged_aliases: set[str] = set()
        for item in group:
            merged_aliases.update(item["aliases"])
        merged["aliases"] = merged_aliases
        if not merged.get("avatar"):
            for item in sorted(group, key=session_rank_key, reverse=True):
                if item.get("avatar"):
                    merged["avatar"] = item["avatar"]
                    break
        readable = [
            item
            for item in group
            if item.get("raw_name")
            and item["raw_name"]
            not in {item["sid"], item["gid"], item["uid"], item["spec"]}
        ]
        if readable:
            merged["name"] = max(readable, key=session_rank_key)["raw_name"]
        collapsed_records.append(merged)

    result: list[dict] = []
    for rec in collapsed_records:
        aliases = rec["aliases"]
        is_enabled = bool(aliases & enabled_set)
        proactive_level = ""
        for candidate in [rec["spec"], rec["sid"], *sorted(aliases)]:
            if candidate and candidate in level_map:
                proactive_level = level_map[candidate]
                break
        result.append(
            {
                "chat_id": rec["sid"],
                "stream_spec": rec["spec"],
                "stream_id": rec["spec"] or rec["sid"],
                "name": rec["name"],
                "platform": rec["plat"],
                "chat_type": rec["ctype"],
                "group_id": rec["gid"],
                "user_id": rec["uid"],
                "avatar": rec["avatar"],
                "enabled": is_enabled,
                "proactive_level": proactive_level,
            }
        )
    return result


def _history_path(plugin) -> Path:
    return plugin.data_root / _SETTINGS_HISTORY_FILE


def _load_or_sync_settings_history(plugin) -> dict:
    path = _history_path(plugin)
    live = copy.deepcopy(plugin.settings.model_dump())
    raw = MemoryLayers._bytes(path)
    state: dict | None = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("entries"), list) and parsed["entries"]:
                state = parsed
        except (ValueError, UnicodeDecodeError):
            state = None
    if state is None:
        state = {
            "current_version": 0,
            "entries": [
                {
                    "version": 0,
                    "settings": live,
                    "author": "initial",
                    "reason": "初始配置",
                    "ts": round(time.time(), 3),
                }
            ],
        }
        atomic_private_write(path, json.dumps(state, ensure_ascii=False).encode())
        return state

    latest = state["entries"][-1]
    if latest.get("settings") != live:
        next_ver = int(state.get("current_version", 0)) + 1
        state["current_version"] = next_ver
        state["entries"].append(
            {
                "version": next_ver,
                "settings": live,
                "author": "sync",
                "reason": "同步宿主或管理台配置变更",
                "ts": round(time.time(), 3),
            }
        )
        state["entries"] = state["entries"][-_MAX_SETTINGS_HISTORY:]
        atomic_private_write(path, json.dumps(state, ensure_ascii=False).encode())
    return state


async def read_settings_state(plugin) -> dict:
    async with _shaping_lock(plugin):
        state = await completed_thread(_load_or_sync_settings_history, plugin)
        return {
            "kind": "settings",
            "settings": state["entries"][-1]["settings"],
            "version": int(state["current_version"]),
            "hash": plugin.management._settings()["version"],
            "history_count": len(state["entries"]),
        }


async def settings_history(plugin) -> dict:
    async with _shaping_lock(plugin):
        state = await completed_thread(_load_or_sync_settings_history, plugin)
        items = [
            {
                "version": int(entry["version"]),
                "ts": entry.get("ts"),
                "author": entry.get("author", ""),
                "reason": entry.get("reason", ""),
                "enabled_chats": list((entry.get("settings") or {}).get("enabled_chats") or ()),
                "sleep_window": (entry.get("settings") or {}).get("sleep_window", ""),
                "proactive_level": (entry.get("settings") or {}).get("proactive_level", ""),
            }
            for entry in reversed(state["entries"])
        ]
        return {
            "kind": "settings",
            "version": int(state["current_version"]),
            "history": items,
        }


def _check_no_lockout(candidate: dict) -> None:
    plugin_cfg = candidate.get("plugin")
    if isinstance(plugin_cfg, dict) and plugin_cfg.get("enabled") is False:
        raise ValueError(_LOCKOUT_MESSAGE)


async def _apply_validated_settings(
    plugin, candidate: dict, *, state: dict, author: str, reason: str
) -> dict:
    _check_no_lockout(candidate)
    validated = Settings.model_validate(candidate)
    dumped = validated.model_dump()
    await plugin.on_config_update("self", dumped, 0)
    next_ver = int(state.get("current_version", 0)) + 1
    entry = {
        "version": next_ver,
        "settings": dumped,
        "author": author,
        "reason": reason,
        "ts": round(time.time(), 3),
    }

    def _persist() -> None:
        atomic_private_write(
            plugin.data_root / "settings.json",
            json.dumps(dumped, ensure_ascii=False).encode(),
        )
        state["current_version"] = next_ver
        state["entries"].append(entry)
        state["entries"] = state["entries"][-_MAX_SETTINGS_HISTORY:]
        atomic_private_write(
            _history_path(plugin),
            json.dumps(state, ensure_ascii=False).encode(),
        )

    await completed_thread(_persist)
    return {
        "kind": "settings",
        "version": next_ver,
        "hash": plugin.management._settings()["version"],
        "settings": dumped,
    }


async def patch_settings(
    plugin,
    patch: dict | None,
    *,
    current_chat_id: str = "",
    current_platform: str = "",
    expected_version: int | None = None,
    author: str = "maisya_shape",
    reason: str = "",
) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch 必须为非空对象")
    _assert_no_secret(plugin, patch, "设置修改内容")

    working_patch = copy.deepcopy(patch)
    add_chats = (
        _clean_str_list(working_patch.pop("add_chats"), label="add_chats")
        if "add_chats" in working_patch
        else None
    )
    remove_chats = (
        _clean_str_list(working_patch.pop("remove_chats"), label="remove_chats")
        if "remove_chats" in working_patch
        else None
    )
    resolved_add: list[str] = []
    resolved_add_aliases: dict[str, set[str]] = {}
    for raw in add_chats or ():
        spec = await resolve_stream_spec(
            plugin,
            raw,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
            allow_unknown_ascii=True,
        )
        if spec and spec not in resolved_add:
            resolved_add.append(spec)
            aliases = {raw, spec}
            try:
                cid, cplat = await resolve_chat_target(
                    plugin,
                    spec,
                    current_chat_id=current_chat_id,
                    current_platform=current_platform,
                    allow_unknown_ascii=True,
                )
                aliases.add(cid)
                aliases.update(plugin.chat_aliases_for(cid, cplat))
            except ValueError:
                pass
            resolved_add_aliases[spec] = aliases

    remove_match_tokens: set[str] = set()
    for raw in remove_chats or ():
        remove_match_tokens.add(raw)
        parsed = parse_stream_spec(raw)
        if parsed is not None:
            remove_match_tokens.add(format_stream_spec(*parsed))
        try:
            spec = await resolve_stream_spec(
                plugin,
                raw,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                allow_unknown_ascii=True,
            )
            remove_match_tokens.add(spec)
            cid, cplat = await resolve_chat_target(
                plugin,
                raw,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                allow_unknown_ascii=True,
            )
            remove_match_tokens.add(cid)
            remove_match_tokens.update(plugin.chat_aliases_for(cid, cplat))
        except ValueError:
            pass

    resolved_enabled: list[str] | None = None
    if "enabled_chats" in working_patch:
        resolved_enabled = []
        for raw in _clean_str_list(working_patch.pop("enabled_chats"), label="enabled_chats"):
            spec = await resolve_stream_spec(
                plugin,
                raw,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                allow_unknown_ascii=True,
            )
            if spec and spec not in resolved_enabled:
                resolved_enabled.append(spec)

    level_updates: list[tuple[str, str | None, set[str]]] = []
    if "chat_proactive_levels" in working_patch:
        raw_levels = working_patch.pop("chat_proactive_levels")
        if not isinstance(raw_levels, dict):
            raise ValueError("chat_proactive_levels 必须是对象")
        for raw_key, raw_val in raw_levels.items():
            spec = await resolve_stream_spec(
                plugin,
                raw_key,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                allow_unknown_ascii=True,
            )
            aliases = {str(raw_key).strip(), spec}
            parsed_k = parse_stream_spec(str(raw_key))
            if parsed_k is not None:
                aliases.add(format_stream_spec(*parsed_k))
            try:
                cid, cplat = await resolve_chat_target(
                    plugin,
                    raw_key,
                    current_chat_id=current_chat_id,
                    current_platform=current_platform,
                    allow_unknown_ascii=True,
                )
                aliases.add(cid)
                aliases.update(plugin.chat_aliases_for(cid, cplat))
            except ValueError:
                pass
            cleaned_val = None if raw_val in (None, "", "inherit", "default") else str(raw_val).strip()
            level_updates.append((spec, cleaned_val, aliases))

    unknown = set(working_patch) - set(Settings.model_fields)
    if unknown:
        raise ValueError(f"未知设置项：{', '.join(sorted(unknown))}")

    async with _shaping_lock(plugin):
        state = await completed_thread(_load_or_sync_settings_history, plugin)
        if expected_version is not None and expected_version != int(state["current_version"]):
            raise ValueError(
                f"设置版本已变化（当前 {state['current_version']}，传入 {expected_version}），请先 read 最新配置"
            )
        candidate = copy.deepcopy(state["entries"][-1]["settings"])
        for key, value in working_patch.items():
            if isinstance(value, dict) and isinstance(candidate.get(key), dict):
                candidate[key] = {**candidate[key], **value}
            else:
                candidate[key] = value
        _check_no_lockout(candidate)

        chats = list(resolved_enabled if resolved_enabled is not None else candidate.get("enabled_chats") or ())
        if resolved_add:
            for spec in resolved_add:
                aliases = resolved_add_aliases.get(spec, {spec})
                chats = [
                    c
                    for c in chats
                    if c == spec or (c not in aliases and not (plugin.chat_aliases_for(c) & aliases))
                ]
                if spec not in chats:
                    chats.append(spec)
        if remove_match_tokens:
            chats = [
                c
                for c in chats
                if c not in remove_match_tokens and not (plugin.chat_aliases_for(c) & remove_match_tokens)
            ]
        candidate["enabled_chats"] = chats

        if level_updates:
            levels = dict(candidate.get("chat_proactive_levels") or {})
            for spec, new_val, aliases in level_updates:
                for k in list(levels.keys()):
                    if k in aliases or (plugin.chat_aliases_for(k) & aliases):
                        levels.pop(k, None)
                if new_val is not None:
                    levels[spec] = new_val
            candidate["chat_proactive_levels"] = levels

        return await _apply_validated_settings(
            plugin,
            candidate,
            state=state,
            author=author,
            reason=reason or "对话内局部调整设置",
        )


async def write_settings(
    plugin,
    settings_data: dict,
    *,
    expected_version: int | None = None,
    author: str = "maisya_shape",
) -> dict:
    if expected_version is None:
        raise ValueError("write 设置时必须提供 expected_version")
    if not isinstance(settings_data, dict):
        raise ValueError("写入设置需要完整的 JSON 对象")
    _assert_no_secret(plugin, settings_data, "设置")
    _check_no_lockout(settings_data)
    normalized = copy.deepcopy(settings_data)
    if isinstance(normalized.get("enabled_chats"), (list, tuple, set)):
        norm_chats: list[str] = []
        for raw in _clean_str_list(normalized["enabled_chats"], label="enabled_chats"):
            spec = await resolve_stream_spec(plugin, raw, allow_unknown_ascii=True)
            if spec and spec not in norm_chats:
                norm_chats.append(spec)
        normalized["enabled_chats"] = norm_chats
    if isinstance(normalized.get("chat_proactive_levels"), dict):
        norm_levels: dict[str, str] = {}
        for raw_k, raw_v in normalized["chat_proactive_levels"].items():
            val = str(raw_v or "").strip()
            if not val:
                continue
            spec = await resolve_stream_spec(plugin, raw_k, allow_unknown_ascii=True)
            if spec:
                norm_levels[spec] = val
        normalized["chat_proactive_levels"] = norm_levels
    async with _shaping_lock(plugin):
        state = await completed_thread(_load_or_sync_settings_history, plugin)
        if expected_version != int(state["current_version"]):
            raise ValueError("设置版本已变化，请刷新后再保存")
        return await _apply_validated_settings(
            plugin,
            normalized,
            state=state,
            author=author,
            reason="整份覆写设置",
        )


async def rollback_settings(
    plugin,
    version: int,
    *,
    expected_version: int | None = None,
    author: str = "maisya_shape",
) -> dict:
    if expected_version is None:
        raise ValueError("回滚设置时必须提供 expected_version")
    async with _shaping_lock(plugin):
        state = await completed_thread(_load_or_sync_settings_history, plugin)
        if expected_version != int(state["current_version"]):
            raise ValueError("设置版本已变化，请刷新后再回滚")
        target_entry = next(
            (e for e in state["entries"] if int(e["version"]) == int(version)),
            None,
        )
        if target_entry is None:
            raise ValueError(f"未找到版本 {version} 的设置历史快照")
        result = await _apply_validated_settings(
            plugin,
            copy.deepcopy(target_entry["settings"]),
            state=state,
            author=author,
            reason=f"回滚至版本 {version}",
        )
        result["restored_from"] = int(version)
        return result


def profile_summary(plugin, profile_id: str) -> dict:
    p = plugin.profiles.get(profile_id)
    return {
        "id": p.id,
        "title": p.title,
        "enabled": p.enabled,
        "model_name": p.model_name,
        "fallback_models": list(p.fallback_models),
        "model_list": list(p.model_list),
        "task": p.task,
        "skills": list(p.skills),
        "tools": list(p.tools),
        "access": p.access,
        "allow_chats": list(p.allow_chats),
        "allow_users": list(p.allow_users),
        "environment": p.environment,
        "network": p.network,
        "web_provider": p.web_provider,
        "max_steps": p.max_steps,
        "max_tokens": p.max_tokens,
        "temperature": p.temperature,
        "autonomous": p.autonomous,
        "read_only": p.environment == "readonly",
        "instructions_chars": len(p.instructions),
        "fish_seed": p.fish_seed,
    }


async def patch_profile(
    plugin,
    target: str,
    patch: dict | None,
    *,
    current_chat_id: str = "",
    current_platform: str = "",
    expected_version: int | None = None,
    author: str = "maisya_shape",
) -> dict:
    target = str(target or "").strip()
    if not target:
        raise ValueError("修改角色必须指定 target")
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch 必须为非空对象")
    _assert_no_secret(plugin, patch, "角色配置")

    working = copy.deepcopy(patch)
    new_instructions = working.pop("instructions", None) if "instructions" in working else None
    append_instructions = (
        str(working.pop("append_instructions") or "").strip()
        if "append_instructions" in working
        else ""
    )

    add_allow_chats = (
        _clean_str_list(working.pop("add_allow_chats"), label="add_allow_chats")
        if "add_allow_chats" in working
        else None
    )
    remove_allow_chats = (
        _clean_str_list(working.pop("remove_allow_chats"), label="remove_allow_chats")
        if "remove_allow_chats" in working
        else None
    )
    resolved_allow_chats: list[str] | None = None
    if "allow_chats" in working:
        resolved_allow_chats = []
        for raw in _clean_str_list(working.pop("allow_chats"), label="allow_chats"):
            spec = await resolve_stream_spec(
                plugin,
                raw,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                allow_unknown_ascii=True,
            )
            if spec and spec not in resolved_allow_chats:
                resolved_allow_chats.append(spec)
    resolved_add_allow: list[str] = []
    resolved_add_allow_aliases: dict[str, set[str]] = {}
    for raw in add_allow_chats or ():
        spec = await resolve_stream_spec(
            plugin,
            raw,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
            allow_unknown_ascii=True,
        )
        if spec and spec not in resolved_add_allow:
            resolved_add_allow.append(spec)
            aliases = {raw, spec}
            try:
                cid, cplat = await resolve_chat_target(
                    plugin,
                    spec,
                    current_chat_id=current_chat_id,
                    current_platform=current_platform,
                    allow_unknown_ascii=True,
                )
                aliases.add(cid)
                aliases.update(plugin.chat_aliases_for(cid, cplat))
            except ValueError:
                pass
            resolved_add_allow_aliases[spec] = aliases

    remove_allow_tokens: set[str] = set()
    for raw in remove_allow_chats or ():
        remove_allow_tokens.add(raw)
        parsed = parse_stream_spec(raw)
        if parsed is not None:
            remove_allow_tokens.add(format_stream_spec(*parsed))
        try:
            spec = await resolve_stream_spec(
                plugin,
                raw,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                allow_unknown_ascii=True,
            )
            remove_allow_tokens.add(spec)
            cid, cplat = await resolve_chat_target(
                plugin,
                raw,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                allow_unknown_ascii=True,
            )
            remove_allow_tokens.add(cid)
            remove_allow_tokens.update(plugin.chat_aliases_for(cid, cplat))
        except ValueError:
            pass

    model_list = working.pop("model_list", None) if "model_list" in working else None
    add_skills = _clean_str_list(working.pop("add_skills"), label="add_skills") if "add_skills" in working else None
    remove_skills = set(_clean_str_list(working.pop("remove_skills"), label="remove_skills")) if "remove_skills" in working else set()
    add_tools = _clean_str_list(working.pop("add_tools"), label="add_tools") if "add_tools" in working else None
    remove_tools = set(_clean_str_list(working.pop("remove_tools"), label="remove_tools")) if "remove_tools" in working else set()

    unknown = set(working) - _PROFILE_TOML_KEYS
    if unknown:
        raise ValueError(f"角色配置含未知字段：{', '.join(sorted(unknown))}")

    def _apply_sync() -> dict:
        toml_doc = plugin.memory.read("profile", target=target, document="profile.toml", allow_custom=True)
        md_doc = plugin.memory.read("profile", target=target, document="PROFILE.md", allow_custom=True)
        if expected_version is not None and toml_doc["version"] != expected_version:
            raise ValueError("角色 profile.toml 版本已变化，请重新读取后再修改")

        cfg = tomllib.loads(toml_doc["content"]) if toml_doc["content"].strip() else {}
        orig_cfg = copy.deepcopy(cfg)

        if model_list is not None:
            cleaned_models = _clean_str_list(model_list, label="model_list")
            cfg.pop("model_name", None)
            cfg["model"] = cleaned_models[0] if cleaned_models else ""
            cfg["fallback_models"] = cleaned_models[1:]
        if "model_name" in working and "model" not in working:
            cfg.pop("model_name", None)
            cfg["model"] = str(working.pop("model_name") or "").strip()
        elif "model_name" in working:
            working.pop("model_name", None)
        if "model" in working:
            cfg.pop("model_name", None)
            cfg["model"] = str(working.pop("model") or "").strip()
        if "fallback_models" in working:
            cfg["fallback_models"] = _clean_str_list(working.pop("fallback_models"), label="fallback_models")

        for list_key in ("skills", "tools", "mcp", "apis", "secrets", "sandbox_targets", "allow_users"):
            if list_key in working:
                cfg[list_key] = _clean_str_list(working.pop(list_key), label=list_key)

        for key, value in working.items():
            if value is None:
                cfg.pop(key, None)
            else:
                cfg[key] = value

        if add_skills or remove_skills:
            cur_skills = list(cfg.get("skills") or ())
            for sid in add_skills or ():
                if sid not in cur_skills:
                    cur_skills.append(sid)
            if remove_skills:
                cur_skills = [s for s in cur_skills if s not in remove_skills]
            cfg["skills"] = cur_skills

        if add_tools or remove_tools:
            cur_tools = list(cfg.get("tools") or ())
            for tid in add_tools or ():
                if tid not in cur_tools:
                    cur_tools.append(tid)
            if remove_tools:
                cur_tools = [t for t in cur_tools if t not in remove_tools]
            cfg["tools"] = cur_tools

        if resolved_allow_chats is not None or resolved_add_allow or remove_allow_tokens:
            cur_chats = list(
                resolved_allow_chats if resolved_allow_chats is not None else cfg.get("allow_chats") or ()
            )
            for spec in resolved_add_allow:
                aliases = resolved_add_allow_aliases.get(spec, {spec})
                cur_chats = [
                    c
                    for c in cur_chats
                    if c == spec or (c not in aliases and not (plugin.chat_aliases_for(c) & aliases))
                ]
                if spec not in cur_chats:
                    cur_chats.append(spec)
            if remove_allow_tokens:
                cur_chats = [
                    c
                    for c in cur_chats
                    if c not in remove_allow_tokens
                    and not (plugin.chat_aliases_for(c) & remove_allow_tokens)
                ]
            cfg["allow_chats"] = cur_chats

        new_toml_ver = toml_doc["version"]
        if cfg != orig_cfg or not toml_doc["content"].strip():
            rendered = serialize_profile_toml(cfg)
            toml_res = plugin.memory.write(
                "profile",
                rendered,
                expected_version=toml_doc["version"],
                target=target,
                document="profile.toml",
                author=author,
                allow_custom=True,
            )
            new_toml_ver = toml_res["version"]
            try:
                plugin.profiles.get(target)
            except Exception:
                plugin.memory.write(
                    "profile",
                    toml_doc["content"],
                    expected_version=new_toml_ver,
                    target=target,
                    document="profile.toml",
                    author=author,
                    allow_custom=True,
                )
                raise

        new_md_ver = md_doc["version"]
        md_content = md_doc["content"]
        updated_md: str | None = None
        if new_instructions is not None:
            updated_md = str(new_instructions)
        elif append_instructions and append_instructions not in md_content:
            base = md_content.rstrip()
            updated_md = f"{base}\n\n{append_instructions}\n" if base else f"{append_instructions}\n"
        if updated_md is not None and updated_md != md_content:
            md_res = plugin.memory.write(
                "profile",
                updated_md,
                expected_version=md_doc["version"],
                target=target,
                document="PROFILE.md",
                author=author,
                allow_custom=True,
            )
            new_md_ver = md_res["version"]

        return {
            "kind": "profile",
            "target": target,
            "version": new_toml_ver,
            "instructions_version": new_md_ver,
            "profile": profile_summary(plugin, target),
        }

    return await completed_thread(_apply_sync)


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    text = raw or ""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            meta = parse_skill_meta("skill", text)
            body = text[end + 5 :]
            return meta, body
    return {}, text


def _compose_skill_markdown(title: str, description: str, body: str) -> str:
    header_lines = ["---"]
    if title:
        header_lines.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    if description:
        header_lines.append(f"description: {json.dumps(description, ensure_ascii=False)}")
    header_lines.append("---")
    clean_body = body.lstrip("\n")
    return "\n".join(header_lines) + ("\n\n" + clean_body if clean_body else "\n")


async def _bind_skill_profiles(
    plugin,
    skill_id: str,
    bind_profiles: list[str],
    unbind_profiles: list[str],
    *,
    author: str,
) -> list[str]:
    bound: list[str] = []
    for pid in bind_profiles:
        await patch_profile(plugin, pid, {"add_skills": [skill_id]}, author=author)
        if pid not in bound:
            bound.append(pid)
    for pid in unbind_profiles:
        await patch_profile(plugin, pid, {"remove_skills": [skill_id]}, author=author)
    return bound


async def patch_memory_doc(
    plugin,
    *,
    kind: str,
    target: str = "",
    chat_id: str = "",
    document: str = "",
    patch: dict | None,
    expected_version: int | None = None,
    author: str = "maisya_shape",
) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch 必须为非空对象")
    _assert_no_secret(plugin, patch, "文本写入内容")

    def _apply_sync() -> dict:
        cur = plugin.memory.read(
            kind,
            target=target,
            chat_id=chat_id,
            document=document,
            allow_custom=True,
        )
        if expected_version is not None and cur["version"] != expected_version:
            raise ValueError("内容版本已变化，请重新读取后再修改")
        old_text = cur["content"]
        if "content" in patch:
            new_text = str(patch["content"])
        else:
            new_text = old_text
            if "replace" in patch:
                rep = patch["replace"]
                if not isinstance(rep, dict) or "old" not in rep or "new" not in rep:
                    raise ValueError("replace 必须包含 old 和 new 字符串")
                old_seg = str(rep["old"])
                if not old_seg or old_seg not in new_text:
                    raise ValueError("未在原文中找到待替换的片段")
                new_text = new_text.replace(old_seg, str(rep["new"]), 1)
            if "prepend" in patch:
                pre = str(patch["prepend"] or "").strip()
                if pre and pre not in new_text:
                    new_text = f"{pre}\n{new_text.lstrip()}" if new_text.strip() else f"{pre}\n"
            if "append" in patch:
                app = str(patch["append"] or "").strip()
                if app and app not in new_text:
                    new_text = f"{new_text.rstrip()}\n{app}\n" if new_text.strip() else f"{app}\n"
        return plugin.memory.write(
            kind,
            new_text,
            expected_version=cur["version"],
            target=target,
            chat_id=chat_id,
            document=document,
            author=author,
            allow_custom=True,
        )

    return await completed_thread(_apply_sync)


async def handle_skill(
    plugin,
    *,
    action: str,
    target: str,
    content: str = "",
    patch: dict | None = None,
    expected_version: int | None = None,
    version: int | None = None,
    author: str = "maisya_shape",
) -> dict:
    target = str(target or "").strip()
    if not target:
        raise ValueError("操作技能必须指定 target（英文短横线 id）")
    opts = dict(patch or {})
    bind_profiles = _clean_str_list(opts.pop("bind_profiles", []), label="bind_profiles")
    unbind_profiles = _clean_str_list(opts.pop("unbind_profiles", []), label="unbind_profiles")

    if action == "read":
        return await completed_thread(
            plugin.memory.read, "skill", target=target, version=version, allow_custom=True
        )
    if action == "history":
        return {
            "kind": "skill",
            "target": target,
            "history": await completed_thread(plugin.memory.history, "skill", target=target),
        }
    if action == "delete":
        return await completed_thread(plugin.memory.delete_skill, target)
    if action == "rollback":
        if version is None or expected_version is None:
            raise ValueError("回滚技能需要提供 version 与 expected_version")
        return await completed_thread(
            plugin.memory.rollback,
            "skill",
            version,
            expected_version=expected_version,
            target=target,
            allow_custom=True,
        )
    if action == "create":
        _assert_no_secret(plugin, content or opts, "技能内容")
        raw_content = content or ""
        if not raw_content.strip() and opts.get("body"):
            raw_content = _compose_skill_markdown(
                str(opts.get("title") or target),
                str(opts.get("description") or ""),
                str(opts.get("body") or ""),
            )
        res = await completed_thread(
            plugin.memory.create_skill,
            target,
            base=str(opts.get("base") or ""),
            title="" if raw_content.lstrip().startswith("---\n") else str(opts.get("title") or ""),
            description="" if raw_content.lstrip().startswith("---\n") else str(opts.get("description") or ""),
            content=raw_content,
            allow_custom=True,
        )
        if bind_profiles or unbind_profiles:
            res["bound_profiles"] = await _bind_skill_profiles(
                plugin, target, bind_profiles, unbind_profiles, author=author
            )
        return res
    if action == "write":
        _assert_no_secret(plugin, content, "技能内容")
        if expected_version is None:
            raise ValueError("覆盖写入技能必须提供 expected_version")
        res = await completed_thread(
            plugin.memory.write,
            "skill",
            content,
            expected_version=expected_version,
            target=target,
            author=author,
            allow_custom=True,
        )
        if bind_profiles or unbind_profiles:
            res["bound_profiles"] = await _bind_skill_profiles(
                plugin, target, bind_profiles, unbind_profiles, author=author
            )
        return res
    if action == "patch":
        _assert_no_secret(plugin, opts or content, "技能更新内容")
        if content or any(k in opts for k in ("title", "description", "body", "append", "prepend", "replace", "content")):
            def _patch_skill_sync() -> dict:
                cur = plugin.memory.read("skill", target=target, allow_custom=True)
                if expected_version is not None and cur["version"] != expected_version:
                    raise ValueError("技能版本已变化，请重新读取后再修改")
                if content:
                    new_md = content
                elif "content" in opts:
                    new_md = str(opts["content"])
                else:
                    meta, body = _split_frontmatter(cur["content"])
                    title = str(opts.get("title") if "title" in opts else meta.get("title") or target)
                    desc = str(opts.get("description") if "description" in opts else meta.get("description") or "")
                    new_body = str(opts["body"]) if "body" in opts else body
                    if "replace" in opts:
                        rep = opts["replace"]
                        if not isinstance(rep, dict) or str(rep.get("old") or "") not in new_body:
                            raise ValueError("未在技能正文中找到待替换内容")
                        new_body = new_body.replace(str(rep["old"]), str(rep["new"]), 1)
                    if "prepend" in opts:
                        pre = str(opts["prepend"] or "").strip()
                        if pre and pre not in new_body:
                            new_body = f"{pre}\n{new_body.lstrip()}"
                    if "append" in opts:
                        app = str(opts["append"] or "").strip()
                        if app and app not in new_body:
                            new_body = f"{new_body.rstrip()}\n{app}\n"
                    new_md = _compose_skill_markdown(title, desc, new_body)
                return plugin.memory.write(
                    "skill",
                    new_md,
                    expected_version=cur["version"],
                    target=target,
                    author=author,
                    allow_custom=True,
                )

            res = await completed_thread(_patch_skill_sync)
        else:
            res = await completed_thread(plugin.memory.read, "skill", target=target, allow_custom=True)
        if bind_profiles or unbind_profiles:
            res["bound_profiles"] = await _bind_skill_profiles(
                plugin, target, bind_profiles, unbind_profiles, author=author
            )
        return res
    raise ValueError(f"技能不支持 action={action}")


async def handle_services_or_webhooks(
    plugin, *, kind: str, action: str, content: str = "", patch: dict | None = None
) -> dict:
    if action == "read":
        return await plugin.management.read(kind, "{}")
    if action not in {"patch", "write"}:
        raise ValueError(f"{kind} 仅支持 read / patch / write")
    payload_data = patch if isinstance(patch, dict) and patch else None
    if payload_data is None and content.strip():
        try:
            payload_data = json.loads(content)
        except ValueError as exc:
            raise ValueError(f"{kind} 写入内容必须是合法 JSON") from exc
    if not isinstance(payload_data, dict):
        raise ValueError(f"修改 {kind} 需要提供 patch 对象或 JSON content")
    _assert_no_secret(plugin, payload_data, kind)
    cur = await plugin.management.read(kind, "{}")
    if action == "patch":
        merged = copy.deepcopy(cur.get("configuration") or {})
        for key, value in payload_data.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    else:
        merged = payload_data
    return await plugin.management.write(
        kind,
        json.dumps(
            {"configuration": merged, "expected_version": cur["version"]},
            ensure_ascii=False,
        ),
    )


async def handle_recipe(
    plugin,
    *,
    action: str,
    target: str = "",
    url: str = "",
    preview_token: str = "",
    patch: dict | None = None,
) -> dict:
    if action == "read":
        return await completed_thread(build_recipe_draft, plugin, target or "scout")
    opts = dict(patch or {})
    tok = str(preview_token or opts.get("preview_token") or "").strip()
    if tok:
        return await plugin.management.write(
            "recipe_import",
            json.dumps(
                {
                    "preview_token": tok,
                    "target": target or str(opts.get("target") or "").strip(),
                    "mode": str(opts.get("mode") or "create"),
                    "on_conflict": str(opts.get("on_conflict") or "keep"),
                },
                ensure_ascii=False,
            ),
        )
    source_url = str(url or opts.get("url") or "").strip()
    if not source_url:
        raise ValueError("导入配方需要提供 url 或 preview_token")
    preview = await plugin.management.write(
        "recipe_preview",
        json.dumps({"url": source_url, "target": target}, ensure_ascii=False),
    )
    if action == "create" or opts.get("apply") is True:
        ptok = str(preview.get("preview_token") or preview.get("token") or "").strip()
        if ptok and preview.get("can_import", True):
            imported = await plugin.management.write(
                "recipe_import",
                json.dumps(
                    {
                        "preview_token": ptok,
                        "target": target or str(preview.get("suggested_id") or "").strip(),
                        "mode": str(opts.get("mode") or "create"),
                        "on_conflict": str(opts.get("on_conflict") or "keep"),
                    },
                    ensure_ascii=False,
                ),
            )
            return {**preview, "imported": imported}
    return preview


async def handle_digest_sources(
    plugin,
    *,
    action: str,
    target: str = "",
    target_chat: str = "",
    patch: dict | None = None,
    current_chat_id: str = "",
    current_platform: str = "",
) -> dict:
    if action not in {"read", "patch", "write"}:
        raise ValueError(f"digest_sources 仅支持 read / patch / write，不支持 {action}")
    opts = dict(patch or {}) if isinstance(patch, dict) else {}
    ws_name = str(target or opts.get("workspace") or "digest").strip() or "digest"
    if not re.fullmatch(r"[\w-]{1,64}", ws_name):
        raise ValueError("工作区名称只能含文字、数字、下划线或横线")
    if target_chat and str(target_chat).strip():
        effective_chat_id, effective_platform = await resolve_chat_target(
            plugin,
            target_chat,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
        )
    else:
        effective_chat_id, effective_platform = current_chat_id, current_platform
    if not effective_chat_id:
        raise ValueError("请通过 target_chat 指定目标会话")
    stream_spec = (
        plugin.canonical_stream_spec(effective_chat_id, effective_platform)
        if hasattr(plugin, "canonical_stream_spec")
        else ""
    ) or effective_chat_id

    alias_candidates = {effective_chat_id}
    if hasattr(plugin, "chat_aliases_for"):
        alias_candidates.update(
            plugin.chat_aliases_for(effective_chat_id, effective_platform)
        )

    def _find_binding():
        with plugin.threads.connect() as db:
            placeholders = ",".join("?" for _ in alias_candidates)
            row = db.execute(
                f"SELECT w.*, t.state FROM agent_workspaces w LEFT JOIN agent_threads t ON t.thread_id=w.thread_id "
                f"WHERE w.owner='chat' AND w.name=? AND w.chat_id IN ({placeholders}) "
                "ORDER BY CASE WHEN w.chat_id=? THEN 0 ELSE 1 END LIMIT 1",
                [ws_name, *sorted(alias_candidates), effective_chat_id],
            ).fetchone()
            return dict(row) if row else None

    binding = await completed_thread(_find_binding)
    if action == "read":
        if binding is None:
            return {
                "kind": "digest_sources",
                "chat_id": effective_chat_id,
                "stream_spec": stream_spec,
                "workspace": ws_name,
                "feeds": [],
                "rejected": [],
                "history_count": 0,
            }

        def _read_sources():
            row_ref = {
                "thread_id": binding["thread_id"],
                "workspace_id": binding["workspace_id"],
            }
            with plugin.runtime.files.activity(row_ref) as work:
                dw = DigestWorkspace(work)
                sources = dw._load_sources()
                history = dw._load_history()
                return {
                    "kind": "digest_sources",
                    "chat_id": effective_chat_id,
                    "stream_spec": stream_spec,
                    "workspace": ws_name,
                    "feeds": list(sources.get("feeds", [])),
                    "rejected": list(sources.get("rejected", [])),
                    "history_count": len(history.get("items", [])),
                }

        return await completed_thread(_read_sources)

    add_raw = opts.get("add_urls") if "add_urls" in opts else opts.get("urls", [])
    discover_raw = (
        opts.get("discover_urls")
        if "discover_urls" in opts
        else opts.get("discover", [])
    )
    unsub_raw = (
        opts.get("unsubscribe_ids")
        if "unsubscribe_ids" in opts
        else opts.get("remove_ids", [])
    )
    if add_raw is None:
        add_raw = []
    if discover_raw is None:
        discover_raw = []
    if unsub_raw is None:
        unsub_raw = []
    if not isinstance(add_raw, (list, tuple)) or not isinstance(
        discover_raw, (list, tuple)
    ) or not isinstance(unsub_raw, (list, tuple)):
        raise ValueError("add_urls / discover_urls / unsubscribe_ids 必须是列表")

    auto_default = bool(opts.get("auto", False))
    created_helper = False
    target_row: dict
    if binding is not None and binding.get("state") not in (None, "closed"):
        target_row = await completed_thread(
            plugin.threads.get,
            binding["thread_id"],
            binding["chat_id"],
            allow_any_chat=True,
        )
    else:
        digest_profile = await completed_thread(plugin.profiles.get, "digest")
        target_row = await completed_thread(
            lambda: plugin.threads.create(
                digest_profile,
                "管理员配置资讯源",
                chat_id=effective_chat_id,
                platform=effective_platform or "qq",
                user_id="",
                owner="chat",
                workspace=ws_name,
                grant={"scheduled_admin": True},
            )
        )
        created_helper = True

    added: list[dict] = []
    discovered: list[dict] = []
    unsubscribed: list[dict] = []
    activity_cm = plugin.runtime.files.activity(target_row)
    work = await completed_thread(activity_cm.__enter__)
    try:
        dw = DigestWorkspace(work)
        for item in add_raw:
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                title = str(item.get("title") or "").strip()
                auto_flag = bool(item.get("auto", auto_default))
            else:
                url = str(item or "").strip()
                title = ""
                auto_flag = auto_default
            if url:
                added.append(
                    await dw.add_feed(url, title=title, auto=auto_flag, origin="admin")
                )
        for url in discover_raw:
            u = str(url or "").strip()
            if u:
                discovered.append(await dw.discover(u, origin="admin"))
        if unsub_raw:
            sources_snapshot = dw._load_sources()
            by_url = {
                str(f.get("url") or "").strip(): str(f.get("id") or "")
                for f in sources_snapshot.get("feeds", [])
            }
            for raw_id in unsub_raw:
                token = str(raw_id or "").strip()
                if not token:
                    continue
                fid = by_url.get(token, token)
                unsubscribed.append(dw.unsubscribe(fid, reason="管理员退订"))
        cur_sources = dw._load_sources()
        cur_history = dw._load_history()
    except BaseException as exc:
        await completed_thread(activity_cm.__exit__, type(exc), exc, exc.__traceback__)
        if created_helper:
            try:
                await completed_thread(
                    plugin.threads.stop, target_row["thread_id"], target_row["chat_id"]
                )
            except (ValueError, PermissionError, sqlite3.Error):
                pass
        raise
    else:
        await completed_thread(activity_cm.__exit__, None, None, None)

    if created_helper:
        try:
            await completed_thread(
                plugin.threads.stop, target_row["thread_id"], target_row["chat_id"]
            )
        except (ValueError, PermissionError, sqlite3.Error):
            pass

    return {
        "kind": "digest_sources",
        "chat_id": effective_chat_id,
        "stream_spec": stream_spec,
        "workspace": ws_name,
        "added": added,
        "discovered": discovered,
        "unsubscribed": unsubscribed,
        "feeds": list(cur_sources.get("feeds", [])),
        "rejected": list(cur_sources.get("rejected", [])),
        "history_count": len(cur_history.get("items", [])),
    }


async def build_overview(
    plugin, *, current_chat_id: str = "", current_platform: str = "", target_chat: str = ""
) -> dict:
    settings_state = await read_settings_state(plugin)
    known_chats = await collect_known_chats(plugin)
    assets = await completed_thread(plugin.management._assets)
    host_models = await plugin.management._collect_host_models(force=False, probe_host=True)
    profiles = [profile_summary(plugin, item["id"]) for item in assets.get("profiles") or ()]
    pending = (
        await completed_thread(plugin.intents.list_all, status="pending", limit=50)
        if getattr(plugin, "intents", None) is not None
        else []
    )
    for row in pending:
        tgt = str(row.get("target") or "")
        if tgt.startswith("chat:") and hasattr(plugin, "canonical_stream_spec"):
            row["target_spec"] = (
                plugin.canonical_stream_spec(tgt[5:], (row.get("payload") or {}).get("platform", ""))
                or tgt[5:]
            )

    def _runtime_snapshots():
        if getattr(plugin, "threads", None) is None:
            return [], [], []
        spec_of = (
            plugin.canonical_stream_spec
            if hasattr(plugin, "canonical_stream_spec")
            else (lambda cid, plat="": cid)
        )
        with plugin.threads.connect() as db:
            ws_rows = [
                {
                    "workspace_id": r["workspace_id"],
                    "owner": r["owner"],
                    "chat_id": r["chat_id"],
                    "stream_spec": spec_of(r["chat_id"]) or r["chat_id"],
                    "name": r["name"],
                    "profile": r["profile"],
                    "thread_id": r["thread_id"],
                }
                for r in db.execute(
                    "SELECT * FROM agent_workspaces ORDER BY name LIMIT 50"
                ).fetchall()
            ]
            th_rows = [
                {
                    "thread_id": r["thread_id"],
                    "profile": r["profile"],
                    "owner": r["owner"],
                    "chat_id": r["chat_id"],
                    "stream_spec": spec_of(r["chat_id"], r["platform"]) or r["chat_id"],
                    "state": r["state"],
                    "updated": r["updated"],
                }
                for r in db.execute(
                    "SELECT thread_id, profile, owner, chat_id, platform, state, updated FROM agent_threads ORDER BY updated DESC LIMIT 15"
                ).fetchall()
            ]
            mem_rows = [
                {
                    "chat_id": r["chat_id"],
                    "stream_spec": spec_of(r["chat_id"]) or r["chat_id"],
                    "version": r["version"],
                    "chars": len(r["content"] or ""),
                }
                for r in db.execute(
                    'SELECT v.chat_id, v.version, v.content FROM memory_versions v '
                    'JOIN (SELECT asset, MAX(version) AS version FROM memory_versions GROUP BY asset) latest '
                    'ON v.asset=latest.asset AND v.version=latest.version '
                    'WHERE v.kind="memory" AND length(trim(v.content))>0 ORDER BY v.updated DESC LIMIT 30'
                ).fetchall()
            ]
        return ws_rows, th_rows, mem_rows

    workspaces, recent_threads, memory_chats = await completed_thread(_runtime_snapshots)
    heartbeat = await completed_thread(plugin.memory.read, "heartbeat", allow_custom=True)
    services = await plugin.management.read("services", "{}")
    webhooks = await plugin.management.read("webhooks", "{}")
    current_spec = (
        plugin.canonical_stream_spec(current_chat_id, current_platform)
        if hasattr(plugin, "canonical_stream_spec")
        else ""
    ) or current_chat_id

    payload = {
        "kind": "overview",
        "current_chat": {
            "chat_id": current_chat_id,
            "platform": current_platform,
            "stream_spec": current_spec,
            "enabled": bool(
                hasattr(plugin, "enabled_for")
                and plugin.enabled_for(current_chat_id, current_platform)
            ),
        },
        "settings": settings_state["settings"],
        "settings_version": settings_state["version"],
        "known_chats": known_chats,
        "profiles": profiles,
        "skills": list(assets.get("skill_items") or ()),
        "host_models": host_models,
        "vault_handles": list(plugin.vault.handles()),
        "pending_intents": pending,
        "workspaces": workspaces,
        "recent_threads": recent_threads,
        "memory_chats": memory_chats,
        "heartbeat": heartbeat,
        "services": services.get("configuration") or {},
        "webhooks": webhooks.get("configuration") or {},
    }

    if target_chat and str(target_chat).strip():
        tc_id, tc_plat = await resolve_chat_target(
            plugin,
            target_chat,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
        )
        tc_spec = (
            plugin.canonical_stream_spec(tc_id, tc_plat)
            if hasattr(plugin, "canonical_stream_spec")
            else ""
        ) or tc_id
        tc_mem = await completed_thread(
            plugin.memory.read, "memory", chat_id=tc_id, allow_custom=True
        )
        tc_digest = await handle_digest_sources(
            plugin,
            action="read",
            target="digest",
            target_chat=tc_id,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
        )
        tc_aliases = (
            {f"chat:{a}" for a in plugin.chat_aliases_for(tc_id, tc_plat) | {tc_id}}
            if hasattr(plugin, "chat_aliases_for")
            else {f"chat:{tc_id}"}
        )
        payload["target_stream"] = {
            "chat_id": tc_id,
            "platform": tc_plat,
            "stream_spec": tc_spec,
            "enabled": bool(hasattr(plugin, "enabled_for") and plugin.enabled_for(tc_id, tc_plat)),
            "memory": tc_mem,
            "digest_sources": tc_digest,
            "intents": [row for row in pending if row.get("target") in tc_aliases],
        }

    return _redact(plugin, payload)


async def execute_shape(
    plugin,
    *,
    action: str,
    kind: str,
    target: str = "",
    target_chat: str = "",
    document: str = "",
    content: str = "",
    patch: dict | None = None,
    version: int | None = None,
    expected_version: int | None = None,
    base: str = "scout",
    url: str = "",
    preview_token: str = "",
    current_chat_id: str = "",
    current_platform: str = "",
    author: str = "maisya_shape",
) -> dict:
    if kind == "overview":
        if action != "read":
            raise ValueError("overview 仅支持 action='read'")
        return await build_overview(
            plugin,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
            target_chat=target_chat,
        )

    if kind == "digest_sources":
        return _redact(
            plugin,
            await handle_digest_sources(
                plugin,
                action=action,
                target=target,
                target_chat=target_chat,
                patch=patch,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
            ),
        )

    if kind == "settings":
        if action == "read":
            return _redact(plugin, await read_settings_state(plugin))
        if action == "patch":
            return _redact(
                plugin,
                await patch_settings(
                    plugin,
                    patch,
                    current_chat_id=current_chat_id,
                    current_platform=current_platform,
                    expected_version=expected_version,
                    author=author,
                ),
            )
        if action == "write":
            data = patch if isinstance(patch, dict) else json.loads(content or "{}")
            return _redact(
                plugin,
                await write_settings(plugin, data, expected_version=expected_version, author=author),
            )
        if action == "history":
            return _redact(plugin, await settings_history(plugin))
        if action == "rollback":
            if version is None:
                raise ValueError("回滚设置需要提供 version")
            return _redact(
                plugin,
                await rollback_settings(
                    plugin, int(version), expected_version=expected_version, author=author
                ),
            )
        raise ValueError(f"settings 不支持 action={action}")

    if kind == "profile":
        if action == "patch":
            return _redact(
                plugin,
                await patch_profile(
                    plugin,
                    target,
                    patch,
                    current_chat_id=current_chat_id,
                    current_platform=current_platform,
                    expected_version=expected_version,
                    author=author,
                ),
            )
        if action == "create":
            opts = dict(patch or {})
            base_id = str(opts.pop("base", base or "scout") or "scout")
            await completed_thread(plugin.profiles.clone, base_id, target)
            if opts:
                return _redact(
                    plugin,
                    await patch_profile(
                        plugin,
                        target,
                        opts,
                        current_chat_id=current_chat_id,
                        current_platform=current_platform,
                        author=author,
                    ),
                )
            doc_res = await completed_thread(
                plugin.memory.read,
                "profile",
                target=target,
                document=document,
                allow_custom=True,
            )
            prof_info = await completed_thread(profile_summary, plugin, target)
            return _redact(plugin, {**doc_res, "profile": prof_info})
        if action == "delete":
            deleted_res = await completed_thread(plugin.profiles.delete, target)
            return {
                "kind": "profile",
                "target": target,
                **(deleted_res if isinstance(deleted_res, dict) else {"deleted": True}),
            }

    if kind == "skill":
        return _redact(
            plugin,
            await handle_skill(
                plugin,
                action=action,
                target=target,
                content=content,
                patch=patch,
                expected_version=expected_version,
                version=version,
                author=author,
            ),
        )

    if kind in {"services", "webhooks"}:
        return _redact(
            plugin,
            await handle_services_or_webhooks(
                plugin, kind=kind, action=action, content=content, patch=patch
            ),
        )

    if kind == "recipe":
        return _redact(
            plugin,
            await handle_recipe(
                plugin,
                action=action,
                target=target,
                url=url,
                preview_token=preview_token,
                patch=patch,
            ),
        )

    if kind not in _MEMORY_KINDS:
        raise ValueError(f"不支持的塑造类型：{kind}")

    effective_chat_id = current_chat_id
    if target_chat and str(target_chat).strip():
        effective_chat_id, _ = await resolve_chat_target(
            plugin,
            target_chat,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
            allow_unknown_ascii=False,
        )

    if action == "read":
        res = await completed_thread(
            plugin.memory.read,
            kind,
            target=target,
            chat_id=effective_chat_id,
            document=document,
            version=version,
            allow_custom=True,
        )
        if kind == "profile" and (not document or document == "profile.toml"):
            res["profile"] = await completed_thread(profile_summary, plugin, target)
        return _redact(plugin, res)

    if action == "patch":
        return _redact(
            plugin,
            await patch_memory_doc(
                plugin,
                kind=kind,
                target=target,
                chat_id=effective_chat_id,
                document=document,
                patch=patch,
                expected_version=expected_version,
                author=author,
            ),
        )

    if action == "write":
        if expected_version is None:
            raise ValueError("覆写文档必须提供 expected_version")
        _assert_no_secret(plugin, content, kind)
        res = await completed_thread(
            plugin.memory.write,
            kind,
            content,
            expected_version=expected_version,
            target=target,
            chat_id=effective_chat_id,
            document=document,
            author=author,
            allow_custom=True,
        )
        return _redact(plugin, res)

    if action == "history":
        return _redact(
            plugin,
            {
                "kind": kind,
                "history": await completed_thread(
                    plugin.memory.history,
                    kind,
                    target=target,
                    chat_id=effective_chat_id,
                    document=document,
                ),
            },
        )

    if action == "rollback":
        if version is None or expected_version is None:
            raise ValueError("回滚文档需要提供 version 与 expected_version")
        return _redact(
            plugin,
            await completed_thread(
                plugin.memory.rollback,
                kind,
                version,
                expected_version=expected_version,
                target=target,
                chat_id=effective_chat_id,
                document=document,
                allow_custom=True,
            ),
        )

    raise ValueError(f"不支持 action={action}")
