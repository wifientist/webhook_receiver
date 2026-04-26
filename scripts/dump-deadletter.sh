#!/usr/bin/env bash
# Dump every entry in wh:dead — events the worker gave up on after MAX_DELIVERIES retries.
# Each entry's fields include the original stream id, source, event_id, dead_at timestamp,
# and (depending on entry shape) the original delivery_id used to look up the body.
#
# Usage:
#   ./scripts/dump-deadletter.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "no .env in $(pwd) — run from project root" >&2
  exit 1
fi
set -a; source .env; set +a

count=$(docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning XLEN wh:dead | tr -d '[:space:]')
echo "wh:dead contains ${count} entries"
if [[ "${count:-0}" -eq 0 ]]; then exit 0; fi
echo

# XRANGE - + dumps every entry from oldest to newest, alternating field/value pairs.
docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning XRANGE wh:dead - +

echo
echo "tip: to inspect the original payload (if still within 24h TTL), run:"
echo "  redis-cli -a \"\$REDIS_PASSWORD\" GET wh:payload:<source>:<delivery_id>"
