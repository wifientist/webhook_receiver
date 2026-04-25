from pydantic import BaseModel


class EnqueueResponse(BaseModel):
    status: str  # "accepted" | "duplicate"
    event_id: str
    source: str


class HealthResponse(BaseModel):
    ok: bool
    redis: bool


class StatsResponse(BaseModel):
    stream_key: str
    length: int
    pending: int
    dead_length: int
