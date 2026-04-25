from __future__ import annotations

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

import app.redis_client as redis_module
from app.main import app


@pytest.fixture
def fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=False)
    monkeypatch.setattr(redis_module, "_client", fake)
    monkeypatch.setattr(redis_module, "get_redis", lambda: fake)
    # app.main imported get_redis at module load; patch the binding there too.
    import app.main as main_module
    monkeypatch.setattr(main_module, "get_redis", lambda: fake)
    return fake


@pytest.fixture
async def client(fake_redis):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
