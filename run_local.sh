#!/bin/bash
# Corre el pipeline completo local: abre Chrome, loguea automáticamente y
# actualiza el Google Sheets.
#
# Uso:
#   ./run_local.sh
#
# Requisitos:
#   - .env configurado con BA_USER, BA_PASSWORD, SPREADSHEET_ID, GOOGLE_CREDENTIALS_FILE
#   - .venv con dependencias instaladas (pip install -r requirements.txt)

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Matar cualquier Chrome CDP viejo que pueda interferir
lsof -ti tcp:9222 | xargs kill -9 2>/dev/null || true

source "$ROOT/.venv/bin/activate"

# Adjuntos siempre activos (igual que en CI)
export PROCESAR_ADJUNTOS=1
export BROWSER_MODE=persistent

echo ""
echo "Corriendo pipeline (scraper + update sheets + adjuntos)..."
echo "─────────────────────────────────────────────"
python "$ROOT/main.py"
EXIT_CODE=$?
echo "─────────────────────────────────────────────"
exit $EXIT_CODE
