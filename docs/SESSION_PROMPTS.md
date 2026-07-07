# SESSION_PROMPTS.md — Copy-paste prompts for Claude Code

How to use: one session = one Claude Code conversation, run in order. Paste the prompt, let it work, run the verification commands it gives you, and only move to the next session when acceptance passes and the commit is made. If something fails, stay in the same conversation and say "acceptance failed: <paste error>" until it passes.

Every prompt assumes `CLAUDE.md` is in the repo root and the docs are in `docs/` — Claude Code reads CLAUDE.md automatically.

---

## ONE-TIME KICKOFF (paste first, before Session 0.1)

```
This is a brand-new project. Read CLAUDE.md, docs/PROJECT_PLAN.md, docs/implementation-plan.md, and docs/uiux-spec.md fully.

Then give me: (1) a 10-line summary of what we are building and the fixed architecture, (2) the golden rules you will follow in every session, (3) any contradiction or ambiguity you found between the documents that I should resolve before we start. Do not write any code yet.
```

---

## PHASE 0

### Session 0.1 — Monorepo scaffold

```
Read CLAUDE.md. This is Session 0.1 of docs/PROJECT_PLAN.md. Implement ONLY this session's scope.

GOAL: Scaffold the monorepo exactly per the folder structure in CLAUDE.md. No features.

BUILD:
- backend/: FastAPI app factory, uvicorn entrypoint, GET /api/health returning {"status":"ok","version":"0.1.0"}, pyproject.toml (fastapi, uvicorn, sqlalchemy, alembic, pydantic-settings, rq, redis, asyncssh, cryptography, argon2-cffi, pytest, ruff), ruff configured.
- frontend/: Vite + Vue 3 + TypeScript + frappe-ui + Tailwind with the frappe-ui preset, dark mode as DEFAULT (class strategy), a minimal app shell page saying "FDM Platform" using the design tokens from CLAUDE.md.
- docker-compose.dev.yml: postgres:16 and redis:7 for the PLATFORM's own use, with volumes and healthchecks.
- Makefile: dev (compose up -d + backend + frontend), worker (rq worker high default low), test, lint, stop.
- .env.example with every variable the backend reads (DATABASE_URL, REDIS_URL, FDM_SECRET_KEY, JWT_SECRET, CORS_ORIGINS, DEFAULT_TZ=Asia/Dubai).
- README.md: prerequisites and how to run on Windows 11 (backend/frontend run on host or in a VM; compose services required).

CONSTRAINTS: No auth, no models, no extra pages. If frappe-ui + Tailwind preset wiring has version issues, resolve them now — this is the session's main risk.

ACCEPTANCE: `make dev` boots everything; curl /api/health returns ok; browser shows the dark shell. Initialize git, commit "chore: scaffold monorepo", then give me exact copy-paste verification commands (I am non-technical) with what I should see for each.
```

### Session 0.2 — Config, DB, app skeleton

```
Read CLAUDE.md. This is Session 0.2 of docs/PROJECT_PLAN.md. Run `git log --oneline` first to confirm state. Implement ONLY this session's scope.

BUILD:
- backend: Pydantic Settings in app/config.py loading .env; SQLAlchemy 2 engine/session in app/db.py; Alembic wired with an empty initial migration; structured JSON logging; global exception handlers returning consistent {"error": {code, message}} shapes; CORS from settings.
- frontend: vue-router with the full sidebar IA from docs/uiux-spec.md B3 (Dashboard; Infrastructure: Servers/Benches/Sites/Apps; Data Protection: Backups/Restore; Operations: Jobs/Monitoring/Logs/Terminal; Automation: Schedules/Tools/AI Agents; Governance: Users & Roles/Audit Log/Security/Reports; Settings). Each route renders a placeholder page with an EmptyState-style message. Collapsible sidebar, fixed top bar with placeholder search, light/dark toggle persisted to localStorage (dark default).

ACCEPTANCE: `alembic upgrade head` succeeds against the compose Postgres; every sidebar item navigates; toggle persists across refresh. Commit "feat: config, db, routing shell" and give verification commands.
```

### Session 0.3 — Design tokens + core component library

