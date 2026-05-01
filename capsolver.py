"""Cliente HTTP de CapSolver (capsolver.com) para resolver reCAPTCHA.

Soporta v2, v2-invisible y v2-Enterprise. Usamos Enterprise porque es
lo que mostró ser el caso para el Keycloak del GCBA (anti-captcha rechaza
el sitekey con 'is from another Recaptcha type', mientras que CapSolver
acepta y resuelve como Enterprise).

API ref: https://docs.capsolver.com/guide/captcha/ReCaptchaV2.html
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


def solve_recaptcha(
    api_key: str,
    site_url: str,
    site_key: str,
    *,
    enterprise: bool = True,
    is_invisible: bool = True,
    timeout_s: int = 180,
    poll_interval_s: float = 3.0,
    log=print,
) -> str:
    """Manda un reCAPTCHA a CapSolver y devuelve el token gRecaptchaResponse.

    enterprise    — usar el endpoint de Enterprise (lo que necesita el GCBA)
    is_invisible  — si el widget es invisible (típico en Enterprise / login forms)
    """
    if enterprise:
        task_type = "ReCaptchaV2EnterpriseTaskProxyLess"
    else:
        task_type = "ReCaptchaV2TaskProxyLess"

    task = {
        "type": task_type,
        "websiteURL": site_url,
        "websiteKey": site_key,
    }
    if is_invisible:
        task["isInvisible"] = True

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
    log(f"[capsolver] tarea creada (id={task_id}, type={task_type}), esperando…")

    deadline = time.time() + timeout_s
    started = time.time()
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
        status = result.get("status")
        if status == "ready":
            elapsed = int(time.time() - started)
            log(f"[capsolver] ✓ resuelto en {elapsed}s")
            return result["solution"]["gRecaptchaResponse"]

    raise TimeoutError(f"CapSolver no resolvió en {timeout_s}s.")


def get_balance(api_key: str) -> float:
    """Devuelve el saldo en USD de la cuenta CapSolver."""
    resp = _post("/getBalance", {"clientKey": api_key})
    if resp.get("errorId", 0) != 0:
        raise RuntimeError(
            f"CapSolver getBalance falló: {resp.get('errorDescription')}"
        )
    return float(resp["balance"])
