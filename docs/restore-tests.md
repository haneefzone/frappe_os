# Restore-test automation (FDM 3.4)

Proof-of-restorability. A backup is only trustworthy once it has actually been
restored *somewhere* and verified. This feature restores a site's newest backup
into a throwaway **scratch site**, verifies it, then **always destroys** the
scratch and stamps the backup's *restore-tested* badge — on a schedule, or on
demand.

```
pick newest backup → disk preflight → restore into scratch site → verify
   (per site)          (df, ≥3×)         (reuse 3.3 machinery)    (boot/sched/rows)
                                                                        │
                    stamp badge (passed|failed) ◀── ALWAYS destroy scratch (finally)
```

Nothing here touches the source site or its data: the source is only ever read
(a single row-count probe), and every write lands on the scratch site, which is
created fresh and torn down in a `finally` — so a failure, a crash mid-restore,
or a half-created scratch never leaves an orphaned site or database.

## The job

`backup.restore_test` → `RestoreTestAction` (`app/core/commands/actions.py`),
one non-idempotent job **locked on a per-source restore-test key**
(`{bench_path}::restore-test::{site}`) so two restore-tests of one site can't
overlap and it never collides with the source site's own backup/restore lock.
Ordered steps:

1. **Resolve the backup** — an explicit `backup_id`, else the site's newest
   `success` backup that carries a database artifact.
2. **Disk preflight** — `df -Pk <bench_path>`; refuse (record the test as
   *failed*, create no scratch) when free space < ~3× the backup size, so a
   restore-test never fills the disk.
3. **Restore into a scratch site** — `rt-<job_id>.restoretest.localhost`, via
   3.3's `_restore_artifacts_into_site(create=True)` (`bench new-site` →
   `bench --force restore` → copy `encryption_key` → `bench migrate`). Cleanup
   is *armed the instant before* `bench new-site`.
4. **Verify** (`RestoreTestAction._verify`) — three read-mostly probes on the
   scratch: (a) it **boots** (`frappe.ping` → `pong`); (b) its **scheduler**
   responds (`bench scheduler status` → enabled); (c) **row-count sanity** — a
   `frappe.client.get_count` on the scratch vs the source shows no gross data
   loss (>½ the rows gone = fail), matching 3.3's verify tolerance.
5. **Record the verdict** on the source backup: `restore_test_status`
   (untested|passed|failed), `restore_tested_at`, `restore_test_detail`,
   `restore_test_job_id` (the legacy 1.11 `restore_tested` bool stays true only
   while the newest proof passed).
6. **`finally`: destroy the scratch site** — `bench drop-site <scratch> --force
   --no-backup` drops its DB + removes its site dir. `--force` never prompts and
   tolerates a half-created site, so cleanup is idempotent and *always* runs — a
   cleanup hiccup is a warning, never masks the real verdict. If the drop exits
   non-zero the scratch may still hold a copy of the source data, so a
   `backup.restore_test_orphan` **notification** is dispatched (in-app / email /
   webhook) telling an operator to reap it — a job-log WARNING alone is not
   enough (A.8.10, DOO-1071).

A restore/verify failure records the *failed* badge, then the job fails loudly
(so the operator sees a failed job) — and because the template is
`idempotent=False`, a half-restore is never auto-retried on top of itself.

## Scheduling

The 2.1 scheduler process registers a recurring **restore-test sweep**
(`app/workers/scheduler.py:evaluate_restore_tests`, every
`RESTORE_TEST_TICK_SECONDS`, hourly by default). Each tick, `restore_tests.select_due`
finds every site whose newest backup is due under an **enabled** `BackupPolicy`
with `require_restore_test` set and a non-NULL `restore_test_interval_days`
(default 7; NULL = on-demand only), then **fans out one job per site** — one
site's failure or busy lock (`LockConflict` → skipped, retried next tick) never
sinks the others. The per-site cadence surfaces in the policy API for the
Schedules view.

## API

| Action | Endpoint | Perm |
| --- | --- | --- |
| Run restore-test now | `POST /api/sites/{id}/restore-test` | `backup:restore` |
| Per-site cadence | `GET/PUT /api/sites/{id}/policy` (`restore_test_interval_days`) | `schedule:manage` |
| Badge on a backup | `GET /api/backups[/{id}]` (`restore_test_status` + timestamp + detail) | `read` |
| Failed-test feed | `GET /api/dashboard` → `needs_attention.failed_restore_tests` | `read` |

The scratch site's admin password is a throwaway generated per run (it is
destroyed after the test); the MariaDB root password is resolved server-side
from the Server row, never sent from the browser (rule 6). Read-only roles can
never launch a restore-test; the internal scratch drop requires `danger`.

## Verification commands

Backend unit/behaviour tests (no target needed — in-memory fakes; covers the
happy path, the destroy-on-failure path, the boot/row-count failures, and the
disk preflight):

```bash
cd backend
DEBUG=true python -m pytest tests/test_restore_tests.py -q
# expected: 19 passed
```

Ruff + migration up/down/up on Postgres:

```bash
cd backend
ruff check app tests/test_restore_tests.py            # expected: All checks passed!
DEBUG=true DATABASE_URL=postgresql+psycopg://fdm:fdm@localhost:5432/fdm_verify \
  alembic upgrade head && alembic downgrade -1 && alembic upgrade head
alembic heads                                          # expected: d4e5f6a7b8c9 (head) — single head
```

Live end-to-end (requires the bench-capable target, DOO-141): enable
`require_restore_test` on a site's policy with a recent backup, then
`POST /api/sites/{id}/restore-test`; observe an `rt-*.restoretest.localhost`
scratch site created + verified, the backup's badge flip to *passed* with a
timestamp, and the scratch site dropped (no leftover site/DB). Induce a restore
failure (e.g. a corrupt backup) and observe the badge flip to *failed*, the
dashboard "failed restore tests" count rise, and the scratch still destroyed.
```
