"""`report.generate` — the platform-local report job (session 6.2).

Unlike every other action, this one never reaches a managed server: it queries
the platform database, renders an artifact, and optionally emails it. It still
goes through the JobRunner, so a report run is a real `CommandJob` — locked,
audited, step-tracked and visible in the Jobs list exactly like a backup
(golden rules 2 and 3). What makes that possible is `CommandTemplate.local`,
which tells the runner to hand the action a `LocalExecutor` instead of opening
an SSH connection to a server that doesn't exist.

Recipients are carried as a comma-separated parameter rather than a free-form
string: each address is validated against a conservative pattern before the
template will render, so a recipient list cannot smuggle anything into the
message envelope.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.core.commands.actions import Action, JobContext
from app.core.commands.registry import register
from app.core.commands.templates import CommandTemplate, ParamSpec
from app.core.permissions import REPORT_SENSITIVE

# Deliberately conservative: no display names, no quoted local parts, no commas
# or newlines (header-injection shapes) — an operator address list, nothing more.
EMAIL_RE = r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
RECIPIENTS_RE = rf"{EMAIL_RE}(,{EMAIL_RE})*"

MIME_SUBTYPES = {"csv": "text/csv", "pdf": "application/pdf"}


def parse_recipients(raw: str | None) -> list[str]:
    """Split and validate a recipient list. Raises ValueError on a bad address."""
    if not raw:
        return []
    addresses = [part.strip() for part in raw.split(",") if part.strip()]
    for address in addresses:
        if re.fullmatch(EMAIL_RE, address) is None:
            raise ValueError(f"invalid recipient address: {address!r}")
    return addresses


class ReportGenerateAction(Action):
    """Generate a report artifact and, if recipients were given, email it."""

    async def run(self, ctx: JobContext) -> None:
        from app.core.notifications import send_email_with_attachment
        from app.core.reports import artifact_path, filename_for, generate, get_report

        params = dict(ctx.rendered.params_sanitized)
        report_id = params["report_id"]
        fmt = params.get("format", "csv")
        recipients = parse_recipients(params.get("recipients"))
        requested_by = params.get("requested_by")
        report_params = {}
        if params.get("range_days"):
            report_params["range_days"] = params["range_days"]
        if params.get("within_days"):
            report_params["within_days"] = params["within_days"]
        if params.get("status"):
            report_params["status"] = params["status"]

        report = get_report(report_id)
        run = None

        with ctx.step(f"Generate {report.title} ({fmt.upper()})"):
            run = generate(
                ctx.session,
                report_id=report_id,
                params=report_params,
                fmt=fmt,
                requested_by=int(requested_by) if requested_by else None,
                job_id=ctx.job_id,
                now=datetime.now(UTC),
            )
            await ctx.emit(
                f"report run {run.id}: {run.row_count} rows, "
                f"{run.artifact_bytes} bytes, sha256 {run.sha256}"
            )

        if not recipients:
            return

        with ctx.step(f"Email report to {len(recipients)} recipient(s)"):
            path = artifact_path(run)
            payload = path.read_bytes()
            filename = filename_for(run)
            subject = f"{report.title} — {run.created_at:%Y-%m-%d}"
            body = (
                f"{report.title}\n\n"
                f"{report.description}\n\n"
                f"Rows: {run.row_count}\n"
                f"Generated at: {run.created_at.isoformat(timespec='seconds')}\n"
                f"sha256: {run.sha256}\n\n"
                "Attached is the generated report artifact."
            )
            for address in recipients:
                send_email_with_attachment(
                    address,
                    subject,
                    body,
                    attachments=[(filename, payload, MIME_SUBTYPES[fmt])],
                )
                await ctx.emit(f"emailed {filename} to {address}")


register(
    CommandTemplate(
        action_name="report.generate",
        # Nominal argv: a local template runs no command. The runner never
        # executes it — ReportGenerateAction does all the work in-process.
        argv=("true",),
        cwd=None,
        params=(
            ParamSpec(name="report_id", regex=r"[a-z0-9_]{1,60}"),
            ParamSpec(name="format", enum=("csv", "pdf")),
            ParamSpec(name="recipients", regex=RECIPIENTS_RE, required=False),
            ParamSpec(name="range_days", regex=r"[0-9]{1,4}", required=False),
            ParamSpec(name="within_days", regex=r"[0-9]{1,3}", required=False),
            ParamSpec(
                name="status",
                enum=("all", "pending", "running", "success", "failure", "cancelled"),
                required=False,
            ),
            ParamSpec(name="requested_by", regex=r"[0-9]{1,12}", required=False),
        ),
        action_class=ReportGenerateAction,
        # Re-running a report over the same window is safe and produces a fresh
        # artifact, so a transient failure (SMTP hiccup) may auto-retry.
        idempotent=True,
        # No lock: reports only read, so two concurrent runs cannot corrupt
        # anything, and locking would make a schedule and an operator collide.
        requires_lock=False,
        # This is the permission the *generic* POST /api/jobs endpoint checks,
        # and it cannot know which report_id is being asked for. Setting it to
        # the strictest report's permission means the generic path can never be
        # used to route around a per-report gate; `/api/reports/{id}/run`
        # enforces the actual per-report permission from the catalogue, so a
        # Read-only user still runs the non-sensitive reports there.
        required_permission=REPORT_SENSITIVE,
        local=True,
    )
)
