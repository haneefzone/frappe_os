"""AlertRule engine — rules, per-server dedup state, firings (session 3.1)

Creates the three tables of the FDM 3.1 AlertRule engine and grants the new
``alert:manage`` action-class to the Developer and Operator roles (Admin already
holds ``*``). Idempotent + reversible (golden rule 8): the permission grant only
appends when absent, so re-running is a no-op, and downgrade drops the tables and
removes the grant.

Revision ID: e2f4a6c8d1b3
Revises: a2b6d4f8c3e1
Create Date: 2026-07-25 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e2f4a6c8d1b3"
down_revision: str | Sequence[str] | None = "a2b6d4f8c3e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# JSONB on Postgres, plain JSON on the SQLite test fallback (mirrors the models).
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

PERMISSION = "alert:manage"
GRANT_ROLES = ("Developer", "Operator")


def _ts_col(name: str):
    """A NOT-NULL timezone-aware timestamp column defaulting to now()."""
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("metric", sa.String(length=20), nullable=False),
        sa.Column("comparator", sa.String(length=2), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("scope", sa.String(length=10), nullable=False, server_default="global"),
        sa.Column("scope_server_id", sa.Integer(), nullable=True),
        sa.Column("cooldown_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("channel_email", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("channel_webhook", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("email_to", sa.String(length=500), nullable=True),
        sa.Column("webhook_url", sa.String(length=500), nullable=True),
        sa.Column("webhook_secret_enc", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), nullable=True),
        _ts_col("created_at"),
        _ts_col("updated_at"),
        sa.ForeignKeyConstraint(["scope_server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_alert_rules_name"), "alert_rules", ["name"], unique=True)
    op.create_index(
        op.f("ix_alert_rules_scope_server_id"), "alert_rules", ["scope_server_id"], unique=False
    )

    op.create_table(
        "alert_rule_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("in_breach", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "server_id", name="uq_alert_rule_state_rule_server"),
    )
    op.create_index(
        op.f("ix_alert_rule_states_rule_id"), "alert_rule_states", ["rule_id"], unique=False
    )
    op.create_index(
        op.f("ix_alert_rule_states_server_id"), "alert_rule_states", ["server_id"], unique=False
    )

    op.create_table(
        "alert_firings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("rule_name", sa.String(length=120), nullable=True),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("server_name", sa.String(length=200), nullable=True),
        sa.Column("metric", sa.String(length=20), nullable=False),
        sa.Column("comparator", sa.String(length=2), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("channels", _JSON, nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        _ts_col("created_at"),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_alert_firings_rule_id"), "alert_firings", ["rule_id"], unique=False)
    op.create_index(
        op.f("ix_alert_firings_server_id"), "alert_firings", ["server_id"], unique=False
    )
    op.create_index(
        op.f("ix_alert_firings_created_at"), "alert_firings", ["created_at"], unique=False
    )

    _grant_permission(op.get_bind())


def downgrade() -> None:
    _revoke_permission(op.get_bind())
    op.drop_index(op.f("ix_alert_firings_created_at"), table_name="alert_firings")
    op.drop_index(op.f("ix_alert_firings_server_id"), table_name="alert_firings")
    op.drop_index(op.f("ix_alert_firings_rule_id"), table_name="alert_firings")
    op.drop_table("alert_firings")
    op.drop_index(op.f("ix_alert_rule_states_server_id"), table_name="alert_rule_states")
    op.drop_index(op.f("ix_alert_rule_states_rule_id"), table_name="alert_rule_states")
    op.drop_table("alert_rule_states")
    op.drop_index(op.f("ix_alert_rules_scope_server_id"), table_name="alert_rules")
    op.drop_index(op.f("ix_alert_rules_name"), table_name="alert_rules")
    op.drop_table("alert_rules")


def _grant_permission(conn) -> None:
    for role in GRANT_ROLES:
        row = conn.execute(
            sa.text("SELECT id, permissions FROM roles WHERE name = :n"), {"n": role}
        ).fetchone()
        if row is None:
            continue
        perms = list(row.permissions or [])
        if PERMISSION not in perms and "*" not in perms:
            perms.append(PERMISSION)
            conn.execute(
                sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
                {"p": sa.JSON().bind_processor(conn.dialect)(perms), "id": row.id},
            )


def _revoke_permission(conn) -> None:
    for role in GRANT_ROLES:
        row = conn.execute(
            sa.text("SELECT id, permissions FROM roles WHERE name = :n"), {"n": role}
        ).fetchone()
        if row is None:
            continue
        perms = list(row.permissions or [])
        if PERMISSION in perms:
            perms = [p for p in perms if p != PERMISSION]
            conn.execute(
                sa.text("UPDATE roles SET permissions = :p WHERE id = :id"),
                {"p": sa.JSON().bind_processor(conn.dialect)(perms), "id": row.id},
            )
