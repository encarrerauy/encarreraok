"""
Configuración centralizada del entorno Jinja2 para EncarreraOK.
Importar templates_env desde aquí en routers y en main.py.
"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader


def fecha_ddmmaaaa(value: str) -> str:
    try:
        y, m, d = value.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return value


templates_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=True
)
templates_env.filters["fecha_ddmmaaaa"] = fecha_ddmmaaaa
