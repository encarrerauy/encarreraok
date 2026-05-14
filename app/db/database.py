import sqlite3
import os
import logging
from typing import Union, Any, List

# Configuración de logger
logger = logging.getLogger(__name__)

# Intentar importar psycopg (para PostgreSQL)
try:
    import psycopg
    from psycopg.rows import dict_row
    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False
    logger.warning("psycopg no instalado. Soporte PostgreSQL deshabilitado.")

# ------------------------------------------------------------------------------
# Configuración de base de datos
# ------------------------------------------------------------------------------
DEFAULT_DB_PATH = "/var/lib/encarreraok/encarreraok.sqlite3"
DB_PATH = os.environ.get("ENCARRERAOK_DB_PATH", DEFAULT_DB_PATH)
DATABASE_URL = os.environ.get("DATABASE_URL")

def is_postgres_connection(conn: Any) -> bool:
    if not PSYCOPG_AVAILABLE:
        return False
    try:
        return isinstance(conn, psycopg.Connection)
    except Exception:
        return False

def is_postgres_configured() -> bool:
    if not DATABASE_URL:
        return False
    return DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

def sql_param(conn: Any = None) -> str:
    if conn is not None:
        return "%s" if is_postgres_connection(conn) else "?"
    return "%s" if is_postgres_configured() else "?"

def sql_placeholders(count: int, conn: Any = None) -> str:
    if count <= 0:
        return ""
    p = sql_param(conn)
    return ", ".join([p] * count)

class _SQLiteCompatCursor:
    """Cursor SQLite que acepta %s como placeholder (igual que PostgreSQL)."""
    def __init__(self, cursor):
        self._cur = cursor

    def execute(self, sql, params=None):
        sql = sql.replace("%s", "?")
        if params is None:
            return self._cur.execute(sql)
        return self._cur.execute(sql, params)

    def executemany(self, sql, seq):
        sql = sql.replace("%s", "?")
        return self._cur.executemany(sql, seq)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __iter__(self):
        return iter(self._cur)


class _SQLiteCompatConnection:
    """Conexión SQLite que devuelve cursores compatibles con %s."""
    def __init__(self, conn):
        self._conn = conn
        self.row_factory = conn.row_factory

    def cursor(self):
        return _SQLiteCompatCursor(self._conn.cursor())

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._conn.__exit__(*args)


def get_connection() -> Union[sqlite3.Connection, Any]:
    """
    Crea una conexión a la base de datos.
    Soporta SQLite (por defecto) y PostgreSQL (si DATABASE_URL está definido).
    """
    # 1. Detectar si se debe usar PostgreSQL
    if is_postgres_configured():
        if not PSYCOPG_AVAILABLE:
            raise ImportError("DATABASE_URL detectada pero psycopg no está instalado.")

        try:
            conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
            return conn
        except Exception as e:
            logger.error(f"Error conectando a PostgreSQL: {e}")
            raise

    # 2. Fallback a SQLite — envuelto para aceptar %s como placeholder
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return _SQLiteCompatConnection(conn)
    except Exception as e:
        logger.error(f"Error conectando a SQLite en {DB_PATH}: {e}")
        raise

def get_table_columns(conn: Union[sqlite3.Connection, Any], table_name: str) -> List[str]:
    """
    Obtiene la lista de nombres de columnas de una tabla.
    Compatible con SQLite y PostgreSQL.
    """
    is_postgres = False
    if PSYCOPG_AVAILABLE and isinstance(conn, psycopg.Connection):
        is_postgres = True

    try:
        cur = conn.cursor()
        if is_postgres:
            # PostgreSQL: usar information_schema
            # Asegurar que el nombre de tabla sea el correcto (comúnmente minúsculas en PG)
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table_name,)
            )
            rows = cur.fetchall()
            # rows es una lista de dicts con 'column_name' (gracias a dict_row)
            return [row['column_name'] for row in rows]
        else:
            # SQLite: usar PRAGMA table_info
            # PRAGMA no acepta parámetros, se debe interpolar con cuidado (solo uso interno)
            cur.execute(f"PRAGMA table_info({table_name})")
            rows = cur.fetchall()
            # rows es una lista de sqlite3.Row (acceso por índice o nombre)
            # PRAGMA devuelve: cid, name, type, notnull, dflt_value, pk
            return [row['name'] for row in rows]
    except Exception as e:
        logger.error(f"Error obteniendo columnas de tabla {table_name}: {e}")
        return []
