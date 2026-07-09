# FDM Platform

A standalone, self-hosted web control panel for managing bare-metal Frappe/ERPNext
deployments — benches, sites, apps, backups, servers — over SSH.

This repo is a monorepo:

```
backend/    FastAPI API (Python 3.12+)
frontend/   Vue 3 + Vite + frappe-ui SPA (dark mode default)
docs/       Architecture, UI/UX spec, session roadmap
```

Read `CLAUDE.md` first — it is the project constitution.

## Install (one command)

On a fresh Ubuntu 22.04 / 24.04 server:

```bash
curl -fsSL https://raw.githubusercontent.com/Duncan-and-Ross/fdm-platform/main/install.sh | sudo bash
```

> On an air-gapped box, run the identical installer from a checkout instead:
> `sudo bash install.sh`

The installer:

- installs missing prerequisites: uv (+ managed Python 3.12+), Node 20
  (frontend build only), PostgreSQL 16 (PGDG), Redis 7;
- installs the backend, builds the frontend, and serves both from one port;
- generates a `.env` with per-install secrets (Fernet key, JWT secret,
  DB password) — nothing shared, nothing committed;
- runs migrations, seeds the admin user, starts `fdm-api` + `fdm-worker`
  under systemd, then prints the URL and one-time admin credentials.

It is **idempotent**: re-running upgrades code and dependencies, re-runs
migrations, and restarts services without touching secrets, the database,
or the admin password. Tunables (`FDM_HOME`, `FDM_PORT`, `FDM_ADMIN_EMAIL`,
`FDM_DATABASE_URL`, …) are documented in the header of `install.sh`.

The install serves plain HTTP; before exposing it beyond a trusted network,
put it behind an HTTPS reverse proxy and set `COOKIE_SECURE=true` in the
generated `backend/.env`.

Everything below is the **development** setup.

## Prerequisites

| Tool | Version | Why |
|---|---|---|
| Python | 3.12+ | backend |
| [uv](https://docs.astral.sh/uv/) | latest | Python env + package manager |
| Node.js | 20+ | frontend |
| Docker Desktop (or any Docker with compose v2) | latest | Postgres 16 + Redis 7 for the platform itself |
| GNU Make | any | task runner (`make dev`, `make test`, …) |

## Running on Windows 11

The compose services (Postgres + Redis) are **required** and run in Docker Desktop.
The backend and frontend can run directly on the host or inside a Linux VM/WSL2 —
your choice. The recommended path is **WSL2 (Ubuntu)**, which gives you `make`,
Python, and Node in one place:

1. Install **Docker Desktop** and enable the WSL2 backend.
2. In an Ubuntu (WSL2) terminal, install tools:
   ```bash
   sudo apt update && sudo apt install -y make python3.12 nodejs npm
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
3. Clone the repo and set up:
   ```bash
   git clone <repo-url> fdm-platform
   cd fdm-platform
   cp .env.example .env
   make setup
   ```
4. Start everything:
   ```bash
   make dev
   ```

If you prefer running backend/frontend **natively on Windows** (PowerShell,
without make): start the services with
`docker compose -f docker-compose.dev.yml up -d`, then in two terminals:

```powershell
# terminal 1 — backend
cd backend
uv venv; uv pip install -e ".[dev]"
.venv\Scripts\uvicorn app.main:app --reload --port 8000

# terminal 2 — frontend
cd frontend
npm install
npm run dev
```

## What you get

- API: http://localhost:8000 — health check at `/api/health`
- Web UI: http://localhost:5173 — dark "FDM Platform" shell
- Postgres on `localhost:5432` (user/pass/db: `fdm`/`fdm`/`fdm`), Redis on `localhost:6379`

## Make targets

| Target | What it does |
|---|---|
| `make setup` | one-time: backend venv + frontend `npm install` |
| `make dev` | compose up Postgres+Redis, then backend (uvicorn :8000) + frontend (vite :5173) |
| `make worker` | RQ worker on queues `high default low` (needs Redis up) |
| `make test` | backend pytest |
| `make lint` | ruff check |
| `make stop` | stop the compose services |

## Verify it works

```bash
curl http://localhost:8000/api/health
# → {"status":"ok","version":"0.1.0"}
```

Then open http://localhost:5173 — you should see the dark **FDM Platform** shell.
