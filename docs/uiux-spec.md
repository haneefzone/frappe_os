# FDM Platform — Gap Analysis & Complete UI/UX Specification

Companion to the Implementation Plan. Part A closes functional/strategic gaps; Part B is the full UI/UX spec; Part C maps everything back into the phased plan.

---

# PART A — GAP ANALYSIS

## A1. Strategic gaps (the CEO lens)

**1. Executive dashboard with a fleet health score.** The plan had "basic monitoring." A CEO-level system opens on *answers*, not data: one screen, ten seconds — is everything okay? Composite Fleet Health % (suggested weighting: uptime 40, backup compliance 30, open alerts 20, pending updates 10), sites up n/n, backup compliance %, SSL expiring, failed jobs 24h. Plus a one-line "morning brief": *"All 6 sites healthy. 4 backups completed overnight. 1 update available."*

**2. Safe update pipeline — the single biggest functional gap.** Nothing in the plan protects a production update. Add: **clone site → staging bench → update staging → verification checklist → promote to production (with automatic pre-backup and a visible rollback path)**. This is the bare-metal equivalent of Press's deploy candidates and is what separates a control panel from a script dashboard.

**3. Environment awareness & production guardrails.** Tag every bench/site as **Production / Staging / Development**. Prod gets a red badge everywhere, stricter confirmation flows, role restrictions, and optional maintenance-window enforcement (no dangerous jobs during business hours).

**4. Uptime monitoring & response-time tracking.** External HTTP(S) checks per site (status code + latency), uptime history sparklines, optional shareable status page. Currently the plan only polls server metrics — a server can be green while a site is down.

**5. Update advisor.** Detect new Frappe/ERPNext/app releases and security advisories; surface "3 sites are behind latest version-15" with changelog preview and one-click scheduled update through the safe pipeline.

**6. Compliance pack (ISO 27001 alignment).** Exportable immutable audit trail, access-review report, backup-evidence report, per-site RPO/RTO targets with compliance tracking. Direct synergy with the dr_isms / nexo-isms work — same control families (access control, operations security, backup). This makes the platform itself audit-ready for client environments.

**7. Product potential — decide now, cheap now, expensive later.** Built white-label-ready (logo, product name, theme in Settings; a brand token layer in the design system), this platform is a sellable Doosly/NexoAI product for managing client deployments. Costs almost nothing at Phase 0; retrofitting branding later touches every screen.

## A2. Functional gaps

**8. Site cloning** (prod → staging/dev copy) — required by the update pipeline anyway; add an optional data-scrub hook (mask emails/phones) for prod→dev copies.

**9. Bulk operations.** Backup all sites, update selected benches, restart all workers — executed as one aggregated parent job with per-target child jobs.

**10. Schedules calendar & maintenance windows.** Visual calendar (month/week) of scheduled backups, restore tests, and update windows; conflict warnings; per-server "no dangerous ops" windows.

**11. Backup compliance engine (RPO/RTO).** Per-site policy ("daily backup, 30-day retention, weekly restore test"); dashboard compliance %; alert on breach. Backups without policy tracking are just files.

**12. Restore-test automation.** Scheduled restore of the latest backup to a scratch site → verify (site boots, row-count sanity) → destroy → mark backup "restore-tested." The only real proof backups work.

**13. Session & security management UI.** Active sessions with revoke, failed-login log with lockout status, IP allowlist, password-policy configuration, API token management with one-time display.

**14. Notification center.** In-app bell/drawer with read-unread, per-user channel preferences (in-app / email / webhook per event type).

**15. Activity timeline per entity.** Every server/bench/site detail page gets a unified chronological history (jobs, config changes, backups, alerts). Cheap: it's the audit log rendered per-entity — but only if audit rows carry entity references from day one.

**16. Config drift detection** (stretch). Hash nginx/supervisor/site_config baselines after each managed change; flag manual out-of-band edits.

**17. Panel AI copilot** — distinct from the AI Agents module. "Ask AI to analyze" on any failed job (sends sanitized log + context to Claude, returns root cause + suggested fix); natural-language command palette ("backup all Duncan Ross sites"). On-brand with Intellix and cheap once the job engine exists.

**18. App version matrix.** Installed-apps grid (app × site) with version chips — instantly see which site is behind.

