# webhook_rx

A transient Ruckus One webhook receiver. FastAPI on **:5000**, Redis on **:5001**, a worker that drains the queue through a consumer group. Every Redis key lives for 24 hours and then vanishes — no SQL, no long-term storage.

## Layout

```
app/         FastAPI receiver (Ruckus auth + shape validation, dedup, enqueue)
app/static/  dashboard (vanilla JS + Chart.js, served at /dashboard)
worker/      consumer-group worker + Ruckus handler
scripts/     convenience helpers (e.g. dump-payloads.sh)
tests/       pytest + fakeredis
```

## What gets accepted

The receiver is a tight funnel. Anything that isn't a real Ruckus delivery gets rejected before it touches the queue:

| Reject reason | HTTP | Counter |
|---|---|---|
| Path not in `RUCKUS_ONE_SOURCE_NAMES` | `404` | `unknown_source` |
| Body larger than `MAX_BODY_BYTES` (default 1 MiB) | `413` | `body_too_large` |
| Bad `Content-Length` header | `400` | `invalid_content_length` |
| Missing or wrong `Authorization` header (when secret configured) | `401` | `verification_failed` |
| Body not valid JSON, or `type` ∉ `{incident, activity, event}`, or no object `payload` | `401` | `verification_failed` |
| Valid + auth'd | `202` (and `{"status":"accepted","event_id":"…"}`) | — |

Rejections are counted under `wh:stats:total:rejection_reason:<reason>` and surfaced in `/stats/counters` under the `rejections` block — useful for spotting probing/abuse spikes.

Headers are used for verification + event-id derivation only. Nothing from the headers is persisted.

## Dedup model

Every accepted body is hashed (`sha256(body)[:32]`). The hash is the dedup key:

- **Byte-identical retries** (Ruckus often re-fires the same body 3-5x in a burst) → caught, the second through Nth show `"duplicate"` and the worker dispatches once.
- **Status transitions** (same `payload.id`, but `status` flipped from `IN_PROGRESS` to `SUCCESS` — different bytes) → both pass through, both queued, both dispatched. This is intentional: a `payload.id`-based dedup would silently drop the SUCCESS update.

## Required env vars

Set these in `.env` (copy from `.env.example`):

| Var | Required | Purpose |
|---|---|---|
| `REDIS_PASSWORD` | yes | Generate with `openssl rand -hex 32`. Compose refuses to start without it. |
| `RUCKUS_ONE_WEBHOOK_SECRET` | recommended | The "Secret" value you set on the Ruckus webhook config. When unset, the receiver accepts unsigned bodies (dev only). |
| `RUCKUS_ONE_SOURCE_NAMES` | optional | CSV of `/webhook/<name>` paths this receiver answers. Default `ruckus`. Whatever URL ID you typed into Ruckus goes here. |

## Quickstart

```bash
cp .env.example .env
# edit .env: REDIS_PASSWORD, RUCKUS_ONE_WEBHOOK_SECRET, RUCKUS_ONE_SOURCE_NAMES

docker compose up --build
```

- Receiver: `http://localhost:5000/webhook/<source>`
- Redis (host): `localhost:5001` — auth required
- Worker logs: `docker compose logs -f worker`

### Smoke test

```bash
# Source the secret so curl can include it
set -a; source .env; set +a

curl -X POST http://localhost:5000/webhook/ruckus \
  -H "Authorization: $RUCKUS_ONE_WEBHOOK_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"id":"cfg","name":"probe","type":"event","payload":{"id":"smoke-1","eventCode":"101"}}'
# → {"status":"accepted","event_id":"smoke-1","source":"ruckus"}
```

Repeat the exact same body within 24h → `{"status":"duplicate"}`. Change any byte of the payload → `{"status":"accepted"}` again.

### Endpoints

| Endpoint | What it tells you |
|---|---|
| `GET /healthz` | Receiver up + Redis reachable |
| `GET /stats` | Live queue depth, in-flight count, dead-letter size |
| `GET /stats/counters` | Throughput counters by `type` / `source` / `event_code` / `entity_type` / `severity` / `status`, plus rejections by reason. Three time windows: running totals (∞), today (1y TTL), last-24h hourly (90d TTL) |
| `GET /dashboard` | Browser dashboard (see below). Redirects to `/login` when `DASHBOARD_PASSWORD` is set and you're not logged in. |
| `GET /login` | Password form (only when `DASHBOARD_PASSWORD` is set; otherwise redirects to `/dashboard`). |
| `POST /api/login` | `{"password": "…"}` → sets `wh_session` cookie. |
| `POST /api/logout` | Clears the session cookie. |
| `GET /events/recent?n=N` | Newest-first stream tail with extracted payload fields + action/info classification. Cookie-gated. |
| `GET /events/{source}/{delivery_id}` | Full stored payload. 404 once the 24h TTL elapses. Cookie-gated. |
| `POST /webhook/{source}` | Ingest. `{source}` must match `RUCKUS_ONE_SOURCE_NAMES`. |

### Inspecting from the CLI

