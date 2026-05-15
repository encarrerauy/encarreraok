"""Add recarga token fields to aceptaciones.

Revision ID: 007
Revises: 006
Create Date: 2026-05-14
"""

from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE aceptaciones ADD COLUMN IF NOT EXISTS recarga_token TEXT;")
    op.execute("ALTER TABLE aceptaciones ADD COLUMN IF NOT EXISTS recarga_token_expires_at TEXT;")
    op.execute("ALTER TABLE aceptaciones ADD COLUMN IF NOT EXISTS recarga_token_usado INTEGER DEFAULT 0;")


def downgrade() -> None:
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS recarga_token_usado;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS recarga_token_expires_at;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS recarga_token;")