```
Read CLAUDE.md and docs/uiux-spec.md sections B1, B2, B5. This is Session 0.3. Implement ONLY this session's scope.

GOAL: Build the reusable component library BEFORE any feature screens, so every later session composes from it.

BUILD (frontend/src/components/, TypeScript, frappe-ui primitives where sensible):
- Tokens: implement the exact CSS variables/Tailwind theme from CLAUDE.md (dark + light).
- StatusDot, StatusBadge, EnvironmentBadge (PROD red-outline / STAGING amber / DEV grey).
- KPICard (label, big value, delta/sublabel, optional status).
- DataTable: generic, typed; sortable columns, text filter, column chooser, compact/comfortable density, sticky header, row-action slot, empty + loading (skeleton rows) states.
- ConfirmModal: standard variant and destructive variant (red header, consequence list slot, "what will be backed up first" slot, type-the-exact-name-to-confirm input that disables the single red verb button until it matches).
- Wizard: stepper header, back-safe navigation, review-step slot, emits submit.
- EmptyState (icon, title, line, primary CTA), Toast service, CopyField (masked secret + copy), Sparkline (SVG, monochrome + status accent).
- JobTimeline (static for now): vertical steps with ○ pending, ◐ running (spinner + elapsed), ● done (duration), ✕ failed (expandable error slot).
- LogViewer (static for now): mono font, line numbers, search, error-line highlighting, follow-tail toggle, download button, fullscreen.
- /styleguide route demonstrating every component with realistic fake data, in both themes.

ACCEPTANCE: /styleguide renders all components both themes; destructive modal genuinely blocks until exact name typed; DataTable handles 1,000 fake rows smoothly. Commit "feat(ui): design tokens + core component library" and give verification steps.
```

### Session 0.4 — Dev & test workflow docs

```
Read CLAUDE.md. This is Session 0.4. Write docs/dev-setup.md for a non-technical user on Windows 11 with Hyper-V Ubuntu VMs:
- Running the platform (compose + backend + frontend) and the RQ worker.
- Preparing a managed test VM: which user the platform SSHes as (bench owner, e.g. 'frappe'), how to paste the platform's public key into authorized_keys, required sudoers allowlist snippet from docs/implementation-plan.md security section.
- The snapshot-before-destructive-test workflow: PowerShell Checkpoint-VM / Restore-VMCheckpoint commands, and the rule that destructive integration tests only run against a designated throwaway VM.
- Troubleshooting table (SSH refused, port already in use, compose services unhealthy).
ACCEPTANCE: I can follow it top to bottom. Commit "docs: dev setup and VM test workflow".
```

---

## PHASE 1

### Session 1.1 — Auth & RBAC

```
Read CLAUDE.md. This is Session 1.1 of docs/PROJECT_PLAN.md. Check git state first. Implement ONLY this scope.

BUILD:
- Models + Alembic migration: User (email, password_hash, full_name, is_active, role_id, last_login), Role (name, permissions JSON), ApiToken (user_id, name, token_hash, expires_at, last_used).
- Seed CLI: `python -m app.seed --admin-email ... --admin-password ...` creating the four roles (Admin, Developer, Operator, Read-only) with a sensible permission matrix by action-class, and the admin user.
- Auth: argon2 hashing; login endpoint issuing JWT access (15m) + refresh (7d) as httpOnly Secure SameSite=Lax cookies; CSRF double-submit token; /api/auth/me; logout; refresh; login rate-limiting with temporary lockout.
- RBAC dependency `require(permission)` used by all future routers; permission checks derive from Role.permissions.
- Frontend: login page (frappe-style card on dark bg), Pinia auth store, router guards, user menu with logout, 401 interceptor redirecting to login.
- Tests: login flow, RBAC denial (read-only gets 403 on a mutating test route), lockout.

ACCEPTANCE: seed admin, log in via UI, /api/auth/me works, read-only user blocked from mutation, 6 wrong passwords locks temporarily. Commit "feat(auth): sessions, rbac, login" and give verification steps including the exact seed command.
```

### Session 1.2 — Server registry & SSH test

