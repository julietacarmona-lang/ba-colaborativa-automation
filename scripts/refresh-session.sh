#!/bin/bash
# Refresca la sesión de BA Colaborativa para que el workflow de GitHub
# Actions pueda correr otra vez. Pasos:
#  1. Asegura que Chrome dedicado está abierto.
#  2. Te pide confirmación una vez que estés logueada en la bandeja.
#  3. Dumpea cookies + storage al session.json.
#  4. Sube el contenido como secret BA_SESSION_JSON en GitHub.
#  5. Borra session.json local.
#  6. Dispara el workflow.
#
# Ejecutar desde la raíz del proyecto:
#   ./scripts/refresh-session.sh

set -e

cd "$(dirname "$0")/.."

REPO="july-carmona/tickets-automation"
GH="$HOME/.local/bin/gh"

echo "🔧 Refresh de sesión de BA Colaborativa"
echo ""

# 1. Chequear/abrir Chrome CDP
if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
    echo "✓ Chrome CDP ya está corriendo en puerto 9222."
else
    echo "→ Lanzando Chrome dedicado…"
    ./scripts/chrome-cdp.sh &
    sleep 3
fi

# 2. Abrir tab de la bandeja
curl -s -X PUT "http://localhost:9222/json/new?https://bacolaborativa-backoffice.buenosaires.gob.ar/contacto/bandeja" >/dev/null

echo ""
echo "👉 Andá a la ventana de Chrome dedicada, logueate y entrá a la Bandeja de entrada."
echo "   (Contactos → Bandeja de entrada en el menú)"
echo ""
read -p "Apretá ENTER cuando estés en la bandeja con los tickets visibles… " _

# 3. Dump
.venv/bin/python scripts/dump_cookies.py >/dev/null
echo "✓ Estado dumpeado a session.json"

# 4. Subir como secret
$GH secret set BA_SESSION_JSON --body "$(cat session.json)" --repo "$REPO" >/dev/null
echo "✓ Secret BA_SESSION_JSON actualizado en GitHub."

# 5. Borrar local
rm session.json
echo "✓ session.json local borrado."

# 6. Reactivar el workflow si estaba pausado (después de 2 fails seguidos
#    el step de Slack ERROR lo pausa con `gh workflow disable`). Idempotente.
$GH workflow enable "Bajada diaria de tickets BA Colaborativa" --repo "$REPO" >/dev/null 2>&1 \
    && echo "✓ Workflow reactivado (estaba pausado)." \
    || echo "(Workflow ya estaba activo, sigo.)"

# 7. Disparar workflow
$GH workflow run "Bajada diaria de tickets BA Colaborativa" --repo "$REPO" >/dev/null
sleep 4
RUN=$($GH run list --repo "$REPO" --limit 1 --json databaseId -q '.[0].databaseId')
echo "✓ Workflow disparado: https://github.com/$REPO/actions/runs/$RUN"
echo ""
echo "Vas a recibir el mensaje en Slack en ~3 minutos."
