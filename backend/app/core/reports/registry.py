"""The report catalogue (session 6.2).

A report is a *declaration* — id, title, who may run it, what parameters it
accepts — plus a pure generator function `(db, params, *, now) -> ReportResult`.
Nothing here computes a new metric: every number a report shows is pulled from
the module that already owns its definition (`app.core.dashboard`,
`app.core.compliance`, `app.core.uptime`), so a report can never drift from the
dashboard that shows the same figure.

Parameters are validated the same way command templates validate theirs: a
declared, typed spec, rejected loudly on a bad value. There is no free-text
parameter anywhere in this catalogue, so no report parameter can reach a query
as anything but a bound value.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.permissions import READ, REPORT_SENSITIVE


class ReportError(ValueError):
    """A bad report id or parameter value. The API maps this to 422/404."""


@dataclass(frozen=True)
class ReportParam:
    """One declared report parameter."""

    name: str
    kind: str  # "int" | "enum" | "date"
    label: str
    required: bool = False
    default: Any = None
    enum: tuple[str, ...] | None = None
    min: int | None = None
    max: int | None = None

    def coerce(self, raw: Any) -> Any:
        if raw is None or raw == "":
            if self.required:
                raise ReportError(f"parameter {self.name!r} is required")
            return self.default
        if self.kind == "int":
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ReportError(f"parameter {self.name!r} must be a whole number") from None
            if self.min is not None and value < self.min:
                raise ReportError(f"parameter {self.name!r} must be >= {self.min}")
            if self.max is not None and value > self.max:
                raise ReportError(f"parameter {self.name!r} must be <= {self.max}")
            return value
        if self.kind == "enum":
            value = str(raw)
            if self.enum is not None and value not in self.enum:
                raise ReportError(
                    f"parameter {self.name!r} must be one of {list(self.enum)}"
                )
            return value
        raise ReportError(f"parameter {self.name!r} has unsupported kind {self.kind!r}")


@dataclass
class ReportResult:
    """A generated report: typed rows plus the summary block rendered above them."""

    columns: list[tuple[str, str]]  # (key, header)
    rows: list[dict[str, Any]]
    summary: list[tuple[str, str]] = field(default_factory=list)  # (label, value)
    # Free-text note rendered under the summary (e.g. what the range means).
    note: str = ""

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class ReportDef:
    """One entry in the catalogue."""

    id: str
    title: str
    description: str
    required_permission: str
    params: tuple[ReportParam, ...]
    generator: Callable[..., ReportResult]
    # ISO-facing evidence export (uiux-spec A1.6): the renderers add the
    # generated-at / generated-by / explicit-range / row-count evidence header
    # so the artifact is self-describing.
    evidence: bool = False

    def coerce_params(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        """Validate and normalise the supplied params. Unknown keys are an error
        rather than silently ignored, so a typo'd range never yields a report
        over the default window that the caller believes is over theirs."""
        supplied = dict(raw or {})
        known = {spec.name for spec in self.params}
        unknown = sorted(set(supplied) - known)
        if unknown:
            raise ReportError(f"unknown parameter(s): {unknown}")
        return {spec.name: spec.coerce(supplied.get(spec.name)) for spec in self.params}


_REPORTS: dict[str, ReportDef] = {}


def register(report: ReportDef) -> ReportDef:
    _REPORTS[report.id] = report
    return report


def get_report(report_id: str) -> ReportDef:
    try:
        return _REPORTS[report_id]
    except KeyError:
        raise ReportError(f"unknown report {report_id!r}") from None


def all_reports() -> list[ReportDef]:
    return sorted(_REPORTS.values(), key=lambda r: r.title)


def visible_reports(permissions: list[str]) -> list[ReportDef]:
    """The catalogue entries this role may run (golden rule 7)."""
    from app.core.permissions import role_allows

    return [r for r in all_reports() if role_allows(permissions, r.required_permission)]


# --------------------------------------------------------------------------- #
# Shared parameter specs
# --------------------------------------------------------------------------- #

# Every range-scoped report uses the same window parameter so "last 30 days"
# means the same thing everywhere. Capped at two years so a report can never
# scan the whole audit table by accident.
RANGE_DAYS = ReportParam(
    name="range_days",
    kind="int",
    label="Date range (days)",
    default=30,
    min=1,
    max=730,
)


def resolve_range(params: dict[str, Any], *, now: datetime) -> tuple[datetime, datetime]:
    """The explicit [start, end] window a range-scoped report covers.

    Returned (and rendered) as real instants rather than "last 30 days" so an
    exported artifact states exactly what it covers (uiux-spec A1.6).
    """
    days = int(params.get("range_days") or RANGE_DAYS.default)
    end = now.astimezone(UTC)
    return end - timedelta(days=days), end


def build(db: Session, report_id: str, params: dict[str, Any] | None, *, now: datetime):
    """Validate params and run the generator. The single entry point every
    caller (API, worker, scheduler) goes through."""
    report = get_report(report_id)
    resolved = report.coerce_params(params)
    return report, resolved, report.generator(db, resolved, now=now)


# Permissions re-exported for the generator module's registrations.
__all__ = [
    "READ",
    "RANGE_DAYS",
    "REPORT_SENSITIVE",
    "ReportDef",
    "ReportError",
    "ReportParam",
    "ReportResult",
    "all_reports",
    "build",
    "get_report",
    "register",
    "resolve_range",
    "visible_reports",
]
