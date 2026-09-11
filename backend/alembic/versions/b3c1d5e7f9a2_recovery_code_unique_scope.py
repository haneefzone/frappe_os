"""Scope recovery_codes uniqueness per user (DOO-418)

Replaces the *global* unique on `recovery_codes.code_hash` with a composite
unique on `(user_id, code_hash)`. A cross-user SHA-256 collision is
astronomically unlikely, but the global constraint would turn one — if it ever
occurred — into an IntegrityError that fails the victim's enrolment. Per-user
scope still forbids the only case that matters (the same code minted twice for
one user).

Revision ID: b3c1d5e7f9a2
Revises: a9b2c3d4e5f6
Create Date: 2026-09-11 04:10:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b3c1d5e7f9a2'
down_revision: str | Sequence[str] | None = 'a9b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Auto-generated name Postgres gave the original unnamed UniqueConstraint in
# c3d9a1f5e6b8 (`<table>_<col>_key`).
_OLD_UNIQUE = "recovery_codes_code_hash_key"
_NEW_UNIQUE = "uq_recovery_codes_user_code"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(_OLD_UNIQUE, "recovery_codes", type_="unique")
    op.create_unique_constraint(
        _NEW_UNIQUE, "recovery_codes", ["user_id", "code_hash"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(_NEW_UNIQUE, "recovery_codes", type_="unique")
    op.create_unique_constraint(_OLD_UNIQUE, "recovery_codes", ["code_hash"])