```
Read CLAUDE.md. This is Session 1.2. Implement ONLY this scope.

BUILD:
- Models + migration: Server (name, hostname, ssh_port, os_version, status, env_tag prod/staging/dev, tags, notes), SSHCredential (server_id, username, auth_type key/password, private_key_enc, passphrase_enc, password_enc, known_host_key, sudo_mode).
- SecretsService (app/core/security.py): Fernet encrypt/decrypt using FDM_SECRET_KEY; refuse to boot if key missing/malformed; helper to generate an ed25519 keypair server-side (private key immediately encrypted; public key returned once for the user to install).
- SSHService (app/core/ssh.py): AsyncSSH connect with known-host pinning (store host key on first connect, verify thereafter), pooled connections per server, `run(argv, cwd=None, timeout=)` streaming lines, and `check_connection()` returning structured results: ssh ok, whoami, sudo -n true ok, lsb_release, and detected tool versions (git, python3, uv, node, mariadb, redis-server, wkhtmltopdf, bench) — each via safe fixed argv, no user input.
- API: CRUD /api/servers (RBAC: Admin/Developer manage, others read), POST /api/servers/{id}/test streaming results via SSE.
- Frontend: Servers list (DataTable: StatusDot, name, EnvironmentBadge, IP, OS, last-seen) + "Add Server" right-side sheet wizard per uiux-spec B4.2 (identity → auth method paste/upload/generate-with-copy-pubkey-instructions → live Test Connection rendering each check as it streams → save). Server detail page with Overview tab (specs + detected tools table + re-test button).
- Tests: secrets round-trip; host-key mismatch rejected; check_connection parsing with mocked SSH.

CONSTRAINTS: private keys and passwords must never appear in logs, API responses, or the DB in plaintext (Fernet tokens only).

ACCEPTANCE: I register my Hyper-V VM through the wizard, watch checks stream, and see detected tools; the DB row shows an unreadable Fernet token. Commit "feat(servers): registry, secrets, ssh test".
```

### Session 1.3 — Job engine core

```
Read CLAUDE.md (Job engine pattern + Golden rules 1–4) — this is the most important session in the project. This is Session 1.3. Implement ONLY this scope.

BUILD:
- Models + migration: CommandJob (server_id, target_type/target_id, action_name, priority, status pending/running/success/failure/cancelled, rq_job_id, params_sanitized JSON, lock_key, retry_count, exit_code, started_at, ended_at, created_by), CommandStep (job_id, name, order, status, started_at, ended_at, error_traceback), LogEntry (job_id, seq, stream, content, ts).
- Command template registry (app/core/commands/): CommandTemplate dataclass {action_name, argv template list, cwd template, validators per param (regex/enum), secret_params set, action_class, idempotent bool, requires_lock bool, run_as user}. Include starter templates: system.echo_demo (multi-step demo), server.detect_tools. Central `render(template, params)` that validates every param, shlex-quotes, and returns final argv + a display string with secrets masked.
- JobRunner (app/core/jobs.py): create() per CLAUDE.md — validate, persist, acquire Redis lock (409 with blocking job id on conflict), enqueue to the right queue, return id. Worker execute_job(): status transitions, ctx.step contextmanager writing CommandStep rows, SSH execution streaming lines → LogEntry batches (25 lines/500ms) + Redis pub/sub `job:{id}:logs`, retry ≤3 only when idempotent, 4h RQ timeout, cancellation support (best-effort: send signal via SSH process close), graceful shutdown honored.
- API: POST /api/jobs (action_name+target+params), GET /api/jobs (filters: status/server/target/user/mine), GET /api/jobs/{id} (job+steps+params_sanitized), POST /api/jobs/{id}/cancel, /retry (RBAC-gated).
- workers/: entrypoint + Makefile `worker` target confirmed working.
- Tests (this session must be heavily tested): template rendering rejects `; rm -rf /`, backticks, $(), newlines in params; secrets masked in params_sanitized and logs; lock conflict → 409; step failure marks job failed with traceback; retry only for idempotent; cancellation transitions correctly.

ACCEPTANCE: run system.echo_demo against my VM via curl or a temporary UI button: job goes pending→running→success with 3 steps and streamed-to-DB logs; launching it twice concurrently on the same target returns 409. Commit "feat(jobs): engine, templates, locking".
```

### Session 1.4 — Live logs + Jobs UI + job tray

