"""Orquestador: corre el scraper y después appendea al Google Sheets.

Uso:
    python main.py

Variables requeridas (en .env o variables de entorno):
    BA_USER, BA_PASSWORD   — credenciales de BA Colaborativa
    SPREADSHEET_ID         — id del Google Sheets destino
    GOOGLE_CREDENTIALS_JSON o GOOGLE_CREDENTIALS_FILE — credenciales de service account
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # cargar .env ANTES de importar módulos que leen env vars al inicio

import notify
import scraper
import update_sheets
from scraper import CaptchaRejectedError, PlatformDownError


def log(msg: str) -> None:
    print(f"[main] {msg}", flush=True)


def run() -> dict:
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        raise RuntimeError("Falta SPREADSHEET_ID en .env / variables de entorno.")

    log("1/3 — Descargando tickets desde BA Colaborativa…")
    export_path: Path = scraper.download_tickets()
    log(f"Export: {export_path}")

    log("2/3 — Mergeando contra Google Sheets…")
    stats = update_sheets.update_sheets(export_path, spreadsheet_id)
    log(f"✓ {stats['added']} tickets nuevos agregados (export: {stats['export_total']}).")

    # 3. Adjuntos: opt-in via PROCESAR_ADJUNTOS=1 hasta que esté testeado en CI.
    # Reusa la sesión del scraper en el Chrome dedicado (BROWSER_MODE=cdp). Si
    # falla, no rompe el pipeline — solo logguea.
    if os.environ.get("PROCESAR_ADJUNTOS", "").strip() in ("1", "true", "yes"):
        log("3/3 — Procesando URLs de adjuntos…")
        try:
            import update_adjuntos
            adj_stats = update_adjuntos.process_adjuntos(export_path, spreadsheet_id)
            log(f"✓ adjuntos: {adj_stats}")
            stats["adjuntos"] = adj_stats
        except Exception as e:
            log(f"⚠️  adjuntos falló (no rompe el pipeline): {e!r}")
            stats["adjuntos_error"] = repr(e)
    else:
        log("3/3 — Adjuntos: skip (poner PROCESAR_ADJUNTOS=1 para activar).")

    return stats


if __name__ == "__main__":
    try:
        stats = run()
    except PlatformDownError:
        # Alerta ya enviada desde scraper.py — no duplicar.
        log("ERROR: BA Colaborativa está caída. Próximo cron reintentará.")
        sys.exit(1)
    except CaptchaRejectedError as e:
        log(f"ERROR: {e}")
        notify.send_failure_alert(
            subject="[BA Colaborativa] Captcha no resolvible — probable sesión vencida",
            body=(
                "El solver de captcha no pudo pasar el score de Keycloak.\n\n"
                "Causa probable: las cookies de sesión expiraron y Keycloak "
                "exige CAPTCHA al detectar un login 'frío' desde una IP de CI.\n\n"
                "Para arreglarlo: corré ./scripts/refresh-session.sh desde tu compu "
                "(genera cookies frescas y las sube como secret).\n\n"
                f"Detalle técnico: {e!r}"
            ),
            exc=e,
        )
        sys.exit(1)
    except Exception as e:
        log(f"ERROR: {e}")
        notify.send_failure_alert(
            subject="[BA Colaborativa] Scraper falló después de reintentos",
            body=f"El scraper no pudo descargar los tickets.\n\nÚltimo error: {e!r}",
            exc=e,
        )
        sys.exit(1)
    log(f"Pipeline OK. Total agregados: {stats['added']}")
    notify.send_success_message(
        added=stats["added"],
        total_in_export=stats["export_total"],
        adjuntos_added=stats.get("adjuntos", {}).get("agregados", 0),
    )
