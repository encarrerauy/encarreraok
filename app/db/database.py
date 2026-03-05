import sqlite3
import os

# ------------------------------------------------------------------------------
# Configuración de base de datos SQLite
# ------------------------------------------------------------------------------
DEFAULT_DB_PATH = "/var/lib/encarreraok/encarreraok.sqlite3"
DB_PATH = os.environ.get("ENCARRERAOK_DB_PATH", DEFAULT_DB_PATH)

def get_connection() -> sqlite3.Connection:
    """
    Crea una conexión a la base SQLite.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
