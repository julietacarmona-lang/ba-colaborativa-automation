# Setup: Comandos de Slack para BA Colaborativa

Permite que cualquier persona en el canal escriba comandos de Slack para operar el bot.

| Comando | Para qué sirve |
|---|---|
| `/bajada-tickets` | Arranca el bot ahora (sin esperar el cron) |
| `/estado-bot` | Muestra si las últimas corridas anduvieron bien |
| `/renovar-sesion` | Explica cómo renovar las cookies si el bot no puede loguearse |

---

## Paso 1 — Crear la Slack App

1. Ir a https://api.slack.com/apps → **Create New App** → **From scratch**
2. Nombre: `BA Colaborativa Bot`
3. Workspace: elegir el workspace del equipo

### Crear los slash commands (después del Paso 2, cuando tengas la URL del Worker)

En la app → **Slash Commands** → **Create New Command** × 3:

| Command | Request URL | Description |
|---|---|---|
| `/bajada-tickets` | `https://lively-pond-17cd.julieta-carmona.workers.dev` | Dispara la bajada de tickets |
| `/estado-bot` | `https://lively-pond-17cd.julieta-carmona.workers.dev` | Ver estado de las últimas corridas |
| `/renovar-sesion` | `https://lively-pond-17cd.julieta-carmona.workers.dev` | Instrucciones para renovar cookies |

### Instalar la app
**OAuth & Permissions** → **Install to Workspace** → Autorizar

### Copiar el Signing Secret
**Basic Information** → **Signing Secret** → copiar

---

## Paso 2 — Configurar el Worker en Cloudflare

El Worker ya existe en `https://lively-pond-17cd.julieta-carmona.workers.dev`.

Para actualizar el código: Cloudflare Dashboard → Workers → `lively-pond-17cd` → Edit Code → pegar `worker.js`.

### Secrets a configurar en el Worker

Workers → `lively-pond-17cd` → **Settings** → **Variables** → **Add variable** (marcar como Secret):

| Variable | Valor |
|---|---|
| `SLACK_SIGNING_SECRET` | Signing Secret de la Slack App (Paso 1) |
| `GITHUB_TOKEN` | PAT de GitHub (ver abajo) |
| `REFRESH_TOKEN` | Cualquier string largo y aleatorio — **anotalo**, lo necesitás para el bookmarklet |

**Cómo crear el `REFRESH_TOKEN`**: inventá algo del tipo `ba-renovar-AbCd1234xYzW` (mínimo 20 caracteres). No se puede recuperar, así que guardalo.

### Cómo crear el GitHub PAT
1. Ir a https://github.com/settings/tokens → **Generate new token (classic)**
2. Nombre: `slack-trigger-ba`
3. Scopes: tildar `workflow` + `repo`
4. Expiration: 1 year
5. Copiar el token (empieza con `ghp_...`)

---

## Paso 3 — Crear el bookmarklet de renovación

El bookmarklet le permite a cualquier persona renovar las cookies del bot desde su browser,
sin necesidad de terminal ni acceso técnico.

Reemplazá `TU_REFRESH_TOKEN_AQUI` con el valor de `REFRESH_TOKEN` que elegiste arriba,
luego usá ese texto como URL de un bookmark en el browser:

```
javascript:(function(){if(!location.hostname.includes('bacolaborativa-backoffice')){alert('⚠️ Abrí este bookmark estando en BA Colaborativa, no en otra página.');return;}var s={origins:[{origin:location.origin,localStorage:Object.entries(localStorage).map(([k,v])=>({name:k,value:v})),sessionStorage:Object.entries(sessionStorage).map(([k,v])=>({name:k,value:v}))}]};fetch('https://lively-pond-17cd.julieta-carmona.workers.dev/renovar',{method:'POST',headers:{'Content-Type':'application/json','X-Refresh-Token':'TU_REFRESH_TOKEN_AQUI'},body:JSON.stringify(s)}).then(r=>r.json()).then(d=>alert(d.ok?'✅ Sesión renovada correctamente. El bot va a volver a funcionar en la próxima corrida.':'❌ Error al renovar: '+(d.error||'desconocido'))).catch(e=>alert('❌ Error de conexión: '+e));})();
```

### Cómo guardar el bookmark en Chrome / Edge
1. Copiá el texto de arriba (con tu REFRESH_TOKEN ya reemplazado)
2. Click derecho en la barra de favoritos → **Agregar página** (o presioná Ctrl+D)
3. Nombre: `🔑 Renovar sesión bot`
4. URL: pegá el texto del bookmarklet
5. Guardar

### Cómo usar el bookmarklet
1. Ir a BA Colaborativa y loguearse normalmente
2. Cuando cargue la pantalla principal → click en el bookmark `🔑 Renovar sesión bot`
3. Esperar el cartel de confirmación `✅ Sesión renovada`
4. En ~2 minutos el bot puede volver a loguearse

---

## Paso 4 — Crear el slash command `/renovar-sesion`

Igual que los otros: **Slash Commands** → **Create New Command**:
- Command: `/renovar-sesion`
- Request URL: `https://lively-pond-17cd.julieta-carmona.workers.dev`
- Description: `Instrucciones para renovar las cookies de login del bot`

---

## Paso 5 — Invitar al bot al canal

En el canal de Slack donde llegan las notificaciones:
```
/invite @BA Colaborativa Bot
```

---

## Flujo de escalada para la persona no técnica

```
Bot falla una vez
    → Escribí /bajada-tickets para reintentar

Sigue fallando varias corridas
    → Escribí /estado-bot para ver qué pasa
    → Si dice "captcha" o "sesión" → /renovar-sesion y seguí los pasos del bookmarklet
    → Si otro error → avisá a quien administra el bot
```

---

## Notas técnicas

- El Worker también expone `POST /renovar` (usado por el bookmarklet, no por Slack)
- El secret `REFRESH_TOKEN` protege ese endpoint — sin él, nadie puede enviar cookies al bot
- El bookmarklet captura `localStorage` y `sessionStorage` de BA Colaborativa
  (Keycloak guarda los JWT tokens ahí — son suficientes para autenticar al bot)
- El workflow `renovar-sesion.yml` recibe los datos y actualiza el secret `BA_SESSION_JSON` vía `gh secret set`
- Requiere que el GitHub PAT tenga scope `repo` (para actualizar secrets)
