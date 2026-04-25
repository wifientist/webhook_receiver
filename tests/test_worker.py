from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.config import settings
from worker import worker as worker_mod


@pytest.fixture
def fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    import app.redis_client as redis_module
    monkeypatch.setattr(redis_module, "_client", fake)
    monkeypatch.setattr(redis_module, "get_redis", lambda: fake)
    monkeypatch.setattr(worker_mod, "get_redis", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_process_entry_dispatches(fake_redis, monkeypatch):
    called: list[tuple[str, bytes]] = []

    async def capture(event_id: str, body: bytes) -> None:
        called.append((event_id, body))

    monkeypatch.setitem(
        __import__("worker.handlers", fromlist=["HANDLERS"]).HANDLERS,
        "test",
        capture,
    )

    await fake_redis.set("wh:payload:test:e1", b'{"ok":true}')
    ok = await worker_mod._process_entry(
        fake_redis,
        b"1-0",
        {b"source": b"test", b"event_id": b"e1"},
    )
    assert ok is True
    assert called == [("e1", b'{"ok":true}')]


@pytest.mark.asyncio
async def test_process_entry_missing_payload_acks(fake_redis):
    ok = await worker_mod._process_entry(
        fake_redis,
        b"1-0",
        {b"source": b"gone", b"event_id": b"missing"},
    )
    assert ok is True  # ack to not loop forever


@pytest.mark.asyncio
async def test_handler_failure_returns_false(fake_redis, monkeypatch):
    async def boom(event_id: str, body: bytes) -> None:
        raise RuntimeError("handler blew up")

    monkeypatch.setitem(
        __import__("worker.handlers", fromlist=["HANDLERS"]).HANDLERS,
        "bad",
        boom,
    )
    await fake_redis.set("wh:payload:bad:e2", b"x")
    ok = await worker_mod._process_entry(
        fake_redis,
        b"1-0",
        {b"source": b"bad", b"event_id": b"e2"},
    )
    assert ok is False  # leave pending for retry


@pytest.mark.asyncio
async def test_ensure_group_idempotent(fake_redis):
    await worker_mod._ensure_group(fake_redis)
    await worker_mod._ensure_group(fake_redis)  # should not raise (BUSYGROUP swallowed)
    info = await fake_redis.xinfo_groups(settings.stream_key)
    assert len(info) == 1
