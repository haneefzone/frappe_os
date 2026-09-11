"""merge the diverged local and origin/main migration lines (DOO-1119)

Local `main` and `origin/main` diverged (merge-base ``d961d96``): the local line
carried FDM 3.2/3.4/3.5/4.x/6.x + the M4 fixes (head ``e1a2b3c4d5e6``), while
``origin/main`` carried FDM 5.1/5.2 + job-analyses + the canonical FDM 4.2 restic
(head ``d4e2f7a9c6b1``, merged via PR #1). This migration merges the two heads
into a single line so ``alembic heads`` reports exactly one head.

Reversible by design — both ``upgrade`` and ``downgrade`` are no-ops; ``downgrade``
re-forks into the two heads exactly as before. The local line's own FDM 4.2 restic
migration (``b7e2d9f4c1a8``) was neutralised in the same reconcile so only origin's
canonical restic columns are created (no duplicate-column collision).

Revision ID: c0ffee1119ab
Revises: e1a2b3c4d5e6, d4e2f7a9c6b1
Create Date: 2026-09-11 00:00:00.000000

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c0ffee1119ab"
down_revision: str | Sequence[str] | None = ("e1a2b3c4d5e6", "d4e2f7a9c6b1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: this is a head-merge only; no schema change."""


def downgrade() -> None:
    """No-op: re-forks into the two pre-merge heads."""
