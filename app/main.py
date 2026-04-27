from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import events, stats
from app.auth import (
    COOKIE_MAX_AGE,
    COOKIE_NAME,
    auth_disabled,
    is_authed,
    make_cookie_value,
    password_ok,
    require_session,
)
from app.config import settings
from app.receiver import enqueue
from app.redis_client import close_redis, get_redis
from app.schemas import EnqueueResponse, HealthResponse, StatsResponse
from app.verifiers import resolve_verifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("webhook_rx")


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = get_redis()
    try:
        await redis.ping()
        log.info("connected to redis %s", settings.redis_url)
    except Exception as exc:
        log.error("redis ping failed at startup: %s", exc)
    if settings.dashboard_password:
        log.info("dashboard auth: ENABLED (password set)")
    else:
        log.warning(
            "dashboard auth: DISABLED — set DASHBOARD_PASSWORD in .env "
            "to gate /dashboard and /events/*"
        )
    yield
    await close_redis()


app = FastAPI(title="webhook_rx", lifespan=lifespan)

_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


async def _safe_record_rejection(redis, reason: str) -> None:
    """Best-effort: never let stats failures break the rejection path."""
    try:
        await stats.record_rejection(redis, reason)
    except Exception as exc:
        log.warning("stats.record_rejection failed reason=%s: %s", reason, exc)


@app.post("/webhook/{source}", response_model=EnqueueResponse, status_code=202)
async def webhook(source: str, request: Request):
    redis = get_redis()
    too_large = f"body too large (limit {settings.max_body_bytes} bytes)"

    # Reject oversized bodies before reading them when Content-Length tells us.
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            cl = int(content_length)
        except ValueError:
            await _safe_record_rejection(redis, "invalid_content_length")
            raise HTTPException(status_code=400, detail="invalid content-length")
        if cl > settings.max_body_bytes:
            await _safe_record_rejection(redis, "body_too_large")
            raise HTTPException(status_code=413, detail=too_large)

    verifier = resolve_verifier(source)
    if verifier is None:
        # Don't acknowledge unknown paths — keep the surface area small.
        await _safe_record_rejection(redis, "unknown_source")
        raise HTTPException(status_code=404, detail="not found")

    body = await request.body()
    if len(body) > settings.max_body_bytes:
        await _safe_record_rejection(redis, "body_too_large")
        raise HTTPException(status_code=413, detail=too_large)

    if not verifier.verify(request.headers, body):
        log.warning("verification failed source=%s", source)
        await _safe_record_rejection(redis, "verification_failed")
        raise HTTPException(status_code=401, detail="rejected")

    status, event_id, delivery_id = await enqueue(
        redis, source, request.headers, body, verifier
    )
    log.info(
        "enqueued source=%s event_id=%s delivery=%s status=%s",
        source, event_id, delivery_id[:12], status,
    )
    # Counters are accounting, not delivery — failure here must not break the
    # request. Worst case we miss an increment and notice in /stats/counters.
    try:
        await stats.record(redis, source, status, body)
    except Exception as exc:
        log.warning("stats.record failed source=%s: %s", source, exc)
    return EnqueueResponse(status=status, event_id=event_id, source=source)


@app.get("/healthz", response_model=HealthResponse)
async def healthz():
    try:
        ok = bool(await get_redis().ping())
    except Exception:
        ok = False
    return HealthResponse(ok=ok, redis=ok)


@app.get("/stats", response_model=StatsResponse)
async def stats_queue():
    r = get_redis()
    length = await r.xlen(settings.stream_key)
    try:
        summary = await r.xpending(settings.stream_key, settings.consumer_group)
        pending = summary["pending"] if isinstance(summary, dict) else summary[0]
    except Exception:
        pending = 0
    try:
        dead = await r.xlen(settings.dead_stream_key)
    except Exception:
        dead = 0
    return StatsResponse(
        stream_key=settings.stream_key,
        length=length,
        pending=int(pending or 0),
        dead_length=dead,
    )


@app.get("/stats/counters")
async def stats_counters():
    return await stats.snapshot(get_redis())


class LoginRequest(BaseModel):
    password: str


@app.get("/login")
async def login_page(token: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    # If auth is off, or the user is already in, send them straight to the dashboard.
    if auth_disabled() or is_authed(token):
        return RedirectResponse("/dashboard", status_code=303)
    page = _STATIC_DIR / "login.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="login page missing")
    return FileResponse(page, media_type="text/html")


@app.post("/api/login")
async def api_login(payload: LoginRequest):
    if not password_ok(payload.password):
        raise HTTPException(status_code=401, detail="bad password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME,
        make_cookie_value(payload.password),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@app.get("/dashboard")
async def dashboard(token: str | None = Cookie(default=None, alias=COOKIE_NAME)):
    if not is_authed(token):
        return RedirectResponse("/login", status_code=303)
    index = _STATIC_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="dashboard not built")
    return FileResponse(index, media_type="text/html")


@app.get("/events/recent", dependencies=[Depends(require_session)])
async def events_recent(n: int = 50):
    return {"events": await events.recent(get_redis(), n=n)}


@app.get(
    "/events/{source}/{delivery_id}",
    dependencies=[Depends(require_session)],
)
async def event_detail(source: str, delivery_id: str):
    data = await events.get_payload_json(get_redis(), source, delivery_id)
    if data is None:
        raise HTTPException(status_code=404, detail="payload expired or unknown")
    return data
