import os
import shutil
import base64
import uuid
import logging
import traceback
import io
from typing import Optional, Tuple
from fastapi import HTTPException, UploadFile
from app.db.database import DB_PATH

# ------------------------------------------------------------------------------
# Configuración de Logging
# ------------------------------------------------------------------------------
logger = logging.getLogger('encarreraok')

# ------------------------------------------------------------------------------
# Constantes y Configuración de Almacenamiento
# ------------------------------------------------------------------------------
EVIDENCIAS_DIR = os.path.join(os.path.dirname(DB_PATH), "evidencias")
FIRMAS_DIR = os.path.join(EVIDENCIAS_DIR, "firmas")
DOCUMENTOS_DIR = os.path.join(EVIDENCIAS_DIR, "documentos")
AUDIOS_DIR = os.path.join(EVIDENCIAS_DIR, "audios")
SALUD_DIR = os.path.join(EVIDENCIAS_DIR, "salud")

# Límites de tamaño por tipo de evidencia (prevención 413)
MAX_IMAGE_DOC_MB = 8  # Imagen documento: máx 8 MB por archivo
MAX_FIRMA_MB = 1      # Firma canvas: máx 1 MB
MAX_AUDIO_MB = 5      # Audio: máx 5 MB
# Límites para compresión automática
MAX_IMAGE_COMPRESS_THRESHOLD_MB = 2  # Si supera esto, comprimir
MAX_IMAGE_COMPRESS_TARGET_MB = 1.5   # Objetivo después de compresión

# Intentar importar PIL para compresión de imágenes (opcional)
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


def ensure_evidencias_storage() -> None:
    """
    Garantiza que directorios de evidencias existan.
    """
    try:
        os.makedirs(FIRMAS_DIR, exist_ok=True)
        os.makedirs(DOCUMENTOS_DIR, exist_ok=True)
        os.makedirs(AUDIOS_DIR, exist_ok=True)
        os.makedirs(SALUD_DIR, exist_ok=True)
    except Exception:
        # Entorno local dev windows etc
        pass


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


