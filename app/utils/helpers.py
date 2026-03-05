import os
import re
import hashlib
import stat
import logging
import secrets
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict
from app.db.database import DB_PATH
from app.services.evidencias_service import ensure_evidencias_storage

# Configure logger for this module
# Note: This logger instance is for this module's use.
# The main app logger is configured via setup_logging() below.
logger = logging.getLogger('encarreraok')

# ------------------------------------------------------------------------------
# Configuración de Logging
# ------------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """
    Configura logging a archivo con rotación.
    Intenta escribir en /var/log/encarreraok, fallback a directorio raíz del proyecto.
    """
    # Intentar primero en /var/log
    target_dir = "/var/log/encarreraok"
    
    try:
        os.makedirs(target_dir, exist_ok=True)
        # Verificar escritura
        test_file = os.path.join(target_dir, ".test_write")
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
    except Exception:
        # Fallback: directorio raíz del proyecto
        # helpers.py está en app/utils/, así que subimos 2 niveles
        current_dir = os.path.dirname(os.path.abspath(__file__))
        target_dir = os.path.dirname(os.path.dirname(current_dir))
    
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

# ------------------------------------------------------------------------------
# Constantes de Deslinde
# ------------------------------------------------------------------------------
LEGAL_DIR = os.environ.get("ENCARRERAOK_LEGAL_DIR", "legal")
DESLINDES_CONFIG = {
    "v1_1": "deslinde_v1_1_ligero.txt",
    "v2_0": "deslinde_v2_0_legal_fuerte.txt",
    "v3_0": "deslinde_v3_0_legal_full.txt",
}
DEFAULT_DESLINDE_VERSION = "v1_1"

# ------------------------------------------------------------------------------
# Helpers de Archivo y Sistema
# ------------------------------------------------------------------------------

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
        ensure_evidencias_storage()
    except Exception:
        # Entorno local dev windows etc
        pass

def cargar_deslinde(version: str = DEFAULT_DESLINDE_VERSION) -> str:
    """
    Carga el texto del deslinde desde archivo según la versión.
    Retorna el texto base con placeholders.
    """
    filename = DESLINDES_CONFIG.get(version)
    if not filename:
        logger.error(f"Versión de deslinde desconocida: {version}, usando default")
        filename = DESLINDES_CONFIG[DEFAULT_DESLINDE_VERSION]
    
    path = os.path.join(LEGAL_DIR, filename)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Error leyendo archivo de deslinde {path}: {e}")
        # Fallback de emergencia si no se puede leer el archivo
        return """DESLINDE DE RESPONSABILIDAD Y ACEPTACIÓN DE RIESGOS

Declaro que participo en el evento deportivo {{NOMBRE_EVENTO}}, organizado por {{ORGANIZADOR}}, de manera voluntaria y bajo mi exclusiva responsabilidad.

Reconozco que la participación en actividades deportivas implica riesgos inherentes, incluyendo, pero no limitándose a, caídas, lesiones físicas, traumatismos, accidentes cardiovasculares, condiciones climáticas adversas y otros riesgos propios de la actividad.

Declaro encontrarme en condiciones físicas y de salud adecuadas para participar, y que he sido debidamente informado/a sobre las características del evento.

Eximo de toda responsabilidad civil, penal y administrativa al organizador, auspiciantes, colaboradores, personal médico, autoridades y cualquier otra persona vinculada a la organización del evento, por cualquier daño, lesión o perjuicio que pudiera sufrir antes, durante o después de mi participación.

Autorizo la utilización de mi imagen, voz y datos personales con fines de difusión, promoción y registro del evento, sin derecho a compensación económica.

Declaro haber leído, comprendido y aceptado íntegramente el presente deslinde de responsabilidad."""

def calcular_hash_archivo(filepath: str) -> str:
    """Calcula SHA256 de un archivo en disco."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        # Leer en chunks para eficiencia
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

# ------------------------------------------------------------------------------
# Helpers de Strings y Normalización
# ------------------------------------------------------------------------------

def normalizar_documento_helper(doc: str) -> str:
    """Normaliza documento: quita puntos, guiones, espacios y pasa a mayúsculas."""
    if not doc:
        return ""
    return re.sub(r"[.\-\s]", "", doc).upper()

def fecha_ddmmaaaa(value: str) -> str:
    try:
        y, m, d = value.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return value

def generar_token(length: int = 32) -> str:
    """Genera un token seguro URL-safe."""
    return secrets.token_urlsafe(length)

def limpiar_nombre_archivo(nombre: str) -> str:
    """
    Sanitiza un string para ser usado como nombre de archivo/directorio.
    Permite alfanuméricos, espacios, guiones y guiones bajos.
    """
    if not nombre:
        return "sin_nombre"
    clean = "".join([c for c in nombre if c.isalnum() or c in (' ', '_', '-')]).strip()
    return clean or "sin_nombre"
