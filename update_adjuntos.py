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
    de los popups que se abren. Expande el accordion 'Archivos del contacto'
    si está colapsado.
    """
    accordion = page.locator("app-panel-desplegable").filter(
        has=page.get_by_role("button", name=re.compile(r"archivos del contacto", re.I))
    )
    try:
        accordion.first.wait_for(state="attached", timeout=10000)
    except PlaywrightTimeoutError:
        log("  (no apareció la sección 'Archivos del contacto')")
        return []

    # Expandir el accordion si está colapsado (los wrappers no son visibles si está collapsed)
    try:
        header_btn = accordion.locator(".accordion-button").first
        # accordion-button COLLAPSED tiene clase ".collapsed"; si la tiene, clickear
        if "collapsed" in (header_btn.get_attribute("class") or ""):
            header_btn.click(force=True)
            page.wait_for_timeout(800)
    except Exception:
        pass

    items = accordion.locator(".image-wrapper").all()
    urls: List[str] = []
    for item in items:
        # Caso imagen: el wrapper ya tiene <img src=cdn.../> visible.
        # Lo capturamos sin hacer click (más rápido y robusto).
        try:
            img_src = item.locator("img").first.get_attribute("src", timeout=1500)
            if img_src and "cdn.buenosaires" in img_src:
                urls.append(img_src)
                continue
        except Exception:
            pass

        # Caso PDF u otro: hay un mat-icon, hay que clickear para abrir popup.
        try:
            with page.expect_popup(timeout=20000) as popup_info:
                item.click()
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded", timeout=15000)
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
    """En la bandeja: agrega/reusa fila 'Número de contacto contiene {numero}'
    y click Buscar. Después la grilla queda con esa única fila."""
    # Asegurar que estoy en la bandeja
    if "/contacto/bandeja" not in page.url:
        page.goto(f"{BACKOFFICE_URL}/contacto/bandeja")
        time.sleep(5)

    # Si Buscar no es visible, expandir el panel
    buscar = page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first
    if not buscar.is_visible(timeout=2000):
        try:
            page.get_by_text(re.compile(r"Criterios de b[uú]squeda", re.I)).first.click(force=True)
            time.sleep(2)
        except Exception:
            pass

    # Esperar a que las filas estén renderizadas
    try:
        page.locator("tr").filter(has=page.locator("ng-select")).first.wait_for(state="attached", timeout=8000)
        time.sleep(1)
    except Exception:
        pass

    # Estrategia simple: si ya existe fila "Número de contacto", solo cambiar
    # el valor. Si no, agregarla.
    rows = page.locator("tr").filter(has=page.locator("ng-select")).all()
    rows = [r for r in rows if r.locator("ng-select").count() >= 2]
    if not rows:
        raise RuntimeError("no detecté filas de criterios")

    # Buscar si ya hay una fila con "Número de contacto"
    target_row = None
    for r in rows:
        try:
            labels = r.locator(".ng-value-label").all_inner_texts()
            if any("número de contacto" in (l or "").lower() for l in labels):
                target_row = r
                break
        except Exception:
            pass

    if target_row is None:
        # Click + en la última fila para agregar nueva
        try:
            rows[-1].locator("button.addButton, button[title='Agregar']").first.click(force=True)
            page.wait_for_timeout(800)
            rows = page.locator("tr").filter(has=page.locator("ng-select")).all()
            rows = [r for r in rows if r.locator("ng-select").count() >= 2]
            target_row = rows[-1]
        except Exception as e:
            raise RuntimeError(f"no pude agregar fila: {e}")

        # Configurar el campo en la fila nueva
        sels = target_row.locator("ng-select").all()
        sels[0].click()
        page.wait_for_timeout(400)
        try:
            sels[0].locator(".ng-input input").first.fill("")
            sels[0].locator(".ng-input input").first.type("número", delay=20)
        except Exception:
            pass
        page.wait_for_timeout(600)
        page.locator(".ng-option").filter(has_text=re.compile(r"n[uú]mero de contacto", re.I)).first.click()
        page.wait_for_timeout(800)

    # Llenar el valor (input de texto en la última columna de la fila)
    try:
        inp = target_row.locator("input[type='text'], input:not([type])").last
        inp.click()
        inp.fill(numero)
    except Exception as e:
        raise RuntimeError(f"no pude llenar valor: {e}")
    page.wait_for_timeout(500)

    # Buscar
    buscar = page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first
    buscar.wait_for(timeout=8000)
    buscar.click(force=True)
    # Esperar resultados
    time.sleep(4)


def _process_one_ticket(page, numero: str) -> List[str]:
    """Filtra la bandeja por `numero`, abre el detalle en una NUEVA pestaña
    (para no destruir el estado de la pestaña principal con go_back), extrae
    URLs, cierra la pestaña del detalle."""
    _search_by_numero(page, numero)

    # Verificar que aparece la fila con el número
    cell = page.locator("datatable-body-cell").filter(has_text=numero).first
    cell.wait_for(state="visible", timeout=10000)
    cell.click()

    # Capturar el href del link "Ver detalles" en lugar de clickear (así abro
    # en pestaña nueva manualmente y no toco la principal).
    link = page.get_by_role("link", name=re.compile(r"^\s*ver detalles\s*$", re.I)).first
    link.wait_for(state="visible", timeout=5000)
    href = link.get_attribute("href")
    if not href:
        raise RuntimeError("link 'Ver detalles' no tiene href")
    if href.startswith("/"):
        href = BACKOFFICE_URL + href

    # Abrir detalle en NUEVA pestaña del mismo contexto (comparte cookies)
    ctx = page.context
    detail_page = ctx.new_page()
    try:
        detail_page.goto(href, wait_until="domcontentloaded", timeout=20000)
        time.sleep(4)  # Angular hidrata
        urls = _extract_urls_from_detail(detail_page)
    finally:
        try:
            detail_page.close()
        except Exception:
            pass

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
