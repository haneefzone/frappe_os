# Dev & test workflow

How to run the FDM Platform for development, and how to prepare, snapshot,
and safely break the virtual machines it manages. Written for a
non-technical operator on **Windows 11 with Hyper-V**.

Every command block below states **where it runs**. There are three places:

| Label | What it means | How to get there |
|---|---|---|
| **PowerShell (Windows host)** | Your Windows 11 machine itself | Right-click Start → *Terminal (Admin)* |
| **dev VM** | The Ubuntu VM where this repo lives | Your usual SSH/terminal session into it |
| **VM console** | The screen of a target VM | Hyper-V Manager → double-click the VM |

## Topology

Everything runs on the operator's Windows machine under Hyper-V, on the
**Default Switch** NAT network (`172.30.0.0/20` — addresses are
DHCP-assigned and can change across Windows host reboots; re-check with
`ip -4 addr` after a reboot):

| Machine | Role | Rules |
|---|---|---|
| Windows host | Hyper-V host, takes/restores checkpoints | — |
| `dev` VM (`172.30.14.160` at time of writing) | Paperclip + FDM control-plane dev + shared study bench | Study bench is **READ-ONLY**. Never run destructive tests here. |
| `fdm-test` VM | Throwaway destructive-test target | Nothing valuable ever lives here. Rebuild/restore freely. |

---

## 1. One-time setup (dev VM)

