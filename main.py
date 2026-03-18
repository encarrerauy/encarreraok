# EncarreraOK - MVP de deslindes digitales
#
# Requisitos del MVP:
# - FastAPI + Uvicorn (sirve bajo systemd)
# - Nginx como reverse proxy (ya configurado)
# - SQLite para persistencia
# - HTML mínimo renderizado con Jinja2
# - Sin frameworks extra ni ORM (sqlite3 estándar)
#
# Este archivo `main.py` es autocontenido para el MVP:
# - Inicializa la base SQLite y crea las tablas si no existen
# - Define los modelos de datos (Pydantic) para claridad tipada
# - Expone endpoints:
#     GET  /e/{evento_id}        -> Formulario de aceptación
#     POST /e/{evento_id}        -> Guarda aceptación y confirma
#     GET  /admin/aceptaciones   -> Lista aceptaciones (sin auth)
# - Renderiza HTML con Jinja2 usando plantillas en memoria
#
# Notas:
# - En producción, se recomienda mover las plantillas a /var/www/encarreraok/app/templates
#   y reemplazar el DictLoader por FileSystemLoader.
# - Ruta de la base: configurable con ENV `ENCARRERAOK_DB_PATH`.

from fastapi import FastAPI
from app.config import settings
from pathlib import Path
from pydantic import BaseModel
from datetime import datetime, date
import sqlite3
import os
import re
import stat
import hashlib
import logging
from typing import Optional, List, Dict, Any
from logging.handlers import RotatingFileHandler


# ------------------------------------------------------------------------------
# Configuración de logging
# ------------------------------------------------------------------------------

def setup_logging() -> None:
    """Configura logging a archivo con rotación."""
    target_dir = "/var/log/encarreraok"

    try:
        os.makedirs(target_dir, exist_ok=True)
        test_file = os.path.join(target_dir, ".test_write")
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
    except Exception:
        target_dir = os.path.dirname(os.path.abspath(__file__))

    final_log_file = os.path.join(target_dir, "app.log")

    handler = RotatingFileHandler(
        final_log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)

    logger = logging.getLogger('encarreraok')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger

app_logger = setup_logging()


# ------------------------------------------------------------------------------
# Constantes
# ------------------------------------------------------------------------------
DEFAULT_DESLINDE_VERSION = "v1_1"

# ------------------------------------------------------------------------------
# Configuración de base de datos SQLite y Almacenamiento
# ------------------------------------------------------------------------------
DEFAULT_DB_PATH = "/var/lib/encarreraok/encarreraok.sqlite3"
DB_PATH = settings.db_path
EVIDENCIAS_DIR = os.path.join(os.path.dirname(DB_PATH), "evidencias")
FIRMAS_DIR = os.path.join(EVIDENCIAS_DIR, "firmas")
DOCUMENTOS_DIR = os.path.join(EVIDENCIAS_DIR, "documentos")
AUDIOS_DIR = os.path.join(EVIDENCIAS_DIR, "audios")
SALUD_DIR = os.path.join(EVIDENCIAS_DIR, "salud")


