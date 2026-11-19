"""Planner 工具参数保守纠偏、可照抄自纠报错模板与人话故障说明（借鉴 Hermes arg_coercion / failure gloss）。"""

from __future__ import annotations

import json
from typing import Any


SHAPE_READONLY_OVERVIEW_ALIASES: frozenset[str] = frozenset(
    {"help", "status", "info", "list", "chats", "summary", "check", "inspect", "manage", "self_manage"}
)

SHAPE_ENABLE_CHAT_ALIASES: frozenset[str] = frozenset(
    {
        "enable",
        "enable_chat",
        "add_chat",
        "allow_chat",
        "whitelist",
        "whitelist_add",
        "enable_whitelist",
        "chat_allow",
        "allow",
        "permit",
        "join",
        "activate",
        "enable_chat_delivery",
        "set_enabled",
        "toggle",
        "add",
        "register",
        "chat",
        "update",
        "patch",
        "apply",
        "promote",
    }
)


def coerce_bool(value: Any) -> Any:
    """保守将字符串布尔转为 bool；无法确定时保留原值。"""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "on", "1"}:
            return True
        if low in {"false", "no", "off", "0"}:
            return False
    return value


def coerce_int(value: Any) -> Any:
    """保守将纯数字字符串转为 int；无法确定时保留原值。"""
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw and (raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit())):
            try:
                return int(raw)
            except ValueError:
                return value
    return value


def coerce_dict(value: Any) -> Any:
    """保守将 JSON 对象字符串转为 dict，并递归纠偏常见浅层数值/布尔字符串。"""
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.startswith("{") and trimmed.endswith("}"):
            try:
                parsed = json.loads(trimmed)
                if isinstance(parsed, dict):
                    value = parsed
            except ValueError:
                return value
    if not isinstance(value, dict):
        return value
    _BOOL_KEYS = {"enabled", "run_now", "overwrite", "readonly", "shared_secrets", "autonomous", "bound", "replace"}
    _INT_KEYS = {
        "daily_proactive_limit",
        "per_chat_daily_proactive_limit",
        "daily_bg_runs_limit",
        "daily_bg_tokens_limit",
        "bg_max_consecutive_failures",
        "heartbeat_interval_minutes",
        "longing_quiet_hours",
        "every_seconds",
        "max_tokens",
        "context_tokens",
        "max_steps",
        "timeout_seconds",
        "coalesce_window_ms",
    }
    _LIST_KEYS = {
        "enabled_chats",
        "admins",
        "topics",
        "digest_topics",
        "urls",
        "digest_feeds",
        "fallback_models",
        "toolsets",
        "tools",
        "skills",
        "mcp",
        "mcp_servers",
        "apis",
        "secrets",
        "sandbox_targets",
        "allow_chats",
        "allow_users",
        "bind_profiles",
        "acquaintance_users",
    }
    out = dict(value)
    for k, v in list(out.items()):
        if k in _BOOL_KEYS:
            out[k] = coerce_bool(v)
        elif k in _INT_KEYS:
            out[k] = coerce_int(v)
        elif k in _LIST_KEYS:
            out[k] = coerce_str_list(v)
    return out


def coerce_str_list(value: Any) -> Any:
    """保守将 JSON 数组字符串或单值非空字符串转为字符串列表。"""
    if value is None or isinstance(value, (list, tuple)):
        return value
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return []
        if trimmed.startswith("[") and trimmed.endswith("]"):
            try:
                parsed = json.loads(trimmed)
                if isinstance(parsed, list):
                    return parsed
            except ValueError:
                pass
        return [trimmed]
    return value


