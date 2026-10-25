"""Envío de libros por correo a la dirección @kindle.com (Send to Kindle)."""

from __future__ import annotations

import mimetypes
import re
import smtplib
import ssl
import unicodedata
from email.message import EmailMessage
from pathlib import Path

from .config import Settings

# Límite de Amazon para documentos por correo; Gmail permite 25 MB.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SendError(Exception):
    pass


def ascii_filename(title: str, authors: str, ext: str) -> str:
    """Amazon rechaza a veces adjuntos con tildes o símbolos en el nombre."""
    base = f"{title} - {authors}" if authors else title
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = re.sub(r"[^\w\s.,()'-]", "", base)
    base = re.sub(r"\s+", " ", base).strip(" .-")[:120] or "libro"
    return f"{base}.{ext}"


def _smtp(settings: Settings) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, context=context, timeout=60
        )
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
        if settings.smtp_security == "starttls":
            server.starttls(context=context)
    if settings.smtp_user:
        server.login(settings.smtp_user, settings.smtp_password)
    return server


def send_email(
    settings: Settings,
    to: str,
    subject: str,
    body: str,
    attachment: Path | None = None,
    filename: str | None = None,
) -> None:
    if not settings.smtp_configured:
        raise SendError("El correo no está configurado (SMTP_HOST / SMTP_FROM en .env)")
    if attachment is not None and attachment.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise SendError("El fichero supera los 25 MB que admite el correo")
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment is not None:
        name = filename or attachment.name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".epub"):
            mime = "application/epub+zip"
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(
            attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=name
        )
    try:
        with _smtp(settings) as server:
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise SendError(f"Error enviando el correo: {exc}") from exc
