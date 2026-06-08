"""Procesa la columna 'Adjuntos' del Sheets: para tickets que tengan adjuntos
en el CSV recién bajado, no estén marcados como 'Respuesta cerrada producto = Sí',
y aún no tengan URLs en 'Adjuntos', extrae las URLs del detalle y las escribe.

Se ejecuta DESPUÉS del scraper/update_sheets en el mismo run del cron, reusando
la sesión que el scraper dejó activa en el Chrome dedicado (BROWSER_MODE=cdp).

API: `process_adjuntos(export_path, spreadsheet_id) -> dict` (stats).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import List

import gspread
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from update_sheets import _load_credentials, SHEET_TAB

CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
BACKOFFICE_URL = "https://bacolaborativa-backoffice.buenosaires.gob.ar"
MAX_TICKETS_PER_RUN = int(os.environ.get("ADJUNTOS_MAX_TICKETS", "20"))


def log(msg: str) -> None:
    print(f"[adjuntos] {msg}", flush=True)


def _extract_urls_from_detail(page) -> List[str]:
    """En la pestaña del detalle, clickea cada adjunto y captura las URLs únicas
    de los popups que se abren. Espera a que el accordion 'Archivos del contacto'
    esté visible.
    """
    accordion = page.locator("app-panel-desplegable").filter(
        has=page.get_by_role("button", name=re.compile(r"archivos del contacto", re.I))
    )
    try:
        accordion.first.wait_for(state="attached", timeout=10000)
    except PlaywrightTimeoutError:
        log("  (no apareció la sección 'Archivos del contacto')")
        return []

    items = accordion.locator(".image-wrapper").all()
    urls: List[str] = []
    for item in items:
        try:
            with page.expect_popup(timeout=8000) as popup_info:
                item.click()
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=10000)
            urls.append(popup.url)
            try:
                popup.close()
            except Exception:
                pass
        except Exception as e:
            log(f"  ⚠️  un adjunto falló: {e!r}")

    # Dedup conservando orden — el DOM puede tener wrapper/container apuntando al mismo archivo
    seen = set()
    unique: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def _search_by_numero(page, numero: str) -> None:
    """En la bandeja: limpia criterios, agrega 'Número = {numero}', click Buscar.
    Después de esto, la grilla queda con (idealmente) solo esa fila."""
    # Asegurar panel de criterios expandido
    buscar = page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first
    if not buscar.is_visible(timeout=1500):
        try:
            page.get_by_text(re.compile(r"Criterios de b[uú]squeda", re.I)).first.click(force=True)
            time.sleep(1)
        except Exception:
            pass

    # Click en 'Limpiar' para resetear criterios previos
    try:
        page.get_by_role("button", name=re.compile(r"^\s*limpiar\s*$", re.I)).first.click(force=True)
        time.sleep(1)
    except Exception:
        pass

    # La grilla tiene una fila default vacía con 3 ng-select (campo/operador/valor).
    rows = page.locator("tr").filter(has=page.locator("ng-select")).all()
    rows = [r for r in rows if r.locator("ng-select").count() >= 2]
    if not rows:
        raise RuntimeError("no detecté filas de criterios")

    # Configurar la primera fila con Número = numero
    row1_sels = rows[0].locator("ng-select").all()
    # 1) campo
    row1_sels[0].click()
    page.wait_for_timeout(400)
    try:
        active = row1_sels[0].locator(".ng-input input").first
        active.fill("")
        active.type("Número", delay=20)
    except Exception:
        pass
    page.wait_for_timeout(600)
    page.locator(".ng-option").filter(has_text=re.compile(r"^\s*N[uú]mero\s*$", re.I)).first.click()
    page.wait_for_timeout(700)

    # 2) valor — re-leer (puede tener ahora 2 o 3 ng-select)
    row1_sels = rows[0].locator("ng-select").all()
    # El último ng-select es el del valor; debería ser un input editable
    val_sel = row1_sels[-1]
    val_sel.click()
    page.wait_for_timeout(300)
    try:
        # Algunos campos numéricos son inputs simples, no ng-select; probemos ambos
        active = val_sel.locator(".ng-input input").first
        active.fill("")
        active.type(numero, delay=20)
    except Exception:
        # Fallback: input genérico dentro de la fila
        try:
            inp = rows[0].locator("input[type='text'], input:not([type])").last
            inp.fill(numero)
        except Exception:
            pass
    page.wait_for_timeout(500)

    # Buscar
    buscar = page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first
    buscar.wait_for(timeout=8000)
    buscar.click(force=True)
    # Esperar resultados
    time.sleep(4)


def _process_one_ticket(page, numero: str) -> List[str]:
    """Filtra la bandeja por `numero`, abre el detalle del único resultado,
    extrae URLs, vuelve a la bandeja."""
    _search_by_numero(page, numero)

    # Verificar que aparece la celda con el número (la única fila)
    cell = page.locator("datatable-body-cell").filter(has_text=numero).first
    cell.wait_for(state="visible", timeout=10000)
    cell.click()
    # Click en 'Ver detalles' → navega al detalle
    page.get_by_role("link", name=re.compile(r"^\s*ver detalles\s*$", re.I)).first.click(force=True)
    page.wait_for_url(re.compile(r"/contacto/consulta/"), timeout=15000)
    time.sleep(4)

    urls = _extract_urls_from_detail(page)

    # Volver a la bandeja
    try:
        page.go_back(wait_until="domcontentloaded", timeout=15000)
    except Exception:
        page.goto(f"{BACKOFFICE_URL}/contacto/bandeja")
    time.sleep(3)
    return urls


def process_adjuntos(export_path: Path, spreadsheet_id: str) -> dict:
    """Cruza CSV recién bajado con Sheets, identifica candidatos (tienen adjunto
    en el CSV, no están cerrados, no tienen URL aún), y los procesa.

    Devuelve {procesados, agregados, errores, candidatos_totales}.
    """
    # 1. Leer CSV: tickets con adjuntos
    try:
        df = pd.read_csv(export_path, sep=",", encoding="utf-8", dtype=str, on_bad_lines="skip")
    except Exception as e:
        log(f"⚠️  no pude leer CSV: {e}")
        return {"error": f"CSV: {e}"}
    if "Contiene archivos adjuntos" not in df.columns or "Número" not in df.columns:
        log("⚠️  CSV no tiene las columnas esperadas")
        return {"error": "CSV columnas faltantes"}
    csv_con_adjunto = set(
        df[df["Contiene archivos adjuntos"].astype(str).str.strip().str.lower().isin(["si", "sí"])]["Número"]
        .astype(str).str.strip().tolist()
    )
    log(f"CSV: {len(csv_con_adjunto)} tickets con adjuntos")

    # 2. Leer Sheets, identificar candidatos
    creds = _load_credentials()
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(SHEET_TAB)
    all_rows = ws.get_all_values()
    if not all_rows:
        log("Sheets vacío.")
        return {"procesados": 0, "agregados": 0, "errores": 0}
    headers = all_rows[0]

    try:
        col_numero = headers.index("Número")
    except ValueError:
        log("⚠️  Falta columna 'Número' en el Sheets.")
        return {"error": "missing Número"}
    try:
        col_resp_cerrada = headers.index("Respuesta cerrada producto")
    except ValueError:
        log("⚠️  Falta columna 'Respuesta cerrada producto' en el Sheets.")
        return {"error": "missing Respuesta cerrada producto"}
    try:
        col_adjuntos = headers.index("Adjuntos")
    except ValueError:
        log("⚠️  Falta columna 'Adjuntos' en el Sheets.")
        return {"error": "missing Adjuntos"}

    candidates = []  # (sheet_row_1indexed, numero)
    for i, row in enumerate(all_rows[1:], start=2):
        # row[i] puede no existir si la fila es corta — defensivo
        def cell(idx):
            return row[idx].strip() if idx < len(row) else ""
        numero = cell(col_numero)
        resp = cell(col_resp_cerrada).lower()
        adj = cell(col_adjuntos)
        if not numero:
            continue
        if numero not in csv_con_adjunto:
            continue
        if resp in ("sí", "si"):
            continue
        if adj:  # ya tiene URL
            continue
        candidates.append((i, numero))

    total = len(candidates)
    log(f"Candidatos: {total}. Proceso hasta {MAX_TICKETS_PER_RUN} en esta corrida.")
    candidates = candidates[:MAX_TICKETS_PER_RUN]
    if not candidates:
        return {"procesados": 0, "agregados": 0, "errores": 0, "candidatos_totales": total}

    # 3. Conectar al browser CDP — el scraper ya dejó la sesión arriba
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            log(f"⚠️  no pude conectar a {CDP_URL}: {e}")
            return {"error": f"CDP: {e}", "candidatos_totales": total}

        if not browser.contexts:
            log("⚠️  el browser no tiene contextos.")
            return {"error": "no contexts", "candidatos_totales": total}
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Asegurar bandeja
        if "/contacto/bandeja" not in page.url:
            page.goto(f"{BACKOFFICE_URL}/contacto/bandeja")
            time.sleep(5)
        # Si la sesión expiró → vamos a ver Keycloak. Abort.
        if "identidad-gcaba" in page.url:
            log("⚠️  sesión expiró antes de empezar. Abort.")
            return {"error": "session expired", "candidatos_totales": total}

        # Si la grilla está vacía (volvimos a la bandeja sin Buscar previo), buscar
        try:
            buscar = page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first
            if buscar.is_visible(timeout=1500):
                buscar.click(force=True)
                time.sleep(5)
        except Exception:
            pass

        agregados = 0
        errores = 0
        for sheet_row, numero in candidates:
            try:
                log(f"Procesando {numero} (fila {sheet_row})…")
                urls = _process_one_ticket(page, numero)
                if urls:
                    ws.update_cell(sheet_row, col_adjuntos + 1, " | ".join(urls))
                    log(f"  ✓ {len(urls)} URL(s) escritas")
                    agregados += 1
                else:
                    log(f"  (no había adjuntos detectables — no escribo nada)")
            except Exception as e:
                log(f"  ✗ {numero}: {e!r}")
                errores += 1
                # Volver a la bandeja por las dudas
                try:
                    page.goto(f"{BACKOFFICE_URL}/contacto/bandeja")
                    time.sleep(3)
                    buscar = page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first
                    if buscar.is_visible(timeout=1500):
                        buscar.click(force=True)
                        time.sleep(5)
                except Exception:
                    pass

        return {
            "procesados": len(candidates),
            "agregados": agregados,
            "errores": errores,
            "candidatos_totales": total,
        }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Uso: python update_adjuntos.py <csv_path> <spreadsheet_id>")
        sys.exit(1)
    stats = process_adjuntos(Path(sys.argv[1]), sys.argv[2])
    print(stats)
