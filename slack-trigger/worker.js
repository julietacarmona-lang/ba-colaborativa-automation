/**
 * Cloudflare Worker — comandos de Slack + renovación de sesión para BA Colaborativa
 *
 * Endpoints:
 *   POST /              — slash commands de Slack (/bajada-tickets, /estado-bot, /renovar-sesion)
 *   POST /renovar       — bookmarklet (no Slack), requiere X-Refresh-Token
 *   OPTIONS /renovar    — CORS preflight
 *
 * Secrets en Cloudflare (Workers > Settings > Variables > Secrets):
 *   SLACK_SIGNING_SECRET  — Basic Information → Signing Secret en api.slack.com/apps
 *   GITHUB_TOKEN          — PAT de julietacarmona-lang con scope "workflow" + "repo"
 *   REFRESH_TOKEN         — token secreto para autenticar el bookmarklet (cualquier string largo)
 */

const GITHUB_REPO      = "julietacarmona-lang/ba-colaborativa-automation";
const WORKFLOW_FILE    = "daily.yml";
const RENOVAR_WORKFLOW = "renovar-sesion.yml";
const GITHUB_BRANCH    = "main";
const BA_ORIGIN        = "https://bacolaborativa-backoffice.buenosaires.gob.ar";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS preflight para el bookmarklet
    if (request.method === "OPTIONS" && url.pathname === "/renovar") {
      return corsPreflightResponse();
    }

    // Endpoint del bookmarklet (sin verificación Slack, usa su propio token)
    if (request.method === "POST" && url.pathname === "/renovar") {
      return handleRenovar(request, env);
    }

    // Página de instalación del bookmarklet (GET con token)
    if (request.method === "GET" && url.pathname === "/setup") {
      return handleSetupPage(url, env);
    }

    // Todo lo demás: slash commands de Slack
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const body      = await request.text();
    const timestamp = request.headers.get("x-slack-request-timestamp") || "";
    const slackSig  = request.headers.get("x-slack-signature") || "";

    if (!await verifySlackSignature(env.SLACK_SIGNING_SECRET, timestamp, body, slackSig)) {
      return new Response("Unauthorized", { status: 401 });
    }

    const params  = new URLSearchParams(body);
    const command = params.get("command") || "";

    if (command === "/bajada-tickets") return handleTrigger(env);
    if (command === "/estado-bot")     return handleEstado(env);
    if (command === "/renovar-sesion") return handleRenovarSlack(env);

    return slackResponse(`Comando no reconocido: ${command}`);
  },
};

// ─── /bajada-tickets — dispara el workflow ────────────────────────────────────

async function handleTrigger(env) {
  const result = await triggerWorkflow(env.GITHUB_TOKEN, WORKFLOW_FILE, {});
  if (result.ok) {
    return slackResponse(
      "▶️ *Bot BA Colaborativa arrancando!*\nEn ~5 minutos llega la notificación de resultado. :hourglass_flowing_sand:",
      false
    );
  }
  return slackResponse(
    `❌ No pude arrancar el bot (error ${result.status}).\nAvisá a quien administra el bot.`,
    true
  );
}

// ─── /estado-bot — muestra el estado de las últimas corridas ──────────────────

async function handleEstado(env) {
  const runs = await getRecentRuns(env.GITHUB_TOKEN, 5);
  if (!runs) {
    return slackResponse("❌ No pude consultar el estado (error al llamar a GitHub).", true);
  }

  const lines = runs.map(r => {
    const icon  = r.conclusion === "success" ? "✅" : r.conclusion === "failure" ? "❌" : "⚠️";
    const when  = timeAgo(r.created_at);
    const label = r.event === "schedule" ? "automático" : "manual";
    return `${icon} ${when} (${label})`;
  });

  const last      = runs[0];
  const failCount = runs.filter(r => r.conclusion !== "success").length;
  let header = "📊 *Estado del bot BA Colaborativa*";
  if (last.conclusion === "success") {
    header += " — todo bien :white_check_mark:";
  } else {
    header += failCount >= 3
      ? " — *lleva varios fallos seguidos, avisá a quien administra el bot* :rotating_light:"
      : " — falló la última corrida, pero el cron sigue activo :warning:";
  }

  const hint = last.conclusion !== "success"
    ? "\nPodés intentar ahora con */bajada-tickets*\nSi sigue fallando: */renovar-sesion* para renovar las cookies de login."
    : "";

  return slackResponse(`${header}\n${lines.join("\n")}${hint}`, true);
}

