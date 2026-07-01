# Makefile - Kabale Transport Platform
# Common development commands

.PHONY: help dev stop build migrate test lint format clean logs shell

help:
	@echo "Available commands:"
	@echo "  make dev        - Start full development environment"
	@echo "  make stop       - Stop all containers"
	@echo "  make build      - Rebuild Docker images"
	@echo "  make migrate    - Run database migrations"
	@echo "  make test       - Run test suite"
	@echo "  make lint       - Run linter (ruff)"
	@echo "  make format     - Format code (black)"
	@echo "  make clean      - Remove containers and volumes"
	@echo "  make logs       - Follow API logs"
	@echo "  make shell      - Open shell in API container"

dev:
	docker compose --profile dev up -d
	@echo "API running at http://localhost:8000"
	@echo "API docs at   http://localhost:8000/docs"
	@echo "Flower at     http://localhost:5555"

stop:
	docker compose down

build:
	docker compose build --no-cache

migrate:
	docker compose exec api alembic upgrade head

migrate-down:
	docker compose exec api alembic downgrade -1

migrate-history:
	docker compose exec api alembic history

test:
	docker compose exec api pytest tests/ -v --tb=short

test-unit:
	docker compose exec api pytest tests/unit/ -v

test-integration:
	docker compose exec api pytest tests/integration/ -v

lint:
	docker compose exec api ruff check app/ tests/

format:
	docker compose exec api black app/ tests/

clean:
	docker compose down -v --remove-orphans
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete

logs:
	docker compose logs -f api

logs-celery:
	docker compose logs -f celery_worker

shell:
	docker compose exec api bash

db-shell:
	docker compose exec db psql -U kabale_user -d kabale_transport
