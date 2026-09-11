"""restic retention policy + integrity-check evidence (session 4.2) — NEUTRALISED

DOO-1119 reconcile note: this was the *local* line's FDM 4.2 restic migration. The
board's `origin/main` shipped its own canonical FDM 4.2 restic migration
(`d4e2f7a9c6b1`, DOO-1023, merged via PR #1) which adds `retention_keep_last/daily/
weekly/monthly`, `last_check_summary`, `last_check_ok` and `last_forget_at`. Both
lines were reconciled onto the origin schema (the ORM models reference only origin's
columns). Since `d4e2f7a9c6b1` already adds `last_check_ok` and `last_forget_at`,
running this migration too would raise a duplicate-column error, and its own
`keep_*` / `check_read_data_subset` / `last_check_message` columns are unreferenced
dead columns under the reconciled models. So this migration is neutralised to a
no-op. It is retained (not deleted) purely as a chain link — `d4e5f6a7b8c9`
(restore-test automation) revises it, and rewriting that history would be riskier
than a no-op.

Revision ID: b7e2d9f4c1a8
Revises: d1c3b5a7e9f2
Create Date: 2026-08-01 03:00:00.000000

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = 'b7e2d9f4c1a8'
down_revision: str | Sequence[str] | None = 'd1c3b5a7e9f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: the restic 4.2 columns are added by origin's `d4e2f7a9c6b1`
    (see the module docstring for the DOO-1119 reconcile rationale)."""


def downgrade() -> None:
    """No-op counterpart to the neutralised upgrade."""