```bash
make counters          # /stats/counters, pretty-printed
make queue             # /stats, pretty-printed
make health            # /healthz
make payloads          # dump every stored body (24h TTL)
make deadletter        # dump entries the worker gave up on

# raw redis-cli, if you want it
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" XLEN wh:stream
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" XPENDING wh:stream workers
```

The Makefile is self-documenting: `make` (no args) lists every target.

---

## Dashboard

A single-file dashboard at `http://localhost:5000/dashboard` reads exclusively from Redis — no extra services, no persistence beyond the existing 24h window. It shows:

- **Top strip** — stream depth, in-flight, dead-letter (red when > 0), received-today, rejected-today
- **Last 24h** — hourly stacked bar by `type` (incident / activity / event)
- **Top event codes** (today) and **top entity types** (running)
- **Recent events table** — newest 100, filterable by category (action / info), type, and free-text search; click a row to see the full payload
- **Rejections panel** (collapsed) — today's rejection reasons

The action/info split is rules-based — see `classify()` in [app/events.py](app/events.py). Tune the rules as you observe real traffic.

### Auth

A single shared password gates the dashboard. Set it in `.env`:

```bash
DASHBOARD_PASSWORD=$(openssl rand -hex 16)
```

Visiting `/dashboard` while unauthenticated redirects to `/login` — a small password form. Submitting the right password sets an HttpOnly session cookie (`wh_session`) and lands you on the dashboard. The cookie is good for 7 days; rotating `DASHBOARD_PASSWORD` invalidates every outstanding cookie automatically (the cookie value is `HMAC-SHA256(password, fixed-string)` — the password *is* the signing key). The "sign out" link top-right clears it.

Leave `DASHBOARD_PASSWORD` empty for an open dashboard (local-only dev) — `/login` redirects straight to `/dashboard`. The webhook intake path is **not** affected either way; Ruckus still POSTs unauthenticated (gated by its own `Authorization` secret).

If you also expose this through a Cloudflare Tunnel and want SSO instead of a shared password, point **Cloudflare Access** (Zero Trust → Applications → email/Google OTP) at the `/dashboard`, `/login`, `/api/*`, and `/events/*` paths. The shared password and Cloudflare Access aren't mutually exclusive — Access in front, password as a second factor — but for personal use one or the other is plenty.

---

## Optional: Cloudflare Tunnel (public exposure)

The `cloudflared` service in `docker-compose.yml` terminates a Cloudflare named tunnel — no inbound ports on the host, free TLS, free DNS. Skip this whole section if you only need the receiver on a private network.

**Setup:**

1. In Cloudflare Zero Trust → **Networks → Tunnels → Create a tunnel** (named).
2. Copy the tunnel token; put it in `.env` as `TUNNEL_TOKEN=eyJ…`.
3. In the tunnel's **Public Hostname** tab, point a hostname at `http://receiver:5000`.

The `cloudflared` container is part of the default profile, so it starts with `docker compose up -d` automatically. To run *without* the tunnel (e.g. local-only dev):

```bash
docker compose up -d redis receiver worker
```

The public URL becomes whatever you set in the dashboard, plus the source name:
`https://<public-host>/webhook/<source>` — paste into Ruckus's webhook config.

---

## Optional: RedisInsight (web UI for inspection)

The `redisinsight` service is gated behind a compose **profile** so it only runs when you ask for it. Useful for dev/debug, off in prod.

```bash
# start (also kept running until you stop it)
docker compose --profile debug up -d redisinsight

# stop without taking down the rest of the stack
docker compose --profile debug stop redisinsight
```

Browse: `http://localhost:5540` (also reachable on any other host interface — bind defaults to `0.0.0.0:5540`).

The connection to your Redis is **auto-imported on first run** via the `RI_REDIS_*` env vars, so the UI lands you straight on a tile labeled `webhook_rx-redis`. First-launch flow asks you to accept the EULA and optionally set an encryption key (skip is fine for dev).

To wipe the auto-imported config and start over (e.g. after rotating `REDIS_PASSWORD`):

```bash
docker compose --profile debug down redisinsight
docker volume rm webhook_rx_redisinsight-data
docker compose --profile debug up -d redisinsight
```

---

## Scaling

- **Receiver** is stateless — add uvicorn workers (`--workers N`) or more containers.
- **Worker** scales horizontally — bump `worker` replicas; each picks a unique consumer name (`<hostname>-<pid>`), the consumer group distributes entries.
- Stream is capped approximately at `STREAM_MAXLEN` (1M). Payload keys TTL out at 24h independently. If workers fall behind, oldest stream entries get trimmed — watch `curl /stats` or `XLEN wh:stream`.
- Failed handlers leave entries pending; after `MAX_DELIVERIES` retries (default 5) they move to `wh:dead` for inspection.

## Customising the handler

Edit [worker/handlers.py](worker/handlers.py). `handle_ruckus(event_id, body)` is async; raise to signal failure (entry stays pending for retry, dead-letters after `MAX_DELIVERIES`).

## Tests

```bash
pip install -e '.[dev]'
pytest
```

Coverage: source-routing 404, body-size 413, missing/wrong auth 401, shape rejection 401, byte-identical dedup, status-transition non-dedup, header non-persistence, worker dispatch, consumer-group idempotency.
