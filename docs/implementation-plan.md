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

# Domains & SSL (Session 2.4). The vhost writer targets the bench-owned
# config/nginx-vhosts/ dir (no root file write), so the only new root rights it
# needs are a graceful config reload after the `nginx -t` gate passes, plus
# certbot to issue/renew Let's Encrypt certs (writes /etc/letsencrypt). `reload`
# (keeps connections, unlike the existing `restart nginx`) is added.
#
# certbot is NOT granted directly. A sudoers `*` matches spaces and arbitrary
# args, and `certbot certonly`/`renew` accept `--deploy-hook`/`--pre-hook`/
# `--post-hook`, which run an arbitrary shell command AS ROOT — so a bare
# `certonly *` / `renew *` grant is a root-escalation vector (DOO-220: any code
# running as `frappe` could inject `--deploy-hook 'id > /root/pwned'`). Sudoers
# glob matching cannot reliably exclude an injected `--*-hook`, so — exactly like
# `fdm-elevate` for `bench setup production` (2.5) — certbot is routed through the
# fixed root-owned wrapper `deploy/fdm-certbot` (installed at
# /usr/local/sbin/fdm-certbot). The wrapper pins the exact certonly/renew argv,
# builds the certbot command line itself, and REFUSES any flag-shaped argument, so
# the NOPASSWD line targets only the wrapper — never the raw certbot binary.
# `certbot certificates` is read-only (no hook that mutates) and the wrapper's
# `certificates` subcommand takes no arguments. Ratified in the DOO-135/DOO-214
# Technical-Architect review; the wrapper is the DOO-220 fix required before a
# live install.
frappe ALL=(root) NOPASSWD: /usr/bin/systemctl reload nginx
frappe ALL=(root) NOPASSWD: /usr/local/sbin/fdm-certbot

# Config drift detection (Session 6.7). Read-only hashing of root-owned managed
# config artefacts. Each is a fixed, absolute, single command with NO wildcard
# and NO shell (`sudo -n` fails loudly rather than prompting). `cat` cannot
# mutate; `find` here only lists names. These grant READ, never write/reload.
# The drift checker prefers bench-owner-readable paths (common_site_config.json,
# site_config.json, config/nginx-vhosts/) which need no sudo; only these four
# root-owned artefacts require elevation. See app/core/drift.py ARTIFACTS.
frappe ALL=(root) NOPASSWD: /bin/cat /etc/nginx/nginx.conf
frappe ALL=(root) NOPASSWD: /bin/cat /etc/supervisor/supervisord.conf
frappe ALL=(root) NOPASSWD: /bin/cat /etc/sudoers.d/fdm-platform
frappe ALL=(root) NOPASSWD: /bin/cat /etc/sudoers.d/fdm-prod-elevation
frappe ALL=(root) NOPASSWD: /usr/bin/find /etc/supervisor/conf.d -maxdepth 1 -type f -printf %f\n

# bench setup production needs broader rights ONCE — run it with a temporary elevation, not a
# permanent allowlist entry (Phase 2.5 decision point).
#
# RESOLVED in Session 2.5: the only standing grant is the fixed helper below; it installs a
# time-boxed, single-command drop-in for `bench setup production <user>` and revokes it after
# the run, so no permanent setup-production grant exists (drift-baseline-clean, Phase 6.7).
frappe ALL=(root) NOPASSWD: /usr/local/sbin/fdm-elevate

# Tool installer (Session 6.1). Each root-needing install is ONE explicit line
# with the package name baked in — no wildcard binary, no `apt-get install *`
# (which would let any package, including a malicious local .deb, be named).
# The command templates render exactly these argvs as constants; nothing is
# assembled from a tool id.
frappe ALL=(root) NOPASSWD: /usr/bin/apt-get install -y git
frappe ALL=(root) NOPASSWD: /usr/bin/apt-get install -y redis-server
frappe ALL=(root) NOPASSWD: /usr/bin/apt-get install -y nginx
frappe ALL=(root) NOPASSWD: /usr/bin/apt-get install -y supervisor
frappe ALL=(root) NOPASSWD: /usr/bin/apt-get install -y htop
frappe ALL=(root) NOPASSWD: /usr/bin/apt-get install -y jq

# wkhtmltopdf needs the patched-Qt 0.12.6.1 build (gotcha #6), which is a .deb
# fetched from GitHub rather than a distro package. dpkg is NOT granted directly:
# a .deb's maintainer scripts run as root by design, so `dpkg -i <path>` where the
# path is bench-user-writable is arbitrary root code execution (a TOCTOU — the
# caller can swap the file after any checksum gate the caller itself performs).
# That is the DOO-220 lesson again: sudoers can pin a path but not the bytes at
# it. So wkhtmltopdf is routed through the fixed root-owned wrapper
# `deploy/fdm-wkhtmltopdf` (installed at /usr/local/sbin/fdm-wkhtmltopdf), which
# takes no caller input at all — URL, version and SHA-256 are pinned in the
# wrapper, it stages into root-owned 0700 /var/lib/fdm-platform/wkhtmltopdf, and
# it verifies the digest itself before invoking dpkg.
frappe ALL=(root) NOPASSWD: /usr/local/sbin/fdm-wkhtmltopdf
```

**Userspace installs need no sudoers line at all.** `uv`, Node (via nvm), the
bench CLI (via `uv tool install`), `gh`, `code-server` and `claude-code` all
install into the bench-owner's `$HOME` and are executed as the SSH user with no
sudo in the argv. Python and MariaDB are **detect-only** by design — swapping the
system interpreter or a database major version is an OS/data-migration decision,
not a one-click button, so no template (and no sudo right) exists for them.

The Session 2.5 elevation mechanism and rollback are documented in
`docs/production-setup.md`. The `fdm-elevate` helper ships in `deploy/fdm-elevate`; the
`fdm-certbot` SSL helper (Session 2.4 / DOO-220) ships in `deploy/fdm-certbot` and installs
to `/usr/local/sbin/fdm-certbot` (root-owned, 0755); both are standing, fixed-argv wrappers.
Drift detection (6.7) hashes `/etc/sudoers.d/fdm-platform` and asserts no
`/etc/sudoers.d/fdm-prod-elevation` drop-in remains at rest.

Rules: no wildcard binaries, no shell built-ins, absolute paths only, one drop-in file owned by the
platform so drift detection (Phase 6.7) can hash it. Every sudo-using command template in
`app/core/commands/` must reference a line in this allowlist.

## What to do when the original document surfaces

Replace this stub wholesale and diff its security section against the interim draft above; any
command templates added meanwhile must be re-checked against the ratified allowlist.
