COMPOSE = docker compose -f docker-compose.dev.yml

.PHONY: setup dev backend frontend worker scheduler test lint stop

## One-time setup: backend venv + frontend node_modules
setup:
	cd backend && uv venv && uv pip install -e ".[dev]"
	cd frontend && npm install

## Start everything: compose services, then backend + frontend together
dev:
	$(COMPOSE) up -d
	$(MAKE) -j2 backend frontend

# DEBUG=true skips the fail-closed placeholder-secret startup check (dev only,
# never set it in production).
backend:
	cd backend && DEBUG=true .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

frontend:
	cd frontend && npm run dev

worker:
	cd backend && .venv/bin/python -m app.workers.worker

## rq-scheduler process: ticks and enqueues due schedules (session 2.1).
## Needs a running `make worker` to execute the enqueued jobs.
scheduler:
	cd backend && .venv/bin/python -m app.workers.scheduler

test:
	cd backend && .venv/bin/pytest

lint:
	cd backend && .venv/bin/ruff check .

stop:
	$(COMPOSE) down
