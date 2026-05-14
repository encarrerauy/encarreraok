"""Add email field to aceptaciones.

Revision ID: 006
Revises: 005
Create Date: 2026-05-14
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE aceptaciones ADD COLUMN IF NOT EXISTS email TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS email;")
