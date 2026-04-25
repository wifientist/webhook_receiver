from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

from app.config import settings

log = logging.getLogger("worker.handlers")

Handler = Callable[[str, bytes], Awaitable[None]]


async def handle_ruckus(event_id: str, body: bytes) -> None:
    try:
        data = json.loads(body)
    except ValueError:
        log.warning("ruckus event_id=%s non-json body bytes=%d", event_id, len(body))
        return
    rtype = data.get("type")  # "incident" | "activity" | "event"
    name = data.get("name")
    payload = data.get("payload") or {}
    if rtype == "incident":
        log.info(
            "ruckus.incident event_id=%s webhook=%s severity=%s title=%s",
            event_id, name, payload.get("severity"), payload.get("title"),
        )
    elif rtype == "activity":
        log.info(
            "ruckus.activity event_id=%s webhook=%s useCase=%s status=%s msg=%s",
            event_id, name, payload.get("useCase"),
            payload.get("status"), payload.get("message"),
        )
    elif rtype == "event":
        log.info(
            "ruckus.event event_id=%s webhook=%s code=%s eventName=%s severity=%s entity=%s",
            event_id, name, payload.get("eventCode"),
            payload.get("eventName"), payload.get("severity"), payload.get("entityType"),
        )
    else:
        log.info("ruckus.unknown_type=%s event_id=%s bytes=%d", rtype, event_id, len(body))


HANDLERS: dict[str, Handler] = {}
for _name in (n.strip() for n in settings.ruckus_one_source_names.split(",")):
    if _name:
        HANDLERS[_name] = handle_ruckus
