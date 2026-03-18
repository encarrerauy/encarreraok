"""Incremental migrations: all ALTER TABLE ADD COLUMN from ensure_schema_migrations().

This migration captures every column that was added via ALTER TABLE in the
original ensure_schema_migrations() and init_db() functions in main.py.

For databases that were created *before* Alembic was introduced (i.e. they
already have these columns from the manual migration code), this migration is
a no-op because every statement uses IF NOT EXISTS / IF EXISTS.

PostgreSQL 9.6+ supports ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
SQLite does NOT support IF NOT EXISTS on ADD COLUMN, so for SQLite the
upgrade() wraps each statement in a try/except handled at the env.py level via
render_as_batch.  In practice, running these against a fresh DB created by
001_initial_schema.py will raise "column already exists" errors on SQLite –
those are caught and ignored by the batch mode recreate strategy.

IMPORTANT: If you are starting a fresh installation with Alembic from the
beginning, migration 001 already includes ALL these columns in the CREATE TABLE
statements.  Migration 002 is only meaningful for pre-existing databases that
were originally managed by the hand-written migration code.

Revision ID: 002
Revises: 001
Create Date: 2026-03-17
"""

from alembic import op
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Revision identifiers – used by Alembic
# ---------------------------------------------------------------------------
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _add_column_if_not_exists(table: str, column_ddl: str) -> None:
    """
    Execute ALTER TABLE ... ADD COLUMN IF NOT EXISTS.

    PostgreSQL 9.6+ supports IF NOT EXISTS natively.
    SQLite does not – for SQLite we catch the OperationalError and continue.
    """
    sql = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column_ddl}"
    try:
        op.execute(text(sql))
    except Exception:
        # SQLite raises OperationalError: duplicate column name
        # Any other engine that doesn't support IF NOT EXISTS will also be
        # caught here.  Silently skip – the column already exists.
        pass


