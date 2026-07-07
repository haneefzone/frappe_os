# CLAUDE.md — FDM Platform

Read this file fully before doing anything. It is the constitution for this project. Rules here override anything else unless the user explicitly says otherwise in the session.

## What this project is

A **standalone, self-hosted web control panel** for managing **bare-metal Frappe/ERPNext deployments** — benches, sites, apps, backups, servers — on Ubuntu VMs and physical servers, over SSH. Think "self-hosted Frappe Cloud for bare-metal benches."

- One control plane, **agentless** (all operations over SSH), multi-server capable.
- **NOT a Frappe app.** NOT Docker-based benches. It manages plain `bench init` installs.
- UI must **look like Frappe**: frappe-ui component library, black-and-white theme, **dark mode default**.
- Product name is configurable (white-label ready). Working name: FDM Platform.
- Reference docs: `docs/implementation-plan.md` (architecture + phases), `docs/uiux-spec.md` (design system + screens), `docs/PROJECT_PLAN.md` (session roadmap).

## Architecture (fixed decisions — do not change)

| Layer | Choice |
|---|---|
| Backend | Python 3.12+, FastAPI (async), SQLAlchemy 2 + Alembic, Pydantic v2 |
| Platform DB | PostgreSQL (docker-compose in dev); SQLite fallback allowed for tests |
| Job queue | RQ with three priority queues `high, default, low`; Redis |
| SSH | AsyncSSH (pooled connections; PTY via `create_process(term_type='xterm')`) |
| Real-time | WebSocket for interactive terminal; SSE for job-log tailing |
| Frontend | Vue 3 + Vite + TypeScript + frappe-ui + Tailwind (frappe-ui preset) + Pinia + vue-router |
| Secrets | Fernet (`cryptography`), master key from env `FDM_SECRET_KEY` — never stored in DB |
| Terminal | xterm.js + @xterm/addon-attach ↔ WS ↔ AsyncSSH PTY |
| Auth | JWT access (15m) + refresh (7d) in httpOnly cookies, CSRF double-submit, argon2 password hashing, RBAC |

RQ workers are synchronous processes — wrap AsyncSSH coroutines with `asyncio.run()` inside job handlers.

## Golden rules (non-negotiable, every session)

1. **Command safety.** All remote commands come from a **parameterized template registry** (`app/core/commands/`). Templates define an argv list, allowed flags, input validators, and which params are secret. NEVER interpolate raw user input into a shell string. Site names must match `^[a-z0-9][a-z0-9.-]{1,80}$`; repo URLs validated against a host allowlist (github.com, gitlab.com, configurable). `shlex.quote` anything that must pass through a shell.
2. **No silent mutations.** Every state-changing operation creates a `CommandJob` row AND an `AuditLog` row (user, action, entity refs, params-with-secrets-masked, result, source IP).
3. **Nothing long runs in request/response.** Enqueue to RQ, return `202 {job_id}` immediately. Job timeout 4 hours. Graceful worker shutdown configured (long `bench init`/`update` must survive deploys).
4. **Job locking.** Redis lock keyed `(server_id, bench, site, action_class)` — two dangerous operations on the same target must never run concurrently. Second attempt gets HTTP 409 with the blocking job id.
5. **Dangerous actions** (drop site, restore-over-existing, delete bench, drop database): UI requires type-the-target-name-to-confirm AND an automatic pre-action backup job runs first.
6. **Secrets** encrypted at rest with Fernet; API tokens stored hashed (shown once); user passwords argon2. Secrets never appear in logs, job params displays, or error messages — mask as `••••`.
7. **RBAC on every endpoint.** Roles: Admin, Developer, Operator, Read-only. Read-only can never mutate.
8. **Timestamps** stored UTC, rendered in user TZ (default `Asia/Dubai`), relative with absolute-on-hover.
9. **Frontend data layer** is the typed `apiClient` in `frontend/src/api/` — do NOT use frappe-ui's `createListResource`/doctype semantics. frappe-ui is used for components and design tokens only.
10. **Every session ends with:** Alembic migration for any model change, tests passing, `ruff check` clean, frontend builds with zero errors, a git commit, and copy-paste verification commands with expected output for a non-technical user.

## Job engine pattern

Central `JobRunner` service:

- `runner.create(action_name, server_id, target, params, priority)` → validates against the template registry, creates `CommandJob` (status `pending`), acquires lock, enqueues `execute_job(job_id)` to RQ, returns job id.
- Worker `execute_job(job_id)`: loads job, sets `running`, executes the registered handler.
- Handlers structure work with a step context manager: `with ctx.step("Validate prerequisites"): ...` — each step writes a `CommandStep` row (order, status, started/ended, error traceback on failure).
- Output streaming: the SSH executor yields lines; persist `LogEntry` rows in batches (every 25 lines or 500 ms) AND publish to Redis pub/sub channel `job:{id}:logs`.
- Terminal state: `success` / `failure` / `cancelled`, with exit code and duration. Idempotent jobs may auto-retry up to 3×; destructive jobs never auto-retry.

SSE endpoint `GET /api/jobs/{id}/logs/stream`: replays persisted lines from `?after_seq=N`, then live-tails pub/sub; heartbeat comment every 15s; header `X-Accel-Buffering: no`.

## Folder structure

