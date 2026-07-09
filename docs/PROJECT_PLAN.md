# PROJECT_PLAN.md — FDM Platform Roadmap

Sessions are executed in order, one Claude Code conversation each. A session is done only when its acceptance criteria pass and it is committed. Tick the box when done.

Reference: `CLAUDE.md` (rules), `docs/implementation-plan.md` (architecture), `docs/uiux-spec.md` (design system + screens).

---

## Phase 0 — Scaffold & Foundation

- [x] **0.1 Monorepo scaffold.** Repo layout per CLAUDE.md, FastAPI skeleton with `/api/health`, Vite+Vue3+TS+frappe-ui+Tailwind app shell (dark default), docker-compose.dev.yml (Postgres+Redis), Makefile, .env.example, README.
  *Accept:* `make dev` boots API + SPA; health returns `{"status":"ok"}`; browser shows dark app shell.
- [x] **0.2 Config, DB, app skeleton.** Pydantic Settings, SQLAlchemy 2 + Alembic wired (empty first migration), structured JSON logging, global error handlers, CORS; frontend router + sidebar layout with all nav groups (empty placeholder pages), light/dark toggle.
  *Accept:* `alembic upgrade head` runs; all sidebar routes render placeholders; toggle persists.
- [x] **0.3 Core component library + design tokens.** Tailwind tokens from CLAUDE.md; build StatusDot, StatusBadge, EnvironmentBadge, KPICard, DataTable (sort/filter/density/sticky header/row actions), ConfirmModal (standard + destructive type-to-confirm), Wizard (stepper), EmptyState, Toast, CopyField, JobTimeline (static), LogViewer (static), Sparkline. Demo everything on a `/styleguide` route.
  *Accept:* `/styleguide` renders every component in both themes; destructive modal blocks until exact name typed.
- [x] **0.4 Dev & test workflow docs.** `docs/dev-setup.md`: running control plane against Hyper-V Ubuntu VMs, SSH reachability, snapshot-before-destructive-test workflow (Checkpoint-VM PowerShell snippet), throwaway-VM policy.
  *Accept:* doc is followable start-to-finish by a non-technical user.

## Phase 1 — MVP (vertical slice on one Hyper-V VM)

- [x] **1.1 Auth & RBAC.** User/Role/ApiToken models + migration; argon2; JWT httpOnly cookies + CSRF; login/logout/me endpoints; RBAC dependency; seed admin CLI; login page; route guards; user menu.
  *Accept:* login works; Read-only user gets 403 on a mutating endpoint; wrong password rate-limited.
- [x] **1.2 Server registry & SSH test.** Server + SSHCredential models; Fernet SecretsService; AsyncSSH service with pooling + known-host pinning; Add Server sheet wizard (identity → auth: paste/upload/generate keypair with copy-pubkey instructions → live streamed Test Connection: SSH ✓, sudo ✓, OS ✓, detected tools table); servers list + detail Overview tab.
  *Accept:* register the Hyper-V VM; test connection streams checks and stores results; private key unreadable in DB (Fernet token).
- [x] **1.3 Job engine core.** CommandJob/CommandStep/LogEntry models; RQ queues high/default/low; JobRunner + ctx.step; command template registry with validators + secret masking; Redis job locking (409 on conflict); 202+job_id pattern; retry (3×, idempotent only); jobs list/detail API; 4h timeout + graceful worker shutdown configured.
  *Accept:* a demo `echo`-over-SSH job runs through the full lifecycle; concurrent duplicate returns 409; unit tests prove injection-unsafe input is rejected.