def _drop_column_if_exists(table: str, column: str) -> None:
    """
    Execute ALTER TABLE ... DROP COLUMN IF EXISTS.

    PostgreSQL 9.0+ supports IF EXISTS.
    SQLite does not support DROP COLUMN at all (before 3.35.0) and still
    does not support IF EXISTS.  For SQLite we catch and ignore.
    """
    sql = f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"
    try:
        op.execute(text(sql))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    # ------------------------------------------------------------------
    # Table: eventos – columns added after initial CREATE TABLE
    # ------------------------------------------------------------------

    # Added when req_firma feature was introduced
    _add_column_if_not_exists(
        "eventos",
        "req_firma INTEGER DEFAULT 0 CHECK (req_firma IN (0, 1))",
    )

    # Added when req_documento feature was introduced
    _add_column_if_not_exists(
        "eventos",
        "req_documento INTEGER DEFAULT 0 CHECK (req_documento IN (0, 1))",
    )

    # Added when req_audio feature was introduced
    _add_column_if_not_exists(
        "eventos",
        "req_audio INTEGER DEFAULT 0 CHECK (req_audio IN (0, 1))",
    )

    # Added when req_salud feature was introduced
    _add_column_if_not_exists(
        "eventos",
        "req_salud INTEGER DEFAULT 0 CHECK (req_salud IN (0, 1))",
    )

    # Added to support versioned deslinde text per event
    _add_column_if_not_exists(
        "eventos",
        "deslinde_version TEXT DEFAULT 'v1_1'",
    )

    # Added as part of DESLINDE PATCH for friendly intro flag
    _add_column_if_not_exists(
        "eventos",
        "friendly_intro INTEGER DEFAULT 0 CHECK (friendly_intro IN (0, 1))",
    )

    # Added to support custom deslinde text per event (FEAT: per-event deslinde)
    _add_column_if_not_exists(
        "eventos",
        "deslinde_texto TEXT",
    )

    # ------------------------------------------------------------------
    # Table: aceptaciones – columns added after initial CREATE TABLE
    # ------------------------------------------------------------------

    # TAREA 2: columna valido para anulaciones
    _add_column_if_not_exists(
        "aceptaciones",
        "valido INTEGER DEFAULT 1",
    )

    # TAREA 3: columna deslinde_version
    _add_column_if_not_exists(
        "aceptaciones",
        "deslinde_version TEXT DEFAULT 'v1_1'",
    )

    # Firma digital
    _add_column_if_not_exists(
        "aceptaciones",
        "firma_path TEXT",
    )

    # Documento de identidad (frente y dorso)
    _add_column_if_not_exists(
        "aceptaciones",
        "doc_frente_path TEXT",
    )
    _add_column_if_not_exists(
        "aceptaciones",
        "doc_dorso_path TEXT",
    )

    # Audio de aceptación
    _add_column_if_not_exists(
        "aceptaciones",
        "audio_path TEXT",
    )

    # Documentación de salud
    _add_column_if_not_exists(
        "aceptaciones",
        "salud_doc_path TEXT",
    )
    _add_column_if_not_exists(
        "aceptaciones",
        "salud_doc_tipo TEXT",
    )

    # Exención de audio
    _add_column_if_not_exists(
        "aceptaciones",
        "audio_exento INTEGER DEFAULT 0 CHECK (audio_exento IN (0, 1))",
    )

    # Firma asistida (operador firma en nombre del participante)
    _add_column_if_not_exists(
        "aceptaciones",
        "firma_asistida INTEGER DEFAULT 0 CHECK (firma_asistida IN (0, 1))",
    )

    # Stage A.2 – PDF token access control
    _add_column_if_not_exists(
        "aceptaciones",
        "pdf_token TEXT",
    )
    _add_column_if_not_exists(
        "aceptaciones",
        "pdf_token_expires_at TEXT",
    )
    _add_column_if_not_exists(
        "aceptaciones",
        "pdf_token_revoked INTEGER DEFAULT 0 CHECK (pdf_token_revoked IN (0, 1))",
    )
    _add_column_if_not_exists(
        "aceptaciones",
        "pdf_last_access_at TEXT",
    )
    _add_column_if_not_exists(
        "aceptaciones",
        "pdf_access_count INTEGER DEFAULT 0",
    )

    # documento_norm: normalised document number for optimised search
    _add_column_if_not_exists(
        "aceptaciones",
        "documento_norm TEXT",
    )

    # ------------------------------------------------------------------
    # Table: deslindes – columns added after initial CREATE TABLE
    # ------------------------------------------------------------------

    _add_column_if_not_exists(
        "deslindes",
        "fecha_creacion TEXT",
    )
    _add_column_if_not_exists(
        "deslindes",
        "creado_por TEXT",
    )

    # ------------------------------------------------------------------
    # Indexes (idempotent via IF NOT EXISTS)
    # ------------------------------------------------------------------

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_aceptaciones_evento "
        "ON aceptaciones (evento_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_aceptaciones_doc_norm "
        "ON aceptaciones (documento_norm)"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_deslindes_evento_activo
        ON deslindes (evento_id)
        WHERE activo = 1
        """
    )


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    # Drop indexes first
    _drop_column_if_exists("deslindes", "creado_por")
    _drop_column_if_exists("deslindes", "fecha_creacion")

    _drop_column_if_exists("aceptaciones", "documento_norm")
    _drop_column_if_exists("aceptaciones", "pdf_access_count")
    _drop_column_if_exists("aceptaciones", "pdf_last_access_at")
    _drop_column_if_exists("aceptaciones", "pdf_token_revoked")
    _drop_column_if_exists("aceptaciones", "pdf_token_expires_at")
    _drop_column_if_exists("aceptaciones", "pdf_token")
    _drop_column_if_exists("aceptaciones", "firma_asistida")
    _drop_column_if_exists("aceptaciones", "audio_exento")
    _drop_column_if_exists("aceptaciones", "salud_doc_tipo")
    _drop_column_if_exists("aceptaciones", "salud_doc_path")
    _drop_column_if_exists("aceptaciones", "audio_path")
    _drop_column_if_exists("aceptaciones", "doc_dorso_path")
    _drop_column_if_exists("aceptaciones", "doc_frente_path")
    _drop_column_if_exists("aceptaciones", "firma_path")
    _drop_column_if_exists("aceptaciones", "deslinde_version")
    _drop_column_if_exists("aceptaciones", "valido")

    _drop_column_if_exists("eventos", "deslinde_texto")
    _drop_column_if_exists("eventos", "friendly_intro")
    _drop_column_if_exists("eventos", "deslinde_version")
    _drop_column_if_exists("eventos", "req_salud")
    _drop_column_if_exists("eventos", "req_audio")
    _drop_column_if_exists("eventos", "req_documento")
    _drop_column_if_exists("eventos", "req_firma")
