"""管理员自管理与极简塑造接口（overview / settings / profile / file）。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import tomllib
from pathlib import Path

from .config import Settings
from .memory import SKILL_ID_RE
from .pathsafe import path_is_link_like
from .trust import format_stream_spec, parse_stream_spec, session_rank_key
from .vault import atomic_private_write
from .workspaces import completed_thread

_SETTINGS_HISTORY_FILE = "settings_history.json"
_MAX_SETTINGS_HISTORY = 50
_CURRENT_CHAT_ALIASES = {"", "current", "here", "this", "当前", "本群", "当前会话"}
_LOCKOUT_MESSAGE = "拒绝在聊天内关闭总开关（plugin.enabled=false 会锁死管理入口，请前往 WebUI 操作）"
_SLEEP_WINDOW_RE = re.compile(
    r"^(\d{1,2})(?:\s*[:：]\s*(\d{2}))?\s*(?:-|–|—|~|～|至|到)\s*(\d{1,2})(?:\s*[:：]\s*(\d{2}))?$"
)
_DIGEST_WORKSPACE_ALIASES = frozenset(
    {"", "digest", "digest-feeds", "digest_feeds", "digest-news", "digest-daily", "digest_feed"}
)
_PROFILE_TOML_KEYS = {
    "title", "description", "enabled", "base", "session_mode", "access",
    "allow_chats", "allow_users", "share_credentials", "model", "model_name",
    "fallback_models", "toolsets", "tools", "skills", "connectors", "mcp",
    "apis", "secrets", "sandbox_targets", "network", "environment", "task",
    "max_tokens", "context_tokens", "temperature", "max_steps", "web_provider",
    "bound", "fish_seed",
}
DEFAULT_DIGEST_SCHEDULE_PROMPT = (
    "结合当前会话 MEMORY.md 中记录的关注主题、已订阅资讯源与读者反馈，"
    "拆分为 3~4 个具体子方向用 8~12 组中英文短词调用 digest_feed(action='collect', max_items=20) 广搜（兼顾官方一手发布与权威垂直媒体的深度报道、专访与数据盘点），"
    "向 digest_feed(action='score_select', max_items=5) 一次提交 5~7 条高分候选以精选出 4~5 条丰富内容（quote 必须从 source_text 原语言逐字复制，严禁把中文翻译成英文或改写；无需在参数里重复粘贴长段 source_text），"
    "每条生成中文标题、2~4 句扎实口语化中文摘要（110~180 字）与 1 句入选看点（有连续数字或对比数据时附带 viz 图解）后，"
    "调用 digest_feed(action='render_card_html', max_items=5, output_path='out/digest-card.html') 输出卡片 HTML，"
    "再用 html_render(html_path='out/digest-card.html', output_path='out/digest-card.png', width=600, selector='.card') 渲染出图并 result_attach 交付。"
)


def normalize_sleep_window(raw: object) -> str:
    """规范化睡眠安静时段写法；关闭返回空字符串。"""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("sleep_window 必须是字符串（如 01-08 或 off）")
    text = raw.strip()
    if text.lower() in {"", "off", "none", "disable", "disabled", "关闭", "不限制", "无"}:
        return ""
    match = _SLEEP_WINDOW_RE.fullmatch(text)
    if match is None:
        raise ValueError("sleep_window 格式应为 HH-HH 或 HH:MM-HH:MM，关闭请传 off")
    sh, sm, eh, em = int(match.group(1)), int(match.group(2) or 0), int(match.group(3)), int(match.group(4) or 0)
    if not (0 <= sm <= 59 and 0 <= em <= 59) or sh > 24 or eh > 24 or (sh == 24 and sm) or (eh == 24 and em):
        raise ValueError("sleep_window 小时或分钟范围无效")
    sh = 0 if sh == 24 else sh
    if (sh, sm) == (eh, em):
        raise ValueError("sleep_window 开始时间与结束时间不能相同")
    return f"{sh:02d}-{eh:02d}" if (sm == 0 and em == 0) else f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"


def normalize_profile_workspace(profile_id: str, workspace: str) -> str:
    """统一 digest 角色的工作区别名。"""
    pid, ws = str(profile_id or "").strip(), str(workspace or "").strip()
    return "digest" if pid == "digest" and ws.lower() in _DIGEST_WORKSPACE_ALIASES else ws


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
    if vault.contains_secret(raw if isinstance(raw, bytes) else raw.encode("utf-8")):
        raise ValueError(f"{label}包含已知凭据明文，已拒绝写入")


def _clean_str_list(value, *, label: str = "列表") -> list[str]:
    items = [value] if isinstance(value, str) else (list(value) if isinstance(value, (list, tuple, set)) else None)
    if items is None:
        raise ValueError(f"{label}必须是字符串或字符串列表")
    return list(dict.fromkeys(str(item or "").strip() for item in items if str(item or "").strip()))


def _stream_aliases(session_id: str, info: dict) -> set[str]:
    plat, ctype = str(info.get("platform") or "").strip().lower(), str(info.get("chat_type") or "").strip().lower()
    gid, uid = str(info.get("group_id") or "").strip(), str(info.get("user_id") or "").strip()
    out = {session_id} if session_id else set()
    for tag, tid in (("group", gid), ("private", uid)):
        if plat and tid:
            out.update({format_stream_spec(plat, tag, tid), f"{plat}:{tag}:{tid}", tid, f"{tag}:{tid}"})
    if plat and ctype in ("group", "private") and (gid or uid):
        out.add(format_stream_spec(plat, ctype, gid or uid))
    if str(info.get("stream_spec") or "").strip():
        out.add(str(info["stream_spec"]).strip())
    return {x for x in out if x}


async def _session_snapshot(plugin) -> tuple[dict, object, dict[str, dict]]:
    streams = await plugin._resolve_chat_sources() if hasattr(plugin, "_resolve_chat_sources") else {}
    messages = getattr(plugin, "messages", None)
    routes = {r["session_id"]: r for r in (messages.list_routes() if messages is not None else [])}
    merged: dict[str, dict] = {}
    for sid in {*streams.keys(), *routes.keys()}:
        if not sid:
            continue
        s, r = streams.get(sid) or {}, routes.get(sid) or {}
        plat = str(s.get("platform") or r.get("platform") or "").strip().lower()
        gid, uid = str(s.get("group_id") or r.get("group_id") or "").strip(), str(s.get("user_id") or r.get("user_id") or "").strip()
        ctype = str(s.get("chat_type") or r.get("chat_type") or ("group" if gid else ("private" if uid else ""))).strip().lower()
        spec = str(s.get("stream_spec") or "").strip() or (format_stream_spec(plat, "group" if gid else "private", gid or uid) if plat and (gid or uid) else "")
        merged[sid] = {
            "session_id": sid, "platform": plat, "chat_type": ctype, "group_id": gid, "user_id": uid,
            "stream_spec": spec, "name": str(s.get("name") or "").strip(),
            "account_id": str(s.get("account_id") or r.get("account_id") or "").strip(),
            "human_count": int(s.get("human_count") or r.get("human_count") or 0),
            "last_human_at": float(s.get("last_human_at") or r.get("last_human_at") or 0.0),
            "updated": float(s.get("updated") or r.get("updated") or 0.0),
            "avatar": str(s.get("avatar") or ""),
        }
    return streams, messages, merged


def _pick_match(hits: list[dict], raw: str, cur_plat: str, *, preferred_account_id: str = "") -> tuple[str, str]:
    dedup = list({h["session_id"]: h for h in hits if h.get("session_id")}.values())
    if cur_plat:
        plat_hits = [h for h in dedup if str(h.get("platform") or "").lower() == cur_plat]
        if plat_hits:
            dedup = plat_hits
    if len(dedup) > 1:
        def _lkey(m: dict) -> tuple[str, str, str] | None:
            p, g, u = str(m.get("platform") or "").lower(), str(m.get("group_id") or ""), str(m.get("user_id") or "")
            return (p, "group" if g else "private", g or u) if p and (g or u) else None
        if len({_lkey(h) for h in dedup if _lkey(h)}) > 1:
            names = ", ".join(f"{h.get('name') or h['session_id']}({h.get('stream_spec') or h['session_id']})" for h in dedup[:5])
            raise ValueError(f"「{raw}」匹配到多个会话：{names}，请改用 平台:类型:id 精确指定")
    best = max(dedup, key=lambda m: session_rank_key(m, preferred_account_id=preferred_account_id))
    return best["session_id"], str(best.get("platform") or cur_plat or "qq").strip().lower()


async def resolve_chat_target(plugin, raw_target: str | None, *, current_chat_id: str = "", current_platform: str = "", allow_unknown_ascii: bool = True) -> tuple[str, str]:
    """将群名、流标识或 ID 解析为 (session_id, platform)。"""
    raw, cur_plat = str(raw_target or "").strip(), str(current_platform or "").strip().lower()
    if raw in _CURRENT_CHAT_ALIASES or raw.lower() in {"current", "here", "this"}:
        if not current_chat_id:
            raise ValueError("缺少目标会话，请提供 平台:类型:id")
        return current_chat_id, cur_plat or "qq"
    streams, messages, merged = await _session_snapshot(plugin)
    pref_acct = str((messages.route_for(current_chat_id) or {}).get("account_id") or "") if messages and current_chat_id else ""
    if raw in merged:
        return raw, str(merged[raw].get("platform") or cur_plat or "qq").lower()
    parsed = parse_stream_spec(raw)
    norm_spec = format_stream_spec(*parsed) if parsed else ""
    if messages is not None:
        route = messages.route_for(raw)
        if route:
            return raw, str(route.get("platform") or cur_plat or "qq").lower()
        found = messages.find_sessions(raw)
        if found:
            return _pick_match([dict(merged.get(f["session_id"]) or f) for f in found], raw, cur_plat, preferred_account_id=pref_acct)
    hits = [info for sid, info in merged.items() if raw in _stream_aliases(sid, info) or (norm_spec and norm_spec in _stream_aliases(sid, info))]
    if hits:
        return _pick_match(hits, raw, cur_plat, preferred_account_id=pref_acct)
    low = raw.lower()
    name_hits = [info for info in merged.values() if info.get("name") and low in str(info["name"]).lower()]
    if name_hits:
        exact = [info for info in name_hits if str(info["name"]).lower() == low]
        return _pick_match(exact or name_hits, raw, cur_plat, preferred_account_id=pref_acct)
    if parsed is not None:
        plat, kind, tid = parsed
        ctx = getattr(plugin, "ctx", None) or getattr(plugin, "_ctx", None)
        if ctx and callable(getattr(ctx, "call_capability", None)):
            try:
                res = await ctx.call_capability("chat.open_session", platform=plat, chat_type=kind, group_id=tid if kind == "group" else "", user_id=tid if kind == "private" else "")
                if isinstance(res, dict) and res.get("session_id"):
                    return str(res["session_id"]), plat
            except (RuntimeError, OSError, ValueError, TypeError):
                pass
        return norm_spec, plat
    if raw in set(getattr(plugin.settings, "enabled_chats", ()) or ()) or raw == current_chat_id or (re.fullmatch(r"[A-Za-z0-9_.:-]+", raw) and (allow_unknown_ascii or not streams)):
        return raw, cur_plat or "qq"
    raise ValueError(f"未找到匹配的会话「{raw}」，请使用 平台:类型:id（如 qq:group:100246809）")


async def resolve_stream_spec(plugin, raw_target: str | None, *, current_chat_id: str = "", current_platform: str = "", allow_unknown_ascii: bool = True) -> str:
    """将任意会话标识归一化为标准 平台:类型:id 或既有短 ID。"""
    raw = str(raw_target or "").strip()
    if raw in _CURRENT_CHAT_ALIASES or raw.lower() in {"current", "here", "this"}:
        canon = plugin.canonical_stream_spec(current_chat_id, current_platform) if hasattr(plugin, "canonical_stream_spec") else ""
        return canon or current_chat_id
    parsed = parse_stream_spec(raw)
    if parsed is not None:
        return format_stream_spec(*parsed)
    cid, plat = await resolve_chat_target(plugin, raw, current_chat_id=current_chat_id, current_platform=current_platform, allow_unknown_ascii=allow_unknown_ascii)
    canon = plugin.canonical_stream_spec(cid, plat) if hasattr(plugin, "canonical_stream_spec") else ""
    return canon or cid


async def collect_known_chats(plugin) -> list[dict]:
    """合并宿主会话、已记录路由与白名单，返回去重会话清单。"""
    _, _, merged = await _session_snapshot(plugin)
    enabled_set = set(getattr(plugin.settings, "enabled_chats", ()) or ())
    for item in list(enabled_set):
        parsed = parse_stream_spec(item)
        if parsed is not None:
            enabled_set.add(format_stream_spec(*parsed))
    level_map = dict(getattr(plugin.settings, "chat_proactive_levels", None) or {})
    records, known_aliases = [], set()
    for sid, rec in merged.items():
        aliases = (set(plugin.chat_aliases_for(sid, rec["platform"])) if hasattr(plugin, "chat_aliases_for") else set()) | _stream_aliases(sid, rec)
        known_aliases.update(aliases)
        records.append({**rec, "aliases": aliases, "display_name": rec["name"] or rec["group_id"] or rec["user_id"] or rec["stream_spec"] or sid})
    for raw in getattr(plugin.settings, "enabled_chats", ()) or ():
        tok = str(raw or "").strip()
        if not tok:
            continue
        parsed = parse_stream_spec(tok)
        norm = format_stream_spec(*parsed) if parsed else tok
        if tok in known_aliases or norm in known_aliases:
            continue
        plat, ctype, tid = parsed if parsed else ("", "", tok)
        aliases = {tok, norm}
        known_aliases.update(aliases)
        records.append({
            "session_id": norm, "stream_spec": norm if parsed else "", "display_name": tid or norm, "name": "",
            "platform": plat, "chat_type": ctype, "group_id": tid if ctype == "group" else "",
            "user_id": tid if ctype == "private" else "", "avatar": "", "aliases": aliases, "updated": 0.0, "human_count": 0, "last_human_at": 0.0,
        })
    grouped: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        grouped.setdefault(("spec", rec["stream_spec"]) if rec["stream_spec"] else ("sid", rec["session_id"]), []).append(rec)
    out = []
    for grp in grouped.values():
        best = max(grp, key=session_rank_key) if len(grp) > 1 else grp[0]
        aliases = set().union(*(g["aliases"] for g in grp))
        avatar = best.get("avatar") or next((g.get("avatar", "") for g in grp if g.get("avatar")), "")
        name = next((g["name"] for g in sorted(grp, key=session_rank_key, reverse=True) if g.get("name") and g["name"] not in aliases), best["display_name"])
        level = next((level_map[c] for c in [best["stream_spec"], best["session_id"], *sorted(aliases)] if c and c in level_map), "")
        out.append({
            "chat_id": best["session_id"], "stream_spec": best["stream_spec"], "stream_id": best["stream_spec"] or best["session_id"],
            "name": name, "platform": best["platform"], "chat_type": best["chat_type"], "group_id": best["group_id"],
            "user_id": best["user_id"], "avatar": avatar, "enabled": bool(aliases & enabled_set), "proactive_level": level,
        })
    return out


def _effective_host_admins(plugin) -> set[str]:
    info = getattr(plugin, "admins_info", None)
    if isinstance(info, dict) and "host_admins" in info:
        return set(info.get("host_admins") or ())
    if hasattr(plugin, "host_admins"):
        return set(getattr(plugin, "host_admins") or ())
    return set(plugin._host_admins() or ()) if callable(getattr(plugin, "_host_admins", None)) else set()


def _verify_admin(plugin, source_msg_id: str, chat_id: str, platform: str, *, require_auth: bool, admin_verified: bool) -> str:
    msg_id = str(source_msg_id or "").strip()
    auth_fn = getattr(plugin, "authorize_admin_source", None)
    if require_auth:
        ok = admin_verified or (callable(auth_fn) and auth_fn(msg_id, chat_id, platform))
        if not ok:
            raise PermissionError("此变更动作需要已核验的真实管理员身份")
    elif msg_id and callable(auth_fn) and not auth_fn(msg_id, chat_id, platform) and not admin_verified:
        raise PermissionError("管理员身份核验失败")
    messages = getattr(plugin, "messages", None)
    if msg_id and messages is not None:
        plat = str(platform or "").strip() or (messages.platform_for(chat_id) if chat_id else "")
        msg = messages.lookup(msg_id, chat_id, plat) if (chat_id and plat and hasattr(messages, "lookup")) else None
        if msg is not None and not msg.is_plugin_relayed:
            return f"{msg.platform.lower()}:{msg.user_id}"
    return ""


def _history_path(plugin) -> Path:
    return plugin.data_root / _SETTINGS_HISTORY_FILE


def _load_settings_history(plugin) -> dict:
    path, current = _history_path(plugin), plugin.settings.model_dump()
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() and not path_is_link_like(path) else {"versions": []}
    versions = list(data.get("versions") or [])
    if not versions or versions[-1].get("snapshot") != current:
        ver = (int(versions[-1]["version"]) + 1) if versions else 1
        versions = (versions + [{"version": ver, "timestamp": time.time(), "author": "sync", "changes": [], "snapshot": copy.deepcopy(current)}])[-_MAX_SETTINGS_HISTORY:]
        atomic_private_write(path, json.dumps({"versions": versions}, ensure_ascii=False).encode("utf-8"))
    return {"versions": versions}


async def read_settings_state(plugin) -> dict:
    hist = await completed_thread(_load_settings_history, plugin)
    eff = sorted(_effective_host_admins(plugin) | set(getattr(plugin.settings, "admins", ()) or ()))
    return {"kind": "settings", "version": hist["versions"][-1]["version"], "settings": plugin.settings.model_dump(), "effective_admins": eff}


async def settings_history(plugin) -> dict:
    hist = await completed_thread(_load_settings_history, plugin)
    return {"kind": "settings", "history": [{"version": v["version"], "timestamp": v["timestamp"], "author": v.get("author", ""), "changes": v.get("changes", [])} for v in hist["versions"]]}


async def _commit_settings(plugin, candidate: dict, *, expected_version: int | None, author: str, changes: list[str], caller_ident: str = "") -> dict:
    _assert_no_secret(plugin, candidate, "settings")
    if isinstance(candidate.get("plugin"), dict) and candidate["plugin"].get("enabled") is False:
        raise ValueError(_LOCKOUT_MESSAGE)
    if getattr(plugin.settings, "enabled_chats", None) and not candidate.get("enabled_chats"):
        raise ValueError("禁止移除全部已启用会话（至少保留 1 个有效会话以防管理锁死）")
    if "admins" in Settings.model_fields:
        host_admins = _effective_host_admins(plugin)
        old_eff = host_admins | set(getattr(plugin.settings, "admins", ()) or ())
        new_eff = host_admins | set(candidate.get("admins") or ())
        if (old_eff or "admins" in changes) and not new_eff:
            raise ValueError("禁止清空全部管理员（至少保留 1 名有效管理员，防止自锁死）")
        if caller_ident and caller_ident in old_eff and caller_ident not in new_eff:
            raise ValueError("禁止在会话内移除当前操作者自身的管理员权限，防止自锁死")
    validated = Settings(**candidate).model_dump()
    async with _shaping_lock(plugin):
        hist = await completed_thread(_load_settings_history, plugin)
        prev = hist["versions"][-1]
        if expected_version is not None and int(expected_version) != int(prev["version"]):
            raise ValueError(f"设置版本冲突：当前版本 {prev['version']}，传入 expected_version={expected_version}")
        await plugin.persist_settings(validated)
        new_ver = int(prev["version"]) + 1
        entry = {"version": new_ver, "timestamp": time.time(), "author": author, "changes": changes, "snapshot": copy.deepcopy(validated)}
        versions = (hist["versions"] + [entry])[-_MAX_SETTINGS_HISTORY:]
        await completed_thread(atomic_private_write, _history_path(plugin), json.dumps({"versions": versions}, ensure_ascii=False).encode("utf-8"))
    return {
        "kind": "settings", "version": new_ver, "previous_version": int(prev["version"]),
        "changes": changes, "snapshot": validated, "settings": validated,
        "rollback_hint": f"maisya_shape(action='settings', op='rollback', version={prev['version']}, expected_version={new_ver})",
    }


def _apply_list_delta(current: list[str], *, add_items: list[str] = (), remove_items: list[str] = ()) -> list[str]:
    out = list(current)
    for item in add_items:
        if item not in out:
            out.append(item)
    rem = set(remove_items)
    return [x for x in out if x not in rem] if rem else out


async def patch_settings(plugin, patch: dict | None, *, current_chat_id: str = "", current_platform: str = "", expected_version: int | None = None, author: str = "maisya_shape", caller_ident: str = "") -> dict:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("settings patch 必须是非空对象")
    cand, changes = copy.deepcopy(plugin.settings.model_dump()), []
    if "enabled" in patch or (isinstance(patch.get("plugin"), dict) and "enabled" in patch["plugin"]):
        flag = patch["plugin"]["enabled"] if isinstance(patch.get("plugin"), dict) and "enabled" in patch["plugin"] else patch["enabled"]
        if not (flag is True or (isinstance(flag, str) and flag.strip().lower() in {"true", "1", "yes", "on"})):
            raise ValueError(_LOCKOUT_MESSAGE)
        cand.setdefault("plugin", {})["enabled"] = True
        changes.append("plugin.enabled")
    if "enabled_chats" in patch:
        cand["enabled_chats"] = list(dict.fromkeys([await resolve_stream_spec(plugin, x, current_chat_id=current_chat_id, current_platform=current_platform) for x in _clean_str_list(patch["enabled_chats"], label="enabled_chats")]))
        changes.append("enabled_chats")
    if "add_chats" in patch or "remove_chats" in patch:
        adds = [await resolve_stream_spec(plugin, x, current_chat_id=current_chat_id, current_platform=current_platform) for x in _clean_str_list(patch.get("add_chats", []), label="add_chats")]
        raw_rems = _clean_str_list(patch.get("remove_chats", []), label="remove_chats")
        rems = raw_rems + [await resolve_stream_spec(plugin, x, current_chat_id=current_chat_id, current_platform=current_platform) for x in raw_rems]
        cand["enabled_chats"] = _apply_list_delta(list(cand.get("enabled_chats") or []), add_items=adds, remove_items=rems)
        changes.append("enabled_chats")
    if "admins" in Settings.model_fields:
        if "admins" in patch:
            cand["admins"] = _clean_str_list(patch["admins"], label="admins")
            changes.append("admins")
        if "add_admins" in patch or "remove_admins" in patch:
            cand["admins"] = _apply_list_delta(list(cand.get("admins") or []), add_items=_clean_str_list(patch.get("add_admins", []), label="add_admins"), remove_items=_clean_str_list(patch.get("remove_admins", []), label="remove_admins"))
            changes.append("admins")
    for simple in ("permission_mode", "default_model", "proactive_level", "ui_style"):
        if simple in patch and simple in Settings.model_fields:
            cand[simple] = str(patch[simple] or "").strip()
            changes.append(simple)
    if "sleep_window" in patch:
        cand["sleep_window"] = normalize_sleep_window(patch["sleep_window"])
        changes.append("sleep_window")
    if "daily_proactive_limit" in patch and "daily_proactive_limit" in Settings.model_fields:
        cand["daily_proactive_limit"] = int(patch["daily_proactive_limit"])
        changes.append("daily_proactive_limit")
    if not changes:
        raise ValueError("未提供可修改的核心设置字段")
    return await _commit_settings(plugin, cand, expected_version=expected_version, author=author, changes=changes, caller_ident=caller_ident)


async def write_settings(plugin, data: dict, *, expected_version: int | None = None, author: str = "maisya_shape", caller_ident: str = "") -> dict:
    if not isinstance(data, dict):
        raise ValueError("settings write 需要对象")
    return await _commit_settings(plugin, data, expected_version=expected_version, author=author, changes=["replace"], caller_ident=caller_ident)


async def rollback_settings(plugin, version: int, *, expected_version: int | None = None, author: str = "maisya_shape", caller_ident: str = "") -> dict:
    hist = await completed_thread(_load_settings_history, plugin)
    target = next((v for v in hist["versions"] if int(v["version"]) == int(version)), None)
    if target is None:
        raise ValueError(f"未找到设置版本 {version}")
    return await _commit_settings(plugin, copy.deepcopy(target["snapshot"]), expected_version=expected_version, author=author, changes=[f"rollback:{version}"], caller_ident=caller_ident)


def _format_toml(cfg: dict) -> str:
    def _val(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, (list, tuple)):
            return "[" + ", ".join(_val(i) for i in v) + "]"
        return json.dumps(str(v), ensure_ascii=False)
    return "\n".join(f"{k} = {_val(v)}" for k, v in cfg.items() if v is not None) + "\n"


def _profile_summary(plugin, profile_id: str) -> dict:
    prof = plugin.profiles.get(profile_id)
    return {
        "profile_id": profile_id, "title": getattr(prof, "title", "") or profile_id,
        "description": getattr(prof, "description", ""), "enabled": bool(prof.enabled),
        "access": getattr(prof, "access", "everyone"), "session_mode": getattr(prof, "session_mode", "task"),
        "allow_chats": list(getattr(prof, "allow_chats", ()) or ()), "skills": list(getattr(prof, "skills", ()) or ()),
        "mcp": list(getattr(prof, "mcp", ()) or ()),
    }


async def _handle_profile(plugin, *, op: str, target: str, base: str, patch: dict | None, content: str, author: str) -> dict:
    if op == "list":
        ids = sorted(p.name for p in plugin.profiles.path.iterdir() if p.is_dir() and not path_is_link_like(p) and (p / "profile.toml").is_file())
        return {"kind": "profile", "profiles": [await completed_thread(_profile_summary, plugin, pid) for pid in ids]}
    if not target:
        raise ValueError("profile 操作需要指定 target（档案标识）")
    if op == "read":
        doc = await completed_thread(plugin.memory.read, "profile", target=target, document="PROFILE.md", allow_custom=True)
        return {**doc, "kind": "profile", "profile": await completed_thread(_profile_summary, plugin, target)}
    if op == "delete":
        res = await completed_thread(plugin.profiles.delete, target)
        await completed_thread(plugin.memory.purge_profile, target)
        return {"kind": "profile", "target": target, **(res if isinstance(res, dict) else {"deleted": True})}
    if op == "create":
        opts = dict(patch or {}) if isinstance(patch, dict) else {}
        base_id = str(opts.pop("base", base or "") or "").strip() or ("default" if (plugin.profiles.path / "default" / "profile.toml").is_file() else "scout")
        await completed_thread(plugin.profiles.clone, base_id, target)
        await completed_thread(plugin.memory.seed_profile, target)
        if not opts and not content:
            return {"kind": "profile", "profile": await completed_thread(_profile_summary, plugin, target)}
        patch, op = opts, "patch"
    if op == "patch":
        opts = dict(patch or {}) if isinstance(patch, dict) else {}
        _assert_no_secret(plugin, opts, "profile")
        toml_cur = await completed_thread(plugin.memory.read, "profile", target=target, document="profile.toml", allow_custom=True)
        cfg = {**tomllib.loads(toml_cur["content"] or ""), **{k: v for k, v in opts.items() if k in _PROFILE_TOML_KEYS}}
        wr = await completed_thread(plugin.memory.write, "profile", _format_toml(cfg), expected_version=toml_cur["version"], target=target, document="profile.toml", author=author, allow_custom=True)
        md_text = content or opts.get("instructions") or opts.get("profile_md")
        if isinstance(md_text, str) and md_text.strip():
            _assert_no_secret(plugin, md_text, "PROFILE.md")
            md_cur = await completed_thread(plugin.memory.read, "profile", target=target, document="PROFILE.md", allow_custom=True)
            wr = await completed_thread(plugin.memory.write, "profile", md_text, expected_version=md_cur["version"], target=target, document="PROFILE.md", author=author, allow_custom=True)
        return {"kind": "profile", "target": target, "version": wr["version"], "profile": await completed_thread(_profile_summary, plugin, target), "rollback_hint": f"maisya_shape(action='file', op='rollback', target='{target}', document='PROFILE.md', version={wr['version'] - 1})"}
    raise ValueError(f"profile 不支持 op={op}")


def _resolve_file_route(kind: str, target: str, document: str) -> tuple[str, str, str]:
    raw = str(document or "").strip()
    if any(bad in raw for bad in ("\x00", "..", "\\")) or raw.startswith("/"):
        raise ValueError("受管文件路径禁止目录穿越或绝对路径")
    if kind in {"memory", "journal", "heartbeat", "skill"}:
        return kind, target, raw
    fixed = {"TOOLS.md": ("tools", "self", "TOOLS.md"), "memory/self/TOOLS.md": ("tools", "self", "TOOLS.md"), "JOURNAL.md": ("journal", "self", "JOURNAL.md"), "memory/self/JOURNAL.md": ("journal", "self", "JOURNAL.md"), "HEARTBEAT.md": ("heartbeat", "self", "HEARTBEAT.md"), "memory/self/HEARTBEAT.md": ("heartbeat", "self", "HEARTBEAT.md"), "MEMORY.md": ("memory", target, "MEMORY.md")}
    if raw in fixed or raw.upper() in fixed:
        return fixed.get(raw) or fixed[raw.upper()]
    parts = raw.split("/")
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md" and SKILL_ID_RE.fullmatch(parts[1]):
        return "skill", parts[1], "SKILL.md"
    if len(parts) == 3 and parts[0] == "profiles" and parts[2] in {"PROFILE.md", "profile.toml"} and SKILL_ID_RE.fullmatch(parts[1]):
        return "profile", parts[1], parts[2]
    raise ValueError(f"仅允许访问受管系统文件：{raw or '(未指定)'}")


def _tools_file_op(plugin, op: str, content: str, version: int | None, expected_version: int | None, author: str) -> dict:
    path, asset = plugin.data_root / "memory/self/TOOLS.md", "tools:self:TOOLS.md"
    if path_is_link_like(path) or path_is_link_like(path.parent):
        raise ValueError("受管文件路径禁止符号链接")
    with plugin.store.connect() as db, db:
        rows = db.execute("SELECT * FROM memory_versions WHERE asset=? ORDER BY version ASC", (asset,)).fetchall()
        cur_ver = int(rows[-1]["version"]) if rows else 0
        if op == "read":
            hit = next((r for r in rows if int(r["version"]) == int(version)), None) if version is not None else None
            if version is not None and hit is None:
                raise ValueError(f"未找到版本 {version}")
            return {"kind": "file", "document": "TOOLS.md", "version": int(hit["version"]) if hit else cur_ver, "content": hit["content"] if hit else (path.read_text(encoding="utf-8") if path.is_file() else "")}
        if op == "history":
            return {"kind": "file", "document": "TOOLS.md", "history": [dict(r) for r in rows]}
        if expected_version is None or int(expected_version) != cur_ver:
            raise ValueError(f"版本冲突或缺少 expected_version（当前版本 {cur_ver}）")
        if op == "rollback":
            hit = next((r for r in rows if int(r["version"]) == int(version or -1)), None)
            if hit is None:
                raise ValueError(f"未找到回滚版本 {version}")
            content = hit["content"]
        _assert_no_secret(plugin, content, "TOOLS.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_private_write(path, content.encode("utf-8"))
        new_ver = cur_ver + 1
        db.execute("INSERT INTO memory_versions (asset, version, kind, target, chat_id, document, content, author, updated) VALUES (?, ?, 'tools', 'self', '', 'TOOLS.md', ?, ?, ?)", (asset, new_ver, content, author, time.time()))
        return {"kind": "file", "document": "TOOLS.md", "version": new_ver, "previous_version": cur_ver, "content": content, "rollback_hint": f"maisya_shape(action='file', op='rollback', document='TOOLS.md', version={cur_ver}, expected_version={new_ver})"}


async def _handle_file(plugin, *, kind: str, op: str, target: str, target_chat: str, document: str, content: str, version: int | None, expected_version: int | None, current_chat_id: str, current_platform: str, author: str) -> dict:
    if op == "list":
        skills = [s["skill_id"] for s in await completed_thread(plugin.memory.skills)]
        return {"kind": "file", "files": ["JOURNAL.md", "HEARTBEAT.md", "TOOLS.md", "MEMORY.md", *(f"skills/{s}/SKILL.md" for s in skills)]}
    f_kind, f_target, f_doc = _resolve_file_route(kind, target, document)
    if f_kind == "tools":
        return await completed_thread(_tools_file_op, plugin, op, content, version, expected_version, author)
    chat_id = current_chat_id
    if target_chat and str(target_chat).strip():
        chat_id, _ = await resolve_chat_target(plugin, target_chat, current_chat_id=current_chat_id, current_platform=current_platform, allow_unknown_ascii=False)
    if op == "read":
        return await completed_thread(plugin.memory.read, f_kind, target=f_target, chat_id=chat_id, document=f_doc, version=version, allow_custom=True)
    if op == "history":
        return {"kind": "file", "history": await completed_thread(plugin.memory.history, f_kind, target=f_target, chat_id=chat_id, document=f_doc)}
    if op == "write":
        if expected_version is None:
            raise ValueError("覆写文档必须提供 expected_version")
        _assert_no_secret(plugin, content, f_doc or f_kind)
        res = await completed_thread(plugin.memory.write, f_kind, content, expected_version=expected_version, target=f_target, chat_id=chat_id, document=f_doc, author=author, allow_custom=True)
        return {**res, "rollback_hint": f"maisya_shape(action='file', op='rollback', document='{document or f_doc}', version={expected_version}, expected_version={res['version']})"}
    if op == "rollback":
        if version is None or expected_version is None:
            raise ValueError("回滚文档需要提供 version 与 expected_version")
        return await completed_thread(plugin.memory.rollback, f_kind, version, expected_version=expected_version, target=f_target, chat_id=chat_id, document=f_doc, allow_custom=True)
    raise ValueError(f"file 不支持 op={op}")


async def build_overview(plugin, *, current_chat_id: str = "", current_platform: str = "", target_chat: str = "") -> dict:
    known_chats = await collect_known_chats(plugin)
    pids = sorted(p.name for p in plugin.profiles.path.iterdir() if p.is_dir() and not path_is_link_like(p) and (p / "profile.toml").is_file())
    pending = plugin.intents.list_all(status="pending", limit=20) if getattr(plugin, "intents", None) is not None else []
    eff_admins = sorted(_effective_host_admins(plugin) | set(getattr(plugin.settings, "admins", ()) or ()))
    return {
        "kind": "overview", "plugin_enabled": bool(plugin.settings.plugin.enabled), "known_chats": known_chats, "admins": eff_admins,
        "profiles": [await completed_thread(_profile_summary, plugin, pid) for pid in pids],
        "intents": [{"intent_id": r["intent_id"], "mode": r.get("mode", "schedule"), "condition": r.get("condition", ""), "due_at": r["due_at"], "last_checked_at": r.get("last_checked_at"), "last_triggered_at": r.get("last_triggered_at")} for r in pending],
    }


async def execute_shape(
    plugin, *, action: str = "overview", kind: str = "", op: str = "", target: str = "", target_chat: str = "",
    document: str = "", content: str = "", patch: dict | None = None, version: int | None = None,
    expected_version: int | str | None = None, base: str = "", url: str = "", preview_token: str = "",
    current_chat_id: str = "", current_platform: str = "", source_msg_id: str = "", author: str = "maisya_shape", admin_verified: bool = False,
) -> dict:
    # WAVE2-COMPAT: unused url/preview_token kept until plugin.py core-wiring cleanup.
    del url, preview_token
    act, knd = str(action or "").strip().lower(), str(kind or "").strip().lower()
    if act in {"overview", "settings", "profile", "file"} and (not knd or knd == act):
        area = act
        resolved_op = str(op or "").strip().lower() or ("read" if area == "overview" else ("rollback" if version is not None else ("write" if content else ("patch" if patch is not None else "read"))))
    else:
        area, resolved_op = knd or act, str(op or act or "read").strip().lower()
    if area in {"setup", "webhooks", "services", "recipe", "digest_sources"}:
        raise ValueError(f"已收敛移除 {area}，仅支持 overview / settings / profile / file")
    caller_ident = _verify_admin(plugin, source_msg_id, current_chat_id, current_platform, require_auth=(resolved_op not in {"read", "list", "history"}), admin_verified=admin_verified)
    if area == "overview":
        if resolved_op != "read":
            raise ValueError("overview 仅支持只读操作")
        return _redact(plugin, await build_overview(plugin, current_chat_id=current_chat_id, current_platform=current_platform, target_chat=target_chat))
    if area == "settings":
        if resolved_op == "read":
            return _redact(plugin, await read_settings_state(plugin))
        if resolved_op == "history":
            return _redact(plugin, await settings_history(plugin))
        if resolved_op == "patch":
            return _redact(plugin, await patch_settings(plugin, patch, current_chat_id=current_chat_id, current_platform=current_platform, expected_version=expected_version, author=author, caller_ident=caller_ident))
        if resolved_op == "write":
            return _redact(plugin, await write_settings(plugin, patch if isinstance(patch, dict) else json.loads(content or "{}"), expected_version=expected_version, author=author, caller_ident=caller_ident))
        if resolved_op == "rollback":
            if version is None:
                raise ValueError("回滚设置需要提供 version")
            return _redact(plugin, await rollback_settings(plugin, int(version), expected_version=expected_version, author=author, caller_ident=caller_ident))
        raise ValueError(f"settings 不支持 op={resolved_op}")
    if area == "profile":
        return _redact(plugin, await _handle_profile(plugin, op=resolved_op, target=target, base=base, patch=patch, content=content, author=author))
    if area in {"file", "memory", "journal", "heartbeat", "tools", "skill"}:
        return _redact(plugin, await _handle_file(plugin, kind=area, op=resolved_op, target=target, target_chat=target_chat, document=document, content=content, version=version, expected_version=expected_version, current_chat_id=current_chat_id, current_platform=current_platform, author=author))
    raise ValueError(f"不支持的自管理类型：{area}（仅支持 overview / settings / profile / file）")
