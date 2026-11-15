"""宿主认证 API 接收签名事件，转换为原有 Intent。"""

import asyncio
import hashlib
import hmac
import inspect
import json
import re
import time

from .intents import CHAT_TARGET, parse_stream_spec
from .memory import MemoryLayers
from .workspaces import completed_thread


def _extract_target_chat(raw_target: str) -> str:
    text = str(raw_target or "").strip()
    if text.lower().startswith("chat:"):
        return text[5:].strip()
    return text


def _valid_webhook_target(raw_target: str) -> bool:
    text = str(raw_target or "").strip()
    if not text:
        return False
    return bool(
        CHAT_TARGET.fullmatch(text)
        or parse_stream_spec(text) is not None
        or text == "self"
    )


class Incoming:
    def __init__(self, plugin):
        self.plugin = plugin
        self.path = plugin.data_root / "webhooks.json"
        self._lock = asyncio.Lock()

    def load(self):
        raw = MemoryLayers._bytes(self.path)
        config = json.loads(raw) if raw else {}
        self.validate(config)
        return config

    def validate(self, config, *, strict_enabled=False):
        if not isinstance(config, dict) or len(config) > 16:
            raise ValueError("最多登记 16 个事件通道")
        for name, channel in config.items():
            self.plugin.vault._validate_handle(name)
            if not isinstance(channel, dict) or set(channel) - {
                "target",
                "kind",
                "secret_handle",
                "thread_id",
                "profile",
                "workspace",
                "enabled",
            }:
                raise ValueError("事件通道含未知字段")
            if not _valid_webhook_target(channel.get("target", "")) or channel.get(
                "kind"
            ) not in {"proactive", "silent"}:
                raise ValueError("事件通道需要固定目标（如 qq:group:12345678）与 proactive/silent 类型")
            self.plugin.vault._validate_handle(channel.get("secret_handle", ""))
            has_profile = bool(channel.get("profile"))
            has_thread = bool(channel.get("thread_id"))
            if has_profile and has_thread:
                raise ValueError("事件通道不能同时指定 profile 和 thread_id")
            if channel.get("workspace"):
                if not has_profile:
                    raise ValueError("事件通道指定 workspace 时必须同时指定 profile")
                if not isinstance(channel["workspace"], str) or not re.fullmatch(
                    r"[a-z0-9][a-z0-9_-]{0,47}", channel["workspace"]
                ):
                    raise ValueError("事件通道工作区编号格式无效")
            if has_profile:
                ability = self.plugin.profiles.get(channel["profile"])
                if strict_enabled and not ability.enabled:
                    raise ValueError("事件通道绑定的角色已停用")
                allowed = (
                    ability.autonomous if strict_enabled else ability.autonomous_config
                )
                if not allowed:
                    raise ValueError("事件通道绑定的角色必须允许自主使用")
            elif channel.get("kind") == "silent":
                chat_token = _extract_target_chat(channel["target"])
                chat = (
                    "self"
                    if chat_token == "self"
                    else (self.plugin.resolve_known_session_id(chat_token) or chat_token)
                )
                row = self.plugin.threads.get(
                    channel.get("thread_id", ""), chat, allow_self=chat == "self"
                )
                row_aliases = (
                    self.plugin.chat_aliases_for(row["chat_id"], row.get("platform", ""))
                    if row.get("chat_id")
                    else set()
                )
                if (
                    row["chat_id"] != chat and chat_token not in row_aliases
                ) or not self.plugin._activity_permitted({**row, "grant": {}}):
                    raise ValueError("外部续接只允许无需真实用户授权的原目标线程")

    async def receive(self, channel, event_id, timestamp, payload, signature):
        if not isinstance(event_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", event_id
        ):
            raise ValueError("事件编号无效")
        if type(timestamp) is not int or abs(time.time() - timestamp) > 300:
            raise PermissionError("事件时间超出五分钟窗口")
        if (
            not isinstance(payload, str)
            or len(payload.encode()) > 16000
            or not isinstance(signature, str)
            or not re.fullmatch(r"[a-f0-9]{64}", signature)
        ):
            raise ValueError("签名或事件内容无效")
        config = await completed_thread(self.load)
        binding = config.get(channel)
        if not binding or binding.get("enabled") is False:
            raise PermissionError("事件通道未启用")
        secret = await completed_thread(
            self.plugin.vault.for_execution, binding["secret_handle"]
        )
        canonical = json.dumps(
            {
                "channel": channel,
                "event_id": event_id,
                "timestamp": timestamp,
                "payload": payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        expected = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise PermissionError("事件签名不正确")
        event = json.loads(payload)
        if not isinstance(event, dict) or set(event) - {"reason", "data"}:
            raise ValueError("事件内容仅接受 reason 和 data")
        digest = hashlib.sha256(
            json.dumps(
                event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        key = hashlib.sha256((channel + "\n" + event_id).encode()).hexdigest()
        raw_target_chat = _extract_target_chat(binding["target"])
        parsed_spec = parse_stream_spec(raw_target_chat)
        target_chat = (
            "self"
            if raw_target_chat == "self"
            else (self.plugin.resolve_known_session_id(raw_target_chat) or raw_target_chat)
        )
        platform = (
            await completed_thread(self.plugin.messages.platform_for, target_chat)
            if target_chat != "self"
            else "self"
        )
        if not platform and parsed_spec is not None:
            platform = parsed_spec[0]
        intent_target = f"chat:{target_chat}"
        async with self._lock:
            existing = await completed_thread(
                self.plugin.intents.event_exists, key, digest
            )
            if existing is not None:
                return {
                    "success": True,
                    "intent_id": existing["intent_id"],
                    "status": existing["status"],
                    **(
                        {"thread_id": existing["thread_id"]}
                        if existing.get("thread_id")
                        else {}
                    ),
                }
            if binding.get("profile"):
                ability = await completed_thread(
                    self.plugin.profiles.get, binding["profile"]
                )
                self.plugin.runtime.available(ability)
                if not ability.autonomous:
                    raise PermissionError("事件通道绑定的角色不允许自主执行")
                redacted_event = self.plugin.vault.redact(event)
                reason_text = (
                    self.plugin.vault.redact(event.get("reason", ""))
                    or f"处理外部事件 {channel}"
                )
                prompt = (
                    reason_text
                    + "\n<external_event>"
                    + json.dumps(redacted_event, ensure_ascii=False)
                    + "</external_event>"
                )
                self.plugin.engine._validate_input(prompt, 0)
                intent_reason = reason_text.strip()[:500] or f"处理外部事件 {channel}"
                if binding["kind"] == "proactive":
                    quota_fn = self.plugin.intents.proactive_quota_available
                    params = inspect.signature(quota_fn).parameters
                    if "target" in params:
                        quota_ok = await completed_thread(quota_fn, intent_target)
                    else:
                        quota_ok = await completed_thread(quota_fn)
                    if not quota_ok:
                        raise ValueError("普通惦记已达到每日上限")
                run_chat = "self" if target_chat == "self" else target_chat
                started = await self.plugin.engine.start(
                    ability,
                    prompt,
                    chat_id=run_chat,
                    platform="self" if target_chat == "self" else (platform or "webhook"),
                    user_id="",
                    owner="self" if target_chat == "self" else "chat",
                    wait_seconds=0,
                    workspace=binding.get("workspace", ""),
                    background=True,
                    defer_if_full=True,
                )
                payload_data = {
                    "event": redacted_event,
                    "platform": platform,
                    **(
                        {"wait_thread": True}
                        if binding["kind"] == "proactive"
                        else {"already_started": True}
                    ),
                }
                try:
                    intent = await completed_thread(
                        self.plugin.intents.schedule,
                        kind=binding["kind"],
                        source="webhook",
                        target=intent_target,
                        due_at=self.plugin.intents.clock(),
                        reason=intent_reason,
                        thread_id=started["thread_id"],
                        payload=payload_data,
                        event_key=key,
                        event_digest=digest,
                    )
                except Exception:
                    try:
                        await self.plugin.engine.stop(
                            started["thread_id"], chat_id=run_chat
                        )
                    except Exception:
                        pass
                    raise
                return {
                    "success": True,
                    "intent_id": intent["intent_id"],
                    "status": intent["status"],
                    "thread_id": started["thread_id"],
                }
            intent = await completed_thread(
                self.plugin.intents.schedule,
                kind=binding["kind"],
                source="webhook",
                target=intent_target,
                due_at=self.plugin.intents.clock(),
                reason=self.plugin.vault.redact(event.get("reason", "")),
                thread_id=binding.get("thread_id", ""),
                payload={"event": self.plugin.vault.redact(event), "platform": platform},
                event_key=key,
                event_digest=digest,
            )
            return {
                "success": True,
                "intent_id": intent["intent_id"],
                "status": intent["status"],
            }
