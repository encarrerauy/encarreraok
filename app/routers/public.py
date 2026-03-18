"""
Rutas públicas de EncarreraOK.

Endpoints:
  GET  /e/{evento_id}            - Mostrar formulario de aceptación
  POST /e/{evento_id}            - Procesar aceptación
  GET  /aceptacion/pdf/{token}   - Descarga pública de PDF
"""

import io
import os
import re
import base64
import uuid
import secrets
import shutil
import logging
import traceback
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse

from app.config import settings
from app.templates_config import templates_env
from app.pdf_generator import (
    _generar_bytes_pdf,
    cargar_deslinde,
    calcular_hash_sha256,
    DEFAULT_DESLINDE_VERSION,
)

app_logger = logging.getLogger('encarreraok')

router = APIRouter()

# ------------------------------------------------------------------------------
# Constantes de límites de tamaño (compartidas con admin router)
# ------------------------------------------------------------------------------
MAX_IMAGE_DOC_MB = 4
MAX_FIRMA_MB = 1
MAX_AUDIO_MB = 5
MAX_IMAGE_COMPRESS_THRESHOLD_MB = 2
MAX_IMAGE_COMPRESS_TARGET_MB = 1.5

# Intentar importar PIL para compresión de imágenes (opcional)
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Directorios de almacenamiento de evidencias
DB_PATH = settings.db_path
EVIDENCIAS_DIR = os.path.join(os.path.dirname(DB_PATH), "evidencias")
FIRMAS_DIR = os.path.join(EVIDENCIAS_DIR, "firmas")
DOCUMENTOS_DIR = os.path.join(EVIDENCIAS_DIR, "documentos")
AUDIOS_DIR = os.path.join(EVIDENCIAS_DIR, "audios")
SALUD_DIR = os.path.join(EVIDENCIAS_DIR, "salud")


def normalizar_documento_helper(doc: str) -> str:
    """Normaliza documento: quita puntos, guiones, espacios y pasa a mayúsculas."""
    if not doc:
        return ""
    return re.sub(r"[.\-\s]", "", doc).upper()


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

        img = Image.open(file_path)
        original_format = img.format or 'JPEG'

        if original_format in ('JPEG', 'JPG') and img.mode != 'RGB':
            img = img.convert('RGB')

        buffer = io.BytesIO()
        img.save(buffer, format=original_format, quality=85, optimize=True)
        current_size = buffer.tell()

        if current_size <= max_size_bytes:
            return file_path

        original_width, original_height = img.size
        ratio = (max_size_bytes / current_size) ** 0.5
        new_width = int(original_width * ratio)
        new_height = int(original_height * ratio)

        if max(new_width, new_height) < 800:
            if new_width > new_height:
                new_width = 800
                new_height = int(original_height * (800 / original_width))
            else:
                new_height = 800
                new_width = int(original_width * (800 / original_height))

        try:
            resample = Image.Resampling.LANCZOS
        except AttributeError:
            resample = Image.LANCZOS
        img_resized = img.resize((new_width, new_height), resample)

        for quality in [85, 75, 65, 55, 45]:
            buffer = io.BytesIO()
            img_resized.save(buffer, format=original_format, quality=quality, optimize=True)
            if buffer.tell() <= max_size_bytes:
                with open(file_path, 'wb') as f:
                    f.write(buffer.getvalue())
                return file_path

        buffer = io.BytesIO()
        img_resized.save(buffer, format=original_format, quality=40, optimize=True)
        if buffer.tell() <= max_size_bytes * 1.2:
            with open(file_path, 'wb') as f:
                f.write(buffer.getvalue())
            return file_path

        return None
    except Exception:
        return None


def _get_connection():
    """Obtiene una conexión a la base de datos SQLite."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_evento(evento_id: int):
    """Obtiene un evento por id."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def aceptacion_existente(conn, evento_id: int, documento_norm: str) -> bool:
    """Detecta si existe duplicado para el evento y documento normalizado."""
    if not documento_norm:
        return False

    cur = conn.cursor()

    cur.execute("PRAGMA table_info(aceptaciones)")
    columns = [info[1] for info in cur.fetchall()]
    has_valido = "valido" in columns

    if has_valido:
        cur.execute(
            "SELECT 1 FROM aceptaciones WHERE evento_id = ? AND documento_norm = ? AND valido = 1 LIMIT 1",
            (evento_id, documento_norm)
        )
    else:
        cur.execute(
            "SELECT 1 FROM aceptaciones WHERE evento_id = ? AND documento_norm = ? LIMIT 1",
            (evento_id, documento_norm)
        )

    return cur.fetchone() is not None


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
    conn = _get_connection()
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


def get_aceptacion_por_token(pdf_token: str):
    """Obtiene aceptación por token público."""
    conn = _get_connection()
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
        return dict(row)
    finally:
        conn.close()


