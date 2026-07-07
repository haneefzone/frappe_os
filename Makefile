COMPOSE = docker compose -f docker-compose.dev.yml

.PHONY: setup dev backend frontend worker test lint stop

## One-time setup: backend venv + frontend node_modules
setup:
	cd backend && uv venv && uv pip install -e ".[dev]"
	cd frontend && npm install

## Start everything: compose services, then backend + frontend together
dev:
	$(COMPOSE) up -d
	$(MAKE) -j2 backend frontend

backend:
	cd backend && .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

frontend:
	cd frontend && npm run dev

worker:
	cd backend && .venv/bin/rq worker high default low

test:
	cd backend && .venv/bin/pytest

lint:
	cd backend && .venv/bin/ruff check .

stop:
	$(COMPOSE) down
