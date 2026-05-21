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
# Webhook para mensajes de éxito (canal compartido del equipo).
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
# Webhook para mensajes de error (idealmente un canal privado solo del owner).
# Si está vacío, los errores NO se mandan a Slack (solo quedan en logs/mail).
SLACK_WEBHOOK_URL_ERROR = os.environ.get("SLACK_WEBHOOK_URL_ERROR", "").strip()
# Link al Sheets — se incluye al final de cada mensaje de Slack.
SHEET_LINK = os.environ.get("SHEET_LINK", "").strip()


def _with_sheet_link(text: str) -> str:
    return f"{text}\n<{SHEET_LINK}|Abrir Sheets>" if SHEET_LINK else text


BOT_NAME = "BA Colaborativa Bot"
SUCCESS_EMOJI = ":tada:"  # 🎉
ERROR_EMOJI = ":rotating_light:"  # 🚨


def _send_slack_to(
    webhook_url: str, text: str, label: str, username: str, icon_emoji: str
) -> None:
    """Manda un mensaje a un webhook de Slack puntual con nombre e ícono
    customizados. Respeta SKIP_SLACK=1."""
    if not webhook_url or os.environ.get("SKIP_SLACK", "").strip() in ("1", "true", "yes"):
        return
    try:
        payload = json.dumps({
            "text": text,
            "username": username,
            "icon_emoji": icon_emoji,
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                print(f"[notify] Slack {label} respondió {resp.status}: {resp.read()!r}")
            else:
                print(f"[notify] Slack {label} OK")
    except Exception as e:
        print(f"[notify] Slack {label} falló: {e}")


def send_slack(text: str) -> None:
    """Mensaje al canal del equipo (éxitos). Bot 'BA Colaborativa Bot' con bandeja."""
    _send_slack_to(
        SLACK_WEBHOOK_URL, text,
        label="success",
        username=BOT_NAME,
        icon_emoji=SUCCESS_EMOJI,
    )


def send_slack_error(text: str) -> None:
    """Mensaje al canal privado del owner (errores). Mismo bot, ícono de alerta.
    Si no hay webhook de error configurado, no se manda nada."""
    _send_slack_to(
        SLACK_WEBHOOK_URL_ERROR, text,
        label="error",
        username=BOT_NAME,
        icon_emoji=ERROR_EMOJI,
    )


def send_success_message(added: int, total_in_export: int) -> None:
    """Notifica a Slack que el pipeline terminó OK.

    Override del SKIP_SLACK: si hay tickets nuevos (added > 0), FORZAMOS el
    envío a Slack aunque sea un cron keep-alive 'silencioso'. La intención de
    SKIP_SLACK en los keep-alives es evitar spam de '0 tickets nuevos' x4 por
    día — pero cuando hay novedades reales, el equipo tiene que enterarse
    siempre, no importa el horario."""
    if added == 0:
        # Sin novedad → respeto el SKIP_SLACK que se haya configurado.
        text = f"✅ BA Colaborativa: pipeline OK — *0 tickets nuevos*. Hay {total_in_export} abiertos actualmente."
        send_slack(_with_sheet_link(text))
        return

    # Hay tickets nuevos → forzamos el envío ignorando SKIP_SLACK.
    text = f"✅ BA Colaborativa: pipeline OK — *{added} tickets nuevos agregados* al Sheets. Hay {total_in_export} abiertos actualmente."
    _force_send_slack(_with_sheet_link(text))


def _force_send_slack(text: str) -> None:
    """Manda a Slack ignorando SKIP_SLACK. Usado cuando hay novedades reales
    que el equipo tiene que ver (tickets nuevos), aunque el cron sea uno de
    los keep-alives 'silenciosos'."""
    if not SLACK_WEBHOOK_URL:
        return
    try:
        payload = json.dumps({
            "text": text,
            "username": BOT_NAME,
            "icon_emoji": SUCCESS_EMOJI,
        }).encode("utf-8")
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status >= 300:
                print(f"[notify] Slack success (forced) respondió {resp.status}")
            else:
                print(f"[notify] Slack success (forced) OK — había tickets nuevos")
    except Exception as e:
        print(f"[notify] Slack success (forced) falló: {e}")


REFRESH_HINT_KEYWORDS = (
    "captcha",
    "form de login",
    "se acabó el tiempo",
    "auth/logout",
    "401",
    "contactos",
)


def _is_session_expired(exc: Optional[BaseException], body: str) -> bool:
    """Heurística para distinguir 'sesión expirada' de 'otro error'.
    Si lo es, el mensaje de Slack incluye el comando exacto para refrescar."""
    text = (str(exc or "") + " " + body).lower()
    return any(k in text for k in REFRESH_HINT_KEYWORDS)


def send_failure_alert(subject: str, body: str, exc: Optional[BaseException] = None) -> None:
    """Mandá una alerta con `subject` y `body`. Si `exc` está, appendea el traceback."""
    full_body = body
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        full_body = f"{body}\n\n--- Traceback ---\n{tb}"

    # Notificar a Slack — al webhook de errores (canal privado del owner),
    # NO al webhook general del equipo. Si no hay webhook de error configurado,
    # el mensaje no se manda a ningún lado (queda en logs de GitHub Actions).
    short_err = str(exc)[:200] if exc else "ver mail/logs"
    if _is_session_expired(exc, body):
        msg = (
            "⚠️ *BA Colaborativa: sesión expirada* — el SPA pidió relogin "
            "y Keycloak mostró captcha. Hay que refrescar las cookies.\n\n"
            "*Para arreglarlo* (3 min): abrí terminal en la carpeta del proyecto y corré:\n"
            "```./scripts/refresh-session.sh```"
        )
    else:
        msg = f"❌ BA Colaborativa: *{subject}*\n```{short_err}```"
    send_slack_error(_with_sheet_link(msg))

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