```
Read CLAUDE.md and docs/uiux-spec.md B4.8. This is Session 1.4. Implement ONLY this scope.

BUILD:
- SSE endpoint GET /api/jobs/{id}/logs/stream: replay LogEntry from ?after_seq, then live-tail Redis pub/sub; 15s heartbeat; X-Accel-Buffering: no; close cleanly on client disconnect.
- Frontend: Jobs page (live-updating DataTable via polling every 3s or a lightweight jobs SSE; status pulse on running). Job detail page: JobTimeline (now live) left, LogViewer (now live: connects SSE, follow-tail, resumes with after_seq on reconnect) right; header with sanitized params, "Show exact command" expander, Cancel/Retry (RBAC-gated); failed step auto-expanded with stderr highlighted.
- Persistent job tray (bottom-right, global): collapsed pill "N jobs running", expands to mini-list with per-job status, click-through to detail. Driven by a Pinia jobs store.
- Wire the demo job to a real "Run demo job" button on the server detail page so everything is exercisable from the UI.

ACCEPTANCE: I start the demo job from the UI and watch steps tick and logs stream live; I refresh mid-job and the log resumes without gaps; the tray shows it globally. Commit "feat(jobs): live streaming ui + tray".
```

### Session 1.5 — Browser SSH terminal

```
Read CLAUDE.md and docs/uiux-spec.md B4.11. This is Session 1.5. Implement ONLY this scope.

BUILD:
- POST /api/terminal/sessions {server_id} (RBAC: Developer+) → creates an audited session record + returns a short-lived (60s, single-use) ticket.
- WS /api/terminal/ws?ticket=... → validates ticket, opens AsyncSSH create_process(term_type='xterm') as the stored SSH user, bridges bytes both ways, handles {"type":"resize",cols,rows}, idle timeout (configurable, default 15 min) with a 60s warning message injected, closes + finalizes audit row (duration) on disconnect.
- Frontend Terminal page: xterm.js + fit addon; multiple tabs (one WS each); context bar showing user@server with quick-insert buttons ("cd ~/frappe-bench", "bench --site "); idle countdown indicator; clean reconnect UX.
- Security: never expose private keys to the frontend; ticket single-use; Read-only/Operator users don't even see the page (route guard + API 403).

ACCEPTANCE: open a terminal to my VM, run htop, resize the window and it reflows, leave it idle and it warns then disconnects, and the session (user, server, start, duration) appears in the jobs/audit data. Commit "feat(terminal): browser ssh".
```

### Session 1.6 — Bench discovery & list

```
Read CLAUDE.md (Frappe domain knowledge — the version matrix and common_site_config parsing). This is Session 1.6. Implement ONLY this scope.

BUILD:
- Bench model + migration: server_id, name, path, frappe_version, python_version, node_version, webserver_port, socketio_port, redis_cache_port, redis_queue_port, redis_socketio_port, file_watcher_port, is_production, status, discovered_at.
- Discovery job template bench.discover: safely lists candidate dirs (home of the SSH user, configurable base paths), identifies benches (has apps/frappe and sites/), reads sites/common_site_config.json for ports, runs `bench version --format json` (fallback: parse plain) inside each bench; upserts Bench rows; marks vanished benches.
- API: GET /api/benches (+by server), POST /api/servers/{id}/discover-benches → job.
- Frontend: Benches page grouped by server; rows show version chip (v14/v15/v16 colored by status tokens only — grey chip with text), py/node versions, port map popover, mode badge, site count placeholder, "Discover" button per server. Bench detail page skeleton with Overview tab rendering parsed config.

ACCEPTANCE: after Discover, my existing v15 and v16 benches appear with correct versions and ports; deleting a bench dir on the VM and re-discovering marks it missing. Commit "feat(benches): discovery + list".
```

### Session 1.7 — Bench create

```
Read CLAUDE.md gotchas #1, #2, #5, #6, #8 and the version matrix. This is Session 1.7. Implement ONLY this scope.

BUILD:
- Command templates: bench.preflight (checks per selected version: uv --version present (gotcha #2), node major version matches matrix, mariadb version + innodb_snapshot_isolation when ≥11.6 (gotcha #5), wkhtmltopdf patched-qt (gotcha #6), target ports free vs sibling benches' common_site_config (gotcha #8), disk space ≥ 5GB) — each check is a step with pass/fail detail; bench.init (`bench init --frappe-branch {branch} {name}` run as the bench user in their home, long-running, high queue).
- API: POST /api/benches (server, version, name, path) → orchestrated as: preflight job → on success auto-enqueue init job → on success auto-run discovery to register it. Model this as a parent job with child jobs OR sequential steps — choose one, document it.
- Frontend Create Bench wizard per uiux-spec B4.3: server → version radio cards that display the matrix line for the selection ("v16 → Python 3.14 · Node 24 · MariaDB 11.8") → live pre-flight results inline (re-runnable) → name/path with defaults → review step showing the exact commands (collapsed expander) → submit → navigate to job detail.
- Tests: preflight failure blocks init; version/name validation; template injection tests for name/path.

ACCEPTANCE: from the UI I create a brand-new v16 bench on my throwaway VM (snapshot first per docs/dev-setup.md) and watch init stream for several minutes to success; running it with uv removed fails cleanly at preflight with a readable message. Commit "feat(benches): guided create with preflight".
```

