"""Reports suite (session 6.2, uiux-spec B4.16).

Importing this package registers the seven prebuilt reports; importing
`registry` alone gives you an empty catalogue, so every consumer (API, worker,
scheduler) imports from here.
"""

from app.core.reports import generators as _generators  # noqa: F401 — registers reports
from app.core.reports.registry import (
    ReportDef,
    ReportError,
    ReportParam,
    ReportResult,
    all_reports,
    build,
    get_report,
    resolve_range,
    visible_reports,
)
from app.core.reports.render import render_csv, render_pdf
from app.core.reports.runs import (
    artifact_path,
    filename_for,
    generate,
    prune,
    render_bytes,
    reports_dir,
)

__all__ = [
    "ReportDef",
    "ReportError",
    "ReportParam",
    "ReportResult",
    "all_reports",
    "artifact_path",
    "build",
    "filename_for",
    "generate",
    "get_report",
    "prune",
    "render_bytes",
    "render_csv",
    "render_pdf",
    "reports_dir",
    "resolve_range",
    "visible_reports",
]
