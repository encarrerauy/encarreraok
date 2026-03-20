"""
Middleware de autenticación HTTP Basic para el panel de administración.
"""

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings

security = HTTPBasic()


def get_current_username(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Verifica credenciales para acceso admin."""
    # Comparación segura para evitar timing attacks
    is_correct_username = secrets.compare_digest(
        credentials.username.encode("utf8"),
        settings.admin_user.encode("utf8"),
    )
    is_correct_password = secrets.compare_digest(
        credentials.password.encode("utf8"),
        settings.admin_password.encode("utf8"),
    )

    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
