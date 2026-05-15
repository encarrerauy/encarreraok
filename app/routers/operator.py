"""
Rutas de operador de evento para EncarreraOK.

Los operadores tienen acceso restringido: solo pueden actuar sobre los eventos
que tengan asignados. Autenticación: HTTP Basic con credenciales de operador
(tabla operadores).

Endpoints:
  GET  /op/{evento_id}/monitor                            - Monitor del evento
  GET  /op/{evento_id}/preview/{aceptacion_id}            - Vista de deslinde + evidencias
  GET  /op/{evento_id}/evidencia/{aceptacion_id}/{tipo}   - Sirve archivo de evidencia
  POST /op/{evento_id}/aceptaciones/{id}/anular           - Anular deslinde
  GET  /op/{evento_id}/exportar_csv                       - Descarga CSV del evento
"""

import csv
import io
import json
import logging
import os
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from app.middleware.auth_operator import get_current_operator, check_evento_access
from app.templates_config import templates_env

app_logger = logging.getLogger("encarreraok")

router = APIRouter(prefix="/op")


def _get_connection():
    from app.db.database import get_connection
    return get_connection()


def _generar_recarga_token(conn, aceptacion_id: int, horas: int = 72) -> str:
    """Genera y guarda un token de re-carga válido por `horas` horas."""
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


def _get_evento(evento_id: int) -> Optional[dict]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM eventos WHERE id = %s", (evento_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

