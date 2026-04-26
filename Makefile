.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash

help:  ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | sort \
	  | awk -F ':.*## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## Start default-profile services in the background
	docker compose up -d

down:  ## Stop and remove default-profile containers (volumes preserved)
	docker compose down

rebuild:  ## Rebuild + restart the receiver and worker (skip redis/cloudflared)
	docker compose up -d --build receiver worker

logs:  ## Tail receiver + worker logs (timestamped, last 100 lines)
	docker compose logs -f -t --tail=100 receiver worker

logs-all:  ## Tail every service's logs
	docker compose logs -f -t --tail=100

ps:  ## Show container status
	docker compose ps

test:  ## Run pytest in the local venv
	.venv/bin/pytest

counters:  ## Pretty-print /stats/counters
	@curl -sS http://localhost:5000/stats/counters | python3 -m json.tool

queue:  ## Pretty-print /stats (queue depth, pending, dead-letter)
	@curl -sS http://localhost:5000/stats | python3 -m json.tool

health:  ## Hit /healthz
	@curl -sS http://localhost:5000/healthz | python3 -m json.tool

payloads:  ## Dump all stored webhook payloads
	@./scripts/dump-payloads.sh

deadletter:  ## Dump the wh:dead stream
	@./scripts/dump-deadletter.sh

debug-up:  ## Start RedisInsight UI (debug profile) on :5540
	docker compose --profile debug up -d redisinsight

debug-down:  ## Stop RedisInsight without taking down the rest
	docker compose --profile debug stop redisinsight

.PHONY: help up down rebuild logs logs-all ps test counters queue health payloads deadletter debug-up debug-down
