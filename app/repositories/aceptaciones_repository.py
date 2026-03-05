import sqlite3
from typing import List, Dict, Any, Optional
from app.db.database import get_connection

def crear_aceptacion(
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
    deslinde_version: str = "v1_1",
) -> int:
    """Inserta una aceptación en la base de datos y devuelve el ID creado."""
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
    hash_sha256: str,
    fecha_creacion: str,
    activo: int = 1,
    creado_por: str = "sistema",
) -> int:
    """Inserta un deslinde para un evento."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO deslindes (evento_id, texto, hash_sha256, activo, fecha_creacion, creado_por)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (evento_id, texto, hash_sha256, activo, fecha_creacion, creado_por),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def existe_aceptacion(evento_id: int, documento_norm: str) -> bool:
    """
    Verifica si existe una aceptación válida para un evento y documento normalizado.
    Maneja la compatibilidad con esquemas antiguos (columna 'valido').
    """
    if not documento_norm:
        return False
        
    conn = get_connection()
    try:
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

def buscar_aceptacion_por_id(aceptacion_id: int) -> Optional[Dict[str, Any]]:
    """Obtiene detalle completo de una aceptación por ID (sin verificar archivos en disco)."""
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
        return dict(row)
    finally:
        conn.close()

def buscar_aceptacion_por_token(pdf_token: str) -> Optional[Dict[str, Any]]:
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
        return dict(row)
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

def registrar_acceso_pdf(aceptacion_id: int, timestamp_utc: str):
    """Registra un acceso exitoso al PDF."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE aceptaciones 
            SET pdf_last_access_at = ?, 
            pdf_access_count = COALESCE(pdf_access_count, 0) + 1 
            WHERE id = ?
            """,
            (timestamp_utc, aceptacion_id)
        )
        conn.commit()
    finally:
        conn.close()
