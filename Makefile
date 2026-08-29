# Convenience commands. Requires Docker + Docker Compose v2.
.DEFAULT_GOAL := help

.PHONY: help setup up down restart build logs logs-bot logs-dash ps shell-bot \
        prod-up prod-down prod-logs backup backup-daily backup-status pg-up pg-down

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Generate .env interactively
	python scripts/setup.py

build: ## Build the Docker image
	docker compose build

up: ## Start bot + dashboard in the background
	docker compose up -d --build

down: ## Stop and remove containers
	docker compose down

restart: ## Restart services
	docker compose restart

logs: ## Tail logs from all services
	docker compose logs -f

logs-bot: ## Tail bot logs
	docker compose logs -f bot

logs-dash: ## Tail dashboard logs
	docker compose logs -f dashboard

ps: ## Show running services
	docker compose ps

shell-bot: ## Open a shell in the bot container
	docker compose exec bot /bin/sh

# ---- production ----------------------------------------------------------
# Everything below adds the TLS proxy. Needs SPOOLDESK_DOMAIN in .env and its
# DNS record already pointing here.
PROD := -f docker-compose.yml -f docker-compose.prod.yml

prod-up: ## Start with TLS in front (needs SPOOLDESK_DOMAIN)
	docker compose $(PROD) up -d --build

prod-down: ## Stop the production stack
	docker compose $(PROD) down

prod-logs: ## Tail logs including the proxy
	docker compose $(PROD) logs -f

backup: ## Archive the database and its attachments together
	bash scripts/backup.sh

backup-daily: ## Run that archive every night (cron; pass AT=04:15 to change the time)
	bash scripts/backup_schedule.sh $(AT)

backup-status: ## Show whether a nightly backup is scheduled
	bash scripts/backup_schedule.sh --show

# ---- PostgreSQL ----------------------------------------------------------
# Only worth it for more than one dashboard process, or an ops team that would
# rather run the database themselves. Needs POSTGRES_PASSWORD in .env.
PG := -f docker-compose.yml -f docker-compose.postgres.yml

pg-up: ## Start with PostgreSQL instead of SQLite
	docker compose $(PG) up -d --build

pg-down: ## Stop the PostgreSQL stack
	docker compose $(PG) down