### Session 1.8 — Site list & create

```
Read CLAUDE.md gotchas #3 and #4 — they are the heart of this session. This is Session 1.8. Implement ONLY this scope.

BUILD:
- Site model + migration: bench_id, name, status, scheduler_enabled, maintenance_mode, health, created via discovery too (extend bench.discover to list sites/ dirs and read site status where cheap).
- Command template site.create implementing gotcha #4 exactly: bench new-site {site} --mariadb-root-username root --mariadb-root-password {db_root_pw} --admin-password {admin_pw} --mariadb-user-host-login-scope='%' ... with db_root_pw and admin_pw as secret_params; PLUS the dev-bench Redis dance (gotcha #3) as explicit steps: detect production vs dev bench (supervisor conf presence), if dev → start redis-server config/redis_queue.conf and redis_cache.conf --daemonize yes before, and redis-cli -p 11000/13000 shutdown nosave after (best-effort, || true).
- Where does db root pw come from: add a per-server "MariaDB root password" secret field (Fernet) on the Server settings tab; template pulls it server-side, never from the browser.
- Also implement: site.enable_scheduler, site.set_maintenance (on/off) templates.
- API: sites CRUD-lite + create → job; toggles → jobs (fast queue).
- Frontend: Sites page (DataTable: name, bench, EnvironmentBadge inherited, health dot placeholder, scheduler state, created); Create Site wizard (bench → validated name with live regex feedback → admin password field with generate button + strength meter → review with exact command, secrets masked → job). Site detail Overview with quick actions: Open site ↗ (http://ip:port with host-header note), scheduler toggle, maintenance toggle.

ACCEPTANCE: create test1.localhost on the v16 bench entirely from the UI with zero interactive prompts (the job must never hang waiting for input); toggles work; the site loads in my browser. Commit "feat(sites): create + controls".
```

### Session 1.9 — App install (ERPNext / GitHub)

```
Read CLAUDE.md golden rule 1 (repo URL allowlist) and gotcha #3. This is Session 1.9. Implement ONLY this scope.

BUILD:
- Models + migration: AppSource (name, repo_url, default_branch, is_private, deploy_key_enc), InstalledApp (site_id, bench_id, app_source_id nullable for discovered apps, app_name, branch, version, installed_at).
- Templates: app.get (bench get-app --branch {branch} {url_or_name}, validating url against host allowlist github.com/gitlab.com or bare marketplace name ^[a-z0-9_]+$), app.install (bench --site {site} install-app {app}, wrapping the dev-bench Redis steps from 1.8 — reuse that step helper, don't duplicate), app.uninstall (destructive class).
- Private repos: deploy key stored Fernet-encrypted on AppSource; written to a temp key file on the target with 0600 via SSH for the duration of get-app using GIT_SSH_COMMAND, then removed — never logged.
- API: app sources CRUD, POST /api/sites/{id}/apps (get if needed + install as chained steps), branch listing endpoint (git ls-remote --heads via safe template) for the picker.
- Frontend: Apps page (Sources tab + Installed matrix: app × site grid with version chips per uiux-spec B4.5); Add Source flow (URL → fetch branches → pick default → unknown-compatibility warning banner); Site detail Apps tab (installed table, Install app button opening picker: marketplace name or source+branch, Remove behind destructive ConfirmModal).

ACCEPTANCE: install ERPNext (version-16 branch) on test1.localhost from the UI and log into ERPNext afterwards; install one custom app from a GitHub URL with branch selection; uninstall requires typing the app name. Commit "feat(apps): sources + install".
```

### Session 1.10 — Maintenance actions

