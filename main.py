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

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File, Depends, status
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, FileSystemLoader
from app.config import settings
from app.middleware.auth import get_current_username, security
from pathlib import Path
from pydantic import BaseModel
from datetime import datetime, date
import sqlite3
import os
import secrets
import stat
import hashlib
import re
import base64
import uuid
import shutil
import zipfile
import json
import struct
from typing import Optional, List, Dict, Any
import io
import logging
import traceback
from logging.handlers import RotatingFileHandler

# Intentar importar PIL para compresión de imágenes (opcional)
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ------------------------------------------------------------------------------
# Configuración de logging
# ------------------------------------------------------------------------------

def setup_logging() -> None:
    """Configura logging a archivo con rotación."""
    # Intentar primero en /var/log, fallback a directorio local
    target_dir = "/var/log/encarreraok"
    
    try:
        os.makedirs(target_dir, exist_ok=True)
        # Verificar escritura intentando crear un archivo temporal
        test_file = os.path.join(target_dir, ".test_write")
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
    except Exception:
        # Fallback: usar directorio actual si no se puede escribir en /var/log
        target_dir = os.path.dirname(os.path.abspath(__file__))
    
    final_log_file = os.path.join(target_dir, "app.log")
    
    # Handler con rotación (10MB, 5 backups)
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

def normalizar_documento_helper(documento: str) -> Optional[str]:
    """
    Normaliza un número de documento para búsqueda y persistencia.
    Elimina todo lo que no sea dígito.
    Si el resultado es vacío, retorna None (o string vacío, según uso).
    """
    if not documento:
        return None
    # Filtrar solo dígitos
    norm = "".join(filter(str.isdigit, str(documento)))
    return norm if norm else None


# ------------------------------------------------------------------------------
# Constantes
# ------------------------------------------------------------------------------
# Límites de tamaño por tipo de evidencia (prevención 413)
MAX_IMAGE_DOC_MB = 4  # Imagen documento: máx 4 MB por archivo
MAX_FIRMA_MB = 1      # Firma canvas: máx 1 MB
MAX_AUDIO_MB = 5      # Audio: máx 5 MB
# Límites para compresión automática
MAX_IMAGE_COMPRESS_THRESHOLD_MB = 2  # Si supera esto, comprimir
MAX_IMAGE_COMPRESS_TARGET_MB = 1.5   # Objetivo después de compresión

# Configuración de versiones de deslinde
LEGAL_DIR = settings.legal_dir
DESLINDES_CONFIG = {
    "v1_1": "deslinde_v1_1_ligero.txt",
    "v2_0": "deslinde_v2_0_legal_fuerte.txt",
    "v3_0": "deslinde_v3_0_legal_full.txt",
}
DEFAULT_DESLINDE_VERSION = "v1_1"

def cargar_deslinde(version: str = DEFAULT_DESLINDE_VERSION) -> str:
    """
    Carga el texto del deslinde desde archivo según la versión.
    Retorna el texto base con placeholders.
    """
    filename = DESLINDES_CONFIG.get(version)
    if not filename:
        app_logger.error(f"Versión de deslinde desconocida: {version}, usando default")
        filename = DESLINDES_CONFIG[DEFAULT_DESLINDE_VERSION]
    
    path = os.path.join(LEGAL_DIR, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        app_logger.error(f"Error leyendo archivo de deslinde {path}: {e}")
        # Fallback de emergencia si no se puede leer el archivo
        return """DESLINDE DE RESPONSABILIDAD Y ACEPTACIÓN DE RIESGOS

Declaro que participo en el evento deportivo {{NOMBRE_EVENTO}}, organizado por {{ORGANIZADOR}}, de manera voluntaria y bajo mi exclusiva responsabilidad.

Reconozco que la participación en actividades deportivas implica riesgos inherentes, incluyendo, pero no limitándose a, caídas, lesiones físicas, traumatismos, accidentes cardiovasculares, condiciones climáticas adversas y otros riesgos propios de la actividad.

Declaro encontrarme en condiciones físicas y de salud adecuadas para participar, y que he sido debidamente informado/a sobre las características del evento.

Eximo de toda responsabilidad civil, penal y administrativa al organizador, auspiciantes, colaboradores, personal médico, autoridades y cualquier otra persona vinculada a la organización del evento, por cualquier daño, lesión o perjuicio que pudiera sufrir antes, durante o después de mi participación.

Autorizo la utilización de mi imagen, voz y datos personales con fines de difusión, promoción y registro del evento, sin derecho a compensación económica.

Declaro haber leído, comprendido y aceptado íntegramente el presente deslinde de responsabilidad."""


# ------------------------------------------------------------------------------
# Configuración de aplicación y plantillas Jinja2 (en memoria para el MVP)
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

templates_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "app" / "templates"),
    autoescape=True
)

def fecha_ddmmaaaa(value: str) -> str:
    try:
        y, m, d = value.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return value
