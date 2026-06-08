"""Scraper de BA Colaborativa — descarga el export de la Bandeja de entrada.

Flujo:
  1. Login Keycloak con CUIL/CUIT + password.
  2. Ir a Contactos → Bandeja.
  3. Filtro Estado general = Abierto → Buscar.
  4. Exportar → modal → "Todos los campos" → Exportar.
  5. Esperar a que el reporte async se genere y descargar el archivo.

Uso:
  python scraper.py                 # corre el flujo completo, modo headful por default
  HEADLESS=true python scraper.py   # headless (como corre en CI)
  DEBUG_PAUSE=1 python scraper.py   # pausa con Playwright Inspector después del Exportar
                                    # para identificar dónde aparece el link de descarga
  KEEP_OPEN=1 python scraper.py     # no cierra el browser al terminar
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from playwright.sync_api import (
    Download,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

import notify

load_dotenv()

BA_USER = os.environ.get("BA_USER", "").strip()
BA_PASSWORD = os.environ.get("BA_PASSWORD", "")
HEADLESS = os.environ.get("HEADLESS", "false").strip().lower() in ("1", "true", "yes")
SLOW_MO = int(os.environ.get("SLOW_MO", "150"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "./downloads")).resolve()
KEEP_OPEN = os.environ.get("KEEP_OPEN", "0") == "1"
DEBUG_PAUSE = os.environ.get("DEBUG_PAUSE", "0") == "1"
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
# Nombre del filtro guardado a cargar antes de exportar. Si se define, el
# script lo carga desde el dropdown "Filtros guardados" en vez de configurar
# los filtros uno por uno.
SAVED_FILTER_NAME = os.environ.get("SAVED_FILTER_NAME", "Asignados a Milton")
# JSON con cookies pre-cargadas (saltea login). Se usa en CI con
# BROWSER_MODE=persistent: el workflow inyecta cookies extraídas con
# scripts/dump_cookies.py para evitar el form de login.
BA_SESSION_JSON = os.environ.get("BA_SESSION_JSON", "").strip()
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "./.playwright-user-data")).resolve()
CAPTCHA_MANUAL_TIMEOUT_MS = int(os.environ.get("CAPTCHA_MANUAL_TIMEOUT_MS", "300000"))
LOGIN_MODE = os.environ.get("LOGIN_MODE", "manual").strip().lower()
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome").strip().lower()
# Modo de browser:
#   "cdp"        — conectarse a un Chrome que ya está corriendo en
#                  --remote-debugging-port=CDP_PORT. Evita toda detección
#                  de automation y usa la sesión real del usuario.
#   "persistent" — Playwright lanza su propio Chrome con user_data_dir.
BROWSER_MODE = os.environ.get("BROWSER_MODE", "cdp").strip().lower()
CDP_URL = os.environ.get("CDP_URL", "http://localhost:9222")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

BACKOFFICE_URL = "https://bacolaborativa-backoffice.buenosaires.gob.ar"
BANDEJA_URL = f"{BACKOFFICE_URL}/contacto/bandeja"

# Tiempo máx de espera para que termine de generarse el reporte asíncrono.
REPORT_WAIT_TIMEOUT_MS = 10 * 60 * 1000  # 10 minutos
# Intervalo de polling mientras se espera el reporte.
REPORT_POLL_INTERVAL_MS = 5000


def log(msg: str) -> None:
    print(f"[scraper] {msg}", flush=True)


class CaptchaRejectedError(Exception):
    """Keycloak rechazó el token del solver con "Error en el reCAPTCHA".
    Indica que el score del solver no le alcanza al GCBA en este momento —
    reintentar más veces solo gasta créditos y tiempo. download_tickets()
    aborta después de 2 ocurrencias consecutivas."""


def is_logged_in(page: Page) -> bool:
    """Heurística robusta: estamos logueadas si y solo si la URL es del
    backoffice Y no se ve un input de password (que sería la pantalla de
    Keycloak incluso si la URL ya muestra el backoffice por una fracción de
    segundo antes del redirect OIDC)."""
    try:
        url = page.url
    except Exception:
        return False
    if "identidad-gcaba" in url:
        return False
    if "bacolaborativa-backoffice.buenosaires.gob.ar" not in url:
        return False
    try:
        # Si hay un password visible, estamos en el form de login.
        if page.locator('input[type="password"]').first.is_visible(timeout=300):
            return False
    except Exception:
        pass
    return True


def _detect_auth_state(page: Page, timeout_s: float = 30.0) -> str:
    """Mira el DOM y devuelve uno de:
       - 'login'       — hay un input type=password o textos de form de login
       - 'backoffice'  — hay nav/header del backoffice visible (Contactos/Ciudadanos)
       - 'unknown'     — no detectó estado claro

    Requiere evidencia POSITIVA del estado (no se basa solo en URL, que puede
    ser engañosa durante redirects OIDC).
    """
    pw = page.locator('input[type="password"]')
    login_text = page.get_by_text(re.compile(r"Iniciar sesi[oó]n|Usuario \(CUIL", re.I))
    backoffice_nav = page.get_by_text(re.compile(r"^\s*(Contactos|Ciudadanos)\s*$", re.I))
    deadline = time.time() + timeout_s
    last_log = 0.0
    while time.time() < deadline:
        if time.time() - last_log > 5:
            try:
                log(f"  … esperando (url={page.url[:80]}  title={page.title()!r})")
            except Exception:
                pass
            last_log = time.time()

        # Login: hay password input o título "Iniciar sesión".
        try:
            if pw.first.is_visible(timeout=200):
                return "login"
        except Exception:
            pass
        try:
            if login_text.first.is_visible(timeout=200):
                return "login"
        except Exception:
            pass

        # Backoffice: está renderizado el nav del backoffice.
        try:
            if backoffice_nav.first.is_visible(timeout=200):
                return "backoffice"
        except Exception:
            pass

        page.wait_for_timeout(400)

    _dump_debug(page, "auth_unknown")
    return "unknown"


def _detect_bandeja(page: Page, timeout_s: float = 30.0) -> bool:
    """Una vez logueada, espera a que aparezcan controles típicos de la bandeja
    (botón Exportar o Buscar). Solo considera 'bandeja' si la URL es del
    backoffice — evita falsos positivos en chrome://new-tab-page (que tiene
    'Buscar en Google') u otras pantallas."""
    bandeja_btn = page.get_by_role(
        "button", name=re.compile(r"exportar|buscar", re.I)
    )
    deadline = time.time() + timeout_s
    last_log = 0.0
    while time.time() < deadline:
        if time.time() - last_log > 5:
            try:
                log(f"  … buscando bandeja (url={page.url[:80]})")
            except Exception:
                pass
            last_log = time.time()
        try:
            url = page.url
            if "bacolaborativa-backoffice" in url and "identidad-gcaba" not in url:
                if bandeja_btn.first.is_visible(timeout=300):
                    return True
        except Exception:
            pass
        page.wait_for_timeout(400)
    _dump_debug(page, "bandeja_not_found")
    return False


def _dump_debug(page: Page, tag: str) -> None:
    """Guarda HTML del estado actual para debuguear a posteriori.
    Captura tanto el HTML estático como el DOM renderizado por JS, y
    estado clave de Angular si está hidratado."""
    try:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        html_path = DOWNLOAD_DIR / f"debug_{tag}_{ts}.html"
        html_path.write_text(page.content(), encoding="utf-8")
        log(f"📄 html: {html_path}")

        # DOM renderizado (después de JS) para ver qué está pasando con Angular.
        try:
            rendered = page.evaluate("""() => ({
                ngVersion: document.querySelector('[ng-version]')?.getAttribute('ng-version'),
                appRootChildren: document.querySelector('app-root')?.children.length || 0,
                bodyTextLen: document.body?.innerText?.length || 0,
                bodyTextPreview: document.body?.innerText?.substring(0, 300) || '',
                docTitle: document.title,
                visibleButtons: Array.from(document.querySelectorAll('button, a')).filter(e => e.offsetParent !== null).map(e => e.textContent.trim()).filter(Boolean).slice(0, 20),
            })""")
            log(f"🔬 render: {rendered}")
        except Exception as e:
            log(f"(no pude evaluar DOM renderizado: {e})")
    except Exception as e:
        log(f"(no pude capturar debug: {e})")


def login(page: Page) -> None:
    # IMPORTANTE: vamos a la RAÍZ del backoffice, no directo a /contacto/bandeja.
    # El redirect_uri del OIDC apunta a /, así que el SPA hace su bootstrap de
    # OAuth ahí. Ir directo a /contacto/bandeja saltea ese bootstrap y la app
    # queda colgada (visto en CI: backend devuelve 401, Angular nunca arranca).
    log("Abriendo backoffice (raíz, para que arranque OAuth bien)…")
    try:
        page.goto(BACKOFFICE_URL + "/", wait_until="commit")
    except Exception as e:
        if "interrupted" not in str(e).lower():
            raise

    # Esperamos a que Angular esté HIDRATADO (no solo a que cargue el HTML).
    log("Esperando que Angular arranque…")
    try:
        page.wait_for_function(
            """() => {
                const r = document.querySelector('app-root');
                return r && r.children.length > 0;
            }""",
            timeout=45000,
        )
        log("✓ Angular hidratado.")
    except PlaywrightTimeoutError:
        log("⚠️  Angular no arrancó en 45s — sigo igual a ver qué dice el DOM.")

    log("Detectando estado de autenticación…")
    state = _detect_auth_state(page, timeout_s=30.0)

    if state == "backoffice":
        log("✓ Ya estoy logueada en el backoffice.")
        return
    if state == "unknown":
        raise RuntimeError(
            "No pude determinar si hay que loguearse o no. "
            "Revisá el screenshot en ./downloads/"
        )
    # state == "login" → hay que loguearse
    log("Form de login detectado.")

    if LOGIN_MODE == "manual":
        log(
            "LOGIN_MODE=manual — completá CUIL, contraseña, captcha e Ingresar a mano. "
            f"Espero hasta {CAPTCHA_MANUAL_TIMEOUT_MS // 1000}s."
        )
        state = _detect_auth_state(page, timeout_s=CAPTCHA_MANUAL_TIMEOUT_MS / 1000)
        if state == "backoffice":
            log("✓ Login manual OK — sesión guardada para próximas corridas.")
            return
        raise RuntimeError(
            f"Después del login manual no caí en el backoffice (estado={state}). "
            "Probá de nuevo."
        )

    # Keycloak muestra un form con "Usuario (CUIL/CUIT)" y "Contraseña".
    log("Esperando form de login…")
    user_input = _first_visible(
        page,
        [
            lambda: page.get_by_label(re.compile(r"CUIL.?CUIT|Usuario", re.I)),
            lambda: page.locator("#username"),
            lambda: page.locator('input[name="username"]'),
        ],
        timeout_ms=30000,
    )

    # Esperamos un poco para que el JS de reCAPTCHA Enterprise tenga tiempo
    # de generar su token natural. Sin esta pausa el score baja porque
    # llenamos todo en milisegundos.
    page.wait_for_timeout(2500)

    # Tipeo "humano" con delay entre teclas — fill() instantáneo da score bajo.
    log("Tipeando CUIL…")
    user_input.click()
    user_input.type(BA_USER, delay=80)

    page.wait_for_timeout(800)

    pw_input = _first_visible(
        page,
        [
            lambda: page.get_by_label(re.compile(r"contraseña|password", re.I)),
            lambda: page.locator("#password"),
            lambda: page.locator('input[name="password"]'),
        ],
    )
    log("Tipeando contraseña…")
    pw_input.click()
    pw_input.type(BA_PASSWORD, delay=80)

    # Pausa antes de clickear — humano lee/duda antes de submit.
    page.wait_for_timeout(1500)

    # NO llamamos a anti-captcha antes del primer submit: la página ya tiene
    # un token generado por el JS de Google al cargar. Submitamos con ese
    # primero. Si Keycloak lo rechaza, el bloque de retry abajo usa anti-captcha.

    log("Enviando credenciales…")
    submit = _first_visible(
        page,
        [
            lambda: page.get_by_role("button", name=re.compile(r"ingresar|iniciar|acceder|continuar|entrar", re.I)),
            lambda: page.locator('input[type="submit"]'),
            lambda: page.locator('button[type="submit"]'),
        ],
    )
    # Click normal (NO force) — un click sintético con force gatilla
    # detección de automation en Google reCAPTCHA y baja el score.
    # Si falla por el badge invisible del reCAPTCHA, ahí caemos al fallback.
    try:
        submit.click()
    except Exception as e:
        log(f"⚠️  click normal falló ({e}), reintento con force=True")
        submit.click(force=True)

    # Esperamos largo para que el captcha INVISIBLE termine naturalmente.
    # Desde IP residencial, Google suele dar score alto → redirige sin pedir
    # nada. Pero la cadena Angular → grecaptcha.enterprise.execute() →
    # validación server-side puede tardar 20-40s. Si entramos a CapSolver
    # antes de tiempo, su token de score bajo termina rechazado por Keycloak.
    try:
        page.wait_for_url(
            re.compile(r"bacolaborativa-backoffice\.buenosaires\.gob\.ar"),
            timeout=45000,
        )
    except PlaywrightTimeoutError:
        # Solo entramos al solver si DE VERDAD hay un challenge visible o
        # Keycloak ya nos mostró un error — no por el badge invisible que
        # siempre está ahí.
        if _real_captcha_challenge(page):
            # HARD RELOAD retry: Juli observó que "Error en el reCAPTCHA"
            # muchas veces es el token invisible que expiró por demorar
            # mucho en submitir, NO un challenge real. La solución que ella
            # hace a mano es refrescar la página → form fresco → token
            # invisible nuevo → login pasa sin captcha. Replicamos eso
            # antes de gastar créditos del solver. Re-tipear sobre el mismo
            # form (lo que hacíamos antes) heredaba el token vencido.
            log("⚠️  Error en el reCAPTCHA / no redirect. Hard reload + login fresh…")
            hard_retry_ok = False
            try:
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(3000)  # dejar que reCAPTCHA Enterprise genere su token natural
                user_input2 = _first_visible(
                    page,
                    [
                        lambda: page.get_by_label(re.compile(r"CUIL.?CUIT|Usuario", re.I)),
                        lambda: page.locator("#username"),
                        lambda: page.locator('input[name="username"]'),
                    ],
                    timeout_ms=15000,
                )
                user_input2.click()
                user_input2.type(BA_USER, delay=80)
                page.wait_for_timeout(800)
                pw_input2 = _first_visible(
                    page,
                    [
                        lambda: page.get_by_label(re.compile(r"contraseña|password", re.I)),
                        lambda: page.locator("#password"),
                        lambda: page.locator('input[name="password"]'),
                    ],
                )
                pw_input2.click()
                pw_input2.type(BA_PASSWORD, delay=80)
                page.wait_for_timeout(1500)
                submit2 = _first_visible(
                    page,
                    [
                        lambda: page.get_by_role("button", name=re.compile(r"ingresar|iniciar|acceder|continuar|entrar", re.I)),
                        lambda: page.locator('input[type="submit"]'),
                        lambda: page.locator('button[type="submit"]'),
                    ],
                )
                submit2.click()
                page.wait_for_url(
                    re.compile(r"bacolaborativa-backoffice\.buenosaires\.gob\.ar"),
                    timeout=45000,
                )
                hard_retry_ok = True
                log("✓ Hard reload retry funcionó — login OK sin llamar al solver.")
            except PlaywrightTimeoutError:
                log("Hard reload retry no alcanzó — challenge persistente. Llamando solver…")
            except Exception as e:
                log(f"Hard reload retry tiró excepción ({e}). Llamando solver…")

            if hard_retry_ok:
                log("Login OK.")
                return

            _solve_captcha(page)
            # Diagnóstico: capturamos qué responde Keycloak después del submit
            # para entender por qué falla (mensaje de error visible).
            page.wait_for_timeout(5000)
            error_text = ""
            try:
                error_text = page.evaluate("""
                    () => {
                        const errs = document.querySelectorAll('.kc-feedback-text, .alert, .error, [class*="error"], [class*="Error"]');
                        return Array.from(errs).map(e => e.textContent.trim()).filter(Boolean).join(' | ').substring(0, 500);
                    }
                """) or ""
                if error_text:
                    log(f"📋 Mensaje de Keycloak post-submit: {error_text}")
                log(f"📋 URL post-submit: {page.url[:120]}")
            except Exception:
                pass
            _dump_debug(page, "after_captcha_submit")
            # Si Keycloak rechazó el token del solver explícitamente, no tiene
            # sentido seguir esperando 120s ni reintentar 10 veces — el score
            # de CapSolver simplemente no le alcanza al GCBA. Disparamos una
            # excepción específica para que download_tickets aborte rápido.
            if re.search(r"Error en el reCAPTCHA", error_text, re.I):
                raise CaptchaRejectedError(
                    "Keycloak rechazó el token del solver (score insuficiente)."
                )
            page.wait_for_url(
                re.compile(r"bacolaborativa-backoffice\.buenosaires\.gob\.ar"),
                timeout=120000,
            )
        else:
            # Reintentamos esperar — puede estar yendo lento.
            page.wait_for_url(
                re.compile(r"bacolaborativa-backoffice\.buenosaires\.gob\.ar"),
                timeout=45000,
            )
    log("Login OK.")


def _captcha_present(page: Page) -> bool:
    """Detecta reCAPTCHA (iframe) o hCaptcha visible en la página actual.
    OJO: detecta también el BADGE invisible que siempre está en la página,
    así que da falsos positivos. Para chequear si hay challenge REAL,
    usar _real_captcha_challenge."""
    selectors = [
        'iframe[src*="recaptcha"]',
        'iframe[title*="reCAPTCHA" i]',
        'iframe[src*="hcaptcha"]',
        '.g-recaptcha',
        '#captcha',
    ]
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=1000):
                return True
        except Exception:
            continue
    # Texto explícito de error de captcha.
    try:
        if page.get_by_text(re.compile(r"captcha", re.I)).first.is_visible(timeout=500):
            return True
    except Exception:
        pass
    return False


def _real_captcha_challenge(page: Page) -> bool:
    """Distingue un challenge real (el modal "click en los semáforos" o el
    error post-submit "Error en el reCAPTCHA") del badge invisible que el
    reCAPTCHA siempre planta en la página. Solo en estos casos debemos
    llamar al solver — si no, conviene seguir esperando el redirect natural.

    Casos que cuentan como "real":
      - Texto "Error en el reCAPTCHA" visible (Keycloak ya rechazó algo)
      - iframe del challenge modal visible (el de "elegí imágenes…")
      - URL sigue en Keycloak sin signs de redirect
    """
    # 1) Mensaje de error de Keycloak — claro signo de rechazo.
    try:
        if page.get_by_text(re.compile(r"Error en el reCAPTCHA", re.I)).first.is_visible(timeout=500):
            return True
    except Exception:
        pass

    # 2) Modal de challenge visible (el iframe "bframe" del reCAPTCHA challenge,
    # NO el "anchor" que es solo el badge invisible).
    try:
        if page.locator('iframe[src*="recaptcha/api2/bframe"], iframe[src*="recaptcha/enterprise/bframe"]').first.is_visible(timeout=500):
            return True
    except Exception:
        pass

    # 3) Si seguimos en Keycloak y NO hay redirect en curso, asumimos que
    # algo se trabó.
    try:
        if "identidad-gcaba" in page.url:
            # Esperá un toque más por las dudas
            page.wait_for_timeout(500)
            if "identidad-gcaba" in page.url:
                return True
    except Exception:
        pass

    return False


def _solve_captcha(page: Page) -> None:
    """Resuelve el captcha. Prueba CapSolver primero (mejor con Enterprise V3),
    cae a anti-captcha si CapSolver falla o no está configurado, y al modo
    manual si no hay ninguna API key."""
    import importlib

    # Orden importa: CapSolver primero porque sus tokens pasan el threshold
    # del GCBA y los de anti-captcha vienen siendo rechazados.
    providers = []
    if os.environ.get("CAPSOLVER_API_KEY", "").strip():
        providers.append(("CapSolver", "CAPSOLVER_API_KEY", "capsolver"))
    if os.environ.get("ANTICAPTCHA_API_KEY", "").strip():
        providers.append(("Anti-captcha", "ANTICAPTCHA_API_KEY", "anti_captcha"))

    if not providers:
        _wait_for_manual_captcha_solve(page)
        return

    site_key = page.evaluate("""
        () => {
            const el = document.querySelector('.g-recaptcha[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            const iframe = document.querySelector('iframe[src*="recaptcha"]');
            if (iframe) {
                const m = iframe.src.match(/[?&]k=([^&]+)/);
                if (m) return m[1];
            }
            try { return localStorage.getItem('captchaSiteKey'); } catch (e) { return null; }
        }
    """)
    if not site_key:
        raise RuntimeError("No pude detectar el sitekey del reCAPTCHA en la página.")
    site_url = page.url

    last_err: Optional[BaseException] = None
    for label, env_var, module_name in providers:
        try:
            api_key = os.environ[env_var].strip()
            module = importlib.import_module(module_name)
            log(f"🤖 {label}: enviando reCAPTCHA (sitekey {site_key[:12]}…) desde {site_url[:60]}…")
            token = module.solve_recaptcha_v2(api_key, site_url, site_key, log=log)
            _inject_token_and_submit(page, token)
            return
        except Exception as e:
            log(f"⚠️  {label} falló: {e!r}. Probando siguiente proveedor…")
            last_err = e
            continue

    assert last_err is not None
    raise last_err


def _inject_token_and_submit(page: Page, token: str) -> None:
    """Inyecta el token reCAPTCHA en todos los campos posibles y somete el
    form via JS. Keycloak GCBA usa Enterprise V3 con campo 'g-recaptcha-token'
    (no el 'g-recaptcha-response' estándar de v2), así que inyectamos en ambos."""
    log("Inyectando token y sometiendo el form via JS…")
    result = page.evaluate("""
        (token) => {
            const fields = [
                ...document.querySelectorAll('input[name="g-recaptcha-token"]'),
                ...document.querySelectorAll('input[id="g-recaptcha-token"]'),
                ...document.querySelectorAll('textarea[name="g-recaptcha-response"]'),
                ...document.querySelectorAll('textarea[id^="g-recaptcha-response"]'),
            ];
            fields.forEach(f => { f.value = token; });

            const form = document.querySelector('form#kc-form-login')
                      || document.querySelector('form[action*="login"]')
                      || document.querySelector('form');
            if (form) {
                HTMLFormElement.prototype.submit.call(form);
                return 'submitted (' + fields.length + ' fields injected)';
            }
            return 'no form found';
        }
    """, token)
    log(f"✓ {result}")


def _wait_for_manual_captcha_solve(page: Page) -> None:
    """En modo headful: pedimos al usuario que resuelva el captcha a mano y
    esperamos hasta que la página navegue fuera de identidad-gcaba."""
    if HEADLESS:
        raise RuntimeError(
            "Apareció un captcha y el scraper corre headless. "
            "Corré primero en modo headful (HEADLESS=false) para guardar la sesión."
        )
    log(
        "⚠️  CAPTCHA detectado. Resolvelo a mano en la ventana de Chromium "
        "y apretá 'Ingresar'. Espero hasta "
        f"{CAPTCHA_MANUAL_TIMEOUT_MS // 1000}s."
    )
    # Esperamos a que la URL salga de la pantalla de identidad.
    try:
        page.wait_for_url(
            lambda url: "bacolaborativa-backoffice.buenosaires.gob.ar" in url,
            timeout=CAPTCHA_MANUAL_TIMEOUT_MS,
        )
        log("✓ Captcha resuelto, seguimos.")
    except PlaywrightTimeoutError:
        raise RuntimeError(
            "Se acabó el tiempo esperando que resuelvas el captcha a mano."
        )


def go_to_bandeja(page: Page) -> None:
    """El SPA de BA Colaborativa rebota la URL directa al home y exige
    navegación por menú (setea estado interno). Clickeamos 'Contactos' en el
    navbar, y después 'Bandeja de entrada' en el dropdown."""
    if _detect_bandeja(page, timeout_s=2.0):
        log("Ya estoy en la bandeja.")
        return

    log("Abriendo dropdown 'Contactos'…")
    contactos = _first_visible(
        page,
        [
            lambda: page.get_by_role("button", name=re.compile(r"^\s*contactos\s*$", re.I)),
            lambda: page.get_by_role("link", name=re.compile(r"^\s*contactos\s*$", re.I)),
            lambda: page.locator("nav, header").get_by_text(
                re.compile(r"^\s*contactos\s*$", re.I)
            ),
            lambda: page.get_by_text(re.compile(r"^\s*contactos\s*$", re.I)),
        ],
        timeout_ms=15000,
    )
    contactos.click(force=True)

    log("Clickeando 'Bandeja de entrada'…")
    bandeja_item = _first_visible(
        page,
        [
            lambda: page.get_by_role("menuitem", name=re.compile(r"bandeja", re.I)),
            lambda: page.get_by_role("link", name=re.compile(r"bandeja", re.I)),
            lambda: page.locator('a, li, button').filter(
                has_text=re.compile(r"bandeja", re.I)
            ),
        ],
        timeout_ms=5000,
    )
    bandeja_item.click(force=True)

    if not _detect_bandeja(page, timeout_s=30.0):
        raise RuntimeError(
            "Clickeé Contactos→Bandeja pero no aparecieron Exportar/Buscar. "
            "Revisá el HTML en ./downloads/"
        )
    log("✓ Bandeja cargada vía menú.")


def apply_filter_abierto_and_search(page: Page) -> None:
    """Carga el filtro guardado SAVED_FILTER_NAME (por default 'Asignados a
    Milton', que ya trae Estado=Abierto + Usuario asignado=Milton). Después
    clickea Buscar."""
    buscar_loc = page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)).first

    def buscar_visible() -> bool:
        try:
            return buscar_loc.is_visible(timeout=500)
        except Exception:
            return False

    if not buscar_visible():
        log("Panel colapsado — clickeo header 'Criterios de búsqueda'…")
        try:
            header = _first_visible(
                page,
                [
                    lambda: page.get_by_role("button", name=re.compile(r"criterios de b[uú]squeda", re.I)),
                    lambda: page.get_by_text(re.compile(r"^\s*criterios de b[uú]squeda\s*$", re.I)),
                ],
                timeout_ms=5000,
            )
            header.click(force=True)
            page.wait_for_timeout(800)
        except PlaywrightTimeoutError:
            log("(No encontré el header para expandir — sigo igual.)")
    else:
        log("✓ Panel ya expandido (Buscar visible).")

    # Cargamos el filtro guardado. Si falla (por ejemplo si lo renombraron),
    # aplicamos los criterios a mano (Estado=Abierto + Usuario=Milton).
    loaded = False
    if SAVED_FILTER_NAME:
        loaded = _load_saved_filter(page, SAVED_FILTER_NAME)
    if not loaded:
        log("Aplicando criterios manualmente como fallback…")
        _apply_manual_filters(page)

    log("Click en Buscar…")
    _wait_for_loader_gone(page)
    buscar = _first_visible(
        page,
        [
            lambda: page.get_by_role("button", name=re.compile(r"^\s*buscar\s*$", re.I)),
            lambda: page.locator('button:has-text("Buscar")'),
        ],
    )
    # force=True ignora el backdrop de Angular (que intercepta pointer events
    # cuando hay muchos resultados cargando).
    buscar.click(force=True)
    page.wait_for_timeout(3000)
    _wait_for_loader_gone(page)


def _load_saved_filter(page: Page, filter_name: str) -> bool:
    """Abre el dropdown 'Filtros guardados', busca un filtro que coincida
    aprox. con `filter_name` (case-insensitive, sin importar puntuación),
    y clickea 'Cargar'. Devuelve True si lo cargó, False si no encontró
    nada o falló — así el caller puede caer al fallback manual.

    Tolera renombres como 'Asignados a Milton' → 'Asignado a milton'."""
    log(f"Cargando filtro guardado '{filter_name}'…")

    # Normalizamos: lowercase + sin acentos/espacios extras
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip().lower())
    target = norm(filter_name)
    # Para matchear flexible, partimos en palabras y buscamos AL MENOS las palabras "clave"
    # (de 4+ letras) en el nombre del filtro. Así 'Asignados a Milton' matchea
    # 'Asignado a milton', 'Asignados-Milton', etc.
    key_words = [w for w in target.split() if len(w) >= 4]

    def matches(candidate: str) -> bool:
        cand = norm(candidate)
        if cand == target:
            return True
        # Match aprox: todas las palabras clave aparecen (puede ser plural/singular)
        if key_words and all(w[:5] in cand for w in key_words):
            return True
        return False

    # Si ya está cargado (el nombre aparece en el dropdown), no hacemos nada.
    try:
        labels = page.locator(".ng-value-label").all()
        for lab in labels:
            try:
                txt = lab.inner_text(timeout=200)
                if matches(txt):
                    log(f"✓ Filtro '{txt.strip()}' ya estaba cargado.")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # Abrimos el dropdown de Filtros guardados.
    try:
        dropdown = _first_visible(
            page,
            [
                lambda: page.locator(
                    'xpath=//*[contains(normalize-space(.), "Filtros guardados")]/following::ng-select[1]'
                ),
                lambda: page.get_by_label(re.compile(r"Filtros guardados", re.I)),
            ],
            timeout_ms=5000,
        )
        dropdown.click(force=True)
        page.wait_for_timeout(500)
    except PlaywrightTimeoutError:
        log("(No encontré el dropdown 'Filtros guardados' — voy a fallback manual.)")
        return False

    # Listamos todas las opciones visibles y buscamos un match flexible
    page.wait_for_timeout(200)
    try:
        opciones = page.locator(".ng-option").all()
        found_opt = None
        for op in opciones:
            try:
                txt = op.inner_text(timeout=200)
                if matches(txt):
                    found_opt = op
                    log(f"→ Match flexible: '{txt.strip()}' coincide con '{filter_name}'")
                    break
            except Exception:
                continue
        if not found_opt:
            log(f"⚠️  No encontré ninguna opción que matchee '{filter_name}'. Voy a fallback manual.")
            # Cerramos el dropdown para no dejar UI bloqueada
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return False
        found_opt.click(force=True)
        page.wait_for_timeout(400)
    except Exception as e:
        log(f"⚠️  Error buscando opciones: {e}. Fallback manual.")
        return False

    # Clickeamos 'Cargar' para aplicar el filtro seleccionado.
    try:
        cargar = _first_visible(
            page,
            [
                lambda: page.get_by_role("button", name=re.compile(r"^\s*cargar\s*$", re.I)),
                lambda: page.locator('button:has-text("Cargar")'),
            ],
            timeout_ms=5000,
        )
        cargar.click(force=True)
        page.wait_for_timeout(800)
        log(f"✓ Filtro cargado.")
        return True
    except PlaywrightTimeoutError:
        log("⚠️  No encontré el botón 'Cargar'. Fallback manual.")
        return False


def _criterio_rows(page: Page):
    """Devuelve los <tr> del panel de criterios. Una fila configurada tiene
    3 ng-select (campo, operador, valor); una recién agregada tiene 2 hasta
    que se elige el campo. La paginación tiene 1, así filtramos por >=2."""
    rows = page.locator("tr").filter(has=page.locator("ng-select")).all()
    return [r for r in rows if r.locator("ng-select").count() >= 2]


def _ngselect_pick(page: Page, ng_select, value: str) -> None:
    """Abre un <ng-select> y elige la opción que coincide con `value`.
    Tipea para filtrar las opciones (el dropdown de Usuario tiene cientos)."""
    ng_select.click()
    page.wait_for_timeout(400)
    # El input activo del ng-select abierto. Tipeamos para filtrar.
    typed_ok = False
    try:
        active_input = ng_select.locator(".ng-input input").first
        active_input.fill("")
        active_input.type(value, delay=30)
        typed_ok = True
    except Exception:
        # Algunos ng-select no tienen input editable; sigue solo con click.
        pass

    page.wait_for_timeout(700)

    # Buscamos la opción. Match exact-ish primero, después contains.
    pattern_exact = re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
    pattern_loose = re.compile(re.escape(value), re.I)

    option = None
    for factory in (
        lambda: page.locator(".ng-option").filter(has_text=pattern_exact).first,
        lambda: page.get_by_role("option", name=pattern_exact).first,
        lambda: page.locator(".ng-option").filter(has_text=pattern_loose).first,
        lambda: page.get_by_role("option", name=pattern_loose).first,
    ):
        try:
            cand = factory()
            cand.wait_for(state="visible", timeout=3000)
            option = cand
            break
        except Exception:
            continue

    if option is None:
        raise RuntimeError(f"No encontré opción '{value}' en el ng-select.")

    option.click()
    page.wait_for_timeout(400)


def _apply_manual_filters(page: Page) -> None:
    """Aplica los criterios sin usar filtro guardado:
       - Criterio 1: Estado general del contacto = Abierto  (default del SPA)
       - Criterio 2: <FILTRO_CAMPO> = <FILTRO_VALOR>        (configurable)

    Es el fallback cuando el filtro guardado no se encuentra (porque la
    cuenta nunca lo creó, lo renombraron, etc.)."""
    estado_valor = os.environ.get("FILTRO_ESTADO", "Abierto")
    campo_extra = os.environ.get("FILTRO_CAMPO", "Usuario asignado")
    valor_extra = os.environ.get("FILTRO_VALOR", "Messina Milton Messina")

    log(f"  Criterio 1: Estado general del contacto = {estado_valor}")
    log(f"  Criterio 2: {campo_extra} = {valor_extra}")

    # Esperar a que el panel termine de renderizar.
    try:
        page.wait_for_selector("tr:has(ng-select)", timeout=5000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(300)

    rows = _criterio_rows(page)
    log(f"  Filas de criterios detectadas: {len(rows)}")
    if not rows:
        raise RuntimeError("No detecté ninguna fila de criterios para configurar.")

    # Asegurar fila 1 = Estado general del contacto = Abierto (suele venir así).
    try:
        labels1 = rows[0].locator(".ng-value-label").all_inner_texts()
    except Exception:
        labels1 = []
    if not (labels1 and "Estado general del contacto" in labels1[0]):
        try:
            row1_sels = rows[0].locator("ng-select").all()
            _ngselect_pick(page, row1_sels[0], "Estado general del contacto")
            _ngselect_pick(page, row1_sels[2], estado_valor)
        except Exception as e:
            log(f"  ⚠️  No pude setear fila 1: {e} — confío en el default.")

    # Si solo hay 1 fila, agregamos la 2da con el botón + de esa fila.
    if len(rows) < 2:
        log("  Agregando segunda fila con el botón '+' …")
        add_btn = rows[-1].locator('button.addButton, button[title="Agregar"]').first
        add_btn.click(force=True)
        page.wait_for_timeout(500)
        rows = _criterio_rows(page)
        log(f"  Filas tras agregar: {len(rows)}")
        if len(rows) < 2:
            raise RuntimeError("No pude agregar la segunda fila de criterios.")

    # Llenar fila 2: primero el campo. El ng-select del VALOR aparece recién
    # después de elegir el campo (es dinámico según el tipo de campo).
    row2 = rows[1]
    row2_sels = row2.locator("ng-select").all()
    if len(row2_sels) < 2:
        raise RuntimeError(
            f"Fila 2 no tiene al menos 2 ng-select (tiene {len(row2_sels)})."
        )
    _ngselect_pick(page, row2_sels[0], campo_extra)
    # Esperar a que se renderice el ng-select del valor.
    page.wait_for_timeout(800)
    row2_sels = row2.locator("ng-select").all()
    if len(row2_sels) < 3:
        # Algunos campos (texto libre) podrían no usar ng-select para el valor.
        # Hacemos fallback: buscar el último ng-select de la fila o un input.
        log(f"  ⚠️  Fila 2 sigue con {len(row2_sels)} ng-select después de elegir campo; pruebo con el último.")
    _ngselect_pick(page, row2_sels[-1], valor_extra)
    log("  ✓ Criterios configurados manualmente.")


def export_all_fields(page: Page) -> None:
    log("Click en Exportar…")
    _wait_for_loader_gone(page)
    exportar_btn = _first_visible(
        page,
        [
            lambda: page.get_by_role("button", name=re.compile(r"^\s*exportar\s*$", re.I)),
            lambda: page.locator('button:has-text("Exportar")'),
        ],
    )
    exportar_btn.click(force=True)

    log("Esperando modal 'Columnas a exportar'…")
    # El modal puede no tener role=dialog — probamos varias estrategias.
    modal = None
    for factory in (
        lambda: page.get_by_role("dialog"),
        lambda: page.locator('[class*="modal"][class*="show"]'),
        lambda: page.locator('.modal-dialog, .modal-content'),
        # Buscamos el contenedor que tenga el título "Columnas a exportar".
        lambda: page.locator('*').filter(
            has=page.get_by_text(re.compile(r"Columnas a exportar", re.I))
        ).locator('xpath=ancestor-or-self::*[self::div or self::dialog][1]'),
    ):
        try:
            candidate = factory().first
            candidate.wait_for(state="visible", timeout=5000)
            modal = candidate
            break
        except PlaywrightTimeoutError:
            continue

    if modal is None:
        _dump_debug(page, "modal_not_found")
        raise RuntimeError(
            "No apareció el modal 'Columnas a exportar' después de clickear Exportar. "
            "Revisá el HTML en ./downloads/"
        )

    log("Abriendo dropdown 'Selección de campos'…")
    # El campo es un ng-select con placeholder "Ingresá el nombre de los campos…"
    # Clickeamos cualquier parte del control para desplegar las opciones.
    seleccion = _first_visible(
        page,
        [
            lambda: modal.locator("ng-select").first,
            lambda: modal.get_by_placeholder(re.compile(r"Ingres[aá].*campos", re.I)),
            lambda: modal.locator('[role="combobox"]').first,
        ],
        timeout_ms=10000,
    )
    seleccion.click(force=True)
    page.wait_for_timeout(400)

    log("Seleccionando 'Todos los campos'…")
    # Las opciones del ng-select aparecen en un panel (puede estar fuera del modal
    # en el DOM por portaling), así que buscamos en toda la page.
    todos = _first_visible(
        page,
        [
            lambda: page.get_by_role("option", name=re.compile(r"^\s*Todos los campos\s*$", re.I)),
            lambda: page.locator(".ng-option").filter(
                has_text=re.compile(r"^\s*Todos los campos\s*$", re.I)
            ),
            lambda: page.get_by_text(re.compile(r"^\s*Todos los campos\s*$", re.I)),
        ],
        timeout_ms=10000,
    )
    todos.click(force=True)
    page.wait_for_timeout(400)

    log("Confirmando Exportar dentro del modal…")
    confirmar = _first_visible(
        page,
        [
            lambda: modal.get_by_role("button", name=re.compile(r"^\s*exportar\s*$", re.I)),
            lambda: modal.locator('button:has-text("Exportar")'),
            lambda: page.locator('button:has-text("Exportar")').last,
        ],
    )
    confirmar.click(force=True)


def wait_for_report_and_download(page: Page, captured: list) -> Path:
    """Espera a que aparezca la descarga. Tres escenarios:
      - Playwright captura un evento `download` (listener en _run_once).
      - Aparece el banner async y después un botón/link 'Descargar'.
      - En modo CDP, Chrome baja el archivo a ~/Downloads sin disparar
        el evento de Playwright. Fallback: polleamos ~/Downloads buscando
        un archivo Reporte_BandejaDeEntrada_*.csv nuevo.
    """
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Snapshot de ~/Downloads ANTES de esperar, para detectar archivos nuevos.
    home_downloads = Path.home() / "Downloads"
    existing_reports = _list_bandeja_reports(home_downloads)
    start_time = time.time()

    if captured:
        log("✓ Descarga directa capturada.")
        return _save_download(captured[0])

    try:
        page.get_by_text(
            re.compile(r"El reporte se está generando", re.I)
        ).wait_for(timeout=5000)
        log("Banner de 'reporte generándose' detectado. Esperando que esté listo…")
    except PlaywrightTimeoutError:
        log("No vi banner async — sigo esperando la descarga.")

    if DEBUG_PAUSE:
        log("DEBUG_PAUSE=1 — clickeá la descarga a mano.")
        page.pause()

    deadline = time.time() + REPORT_WAIT_TIMEOUT_MS / 1000

    while time.time() < deadline:
        if captured:
            return _save_download(captured[0])

        # Fallback CDP: ¿apareció un archivo nuevo en ~/Downloads?
        new_report = _find_new_bandeja_report(
            home_downloads, existing_reports, min_mtime=start_time
        )
        if new_report:
            target = DOWNLOAD_DIR / f"{int(time.time())}_{new_report.name}"
            import shutil
            shutil.move(str(new_report), str(target))
            log(f"✓ Archivo detectado en ~/Downloads y movido a {target}")
            return target

        # Intentamos encontrar un botón/link "Descargar" visible.
        candidates = [
            page.get_by_role("button", name=re.compile(r"^\s*descargar\s*$", re.I)),
            page.get_by_role("link", name=re.compile(r"^\s*descargar\s*$", re.I)),
            page.locator('a:has-text("Descargar")'),
            page.locator('button:has-text("Descargar")'),
        ]
        for c in candidates:
            try:
                if c.first.is_visible(timeout=500):
                    log("Encontré botón/link 'Descargar' — clickeando…")
                    with page.expect_download(timeout=60000) as dl_info:
                        c.first.click()
                    return _save_download(dl_info.value)
            except PlaywrightTimeoutError:
                pass
            except Exception:
                pass

        # Algunas apps ponen las descargas en una campana de notificaciones.
        # Si aparece un ícono con badge, lo abrimos e intentamos de nuevo.
        try:
            bell = page.locator(
                '[aria-label*="notificacion" i], [aria-label*="notification" i], .notification-bell'
            ).first
            if bell.is_visible(timeout=500):
                bell.click()
                page.wait_for_timeout(500)
        except Exception:
            pass

        page.wait_for_timeout(REPORT_POLL_INTERVAL_MS)

    raise TimeoutError(
        f"El reporte no estuvo listo dentro de {REPORT_WAIT_TIMEOUT_MS / 1000:.0f}s."
    )


def _save_download(dl: Download) -> Path:
    suggested = dl.suggested_filename or "export.xlsx"
    target = DOWNLOAD_DIR / f"{int(time.time())}_{suggested}"
    dl.save_as(target)
    log(f"Archivo descargado: {target}")
    return target


def _list_bandeja_reports(downloads_dir: Path) -> set[str]:
    """Devuelve los nombres de archivos tipo Reporte_BandejaDeEntrada_*.csv
    que ya existen en ~/Downloads (para comparar 'antes/después')."""
    if not downloads_dir.exists():
        return set()
    return {f.name for f in downloads_dir.glob("Reporte_BandejaDeEntrada_*")}


def _find_new_bandeja_report(
    downloads_dir: Path, existing: set[str], min_mtime: float
) -> Optional[Path]:
    """Busca un archivo Reporte_BandejaDeEntrada_* que NO esté en `existing`
    y cuya fecha de modificación sea >= min_mtime (para no agarrar uno viejo)."""
    if not downloads_dir.exists():
        return None
    candidates = [
        f for f in downloads_dir.glob("Reporte_BandejaDeEntrada_*")
        if f.name not in existing and f.stat().st_mtime >= min_mtime
    ]
    if not candidates:
        return None
    # Si hay varios (raro), tomamos el más reciente.
    candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return candidates[0]


def _wait_for_loader_gone(page: Page, timeout_ms: int = 30000) -> None:
    """Espera a que el loader/backdrop de Angular desaparezca. El SPA muestra
    un <app-loader> con un backdrop full-screen que intercepta los clicks
    mientras carga datos. Si no esperamos, los clicks fallan con
    'subtree intercepts pointer events'."""
    selectors = [
        "app-loader .backdrop",
        "app-loader",
        ".loader-overlay",
        ".backdrop.full-screen",
    ]
    for sel in selectors:
        try:
            page.locator(sel).first.wait_for(state="hidden", timeout=timeout_ms)
        except Exception:
            pass


def _first_visible(page: Page, factories, timeout_ms: int = 15000):
    """Devuelve el primer locator que esté visible, probando varias estrategias."""
    last_err: Optional[Exception] = None
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for factory in factories:
            try:
                loc = factory().first
                loc.wait_for(state="visible", timeout=500)
                return loc
            except Exception as e:
                last_err = e
                continue
        page.wait_for_timeout(250)
    raise PlaywrightTimeoutError(
        f"Ningún selector fue visible en {timeout_ms}ms. Último error: {last_err}"
    )


def _run_once() -> Path:
    """Una pasada completa: conecta al browser, loguea si hace falta, filtra,
    exporta y descarga."""
    with sync_playwright() as p:
        if BROWSER_MODE == "cdp":
            context, cleanup = _connect_cdp(p)
        else:
            context, cleanup = _launch_persistent(p)

        page = context.pages[0] if context.pages else context.new_page()

        # Listener global de descargas. Lo seteamos ANTES de cualquier click
        # de exportar para no perder una descarga que dispara inmediatamente.
        captured_downloads: list[Download] = []
        page.on("download", lambda dl: captured_downloads.append(dl))

        # Debug: capturar errores de consola y de página (exceptions JS).
        page.on(
            "console",
            lambda msg: log(f"[browser console.{msg.type}] {msg.text[:200]}")
            if msg.type in ("error", "warning")
            else None,
        )
        page.on("pageerror", lambda err: log(f"[browser pageerror] {err}"))

        try:
            # Fast path 1: si ya estamos en la bandeja con Exportar visible,
            # no hacemos login ni navegación.
            # Fast path 2: si la URL ya es del backoffice (no Keycloak), estamos
            # autenticadas — vamos directo a la bandeja sin llamar login() (que
            # hace goto a la raíz y puede romper la sesión OAuth en curso).
            already_in_backoffice = (
                "bacolaborativa-backoffice" in page.url
                and "identidad-gcaba" not in page.url
            )
            if _detect_bandeja(page, timeout_s=3.0):
                log("✓ Ya estoy en la bandeja — skippeando login y navegación.")
            elif already_in_backoffice:
                log("✓ Ya autenticada en el backoffice — voy directo a la bandeja sin pasar por login.")
                go_to_bandeja(page)
            else:
                login(page)
                go_to_bandeja(page)
            apply_filter_abierto_and_search(page)
            export_all_fields(page)
            path = wait_for_report_and_download(page, captured_downloads)

            # Antes de cerrar el browser, dumpeamos el storage state actual.
            # Estas cookies fueron renovadas por Keycloak durante esta corrida,
            # así que extienden la vida de la sesión. El workflow las sube
            # como secret nuevo después.
            try:
                refreshed = DOWNLOAD_DIR / "session_refreshed.json"
                refreshed.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(refreshed))
                log(f"✓ Storage state refrescado guardado en {refreshed.name}")
            except Exception as e:
                log(f"(no pude dumpear storage refrescado: {e})")

            return path
        finally:
            if KEEP_OPEN:
                log("KEEP_OPEN=1 — dejando el browser abierto.")
                try:
                    page.pause()
                except Exception:
                    pass
            cleanup()


def _connect_cdp(p):
    """Se conecta a un Chrome ya corriendo con --remote-debugging-port."""
    log(f"Conectando por CDP a {CDP_URL}…")
    try:
        browser = p.chromium.connect_over_cdp(CDP_URL)
    except Exception as e:
        raise RuntimeError(
            f"No pude conectar a {CDP_URL}. ¿Abriste Chrome con "
            f"--remote-debugging-port=9222? (ver README). Error: {e}"
        )
    if not browser.contexts:
        raise RuntimeError("El Chrome al que conectaste no tiene contextos activos.")
    context = browser.contexts[0]
    log(f"✓ Conectado. {len(context.pages)} pestaña(s) abierta(s).")

    def cleanup():
        # NO cerramos Chrome — es el browser de la usuaria. Solo desconectamos.
        try:
            browser.close()
        except Exception:
            pass

    return context, cleanup


def _launch_persistent(p):
    """Lanza un Chrome (modo CI). Si hay BA_SESSION_JSON, usa
    new_context(storage_state=...) que carga cookies + localStorage. Si no,
    cae a launch_persistent_context."""
    if BA_SESSION_JSON:
        # Modo CI: browser fresh + context con storage_state inyectado.
        # Esto incluye cookies + localStorage + sessionStorage del estado
        # capturado (vital para que el SPA tenga el JWT que el backend espera).
        launch_kwargs = dict(headless=HEADLESS, slow_mo=SLOW_MO)
        if BROWSER_CHANNEL and BROWSER_CHANNEL != "chromium":
            launch_kwargs["channel"] = BROWSER_CHANNEL
        browser = p.chromium.launch(**launch_kwargs)
        try:
            storage_state = json.loads(BA_SESSION_JSON)
        except Exception as e:
            log(f"⚠️  BA_SESSION_JSON inválido: {e}. Cayendo a contexto vacío.")
            storage_state = None

        context = browser.new_context(
            storage_state=storage_state,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        if storage_state:
            n_cookies = len(storage_state.get("cookies", []))
            n_origins = len(storage_state.get("origins", []))
            log(f"✓ Storage state cargado: {n_cookies} cookies, {n_origins} orígenes (con localStorage).")

        def cleanup():
            try:
                context.close()
                browser.close()
            except Exception:
                pass

        return context, cleanup

    # Modo local sin sesión inyectada: persistent context.
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    launch_kwargs = dict(
        user_data_dir=str(USER_DATA_DIR),
        headless=HEADLESS,
        slow_mo=SLOW_MO,
        accept_downloads=True,
        viewport={"width": 1280, "height": 900},
    )
    if BROWSER_CHANNEL and BROWSER_CHANNEL != "chromium":
        launch_kwargs["channel"] = BROWSER_CHANNEL
    context = p.chromium.launch_persistent_context(**launch_kwargs)

    def cleanup():
        try:
            context.close()
        except Exception:
            pass

    return context, cleanup


def download_tickets() -> Path:
    """Corre el scraper con reintentos. Si todos los intentos fallan,
    manda un mail de alerta y re-raisea la excepción original."""
    if not BA_USER or not BA_PASSWORD:
        raise RuntimeError(
            "Faltan BA_USER / BA_PASSWORD. Definilos en .env o como variables de entorno."
        )

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    last_exc: Optional[BaseException] = None
    captcha_rejections = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"Intento {attempt}/{MAX_ATTEMPTS}…")
        try:
            return _run_once()
        except CaptchaRejectedError as e:
            last_exc = e
            captcha_rejections += 1
            log(f"Intento {attempt}: captcha rechazado por Keycloak ({captcha_rejections}/2).")
            if captcha_rejections >= 2:
                log(
                    "⛔ 2 captchas rechazados seguidos — abortando run. "
                    "El solver no está pasando el score del GCBA. "
                    "Refrescar BA_SESSION_JSON manualmente con scripts/refresh-session.sh "
                    "para saltear el login."
                )
                break
            log("Reintentando inmediatamente…")
            time.sleep(1)
        except Exception as e:
            last_exc = e
            log(f"Intento {attempt} falló: {e!r}")
            if attempt < MAX_ATTEMPTS:
                # Los 2 primeros fallos suelen ser captcha/timing — reintento
                # rápido. Si seguimos fallando, la plataforma está caída
                # (pasa: ver incidente 09-11/05) y conviene darle aire.
                pauses = [1, 1, 60, 90]
                pause = pauses[attempt - 1] if attempt - 1 < len(pauses) else 90
                log(f"Esperando {pause}s antes de reintentar…")
                time.sleep(pause)

    # Llegamos acá solo si todos los intentos fallaron.
    notify.send_failure_alert(
        subject="[BA Colaborativa] Scraper falló después de reintentos",
        body=(
            f"El scraper no pudo descargar los tickets después de "
            f"{MAX_ATTEMPTS} intentos.\n\n"
            f"Último error: {last_exc!r}"
        ),
        exc=last_exc,
    )
    assert last_exc is not None
    raise last_exc


if __name__ == "__main__":
    try:
        out = download_tickets()
    except Exception as e:
        log(f"ERROR final: {e}")
        sys.exit(1)
    log(f"OK — {out}")
