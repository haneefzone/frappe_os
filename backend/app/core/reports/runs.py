"""Executing a report: generate -> render -> persist artifact -> ReportRun row.

This is the single place a report artifact is produced, whether the trigger was
an interactive download, a queued job, or a schedule. Everything that makes an
artifact *evidence* — the sha256, the byte size, the row count, the requesting
user, the explicit window — is recorded here rather than at any call site, so no
trigger can produce an artifact that isn't accounted for.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.reports.registry import (
    RANGE_DAYS,
    ReportDef,
    ReportError,
    ReportResult,
    get_report,
    resolve_range,
)
from app.core.reports.render import render_csv, render_pdf
from app.models.auth import User
from app.models.report_run import ReportRun

# Report ids and formats are registry-controlled, but the filename is built from
# them, so it is whitelisted anyway — a filename is a path, and a path built
# from anything but a whitelist is how directory traversal starts.
_SAFE_COMPONENT = re.compile(r"^[a-z0-9_]+$")


def reports_dir() -> Path:
    """The artifact directory, created on first use."""
    path = Path(get_settings().reports_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _window(report: ReportDef, params: dict[str, Any], *, now: datetime):
    """The explicit [start, end] a range-scoped report covers, else None."""
    if any(spec.name == RANGE_DAYS.name for spec in report.params):
        return resolve_range(params, now=now)
    return None


def _generated_by(db: Session, user_id: int | None) -> str:
    if user_id is None:
        return "scheduled (platform)"
    user = db.get(User, user_id)
    return user.email if user else f"user {user_id} (deleted)"


def generate(
    db: Session,
    *,
    report_id: str,
    params: dict[str, Any] | None,
    fmt: str,
    requested_by: int | None,
    job_id: int | None = None,
    now: datetime | None = None,
) -> ReportRun:
    """Run a report end to end and return its completed `ReportRun`.

    A generator or renderer failure is recorded on the row as `failure` with the
    message, then re-raised: a report that broke must still be visible in the
    run history rather than vanishing, but the caller (and the job) must still
    see it fail.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if fmt not in ("csv", "pdf"):
        raise ReportError(f"unsupported format {fmt!r}")

    report = get_report(report_id)
    resolved = report.coerce_params(params)

    run = ReportRun(
        report_id=report.id,
        params=dict(resolved),
        format=fmt,
        status="running",
        requested_by=requested_by,
        job_id=job_id,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        result = report.generator(db, resolved, now=now)
        payload = render_bytes(
            report,
            result,
            fmt=fmt,
            generated_at=now,
            generated_by=_generated_by(db, requested_by),
            params=resolved,
            window=_window(report, resolved, now=now),
        )
        path = _write_artifact(run, payload, now=now)
        run.artifact_path = str(path)
        run.artifact_bytes = len(payload)
        run.sha256 = hashlib.sha256(payload).hexdigest()
        run.row_count = result.row_count
        run.status = "success"
    except Exception as exc:
        run.status = "failure"
        run.error = f"{type(exc).__name__}: {exc}"
        run.completed_at = datetime.now(UTC)
        db.commit()
        raise
    run.completed_at = datetime.now(UTC)
    db.commit()
    db.refresh(run)
    return run


def render_bytes(
    report: ReportDef,
    result: ReportResult,
    *,
    fmt: str,
    generated_at: datetime,
    generated_by: str,
    params: dict[str, Any],
    window,
) -> bytes:
    """Render a result to the artifact bytes for `fmt`."""
    if fmt == "pdf":
        return render_pdf(
            report,
            result,
            generated_at=generated_at,
            generated_by=generated_by,
            params=params,
            window=window,
        )
    chunks = render_csv(
        report,
        result,
        generated_at=generated_at,
        generated_by=generated_by,
        params=params,
        window=window,
    )
    return "".join(chunks).encode("utf-8")


def _write_artifact(run: ReportRun, payload: bytes, *, now: datetime) -> Path:
    if not _SAFE_COMPONENT.match(run.report_id) or not _SAFE_COMPONENT.match(run.format):
        raise ReportError("report id and format must be simple identifiers")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = reports_dir() / f"{run.report_id}-{stamp}-{run.id}.{run.format}"
    path.write_bytes(payload)
    return path


def artifact_path(run: ReportRun) -> Path:
    """The artifact's path, verified to still be inside the reports directory.

    Re-resolving against the configured root means a stored path can never walk
    out of it, even if the row were tampered with.
    """
    if not run.artifact_path:
        raise ReportError("this run has no artifact")
    root = reports_dir().resolve()
    path = Path(run.artifact_path).resolve()
    if not path.is_relative_to(root):
        raise ReportError("artifact path escapes the reports directory")
    if not path.exists():
        raise ReportError("artifact has been pruned by retention")
    return path


def filename_for(run: ReportRun) -> str:
    """The download filename a browser should save the artifact under."""
    created = run.created_at or datetime.now(UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return f"{run.report_id}-{created.strftime('%Y%m%d')}-{run.id}.{run.format}"


def prune(db: Session, *, now: datetime | None = None, keep_days: int | None = None) -> dict:
    """Delete artifacts older than the retention window and clear their paths.

    The `ReportRun` rows themselves are kept — the audit fact that a report was
    generated, by whom and over what range outlives the (re-generatable) file.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    days = keep_days if keep_days is not None else get_settings().reports_retention_days
    cutoff = now - timedelta(days=days)

    removed = freed = 0
    stale = db.scalars(
        select(ReportRun).where(
            ReportRun.artifact_path.is_not(None), ReportRun.created_at < cutoff
        )
    ).all()
    for run in stale:
        try:
            path = Path(run.artifact_path)
            if path.exists():
                freed += path.stat().st_size
                path.unlink()
        except OSError:
            # A missing/unlinkable artifact must not stall the sweep; the row is
            # still cleared so it stops advertising a file that isn't there.
            pass
        run.artifact_path = None
        removed += 1
    db.commit()
    return {"removed": removed, "freed_bytes": freed, "cutoff": cutoff.isoformat()}
