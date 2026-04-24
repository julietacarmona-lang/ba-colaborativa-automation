"""Extrae cookies de la sesión activa en el Chrome CDP local y las guarda
en session.json para usarlas en GitHub Actions.

Uso:
  1. Tener corriendo ./scripts/chrome-cdp.sh con sesión activa de BA Colaborativa.
  2. Correr: .venv/bin/python scripts/dump_cookies.py
  3. Subir el contenido de session.json como secret BA_SESSION_JSON en GitHub.

Las cookies expiran cada cierto tiempo (típicamente días/semanas). Cuando el
workflow falle por sesión expirada, repetir este proceso.
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
        all_cookies = context.cookies()
        relevant = [
            c for c in all_cookies
            if any(d in c.get("domain", "") for d in RELEVANT_DOMAINS)
        ]
        if not relevant:
            print(
                "⚠️  No encontré cookies de BA Colaborativa. ¿Estás logueada "
                "en el Chrome CDP?",
                file=sys.stderr,
            )
            return 1

        OUT_FILE.write_text(json.dumps(relevant, indent=2, ensure_ascii=False))
        print(f"✓ Guardadas {len(relevant)} cookies en {OUT_FILE.resolve()}")
        print()
        print("Próximos pasos:")
        print("  1. Abrí GitHub → repo → Settings → Secrets and variables → Actions")
        print("  2. Editá (o creá) el secret 'BA_SESSION_JSON'")
        print("  3. Pegá el contenido completo del archivo session.json")
        print("  4. Save")
        print()
        print(f"⚠️  IMPORTANTE: {OUT_FILE} contiene cookies de sesión sensibles.")
        print(f"   Está en .gitignore, NO la subas al repo. Borrala cuando termines.")

        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
