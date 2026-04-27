from __future__ import annotations

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

import app.redis_client as redis_module
from app.config import settings as app_settings
from app.main import app


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Pin dashboard auth to OFF by default. Individual tests opt into auth
    by setting `dashboard_password` themselves. Without this, a developer's
    real .env (with DASHBOARD_PASSWORD set) leaks into the test harness."""
    monkeypatch.setattr(app_settings, "dashboard_password", None)


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