**19. Time-zone correctness.** Store UTC, render in user TZ (default Asia/Dubai), absolute timestamp on hover.

## A3. UX gaps

The plan named 15 screens but specified none of: design tokens, component inventory, screen layouts, wizards, empty/loading/error states, dangerous-action patterns, keyboard/search interaction model, accessibility, responsiveness. Part B closes all of it.

---

# PART B — COMPLETE UI/UX SPECIFICATION

## B1. Design principles

1. **Answers before data.** Every screen leads with the conclusion; raw data is one level down.
2. **Nothing dangerous is one click away.** Destroy-class actions always cost a typed confirmation.
3. **Every long action is a visible job.** No silent mutations; the job tray never lets running work disappear.
4. **The terminal is one keystroke away.** Power users are never trapped in the UI.
5. **Monochrome discipline.** The interface is black/white/grey; color exists only to mean state.

## B2. Design system

**Foundation:** frappe-ui components + Tailwind preset, extended with the tokens below. UI font Inter (frappe-ui default); logs/terminal/code JetBrains Mono. Type scale: 12 meta · 13 secondary · 14 body · 16 section titles · 20 page titles · 28 dashboard KPIs.

**Color tokens (dark = default):**

```
--bg-base:       #0A0A0B    app background
--bg-surface:    #111113    cards, sidebar
--bg-raised:     #17171A    modals, popovers, hover rows
--border:        #232326    1px borders everywhere
--border-strong: #2E2E33    focus/active borders
--text-primary:  #F4F4F5
--text-secondary:#A1A1AA
--text-muted:    #6B6B74
--accent:        #FFFFFF    primary buttons = white bg, black text (inverted)
Status (the ONLY colors in the UI):
--ok:   #22C55E   --warn: #F59E0B   --err: #EF4444   --run/info: #3B82F6
```

Light mode inverts the neutral scale; status colors unchanged. Cards are flat: 1px border, 8px radius, no drop shadows. Motion: 150ms ease-out only; skeletons for content loading, spinners only inside buttons and job steps; respect `prefers-reduced-motion`.

**Core component inventory (build once in Phase 0, reuse everywhere):**
`StatusDot` · `StatusBadge` · `EnvironmentBadge` (PROD red-outline / STAGING amber / DEV grey) · `KPICard` · `DataTable` (sort, filter, column chooser, compact/comfortable density, sticky header, row actions) · `JobTimeline` (vertical steps: ○ pending, ◐ running w/ spinner + elapsed, ● done w/ duration, ✕ failed) · `LogViewer` (mono, follow-tail toggle, search, error highlighting, line numbers, download, fullscreen) · `TerminalPanel` (xterm.js, tabs, context bar) · `ConfirmModal` (standard + destructive variant) · `Wizard` (stepper, back-safe) · `CommandPalette` · `Toast` · `NotificationDrawer` · `EmptyState` · `Sparkline`/`Gauge` (monochrome + status accent) · `CopyField` (secrets/commands) · `DiffViewer` · `ActivityTimeline`.

Icons: Lucide, 16px in tables, 20px in nav.

## B3. Information architecture

**Top bar (fixed):** ⌘K search/command · scope filter (All servers / specific) · running-jobs indicator (pulsing dot + count → opens job tray) · notifications bell · user menu.

**Sidebar (collapsible, grouped):**

```
Dashboard
INFRASTRUCTURE   Servers · Benches · Sites · Apps
DATA PROTECTION  Backups · Restore
OPERATIONS       Jobs · Monitoring · Logs · Terminal
AUTOMATION       Schedules · Tools · AI Agents
GOVERNANCE       Users & Roles · Audit Log · Security · Reports
Settings         (bottom, shows platform version + update check)
```

**Persistent job tray:** bottom-right collapsed pill ("2 jobs running"); expands to a mini-list with per-job progress; click through to job detail. Running work is never invisible.

## B4. Screen specifications

