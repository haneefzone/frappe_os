"""Config drift baselines (session 6.7, uiux-spec A2.16).

The platform *manages* a small, explicit set of on-disk config artefacts (nginx,
supervisor, the platform sudoers drop-in, each bench's `common_site_config.json`
and each site's `site_config.json`). A `ConfigBaseline` row is the hash of one
such artefact captured the moment a **managed** job last wrote it — so a manual,
out-of-band edit later shows up as a hash mismatch (drift) while a managed change
simply moves the baseline forward (`app.core.drift`).

Security (golden rule 6): the hash is computed over a **secret-stripped,
canonicalised** copy of the artefact — never the raw file. `sanitized_content`
holds that same secret-free canonical text so "View drift" can show a unified
diff without a secret value ever reaching the DB, a log, the diff, or a
notification. A `site_config.json`'s `encryption_key` / db password / API keys
are masked to ``••••`` *before* hashing, so they can never leak here.

`status`:
- ``baseline`` — current on-disk state matches the last managed capture (clean).
- ``drifted``  — the last `server.drift_check` found a mismatch (or a tracked
  artefact went missing, or the elevation drop-in reappeared). `current_sha256`
  / `current_content` snapshot what was found so the drawer can diff without a
  second live read; `drift_detected_at` stamps the edge.
- ``accepted`` — an Admin reviewed the drift and adopted the new state as the
  baseline (audited, with a reason). Functionally clean again, but distinguished
  from ``baseline`` so the UI can show "manually accepted".
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# The lifecycle states a baseline row moves through (see module docstring).
BASELINE_STATUSES = ("baseline", "drifted", "accepted")


class ConfigBaseline(Base):
    """One tracked config artefact's last-known managed hash + drift state."""

    __tablename__ = "config_baselines"
    __table_args__ = (
        # One row per (server, artefact, resolved path): re-hashing an artefact
        # upserts this row rather than appending history.
        UniqueConstraint(
            "server_id", "artifact_key", "path", name="uq_config_baselines_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # The server the artefact lives on. Server-scoped artefacts (nginx, sudoers)
    # leave bench_id/site_id NULL; a bench artefact sets bench_id; a site
    # artefact sets both bench_id and site_id. Cascade so removing a server (and
    # its benches/sites) reaps its baselines rather than stranding them.
    server_id: Mapped[int] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), index=True
    )
    bench_id: Mapped[int | None] = mapped_column(
        ForeignKey("benches.id", ondelete="CASCADE"), index=True
    )
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )

    # A stable identifier for *which* tracked artefact this is (see
    # app.core.drift.ARTIFACTS), e.g. "nginx.conf" or "site_config". Combined
    # with the scope ids it is unique per resolved path.
    artifact_key: Mapped[str] = mapped_column(String(60), index=True)
    # The absolute path (or "<absent> …" sentinel for the elevation drop-in that
    # must NOT exist) the hash was taken from — shown in the UI, never a secret.
    path: Mapped[str] = mapped_column(String(500))

    # sha256 (hex) of the secret-stripped, canonicalised artefact content, and
    # the byte length of that canonical form. NULL sha256 == the artefact was
    # missing when captured (an absent tracked file is itself a fact).
    sha256: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    # The secret-free canonical content, kept so "View drift" can diff without a
    # live re-read. NULL for a missing artefact.
    sanitized_content: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(12), default="baseline", index=True)

    # Snapshot of what the latest drift_check actually found, when status flipped
    # to `drifted` — lets the drawer diff baseline↔current with no second read.
    current_sha256: Mapped[str | None] = mapped_column(String(64))
    current_content: Mapped[str | None] = mapped_column(Text)

    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # The managed job whose write produced this baseline (attribution). SET NULL
    # so a job purge doesn't cascade-delete the baseline it established.
    captured_by_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("command_jobs.id", ondelete="SET NULL")
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    drift_detected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