def format_actionable_shape_error(
    area: str,
    *,
    target_chat: str = "",
    source_msg_id: str = "",
) -> str:
    """为 maisya_shape 非法 action 生成附带真实参数的可照抄调用示例，防 Flash 模型盲猜死循环。"""
    clean_area = str(area or "").strip()
    low = clean_area.lower()
    tgt = str(target_chat or "").strip() or "qq:group:<群号>"
    src = str(source_msg_id or "").strip() or "<真实source_msg_id>"
    if low in SHAPE_ENABLE_CHAT_ALIASES or any(
        k in low for k in ("enable", "allow", "white", "chat", "group", "join", "add", "permit")
    ):
        return (
            f"不支持的自管理类型：{clean_area}。"
            f'若要开启群白名单或一站配齐，请直接调用：maisya_shape(action="setup", target_chat="{tgt}", source_msg_id="{src}")；'
            f'或修改总设置：maisya_shape(action="settings", op="patch", patch={{"enabled_chats": ["{tgt}"]}}, source_msg_id="{src}")。'
            "全部合法 action：overview / setup / settings / profile / file / memory / role_memory / notes / journal / heartbeat / planner / digest_sources / services / skill_proposal。"
        )
    return (
        f"不支持的自管理类型：{clean_area}。"
        "可用 action：overview / setup / settings / profile / file / memory / role_memory / "
        "notes / journal / heartbeat / planner / digest_sources / services / skill_proposal；"
        f'查看全盘状态请调 maisya_shape(action="overview", source_msg_id="{src}")；'
        f'开启群白名单请调 maisya_shape(action="setup", target_chat="{tgt}", source_msg_id="{src}")。'
    )


def format_actionable_agent_error(
    err_msg: str,
    *,
    target_chat: str = "",
    source_msg_id: str = "",
    thread_id: str = "",
) -> str:
    """为 maisya_agent 常见失败补上一步即可执行的修正模板。"""
    msg = str(err_msg or "").strip()
    if not msg:
        return msg
    tgt = str(target_chat or "").strip()
    src = str(source_msg_id or "").strip() or "<真实source_msg_id>"
    if "尚未加入 enabled_chats" in msg and "action=" not in msg:
        tgt_hint = tgt or "qq:group:<群号>"
        return (
            f"{msg}。两选一修正："
            f'1) 仅私聊预览请加 deliver_to="current"；'
            f'2) 开启该群白名单请先调 maisya_shape(action="setup", target_chat="{tgt_hint}", source_msg_id="{src}")。'
        )
    return msg


def is_transient_llm_error(exc: Exception) -> bool:
    """识别适合自动回退或断点静默重试的上游瞬态故障（超时/限流/5xx/断连）。"""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    text = str(exc or "").lower()
    markers = (
        "timed out",
        "timeout",
        "apitimeouterror",
        "rate limit",
        "429",
        "502",
        "503",
        "504",
        "bad gateway",
        "service unavailable",
        "temporarily unavailable",
        "connection reset",
        "connection refused",
        "remote protocol",
        "eof occurred",
        "overloaded",
    )
    return any(m in text for m in markers)


def format_human_thread_failure(exc: Exception, *, redact=None) -> str:
    """将底层异常归类为一句大白话原因 + 下一步操作提示（绝不透传原始 traceback）。"""
    raw = str(exc or "")
    low = raw.lower()
    if isinstance(exc, PermissionError):
        clean = str(redact(raw) if callable(redact) else raw).strip()
        return f"{clean or '当前操作缺少必要授权'}。确认授权后可再试。"
    if isinstance(exc, TimeoutError) or any(
        k in low for k in ("timed out", "timeout", "apitimeouterror", "运行时限")
    ):
        return "本轮 Agent 活动中断（模型或工具响应超时），已保留中间进度。可直接给原线程发消息继续，或在管理台补充备选模型。"
    if any(k in low for k in ("429", "rate limit", "too many requests", "quota", "overloaded")):
        return "本轮 Agent 活动中断（上游模型接口当前限流或繁忙）。稍等片刻后给原线程发消息即可续跑。"
    if any(k in low for k in ("400", "违规", "content_filter", "safety", "policy violation", "moderation")):
        return "本轮 Agent 活动中断（上游模型因内容安全策略拒绝了本轮请求）。可换个中性表述或切换备选模型后重试。"
    if "步数上限" in raw:
        return "本轮 Agent 活动中断（任务较复杂，已达到单轮步数上限并保存进度）。回复一句「继续」即可接着推进。"
    if "没有给出结果" in raw or "没有返回文字或工具调用" in raw:
        return "本轮 Agent 活动中断（上游模型本轮返回了空内容）。直接给原线程补充一句即可重跑。"
    if any(k in low for k in ("502", "503", "504", "connection", "bad gateway", "unavailable")):
        return "本轮 Agent 活动中断（上游模型服务暂时不可用或网络闪断）。稍后给原线程发消息即可接着跑。"
    return "本轮 Agent 活动中断，请检查模型、能力配置与执行环境。可给原线程补充消息后继续。"
