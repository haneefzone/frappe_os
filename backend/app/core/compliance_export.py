"""Compliance report export service (session 4.4 — FDM 4.4).

Generates ISO-27001-friendly compliance reports as PDF or CSV, content-hashes
each export, and records the hash in AuditLog for tamper evidence.

Three report types:
  access          — AuditLog entries for the date range; ISO A.9 access control.
  backup_evidence — Per-site backup compliance state + policy; ISO A.12.3.
  access_review   — Point-in-time users × roles matrix; ISO A.9.2.5.

Every report is read-only/immutable (never mutates AuditLog or any other table).
"""

from __future__ import annotations

import csv
import hashlib
import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Report type / format constants
# ---------------------------------------------------------------------------

REPORT_TYPES = ("access", "backup_evidence", "access_review")
REPORT_FORMATS = ("csv", "pdf")

# Column definitions per report type (header, extractor)
_ACCESS_COLUMNS = [
    "Timestamp (UTC)",
    "User",
    "Action",
    "Entity Type",
    "Entity ID",
    "Result",
    "Source IP",
    "Job ID",
    "Summary",
]

_BACKUP_COLUMNS = [
    "Site",
    "Policy Enabled",
    "RPO (hours)",
    "Retention (days)",
    "Require Offsite",
    "Compliance State",
    "Last Backup (UTC)",
    "Breaches",
    "Evaluated At (UTC)",
]

_ACCESS_REVIEW_COLUMNS = [
    "User ID",
    "Email",
    "Full Name",
    "Role",
    "Active",
    "Last Login (UTC)",
    "Created At (UTC)",
]


# ---------------------------------------------------------------------------
# Data assembly helpers
# ---------------------------------------------------------------------------

def _load_access_rows(
    db: Session, since: datetime | None, until: datetime | None
) -> list[dict]:
    from app.models.audit import AuditLog
    from app.models.auth import User

    stmt = select(AuditLog).order_by(AuditLog.ts.asc())
    if since:
        stmt = stmt.where(AuditLog.ts >= since)
    if until:
        stmt = stmt.where(AuditLog.ts <= until)
    # Cap at 100k rows so a runaway export never OOMs.
    stmt = stmt.limit(100_000)
    rows = list(db.scalars(stmt).all())

    # Build email lookup
    ids = {r.user_id for r in rows if r.user_id is not None}
    email_map: dict[int, str] = {}
    if ids:
        email_map = {
            uid: email
            for uid, email in db.execute(
                select(User.id, User.email).where(User.id.in_(ids))
            ).all()
        }

    return [
        {
            "ts": r.ts.strftime("%Y-%m-%dT%H:%M:%SZ") if r.ts else "",
            "user": email_map.get(r.user_id) or (str(r.user_id) if r.user_id else "—"),
            "action": r.action,
            "entity_type": r.entity_type or "",
            "entity_id": r.entity_id or "",
            "result": r.result,
            "source_ip": r.source_ip or "",
            "job_id": str(r.job_id) if r.job_id is not None else "",
            "summary": r.summary,
        }
        for r in rows
    ]


def _load_backup_evidence_rows(
    db: Session, since: datetime | None, until: datetime | None
) -> list[dict]:
    from app.models.compliance import BackupPolicy, ComplianceStatus
    from app.models.site import Site

    # Join policy → status → site for a complete per-site picture.
    stmt = (
        select(BackupPolicy, ComplianceStatus, Site)
        .join(ComplianceStatus, ComplianceStatus.site_id == BackupPolicy.site_id, isouter=True)
        .join(Site, Site.id == BackupPolicy.site_id)
        .order_by(Site.name.asc())
    )
    # If a date range is given, filter to statuses evaluated within the window
    # (NULL evaluated_at = never evaluated → include regardless).
    if since:
        stmt = stmt.where(
            (ComplianceStatus.evaluated_at >= since) | (ComplianceStatus.evaluated_at.is_(None))
        )
    if until:
        stmt = stmt.where(
            (ComplianceStatus.evaluated_at <= until) | (ComplianceStatus.evaluated_at.is_(None))
        )

    rows = []
    for policy, status, site in db.execute(stmt).all():
        breach_codes = ""
        last_backup = ""
        evaluated_at = ""
        state = "unknown"
        if status:
            state = status.state
            last_backup = (
                status.last_backup_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if status.last_backup_at
                else ""
            )
            evaluated_at = (
                status.evaluated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if status.evaluated_at
                else ""
            )
            breach_codes = ", ".join(b.get("code", "") for b in (status.breaches or []))
        rows.append(
            {
                "site": site.name,
                "policy_enabled": "yes" if policy.enabled else "no",
                "rpo_hours": str(policy.rpo_hours),
                "retention_days": str(policy.retention_days) if policy.retention_days else "—",
                "require_offsite": "yes" if policy.require_offsite else "no",
                "state": state,
                "last_backup": last_backup,
                "breaches": breach_codes,
                "evaluated_at": evaluated_at,
            }
        )
    return rows


