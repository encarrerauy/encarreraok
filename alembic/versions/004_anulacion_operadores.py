"""Add anulacion columns to aceptaciones and create operadores table.

- aceptaciones: motivo_anulacion, fecha_anulacion, anulado_por
  (valido column already added in 002)
- operadores: id, username, password_hash, evento_ids, activo, created_at

Revision ID: 004
Revises: 003
Create Date: 2026-03-18
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # aceptaciones: columnas de anulación (valido ya existe desde 002)
    # -----------------------------------------------------------------------
    op.execute("""
        ALTER TABLE aceptaciones
        ADD COLUMN IF NOT EXISTS motivo_anulacion TEXT;
    """)
    op.execute("""
        ALTER TABLE aceptaciones
        ADD COLUMN IF NOT EXISTS fecha_anulacion TEXT;
    """)
    op.execute("""
        ALTER TABLE aceptaciones
        ADD COLUMN IF NOT EXISTS anulado_por TEXT;
    """)

    # -----------------------------------------------------------------------
    # operadores
    # -----------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS operadores (
            id           SERIAL PRIMARY KEY,
            username     TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            evento_ids   TEXT NOT NULL DEFAULT '',
            activo       INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL
        );
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS operadores;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS anulado_por;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS fecha_anulacion;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS motivo_anulacion;")
