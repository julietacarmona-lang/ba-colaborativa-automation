#!/bin/bash
# Lanza una instancia de Chrome dedicada al scraper, escuchando en el puerto
# 9222 para que Playwright se pueda conectar por CDP.
#
# Importante: usa un --user-data-dir distinto al de tu Chrome normal, así
# no interfiere con tu sesión diaria y Chrome no bloquea el remote debugging.
#
# Ejecutar desde la raíz del proyecto:
#   ./scripts/chrome-cdp.sh
#
# La primera vez que lo abras, tenés que loguearte una vez en BA Colaborativa.
# Después, la sesión queda guardada en el perfil dedicado.

set -e

PORT="${CDP_PORT:-9222}"
PROFILE_DIR="${CDP_PROFILE_DIR:-$HOME/.chrome-bacolaborativa}"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if [ ! -x "$CHROME" ]; then
  echo "❌ No encontré Google Chrome en $CHROME"
  exit 1
fi

mkdir -p "$PROFILE_DIR"

echo "🚀 Abriendo Chrome con remote-debugging en puerto $PORT"
echo "   Perfil: $PROFILE_DIR"
echo "   Dejá esta ventana abierta mientras corrés el scraper."
echo ""

exec "$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --no-default-browser-check \
  --no-first-run
