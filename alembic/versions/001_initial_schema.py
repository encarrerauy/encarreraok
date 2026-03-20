"""Initial schema: eventos, deslindes, aceptaciones and indexes.

This migration captures the full baseline schema extracted from init_db() in
main.py.  All tables and indexes that were originally created inside that
function are represented here as the canonical starting point for Alembic
version control.

SQL is written to be compatible with PostgreSQL 14+.
For SQLite compatibility the env.py sets render_as_batch=True.

Revision ID: 001
Revises:
Create Date: 2026-03-17
"""

from alembic import op

# ---------------------------------------------------------------------------
# Revision identifiers – used by Alembic
# ---------------------------------------------------------------------------
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    # ------------------------------------------------------------------
    # Table: eventos
    # ------------------------------------------------------------------
    # INTEGER PRIMARY KEY is auto-increment on both SQLite and PostgreSQL
    # (in PostgreSQL it maps to a 4-byte integer with implicit sequence).
    # TEXT is used for all string columns – portable across both engines.
    # CHECK constraints are supported by PostgreSQL 14+ and SQLite.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS eventos (
            id          INTEGER PRIMARY KEY,
            nombre      TEXT    NOT NULL,
            fecha       TEXT    NOT NULL,
            organizador TEXT    NOT NULL,
            activo      INTEGER NOT NULL CHECK (activo IN (0, 1)),
            req_firma      INTEGER DEFAULT 0 CHECK (req_firma      IN (0, 1)),
            req_documento  INTEGER DEFAULT 0 CHECK (req_documento  IN (0, 1)),
            req_audio      INTEGER DEFAULT 0 CHECK (req_audio      IN (0, 1)),
            req_salud      INTEGER DEFAULT 0 CHECK (req_salud      IN (0, 1)),
            deslinde_version TEXT DEFAULT 'v1_1',
            friendly_intro   INTEGER DEFAULT 0 CHECK (friendly_intro IN (0, 1)),
            deslinde_texto   TEXT
        )
        """
    )

    # ------------------------------------------------------------------
    # Table: deslindes
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS deslindes (
            id           INTEGER PRIMARY KEY,
            evento_id    INTEGER NOT NULL,
            texto        TEXT    NOT NULL,
            hash_sha256  TEXT    NOT NULL,
            activo       INTEGER NOT NULL CHECK (activo IN (0, 1)),
            fecha_creacion TEXT,
            creado_por     TEXT,
            FOREIGN KEY (evento_id) REFERENCES eventos (id)
        )
        """
    )

    # ------------------------------------------------------------------
    # Table: aceptaciones
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS aceptaciones (
            id                    INTEGER PRIMARY KEY,
            evento_id             INTEGER NOT NULL,
            nombre_participante   TEXT    NOT NULL,
            documento             TEXT    NOT NULL,
            fecha_hora            TEXT    NOT NULL,
            ip                    TEXT    NOT NULL,
            user_agent            TEXT    NOT NULL,
            deslinde_hash_sha256  TEXT,
            firma_path            TEXT,
            doc_frente_path       TEXT,
            doc_dorso_path        TEXT,
            audio_path            TEXT,
            salud_doc_path        TEXT,
            salud_doc_tipo        TEXT,
            audio_exento          INTEGER DEFAULT 0 CHECK (audio_exento     IN (0, 1)),
            firma_asistida        INTEGER DEFAULT 0 CHECK (firma_asistida   IN (0, 1)),
            pdf_token             TEXT,
            pdf_token_expires_at  TEXT,
            pdf_token_revoked     INTEGER DEFAULT 0 CHECK (pdf_token_revoked IN (0, 1)),
            pdf_last_access_at    TEXT,
            pdf_access_count      INTEGER DEFAULT 0,
            valido                INTEGER DEFAULT 1,
            deslinde_version      TEXT    DEFAULT 'v1_1',
            documento_norm        TEXT,
            FOREIGN KEY (evento_id) REFERENCES eventos (id)
        )
        """
    )

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------

    # Regular index: speed up lookups by evento_id on aceptaciones
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_aceptaciones_evento "
        "ON aceptaciones (evento_id)"
    )

    # Regular index: speed up lookups by documento_norm on aceptaciones
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_aceptaciones_doc_norm "
        "ON aceptaciones (documento_norm)"
    )

    # Partial unique index: enforce at most one active deslinde per event.
    # NOTE: Partial indexes (WHERE clause) are supported by PostgreSQL and
    # SQLite 3.8.9+.  They are NOT supported by MySQL/MariaDB.
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
    # Drop indexes first (reverse order)
    op.execute("DROP INDEX IF EXISTS idx_deslindes_evento_activo")
    op.execute("DROP INDEX IF EXISTS idx_aceptaciones_doc_norm")
    op.execute("DROP INDEX IF EXISTS idx_aceptaciones_evento")

    # Drop tables in reverse dependency order
    op.execute("DROP TABLE IF EXISTS aceptaciones")
    op.execute("DROP TABLE IF EXISTS deslindes")
    op.execute("DROP TABLE IF EXISTS eventos")
