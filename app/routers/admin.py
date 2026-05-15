"""
Rutas de administración de EncarreraOK.
Todas las rutas requieren autenticación HTTP Basic.

Endpoints:
  GET  /admin                               - Dashboard admin
  GET  /admin/home                          - Dashboard admin (alias)
  GET  /admin/search                        - Búsqueda global
  GET  /admin/eventos                       - Listado de eventos
  GET  /admin/eventos/nuevo                 - Formulario nuevo evento
  POST /admin/eventos/nuevo                 - Crear evento
  GET  /admin/eventos/{id}/editar           - Formulario editar evento
  POST /admin/eventos/{id}/editar           - Guardar edición evento
  GET  /admin/aceptaciones                  - Listado de aceptaciones
  GET  /admin/aceptaciones/{id}/pdf         - Descargar PDF aceptación
  GET  /admin/exportar_zip/{evento_id}      - Exportar ZIP de evidencias
  GET  /admin/gestion_eliminacion/{id}      - Pantalla de eliminación
  POST /admin/eliminar_evento               - Procesar eliminación
  GET  /admin/aceptaciones/{id}             - Detalle aceptación
  POST /admin/aceptaciones/{id}/revocar_token - Revocar token PDF
  GET  /admin/evento/{id}/monitor           - Monitor de entrada
  GET  /admin/evento/{id}/preview/{acep_id} - Vista previa evidencias
  GET  /admin/evidencia/{id}/{tipo}         - Servir evidencia (streaming)
  GET  /admin/evidence/view/{id}/{tipo}     - Visualizar evidencia (FileResponse)
"""

import io
import os
import re
import json
import hashlib
import logging
import sqlite3
from datetime import datetime, date
from typing import Optional, List, Any, Dict

from fastapi import APIRouter, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, RedirectResponse

from app.middleware.auth import get_current_username
from app.config import settings
from app.templates_config import templates_env
from app.pdf_generator import (
    _generar_bytes_pdf,
    cargar_deslinde,
    calcular_hash_sha256,
    DEFAULT_DESLINDE_VERSION,
)

app_logger = logging.getLogger('encarreraok')

# Intentar importar PIL para thumbnails (opcional)
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

router = APIRouter(prefix="/admin", dependencies=[Depends(get_current_username)])

# Directorios de almacenamiento
DB_PATH = settings.db_path


def _get_connection():
    """Obtiene una conexión a la base de datos (SQLite o PostgreSQL)."""
    from app.db.database import get_connection as _db_get_connection
    return _db_get_connection()


def _generar_recarga_token(conn, aceptacion_id: int, horas: int = 72) -> str:
    """Genera y guarda un token de re-carga válido por `horas` horas. Retorna el token."""
    import secrets
    from datetime import timedelta
    from app.db.database import sql_placeholders
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=horas)).replace(microsecond=0).isoformat() + "Z"
    cur = conn.cursor()
    cur.execute(
        f"UPDATE aceptaciones SET recarga_token = {sql_placeholders(1, conn)}, "
        f"recarga_token_expires_at = {sql_placeholders(1, conn)}, "
        f"recarga_token_usado = 0 WHERE id = {sql_placeholders(1, conn)}",
        (token, expires_at, aceptacion_id),
    )
    return token


def _log_historial(conn, aceptacion_id: int, evento_id: int, accion: str, realizado_por: str, detalle: str = None):
    """Inserta una entrada en aceptaciones_historial."""
    from app.db.database import sql_placeholders
    fecha = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    try:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO aceptaciones_historial (aceptacion_id, evento_id, accion, realizado_por, fecha, detalle) "
            f"VALUES ({sql_placeholders(6, conn)})",
            (aceptacion_id, evento_id, accion, realizado_por, fecha, detalle),
        )
    except Exception as e:
        app_logger.warning(f"No se pudo registrar historial: {e}")


