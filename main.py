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
    except Exception as e:
        log(f"ERROR: {e}")
        notify.send_failure_alert(
            subject="[BA Colaborativa] Pipeline falló",
            body=f"El orquestador falló con: {e!r}",
            exc=e,
        )
        sys.exit(1)
    log(f"Pipeline OK. Total agregados: {stats['added']}")
    notify.send_success_message(
        added=stats["added"],
        total_in_export=stats["export_total"],
        adjuntos_added=stats.get("adjuntos", {}).get("agregados", 0),
    )