// ─── /renovar-sesion — manda link a la página de instalación ─────────────────

function handleRenovarSlack(env) {
  const setupUrl = `https://lively-pond-17cd.julieta-carmona.workers.dev/setup?t=${env.REFRESH_TOKEN}`;
  const text = [
    "🔑 *Renovar sesión de BA Colaborativa*",
    "",
    "*¿Primera vez? Guardá el bookmark (una sola vez):*",
    `<${setupUrl}|➡️ Abrí esta página y seguí los pasos>`,
    "",
    "*¿Ya tenés el bookmark guardado?*",
    "1. Abrí <https://bacolaborativa-backoffice.buenosaires.gob.ar|BA Colaborativa> y logueate",
    "2. Hacé clic en el bookmark *🔑 Renovar sesión bot* de tus favoritos",
    "3. Vas a ver un cartelito de confirmación",
    "4. Probá con */bajada-tickets* — ya debería andar",
  ].join("\n");
  return slackResponse(text, true);
}

// ─── GET /setup — página de instalación del bookmarklet ──────────────────────

function handleSetupPage(url, env) {
  const token = url.searchParams.get("t") || "";
  if (!env.REFRESH_TOKEN || token !== env.REFRESH_TOKEN) {
    return new Response("No autorizado", { status: 401 });
  }

  const workerUrl = "https://lively-pond-17cd.julieta-carmona.workers.dev";
  const bookmarkletCode = `javascript:(function(){if(!location.hostname.includes('bacolaborativa-backoffice')){alert('⚠️ Abrí este bookmark estando en BA Colaborativa, no en otra página.');return;}var s={origins:[{origin:location.origin,localStorage:Object.entries(localStorage).map(([k,v])=>({name:k,value:v})),sessionStorage:Object.entries(sessionStorage).map(([k,v])=>({name:k,value:v}))}]};fetch('${workerUrl}/renovar',{method:'POST',headers:{'Content-Type':'application/json','X-Refresh-Token':'${token}'},body:JSON.stringify(s)}).then(r=>r.json()).then(d=>alert(d.ok?'✅ Sesión renovada. El bot va a volver a funcionar en la próxima corrida.':'❌ Error: '+(d.error||'desconocido'))).catch(e=>alert('❌ Error de conexión: '+e));})();`;

  const html = `<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>🔑 Renovar sesión — BA Colaborativa Bot</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 600px; margin: 40px auto; padding: 0 20px; color: #1a1a2e; }
    h1 { color: #2A205E; }
    .step { background: #f4f4f8; border-radius: 8px; padding: 16px 20px; margin: 16px 0; }
    .step h2 { margin: 0 0 8px; font-size: 1rem; color: #2A205E; }
    .bookmarklet-link { display: inline-block; background: #2A205E; color: white !important; text-decoration: none; padding: 10px 20px; border-radius: 6px; font-size: 1.1rem; cursor: grab; border: 2px dashed #00C4B4; margin: 8px 0; }
    .bookmarklet-link:hover { background: #00C4B4; }
    .note { font-size: 0.85rem; color: #666; margin-top: 8px; }
    .badge { display: inline-block; background: #00C4B4; color: white; border-radius: 12px; padding: 2px 10px; font-size: 0.8rem; }
  </style>
</head>
<body>
  <h1>🔑 Renovar sesión del bot</h1>
  <p>Cuando el bot de BA Colaborativa no puede loguearse, esto lo arregla. Tarda 1 minuto.</p>

  <div class="step">
    <h2>Paso 1 — Guardar el bookmark <span class="badge">solo una vez</span></h2>
    <p>Arrastrá este botón a tu barra de favoritos del browser:</p>
    <a class="bookmarklet-link" href="${bookmarkletCode}">🔑 Renovar sesión bot</a>
    <p class="note">¿No podés arrastrarlo? Hacé clic derecho → "Guardar enlace como marcador" (o "Bookmark this link").</p>
  </div>

  <div class="step">
    <h2>Paso 2 — Úsalo cuando el bot falla</h2>
    <ol>
      <li>Ir a <a href="https://bacolaborativa-backoffice.buenosaires.gob.ar" target="_blank">BA Colaborativa</a> y loguearte normalmente</li>
      <li>Cuando cargue la pantalla, hacer clic en el bookmark <strong>🔑 Renovar sesión bot</strong></li>
      <li>Vas a ver un cartelito de confirmación en pantalla</li>
      <li>En ~2 minutos el bot puede volver a funcionar — probá con <code>/bajada-tickets</code> en Slack</li>
    </ol>
  </div>

  <p class="note">Este link es privado — no lo compartas.</p>
</body>
</html>`;

  return new Response(html, {
    headers: { "Content-Type": "text/html; charset=UTF-8" },
  });
}

