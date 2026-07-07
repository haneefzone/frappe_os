# implementation-plan.md — STUB (original document not attached)

> **Status: placeholder.** The original `docs/implementation-plan.md` (architecture + phases + security
> section) was **not** among the study documents attached to
> [DOO-36](../../..). Its content is covered by the other docs as mapped below — with **one real gap**
> (the sudoers allowlist), for which an interim draft is provided here so Session 0.4 does not dead-end.
> Gap recorded on issue DOO-37.

## Where the referenced content actually lives

| implementation-plan.md was cited for | Use instead |
|---|---|
| Architecture (stack, fixed decisions) | `CLAUDE.md` → "Architecture (fixed decisions)" + "Job engine pattern" + "Folder structure" |
| Phases / roadmap | `docs/PROJECT_PLAN.md` (authoritative session list and acceptance criteria) |
| Design system + screens | `docs/uiux-spec.md` (Part B) |
| Gap analysis / product scope | `docs/uiux-spec.md` (Part A) |
| Per-session scope | `docs/SESSION_PROMPTS.md` |
| **Security section — sudoers allowlist** (needed by Session 0.4 and used by Sessions 1.10/1.12) | **Missing. Interim draft below.** |

## Security section: sudoers allowlist (ratified by the Technical Architect in Session 0.4)

The platform SSHes into managed servers as the bench-owner user (e.g. `frappe`), which must NOT have
general sudo. Grant only the exact commands the job templates need, via a drop-in file
(`/etc/sudoers.d/fdm-platform`, mode 0440, always edited with `visudo -cf` validation):

```
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

# bench setup production needs broader rights ONCE — run it with a temporary elevation, not a
# permanent allowlist entry (Phase 2.5 decision point).
```

Rules: no wildcard binaries, no shell built-ins, absolute paths only, one drop-in file owned by the
platform so drift detection (Phase 6.7) can hash it. Every sudo-using command template in
`app/core/commands/` must reference a line in this allowlist.

## What to do when the original document surfaces

Replace this stub wholesale and diff its security section against the interim draft above; any
command templates added meanwhile must be re-checked against the ratified allowlist.
