# FDM Platform — MVP demo script (v0.1.0-mvp)

The end-to-end click-path that proves the MVP: **register a server → create a
bench → create a site → install ERPNext → migrate → back up → restore.** Every
step is a real job you watch run; every step writes an audit row.

Written for a non-technical operator. Run it against a **fresh snapshot of the
throwaway `fdm-test` VM** — never the read-only study bench (see
`docs/dev-setup.md` for the snapshot-before-destructive-test workflow).

> **Before you start**
> 1. On the **dev VM**, start the platform: `make dev` (backend + frontend) and,
>    in a second terminal, `make worker` (the job runner — nothing executes
>    without it). Open the UI at `http://localhost:5173` and sign in.
> 2. On the **fdm-test VM**, take a Hyper-V checkpoint named `mvp-demo-clean` so
>    you can re-run this script from zero.
> 3. Make sure the platform's SSH public key is in the `frappe` user's
>    `~/.ssh/authorized_keys` on `fdm-test`, and the sudoers allowlist from
>    `docs/implementation-plan.md` is installed (needed for the services grid
>    restart buttons and production bench restarts).

Each step lists **what to click**, **what you should see**, and the **10-second
check** on the Dashboard.

---

## 0. The Dashboard (the "is everything okay?" screen)

- Open **Dashboard** (the home screen, `g d`).
- On a brand-new install with no servers you see the **first-run checklist**:
  *Add a server → Create a bench → Create a site.* That is your roadmap.

Leave this tab open in the background — after each step below, glance at it: the
KPI cards, the servers strip gauges, and the 7-day backup grid all update from
real data within ~15 seconds.

---

## 1. Register the `fdm-test` server

- Sidebar → **Servers** → **Add Server** (opens the right-side sheet wizard).
- **Identity:** name `fdm-test`, hostname = the VM's IP, SSH port `22`, env tag
  `dev`.
- **Auth:** paste the platform's private key (or choose *Generate a key pair*
  and copy the shown public key into `frappe@fdm-test:~/.ssh/authorized_keys`).
  Username `frappe`.
- Optionally set the **MariaDB root password** (needed later to create a site
  non-interactively).
- Click **Test connection** and watch the live checks stream:
  `SSH ✓ · sudo ✓ · OS ✓` and the detected-tools table.
- Click **Save server**.

**You should see:** the server appears in the list with a green status dot.
**10-second check:** Dashboard now shows `1` server online; the onboarding
checklist step 1 is done. **Audit:** *Audit Log* shows `server.register`.

---

## 2. Create a bench

- Sidebar → **Benches** → **Create bench** (wizard).
- **Pick server:** `fdm-test`.
- **Pick Frappe version:** choose **v16** — the card shows the matrix
  (Python 3.14 · Node 24 · MariaDB 11.8).
- **Pre-flight** runs as a live job: it checks `uv`, Node, disk, and free ports.
  If anything is missing it blocks here with a clear reason — fix it on the VM
  and re-run.
- **Name / path:** name `frappe-bench`, parent path `/home/frappe`.
- **Review:** expand the collapsed command block to see the exact `bench init`
  that will run, then **Create bench**.
- You land on the **job detail** page — watch the steps and streaming logs.
  `bench init` is long (several minutes); the persistent job tray shows it
  running even if you navigate away.

**You should see:** the job finishes `success`; the bench appears under
`fdm-test`. **Audit:** `bench.create`.

---

## 3. Create a site

- Sidebar → **Sites** → **Create site** (wizard).
- **Bench:** `frappe-bench`.
- **Name:** `demo.localhost` (live-validated against the site-name rules).
- **Admin password:** generate one (or type it) — the strength meter guides you.
  This password is a **secret**: it is sent to the job encrypted and never
  appears in logs or the job's parameters display.
- **Review → Create site.** Watch the `bench new-site` job (the dev-bench Redis
  dance runs automatically around it).

