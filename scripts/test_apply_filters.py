"""Refresca la página del Chrome dedicado, vuelve a la bandeja, expande el
panel de Criterios y ejerce _apply_manual_filters de scraper.py. Útil para
probar el fallback manual sin correr el flujo completo (login + export).
"""

from __future__ import annotations

import sys
from playwright.sync_api import sync_playwright

import scraper


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = None
        for pg in ctx.pages:
            if "bacolaborativa-backoffice" in pg.url:
                page = pg
                break
        if page is None:
            page = ctx.pages[0]
        print(f"URL inicial: {page.url}")

        # Volver a la bandeja desde cero (resetea criterios al default).
        page.goto("https://bacolaborativa-backoffice.buenosaires.gob.ar/contacto/bandeja")
        page.wait_for_load_state("networkidle", timeout=20000)
        page.wait_for_timeout(1500)
        print(f"URL post-goto: {page.url}")

        # Expandir el panel si está cerrado.
        try:
            buscar = page.get_by_role("button", name="Buscar")
            if not buscar.first.is_visible(timeout=1000):
                header = page.get_by_text("Criterios de búsqueda").first
                header.click()
                page.wait_for_timeout(800)
        except Exception as e:
            print(f"(no pude expandir: {e})")

        # Probar la nueva lógica.
        scraper._apply_manual_filters(page)
        print("✓ _apply_manual_filters terminó sin excepción.")

        # Verificar resultado: dumpear los ng-value-label de las filas.
        labels = page.evaluate(
            """
            () => {
              const trs = Array.from(document.querySelectorAll('tr')).filter(
                tr => tr.querySelector('ng-select')
              );
              return trs.map(tr =>
                Array.from(tr.querySelectorAll('.ng-value-label')).map(l => l.innerText.trim())
              );
            }
            """
        )
        print("Filas configuradas:")
        for i, row in enumerate(labels):
            print(f"  Fila {i}: {row}")

        browser.close()


if __name__ == "__main__":
    main()
