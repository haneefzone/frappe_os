# Production setup — dev→prod conversion & the temporary-elevation mechanism

Session 2.5. How the platform converts a **development** bench to **production**
(`bench setup production`), and — the highest-sensitivity Phase 2 decision — how
it runs that root-only command **without** a permanent broad sudo grant.

## What the conversion does

`bench setup production <user>` rewrites the server's **nginx** and
**supervisor** configuration so the bench's sites are served by nginx +
supervisor (as long-running services) instead of the foreground dev
`bench start`. It needs root: it writes under `/etc/nginx` and
`/etc/supervisor` and enables services.

The platform runs it as a `bench.setup_production` job (high queue,
long-running, locked per bench) whose ordered steps are:

1. **Detect mode** — refuse if the bench is already production.
2. **Capture nginx/supervisor config (pre-backup)** — tar `/etc/nginx` +
   `/etc/supervisor` to `/var/backups/fdm/pre-production-job<id>.tar.gz` (0600)
   and record a sha256 manifest of every file (the *before* state), for rollback
   and the diff.
3. **Grant temporary elevation** — install a time-boxed, single-command sudoers
   drop-in (see below) and learn the exact `bench` binary path.
4. **Set up production** — `sudo -n <bench> setup production <user>`.
5. **Mark bench as production** — flip `Bench.is_production = True` so the UI
   badge updates immediately (the next discovery would set it anyway).
6. **Diff config** — re-capture the manifest (the *after* state) and emit the
   added / changed / removed config files, plus a machine-readable
   `POSTCONFIG_RESULT` line.
7. **Validate nginx** — `sudo -n nginx -t`; a failure fails the job with a clear
   "investigate or roll back" message.
8. **Revoke temporary elevation** — *always* (in a `finally`), success or
   failure.

## The Phase 2.5 sudo decision point

The ratified sudoers allowlist (`docs/implementation-plan.md` security section)
deliberately does **not** grant `bench setup production` a permanent NOPASSWD
line:

> `bench setup production` needs broader rights ONCE — run it with a temporary
> elevation, not a permanent allowlist entry (Phase 2.5 decision point).

**Decision (implemented, for ISO review):** the platform holds exactly one
standing sudo grant — for the fixed, absolute, argument-validated helper
`deploy/fdm-elevate` (the same "fixed binary, self-validating args, no wildcard
binary" posture as the ratified `supervisorctl restart *` line). That helper can:

- `manifest` — print `sha256␠␠path` for every file under `/etc/nginx` +
  `/etc/supervisor` (read-only), for the before/after diff;
- `backup <tar>` — tar those trees to a path **confined under
  `/var/backups/fdm`** (0600) and print the manifest — the one-shot pre-backup;
- `grant <user>` — install `/etc/sudoers.d/fdm-prod-elevation` containing a
  **single, non-wildcard** line permitting exactly
  `<bench> setup production <user>` and nothing else (0440, validated with
  `visudo -cf`), and print `BENCH_BIN=<abs path>`. **`<bench>` is resolved
  hardened (DOO-163):** never from the caller's `$PATH` (the bench user invokes
  the helper over SSH and controls their environment) — it comes from a
  root-owned pin file (`/etc/fdm-platform/bench-bin`) if present, else a lookup
  on a fixed `SECURE_PATH`, then `readlink -f` canonicalisation. `grant` then
  **refuses** unless the resolved binary *and every ancestor directory* are
  owned by root and not group/other-writable. A NOPASSWD grant on a binary a
  non-root user can overwrite is root-equivalent for that user, so the helper
  fails closed rather than installing an unsafe drop-in;
- `revoke` — remove that drop-in (idempotent).

So the elevation to run `bench setup production` is **temporary by
construction**: the drop-in is created just before the run (step 3) and removed
in the job's `finally` (step 8). **At rest there is no sudo path to
`bench setup production`** — the drift baseline (Phase 6.7) can assert
`/etc/sudoers.d/fdm-prod-elevation` does not exist. The standing grant only lets
the platform *temporarily* grant, then revoke, a single fixed command.

Every conversion writes a `CommandJob` + `AuditLog` row (golden rule 2, incl.
the acting user and source IP), and the grant/run/revoke are individual job
steps with streamed logs — the elevation episode is fully audited.

## One-time server bootstrap (operator, documented)

Run **once per managed server**, as root:

```bash
# 1. Install the helper (root-owned, not writable by the bench user).
sudo install -m 0755 -o root -g root deploy/fdm-elevate /usr/local/sbin/fdm-elevate

# 2. Grant ONLY the helper (append to /etc/sudoers.d/fdm-platform, then validate).
echo 'frappe ALL=(root) NOPASSWD: /usr/local/sbin/fdm-elevate' \
  | sudo tee -a /etc/sudoers.d/fdm-platform
sudo visudo -cf /etc/sudoers.d/fdm-platform     # must print "parsed OK"
sudo chmod 0440 /etc/sudoers.d/fdm-platform
```

`nginx -t` uses the already-ratified `/usr/sbin/nginx -t` allowlist line — no
extra grant.

