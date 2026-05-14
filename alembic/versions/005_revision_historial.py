"""Add revision fields to aceptaciones and create aceptaciones_historial table.

- aceptaciones: estado_revision, revisado_por, fecha_revision, motivo_rechazo
- aceptaciones_historial: registro de auditoría de todos los cambios

Revision ID: 005
Revises: 004
Create Date: 2026-05-14
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE aceptaciones ADD COLUMN IF NOT EXISTS estado_revision TEXT;")
    op.execute("ALTER TABLE aceptaciones ADD COLUMN IF NOT EXISTS revisado_por TEXT;")
    op.execute("ALTER TABLE aceptaciones ADD COLUMN IF NOT EXISTS fecha_revision TEXT;")
    op.execute("ALTER TABLE aceptaciones ADD COLUMN IF NOT EXISTS motivo_rechazo TEXT;")

    op.execute("""
        CREATE TABLE IF NOT EXISTS aceptaciones_historial (
            id            SERIAL PRIMARY KEY,
            aceptacion_id INTEGER NOT NULL,
            evento_id     INTEGER NOT NULL,
            accion        TEXT NOT NULL,
            realizado_por TEXT NOT NULL,
            fecha         TEXT NOT NULL,
            detalle       TEXT
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_historial_aceptacion
        ON aceptaciones_historial(aceptacion_id);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS aceptaciones_historial;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS motivo_rechazo;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS fecha_revision;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS revisado_por;")
    op.execute("ALTER TABLE aceptaciones DROP COLUMN IF EXISTS estado_revision;")
