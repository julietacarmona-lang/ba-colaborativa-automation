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
BROWSER_MODE = os.environ.get("BROWSER_MODE", "cdp").strip().lower()
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "./.playwright-user-data")).resolve()
HEADLESS = os.environ.get("HEADLESS", "false").strip().lower() in ("1", "true", "yes")
BACKOFFICE_URL = "https://bacolaborativa-backoffice.buenosaires.gob.ar"
MAX_TICKETS_PER_RUN = int(os.environ.get("ADJUNTOS_MAX_TICKETS", "20"))

# Solo matchea el wrapper exterior, no los mat-icon anidados que también tienen image-container
ITEM_SEL = "div.image-wrapper, div.image-container"


class _NoUrlsError(RuntimeError):
    """El ticket tiene adjuntos según el CSV pero no se pudo extraer ninguna URL."""


def log(msg: str) -> None:
    print(f"[adjuntos] {msg}", flush=True)


def _extract_urls_from_detail(page, detail_href: str) -> List[str]:
    """En la pestaña del detalle extrae las URLs de todos los adjuntos.

    Estrategia por tipo:
      - Imagen: <img src> en el wrapper (sin click).
      - PDF/otro vía JS: extrae href/data-url del elemento o __ngContext__ de Angular.
      - PDF/otro vía popup: el click abre nueva pestaña.
      - PDF/otro vía navegación: el click navega la página actual → capturamos URL y volvemos.
    """
    accordion = page.locator("app-panel-desplegable").filter(
        has=page.get_by_role("button", name=re.compile(r"archivos del contacto", re.I))
    )
    try:
        accordion.first.wait_for(state="attached", timeout=10000)
    except PlaywrightTimeoutError:
        log("  (no apareció la sección 'Archivos del contacto')")
        return []
    except Exception as e:
        log(f"  (error esperando accordion: {e!r})")
        return []

    # Capturar respuestas de API cuando se expande el accordion
    api_responses: list = []

    def _on_accordion_response(resp):
        try:
            url = resp.url
            if resp.status == 200 and "GCS-backend" in url:
                body = resp.text()
                api_responses.append({"url": url, "body": body[:2000]})
        except Exception:
            pass

    page.on("response", _on_accordion_response)
    try:
        header_btn = accordion.locator(".accordion-button").first
        if "collapsed" in (header_btn.get_attribute("class") or ""):
            header_btn.click(force=True)
            try:
                accordion.locator(".accordion-collapse.show").first.wait_for(state="visible", timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
        # Si el accordion YA estaba abierto (sin "collapsed"), cerrarlo y reabrirlo
        # para forzar una nueva llamada a la API (puede haber estado abierto por visita previa)
        else:
            header_btn.click(force=True)  # cerrar
            page.wait_for_timeout(500)
            header_btn.click(force=True)  # reabrir → dispara API
            try:
                accordion.locator(".accordion-collapse.show").first.wait_for(state="visible", timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
    except Exception:
        pass
    page.wait_for_timeout(1500)  # esperar respuestas pendientes
    page.remove_listener("response", _on_accordion_response)

    # Extraer URLs de las respuestas de API capturadas
    import json as _json
    from_api: List[str] = []
    for r in api_responses:
        body_text = r["body"]
        # Primero intentar parsear JSON
        extracted = False
        try:
            data = _json.loads(body_text)
            items = data if isinstance(data, list) else (data.get("items") or data.get("archivos") or [])
            for item in items:
                if isinstance(item, dict) and item.get("url"):
                    from_api.append(item["url"])
                    extracted = True
        except Exception:
            pass
        # Fallback: regex sobre el body si el JSON falla
        if not extracted:
            url_matches = re.findall(
                r'https?://[^\s"\'<>\\]+(?:adjunto|archivo|file|cdn|download)[^\s"\'<>\\]*',
                body_text,
                re.I,
            )
            from_api.extend(url_matches)
    if from_api:
        unique_api = list(dict.fromkeys(from_api))
        log(f"  → {len(unique_api)} URL(s) de adjuntos vía API")
        return unique_api

    # Polling: esperar hasta 5s para que Angular cargue los items
    item_count = 0
    for _ in range(5):
        item_count = accordion.locator(ITEM_SEL).count()
        if item_count > 0:
            break
        page.wait_for_timeout(1000)

    if item_count == 0:
        try:
            html_sample = accordion.first.inner_html(timeout=2000)
            log(f"  ⚠️  sección encontrada pero 0 items ({ITEM_SEL!r}). HTML: {html_sample[:300]!r}")
        except Exception:
            log(f"  ⚠️  sección encontrada pero 0 items ({ITEM_SEL!r})")
    else:
        log(f"  {item_count} item(s) encontrados.")
    urls: List[str] = []

    for idx in range(item_count):
        log(f"  → item {idx}: evaluando tiers…")
        # Re-fetching por índice: el DOM puede haberse refrescado si hubo navegación y back.
        try:
            acc = page.locator("app-panel-desplegable").filter(
                has=page.get_by_role("button", name=re.compile(r"archivos del contacto", re.I))
            )
            item = acc.locator(ITEM_SEL).nth(idx)
            item.wait_for(state="attached", timeout=3000)
        except Exception as e:
            log(f"  ⚠️  item {idx} no disponible: {e}")
            continue

        # DEBUG: ver HTML y Angular context del item
        try:
            item_html = item.inner_html(timeout=2000)
            log(f"  [debug] item {idx} HTML: {item_html[:300]}")
        except Exception as e:
            log(f"  [debug] item {idx} HTML error: {e}")
        try:
            ng_data = item.evaluate("""el => {
                if (!window.ng || !window.ng.getComponent) return 'no_ng';
                // Buscar en el item y todos sus ancestros
                let node = el;
                while (node && node.tagName !== 'BODY') {
                    try {
                        const comp = window.ng.getComponent(node);
                        if (comp) {
                            const s = JSON.stringify(comp, (k,v) => typeof v === 'function' ? undefined : v);
                            // Buscar cualquier URL http
                            const m = s.match(/"(?:url|src|href|file|ruta|path|nombre|adjunto|name|link|urlArchivo|urlAdjunto)[^"]*":\\s*"(https?:\\/\\/[^"]+)"/i);
                            if (m) return 'found:' + m[1];
                            return 'comp_at_' + node.tagName + ':' + s.slice(0, 400);
                        }
                    } catch(e) {}
                    node = node.parentElement;
                }
                return 'no_comp_any';
            }""")
            log(f"  [debug] ngData: {ng_data[:200]}")
        except Exception as e:
            log(f"  [debug] ngData error: {e}")

        # 1. Imagen: <img src> directo, sin click.
        try:
            img_src = item.locator("img").first.get_attribute("src", timeout=1500)
            if img_src:
                log(f"  [debug] img src: {img_src[:120]}")
            if img_src and img_src.startswith("http") and BACKOFFICE_URL not in img_src:
                urls.append(img_src)
                continue
        except Exception:
            pass

        # 2. Sin click: intentar extraer URL de atributos del DOM o de __ngContext__ de Angular.
        try:
            pdf_url = item.evaluate("""el => {
                const a = el.querySelector('a[href]');
                if (a && a.href && !a.href.startsWith('javascript')) return a.href;
                for (const attr of ['data-url', 'data-src', 'data-href', 'data-file']) {
                    const v = el.getAttribute(attr);
                    if (v && v.startsWith('http')) return v;
                }
                const ctx = el.__ngContext__;
                if (ctx) {
                    const s = JSON.stringify(ctx);
                    const m = s.match(/"(?:url|src|href|file|link)"\\s*:\\s*"(https?:\\/\\/[^"]+)"/);
                    if (m) return m[1];
                }
                return null;
            }""")
            if pdf_url and pdf_url.startswith("http"):
                urls.append(pdf_url)
                continue
        except Exception:
            pass

        # 3. Interceptar la request de red antes del click para capturar la URL
        #    del adjunto SIN descargarlo. Capturamos CDN y llamadas al backend
        #    de archivos (GCS-backend/adjunto, GCS-backend/file, etc.).
        intercepted: list = []
        all_reqs: list = []

        def _intercept(route):
            url = route.request.url
            all_reqs.append(url)
            is_adjunto = (
                "cdn.buenosaires" in url
                or "adjuntosSUACI" in url
                or "/adjunto" in url.lower()
                or "/archivo" in url.lower()
                or "/file" in url.lower()
                or "/download" in url.lower()
            )
            if is_adjunto:
                intercepted.append(url)
                try:
                    route.abort()
                except Exception:
                    pass
            else:
                try:
                    route.continue_()
                except Exception:
                    pass

        page.route("**/*", _intercept)
        try:
            item.click()
            page.wait_for_timeout(2000)
        except Exception:
            pass
        page.unroute("**/*", _intercept)

        # Debug: mostrar todos los requests disparados por el click
        if not intercepted and all_reqs:
            log(f"  [debug] {len(all_reqs)} req(s) al clickear, ninguno capturado como adjunto:")
            for u in all_reqs[-5:]:
                log(f"    {u[:120]}")
        if intercepted:
            for u in intercepted:
                log(f"  → interceptado: {u[:80]}")
            urls.extend(intercepted)
            continue

        # 4. Popup: el click abrió pestaña nueva.
        # IMPORTANTE: excluir la página principal (bandeja) que ya existe en el contexto.
        for pp in list(page.context.pages):
            pp_url = pp.url or ""
            if (pp is not page
                    and pp_url not in ("about:blank", "", detail_href)
                    and "/contacto/bandeja" not in pp_url
                    and "identidad-gcaba" not in pp_url):
                try:
                    pp.wait_for_load_state("domcontentloaded", timeout=5000)
                    log(f"  → popup capturado: {pp.url[:80]}")
                    urls.append(pp.url)
                    pp.close()
                except Exception:
                    pass
                break

        # 5. Navegación de la página actual (PDF que navega en lugar de descargar).
        #    Solo captura si navegó a un dominio DISTINTO al backoffice
        #    (evita falsos positivos por Angular cambiando query params).
        else:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
                nav_url = page.url
                detail_base = detail_href.split("?")[0]
                nav_base = (nav_url or "").split("?")[0]
                is_real_file_nav = (
                    nav_url
                    and nav_base != detail_base
                    and BACKOFFICE_URL not in nav_url
                    and "identidad-gcaba" not in nav_url
                )
                if is_real_file_nav:
                    log(f"  → navegación capturada: {nav_url[:80]}")
                    urls.append(nav_url)
                    # Volver al detalle con go_back
                    page.go_back()
                    page.wait_for_timeout(3000)
                else:
                    page.wait_for_timeout(1000)
                # Re-expandir accordion en ambos casos
                try:
                    acc2 = page.locator("app-panel-desplegable").filter(
                        has=page.get_by_role("button", name=re.compile(r"archivos del contacto", re.I))
                    )
                    btn2 = acc2.locator(".accordion-button").first
                    if "collapsed" in (btn2.get_attribute("class") or ""):
                        btn2.click(force=True)
                        page.wait_for_timeout(2000)
                except Exception:
                    pass
            except Exception as e:
                log(f"  ⚠️  adjunto idx={idx} falló en tier5: {e!r}")

    # DEBUG: buscar URLs en todos los componentes del detalle
    if not urls:
        try:
            all_comp_data = page.evaluate("""() => {
                if (!window.ng || !window.ng.getComponent) return 'no_ng';
                const results = [];
                // Probar en todos los custom elements de la página
                const els = document.querySelectorAll('app-panel-desplegable, app-adjunto, app-archivos, [class*="adjunto"], [class*="archivo"]');
                for (const el of els) {
                    try {
                        const comp = window.ng.getComponent(el);
                        if (comp) {
                            const s = JSON.stringify(comp, (k,v) => typeof v === 'function' ? undefined : v);
                            results.push(el.tagName + '/' + el.className.slice(0,30) + ':' + s.slice(0, 300));
                        }
                    } catch(e) {}
                }
                return results.length ? results.join('\\n---\\n') : 'ningún componente Angular encontrado';
            }""")
            log(f"  [debug] componentes detalle: {all_comp_data[:600]}")
        except Exception as e:
            log(f"  [debug] componentes error: {e}")

    seen = set()
    unique: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


BA_USER = os.environ.get("BA_USER", "").strip()
BA_PASSWORD = os.environ.get("BA_PASSWORD", "")


def _wait_for_visible_any(page, factories, timeout_ms: int = 15000):
    """Devuelve el primer locator visible de la lista (replica scraper._first_visible)."""
    import time as _t
    deadline = _t.time() + timeout_ms / 1000
    last_err = None
    while _t.time() < deadline:
        for factory in factories:
            try:
                loc = factory().first
                loc.wait_for(state="visible", timeout=500)
                return loc
            except Exception as e:
                last_err = e
        page.wait_for_timeout(250)
    raise RuntimeError(f"Ningún selector visible en {timeout_ms}ms. Último: {last_err}")


def _detect_auth_state(page, timeout_s: float = 30.0) -> str:
    """Espera hasta que aparezca login form O nav del backoffice. Devuelve 'login'|'backoffice'|'unknown'."""
    pw = page.locator('input[type="password"]')
    login_text = page.get_by_text(re.compile(r"Iniciar sesi[oó]n|Usuario \(CUIL", re.I))
    nav = page.get_by_text(re.compile(r"^\s*(Contactos|Ciudadanos)\s*$", re.I))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if pw.first.is_visible(timeout=200):
                return "login"
        except Exception:
            pass
        try:
            if login_text.first.is_visible(timeout=200):
                return "login"
        except Exception:
            pass
        try:
            if nav.first.is_visible(timeout=200):
                return "backoffice"
        except Exception:
            pass
        page.wait_for_timeout(400)
    return "unknown"


def _authenticate_and_goto_bandeja(page) -> None:
    """Navega a root, login si expiró la sesión, va a bandeja via menú (Angular router).
    Uso de menú en vez de goto() para evitar que OAuth redirija siempre a /."""
    log("  Autenticando y navegando a bandeja…")

    # Ir a root: el SPA bootstrap + OAuth arranca desde aquí
    try:
        page.goto(BACKOFFICE_URL + "/", wait_until="commit", timeout=20000)
    except Exception as e:
        if "interrupted" not in str(e).lower():
            raise

    # Esperar Angular
    try:
        page.wait_for_function(
            "() => { const r = document.querySelector('app-root'); return r && r.children.length > 0; }",
            timeout=30000,
        )
    except Exception:
        pass
    time.sleep(2)

    # Detectar si se necesita login o ya está autenticado
    state = _detect_auth_state(page, timeout_s=30.0)

    if state == "login":
        if not BA_USER or not BA_PASSWORD:
            raise RuntimeError("sesión expirada pero BA_USER/BA_PASSWORD no están configurados")
        log("  (sesión expirada — logueando)")
        try:
            u = _wait_for_visible_any(page, [
                lambda: page.locator("#username"),
                lambda: page.locator("input[name='username']"),
                lambda: page.locator("input[autocomplete='username']"),
            ], timeout_ms=10000)
            u.fill("")
            u.type(BA_USER, delay=60)
            page.wait_for_timeout(600)
            pw = _wait_for_visible_any(page, [
                lambda: page.locator("#password"),
                lambda: page.locator("input[name='password']"),
                lambda: page.locator("input[type='password']"),
            ], timeout_ms=5000)
            pw.fill("")
            pw.type(BA_PASSWORD, delay=60)
            page.wait_for_timeout(1200)
            _wait_for_visible_any(page, [
                lambda: page.locator("button[type='submit']"),
                lambda: page.get_by_role("button", name=re.compile(r"ingresar|iniciar|acceder|continuar|entrar", re.I)),
                lambda: page.locator("input[type='submit']"),
            ], timeout_ms=5000).click()
        except Exception as e:
            raise RuntimeError(f"login falló: {e}")
        # Esperar que Keycloak redirija al backoffice (igual que scraper: 45s)
        try:
            page.wait_for_url(
                re.compile(r"bacolaborativa-backoffice\.buenosaires\.gob\.ar"),
                timeout=45000,
            )
        except Exception:
            pass
        # Esperar que Angular procese el OAuth callback y renderice el nav
        try:
            page.wait_for_function(
                "() => { const r = document.querySelector('app-root'); return r && r.children.length > 0; }",
                timeout=20000,
            )
        except Exception:
            pass
        time.sleep(3)
        # Verificar estado (busca Contactos en el nav)
        state = _detect_auth_state(page, timeout_s=30.0)
        if state != "backoffice":
            raise RuntimeError(f"login no llevó al backoffice (state={state!r}, url={page.url[:80]!r})")

    elif state == "unknown":
        raise RuntimeError(f"estado desconocido después de cargar root (url={page.url[:80]!r})")

    # Navegar a bandeja via click en menú (Angular router, no goto → no OAuth)
    contactos = _wait_for_visible_any(page, [
        lambda: page.get_by_role("button", name=re.compile(r"^\s*contactos\s*$", re.I)),
        lambda: page.get_by_role("link", name=re.compile(r"^\s*contactos\s*$", re.I)),
        lambda: page.locator("nav, header").get_by_text(re.compile(r"^\s*contactos\s*$", re.I)),
        lambda: page.get_by_text(re.compile(r"^\s*contactos\s*$", re.I)),
    ], timeout_ms=15000)
    contactos.click(force=True)
    time.sleep(1)

    bandeja_item = _wait_for_visible_any(page, [
        lambda: page.get_by_role("menuitem", name=re.compile(r"bandeja", re.I)),
        lambda: page.get_by_role("link", name=re.compile(r"bandeja", re.I)),
        lambda: page.locator("a, li, button").filter(has_text=re.compile(r"bandeja", re.I)),
    ], timeout_ms=5000)
    bandeja_item.click(force=True)

    # Esperar que la bandeja cargue (botón Buscar visible)
    try:
        page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first.wait_for(
            state="visible", timeout=25000
        )
    except Exception:
        # Panel colapsado — expandir
        try:
            page.get_by_role("button", name=re.compile(r"criterios de b[uú]squeda", re.I)).first.click(force=True)
            time.sleep(2)
        except Exception:
            pass

    if "/contacto/bandeja" not in page.url:
        raise RuntimeError(f"no llegué a bandeja (url={page.url!r})")
    log("  ✓ Bandeja lista.")


def _ng_select_open_and_pick(page, ng_sel_locator, option_text_re, search_text: str = "") -> None:
    """Abre un ng-select usando coordenadas del mouse (bypassa visibility checks)
    y selecciona la opción que matchea option_text_re."""
    # Scroll + click via coordenadas reales (bypassa Playwright visibility checks)
    coords = ng_sel_locator.evaluate("""el => {
        el.scrollIntoView({block: 'center', behavior: 'instant'});
        const r = el.getBoundingClientRect();
        return {x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width};
    }""")
    if coords and coords.get("w", 0) > 0:
        page.mouse.click(coords["x"], coords["y"])
    else:
        ng_sel_locator.evaluate("el => el.click()")
    page.wait_for_timeout(600)

    # Tipear texto para filtrar opciones
    if search_text:
        try:
            inp = ng_sel_locator.locator(".ng-input input").first
            inp.type(search_text, delay=30)
        except Exception:
            page.keyboard.type(search_text, delay=30)
        page.wait_for_timeout(600)

    # Seleccionar opción
    page.locator(".ng-option").filter(has_text=option_text_re).first.click(timeout=10000)
    page.wait_for_timeout(600)


def _search_on_bandeja(page, numero: str) -> None:
    """Asume que page está en /contacto/bandeja. Configura el criterio
    'Número de contacto = {numero}' como ÚNICO criterio y hace click en Buscar.
    Elimina criterios previos (ej. Estado=Abierto) para no excluir tickets cerrados."""
    buscar_loc = page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first

    # Expandir panel si hace falta
    if not buscar_loc.is_visible(timeout=1000):
        try:
            page.get_by_role("button", name=re.compile(r"criterios de b[uú]squeda", re.I)).first.click(force=True)
            time.sleep(2)
        except Exception:
            pass

    # Esperar filas de criterios
    rows = []
    for _ in range(5):
        rows = page.locator("tr").filter(has=page.locator("ng-select")).all()
        rows = [r for r in rows if r.locator("ng-select").count() >= 2]
        if rows:
            break
        time.sleep(2)
    if not rows:
        raise RuntimeError("no detecté filas de criterios")

    # Buscar fila "Número de contacto" existente
    target_row = None
    non_numero_rows = []
    for r in rows:
        try:
            labels = r.locator(".ng-value-label").all_inner_texts()
            if any("número de contacto" in (lbl or "").lower() for lbl in labels):
                target_row = r
            else:
                non_numero_rows.append(r)
        except Exception:
            non_numero_rows.append(r)

    # FIX Error 2: eliminar criterios que no son Número (ej. Estado=Abierto)
    # para que la búsqueda no excluya tickets cerrados o reasignados.
    if target_row is not None:
        for r in non_numero_rows:
            try:
                del_btn = r.locator(
                    "button.removeButton, button[title='Eliminar'], "
                    "button[aria-label='Eliminar'], button[aria-label='Remove']"
                ).first
                del_btn.evaluate("el => el.click()")
                page.wait_for_timeout(400)
            except Exception:
                pass

    if target_row is None:
        # FIX Error 1: abrir ng-select via coordenadas de mouse (no JS click)
        # Primero eliminar filas no-Número para dejar solo la fila vacía
        for r in non_numero_rows[:-1]:  # dejar la última como base
            try:
                del_btn = r.locator(
                    "button.removeButton, button[title='Eliminar'], "
                    "button[aria-label='Eliminar'], button[aria-label='Remove']"
                ).first
                del_btn.evaluate("el => el.click()")
                page.wait_for_timeout(400)
            except Exception:
                pass

        # Refetch para obtener la fila que queda
        rows = page.locator("tr").filter(has=page.locator("ng-select")).all()
        rows = [r for r in rows if r.locator("ng-select").count() >= 2]
        if not rows:
            raise RuntimeError("no quedaron filas de criterios tras limpiar")

        target_row = rows[-1]
        sels = target_row.locator("ng-select").all()

        # Verificar si la fila ya tiene "Número de contacto" seleccionado
        try:
            existing_labels = sels[0].locator(".ng-value-label").all_inner_texts()
            already_numero = any("número de contacto" in (l or "").lower() for l in existing_labels)
        except Exception:
            already_numero = False

        if not already_numero:
            _ng_select_open_and_pick(
                page, sels[0], re.compile(r"n[uú]mero de contacto", re.I), search_text="numer"
            )

    # Llenar el valor del número
    inp = target_row.locator("input[type='text'], input:not([type])").last
    inp.evaluate("el => el.scrollIntoView({block:'center'})")
    inp.click()
    inp.fill(numero)
    page.wait_for_timeout(400)
    buscar_loc.click(force=True)
    time.sleep(4)


def _navigate_to_bandeja_via_menu(page) -> None:
    """Navega a bandeja clickeando Contactos → Bandeja de entrada (Angular router)."""
    try:
        btn = page.get_by_role("button", name=re.compile(r"^contactos$", re.I)).first
        btn.wait_for(state="visible", timeout=8000)
        btn.click()
        time.sleep(1)
        page.get_by_role("menuitem", name=re.compile(r"bandeja", re.I)).first.click()
        time.sleep(3)
    except Exception as e:
        raise RuntimeError(f"no pude navegar a bandeja via menú: {e}")


def _process_one_ticket(page, numero: str) -> List[str]:
    """Busca el ticket en bandeja, hace click en 'Ver detalles' (Angular router —
    no dispara OAuth), extrae adjuntos, y vuelve a bandeja con go_back()."""
    _search_on_bandeja(page, numero)

    cell = page.locator("datatable-body-cell").filter(has_text=numero).first
    cell.wait_for(state="visible", timeout=10000)
    cell.click()

    link = page.get_by_role("link", name=re.compile(r"^\s*ver detalles\s*$", re.I)).first
    link.wait_for(state="visible", timeout=5000)
    detail_href = link.get_attribute("href") or ""
    if detail_href.startswith("/"):
        detail_href = BACKOFFICE_URL + detail_href

    # Click en el link: Angular router navega al detalle SIN disparar OAuth
    link.click()
    time.sleep(4)

    detail_url = page.url
    try:
        urls = _extract_urls_from_detail(page, detail_url)
    finally:
        # Volver a bandeja via go_back (Angular router popstate, no OAuth)
        try:
            page.go_back()
            time.sleep(3)
        except Exception:
            pass
        # Si go_back no nos devolvió a bandeja, navegar via menú
        if "/contacto/bandeja" not in (page.url or ""):
            try:
                _navigate_to_bandeja_via_menu(page)
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
    csv_sin_adjunto = set(
        df[~df["Contiene archivos adjuntos"].astype(str).str.strip().str.lower().isin(["si", "sí"])]["Número"]
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

    candidates = []        # (sheet_row_1indexed, numero) — tienen adjuntos, sin URL todavía
    sin_adj_rows = []      # sheet_row_1indexed — no tienen adjuntos, celda vacía
    for i, row in enumerate(all_rows[1:], start=2):
        def cell(idx):
            return row[idx].strip() if idx < len(row) else ""
        numero = cell(col_numero)
        adj = cell(col_adjuntos)
        if not numero:
            continue
        if adj:  # ya tiene algo (URL o "sin adjuntos") — no tocar
            continue
        if numero in csv_con_adjunto:
            resp = cell(col_resp_cerrada).lower()
            if resp not in ("sí", "si"):
                candidates.append((i, numero))
        elif numero in csv_sin_adjunto:
            sin_adj_rows.append(i)

    # Marcar "sin adjuntos" en batch para tickets sin archivos
    if sin_adj_rows:
        log(f"Marcando {len(sin_adj_rows)} ticket(s) como 'sin adjuntos'…")
        try:
            cell_list = [gspread.Cell(r, col_adjuntos + 1, "sin adjuntos") for r in sin_adj_rows]
            ws.update_cells(cell_list, value_input_option="RAW")
            log(f"  ✓ {len(sin_adj_rows)} celdas marcadas.")
        except Exception as e:
            log(f"  ⚠️  batch update 'sin adjuntos' falló: {e}")

    total = len(candidates)
    log(f"Candidatos con adjuntos: {total}. Proceso hasta {MAX_TICKETS_PER_RUN} en esta corrida.")
    candidates = candidates[:MAX_TICKETS_PER_RUN]
    if not candidates:
        return {"procesados": 0, "agregados": 0, "errores": 0, "candidatos_totales": total, "sin_adjuntos_marcados": len(sin_adj_rows)}

    # 3. Conectar al browser (CDP o persistent según BROWSER_MODE)
    with sync_playwright() as p:
        cleanup = None
        if BROWSER_MODE == "cdp":
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception as e:
                log(f"⚠️  no pude conectar a {CDP_URL}: {e}")
                return {"error": f"CDP: {e}", "candidatos_totales": total}
            if not browser.contexts:
                log("⚠️  el browser no tiene contextos.")
                return {"error": "no contexts", "candidatos_totales": total}
            ctx = browser.contexts[0]
            cleanup = lambda: browser.close()
        else:
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=HEADLESS,
                slow_mo=150,
                accept_downloads=True,
                viewport={"width": 1280, "height": 900},
                channel="chrome",
            )
            cleanup = lambda: ctx.close()

        import notify as _notify

        # 4. Autenticar UNA vez y obtener la pestaña de bandeja reutilizable.
        #    _authenticate_and_goto_bandeja navega root → login si expiró → menú → bandeja.
        #    El click en el menú usa Angular router (no dispara OAuth en futuros gotos).
        bandeja_page = ctx.new_page()
        try:
            _authenticate_and_goto_bandeja(bandeja_page)
        except Exception as e:
            log(f"⚠️  no pude autenticar/navegar a bandeja: {e!r}")
            if cleanup:
                try:
                    cleanup()
                except Exception:
                    pass
            return {"error": f"auth: {e}", "candidatos_totales": total}

        agregados = 0
        errores = 0
        sin_urls: list = []
        for sheet_row, numero in candidates:
            try:
                log(f"Procesando {numero} (fila {sheet_row})…")
                urls = _process_one_ticket(bandeja_page, numero)
                if urls:
                    ws.update_cell(sheet_row, col_adjuntos + 1, " | ".join(urls))
                    log(f"  ✓ {len(urls)} URL(s) escritas")
                    agregados += 1
                else:
                    raise _NoUrlsError(f"{numero}: tiene adjuntos según CSV pero no se extrajo ninguna URL")
            except _NoUrlsError as e:
                log(f"  ✗ {e} — notificando y continuando con el siguiente.")
                errores += 1
                sin_urls.append(numero)
                _notify.send_slack_error(
                    f"⚠️ *Adjunto sin URL*: `{numero}` tiene archivos adjuntos según el CSV "
                    f"pero no se pudo extraer ninguna URL desde el detalle. Revisar manualmente."
                )
            except Exception as e:
                log(f"  ✗ {numero}: {e!r}")
                errores += 1
                # Verificar si el contexto sigue vivo
                context_alive = False
                try:
                    ctx.pages
                    context_alive = True
                except Exception:
                    pass
                if not context_alive:
                    log("  ⚠️  contexto del browser muerto — deteniendo.")
                    break
                # Re-navegar a bandeja para el próximo ticket
                try:
                    if "/contacto/bandeja" not in (bandeja_page.url or ""):
                        _navigate_to_bandeja_via_menu(bandeja_page)
                except Exception:
                    pass

        if cleanup:
            try:
                cleanup()
            except Exception:
                pass
        return {
            "procesados": len(candidates),
            "agregados": agregados,
            "errores": errores,
            "candidatos_totales": total,
            "sin_urls": sin_urls,
            "sin_adjuntos_marcados": len(sin_adj_rows),
        }


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Uso: python update_adjuntos.py <csv_path> <spreadsheet_id>")
        sys.exit(1)
    stats = process_adjuntos(Path(sys.argv[1]), sys.argv[2])
    print(stats)
