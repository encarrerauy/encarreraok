"""
Envío de emails transaccionales vía Mailgun.
Si las variables de entorno no están configuradas, las funciones fallan silenciosamente.
"""

import logging
import urllib.request
import urllib.parse
import base64
from app.config import settings

logger = logging.getLogger("encarreraok")


def _mailgun_configured() -> bool:
    return bool(settings.mailgun_api_key and settings.mailgun_domain and settings.mailgun_from)


def _mailgun_url() -> str:
    base = "https://api.eu.mailgun.net" if settings.mailgun_region == "eu" else "https://api.mailgun.net"
    return f"{base}/v3/{settings.mailgun_domain}/messages"


def _send(to: str, subject: str, html: str, text: str) -> bool:
    """Envía un email vía Mailgun HTTP API. Retorna True si tuvo éxito."""
    if not _mailgun_configured():
        logger.warning("Mailgun no configurado — email no enviado.")
        return False

    data = urllib.parse.urlencode({
        "from": settings.mailgun_from,
        "to": to,
        "subject": subject,
        "html": html,
        "text": text,
    }).encode("utf-8")

    credentials = base64.b64encode(f"api:{settings.mailgun_api_key}".encode()).decode()
    req = urllib.request.Request(
        _mailgun_url(),
        data=data,
        headers={"Authorization": f"Basic {credentials}"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            if status == 200:
                logger.info(f"Email enviado a {to}: {subject}")
                return True
            else:
                logger.warning(f"Mailgun respondió {status} para {to}")
                return False
    except Exception as e:
        logger.error(f"Error enviando email a {to}: {e}")
        return False


def send_rechazo_email(
    email: str,
    nombre: str,
    evento_nombre: str,
    motivo: str,
    revisado_por: str,
    recarga_token: str = None,
) -> bool:
    """Notifica al participante que su deslinde fue rechazado."""
    if not email:
        return False

    subject = f"Tu deslinde en {evento_nombre} fue rechazado"

    recarga_link = ""
    recarga_btn = ""
    recarga_text = ""
    if recarga_token:
        url = f"{settings.app_base_url}/recarga/{recarga_token}"
        recarga_btn = f"""
        <div style="text-align: center; margin: 28px 0;">
            <a href="{url}"
               style="background: #0d6efd; color: white; text-decoration: none;
                      padding: 14px 28px; border-radius: 6px; font-size: 1rem;
                      font-weight: 600; display: inline-block;">
                📎 Corregir y reenviar documentos
            </a>
            <p style="font-size: 0.8rem; color: #888; margin-top: 10px;">
                Este link es válido por 72 horas y de uso único.
            </p>
        </div>
        """
        recarga_text = f"\nPodés corregir y reenviar tus documentos en:\n{url}\n(válido 72 horas)\n"

    html = f"""
    <div style="font-family: system-ui, Arial, sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #1a1a1a;">
        <div style="background: #f8d7da; border-left: 4px solid #dc3545; padding: 16px; border-radius: 4px; margin-bottom: 24px;">
            <strong style="color: #842029;">❌ Deslinde rechazado</strong>
        </div>

        <p>Hola <strong>{nombre}</strong>,</p>

        <p>Tu deslinde registrado para el evento <strong>{evento_nombre}</strong>
        fue <strong>rechazado</strong> por el equipo organizador.</p>

        <div style="background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 6px; padding: 16px; margin: 20px 0;">
            <strong>Motivo del rechazo:</strong><br>
            <span style="color: #495057;">{motivo}</span>
        </div>

        {recarga_btn}

        <hr style="border: none; border-top: 1px solid #dee2e6; margin: 24px 0;">
        <p style="font-size: 0.85rem; color: #888;">
            Este mensaje fue generado automáticamente por EncarreraOK.<br>
            Revisado por: {revisado_por}
        </p>
    </div>
    """

    text = (
        f"Hola {nombre},\n\n"
        f"Tu deslinde para el evento '{evento_nombre}' fue RECHAZADO.\n\n"
        f"Motivo: {motivo}\n"
        f"{recarga_text}\n"
        f"Revisado por: {revisado_por}\n"
        f"— EncarreraOK"
    )

    return _send(email, subject, html, text)
