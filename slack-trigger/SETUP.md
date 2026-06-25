# Setup: Comando /bajada-tickets en Slack

Permite que cualquier persona en el canal de Slack escriba `/bajada-tickets`
y el bot arranca. A los ~5 minutos llega la notificación de resultado.

---

## Paso 1 — Crear la Slack App

1. Ir a https://api.slack.com/apps → **Create New App** → **From scratch**
2. Nombre: `BA Colaborativa Bot`
3. Workspace: elegir el workspace del equipo

### Configurar el slash command (después del Paso 2)
En la app → **Slash Commands** → **Create New Command**:
- Command: `/bajada-tickets`
- Request URL: `https://ba-trigger.TU_SUBDOMINIO.workers.dev` ← vas a tener esta URL después del Paso 2
- Short description: `Dispara la bajada de tickets de BA Colaborativa`

### Instalar la app
**OAuth & Permissions** → **Install to Workspace** → Autorizar

### Copiar el Signing Secret
**Basic Information** → **Signing Secret** → copiar (lo necesitás en Paso 2)

---

## Paso 2 — Deploy del Worker en Cloudflare (gratis)

1. Crear cuenta en https://cloudflare.com (gratis)
2. Ir a **Workers & Pages** → **Create** → **Create Worker**
3. Nombrar el worker: `ba-trigger`
4. Clickear **Edit code** y pegar el contenido de `worker.js`
5. **Save and deploy**

### Configurar los secrets
En el Worker → **Settings** → **Variables** → **Add variable** (marcar como Secret):

| Variable | Valor |
|---|---|
| `SLACK_SIGNING_SECRET` | El signing secret del Paso 1 |
| `GITHUB_TOKEN` | PAT de GitHub con scope `workflow` (ver abajo) |

### Cómo crear el GitHub PAT
1. Ir a https://github.com/settings/tokens → **Generate new token (classic)**
2. Nombre: `slack-trigger-ba`
3. Scope: tildar solo `workflow`
4. Expiration: 1 year (o "No expiration")
5. Copiar el token (empieza con `ghp_...`)

---

## Paso 3 — Actualizar la Slack App con la URL del Worker

1. En Cloudflare, copiar la URL del worker (algo como `https://ba-trigger.TU.workers.dev`)
2. En la Slack App → **Slash Commands** → editar `/bajada-tickets`
3. Pegar la URL en **Request URL**
4. Guardar

---

## Paso 4 — Invitar al bot al canal

En el canal de Slack donde ya están las notificaciones:
```
/invite @BA Colaborativa Bot
```

---

## Uso

Cualquier persona en el canal escribe:
```
/bajada-tickets
```

El bot responde inmediatamente:
> ▶️ Bot BA Colaborativa arrancando! En ~5 minutos llega la notificación.

Y a los ~5 minutos llega la notificación de éxito o error al canal, igual que con el cron automático.
