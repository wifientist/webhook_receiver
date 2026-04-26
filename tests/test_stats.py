from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import stats


@pytest.mark.asyncio
async def test_record_increments_all_buckets(fake_redis):
    body = (
        b'{"id":"cfg","name":"w","type":"event","payload":'
        b'{"id":"e1","eventCode":"101","entityType":"WEBHOOK_EVENT_AP",'
        b'"severity":"WEBHOOK_SEVERITY_INFO"}}'
    )
    await stats.record(fake_redis, source="ruckus", status="accepted", body=body)

    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    hour = now.strftime("%Y-%m-%d-%H")

    # throughput counters
    assert await fake_redis.get("wh:stats:total:received") == b"1"
    assert await fake_redis.get(f"wh:stats:daily:{day}:received") == b"1"
    assert await fake_redis.get(f"wh:stats:hourly:{hour}:received") == b"1"

    # dim counters: type
    assert await fake_redis.get("wh:stats:total:type:event") == b"1"
    assert await fake_redis.get(f"wh:stats:daily:{day}:type:event") == b"1"
    assert await fake_redis.get(f"wh:stats:hourly:{hour}:type:event") == b"1"

    # dim counters: source / status
    assert await fake_redis.get("wh:stats:total:source:ruckus") == b"1"
    assert await fake_redis.get("wh:stats:total:status:accepted") == b"1"

    # nested payload dims
    assert await fake_redis.get("wh:stats:total:event_code:101") == b"1"
    assert await fake_redis.get("wh:stats:total:entity_type:WEBHOOK_EVENT_AP") == b"1"
    assert await fake_redis.get("wh:stats:total:severity:WEBHOOK_SEVERITY_INFO") == b"1"


@pytest.mark.asyncio
async def test_record_separates_accepted_from_duplicate(fake_redis):
    body = b'{"type":"event","payload":{"id":"e1"}}'
    await stats.record(fake_redis, "ruckus", "accepted", body)
    await stats.record(fake_redis, "ruckus", "duplicate", body)
    await stats.record(fake_redis, "ruckus", "duplicate", body)

    assert await fake_redis.get("wh:stats:total:status:accepted") == b"1"
    assert await fake_redis.get("wh:stats:total:status:duplicate") == b"2"
    # `received` includes both
    assert await fake_redis.get("wh:stats:total:received") == b"3"


@pytest.mark.asyncio
async def test_record_with_garbage_body_still_counts_throughput(fake_redis):
    """Even if the body can't be parsed, source/status/throughput still increment."""
    await stats.record(fake_redis, "ruckus", "accepted", b"not-json")
    assert await fake_redis.get("wh:stats:total:received") == b"1"
    assert await fake_redis.get("wh:stats:total:source:ruckus") == b"1"
    # but no `type` dim was extracted
    assert await fake_redis.get("wh:stats:total:type:event") is None


@pytest.mark.asyncio
async def test_snapshot_shape(fake_redis):
    body = b'{"type":"event","payload":{"id":"e1","eventCode":"101"}}'
    await stats.record(fake_redis, "ruckus", "accepted", body)
    await stats.record(fake_redis, "ruckus", "accepted", body)
    snap = await stats.snapshot(fake_redis)

    assert snap["running_totals"]["received"] == 2
    assert snap["running_totals"]["by_type"] == {"event": 2}
    assert snap["running_totals"]["by_event_code"] == {"101": 2}
    assert snap["running_totals"]["by_status"] == {"accepted": 2}

    assert snap["today"]["received"] == 2
    assert snap["today"]["by_type"] == {"event": 2}
    assert isinstance(snap["last_24h_hourly"], list)
    assert len(snap["last_24h_hourly"]) == 24
    # current hour should reflect both events
    assert snap["last_24h_hourly"][-1]["count"] == 2


@pytest.mark.asyncio
async def test_record_rejection_increments(fake_redis):
    await stats.record_rejection(fake_redis, "unknown_source")
    await stats.record_rejection(fake_redis, "unknown_source")
    await stats.record_rejection(fake_redis, "verification_failed")

    assert await fake_redis.get("wh:stats:total:rejected:received") == b"3"
    assert await fake_redis.get("wh:stats:total:rejection_reason:unknown_source") == b"2"
    assert await fake_redis.get("wh:stats:total:rejection_reason:verification_failed") == b"1"


@pytest.mark.asyncio
async def test_rejections_appear_in_snapshot(client, fake_redis, monkeypatch):
    """End-to-end: 401/404/413 each bump their own reason counter."""
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "ruckus_one_webhook_secret", "real")
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier("real"))

    # 404 unknown source
    await client.post("/webhook/nope", content=b'{"type":"event","payload":{}}')
    # 401 bad shape on a configured path
    await client.post("/webhook/ruckus", content=b'{"hello":"world"}',
                      headers={"Authorization": "real"})
    # 413 oversized
    monkeypatch.setattr(app_settings, "max_body_bytes", 50)
    await client.post("/webhook/ruckus", content=b"x" * 100,
                      headers={"Authorization": "real"})

    snap = (await client.get("/stats/counters")).json()
    rejections = snap["rejections"]["running_totals"]
    assert rejections["received"] == 3
    assert rejections["by_reason"] == {
        "unknown_source": 1,
        "verification_failed": 1,
        "body_too_large": 1,
    }


@pytest.mark.asyncio
async def test_endpoint_via_post_then_counters(client, fake_redis, monkeypatch):
    """End-to-end: POST a webhook, /stats/counters reflects it."""
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "ruckus_one_webhook_secret", None)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(None))

    body = b'{"type":"event","name":"w","payload":{"id":"e-1","eventCode":"101"}}'
    r = await client.post("/webhook/ruckus", content=body)
    assert r.status_code == 202

    counters = (await client.get("/stats/counters")).json()
    assert counters["running_totals"]["received"] == 1
    assert counters["running_totals"]["by_type"] == {"event": 1}
    assert counters["running_totals"]["by_event_code"] == {"101": 1}
    assert counters["running_totals"]["by_source"] == {"ruckus": 1}
    assert counters["running_totals"]["by_status"] == {"accepted": 1}