**You should see:** the job finishes `success`; the site appears with a health
dot. **Audit:** `site.create` (its params show the admin password masked as
`••••`).

---

## 4. Install ERPNext

- Open the site → **Apps** tab (or the **Apps** screen) → **Install app**.
- Pick / add the **ERPNext** source (GitHub `frappe/erpnext`, branch matching
  your Frappe major, e.g. `version-16`). If it is not yet a saved source, add it
  first under **Apps → Sources**.
- Confirm and launch. This runs `bench get-app` (if the app isn't on the bench
  yet) then `bench --site demo.localhost install-app erpnext` as job steps.

**You should see:** the job finishes `success`; the installed-apps matrix shows
`erpnext` against `demo.localhost` with its version chip. **Audit:**
`app.get` / `site.install_app`.

---

## 5. Migrate the site

- On the site detail page → **Migrate** (a confirm, then a job).
- This runs `bench --site demo.localhost migrate` to apply any pending schema
  patches (the Redis dance is handled for you on a dev bench).

**You should see:** the migrate job finishes `success`. **Audit:**
`site.migrate`.

---

## 6. Back up the site

- Site detail → **Backups** tab → **Back up now** (choose *with files* for a
  full backup).
- Watch the `site.backup` job: it captures the database dump, public/private
  files, and the `site_config_backup.json` (which carries the `encryption_key`),
  records each artifact's size + **sha256 checksum**, and creates a first-class
  **Backup** row.

**You should see:** a new backup with a green checksum tick and an encryption
lock icon. **10-second check:** the Dashboard **Backups 24h** KPI ticks up, the
**Fleet Health %** rises (backup compliance is the live component), and today's
square in the **7-day backup grid** turns green. **Audit:** `site.backup`.

---

## 7. Restore (the guided flow)

Restore proves the backup actually works. Do the **new-site** restore first (the
safest to demo), then optionally the destructive same-site restore.

- Sidebar → **Restore** (a guided flow, not a table).
- **Step 1 — pick the backup** you just took. A validation job checks the
  checksums and detects the source Frappe major.
- **Step 2 — target:** choose **New site** (e.g. `restored.localhost`) on the
  same bench. (Choosing *Same site* shows a red destructive banner and requires
  you to type the site name to confirm — and the platform takes an **automatic
  pre-restore backup first**.)
- **Step 3 — review:** read the consequence panel, then confirm.
- Watch the `site.restore` job's timeline: it restores the db + files, then does
  the post-steps automatically — **copies the `encryption_key`** into the target
  site config and runs **`bench migrate`** (gotcha #7). A newer-major backup onto
  older code is blocked (never downgrade).

**You should see:** a success card with a link to the restored site; the source
backup is now flagged **restore-tested**. **Audit:** `site.restore`.

---

## 8. Confirm the audit trail + settings

- Sidebar → **Audit Log**: every action above is there — `server.register`,
  `bench.create`, `site.create`, `site.install_app`, `site.migrate`,
  `site.backup`, `site.restore` — each with the actor, timestamp, source IP, and
  masked parameters. Click **Export CSV** for the compliance-friendly evidence
  file.
- Sidebar → **Settings → General**: change the **product name** and upload a
  **logo**. Save, and watch the **sidebar branding update live** (the white-label
  layer). **Defaults** holds the bench base path + port range the Create-Bench
  wizard pre-fills; **Environment** shows the read-only platform facts.

---

## Re-running

Restore the `mvp-demo-clean` Hyper-V checkpoint on `fdm-test` to wipe everything
and run the script again from step 1. In the platform UI, delete the `fdm-test`
server (Servers → server detail → delete) to reset the control-plane side, or
keep it and re-discover.

## What "done" looks like

You completed all eight steps end-to-end on a fresh VM snapshot, the Dashboard
answered "is everything okay?" at a glance after each step, and the Audit Log
shows an unbroken trail of every state change. That is the MVP.
