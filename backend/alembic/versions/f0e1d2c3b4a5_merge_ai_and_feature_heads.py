"""merge the 5.x AI head with the 2.6->6.7 feature head (DOO-1065)

Repository-integrity recovery. The 5.1/5.2 AI landing (DOO-166/DOO-167) and the
2.6->6.7 feature line both branched from ``f8b3d1c6a2e9`` (backup_compliance),
leaving two alembic heads once the dropped feature line was merged back into
``main``:

* ``f7a2c9e1b3d5`` — ai_agents_module (5.x AI line)
* ``e2f4a6c8d1b3`` — alert_rule_engine (2.6->6.7 line)

This is a pure DAG merge: no schema change. It gives ``main`` a single alembic
head again so ``alembic upgrade head`` is unambiguous and downstream work
(DOO-1020 / 4.2) has one revision to chain onto. Reversible by design — both
``upgrade`` and ``downgrade`` are no-ops; ``downgrade`` re-forks into the two
heads exactly as before.

Revision ID: f0e1d2c3b4a5
Revises: f7a2c9e1b3d5, e2f4a6c8d1b3
Create Date: 2026-09-11 00:00:00.000000

"""
from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f0e1d2c3b4a5"
down_revision: str | Sequence[str] | None = ("f7a2c9e1b3d5", "e2f4a6c8d1b3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: pure merge of two alembic heads (no schema change)."""


def downgrade() -> None:
    """No-op: re-forks into the two prior heads (f7a2c9e1b3d5, e2f4a6c8d1b3)."""
