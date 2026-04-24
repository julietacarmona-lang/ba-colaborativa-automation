# tickets-automation

Pipeline que descarga diariamente los tickets abiertos de **BA Colaborativa**
(GCBA) y los appendea a un Google Sheets (tab `Tickets - General`), deduplicando
por número.

## Arquitectura

```
┌──────────────┐   ┌──────────────┐   ┌─────────────────┐
│  scraper.py  │──▶│ xlsx/csv en  │──▶│ update_sheets.py│
│ (Playwright) │   │  ./downloads │   │   (gspread)     │
└──────────────┘   └──────────────┘   └─────────────────┘
        ▲                                      │
        │                                      ▼
┌──────────────┐                       ┌─────────────────┐
│ main.py      │                       │ Google Sheets   │
│ (orquesta)   │                       │ "Tickets - ..." │
└──────────────┘                       └─────────────────┘
```

## Setup local (macOS)

### 1. Dependencias

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

### 2. Variables de entorno

```bash
cp .env.example .env
# editá .env con tus valores
```

Variables que vas a necesitar:

| Variable | Qué es |
|---|---|
| `BA_USER` | CUIL/CUIT completo, 11 dígitos, sin guiones |
| `BA_PASSWORD` | contraseña de BA Colaborativa |
| `SPREADSHEET_ID` | el ID del Sheets (lo sacás de la URL: `https://docs.google.com/spreadsheets/d/<ID>/edit`) |
| `GOOGLE_CREDENTIALS_FILE` | path al JSON del service account (ver abajo) |

### 3. Service account de Google

