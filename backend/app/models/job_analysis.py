"""AI root-cause analysis of a failed job (session 5.2 — panel copilot).

`POST /api/jobs/{id}/analyze` creates one `JobAnalysis` row (status `pending`)
and enqueues the AI call as a background job (rule 3 — nothing long runs in the
request). The worker builds a **sanitized** payload (the already-redacted log
tail + template/action name + step statuses), calls the shared 5.0
`AnthropicClient` with the deep model, and folds the structured
root-cause/suggested-fix back onto this row for the panel to poll.

No secret ever lands here: the log tail is drawn from `LogEntry.content` (which
the job engine redacted at write time, rule 6) and the outbound prompt is passed
through `app.core.ai.build_prompt_payload` — the same value-masking gate — with
the job's own secret plaintexts, so even an un-redacted traceback is scrubbed
before it can reach the SDK.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Lifecycle: pending (enqueued) -> running (worker started) -> success | failure.
ANALYSIS_STATUSES = ("pending", "running", "success", "failure")


class JobAnalysis(Base):
    """One AI analysis of a single failed `CommandJob`."""

    __tablename__ = "job_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="CASCADE"), index=True
    )

    # pending | running | success | failure
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)

    # The model the call actually used (echoed back by the SDK). Proves the deep
    # model (default claude-opus-4-8) was used, per the acceptance test.
    model: Mapped[str | None] = mapped_column(String(80))

    # Structured result (via output_config.format). NULL until the call lands.
    root_cause: Mapped[str | None] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(String(400))

    # Secret-free failure surface when status == "failure" (AIError message or a
    # generic fallback — never an SDK traceback, rule 6).
    error: Mapped[str | None] = mapped_column(Text)

    requested_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped["CommandJob"] = relationship()  # noqa: F821
