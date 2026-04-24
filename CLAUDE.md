# tickets-automation

## Qué hace este proyecto
Automatiza la descarga diaria de tickets desde BA Colaborativa (plataforma del GCBA) y los sincroniza con un Google Sheets, agregando solo los tickets nuevos al final.

## Flujo completo
1. Login en BA Colaborativa con CUIL/CUIT + contraseña (Keycloak)
2. Contactos → Bandeja de entrada
3. Aplicar filtro: Estado general = Abierto → Buscar
4. Click en "Exportar" → modal "Columnas a exportar" → seleccionar "Todos los campos" → click "Exportar"
5. El reporte se genera de forma ASÍNCRONA (aparece mensaje azul "El reporte se está generando...") — hay que esperar y detectar cuándo está listo para descargar
6. Descargar el archivo (xlsx o csv)
7. Comparar contra Google Sheets tab "Tickets - General" por columna "Número"
8. Agregar al final SOLO los tickets cuyo número no esté ya en el Sheets
9. Las columnas exclusivas del Sheets (Respuesta AI, Respuesta de Producto, etc.) se dejan en blanco para los tickets nuevos

## URLs clave
- Login: https://identidad-gcaba.apps.buenosaires.gob.ar/realms/open-id/protocol/openid-connect/auth
- Backoffice: https://bacolaborativa-backoffice.buenosaires.gob.ar
- Bandeja: https://bacolaborativa-backoffice.buenosaires.gob.ar/contacto/bandeja

## Plataforma de login
- Sistema: Keycloak
- Campo usuario: "Usuario (CUIL/CUIT)"
- Sin 2FA
- Tiene reCAPTCHA pero generalmente no se activa

## Estructura del Google Sheets
- Tab principal: "Tickets - General"
- Fila 1 (header real): 115 columnas, empieza con col 0 = categoría, col 1 = "Número"
- Columna clave para deduplicar: "Número" (formato: "00352784/26")
- Columnas que vienen del CSV exportado (se mapean directo):
  Número, Prestación, Código de prestación, Tipo de prestación, Estado general,
  Esquema, Estado del esquema, Motivo, Ubicación, Comuna, Canal, Espacio público,
  Tipo de espacio público, Id de espacio público, Ciudadano, Tipo documento,
  Número documento, Email de contacto, Observación, Cuestionario respondido,
  Fecha de inicio, Fecha de última modificación, Temática,
  Organismos responsables del estado, Organismos responsables de la prestación,
  Año de inicio, # reiteraciones, # ciudadanos que reiteraron, Última reiteración,
  Derivado, Usuario, Historial de cambios, Contiene archivos adjuntos,
  Historial de derivaciones, Telefono, Celular, Usuario asignado, Etiqueta,
  Prioridad, Educación - Cue, Educación - Claverama, Educación - Email,
  Educación - Establecimiento, Educación - Comuna, Educación - Cargo,
  Educación - Cui, Educación - Cuc, Educación - Modalidad, Educación - Distrito
- Columna extra del Sheets (col 0, antes de "Número"): categoría derivada del
  último segmento de "Prestación" (ej: "DJ cursos y docentes")
- Columnas SOLO del Sheets que quedan en blanco para tickets nuevos:
  Revision, ✨ Respuesta Sugerida AI ✨, Respuesta de Producto,
  Respuesta cerrada producto, Automatización (No tocar), Respuesta final,
  Fecha de respuesta, Tiempo Respuesta, Respondido

## Colores de marca del Sheets (para mantener si se formatea)
- Navy: #2A205E
- Turquesa: #00C4B4

## Credenciales (vienen de GitHub Secrets / variables de entorno)
- BA_USER: CUIL/CUIT del usuario
- BA_PASSWORD: contraseña de BA Colaborativa
- GOOGLE_CREDENTIALS: JSON de service account de Google (base64 o directo)
- SPREADSHEET_ID: ID del Google Sheets

## Infraestructura
- Corre en GitHub Actions
- Schedule: todos los días a las 8am hora Argentina (UTC-3) = 11:00 UTC
- OS: ubuntu-latest
- Browser: Playwright con Chromium headless

## Stack técnico
- Python 3.11+
- playwright (scraping)
- gspread + google-auth (Google Sheets API)
- pandas (procesamiento del CSV)
- openpyxl (si el export es xlsx)

## Archivos del proyecto
- scraper.py — login + navegación + descarga del reporte
- update_sheets.py — comparación y escritura en Google Sheets
- main.py — orquesta scraper + update_sheets
- requirements.txt
- .github/workflows/daily.yml — cron job
- README.md — instrucciones para configurar secrets y Google API

## Notas importantes
- El export es ASÍNCRONO: después de hacer click en Exportar aparece un banner azul
  "El reporte se está generando y estará disponible para su descarga en cuanto esté listo"
  Hay que detectar cuándo está disponible (probablemente por notificación o sección de descargas)
  → PENDIENTE CONFIRMAR: dónde aparece el link de descarga una vez generado
- El archivo exportado es un xlsx con múltiples tabs en algunos casos
- La lógica de merge ya fue desarrollada y testeada (ver update_sheets.py de contexto anterior)