```
fdm-platform/
├── CLAUDE.md
├── backend/
│   ├── app/
│   │   ├── main.py            # app factory
│   │   ├── config.py          # Pydantic Settings (env-driven)
│   │   ├── db.py
│   │   ├── models/            # SQLAlchemy models
│   │   ├── schemas/           # Pydantic request/response
│   │   ├── api/
│   │   │   ├── deps.py        # auth/RBAC dependencies
│   │   │   └── routes/        # auth, servers, benches, sites, apps, backups, jobs, terminal, monitoring, settings
│   │   ├── core/
│   │   │   ├── security.py    # hashing, JWT, Fernet SecretsService
│   │   │   ├── ssh.py         # AsyncSSH service + pool
│   │   │   ├── jobs.py        # JobRunner, ctx.step, locking
│   │   │   ├── commands/      # template registry + validators
│   │   │   ├── streaming.py   # SSE/WS, Redis pub/sub
│   │   │   ├── monitoring.py
│   │   │   └── backups.py
│   │   ├── workers/           # RQ entrypoints
│   │   └── audit.py
│   ├── alembic/
│   ├── tests/
│   └── pyproject.toml
├── frontend/
│   ├── src/
│   │   ├── api/               # typed apiClient
│   │   ├── components/        # StatusDot, DataTable, JobTimeline, LogViewer, ConfirmModal, Wizard, ...
│   │   ├── pages/
│   │   ├── stores/
│   │   ├── router/
│   │   └── main.ts
│   ├── tailwind.config.js
│   └── vite.config.ts
├── docs/
├── docker-compose.dev.yml     # Postgres + Redis for the PLATFORM (not managed benches)
├── deploy/                    # systemd units, nginx conf for the platform itself
├── Makefile
└── .env.example
```

## Design system (summary — full spec in docs/uiux-spec.md)

Dark default. Tokens:
`--bg-base:#0A0A0B` · `--bg-surface:#111113` · `--bg-raised:#17171A` · `--border:#232326` · `--border-strong:#2E2E33` · `--text-primary:#F4F4F5` · `--text-secondary:#A1A1AA` · `--text-muted:#6B6B74` · accent = white (primary buttons are white bg / black text).
Status colors are the ONLY colors: ok `#22C55E`, warn `#F59E0B`, err `#EF4444`, running/info `#3B82F6`.
UI font Inter; logs/terminal JetBrains Mono. Cards flat, 1px border, 8px radius, no shadows. Motion 150ms ease-out only; skeletons for loading; spinners only inside buttons and job steps.
Buttons carry verbs ("Create bench", never "Submit"). Destructive modals: consequence list + type-name-to-confirm + single red verb button.

## Frappe domain knowledge (encode this — it is the product)

**Version matrix (enforce in pre-flight checks and UI):**

| Frappe | Python | Node | MariaDB | Bench tooling |
|---|---|---|---|---|
| v14 | 3.10 | 16–18 | 10.6+ | pip/virtualenv era |
| v15 | 3.11–3.12 | 18–20 | 10.6+ | pipx + uv hybrid |
| v16 | 3.14 | 24 | 10.6+ (11.8 recommended) | uv-based |

**Validated gotchas (from real installs — never regress these):**
1. bench refuses to run as root → all bench commands execute over SSH as the bench-owner user (e.g. `frappe`).
2. The **current bench CLI requires `uv` installed even for v14/v15 benches** (it runs `uv venv env --seed` during `bench init`). Pre-flight must check `uv --version`.
3. Dev benches: bench's own Redis (**queue :11000, cache :13000**) must be running before `install-app` or any site op that enqueues background jobs, else `Error 111 connecting to 127.0.0.1:11000`. Start `redis-server config/redis_queue.conf --daemonize yes` (and cache), and shut them down after (`redis-cli -p 11000 shutdown nosave`) so a later `bench start` can bind.
4. `bench new-site` non-interactive: `--mariadb-root-username root --mariadb-root-password <pw> --admin-password <pw> --mariadb-user-host-login-scope='%'`. The old `--no-mariadb-socket` is deprecated. Omitting the username causes an interactive prompt that hangs jobs.
5. MariaDB ≥ 11.6 needs `innodb_snapshot_isolation = OFF` for Frappe.
6. wkhtmltopdf must be the **patched-Qt 0.12.6.1** build, not the distro package.
7. Restore: after restoring db/files, copy `encryption_key` from the source `site_config.json` into the target site config, then run `bench migrate`. Use `--force` restore flags. Never allow version downgrades.
8. Multi-bench port allocation: parse each bench's `sites/common_site_config.json` (`webserver_port`, `socketio_port`, `redis_*` ports, `file_watcher_port`); detect conflicts before creating a new bench on the same server.

## How to run (dev)

```
make dev        # docker compose up postgres+redis, uvicorn backend, vite frontend
make worker     # rq worker high default low
make test       # backend pytest + frontend vitest
make lint       # ruff + eslint
```

## Working style for sessions

- Implement ONLY the current session's scope from `docs/PROJECT_PLAN.md`. Do not build ahead.
- Before coding: `git log --oneline -10` and inspect the tree to confirm state.
- The user is non-technical: at the end of every session, give exact copy-paste verification commands and describe what they should see.
- Commit at the end with a conventional message (`feat(scope): ...`), and tick the session checkbox in `docs/PROJECT_PLAN.md`.
