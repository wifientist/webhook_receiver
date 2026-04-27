from __future__ import annotations

import hmac
import logging
from hashlib import sha256
from typing import Annotated

from fastapi import Cookie, HTTPException, status

from app.config import settings

log = logging.getLogger(__name__)

COOKIE_NAME = "wh_session"
COOKIE_MAX_AGE = 7 * 24 * 3600
# Fixed string the cookie token is HMAC'd over. Bumping this version forces
# every existing cookie to re-auth (rotate if you ever need to invalidate
# all sessions without changing the password).
_PROOF = b"webhook_rx-dashboard-v1"


def make_cookie_value(password: str) -> str:
    """HMAC-SHA256(password, fixed-proof). Anyone who knows the password can
    produce the value; rotating `DASHBOARD_PASSWORD` invalidates every
    outstanding cookie automatically — no separate signing secret to manage.
    """
    return hmac.new(password.encode(), _PROOF, sha256).hexdigest()


def _expected_token() -> str | None:
    pw = settings.dashboard_password
    return make_cookie_value(pw) if pw else None


def password_ok(password: str) -> bool:
    expected = settings.dashboard_password
    if not expected:
        # Auth disabled — only a non-empty submission "logs in", to give the
        # login page some signal in dev. In practice the dashboard skips the
        # form entirely when auth is off.
        return bool(password)
    return hmac.compare_digest(password.encode(), expected.encode())


def is_authed(token: str | None) -> bool:
    expected = _expected_token()
    if expected is None:
        return True  # auth disabled
    if not token:
        return False
    return hmac.compare_digest(token, expected)


def auth_disabled() -> bool:
    return not settings.dashboard_password


def require_session(
    token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> None:
    """Dependency for JSON endpoints — 401 with no body when not authed.
    HTML routes should check `is_authed()` themselves and redirect."""
    if not is_authed(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="auth required",
        )
