"""管理员对话内配置与整形（settings / profile / skill / memory / overview / services）。"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
import shutil
import sqlite3
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from .abilities import READONLY_TOOLS
from .config import Settings
from .digest import DigestWorkspace
from .intents import next_daily_due
from .memory import SKILL_ID_RE, MemoryLayers, parse_skill_meta
from .pathsafe import path_is_link_like
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
DEFAULT_DIGEST_SCHEDULE_PROMPT = (
    "结合当前会话 MEMORY.md 中记录的关注主题、已订阅资讯源与读者反馈，"
    "拆分为 3~4 个具体子方向用 8~12 组中英文短词调用 digest_feed(action='collect', max_items=20) 广搜（兼顾官方一手发布与权威垂直媒体的深度报道、专访与数据盘点），"
    "向 digest_feed(action='score_select', max_items=5) 一次提交 5~7 条高分候选以精选出 4~5 条丰富内容（quote 必须从 source_text 原语言逐字复制，严禁把中文翻译成英文或改写；无需在参数里重复粘贴长段 source_text），"
    "每条生成中文标题、2~4 句扎实口语化中文摘要（110~180 字）与 1 句入选看点（有连续数字或对比数据时附带 viz 图解）后，"
    "调用 digest_feed(action='render_card_html', max_items=5, output_path='out/digest-card.html') 输出卡片 HTML，"
    "再用 html_render(html_path='out/digest-card.html', output_path='out/digest-card.png', width=600, selector='.card') 渲染出图并 result_attach 交付。"
)
_DIGEST_ADD_KEYS = ("rss_urls", "add_urls", "urls", "add", "feeds")
_DIGEST_ADD_SOURCE_KEYS = ("add_urls", "urls", "add", "feeds", "rss_urls")
_DIGEST_DISCOVER_KEYS = ("discover_urls", "discover")
_DIGEST_UNSUB_KEYS = (
    "unsubscribe_ids",
    "remove_ids",
    "remove",
    "unsubscribe",
    "delete",
    "remove_urls",
    "unsubscribe_urls",
)
_DIGEST_SOURCE_OP_KEYS = (
    *_DIGEST_ADD_KEYS,
    *_DIGEST_DISCOVER_KEYS,
    *_DIGEST_UNSUB_KEYS,
)
_NON_CHAT_ENABLE_TOKENS = frozenset(
    {"", "true", "false", "yes", "no", "on", "off", "1", "0", "y", "n", "maybe"}
)
_DIGEST_WORKSPACE_ALIASES = frozenset(
    {"", "digest", "digest-feeds", "digest_feeds", "digest-news", "digest-daily", "digest_feed"}
)
_SLEEP_WINDOW_RE = re.compile(
    r"^(\d{1,2})(?:\s*[:：]\s*(\d{2}))?\s*(?:-|–|—|~|～|至|到)\s*(\d{1,2})(?:\s*[:：]\s*(\d{2}))?$"
)


def normalize_sleep_window(raw: object) -> str:
    """将常见睡眠安静时段写法（如 1:00-8:00 / 23:30-7:00 / off）规范为 HH-HH 或空字符串。"""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("sleep_window 必须是字符串（如 01-08 或 off）")
    text = raw.strip()
    if text.lower() in {"", "off", "none", "disable", "disabled", "关闭", "不限制", "无"}:
        return ""
    match = _SLEEP_WINDOW_RE.fullmatch(text)
    if match is None:
        raise ValueError("sleep_window 格式应为 HH-HH 或 H:MM-H:MM（如 01-08），关闭请传 off")
    start_h = int(match.group(1))
    start_m = int(match.group(2) or 0)
    end_h = int(match.group(3))
    end_m = int(match.group(4) or 0)
    if start_h == 24 and start_m == 0:
        start_h = 0
    if end_h == 24 and end_m == 0:
        end_h = 0
    if not (0 <= start_h <= 23 and 0 <= end_h <= 23 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
        raise ValueError("sleep_window 小时范围应在 0 到 23 之间")
    return f"{start_h:02d}-{end_h:02d}"


def normalize_profile_workspace(profile_id: str, workspace: str) -> str:
    """把 digest 角色的常见工作区别名统一收敛到 digest，防止资讯源状态机分裂。"""
    pid = str(profile_id or "").strip()
    ws = str(workspace or "").strip()
    if pid == "digest" and ws.lower() in _DIGEST_WORKSPACE_ALIASES:
        return "digest"
    return ws
_PROFILE_TOML_KEYS = {
    "title",
    "model",
    "model_name",
    "fallback_models",
    "task",
    "max_tokens",
    "context_tokens",
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


def _unwrap_item_wrappers(value, _seen: set[int] | None = None):
    """递归解包 LLM 工具调用中误套的 `{"item": ...}` 单键包裹，并防循环嵌套。"""
    seen = set() if _seen is None else _seen
    cur = value
    local_ids: list[int] = []
    try:
        while isinstance(cur, dict) and set(cur.keys()) == {"item"}:
            oid = id(cur)
            if oid in seen:
                return cur
            seen.add(oid)
            local_ids.append(oid)
            cur = cur["item"]
        if isinstance(cur, dict):
            oid = id(cur)
            if oid in seen:
                return cur
            seen.add(oid)
            local_ids.append(oid)
            return {k: _unwrap_item_wrappers(v, seen) for k, v in cur.items()}
        if isinstance(cur, list):
            oid = id(cur)
            if oid in seen:
                return cur
            seen.add(oid)
            local_ids.append(oid)
            return [_unwrap_item_wrappers(item, seen) for item in cur]
        if isinstance(cur, tuple):
            oid = id(cur)
            if oid in seen:
                return cur
            seen.add(oid)
            local_ids.append(oid)
            return tuple(_unwrap_item_wrappers(item, seen) for item in cur)
        return cur
    finally:
        for oid in local_ids:
            seen.discard(oid)


def _coerce_bool(val, field_name: str) -> bool:
    """接受布尔值或去空白/忽略大小写的 \"true\"/\"false\"，其余值抛错。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        norm = val.strip().lower()
        if norm == "true":
            return True
        if norm == "false":
            return False
    raise ValueError(f"{field_name} 必须是布尔值")


def _coerce_int(val, field_name: str) -> int:
    """接受整数或十进制整数字符串（如 \"480\"），拒绝布尔值。"""
    if type(val) is bool:
        raise ValueError(f"{field_name} 必须是整数")
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        text = val.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text, 10)
    raise ValueError(f"{field_name} 必须是整数")


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
    known_aliases: set[str] = set()

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
        known_aliases.update(aliases)
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
        if token in known_aliases or norm_tok in known_aliases:
            continue
        if parsed is not None:
            plat, ctype, tid = parsed
            new_aliases = {token, norm_tok}
            known_aliases.update(new_aliases)
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
                    "aliases": new_aliases,
                }
            )
        else:
            known_aliases.add(token)
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


_SETTINGS_INT_FIELDS = frozenset(
    {
        "agent_max_steps",
        "agent_timeout",
        "agent_max_active",
        "thread_idle_seconds",
        "thread_context_tokens",
        "thread_context_chars",
        "daily_proactive_limit",
        "heartbeat_interval_minutes",
        "longing_quiet_hours",
    }
)


def _is_falsy_lockout_value(val: object) -> bool:
    if val is False:
        return True
    if isinstance(val, (int, float)) and val == 0:
        return True
    if isinstance(val, str) and val.strip().lower() in {"false", "0", "no", "off", "n", "f"}:
        return True
    return False


def _check_no_lockout(candidate: dict) -> None:
    plugin_cfg = candidate.get("plugin")
    if isinstance(plugin_cfg, dict) and _is_falsy_lockout_value(plugin_cfg.get("enabled")):
        raise ValueError(_LOCKOUT_MESSAGE)


async def _apply_validated_settings(
    plugin, candidate: dict, *, state: dict, author: str, reason: str
) -> dict:
    _check_no_lockout(candidate)
    validated = Settings.model_validate(candidate)
    if not validated.plugin.enabled:
        raise ValueError(_LOCKOUT_MESSAGE)
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
    dry_run: bool = False,
) -> dict:
    if patch is not None:
        patch = _unwrap_item_wrappers(patch)
    if not isinstance(patch, dict) or not patch:
        raise ValueError("patch 必须为非空对象")
    _assert_no_secret(plugin, patch, "设置修改内容")

    working_patch = copy.deepcopy(patch)
    if "sleep_window" in working_patch:
        working_patch["sleep_window"] = normalize_sleep_window(working_patch["sleep_window"])
    for int_key in _SETTINGS_INT_FIELDS:
        if int_key in working_patch and working_patch[int_key] is not None:
            working_patch[int_key] = _coerce_int(working_patch[int_key], int_key)
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

        if dry_run:
            _check_no_lockout(candidate)
            validated = Settings.model_validate(candidate)
            if not validated.plugin.enabled:
                raise ValueError(_LOCKOUT_MESSAGE)
            return {
                "kind": "settings",
                "version": int(state["current_version"]),
                "settings": validated.model_dump(),
            }

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
    if "sleep_window" in normalized:
        normalized["sleep_window"] = normalize_sleep_window(normalized["sleep_window"])
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
    effective_model = p.model_name or p.model
    return {
        "id": p.id,
        "title": p.title,
        "enabled": p.enabled,
        "model": effective_model,
        "model_name": effective_model,
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
        "context_tokens": p.context_tokens,
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
    dry_run: bool = False,
) -> dict:
    target = str(target or "").strip()
    if not target:
        raise ValueError("修改角色必须指定 target")
    if patch is not None:
        patch = _unwrap_item_wrappers(patch)
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
            elif key in {"enabled", "bound"}:
                cfg[key] = _coerce_bool(value, key)
            elif key in {"max_tokens", "max_steps", "context_tokens"}:
                cfg[key] = _coerce_int(value, key)
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
            raw_tools = cfg.get("tools")
            cur_tools = (
                list(raw_tools)
                if raw_tools is not None
                else sorted(READONLY_TOOLS)
            )
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

        new_md_ver = md_doc["version"]
        md_content = md_doc["content"]
        updated_md: str | None = None
        if new_instructions is not None:
            updated_md = str(new_instructions)
        elif append_instructions and append_instructions not in md_content:
            base = md_content.rstrip()
            updated_md = f"{base}\n\n{append_instructions}\n" if base else f"{append_instructions}\n"

        if dry_run:
            if cfg != orig_cfg or not toml_doc["content"].strip():
                rendered_check = serialize_profile_toml(cfg)
                tomllib.loads(rendered_check)
            if updated_md is not None and updated_md != md_content:
                plugin.memory._guard("profile", updated_md, "", "PROFILE.md", allow_custom=True)
            return {
                "kind": "profile",
                "target": target,
                "version": toml_doc["version"],
                "instructions_version": new_md_ver,
            }

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
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("---\n"):
        match = re.search(r"\n---(?:\n|$)", text[3:])
        if match is not None:
            meta = parse_skill_meta("skill", text)
            body = text[3 + match.end() :]
            return meta, body
    return {}, text