def get_evento(evento_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene un evento por id."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM eventos WHERE id = %s", (evento_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def listar_eventos() -> List[Dict[str, Any]]:
    """Lista todos los eventos para filtrado."""
    conn = _get_connection()
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
    friendly_intro: int
) -> int:
    """Crea un nuevo evento y devuelve su ID."""
    conn = _get_connection()
    try:
        from app.db.database import sql_placeholders, is_postgres_connection
        cur = conn.cursor()
        ph = sql_placeholders(10, conn)
        if is_postgres_connection(conn):
            cur.execute(
                f"INSERT INTO eventos (nombre, fecha, organizador, activo, req_firma, req_documento, req_salud, req_audio, deslinde_version, friendly_intro) VALUES ({ph}) RETURNING id",
                (nombre, fecha, organizador, activo, req_firma, req_documento, req_salud, req_audio, deslinde_version, friendly_intro)
            )
            row = cur.fetchone()
            evento_id = row['id'] if row else None
        else:
            cur.execute(
                f"INSERT INTO eventos (nombre, fecha, organizador, activo, req_firma, req_documento, req_salud, req_audio, deslinde_version, friendly_intro) VALUES ({ph})",
                (nombre, fecha, organizador, activo, req_firma, req_documento, req_salud, req_audio, deslinde_version, friendly_intro)
            )
            evento_id = cur.lastrowid
        conn.commit()
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
    friendly_intro: int
) -> bool:
    """Actualiza un evento existente."""
    conn = _get_connection()
    try:
        from app.db.database import sql_placeholders
        cur = conn.cursor()
        ph = sql_placeholders(1, conn)
        cur.execute(
            f"UPDATE eventos SET nombre={ph}, fecha={ph}, organizador={ph}, activo={ph}, "
            f"req_firma={ph}, req_documento={ph}, req_salud={ph}, req_audio={ph}, "
            f"deslinde_version={ph}, friendly_intro={ph} WHERE id={ph}",
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
    conn = _get_connection()
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
            conditions.append("a.evento_id = %s")
            params.append(evento_id)

        if query:
            q_norm = "".join(filter(str.isdigit, query))

            clauses = ["a.nombre_participante LIKE %s"]
            params_list = [f"%{query}%"]

            if len(q_norm) >= 3:
                clauses.append("a.documento_norm LIKE %s")
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
    conn = _get_connection()
    try:
        cur = conn.cursor()
        placeholders = ','.join('?' * len(ids))
        sql = f"DELETE FROM aceptaciones WHERE id IN ({placeholders})"
        cur.execute(sql, ids)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def eliminar_evento_completo(evento_id: int) -> bool:
    """Elimina un evento y todas sus referencias."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM aceptaciones WHERE evento_id = %s", (evento_id,))
        cur.execute("DELETE FROM eventos WHERE id = %s", (evento_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def get_aceptacion_detalle(aceptacion_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene detalle completo de una aceptación con verificación de existencia de archivos."""
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
                a.pdf_access_count,
                a.email,
                a.valido,
                a.motivo_anulacion,
                a.fecha_anulacion,
                a.anulado_por,
                a.estado_revision,
                a.motivo_rechazo,
                a.revisado_por,
                a.fecha_revision
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            WHERE a.id = %s
            """,
            (aceptacion_id,)
        )
        row = cur.fetchone()
        if not row:
            return None

        data = dict(row)

        data['firma_exists'] = os.path.exists(data['firma_path']) if data['firma_path'] else False
        data['doc_frente_exists'] = os.path.exists(data['doc_frente_path']) if data['doc_frente_path'] else False
        data['doc_dorso_exists'] = os.path.exists(data['doc_dorso_path']) if data['doc_dorso_path'] else False
        data['audio_exists'] = os.path.exists(data['audio_path']) if data['audio_path'] else False
        data['salud_doc_exists'] = os.path.exists(data['salud_doc_path']) if data['salud_doc_path'] else False

        return data
    finally:
        conn.close()


def revocar_pdf_token(aceptacion_id: int) -> bool:
    """Revoca el token PDF de una aceptación (soft revoke)."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE aceptaciones SET pdf_token_revoked = 1 WHERE id = %s",
            (aceptacion_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def calcular_hash_archivo(filepath: str) -> str:
    """Calcula SHA256 de un archivo en disco."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


# ------------------------------------------------------------------------------
# Rutas admin
# ------------------------------------------------------------------------------

# ADMIN PATCH: fix admin home auth (moved security block up)
# ADMIN PATCH: admin home v1
@router.get("", response_class=HTMLResponse)
@router.get("/home", response_class=HTMLResponse)
def admin_home(username: str = Depends(get_current_username)) -> HTMLResponse:
    """Dashboard principal de administración."""

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
                content: "\u2192";
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
                    <div class="card-icon">&#128197;</div>
                    <div class="card-title">Gestión de Eventos</div>
                    <div class="card-desc">Crear, editar y configurar eventos activos. Obtener enlaces públicos.</div>
                    <div class="card-action">Ir a Eventos</div>
                </a>

                <!-- Aceptaciones -->
                <a href="/admin/aceptaciones" class="card">
                    <div class="card-icon">&#128221;</div>
                    <div class="card-title">Aceptaciones</div>
                    <div class="card-desc">Listado completo de deslindes firmados. Filtrar, buscar y verificar evidencias.</div>
                    <div class="card-action">Ver Registros</div>
                </a>

                <!-- Exportes -->
                <a href="/admin/eventos" class="card">
                    <div class="card-icon">&#128203;</div>
                    <div class="card-title">Exportar CSV</div>
                    <div class="card-desc">Descargar planilla CSV con el detalle de deslindes para cruzar con inscriptos.</div>
                    <div class="card-action">Ir a Aceptaciones</div>
                </a>

                <!-- Monitor en Vivo -->
                <a href="/admin/eventos" class="card">
                    <div class="card-icon">&#128250;</div>
                    <div class="card-title">Monitor de Entrada</div>
                    <div class="card-desc">Pantalla de validación en tiempo real para operadores de acceso.</div>
                    <div class="card-action">Seleccionar Evento</div>
                </a>

                <!-- Operadores -->
                <a href="/admin/operadores" class="card">
                    <div class="card-icon">&#128101;</div>
                    <div class="card-title">Operadores</div>
                    <div class="card-desc">Gestionar usuarios con acceso restringido al monitor y CSV por evento.</div>
                    <div class="card-action">Gestionar</div>
                </a>

                <!-- Búsqueda Global -->
                <a href="/admin/search" class="card">
                    <div class="card-icon">&#128269;</div>
                    <div class="card-title">Búsqueda Global</div>
                    <div class="card-desc">Buscar deslindes por DNI o apellido en todos los eventos históricos.</div>
                    <div class="card-action">Buscar ahora</div>
                </a>

                <div class="card disabled">
                    <div class="card-icon">&#128202;</div>
                    <span class="badge">Próximamente</span>
                    <div class="card-title">Estado del Sistema</div>
                    <div class="card-desc">Métricas de disco, uso de CPU y estado de servicios.</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content.replace("{{ username }}", username))
# /ADMIN PATCH


# ADMIN PATCH: admin search deslindes
@router.get("/search", response_class=HTMLResponse)
def admin_search(q: Optional[str] = None, username: str = Depends(get_current_username)) -> HTMLResponse:
    """Búsqueda transversal de deslindes."""
    resultados = []
    if q:
        resultados = listar_aceptaciones(query=q)[:50]

    template = templates_env.get_template("admin_busqueda_deslindes.html")
    html = template.render(query=q, resultados=resultados, username=username)
    return HTMLResponse(content=html)
# /ADMIN PATCH


@router.get("/eventos", response_class=HTMLResponse)
def admin_eventos(username: str = Depends(get_current_username)) -> HTMLResponse:
    """Listado de eventos para administración."""
    eventos = listar_eventos()
    template = templates_env.get_template("admin_eventos_lista.html")
    html = template.render(eventos=eventos, username=username)
    return HTMLResponse(content=html)


@router.get("/eventos/nuevo", response_class=HTMLResponse)
def admin_evento_nuevo_form(username: str = Depends(get_current_username)) -> HTMLResponse:
    """Formulario para crear evento."""
    template = templates_env.get_template("admin_eventos_form.html")
    html = template.render(evento=None, username=username)
    return HTMLResponse(content=html)


@router.post("/eventos/nuevo")
def admin_evento_nuevo_post(
    nombre: str = Form(...),
    fecha: str = Form(...),
    organizador: str = Form(...),
    activo: Optional[int] = Form(0),
    req_firma: Optional[int] = Form(0),
    req_documento: Optional[int] = Form(0),
    req_salud: Optional[int] = Form(0),
    req_audio: Optional[int] = Form(0),
    friendly_intro: Optional[int] = Form(0),  # DESLINDE PATCH: friendly intro
    deslinde_version: str = Form("v1_1"),
    username: str = Depends(get_current_username)
):
    """Procesa creación de evento."""
    try:
        if not nombre.strip() or not organizador.strip():
            raise HTTPException(status_code=400, detail="Nombre y organizador son obligatorios")

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
            friendly_intro=friendly_intro or 0  # DESLINDE PATCH: friendly intro
        )

    except Exception as e:
        app_logger.error(f"Error creando evento: {e}")
        raise HTTPException(status_code=500, detail=f"Error creando evento: {e}")

    # ADMIN PATCH: fix try except syntax
    return RedirectResponse(url="/admin/eventos", status_code=303)


@router.get("/eventos/{evento_id}/editar", response_class=HTMLResponse)
def admin_evento_editar_form(evento_id: int, username: str = Depends(get_current_username)) -> HTMLResponse:
    """Formulario para editar evento."""
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")

    template = templates_env.get_template("admin_eventos_form.html")
    html = template.render(evento=evento, username=username)
    return HTMLResponse(content=html)


@router.post("/eventos/{evento_id}/editar")
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
    friendly_intro: Optional[int] = Form(0),  # DESLINDE PATCH: friendly intro
    deslinde_version: str = Form(...),
    username: str = Depends(get_current_username)
):
    """Procesa edición de evento."""
    try:
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
            friendly_intro=friendly_intro or 0  # DESLINDE PATCH: friendly intro
        )

        return RedirectResponse(url="/admin/eventos", status_code=303)
    except Exception as e:
        app_logger.error(f"Error editando evento {evento_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error editando evento: {e}")


@router.get("/aceptaciones", response_class=HTMLResponse)
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

    context = {
        "aceptaciones": datos,
        "eventos": eventos,
        "filtro_evento_id": evento_id,
        "username": username
    }

    template = templates_env.get_template("admin_aceptaciones.html")
    html = template.render(**context)
    return HTMLResponse(content=html)


@router.get("/aceptaciones/{aceptacion_id}/pdf")
def admin_descargar_pdf_aceptacion(
    aceptacion_id: int,
    username: str = Depends(get_current_username)
):
    """Genera PDF legal de la aceptación."""
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")

    evento = get_evento(aceptacion["evento_id"])
    if not evento:
        raise HTTPException(status_code=404, detail="Evento asociado no encontrado")

    pdf_bytes = _generar_bytes_pdf(aceptacion, evento)

    app_logger.info(f"PDF generado para aceptacion_id={aceptacion_id} evento_id={evento['id']}")

    filename = f"aceptacion_{aceptacion_id}.pdf"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"'
    }
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf", headers=headers)


@router.get("/evento/{evento_id}/exportar_csv")
def admin_exportar_csv(
    evento_id: int,
    username: str = Depends(get_current_username)
):
    """
    Genera y descarga un CSV con el detalle de todos los deslindes de un evento.
    Útil para cruzar inscriptos vs. deslindes cargados.
    """
    import csv

    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.documento,
                a.nombre_participante,
                a.fecha_hora,
                a.valido,
                a.firma_path,
                a.doc_frente_path,
                a.doc_dorso_path,
                a.audio_path,
                a.audio_exento,
                a.salud_doc_path,
                a.firma_asistida,
                a.motivo_anulacion,
                a.fecha_anulacion,
                a.anulado_por,
                a.ip,
                a.estado_revision,
                a.revisado_por,
                a.fecha_revision,
                a.motivo_rechazo
            FROM aceptaciones a
            WHERE a.evento_id = %s
            ORDER BY a.fecha_hora ASC
            """,
            (evento_id,)
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    req_firma = bool(evento.get("req_firma"))
    req_documento = bool(evento.get("req_documento"))
    req_audio = bool(evento.get("req_audio"))
    req_salud = bool(evento.get("req_salud"))

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_ALL)

    writer.writerow([
        "cedula",
        "nombre",
        "fecha_hora_registro",
        "estado",
        "revision",
        "motivo_rechazo",
        "revisado_por",
        "fecha_revision",
        "tiene_firma",
        "tiene_doc_frente",
        "tiene_doc_dorso",
        "tiene_audio",
        "audio_exento",
        "tiene_salud",
        "firma_asistida",
        "motivo_anulacion",
        "fecha_anulacion",
        "anulado_por",
        "ip",
    ])

    for a in rows:
        if a.get("valido") == 0:
            estado = "ANULADO"
        else:
            completo = True
            if req_firma and not a.get("firma_path"):
                completo = False
            if req_documento and (not a.get("doc_frente_path") or not a.get("doc_dorso_path")):
                completo = False
            if req_audio and not a.get("audio_path") and not a.get("audio_exento"):
                completo = False
            if req_salud and not a.get("salud_doc_path"):
                completo = False
            estado = "COMPLETO" if completo else "INCOMPLETO"

        writer.writerow([
            a.get("documento", ""),
            a.get("nombre_participante", ""),
            (a.get("fecha_hora") or "").replace("T", " ").replace("Z", ""),
            estado,
            a.get("estado_revision") or "SIN REVISAR",
            a.get("motivo_rechazo") or "",
            a.get("revisado_por") or "",
            (a.get("fecha_revision") or "").replace("T", " ").replace("Z", ""),
            "SI" if a.get("firma_path") else "NO",
            "SI" if a.get("doc_frente_path") else "NO",
            "SI" if a.get("doc_dorso_path") else "NO",
            "SI" if a.get("audio_path") else "NO",
            "SI" if a.get("audio_exento") else "NO",
            "SI" if a.get("salud_doc_path") else "NO",
            "SI" if a.get("firma_asistida") else "NO",
            a.get("motivo_anulacion") or "",
            (a.get("fecha_anulacion") or "").replace("T", " ").replace("Z", ""),
            a.get("anulado_por") or "",
            a.get("ip") or "",
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")  # BOM para Excel

    safe_name = "".join([c for c in evento["nombre"] if c.isalnum() or c in (' ', '_', '-')]).strip().replace(" ", "_")
    filename = f"deslindes_{safe_name}_{evento['fecha']}.csv"

    app_logger.info(f"CSV exportado para evento {evento_id}: {len(rows)} registros.")
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@router.get("/gestion_eliminacion/{evento_id}", response_class=HTMLResponse)
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


@router.post("/eliminar_evento", response_class=HTMLResponse)
def admin_procesar_eliminacion(
    evento_id: int = Form(...),
    tipo_eliminacion: str = Form(...),  # 'parcial' o 'total'
    fecha_corte: Optional[str] = Form(None),  # Para parcial
    username: str = Depends(get_current_username)
) -> HTMLResponse:
    """Procesa la eliminación solicitada."""
    evento = get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")

    msg = ""

    if tipo_eliminacion == "total":
        aceptaciones = listar_aceptaciones(evento_id=evento_id)

        archivos_borrados = borrar_evidencias_fisicas(aceptaciones)

        eliminar_evento_completo(evento_id)

        msg = f"Evento '{evento['nombre']}' eliminado completamente. {len(aceptaciones)} registros y {archivos_borrados} archivos eliminados."

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

        aceptaciones = listar_aceptaciones(evento_id=evento_id)
        a_borrar = []
        ids_borrar = []

        for a in aceptaciones:
            fecha_bd = a['fecha_hora'][:16]  # YYYY-MM-DDTHH:MM
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

        archivos_borrados = borrar_evidencias_fisicas(a_borrar)

        regs_borrados = eliminar_aceptaciones_por_ids(ids_borrar)

        msg = f"Limpieza completada. {regs_borrados} registros y {archivos_borrados} archivos eliminados anteriores a {fecha_corte}."

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


@router.get("/aceptaciones/{aceptacion_id}", response_class=HTMLResponse)
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


@router.post("/aceptaciones/{aceptacion_id}/revocar_token", response_class=HTMLResponse)
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


@router.post("/aceptaciones/{aceptacion_id}/anular", response_class=HTMLResponse)
def admin_anular_aceptacion(
    aceptacion_id: int,
    motivo: str = Form(...),
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    """
    Anula una aceptación: la marca como inválida (valido=0) sin eliminarla.
    Registra motivo, fecha y quién anuló.
    Al anularse, el mismo documento puede volver a registrarse en el evento.
    """
    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")

    if not aceptacion.get("valido", 1):
        raise HTTPException(status_code=400, detail="La aceptación ya está anulada")

    conn = _get_connection()
    try:
        cur = conn.cursor()
        fecha_anulacion = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        cur.execute(
            """
            UPDATE aceptaciones
            SET valido = 0,
                motivo_anulacion = %s,
                fecha_anulacion = %s,
                anulado_por = %s
            WHERE id = %s
            """,
            (motivo.strip(), fecha_anulacion, username, aceptacion_id),
        )
        _log_historial(conn, aceptacion_id, aceptacion["evento_id"], "ANULADO", username,
                       json.dumps({"motivo": motivo.strip()}, ensure_ascii=False))
        conn.commit()
        app_logger.info(
            f"Aceptación anulada: id={aceptacion_id}, evento_id={aceptacion['evento_id']}, "
            f"doc={aceptacion['documento']}, motivo='{motivo}', por={username}"
        )
    except Exception as e:
        app_logger.error(f"Error anulando aceptación {aceptacion_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error al anular: {e}")
    finally:
        conn.close()

    evento_id = aceptacion["evento_id"]
    return HTMLResponse(
        content=f"""
        <script>
            alert("Aceptación anulada correctamente.");
            window.location.href = "/admin/evento/{evento_id}/monitor";
        </script>
        """
    )


@router.post("/aceptaciones/{aceptacion_id}/revisar", response_class=HTMLResponse)
def admin_revisar_aceptacion(
    aceptacion_id: int,
    decision: str = Form(...),
    motivo: str = Form(""),
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    """
    Marca una aceptación como ACEPTADO o RECHAZADO.
    El motivo es obligatorio cuando se rechaza.
    Registra la acción en aceptaciones_historial.
    """
    decision = decision.upper()
    if decision not in ("ACEPTADO", "RECHAZADO"):
        raise HTTPException(status_code=400, detail="Decisión inválida. Use ACEPTADO o RECHAZADO.")

    if decision == "RECHAZADO" and not motivo.strip():
        raise HTTPException(status_code=400, detail="El motivo es obligatorio al rechazar.")

    aceptacion = get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")

    if not aceptacion.get("valido", 1):
        raise HTTPException(status_code=400, detail="No se puede revisar una aceptación anulada.")

    from app.db.database import sql_placeholders
    fecha_revision = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    motivo_rechazo = motivo.strip() if decision == "RECHAZADO" else None

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE aceptaciones SET estado_revision = {sql_placeholders(1, conn)}, "
            f"revisado_por = {sql_placeholders(1, conn)}, "
            f"fecha_revision = {sql_placeholders(1, conn)}, "
            f"motivo_rechazo = {sql_placeholders(1, conn)} "
            f"WHERE id = {sql_placeholders(1, conn)}",
            (decision, username, fecha_revision, motivo_rechazo, aceptacion_id),
        )
        recarga_token = None
        if decision == "RECHAZADO" and aceptacion.get("email"):
            recarga_token = _generar_recarga_token(conn, aceptacion_id)
        detalle = json.dumps({"decision": decision, "motivo": motivo_rechazo}, ensure_ascii=False)
        _log_historial(conn, aceptacion_id, aceptacion["evento_id"], f"REVISION_{decision}", username, detalle)
        conn.commit()
        app_logger.info(
            f"Revisión registrada: id={aceptacion_id}, decision={decision}, "
            f"doc={aceptacion['documento']}, por={username}"
        )
    except Exception as e:
        app_logger.error(f"Error revisando aceptación {aceptacion_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error al revisar: {e}")
    finally:
        conn.close()

    if decision == "RECHAZADO" and aceptacion.get("email"):
        from app.email import send_rechazo_email
        send_rechazo_email(
            email=aceptacion["email"],
            nombre=aceptacion["nombre_participante"],
            evento_nombre=aceptacion["evento_nombre"],
            motivo=motivo_rechazo,
            revisado_por=username,
            recarga_token=recarga_token,
        )

    evento_id = aceptacion["evento_id"]
    return HTMLResponse(
        content=f"""
        <script>
            window.location.href = "/admin/evento/{evento_id}/monitor";
        </script>
        """
    )


@router.get("/evento/{evento_id}/monitor", response_class=HTMLResponse)
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

    conn = _get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT COUNT(*) AS c FROM aceptaciones WHERE evento_id = %s AND valido = 1",
            (evento_id,),
        )
        row = cur.fetchone()
        total_deslindes = row["c"] if row else 0

        cur.execute(
            "SELECT COUNT(*) AS c FROM aceptaciones WHERE evento_id = %s AND valido = 0",
            (evento_id,),
        )
        row = cur.fetchone()
        total_anulados = row["c"] if row else 0

        where_clauses = ["a.evento_id = %s"]
        params_base: List[Any] = [evento_id]

        if q:
            q_norm = "".join(filter(str.isdigit, q))
            clauses = ["a.nombre_participante LIKE %s"]
            params_q: List[Any] = [f"%{q}%"]

            if len(q_norm) >= 3:
                clauses.append("a.documento_norm LIKE %s")
                params_q.append(f"%{q_norm}%")

            where_clauses.append(f"({' OR '.join(clauses)})")
            params_base.extend(params_q)

        where_sql = " AND ".join(where_clauses)

        sql_count = f"""
            SELECT COUNT(*) AS c
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            WHERE {where_sql}
        """
        cur.execute(sql_count, tuple(params_base))
        row = cur.fetchone()
        total_filtrado = row["c"] if row else 0

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
                a.firma_asistida,
                a.valido,
                a.motivo_anulacion,
                a.fecha_anulacion,
                a.anulado_por,
                a.estado_revision,
                a.revisado_por,
                a.fecha_revision,
                a.motivo_rechazo
            FROM aceptaciones a
            JOIN eventos e ON e.id = a.evento_id
            WHERE {where_sql}
            ORDER BY a.fecha_hora DESC
            LIMIT %s OFFSET %s
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
        total_anulados=total_anulados,
        page=page,
        has_prev=has_prev,
        has_next=has_next,
    )
    return HTMLResponse(content=html)


@router.get("/evento/{evento_id}/preview/{aceptacion_id}", response_class=HTMLResponse)
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


@router.get("/evidencia/{aceptacion_id}/{tipo}")
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
        media_type = "image/png"
    elif tipo == "doc_frente":
        file_path = aceptacion.get("doc_frente_path")
        media_type = "image/jpeg"
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
        raise HTTPException(status_code=404, detail="Evidencia no encontrada")

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
                img.thumbnail((400, 400))
                buf = io.BytesIO()

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
            app_logger.error(f"Error generando thumbnail para {file_path}: {e}")

    def iterfile():
        with open(file_path, mode="rb") as file_like:
            yield from file_like

    return StreamingResponse(iterfile(), media_type=media_type)


# ADMIN PATCH: serve local evidences
@router.get("/evidence/view/{aceptacion_id}/{tipo}")
def admin_ver_evidencia_full(
    aceptacion_id: int,
    tipo: str,
    username: str = Depends(get_current_username)
):
    """
    Endpoint dedicado para visualizar evidencias en navegador (FileResponse).
    Solo lectura. No expone path real.
    """
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


# ===========================================================================
# Gestión de operadores
# ===========================================================================

def _listar_eventos_simple():
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, nombre, fecha FROM eventos ORDER BY fecha DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _listar_operadores():
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, username, evento_ids, activo, created_at FROM operadores ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@router.get("/operadores", response_class=HTMLResponse)
def admin_operadores(
    msg: Optional[str] = None,
    error: Optional[str] = None,
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    """Listado y gestión de operadores."""
    template = templates_env.get_template("admin_operadores.html")
    html = template.render(
        username=username,
        operadores=_listar_operadores(),
        eventos=_listar_eventos_simple(),
        msg=msg,
        error=error,
    )
    return HTMLResponse(content=html)


@router.post("/operadores/nuevo", response_class=HTMLResponse)
def admin_operadores_nuevo(
    username_op: str = Form(..., alias="username"),
    password: str = Form(...),
    evento_ids: List[int] = Form(default=[]),
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    """Crea un nuevo operador."""
    from app.middleware.auth_operator import hash_password

    username_op = username_op.strip()
    if not username_op or len(password) < 8:
        return RedirectResponse(
            url="/admin/operadores?error=Usuario+inv%C3%A1lido+o+contrase%C3%B1a+menor+a+8+caracteres",
            status_code=303
        )

    ids_str = ",".join(str(i) for i in evento_ids)
    pwd_hash = hash_password(password)
    created_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO operadores (username, password_hash, evento_ids, activo, created_at) VALUES (%s, %s, %s, 1, %s)",
            (username_op, pwd_hash, ids_str, created_at)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        app_logger.error(f"Error creando operador '{username_op}': {e}")
        return RedirectResponse(
            url=f"/admin/operadores?error=El+usuario+ya+existe+o+hubo+un+error",
            status_code=303
        )
    finally:
        conn.close()

    app_logger.info(f"Operador creado: {username_op} por admin {username}")
    return RedirectResponse(url=f"/admin/operadores?msg=Operador+{username_op}+creado+correctamente", status_code=303)


@router.post("/operadores/{op_id}/toggle", response_class=HTMLResponse)
def admin_operadores_toggle(
    op_id: int,
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    """Activa o desactiva un operador."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT activo, username FROM operadores WHERE id = %s", (op_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Operador no encontrado")
        nuevo_estado = 0 if row["activo"] else 1
        cur.execute("UPDATE operadores SET activo = %s WHERE id = %s", (nuevo_estado, op_id))
        conn.commit()
        op_username = row["username"]
    finally:
        conn.close()

    app_logger.info(f"Operador {op_username} {'activado' if nuevo_estado else 'desactivado'} por {username}")
    return RedirectResponse(url="/admin/operadores?msg=Estado+actualizado", status_code=303)


@router.post("/operadores/{op_id}/eventos", response_class=HTMLResponse)
def admin_operadores_eventos(
    op_id: int,
    evento_ids: List[int] = Form(default=[]),
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    """Actualiza los eventos asignados a un operador."""
    ids_str = ",".join(str(i) for i in evento_ids)
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE operadores SET evento_ids = %s WHERE id = %s", (ids_str, op_id))
        conn.commit()
    finally:
        conn.close()

    app_logger.info(f"Eventos de operador {op_id} actualizados a [{ids_str}] por {username}")
    return RedirectResponse(url="/admin/operadores?msg=Eventos+actualizados", status_code=303)


@router.post("/operadores/{op_id}/password", response_class=HTMLResponse)
def admin_operadores_password(
    op_id: int,
    password: str = Form(...),
    username: str = Depends(get_current_username),
) -> HTMLResponse:
    """Cambia la contraseña de un operador."""
    from app.middleware.auth_operator import hash_password

    if len(password) < 8:
        return RedirectResponse(url="/admin/operadores?error=Contrase%C3%B1a+menor+a+8+caracteres", status_code=303)

    pwd_hash = hash_password(password)
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE operadores SET password_hash = %s WHERE id = %s", (pwd_hash, op_id))
        conn.commit()
    finally:
        conn.close()

    app_logger.info(f"Contraseña de operador {op_id} actualizada por {username}")
    return RedirectResponse(url="/admin/operadores?msg=Contrase%C3%B1a+actualizada", status_code=303)
