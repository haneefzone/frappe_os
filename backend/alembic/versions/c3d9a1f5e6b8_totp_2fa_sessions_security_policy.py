"""TOTP 2FA, recovery codes, session enumeration, login attempts, security policy (session 6.5)

Adds five tables:
- `user_totp`       — one per user; Fernet-encrypted secret, confirmed_at, replay-guard step
- `recovery_codes`  — ten single-use hashed codes minted at confirm time
- `user_sessions`   — one row per login/refresh chain (the `sid` JWT claim), revocable
- `login_attempts`  — persisted password + MFA-code attempt log
- `security_policy` — one-row (id=1) Admin-editable policy

Revision ID: c3d9a1f5e6b8
Revises: e2f4a6c8d1b3
Create Date: 2026-07-24 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d9a1f5e6b8'
down_revision: str | Sequence[str] | None = 'e2f4a6c8d1b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON on the SQLite fallback (mirrors the models).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'user_totp',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('secret_encrypted', sa.String(length=500), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_step', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_user_totp_user_id'), 'user_totp', ['user_id'], unique=True
    )

    op.create_table(
        'recovery_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code_hash'),
    )
    op.create_index(
        op.f('ix_recovery_codes_user_id'), 'recovery_codes', ['user_id'], unique=False
    )

    op.create_table(
        'user_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('jti', sa.String(length=64), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            'last_seen_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=300), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('jti'),
    )
    op.create_index(
        op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'], unique=False
    )
    op.create_index(
        op.f('ix_user_sessions_jti'), 'user_sessions', ['jti'], unique=True
    )

    op.create_table(
        'login_attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=300), nullable=True),
        sa.Column('success', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.String(length=40), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_login_attempts_email'), 'login_attempts', ['email'], unique=False
    )

    op.create_table(
        'security_policy',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enforce_2fa_roles', _JSON, nullable=True),
        sa.Column('password_min_length', sa.Integer(), nullable=False),
        sa.Column('password_require_complexity', sa.Boolean(), nullable=False),
        sa.Column('password_reuse_history', sa.Integer(), nullable=False),
        sa.Column('ip_allowlist', _JSON, nullable=True),
        sa.Column('session_idle_timeout_minutes', sa.Integer(), nullable=False),
        sa.Column('session_absolute_timeout_minutes', sa.Integer(), nullable=False),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('security_policy')

    op.drop_index(op.f('ix_login_attempts_email'), table_name='login_attempts')
    op.drop_table('login_attempts')

    op.drop_index(op.f('ix_user_sessions_jti'), table_name='user_sessions')
    op.drop_index(op.f('ix_user_sessions_user_id'), table_name='user_sessions')
    op.drop_table('user_sessions')

    op.drop_index(op.f('ix_recovery_codes_user_id'), table_name='recovery_codes')
    op.drop_table('recovery_codes')

    op.drop_index(op.f('ix_user_totp_user_id'), table_name='user_totp')
    op.drop_table('user_totp')
