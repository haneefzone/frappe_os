"""user token_version: server-side session revocation (SEC-M2)

Adds `users.token_version` (int, NOT NULL, default 0). Embedded as the `tv`
claim in every access/refresh JWT at issue time; get_current_user and /refresh
reject any token whose claim != this value. Bumping it (password/role change,
deactivate, "log out everywhere") revokes all outstanding tokens for the user.
ISO 27001:2022 A.5.17, A.8.5.

Revision ID: b2d4f6a8c1e3
Revises: a7c3e9f10b22
Create Date: 2026-07-10 15:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2d4f6a8c1e3'
down_revision: str | Sequence[str] | None = 'a7c3e9f10b22'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'users',
        sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'token_version')
