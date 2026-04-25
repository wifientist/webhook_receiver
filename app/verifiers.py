from __future__ import annotations

import hmac
import json
import logging
from typing import Mapping, Protocol

from app.config import settings

log = logging.getLogger(__name__)

VALID_RUCKUS_TYPES = {"incident", "activity", "event"}


class Verifier(Protocol):
    def verify(self, headers: Mapping[str, str], body: bytes) -> bool: ...
    def event_id(self, headers: Mapping[str, str], body: bytes) -> str | None: ...


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return None


class RuckusOneVerifier:
    """Validates incoming Ruckus One webhook deliveries.

    Two checks must pass:
      1. Authorization header matches the configured secret (when one is
         configured). Ruckus accepts the secret raw or as 'Bearer <secret>'.
      2. Body has the shape Ruckus actually sends: JSON object with a
         recognised `type` field and a `payload` object. Junk / stray probes
         are rejected before they ever hit the queue.

    The per-event id comes from `payload.id` for incidents, activities, and
    AP/client events. Admin events have no `payload.id`, so a synthetic id is
    composed from `tenantId:eventCode:timestamp`.
    """

    def __init__(self, secret: str | None) -> None:
        self.secret = secret

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        if self.secret:
            auth = _header(headers, "Authorization")
            if not auth:
                log.warning("ruckus reject: no Authorization header")
                return False
            candidate = auth[7:].strip() if auth.lower().startswith("bearer ") else auth
            if not hmac.compare_digest(candidate, self.secret):
                log.warning("ruckus reject: Authorization mismatch")
                return False

        try:
            data = json.loads(body)
        except ValueError:
            log.warning("ruckus reject: body is not valid JSON")
            return False
        if not isinstance(data, dict):
            log.warning("ruckus reject: body is not a JSON object")
            return False
        if data.get("type") not in VALID_RUCKUS_TYPES:
            log.warning("ruckus reject: type=%r not in %s", data.get("type"), VALID_RUCKUS_TYPES)
            return False
        if not isinstance(data.get("payload"), dict):
            log.warning("ruckus reject: payload missing or not an object")
            return False
        return True

    def event_id(self, headers: Mapping[str, str], body: bytes) -> str | None:
        try:
            data = json.loads(body)
        except ValueError:
            return None
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        if pid := payload.get("id"):
            return str(pid)
        # Admin events: no payload.id — synthesize a stable id.
        code = payload.get("eventCode")
        ts = payload.get("timestamp") or payload.get("datetime")
        tenant = payload.get("tenantId")
        if code and ts:
            return f"{tenant or 'na'}:{code}:{ts}"
        return None


_KNOWN: dict[str, Verifier] = {}
_ruckus = RuckusOneVerifier(settings.ruckus_one_webhook_secret)
for _name in (n.strip() for n in settings.ruckus_one_source_names.split(",")):
    if _name:
        _KNOWN[_name] = _ruckus


def resolve_verifier(source: str) -> Verifier | None:
    """Returns the verifier for `source`, or None if the source is not
    configured. Callers should treat None as 'unknown source -> 404'."""
    return _KNOWN.get(source)
