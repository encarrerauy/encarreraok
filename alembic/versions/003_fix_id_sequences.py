"""Fix id columns: add SERIAL sequences to eventos, deslindes, aceptaciones.

In PostgreSQL, INTEGER PRIMARY KEY does NOT auto-increment without a sequence.
This migration creates sequences and sets them as the default for each id column,
so that INSERT statements that omit the id field get an auto-generated value.

Revision ID: 003
Revises: 002
Create Date: 2026-03-18
"""

from alembic import op

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # eventos
    # -----------------------------------------------------------------------
    op.execute("""
        CREATE SEQUENCE IF NOT EXISTS eventos_id_seq;
    """)
    op.execute("""
        ALTER TABLE eventos ALTER COLUMN id SET DEFAULT nextval('eventos_id_seq');
    """)
    op.execute("""
        SELECT setval('eventos_id_seq', GREATEST(COALESCE(MAX(id), 0), 1), true) FROM eventos;
    """)
    op.execute("""
        ALTER SEQUENCE eventos_id_seq OWNED BY eventos.id;
    """)

    # -----------------------------------------------------------------------
    # deslindes
    # -----------------------------------------------------------------------
    op.execute("""
        CREATE SEQUENCE IF NOT EXISTS deslindes_id_seq;
    """)
    op.execute("""
        ALTER TABLE deslindes ALTER COLUMN id SET DEFAULT nextval('deslindes_id_seq');
    """)
    op.execute("""
        SELECT setval('deslindes_id_seq', GREATEST(COALESCE(MAX(id), 0), 1), true) FROM deslindes;
    """)
    op.execute("""
        ALTER SEQUENCE deslindes_id_seq OWNED BY deslindes.id;
    """)

    # -----------------------------------------------------------------------
    # aceptaciones
    # -----------------------------------------------------------------------
    op.execute("""
        CREATE SEQUENCE IF NOT EXISTS aceptaciones_id_seq;
    """)
    op.execute("""
        ALTER TABLE aceptaciones ALTER COLUMN id SET DEFAULT nextval('aceptaciones_id_seq');
    """)
    op.execute("""
        SELECT setval('aceptaciones_id_seq', GREATEST(COALESCE(MAX(id), 0), 1), true) FROM aceptaciones;
    """)
    op.execute("""
        ALTER SEQUENCE aceptaciones_id_seq OWNED BY aceptaciones.id;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE aceptaciones ALTER COLUMN id DROP DEFAULT;")
    op.execute("DROP SEQUENCE IF EXISTS aceptaciones_id_seq;")

    op.execute("ALTER TABLE deslindes ALTER COLUMN id DROP DEFAULT;")
    op.execute("DROP SEQUENCE IF EXISTS deslindes_id_seq;")

    op.execute("ALTER TABLE eventos ALTER COLUMN id DROP DEFAULT;")
    op.execute("DROP SEQUENCE IF EXISTS eventos_id_seq;")
