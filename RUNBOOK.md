# Runbook — Bajada diaria de tickets BA Colaborativa

Qué hacer cuando algo se rompe. El cron corre 4 veces al día (2am, 8am, 14h,
20h ARG). Si una corrida falla, las otras 3 son chances de recuperación
automática. Si todas fallan o el chain se rompe seguido, mirá la sección que
corresponde según el síntoma.

## ¿Cómo me entero de que se rompió?

- **Canal `mis-bots-notis` (privado tuyo)**: avisa errores y timeouts. Si ves
  un 🚨 ahí, algo cayó.
- **Canal del equipo**: avisa éxitos con tickets nuevos. Si no ves nada por
  más de 24-36hs, es señal de que el chain está roto.
- **GitHub Actions**: `gh run list --workflow="Bajada diaria de tickets BA Colaborativa"`
  te muestra el estado de los últimos runs.

## Síntoma 1 — ⏱️ Cron cancelled por timeout (lo más probable)

**Mensaje en Slack**: "El job se canceló (probablemente timeout 45min — el scraper se quedó colgado)."

**Por qué pasa**: las cookies de la sesión caducaron y Keycloak pide login
form. El captcha desde IP de GitHub Actions suele dar challenge visible, y
después de varios reintentos el job llega a los 45min y GitHub lo mata.

**Cómo arreglarlo (5 min, desde tu Mac)**:

```bash
cd ~/Automatizacion-bajada-tickets-sinigep
./scripts/refresh-session.sh
```

Pasos que hace el script:
1. Abre Chrome dedicado con perfil aislado.
2. Te pide que te loguees con Cecilia (`27290343270` / `Educabot*117`) y
   navegues a Contactos → Bandeja de entrada.
3. Cuando estés ahí, apretás ENTER en la terminal.
4. Dumpea cookies + storage y los sube como secret `BA_SESSION_JSON`.
5. Dispara un workflow run de prueba.

Si el run de prueba pasa OK → chain reactivado.

## Síntoma 2 — ❌ "Usuario o contraseña incorrectos"

**Mensaje en Slack**: stack trace que menciona Keycloak rechazando credenciales.

**Por qué pasa**: cambiaron la contraseña de la cuenta de Cecilia, o el
usuario fue deshabilitado.

**Cómo arreglarlo (1 min)**:

```bash
gh secret set BA_PASSWORD --repo july-carmona/tickets-automation
# Te pide pegar la password nueva y enter.
```

Si cambió también el CUIL:

```bash
gh secret set BA_USER --repo july-carmona/tickets-automation
```

Después correr `./scripts/refresh-session.sh` para bootstrappear cookies con
las credenciales nuevas.

## Síntoma 3 — ❌ `RuntimeError` en filtros, modal Exportar o ng-select

**Mensaje en Slack**: stack trace con cosas como `_apply_manual_filters`,
`addButton`, `ng-select`, `No detecté ninguna fila de criterios`, modal
'Columnas a exportar' no apareció, etc.

**Por qué pasa**: el SPA Angular del GCBA cambió clases CSS, IDs o estructura
del DOM. Pasa cada tanto cuando hacen deploys.

**Cómo arreglarlo**: no es algo que arregles sola — avisame a mí o a quien
mantenga el repo. La pista debe ir con el stack trace completo del Slack y el
HTML de debug que GitHub Actions dejó como artifact en el run fallido
(descargable desde el run en Actions → "debug-dumps").

Para debuggear local: `./scripts/chrome-cdp.sh` + loguearse + correr
`python scripts/inspect_criterios.py` para ver el DOM actual.

## Síntoma 4 — ❌ Backend GCBA caído (500, CORS, "fuera de servicio")

**Mensaje en Slack**: el log dice "El reporte no estuvo listo dentro de 600s"
o el banner azul nunca termina, o errores 500/CORS contra
`gestioncolaborativa-backend.buenosaires.gob.ar`.

**Por qué pasa**: GCBA tiene rachas de inestabilidad. No es nuestro.

**Cómo arreglarlo**: no hacer nada por 1-2hs. La siguiente corrida del cron
probablemente pase. Si después de 24hs sigue cayéndose contra el backend,
fijate en https://x.com/GCBA o avisame para que mire si hay un cambio de API.

## Síntoma 5 — ❌ CapSolver / anti-captcha sin créditos

**Mensaje en Slack**: stack trace mencionando "insufficient balance" o
"insufficient funds" desde CapSolver/AntiCaptcha.

**Cómo arreglarlo**: recargar la API key en la web del provider:
- CapSolver: https://dashboard.capsolver.com → Top up
- AntiCaptcha: https://anti-captcha.com → Recargar

No hace falta tocar nada en el repo — el secret no cambió, solo el balance.

## Comandos útiles

```bash
# Ver los últimos runs
gh run list --workflow="Bajada diaria de tickets BA Colaborativa" --limit 10

# Disparar un run manualmente
gh workflow run "Bajada diaria de tickets BA Colaborativa"

# Ver los logs del último run
gh run view --log

# Ver qué secrets están definidos (no muestra valores)
gh secret list --repo july-carmona/tickets-automation
```

## Cuándo escalar / pedir ayuda

Pedí ayuda si:
- Corriste `refresh-session.sh` y la siguiente corrida igual falla con
  timeout (3 veces seguidas).
- El stack trace tiene cosas que no estás reconociendo en este runbook.
- 24hs sin tickets nuevos en el canal del equipo y no ves errores en Slack
  (puede que se haya silenciado por bug).