def guardar_firma(firma_base64: Optional[str], request_id: str, req_firma: bool = False) -> Optional[str]:
    """
    Procesa y guarda la firma en base64.
    Retorna la ruta del archivo o None.
    """
    if not firma_base64:
        return None

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
            logger.warning(f"[{request_id}] Firma demasiado grande: {firma_size} bytes")
            raise HTTPException(
                status_code=413,
                detail=f"La firma es demasiado grande. Máximo permitido: {MAX_FIRMA_MB} MB. Por favor, firme más pequeña."
            )
        
        filename = f"{uuid.uuid4()}.png"
        filepath = os.path.join(FIRMAS_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(data)
        
        logger.info(f"[{request_id}] Firma guardada: path={filepath}, size={firma_size} bytes")
        return filepath
        
    except HTTPException:
        raise
    except Exception:
        # Si falla guardar la firma y es requerida, error.
        if req_firma:
            raise HTTPException(status_code=500, detail="Error al guardar la firma")
        # Si no es requerida pero vino data corrupta, se ignora o se loguea.
        return None


def _guardar_imagen_generica(
    file: UploadFile, 
    target_dir: str, 
    suffix: str, 
    request_id: str, 
    tipo_log: str
) -> str:
    """
    Helper interno para guardar una imagen de documento, validar tamaño y comprimir.
    Retorna la ruta final del archivo.
    """
    try:
        max_doc_bytes = MAX_IMAGE_DOC_MB * 1024 * 1024
        
        # Validar tamaño
        file.file.seek(0, os.SEEK_END)
        size = file.file.tell()
        file.file.seek(0)
        
        if size > max_doc_bytes:
            logger.warning(f"[{request_id}] {tipo_log} demasiado grande: {size} bytes")
            raise HTTPException(
                status_code=413,
                detail=f"La imagen ({tipo_log}) es demasiado grande. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
            )
        
        # Guardar archivo
        ext = os.path.splitext(file.filename)[1]
        if not ext: ext = ".jpg"
        
        # Si suffix está vacío, no agregamos guion bajo extra si no queremos
        # Pero aquí asumimos que suffix viene como "_frente" o ""
        filename = f"{uuid.uuid4()}{suffix}{ext}"
        filepath = os.path.join(target_dir, filename)
        
        with open(filepath, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        # Comprimir si es necesario
        if size > MAX_IMAGE_COMPRESS_THRESHOLD_MB * 1024 * 1024:
            logger.info(f"[{request_id}] Comprimiendo {tipo_log}: {size} bytes")
            compressed = comprimir_imagen(filepath, MAX_IMAGE_COMPRESS_TARGET_MB)
            if not compressed:
                os.remove(filepath)
                logger.error(f"[{request_id}] No se pudo comprimir {tipo_log}")
                raise HTTPException(
                    status_code=413,
                    detail=f"La imagen ({tipo_log}) es demasiado grande y no se pudo comprimir. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                )
            final_size = os.path.getsize(filepath)
            logger.info(f"[{request_id}] {tipo_log} comprimido: {size} -> {final_size} bytes")
        else:
            final_size = size
            
        logger.info(f"[{request_id}] {tipo_log} guardado: path={filepath}, size={final_size} bytes")
        return filepath
        
    except HTTPException:
        raise
    except Exception as e:
        # La limpieza debe hacerla el llamador si es una operación compuesta, 
        # o aquí si es simple. Para simplicidad, lanzamos excepción y el caller limpia si necesita.
        raise e


def guardar_documentos(
    doc_frente: Optional[UploadFile], 
    doc_dorso: Optional[UploadFile], 
    request_id: str
) -> Tuple[Optional[str], Optional[str]]:
    """
    Guarda frente y dorso del documento.
    Si falla uno, intenta limpiar el otro.
    """
    if not doc_frente or not doc_dorso:
        return None, None
        
    frente_path = None
    dorso_path = None
    
    try:
        # Frente
        frente_path = _guardar_imagen_generica(
            doc_frente, DOCUMENTOS_DIR, "_frente", request_id, "Doc frente"
        )
        
        # Dorso
        dorso_path = _guardar_imagen_generica(
            doc_dorso, DOCUMENTOS_DIR, "_dorso", request_id, "Doc dorso"
        )
        
        return frente_path, dorso_path
        
    except Exception as e:
        # Limpiar archivos parciales
        if frente_path and os.path.exists(frente_path):
            try:
                os.remove(frente_path)
            except:
                pass
        if dorso_path and os.path.exists(dorso_path):
            try:
                os.remove(dorso_path)
            except:
                pass
        
        if isinstance(e, HTTPException):
            raise e
        
        logger.error(f"[{request_id}] Error guardando documentos: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Error al guardar las imágenes del documento")


def guardar_documento_salud(doc: Optional[UploadFile], request_id: str) -> Optional[str]:
    """
    Guarda el documento de salud.
    """
    if not doc:
        return None
        
    path = None
    try:
        path = _guardar_imagen_generica(
            doc, SALUD_DIR, "", request_id, "Doc salud"
        )
        return path
    except HTTPException:
        raise
    except Exception:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except:
                pass
        raise HTTPException(status_code=500, detail="Error al guardar el documento de salud")


def guardar_audio(
    audio_base64: Optional[str], 
    request_id: str, 
    req_audio: bool = False, 
    audio_exento: int = 0
) -> Optional[str]:
    """
    Guarda el audio en base64.
    """
    if not audio_base64:
        return None
        
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
            logger.warning(f"[{request_id}] Audio demasiado grande: {audio_size} bytes")
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
            
        logger.info(f"[{request_id}] Audio guardado: path={filepath_audio}, size={audio_size} bytes")
        return filepath_audio
        
    except HTTPException:
        raise
    except Exception:
        if req_audio and audio_exento != 1:
            raise HTTPException(status_code=500, detail="Error al guardar el audio")
        logger.error(f"[{request_id}] Error no bloqueante al guardar audio: {traceback.format_exc()}")
        return None