```
Read CLAUDE.md. This is Session 1.10. Implement ONLY this scope.

BUILD: Templates + API + UI buttons (each behind the appropriate ConfirmModal, each a job with audit): site.migrate, site.clear_cache, site.clear_website_cache, bench.build, bench.restart (production benches: sudo supervisorctl restart via the sudoers allowlist; dev benches: informative failure explaining bench start is manual), bench.migrate_all (parent job iterating sites as steps), bench.update (high queue, long-running, confirm modal warns about downtime, auto pre-step: bench.backup_all_sites lightweight db-only safety backup).
Frontend: Actions section on bench detail and quick actions on site detail wired to these; all land on job detail.

ACCEPTANCE: run migrate and clear-cache on test1.localhost from UI; bench.update on the throwaway bench completes with the safety backup step visible first. Commit "feat(maintenance): core bench/site actions".
```

### Session 1.11 — Backup & restore

```
Read CLAUDE.md gotcha #7 and docs/uiux-spec.md B4.6–B4.7 — restore is a guided flow, not a form. This is Session 1.11. Implement ONLY this scope.

BUILD:
- Backup model + migration: site_id, type (db / with-files), file paths (db, files, private_files, config), size_bytes, checksum_sha256 per artifact, status, taken_by_job_id, restore_tested bool default false.
- Templates: site.backup (bench --site {site} backup --with-files; then sha256sum + stat each artifact; parse output paths from ./sites/{site}/private/backups), backup.validate (checksum verify + read backup metadata/version), site.restore (bench --site {site} --force restore {db_path} --with-public-files {f} --with-private-files {p}; then step: copy encryption_key from source site_config backup into target site_config via a safe python one-liner or bench set-config; then step: bench --site {site} migrate — gotcha #7 exactly), plus pre-restore automatic site.backup step when target exists.
- Restore modes: same site (destructive), new site (site.create first, then restore into it), different bench (compatibility check: source Frappe major ≤ target; block downgrades).
- API: POST /api/sites/{id}/backups, GET /api/backups (filters), POST /api/restores (mode, backup_id, target...), download endpoint streaming artifacts (RBAC Developer+, audited).
- Frontend: Backups page per spec (KPI header placeholders, table with checksum tick + type + size + restore-tested badge, Backup now button site/all); Restore guided flow per B4.7: Step1 pick backup → validation job inline → Step2 target mode with compatibility results → Step3 red review panel (consequences list + automatic pre-restore backup notice + type-site-name-to-confirm) → progress on job detail → success card with "Open site" link.

ACCEPTANCE: backup test1.localhost with files; restore it to a NEW site test2.localhost and verify a record I created beforehand survives; then restore OVER test1 — it must demand the typed name and show a pre-restore backup step in the timeline. Commit "feat(backups): backup + guided restore".
```

### Session 1.12 — Dashboard v1, monitoring, audit, settings (MVP close)

```
Read docs/uiux-spec.md B4.1, B4.9, B4.15-17 and CLAUDE.md. This is Session 1.12, the MVP closer. Implement ONLY this scope.

BUILD:
- Monitoring: scheduled lightweight SSH poll per server (every 60s, configurable): /proc-based CPU+RAM, df for disk, systemctl is-active for nginx/mariadb/redis-server/supervisor; store recent samples (ring buffer table or last-N rows); server detail Overview gets live gauges + services grid with restart buttons (jobs via sudoers allowlist).
- AuditLog: model existed implicitly — formalize (user, action, entity_type, entity_id, summary, params_masked, result, source_ip, ts) and verify every mutating endpoint from 1.1–1.11 writes one (add a shared dependency/decorator; add tests). Audit page: filterable DataTable + CSV export.
- Dashboard v1 per B4.1: KPI cards — Fleet Health % (weighted: uptime placeholder 40 until Phase 2, backup-in-last-24h 30, no-critical-alerts 20 placeholder, updates 10 placeholder — document the formula), Sites n/n, Backups 24h, Failed jobs 24h; morning-brief sentence generated from real data; servers strip with mini-gauges; backup 7-day grid; running jobs feed; first-run onboarding EmptyState (3-step checklist) when no servers exist.
- Settings page: General (product name + logo upload → replaces sidebar branding = white-label layer; default TZ), Defaults (bench base path, port ranges), read-only environment info.
- MVP DEMO script in docs/mvp-demo.md: the exact click-path register server → create bench → create site → install ERPNext → migrate → backup → restore, for me to run end-to-end.

ACCEPTANCE: dashboard answers "is everything okay?" in 10 seconds with real data; audit shows entries from every prior session's actions; I complete the full MVP demo script on a fresh snapshot of my throwaway VM. Commit "feat(mvp): dashboard, monitoring, audit, settings" and tag v0.1.0-mvp.
```