1. Andá a [Google Cloud Console](https://console.cloud.google.com/) → creá un proyecto (o usá uno existente).
2. **APIs & Services → Enable APIs**: habilitá **Google Sheets API** y **Google Drive API**.
3. **IAM → Service Accounts → Create service account**. Poné cualquier nombre, no hace falta darle roles.
4. Creá una **Key** de tipo **JSON** y descargala. Guardala como `credentials.json` en la raíz del proyecto (está en `.gitignore`, no se sube al repo).
5. **Compartí tu Google Sheets** con el mail del service account (mirá dentro del JSON, campo `client_email`). Darle permiso de **Editor**.

### 4. Correr local

Este proyecto tiene **dos modos de ejecución**, elegidos por la variable
`BROWSER_MODE`:

- **`cdp` (recomendado localmente)**: el scraper se conecta por Chrome
  DevTools Protocol a un Chrome real que vos lanzás aparte. Así evitamos la
  detección de automation y el captcha. Por defecto está en este modo.

- **`persistent` (modo CI)**: Playwright lanza su propio Chromium. Es el que
  usa GitHub Actions. Local puede tener problemas (ver notas abajo).

#### Modo `cdp` (paso a paso)

1. Abrí una terminal y lanzá el Chrome dedicado:
   ```bash
   ./scripts/chrome-cdp.sh
   ```
   Esto abre una ventana de Chrome separada de tu Chrome normal, en
   `--remote-debugging-port=9222` y con perfil en `~/.chrome-bacolaborativa`.

2. En esa ventana, andá a BA Colaborativa, logueate y navegá a
   **Contactos → Bandeja de entrada**. La sesión queda guardada en ese
   perfil para próximas corridas (no vas a tener que loguearte de nuevo
   hasta que expire Keycloak, en general duran horas/días).

3. En otra terminal, corré el pipeline:
   ```bash
   .venv/bin/python main.py
   ```
   El scraper se conecta al Chrome existente, detecta que ya estás en la
   bandeja, clickea Buscar, Exportar, "Todos los campos", confirma. Espera
   a que el reporte async se descargue y después mergea al Sheets.

## Correr en GitHub Actions

El workflow está en `.github/workflows/daily.yml`. Corre todos los días a las
**11:00 UTC (8:00 Argentina)**.

**El workflow no hace login** — usa cookies pre-autenticadas que extraés vos
una vez y subís como secret. Cuando expiran, hay que refrescarlas (ver
"Runbook: refrescar cookies" más abajo).

### Secrets que hay que configurar

En el repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Contenido | Cómo conseguirlo |
|---|---|---|
| `BA_SESSION_JSON` | cookies de la sesión activa | corriendo `scripts/dump_cookies.py` (ver runbook) |
| `BA_USER` | CUIL/CUIT (fallback) | el de la cuenta operativa |
| `BA_PASSWORD` | contraseña (fallback) | la de la cuenta operativa |
| `SPREADSHEET_ID` | id del Sheets | de la URL del Google Sheets |
| `GOOGLE_CREDENTIALS_JSON` | JSON del service account de Google | abrí `credentials.json` y copiá todo |
| `RESEND_API_KEY` | (opcional) API key de [Resend](https://resend.com) | si querés mail de alerta cuando falla |
| `FROM_EMAIL` | (opcional) dirección "from" verificada en Resend | |

### Variables (no son secretos, opcionales)

En **Settings → Secrets and variables → Actions → Variables tab**:

| Variable | Default | Para qué |
|---|---|---|
| `SHEET_TAB` | `Tickets - General` | nombre del tab destino |
| `SAVED_FILTER_NAME` | `Asignados a Milton` | filtro guardado a cargar |

### Ejecutar a mano

En la pestaña **Actions** del repo, elegí el workflow y clickeá **Run workflow**.

## Runbook: refrescar cookies cuando expiren

Las cookies de Keycloak expiran cada cierto tiempo (típicamente entre algunos
días y unas semanas). Cuando el workflow falle con error de "session expired"
o no logre llegar a la bandeja, **cualquier persona del equipo** con acceso
al repo puede ejecutar este proceso desde su máquina:

1. **Abrir el Chrome dedicado**:
   ```bash
   ./scripts/chrome-cdp.sh
   ```

2. **Loguearse en BA Colaborativa** en esa ventana de Chrome:
   - Andar a https://bacolaborativa-backoffice.buenosaires.gob.ar/contacto/bandeja
   - Completar CUIL + contraseña + captcha
   - Confirmar que llegó al backoffice (no importa qué pantalla muestre)

3. **Extraer las cookies**:
   ```bash
   .venv/bin/python scripts/dump_cookies.py
   ```
   Esto crea un archivo `session.json` en la raíz.

4. **Subir las cookies como secret**:
   - GitHub → repo → Settings → Secrets and variables → Actions
   - Editar (o crear) el secret `BA_SESSION_JSON`
   - **Pegar el contenido completo** de `session.json`
   - Save

5. **Borrar el archivo local** (contiene cookies sensibles):
   ```bash
   rm session.json
   ```

6. **Verificar**: en la pestaña Actions del repo, disparar a mano el workflow
   "Bajada diaria…" y mirar que termine OK.

> ⚠️ El archivo `session.json` está en `.gitignore` — no se sube al repo.
> Es solo un puente entre tu Chrome local y el secret de GitHub.

## Troubleshooting

### "El reporte se está generando…" pero no termina

Puede ser que el backend de BA Colaborativa esté caído temporalmente —
es una web gubernamental y pasa seguido. El scraper espera hasta 10 minutos.
Si ves el mensaje "En este momento la web está fuera de servicio por problemas
técnicos", no hay nada que hacer desde nuestro lado, esperá un rato y reintentá.

### Captcha en modo `persistent`

Si corrés local en `BROWSER_MODE=persistent` y salta el reCAPTCHA, cambiá a
`BROWSER_MODE=cdp` (ver sección de más arriba). En GitHub Actions esto es
un problema abierto — la estrategia de corto plazo es capturar cookies de
Keycloak después de un login manual y pasarlas al workflow.

### Debug por DOM

Si el scraper no encuentra un elemento, guarda el HTML de la pantalla en
`./downloads/debug_*.html`. Abrílo en el navegador para ver qué está viendo.

### Mail de alerta

Si el scraper falla después de los reintentos, manda mail a
`julieta.carmona@educabot.com`. Se elige backend automático:

- Si hay `RESEND_API_KEY` → manda por [Resend](https://resend.com) (recomendado).
- Si hay `SMTP_HOST/USER/PASSWORD` → manda por SMTP (p.ej. Gmail con app password).
- Si no hay ninguno → solo imprime la alerta en stdout.

## Estructura del Google Sheets

El tab `Tickets - General` tiene 115 columnas. La fila 1 es el header.

- **Col 0**: "categoría" — derivada del último segmento de `Prestación` (se
  llena automáticamente para tickets nuevos).
- **Col 1**: `Número` — clave única para deduplicar.
- **Cols siguientes**: se mapean 1:1 desde el export.
- **Columnas "solo Sheets"** (`Revisión`, `✨ Respuesta Sugerida AI ✨`,
  `Respuesta de Producto`, etc.) quedan en blanco para los tickets nuevos;
  las completás vos/tu equipo a mano en el Sheets.

## Estado del proyecto

- ✅ Scraper end-to-end funcionando en modo CDP.
- ✅ Merge contra Google Sheets listo (`update_sheets.py`).
- ✅ Orquestador (`main.py`) y workflow base.
- ⚠️  Login automatizado en GitHub Actions está **pendiente** — en CI no hay
  humana para resolver captcha. Hay tres caminos posibles (de simple a
  ambicioso): (a) reusar cookies capturadas con login manual, (b) ver si
  BA Colaborativa tiene API directa, (c) pedir service account al GCBA.
