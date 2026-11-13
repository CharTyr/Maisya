"""宿主认证 API 接收签名事件，转换为原有 Intent。"""

import hashlib
import hmac
import json
import re
import time

from .intents import CHAT_TARGET
from .memory import MemoryLayers
from .workspaces import completed_thread


class Incoming:
    def __init__(self, plugin):
        self.plugin = plugin
        self.path = plugin.data_root / "webhooks.json"

    def load(self):
        raw = MemoryLayers._bytes(self.path)
        config = json.loads(raw) if raw else {}
        self.validate(config)
        return config

    def validate(self, config):
        if not isinstance(config, dict) or len(config) > 16:
            raise ValueError("最多登记 16 个事件通道")
        for name, channel in config.items():
            self.plugin.vault._validate_handle(name)
            if not isinstance(channel, dict) or set(channel) - {
                "target",
                "kind",
                "secret_handle",
                "thread_id",
                "enabled",
            }:
                raise ValueError("事件通道含未知字段")
            if not CHAT_TARGET.fullmatch(channel.get("target", "")) or channel.get(
                "kind"
            ) not in {"proactive", "silent"}:
                raise ValueError("事件通道需要固定目标与 proactive/silent 类型")
            self.plugin.vault._validate_handle(channel.get("secret_handle", ""))
            if channel.get("kind") == "silent":
                chat = channel["target"].split(":", 1)[1]
                row = self.plugin.threads.get(
                    channel.get("thread_id", ""), chat, allow_self=chat == "self"
                )
                if row["chat_id"] != chat or not self.plugin._activity_permitted(
                    {**row, "grant": {}}
                ):
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
        target_chat = binding["target"].split(":", 1)[1]
        platform = await completed_thread(
            self.plugin.messages.platform_for, target_chat
        )
        intent = await completed_thread(
            self.plugin.intents.schedule,
            kind=binding["kind"],
            source="webhook",
            target=binding["target"],
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
