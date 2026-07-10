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
  `visudo -cf`), and print `BENCH_BIN=<abs path>`;
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