templates_env.filters["fecha_ddmmaaaa"] = fecha_ddmmaaaa

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
    # Directorio base y DB
    db_dir = os.path.dirname(DB_PATH)
    try:
        os.makedirs(db_dir, exist_ok=True)
        # Permisos 0750 (rwxr-x---) para directorio base
        try:
            os.chmod(db_dir, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        except Exception:
            pass
            
        if os.path.exists(DB_PATH):
            try:
                os.chmod(DB_PATH, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            except Exception:
                pass
                
        # Directorios de evidencias
        os.makedirs(FIRMAS_DIR, exist_ok=True)
        os.makedirs(DOCUMENTOS_DIR, exist_ok=True)
        os.makedirs(AUDIOS_DIR, exist_ok=True)
        os.makedirs(SALUD_DIR, exist_ok=True)
        # Podríamos ajustar permisos de evidencias también
    except Exception:
        # Entorno local dev windows etc
        pass


def normalizar_documento_helper(doc: str) -> str:
    """Normaliza documento: quita puntos, guiones, espacios y pasa a mayúsculas."""
    if not doc:
        return ""
    return re.sub(r"[.\-\s]", "", doc).upper()


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
            # Default v1_1 para registros viejos
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
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN pdf_token_expires_at TEXT") # ISO UTC
        except sqlite3.OperationalError:
            pass
        
        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN pdf_token_revoked INTEGER DEFAULT 0 CHECK (pdf_token_revoked IN (0,1))")
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute("ALTER TABLE aceptaciones ADD COLUMN pdf_last_access_at TEXT") # ISO UTC
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
            pass # Ya existe
            
        try:
            cur.execute("ALTER TABLE deslindes ADD COLUMN creado_por TEXT")
        except sqlite3.OperationalError:
            pass # Ya existe

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


def get_evento(evento_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene un evento por id."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insertar_aceptacion(
    evento_id: int,
    nombre_participante: str,
    documento: str,
    fecha_hora: str,
    ip: str,
    user_agent: str,
    deslinde_hash_sha256: str,
    firma_path: Optional[str] = None,
    doc_frente_path: Optional[str] = None,
    doc_dorso_path: Optional[str] = None,
    audio_path: Optional[str] = None,
    salud_doc_path: Optional[str] = None,
    salud_doc_tipo: Optional[str] = None,
    audio_exento: int = 0,
    firma_asistida: int = 0,
    pdf_token: Optional[str] = None,
    documento_norm: Optional[str] = None,
    deslinde_version: str = DEFAULT_DESLINDE_VERSION,
) -> int:
    """Inserta una aceptación y devuelve el ID creado."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO aceptaciones (
                evento_id, nombre_participante, documento, fecha_hora, ip, user_agent, deslinde_hash_sha256, firma_path, doc_frente_path, doc_dorso_path, audio_path, salud_doc_path, salud_doc_tipo, audio_exento, firma_asistida, pdf_token, documento_norm, deslinde_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (evento_id, nombre_participante, documento, fecha_hora, ip, user_agent, deslinde_hash_sha256, firma_path, doc_frente_path, doc_dorso_path, audio_path, salud_doc_path, salud_doc_tipo, audio_exento, firma_asistida, pdf_token, documento_norm, deslinde_version),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()



def aceptacion_existente(conn: sqlite3.Connection, evento_id: int, documento_norm: str) -> bool:
    """
    TAREA 1: Código defensivo para verificar duplicados.
    Detecta si la columna 'valido' existe antes de usarla.
    """
    if not documento_norm:
        return False
        
    cur = conn.cursor()
    
    # Detectar si existe columna 'valido'
    cur.execute("PRAGMA table_info(aceptaciones)")
    columns = [info[1] for info in cur.fetchall()]
    has_valido = "valido" in columns
    
    if has_valido:
        # Si existe, filtrar por valido=1
        cur.execute(
            "SELECT 1 FROM aceptaciones WHERE evento_id = ? AND documento_norm = ? AND valido = 1 LIMIT 1",
            (evento_id, documento_norm)
        )
    else:
        # Si NO existe, usar query legacy (compatible)
        cur.execute(
            "SELECT 1 FROM aceptaciones WHERE evento_id = ? AND documento_norm = ? LIMIT 1",
            (evento_id, documento_norm)
        )
        
    return cur.fetchone() is not None


def listar_eventos() -> List[Dict[str, Any]]:
    """Lista todos los eventos para filtrado."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nombre, fecha, organizador, activo, req_firma, req_documento, req_audio, deslinde_version, friendly_intro FROM eventos ORDER BY id DESC")
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def crear_evento(
    nombre: str,
    fecha: str,
    organizador: str,
    activo: int,
    req_firma: int,
    req_documento: int,
    req_salud: int,
    req_audio: int,
    deslinde_version: str,
    friendly_intro: int # DESLINDE PATCH: friendly intro
) -> int:
    """Crea un nuevo evento y devuelve su ID."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO eventos (
                nombre, fecha, organizador, activo, req_firma, req_documento, req_salud, req_audio, deslinde_version, friendly_intro
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (nombre, fecha, organizador, activo, req_firma, req_documento, req_salud, req_audio, deslinde_version, friendly_intro)
        )
        conn.commit()
        evento_id = cur.lastrowid
        app_logger.info(f"Evento creado: id={evento_id}, nombre={nombre}")
        return evento_id
    finally:
        conn.close()


def actualizar_evento(
    evento_id: int,
    nombre: str,
    fecha: str,
    organizador: str,
    activo: int,
    req_firma: int,
    req_documento: int,
    req_salud: int,
    req_audio: int,
    deslinde_version: str,
    friendly_intro: int # DESLINDE PATCH: friendly intro
) -> bool:
    """Actualiza un evento existente."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE eventos 
            SET nombre=?, fecha=?, organizador=?, activo=?, req_firma=?, req_documento=?, req_salud=?, req_audio=?, deslinde_version=?, friendly_intro=?
            WHERE id=?
            """,
            (nombre, fecha, organizador, activo, req_firma, req_documento, req_salud, req_audio, deslinde_version, friendly_intro, evento_id)
        )
        conn.commit()
        if cur.rowcount > 0:
            app_logger.info(f"Evento actualizado: id={evento_id}")
            return True
        return False
    finally:
        conn.close()


def listar_aceptaciones(evento_id: Optional[int] = None, query: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lista aceptaciones con datos del evento (join simple). 
    Filtra por evento si se especifica.
    Filtra por nombre o documento si query se especifica.
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        sql = """
            SELECT
                a.id,
                a.evento_id,
                e.nombre AS evento_nombre,
                e.fecha AS evento_fecha,
                e.organizador AS evento_organizador,
                a.nombre_participante,
                a.documento,
                a.fecha_hora,
                a.ip,
                a.user_agent,
                a.deslinde_hash_sha256,
                a.firma_path,
                a.doc_frente_path,
                a.doc_dorso_path,
                a.audio_path,
                a.salud_doc_path,
                a.salud_doc_tipo,
                a.audio_exento,
                a.firma_asistida
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
        """
        params = []
        conditions = []
        
        if evento_id is not None:
            conditions.append("a.evento_id = ?")
            params.append(evento_id)
            
        if query:
            # Búsqueda insensible a mayúsculas/minúsculas simple
            # P1.1 - Fix buscador por documento: soporte parcial y normalizado
            q_norm = "".join(filter(str.isdigit, query))
            
            # Siempre buscamos por nombre
            clauses = ["a.nombre_participante LIKE ?"]
            params_list = [f"%{query}%"]
            
            # Si hay suficientes dígitos, buscamos también por documento normalizado
            # (tolerancia a formato y búsqueda parcial)
            if len(q_norm) >= 3:
                clauses.append("a.documento_norm LIKE ?")
                params_list.append(f"%{q_norm}%")
            
            conditions.append(f"({' OR '.join(clauses)})")
            params.extend(params_list)
            
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        
        sql += " ORDER BY a.id DESC"
        
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def borrar_evidencias_fisicas(aceptaciones: List[Dict[str, Any]]):
    """Borra archivos físicos de una lista de aceptaciones."""
    count = 0
    for a in aceptaciones:
        paths = [
            a.get('firma_path'),
            a.get('doc_frente_path'),
            a.get('doc_dorso_path'),
            a.get('audio_path'),
            a.get('salud_doc_path')
        ]
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    count += 1
                except OSError as e:
                    app_logger.error(f"Error borrando archivo {p}: {e}")
    return count


def eliminar_aceptaciones_por_ids(ids: List[int]) -> int:
    """Elimina registros de aceptaciones por lista de IDs."""
    if not ids:
        return 0
    conn = get_connection()
    try:
        cur = conn.cursor()
        # SQLite no soporta arrays nativos, usamos placeholders dinámicos
        placeholders = ','.join('?' * len(ids))
        sql = f"DELETE FROM aceptaciones WHERE id IN ({placeholders})"
        cur.execute(sql, ids)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def eliminar_evento_completo(evento_id: int) -> bool:
    """Elimina un evento y todas sus referencias."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        # Primero aceptaciones (redundante si ya se borraron, pero seguro)
        cur.execute("DELETE FROM aceptaciones WHERE evento_id = ?", (evento_id,))
        # Luego el evento
        cur.execute("DELETE FROM eventos WHERE id = ?", (evento_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def get_aceptacion_detalle(aceptacion_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene detalle completo de una aceptación con verificación de existencia de archivos."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.id,
                a.evento_id,
                e.nombre AS evento_nombre,
                e.fecha AS evento_fecha,
                e.organizador AS evento_organizador,
                a.nombre_participante,
                a.documento,
                a.fecha_hora,
                a.ip,
                a.user_agent,
                a.deslinde_hash_sha256,
                a.firma_path,
                a.doc_frente_path,
                a.doc_dorso_path,
                a.audio_path,
                a.salud_doc_path,
                a.salud_doc_tipo,
                a.audio_exento,
                a.firma_asistida,
                a.pdf_token,
                a.pdf_token_expires_at,
                a.pdf_token_revoked,
                a.pdf_last_access_at,
                a.pdf_access_count
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            WHERE a.id = ?
            """,
            (aceptacion_id,)
        )
        row = cur.fetchone()
        if not row:
            return None
        
        data = dict(row)
        
        # Verificar existencia de archivos
        data['firma_exists'] = os.path.exists(data['firma_path']) if data['firma_path'] else False
        data['doc_frente_exists'] = os.path.exists(data['doc_frente_path']) if data['doc_frente_path'] else False
        data['doc_dorso_exists'] = os.path.exists(data['doc_dorso_path']) if data['doc_dorso_path'] else False
        data['audio_exists'] = os.path.exists(data['audio_path']) if data['audio_path'] else False
        data['salud_doc_exists'] = os.path.exists(data['salud_doc_path']) if data['salud_doc_path'] else False
        
        return data
    finally:
        conn.close()


def get_aceptacion_por_token(pdf_token: str) -> Optional[Dict[str, Any]]:
    """Obtiene aceptación por token público."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.id,
                a.evento_id,
                e.nombre AS evento_nombre,
                e.fecha AS evento_fecha,
                e.organizador AS evento_organizador,
                a.nombre_participante,
                a.documento,
                a.fecha_hora,
                a.ip,
                a.user_agent,
                a.deslinde_hash_sha256,
                a.firma_path,
                a.doc_frente_path,
                a.doc_dorso_path,
                a.audio_path,
                a.salud_doc_path,
                a.salud_doc_tipo,
                a.audio_exento,
                a.firma_asistida,
                a.pdf_token,
                a.pdf_token_expires_at,
                a.pdf_token_revoked,
                a.pdf_last_access_at,
                a.pdf_access_count
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            WHERE a.pdf_token = ?
            """,
            (pdf_token,)
        )
        row = cur.fetchone()
        if not row:
            return None
        
        data = dict(row)
        return data
    finally:
        conn.close()


def revocar_pdf_token(aceptacion_id: int) -> bool:
    """Revoca el token PDF de una aceptación (soft revoke)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE aceptaciones SET pdf_token_revoked = 1 WHERE id = ?",
            (aceptacion_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def registrar_acceso_pdf(aceptacion_id: int):
    """Registra un acceso exitoso al PDF."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        now_utc = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        cur.execute(
            """
            UPDATE aceptaciones 
            SET pdf_last_access_at = ?, 
                pdf_access_count = COALESCE(pdf_access_count, 0) + 1 
            WHERE id = ?
            """,
            (now_utc, aceptacion_id)
        )
        conn.commit()
    except Exception as e:
        app_logger.error(f"Error registrando acceso PDF id={aceptacion_id}: {e}")
    finally:
        conn.close()


def calcular_hash_sha256(texto: str) -> str:
    """Calcula SHA256 en hex del texto provisto."""
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def calcular_hash_archivo(filepath: str) -> str:
    """Calcula SHA256 de un archivo en disco."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        # Leer en chunks para eficiencia
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def comprimir_imagen(file_path: str, max_size_mb: float = MAX_IMAGE_COMPRESS_TARGET_MB) -> Optional[str]:
    """
    Comprime una imagen si es posible usando PIL.
    Retorna la ruta del archivo comprimido o None si no se pudo comprimir.
    Si PIL no está disponible, retorna None.
    """
    if not PIL_AVAILABLE:
        return None
    
    try:
        max_size_bytes = int(max_size_mb * 1024 * 1024)
        
        # Abrir imagen
        img = Image.open(file_path)
        original_format = img.format or 'JPEG'
        
        # Convertir a RGB si es necesario (para JPEG)
        if original_format in ('JPEG', 'JPG') and img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Calcular tamaño actual
        buffer = io.BytesIO()
        img.save(buffer, format=original_format, quality=85, optimize=True)
        current_size = buffer.tell()
        
        if current_size <= max_size_bytes:
            # Ya está dentro del límite
            return file_path
        
        # Reducir resolución manteniendo aspecto
        original_width, original_height = img.size
        ratio = (max_size_bytes / current_size) ** 0.5  # Factor de reducción
        new_width = int(original_width * ratio)
        new_height = int(original_height * ratio)
        
        # Asegurar mínimo de 800px en el lado más largo
        if max(new_width, new_height) < 800:
            if new_width > new_height:
                new_width = 800
                new_height = int(original_height * (800 / original_width))
            else:
                new_height = 800
                new_width = int(original_width * (800 / original_height))
        
        # Redimensionar (compatible con versiones antiguas de PIL)
        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        img_resized = img.resize((new_width, new_height), resample)
        
        # Intentar diferentes calidades hasta alcanzar el tamaño objetivo
        for quality in [85, 75, 65, 55, 45]:
            buffer = io.BytesIO()
            img_resized.save(buffer, format=original_format, quality=quality, optimize=True)
            if buffer.tell() <= max_size_bytes:
                # Guardar archivo comprimido
                with open(file_path, 'wb') as f:
                    f.write(buffer.getvalue())
                return file_path
        
        # Si aún no cumple, usar calidad mínima
        buffer = io.BytesIO()
        img_resized.save(buffer, format=original_format, quality=40, optimize=True)
        if buffer.tell() <= max_size_bytes * 1.2:  # Tolerancia del 20%
            with open(file_path, 'wb') as f:
                f.write(buffer.getvalue())
            return file_path
        
        return None
    except Exception:
        return None


def get_deslinde_activo(evento_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene el deslinde activo para un evento."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, evento_id, texto, hash_sha256, activo
            FROM deslindes
            WHERE evento_id = ? AND activo = 1
            LIMIT 1
            """,
            (evento_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insertar_deslinde(
    evento_id: int,
    texto: str,
    activo: int = 1,
    creado_por: str = "sistema",
) -> int:
    """Inserta un deslinde para un evento (por defecto activo) y devuelve su ID."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        hashv = calcular_hash_sha256(texto)
        fecha_creacion = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        cur.execute(
            """
            INSERT INTO deslindes (evento_id, texto, hash_sha256, activo, fecha_creacion, creado_por)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (evento_id, texto, hashv, activo, fecha_creacion, creado_por),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()



# ------------------------------------------------------------------------------
# Generador PDF con soporte Unicode (TTF Embed + Identity-H)
# ------------------------------------------------------------------------------
class TTFFont:
    """
    Parser minimalista de archivos TTF para extracción de métricas y mapeo Unicode.
    Soporta tablas: head, hhea, hmtx, cmap (format 4).
    """
    def __init__(self, font_path: str):
        with open(font_path, 'rb') as f:
            self.data = f.read()
        
        self.tables = {}
        self.units_per_em = 1000
        self.ascent = 0
        self.descent = 0
        self.cap_height = 0
        self.bbox = [0, 0, 0, 0]
        self.advance_widths = []
        self.cmap = {}  # unicode -> gid
        self.gid_to_unicode = {} # gid -> unicode
        self.num_metrics = 0
        
        self._parse()

    def _parse(self):
        # Offset Table
        num_tables = struct.unpack('>H', self.data[4:6])[0]
        offset = 12
        for _ in range(num_tables):
            tag = self.data[offset:offset+4].decode('latin1')
            checksum, t_offset, t_length = struct.unpack('>III', self.data[offset+4:offset+16])
            self.tables[tag] = (t_offset, t_length)
            offset += 16
            
        self._parse_head()
        self._parse_hhea()
        self._parse_hmtx()
        self._parse_cmap()

    def _parse_head(self):
        if 'head' not in self.tables: return
        off, _ = self.tables['head']
        self.units_per_em = struct.unpack('>H', self.data[off+18:off+20])[0]
        x_min, y_min, x_max, y_max = struct.unpack('>hhhh', self.data[off+36:off+44])
        self.bbox = [x_min, y_min, x_max, y_max]

    def _parse_hhea(self):
        if 'hhea' not in self.tables: return
        off, _ = self.tables['hhea']
        self.ascent, self.descent = struct.unpack('>hh', self.data[off+4:off+8])
        self.num_metrics = struct.unpack('>H', self.data[off+34:off+36])[0]

    def _parse_hmtx(self):
        if 'hmtx' not in self.tables: return
        off, _ = self.tables['hmtx']
        # Read advance widths
        self.advance_widths = []
        for i in range(self.num_metrics):
            aw, lsb = struct.unpack('>Hh', self.data[off + i*4 : off + i*4 + 4])
            self.advance_widths.append(aw)
        
        # We don't read LSBs for trailing glyphs to save memory/time, 
        # usually assume last width for remaining glyphs (monospaced logic) or 0
        
    def _parse_cmap(self):
        if 'cmap' not in self.tables: return
        off, _ = self.tables['cmap']
        num_subtables = struct.unpack('>H', self.data[off+2:off+4])[0]
        
        subtable_offset = 0
        for i in range(num_subtables):
            platform_id, encoding_id, s_off = struct.unpack('>HHI', self.data[off+4 + i*8 : off+4 + i*8 + 8])
            # Prefer Windows Unicode (3, 1) or (3, 10)
            if platform_id == 3 and encoding_id in (1, 10):
                subtable_offset = off + s_off
                break
            # Fallback to Unicode Platform (0, *)
            if platform_id == 0:
                subtable_offset = off + s_off
        
        if subtable_offset == 0: return

        format = struct.unpack('>H', self.data[subtable_offset:subtable_offset+2])[0]
        if format == 4:
            self._parse_cmap_format_4(subtable_offset)

    def _parse_cmap_format_4(self, offset):
        length = struct.unpack('>H', self.data[offset+2:offset+4])[0]
        seg_count_x2 = struct.unpack('>H', self.data[offset+6:offset+8])[0]
        seg_count = seg_count_x2 // 2
        
        end_counts = []
        for i in range(seg_count):
            pos = offset + 14 + i*2
            end_counts.append(struct.unpack('>H', self.data[pos:pos+2])[0])
            
        start_counts = []
        for i in range(seg_count):
            pos = offset + 14 + seg_count_x2 + 2 + i*2
            start_counts.append(struct.unpack('>H', self.data[pos:pos+2])[0])
            
        id_deltas = []
        for i in range(seg_count):
            pos = offset + 14 + seg_count_x2 + 2 + seg_count_x2 + i*2
            id_deltas.append(struct.unpack('>h', self.data[pos:pos+2])[0])
            
        id_range_offsets = []
        id_range_offsets_start = offset + 14 + seg_count_x2 * 3 + 2
        for i in range(seg_count):
            pos = id_range_offsets_start + i*2
            id_range_offsets.append(struct.unpack('>H', self.data[pos:pos+2])[0])

        # Map all chars (this is expensive but done once)
        # To optimize, we could do on-demand, but for <65k chars it's fast enough in Python
        # Iterate segments
        for i in range(seg_count):
            start = start_counts[i]
            end = end_counts[i]
            delta = id_deltas[i]
            range_off = id_range_offsets[i]
            
            if start == 0xFFFF: break
            
            for char_code in range(start, end + 1):
                if range_off == 0:
                    gid = (char_code + delta) & 0xFFFF
                else:
                    # Address calculation based on spec
                    range_off_loc = id_range_offsets_start + i*2
                    glyph_index_addr = range_off_loc + range_off + (char_code - start) * 2
                    if glyph_index_addr >= offset + length:
                        gid = 0
                    else:
                        gid = struct.unpack('>H', self.data[glyph_index_addr:glyph_index_addr+2])[0]
                        if gid != 0:
                            gid = (gid + delta) & 0xFFFF
                
                if gid != 0:
                    self.cmap[char_code] = gid
                    self.gid_to_unicode[gid] = char_code

    def get_gid(self, char_code):
        return self.cmap.get(char_code, 0)

    def get_width(self, gid):
        if gid < len(self.advance_widths):
            return self.advance_widths[gid]
        # Fallback to last known width
        if self.advance_widths:
            return self.advance_widths[-1]
        return 1000 # Fallback default


class SimplePDFGenerator:
    """
    Generador de PDF 1.4 con soporte Unicode real (TTF Embed + Identity-H).
    Reemplaza la implementación anterior WinAnsi para cumplir P0.6.
    """
    def __init__(self):
        self.buffer = io.BytesIO()
        self.pages_content = []
        self.current_content = []
        self.obj_offsets = []
        self.obj_count = 0
        
        # Configuración página Letter (612x792 pt)
        self.page_width = 612
        self.page_height = 792
        self.margin_left = 50
        self.margin_top = 50
        self.y = self.page_height - self.margin_top
        
        # Fuente TTF
        self.font_path = "assets/fonts/DejaVuSans.ttf"
        try:
            self.font = TTFFont(self.font_path)
            self.font_loaded = True
        except Exception as e:
            # Fallback a modo seguro si falla carga (aunque no debería)
            print(f"Error cargando fuente: {e}")
            self.font_loaded = False
            
        self.font_size = 10
        self.line_height = 12
        
        # Tracking de GIDs usados para optimizar PDF (ToUnicode/Widths)
        self.used_gids = set()
        self.used_gids.add(0) # .notdef
        
        # Inicializar primera página
        self._init_page_state()
    
    def _init_page_state(self):
        self.current_content.append(f"BT /F1 {self.font_size} Tf\n".encode('ascii'))

    def _add_page(self):
        if self.current_content:
            self.current_content.append(b"ET\n")
            self.pages_content.append(b"".join(self.current_content))
        self.current_content = []
        self.y = self.page_height - self.margin_top
        self._init_page_state()

    def set_font_size(self, size: int):
        self.font_size = size
        self.line_height = int(size * 1.2)
        if self.current_content:
             self.current_content.append(f"/F1 {self.font_size} Tf\n".encode('ascii'))

    def add_text(self, text: str):
        """Agrega texto manejando saltos de línea y paginación con métricas reales."""
        # 1. Convertir texto a GIDs y calcular anchos
        gids = []
        words = [] # Lista de (palabra_gids, ancho)
        
        # Normalización básica: reemplazar newlines y tabs
        lines = text.split('\n')
        
        scale = self.font_size / self.font.units_per_em if self.font_loaded else 0.001
        max_width = self.page_width - 2 * self.margin_left
        
        for line_text in lines:
            current_line_gids = []
            current_line_width = 0
            
            # Procesar por palabras para wrapping
            # Split manual preservando espacios no es trivial, simplificamos:
            # Vamos caracter a caracter acumulando en linea
            
            # Mejor aproximación: split por espacio
            words_in_line = line_text.split(' ')
            
            for i, word in enumerate(words_in_line):
                word_gids = []
                word_width = 0
                
                # Agregar espacio previo si no es la primera palabra
                if i > 0:
                    space_gid = self.font.get_gid(32)
                    self.used_gids.add(space_gid)
                    w = self.font.get_width(space_gid) * scale
                    word_gids.append(space_gid)
                    word_width += w
                
                # Caracteres de la palabra
                for char in word:
                    gid = self.font.get_gid(ord(char))
                    self.used_gids.add(gid)
                    w = self.font.get_width(gid) * scale
                    word_gids.append(gid)
                    word_width += w
                
                # Check wrap
                if current_line_width + word_width > max_width and current_line_gids:
                    # Flush current line
                    self._write_line_gids(current_line_gids)
                    current_line_gids = []
                    current_line_width = 0
                    # Si era espacio inicial, quitarlo para nueva linea
                    if word_gids and word_gids[0] == self.font.get_gid(32):
                        w_space = self.font.get_width(self.font.get_gid(32)) * scale
                        word_gids.pop(0)
                        word_width -= w_space
                
                current_line_gids.extend(word_gids)
                current_line_width += word_width
                
            self._write_line_gids(current_line_gids)

    def _write_line_gids(self, gids: List[int]):
        if not gids: return
        
        if self.y < self.margin_top:
            self._add_page()
            
        # Convert gids to big-endian hex string
        hex_str = "".join([f"{gid:04X}" for gid in gids])
        
        # Posicionar texto: 1 0 0 1 x y Tm
        cmd = f"1 0 0 1 {self.margin_left} {self.y} Tm <{hex_str}> Tj\n"
        self.current_content.append(cmd.encode('ascii'))
        self.y -= self.line_height

    def get_pdf_bytes(self) -> bytes:
        # Cerrar última página
        if self.current_content:
            self.current_content.append(b"ET\n")
            self.pages_content.append(b"".join(self.current_content))
        
        if not self.pages_content:
            # Pagina vacia dummy
            self.pages_content.append(b"BT /F1 12 Tf ET\n")

        self.buffer = io.BytesIO()
        self.obj_offsets = []
        self.obj_count = 0
        
        def write(data: bytes):
            self.buffer.write(data)

        def start_obj():
            self.obj_count += 1
            self.obj_offsets.append(self.buffer.tell())
            write(f"{self.obj_count} 0 obj\n".encode('ascii'))
            return self.obj_count

        def end_obj():
            write(b"\nendobj\n")

        # Header
        write(b"%PDF-1.4\n%\xE2\xE3\xCF\xD3\n")
        
        # IDs
        catalog_id = 1
        pages_root_id = 2
        font_id = 3
        
        # 1. Catalog
        start_obj() # ID 1
        write(f"<< /Type /Catalog /Pages {pages_root_id} 0 R >>".encode('ascii'))
        end_obj()
        
        num_pages = len(self.pages_content)
        # IDs dinámicos
        # 1: Catalog, 2: Pages, 3: Type0 Font, 4: CIDFont, 5: FontDesc, 6: ToUnicode, 7: FontFile
        cid_font_id = 4
        font_desc_id = 5
        to_unicode_id = 6
        font_file_id = 7
        
        first_page_id = 8
        first_content_id = first_page_id + num_pages
        
        # 2. Pages Root
        start_obj() # ID 2
        kids_refs = [f"{first_page_id + i} 0 R" for i in range(num_pages)]
        write(f"<< /Type /Pages /Kids [{' '.join(kids_refs)}] /Count {num_pages} >>".encode('ascii'))
        end_obj()
        
        # 3. Type0 Font (Composite)
        start_obj() # ID 3
        write(f"""<< 
/Type /Font 
/Subtype /Type0 
/BaseFont /DejaVuSans 
/Encoding /Identity-H 
/DescendantFonts [{cid_font_id} 0 R] 
/ToUnicode {to_unicode_id} 0 R 
>>""".encode('ascii'))
        end_obj()
        
        # 4. CIDFontType2
        start_obj() # ID 4
        # Construir array de anchos (W)
        # Formato: [ first_gid [ w1 w2 ... ] ... ]
        # Para simplificar, agrupamos por rangos consecutivos o dumpamos todo si no es muy grande.
        # Solo necesitamos anchos de los usados.
        sorted_gids = sorted(list(self.used_gids))
        w_array = []
        if sorted_gids:
            # Algoritmo simple: bloques consecutivos
            current_block = []
            block_start = sorted_gids[0]
            prev_gid = block_start - 1
            
            for gid in sorted_gids:
                if gid != prev_gid + 1:
                    # Cerrar bloque anterior
                    w_array.append(f"{block_start} [{' '.join(map(str, current_block))}]")
                    current_block = []
                    block_start = gid
                
                current_block.append(self.font.get_width(gid))
                prev_gid = gid
            
            if current_block:
                w_array.append(f"{block_start} [{' '.join(map(str, current_block))}]")
        
        w_str = " ".join(w_array)
        
        write(f"""<< 
/Type /Font 
/Subtype /CIDFontType2 
/BaseFont /DejaVuSans 
/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> 
/FontDescriptor {font_desc_id} 0 R 
/DW 1000 
/W [{w_str}] 
>>""".encode('ascii'))
        end_obj()
        
        # 5. FontDescriptor
        start_obj() # ID 5
        # Flags 4 = Symbolic
        write(f"""<< 
/Type /FontDescriptor 
/FontName /DejaVuSans 
/Flags 4 
/FontBBox [{self.font.bbox[0]} {self.font.bbox[1]} {self.font.bbox[2]} {self.font.bbox[3]}] 
/ItalicAngle 0 
/Ascent {self.font.ascent} 
/Descent {self.font.descent} 
/CapHeight {self.font.cap_height} 
/StemV 80 
/FontFile2 {font_file_id} 0 R 
>>""".encode('ascii'))
        end_obj()
        
        # 6. ToUnicode CMap
        start_obj() # ID 6
        # Generar CMap para copiar texto
        cmap_lines = []
        cmap_lines.append("/CIDInit /ProcSet findresource begin")
        cmap_lines.append("12 dict begin")
        cmap_lines.append("begincmap")
        cmap_lines.append("/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def")
        cmap_lines.append("/CMapName /Adobe-Identity-UCS def")
        cmap_lines.append("/CMapType 2 def")
        cmap_lines.append("1 begincodespacerange")
        cmap_lines.append("<0000> <FFFF>")
        cmap_lines.append("endcodespacerange")
        
        # bfchar lines
        # Group in chunks of 100
        chunk_size = 100
        gids_list = list(self.used_gids)
        for i in range(0, len(gids_list), chunk_size):
            chunk = gids_list[i:i+chunk_size]
            cmap_lines.append(f"{len(chunk)} beginbfchar")
            for gid in chunk:
                uni = self.font.gid_to_unicode.get(gid, 0)
                # UTF-16BE hex
                uni_hex = f"{uni:04X}"
                cmap_lines.append(f"<{gid:04X}> <{uni_hex}>")
            cmap_lines.append("endbfchar")
            
        cmap_lines.append("endcmap")
        cmap_lines.append("CMapName currentdict /CMap defineresource pop")
        cmap_lines.append("end")
        cmap_lines.append("end")
        
        cmap_data = "\n".join(cmap_lines).encode('ascii')
        write(f"<< /Length {len(cmap_data)} >>\nstream\n".encode('ascii'))
        write(cmap_data)
        write(b"\nendstream")
        end_obj()
        
        # 7. FontFile2 (Embedded TTF)
        start_obj() # ID 7
        write(f"<< /Length {len(self.font.data)} >>\nstream\n".encode('ascii'))
        write(self.font.data)
        write(b"\nendstream")
        end_obj()
        
        # Pages and Content
        for i, content in enumerate(self.pages_content):
            page_id = first_page_id + i
            content_id = first_content_id + i
            
            # Page Object
            start_obj() 
            write(f"<< /Type /Page /Parent {pages_root_id} 0 R /MediaBox [0 0 {self.page_width} {self.page_height}] /Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode('ascii'))
            end_obj()
            
        for i, content in enumerate(self.pages_content):
            # Content Stream Object
            start_obj() 
            write(f"<< /Length {len(content)} >>\nstream\n".encode('ascii'))
            write(content)
            write(b"\nendstream")
            end_obj()
            
        # Xref
        xref_offset = self.buffer.tell()
        write(b"xref\n")
        write(f"0 {self.obj_count + 1}\n".encode('ascii'))
        write(b"0000000000 65535 f \n")
        for offset in self.obj_offsets:
            write(f"{offset:010d} 00000 n \n".encode('ascii'))
            
        # Trailer
        write(b"trailer\n")
        write(f"<< /Size {self.obj_count + 1} /Root {catalog_id} 0 R >>\n".encode('ascii'))
        write(b"startxref\n")
        write(f"{xref_offset}\n".encode('ascii'))
        write(b"%%EOF\n")
        
        return self.buffer.getvalue()



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
    friendly_intro: bool = False # DESLINDE PATCH: friendly intro


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
            # Insertar sin forzar ID para evitar colisiones en seeds repetidos
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
# Endpoints del MVP
# ------------------------------------------------------------------------------
@app.get("/e/{evento_id}", response_class=HTMLResponse)
def mostrar_formulario(evento_id: int, request: Request) -> HTMLResponse:
    """
    Muestra el formulario de aceptación para un evento.
    - Si el evento no existe, retorna 404.
    - Si el evento está inactivo, muestra el formulario deshabilitado.
    - Carga deslinde desde archivo según versión configurada.
    """
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    # Normaliza booleano 'activo' (0/1 en SQLite)
    evento["activo"] = bool(evento["activo"])
    evento["req_firma"] = bool(evento.get("req_firma", 0))
    evento["req_documento"] = bool(evento.get("req_documento", 0))
    evento["req_audio"] = bool(evento.get("req_audio", 0))
    evento["req_salud"] = bool(evento.get("req_salud", 0))
    evento["friendly_intro"] = bool(evento.get("friendly_intro", 0)) # DESLINDE PATCH: friendly intro

    # Obtener texto del deslinde
    deslinde_custom = evento.get("deslinde_texto")
    if deslinde_custom and deslinde_custom.strip():
        # Deslinde personalizado por evento
        texto_final = deslinde_custom
    else:
        # Deslinde estándar según versión
        version = evento.get("deslinde_version") or DEFAULT_DESLINDE_VERSION
        texto_base = cargar_deslinde(version)
        # Reemplazar placeholders dinámicos solo en el estándar (o en ambos? Mejor solo estándar por ahora para cumplir "renderizar ese texto")
        # El usuario dijo "renderizar ese texto", no mencionó placeholders. 
        # Si el usuario pone texto fijo, es fijo.
        texto_final = texto_base.replace("{{NOMBRE_EVENTO}}", evento["nombre"])\
                                .replace("{{ORGANIZADOR}}", evento["organizador"])

    template = templates_env.get_template("evento_form.html")
    html = template.render(
        evento=evento, 
        request=request, 
        deslinde_texto=texto_final,
        MAX_IMAGE_DOC_MB=MAX_IMAGE_DOC_MB,
        MAX_FIRMA_MB=MAX_FIRMA_MB,
        MAX_AUDIO_MB=MAX_AUDIO_MB,
        MAX_IMAGE_COMPRESS_THRESHOLD_MB=MAX_IMAGE_COMPRESS_THRESHOLD_MB
    )
    return HTMLResponse(content=html)


@app.post("/e/{evento_id}", response_class=HTMLResponse)
def procesar_aceptacion(
    evento_id: int,
    request: Request,
    nombre_participante: str = Form(...),
    documento: str = Form(...),
    acepto: Optional[str] = Form(None),
    firma_base64: Optional[str] = Form(None),
    doc_frente: Optional[UploadFile] = File(None),
    doc_dorso: Optional[UploadFile] = File(None),
    salud_doc: Optional[UploadFile] = File(None),
    audio_base64: Optional[str] = Form(None),
    salud_doc_tipo: Optional[str] = Form(None),
    audio_exento: Optional[int] = Form(0),
    firma_asistida: Optional[int] = Form(0),
) -> HTMLResponse:
    """
    Procesa el formulario de aceptación:
    - Verifica existencia y estado del evento
    - Requiere checkbox 'acepto' marcado
    - Guarda registro en SQLite con IP y User-Agent
    - Normaliza documento
    - Usa fecha/hora UTC con sufijo 'Z'
    - Asocia el hash del deslinde activo aceptado
    - Guarda firma manuscrita si el evento lo requiere
    - Guarda imágenes de documento si el evento lo requiere
    - Guarda audio de aceptación si el evento lo requiere
    - Renderiza confirmación
    """
    # Generar request_id único para trazabilidad
    request_id = str(uuid.uuid4())[:8]
    
    try:
        app_logger.info(f"[{request_id}] Inicio procesamiento aceptación - evento_id={evento_id}")
        
        evento = get_evento(evento_id)
        if not evento:
            app_logger.warning(f"[{request_id}] Evento no encontrado: evento_id={evento_id}")
            raise HTTPException(status_code=404, detail="Evento no encontrado")
        if not bool(evento["activo"]):
            app_logger.warning(f"[{request_id}] Evento inactivo: evento_id={evento_id}")
            raise HTTPException(status_code=400, detail="Evento inactivo")
        # Validación del checkbox (HTML ya tiene required, pero validamos servidor)
        if acepto is None:
            app_logger.warning(f"[{request_id}] Checkbox acepto no marcado")
            raise HTTPException(status_code=400, detail="Debe aceptar el deslinde")

        # Validación de firma
        req_firma = bool(evento.get("req_firma", 0))
        if req_firma and not firma_base64:
             raise HTTPException(status_code=400, detail="La firma manuscrita es obligatoria")
             
        # Validación de documento
        req_documento = bool(evento.get("req_documento", 0))
        if req_documento:
            if not doc_frente or not doc_frente.filename:
                raise HTTPException(status_code=400, detail="La foto del frente del documento es obligatoria")
            if not doc_dorso or not doc_dorso.filename:
                raise HTTPException(status_code=400, detail="La foto del dorso del documento es obligatoria")
            
            # Validación de tamaño backend (defensiva) - esta validación se hace después en procesamiento
            # pero mantenemos aquí como validación temprana
            try:
                doc_frente.file.seek(0, os.SEEK_END)
                size_frente = doc_frente.file.tell()
                doc_frente.file.seek(0)
                
                doc_dorso.file.seek(0, os.SEEK_END)
                size_dorso = doc_dorso.file.tell()
                doc_dorso.file.seek(0)
                
                max_bytes_img = MAX_IMAGE_DOC_MB * 1024 * 1024
                if size_frente > max_bytes_img or size_dorso > max_bytes_img:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Las imágenes no deben superar {MAX_IMAGE_DOC_MB} MB cada una."
                    )
            except HTTPException:
                raise
            except Exception:
                # Si falla la verificación de tamaño, continuamos (validación más estricta después)
                pass

        req_salud = bool(evento.get("req_salud", 0))
        if req_salud:
            if not salud_doc or not salud_doc.filename:
                raise HTTPException(status_code=400, detail="El documento de salud es obligatorio")
            if not salud_doc_tipo:
                raise HTTPException(status_code=400, detail="Debe seleccionar el tipo de documento de salud")
            try:
                salud_doc.file.seek(0, os.SEEK_END)
                salud_size = salud_doc.file.tell()
                salud_doc.file.seek(0)
                max_bytes_img = MAX_IMAGE_DOC_MB * 1024 * 1024
                if salud_size > max_bytes_img:
                    raise HTTPException(
                        status_code=413,
                        detail=f"El documento de salud no debe superar {MAX_IMAGE_DOC_MB} MB."
                    )
            except HTTPException:
                raise
            except Exception:
                pass

        # Validación de audio
        req_audio = bool(evento.get("req_audio", 0))
        if req_audio:
            if audio_exento == 1:
                app_logger.info(f"[{request_id}] Audio exento por imposibilidad física")
            elif not audio_base64:
                raise HTTPException(status_code=400, detail="El audio de aceptación es obligatorio")

        # Metadatos del cliente
        ip = request.client.host if request.client else "0.0.0.0"
        user_agent = request.headers.get("user-agent", "")
        fecha_hora = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        # Normalización de documento: quitar puntos, guiones y espacios; a mayúsculas
        documento_norm = normalizar_documento_helper(documento)
        
        # TAREA 1: Validación de duplicados defensiva
        conn = get_connection()
        try:
            if aceptacion_existente(conn, evento_id, documento_norm):
                app_logger.warning(f"[{request_id}] Intento de duplicado bloqueado: evento={evento_id}, doc={documento_norm}")
                raise HTTPException(status_code=400, detail="Ya existe una aceptación registrada para este documento en este evento.")
        finally:
            conn.close()

        # Obtiene texto y hash del deslinde que se está aceptando
        deslinde_custom = evento.get("deslinde_texto")
        if deslinde_custom and deslinde_custom.strip():
             texto_final = deslinde_custom
        else:
             version = evento.get("deslinde_version") or DEFAULT_DESLINDE_VERSION
             texto_base = cargar_deslinde(version)
             texto_final = texto_base.replace("{{NOMBRE_EVENTO}}", evento["nombre"])\
                                     .replace("{{ORGANIZADOR}}", evento["organizador"])
        
        deslinde_hash_sha256 = calcular_hash_sha256(texto_final)
        
        # Procesamiento de firma
        firma_path_final = None
        if firma_base64:
            # data:image/png;base64,.....
            # Separar encabezado si existe
            if "," in firma_base64:
                header, encoded = firma_base64.split(",", 1)
            else:
                encoded = firma_base64
            
            try:
                data = base64.b64decode(encoded)
                
                # Validación tamaño firma (prevención 413)
                firma_size = len(data)
                max_firma_bytes = MAX_FIRMA_MB * 1024 * 1024
                if firma_size > max_firma_bytes:
                    app_logger.warning(f"[{request_id}] Firma demasiado grande: {firma_size} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"La firma es demasiado grande. Máximo permitido: {MAX_FIRMA_MB} MB. Por favor, firme más pequeña."
                    )
                
                filename = f"{uuid.uuid4()}.png"
                filepath = os.path.join(FIRMAS_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(data)
                firma_path_final = filepath
                app_logger.info(f"[{request_id}] Firma guardada: path={filepath}, size={firma_size} bytes")
            except HTTPException:
                raise
            except Exception:
                # Si falla guardar la firma y es requerida, error.
                if req_firma:
                    raise HTTPException(status_code=500, detail="Error al guardar la firma")
                # Si no es requerida pero vino data corrupta, se ignora o se loguea.
            
        # Procesamiento de documentos
        doc_frente_path_final = None
        doc_dorso_path_final = None
        
        if req_documento and doc_frente and doc_dorso:
            try:
                # Validación tamaño documentos (prevención 413) - ANTES de guardar
                max_doc_bytes = MAX_IMAGE_DOC_MB * 1024 * 1024
                
                # Validar frente
                doc_frente.file.seek(0, os.SEEK_END)
                size_frente = doc_frente.file.tell()
                doc_frente.file.seek(0)
                if size_frente > max_doc_bytes:
                    app_logger.warning(f"[{request_id}] Doc frente demasiado grande: {size_frente} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"La imagen del frente es demasiado grande. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                    )
            
                # Validar dorso
                doc_dorso.file.seek(0, os.SEEK_END)
                size_dorso = doc_dorso.file.tell()
                doc_dorso.file.seek(0)
                if size_dorso > max_doc_bytes:
                    app_logger.warning(f"[{request_id}] Doc dorso demasiado grande: {size_dorso} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"La imagen del dorso es demasiado grande. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                    )
            
                # Frente
                ext_frente = os.path.splitext(doc_frente.filename)[1]
                if not ext_frente: ext_frente = ".jpg"
                filename_frente = f"{uuid.uuid4()}_frente{ext_frente}"
                filepath_frente = os.path.join(DOCUMENTOS_DIR, filename_frente)
                with open(filepath_frente, "wb") as buffer:
                    shutil.copyfileobj(doc_frente.file, buffer)
            
                # Comprimir si es necesario (si supera 2MB)
                if size_frente > MAX_IMAGE_COMPRESS_THRESHOLD_MB * 1024 * 1024:
                    app_logger.info(f"[{request_id}] Comprimiendo doc frente: {size_frente} bytes")
                    compressed = comprimir_imagen(filepath_frente, MAX_IMAGE_COMPRESS_TARGET_MB)
                    if not compressed:
                        # Si no se pudo comprimir, rechazar
                        os.remove(filepath_frente)
                        app_logger.error(f"[{request_id}] No se pudo comprimir doc frente")
                        raise HTTPException(
                            status_code=413,
                            detail=f"La imagen del frente es demasiado grande y no se pudo comprimir. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                        )
                    final_size_frente = os.path.getsize(filepath_frente)
                    app_logger.info(f"[{request_id}] Doc frente comprimido: {size_frente} -> {final_size_frente} bytes")
                else:
                    final_size_frente = size_frente
            
                doc_frente_path_final = filepath_frente
                app_logger.info(f"[{request_id}] Doc frente guardado: path={filepath_frente}, size={final_size_frente} bytes")
            
                # Dorso
                ext_dorso = os.path.splitext(doc_dorso.filename)[1]
                if not ext_dorso: ext_dorso = ".jpg"
                filename_dorso = f"{uuid.uuid4()}_dorso{ext_dorso}"
                filepath_dorso = os.path.join(DOCUMENTOS_DIR, filename_dorso)
                with open(filepath_dorso, "wb") as buffer:
                    shutil.copyfileobj(doc_dorso.file, buffer)
            
                # Comprimir si es necesario
                if size_dorso > MAX_IMAGE_COMPRESS_THRESHOLD_MB * 1024 * 1024:
                    app_logger.info(f"[{request_id}] Comprimiendo doc dorso: {size_dorso} bytes")
                    compressed = comprimir_imagen(filepath_dorso, MAX_IMAGE_COMPRESS_TARGET_MB)
                    if not compressed:
                        # Si no se pudo comprimir, rechazar
                        os.remove(filepath_dorso)
                        if doc_frente_path_final and os.path.exists(doc_frente_path_final):
                            os.remove(doc_frente_path_final)
                        app_logger.error(f"[{request_id}] No se pudo comprimir doc dorso")
                        raise HTTPException(
                            status_code=413,
                            detail=f"La imagen del dorso es demasiado grande y no se pudo comprimir. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                        )
                    final_size_dorso = os.path.getsize(filepath_dorso)
                    app_logger.info(f"[{request_id}] Doc dorso comprimido: {size_dorso} -> {final_size_dorso} bytes")
                else:
                    final_size_dorso = size_dorso
            
                doc_dorso_path_final = filepath_dorso
                app_logger.info(f"[{request_id}] Doc dorso guardado: path={filepath_dorso}, size={final_size_dorso} bytes")
                
            except HTTPException:
                raise
            except Exception as e:
                # Limpiar archivos parciales en caso de error
                if doc_frente_path_final and os.path.exists(doc_frente_path_final):
                    try:
                        os.remove(doc_frente_path_final)
                    except:
                        pass
                if doc_dorso_path_final and os.path.exists(doc_dorso_path_final):
                    try:
                        os.remove(doc_dorso_path_final)
                    except:
                        pass
                raise HTTPException(status_code=500, detail="Error al guardar las imágenes del documento")

        salud_doc_path_final = None
        if req_salud and salud_doc:
            try:
                max_doc_bytes = MAX_IMAGE_DOC_MB * 1024 * 1024

                salud_doc.file.seek(0, os.SEEK_END)
                salud_size = salud_doc.file.tell()
                salud_doc.file.seek(0)
                if salud_size > max_doc_bytes:
                    app_logger.warning(f"[{request_id}] Doc salud demasiado grande: {salud_size} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"El documento de salud es demasiado grande. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                    )

                ext_salud = os.path.splitext(salud_doc.filename)[1]
                if not ext_salud:
                    ext_salud = ".jpg"
                filename_salud = f"{uuid.uuid4()}{ext_salud}"
                filepath_salud = os.path.join(SALUD_DIR, filename_salud)
                with open(filepath_salud, "wb") as buffer:
                    shutil.copyfileobj(salud_doc.file, buffer)

                if salud_size > MAX_IMAGE_COMPRESS_THRESHOLD_MB * 1024 * 1024:
                    app_logger.info(f"[{request_id}] Comprimiendo doc salud: {salud_size} bytes")
                    compressed = comprimir_imagen(filepath_salud, MAX_IMAGE_COMPRESS_TARGET_MB)
                    if not compressed:
                        os.remove(filepath_salud)
                        app_logger.error(f"[{request_id}] No se pudo comprimir doc salud")
                        raise HTTPException(
                            status_code=413,
                            detail=f"El documento de salud es demasiado grande y no se pudo comprimir. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                        )
                    final_size_salud = os.path.getsize(filepath_salud)
                    app_logger.info(f"[{request_id}] Doc salud comprimido: {salud_size} -> {final_size_salud} bytes")
                else:
                    final_size_salud = salud_size

                salud_doc_path_final = filepath_salud
                app_logger.info(f"[{request_id}] Doc salud guardado: path={filepath_salud}, size={final_size_salud} bytes")
            except HTTPException:
                raise
            except Exception:
                if salud_doc_path_final and os.path.exists(salud_doc_path_final):
                    try:
                        os.remove(salud_doc_path_final)
                    except Exception:
                        pass
                raise HTTPException(status_code=500, detail="Error al guardar el documento de salud")

        # Procesamiento de audio
        audio_path_final = None
        if audio_base64:
            # data:audio/webm;base64,.....
            header = ""
            if "," in audio_base64:
                header, encoded = audio_base64.split(",", 1)
            else:
                encoded = audio_base64
            
            try:
                data = base64.b64decode(encoded)
                
                # Validación tamaño audio backend (prevención 413)
                max_audio_bytes = MAX_AUDIO_MB * 1024 * 1024
                audio_size = len(data)
                if audio_size > max_audio_bytes:
                    app_logger.warning(f"[{request_id}] Audio demasiado grande: {audio_size} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"El audio es demasiado grande. Máximo permitido: {MAX_AUDIO_MB} MB. Por favor, intente ser más breve."
                    )
                
                # Extensión default
                ext = ".webm"
                if "audio/mp3" in header: ext = ".mp3"
                elif "audio/wav" in header: ext = ".wav"
                elif "audio/ogg" in header: ext = ".ogg"
                elif "audio/mp4" in header: ext = ".mp4"
                
                filename_audio = f"{uuid.uuid4()}{ext}"
                filepath_audio = os.path.join(AUDIOS_DIR, filename_audio)
                with open(filepath_audio, "wb") as f:
                    f.write(data)
                audio_path_final = filepath_audio
                app_logger.info(f"[{request_id}] Audio guardado: path={filepath_audio}, size={audio_size} bytes")
            except HTTPException:
                raise
            except Exception:
                if req_audio and audio_exento != 1:
                    raise HTTPException(status_code=500, detail="Error al guardar el audio")
                app_logger.error(f"[{request_id}] Error no bloqueante al guardar audio: {traceback.format_exc()}")
        
        # Generar token público para descarga de PDF
        pdf_token = secrets.token_urlsafe(32)

        aceptacion_id = insertar_aceptacion(
            evento_id=evento_id,
            nombre_participante=nombre_participante.strip(),
            documento=documento.strip(),
            fecha_hora=fecha_hora,
            ip=ip,
            user_agent=user_agent,
            deslinde_hash_sha256=deslinde_hash_sha256,
            firma_path=firma_path_final,
            doc_frente_path=doc_frente_path_final,
            doc_dorso_path=doc_dorso_path_final,
            audio_path=audio_path_final,
            salud_doc_path=salud_doc_path_final,
            salud_doc_tipo=salud_doc_tipo,
            audio_exento=audio_exento or 0,
            firma_asistida=firma_asistida or 0,
            pdf_token=pdf_token,
            documento_norm=documento_norm,
            deslinde_version=version,
        )
        
        # Log final con todos los datos
        app_logger.info(
            f"[{request_id}] Aceptación guardada exitosamente - "
            f"aceptacion_id={aceptacion_id}, evento_id={evento_id}, pdf_token={pdf_token[:8]}..., "
            f"firma_path={firma_path_final}, doc_frente_path={doc_frente_path_final}, "
            f"doc_dorso_path={doc_dorso_path_final}, audio_path={audio_path_final}, salud_doc_path={salud_doc_path_final}"
        )

        template = templates_env.get_template("confirmacion.html")
        html = template.render(
            nombre_participante=nombre_participante,
            evento=evento,
            aceptacion_id=aceptacion_id,
            fecha_hora=fecha_hora,
            pdf_token=pdf_token,
        )
        return HTMLResponse(content=html)
    except HTTPException:
        raise
    except Exception as e:
        # Loggear cualquier excepción con stacktrace
        app_logger.error(
            f"[{request_id}] Excepción en procesar_aceptacion - evento_id={evento_id}: {str(e)}\n"
            f"{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail="Error interno del servidor")


# ADMIN PATCH: fix admin home auth (moved security block up)
# ADMIN PATCH: admin home v1
@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/home", response_class=HTMLResponse)
def admin_home(username: str = Depends(get_current_username)) -> HTMLResponse:
    """Dashboard principal de administración."""
    
    # Template embebido para el home admin
    html_content = """
    <!doctype html>
    <html lang="es">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Admin Dashboard - EncarreraOK</title>
        <style>
            body { 
                font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; 
                margin: 0; 
                background: #f4f6f9; 
                color: #333;
            }
            .header {
                background: #fff;
                padding: 1rem 1.5rem;
                border-bottom: 1px solid #ddd;
                display: flex;
                align-items: center;
                justify-content: space-between;
                box-shadow: 0 1px 2px rgba(0,0,0,0.05);
            }
            .header h1 { margin: 0; font-size: 1.25rem; color: #1a1a1a; }
            .user-info { font-size: 0.9rem; color: #666; }
            
            .container {
                max-width: 1000px;
                margin: 2rem auto;
                padding: 0 1.5rem;
            }
            
            .welcome-text {
                margin-bottom: 2rem;
                color: #555;
            }

            .grid {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                gap: 1.5rem;
            }

            .card {
                background: white;
                border-radius: 8px;
                padding: 1.5rem;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                transition: transform 0.2s, box-shadow 0.2s;
                text-decoration: none;
                color: inherit;
                border: 1px solid transparent;
                display: flex;
                flex-direction: column;
                height: 100%;
                box-sizing: border-box;
            }
            
            .card:hover {
                transform: translateY(-2px);
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                border-color: #b0c4de;
            }

            .card-icon {
                font-size: 2rem;
                margin-bottom: 1rem;
            }
            
            .card-title {
                font-size: 1.1rem;
                font-weight: 600;
                margin-bottom: 0.5rem;
                color: #0d6efd;
            }
            
            .card-desc {
                font-size: 0.9rem;
                color: #666;
                line-height: 1.4;
                flex-grow: 1;
            }
            
            .card-action {
                margin-top: 1rem;
                font-size: 0.9rem;
                font-weight: 500;
                color: #0d6efd;
                display: flex;
                align-items: center;
            }
            .card-action::after {
                content: "→";
                margin-left: 5px;
                transition: margin-left 0.2s;
            }
            .card:hover .card-action::after {
                margin-left: 8px;
            }

            .card.disabled {
                opacity: 0.6;
                cursor: default;
                background: #f8f9fa;
            }
            .card.disabled:hover {
                transform: none;
                box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                border-color: transparent;
            }
            .card.disabled .card-title { color: #6c757d; }
            .card.disabled .card-action { display: none; }
            
            .badge {
                display: inline-block;
                padding: 2px 8px;
                font-size: 0.75rem;
                background: #e9ecef;
                color: #495057;
                border-radius: 10px;
                margin-bottom: 0.5rem;
            }

        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1 style="margin:0; font-size: 1.5rem;">EncarreraOK <span style="font-weight:normal; font-size:1rem; color:#666;">Admin</span></h1>
                <!-- BRAND PATCH: add logo -->
                <img src="/assets/logo-encarreraok.png" alt="EncarreraOK" style="max-width:180px; width:100%; height:auto; margin-bottom:12px;">
                <div style="font-size: 0.8rem; color: #666; text-transform: uppercase; letter-spacing: 1px;">Evidencia clara. Eventos seguros.</div>
            </div>
            <div class="user-info">Usuario: <strong>{{ username }}</strong></div>
        </div>

        <div class="container">
            <div class="welcome-text">
                <p>Bienvenido al panel de control. Seleccione una opción para gestionar el sistema.</p>
            </div>

            <div class="grid">
                <!-- Gestión de Eventos -->
                <a href="/admin/eventos" class="card">
                    <div class="card-icon">📅</div>
                    <div class="card-title">Gestión de Eventos</div>
                    <div class="card-desc">Crear, editar y configurar eventos activos. Obtener enlaces públicos.</div>
                    <div class="card-action">Ir a Eventos</div>
                </a>

                <!-- Aceptaciones -->
                <a href="/admin/aceptaciones" class="card">
                    <div class="card-icon">📝</div>
                    <div class="card-title">Aceptaciones</div>
                    <div class="card-desc">Listado completo de deslindes firmados. Filtrar, buscar y verificar evidencias.</div>
                    <div class="card-action">Ver Registros</div>
                </a>

                <!-- Exportes -->
                <a href="/admin/eventos" class="card">
                    <div class="card-icon">📦</div>
                    <div class="card-title">Exportes Legales</div>
                    <div class="card-desc">Descargar paquetes ZIP con PDFs firmados, evidencias y manifiestos de auditoría.</div>
                    <div class="card-action">Ir a Descargas</div>
                </a>

                <!-- Monitor en Vivo (si se tiene el ID, aquí linkeamos al listado para elegir) -->
                <a href="/admin/eventos" class="card">
                    <div class="card-icon">📺</div>
                    <div class="card-title">Monitor de Entrada</div>
                    <div class="card-desc">Pantalla de validación en tiempo real para operadores de acceso.</div>
                    <div class="card-action">Seleccionar Evento</div>
                </a>
                
                <!-- Búsqueda Global -->
                <a href="/admin/search" class="card">
                    <div class="card-icon">🔍</div>
                    <div class="card-title">Búsqueda Global</div>
                    <div class="card-desc">Buscar deslindes por DNI o apellido en todos los eventos históricos.</div>
                    <div class="card-action">Buscar ahora</div>
                </a>

                <div class="card disabled">
                    <div class="card-icon">📊</div>
                    <span class="badge">Próximamente</span>
                    <div class="card-title">Estado del Sistema</div>
                    <div class="card-desc">Métricas de disco, uso de CPU y estado de servicios.</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    # Renderizamos simple con replace del username (sin cargar template externo para mantenerlo autocontenido)
    return HTMLResponse(content=html_content.replace("{{ username }}", username))
# /ADMIN PATCH

# ADMIN PATCH: admin search deslindes
@app.get("/admin/search", response_class=HTMLResponse)
def admin_search(q: Optional[str] = None, username: str = Depends(get_current_username)) -> HTMLResponse:
    """Búsqueda transversal de deslindes."""
    resultados = []
    if q:
        # Reutilizamos listar_aceptaciones que ya tiene lógica de búsqueda
        # y limitamos a 50 resultados para performance
        resultados = listar_aceptaciones(query=q)[:50]
    
    template = templates_env.get_template("admin_busqueda_deslindes.html")
    html = template.render(query=q, resultados=resultados, username=username)
    return HTMLResponse(content=html)
# /ADMIN PATCH




@app.get("/admin/eventos", response_class=HTMLResponse)
def admin_eventos(username: str = Depends(get_current_username)) -> HTMLResponse:
    """Listado de eventos para administración."""
    eventos = listar_eventos()
    template = templates_env.get_template("admin_eventos_lista.html")
    html = template.render(eventos=eventos, username=username)
    return HTMLResponse(content=html)


@app.get("/admin/eventos/nuevo", response_class=HTMLResponse)
def admin_evento_nuevo_form(username: str = Depends(get_current_username)) -> HTMLResponse:
    """Formulario para crear evento."""
    template = templates_env.get_template("admin_eventos_form.html")
    html = template.render(evento=None, username=username)
    return HTMLResponse(content=html)


@app.post("/admin/eventos/nuevo")
def admin_evento_nuevo_post(
    nombre: str = Form(...),
    fecha: str = Form(...),
    organizador: str = Form(...),
    activo: Optional[int] = Form(0),
    req_firma: Optional[int] = Form(0),
    req_documento: Optional[int] = Form(0),
    req_salud: Optional[int] = Form(0),
    req_audio: Optional[int] = Form(0),
    friendly_intro: Optional[int] = Form(0), # DESLINDE PATCH: friendly intro
    deslinde_version: str = Form("v1_1"),
    username: str = Depends(get_current_username)
):
    """Procesa creación de evento."""
    from fastapi.responses import RedirectResponse
    try:
        # Validaciones básicas
        if not nombre.strip() or not organizador.strip():
            raise HTTPException(status_code=400, detail="Nombre y organizador son obligatorios")
        
        # Validar fecha ISO
        try:
            datetime.strptime(fecha, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido (YYYY-MM-DD)")
            
        if deslinde_version not in ["v1_1", "v2_0", "v3_0"]:
            raise HTTPException(status_code=400, detail="Versión de deslinde inválida")

        crear_evento(
            nombre=nombre.strip(),
            fecha=fecha,
            organizador=organizador.strip(),
            activo=activo or 0,
            req_firma=req_firma or 0,
            req_documento=req_documento or 0,
        req_salud=req_salud or 0,
        req_audio=req_audio or 0,
        deslinde_version=deslinde_version,
        friendly_intro=friendly_intro or 0 # DESLINDE PATCH: friendly intro
    )
    
    except Exception as e:
        app_logger.error(f"Error creando evento: {e}")
        raise HTTPException(status_code=500, detail=f"Error creando evento: {e}")

    # ADMIN PATCH: fix try except syntax
    return RedirectResponse(url="/admin/eventos", status_code=303)


@app.get("/admin/eventos/{evento_id}/editar", response_class=HTMLResponse)
def admin_evento_editar_form(evento_id: int, username: str = Depends(get_current_username)) -> HTMLResponse:
    """Formulario para editar evento."""
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
        
    template = templates_env.get_template("admin_eventos_form.html")
    html = template.render(evento=evento, username=username)
    return HTMLResponse(content=html)


@app.post("/admin/eventos/{evento_id}/editar")
def admin_evento_editar_post(
    evento_id: int,
    nombre: str = Form(...),
    fecha: str = Form(...),
    organizador: str = Form(...),
    activo: Optional[int] = Form(0),
    req_firma: Optional[int] = Form(0),
    req_documento: Optional[int] = Form(0),
    req_salud: Optional[int] = Form(0),
    req_audio: Optional[int] = Form(0),
    friendly_intro: Optional[int] = Form(0), # DESLINDE PATCH: friendly intro
    deslinde_version: str = Form(...),
    username: str = Depends(get_current_username)
):
    """Procesa edición de evento."""
    from fastapi.responses import RedirectResponse
    try:
        # Validaciones
        if not nombre.strip() or not organizador.strip():
             raise HTTPException(status_code=400, detail="Nombre y organizador son obligatorios")
             
        try:
            datetime.strptime(fecha, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido")
            
        if deslinde_version not in ["v1_1", "v2_0", "v3_0"]:
            raise HTTPException(status_code=400, detail="Versión de deslinde inválida")

        actualizar_evento(
            evento_id=evento_id,
            nombre=nombre.strip(),
            fecha=fecha,
            organizador=organizador.strip(),
            activo=activo or 0,
            req_firma=req_firma or 0,
            req_documento=req_documento or 0,
            req_salud=req_salud or 0,
            req_audio=req_audio or 0,
            deslinde_version=deslinde_version,
            friendly_intro=friendly_intro or 0 # DESLINDE PATCH: friendly intro
        )
        
        return RedirectResponse(url="/admin/eventos", status_code=303)
    except Exception as e:
        app_logger.error(f"Error editando evento {evento_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error editando evento: {e}")


@app.get("/admin/aceptaciones", response_class=HTMLResponse)
def admin_aceptaciones(
    evento_id: Optional[int] = None,
    username: str = Depends(get_current_username)
) -> HTMLResponse:
    """
    Lista de aceptaciones.
    - Requiere autenticación Basic Auth.
    - Ordenadas por ID descendente.
    - Soporta filtrado por evento_id.
    """
    datos = listar_aceptaciones(evento_id=evento_id)
    eventos = listar_eventos()
    
    # Prepara contexto para la plantilla
    context = {
        "aceptaciones": datos,
        "eventos": eventos,
        "filtro_evento_id": evento_id,
        "username": username
    }
    
    template = templates_env.get_template("admin_aceptaciones.html")
    html = template.render(**context)
    return HTMLResponse(content=html)


def _generar_bytes_pdf(aceptacion: Dict[str, Any], evento: Dict[str, Any]) -> bytes:
    """Helper para generar el PDF legal de una aceptación."""
    # Reconstruir texto deslinde
    version = evento.get("deslinde_version") or DEFAULT_DESLINDE_VERSION
    texto_base = cargar_deslinde(version)
    texto_final = texto_base.replace("{{NOMBRE_EVENTO}}", evento["nombre"])\
                            .replace("{{ORGANIZADOR}}", evento["organizador"])

    # Verificación de consistencia de hash
    hash_calculado = calcular_hash_sha256(texto_final)
    hash_bd = aceptacion['deslinde_hash_sha256']
    consistencia = "OK" if hash_calculado == hash_bd else "NO COINCIDE"

    if consistencia != "OK":
        app_logger.warning(f"Inconsistencia de hash detectada - AceptacionID: {aceptacion['id']}, EventoID: {evento['id']}. BD: {hash_bd}, Calc: {hash_calculado}")

    # Generar PDF
    pdf = SimplePDFGenerator()
    
    # Encabezado
    pdf.set_font_size(14)
    pdf.add_text("ACEPTACIÓN DE DESLINDE DE RESPONSABILIDAD")
    pdf.set_font_size(10)
    pdf.add_text(f"ID Aceptación: {aceptacion['id']}")
    pdf.add_text(f"Fecha y hora de generación del documento (UTC): {datetime.utcnow().replace(microsecond=0).isoformat()}Z")
    pdf.add_text("\n")
    
    # Evento
    pdf.set_font_size(12)
    pdf.add_text("EVENTO")
    pdf.set_font_size(10)
    pdf.add_text(f"Nombre: {evento['nombre']}")
    pdf.add_text(f"Fecha: {evento['fecha']}")
    pdf.add_text(f"Organizador: {evento['organizador']}")
    pdf.add_text("\n")
    
    # Participante
    pdf.set_font_size(12)
    pdf.add_text("PARTICIPANTE")
    pdf.set_font_size(10)
    pdf.add_text(f"Nombre: {aceptacion['nombre_participante']}")
    pdf.add_text(f"Documento: {aceptacion['documento']}")
    pdf.add_text("\n")
    
    # Texto Legal
    pdf.set_font_size(12)
    pdf.add_text("TEXTO DEL DESLINDE ACEPTADO")
    pdf.add_text("-" * 60) # Separador visual
    pdf.set_font_size(9)
    pdf.add_text(texto_final)
    pdf.add_text("-" * 60)
    pdf.add_text("\n")
    
    # Auditoría
    pdf.set_font_size(12)
    pdf.add_text("AUDITORÍA TÉCNICA")
    pdf.set_font_size(10)
    pdf.add_text(f"Hash SHA256 Deslinde (BD): {hash_bd}")
    pdf.add_text(f"Hash SHA256 Calculado: {hash_calculado}")
    pdf.add_text(f"Consistencia del hash: {consistencia}")
    pdf.add_text(f"Fecha Aceptación (UTC): {aceptacion['fecha_hora']}")
    pdf.add_text(f"Dirección IP: {aceptacion['ip']}")
    pdf.add_text(f"User-Agent: {aceptacion['user_agent']}")
    
    # Bloque de evidencias solicitadas (P0.5)
    firma_status = "PRESENTE" if aceptacion.get('firma_path') else "NO APLICA"
    
    doc_status = "NO APLICA"
    if aceptacion.get('doc_frente_path') and aceptacion.get('doc_dorso_path'):
        doc_status = "PRESENTE"
    elif aceptacion.get('doc_frente_path') or aceptacion.get('doc_dorso_path'):
        doc_status = "PARCIAL"
        
    audio_status = "NO APLICA"
    if aceptacion.get('audio_path'):
        audio_status = "PRESENTE"
    elif aceptacion.get('audio_exento'):
        audio_status = "EXENTO"
        
    salud_status = "PRESENTE" if aceptacion.get('salud_doc_path') else "NO APLICA"
    
    pdf.add_text("\n")
    pdf.add_text("EVIDENCIAS ADJUNTAS (VERIFICAR EN SISTEMA):")
    pdf.add_text(f"- Firma Manuscrita (Fichero): {firma_status}")
    pdf.add_text(f"- Documento Identidad (Frente/Dorso): {doc_status}")
    pdf.add_text(f"- Audio Aceptación: {audio_status}")
    pdf.add_text(f"- Documento Salud: {salud_status}")
    pdf.add_text("\n")
    
    # Documento de salud (detalle extra)
    tiene_salud = "Sí" if aceptacion.get('salud_doc_path') else "No"
    pdf.add_text(f"Documento de salud aportado: {tiene_salud}")
    if aceptacion.get('salud_doc_path'):
        pdf.add_text(f"Tipo de documento: {aceptacion.get('salud_doc_tipo', 'No especificado')}")
    
    flags = []
    if aceptacion.get('audio_exento'): flags.append("AUDIO_EXENTO")
    if aceptacion.get('firma_asistida'): flags.append("FIRMA_ASISTIDA")
    if flags:
        pdf.add_text(f"Flags: {', '.join(flags)}")
    
    pdf.add_text("\n")
    pdf.set_font_size(8)
    pdf.add_text("Documento generado automáticamente por el sistema EncarreraOK.")
    
    return pdf.get_pdf_bytes()


@app.get("/aceptacion/pdf/{pdf_token}")
def public_descargar_pdf_aceptacion(pdf_token: str):
    """Endpoint público para descargar PDF de aceptación."""
    # Buscar aceptación por token
    aceptacion = get_aceptacion_por_token(pdf_token)
    if not aceptacion:
        # Token no existe
        app_logger.warning(f"Intento de acceso PDF con token inexistente: {pdf_token[:8]}...")
        raise HTTPException(status_code=404, detail="Aceptación no encontrada o token inválido")
        
    # Validar revocación
    if aceptacion.get("pdf_token_revoked"):
        app_logger.warning(f"Intento de acceso PDF con token REVOCADO: id={aceptacion['id']}")
        raise HTTPException(status_code=404, detail="Aceptación no encontrada o token inválido")
        
    # Validar expiración
    if aceptacion.get("pdf_token_expires_at"):
        try:
            # Comparación simple de cadenas ISO si están en UTC y formato correcto
            now_utc_str = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            # Normalizamos quitando la Z para comparar objetos si queremos ser muy estrictos, 
            # pero dado que el sistema usa strings ISO consistentes, la comparación de strings funciona.
            # Sin embargo, para seguridad, parseamos.
            expires_at_str = aceptacion["pdf_token_expires_at"].rstrip("Z")
            expires_at = datetime.fromisoformat(expires_at_str)
            
            if datetime.utcnow() > expires_at:
                app_logger.warning(f"Intento de acceso PDF con token VENCIDO: id={aceptacion['id']}, expires={aceptacion['pdf_token_expires_at']}")
                raise HTTPException(status_code=404, detail="Aceptación no encontrada o token inválido")
        except Exception:
            # Ante duda o error de formato, denegar
            app_logger.error(f"Error validando expiración token id={aceptacion['id']}")
            raise HTTPException(status_code=404, detail="Aceptación no encontrada o token inválido")

    evento = get_evento(aceptacion["evento_id"])
    if not evento:
        raise HTTPException(status_code=404, detail="Evento asociado no encontrado")

    # Generar PDF
    pdf_bytes = _generar_bytes_pdf(aceptacion, evento)
    
    # Registrar acceso exitoso
    registrar_acceso_pdf(aceptacion["id"])
    
    app_logger.info(f"PDF público descargado para aceptacion_id={aceptacion['id']} via token")
    
    filename = "aceptacion.pdf"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf", headers=headers)


@app.get("/admin/aceptaciones/{aceptacion_id}/pdf")
def admin_descargar_pdf_aceptacion(
    aceptacion_id: int,
    username: str = Depends(get_current_username)
):
    """Genera PDF legal de la aceptación."""
    # Obtener datos completos
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
        
    evento = get_evento(aceptacion["evento_id"])
    if not evento:
        raise HTTPException(status_code=404, detail="Evento asociado no encontrado")

    # Generar PDF
    pdf_bytes = _generar_bytes_pdf(aceptacion, evento)
    
    app_logger.info(f"PDF generado para aceptacion_id={aceptacion_id} evento_id={evento['id']}")
    
    filename = f"aceptacion_{aceptacion_id}.pdf"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf", headers=headers)



@app.get("/admin/exportar_zip/{evento_id}")
def admin_exportar_zip(
    evento_id: int,
    username: str = Depends(get_current_username)
):
    """
    Genera y descarga un ZIP con todas las evidencias de un evento y manifest.json.
    """
    # 1. Obtener datos del evento y aceptaciones
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
        
    aceptaciones = listar_aceptaciones(evento_id=evento_id)
    if not aceptaciones:
        raise HTTPException(status_code=404, detail="No hay aceptaciones para este evento")

    # Pre-calcular texto del deslinde para el evento
    version = evento.get("deslinde_version") or DEFAULT_DESLINDE_VERSION
    texto_base = cargar_deslinde(version)
    texto_final_template = texto_base.replace("{{NOMBRE_EVENTO}}", evento["nombre"])\
                                     .replace("{{ORGANIZADOR}}", evento["organizador"])

    # 2. Crear buffer en memoria para el ZIP
    zip_buffer = io.BytesIO()
    
    # Datos para manifest.json
    manifest_data = {
        "evento": {
            "id": evento["id"],
            "nombre": evento["nombre"],
            "fecha": evento["fecha"],
            "organizador": evento["organizador"]
        },
        "fecha_exportacion_utc": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "aceptaciones": []
    }
    
    # 3. Escribir ZIP
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for a in aceptaciones:
            # Crear nombre de carpeta segura: ID_Nombre (sanitizado)
            clean_name = "".join([c for c in a['nombre_participante'] if c.isalnum() or c in (' ', '_', '-')]).strip()
            folder_name = f"{a['id']}_{clean_name}"
            
            # Entrada para manifest
            aceptacion_entry = {
                "aceptacion_id": a["id"],
                "nombre_participante": a["nombre_participante"],
                "documento": a["documento"],
                "fecha_hora": a["fecha_hora"],
                "deslinde_hash_sha256": a["deslinde_hash_sha256"],
                "flags": {
                    "audio_exento": 1 if a.get("audio_exento") else 0,
                    "firma_asistida": 1 if a.get("firma_asistida") else 0
                },
                "evidencias": {}
            }
            
            # Helper para agregar archivo si existe y retornar hash
            def agregar_archivo(path_bd, nombre_salida):
                if path_bd and os.path.exists(path_bd):
                    try:
                        # Calcular path relativo dentro del ZIP
                        arcname = f"{folder_name}/{nombre_salida}"
                        zip_file.write(path_bd, arcname)
                        # Calcular hash real del archivo
                        sha256 = calcular_hash_archivo(path_bd)
                        return sha256, arcname
                    except Exception as e:
                        app_logger.error(f"Error agregando archivo {path_bd} al ZIP: {e}")
                return None, None

            # AJUSTE 1: Generar PDF legal en memoria e incluirlo
            try:
                # Usar el generador centralizado para consistencia (incluye auditoría P0.5)
                pdf_bytes = _generar_bytes_pdf(a, evento)
                
                # Escribir PDF al ZIP
                pdf_arcname = f"{folder_name}/aceptacion.pdf"
                zip_file.writestr(pdf_arcname, pdf_bytes)
                
                # Calcular hash del PDF (bytes)
                pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
                
                # Agregar al manifest (AJUSTE 2)
                aceptacion_entry["evidencias"]["pdf"] = {
                    "path": pdf_arcname,
                    "sha256": pdf_hash
                }

            except Exception as e:
                app_logger.error(f"Error crítico generando PDF para aceptación {a['id']}: {e}")
                # Abortar exportación
                raise HTTPException(status_code=500, detail=f"Error generando PDF legal para aceptación {a['id']}. Exportación abortada.")

            # Agregar evidencias y poblar manifest
            
            # Firma
            h, p = agregar_archivo(a.get('firma_path'), "firma.png")
            if h: aceptacion_entry["evidencias"]["firma"] = {"path": p, "sha256": h}
            
            # Doc Frente
            if a.get('doc_frente_path'):
                ext = os.path.splitext(a['doc_frente_path'])[1] or ".jpg"
                h, p = agregar_archivo(a['doc_frente_path'], f"doc_frente{ext}")
                if h: aceptacion_entry["evidencias"]["doc_frente"] = {"path": p, "sha256": h}
                
            # Doc Dorso
            if a.get('doc_dorso_path'):
                ext = os.path.splitext(a['doc_dorso_path'])[1] or ".jpg"
                h, p = agregar_archivo(a['doc_dorso_path'], f"doc_dorso{ext}")
                if h: aceptacion_entry["evidencias"]["doc_dorso"] = {"path": p, "sha256": h}

            # Doc Salud
            if a.get('salud_doc_path'):
                ext = os.path.splitext(a['salud_doc_path'])[1] or ".jpg"
                h, p = agregar_archivo(a['salud_doc_path'], f"salud_doc{ext}")
                if h:
                    aceptacion_entry["evidencias"]["salud_doc"] = {
                        "tipo": a.get("salud_doc_tipo", "desconocido"),
                        "path": p,
                        "sha256": h
                    }

            # Audio
            if a.get('audio_path'):
                ext = os.path.splitext(a['audio_path'])[1] or ".webm"
                h, p = agregar_archivo(a['audio_path'], f"audio{ext}")
                if h: aceptacion_entry["evidencias"]["audio"] = {"path": p, "sha256": h}

            manifest_data["aceptaciones"].append(aceptacion_entry)
            
        # Agregar manifest.json al root del ZIP
        manifest_str = json.dumps(manifest_data, indent=2, ensure_ascii=False)
        zip_file.writestr("manifest.json", manifest_str)

        # AJUSTE 3: Agregar README.txt
        readme_content = f"""ENCARRERAOK – EXPORTACIÓN LEGAL DEL EVENTO

Este archivo ZIP contiene las aceptaciones legales del evento:
- Nombre del evento: {evento['nombre']}
- Fecha del evento: {evento['fecha']}
- Organizador: {evento['organizador']}

Estructura del ZIP:

/manifest.json
/README.txt
/<aceptacion_id>_<nombre>/
  aceptacion.pdf
  firma.(ext)
  documento_identidad_frente.(ext)
  documento_identidad_dorso.(ext)
  documento_salud.(ext)
  audio.(ext)

Descripción de archivos:

- aceptacion.pdf:
  Documento legal probatorio generado por el sistema EncarreraOK.
  Contiene el texto completo del deslinde aceptado, datos del participante,
  auditoría técnica (hash, IP, fecha, flags de accesibilidad).

- manifest.json:
  Archivo de control que lista todas las aceptaciones exportadas y los hashes
  SHA256 de cada evidencia incluida en el ZIP.

Integridad:

La integridad de esta exportación puede verificarse recalculando los hashes
SHA256 de cada archivo y comparándolos con los valores indicados en manifest.json.

Este material tiene fines legales y probatorios.
"""
        zip_file.writestr("README.txt", readme_content)

    # 4. Preparar respuesta
    app_logger.info(f"Export ZIP generado para evento {evento_id}. Aceptaciones: {len(aceptaciones)}. Incluye manifest, PDF legal y README.")
    zip_buffer.seek(0)
    
    # Nombre del archivo: Evento_Fecha.zip
    safe_event_name = "".join([c for c in evento['nombre'] if c.isalnum() or c in (' ', '_', '-')]).strip().replace(" ", "_")
    filename = f"{safe_event_name}_{evento['fecha']}.zip"
    
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    
    return StreamingResponse(zip_buffer, media_type="application/zip", headers=headers)


@app.get("/admin/gestion_eliminacion/{evento_id}", response_class=HTMLResponse)
def admin_gestion_eliminacion(
    evento_id: int,
    username: str = Depends(get_current_username)
) -> HTMLResponse:
    """Pantalla de confirmación y opciones para eliminar datos."""
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
        
    aceptaciones = listar_aceptaciones(evento_id=evento_id)
    
    template = templates_env.get_template("admin_gestion_eliminacion.html")
    html = template.render(
        evento=evento,
        total_aceptaciones=len(aceptaciones),
        username=username
    )
    return HTMLResponse(content=html)


@app.post("/admin/eliminar_evento", response_class=HTMLResponse)
def admin_procesar_eliminacion(
    evento_id: int = Form(...),
    tipo_eliminacion: str = Form(...), # 'parcial' o 'total'
    fecha_corte: Optional[str] = Form(None), # Para parcial
    username: str = Depends(get_current_username)
) -> HTMLResponse:
    """Procesa la eliminación solicitada."""
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")

    msg = ""
    
    if tipo_eliminacion == "total":
        # 1. Obtener todas las aceptaciones para borrar archivos
        aceptaciones = listar_aceptaciones(evento_id=evento_id)
        
        # 2. Borrar archivos físicos
        archivos_borrados = borrar_evidencias_fisicas(aceptaciones)
        
        # 3. Borrar evento y registros (Cascade manual)
        eliminar_evento_completo(evento_id)
        
        msg = f"Evento '{evento['nombre']}' eliminado completamente. {len(aceptaciones)} registros y {archivos_borrados} archivos eliminados."
        
        # Redirigir a lista general sin filtro
        return HTMLResponse(
            content=f"""
            <script>
                alert("{msg}");
                window.location.href = "/admin/aceptaciones";
            </script>
            """
        )

    elif tipo_eliminacion == "parcial":
        if not fecha_corte:
            raise HTTPException(status_code=400, detail="Fecha de corte requerida para eliminación parcial")
            
        # fecha_corte viene como 'YYYY-MM-DDTHH:MM'
        # Buscar aceptaciones anteriores a esa fecha
        # La fecha en BD es 'YYYY-MM-DDTHH:MM:SSZ' o similar ISO
        
        aceptaciones = listar_aceptaciones(evento_id=evento_id)
        a_borrar = []
        ids_borrar = []
        
        for a in aceptaciones:
            # Comparación de strings ISO funciona bien si el formato es consistente
            # fecha_corte (input) no tiene Z, fecha_bd sí puede tenerla.
            # Normalizamos a string simple para comparar
            fecha_bd = a['fecha_hora'][:16] # YYYY-MM-DDTHH:MM
            if fecha_bd < fecha_corte:
                a_borrar.append(a)
                ids_borrar.append(a['id'])
        
        if not a_borrar:
            return HTMLResponse(
                content=f"""
                <script>
                    alert("No se encontraron registros anteriores a {fecha_corte}.");
                    window.history.back();
                </script>
                """
            )
            
        # Borrar archivos
        archivos_borrados = borrar_evidencias_fisicas(a_borrar)
        
        # Borrar registros BD
        regs_borrados = eliminar_aceptaciones_por_ids(ids_borrar)
        
        msg = f"Limpieza completada. {regs_borrados} registros y {archivos_borrados} archivos eliminados anteriores a {fecha_corte}."
        
        # Redirigir a la gestión del mismo evento
        return HTMLResponse(
            content=f"""
            <script>
                alert("{msg}");
                window.location.href = "/admin/gestion_eliminacion/{evento_id}";
            </script>
            """
        )
    
    else:
        raise HTTPException(status_code=400, detail="Tipo de eliminación inválido")


@app.get("/admin/aceptaciones/{aceptacion_id}", response_class=HTMLResponse)
def admin_aceptacion_detalle(aceptacion_id: int, username: str = Depends(get_current_username)) -> HTMLResponse:
    """
    Muestra detalle de una aceptación específica.
    - Requiere autenticación Basic Auth.
    - Incluye todos los datos + paths + verificación de existencia de archivos.
    """
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
    
    template = templates_env.get_template("admin_aceptacion_detalle.html")
    html = template.render(aceptacion=aceptacion, username=username)
    return HTMLResponse(content=html)


@app.post("/admin/aceptaciones/{aceptacion_id}/revocar_token", response_class=HTMLResponse)
def admin_revocar_token(
    aceptacion_id: int,
    username: str = Depends(get_current_username)
) -> HTMLResponse:
    """
    Revoca manualmente el token PDF de una aceptación.
    """
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
        
    success = revocar_pdf_token(aceptacion_id)
    if success:
        app_logger.info(f"Token PDF revocado manualmente por admin: id={aceptacion_id}, user={username}")
        msg = "Token revocado correctamente."
    else:
        app_logger.warning(f"Fallo al revocar token PDF: id={aceptacion_id}")
        msg = "No se pudo revocar el token o ya estaba revocado."

    return HTMLResponse(
        content=f"""
        <script>
            alert("{msg}");
            window.location.href = "/admin/aceptaciones/{aceptacion_id}";
        </script>
        """
    )


@app.get("/admin/evento/{evento_id}/monitor", response_class=HTMLResponse)
def admin_monitor_evento(
    evento_id: int,
    q: Optional[str] = None,
    page: int = 1,
    username: str = Depends(get_current_username)
) -> HTMLResponse:
    """
    Monitor en tiempo real para el operador de entrada.
    Auto-refresh cada 10s (si no hay búsqueda).
    """
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
        
    # ADMIN PATCH: pagination + counter
    page_size = 25
    if page is None or page < 1:
        page = 1
    offset = (page - 1) * page_size

    conn = get_connection()
    try:
        cur = conn.cursor()

        # Contador total de deslindes del evento actual (sin filtrar por búsqueda)
        cur.execute(
            "SELECT COUNT(*) AS c FROM aceptaciones WHERE evento_id = ?",
            (evento_id,),
        )
        row = cur.fetchone()
        total_deslindes = row["c"] if row else 0

        # Construir condiciones compartidas para conteo filtrado y listado paginado
        where_clauses = ["a.evento_id = ?"]
        params_base: List[Any] = [evento_id]

        if q:
            q_norm = "".join(filter(str.isdigit, q))
            clauses = ["a.nombre_participante LIKE ?"]
            params_q: List[Any] = [f"%{q}%"]

            if len(q_norm) >= 3:
                clauses.append("a.documento_norm LIKE ?")
                params_q.append(f"%{q_norm}%")

            where_clauses.append(f"({' OR '.join(clauses)})")
            params_base.extend(params_q)

        where_sql = " AND ".join(where_clauses)

        # Conteo filtrado (para saber si hay página siguiente)
        sql_count = f"""
            SELECT COUNT(*) AS c
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            WHERE {where_sql}
        """
        cur.execute(sql_count, tuple(params_base))
        row = cur.fetchone()
        total_filtrado = row["c"] if row else 0

        # Listado paginado, ordenado por fecha de aceptación (más recientes primero)
        sql_list = f"""
            SELECT
                a.id,
                a.evento_id,
                e.nombre AS evento_nombre,
                e.fecha AS evento_fecha,
                e.organizador AS evento_organizador,
                a.nombre_participante,
                a.documento,
                a.fecha_hora,
                a.ip,
                a.user_agent,
                a.deslinde_hash_sha256,
                a.firma_path,
                a.doc_frente_path,
                a.doc_dorso_path,
                a.audio_path,
                a.salud_doc_path,
                a.salud_doc_tipo,
                a.audio_exento,
                a.firma_asistida
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            WHERE {where_sql}
            ORDER BY a.fecha_hora DESC
            LIMIT ? OFFSET ?
        """
        params_list = list(params_base)
        params_list.extend([page_size, offset])
        cur.execute(sql_list, tuple(params_list))
        rows = cur.fetchall()
        aceptaciones = [dict(r) for r in rows]
    finally:
        conn.close()

    has_prev = page > 1
    has_next = page * page_size < total_filtrado
    # /ADMIN PATCH
    
    template = templates_env.get_template("admin_monitor_evento.html")
    html = template.render(
        evento=evento,
        aceptaciones=aceptaciones,
        query=q,
        username=username,
        total_deslindes=total_deslindes,
        page=page,
        has_prev=has_prev,
        has_next=has_next,
    )
    return HTMLResponse(content=html)


@app.get("/admin/evento/{evento_id}/preview/{aceptacion_id}", response_class=HTMLResponse)
def admin_preview_evento(
    evento_id: int,
    aceptacion_id: int,
    username: str = Depends(get_current_username)
) -> HTMLResponse:
    """
    Vista express de validación de evidencias.
    """
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
        
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
        
    if str(aceptacion["evento_id"]) != str(evento_id):
        raise HTTPException(status_code=400, detail="Aceptación no pertenece al evento")
    
    template = templates_env.get_template("admin_preview.html")
    html = template.render(
        evento=evento,
        aceptacion=aceptacion,
        username=username
    )
    return HTMLResponse(content=html)


@app.get("/admin/evidencia/{aceptacion_id}/{tipo}")
def admin_servir_evidencia(
    aceptacion_id: int,
    tipo: str,
    thumbnail: bool = False,
    username: str = Depends(get_current_username)
):
    """
    Sirve archivos de evidencia protegidos (requiere auth).
    tipo: 'firma', 'doc_frente', 'doc_dorso', 'audio', 'salud_doc'
    """
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
        
    file_path = None
    media_type = "application/octet-stream"
    
    if tipo == "firma":
        file_path = aceptacion.get("firma_path")
        media_type = "image/png" # Asumimos PNG por canvas
    elif tipo == "doc_frente":
        file_path = aceptacion.get("doc_frente_path")
        media_type = "image/jpeg" # Default
    elif tipo == "doc_dorso":
        file_path = aceptacion.get("doc_dorso_path")
        media_type = "image/jpeg"
    elif tipo == "audio":
        file_path = aceptacion.get("audio_path")
        media_type = "audio/webm"
    elif tipo == "salud_doc":
        file_path = aceptacion.get("salud_doc_path")
        media_type = "image/jpeg"
    else:
        raise HTTPException(status_code=400, detail="Tipo de evidencia inválido")
        
    if not file_path or not os.path.exists(file_path):
        # Retornar 404 o una imagen placeholder
        raise HTTPException(status_code=404, detail="Evidencia no encontrada")
        
    # Detectar extensión real para mime type si es posible
    _, ext = os.path.splitext(file_path)
    if ext.lower() in ['.jpg', '.jpeg']:
        media_type = "image/jpeg"
    elif ext.lower() == '.png':
        media_type = "image/png"
    elif ext.lower() == '.webm':
        media_type = "audio/webm"
    elif ext.lower() == '.pdf':
        media_type = "application/pdf"

    # Lógica de Thumbnail (P1.2)
    if thumbnail and PIL_AVAILABLE and media_type.startswith("image/"):
        try:
            with Image.open(file_path) as img:
                # Resize manteniendo aspect ratio
                img.thumbnail((400, 400)) 
                buf = io.BytesIO()
                
                # Convertir a RGB si guardamos como JPEG (salvo PNG)
                save_format = "JPEG"
                if media_type == "image/png":
                    save_format = "PNG"
                else:
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGB")
                
                img.save(buf, format=save_format, quality=70)
                buf.seek(0)
                return StreamingResponse(buf, media_type=media_type)
        except Exception as e:
            # Fallback silencioso al original si falla resize
            app_logger.error(f"Error generando thumbnail para {file_path}: {e}")
        
    def iterfile():
        with open(file_path, mode="rb") as file_like:
            yield from file_like

    return StreamingResponse(iterfile(), media_type=media_type)


# ADMIN PATCH: serve local evidences
@app.get("/admin/evidence/view/{aceptacion_id}/{tipo}")
def admin_ver_evidencia_full(
    aceptacion_id: int,
    tipo: str,
    username: str = Depends(get_current_username)
):
    """
    Endpoint dedicado para visualizar evidencias en navegador (FileResponse).
    Solo lectura. No expone path real.
    """
    # Reutilizamos la lógica de obtención para seguridad
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
    
    file_path = None
    
    if tipo == "firma":
        file_path = aceptacion.get("firma_path")
    elif tipo == "doc_frente":
        file_path = aceptacion.get("doc_frente_path")
    elif tipo == "doc_dorso":
        file_path = aceptacion.get("doc_dorso_path")
    elif tipo == "audio":
        file_path = aceptacion.get("audio_path")
    elif tipo == "salud_doc":
        file_path = aceptacion.get("salud_doc_path")
    else:
        raise HTTPException(status_code=400, detail="Tipo de evidencia inválido")

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Archivo de evidencia no encontrado en disco")

    if not os.path.isfile(file_path):
        raise HTTPException(status_code=400, detail="El path no es un archivo válido")

    return FileResponse(file_path)


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