def ensure_storage() -> None:
    """
    Garantiza que directorios de DB y evidencias existan con permisos.
    """
    db_dir = os.path.dirname(DB_PATH)
    try:
        os.makedirs(db_dir, exist_ok=True)
        try:
            os.chmod(db_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        except Exception:
            pass

        if os.path.exists(DB_PATH):
            try:
                os.chmod(DB_PATH, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            except Exception:
                pass

        os.makedirs(FIRMAS_DIR, exist_ok=True)
        os.makedirs(DOCUMENTOS_DIR, exist_ok=True)
        os.makedirs(AUDIOS_DIR, exist_ok=True)
        os.makedirs(SALUD_DIR, exist_ok=True)
    except Exception:
        pass


def normalizar_documento_helper(doc: str) -> str:
    """Normaliza documento: quita puntos, guiones, espacios y pasa a mayúsculas."""
    if not doc:
        return ""
    return re.sub(r"[.\-\s]", "", doc).upper()


def get_connection() -> sqlite3.Connection:
    """
    Crea una conexión a la base SQLite.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ------------------------------------------------------------------------------
# MIGRACIONES Y ESQUEMA (SQLite)
# ------------------------------------------------------------------------------
# REGLA DE PROYECTO:
# - SQLite NO usa ORM ni migraciones externas.
# - Todo cambio de esquema debe:
#     1) Tener migración automática en startup (ensure_schema_migrations).
#     2) Tener código defensivo si la columna aún no existe.
#     3) Nunca provocar un error 500 en runtime.
# NO cambiar comportamiento funcional del sistema.
# NO agregar dependencias externas.
# NO usar Alembic.
# NO usar ORM.

def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    """
    Garantiza que el esquema de la base de datos esté actualizado.
    Ejecuta migraciones idempotentes y seguras al inicio.
    """
    cur = conn.cursor()

    # TAREA 2: Migración automática columna 'valido'
    try:
        cur.execute("PRAGMA table_info(aceptaciones)")
        columns = [info[1] for info in cur.fetchall()]

        if "valido" not in columns:
            app_logger.info("Iniciando migración: agregando columna 'valido' a 'aceptaciones'")
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN valido INTEGER DEFAULT 1")
            app_logger.info("Migración aplicada: columna valido agregada")

        # TAREA 3 (User Request): Migración columna 'deslinde_version'
        if "deslinde_version" not in columns:
            app_logger.info("Iniciando migración: agregando columna 'deslinde_version' a 'aceptaciones'")
            cur.execute(f"ALTER TABLE aceptaciones ADD COLUMN deslinde_version TEXT DEFAULT '{DEFAULT_DESLINDE_VERSION}'")
            app_logger.info("Migración aplicada: columna deslinde_version agregada")

    except sqlite3.OperationalError as e:
        app_logger.error(f"Error en migración de esquema: {e}")

def init_db() -> None:
    """
    Inicializa la base de datos y aplica migraciones manuales si es necesario.
    """
    ensure_storage()
    conn = get_connection()
    try:
        # TAREA 2: Asegurar migraciones antes de cualquier otra operación
        ensure_schema_migrations(conn)

        cur = conn.cursor()
        # Tabla de eventos
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS eventos (
                id INTEGER PRIMARY KEY,
                nombre TEXT NOT NULL,
                fecha TEXT NOT NULL,          -- ISO: YYYY-MM-DD
                organizador TEXT NOT NULL,
                activo INTEGER NOT NULL CHECK (activo IN (0,1)),
                req_firma INTEGER DEFAULT 0 CHECK (req_firma IN (0,1)),
                req_documento INTEGER DEFAULT 0 CHECK (req_documento IN (0,1)),
                req_audio INTEGER DEFAULT 0 CHECK (req_audio IN (0,1)),
                req_salud INTEGER DEFAULT 0 CHECK (req_salud IN (0,1))
            )
            """
        )

        # Migración: req_firma en eventos
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN req_firma INTEGER DEFAULT 0 CHECK (req_firma IN (0,1))")
        except sqlite3.OperationalError:
            pass

        # Migración: req_documento en eventos
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN req_documento INTEGER DEFAULT 0 CHECK (req_documento IN (0,1))")
        except sqlite3.OperationalError:
            pass

        # Migración: req_audio en eventos
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN req_audio INTEGER DEFAULT 0 CHECK (req_audio IN (0,1))")
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN req_salud INTEGER DEFAULT 0 CHECK (req_salud IN (0,1))")
        except sqlite3.OperationalError:
            pass

        # Migración: deslinde_version en eventos (v1_1 default)
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN deslinde_version TEXT DEFAULT 'v1_1'")
        except sqlite3.OperationalError:
            pass

        # DESLINDE PATCH: friendly intro flag
        # Migración: friendly_intro en eventos (default 0)
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN friendly_intro INTEGER DEFAULT 0 CHECK (friendly_intro IN (0,1))")
        except sqlite3.OperationalError:
            pass
        # /DESLINDE PATCH

        # TAREA: Migración deslinde_texto (custom per event)
        try:
            cur.execute("ALTER TABLE eventos ADD COLUMN deslinde_texto TEXT")
        except sqlite3.OperationalError:
            pass


        # Tabla de aceptaciones
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS aceptaciones (
                id INTEGER PRIMARY KEY,
                evento_id INTEGER NOT NULL,
                nombre_participante TEXT NOT NULL,
                documento TEXT NOT NULL,
                fecha_hora TEXT NOT NULL,     -- ISO: YYYY-MM-DDTHH:MM:SSZ (sin zona)
                ip TEXT NOT NULL,
                user_agent TEXT NOT NULL,
                deslinde_hash_sha256 TEXT,
                firma_path TEXT,
                doc_frente_path TEXT,
                doc_dorso_path TEXT,
                audio_path TEXT,
                salud_doc_path TEXT,
                FOREIGN KEY (evento_id) REFERENCES eventos(id)
            )
            """
        )

        # Migración: firma_path en aceptaciones
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN firma_path TEXT")
        except sqlite3.OperationalError:
            pass

        # Migración: doc_frente_path y doc_dorso_path en aceptaciones
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN doc_frente_path TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN doc_dorso_path TEXT")
        except sqlite3.OperationalError:
            pass

        # Migración: audio_path en aceptaciones
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN audio_path TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN salud_doc_path TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN salud_doc_tipo TEXT")
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN audio_exento INTEGER DEFAULT 0 CHECK (audio_exento IN (0,1))")
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN firma_asistida INTEGER DEFAULT 0 CHECK (firma_asistida IN (0,1))")
        except sqlite3.OperationalError:
            pass

        # Migración: pdf_token en aceptaciones
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN pdf_token TEXT")
        except sqlite3.OperationalError:
            pass

        # Migración: Stage A.2 - Control de tokens PDF
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN pdf_token_expires_at TEXT")  # ISO UTC
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN pdf_token_revoked INTEGER DEFAULT 0 CHECK (pdf_token_revoked IN (0,1))")
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN pdf_last_access_at TEXT")  # ISO UTC
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN pdf_access_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        # Tabla de deslindes versionados
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS deslindes (
                id INTEGER PRIMARY KEY,
                evento_id INTEGER NOT NULL,
                texto TEXT NOT NULL,
                hash_sha256 TEXT NOT NULL,
                activo INTEGER NOT NULL CHECK (activo IN (0,1)),
                fecha_creacion TEXT,          -- ISO UTC
                creado_por TEXT,
                FOREIGN KEY (evento_id) REFERENCES eventos(id)
            )
            """
        )

        # Migración manual simple: intentar agregar columnas si no existen
        try:
            cur.execute("ALTER TABLE deslindes ADD COLUMN fecha_creacion TEXT")
        except sqlite3.OperationalError:
            pass  # Ya existe

        try:
            cur.execute("ALTER TABLE deslindes ADD COLUMN creado_por TEXT")
        except sqlite3.OperationalError:
            pass  # Ya existe

        # Índice único parcial: un solo deslinde activo por evento
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_deslindes_evento_activo
            ON deslindes(evento_id) WHERE activo = 1
            """
        )

        # Migración: documento_norm para búsqueda optimizada
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN documento_norm TEXT")
            # Si se creó la columna, ejecutamos backfill inmediato
            app_logger.info("Columna documento_norm creada. Iniciando backfill...")
            cur.execute("SELECT id, documento FROM aceptaciones WHERE documento IS NOT NULL")
            rows = cur.fetchall()
            count = 0
            for r in rows:
                norm = normalizar_documento_helper(r['documento'])
                cur.execute("UPDATE aceptaciones SET documento_norm = ? WHERE id = ?", (norm, r['id']))
            app_logger.info(f"Backfill de documento_norm completado: {count} registros actualizados.")
        except sqlite3.OperationalError:
            # Si ya existe, verificamos si hay nulos para corregir (backfill perezoso)
            cur.execute("SELECT COUNT(*) FROM aceptaciones WHERE documento_norm IS NULL AND documento IS NOT NULL")
            if cur.fetchone()[0] > 0:
                app_logger.info("Detectados registros sin documento_norm. Ejecutando backfill...")
                cur.execute("SELECT id, documento FROM aceptaciones WHERE documento_norm IS NULL AND documento IS NOT NULL")
                rows = cur.fetchall()
                for r in rows:
                    norm = normalizar_documento_helper(r['documento'])
                    cur.execute("UPDATE aceptaciones SET documento_norm = ? WHERE id = ?", (norm, r['id']))

        # Migración: indices para performance
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_aceptaciones_evento ON aceptaciones(evento_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_aceptaciones_doc_norm ON aceptaciones(documento_norm)")
        except sqlite3.OperationalError:
            pass

        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Modelos de datos (Pydantic) para documentación y validación básica
# ------------------------------------------------------------------------------
class Evento(BaseModel):
    id: int
    nombre: str
    fecha: date
    organizador: str
    activo: bool
    req_firma: bool = False
    req_documento: bool = False
    req_audio: bool = False
    friendly_intro: bool = False  # DESLINDE PATCH: friendly intro


class Aceptacion(BaseModel):
    id: int
    evento_id: int
    nombre_participante: str
    documento: str
    fecha_hora: datetime
    ip: str
    user_agent: str
    firma_path: Optional[str] = None
    doc_frente_path: Optional[str] = None
    doc_dorso_path: Optional[str] = None
    audio_path: Optional[str] = None


# ------------------------------------------------------------------------------
# Configuración de aplicación FastAPI
# ------------------------------------------------------------------------------
app = FastAPI(title="EncarreraOK - MVP deslindes")

# STATIC PATCH: serve assets
from fastapi.staticfiles import StaticFiles
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount(
    "/assets",
    StaticFiles(directory=os.path.join(BASE_DIR, "assets")),
    name="assets"
)
# /STATIC PATCH

# ------------------------------------------------------------------------------
# Registrar routers
# ------------------------------------------------------------------------------
from app.routers import public, admin
app.include_router(public.router)
app.include_router(admin.router)


# ------------------------------------------------------------------------------
# Hooks de arranque: inicializa base y crea un evento de ejemplo si vacío
# ------------------------------------------------------------------------------
@app.on_event("startup")
def on_startup() -> None:
    """
    Inicializa la base y, si no hay eventos, crea uno de ejemplo para pruebas.
    """
    init_db()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM eventos")
        count = cur.fetchone()["c"]
        if count == 0:
            cur.execute(
                """
                INSERT INTO eventos (nombre, fecha, organizador, activo, deslinde_version)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Carrera 10K Montevideo", date.today().isoformat(), "Encarrera", 1, "v1_1"),
            )
            conn.commit()

    finally:
        conn.close()


