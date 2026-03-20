"""
Autenticación HTTP Basic para operadores de evento.

Los operadores son usuarios con acceso restringido: solo pueden acceder
al monitor y CSV de los eventos que tengan asignados.

Passwords se almacenan como PBKDF2-HMAC-SHA256 con salt individual.
Formato en DB: pbkdf2$<iterations>$<hex_salt>$<hex_hash>
"""

import hashlib
import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security_op = HTTPBasic(auto_error=True)

_ITERATIONS = 260_000


def hash_password(plain: str) -> str:
    """Genera hash PBKDF2 para almacenar en DB."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """Verifica contraseña contra hash almacenado."""
    try:
        _, iterations, salt_hex, hash_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, int(iterations))
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


def _get_operador(username: str) -> dict | None:
    """Busca el operador en la base de datos."""
    from app.db.database import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, password_hash, evento_ids, activo FROM operadores WHERE username = %s",
            (username,)
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_current_operator(credentials: HTTPBasicCredentials = Depends(security_op)) -> dict:
    """
    Autentica al operador y retorna su registro completo.
    El router luego verifica que el evento_id esté en su lista.
    """
    operador = _get_operador(credentials.username)

    ok = (
        operador is not None
        and operador.get("activo") == 1
        and verify_password(credentials.password, operador["password_hash"])
    )

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return operador


def check_evento_access(operador: dict, evento_id: int) -> None:
    """Lanza 403 si el operador no tiene acceso al evento."""
    ids_str = operador.get("evento_ids") or ""
    ids_permitidos = [int(x.strip()) for x in ids_str.split(",") if x.strip().isdigit()]
    if evento_id not in ids_permitidos:
        raise HTTPException(status_code=403, detail="No tiene acceso a este evento")