@router.get("/{evento_id}/monitor", response_class=HTMLResponse)
def op_monitor(
    evento_id: int,
    q: Optional[str] = None,
    page: int = 1,
    operador: dict = Depends(get_current_operator),
) -> HTMLResponse:
    """Monitor de entrada (solo lectura) para operadores de evento."""
    check_evento_access(operador, evento_id)

    evento = _get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")

    page_size = 25
    if page < 1:
        page = 1
    offset = (page - 1) * page_size

    conn = _get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT COUNT(*) AS c FROM aceptaciones WHERE evento_id = %s AND valido = 1",
            (evento_id,)
        )
        row = cur.fetchone()
        total_deslindes = row["c"] if row else 0

        cur.execute(
            "SELECT COUNT(*) AS c FROM aceptaciones WHERE evento_id = %s AND valido = 0",
            (evento_id,)
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

        sql_count = f"SELECT COUNT(*) AS c FROM aceptaciones a WHERE {where_sql}"
        cur.execute(sql_count, tuple(params_base))
        row = cur.fetchone()
        total_filtrado = row["c"] if row else 0

        sql_list = f"""
            SELECT
                a.id, a.evento_id, a.nombre_participante, a.documento,
                a.fecha_hora, a.valido,
                a.firma_path, a.doc_frente_path, a.doc_dorso_path,
                a.audio_path, a.audio_exento, a.salud_doc_path,
                a.motivo_anulacion, a.fecha_anulacion, a.anulado_por,
                a.estado_revision, a.revisado_por, a.fecha_revision, a.motivo_rechazo
            FROM aceptaciones a
            WHERE {where_sql}
            ORDER BY a.fecha_hora DESC
            LIMIT %s OFFSET %s
        """
        params_list = list(params_base) + [page_size, offset]
        cur.execute(sql_list, tuple(params_list))
        aceptaciones = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    has_prev = page > 1
    has_next = page * page_size < total_filtrado

    template = templates_env.get_template("op_monitor_evento.html")
    html = template.render(
        evento=evento,
        aceptaciones=aceptaciones,
        query=q,
        username=operador["username"],
        total_deslindes=total_deslindes,
        total_anulados=total_anulados,
        page=page,
        has_prev=has_prev,
        has_next=has_next,
    )
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Preview + evidencias
# ---------------------------------------------------------------------------

def _get_aceptacion_detalle(aceptacion_id: int) -> Optional[dict]:
    """Detalle completo de una aceptación (igual que admin, sin datos de token PDF)."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.id, a.evento_id,
                e.nombre AS evento_nombre, e.fecha AS evento_fecha,
                e.req_firma, e.req_documento, e.req_audio, e.req_salud,
                a.nombre_participante, a.documento, a.fecha_hora, a.ip,
                a.firma_path, a.doc_frente_path, a.doc_dorso_path,
                a.audio_path, a.audio_exento, a.salud_doc_path, a.salud_doc_tipo,
                a.firma_asistida, a.valido,
                a.motivo_anulacion, a.fecha_anulacion, a.anulado_por
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
        data["firma_exists"]     = os.path.exists(data["firma_path"])     if data.get("firma_path")     else False
        data["doc_frente_exists"]= os.path.exists(data["doc_frente_path"])if data.get("doc_frente_path")else False
        data["doc_dorso_exists"] = os.path.exists(data["doc_dorso_path"]) if data.get("doc_dorso_path") else False
        data["audio_exists"]     = os.path.exists(data["audio_path"])     if data.get("audio_path")     else False
        data["salud_doc_exists"] = os.path.exists(data["salud_doc_path"]) if data.get("salud_doc_path") else False
        return data
    finally:
        conn.close()


@router.get("/{evento_id}/preview/{aceptacion_id}", response_class=HTMLResponse)
def op_preview(
    evento_id: int,
    aceptacion_id: int,
    operador: dict = Depends(get_current_operator),
) -> HTMLResponse:
    """Vista de deslinde + evidencias para el operador. Solo lectura."""
    check_evento_access(operador, evento_id)

    aceptacion = _get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
    if aceptacion["evento_id"] != evento_id:
        raise HTTPException(status_code=403, detail="La aceptación no pertenece a este evento")

    evento = _get_evento(evento_id)
    template = templates_env.get_template("op_preview.html")
    html = template.render(
        evento=evento,
        aceptacion=aceptacion,
        username=operador["username"],
        evento_id=evento_id,
    )
    return HTMLResponse(content=html)


@router.get("/{evento_id}/evidencia/{aceptacion_id}/{tipo}")
def op_servir_evidencia(
    evento_id: int,
    aceptacion_id: int,
    tipo: str,
    thumbnail: bool = False,
    operador: dict = Depends(get_current_operator),
):
    """
    Sirve archivos de evidencia protegidos para el operador.
    Valida que el operador tenga acceso al evento de la aceptación.
    tipo: 'firma', 'doc_frente', 'doc_dorso', 'audio', 'salud_doc'
    """
    check_evento_access(operador, evento_id)

    aceptacion = _get_aceptacion_detalle(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
    if aceptacion["evento_id"] != evento_id:
        raise HTTPException(status_code=403, detail="La aceptación no pertenece a este evento")

    tipo_map = {
        "firma":      ("firma_path",      "image/png"),
        "doc_frente": ("doc_frente_path", "image/jpeg"),
        "doc_dorso":  ("doc_dorso_path",  "image/jpeg"),
        "audio":      ("audio_path",      "audio/webm"),
        "salud_doc":  ("salud_doc_path",  "image/jpeg"),
    }
    if tipo not in tipo_map:
        raise HTTPException(status_code=400, detail="Tipo de evidencia inválido")

    field, media_type = tipo_map[tipo]
    file_path = aceptacion.get(field)

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Evidencia no encontrada en disco")

    _, ext = os.path.splitext(file_path)
    ext = ext.lower()
    if ext in (".jpg", ".jpeg"):
        media_type = "image/jpeg"
    elif ext == ".png":
        media_type = "image/png"
    elif ext == ".webm":
        media_type = "audio/webm"
    elif ext == ".pdf":
        media_type = "application/pdf"

    # Thumbnail para preview rápido
    if thumbnail and media_type.startswith("image/"):
        try:
            from PIL import Image
            with Image.open(file_path) as img:
                img.thumbnail((400, 400))
                buf = io.BytesIO()
                fmt = "PNG" if media_type == "image/png" else "JPEG"
                if fmt == "JPEG" and img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(buf, format=fmt, quality=70)
                buf.seek(0)
                return StreamingResponse(buf, media_type=media_type)
        except Exception as e:
            app_logger.warning(f"[op:{operador['username']}] Thumbnail falló para {file_path}: {e}")

    def iterfile():
        with open(file_path, "rb") as f:
            yield from f

    return StreamingResponse(iterfile(), media_type=media_type)


# ---------------------------------------------------------------------------
# Anular deslinde
# ---------------------------------------------------------------------------

def _get_aceptacion(aceptacion_id: int) -> Optional[dict]:
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, evento_id, documento, nombre_participante, valido, email FROM aceptaciones WHERE id = %s",
            (aceptacion_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@router.post("/{evento_id}/aceptaciones/{aceptacion_id}/anular", response_class=HTMLResponse)
def op_anular_aceptacion(
    evento_id: int,
    aceptacion_id: int,
    motivo: str = Form(...),
    operador: dict = Depends(get_current_operator),
) -> HTMLResponse:
    """
    Anula una aceptación desde el panel de operador.
    La marca como inválida (valido=0) sin eliminarla.
    Registra motivo, fecha UTC y el username del operador en anulado_por.
    Al anularse, el mismo documento puede volver a registrarse en el evento.
    """
    check_evento_access(operador, evento_id)

    aceptacion = _get_aceptacion(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")

    # Verifica que la aceptación pertenezca al evento del operador
    if aceptacion["evento_id"] != evento_id:
        raise HTTPException(status_code=403, detail="La aceptación no pertenece a este evento")

    if not aceptacion.get("valido", 1):
        raise HTTPException(status_code=400, detail="La aceptación ya está anulada")

    op_username = operador["username"]
    fecha_anulacion = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE aceptaciones
            SET valido = 0,
                motivo_anulacion = %s,
                fecha_anulacion = %s,
                anulado_por = %s
            WHERE id = %s
            """,
            (motivo.strip(), fecha_anulacion, op_username, aceptacion_id),
        )
        _log_historial(conn, aceptacion_id, evento_id, "ANULADO", op_username,
                       json.dumps({"motivo": motivo.strip()}, ensure_ascii=False))
        conn.commit()
        app_logger.info(
            f"[op:{op_username}] Aceptación anulada: id={aceptacion_id}, "
            f"evento_id={evento_id}, doc={aceptacion['documento']}, "
            f"nombre='{aceptacion['nombre_participante']}', motivo='{motivo.strip()}'"
        )
    except Exception as e:
        conn.rollback()
        app_logger.error(f"[op:{op_username}] Error anulando aceptación {aceptacion_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error al anular: {e}")
    finally:
        conn.close()

    return HTMLResponse(content=f"""
        <script>
            alert("Deslinde anulado correctamente.");
            window.location.href = "/op/{evento_id}/monitor";
        </script>
    """)


