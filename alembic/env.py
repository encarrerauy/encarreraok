"""
Alembic environment configuration for EncarreraOK.

Database URL resolution order:
  1. DATABASE_URL env var  -> used as-is (PostgreSQL or any SQLAlchemy URL)
  2. ENCARRERAOK_DB_PATH env var -> wrapped as sqlite:///<path>
  3. Fallback default      -> sqlite:////var/lib/encarreraok/encarreraok.sqlite3
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# ---------------------------------------------------------------------------
# Alembic Config object (gives access to alembic.ini values)
# ---------------------------------------------------------------------------
config = context.config

# Interpret the config file for Python logging if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# We do NOT use SQLAlchemy ORM models, so target_metadata stays None.
# DDL is written as raw SQL inside migration scripts.
# ---------------------------------------------------------------------------
target_metadata = None


# ---------------------------------------------------------------------------
# URL resolution helper
# ---------------------------------------------------------------------------

def get_url() -> str:
    """
    Return the SQLAlchemy database URL based on environment variables.

    Priority:
      1. DATABASE_URL  (supports PostgreSQL, SQLite full URLs, etc.)
      2. ENCARRERAOK_DB_PATH  (SQLite file path, wrapped automatically)
      3. Hardcoded SQLite default (development only)
    """
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url:
        # psycopg v3 requires postgresql+psycopg:// instead of postgresql://
        if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
            database_url = database_url.replace("://", "+psycopg://", 1)
        return database_url

    db_path = os.environ.get(
        "ENCARRERAOK_DB_PATH",
        "/var/lib/encarreraok/encarreraok.sqlite3",
    )
    return f"sqlite:///{db_path}"


# ---------------------------------------------------------------------------
# Offline migrations (no live DB connection)
# ---------------------------------------------------------------------------

def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    In this scenario Alembic emits SQL to stdout (or a file) without
    connecting to the database.  Useful for generating SQL scripts to be
    reviewed before applying them.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # render_as_batch is required for SQLite schema changes (ALTER TABLE
        # is severely limited in SQLite; batch mode rewrites the table).
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migrations (with live DB connection)
# ---------------------------------------------------------------------------

def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode with a real database connection.
    """
    url = get_url()

    # Override the sqlalchemy.url from alembic.ini with our resolved URL.
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # render_as_batch=True is required for SQLite to support ALTER TABLE
            # operations such as dropping/modifying columns.
            render_as_batch=is_sqlite,
        )

        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