def _load_access_review_rows(db: Session) -> list[dict]:
    from app.models.auth import Role, User

    stmt = (
        select(User, Role)
        .join(Role, Role.id == User.role_id)
        .order_by(Role.name.asc(), User.email.asc())
    )
    rows = []
    for user, role in db.execute(stmt).all():
        rows.append(
            {
                "user_id": str(user.id),
                "email": user.email,
                "full_name": user.full_name,
                "role": role.name,
                "active": "yes" if user.is_active else "no",
                "last_login": (
                    user.last_login.strftime("%Y-%m-%dT%H:%M:%SZ") if user.last_login else ""
                ),
                "created_at": (
                    user.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if user.created_at else ""
                ),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# CSV rendering
# ---------------------------------------------------------------------------

def _rows_to_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility


def _access_csv(data: list[dict]) -> bytes:
    return _rows_to_csv(
        _ACCESS_COLUMNS,
        [
            [
                d["ts"], d["user"], d["action"], d["entity_type"],
                d["entity_id"], d["result"], d["source_ip"], d["job_id"], d["summary"],
            ]
            for d in data
        ],
    )


def _backup_evidence_csv(data: list[dict]) -> bytes:
    return _rows_to_csv(
        _BACKUP_COLUMNS,
        [
            [
                d["site"], d["policy_enabled"], d["rpo_hours"], d["retention_days"],
                d["require_offsite"], d["state"], d["last_backup"], d["breaches"],
                d["evaluated_at"],
            ]
            for d in data
        ],
    )


def _access_review_csv(data: list[dict]) -> bytes:
    return _rows_to_csv(
        _ACCESS_REVIEW_COLUMNS,
        [
            [
                d["user_id"], d["email"], d["full_name"], d["role"],
                d["active"], d["last_login"], d["created_at"],
            ]
            for d in data
        ],
    )


# ---------------------------------------------------------------------------
# PDF rendering (reportlab)
# ---------------------------------------------------------------------------

def _build_pdf(
    title: str,
    subtitle: str,
    headers: list[str],
    rows: list[list[str]],
    content_hash_placeholder: str = "",
) -> bytes:
    """Render a minimal but ISO-presentable PDF using reportlab."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    page = landscape(A4) if len(headers) > 6 else A4
    doc = SimpleDocTemplate(
        buf,
        pagesize=page,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=20 * mm,
        title=title,
    )

    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontSize = 7
    normal.leading = 9

    story = []

    # Header block
    story.append(Paragraph(f"<b>{title}</b>", styles["Title"]))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph(subtitle, styles["Normal"]))
    story.append(Spacer(1, 4 * mm))

    # Table data
    col_count = len(headers)
    table_data: list[list] = [headers]
    for row in rows:
        # Wrap long cells in Paragraphs to allow text wrapping
        table_data.append([Paragraph(str(cell)[:300], normal) for cell in row])

    if len(table_data) == 1:
        table_data.append(["No data in the selected range."] + [""] * (col_count - 1))

    # Equal-width columns
    page_width = page[0] - 30 * mm
    col_width = page_width / col_count

    tbl = Table(table_data, colWidths=[col_width] * col_count, repeatRows=1)
    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a1a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 7),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
                ("TOPPADDING", (0, 0), (-1, 0), 4),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
                ("FONTSIZE", (0, 1), (-1, -1), 7),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(tbl)

    if content_hash_placeholder:
        story.append(Spacer(1, 4 * mm))
        story.append(
            Paragraph(
                f"<font size='6' color='#888888'>SHA-256: {content_hash_placeholder}</font>",
                styles["Normal"],
            )
        )

    doc.build(story)
    return buf.getvalue()


def _access_pdf(data: list[dict], subtitle: str) -> bytes:
    rows = [
        [d["ts"], d["user"], d["action"], d["entity_type"],
         d["result"], d["source_ip"], d["summary"]]
        for d in data
    ]
    # Omit entity_id and job_id from PDF to fit columns
    headers = ["Timestamp", "User", "Action", "Entity", "Result", "Source IP", "Summary"]
    return _build_pdf("Access / Audit Trail Report", subtitle, headers, rows)


def _backup_evidence_pdf(data: list[dict], subtitle: str) -> bytes:
    rows = [
        [d["site"], d["rpo_hours"] + "h", d["retention_days"],
         d["require_offsite"], d["state"], d["last_backup"], d["breaches"]]
        for d in data
    ]
    headers = ["Site", "RPO", "Retention", "Offsite Req.", "State", "Last Backup", "Breaches"]
    return _build_pdf("Backup Evidence Report", subtitle, headers, rows)


def _access_review_pdf(data: list[dict], subtitle: str) -> bytes:
    rows = [
        [d["user_id"], d["email"], d["full_name"], d["role"], d["active"], d["last_login"]]
        for d in data
    ]
    headers = ["ID", "Email", "Full Name", "Role", "Active", "Last Login"]
    return _build_pdf("Access Review — Users × Roles", subtitle, headers, rows)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

class ExportResult:
    __slots__ = ("content", "content_hash", "filename", "media_type", "row_count")

    def __init__(
        self,
        content: bytes,
        content_hash: str,
        filename: str,
        media_type: str,
        row_count: int,
    ) -> None:
        self.content = content
        self.content_hash = content_hash
        self.filename = filename
        self.media_type = media_type
        self.row_count = row_count


def generate_report(
    db: Session,
    *,
    report_type: str,
    fmt: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> ExportResult:
    """Assemble data + render to PDF or CSV, hash, and return the result.

    Caller is responsible for recording the hash in AuditLog.
    """
    if report_type not in REPORT_TYPES:
        raise ValueError(f"Unknown report_type: {report_type}")
    if fmt not in REPORT_FORMATS:
        raise ValueError(f"Unknown format: {fmt}")

    now_label = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    since_label = since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else "beginning"
    until_label = until.strftime("%Y-%m-%dT%H:%M:%SZ") if until else now_label
    subtitle = f"Generated: {now_label}  |  Period: {since_label} → {until_label}"

    if report_type == "access":
        data = _load_access_rows(db, since, until)
        row_count = len(data)
        if fmt == "csv":
            content = _access_csv(data)
        else:
            content = _access_pdf(data, subtitle)
        stem = "access-audit"

    elif report_type == "backup_evidence":
        data = _load_backup_evidence_rows(db, since, until)
        row_count = len(data)
        if fmt == "csv":
            content = _backup_evidence_csv(data)
        else:
            content = _backup_evidence_pdf(data, subtitle)
        stem = "backup-evidence"

    else:  # access_review
        data = _load_access_review_rows(db)
        row_count = len(data)
        if fmt == "csv":
            content = _access_review_csv(data)
        else:
            content = _access_review_pdf(data, subtitle)
        stem = "access-review"

    content_hash = hashlib.sha256(content).hexdigest()
    date_tag = datetime.now(UTC).strftime("%Y%m%d")
    ext = fmt
    filename = f"fdm-{stem}-{date_tag}.{ext}"
    media_type = "text/csv" if fmt == "csv" else "application/pdf"

    return ExportResult(
        content=content,
        content_hash=content_hash,
        filename=filename,
        media_type=media_type,
        row_count=row_count,
    )