def registrar_acceso_pdf(aceptacion_id: int):
    """Registra un acceso exitoso al PDF."""
    conn = _get_connection()
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


# ------------------------------------------------------------------------------
# Rutas públicas
# ------------------------------------------------------------------------------

@router.get("/e/{evento_id}", response_class=HTMLResponse)
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
    evento["activo"] = bool(evento["activo"])
    evento["req_firma"] = bool(evento.get("req_firma", 0))
    evento["req_documento"] = bool(evento.get("req_documento", 0))
    evento["req_audio"] = bool(evento.get("req_audio", 0))
    evento["req_salud"] = bool(evento.get("req_salud", 0))
    evento["friendly_intro"] = bool(evento.get("friendly_intro", 0))  # DESLINDE PATCH: friendly intro

    # Obtener texto del deslinde
    deslinde_custom = evento.get("deslinde_texto")
    if deslinde_custom and deslinde_custom.strip():
        texto_final = deslinde_custom
    else:
        version = evento.get("deslinde_version") or DEFAULT_DESLINDE_VERSION
        texto_base = cargar_deslinde(version)
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


@router.post("/e/{evento_id}", response_class=HTMLResponse)
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
        if acepto is None:
            app_logger.warning(f"[{request_id}] Checkbox acepto no marcado")
            raise HTTPException(status_code=400, detail="Debe aceptar el deslinde")

        req_firma = bool(evento.get("req_firma", 0))
        if req_firma and not firma_base64:
            raise HTTPException(status_code=400, detail="La firma manuscrita es obligatoria")

        req_documento = bool(evento.get("req_documento", 0))
        if req_documento:
            if not doc_frente or not doc_frente.filename:
                raise HTTPException(status_code=400, detail="La foto del frente del documento es obligatoria")
            if not doc_dorso or not doc_dorso.filename:
                raise HTTPException(status_code=400, detail="La foto del dorso del documento es obligatoria")

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

        req_audio = bool(evento.get("req_audio", 0))
        if req_audio:
            if audio_exento == 1:
                app_logger.info(f"[{request_id}] Audio exento por imposibilidad física")
            elif not audio_base64:
                raise HTTPException(status_code=400, detail="El audio de aceptación es obligatorio")

        ip = request.client.host if request.client else "0.0.0.0"
        user_agent = request.headers.get("user-agent", "")
        fecha_hora = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        documento_norm = normalizar_documento_helper(documento)

        conn = _get_connection()
        try:
            if aceptacion_existente(conn, evento_id, documento_norm):
                app_logger.warning(f"[{request_id}] Intento de duplicado bloqueado: evento={evento_id}, doc={documento_norm}")
                raise HTTPException(status_code=400, detail="Ya existe una aceptación registrada para este documento en este evento.")
        finally:
            conn.close()

        deslinde_custom = evento.get("deslinde_texto")
        if deslinde_custom and deslinde_custom.strip():
            texto_final = deslinde_custom
            version = evento.get("deslinde_version") or DEFAULT_DESLINDE_VERSION
        else:
            version = evento.get("deslinde_version") or DEFAULT_DESLINDE_VERSION
            texto_base = cargar_deslinde(version)
            texto_final = texto_base.replace("{{NOMBRE_EVENTO}}", evento["nombre"])\
                                    .replace("{{ORGANIZADOR}}", evento["organizador"])

        deslinde_hash_sha256 = calcular_hash_sha256(texto_final)

        # Procesamiento de firma
        firma_path_final = None
        if firma_base64:
            if "," in firma_base64:
                header, encoded = firma_base64.split(",", 1)
            else:
                encoded = firma_base64

            try:
                data = base64.b64decode(encoded)

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
                if req_firma:
                    raise HTTPException(status_code=500, detail="Error al guardar la firma")

        # Procesamiento de documentos
        doc_frente_path_final = None
        doc_dorso_path_final = None

        if req_documento and doc_frente and doc_dorso:
            try:
                max_doc_bytes = MAX_IMAGE_DOC_MB * 1024 * 1024

                doc_frente.file.seek(0, os.SEEK_END)
                size_frente = doc_frente.file.tell()
                doc_frente.file.seek(0)
                if size_frente > max_doc_bytes:
                    app_logger.warning(f"[{request_id}] Doc frente demasiado grande: {size_frente} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"La imagen del frente es demasiado grande. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                    )

                doc_dorso.file.seek(0, os.SEEK_END)
                size_dorso = doc_dorso.file.tell()
                doc_dorso.file.seek(0)
                if size_dorso > max_doc_bytes:
                    app_logger.warning(f"[{request_id}] Doc dorso demasiado grande: {size_dorso} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"La imagen del dorso es demasiado grande. Máximo permitido: {MAX_IMAGE_DOC_MB} MB."
                    )

                ext_frente = os.path.splitext(doc_frente.filename)[1]
                if not ext_frente: ext_frente = ".jpg"
                filename_frente = f"{uuid.uuid4()}_frente{ext_frente}"
                filepath_frente = os.path.join(DOCUMENTOS_DIR, filename_frente)
                with open(filepath_frente, "wb") as buffer:
                    shutil.copyfileobj(doc_frente.file, buffer)

                if size_frente > MAX_IMAGE_COMPRESS_THRESHOLD_MB * 1024 * 1024:
                    app_logger.info(f"[{request_id}] Comprimiendo doc frente: {size_frente} bytes")
                    compressed = comprimir_imagen(filepath_frente, MAX_IMAGE_COMPRESS_TARGET_MB)
                    if not compressed:
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

                ext_dorso = os.path.splitext(doc_dorso.filename)[1]
                if not ext_dorso: ext_dorso = ".jpg"
                filename_dorso = f"{uuid.uuid4()}_dorso{ext_dorso}"
                filepath_dorso = os.path.join(DOCUMENTOS_DIR, filename_dorso)
                with open(filepath_dorso, "wb") as buffer:
                    shutil.copyfileobj(doc_dorso.file, buffer)

                if size_dorso > MAX_IMAGE_COMPRESS_THRESHOLD_MB * 1024 * 1024:
                    app_logger.info(f"[{request_id}] Comprimiendo doc dorso: {size_dorso} bytes")
                    compressed = comprimir_imagen(filepath_dorso, MAX_IMAGE_COMPRESS_TARGET_MB)
                    if not compressed:
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
                if doc_frente_path_final and os.path.exists(doc_frente_path_final):
                    try:
                        os.remove(doc_frente_path_final)
                    except Exception:
                        pass
                if doc_dorso_path_final and os.path.exists(doc_dorso_path_final):
                    try:
                        os.remove(doc_dorso_path_final)
                    except Exception:
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
            header = ""
            if "," in audio_base64:
                header, encoded = audio_base64.split(",", 1)
            else:
                encoded = audio_base64

            try:
                data = base64.b64decode(encoded)

                max_audio_bytes = MAX_AUDIO_MB * 1024 * 1024
                audio_size = len(data)
                if audio_size > max_audio_bytes:
                    app_logger.warning(f"[{request_id}] Audio demasiado grande: {audio_size} bytes")
                    raise HTTPException(
                        status_code=413,
                        detail=f"El audio es demasiado grande. Máximo permitido: {MAX_AUDIO_MB} MB. Por favor, intente ser más breve."
                    )

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
        app_logger.error(
            f"[{request_id}] Excepción en procesar_aceptacion - evento_id={evento_id}: {str(e)}\n"
            f"{traceback.format_exc()}"
        )
        raise HTTPException(status_code=500, detail="Error interno del servidor")


