"""Pydantic schemas for the config-drift API (session 6.7)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DriftBaselineOut(BaseModel):
    """One tracked artefact's baseline + drift state (list/detail row)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    bench_id: int | None
    site_id: int | None
    artifact_key: str
    path: str
    status: str
    sha256: str | None
    size: int
    current_sha256: str | None
    captured_at: datetime
    captured_by_job_id: int | None
    last_checked_at: datetime | None
    drift_detected_at: datetime | None


class DriftDiffOut(BaseModel):
    """The drift-detail drawer payload for one artefact.

    `unified_diff` is the sanitised baseline↔current diff for text artefacts
    (secrets already masked as ``••••``); it is empty and `hash_only` is true for
    artefacts whose content is never stored (unparseable/secret-bearing)."""

    baseline: DriftBaselineOut
    hash_only: bool
    unified_diff: str


class AcceptBaselineRequest(BaseModel):
    """Adopt the current (drifted) state as the new baseline. Requires a reason
    for the audit trail (golden rule 2)."""

    reason: str = Field(min_length=1, max_length=500)


class DriftSummaryOut(BaseModel):
    """Fleet drift counters for the Dashboard "Needs attention" row."""

    drifted_count: int
    server_ids: list[int]
