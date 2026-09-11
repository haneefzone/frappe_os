"""Disaster-recovery runbook generator (session 4.3 — FDM 4.3).

Produces a self-contained DR runbook for the managed fleet: what exists, where
every backup lives, and the exact ordered steps to rebuild each server and
restore each site after a total loss. It is rendered as **Markdown** (the
operator keeps it in a break-glass location) or **PDF**, content-hashed
(SHA-256), and each generation is recorded in ``AuditLog`` — the same
tamper-evidence model the 4.4 compliance exports use.

Two guarantees drive the design:

* **Read-only / immutable.** The generator only reads; it never mutates a row.
* **No secret ever leaves.** SSH keys, DB/root passwords, restic repo passwords,
  S3 access keys and the platform master key are *never* rendered. The runbook
  points at *where* each secret is escrowed (the Fernet secret store keyed by
  ``FDM_SECRET_KEY``, ``docs/master-key-escrow.md``), not at the value
  (CLAUDE.md golden rule 6). This matters precisely because a DR runbook is a
  document that, by definition, leaves the platform.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.models.backup import Backup
from app.models.bench import Bench
from app.models.compliance import BackupPolicy, ComplianceStatus
from app.models.domain import Domain
from app.models.platform_backup import PlatformBackup
from app.models.restic import ResticRepo
from app.models.server import Server
from app.models.settings import PlatformSettings
from app.models.site import Site
from app.models.storage import StorageTarget

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

RUNBOOK_FORMATS = ("md", "pdf")

# Which Frappe major maps to which supporting stack — mirrored from CLAUDE.md so
# the rebuild steps state the correct Python/Node/MariaDB to provision. Purely
# advisory text; nothing here is executed.
_STACK_MATRIX = {
    "14": "Python 3.10 · Node 16–18 · MariaDB 10.6+",
    "15": "Python 3.11–3.12 · Node 18–20 · MariaDB 10.6+ · uv+pipx",
    "16": "Python 3.14 · Node 24 · MariaDB 10.6+ (11.8 recommended) · uv-based",
}


def _stack_hint(frappe_version: str | None) -> str:
    if not frappe_version:
        return "stack unknown — inspect source bench before provisioning"
    major = frappe_version.lstrip("vV").split(".", 1)[0]
    return _STACK_MATRIX.get(major, f"stack for Frappe {frappe_version} — verify manually")


# ---------------------------------------------------------------------------
# Result + intermediate block model
# ---------------------------------------------------------------------------


@dataclass
class RunbookResult:
    content: bytes
    media_type: str
    filename: str
    content_hash: str  # sha256 hex of ``content``
    server_count: int
    site_count: int
    scope: str  # "fleet" or the server name


# A block is a small typed unit both renderers understand, so the Markdown and
# the PDF can never drift apart — they consume the exact same structure.
#   ("h1"|"h2"|"h3", text)
#   ("p", text)
#   ("bullets", [text, ...])
#   ("steps", [text, ...])          ordered / numbered
#   ("table", (headers, [row, ...]))
#   ("note", text)                  rendered italic / prefixed
Block = tuple[str, Any]


@dataclass
class _Data:
    now: datetime
    scope_server: Server | None
    servers: list[Server]
    benches_by_server: dict[int, list[Bench]] = field(default_factory=dict)
    sites_by_bench: dict[int, list[Site]] = field(default_factory=dict)
    domains_by_site: dict[int, list[Domain]] = field(default_factory=dict)
    policy_by_site: dict[int, BackupPolicy] = field(default_factory=dict)
    status_by_site: dict[int, ComplianceStatus] = field(default_factory=dict)
    latest_backup_by_site: dict[int, Backup] = field(default_factory=dict)
    repos_by_server: dict[int, list[ResticRepo]] = field(default_factory=dict)
    storage_targets: list[StorageTarget] = field(default_factory=list)
    latest_platform_backup: PlatformBackup | None = None
    product_name: str = "FDM Platform"


# ---------------------------------------------------------------------------
# Data gathering (read-only)
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str:
    return dt.isoformat(timespec="seconds") if dt else "—"


def _gather(db: Session, *, server_id: int | None, now: datetime) -> _Data:
    scope_server: Server | None = None
    if server_id is not None:
        scope_server = db.get(Server, server_id)
        if scope_server is None:
            raise ValueError(f"Unknown server_id: {server_id}")
        servers = [scope_server]
    else:
        servers = list(db.scalars(select(Server).order_by(Server.name)))

    settings = PlatformSettings.get_or_create(db)
    data = _Data(
        now=now,
        scope_server=scope_server,
        servers=servers,
        storage_targets=list(db.scalars(select(StorageTarget).order_by(StorageTarget.name))),
        latest_platform_backup=db.scalars(
            select(PlatformBackup).order_by(PlatformBackup.created_at.desc())
        ).first(),
        product_name=settings.product_name,
    )

    for srv in servers:
        data.repos_by_server[srv.id] = list(
            db.scalars(select(ResticRepo).where(ResticRepo.server_id == srv.id))
        )
        benches = list(
            db.scalars(select(Bench).where(Bench.server_id == srv.id).order_by(Bench.name))
        )
        data.benches_by_server[srv.id] = benches
        for bench in benches:
            sites = list(
                db.scalars(select(Site).where(Site.bench_id == bench.id).order_by(Site.name))
            )
            data.sites_by_bench[bench.id] = sites
            for site in sites:
                data.domains_by_site[site.id] = list(
                    db.scalars(select(Domain).where(Domain.site_id == site.id))
                )
                policy = db.scalars(
                    select(BackupPolicy).where(BackupPolicy.site_id == site.id)
                ).first()
                if policy:
                    data.policy_by_site[site.id] = policy
                status = db.scalars(
                    select(ComplianceStatus).where(ComplianceStatus.site_id == site.id)
                ).first()
                if status:
                    data.status_by_site[site.id] = status
                latest = db.scalars(
                    select(Backup)
                    .where(Backup.site_id == site.id)
                    .order_by(Backup.created_at.desc())
                ).first()
                if latest:
                    data.latest_backup_by_site[site.id] = latest
    return data


# ---------------------------------------------------------------------------
# Block builder — the runbook itself
# ---------------------------------------------------------------------------


def _all_sites(data: _Data) -> list[tuple[Server, Bench, Site]]:
    out: list[tuple[Server, Bench, Site]] = []
    for srv in data.servers:
        for bench in data.benches_by_server.get(srv.id, []):
            for site in data.sites_by_bench.get(bench.id, []):
                out.append((srv, bench, site))
    return out


def _restore_tested_badge(data: _Data, site: Site) -> str:
    backup = data.latest_backup_by_site.get(site.id)
    if backup and backup.restore_tested:
        return "restore-tested ✓"
    return "NOT restore-tested"


def _build_blocks(data: _Data, *, generated_by: str) -> list[Block]:
    scope = data.scope_server.name if data.scope_server else "entire fleet"
    sites = _all_sites(data)
    blocks: list[Block] = [
        ("h1", f"{data.product_name} — Disaster Recovery Runbook"),
        ("note", "Break-glass procedure. Follow top to bottom after a total loss."),
        (
            "table",
            (
                ["Field", "Value"],
                [
                    ["Scope", scope],
                    ["Generated at (UTC)", _iso(data.now)],
                    ["Generated by", generated_by or "—"],
                    ["Servers", str(len(data.servers))],
                    ["Sites", str(len(sites))],
                ],
            ),
        ),
    ]

    # --- Recovery objectives -------------------------------------------------
    rpos = sorted({p.rpo_hours for p in data.policy_by_site.values() if p.enabled})
    blocks.append(("h2", "1. Recovery objectives"))
    if rpos:
        blocks.append(
            (
                "p",
                f"Tightest RPO across enabled backup policies: {rpos[0]}h; "
                f"loosest: {rpos[-1]}h. A site is only as recoverable as its most "
                "recent verified backup — confirm the last-backup times in section 4 "
                "before promising an RTO.",
            )
        )
    else:
        blocks.append(
            ("note", "No enabled backup policies found — recovery objectives are undefined.")
        )

    # --- Recovery order ------------------------------------------------------
    blocks.append(("h2", "2. Recovery order"))
    blocks.append(
        (
            "steps",
            [
                "Restore the control plane itself (this platform) — section 3.",
                "Provision each managed server host + stack — section 5, phase 1.",
                "Restore the config tier (nginx/supervisor/redis/mariadb) from restic — phase 2.",
                "Recreate benches, then restore each site's database + files — phases 3–4.",
                "Verify sites, domains/SSL and uptime — phase 5.",
            ],
        )
    )

    # --- Control-plane recovery ---------------------------------------------
    blocks.append(("h2", "3. Control-plane recovery (this platform)"))
    pb = data.latest_platform_backup
    if pb:
        blocks.append(
            (
                "table",
                (
                    ["Field", "Value"],
                    [
                        ["Latest self-backup id", str(pb.id)],
                        ["Status", pb.status],
                        ["Encrypted", "yes" if pb.encrypted else "no"],
                        ["Ciphertext SHA-256", pb.sha256 or "—"],
                        ["Verify status", pb.verify_status],
                        ["Verified at", _iso(pb.verified_at)],
                        ["Escrow restore-drill at", _iso(pb.restore_tested_at)],
                        ["Object key", pb.object_key or "—"],
                    ],
                ),
            )
        )
    else:
        blocks.append(
            ("note", "No platform self-backup on record — see section 6 before proceeding.")
        )
    blocks.append(
        (
            "steps",
            [
                "Stand up a fresh host with the platform stack (see docs/production-setup.md).",
                "Retrieve the encrypted self-backup from offsite (object key above).",
                "Recover the master key AND the backup passphrase from their SEPARATE "
                "escrow locations — see docs/master-key-escrow.md. Neither is stored here.",
                "Decrypt + restore the platform database; set FDM_SECRET_KEY to the "
                "escrowed master key so the Fernet secret store decrypts server credentials.",
                "Verify the ciphertext SHA-256 matches the value above before decrypting.",
            ],
        )
    )

    # --- System inventory ----------------------------------------------------
    blocks.append(("h2", "4. System inventory"))
    srv_rows = []
    for srv in data.servers:
        n_benches = len(data.benches_by_server.get(srv.id, []))
        n_sites = sum(
            len(data.sites_by_bench.get(b.id, [])) for b in data.benches_by_server.get(srv.id, [])
        )
        srv_rows.append(
            [
                srv.name,
                f"{srv.hostname}:{srv.ssh_port}",
                srv.os_version or "—",
                srv.env_tag,
                str(n_benches),
                str(n_sites),
            ]
        )
    blocks.append(
        ("table", (["Server", "SSH", "OS", "Env", "Benches", "Sites"], srv_rows))
    )

    site_rows = []
    for srv, bench, site in sites:
        primary = next(
            (d.domain for d in data.domains_by_site.get(site.id, []) if d.is_primary),
            "—",
        )
        site_rows.append(
            [
                site.name,
                srv.name,
                bench.name,
                bench.frappe_version or "—",
                primary,
                _restore_tested_badge(data, site),
            ]
        )
    if site_rows:
        blocks.append(
            (
                "table",
                (
                    ["Site", "Server", "Bench", "Frappe", "Primary domain", "Restore"],
                    site_rows,
                ),
            )
        )
    else:
        blocks.append(("note", "No sites in scope."))

    # --- Backup inventory ----------------------------------------------------
    blocks.append(("h2", "5. Backup inventory"))
    if data.storage_targets:
        st_rows = [
            [
                st.name,
                st.provider,
                st.bucket + (f"/{st.path_prefix}" if st.path_prefix else ""),
                st.endpoint_url or "(provider default)",
                st.region or "—",
                "enabled" if st.enabled else "disabled",
            ]
            for st in data.storage_targets
        ]
        blocks.append(
            (
                "h3",
                "Offsite storage targets (restic repositories live here — passwords are "
                "in the secret store, never below)",
            )
        )
        blocks.append(
            (
                "table",
                (["Target", "Provider", "Bucket/prefix", "Endpoint", "Region", "State"], st_rows),
            )
        )
    else:
        blocks.append(("note", "No storage targets configured — backups may be local only."))

    repo_rows = []
    for srv in data.servers:
        for repo in data.repos_by_server.get(srv.id, []):
            target = next(
                (t for t in data.storage_targets if t.id == repo.storage_target_id), None
            )
            repo_rows.append(
                [
                    srv.name,
                    repo.prefix,
                    target.name if target else "(local)",
                    "yes" if repo.initialized else "no",
                    _iso(repo.last_backup_at),
                    _iso(repo.last_check_at),
                ]
            )
    if repo_rows:
        blocks.append(("h3", "restic repositories (config-tier + system snapshots)"))
        blocks.append(
            (
                "table",
                (
                    ["Server", "Repo prefix", "Target", "Init", "Last backup", "Last check"],
                    repo_rows,
                ),
            )
        )

    bp_rows = []
    for _srv, _bench, site in sites:
        policy = data.policy_by_site.get(site.id)
        status = data.status_by_site.get(site.id)
        latest = data.latest_backup_by_site.get(site.id)
        bp_rows.append(
            [
                site.name,
                f"{policy.rpo_hours}h" if policy else "—",
                f"{policy.retention_days}d" if policy and policy.retention_days else "—",
                "yes" if policy and policy.require_offsite else "no",
                status.state if status else "unknown",
                _iso(status.last_backup_at) if status else "—",
                (latest.storage_state if latest else "—"),
            ]
        )
    if bp_rows:
        blocks.append(("h3", "Per-site backup policy + compliance state"))
        blocks.append(
            (
                "table",
                (
                    ["Site", "RPO", "Retention", "Offsite", "State", "Last backup", "Storage"],
                    bp_rows,
                ),
            )
        )

    # --- Restore procedure ---------------------------------------------------
    blocks.append(("h2", "6. Restore procedure (per server)"))
    if not data.servers:
        blocks.append(("note", "No servers in scope."))
    for srv in data.servers:
        blocks.append(("h3", f"Server: {srv.name} ({srv.hostname})"))
        benches = data.benches_by_server.get(srv.id, [])
        stack = _stack_hint(benches[0].frappe_version if benches else None)
        steps = [
            f"Provision a host reachable at {srv.hostname}:{srv.ssh_port} "
            f"({srv.env_tag}); install the stack: {stack}. Ensure `uv` is present "
            "(bench init requires it even for v14/v15).",
            "Install the fdm-platform sudoers allowlist + fdm-elevate/fdm-certbot "
            "wrappers (docs/implementation-plan.md security section).",
            "Restore the config tier from restic (nginx, supervisor, redis, mariadb "
            "configs + `dpkg --get-selections`) — repo prefixes are in section 5.",
        ]
        for bench in benches:
            bstack = _stack_hint(bench.frappe_version)
            steps.append(
                f"Recreate bench `{bench.name}` at {bench.path} "
                f"(Frappe {bench.frappe_version or '?'} — {bstack}); restore its "
                "common_site_config.json so ports match "
                f"(web {bench.webserver_port or '?'}, socketio {bench.socketio_port or '?'})."
            )
            for site in data.sites_by_bench.get(bench.id, []):
                steps.append(
                    f"Restore site `{site.name}`: restore DB dump + public/private "
                    "files, copy `encryption_key` from the source site_config.json into "
                    "the target, then `bench --site "
                    f"{site.name} migrate` (use --force; NEVER downgrade versions). "
                    f"[{_restore_tested_badge(data, site)}]"
                )
        steps.append(
            "Re-issue SSL for each domain (certbot via fdm-certbot wrapper) and "
            "confirm DNS resolves; re-enable scheduler + uptime checks; verify site "
            "health is green."
        )
        blocks.append(("steps", steps))

    # --- Secret escrow -------------------------------------------------------
    blocks.append(("h2", "7. Secret escrow locations (WHERE, never the value)"))
    blocks.append(
        (
            "bullets",
            [
                "SSH keys / passphrases / DB root passwords / restic repo passwords / "
                "S3 access keys: Fernet-encrypted in the platform DB secret store, "
                "decryptable only with the master key from env FDM_SECRET_KEY.",
                "Platform master key AND self-backup passphrase: escrowed out-of-band "
                "and separately — see docs/master-key-escrow.md.",
                "None of the above is printed in this runbook, in logs, or in job "
                "params (golden rule 6). Retrieve them from escrow at restore time.",
            ],
        )
    )

    blocks.append(("h2", "8. Verification checklist"))
    blocks.append(
        (
            "bullets",
            [
                "Each restored site returns healthy and `bench --site <s> doctor` passes.",
                "Every primary domain resolves and serves a valid TLS certificate.",
                "A fresh backup runs green to the offsite target for each site.",
                "The control plane can SSH to every managed server (secret store decrypts).",
            ],
        )
    )
    return blocks


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _md_escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _render_md(blocks: list[Block]) -> str:
    out: list[str] = []
    for kind, payload in blocks:
        if kind == "h1":
            out.append(f"# {payload}\n")
        elif kind == "h2":
            out.append(f"\n## {payload}\n")
        elif kind == "h3":
            out.append(f"\n### {payload}\n")
        elif kind == "p":
            out.append(f"{payload}\n")
        elif kind == "note":
            out.append(f"> {payload}\n")
        elif kind == "bullets":
            out.extend(f"- {item}" for item in payload)
            out.append("")
        elif kind == "steps":
            out.extend(f"{i}. {item}" for i, item in enumerate(payload, 1))
            out.append("")
        elif kind == "table":
            headers, rows = payload
            out.append("| " + " | ".join(_md_escape_cell(h) for h in headers) + " |")
            out.append("| " + " | ".join("---" for _ in headers) + " |")
            for row in rows:
                out.append("| " + " | ".join(_md_escape_cell(str(c)) for c in row) + " |")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def _render_pdf(blocks: list[Block], *, title: str) -> bytes:
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    def esc(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=7.5, leading=9.5)
    head = ParagraphStyle("head", parent=cell, fontName="Helvetica-Bold", textColor=colors.white)
    story: list[Any] = []
    for kind, payload in blocks:
        if kind == "h1":
            story.append(Paragraph(esc(payload), styles["Title"]))
        elif kind == "h2":
            story += [Spacer(1, 3 * mm), Paragraph(esc(payload), styles["Heading2"])]
        elif kind == "h3":
            story += [Spacer(1, 2 * mm), Paragraph(esc(payload), styles["Heading4"])]
        elif kind == "p":
            story.append(Paragraph(esc(payload), cell))
        elif kind == "note":
            story.append(Paragraph(f"<i>{esc(payload)}</i>", cell))
        elif kind in ("bullets", "steps"):
            items = [ListItem(Paragraph(esc(item), cell)) for item in payload]
            story.append(
                ListFlowable(items, bulletType="1" if kind == "steps" else "bullet", leftIndent=10)
            )
        elif kind == "table":
            headers, rows = payload
            data = [[Paragraph(esc(h), head) for h in headers]]
            data += [[Paragraph(esc(c), cell) for c in row] for row in rows]
            col_w = doc.width / len(headers)
            table = Table(data, colWidths=[col_w] * len(headers), repeatRows=1, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#232326")),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        (
                            "ROWBACKGROUNDS",
                            (0, 1),
                            (-1, -1),
                            [colors.white, colors.HexColor("#FAFAFA")],
                        ),
                        ("LEFTPADDING", (0, 0), (-1, -1), 3),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]
                )
            )
            story += [table, Spacer(1, 2 * mm)]
    doc.build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def generate_runbook(
    db: Session,
    *,
    fmt: str = "md",
    server_id: int | None = None,
    generated_by: str = "",
    now: datetime | None = None,
) -> RunbookResult:
    """Generate the DR runbook and return the rendered bytes + SHA-256.

    ``server_id`` scopes the runbook to a single managed server; omit it for a
    full-fleet runbook. The AuditLog record is written by the route (which owns
    the request user + source IP), mirroring the 4.4 compliance export flow.
    """
    if fmt not in RUNBOOK_FORMATS:
        raise ValueError(f"Unknown format: {fmt!r} (expected one of {RUNBOOK_FORMATS})")
    now = now or datetime.now(UTC)

    data = _gather(db, server_id=server_id, now=now)
    blocks = _build_blocks(data, generated_by=generated_by)
    scope = data.scope_server.name if data.scope_server else "fleet"
    site_count = len(_all_sites(data))
    date_str = now.strftime("%Y%m%d")
    slug = "".join(c if c.isalnum() else "-" for c in scope).strip("-").lower() or "fleet"

    if fmt == "md":
        content = _render_md(blocks).encode("utf-8")
        media_type = "text/markdown; charset=utf-8"
        filename = f"dr-runbook-{slug}-{date_str}.md"
    else:
        content = _render_pdf(blocks, title=f"{data.product_name} DR Runbook — {scope}")
        media_type = "application/pdf"
        filename = f"dr-runbook-{slug}-{date_str}.pdf"

    return RunbookResult(
        content=content,
        media_type=media_type,
        filename=filename,
        content_hash=hashlib.sha256(content).hexdigest(),
        server_count=len(data.servers),
        site_count=site_count,
        scope=scope,
    )


__all__ = ["RUNBOOK_FORMATS", "RunbookResult", "generate_runbook"]
