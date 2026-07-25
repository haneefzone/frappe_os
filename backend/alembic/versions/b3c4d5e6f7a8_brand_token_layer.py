"""brand token layer (session 6.6)

Revision ID: b3c4d5e6f7a8
Revises: f8b3d1c6a2e9
Create Date: 2026-07-19 00:00:00

Adds the extended brand columns to platform_settings:
- logo_dark_path   — dark-theme logo variant URL
- favicon_path     — favicon URL
- accent_hex       — optional brand accent colour (#RRGGBB / #RGB)
- support_link     — optional operator support/contact URL
- footer_line      — optional one-line sidebar footer text

Status colours (ok/warn/err/info) are NOT configurable (uiux-spec B1.5).
All columns are nullable so the row auto-migrates with NULL defaults.
"""

from alembic import op
import sqlalchemy as sa

revision = "b3c4d5e6f7a8"
down_revision = "f8b3d1c6a2e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add brand token columns to platform_settings."""
    op.add_column("platform_settings", sa.Column("logo_dark_path", sa.String(300), nullable=True))
    op.add_column("platform_settings", sa.Column("favicon_path", sa.String(300), nullable=True))
    op.add_column("platform_settings", sa.Column("accent_hex", sa.String(7), nullable=True))
    op.add_column("platform_settings", sa.Column("support_link", sa.String(500), nullable=True))
    op.add_column("platform_settings", sa.Column("footer_line", sa.String(200), nullable=True))


def downgrade() -> None:
    """Remove brand token columns from platform_settings."""
    op.drop_column("platform_settings", "footer_line")
    op.drop_column("platform_settings", "support_link")
    op.drop_column("platform_settings", "accent_hex")
    op.drop_column("platform_settings", "favicon_path")
    op.drop_column("platform_settings", "logo_dark_path")