@router.get("/aceptacion/pdf/{pdf_token}")
def public_descargar_pdf_aceptacion(pdf_token: str):
    """Endpoint público para descargar PDF de aceptación."""
    aceptacion = get_aceptacion_por_token(pdf_token)
    if not aceptacion:
        app_logger.warning(f"Intento de acceso PDF con token inexistente: {pdf_token[:8]}...")
        raise HTTPException(status_code=404, detail="Aceptación no encontrada o token inválido")

    if aceptacion.get("pdf_token_revoked"):
        app_logger.warning(f"Intento de acceso PDF con token REVOCADO: id={aceptacion['id']}")
        raise HTTPException(status_code=404, detail="Aceptación no encontrada o token inválido")

    if aceptacion.get("pdf_token_expires_at"):
        try:
            expires_at_str = aceptacion["pdf_token_expires_at"].rstrip("Z")
            expires_at = datetime.fromisoformat(expires_at_str)

            if datetime.utcnow() > expires_at:
                app_logger.warning(f"Intento de acceso PDF con token VENCIDO: id={aceptacion['id']}, expires={aceptacion['pdf_token_expires_at']}")
                raise HTTPException(status_code=404, detail="Aceptación no encontrada o token inválido")
        except Exception:
            app_logger.error(f"Error validando expiración token id={aceptacion['id']}")
            raise HTTPException(status_code=404, detail="Aceptación no encontrada o token inválido")

    evento = get_evento(aceptacion["evento_id"])
    if not evento:
        raise HTTPException(status_code=404, detail="Evento asociado no encontrado")

    pdf_bytes = _generar_bytes_pdf(aceptacion, evento)

    registrar_acceso_pdf(aceptacion["id"])

    app_logger.info(f"PDF público descargado para aceptacion_id={aceptacion['id']} via token")

    filename = "aceptacion.pdf"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf", headers=headers)
