"""Lee el export descargado (xlsx o csv) y agrega los tickets nuevos al
Google Sheets tab 'Tickets - General', deduplicando por columna 'Número'.

Las columnas del Sheets son 115 en total:
  - Col 0: categoría (derivada del último segmento de 'Prestación').
  - Col 1: 'Número' — clave única para deduplicar.
  - Cols 2..N: vienen mapeadas 1:1 desde el export.
  - Algunas columnas del Sheets (Respuesta AI, Respuesta de Producto, etc.)
    no existen en el export y se dejan en blanco para los tickets nuevos.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Nombre del tab destino. Overridable con SHEET_TAB en .env — útil para
# probar sobre un tab de prueba antes de tocar "Tickets - General".
SHEET_TAB = os.environ.get("SHEET_TAB", "Tickets - General")
NUMBER_COL = "Número"


def log(msg: str) -> None:
    print(f"[sheets] {msg}", flush=True)


def update_sheets(export_path: Path, spreadsheet_id: str) -> dict:
    """Carga el export, compara contra el Sheets y appendea nuevos.
    Devuelve un dict con stats: {'export_total', 'existing', 'added'}."""
    log(f"Leyendo export: {export_path}")
    df_export = _read_export(export_path)
    log(f"  {len(df_export)} filas en el export.")

    if len(df_export) == 0:
        raise RuntimeError(
            "El export tiene 0 tickets. Puede indicar: plataforma inestable, "
            "filtro que no devolvió resultados, o archivo descargado corrupto. "
            "Revisá el archivo en ./downloads/ y el filtro aplicado en BA Colaborativa."
        )

    if NUMBER_COL not in df_export.columns:
        raise RuntimeError(
            f"El export no tiene la columna '{NUMBER_COL}'. "
            f"Columnas disponibles: {list(df_export.columns)[:10]}…"
        )

    creds = _load_credentials()
    gc = gspread.authorize(creds)
    log(f"Abriendo spreadsheet {spreadsheet_id[:12]}…")
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(SHEET_TAB)

    headers = ws.row_values(1)
    log(f"  Headers del Sheets: {len(headers)} columnas.")

    if NUMBER_COL not in headers:
        raise RuntimeError(
            f"El tab '{SHEET_TAB}' no tiene columna '{NUMBER_COL}' en fila 1."
        )

    numero_col_idx = headers.index(NUMBER_COL) + 1  # gspread usa 1-indexed
    existing_numbers = set(
        v for v in ws.col_values(numero_col_idx)[1:] if v
    )
    log(f"  {len(existing_numbers)} tickets ya cargados.")

    # Normalizamos tipos a str para comparar de forma robusta.
    df_export[NUMBER_COL] = df_export[NUMBER_COL].astype(str).str.strip()
    new_df = df_export[~df_export[NUMBER_COL].isin(existing_numbers)]
    log(f"  {len(new_df)} tickets nuevos para agregar.")

    stats = {
        "export_total": len(df_export),
        "existing": len(existing_numbers),
        "added": len(new_df),
    }
    if len(new_df) == 0:
        return stats

    rows_to_append = [
        _build_row(row, headers) for _, row in new_df.iterrows()
    ]

    # Calculamos la primera fila libre DESPUÉS de la última fila con dato en
    # la columna 'Número' (en lugar de usar append_rows, que usa el final del
    # worksheet — eso deja gaps si hay filas vacías reservadas).
    numero_values = ws.col_values(numero_col_idx)
    last_data_row = 0
    for i, v in enumerate(numero_values, start=1):
        if v and v.strip():
            last_data_row = i
    next_row = last_data_row + 1
    end_row = next_row + len(rows_to_append) - 1
    last_col_letter = gspread.utils.rowcol_to_a1(1, len(headers)).rstrip("1")
    range_str = f"A{next_row}:{last_col_letter}{end_row}"

    # El grid del worksheet tiene un row_count fijo. Si vamos a escribir más
    # allá del límite, gspread devuelve 400 "exceeds grid limits". Extender
    # con un buffer extra para no extender en cada corrida.
    if end_row > ws.row_count:
        needed = end_row - ws.row_count + 100
        log(f"Extendiendo grid: +{needed} filas (actual={ws.row_count}).")
        ws.add_rows(needed)

    log(f"Insertando {len(rows_to_append)} filas en {range_str}…")
    # Usamos argumentos nombrados (no posicionales) — gspread cambió el orden
    # entre versiones y los nombrados son robustos a esos cambios.
    ws.update(
        range_name=range_str,
        values=rows_to_append,
        value_input_option="USER_ENTERED",
    )
    log("✓ Listo.")
    return stats


def _read_export(path: Path) -> pd.DataFrame:
    """Lee el export. Si es xlsx multi-tab, concatena los tabs que tengan
    la columna 'Número'."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str).fillna("")
    if suffix in (".xlsx", ".xls"):
        all_sheets = pd.read_excel(path, sheet_name=None, dtype=str)
        frames = [
            df.fillna("") for df in all_sheets.values() if NUMBER_COL in df.columns
        ]
        if not frames:
            raise RuntimeError(
                f"Ningún tab del xlsx tiene la columna '{NUMBER_COL}'. "
                f"Tabs: {list(all_sheets.keys())}"
            )
        return pd.concat(frames, ignore_index=True)
    raise RuntimeError(f"Formato no soportado: {suffix}")


