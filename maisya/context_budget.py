"""线程 Token 预算估算、模型窗口解析与两级无损压缩纯函数集。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

DEFAULT_CONTEXT_TOKENS = 262_144
MIN_CONTEXT_TOKENS = 16_384
MAX_CONTEXT_TOKENS = 2_000_000
DEFAULT_OUTPUT_RESERVE_TOKENS = 4_096

PRUNE_WATERMARK = 0.55
TRIGGER_WATERMARK = 0.75
PRUNE_MIN_RECLAIM_TOKENS = 4_096
DEFAULT_MIN_PRUNE_CHARS = 4_000
DEFAULT_MIN_DUP_CHARS = 256
DEFAULT_TAIL_TOKENS = 20_000
THREAD_READ_DEFAULT_MAX_CHARS = 16_000

_CJK_RE = re.compile(
    r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]"
)
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)\}]+")
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_/:])(?:/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})"
)
_ERROR_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:Error|Exception|Timeout)\b")

_INTERNAL_USER_PREFIXES = (
    "<thread_inputs>",
    "<thread_history>",
    "<workspace_context>",
    "<memory_context>",
    "<activity_interrupted>",
    "<previous_runs>",
)

# 按更具体前缀排在前面；用于给小于 256K 的已知模型家族向下封顶，避免请求直接 400 溢出。
MODEL_FAMILY_WINDOWS: tuple[tuple[str, int], ...] = (
    # Gemini
    ("gemini-3", 1_048_576),
    ("gemini-2", 1_048_576),
    ("gemini-1.5", 1_048_576),
    # OpenAI
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4-32k", 32_768),
    ("gpt-4", 8_192),
    ("gpt-3.5", 16_384),
    ("o4", 200_000),
    ("o3", 200_000),
    ("o1-mini", 128_000),
    ("o1-preview", 128_000),
    ("o1", 200_000),
    # Anthropic Claude
    ("claude-", 200_000),
    # Moonshot / Kimi
    ("kimi-k2.5", 262_144),
    ("kimi-k2", 131_072),
    ("moonshot-v1-128k", 131_072),
    ("moonshot-v1-32k", 32_768),
    ("moonshot-v1-8k", 8_192),
    # Zhipu GLM
    ("glm-5", 202_752),
    ("glm-4.7", 202_752),
    ("glm-4.6", 202_752),
    ("glm-4.5", 202_752),
    ("glm-4", 128_000),
    # MiniMax
    ("minimax", 204_800),
    # DeepSeek
    ("deepseek", 128_000),
    # Qwen
    ("qwen3-coder", 262_144),
    ("qwen3", 131_072),
    ("qwen2.5", 131_072),
    ("qwq", 131_072),
    # xAI Grok
    ("grok-4", 256_000),
    ("grok-3", 131_072),
    # Meta Llama
    ("llama-4", 1_000_000),
    ("llama-3", 128_000),
)

_OVERFLOW_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"maximum context length", re.IGNORECASE),
    re.compile(r"context[_\s-]?length[_\s-]?exceeded", re.IGNORECASE),
    re.compile(r"prompt is too long", re.IGNORECASE),
    re.compile(r"too many tokens", re.IGNORECASE),
    re.compile(r"token limit exceeded", re.IGNORECASE),
    re.compile(r"exceeds? the maximum (?:number of )?tokens", re.IGNORECASE),
    re.compile(r"input length exceeds", re.IGNORECASE),
    re.compile(r"request too large", re.IGNORECASE),
    re.compile(r"reduce the length of the messages", re.IGNORECASE),
    re.compile(r"context window.*exceeded", re.IGNORECASE),
)


def _apply_redact(redact: Any, value: str) -> str:
    if not redact or not isinstance(value, str):
        return value
    return str(redact(value))


def estimate_tokens(text: Any) -> int:
    """混合估算文本 Token 数：CJK 按 1 字 ≈ 1 token，其余按 UTF-8 bytes / 4 向上取整。"""
    if text is None:
        return 0
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    if not text:
        return 0
    if text.isascii():
        return (len(text) + 3) // 4
    rest = _CJK_RE.sub("", text)
    cjk_count = len(text) - len(rest)
    rest_bytes = len(rest.encode("utf-8"))
    return cjk_count + ((rest_bytes + 3) // 4)


def estimate_message_tokens(message: Any) -> int:
    """估算单条 OpenAI-style 对话消息的 Token 开销。"""
    if not isinstance(message, dict):
        return estimate_tokens(str(message)) + 4
    total = 4
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                kind = str(part.get("type") or "")
                if kind == "text":
                    total += estimate_tokens(part.get("text") or "")
                elif kind in {"image_url", "image"}:
                    total += 256
                else:
                    total += estimate_tokens(part)
            else:
                total += estimate_tokens(part)
    elif content is not None:
        total += estimate_tokens(content)

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                total += estimate_tokens(call)
                continue
            fn = call.get("function") or {}
            if not isinstance(fn, dict):
                fn = {}
            name = str(fn.get("name") or call.get("func_name") or call.get("name") or "")
            args = fn.get("arguments") if "arguments" in fn else call.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            total += 4 + estimate_tokens(name) + estimate_tokens(args)

    if message.get("name"):
        total += estimate_tokens(str(message["name"]))
    if message.get("tool_call_id"):
        total += 2
    return total


def estimate_messages_tokens(messages: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> int:
    """估算多条消息列表的总 Token 数。"""
    if not messages:
        return 0
    return sum(estimate_message_tokens(msg) for msg in messages)


def estimate_tools_tokens(tools: list[dict[str, Any]] | None) -> int:
    """估算当前挂载工具 Schema 的 Token 开销。"""
    if not tools:
        return 0
    return sum(estimate_tokens(json.dumps(tool, ensure_ascii=False)) + 4 for tool in tools)


_FAMILY_WINDOW_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = tuple(
    (
        re.compile(
            r"(?<![a-z0-9])"
            + re.escape(prefix)
            + ("" if prefix.endswith("-") else r"(?![a-z0-9])")
        ),
        window,
    )
    for prefix, window in sorted(
        MODEL_FAMILY_WINDOWS, key=lambda item: len(item[0]), reverse=True
    )
)


def _match_family_window(name_or_id: str) -> int | None:
    raw = str(name_or_id or "").strip().lower()
    if not raw:
        return None
    model_part = raw.rsplit("/", 1)[-1].strip()
    if not model_part:
        return None
    for pattern, window in _FAMILY_WINDOW_PATTERNS:
        if pattern.search(model_part):
            return window
    return None


def _resolve_model_descriptor(profile: Any, model_catalog: dict[str, Any] | None) -> tuple[str, str]:
    catalog = model_catalog if isinstance(model_catalog, dict) else {}
    models = catalog.get("models") if isinstance(catalog.get("models"), list) else []
    tasks = catalog.get("tasks") if isinstance(catalog.get("tasks"), dict) else {}

    model_name = ""
    if profile is not None:
        model_name = str(
            getattr(profile, "model_name", "") or getattr(profile, "model", "") or ""
        ).strip()
        if not model_name:
            task = str(getattr(profile, "task", "") or "utils").strip() or "utils"
            task_models = tasks.get(task)
            if isinstance(task_models, list):
                for item in task_models:
                    if isinstance(item, str) and item.strip():
                        model_name = item.strip()
                        break

    identifier = ""
    if model_name:
        for entry in models:
            if not isinstance(entry, dict):
                continue
            if entry.get("name") == model_name or entry.get("identifier") == model_name:
                identifier = str(entry.get("identifier") or "").strip()
                break
    return model_name, identifier


def resolve_context_budget(
    profile: Any = None,
    *,
    default_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    model_catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解析角色有效上下文 Token 窗口及 55%/75% 压缩水位。"""
    try:
        base_default = int(default_context_tokens or DEFAULT_CONTEXT_TOKENS)
    except (TypeError, ValueError):
        base_default = DEFAULT_CONTEXT_TOKENS
    base_default = max(MIN_CONTEXT_TOKENS, min(MAX_CONTEXT_TOKENS, base_default))

    model_name, model_identifier = _resolve_model_descriptor(profile, model_catalog)
    family_window = _match_family_window(model_identifier)
    if family_window is None:
        family_window = _match_family_window(model_name)

    explicit = getattr(profile, "context_tokens", None) if profile is not None else None
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit > 0:
        window_tokens = max(8_192, min(MAX_CONTEXT_TOKENS, int(explicit)))
        source = "profile"
    elif family_window is not None and family_window < base_default:
        window_tokens = family_window
        source = "model_family"
    else:
        window_tokens = base_default
        source = "default"

    prof_max_tokens = getattr(profile, "max_tokens", None) if profile is not None else None
    if isinstance(prof_max_tokens, int) and not isinstance(prof_max_tokens, bool) and prof_max_tokens > 0:
        output_reserve = min(int(prof_max_tokens), max(DEFAULT_OUTPUT_RESERVE_TOKENS, window_tokens // 4))
    else:
        output_reserve = min(DEFAULT_OUTPUT_RESERVE_TOKENS, max(1024, window_tokens // 4))

    effective_tokens = max(2048, window_tokens - output_reserve)
    trigger_tokens = int(effective_tokens * TRIGGER_WATERMARK)
    prune_tokens = int(effective_tokens * PRUNE_WATERMARK)
    tail_tokens = min(24_000, max(1_200, int(effective_tokens * 0.12)))

    return {
        "window_tokens": window_tokens,
        "output_reserve": output_reserve,
        "effective_tokens": effective_tokens,
        "trigger_tokens": trigger_tokens,
        "prune_tokens": prune_tokens,
        "tail_tokens": tail_tokens,
        "source": source,
        "model_name": model_name,
        "model_identifier": model_identifier,
        "family_window": family_window,
    }


def _find_protect_start(history: list[dict[str, Any]], tail_tokens: int) -> int:
    if not history:
        return 0
    accum = 0
    protect_from = len(history)
    limit = max(0, int(tail_tokens))
    for idx in range(len(history) - 1, -1, -1):
        msg_tokens = estimate_message_tokens(history[idx])
        if idx < len(history) - 1 and accum + msg_tokens > limit:
            break
        accum += msg_tokens
        protect_from = idx
    # 若保护起点落在 tool 消息上，向左扩展到拥有该工具调用的 assistant，防止拆散配对
    while protect_from > 0 and history[protect_from].get("role") == "tool":
        protect_from -= 1
    return protect_from


def _map_tool_names(history: list[dict[str, Any]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for msg in history:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "").strip()
            fn = call.get("function") or {}
            if not isinstance(fn, dict):
                fn = {}
            name = str(fn.get("name") or call.get("func_name") or call.get("name") or "").strip()
            if call_id and name:
                mapping[call_id] = name
    return mapping


def _format_pruned_tool_content(
    content: str,
    *,
    reason: str,
    tool_name: str,
    orig_chars: int,
    orig_tokens: int,
    event_seq: int | str,
) -> str:
    stripped = content.strip()
    wrapped = stripped.startswith("<external_data>") and stripped.endswith("</external_data>")
    inner = stripped[len("<external_data>") : -len("</external_data>")].strip() if wrapped else content
    label = tool_name or "tool"
    if reason == "duplicate":
        body = (
            f"[已折叠重复工具输出({label}): 与后文相同，共 {orig_chars} 字 / ~{orig_tokens} tokens；"
            f"完整原文已无损归档于 event_seq={event_seq}，可用 thread_read 查看]"
        )
    else:
        head = inner[:160].replace("<external_data>", "").replace("</external_data>", "").strip()
        prefix = f"{head}\n" if head else ""
        body = (
            f"{prefix}...[已折叠旧工具输出({label}): 原始 {orig_chars} 字 / ~{orig_tokens} tokens，"
            f"完整原文已无损归档于 event_seq={event_seq}，需要细节时调用 thread_read 查看]"
        )
    if wrapped:
        return f"<external_data>{body}</external_data>"
    return body


def prune_tool_outputs(
    history: list[dict[str, Any]],
    *,
    tail_tokens: int = DEFAULT_TAIL_TOKENS,
    min_prune_chars: int = DEFAULT_MIN_PRUNE_CHARS,
    min_dup_chars: int = DEFAULT_MIN_DUP_CHARS,
    event_seq: int | str = "{event_seq}",
    only_tools: set[str] | tuple[str, ...] | list[str] | None = None,
    protect_last_turn: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tier-1 零成本旧工具输出去重与折叠，绝不改动 user 消息和 assistant.tool_calls 参数。"""
    if not history:
        return [], {"reclaimed_tokens": 0, "pruned_entries": [], "protect_from": 0}

    protect_from = _find_protect_start(history, tail_tokens)
    if protect_last_turn:
        last_assistant = next(
            (i for i in range(len(history) - 1, -1, -1) if history[i].get("role") == "assistant"),
            0,
        )
        protect_from = min(protect_from, last_assistant) if protect_from > 0 else last_assistant
    if protect_from <= 0:
        return [dict(m) for m in history], {
            "reclaimed_tokens": 0,
            "pruned_entries": [],
            "protect_from": 0,
        }

    only_set = (
        {str(t).strip() for t in only_tools if str(t).strip()}
        if only_tools is not None
        else None
    )
    tool_names = _map_tool_names(history)
    last_seen: dict[tuple[str, str], int] = {}
    for idx, msg in enumerate(history):
        if msg.get("role") != "tool":
            continue
        raw = msg.get("content")
        if not isinstance(raw, str) or len(raw) < min_dup_chars:
            continue
        if "event_seq=" in raw and ("[已折叠" in raw):
            continue
        tname = str(msg.get("name") or tool_names.get(str(msg.get("tool_call_id") or ""), "") or "")
        if only_set is not None and tname not in only_set:
            continue
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
        last_seen[(tname, digest)] = idx

    new_history: list[dict[str, Any]] = []
    pruned_entries: list[dict[str, Any]] = []
    for idx, msg in enumerate(history):
        copied = dict(msg)
        if idx < protect_from and copied.get("role") == "tool":
            raw = copied.get("content")
            if isinstance(raw, str) and not ("event_seq=" in raw and "[已折叠" in raw):
                tname = str(
                    copied.get("name") or tool_names.get(str(copied.get("tool_call_id") or ""), "") or ""
                )
                if only_set is not None and tname not in only_set:
                    new_history.append(copied)
                    continue
                digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
                is_dup = len(raw) >= min_dup_chars and last_seen.get((tname, digest), -1) > idx
                reason = "duplicate" if is_dup else ("oversize" if len(raw) > min_prune_chars else "")
                if reason:
                    orig_chars = len(raw)
                    orig_tokens = estimate_tokens(raw)
                    copied["content"] = _format_pruned_tool_content(
                        raw,
                        reason=reason,
                        tool_name=tname,
                        orig_chars=orig_chars,
                        orig_tokens=orig_tokens,
                        event_seq=event_seq,
                    )
                    pruned_entries.append(
                        {
                            "index": idx,
                            "tool_call_id": str(copied.get("tool_call_id") or ""),
                            "tool_name": tname,
                            "reason": reason,
                            "orig_chars": orig_chars,
                            "orig_tokens": orig_tokens,
                            "content": raw,
                        }
                    )
        new_history.append(copied)

    orig_tokens_total = estimate_messages_tokens(history)
    new_tokens_total = estimate_messages_tokens(new_history)
    reclaimed = max(0, orig_tokens_total - new_tokens_total)
    return new_history, {
        "reclaimed_tokens": reclaimed,
        "pruned_entries": pruned_entries,
        "protect_from": protect_from,
    }


def extract_exact_anchors(
    messages: list[dict[str, Any]],
    *,
    prior_anchors: dict[str, list[str]] | None = None,
    redact: Any = None,
) -> dict[str, list[str]]:
    """正则抽取必须零失真保留的精确锚点（URL、路径、异常名）。"""
    urls: list[str] = []
    files: list[str] = []
    errors: list[str] = []

    if isinstance(prior_anchors, dict):
        for item in prior_anchors.get("urls") or []:
            if isinstance(item, str) and item and item not in urls:
                urls.append(item)
        for item in prior_anchors.get("files") or []:
            if isinstance(item, str) and item and item not in files:
                files.append(item)
        for item in prior_anchors.get("errors") or []:
            if isinstance(item, str) and item and item not in errors:
                errors.append(item)

    def _collect_text(text: str) -> None:
        if not text:
            return
        safe_text = _apply_redact(redact, text)
        for m in _URL_RE.findall(safe_text):
            cleaned = m.rstrip(".,;:!?)`'\">")
            if cleaned and cleaned not in urls and len(urls) < 12:
                urls.append(cleaned)
        without_urls = _URL_RE.sub(" ", safe_text)
        for m in _PATH_RE.findall(without_urls):
            cleaned = m.strip(".,;:!?)`'\"")
            if (
                cleaned
                and not cleaned.startswith("//")
                and len(cleaned) >= 4
                and cleaned not in files
                and len(files) < 16
            ):
                files.append(cleaned)
        for m in _ERROR_RE.findall(safe_text):
            if m and m not in errors and len(errors) < 10:
                errors.append(m)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            _collect_text(content)
        for call in msg.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") or {}
                args = fn.get("arguments") if isinstance(fn, dict) else call.get("arguments")
                if isinstance(args, str):
                    _collect_text(args)

    return {"urls": urls, "files": files, "errors": errors}


def extract_verbatim_user_messages(
    messages: list[dict[str, Any]],
    *,
    prior_messages: list[str] | None = None,
    redact: Any = None,
    limit: int = 8,
) -> list[str]:
    """原样提取未被内部注入包裹的真实用户诉求，防止经多次压缩后偏离初衷。"""
    collected: list[str] = []
    if isinstance(prior_messages, list):
        for item in prior_messages:
            if isinstance(item, str) and item.strip() and item not in collected:
                collected.append(item.strip()[:240])
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        raw = str(msg.get("content") or "").strip()
        if not raw or raw.startswith(_INTERNAL_USER_PREFIXES):
            continue
        cleaned = _apply_redact(redact, re.sub(r"\s+", " ", raw))[:240]
        if cleaned and cleaned not in collected:
            collected.append(cleaned)
    if len(collected) > limit:
        return [collected[0]] + collected[-(limit - 1) :]
    return collected


def build_static_handoff(
    messages: list[dict[str, Any]],
    *,
    previous_handoff: str = "",
    anchors: dict[str, list[str]] | None = None,
    user_messages: list[str] | None = None,
    tools: list[str] | None = None,
    highlights: list[str] | None = None,
    redact: Any = None,
) -> str:
    """当 LLM 交班总结不可用或失败时的本地结构化回退交班单。"""
    asks = user_messages if user_messages is not None else extract_verbatim_user_messages(messages, redact=redact)
    found_anchors = anchors if anchors is not None else extract_exact_anchors(messages, redact=redact)

    used_tools: list[str] = list(tools or [])
    notes: list[str] = list(highlights or [])
    if not used_tools or not notes:
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                tname = str((fn.get("name") if isinstance(fn, dict) else "") or call.get("func_name") or "").strip()
                if tname and tname not in used_tools and len(used_tools) < 8:
                    used_tools.append(tname)
            text = _apply_redact(redact, re.sub(r"\s+", " ", str(msg.get("content") or "")).strip())
            if text and len(notes) < 3:
                notes.append(text[:140])

    parts: list[str] = []
    goal_line = asks[0] if asks else "延续既有线程任务"
    parts.append(f"## 目标与约束\n{goal_line}")
    if len(asks) > 1:
        parts[-1] += f"\n补充要求：{'；'.join(asks[-2:])}"

    actions = [f"调用工具：{', '.join(used_tools)}"] if used_tools else []
    actions.extend(notes[-2:])
    parts.append("## 已完成动作\n" + ("\n".join(f"- {item}" for item in actions) if actions else "- 历史步进已归档"))

    state_lines: list[str] = []
    if previous_handoff.strip():
        prev_brief = _apply_redact(redact, re.sub(r"\s+", " ", previous_handoff.strip()))[:420]
        state_lines.append(f"合并前次交班：{prev_brief}")
    state_lines.append("已归档旧轮次存于 thread_events，必要时调用 thread_read(query/event_seq) 查原貌。")
    parts.append("## 当前状态与待办\n" + "\n".join(state_lines))

    anchor_items: list[str] = []
    if found_anchors.get("files"):
        anchor_items.append("文件：" + ", ".join(found_anchors["files"][:6]))
    if found_anchors.get("urls"):
        anchor_items.append("链接：" + ", ".join(found_anchors["urls"][:4]))
    if found_anchors.get("errors"):
        anchor_items.append("异常：" + ", ".join(found_anchors["errors"][:4]))
    parts.append("## 关键发现与纠正\n" + ("\n".join(anchor_items) if anchor_items else "暂无额外锚点"))

    text = "\n".join(parts)
    return _apply_redact(redact, text)[:2200]


def _sanitize_thread_history_inner(text: str) -> str:
    return re.sub(r"</thread_history\s*>", "&lt;/thread_history&gt;", str(text), flags=re.IGNORECASE)


def format_thread_history_prompt(summary_raw: str) -> str:
    """将 thread.summary 解析并格式化为紧凑可读的 <thread_history> 前导上下文。"""
    if not summary_raw or not isinstance(summary_raw, str):
        return ""
    try:
        data = json.loads(summary_raw)
    except (TypeError, ValueError):
        return f"<thread_history>\n{_sanitize_thread_history_inner(summary_raw[:2400])}\n</thread_history>"
    if not isinstance(data, dict):
        return f"<thread_history>\n{_sanitize_thread_history_inner(summary_raw[:2400])}\n</thread_history>"

    handoff = str(data.get("handoff") or "").strip()
    if not handoff:
        # 兼容旧版导航索引格式（例如显式 max_chars 压缩）
        return "<thread_history>" + _sanitize_thread_history_inner(summary_raw) + "</thread_history>"

    sections: list[str] = [_sanitize_thread_history_inner(handoff)]
    user_msgs = [u for u in (data.get("user_messages") or []) if isinstance(u, str) and u.strip()]
    if user_msgs:
        sections.append(
            "## 原始诉求列表\n"
            + "\n".join(f"- {_sanitize_thread_history_inner(u)}" for u in user_msgs[:8])
        )

    anchors = data.get("anchors") or {}
    if isinstance(anchors, dict):
        anchor_lines: list[str] = []
        for key, label in (("files", "文件"), ("urls", "链接"), ("errors", "异常")):
            vals = [v for v in (anchors.get(key) or []) if isinstance(v, str) and v.strip()]
            if vals:
                anchor_lines.append(
                    f"- {label}: {_sanitize_thread_history_inner(', '.join(vals[:8]))}"
                )
        if anchor_lines:
            sections.append("## 精确锚点\n" + "\n".join(anchor_lines))

    events = [str(e) for e in (data.get("archive_events") or []) if isinstance(e, int)]
    note = _sanitize_thread_history_inner(str(data.get("note") or "").strip())
    nav = f"归档事件：{', '.join(events) if events else data.get('archive_event', '')}；回读工具：thread_read"
    if note:
        nav += f"（{note}）"
    sections.append(nav)
    return "<thread_history>\n" + "\n\n".join(sections) + "\n</thread_history>"


def is_context_overflow_error(error: Any) -> bool:
    """判断错误文本或异常是否属于模型提供商的上下文窗口超限报错。"""
    text = str(error or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _OVERFLOW_PATTERNS)