@router.post("/{evento_id}/aceptaciones/{aceptacion_id}/revisar", response_class=HTMLResponse)
def op_revisar_aceptacion(
    evento_id: int,
    aceptacion_id: int,
    decision: str = Form(...),
    motivo: str = Form(""),
    operador: dict = Depends(get_current_operator),
) -> HTMLResponse:
    """Marca una aceptación como ACEPTADO o RECHAZADO desde el panel de operador."""
    check_evento_access(operador, evento_id)

    decision = decision.upper()
    if decision not in ("ACEPTADO", "RECHAZADO"):
        raise HTTPException(status_code=400, detail="Decisión inválida.")
    if decision == "RECHAZADO" and not motivo.strip():
        raise HTTPException(status_code=400, detail="El motivo es obligatorio al rechazar.")

    aceptacion = _get_aceptacion(aceptacion_id)
    if not aceptacion:
        raise HTTPException(status_code=404, detail="Aceptación no encontrada")
    if aceptacion["evento_id"] != evento_id:
        raise HTTPException(status_code=403, detail="La aceptación no pertenece a este evento")
    if not aceptacion.get("valido", 1):
        raise HTTPException(status_code=400, detail="No se puede revisar una aceptación anulada.")

    from app.db.database import sql_placeholders
    op_username = operador["username"]
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
            (decision, op_username, fecha_revision, motivo_rechazo, aceptacion_id),
        )
        recarga_token = None
        if decision == "RECHAZADO" and aceptacion.get("email"):
            recarga_token = _generar_recarga_token(conn, aceptacion_id)
        _log_historial(conn, aceptacion_id, evento_id, f"REVISION_{decision}", op_username,
                       json.dumps({"decision": decision, "motivo": motivo_rechazo}, ensure_ascii=False))
        conn.commit()
        app_logger.info(f"[op:{op_username}] Revisión id={aceptacion_id}, decision={decision}")
    except Exception as e:
        app_logger.error(f"[op:{op_username}] Error revisando aceptación {aceptacion_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error al revisar: {e}")
    finally:
        conn.close()

    if decision == "RECHAZADO" and aceptacion.get("email"):
        from app.email import send_rechazo_email
        evento = _get_evento(evento_id)
        send_rechazo_email(
            email=aceptacion["email"],
            nombre=aceptacion["nombre_participante"],
            evento_nombre=evento["nombre"] if evento else str(evento_id),
            motivo=motivo_rechazo,
            revisado_por=op_username,
            recarga_token=recarga_token,
        )

    return HTMLResponse(content=f"""
        <script>window.location.href = "/op/{evento_id}/monitor";</script>
    """)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

@router.get("/{evento_id}/exportar_csv")
def op_exportar_csv(
    evento_id: int,
    operador: dict = Depends(get_current_operator),
):
    """Descarga CSV del evento para el operador."""
    check_evento_access(operador, evento_id)

    evento = _get_evento(evento_id)
    if not evento:
        raise HTTPException(status_code=404, detail="Evento no encontrado")

    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                a.documento, a.nombre_participante, a.fecha_hora,
                a.valido, a.firma_path, a.doc_frente_path, a.doc_dorso_path,
                a.audio_path, a.audio_exento, a.salud_doc_path,
                a.firma_asistida, a.motivo_anulacion, a.fecha_anulacion, a.anulado_por,
                a.estado_revision, a.revisado_por, a.fecha_revision, a.motivo_rechazo
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
        "cedula", "nombre", "fecha_hora_registro", "estado",
        "revision", "motivo_rechazo", "revisado_por", "fecha_revision",
        "tiene_firma", "tiene_doc_frente", "tiene_doc_dorso",
        "tiene_audio", "audio_exento", "tiene_salud", "firma_asistida",
        "motivo_anulacion", "fecha_anulacion", "anulado_por",
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
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    safe_name = "".join([c for c in evento["nombre"] if c.isalnum() or c in (' ', '_', '-')]).strip().replace(" ", "_")
    filename = f"deslindes_{safe_name}_{evento['fecha']}.csv"

    app_logger.info(f"[op:{operador['username']}] CSV exportado para evento {evento_id}: {len(rows)} registros.")
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
