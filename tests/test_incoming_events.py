import hashlib
import hmac
import json
import time

import pytest
from test_foundation import loaded


def signed(secret, **event):
    body = json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_signed_event_creates_only_intent_and_replays_do_not_duplicate(tmp_path):
    plugin = await loaded(tmp_path)
    secret = "event-test-private-key-value"
    plugin.vault.set("event_key", secret)
    config = {
        "news": {
            "target": "chat:chat",
            "kind": "proactive",
            "secret_handle": "event_key",
        }
    }
    try:
        current = await plugin.manage_read("webhooks")
        saved = await plugin.manage_write(
            "webhooks",
            json.dumps(
                {"configuration": config, "expected_version": current["version"]}
            ),
        )
        assert saved["success"], saved
        event = {
            "channel": "news",
            "event_id": "event-1",
            "timestamp": int(time.time()),
            "payload": '{"reason":"发现一条新消息"}',
        }
        accepted = await plugin.incoming_event(
            **event, signature=signed(secret, **event)
        )
        assert accepted["success"], accepted
        repeated = await plugin.incoming_event(
            **event, signature=signed(secret, **event)
        )
        assert repeated["intent_id"] == accepted["intent_id"]
        row = plugin.intents.get(accepted["intent_id"])
        assert (
            row["source"] == "webhook"
            and row["status"] == "pending"
            and row["target"] == "chat:chat"
        )
        denied = await plugin.incoming_event(**event, signature="0" * 64)
        assert not denied["success"]
        old = {**event, "event_id": "expired", "timestamp": int(time.time()) - 400}
        assert not (
            await plugin.incoming_event(**old, signature=signed(secret, **old))
        )["success"]
        changed = {**event, "payload": '{"reason":"different"}'}
        assert not (
            await plugin.incoming_event(**changed, signature=signed(secret, **changed))
        )["success"]
        assert secret not in str([accepted, repeated, denied])
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_webhook_accepts_stream_spec_target_and_resolves_runtime_session(tmp_path):
    plugin = await loaded(tmp_path)
    secret = "event-stream-spec-secret-value"
    plugin.vault.set("spec_key", secret)
    plugin.messages.upsert_route(
        "c0300b39c80870992aca16d9fcb724c5",
        platform="qq",
        chat_type="group",
        group_id="100246809",
    )
    plugin.settings.enabled_chats = ["qq:group:100246809"]
    config = {
        "rss": {
            "target": "qq:group:100246809",
            "kind": "proactive",
            "secret_handle": "spec_key",
        }
    }
    try:
        current = await plugin.manage_read("webhooks")
        saved = await plugin.manage_write(
            "webhooks",
            json.dumps({"configuration": config, "expected_version": current["version"]}),
        )
        assert saved["success"], saved
        event = {
            "channel": "rss",
            "event_id": "rss-1",
            "timestamp": int(time.time()),
            "payload": '{"reason":"仓库发布了新版本"}',
        }
        accepted = await plugin.incoming_event(
            **event, signature=signed(secret, **event)
        )
        assert accepted["success"], accepted
        row = plugin.intents.get(accepted["intent_id"])
        assert row["target"] == "chat:c0300b39c80870992aca16d9fcb724c5"
    finally:
        await plugin.on_unload()
