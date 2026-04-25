"""Extrae el ESTADO COMPLETO de la sesión activa (cookies + localStorage +
sessionStorage) en el Chrome CDP local, y lo guarda en session.json para
usarlo en GitHub Actions.

Uso:
  1. Tener corriendo ./scripts/chrome-cdp.sh con sesión activa de BA Colaborativa.
  2. Tener una pestaña abierta en la bandeja (importante: el localStorage del
     SPA se llena solo cuando pasaste por la app).
  3. Correr: .venv/bin/python scripts/dump_cookies.py
  4. Subir el contenido de session.json como secret BA_SESSION_JSON en GitHub.

Las cookies/JWT expiran. Cuando el workflow falle, repetir este proceso.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")
OUT_FILE = Path(os.environ.get("SESSION_OUT", "session.json"))
RELEVANT_DOMAINS = (
    "buenosaires.gob.ar",
    "apps.buenosaires.gob.ar",
)


def main() -> int:
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(
                f"❌ No pude conectar a {CDP_URL}. ¿Está corriendo "
                f"./scripts/chrome-cdp.sh con sesión activa? Error: {e}",
                file=sys.stderr,
            )
            return 1

        if not browser.contexts:
            print("❌ El Chrome no tiene contextos activos.", file=sys.stderr)
            return 1

        context = browser.contexts[0]

        # 1. Cookies (filtradas a dominios relevantes)
        all_cookies = context.cookies()
        cookies = [
            c for c in all_cookies
            if any(d in c.get("domain", "") for d in RELEVANT_DOMAINS)
        ]

        # 2. localStorage + sessionStorage de cada página relevante.
        origins = []
        for page in context.pages:
            url = page.url
            if not any(d in url for d in RELEVANT_DOMAINS):
                continue
            try:
                origin = page.evaluate("() => window.location.origin")
                local = page.evaluate("""() => {
                    const out = [];
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        out.push({name: k, value: localStorage.getItem(k)});
                    }
                    return out;
                }""")
                session = page.evaluate("""() => {
                    const out = [];
                    for (let i = 0; i < sessionStorage.length; i++) {
                        const k = sessionStorage.key(i);
                        out.push({name: k, value: sessionStorage.getItem(k)});
                    }
                    return out;
                }""")
                if local or session:
                    origins.append({
                        "origin": origin,
                        "localStorage": local,
                        "sessionStorage": session,
                    })
            except Exception as e:
                print(f"(no pude leer storage de {url[:60]}: {e})", file=sys.stderr)

        if not cookies and not origins:
            print(
                "⚠️  No encontré sesión de BA Colaborativa. ¿Estás logueada "
                "y con la bandeja abierta en el Chrome CDP?",
                file=sys.stderr,
            )
            return 1

        # Formato Playwright storage_state — compatible con context.storage_state.
        state = {"cookies": cookies, "origins": origins}
        OUT_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

        local_total = sum(len(o["localStorage"]) for o in origins)
        session_total = sum(len(o["sessionStorage"]) for o in origins)
        print(f"✓ Estado guardado en {OUT_FILE.resolve()}")
        print(f"  - {len(cookies)} cookies")
        print(f"  - {len(origins)} orígenes con storage")
        print(f"    · {local_total} entradas en localStorage")
        print(f"    · {session_total} entradas en sessionStorage")
        print()
        print("Próximos pasos:")
        print("  1. GitHub → repo → Settings → Secrets and variables → Actions")
        print("  2. Editá (o creá) el secret 'BA_SESSION_JSON'")
        print("  3. Pegá el contenido completo del archivo session.json")
        print("  4. Save")
        print()
        print(f"⚠️  IMPORTANTE: {OUT_FILE} contiene cookies y JWTs sensibles.")
        print(f"   Está en .gitignore. Borrala cuando termines.")

        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
