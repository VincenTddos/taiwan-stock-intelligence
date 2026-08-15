.DEFAULT_GOAL := help
SHELL := /bin/bash

BACKEND := backend
WEB     := web
PY      := $(BACKEND)/.venv/bin

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup
.PHONY: setup
setup: ## Create the local dev environment (venv + node modules)
	cd $(BACKEND) && uv venv .venv && . .venv/bin/activate && uv pip install -e ".[dev]" asgi-lifespan
	cd $(WEB) && pnpm install
	@test -f .env || (cp .env.example .env && echo "created .env — edit the CHANGE-ME values")

# ---------------------------------------------------------------- docker
.PHONY: up down logs ps restart
up: ## Start the core stack (db, redis, api, worker, beat, web)
	docker compose up -d --build
	@echo "api  → http://localhost:8000/docs"
	@echo "web  → http://localhost:3000"

up-all: ## Start every profile (adds ollama, flower, minio)
	docker compose --profile llm --profile observability --profile storage up -d --build

down: ## Stop the stack (volumes preserved)
	docker compose down

down-clean: ## Stop the stack and DELETE all data volumes
	docker compose down -v

logs: ## Tail all logs
	docker compose logs -f --tail=100

ps: ## Show container status
	docker compose ps

restart: ## Restart api + worker only
	docker compose restart api worker beat

# ---------------------------------------------------------------- database
.PHONY: migrate migrate-down migrate-check revision seed
migrate: ## Apply all migrations
	cd $(BACKEND) && . .venv/bin/activate && alembic upgrade head

migrate-down: ## Roll back one migration
	cd $(BACKEND) && . .venv/bin/activate && alembic downgrade -1

migrate-check: ## Verify migrations are reversible (up → down → up)
	cd $(BACKEND) && . .venv/bin/activate && \
		alembic upgrade head && alembic downgrade -1 && alembic upgrade head

revision: ## Autogenerate a migration:  make revision m="add stocks"
	cd $(BACKEND) && . .venv/bin/activate && alembic revision --autogenerate -m "$(m)"

seed: ## Create the initial admin user (idempotent)
	cd $(BACKEND) && . .venv/bin/activate && python -m scripts.seed

# ---------------------------------------------------------------- develop
.PHONY: api worker beat web
api: ## Run the API with reload
	cd $(BACKEND) && . .venv/bin/activate && uvicorn app.main:app --reload --port 8000

worker: ## Run a Celery worker
	cd $(BACKEND) && . .venv/bin/activate && \
		celery -A app.workers.celery_app worker -Q q_ingest,q_compute,q_nlp,q_user,q_maint -l info

beat: ## Run the Celery scheduler
	cd $(BACKEND) && . .venv/bin/activate && celery -A app.workers.celery_app beat -l info

web: ## Run the Next.js dev server
	cd $(WEB) && pnpm dev

# ---------------------------------------------------------------- quality
.PHONY: lint format typecheck test test-backend test-frontend coverage security openapi
lint: ## Lint backend + frontend
	cd $(BACKEND) && . .venv/bin/activate && ruff check . && ruff format --check .
	cd $(WEB) && pnpm lint

format: ## Auto-format
	cd $(BACKEND) && . .venv/bin/activate && ruff check --fix . && ruff format .

typecheck: ## Type-check backend (mypy --strict) + frontend (tsc)
	cd $(BACKEND) && . .venv/bin/activate && mypy app
	cd $(WEB) && pnpm typecheck

test: test-backend test-frontend ## Run every test

test-backend: ## Backend tests (needs Postgres + Redis)
	cd $(BACKEND) && . .venv/bin/activate && pytest

test-frontend: ## Frontend tests
	cd $(WEB) && pnpm test

coverage: ## Backend coverage report (fails under 80%)
	cd $(BACKEND) && . .venv/bin/activate && \
		pytest --cov=app --cov-report=term-missing --cov-fail-under=80

security: ## Dependency + secret scanning
	cd $(BACKEND) && . .venv/bin/activate && pip-audit || true
	cd $(WEB) && pnpm audit --audit-level=high || true
	@command -v gitleaks >/dev/null && gitleaks detect --no-banner || echo "gitleaks not installed — skipped"

openapi: ## Export the OpenAPI schema and regenerate frontend types
	cd $(BACKEND) && . .venv/bin/activate && \
		python -c "import json;from app.main import create_app;print(json.dumps(create_app().openapi(),indent=2))" > openapi.json
	cd $(WEB) && pnpm gen:api

# ---------------------------------------------------------------- gate
.PHONY: check
check: lint typecheck test migrate-check ## The full Phase gate — everything CI runs
	@echo ""
	@echo "  ✓ lint · typecheck · tests · migrations"
	@echo "  Phase gate passed."

# ---------------------------------------------------------------- verify
.PHONY: verify
verify: ## Probe a running stack and print a health summary
	@bash scripts/verify_stack.sh
