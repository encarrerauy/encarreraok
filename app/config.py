"""
Configuración centralizada de EncarreraOK.

Variables OBLIGATORIAS en producción:
  - ADMIN_PASSWORD  (sin valor por defecto; lanza ValueError si está vacía)

Variables opcionales (con valores por defecto para desarrollo):
  - ADMIN_USER              (default: "admin")
  - ENCARRERAOK_DB_PATH     (default: "/var/lib/encarreraok/encarreraok.sqlite3")
  - ENCARRERAOK_LEGAL_DIR   (default: "legal")
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Credenciales de administración
    # ADMIN_PASSWORD es OBLIGATORIA en producción (sin default)
    admin_user: str = os.environ.get("ADMIN_USER", "admin")
    admin_password: str = os.environ.get("ADMIN_PASSWORD", "")

    # Base de datos SQLite
    db_path: str = os.environ.get(
        "ENCARRERAOK_DB_PATH",
        "/var/lib/encarreraok/encarreraok.sqlite3",
    )

    # Directorio de archivos legales (textos de deslinde)
    legal_dir: str = os.environ.get("ENCARRERAOK_LEGAL_DIR", "legal")

    def __post_init__(self) -> None:
        if not self.admin_password:
            raise ValueError(
                "La variable de entorno ADMIN_PASSWORD es obligatoria y no puede estar vacía. "
                "Configúrala antes de iniciar la aplicación."
            )


# Instancia singleton – importar con: from app.config import settings
settings = Settings()
