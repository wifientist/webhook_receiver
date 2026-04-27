from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Mapping

from redis.asyncio import Redis

log = logging.getLogger(__name__)

DAILY_TTL = 365 * 24 * 3600  # 1 year
HOURLY_TTL = 90 * 24 * 3600  # 90 days

# Dimensions counted per delivery. The verifier guarantees `type` is present
# and `payload` is a dict, but be defensive: malformed entries become no-ops.
_PAYLOAD_DIMS = ("eventCode", "entityType", "severity")
# Map JSON field names to short, snake_case dim names used in keys + responses.
_DIM_RENAME = {"eventCode": "event_code", "entityType": "entity_type"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _bucket_strs(now: datetime) -> tuple[str, str]:
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d-%H")


def _dims_from_body(body: bytes) -> dict[str, str]:
    """Pull counted dimensions out of a Ruckus payload. Returns {} on parse error."""
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    if rtype := data.get("type"):
        out["type"] = str(rtype)
    payload = data.get("payload") or {}
    if isinstance(payload, dict):
        for field in _PAYLOAD_DIMS:
            if (v := payload.get(field)) is not None:
                out[_DIM_RENAME.get(field, field)] = str(v)
    return out


async def record(redis: Redis, source: str, status: str, body: bytes) -> None:
    """Increment the counters for a single delivery.

    Buckets:
      - total:   wh:stats:total:<dim>:<value>            (no TTL)
      - daily:   wh:stats:daily:<YYYY-MM-DD>:<dim>:<value>      (TTL 1y)
      - hourly:  wh:stats:hourly:<YYYY-MM-DD-HH>:<dim>:<value>  (TTL 90d)

    Dimensions: source, status, type, event_code, entity_type, severity.
    Plus a dim-less "received" counter for raw throughput at each bucket.
    TTLs are set with NX so they aren't refreshed on every increment —
    a daily bucket expires 1y from when it first saw traffic, not from the
    last increment.
    """
    now = _utc_now()
    day, hour = _bucket_strs(now)

    dims: dict[str, str] = {"source": source, "status": status}
    dims.update(_dims_from_body(body))

    pipe = redis.pipeline(transaction=False)

    # Throughput-only (no dim breakdown): "<scope>:received"
    pipe.incr("wh:stats:total:received")
    pipe.incr(f"wh:stats:daily:{day}:received")
    pipe.expire(f"wh:stats:daily:{day}:received", DAILY_TTL, nx=True)
    pipe.incr(f"wh:stats:hourly:{hour}:received")
    pipe.expire(f"wh:stats:hourly:{hour}:received", HOURLY_TTL, nx=True)

    for k, v in dims.items():
        pipe.incr(f"wh:stats:total:{k}:{v}")

        daily_key = f"wh:stats:daily:{day}:{k}:{v}"
        pipe.incr(daily_key)
        pipe.expire(daily_key, DAILY_TTL, nx=True)

        hourly_key = f"wh:stats:hourly:{hour}:{k}:{v}"
        pipe.incr(hourly_key)
        pipe.expire(hourly_key, HOURLY_TTL, nx=True)

    await pipe.execute()


async def record_rejection(redis: Redis, reason: str) -> None:
    """Increment rejection counters. `reason` is a short snake_case label
    like 'unknown_source', 'body_too_large', 'verification_failed'.

    Mirrors record() but lives in its own key namespace so legitimate traffic
    and abuse signals don't get mixed up:
      - wh:stats:total:rejected:received
      - wh:stats:total:rejection_reason:<reason>
      - wh:stats:daily:<date>:rejected:received
      - wh:stats:daily:<date>:rejection_reason:<reason>
      - wh:stats:hourly:<hour>:rejected:received
      - wh:stats:hourly:<hour>:rejection_reason:<reason>
    """
    now = _utc_now()
    day, hour = _bucket_strs(now)

    pipe = redis.pipeline(transaction=False)

    pipe.incr("wh:stats:total:rejected:received")
    pipe.incr(f"wh:stats:daily:{day}:rejected:received")
    pipe.expire(f"wh:stats:daily:{day}:rejected:received", DAILY_TTL, nx=True)
    pipe.incr(f"wh:stats:hourly:{hour}:rejected:received")
    pipe.expire(f"wh:stats:hourly:{hour}:rejected:received", HOURLY_TTL, nx=True)

    pipe.incr(f"wh:stats:total:rejection_reason:{reason}")
    daily_key = f"wh:stats:daily:{day}:rejection_reason:{reason}"
    pipe.incr(daily_key)
    pipe.expire(daily_key, DAILY_TTL, nx=True)
    hourly_key = f"wh:stats:hourly:{hour}:rejection_reason:{reason}"
    pipe.incr(hourly_key)
    pipe.expire(hourly_key, HOURLY_TTL, nx=True)

    await pipe.execute()


async def _read_dim(redis: Redis, prefix: str) -> dict[str, int]:
    """SCAN keys under `prefix` and return {dim_value: count}. The dim_value
    is whatever follows the prefix (i.e. the last colon-segment)."""
    keys: list[bytes] = []
    async for key in redis.scan_iter(match=f"{prefix}*", count=200):
        keys.append(key)
    if not keys:
        return {}
    values = await redis.mget(keys)
    out: dict[str, int] = {}
    for k, v in zip(keys, values):
        key_str = k.decode() if isinstance(k, bytes) else k
        dim_value = key_str[len(prefix):]
        out[dim_value] = int(v) if v is not None else 0
    return out


async def by_dim_total(redis: Redis, dim: str) -> dict[str, int]:
    return await _read_dim(redis, f"wh:stats:total:{dim}:")


async def by_dim_daily(redis: Redis, dim: str, day: str) -> dict[str, int]:
    return await _read_dim(redis, f"wh:stats:daily:{day}:{dim}:")


async def hourly_totals(redis: Redis, hours: int = 24) -> list[dict]:
    """Last `hours` hours of throughput, oldest first. Missing buckets = 0."""
    now = _utc_now()
    keys: list[str] = []
    labels: list[str] = []
    for i in range(hours - 1, -1, -1):
        h = (now - timedelta(hours=i)).strftime("%Y-%m-%d-%H")
        labels.append(h)
        keys.append(f"wh:stats:hourly:{h}:received")
    values = await redis.mget(keys)
    return [
        {"hour": h, "count": int(v) if v is not None else 0}
        for h, v in zip(labels, values)
    ]


async def hourly_by_dim(
    redis: Redis, dim: str, hours: int = 24
) -> list[dict]:
    """Last `hours` hours, each as `{hour, by_value: {value: count}}`.

    Single SCAN over `wh:stats:hourly:*:<dim>:*`, then bucket by hour. Hours
    with no entries for the dim still appear with `by_value: {}` so the
    caller can render an unbroken time axis.
    """
    now = _utc_now()
    wanted: list[str] = []
    for i in range(hours - 1, -1, -1):
        wanted.append((now - timedelta(hours=i)).strftime("%Y-%m-%d-%H"))
    wanted_set = set(wanted)
    out: dict[str, dict[str, int]] = {h: {} for h in wanted}

    prefix = "wh:stats:hourly:"
    keys: list[bytes] = []
    async for key in redis.scan_iter(match=f"{prefix}*:{dim}:*", count=200):
        keys.append(key)
    if keys:
        values = await redis.mget(keys)
        for k, v in zip(keys, values):
            ks = k.decode() if isinstance(k, bytes) else k
            tail = ks[len(prefix):]
            # tail = "<HOUR>:<dim>:<value>"; HOUR uses dashes only.
            parts = tail.split(":", 2)
            if len(parts) != 3:
                continue
            hour, kdim, value = parts
            if kdim != dim or hour not in wanted_set:
                continue
            out[hour][value] = int(v) if v is not None else 0
    return [{"hour": h, "by_value": out[h]} for h in wanted]


async def snapshot(redis: Redis) -> Mapping[str, object]:
    """Single-call summary: running totals + today + last-24h hourly throughput."""
    now = _utc_now()
    today = now.strftime("%Y-%m-%d")
    received_total = await redis.get("wh:stats:total:received")
    received_today = await redis.get(f"wh:stats:daily:{today}:received")
    rejected_total = await redis.get("wh:stats:total:rejected:received")
    rejected_today = await redis.get(f"wh:stats:daily:{today}:rejected:received")
    return {
        "running_totals": {
            "received": int(received_total) if received_total else 0,
            "by_type": await by_dim_total(redis, "type"),
            "by_source": await by_dim_total(redis, "source"),
            "by_status": await by_dim_total(redis, "status"),
            "by_event_code": await by_dim_total(redis, "event_code"),
            "by_entity_type": await by_dim_total(redis, "entity_type"),
            "by_severity": await by_dim_total(redis, "severity"),
        },
        "today": {
            "date": today,
            "received": int(received_today) if received_today else 0,
            "by_type": await by_dim_daily(redis, "type", today),
            "by_source": await by_dim_daily(redis, "source", today),
            "by_status": await by_dim_daily(redis, "status", today),
            "by_event_code": await by_dim_daily(redis, "event_code", today),
        },
        "rejections": {
            "running_totals": {
                "received": int(rejected_total) if rejected_total else 0,
                "by_reason": await by_dim_total(redis, "rejection_reason"),
            },
            "today": {
                "received": int(rejected_today) if rejected_today else 0,
                "by_reason": await by_dim_daily(redis, "rejection_reason", today),
            },
        },
        "last_24h_hourly": await hourly_totals(redis, hours=24),
        "last_24h_by_type": await hourly_by_dim(redis, "type", hours=24),
    }
