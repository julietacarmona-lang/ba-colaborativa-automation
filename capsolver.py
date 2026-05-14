"""Cliente HTTP de CapSolver (capsolver.com) para resolver reCAPTCHA Enterprise V3.

API ref: https://docs.capsolver.com/guide/captcha/ReCaptchaV3.html

Interfaz idéntica a anti_captcha.solve_recaptcha_v2 para que scraper.py pueda
intercambiar proveedores sin refactor mayor.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

API_BASE = "https://api.capsolver.com"


def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def solve_recaptcha_v2(
    api_key: str,
    site_url: str,
    site_key: str,
    timeout_s: int = 180,
    poll_interval_s: float = 3.0,
    is_invisible: bool = True,  # ignorado: V3 Enterprise no tiene "invisible"
    log=print,
) -> str:
    """Manda un reCAPTCHA Enterprise V3 a CapSolver y devuelve el token.

    Nombre conservado por compatibilidad con la firma de anti_captcha — en
    realidad resuelve V3 Enterprise (que es lo que usa Keycloak del GCBA).
    """
    task = {
        "type": "ReCaptchaV3EnterpriseTaskProxyless",
        "websiteURL": site_url,
        "websiteKey": site_key,
        "pageAction": "login",
        "minScore": 0.9,
    }
    create_resp = _post("/createTask", {
        "clientKey": api_key,
        "task": task,
    })
    if create_resp.get("errorId", 0) != 0:
        raise RuntimeError(
            f"CapSolver createTask falló: "
            f"{create_resp.get('errorCode')} — {create_resp.get('errorDescription')}"
        )
    task_id = create_resp["taskId"]
    log(f"[capsolver] tarea creada (id={task_id}), esperando solución…")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_interval_s)
        result = _post("/getTaskResult", {
            "clientKey": api_key,
            "taskId": task_id,
        })
        if result.get("errorId", 0) != 0:
            raise RuntimeError(
                f"CapSolver getTaskResult falló: "
                f"{result.get('errorCode')} — {result.get('errorDescription')}"
            )
        if result.get("status") == "ready":
            elapsed = int(time.time() - (deadline - timeout_s))
            log(f"[capsolver] ✓ resuelto en {elapsed}s")
            return result["solution"]["gRecaptchaResponse"]

    raise TimeoutError(f"CapSolver no resolvió en {timeout_s}s.")


def get_balance(api_key: str) -> float:
    """Devuelve el saldo en USD de la cuenta."""
    resp = _post("/getBalance", {"clientKey": api_key})
    if resp.get("errorId", 0) != 0:
        raise RuntimeError(
            f"CapSolver getBalance falló: {resp.get('errorDescription')}"
        )
    return float(resp["balance"])
