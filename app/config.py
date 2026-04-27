from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:5001/0"
    redis_password: str | None = None
    api_host: str = "0.0.0.0"
    api_port: int = 5000

    payload_ttl_seconds: int = 86_400
    stream_maxlen: int = 1_000_000
    stream_key: str = "wh:stream"
    dead_stream_key: str = "wh:dead"
    consumer_group: str = "workers"
    max_deliveries: int = 5

    # Reject any POST whose body exceeds this many bytes (default 1 MiB).
    max_body_bytes: int = 1_048_576

    ruckus_one_webhook_secret: str | None = None
    # CSV of /webhook/<source> path names that are valid Ruckus endpoints.
    # Anything not in this list returns 404.
    ruckus_one_source_names: str = "ruckus"

    # Dashboard auth. Single shared password — no username. When unset, the
    # dashboard is open (local dev / private networks).
    dashboard_password: str | None = None


settings = Settings()
