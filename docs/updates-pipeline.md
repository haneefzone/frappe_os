# Safe update pipeline (FDM 3.3)

The single safe path to update a production Frappe site. Nothing updates prod
directly — an update is first rehearsed on a throwaway staging clone, verified
against an all-green checklist, and only then promoted to prod behind a
mandatory pre-update backup with an automatic, tested rollback.

```
clone prod → staging   →   update staging   →   verify checklist   →   promote to prod
   (backup+restore)          (bench update)      (boots/migrate/           (pre-backup GATE →
                                                   scheduler/rows)           bench update → post-check
                                                                             → rollback on failure)
```

Every remote step runs through the validated command-template registry
(`app/core/commands/registry.py`) — no raw shell — and as an enqueued
`CommandJob` (never in the request). The run is tracked by an `UpdatePipeline`
row, which persists the checklist verdict and the pre-update backup id so the
promote gate is enforced **server-side**, not in the browser.

## Environments & guardrails

Each `Site` carries `environment` = `dev | staging | prod` (EnvironmentBadge:
PROD red / STAGING amber / DEV grey). Promoting to a **prod** site is destructive
and is refused by `POST /api/update-pipelines/{id}/promote` unless **all** hold:

1. the verification checklist is all-green (`checklist_ok`),
2. the caller has the `danger` permission (Admin),
3. `confirm_name` equals the exact prod site name (type-to-confirm, rule 5), and
4. a per-task client **sign-off** reference is supplied
   ("never write a client's prod without per-task sign-off").

Read-only roles can never mutate; classification (`POST /api/sites/{id}/environment`)
requires `site:operate`.

## API

| Step | Endpoint | Perm |
| --- | --- | --- |
| Classify a site | `POST /api/sites/{id}/environment` | `site:operate` |
| Clone → staging | `POST /api/update-pipelines` | `backup:restore` |
| Update staging | `POST /api/update-pipelines/{id}/update-staging` | `bench:operate` |
| Verify checklist | `POST /api/update-pipelines/{id}/verify` | `site:operate` |
| Promote to prod | `POST /api/update-pipelines/{id}/promote` | `backup:restore` + `danger` (prod) |
| Inspect | `GET /api/update-pipelines[/{id}]` | `read` |

## Jobs

- **`site.clone_to_staging`** — with-files backup of the source site, then create
  a fresh staging site and restore into it (db + files + `encryption_key` +
  migrate, gotcha #7), then an optional **data-scrub hook** (`scrub_method`, a
  shipped dotted bench method that masks PII for prod→dev copies, uiux §8). The
  clone is tagged `environment=staging`.
- **`bench.update`** (reused) — updates the staging clone.
- **`site.verify_checklist`** — four probes, emitted as `CHECKLIST_RESULT <json>`
  and persisted onto the pipeline:
  1. **site boots** — `bench --site X execute frappe.ping` returns `pong`;
  2. **migrations clean** — `bench --site X migrate` is a no-op exit 0;
  3. **scheduler/workers up** — `bench --site X scheduler status` is enabled;
  4. **row-count sanity** — `frappe.client.get_count` on the clone vs the source
     shows no gross data loss.
- **`site.promote_update`** — the safety-critical job (below).

## Promote & rollback (documented + tested)

`PromoteUpdateAction` (`app/core/commands/actions.py`) runs, in order:

1. **Pre-update backup GATE (first).** A with-files backup of prod is taken
   *before any mutation*. If it fails, the job aborts and **prod is never
   touched**. The backup id is recorded on the pipeline (`pre_backup_id`); this
   is exactly the artifact the rollback restores.
2. **Apply update** — `bench update` on the production bench.
3. **Post-update check** — `frappe.ping` must return `pong`.
4. **Rollback (automatic).** If step 2 or 3 fails, the job restores the
   pre-update backup over prod (db + files + `encryption_key` + migrate), marks
   the pipeline `rolled_back`, records `rollback_job_id`, and then fails loudly
   so the operator sees the update did not land. The site is left on the
   pre-update state.

The promote job is **non-idempotent and never auto-retried** — a half-update is
never re-attempted on top of itself.

Test coverage (`backend/tests/test_updates.py`):
`test_promote_takes_prebackup_first_then_updates` asserts the backup runs before
`bench update`; `test_promote_rolls_back_on_failed_update` injects a failing
`bench update` and asserts the restore runs afterwards, the pipeline is
`rolled_back`, and the job ends `failure` with `retry_count == 0`.

## Verification commands

Backend unit/behaviour tests (no target needed — in-memory fakes):

```bash
cd backend
DEBUG=true python -m pytest tests/test_updates.py -q
# expected: 16 passed
```

Ruff + migration up/down/up on Postgres:

```bash
cd backend
ruff check app tests/test_updates.py            # expected: All checks passed!
DEBUG=true DATABASE_URL=postgresql+psycopg://fdm:fdm@localhost:5432/fdm_verify \
  alembic upgrade head && alembic downgrade -1 && alembic upgrade head
alembic heads                                    # expected: b5e9d3c1a4f7 (head)  — single head
```

Live end-to-end (requires the bench-capable target, DOO-141): create a prod
site, `POST /api/update-pipelines` to clone it, `.../update-staging`,
`.../verify` (expect `checklist_ok: true`), then `.../promote` with
`confirm_name` + `signoff` and observe the pre-update backup taken first; induce
a failing update to observe the rollback restore the prior state.
```