---

## PHASES 2–6 — seed prompts

When you reach these, expand each seed using the SESSION TEMPLATE below plus the relevant sections of docs/PROJECT_PLAN.md, docs/implementation-plan.md and docs/uiux-spec.md. Keep one session per line item.

- **2.1** Scheduler: integrate rq-scheduler; Schedule model; per-site backup schedules + retention sweeps as jobs; Schedules page (list first, calendar later).
- **2.2** S3 storage: StorageTarget model (endpoint, bucket, keys Fernet-enc, test-connection); upload artifacts post-backup with checksum verify; download via presigned URL; storage chip in Backups table.
- **2.3** Backup compliance: RPO/retention policy per site; compliance evaluator job; dashboard Backup Compliance % becomes real; breach alerts groundwork.
- **2.4** Domains & SSL: Domain model; nginx vhost template generation with `nginx -t` gate + filelock + config pre-backup; certbot job; DNS A-record check; SSL expiry tracking feeding dashboard.
- **2.5** Production setup: `bench setup production` job with before-state capture of nginx/supervisor configs; toggle dev→prod on bench.
- **2.6** Multi-server hardening: per-server dashboards; connection-pool limits; move-backup-across-servers job.
- **2.7** Uptime checks: HTTP checker (status+latency) per site every 60s; uptime %, response sparkline on site Overview; Fleet Health uptime component becomes real.
- **2.8** Command palette (⌘K: entities + actions + nav, role-filtered) and notification center (bell drawer, per-user prefs; email/webhook channels minimal).
- **3.1** AlertRule engine (metric, threshold, cooldown) + email (SMTP) and signed webhook channels.
- **3.2** Update advisor: poll frappe/erpnext/app repos for new tags on installed branches; "behind by N" chips; changelog preview.
- **3.3** Safe update pipeline: clone-site-to-staging job (uses restore machinery), update staging, verification checklist UI, promote (update prod with auto pre-backup), rollback path documented in the job.
- **3.4** Restore-test automation: scheduled restore latest backup → scratch site → verify boot + row sanity → destroy → set restore_tested badge.
- **3.5** Schedules calendar view + maintenance windows blocking dangerous action-classes per server.
- **4.1–4.4** restic per server → S3 (config tier: /etc/nginx, /etc/supervisor, mariadb confs, dpkg --get-selections), weekly system snapshots + `restic check`, DR runbook generator, compliance/audit report exports (PDF/CSV).
- **5.1** AI Agents module: AIAgentConfig model; scoped terminal session launcher (working-dir jail via cd + restricted user guidance), read-only toggle, pre-change backup, git-diff review screen → apply/rollback.
- **5.2** Panel copilot: "Ask AI to analyze" on failed jobs — send sanitized log tail + template + step statuses to Claude API, render root cause + suggested fix; NL actions in palette (maps to existing job templates only, never raw shell).
- **6.1–6.7** Tool installer (detected vs recommended per matrix, install jobs), Reports suite + scheduled email, platform self-backup + master-key escrow runbook, first-run installation wizard, TOTP 2FA + recovery codes + session management UI, white-label settings surface, drift detection (config hashes vs baseline).

---

## SESSION TEMPLATE (for expanding Phase 2–6 seeds)

```
Read CLAUDE.md. This is Session X.Y of docs/PROJECT_PLAN.md — read that line item and the matching sections of docs/implementation-plan.md and docs/uiux-spec.md. Run git log first. Implement ONLY this scope.

GOAL: <one sentence>
BUILD: <models+migration / command templates with validators / API with RBAC / UI screens per spec / tests>
CONSTRAINTS: <golden rules that especially apply; what NOT to build>
ACCEPTANCE: <observable checks I can run>
Commit "<conventional message>" and give me copy-paste verification commands with expected output.
```

## RECOVERY PROMPTS (when things go wrong)

- Acceptance failed: `Acceptance failed. Here is the exact output: <paste>. Diagnose the root cause first, explain it in one paragraph, then fix it. Do not change scope.`
- Drift/regression: `Before continuing, audit the codebase against CLAUDE.md golden rules 1–10 and list any violations with file:line, then fix them. No new features.`
- Context reset mid-phase: `Read CLAUDE.md and docs/PROJECT_PLAN.md. Summarize completed sessions from the checkboxes and git log, state what Session X.Y requires, and confirm before coding.`
