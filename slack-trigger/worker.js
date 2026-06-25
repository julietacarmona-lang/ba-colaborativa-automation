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
    if (command === "/renovar-sesion") return handleRenovarSlack();

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

// ─── /renovar-sesion — instrucciones para el bookmarklet ──────────────────────

function handleRenovarSlack() {
  const text = [
    "🔑 *Renovar sesión de BA Colaborativa*",
    "",
    "Hacé esto una sola vez desde tu browser:",
    "1. Abrí BA Colaborativa: <https://bacolaborativa-backoffice.buenosaires.gob.ar|Clic acá para ir>",
    "2. Logueate con tu usuario y contraseña (igual que siempre)",
    "3. Cuando cargue la pantalla principal, hacé clic en el bookmark *🔑 Renovar sesión bot* que tenés en favoritos",
    "4. Vas a ver un cartelito de confirmación en la pantalla",
    "5. Listo — en ~2 minutos el bot ya puede usar las cookies nuevas. Probá con */bajada-tickets*",
    "",
    "_¿No tenés el bookmark guardado? Pedíselo a quien configuró el bot._",
  ].join("\n");
  return slackResponse(text, true);
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
