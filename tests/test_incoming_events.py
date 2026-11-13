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
