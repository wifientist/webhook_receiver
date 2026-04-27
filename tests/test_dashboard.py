from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_dashboard_serves_html_when_auth_disabled(client):
    r = await client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "webhook_rx" in r.text


@pytest.mark.asyncio
async def test_events_recent_after_post(client, fake_redis, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "ruckus_one_webhook_secret", None)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(None))

    body = (
        b'{"id":"cfg","name":"w","type":"incident",'
        b'"payload":{"id":"inc-1","severity":"HIGH","title":"AP down"}}'
    )
    r = await client.post("/webhook/ruckus", content=body)
    assert r.status_code == 202
    delivery_id = json.loads(r.request.content)  # not used; keep linter happy

    listing = (await client.get("/events/recent?n=10")).json()
    events = listing["events"]
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "incident"
    assert e["severity"] == "HIGH"
    assert e["title"] == "AP down"
    assert e["category"] == "action"  # HIGH severity → action
    assert e["has_payload"] is True
    assert e["source"] == "ruckus"

    detail = await client.get(f"/events/{e['source']}/{e['delivery_id']}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["payload"]["id"] == "inc-1"


@pytest.mark.asyncio
async def test_events_recent_classifies_info_for_low_severity(client, fake_redis, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "ruckus_one_webhook_secret", None)
    from app import verifiers
    monkeypatch.setitem(verifiers._KNOWN, "ruckus", verifiers.RuckusOneVerifier(None))

    body = (
        b'{"id":"cfg","name":"w","type":"event",'
        b'"payload":{"id":"e-1","eventCode":"101","severity":"WEBHOOK_SEVERITY_INFO"}}'
    )
    await client.post("/webhook/ruckus", content=body)
    events = (await client.get("/events/recent")).json()["events"]
    assert events[0]["category"] == "info"


@pytest.mark.asyncio
async def test_event_detail_404_when_missing(client):
    r = await client.get("/events/ruckus/nonexistent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dashboard_redirects_to_login_when_unauthed(client, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "dashboard_password", "s3cret")

    r = await client.get("/dashboard")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_login_page_served_when_auth_enabled(client, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "dashboard_password", "s3cret")

    r = await client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Sign in" in r.text


@pytest.mark.asyncio
async def test_login_redirects_to_dashboard_when_auth_disabled(client):
    r = await client.get("/login")
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"


@pytest.mark.asyncio
async def test_events_recent_requires_session_when_password_set(client, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "dashboard_password", "s3cret")

    # No cookie → 401
    r = await client.get("/events/recent")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_then_dashboard_flow(client, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "dashboard_password", "s3cret")

    # Wrong password → 401
    r = await client.post("/api/login", json={"password": "wrong"})
    assert r.status_code == 401

    # Right password → 200 + sets a cookie
    r = await client.post("/api/login", json={"password": "s3cret"})
    assert r.status_code == 200
    assert "wh_session" in r.cookies

    # Cookie now lets us hit the JSON endpoint
    r = await client.get("/events/recent")
    assert r.status_code == 200

    # And /dashboard serves HTML
    r = await client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]

    # Logout clears the cookie
    r = await client.post("/api/logout")
    assert r.status_code == 200
    # httpx mirrors Set-Cookie from the response into the client jar — after
    # delete_cookie the value is empty, so the next request goes through
    # without a valid session.
    r = await client.get("/events/recent")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_password_rotation_invalidates_existing_cookies(client, monkeypatch):
    from app.config import settings as app_settings
    monkeypatch.setattr(app_settings, "dashboard_password", "old")

    r = await client.post("/api/login", json={"password": "old"})
    assert r.status_code == 200
    r = await client.get("/events/recent")
    assert r.status_code == 200

    # Rotate the password — same cookie, different expected token.
    monkeypatch.setattr(app_settings, "dashboard_password", "new")
    r = await client.get("/events/recent")
    assert r.status_code == 401