### bench must resolve to a root-owned binary (DOO-163)

`grant` refuses if the resolved bench binary (or any directory on its path) is
writable by a non-root user — otherwise the single-command drop-in would be a
privilege-escalation vector. A stock Frappe host installs `bench` under the bench
user's home (e.g. `/home/frappe/.local/bin/bench`), which **fails this check**.
On such a host, provide a bench whose **binary *and* its interpreter are
root-owned**, so the path named in the drop-in cannot be rewritten by the bench
user, **once per server, as root**.

> **Do not just copy the wrapper.** On a uv-installed host `bench` is a
> console-script whose shebang points at the bench user's interpreter, e.g.
> `#!/home/frappe/.local/share/uv/tools/frappe-bench/bin/python`. Copying only
> that wrapper to a root-owned path (`sudo install … "$(command -v bench)"
> /usr/local/bin/bench`) makes it **pass `assert_secure_path`** — the file and its
> ancestors are root-owned — while, run as root, it still execs the
> **bench-user-writable interpreter**, leaving the substituted-code hole wide open
> (see the residual note below). Pin a bench backed by a root-owned interpreter
> instead.

```bash
# Install bench into a root-owned virtualenv so BOTH the launcher and the
# interpreter its shebang targets are owned by root and not bench-user-writable.
# (The venv path is on the fixed SECURE_PATH once you pin it in step two.)
sudo python3 -m venv /opt/fdm-bench            # root-owned interpreter + venv
sudo /opt/fdm-bench/bin/pip install --upgrade pip frappe-bench
# /opt/fdm-bench/bin/bench now has a shebang into /opt/fdm-bench/bin/python,
# both root-owned. Verify: head -1 /opt/fdm-bench/bin/bench && ls -l "$(readlink -f /opt/fdm-bench/bin/python)"

# Pin that absolute, root-owned bench path explicitly:
sudo install -d -m 0755 -o root -g root /etc/fdm-platform
echo /opt/fdm-bench/bin/bench | sudo tee /etc/fdm-platform/bench-bin
sudo chown root:root /etc/fdm-platform/bench-bin
sudo chmod 0644 /etc/fdm-platform/bench-bin
```

`assert_secure_path` checks the launcher and its ancestor directories; it does
**not** follow the shebang. It is therefore your responsibility to ensure the
interpreter behind that launcher is root-owned too — copying a stock wrapper
satisfies the check without satisfying the intent.

> **Scope of this check (and its inherent limit).** The hardening removes the
> *avoidable* risk: the sudoers line can no longer name a caller-chosen or
> bench-user-writable path, and a symlink can't redirect it after validation. It
> does **not** turn `bench setup production` into a fully sandboxed operation —
> bench is the bench user's own Python, so running it as root inherently executes
> that user's code with root rights. That residual is inherent to
> `bench setup production` and was the accepted Phase 2.5 tradeoff; keep it
> bounded the way DOO-131 does — the grant is single-command and revoked in the
> job's `finally`, never standing. Do **not** paper over the check with a
> root-owned wrapper that `exec`s a bench-user-writable target (e.g. copying the
> stock console-script to `/usr/local/bin/bench` — its shebang still points at the
> bench user's interpreter): that satisfies the path check while leaving the
> substituted-code hole wide open. Pin a bench whose interpreter is root-owned
> too, per the install step above — that closes the wrapper-swap vector; only the
> *inherent* "root runs bench-owned code" residual (when the interpreter itself is
> legitimately the bench user's) remains, and that is the accepted 2.5 tradeoff.

## Rollback

If a conversion goes wrong (e.g. `nginx -t` fails, or the site doesn't serve),
restore the captured config and restart the services:

```bash
# On the server, as root. <id> is the job id shown in the pre-backup log line.
sudo tar -xzf /var/backups/fdm/pre-production-job<id>.tar.gz -C /
sudo nginx -t && sudo systemctl restart nginx
sudo supervisorctl reread && sudo supervisorctl update && sudo supervisorctl restart all
```

Then, if the bench should be treated as dev again, its `is_production` flag
returns to its true value on the next **bench discovery**.

Because the temporary drop-in is always revoked by the job, a failed conversion
leaves no standing elevation to clean up. If the revoke step ever logs a WARNING
(it could not confirm removal), delete `/etc/sudoers.d/fdm-prod-elevation`
manually and re-run `visudo -c`.

## Live acceptance

The unit tests cover the full flow over fakes (grant→run→revoke ordering,
revoke-always-in-`finally`, is_production toggle, config diff, already-prod
refusal, nginx-t gate, RBAC + 409). **Positive live acceptance needs a real root
bench with sudo** — gated behind the DOO-36 test-target (a writable v16 VM).
Run there: on a dev bench, click **Set up production**, confirm the pre-backup
tar exists, the job completes, the bench shows **Production**, sites are served
by nginx/supervisor, the job shows the before/after diff, and
`/etc/sudoers.d/fdm-prod-elevation` is absent afterwards.