You need these installed on the dev VM (they already are on ours):
Docker + the compose plugin, Python 3.12+ with [`uv`](https://docs.astral.sh/uv/),
and Node 20+. Then, from the repo root:

```bash
# dev VM — one time only
cd ~/fdm-platform    # or wherever the repo is checked out
make setup
```

`make setup` creates the backend virtualenv (`backend/.venv`) and installs
frontend packages (`frontend/node_modules`). It takes a few minutes the
first time.

**Environment file (optional in dev).** Every setting has a working dev
default, so you can skip this entirely. If you want to override something
(or you're setting up anything non-throwaway), copy the template and fill
in real secrets:

```bash
# dev VM — optional
cp .env.example .env
# generate a real encryption key:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the output as FDM_SECRET_KEY in .env, and put any long random
# string (30+ characters) as JWT_SECRET
```

## 2. Running the platform

The platform is four pieces: **Postgres + Redis** (Docker containers),
the **backend** (FastAPI), the **frontend** (Vite dev server), and the
**RQ worker** (runs queued jobs like backups and bench installs).

### Start everything

```bash
# dev VM — terminal 1
make dev
```

This starts the Docker containers, then the backend and frontend together
in the same terminal. You'll see interleaved output; it has settled when
you see both:

- `Uvicorn running on http://0.0.0.0:8000` (backend)
- `VITE vX.Y.Z ready in ...ms` (frontend)

The worker is a separate process so job logs don't drown the app output:

```bash
# dev VM — terminal 2
make worker
```

You should see `Worker ... started with PID ...` and `*** Listening on
high, default, low...`. Without the worker, the API still runs, but every
queued job sits at *pending* forever — if jobs never start, check this
terminal first.

### Check it's up

Open these **in your Windows browser** (replace the IP with the dev VM's
current address):

| URL | What you should see |
|---|---|
| `http://172.30.14.160:5173` | The dark FDM shell with the sidebar |
| `http://172.30.14.160:5173/styleguide` | The component library demo page |
| `http://172.30.14.160:8000/api/health` | `{"status":"ok"}` |

Or from a terminal on the dev VM: `curl http://localhost:8000/api/health`.

### Stop everything

`Ctrl+C` in both terminals, then:

```bash
# dev VM
make stop     # stops the Postgres/Redis containers (data is kept)
```

---

## 3. Preparing a managed test VM (production-like)

This is how the platform onboards **any** managed server, test or real.
The quick throwaway variant for `fdm-test` is in section 4 — read this
section anyway, because it's the model the product enforces.

### 3.1 The user the platform connects as

The platform SSHes in as the **bench owner** — the Linux user that owns
the Frappe benches, conventionally `frappe`. Never root: `bench` refuses
to run as root, and the platform never needs a root login. On a fresh
Ubuntu VM, create the user from the **VM console**:

```bash
# VM console — as the user created during Ubuntu install
sudo adduser --disabled-password --gecos "Frappe bench owner" frappe
```

### 3.2 Give the platform's public key access

The platform authenticates with an SSH key pair. The **private** key stays
on the control plane (in dev: `~/.ssh/fdm_test_target_ed25519` on the dev
VM); the **public** key gets pasted into the managed VM.

Print the public key on the dev VM:

```bash
# dev VM
cat ~/.ssh/fdm_test_target_ed25519.pub
```

Copy that single `ssh-ed25519 AAAA... ` line, then on the managed VM:

```bash
# VM console
sudo install -d -m 700 -o frappe -g frappe /home/frappe/.ssh
echo 'PASTE_THE_PUBLIC_KEY_LINE_HERE' | sudo tee -a /home/frappe/.ssh/authorized_keys
sudo chmod 600 /home/frappe/.ssh/authorized_keys
sudo chown frappe:frappe /home/frappe/.ssh/authorized_keys
```

Verify from the dev VM (find the managed VM's IP first with `ip -4 addr`
on its console):

```bash
# dev VM
ssh -i ~/.ssh/fdm_test_target_ed25519 frappe@<managed-vm-ip> 'echo connected as $(whoami)'
```

Expected output: `connected as frappe`.

### 3.3 Sudoers allowlist

The bench owner must **not** have general sudo. The platform needs root
for exactly a handful of commands (service checks/restarts, nginx config
test), granted via a drop-in allowlist. This snippet is the security
section of `docs/implementation-plan.md` — that file is authoritative if
the two ever differ.

On the managed VM, create the file **with validation** (a broken sudoers
file can lock you out of sudo entirely — `visudo -cf` checks it first):

```bash
# VM console
sudo tee /etc/sudoers.d/fdm-platform > /dev/null <<'EOF'
# /etc/sudoers.d/fdm-platform — FDM Platform managed-server allowlist
# Non-interactive checks (server test-connection uses `sudo -n true`)
frappe ALL=(root) NOPASSWD: /usr/bin/true

# Service state + restarts (monitoring services grid, bench.restart on production benches)
frappe ALL=(root) NOPASSWD: /usr/bin/systemctl is-active nginx, /usr/bin/systemctl is-active mariadb, \
    /usr/bin/systemctl is-active redis-server, /usr/bin/systemctl is-active supervisor
frappe ALL=(root) NOPASSWD: /usr/bin/systemctl restart nginx, /usr/bin/systemctl restart mariadb, \
    /usr/bin/systemctl restart redis-server, /usr/bin/systemctl restart supervisor
frappe ALL=(root) NOPASSWD: /usr/bin/supervisorctl restart *
frappe ALL=(root) NOPASSWD: /usr/sbin/nginx -t

# bench setup production needs broader rights ONCE — run it with a temporary
# elevation, not a permanent allowlist entry (Phase 2.5 decision point).
EOF
sudo chmod 440 /etc/sudoers.d/fdm-platform
sudo visudo -cf /etc/sudoers.d/fdm-platform   # must print: parsed OK
```

Test it as the platform would:

```bash
# dev VM
ssh -i ~/.ssh/fdm_test_target_ed25519 frappe@<managed-vm-ip> 'sudo -n true && echo sudo-allowlist OK'
```

Expected output: `sudo-allowlist OK`. If it prints
`sudo: a password is required`, the drop-in file isn't in place or has the
wrong username in it.

---

## 4. The throwaway `fdm-test` VM (fast path)

For the designated destructive-test target we skip the allowlist and give
the service account full passwordless sudo — acceptable **only** because
this VM is throwaway by policy (section 6).

### VM spec

- Ubuntu Server 24.04 LTS, Gen 2 VM, Default Switch
- 4 vCPU, 6–8 GB RAM (dynamic OK), 60 GB disk
  (it must host one or more Frappe benches — see plan session 1.6)
- Created by the operator; provisioned by running
  `scripts/provision-test-target.sh` once from the VM console
  (it creates the `fdm` service account, installs the embedded public
  key, and enables SSH — the script is idempotent)

### SSH access

- Service account: `fdm` (passwordless sudo — throwaway VM policy)
- Key on the dev VM: `~/.ssh/fdm_test_target_ed25519` (private),
  `.pub` alongside; the pubkey is embedded in the provisioning script
- Connect: `ssh -i ~/.ssh/fdm_test_target_ed25519 fdm@<fdm-test-ip>`
- The FDM control plane registers this host in the Server registry
  (plan session 1.2) using the same key

## 5. Snapshot-before-destructive-test workflow

**The rule: destructive integration tests (backup, restore-over-existing,
drop-site, delete-bench) run ONLY against the designated throwaway VM
(`fdm-test`), and only with a checkpoint to fall back to.** Never against
the dev VM, the study bench, or anything a client has touched.

They are only repeatable because the target can be reset. From
**PowerShell (Windows host), run as Administrator**:

```powershell
# One-time, right after provisioning succeeds:
Checkpoint-VM -Name fdm-test -SnapshotName baseline

# After installing benches/sites worth keeping as a new starting point:
Checkpoint-VM -Name fdm-test -SnapshotName "bench-ready"

# Reset before/after a destructive test run:
Restore-VMCheckpoint -VMName fdm-test -Name "bench-ready" -Confirm:$false
Start-VM -Name fdm-test

# See what checkpoints exist:
Get-VMCheckpoint -VMName fdm-test
```

(`Restore-VMCheckpoint` and `Restore-VMSnapshot` are the same command —
newer Windows versions accept both names.)

Notes:

- The VM's IP usually survives a checkpoint restore (same MAC → same
  DHCP lease), but verify after restore and update the Server registry
  entry if it changed.
- Restoring rolls back the SSH host key too, so known-host pinning
  stays valid. If the VM is *rebuilt* from scratch instead, the host
  key changes — re-run the provisioning script and re-pin.

## 6. Throwaway-VM policy

- Never store credentials, client data, or unpushed work on `fdm-test`.
- Any state worth keeping is a checkpoint, not a promise.
- If the VM gets into a weird state, restore `baseline` rather than
  debugging it.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ssh: connect to host ... port 22: Connection refused` | VM is off, or sshd not running | Start the VM in Hyper-V Manager. On the VM console: `sudo systemctl status ssh`; if missing, re-run `scripts/provision-test-target.sh` |
| `ssh: connect to host ... : Connection timed out` | Wrong IP (DHCP lease changed after a host reboot) | On the VM console: `ip -4 addr` and use the new address; update the Server registry entry |
| `Permission denied (publickey)` | Public key not in the target user's `authorized_keys`, or wrong user/permissions | Redo section 3.2 (or re-run the provisioning script on `fdm-test`); check you're connecting as the right user (`frappe` vs `fdm`) |
| `REMOTE HOST IDENTIFICATION HAS CHANGED!` | VM was rebuilt (new host key) — or restored, which should *keep* the key | If you rebuilt it: `ssh-keygen -R <vm-ip>` on the dev VM and reconnect. If you didn't rebuild anything, stop and investigate |
| `sudo: a password is required` over SSH | Sudoers allowlist missing or wrong username | Redo section 3.3; run `sudo visudo -cf /etc/sudoers.d/fdm-platform` on the VM |
| `make dev` fails: `bind: address already in use` (port 8000 or 5173) | A previous backend/frontend is still running | `sudo lsof -i :8000 -i :5173` on the dev VM, then `kill <PID>` — or close the old terminal that's still running `make dev` |
| Compose fails: port 5432 or 6379 already in use | Another Postgres/Redis on the dev VM (e.g. a bench's redis) | `sudo lsof -i :5432 -i :6379` to find it; stop it, or change the published port in `docker-compose.dev.yml` and set `DATABASE_URL`/`REDIS_URL` in `.env` to match |
| Compose services unhealthy / `docker compose ps` shows `(unhealthy)` | Container failed to start, often stale volume or low disk | `docker compose -f docker-compose.dev.yml logs postgres redis` for the reason. Disk full? `df -h`. Last resort for a broken **dev** database: `make stop`, then `docker volume rm fdm-platform_fdm-postgres-data` (deletes all platform dev data) and `make dev` |
| Backend starts but every API call fails with a database error | Containers not up yet or not running | `docker compose -f docker-compose.dev.yml ps` — both services should say `healthy`. If you started uvicorn by hand, run `make dev` instead so compose comes up first |
| Jobs stay `pending` forever | RQ worker not running | Start `make worker` in its own terminal (see section 2) |
| Frontend loads but shows no data / network errors in console | Backend not running, or you opened the built files instead of the dev server | Check `http://<dev-vm-ip>:8000/api/health` responds; always use the `:5173` URL in dev (it proxies `/api` to the backend) |