# ------------------------------------------------------------------------------
# Ejecutable local (opcional). En producción se usa systemd + uvicorn.
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    # Servidor local para pruebas:
    #   python main.py
    #   Navegar a: http://127.0.0.1:8000/docs
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


# ==============================================================================
# PLAN DE PRUEBAS MANUALES
# ==============================================================================
#
# Objetivo: Verificar logging, visualización de paths y detalle de aceptaciones
#
# PREREQUISITOS:
# - Servidor corriendo (python main.py o systemd)
# - Directorio /var/log/encarreraok debe existir o tener permisos
# - Evento creado (se crea automáticamente si la DB está vacía)
#
# PASO 1: Verificar logging básico
#   - Acceder a GET /e/1 (formulario)
#   - Verificar que /var/log/encarreraok/app.log existe
#   - Comando: tail -f /var/log/encarreraok/app.log
#   - Esperar: No debería haber logs aún (solo POST genera logs)
#
# PASO 2: Crear aceptación con todas las evidencias (Desktop)
#   - Navegar a http://localhost:8000/e/1
#   - Completar formulario:
#     * Nombre: "Test Usuario"
#     * Documento: "12345678"
#     * Subir foto frente documento (imagen < 4MB)
#     * Subir foto dorso documento (imagen < 4MB)
#     * Grabar audio de aceptación (< 5MB)
#     * Firmar en canvas
#     * Marcar checkbox acepto
#   - Enviar formulario
#   - Verificar logs esperados:
#     * [request_id] Inicio procesamiento aceptación - evento_id=1
#     * [request_id] Firma guardada: path=..., size=... bytes
#     * [request_id] Doc frente guardado: path=..., size=... bytes
#     * [request_id] Doc dorso guardado: path=..., size=... bytes
#     * [request_id] Audio guardado: path=..., size=... bytes
#     * [request_id] Aceptación guardada exitosamente - aceptacion_id=...
#
# PASO 3: Verificar listado admin con paths
#   - Navegar a http://localhost:8000/admin/aceptaciones
#   - Verificar que aparecen nuevas columnas:
#     * Firma Path
#     * Doc Frente Path
#     * Doc Dorso Path
#     * Audio Path
#   - Verificar que los paths se muestran (truncados si son largos)
#   - Verificar que el ID es un link clickeable
#
# PASO 4: Verificar detalle de aceptación
#   - Click en el ID de la aceptación creada (o navegar a /admin/aceptaciones/1)
#   - Verificar que se muestra:
#     * Todos los datos de la aceptación
#     * Todos los paths completos
#     * "Firma Existe: Sí" (en verde)
#     * "Doc Frente Existe: Sí" (en verde)
#     * "Doc Dorso Existe: Sí" (en verde)
#     * "Audio Existe: Sí" (en verde)
#   - Verificar link "← Volver a lista" funciona
#
# PASO 5: Probar con imagen grande (compresión)
#   - Navegar a http://localhost:8000/e/1
#   - Subir imagen de documento > 2MB pero < 4MB
#   - Verificar en logs:
#     * [request_id] Comprimiendo doc frente: ... bytes
#     * [request_id] Doc frente comprimido: ... -> ... bytes
#   - Completar y enviar formulario
#   - Verificar que la aceptación se guarda correctamente
#
# PASO 6: Probar error 413 y logging de excepciones (Mobile/Desktop)
#   - Navegar a http://localhost:8000/e/1 desde móvil o desktop
#   - Intentar subir imagen > 4MB
#   - Verificar que se rechaza con mensaje claro
#   - Verificar en logs:
#     * [request_id] Doc frente demasiado grande: ... bytes
#   - Intentar enviar firma muy grande (dibujar mucho en canvas)
#   - Verificar que se rechaza antes de enviar (validación frontend)
#   - Si se envía de alguna forma, verificar en logs:
#     * [request_id] Firma demasiado grande: ... bytes
#   - Probar con audio > 5MB
#   - Verificar en logs:
#     * [request_id] Audio demasiado grande: ... bytes
#
# VERIFICACIÓN FINAL:
#   - Revisar /var/log/encarreraok/app.log completo
#   - Verificar que todos los request_id son únicos
#   - Verificar que todos los tamaños están en bytes
#   - Verificar que todos los paths son absolutos
#   - Verificar que no hay excepciones sin loggear
#   - Verificar rotación: si el log supera 10MB, debería rotar
#
# NOTAS:
#   - Si /var/log/encarreraok no tiene permisos, el log se crea en el directorio actual
#   - Los logs incluyen timestamp, nivel, y mensaje estructurado
#   - Los excepciones incluyen stacktrace completo
#   - Los paths verificados en detalle usan os.path.exists() en tiempo real

# ==============================================================================
# BACKLOG DE SEGURIDAD / LEGALES
# ==============================================================================
# - Rate limiting en endpoint público (Nginx / middleware)
# - Firma temporal externa (timestamp authority)
# - Hash anclado externo (blockchain / TSA)
# - Descarga con watermark opcional
# - Política de retención configurable por evento
# ==============================================================================