def _compose_skill_markdown(title: str, description: str, body: str) -> str:
    header_lines = ["---"]
    if title:
        header_lines.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    if description:
        header_lines.append(f"description: {json.dumps(description, ensure_ascii=False)}")
    header_lines.append("---")
    clean_body = body.replace("\r\n", "\n").replace("\r", "\n").lstrip("\n")
    return "\n".join(header_lines) + ("\n\n" + clean_body if clean_body else "\n")


def _merge_skill_frontmatter(
    raw: str,
    *,
    target: str,
    new_body: str,
    title: str = "",
    has_title: bool = False,
    description: str = "",
    has_description: bool = False,
) -> str:
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    fm_block: str | None = None
    if text.startswith("---\n"):
        match = re.search(r"\n---(?:\n|$)", text[3:])
        if match is not None:
            fm_block = text[4 : 3 + match.start()]
    clean_body = new_body.replace("\r\n", "\n").replace("\r", "\n").lstrip("\n")
    if fm_block is None:
        meta = parse_skill_meta(target, text)
        final_title = title if has_title else str(meta.get("title") or target)
        final_desc = description if has_description else str(meta.get("description") or "")
        return _compose_skill_markdown(final_title, final_desc, clean_body)
    out_lines: list[str] = []
    seen_title = False
    seen_desc = False
    skip_indented = False
    for line in fm_block.splitlines():
        if skip_indented:
            if line.startswith((" ", "\t")):
                continue
            skip_indented = False
        key_match = re.match(r"^([A-Za-z0-9_-]+)\s*:(.*)$", line)
        if key_match:
            key = key_match.group(1)
            val_rest = key_match.group(2).strip()
            if key == "title" and has_title:
                out_lines.append(f"title: {json.dumps(title, ensure_ascii=False)}")
                seen_title = True
                if not val_rest or val_rest in {"|", ">", "|-", ">-"}:
                    skip_indented = True
                continue
            if key == "description" and has_description:
                out_lines.append(
                    f"description: {json.dumps(description, ensure_ascii=False)}"
                )
                seen_desc = True
                if not val_rest or val_rest in {"|", ">", "|-", ">-"}:
                    skip_indented = True
                continue
        out_lines.append(line)
    if has_title and not seen_title:
        out_lines.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    if has_description and not seen_desc:
        out_lines.append(f"description: {json.dumps(description, ensure_ascii=False)}")
    header = "\n".join(["---", *out_lines, "---"])
    return header + ("\n\n" + clean_body if clean_body else "\n")


def _require_existing_skill(plugin, target: str) -> None:
    if not SKILL_ID_RE.fullmatch(target):
        raise ValueError("技能编号仅允许小写字母、数字和短横线")
    runtime_md = plugin.data_root / "abilities/skills" / target / "SKILL.md"
    if runtime_md.is_file() or target in plugin.memory.preset_skills():
        return
    raise ValueError(f"技能不存在: {target}")


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
        await completed_thread(_require_existing_skill, plugin, target)
        return await completed_thread(
            plugin.memory.read, "skill", target=target, version=version, allow_custom=True
        )
    if action == "history":
        await completed_thread(_require_existing_skill, plugin, target)
        return {
            "kind": "skill",
            "target": target,
            "history": await completed_thread(plugin.memory.history, "skill", target=target),
        }
    if action == "delete":
        await completed_thread(_require_existing_skill, plugin, target)
        if await completed_thread(lambda: target in plugin.memory.preset_skills()):
            raise ValueError("预置技能不能删除")
        referencing_pids: list[str] = []
        if plugin.profiles.path.is_dir():
            for entry in sorted(plugin.profiles.path.iterdir()):
                if not entry.is_dir() or path_is_link_like(entry):
                    continue
                try:
                    prof = await completed_thread(plugin.profiles.get, entry.name)
                except (ValueError, OSError):
                    continue
                if target in prof.skills:
                    referencing_pids.append(prof.id)
        for pid in referencing_pids:
            await patch_profile(plugin, pid, {"remove_skills": [target]}, author=author)
        del_res = await completed_thread(plugin.memory.delete_skill, target)
        return {
            **(del_res if isinstance(del_res, dict) else {"deleted": True}),
            "unbound_profiles": referencing_pids,
        }
    if action == "rollback":
        if version is None or expected_version is None:
            raise ValueError("回滚技能需要提供 version 与 expected_version")
        await completed_thread(_require_existing_skill, plugin, target)
        return await completed_thread(
            plugin.memory.rollback,
            "skill",
            version,
            expected_version=expected_version,
            target=target,
            allow_custom=True,
        )
    if action == "create":
        _assert_no_secret(plugin, content, "技能内容")
        _assert_no_secret(plugin, opts, "技能内容")
        raw_content = content or ""
        if not raw_content.strip() and opts.get("body"):
            raw_content = _compose_skill_markdown(
                str(opts.get("title") or target),
                str(opts.get("description") or ""),
                str(opts.get("body") or ""),
            )
        has_fm = raw_content.lstrip().startswith(("---\n", "---\r\n"))
        res = await completed_thread(
            plugin.memory.create_skill,
            target,
            base=str(opts.get("base") or ""),
            title="" if has_fm else str(opts.get("title") or ""),
            description="" if has_fm else str(opts.get("description") or ""),
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
        _assert_no_secret(plugin, opts, "技能内容")
        if expected_version is None:
            raise ValueError("覆盖写入技能必须提供 expected_version")
        await completed_thread(_require_existing_skill, plugin, target)
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
        _assert_no_secret(plugin, content, "技能更新内容")
        _assert_no_secret(plugin, opts, "技能更新内容")
        has_text_edits = bool(
            content
            or any(
                k in opts
                for k in (
                    "title",
                    "description",
                    "body",
                    "append",
                    "prepend",
                    "replace",
                    "content",
                )
            )
        )
        has_bind_edits = bool(bind_profiles or unbind_profiles)
        if not has_text_edits and not has_bind_edits:
            raise ValueError("更新技能必须提供修改字段、正文或绑定的角色列表")
        await completed_thread(_require_existing_skill, plugin, target)
        if has_text_edits:
            def _patch_skill_sync() -> dict:
                cur = plugin.memory.read("skill", target=target, allow_custom=True)
                if expected_version is not None and cur["version"] != expected_version:
                    raise ValueError("技能版本已变化，请重新读取后再修改")
                if content:
                    new_md = content
                elif "content" in opts:
                    new_md = str(opts["content"])
                else:
                    _meta, body = _split_frontmatter(cur["content"])
                    has_title = "title" in opts
                    has_description = "description" in opts
                    title = str(opts["title"]) if has_title else ""
                    desc = str(opts["description"]) if has_description else ""
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
                    new_md = _merge_skill_frontmatter(
                        cur["content"],
                        target=target,
                        new_body=new_body,
                        title=title,
                        has_title=has_title,
                        description=desc,
                        has_description=has_description,
                    )
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
            def _read_and_check_version() -> dict:
                cur = plugin.memory.read("skill", target=target, allow_custom=True)
                if expected_version is not None and cur["version"] != expected_version:
                    raise ValueError("技能版本已变化，请重新读取后再修改")
                return cur

            res = await completed_thread(_read_and_check_version)
        if bind_profiles or unbind_profiles:
            res["bound_profiles"] = await _bind_skill_profiles(
                plugin, target, bind_profiles, unbind_profiles, author=author
            )
        return res
    raise ValueError(f"技能不支持 action={action}")


async def handle_services_or_webhooks(
    plugin,
    *,
    kind: str,
    action: str,
    content: str = "",
    patch: dict | None = None,
    expected_version: str | int | None = None,
) -> dict:
    if action == "read":
        return await plugin.management.read(kind, "{}")
    if action not in {"patch", "write"}:
        raise ValueError(f"{kind} 仅支持 read / patch / write")
    if action == "write" and (
        expected_version is None or not str(expected_version).strip()
    ):
        raise ValueError(f"覆盖写入 {kind} 必须提供 expected_version")
    if patch is not None:
        if not isinstance(patch, dict):
            raise ValueError(f"修改 {kind} 需要提供 patch 对象或 JSON content")
        payload_data = patch
    elif content.strip():
        try:
            payload_data = json.loads(content)
        except ValueError as exc:
            raise ValueError(f"{kind} 写入内容必须是合法 JSON") from exc
    else:
        payload_data = None
    if not isinstance(payload_data, dict):
        raise ValueError(f"修改 {kind} 需要提供 patch 对象或 JSON content")
    _assert_no_secret(plugin, payload_data, kind)
    cur = await plugin.management.read(kind, "{}")
    if expected_version is not None and str(expected_version) != str(cur["version"]):
        raise ValueError(f"{kind} 版本已变化，请刷新后再修改")
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
            {
                "configuration": merged,
                "expected_version": (
                    expected_version
                    if expected_version is not None
                    else cur["version"]
                ),
            },
            ensure_ascii=False,
        ),
    )


def _normalize_recipe_import_mode(raw_mode: object) -> str:
    cleaned = str(raw_mode or "").strip()
    if not cleaned or cleaned == "create":
        return "copy"
    return cleaned


def _resolve_recipe_target_hint(source: dict, mode: str) -> str:
    if mode == "copy":
        raw = (
            source.get("suggested_copy_id")
            or source.get("suggested_target")
            or source.get("suggested_id")
        )
    else:
        raw = (
            source.get("suggested_target")
            or source.get("suggested_copy_id")
            or source.get("suggested_id")
        )
    return str(raw or "").strip()


