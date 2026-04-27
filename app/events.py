from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from redis.asyncio import Redis

from app.config import settings

log = logging.getLogger(__name__)

ACTION_SEVERITIES = {"HIGH", "CRITICAL"}
ACTION_STATUSES = {"FAILED", "ERROR"}


def classify(rtype: str | None, payload: Mapping[str, Any]) -> str:
    """Rules-based action/info split. Tune as you learn what matters."""
    severity = str(payload.get("severity") or "").upper()
    if any(tok in severity for tok in ACTION_SEVERITIES):
        return "action"
    if rtype == "activity":
        st = str(payload.get("status") or "").upper()
        if any(tok in st for tok in ACTION_STATUSES):
            return "action"
    if rtype == "incident":
        return "action"
    return "info"


def _decode(v: Any) -> Any:
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


def _payload_key(source: str, delivery_id: str) -> str:
    return f"wh:payload:{source}:{delivery_id}"


RECENT_MAX = 10_000


async def recent(redis: Redis, n: int = 50) -> list[dict[str, Any]]:
    """Newest-first list of recent stream entries, hydrated with extracted
    payload fields. Entries past the 24h TTL still appear (the stream is
    capped by length, not time) but `has_payload` will be False.

    `n` is clamped to RECENT_MAX so callers can ask for "everything" without
    blowing up the receiver. The stream itself is bounded by STREAM_MAXLEN.
    """
    n = max(1, min(int(n), RECENT_MAX))
    raw = await redis.xrevrange(settings.stream_key, "+", "-", count=n)
    if not raw:
        return []

    metas: list[dict[str, Any]] = []
    pipe = redis.pipeline(transaction=False)
    for stream_id, fields in raw:
        meta: dict[str, Any] = {
            (_decode(k)): _decode(v) for k, v in fields.items()
        }
        meta["stream_id"] = _decode(stream_id)
        metas.append(meta)
        pipe.get(_payload_key(meta.get("source") or "", meta.get("delivery_id") or ""))
    bodies = await pipe.execute()

    out: list[dict[str, Any]] = []
    for meta, body in zip(metas, bodies):
        try:
            received_at = int(meta.get("received_at") or 0)
        except (TypeError, ValueError):
            received_at = 0
        row: dict[str, Any] = {
            "stream_id": meta.get("stream_id"),
            "event_id": meta.get("event_id"),
            "delivery_id": meta.get("delivery_id"),
            "source": meta.get("source"),
            "received_at": received_at,
            "type": None,
            "name": None,
            "severity": None,
            "status": None,
            "event_code": None,
            "entity_type": None,
            "title": None,
            "category": "info",
            "has_payload": body is not None,
        }
        if body:
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if isinstance(data, dict):
                payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
                row["type"] = data.get("type")
                row["name"] = data.get("name")
                row["severity"] = payload.get("severity")
                row["status"] = payload.get("status")
                row["event_code"] = payload.get("eventCode")
                row["entity_type"] = payload.get("entityType")
                row["title"] = (
                    payload.get("title")
                    or payload.get("eventName")
                    or payload.get("message")
                )
                row["category"] = classify(row["type"], payload)
        out.append(row)
    return out


async def get_payload_json(
    redis: Redis, source: str, delivery_id: str
) -> dict[str, Any] | None:
    """Fetch the stored payload and return parsed JSON, or None if missing."""
    body = await redis.get(_payload_key(source, delivery_id))
    if body is None:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return {"_raw": body.decode("utf-8", errors="replace")}
    return data
