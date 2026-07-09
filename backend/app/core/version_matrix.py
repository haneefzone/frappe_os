"""The Frappe version matrix (CLAUDE.md "Frappe domain knowledge" — it is the
product). One source of truth for the pre-flight checks *and* the Create-Bench
wizard: the same matrix drives both the server-side gate and the UI's radio
cards, so they can never disagree.

| Frappe | Python    | Node  | MariaDB          | Bench tooling      |
|--------|-----------|-------|------------------|--------------------|
| v14    | 3.10      | 16-18 | 10.6+            | pip/virtualenv era |
| v15    | 3.11-3.12 | 18-20 | 10.6+            | pipx + uv hybrid   |
| v16    | 3.14      | 24    | 10.6+ (11.8 rec) | uv-based           |
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MatrixEntry:
    """One row of the matrix. `major` is the Frappe major ("14"/"15"/"16") — the
    value the API and wizard pass around; `branch` is what `bench init
    --frappe-branch` wants."""

    major: str
    branch: str
    python_versions: tuple[str, ...]
    node_min_major: int
    node_max_major: int
    mariadb_min: str
    mariadb_recommended: str | None
    tooling: str

    # -- display helpers (shared by the API response and, via it, the UI) ---- #

    @property
    def python_display(self) -> str:
        lo, hi = self.python_versions[0], self.python_versions[-1]
        return lo if lo == hi else f"{lo}–{hi}"

    @property
    def node_display(self) -> str:
        if self.node_min_major == self.node_max_major:
            return str(self.node_min_major)
        return f"{self.node_min_major}–{self.node_max_major}"

    @property
    def mariadb_display(self) -> str:
        return self.mariadb_recommended or f"{self.mariadb_min}+"

    @property
    def line(self) -> str:
        """The matrix line the wizard prints under a selected version, e.g.
        "Python 3.14 · Node 24 · MariaDB 11.8"."""
        return (
            f"Python {self.python_display} · Node {self.node_display} "
            f"· MariaDB {self.mariadb_display}"
        )


# Ordered newest-first (the wizard lists v16 at the top).
MATRIX: tuple[MatrixEntry, ...] = (
    MatrixEntry(
        major="16",
        branch="version-16",
        python_versions=("3.14",),
        node_min_major=24,
        node_max_major=24,
        mariadb_min="10.6",
        mariadb_recommended="11.8",
        tooling="uv-based",
    ),
    MatrixEntry(
        major="15",
        branch="version-15",
        python_versions=("3.11", "3.12"),
        node_min_major=18,
        node_max_major=20,
        mariadb_min="10.6",
        mariadb_recommended=None,
        tooling="pipx + uv hybrid",
    ),
    MatrixEntry(
        major="14",
        branch="version-14",
        python_versions=("3.10",),
        node_min_major=16,
        node_max_major=18,
        mariadb_min="10.6",
        mariadb_recommended=None,
        tooling="pip/virtualenv era",
    ),
)

_BY_MAJOR = {entry.major: entry for entry in MATRIX}

# The valid values of the `frappe_version` parameter — used by the command
# templates' enum validator and the API request schema.
SUPPORTED_MAJORS: tuple[str, ...] = tuple(entry.major for entry in MATRIX)
SUPPORTED_BRANCHES: tuple[str, ...] = tuple(entry.branch for entry in MATRIX)


class UnknownVersion(KeyError):
    """The requested Frappe major isn't in the matrix."""


def get_entry(major: str) -> MatrixEntry:
    try:
        return _BY_MAJOR[major]
    except KeyError as exc:
        raise UnknownVersion(major) from exc


def branch_for(major: str) -> str:
    return get_entry(major).branch