**1. Dashboard (the CEO screen).**
Row 1 — KPI cards: Fleet Health %, Sites Up (n/n + 30-day uptime), Backup Compliance %, Open Alerts, Jobs 24h (ok/failed), SSL expiring ≤30d. Row 2 — Incidents & alerts (severity-sorted) beside running/recent jobs feed. Row 3 — Servers strip (per server: env badge, CPU/RAM/disk mini-gauges, status dot) beside a 7-day backup grid (GitHub-contribution style, green/red squares). Row 4 — "Needs attention": updates available, failed restore tests, drift flags. Morning-brief sentence at top. First-run empty state: 3-step onboarding checklist (Add server → Discover/create bench → Create site).

**2. Servers.**
List: status dot, name, env, IP, OS, resource bars, bench count, last-seen. **Add Server** = right-side sheet wizard: identity → auth (paste key / upload / generate keypair with copy-to-authorized_keys instructions) → live streamed "Test connection" (SSH ✓ · sudo ✓ · OS ✓ · detected-tools table) → save. Detail tabs: Overview (specs, live gauges, services grid — nginx/mariadb/redis/supervisor with restart buttons) · Benches · Terminal (scoped) · Packages/Tools · Activity · Settings (credential rotation, sudo mode, env tag).

**3. Benches.**
Grouped by server; rows show version chip (v14/v15/v16), Python/Node versions, ports, mode, site count, status. **Create Bench wizard:** pick server → pick Frappe version via radio cards that auto-display the version matrix ("v16 → Python 3.14 · Node 24 · MariaDB 11.8") → inline pre-flight job (deps present, ports free, disk) → name/path → review step showing exact commands (collapsed) → run as job → auto-navigate to job detail. Detail: Overview (parsed common_site_config, port map) · Sites · Apps · Actions (update / migrate-all / build / restart — each = confirm + job) · Logs · Activity.

**4. Sites.**
List: name, bench, env badge, health dot (HTTP check), version, scheduler state, last backup (relative + compliance tick), SSL days-left. **Create Site wizard:** bench → name (live validation) → admin password (generate + strength) → apps (checkbox list with compatibility check) → review → job. Detail tabs: Overview (health card: uptime sparkline, response time, scheduler, workers; quick actions: open site ↗ · migrate · clear cache · maintenance toggle) · Apps (installed table, update-available chips) · Backups · Domains & SSL · Config (site_config viewer, secrets masked with reveal-per-role) · Jobs · Activity. **Update banner** offers: [Update now — auto-backup first] or [Test on staging first] → clone → update clone → verification checklist → promote.

**5. Apps.** Sources (marketplace / GitHub / private / local; add-source fetches branches, warns on unknown compatibility) + the **installed matrix**: app × site grid with version chips.

**6. Backups.** Header KPIs (compliance %, total size, last failure). Table with filters, checksum tick, encryption lock icon, storage chip (local/S3), restore-tested badge. Policies tab: per-site RPO/retention editors. Actions: backup now (site/all), download, verify, restore →.

**7. Restore (a guided flow, not a table).** Step 1 pick/upload backup → validation job (checksum, version detection). Step 2 target: same site (destructive banner) / new site / other bench (compatibility check). Step 3 review: red consequence panel, automatic pre-restore backup notice, **type the site name to confirm**. Progress via JobTimeline; post-steps automatic (encryption_key copy, migrate); success card with verification links.

**8. Jobs.** Live table (status pulse, action, target, duration, user, retries; filter "mine"). Detail: left = step timeline with per-step durations; right = LogViewer with follow-tail; header = sanitized params + "Show exact command" expander + role-gated Cancel/Retry. On failure: failed step auto-expanded, stderr highlighted, **"Ask AI to analyze"** button.

**9. Monitoring.** Per-server CPU/RAM/disk/swap charts (24h–30d), services grid, per-site response-time charts, AlertRule CRUD (metric, threshold, cooldown, channel).

**10. Logs.** Source tree (server → bench → web/worker/schedule/nginx/mariadb/redis/supervisor/syslog) → LogViewer with live tail, search, download, last 100/500/1000.

**11. Terminal.** Full-page, multiple session tabs, context bar (user@server, quick buttons: "cd bench", "bench console"), idle-timeout countdown, recording indicator. Role-gated (Developer+).

**12. Schedules.** Calendar + list of recurring jobs (backups, restore tests, update windows, maintenance windows); conflict warnings.

