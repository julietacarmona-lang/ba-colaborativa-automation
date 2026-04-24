"""Notificación por mail cuando el scraper falla.

Soporta dos backends, elegidos automáticamente según qué variables de entorno
estén definidas:

  - SMTP (Gmail / cualquier proveedor): requiere SMTP_HOST, SMTP_PORT,
    SMTP_USER, SMTP_PASSWORD. Para Gmail / Google Workspace hay que generar
    una "app password" (requiere 2FA activado en la cuenta).

  - Resend (https://resend.com): más simple, solo requiere RESEND_API_KEY y
    una dirección verificada en FROM_EMAIL. Free tier 3k mails/mes.

Si no hay ningún backend configurado, imprime el error por stdout. Es a
propósito: así en local no se rompe si todavía no configuraste mail, pero en
GitHub Actions vas a definir las variables y el mail va a salir.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import traceback
from email.message import EmailMessage
from typing import Optional

import urllib.request
import json

ALERT_TO = os.environ.get("ALERT_TO_EMAIL", "julieta.carmona@educabot.com")


def send_failure_alert(subject: str, body: str, exc: Optional[BaseException] = None) -> None:
    """Mandá una alerta con `subject` y `body`. Si `exc` está, appendea el traceback."""
    full_body = body
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        full_body = f"{body}\n\n--- Traceback ---\n{tb}"

    # Backend 1: Resend (si hay API key)
    if os.environ.get("RESEND_API_KEY"):
        try:
            _send_via_resend(subject, full_body)
            print(f"[notify] alerta enviada por Resend a {ALERT_TO}")
            return
        except Exception as e:
            print(f"[notify] Resend falló: {e}. Probando SMTP…")

    # Backend 2: SMTP (Gmail / otro)
    if os.environ.get("SMTP_HOST"):
        try:
            _send_via_smtp(subject, full_body)
            print(f"[notify] alerta enviada por SMTP a {ALERT_TO}")
            return
        except Exception as e:
            print(f"[notify] SMTP falló: {e}")

    # Fallback: stdout
    print("=" * 60)
    print(f"[notify] NO hay backend de mail configurado. Alerta:")
    print(f"  To: {ALERT_TO}")
    print(f"  Subject: {subject}")
    print(f"  Body:\n{full_body}")
    print("=" * 60)


def _send_via_resend(subject: str, body: str) -> None:
    api_key = os.environ["RESEND_API_KEY"]
    from_email = os.environ.get("FROM_EMAIL", "onboarding@resend.dev")
    payload = json.dumps({
        "from": from_email,
        "to": [ALERT_TO],
        "subject": subject,
        "text": body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Resend respondió {resp.status}: {resp.read()!r}")


def _send_via_smtp(subject: str, body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    from_email = os.environ.get("FROM_EMAIL", user)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ALERT_TO
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)