// ─── POST /renovar — llamado por el bookmarklet ───────────────────────────────

async function handleRenovar(request, env) {
  const token = request.headers.get("x-refresh-token") || "";
  if (!env.REFRESH_TOKEN || token !== env.REFRESH_TOKEN) {
    return corsResponse(JSON.stringify({ ok: false, error: "Token inválido" }), 401);
  }

  let sessionData;
  try {
    sessionData = await request.json();
  } catch {
    return corsResponse(JSON.stringify({ ok: false, error: "JSON inválido" }), 400);
  }

  const sessionStr = JSON.stringify(sessionData);
  if (sessionStr.length > 60000) {
    return corsResponse(JSON.stringify({ ok: false, error: "Sesión demasiado grande (>60KB)" }), 400);
  }

  const result = await triggerWorkflow(env.GITHUB_TOKEN, RENOVAR_WORKFLOW, { session_data: sessionStr });
  if (result.ok) {
    return corsResponse(JSON.stringify({ ok: true }), 200);
  }
  return corsResponse(JSON.stringify({ ok: false, error: `GitHub respondió ${result.status}` }), 500);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

async function triggerWorkflow(token, workflowFile, inputs) {
  const url  = `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${workflowFile}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: githubHeaders(token),
    body: JSON.stringify({ ref: GITHUB_BRANCH, inputs }),
  });
  return { ok: resp.status === 204, status: resp.status };
}

async function getRecentRuns(token, limit = 5) {
  const url  = `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/runs?per_page=${limit}`;
  const resp = await fetch(url, { headers: githubHeaders(token) });
  if (!resp.ok) return null;
  const data = await resp.json();
  return (data.workflow_runs || []).slice(0, limit);
}

function githubHeaders(token) {
  return {
    "Authorization": `Bearer ${token}`,
    "Accept":        "application/vnd.github+json",
    "Content-Type":  "application/json",
    "User-Agent":    "slack-ba-worker/1.0",
  };
}

function slackResponse(text, ephemeral = false) {
  return new Response(
    JSON.stringify({ response_type: ephemeral ? "ephemeral" : "in_channel", text }),
    { headers: { "Content-Type": "application/json" } }
  );
}

function corsPreflightResponse() {
  return new Response(null, {
    status: 204,
    headers: {
      "Access-Control-Allow-Origin":  BA_ORIGIN,
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-Refresh-Token",
      "Access-Control-Max-Age":       "86400",
    },
  });
}

function corsResponse(body, status) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type":                "application/json",
      "Access-Control-Allow-Origin": BA_ORIGIN,
    },
  });
}

function timeAgo(isoDate) {
  const diffMs  = Date.now() - new Date(isoDate).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 60) return `hace ${diffMin} min`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24)   return `hace ${diffH}h`;
  return `hace ${Math.floor(diffH / 24)} días`;
}

async function verifySlackSignature(secret, timestamp, body, signature) {
  if (!secret || !timestamp || !signature) return false;
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - parseInt(timestamp)) > 300) return false;
  const sigBase = `v0:${timestamp}:${body}`;
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const mac      = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(sigBase));
  const expected = "v0=" + Array.from(new Uint8Array(mac))
    .map(b => b.toString(16).padStart(2, "0")).join("");
  return expected === signature;
}
