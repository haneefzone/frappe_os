# Frappe App Store — Backend API contract (DOO-1194)

Frozen contract for the Frontend / Portal slice. The catalog is the cached
`frappe/marketplace` git checkout (not an API): shallow-cloned, refreshed hourly
by the scheduler, served stale when the remote is unreachable.

All endpoints are under `/api`. Compatibility is computed against a **target
bench's installed Frappe version** (`Bench.frappe_version`, parsed by discovery).

## Browse

`GET /api/marketplace/apps` — permission `read`.

Query params (all optional): `bench` (bench id — enables compatibility fields),
`category`, `q` (matches title/description).

Returns `MarketplaceAppOut[]`:

```jsonc
{
  "name": "hrms", "title": "Frappe HR", "description": "...",
  "repo": "https://github.com/frappe/hrms", "logo_url": "...",
  "website": "...", "documentation": "...",
  "categories": ["HR"], "category": "Applications", "stars": 50,
  // present only when ?bench= is given:
  "is_installable": true,                 // false when incompatible / dep conflict
  "reason": null,                         // human-readable when is_installable=false
  "latest_compatible_version": "16.15.0"  // null when none compatible
}
```

Without `?bench=`, `is_installable`/`reason`/`latest_compatible_version` are `null`.

## Detail

`GET /api/marketplace/apps/{name}?bench={id}` — permission `read`. Returns
`MarketplaceAppDetailOut` = the browse shape plus:

```jsonc
{
  "releases": [
    {"version":"16.15.0","branch":"version-16","commit":"...","frappe_core":">=16.0.0,<17.0.0",
     "dependencies":{"erpnext":">=16.0.0,<17.0.0"},"channel":"stable","is_compatible":true}
  ],
  "plan": [ {"app":"erpnext","version":"16.30.0","branch":"version-16","commit":"...",
             "repo":"https://github.com/frappe/erpnext","channel":"stable",
             "reason":"dependency of hrms"},
            {"app":"hrms","version":"16.15.0","branch":"version-16","reason":"requested", ...} ],
  "plan_error": null   // legible string (cycle/conflict/incompatible) when plan is null
}
```

`plan` is the ordered install plan (dependencies first). `404` if the app is not
in the catalog. `503` if the catalog has never been cloned. `409` if the bench has
no known Frappe version (run discovery first).

## Refresh

`POST /api/marketplace/refresh` — permission `app:manage`. Forces a catalog sync.
Returns `{ "refreshed": bool, "served_stale": bool, "app_count": int }`. A remote
that is unreachable serves the existing cache (`served_stale: true`); only a first
sync with no cache and no network returns `503`.

## Install

`POST /api/sites/{site_id}/marketplace-apps` — permission `app:manage`.
Body: `{ "app": "hrms", "priority": "high" }`.

Resolves the full transitive dependency plan against the site's bench's Frappe
version and **validates it before enqueuing anything** (never a partial install).
On success returns `201` with a `JobDetail` for a `site.install_marketplace_app`
job that reuses the existing get-app/install-app connector path (same queue, same
progress surface). The job's `params_sanitized.plan_b64` is base64(JSON) of the
ordered `[{app, source, branch}]` steps.

Errors:
- `422` — cycle, version conflict, incompatible, unknown app, or a dependency
  repo whose host is off the allowlist. `detail` is a legible message; nothing is
  enqueued.
- `409` — bench has no known Frappe version, or a job is already running on the
  site (`error.blocking_job_id`).
- `404` — site not found. `503` — catalog unavailable.

Progress/logs are the existing job surface (`GET /api/jobs/{id}`, job-logs SSE) —
identical to a custom-app install, so the UI reuses that component.