def _build_recipe_import_payload(
    preview_token: str,
    *,
    target: str,
    mode: str,
    opts: dict,
) -> dict:
    payload: dict = {
        "preview_token": preview_token,
        "target": target,
        "mode": mode,
    }
    for key in (
        "install_skills",
        "overwrite_skills",
        "mount_routines",
        "routine_chat_id",
        "routine_platform",
    ):
        if key in opts and opts[key] is not None:
            payload[key] = copy.deepcopy(opts[key])
    return payload


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
    mode = _normalize_recipe_import_mode(opts.get("mode"))
    tok = str(preview_token or opts.get("preview_token") or "").strip()
    if tok:
        tok_target = (
            str(target or opts.get("target") or "").strip()
            or _resolve_recipe_target_hint(opts, mode)
        )
        payload = _build_recipe_import_payload(
            tok,
            target=tok_target,
            mode=mode,
            opts=opts,
        )
        return await plugin.management.write(
            "recipe_import",
            json.dumps(payload, ensure_ascii=False),
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
        if ptok:
            resolved_target = (
                str(target or opts.get("target") or "").strip()
                or _resolve_recipe_target_hint(preview, mode)
            )
            payload = _build_recipe_import_payload(
                ptok,
                target=resolved_target,
                mode=mode,
                opts=opts,
            )
            imported = await plugin.management.write(
                "recipe_import",
                json.dumps(payload, ensure_ascii=False),
            )
            return {**preview, "imported": imported}
    return preview


def _find_digest_workspace_binding(
    plugin,
    effective_chat_id: str,
    effective_platform: str,
    ws_name: str,
) -> dict | None:
    alias_candidates = {effective_chat_id}
    if hasattr(plugin, "chat_aliases_for"):
        alias_candidates.update(
            plugin.chat_aliases_for(effective_chat_id, effective_platform)
        )
    with plugin.threads.connect() as db:
        placeholders = ",".join("?" for _ in alias_candidates)
        row = db.execute(
            f"SELECT w.*, t.state FROM agent_workspaces w LEFT JOIN agent_threads t ON t.thread_id=w.thread_id "
            f"WHERE w.owner='chat' AND w.name=? AND w.chat_id IN ({placeholders}) "
            "ORDER BY CASE WHEN w.chat_id=? THEN 0 ELSE 1 END LIMIT 1",
            [ws_name, *sorted(alias_candidates), effective_chat_id],
        ).fetchone()
        return dict(row) if row else None


async def _run_in_digest_workspace(
    plugin,
    effective_chat_id: str,
    effective_platform: str,
    ws_name: str,
    binding: dict | None,
    worker,
):
    created_helper = False
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

    activity_cm = plugin.runtime.files.activity(target_row)
    work = await completed_thread(activity_cm.__enter__)
    try:
        dw = DigestWorkspace(work)
        result = await worker(dw)
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
    await completed_thread(activity_cm.__exit__, None, None, None)
    if created_helper:
        try:
            await completed_thread(
                plugin.threads.stop, target_row["thread_id"], target_row["chat_id"]
            )
        except (ValueError, PermissionError, sqlite3.Error):
            pass
    return result


def _collect_digest_source_items(
    opts: dict, keys: tuple[str, ...], *, allow_url_dict: bool = False
) -> list:
    """从多个兼容字段别名中提取资讯源条目列表，支持单条字符串与列表。"""
    collected: list = []
    for key in keys:
        if key not in opts or opts[key] is None:
            continue
        raw = opts[key]
        if isinstance(raw, str):
            text = raw.strip()
            if text:
                collected.append(text)
        elif allow_url_dict and isinstance(raw, dict) and "url" in raw:
            collected.append(raw)
        elif isinstance(raw, (list, tuple)):
            collected.extend(raw)
        else:
            raise ValueError(f"{key} 必须是字符串或列表")
    return collected


def _normalize_feed_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        parsed = urlparse(text)
    elif "/" in text or "?" in text:
        parsed = urlparse(f"https://{text}")
    else:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or ""
    path = "" if path == "/" else path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{port}{path}{query}"


def _bare_feed_host(raw: str) -> str:
    text = str(raw or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return ""
    if "://" in text:
        parsed = urlparse(text)
        if parsed.path.strip("/") or parsed.query:
            return ""
        host = (parsed.hostname or "").lower()
    elif "/" in text or "?" in text:
        cleaned = text.rstrip("/")
        if "/" in cleaned or "?" in cleaned:
            return ""
        host = (urlparse(f"https://{cleaned}").hostname or "").lower()
    else:
        host = (urlparse(f"https://{text}").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else ""


def _feed_url_host(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _resolve_unsub_feed_ids(feeds: list[dict], token: str) -> list[str]:
    """按精确 ID -> 规范化 URL -> 不区分大小写标题 -> 裸域名 host 解析退订目标。"""
    clean = str(token or "").strip()
    if not clean:
        return []
    exact_ids = [
        str(f.get("id") or "").strip()
        for f in feeds
        if str(f.get("id") or "").strip() == clean
    ]
    if exact_ids:
        return list(dict.fromkeys(exact_ids))
    norm_tok = _normalize_feed_url(clean)
    if norm_tok:
        url_matches = [
            str(f.get("id") or "").strip()
            for f in feeds
            if str(f.get("id") or "").strip()
            and _normalize_feed_url(str(f.get("url") or "")) == norm_tok
        ]
        if url_matches:
            return list(dict.fromkeys(url_matches))
    low_tok = clean.lower()
    title_matches = [
        str(f.get("id") or "").strip()
        for f in feeds
        if str(f.get("id") or "").strip()
        and str(f.get("title") or "").strip().lower() == low_tok
    ]
    if title_matches:
        return list(dict.fromkeys(title_matches))
    host_tok = _bare_feed_host(clean)
    if host_tok:
        host_matches = [
            str(f.get("id") or "").strip()
            for f in feeds
            if str(f.get("id") or "").strip()
            and _feed_url_host(str(f.get("url") or "")) == host_tok
        ]
        if host_matches:
            return list(dict.fromkeys(host_matches))
    return [clean]


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
    unwrapped_patch = _unwrap_item_wrappers(patch) if patch is not None else None
    opts = dict(unwrapped_patch or {}) if isinstance(unwrapped_patch, dict) else {}
    raw_ws = str(target or opts.get("workspace") or "digest").strip() or "digest"
    ws_name = normalize_profile_workspace("digest", raw_ws)
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

    binding = await completed_thread(
        _find_digest_workspace_binding,
        plugin,
        effective_chat_id,
        effective_platform,
        ws_name,
    )
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

    add_raw = _collect_digest_source_items(
        opts, _DIGEST_ADD_SOURCE_KEYS, allow_url_dict=True
    )
    discover_raw = _collect_digest_source_items(opts, _DIGEST_DISCOVER_KEYS)
    unsub_raw = _collect_digest_source_items(opts, _DIGEST_UNSUB_KEYS)

    raw_auto = opts.get("auto")
    auto_default = _coerce_bool(raw_auto, "auto") if raw_auto is not None else False

    async def _mutate(dw: DigestWorkspace) -> dict:
        add_sem = asyncio.Semaphore(6)
        discover_sem = asyncio.Semaphore(4)
        add_tasks = []
        for item in add_raw:
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                title = str(item.get("title") or "").strip()
                item_auto = item.get("auto")
                auto_flag = (
                    _coerce_bool(item_auto, "auto")
                    if item_auto is not None
                    else auto_default
                )
            else:
                url = str(item or "").strip()
                title = ""
                auto_flag = auto_default
            if url:
                add_tasks.append((url, title, auto_flag))

        async def _add_one(url: str, title: str, auto_flag: bool) -> dict:
            async with add_sem:
                try:
                    return await asyncio.wait_for(
                        dw.add_feed(url, title=title, auto=auto_flag, origin="admin"),
                        timeout=6.0,
                    )
                except TimeoutError:
                    return {"ok": False, "url": url, "reason": f"RSS 源 {url} 订阅超时（>6s）"}
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    return {"ok": False, "url": url, "reason": str(exc)}

        async def _discover_one(url: str) -> dict:
            async with discover_sem:
                try:
                    return await asyncio.wait_for(
                        dw.discover(url, origin="admin"), timeout=6.0
                    )
                except TimeoutError:
                    return {"ok": False, "url": url, "reason": f"主页嗅探 {url} 超时（>6s）"}
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    return {"ok": False, "url": url, "reason": str(exc)}

        added: list[dict] = (
            list(await asyncio.gather(*(_add_one(u, t, a) for u, t, a in add_tasks)))
            if add_tasks
            else []
        )
        discover_urls = [str(u or "").strip() for u in discover_raw if str(u or "").strip()]
        discovered: list[dict] = (
            list(await asyncio.gather(*(_discover_one(u) for u in discover_urls)))
            if discover_urls
            else []
        )
        unsubscribed: list[dict] = []
        if unsub_raw:
            feeds_snapshot = list(dw._load_sources().get("feeds", []) or ())
            seen_fids: set[str] = set()
            for raw_id in unsub_raw:
                token = str(raw_id or "").strip()
                if not token:
                    continue
                for fid in _resolve_unsub_feed_ids(feeds_snapshot, token):
                    if fid in seen_fids:
                        continue
                    seen_fids.add(fid)
                    unsubscribed.append(dw.unsubscribe(fid, reason="管理员退订"))
        cur_sources = dw._load_sources()
        cur_history = dw._load_history()
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

    return await _run_in_digest_workspace(
        plugin, effective_chat_id, effective_platform, ws_name, binding, _mutate
    )


async def build_overview(
    plugin, *, current_chat_id: str = "", current_platform: str = "", target_chat: str = ""
) -> dict:
    settings_state = await read_settings_state(plugin)
    known_chats = await collect_known_chats(plugin)
    assets = await completed_thread(plugin.management._assets)
    host_models = await plugin.management._collect_host_models(force=False, probe_host=True)
    profiles = await completed_thread(
        lambda: [profile_summary(plugin, item["id"]) for item in assets.get("profiles") or ()]
    )
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


_SETUP_CHAT_FIELDS = frozenset(
    {
        "preferences",
        "memory",
        "append_memory",
        "replace_memory",
        "daily_at",
        "schedules",
        *_DIGEST_SOURCE_OP_KEYS,
        "topics",
        "auto_seed_sources",
        "run_now",
        "chat_proactive_level",
    }
)


def _coerce_setup_opts(plugin, patch: dict | None, content: str) -> dict:
    if isinstance(patch, dict):
        opts = copy.deepcopy(patch)
    elif isinstance(content, str) and content.strip():
        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise ValueError(f"setup content 不是合法 JSON：{exc}") from None
        if not isinstance(parsed, dict):
            raise ValueError("setup 参数必须是 JSON 对象")
        opts = copy.deepcopy(parsed)
    else:
        opts = {}
    opts = _unwrap_item_wrappers(opts)
    if not isinstance(opts, dict):
        raise ValueError("setup 参数必须是 JSON 对象")
    if _is_falsy_lockout_value(opts.get("enabled")) or (
        isinstance(opts.get("plugin"), dict)
        and _is_falsy_lockout_value(opts["plugin"].get("enabled"))
    ):
        raise ValueError(_LOCKOUT_MESSAGE)
    _assert_no_secret(plugin, opts, "一键配置")
    return opts


async def _resolve_setup_target(
    plugin, raw_target_chat: str, current_chat_id: str, current_platform: str
) -> tuple[str, str, str]:
    raw_tc = str(raw_target_chat or "").strip()
    cur_plat = str(current_platform or "").strip().lower() or "qq"
    if raw_tc:
        effective_chat_id, effective_platform = await resolve_chat_target(
            plugin,
            raw_tc,
            current_chat_id=current_chat_id,
            current_platform=cur_plat,
            allow_unknown_ascii=True,
        )
    else:
        if not current_chat_id:
            raise ValueError("请指定目标会话 target_chat")
        effective_chat_id, effective_platform = current_chat_id, cur_plat

    parsed = parse_stream_spec(raw_tc) if raw_tc else None
    canon = (
        plugin.canonical_stream_spec(effective_chat_id, effective_platform)
        if hasattr(plugin, "canonical_stream_spec")
        else ""
    )
    if canon:
        stream_spec = canon
    elif parsed is not None:
        stream_spec = format_stream_spec(*parsed)
    else:
        stream_spec = await resolve_stream_spec(
            plugin,
            raw_tc or effective_chat_id,
            current_chat_id=current_chat_id,
            current_platform=effective_platform,
            allow_unknown_ascii=True,
        )
    return effective_chat_id, effective_platform, stream_spec


async def _resolve_chat_spec_list(
    plugin,
    raw: object,
    *,
    field_name: str,
    effective_chat_id: str,
    effective_platform: str,
) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list = [raw.strip()] if raw.strip() else []
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise ValueError(f"{field_name} 必须是字符串或列表")
    resolved: list[str] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise ValueError(f"{field_name} 列表元素必须是会话标识")
        token = str(item).strip()
        if not token:
            continue
        spec = await resolve_stream_spec(
            plugin,
            token,
            current_chat_id=effective_chat_id,
            current_platform=effective_platform,
            allow_unknown_ascii=True,
        )
        resolved.append(spec)
    return list(dict.fromkeys(resolved))


async def _parse_setup_enable_chat(
    plugin,
    raw_enable_chat: object,
    *,
    effective_chat_id: str,
    effective_platform: str,
    stream_spec: str,
) -> tuple[bool | None, list[str]]:
    if raw_enable_chat is None:
        return None, []
    if isinstance(raw_enable_chat, (list, tuple)):
        specs = await _resolve_chat_spec_list(
            plugin,
            raw_enable_chat,
            field_name="enable_chat",
            effective_chat_id=effective_chat_id,
            effective_platform=effective_platform,
        )
        if not specs:
            return False, []
        return stream_spec in specs, specs
    if isinstance(raw_enable_chat, str):
        cleaned = raw_enable_chat.strip()
        low = cleaned.lower()
        if low in _NON_CHAT_ENABLE_TOKENS:
            return _coerce_bool(raw_enable_chat, "enable_chat"), []
        if parse_stream_spec(cleaned) is not None or ":" in cleaned or cleaned.isdigit():
            spec = await resolve_stream_spec(
                plugin,
                cleaned,
                current_chat_id=effective_chat_id,
                current_platform=effective_platform,
                allow_unknown_ascii=True,
            )
            return stream_spec == spec, [spec]
        try:
            spec = await resolve_stream_spec(
                plugin,
                cleaned,
                current_chat_id=effective_chat_id,
                current_platform=effective_platform,
                allow_unknown_ascii=False,
            )
            return stream_spec == spec, [spec]
        except ValueError:
            return _coerce_bool(raw_enable_chat, "enable_chat"), []
    return _coerce_bool(raw_enable_chat, "enable_chat"), []


async def _apply_setup_settings(
    plugin,
    opts: dict,
    *,
    has_explicit_target: bool,
    effective_chat_id: str,
    effective_platform: str,
    stream_spec: str,
    author: str,
    dry_run: bool = False,
) -> dict | None:
    enable_chat, enable_chat_specs = await _parse_setup_enable_chat(
        plugin,
        opts.get("enable_chat"),
        effective_chat_id=effective_chat_id,
        effective_platform=effective_platform,
        stream_spec=stream_spec,
    )
    extra_add_chats: list[str] = []
    for key in ("add_chats", "enable_chats"):
        if key in opts and opts[key] is not None:
            extra_add_chats.extend(
                await _resolve_chat_spec_list(
                    plugin,
                    opts[key],
                    field_name=key,
                    effective_chat_id=effective_chat_id,
                    effective_platform=effective_platform,
                )
            )
    should_enable = (enable_chat is True) or (
        enable_chat is None
        and (has_explicit_target or any(key in opts for key in _SETUP_CHAT_FIELDS))
    )
    settings_patch: dict = {}
    merged_add_chats = list(
        dict.fromkeys(
            ([stream_spec] if should_enable else [])
            + enable_chat_specs
            + extra_add_chats
        )
    )
    if merged_add_chats:
        settings_patch["add_chats"] = merged_add_chats
    if "remove_chats" in opts:
        settings_patch["remove_chats"] = opts["remove_chats"]
    if "sleep_window" in opts:
        settings_patch["sleep_window"] = normalize_sleep_window(opts["sleep_window"])
    if "proactive_level" in opts and opts["proactive_level"] is not None:
        settings_patch["proactive_level"] = str(opts["proactive_level"]).strip()
    if "daily_proactive_limit" in opts and opts["daily_proactive_limit"] is not None:
        settings_patch["daily_proactive_limit"] = _coerce_int(
            opts["daily_proactive_limit"], "daily_proactive_limit"
        )
    if isinstance(opts.get("chat_proactive_levels"), dict):
        settings_patch["chat_proactive_levels"] = dict(opts["chat_proactive_levels"])
    if "chat_proactive_level" in opts:
        levels = dict(settings_patch.get("chat_proactive_levels") or {})
        levels[stream_spec] = opts["chat_proactive_level"]
        settings_patch["chat_proactive_levels"] = levels
    if not settings_patch:
        return None
    res = await patch_settings(
        plugin,
        settings_patch,
        current_chat_id=effective_chat_id,
        current_platform=effective_platform,
        author=author,
        reason="一段话完成配置",
        dry_run=dry_run,
    )
    applied_settings = res.get("settings") or {}
    return {
        "enabled_chat": (
            stream_spec
            if should_enable
            else (enable_chat_specs[0] if enable_chat_specs else "")
        ),
        "enabled_chats": list(applied_settings.get("enabled_chats") or ()),
        "sleep_window": applied_settings.get("sleep_window", ""),
        "proactive_level": applied_settings.get("proactive_level", ""),
        "chat_proactive_level": (applied_settings.get("chat_proactive_levels") or {}).get(
            stream_spec, ""
        ),
        "version": res.get("version"),
    }


def _validate_setup_schedules_shape(opts: dict) -> None:
    raw_schedules = opts.get("schedules")
    if raw_schedules is not None:
        if not isinstance(raw_schedules, (list, tuple)):
            raise ValueError("schedules 必须是计划列表")
        for item in raw_schedules:
            if not isinstance(item, dict):
                raise ValueError("schedules 中的每一项都必须是对象")


def _digest_is_involved(opts: dict, profile_patches: dict[str, dict]) -> bool:
    if "digest" in profile_patches:
        return profile_patches["digest"].get("enabled") is not False
    raw_auto_seed = opts.get("auto_seed_sources")
    auto_seed_flag = (
        _coerce_bool(raw_auto_seed, "auto_seed_sources")
        if raw_auto_seed is not None
        else False
    )
    if any(
        opts.get(key) for key in (*_DIGEST_SOURCE_OP_KEYS, "topics")
    ) or auto_seed_flag:
        return True
    _validate_setup_schedules_shape(opts)
    for item in opts.get("schedules") or ():
        if isinstance(item, dict) and str(item.get("profile") or "").strip() == "digest":
            return True
    single_prof = str(opts.get("profile") or opts.get("trial_profile") or "").strip()
    if opts.get("daily_at") and single_prof in {"", "digest"}:
        return True
    raw_run_now = opts.get("run_now")
    run_now_flag = (
        _coerce_bool(raw_run_now, "run_now")
        if raw_run_now is not None
        else False
    )
    if run_now_flag and single_prof in {"", "digest"}:
        return True
    return False


async def _prepare_setup_profile_patches(plugin, opts: dict) -> dict[str, dict]:
    profile_patches: dict[str, dict] = {}
    raw_enable = opts.get("enable_profiles")
    if isinstance(raw_enable, str) and raw_enable.strip():
        raw_enable = [raw_enable.strip()]
    if raw_enable is not None:
        if not isinstance(raw_enable, (list, tuple)):
            raise ValueError("enable_profiles 必须是角色名称列表")
        for item in raw_enable:
            pid = str(item or "").strip()
            if pid:
                profile_patches.setdefault(pid, {})["enabled"] = True

    raw_profiles = opts.get("profiles")
    if raw_profiles is not None:
        if not isinstance(raw_profiles, dict):
            raise ValueError("profiles 必须是 {profile_id: {...}} 对象")
        for raw_pid, cfg in raw_profiles.items():
            pid = str(raw_pid or "").strip()
            if not pid:
                continue
            if isinstance(cfg, bool):
                profile_patches.setdefault(pid, {})["enabled"] = cfg
            elif isinstance(cfg, str) and cfg.strip().lower() in {"true", "false"}:
                profile_patches.setdefault(pid, {})["enabled"] = _coerce_bool(
                    cfg, f"profiles[{pid}]"
                )
            elif isinstance(cfg, dict):
                target_dict = profile_patches.setdefault(pid, {})
                target_dict.update(copy.deepcopy(cfg))
                if "enabled" in target_dict:
                    if target_dict["enabled"] is not None:
                        target_dict["enabled"] = _coerce_bool(
                            target_dict["enabled"], f"profiles[{pid}].enabled"
                        )
                else:
                    target_dict["enabled"] = True
            else:
                raise ValueError(f"profiles[{pid}] 必须是配置对象或布尔值")

    single_pid = str(opts.get("profile") or "").strip()
    single_overrides = {
        key: copy.deepcopy(opts[key])
        for key in (
            "model_list",
            "model_name",
            "model",
            "fallback_models",
            "network",
            "max_steps",
            "context_tokens",
            "web_provider",
            "allow_chats",
            "add_allow_chats",
        )
        if key in opts
    }
    if single_pid:
        target_dict = profile_patches.setdefault(single_pid, {})
        target_dict.update(single_overrides)
        target_dict.setdefault("enabled", True)
    elif single_overrides:
        target_pid = (
            next(iter(profile_patches))
            if len(profile_patches) == 1
            else ("digest" if _digest_is_involved(opts, profile_patches) else "")
        )
        if not target_pid:
            raise ValueError("传入 model_list / model_name 时请指定 profile 或 enable_profiles")
        profile_patches.setdefault(target_pid, {}).update(single_overrides)

    if _digest_is_involved(opts, profile_patches):
        digest_patch = profile_patches.setdefault("digest", {})
        if digest_patch.get("enabled") is not False:
            digest_patch.setdefault("enabled", True)
            existing = await completed_thread(plugin.profiles.get, "digest")
            if "network" not in digest_patch and existing.network == "none":
                digest_patch["network"] = "public"
            if "max_steps" not in digest_patch and (
                existing.max_steps is None or existing.max_steps < 20
            ):
                digest_patch["max_steps"] = 20

    return profile_patches


async def _apply_setup_profiles(
    plugin,
    opts: dict,
    *,
    effective_chat_id: str,
    effective_platform: str,
    author: str,
    dry_run: bool = False,
    rollback_snaps: list | None = None,
    prepared_patches: dict[str, dict] | None = None,
) -> dict[str, dict]:
    profile_patches = (
        prepared_patches
        if prepared_patches is not None
        else await _prepare_setup_profile_patches(plugin, opts)
    )
    applied: dict[str, dict] = {}
    for pid, p_patch in profile_patches.items():
        if not p_patch:
            continue
        if dry_run:
            await patch_profile(
                plugin,
                pid,
                p_patch,
                current_chat_id=effective_chat_id,
                current_platform=effective_platform,
                author=author,
                dry_run=True,
            )
            applied[pid] = dict(p_patch)
            continue
        if rollback_snaps is not None:
            toml_snap = await completed_thread(
                lambda target_id=pid: plugin.memory.read(
                    "profile", target=target_id, document="profile.toml", allow_custom=True
                )
            )
            md_snap = await completed_thread(
                lambda target_id=pid: plugin.memory.read(
                    "profile", target=target_id, document="PROFILE.md", allow_custom=True
                )
            )
            rollback_snaps.append((pid, toml_snap, md_snap))
        res = await patch_profile(
            plugin,
            pid,
            p_patch,
            current_chat_id=effective_chat_id,
            current_platform=effective_platform,
            author=author,
        )
        applied[pid] = res.get("profile") or profile_summary(plugin, pid)
    return applied


def _format_memory_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            lines.append(text if text.startswith(("-", "*", "•")) else f"- {text}")
        return "\n".join(lines)
    raise ValueError("偏好或记忆内容必须是字符串或条目列表")


async def _apply_setup_memory(
    plugin,
    opts: dict,
    *,
    effective_chat_id: str,
    author: str,
    dry_run: bool = False,
) -> dict | None:
    replace_raw = opts.get("replace_memory")
    replace_flag = False
    parts: list[str] = []
    if replace_raw is not None:
        if isinstance(replace_raw, str):
            try:
                replace_flag = _coerce_bool(replace_raw, "replace_memory")
            except ValueError:
                replace_flag = True
                if replace_raw.strip():
                    parts.append(replace_raw.strip())
        else:
            replace_flag = _coerce_bool(replace_raw, "replace_memory")
    for key in ("preferences", "memory", "append_memory"):
        if key in opts and opts[key] is not None:
            formatted = _format_memory_text(opts[key])
            if formatted and formatted not in parts:
                parts.append(formatted)
    joined = "\n".join(parts).strip()
    if not replace_flag and not joined:
        return None
    mem_patch = {"content": joined} if replace_flag else {"append": joined}
    _assert_no_secret(plugin, mem_patch, "文本写入内容")
    if dry_run:
        await completed_thread(
            lambda: plugin.memory._guard("memory", joined, effective_chat_id, "", allow_custom=True)
        )
        return {"chat_id": effective_chat_id, "mode": "replace" if replace_flag else "append"}
    res = await patch_memory_doc(
        plugin,
        kind="memory",
        chat_id=effective_chat_id,
        patch=mem_patch,
        author=author,
    )
    return {
        "chat_id": res.get("target") or effective_chat_id,
        "version": res.get("version"),
        "chars": len(res.get("content") or ""),
        "mode": "replace" if replace_flag else "append",
    }


def _clean_setup_items(raw: object, label: str) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    raise ValueError(f"{label} 必须是字符串或列表")


async def _apply_setup_digest_sources(
    plugin,
    opts: dict,
    *,
    effective_chat_id: str,
    effective_platform: str,
    digest_involved: bool,
    warnings: list[str],
    dry_run: bool = False,
    sources_snapshot: dict | None = None,
) -> dict | None:
    rss_raw = _collect_digest_source_items(opts, _DIGEST_ADD_KEYS)
    discover_raw = _collect_digest_source_items(opts, _DIGEST_DISCOVER_KEYS)
    unsub_raw = _collect_digest_source_items(opts, _DIGEST_UNSUB_KEYS)
    topics_raw = [
        str(t).strip()
        for t in _clean_setup_items(opts.get("topics"), "topics")
        if str(t).strip()
    ]
    raw_auto_seed = opts.get("auto_seed_sources")
    auto_seed = (
        _coerce_bool(raw_auto_seed, "auto_seed_sources")
        if raw_auto_seed is not None
        else None
    )
    raw_broad = opts.get("broad")
    broad_flag = _coerce_bool(raw_broad, "broad") if raw_broad is not None else None

    for item in rss_raw:
        if isinstance(item, dict) and item.get("auto") is not None:
            _coerce_bool(item.get("auto"), "rss_urls[].auto")

    ws_name = normalize_profile_workspace("digest", str(opts.get("workspace") or "digest"))
    has_explicit_ops = bool(rss_raw or discover_raw or unsub_raw)
    should_seed_check = digest_involved and auto_seed is not False
    if not has_explicit_ops and not (
        should_seed_check and (topics_raw or auto_seed is True or digest_involved)
    ):
        return None
    if dry_run:
        await completed_thread(plugin.profiles.get, "digest")
        return {"workspace": ws_name}

    binding = await completed_thread(
        _find_digest_workspace_binding,
        plugin,
        effective_chat_id,
        effective_platform,
        ws_name,
    )

    async def _configure_sources(dw: DigestWorkspace) -> dict:
        if sources_snapshot is not None:
            sources_snapshot["workspace"] = ws_name
            sources_snapshot["orig_sources"] = copy.deepcopy(dw._load_sources())
        added_count = 0
        discovered_count = 0
        unsubscribed_count = 0
        seeded_count = 0

        add_sem = asyncio.Semaphore(6)
        discover_sem = asyncio.Semaphore(4)
        budget_deadline = time.monotonic() + 7.0

        async def _add_one(item: object) -> tuple[int, str | None]:
            if isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                title = str(item.get("title") or "").strip()
                raw_item_auto = item.get("auto")
                auto_flag = (
                    _coerce_bool(raw_item_auto, "rss_urls[].auto")
                    if raw_item_auto is not None
                    else False
                )
            else:
                url = str(item or "").strip()
                title = ""
                auto_flag = False
            if not url:
                return 0, None
            async with add_sem:
                rem = budget_deadline - time.monotonic()
                if rem <= 0.05:
                    return 0, f"RSS 源 {url} 订阅超出 8s 总预算，已跳过"
                try:
                    res = await asyncio.wait_for(
                        dw.add_feed(url, title=title, auto=auto_flag, origin="admin"),
                        timeout=min(5.0, rem),
                    )
                    if res.get("ok"):
                        return 1, None
                    return (
                        0,
                        f"RSS 源 {url} 未订阅成功：{res.get('reason') or '未知原因'}",
                    )
                except TimeoutError:
                    return 0, f"RSS 源 {url} 订阅超时，已跳过"
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    return 0, f"RSS 源 {url} 订阅异常：{exc}"

        async def _discover_one(item: object) -> tuple[int, str | None]:
            url = str(item or "").strip()
            if not url:
                return 0, None
            async with discover_sem:
                rem = budget_deadline - time.monotonic()
                if rem <= 0.05:
                    return 0, f"主页嗅探 {url} 超出 8s 总预算，已跳过"
                try:
                    res = await asyncio.wait_for(
                        dw.discover(url, origin="admin"),
                        timeout=min(5.0, rem),
                    )
                    if res.get("ok"):
                        return 1, None
                    return (
                        0,
                        f"主页嗅探 {url} 未发现可用订阅：{res.get('reason') or '未知原因'}",
                    )
                except TimeoutError:
                    return 0, f"主页嗅探 {url} 超时，已跳过"
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    return 0, f"主页嗅探 {url} 异常：{exc}"

        if rss_raw:
            for count_delta, warn in await asyncio.gather(
                *(_add_one(item) for item in rss_raw)
            ):
                added_count += count_delta
                if warn:
                    warnings.append(warn)

        if discover_raw:
            for count_delta, warn in await asyncio.gather(
                *(_discover_one(item) for item in discover_raw)
            ):
                discovered_count += count_delta
                if warn:
                    warnings.append(warn)

        if unsub_raw:
            feeds_snap = list(dw._load_sources().get("feeds", []) or ())
            seen_fids: set[str] = set()
            for item in unsub_raw:
                token = str(item or "").strip()
                if not token:
                    continue
                for fid in _resolve_unsub_feed_ids(feeds_snap, token):
                    if fid in seen_fids:
                        continue
                    seen_fids.add(fid)
                    try:
                        res = dw.unsubscribe(fid, reason="管理员退订")
                        if res.get("ok"):
                            unsubscribed_count += 1
                        else:
                            warnings.append(
                                f"退订 {token} 未生效：{res.get('reason') or '未找到'}"
                            )
                    except (OSError, ValueError, RuntimeError, TypeError) as exc:
                        warnings.append(f"退订 {token} 异常：{exc}")

        cur_sources = dw._load_sources()
        cur_feeds = list(cur_sources.get("feeds") or ())
        seed_fn = getattr(dw, "seed_starter_feeds", None)
        if (
            should_seed_check
            and callable(seed_fn)
            and (topics_raw or auto_seed is True or len(cur_feeds) == 0)
        ):
            rem_seed = budget_deadline - time.monotonic()
            if rem_seed <= 0.1:
                warnings.append("初期试订源冷启动超出 8s 总预算，已跳过")
            else:
                try:
                    sig = inspect.signature(seed_fn)
                    seed_kwargs: dict = {}
                    if "topics" in sig.parameters:
                        seed_kwargs["topics"] = topics_raw
                    elif "topics_or_text" in sig.parameters:
                        seed_kwargs["topics_or_text"] = " ".join(topics_raw)
                    if seed_kwargs:
                        seed_res = seed_fn(**seed_kwargs)
                    elif topics_raw and any(
                        p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                        for p in sig.parameters.values()
                    ):
                        first_param = next(
                            p
                            for p in sig.parameters.values()
                            if p.kind
                            in (
                                inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            )
                        )
                        arg_val = (
                            " ".join(topics_raw)
                            if "text" in first_param.name or first_param.annotation is str
                            else topics_raw
                        )
                        seed_res = seed_fn(arg_val)
                    else:
                        seed_res = seed_fn()
                    if inspect.isawaitable(seed_res):
                        seed_res = await asyncio.wait_for(
                            seed_res, timeout=min(6.0, rem_seed)
                        )
                    if isinstance(seed_res, dict):
                        seeded_items = (
                            seed_res.get("added")
                            if seed_res.get("added") is not None
                            else seed_res.get("seeded")
                        )
                        if isinstance(seeded_items, (list, tuple, set)):
                            seeded_count = len(seeded_items)
                        elif seeded_items is not None:
                            seeded_count = int(seeded_items or 0)
                        else:
                            seeded_count = int(seed_res.get("seeded_count") or 0)
                        for key in ("warnings", "errors"):
                            for w in seed_res.get(key) or ():
                                if str(w).strip():
                                    warnings.append(str(w).strip())
                        if seed_res.get("ok") is False and seed_res.get("reason"):
                            warnings.append(f"冷启动试订提示：{seed_res['reason']}")
                    cur_sources = dw._load_sources()
                except TimeoutError:
                    warnings.append("初期试订源冷启动超时（>6s），已跳过")
                except (OSError, ValueError, RuntimeError, TypeError) as exc:
                    warnings.append(f"初期试订源冷启动异常：{exc}")

        feeds_after = list(cur_sources.get("feeds") or ())
        return {
            "workspace": ws_name,
            "added_count": added_count,
            "discovered_count": discovered_count,
            "unsubscribed_count": unsubscribed_count,
            "seeded_count": seeded_count,
            "feeds_total": len(feeds_after),
            "feed_titles": [
                str(f.get("title") or f.get("url") or "") for f in feeds_after[:10]
            ],
        }

    return await _run_in_digest_workspace(
        plugin, effective_chat_id, effective_platform, ws_name, binding, _configure_sources
    )


async def _apply_setup_schedules(
    plugin,
    opts: dict,
    *,
    effective_chat_id: str,
    effective_platform: str,
    default_profile: str,
    dry_run: bool = False,
    planned_profiles: dict[str, dict] | None = None,
    schedule_tracker: dict | None = None,
) -> tuple[list[dict], list[str]]:
    raw_schedules = opts.get("schedules")
    raw_daily = opts.get("daily_at")
    if raw_schedules is None and raw_daily is None:
        return [], []
    if plugin.intents is None:
        raise PermissionError("Maisya 调度组件尚未就绪")

    raw_tz = opts.get("tz_offset_minutes")
    default_tz = _coerce_int(raw_tz, "tz_offset_minutes") if raw_tz is not None else 480
    raw_replace_sched = opts.get("replace_schedules")
    replace_sched = (
        _coerce_bool(raw_replace_sched, "replace_schedules")
        if raw_replace_sched is not None
        else True
    )

    requests: list[dict] = []
    if raw_daily is not None:
        if not isinstance(raw_daily, (str, list, tuple)):
            raise ValueError("daily_at 必须是 HH:MM 字符串或列表")
        slots = [raw_daily] if isinstance(raw_daily, str) else list(raw_daily)
        requests.append(
            {
                "daily_at": slots,
                "profile": str(opts.get("schedule_profile") or opts.get("profile") or default_profile).strip(),
                "workspace": str(opts.get("workspace") or "").strip(),
                "prompt": str(opts.get("schedule_prompt") or opts.get("prompt") or "").strip(),
                "tz_offset_minutes": default_tz,
                "reason": str(opts.get("schedule_reason") or "").strip(),
            }
        )
    if raw_schedules is not None:
        _validate_setup_schedules_shape(opts)
        for item in raw_schedules:
            item_daily = item.get("daily_at")
            if isinstance(item_daily, str):
                item_slots = [item_daily]
            elif isinstance(item_daily, (list, tuple)):
                item_slots = list(item_daily)
            elif item_daily is None:
                item_slots = []
            else:
                raise ValueError("schedules[].daily_at 必须是字符串或列表")
            raw_item_tz = item.get("tz_offset_minutes")
            tz_val = (
                _coerce_int(raw_item_tz, "schedules[].tz_offset_minutes")
                if raw_item_tz is not None
                else default_tz
            )
            requests.append(
                {
                    "daily_at": item_slots,
                    "profile": str(item.get("profile") or default_profile).strip(),
                    "workspace": str(item.get("workspace") or opts.get("workspace") or "").strip(),
                    "prompt": str(item.get("prompt") or "").strip(),
                    "tz_offset_minutes": tz_val,
                    "reason": str(item.get("reason") or "").strip(),
                }
            )

    normalized_items: list[dict] = []
    seen_slots: set[tuple[str, str, str]] = set()
    now_ts = plugin.intents.clock()
    profiles_checked: dict[str, object] = {}
    for req in requests:
        pid = req["profile"] or default_profile or "digest"
        if pid not in profiles_checked:
            ability = await completed_thread(plugin.profiles.get, pid)
            if planned_profiles is not None and pid in planned_profiles and "enabled" in planned_profiles[pid]:
                if not planned_profiles[pid]["enabled"]:
                    raise PermissionError("角色已停用")
            else:
                plugin.runtime.available(ability)
            profiles_checked[pid] = ability
        ws = normalize_profile_workspace(
            pid, req["workspace"] or ("digest" if pid == "digest" else "")
        )
        prompt = req["prompt"]
        if not prompt and pid == "digest":
            prompt = DEFAULT_DIGEST_SCHEDULE_PROMPT
        for raw_slot in req["daily_at"]:
            slot_text = str(raw_slot or "").strip()
            if not slot_text:
                continue
            due_ts, norm_hhmm = next_daily_due(now_ts, slot_text, req["tz_offset_minutes"])
            dedup_key = (pid, ws, norm_hhmm)
            if dedup_key in seen_slots:
                continue
            seen_slots.add(dedup_key)
            normalized_items.append(
                {
                    "profile": pid,
                    "workspace": ws,
                    "prompt": prompt,
                    "daily_at": norm_hhmm,
                    "tz_offset_minutes": req["tz_offset_minutes"],
                    "due_ts": due_ts,
                    "reason": req["reason"] or f"每日 {norm_hhmm} {pid}",
                }
            )

    if dry_run:
        return normalized_items, []

    cancelled_ids: list[str] = []
    if replace_sched and normalized_items:
        alias_ids = {effective_chat_id}
        if hasattr(plugin, "chat_aliases_for"):
            alias_ids.update(plugin.chat_aliases_for(effective_chat_id, effective_platform))
        for pid in dict.fromkeys(item["profile"] for item in normalized_items):
            for aid in sorted(alias_ids):
                cancelled = await completed_thread(
                    plugin.intents.cancel_matching_workflows,
                    "chat:" + aid,
                    profile=pid,
                    workspace="",
                )
                for cid in cancelled:
                    if cid not in cancelled_ids:
                        cancelled_ids.append(cid)
                        if schedule_tracker is not None:
                            schedule_tracker["cancelled"].append(cid)

    created_schedules: list[dict] = []
    for item in normalized_items:
        payload = {
            "platform": effective_platform,
            "every_seconds": 86400,
            "daily_at": item["daily_at"],
            "tz_offset_minutes": item["tz_offset_minutes"],
            "profile": item["profile"],
            "scheduled_admin": True,
        }
        if item["workspace"]:
            payload["workspace"] = item["workspace"]
        if item["prompt"]:
            payload["prompt"] = item["prompt"]
        row = await completed_thread(
            lambda it=item, pl=payload: plugin.intents.schedule(
                kind="return",
                source="workflow",
                target="chat:" + effective_chat_id,
                due_at=it["due_ts"],
                reason=it["reason"][:500],
                payload=pl,
                bypass_limits=True,
            )
        )
        if schedule_tracker is not None:
            schedule_tracker["created"].append((row["intent_id"], row["target"]))
        created_schedules.append(
            {
                "intent_id": row["intent_id"],
                "target": row["target"],
                "daily_at": item["daily_at"],
                "profile": item["profile"],
                "workspace": item["workspace"],
                "tz_offset_minutes": item["tz_offset_minutes"],
                "due_at": row["due_at"],
            }
        )
    return created_schedules, cancelled_ids


async def _apply_setup_trial(
    plugin,
    opts: dict,
    *,
    effective_chat_id: str,
    effective_platform: str,
    stream_spec: str = "",
    current_chat_id: str,
    current_platform: str,
    source_msg_id: str,
    default_profile: str,
    dry_run: bool = False,
    planned_profiles: dict[str, dict] | None = None,
    planned_enabled_chats: set[str] | None = None,
    trial_tracker: dict | None = None,
) -> dict | None:
    raw_run_now = opts.get("run_now")
    run_now = _coerce_bool(raw_run_now, "run_now") if raw_run_now is not None else False
    if not run_now:
        return None
    if plugin.engine is None:
        raise PermissionError("Maisya Agent 引擎尚未就绪")

    deliver_to = str(opts.get("deliver_to") or "target").strip()
    if deliver_to not in {"auto", "target", "current", "none"}:
        raise ValueError("deliver_to 必须是 auto / target / current / none")
    trial_profile = str(
        opts.get("trial_profile") or opts.get("profile") or default_profile or "digest"
    ).strip()
    trial_ws = normalize_profile_workspace(
        trial_profile,
        str(opts.get("workspace") or ("digest" if trial_profile == "digest" else "")).strip(),
    )
    trial_prompt = str(opts.get("trial_prompt") or opts.get("prompt") or "").strip()
    if not trial_prompt:
        trial_prompt = (
            DEFAULT_DIGEST_SCHEDULE_PROMPT
            if trial_profile == "digest"
            else f"立即为当前会话试跑一次 {trial_profile} 工作流。"
        )
    ability = await completed_thread(plugin.profiles.get, trial_profile)
    if (
        planned_profiles is not None
        and trial_profile in planned_profiles
        and "enabled" in planned_profiles[trial_profile]
    ):
        if not planned_profiles[trial_profile]["enabled"]:
            raise PermissionError("角色已停用")
    else:
        plugin.runtime.available(ability)

    cross_chat = bool(current_chat_id and effective_chat_id != current_chat_id)
    if deliver_to == "none":
        delivery_mode = "none"
    elif not cross_chat or deliver_to == "current":
        delivery_mode = "current"
    else:
        delivery_mode = "target"

    if cross_chat and delivery_mode == "target":
        if planned_enabled_chats is not None:
            will_be_enabled = stream_spec in planned_enabled_chats or (
                hasattr(plugin, "enabled_for")
                and plugin.enabled_for(effective_chat_id, effective_platform)
                and effective_chat_id in planned_enabled_chats
                or stream_spec in planned_enabled_chats
            )
            if not will_be_enabled and hasattr(plugin, "chat_aliases_for"):
                aliases = plugin.chat_aliases_for(effective_chat_id, effective_platform)
                will_be_enabled = bool(aliases & planned_enabled_chats)
        elif hasattr(plugin, "enabled_for"):
            will_be_enabled = plugin.enabled_for(effective_chat_id, effective_platform)
        else:
            will_be_enabled = True
        if not will_be_enabled:
            raise PermissionError(
                "目标会话未加入 enabled_chats，无法向其投递试跑结果；请开启 enable_chat 或传 deliver_to='current'"
            )

    wait_seconds = 0
    if opts.get("wait_seconds") is not None:
        wait_seconds = min(
            max(0, _coerce_int(opts["wait_seconds"], "wait_seconds")),
            5,
        )
    if dry_run:
        return {
            "profile": trial_profile,
            "workspace": trial_ws,
            "deliver_to": delivery_mode,
        }
    route = (
        await plugin.bridge.route(current_chat_id, source_msg_id, wait_seconds)
        if delivery_mode == "current" and current_chat_id and hasattr(plugin, "bridge")
        else None
    )
    grant = {
        "source_msg_id": source_msg_id,
        "source_chat_id": current_chat_id,
        "source_platform": current_platform,
        **({"scheduled_admin": True} if cross_chat else {}),
        **({"admin_direct": True} if cross_chat and delivery_mode == "current" else {}),
    }
    try:
        started = await plugin.engine.start(
            ability,
            trial_prompt,
            chat_id=effective_chat_id,
            platform=effective_platform,
            user_id="",
            owner="chat",
            wait_seconds=wait_seconds if delivery_mode == "current" else 0,
            return_route=route,
            workspace=trial_ws,
            grant=grant,
        )
        if trial_tracker is not None:
            trial_tracker["thread_id"] = started["thread_id"]
        if (
            route
            and started.get("state") in {"idle", "waiting"}
            and hasattr(plugin, "bridge")
        ):
            await plugin.bridge.inline(started["thread_id"], current_chat_id)
    finally:
        if route and hasattr(plugin, "bridge"):
            await plugin.bridge.submitted(route)
    delivery_intent_id = ""
    if cross_chat and delivery_mode == "target" and getattr(plugin, "intents", None) is not None:
        delivery = await plugin.schedule_cross_chat_delivery(
            effective_chat_id,
            effective_platform,
            started["thread_id"],
            trial_prompt,
            "一键配置试跑交付",
            is_admin_caller=True,
        )
        delivery_intent_id = str(delivery.get("intent_id") or "")
        if trial_tracker is not None and delivery_intent_id:
            trial_tracker["delivery_intent_id"] = delivery_intent_id
            trial_tracker["delivery_target"] = "chat:" + effective_chat_id
    return {
        "thread_id": started["thread_id"],
        "state": started.get("state", ""),
        "profile": trial_profile,
        "workspace": trial_ws,
        "deliver_to": delivery_mode,
        "delivery_intent_id": delivery_intent_id,
    }


def _build_setup_summary(
    stream_spec: str,
    applied: dict,
    schedules: list[dict],
    trial_info: dict | None,
    warnings: list[str],
) -> str:
    clauses: list[str] = []
    settings_info = applied.get("settings")
    if settings_info:
        if settings_info.get("enabled_chat"):
            clauses.append("已加入白名单")
        if "sleep_window" in settings_info and settings_info.get("sleep_window"):
            clauses.append(f"安静时段 {settings_info['sleep_window']}")
        if settings_info.get("chat_proactive_level"):
            clauses.append(f"本群主动度 {settings_info['chat_proactive_level']}")
    profiles_info = applied.get("profiles") or {}
    if profiles_info:
        clauses.append("已配置角色 " + "、".join(profiles_info.keys()))
    if applied.get("memory"):
        clauses.append("已更新本群关注偏好")
    sources_info = applied.get("digest_sources")
    if sources_info:
        clauses.append(f"工作区 {sources_info['workspace']} 现有 {sources_info['feeds_total']} 个资讯源")
    if schedules:
        slots = "、".join(f"{s['daily_at']}({s['profile']})" for s in schedules)
        clauses.append(f"已安排每日定时 {slots}")
    if trial_info:
        clauses.append(f"已发起立即试跑({trial_info['thread_id']})")
    if warnings:
        clauses.append(f"含 {len(warnings)} 条提醒")
    body = "，".join(clauses) if clauses else "未检测到需要变更的配置项"
    tail = ""
    if trial_info and trial_info.get("state") == "active":
        tail = " 后台试跑完成后会自动回传结果，请先调用 reply 自然回复用户，勿调用 wait 或 maisya_recall 轮询。"
    return f"已为 {stream_spec} 完成配置：{body}。{tail}"


async def handle_setup(
    plugin,
    *,
    target_chat: str = "",
    patch: dict | None = None,
    content: str = "",
    current_chat_id: str = "",
    current_platform: str = "",
    source_msg_id: str = "",
    author: str = "maisya_shape",
) -> dict:
    """在单次调用中组合完成会话白名单、安静时段、角色/模型、群偏好、资讯源、定时计划与试跑。"""
    opts = _coerce_setup_opts(plugin, patch, content)
    raw_tc = str(target_chat or opts.pop("target_chat", "") or "").strip()
    effective_chat_id, effective_platform, stream_spec = await _resolve_setup_target(
        plugin, raw_tc, current_chat_id, current_platform
    )

    warnings: list[str] = []
    applied: dict = {}

    # Phase 1: pre-validate every setup step before persisting anything
    _validate_setup_schedules_shape(opts)
    planned_settings = await _apply_setup_settings(
        plugin,
        opts,
        has_explicit_target=bool(raw_tc),
        effective_chat_id=effective_chat_id,
        effective_platform=effective_platform,
        stream_spec=stream_spec,
        author=author,
        dry_run=True,
    )
    planned_profile_patches = await _prepare_setup_profile_patches(plugin, opts)
    planned_profiles = await _apply_setup_profiles(
        plugin,
        opts,
        effective_chat_id=effective_chat_id,
        effective_platform=effective_platform,
        author=author,
        dry_run=True,
        prepared_patches=planned_profile_patches,
    )
    await _apply_setup_memory(
        plugin,
        opts,
        effective_chat_id=effective_chat_id,
        author=author,
        dry_run=True,
    )

    default_profile = str(opts.get("profile") or "").strip()
    if not default_profile:
        default_profile = next(iter(planned_profiles), "digest")
    digest_explicitly_disabled = (
        "digest" in planned_profiles and planned_profiles["digest"].get("enabled") is False
    )
    digest_involved = not digest_explicitly_disabled and (
        "digest" in planned_profiles
        or default_profile == "digest"
        or any(
            isinstance(item, dict) and str(item.get("profile") or "").strip() == "digest"
            for item in (opts.get("schedules") or ())
        )
    )
    await _apply_setup_digest_sources(
        plugin,
        opts,
        effective_chat_id=effective_chat_id,
        effective_platform=effective_platform,
        digest_involved=digest_involved,
        warnings=warnings,
        dry_run=True,
    )
    await _apply_setup_schedules(
        plugin,
        opts,
        effective_chat_id=effective_chat_id,
        effective_platform=effective_platform,
        default_profile=default_profile,
        dry_run=True,
        planned_profiles=planned_profiles,
    )
    planned_enabled_chats = (
        set(planned_settings["enabled_chats"])
        if planned_settings is not None
        else set(getattr(plugin.settings, "enabled_chats", ()) or ())
    )
    await _apply_setup_trial(
        plugin,
        opts,
        effective_chat_id=effective_chat_id,
        effective_platform=effective_platform,
        stream_spec=stream_spec,
        current_chat_id=current_chat_id,
        current_platform=current_platform,
        source_msg_id=source_msg_id,
        default_profile=default_profile,
        dry_run=True,
        planned_profiles=planned_profiles,
        planned_enabled_chats=planned_enabled_chats,
    )

    # Phase 2: apply with per-step snapshots and full reverse rollback on any exception
    rollbacks: list = []
    try:
        if planned_settings is not None:
            orig_settings_state = await completed_thread(_load_or_sync_settings_history, plugin)
            settings_path = plugin.data_root / "settings.json"
            orig_settings_bytes = await completed_thread(
                lambda: settings_path.read_bytes() if settings_path.is_file() else None
            )

            async def _rb_settings() -> None:
                async with _shaping_lock(plugin):
                    orig_live = copy.deepcopy(orig_settings_state["entries"][-1]["settings"])
                    await plugin.on_config_update("self", orig_live, 0)

                    def _restore_files() -> None:
                        if orig_settings_bytes is not None:
                            atomic_private_write(settings_path, orig_settings_bytes)
                        else:
                            settings_path.unlink(missing_ok=True)
                        atomic_private_write(
                            _history_path(plugin),
                            json.dumps(orig_settings_state, ensure_ascii=False).encode(),
                        )

                    await completed_thread(_restore_files)

            rollbacks.append(_rb_settings)
            settings_res = await _apply_setup_settings(
                plugin,
                opts,
                has_explicit_target=bool(raw_tc),
                effective_chat_id=effective_chat_id,
                effective_platform=effective_platform,
                stream_spec=stream_spec,
                author=author,
            )
            if settings_res is not None:
                applied["settings"] = settings_res

        touched_profile_snaps: list[tuple[str, dict, dict]] = []

        async def _rb_profiles() -> None:
            def _restore_profiles() -> None:
                for pid, toml_snap, md_snap in reversed(touched_profile_snaps):
                    cur_toml = plugin.memory.read(
                        "profile", target=pid, document="profile.toml", allow_custom=True
                    )
                    if cur_toml["content"] != toml_snap["content"]:
                        plugin.memory.write(
                            "profile",
                            toml_snap["content"],
                            expected_version=cur_toml["version"],
                            target=pid,
                            document="profile.toml",
                            author="rollback",
                            allow_custom=True,
                        )
                    cur_md = plugin.memory.read(
                        "profile", target=pid, document="PROFILE.md", allow_custom=True
                    )
                    if cur_md["content"] != md_snap["content"]:
                        plugin.memory.write(
                            "profile",
                            md_snap["content"],
                            expected_version=cur_md["version"],
                            target=pid,
                            document="PROFILE.md",
                            author="rollback",
                            allow_custom=True,
                        )
                    plugin.profiles.get(pid)

            if touched_profile_snaps:
                await completed_thread(_restore_profiles)

        rollbacks.append(_rb_profiles)
        profiles_res = await _apply_setup_profiles(
            plugin,
            opts,
            effective_chat_id=effective_chat_id,
            effective_platform=effective_platform,
            author=author,
            rollback_snaps=touched_profile_snaps,
            prepared_patches=planned_profile_patches,
        )
        if profiles_res:
            applied["profiles"] = profiles_res

        orig_mem_doc = await completed_thread(
            lambda: plugin.memory.read("memory", chat_id=effective_chat_id, allow_custom=True)
        )
        memory_res = await _apply_setup_memory(
            plugin,
            opts,
            effective_chat_id=effective_chat_id,
            author=author,
        )
        if memory_res is not None:
            applied["memory"] = memory_res

            async def _rb_memory() -> None:
                def _restore_mem() -> None:
                    cur_mem = plugin.memory.read(
                        "memory", chat_id=effective_chat_id, allow_custom=True
                    )
                    if cur_mem["content"] != orig_mem_doc["content"]:
                        plugin.memory.write(
                            "memory",
                            orig_mem_doc["content"],
                            expected_version=cur_mem["version"],
                            chat_id=effective_chat_id,
                            author="rollback",
                            allow_custom=True,
                        )

                await completed_thread(_restore_mem)

            rollbacks.append(_rb_memory)

        sources_snapshot: dict = {}

        async def _rb_digest_sources() -> None:
            if "orig_sources" not in sources_snapshot:
                return
            ws_name = sources_snapshot["workspace"]
            binding = await completed_thread(
                _find_digest_workspace_binding,
                plugin,
                effective_chat_id,
                effective_platform,
                ws_name,
            )

            async def _restore_sources(dw: DigestWorkspace) -> None:
                def _write_and_commit() -> None:
                    dw._save_sources(sources_snapshot["orig_sources"])
                    dw.work.commit()

                await completed_thread(_write_and_commit)

            await _run_in_digest_workspace(
                plugin,
                effective_chat_id,
                effective_platform,
                ws_name,
                binding,
                _restore_sources,
            )

        rollbacks.append(_rb_digest_sources)
        sources_res = await _apply_setup_digest_sources(
            plugin,
            opts,
            effective_chat_id=effective_chat_id,
            effective_platform=effective_platform,
            digest_involved=digest_involved,
            warnings=warnings,
            sources_snapshot=sources_snapshot,
        )
        if sources_res is not None:
            applied["digest_sources"] = sources_res

        sched_tracker: dict[str, list] = {"cancelled": [], "created": []}

        async def _rb_schedules() -> None:
            if plugin.intents is None:
                return

            def _restore_schedules() -> None:
                for intent_id, target in reversed(sched_tracker["created"]):
                    try:
                        plugin.intents.cancel(intent_id, target)
                    except ValueError:
                        pass
                if sched_tracker["cancelled"]:
                    with plugin.intents.store.connect() as db, db:
                        for cid in reversed(sched_tracker["cancelled"]):
                            db.execute(
                                "UPDATE intents SET status='pending', handled=NULL WHERE intent_id=? AND status='cancelled'",
                                (cid,),
                            )

            if sched_tracker["created"] or sched_tracker["cancelled"]:
                await completed_thread(_restore_schedules)

        rollbacks.append(_rb_schedules)
        schedules, cancelled_schedules = await _apply_setup_schedules(
            plugin,
            opts,
            effective_chat_id=effective_chat_id,
            effective_platform=effective_platform,
            default_profile=default_profile,
            schedule_tracker=sched_tracker,
        )
        if schedules or cancelled_schedules:
            applied["schedules"] = {"count": len(schedules), "cancelled": len(cancelled_schedules)}

        trial_tracker: dict = {}

        async def _rb_trial() -> None:
            deliv_id = trial_tracker.get("delivery_intent_id")
            if deliv_id and plugin.intents is not None:
                try:
                    await completed_thread(
                        plugin.intents.cancel,
                        deliv_id,
                        trial_tracker.get("delivery_target", "chat:" + effective_chat_id),
                    )
                except ValueError:
                    pass
            tid = trial_tracker.get("thread_id")
            if tid and plugin.engine is not None:
                try:
                    await plugin.engine.stop(tid, chat_id=effective_chat_id)
                except (ValueError, PermissionError, KeyError, sqlite3.Error):
                    pass

        rollbacks.append(_rb_trial)
        trial_info = await _apply_setup_trial(
            plugin,
            opts,
            effective_chat_id=effective_chat_id,
            effective_platform=effective_platform,
            stream_spec=stream_spec,
            current_chat_id=current_chat_id,
            current_platform=current_platform,
            source_msg_id=source_msg_id,
            default_profile=default_profile,
            trial_tracker=trial_tracker,
        )
        if trial_info is not None:
            applied["trial_run"] = trial_info
    except BaseException:
        for rb in reversed(rollbacks):
            try:
                await rb()
            except Exception:
                pass
        raise

    trial_thread_id = trial_info["thread_id"] if trial_info else ""
    delivery_intent_id = trial_info.get("delivery_intent_id", "") if trial_info else ""
    next_step = ""
    if trial_info and trial_info.get("state") == "active":
        if trial_info.get("deliver_to") == "current":
            next_step = (
                "后台试跑线程已在运行，完成后系统会自动通过 <maisya_returns> 唤醒本会话交付结果与图卡。"
                "请现在立即调用 reply 先向用户自然简短交代已生效的配置与后台试跑进度；"
                "本轮切勿调用 wait、切勿用 maisya_recall 轮询等待，也不要对运行中的同一线程重复发指令。"
            )
        else:
            next_step = (
                "后台试跑线程已在运行，完成后系统会自动投递至目标会话。"
                "请现在立即调用 reply 先向用户自然简短交代已生效的配置与后台试跑安排；"
                "本轮切勿调用 wait 或 maisya_recall 轮询等待。"
            )
    summary = _build_setup_summary(stream_spec, applied, schedules, trial_info, warnings)
    return {
        "kind": "setup",
        "chat_id": effective_chat_id,
        "platform": effective_platform,
        "stream_spec": stream_spec,
        "applied": applied,
        "warnings": warnings,
        "schedules": schedules,
        "cancelled_schedules": cancelled_schedules,
        "trial_thread_id": trial_thread_id,
        **({"delivery_intent_id": delivery_intent_id} if delivery_intent_id else {}),
        **({"next_step": next_step} if next_step else {}),
        "summary": summary,
    }


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
    expected_version: int | str | None = None,
    base: str = "scout",
    url: str = "",
    preview_token: str = "",
    current_chat_id: str = "",
    current_platform: str = "",
    source_msg_id: str = "",
    author: str = "maisya_shape",
) -> dict:
    if action == "setup" or kind == "setup":
        return _redact(
            plugin,
            await handle_setup(
                plugin,
                target_chat=target_chat,
                patch=patch,
                content=content,
                current_chat_id=current_chat_id,
                current_platform=current_platform,
                source_msg_id=source_msg_id,
                author=author,
            ),
        )

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
            raw_opts = _unwrap_item_wrappers(patch) if isinstance(patch, dict) else None
            opts = dict(raw_opts or {}) if isinstance(raw_opts, dict) else {}
            base_id = str(opts.pop("base", base or "scout") or "scout")
            await completed_thread(plugin.profiles.clone, base_id, target)
            try:
                await completed_thread(plugin.memory.seed_profile, target)
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
            except BaseException:
                def _cleanup_created_profile() -> None:
                    pdir = plugin.profiles.path / target
                    if pdir.is_dir() and not path_is_link_like(pdir):
                        shutil.rmtree(pdir, ignore_errors=True)
                    plugin.memory.purge_profile(target)

                await completed_thread(_cleanup_created_profile)
                raise
        if action == "delete":
            deleted_res = await completed_thread(plugin.profiles.delete, target)
            await completed_thread(plugin.memory.purge_profile, target)
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
                plugin,
                kind=kind,
                action=action,
                content=content,
                patch=patch,
                expected_version=expected_version,
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
