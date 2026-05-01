# ── Eido Makefile ──────────────────────────────────────────────────────────────
.PHONY: help dev up down build lint test clean ingest

help: ## Show this help
	@grep -E '^[a-zA-Z_.-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-22s\033[0m %s\n", $$1, $$2}'

dev: ## Start all apps in development mode
	pnpm turbo dev

dev.api: ## Start only the FastAPI backend
	cd apps/api && uvicorn eido_api.main:app --reload --port 8000

dev.web: ## Start only the Next.js gallery
	cd apps/web && pnpm dev

up: ## Boot the full local stack via Docker Compose
	docker compose up -d

down: ## Tear down the local stack
	docker compose down -v

build: ## Build all apps
	pnpm turbo build

lint: ## Lint all apps
	pnpm turbo lint

test: ## Run all tests
	pnpm turbo test

clean: ## Clean all build artifacts
	pnpm turbo clean

ingest: ## Submit a test dataset to the local processing queue
	@echo "Ingesting dataset: $(dataset)"
	@curl -s -X POST http://localhost:8000/api/v1/captures/ingest \
	  -H "Content-Type: application/json" \
	  -d "{\"title\": \"Test Capture\", \"dataset_path\": \"$(dataset)\", \"mode\": \"3dgs\"}" | python3 -m json.tool

db.migrate: ## Run database migrations
	cd apps/api && alembic upgrade head

db.seed: ## Seed the database with sample data
	cd apps/api && python -m eido_api.scripts.seed_db
