#!/usr/bin/env bash
# Dump every stored webhook payload to stdout as pretty JSON.
# Optional arg filters by source name (defaults to all).
#   ./scripts/dump-payloads.sh             # everything
#   ./scripts/dump-payloads.sh testingdnd  # just that source
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "no .env in $(pwd) — run from project root" >&2
  exit 1
fi
set -a; source .env; set +a

source_filter="${1:-*}"
pattern="wh:payload:${source_filter}:*"

mapfile -t keys < <(
  docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning \
    --scan --pattern "$pattern"
)

if (( ${#keys[@]} == 0 )); then
  echo "no payloads matching $pattern" >&2
  exit 0
fi

for key in "${keys[@]}"; do
  ttl=$(docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning TTL "$key")
  echo "=== $key  (TTL ${ttl}s) ==="
  docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" --no-auth-warning GET "$key" \
    | python3 -m json.tool 2>/dev/null \
    || echo "(not valid JSON)"
  echo
done