**13. Tools.** Per-server stack checklist: detected version vs recommended (driven by the Frappe version matrix), install/upgrade → jobs. Groups: core stack, dev tools (code-server, gh, htop…), AI CLIs.

**14. AI Agents.** Agent cards (Claude Code, custom CLI): command template, scoped working-dir picker, read-only toggle, pre-change-backup toggle, allowed servers. "Start session" opens a scoped Terminal tab; ending a session opens the **diff review screen** (DiffViewer → apply / rollback).

**15. Users & Roles / Security / Audit.** Users + invites; role editor as a permission matrix (action-classes × roles). Security: active sessions (revoke), API tokens (one-time show), 2FA enforcement, password policy, IP allowlist, failed logins. Audit: immutable filterable table, CSV/PDF export, **compliance report generator** (date-range access + backup evidence — ISO-friendly).

**16. Reports.** Prebuilt: fleet summary, backup evidence, job history, app versions, SSL expiry, user activity, server capacity. Export PDF/CSV; scheduled email delivery.

**17. Settings.** General (product name + logo = white-label layer, default TZ Asia/Dubai) · Storage (S3 targets with test-connection) · Notifications (SMTP/webhook) · Defaults (version-matrix overrides, port ranges, bench base path) · Platform self-backup (status, run-now, master-key escrow warning) · re-run setup wizard · About/updates.

## B5. Cross-cutting UX patterns

**Dangerous-action modal (one pattern, everywhere):** red icon header → plain-language consequence list → what gets auto-backed-up first → type-target-name-to-confirm for destroy-class → single red button carrying the verb ("Drop site dev.localhost") — never "OK".

**Wizards:** ≤5 steps, stepper, back-safe, review step always exposes the underlying commands (collapsed), submit always lands on job detail.

**States:** every list ships an EmptyState (icon + one-liner + primary CTA); skeletons for loads; inline error banners with cause + "View job/log" deep link; toasts only for background events. Optimistic UI only for cheap toggles (scheduler, maintenance mode).

**Command palette (⌘K):** entity search (servers/benches/sites/jobs) + actions ("Backup site…", "Open terminal on…") + navigation; recents; role-filtered results. Keyboard map: `g d` dashboard, `g j` jobs, `t` terminal, `/` search, `?` shortcut sheet.

**Responsive:** desktop-first. ≥768px: collapsible sidebar; Dashboard/Monitoring/Jobs usable on mobile (read + safe actions); Terminal desktop-only.

**Accessibility:** 4.5:1 contrast minimum (test muted greys on near-black), full keyboard navigation, visible focus rings (1px white offset), `aria-live=polite` on job status changes.

**Microcopy:** buttons are verbs ("Create bench", never "Submit"); relative timestamps with absolute-on-hover in user TZ; technical honesty — power users can always expand to the real command; every error = what failed + why + next step.

## B6. "CEO-level done" — acceptance heuristics

- **10-second test:** the dashboard answers "is everything okay?" with zero clicks.
- **3-click test:** any routine operation (backup a site, restart workers) is ≤3 clicks from the dashboard.
- **Zero-surprise test:** no state change exists without a job record and an audit row.
- **Recovery test:** every failed update/restore shows a working rollback path.
- **Handover test:** a new operator can run day-2 operations without reading code or asking you.

---

# PART C — REVISED PHASE MAPPING (deltas only)

**Phase 0 adds:** session 0.4 — design tokens + core component library (StatusDot → LogViewer → ConfirmModal → JobTimeline → DataTable) built and storybook'd before any feature screens; white-label token decision made now.

**Phase 1 adds:** Dashboard v1 (KPI cards + job tray + onboarding empty state); environment badges + production guardrails; activity-timeline write-side (audit rows carry entity refs from day one).

**Phase 2 adds:** uptime HTTP checks + response-time charts; command palette; notification center; backup compliance engine (RPO/RTO policies + dashboard %).

**Phase 3 adds:** update advisor; **safe update pipeline** (clone → staging → verify → promote); restore-test automation; schedules calendar + maintenance windows.

**Phase 4 adds:** compliance report exports on top of DR work.

**Phase 5:** AI Agents module + **panel copilot** ("Ask AI" on failed jobs, NL palette actions).

**Phase 6 adds:** reports suite, white-label settings surface, session/security UI, drift detection (stretch).
