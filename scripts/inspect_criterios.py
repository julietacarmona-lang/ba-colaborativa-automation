"""Inspecciona el DOM del panel de Criterios de búsqueda conectándose por CDP
al Chrome dedicado. Dumpea estructura útil de los ng-select para diseñar el
fallback manual de filtros.

Uso:
    source .venv/bin/activate
    python scripts/inspect_criterios.py
"""

from __future__ import annotations

import sys
from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        if not browser.contexts:
            print("No hay contextos en Chrome.")
            sys.exit(1)
        ctx = browser.contexts[0]
        page = None
        for pg in ctx.pages:
            if "bacolaborativa-backoffice" in pg.url:
                page = pg
                break
        if page is None:
            print("No encontré la pestaña del backoffice abierta.")
            sys.exit(1)
        print(f"URL: {page.url}")

        # Asegurar que el panel está expandido
        try:
            buscar = page.get_by_role("button", name="Buscar")
            if not buscar.first.is_visible(timeout=500):
                header = page.get_by_text("Criterios de búsqueda").first
                header.click()
                page.wait_for_timeout(500)
        except Exception as e:
            print(f"(no pude verificar panel: {e})")

        # Dumpear los ng-select en orden, con sus aria-labels / placeholders / ng-value
        info = page.evaluate(
            """
            () => {
              const sels = Array.from(document.querySelectorAll('ng-select'));
              return sels.map((s, i) => {
                const label = s.closest('tr, div, mat-form-field')?.innerText?.slice(0, 80) || '';
                const ngVal = s.querySelector('.ng-value-label')?.innerText || '';
                const ph = s.querySelector('.ng-placeholder')?.innerText || '';
                const aria = s.getAttribute('aria-label') || '';
                const cls = s.className || '';
                // Bounding box
                const r = s.getBoundingClientRect();
                return { i, ngVal, ph, aria, cls: cls.slice(0,80), label: label.slice(0,80), x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width) };
              });
            }
            """
        )
        print("\n--- ng-select list ---")
        for entry in info:
            print(entry)

        # Buscar filas <tr> que tengan ng-select adentro
        rows = page.evaluate(
            """
            () => {
              const trs = Array.from(document.querySelectorAll('tr')).filter(tr => tr.querySelector('ng-select'));
              return trs.map((tr, i) => ({
                i,
                cells: Array.from(tr.children).map(td => td.tagName + ':' + (td.innerText || '').slice(0,40)),
                sels: tr.querySelectorAll('ng-select').length,
                hasPlus: !!tr.querySelector('button.btn-success, .green, [class*="add"], [class*="plus"]'),
                btns: Array.from(tr.querySelectorAll('button')).map(b => b.textContent?.trim().slice(0,20) || b.getAttribute('aria-label') || b.className.slice(0,30)),
              }));
            }
            """
        )
        print("\n--- TR rows with ng-select ---")
        for r in rows:
            print(r)

        # Buscar botones +/- cercanos al área de criterios
        btns_near = page.evaluate(
            """
            () => {
              const btns = Array.from(document.querySelectorAll('button'));
              return btns
                .filter(b => {
                  const cls = b.className || '';
                  const t = (b.textContent || '').trim();
                  return /success|danger|warning|btn-add|btn-remove/i.test(cls) || /^[+\\-]$/.test(t);
                })
                .map(b => ({
                  text: (b.textContent || '').trim().slice(0, 10),
                  cls: (b.className || '').slice(0, 80),
                  visible: b.offsetParent !== null,
                }));
            }
            """
        )
        print("\n--- candidate +/- buttons ---")
        for b in btns_near:
            print(b)

        browser.close()


if __name__ == "__main__":
    main()
