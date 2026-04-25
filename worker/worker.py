from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.config import settings
from app.redis_client import close_redis, get_redis
from worker.handlers import HANDLERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("worker")


def _consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


async def _ensure_group(redis: Redis) -> None:
    try:
        await redis.xgroup_create(
            settings.stream_key, settings.consumer_group, id="$", mkstream=True
        )
        log.info("created consumer group %s on %s", settings.consumer_group, settings.stream_key)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _process_entry(redis: Redis, entry_id: bytes, fields: dict[bytes, bytes]) -> bool:
    """Return True if successfully processed (caller should XACK). False => leave pending."""
    source = fields.get(b"source", b"").decode()
    event_id = fields.get(b"event_id", b"").decode()
    delivery_id = fields.get(b"delivery_id", b"").decode()
    # Pre-migration entries may lack delivery_id; fall back to event_id-keyed lookup.
    storage_id = delivery_id or event_id
    if not source or not storage_id:
        log.error("malformed entry %s fields=%s", entry_id, fields)
        return True  # ack malformed so we don't loop forever

    body = await redis.get(f"wh:payload:{source}:{storage_id}")
    if body is None:
        log.warning(
            "payload missing (ttl expired?) source=%s event_id=%s delivery=%s entry=%s",
            source, event_id, delivery_id[:12], entry_id,
        )
        return True

    handler = HANDLERS.get(source)
    if handler is None:
        log.warning("no handler for source=%s entry=%s — acking", source, entry_id)
        return True
    try:
        await handler(event_id, body)
        log.info(
            "dispatched source=%s event_id=%s delivery=%s entry=%s",
            source, event_id, delivery_id[:12], entry_id,
        )
        return True
    except Exception as exc:
        log.exception("handler failed source=%s event_id=%s: %s", source, event_id, exc)
        return False


async def _deadletter(redis: Redis, entry_id: bytes, fields: dict[bytes, bytes]) -> None:
    str_fields = {k.decode(): v for k, v in fields.items()}
    str_fields["original_id"] = entry_id.decode()
    str_fields["dead_at"] = str(int(time.time() * 1000))
    await redis.xadd(settings.dead_stream_key, str_fields, maxlen=100_000, approximate=True)
    await redis.xack(settings.stream_key, settings.consumer_group, entry_id)
    log.error("dead-lettered entry=%s source=%s", entry_id, str_fields.get("source"))


async def _reclaim_stale(redis: Redis, consumer: str) -> None:
    """XAUTOCLAIM stale pending entries (>60s idle) and process them."""
    start_id = b"0-0"
    while True:
        try:
            reply = await redis.xautoclaim(
                settings.stream_key,
                settings.consumer_group,
                consumer,
                min_idle_time=60_000,
                start_id=start_id,
                count=32,
            )
        except ResponseError as exc:
            log.warning("xautoclaim failed: %s", exc)
            return
        # redis-py returns (next_start_id, claimed_entries, deleted_ids)
        next_id = reply[0]
        entries = reply[1]
        if not entries:
            return
        for entry_id, fields in entries:
            # Check delivery count before reprocessing.
            delivery_count = await _delivery_count(redis, entry_id)
            if delivery_count > settings.max_deliveries:
                await _deadletter(redis, entry_id, fields)
                continue
            ok = await _process_entry(redis, entry_id, fields)
            if ok:
                await redis.xack(settings.stream_key, settings.consumer_group, entry_id)
        start_id = next_id
        if start_id == b"0-0":
            return


async def _delivery_count(redis: Redis, entry_id: bytes) -> int:
    try:
        reply = await redis.xpending_range(
            settings.stream_key,
            settings.consumer_group,
            min=entry_id,
            max=entry_id,
            count=1,
        )
    except ResponseError:
        return 0
    if not reply:
        return 0
    return int(reply[0]["times_delivered"])


async def run() -> None:
    redis = get_redis()
    consumer = _consumer_name()
    await _ensure_group(redis)
    log.info("worker=%s attached to group=%s", consumer, settings.consumer_group)

    stop = asyncio.Event()

    def _stop(*_: object) -> None:
        log.info("stop signal received, draining...")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    try:
        while not stop.is_set():
            await _reclaim_stale(redis, consumer)
            if stop.is_set():
                break
            try:
                streams = await redis.xreadgroup(
                    settings.consumer_group,
                    consumer,
                    streams={settings.stream_key: ">"},
                    count=32,
                    block=5_000,
                )
            except ResponseError as exc:
                log.error("xreadgroup failed: %s", exc)
                await asyncio.sleep(1)
                continue
            if not streams:
                continue
            for _stream_name, entries in streams:
                for entry_id, fields in entries:
                    ok = await _process_entry(redis, entry_id, fields)
                    if ok:
                        await redis.xack(settings.stream_key, settings.consumer_group, entry_id)
    finally:
        await close_redis()
        log.info("worker exited")


if __name__ == "__main__":
    asyncio.run(run())
