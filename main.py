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

import notify
import scraper
import update_sheets

load_dotenv()


def log(msg: str) -> None:
    print(f"[main] {msg}", flush=True)


def run() -> dict:
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        raise RuntimeError("Falta SPREADSHEET_ID en .env / variables de entorno.")

    log("1/2 — Descargando tickets desde BA Colaborativa…")
    export_path: Path = scraper.download_tickets()
    log(f"Export: {export_path}")

    log("2/2 — Mergeando contra Google Sheets…")
    stats = update_sheets.update_sheets(export_path, spreadsheet_id)
    log(f"✓ {stats['added']} tickets nuevos agregados (export: {stats['export_total']}).")
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
    )