CATEGORIA_HEADER_RE = re.compile(r"^\s*categor[ií]a\s*$", re.I)


def _build_row(export_row: pd.Series, sheets_headers: list[str]) -> list:
    """Arma una fila con exactamente len(sheets_headers) celdas, en el orden
    del Sheets. Para cada columna del header:
      - Si el header coincide con una columna del export, usa ese valor.
      - Si el header se llama 'Categoría' (con o sin tilde), deriva del último
        segmento de 'Prestación'.
      - Si no, deja vacío. IMPORTANTE: no sobrescribe columnas que el Sheets
        usa para sus propios cálculos (fórmulas, conteos, etc.).
    """
    row: list = []
    for col in sheets_headers:
        if col in export_row.index:
            row.append(str(export_row[col]))
        elif CATEGORIA_HEADER_RE.match(col) and "Prestación" in export_row.index:
            row.append(_derive_categoria(export_row["Prestación"]))
        else:
            row.append("")
    return row


def _derive_categoria(prestacion: str) -> str:
    """Devuelve el último segmento de 'Prestación', split por '/' o '|'."""
    if not prestacion:
        return ""
    parts = re.split(r"\s*[/|>]\s*", prestacion.strip())
    return parts[-1] if parts else ""


def _load_credentials() -> Credentials:
    """Carga las credenciales del service account.
    Prioridad:
      1. GOOGLE_CREDENTIALS_JSON — JSON completo en variable de entorno (CI).
      2. GOOGLE_CREDENTIALS_FILE — path a archivo JSON (local).
    """
    raw_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw_json:
        # Soporta tanto JSON directo como base64-encoded (evita corrupción en shell)
        if not raw_json.startswith("{"):
            import base64
            raw_json = base64.b64decode(raw_json).decode("utf-8")
        info = json.loads(raw_json)
        return Credentials.from_service_account_info(info, scopes=SCOPES)

    file_path = os.environ.get("GOOGLE_CREDENTIALS_FILE", "./credentials.json")
    if not Path(file_path).exists():
        raise RuntimeError(
            f"No encontré credenciales de Google. Seteá GOOGLE_CREDENTIALS_JSON "
            f"o poné el archivo en {file_path}."
        )
    return Credentials.from_service_account_file(file_path, scopes=SCOPES)


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    if len(sys.argv) < 2:
        print("Uso: python update_sheets.py <path_al_export>")
        sys.exit(1)

    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        print("ERROR: falta SPREADSHEET_ID en .env")
        sys.exit(1)

    stats = update_sheets(Path(sys.argv[1]), spreadsheet_id)
    print(f"OK — {stats['added']} tickets agregados (export: {stats['export_total']}, existentes: {stats['existing']}).")
