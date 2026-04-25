from __future__ import annotations

import pytest

from app.config import settings


@pytest.mark.asyncio
async def test_unknown_source_returns_404(client, fake_redis):
    resp = await client.post(
        "/webhook/not-a-configured-source",
        content=b'{"type":"event","payload":{"id":"x"}}',
    )
    assert resp.status_code == 404
    assert await fake_redis.xlen(settings.stream_key) == 0


@pytest.mark.asyncio
async def test_oversized_body_returns_413(client, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "max_body_bytes", 100)
    resp = await client.post(
        "/webhook/ruckus",
        content=b"x" * 200,
    )
    assert resp.status_code == 413
    assert await fake_redis.xlen(settings.stream_key) == 0


@pytest.mark.asyncio
async def test_ruckus_authorized_uses_payload_id(client, fake_redis, monkeypatch):
    secret = "r1-secret"
    monkeypatch.setattr(settings, "ruckus_one_webhook_secret", secret)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(secret))

    body = (
        b'{"id":"webhook-config-1","name":"webhook-1","type":"event",'
        b'"payload":{"id":"38d4a8eb-2dd2-44dd-beaa-70a96b657576",'
        b'"eventCode":"101","tenantId":"t1"}}'
    )
    resp = await client.post(
        "/webhook/ruckus",
        content=body,
        headers={"Authorization": secret, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202
    # event_id MUST come from payload.id, not the top-level config id
    assert resp.json()["event_id"] == "38d4a8eb-2dd2-44dd-beaa-70a96b657576"


@pytest.mark.asyncio
async def test_ruckus_bearer_prefix_accepted(client, fake_redis, monkeypatch):
    secret = "r1-secret"
    monkeypatch.setattr(settings, "ruckus_one_webhook_secret", secret)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(secret))

    resp = await client.post(
        "/webhook/ruckus",
        content=b'{"id":"cfg","name":"w","type":"activity","payload":{"id":"a-1"}}',
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 202


@pytest.mark.asyncio
async def test_ruckus_bad_secret_rejected(client, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "ruckus_one_webhook_secret", "right")
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier("right"))

    resp = await client.post(
        "/webhook/ruckus",
        content=b'{"type":"event","name":"w","payload":{"id":"x"}}',
        headers={"Authorization": "wrong"},
    )
    assert resp.status_code == 401
    assert await fake_redis.xlen(settings.stream_key) == 0


@pytest.mark.asyncio
async def test_ruckus_admin_event_synthesizes_id(client, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "ruckus_one_webhook_secret", None)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(None))

    body = (
        b'{"id":"cfg","name":"w","type":"event","payload":{'
        b'"eventCode":"login-001","tenantId":"t1","timestamp":"1736818174",'
        b'"entityType":"WEBHOOK_EVENT_ADMIN"}}'
    )
    resp = await client.post("/webhook/ruckus", content=body)
    assert resp.status_code == 202
    assert resp.json()["event_id"] == "t1:login-001:1736818174"


@pytest.mark.asyncio
async def test_ruckus_non_ruckus_shape_rejected(client, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "ruckus_one_webhook_secret", None)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(None))

    # Valid JSON but missing Ruckus-specific keys
    resp = await client.post("/webhook/ruckus", content=b'{"hello":"world"}')
    assert resp.status_code == 401

    # Wrong `type` value
    resp = await client.post(
        "/webhook/ruckus",
        content=b'{"type":"spam","payload":{}}',
    )
    assert resp.status_code == 401

    # Missing payload
    resp = await client.post(
        "/webhook/ruckus",
        content=b'{"type":"event"}',
    )
    assert resp.status_code == 401

    # Not JSON at all
    resp = await client.post("/webhook/ruckus", content=b"not json")
    assert resp.status_code == 401

    assert await fake_redis.xlen(settings.stream_key) == 0


@pytest.mark.asyncio
async def test_ruckus_byte_identical_is_duplicate(client, fake_redis, monkeypatch):
    """Provider re-fires the exact same body — that's a true retry, dedup'd."""
    monkeypatch.setattr(settings, "ruckus_one_webhook_secret", None)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(None))

    body = b'{"id":"cfg","name":"w","type":"event","payload":{"id":"dup-1"}}'
    r1 = await client.post("/webhook/ruckus", content=body)
    r2 = await client.post("/webhook/ruckus", content=body)
    assert r1.json()["status"] == "accepted"
    assert r2.json()["status"] == "duplicate"
    assert await fake_redis.xlen(settings.stream_key) == 1


@pytest.mark.asyncio
async def test_ruckus_status_transition_is_not_dedup(client, fake_redis, monkeypatch):
    """Same payload.id, different `status` — distinct deliveries, both accepted.
    This is the core reason we hash the body instead of dedup'ing on payload.id."""
    monkeypatch.setattr(settings, "ruckus_one_webhook_secret", None)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(None))

    in_progress = (
        b'{"id":"cfg","name":"w","type":"activity","payload":'
        b'{"id":"act-1","status":"IN_PROGRESS"}}'
    )
    success = (
        b'{"id":"cfg","name":"w","type":"activity","payload":'
        b'{"id":"act-1","status":"SUCCESS"}}'
    )
    r1 = await client.post("/webhook/ruckus", content=in_progress)
    r2 = await client.post("/webhook/ruckus", content=success)
    assert r1.json()["status"] == "accepted"
    assert r2.json()["status"] == "accepted"
    # both share the same logical event_id...
    assert r1.json()["event_id"] == r2.json()["event_id"] == "act-1"
    # ...but the queue holds both deliveries because the bodies differ
    assert await fake_redis.xlen(settings.stream_key) == 2


@pytest.mark.asyncio
async def test_headers_are_not_persisted(client, fake_redis, monkeypatch):
    monkeypatch.setattr(settings, "ruckus_one_webhook_secret", None)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(None))

    await client.post(
        "/webhook/ruckus",
        content=b'{"id":"cfg","name":"w","type":"event","payload":{"id":"h-1"}}',
        headers={"Authorization": "something-sensitive", "X-Secret": "leak-me"},
    )
    # No wh:headers:* key should exist — headers are discarded after verification.
    keys = [k async for k in fake_redis.scan_iter(match=b"wh:headers:*")]
    assert keys == []


@pytest.mark.asyncio
async def test_healthz(client, fake_redis):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