- [x] **1.4 Live logs.** Worker persists LogEntry batches + publishes to pub/sub; SSE endpoint with replay + tail + heartbeat; Jobs page (live table) + Job detail (step timeline left, live LogViewer right, sanitized params, "Show exact command" expander, role-gated Cancel/Retry); persistent bottom-right job tray.
  *Accept:* watching a 60s multi-step demo job, logs stream live, steps tick, tray shows progress, refresh mid-job resumes cleanly.
  *Carry-in (DOO-94 #2 → owned by DOO-96):* on idempotent auto-retry, each attempt restarts `CommandStep.order` at 1, so a 3-step action retried writes orders 1,2,3,1,2,3 — the step-timeline UI can't tell attempts apart. DOO-96 adds a 1-based `CommandStep.attempt` discriminator (Alembic migration + `StepOut.attempt`); build the timeline to group by `attempt` then render each attempt's steps in `order`.
- [ ] **1.5 Browser SSH terminal.** POST /terminal/sessions issues short-lived ticket; WS bridges xterm.js ↔ AsyncSSH PTY; resize handling; multiple tabs; context bar; idle timeout; Developer+ only; audit row per session.
  *Accept:* open terminal to the VM, run `htop`, resize works, idle disconnect fires, session appears in audit log.
- [x] **1.6 Bench discovery & list.** Discovery job: scan for benches, parse `sites/common_site_config.json` (ports), `bench version`; Bench model + list UI grouped by server with version chips and port map.
  *Accept:* existing v15/v16 benches on the VM appear with correct versions and ports.
- [ ] **1.7 Bench create.** Wizard (server → version radio cards showing the matrix → streamed pre-flight job: uv/node/mariadb/wkhtmltopdf present, ports free, disk → name/path → review with exact commands → job). Encode version matrix + gotchas #1–#6 from CLAUDE.md.
  *Accept:* create a fresh v16 bench on the VM end-to-end from the UI with live logs; pre-flight blocks when uv is missing.
- [ ] **1.8 Site list & create.** Site model; create wizard (bench → validated name → generated admin password → review → job) using gotcha #4 flags; handles dev-bench Redis gotcha #3 (start 11000/13000, stop after); site list with health dot + scheduler state; site detail Overview with quick actions (open ↗, maintenance toggle, scheduler toggle).
  *Accept:* create `test1.localhost` on the v16 bench from UI; site loads in browser; no interactive prompt ever hangs the job.
- [ ] **1.9 App install (ERPNext / GitHub).** AppSource + InstalledApp models; add-source flow (URL → branch fetch → compatibility warning); `bench get-app --branch` + `install-app` jobs; private repo deploy-key support; installed-apps table on site detail.
  *Accept:* install ERPNext on the test site from UI; install one custom GitHub app by URL+branch.
- [ ] **1.10 Maintenance actions.** Migrate / clear-cache / clear-website-cache / build / restart as jobs from bench + site detail, each behind ConfirmModal; bulk "migrate all sites on bench".
  *Accept:* each action produces a job with correct logs and audit rows.
- [ ] **1.11 Backup & restore.** Backup model; `bench backup --with-files` job parsing the 3 artifacts + config, sha256 checksums, size; Backups page with filters; guided Restore flow (pick/validate → target same/new-site/other-bench with compatibility check → red review + type-to-confirm → auto pre-restore backup → restore job → auto encryption_key copy + migrate → success card). Gotcha #7 enforced.
  *Accept:* backup the seeded site; restore to a NEW site; a known record survives; restoring OVER a site requires typed confirmation and takes a pre-backup first.
- [ ] **1.12 Dashboard v1, monitoring, audit, settings.** SSH-polled CPU/RAM/disk + service states (nginx/mariadb/redis/supervisor); Dashboard per uiux-spec (KPI cards incl. Fleet Health, morning-brief line, servers strip, backup 7-day grid, first-run onboarding empty state); Audit Log page (filterable, CSV export); Settings (general incl. product name/logo + TZ, defaults).
  *Accept:* dashboard answers "is everything okay?" in 10 seconds; audit shows every action from sessions 1.1–1.11; MVP DEMO: register server → create bench → create site → install ERPNext → migrate → backup → restore, entirely from the UI.
  *Carry-in (DOO-94 #3):* golden rule 2 wants an `AuditLog` row (incl. **source IP**) per state-changing op; 1.3–1.11 wrote only `CommandJob`. When adding AuditLog here, capture source IP at job-create time (thread the request `X-Forwarded-For`/client host into `JobRunner.create`) going forward, and explicitly accept that 1.3–1.11 history predates it (no back-fill of source IP is possible).

## Phase 2 — Backups at scale, SSL/Domains, Multi-server
2.1 Scheduler (RQ-scheduler) + backup schedules/retention · 2.2 S3-compatible storage + upload/verify/download · 2.3 Backup compliance engine (RPO/RTO policies, dashboard %) · 2.4 Domains & SSL (nginx vhost gen, `nginx -t` + filelock, certbot, DNS check, expiry tracking) · 2.5 Production setup job (`bench setup production` with config pre-backup) · 2.6 Multi-server hardening + per-server dashboards · 2.7 Uptime HTTP checks + response-time charts · 2.8 Command palette (⌘K) + notification center.

## Phase 3 — Alerts, Safe Updates, Schedules
3.1 AlertRule engine + email/webhook channels · 3.2 Update advisor (release/security detection, changelog preview) · 3.3 Safe update pipeline (clone → staging → verify checklist → promote, rollback path) · 3.4 Restore-test automation (scheduled restore→verify→destroy, "restore-tested" badge) · 3.5 Schedules calendar + maintenance windows (block dangerous jobs).

## Phase 4 — Full-system DR
4.1 restic per server → S3 (config tier: nginx/supervisor/redis/mariadb configs + `dpkg --get-selections`) · 4.2 Weekly system snapshots + retention + `restic check` · 4.3 DR runbook generator · 4.4 Compliance report exports (audit + backup evidence, ISO-friendly).

## Phase 5 — AI
5.1 AI Agents module (register Claude Code etc.: command template, working-dir scope, read-only mode, pre-change backup, allowed servers; session in terminal; diff review → apply/rollback) · 5.2 Panel copilot ("Ask AI to analyze" on failed jobs; NL actions in palette).

## Phase 6 — Product polish
6.1 Tool installer module (stack components vs version matrix) · 6.2 Reports suite + scheduled email delivery · 6.3 Platform self-backup + master-key escrow runbook · 6.4 First-run installation wizard · 6.5 2FA (TOTP) + recovery codes + session management UI · 6.6 White-label settings surface · 6.7 Drift detection (stretch).
