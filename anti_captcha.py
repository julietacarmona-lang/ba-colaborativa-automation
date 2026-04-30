"""Cliente HTTP de Anti-Captcha (anti-captcha.com) para resolver reCAPTCHA v2.

API ref: https://anti-captcha.com/apidoc/task-types/NoCaptchaTaskProxyless
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

API_BASE = "https://api.anti-captcha.com"


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
    poll_interval_s: float = 5.0,
    log=print,
) -> str:
    """Manda un reCAPTCHA v2 a anti-captcha y devuelve el token de solución.

    site_url   — URL de la página donde está el captcha
    site_key   — el data-sitekey del widget reCAPTCHA
    timeout_s  — máximo tiempo a esperar la solución
    """
    create_resp = _post("/createTask", {
        "clientKey": api_key,
        "task": {
            "type": "NoCaptchaTaskProxyless",
            "websiteURL": site_url,
            "websiteKey": site_key,
        },
    })
    if create_resp.get("errorId", 0) != 0:
        raise RuntimeError(
            f"Anti-captcha createTask falló: "
            f"{create_resp.get('errorCode')} — {create_resp.get('errorDescription')}"
        )
    task_id = create_resp["taskId"]
    log(f"[anticaptcha] tarea creada (id={task_id}), esperando solución…")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_interval_s)
        result = _post("/getTaskResult", {
            "clientKey": api_key,
            "taskId": task_id,
        })
        if result.get("errorId", 0) != 0:
            raise RuntimeError(
                f"Anti-captcha getTaskResult falló: "
                f"{result.get('errorCode')} — {result.get('errorDescription')}"
            )
        if result.get("status") == "ready":
            elapsed = int(time.time() - (deadline - timeout_s))
            log(f"[anticaptcha] ✓ resuelto en {elapsed}s")
            return result["solution"]["gRecaptchaResponse"]

    raise TimeoutError(f"Anti-captcha no resolvió en {timeout_s}s.")


def get_balance(api_key: str) -> float:
    """Devuelve el saldo en USD de la cuenta."""
    resp = _post("/getBalance", {"clientKey": api_key})
    if resp.get("errorId", 0) != 0:
        raise RuntimeError(
            f"Anti-captcha getBalance falló: {resp.get('errorDescription')}"
        )
    return float(resp["balance"])
