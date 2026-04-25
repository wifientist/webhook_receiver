from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Mapping

from redis.asyncio import Redis

from app.config import settings
from app.verifiers import Verifier

log = logging.getLogger(__name__)


def _payload_key(source: str, delivery_id: str) -> str:
    return f"wh:payload:{source}:{delivery_id}"


def _seen_key(source: str, delivery_id: str) -> str:
    return f"wh:seen:{source}:{delivery_id}"


def _delivery_id(body: bytes) -> str:
    """Stable identifier for *this exact byte-stream*. Two byte-identical
    payloads collapse (provider retries); any meaningful field change —
    a status transition, a different timestamp, anything — produces a new
    delivery_id and is processed independently."""
    return hashlib.sha256(body).hexdigest()[:32]


async def enqueue(
    redis: Redis,
    source: str,
    headers: Mapping[str, str],
    body: bytes,
    verifier: Verifier,
) -> tuple[str, str, str]:
    """Returns (status, event_id, delivery_id).

    - event_id: logical id from the payload (e.g. activity id, incident id);
      may repeat across state transitions of the same entity.
    - delivery_id: sha256(body)[:32] — unique per byte-distinct delivery,
      used for dedup and storage. This is what prevents false-positive
      dedup when a provider re-fires the same logical event with a
      changed status.
    """
    event_id = verifier.event_id(headers, body) or str(uuid.uuid4())
    delivery_id = _delivery_id(body)
    received_at = str(int(time.time() * 1000))

    was_set = await redis.set(
        _seen_key(source, delivery_id),
        b"1",
        nx=True,
        ex=settings.payload_ttl_seconds,
    )
    if not was_set:
        return "duplicate", event_id, delivery_id

    pipe = redis.pipeline(transaction=False)
    pipe.set(_payload_key(source, delivery_id), body, ex=settings.payload_ttl_seconds)
    pipe.xadd(
        settings.stream_key,
        {
            "source": source,
            "event_id": event_id,
            "delivery_id": delivery_id,
            "received_at": received_at,
        },
        maxlen=settings.stream_maxlen,
        approximate=True,
    )
    await pipe.execute()
    return "accepted", event_id, delivery_id
