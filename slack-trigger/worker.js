/**
 * Cloudflare Worker — comandos de Slack para BA Colaborativa
 *
 * Comandos disponibles:
 *   /bajada-tickets  — dispara el bot manualmente
 *   /estado-bot      — muestra cuándo corrió por última vez y si está bien
 *
 * Secrets en Cloudflare (Workers > Settings > Variables > Secrets):
 *   SLACK_SIGNING_SECRET  — Basic Information → Signing Secret en api.slack.com/apps
 *   GITHUB_TOKEN          — PAT de julietacarmona-lang con scope "workflow" + "repo"
 */

const GITHUB_REPO   = "julietacarmona-lang/ba-colaborativa-automation";
const WORKFLOW_FILE = "daily.yml";
const GITHUB_BRANCH = "main";

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const body = await request.text();
    const timestamp = request.headers.get("x-slack-request-timestamp") || "";
    const slackSig  = request.headers.get("x-slack-signature") || "";

    if (!await verifySlackSignature(env.SLACK_SIGNING_SECRET, timestamp, body, slackSig)) {
      return new Response("Unauthorized", { status: 401 });
    }

    const params  = new URLSearchParams(body);
    const command = params.get("command") || "";
    const text    = (params.get("text") || "").trim();

    if (command === "/bajada-tickets") {
      return handleTrigger(env);
    }
    if (command === "/estado-bot") {
      return handleEstado(env);
    }

    return slackResponse(`Comando no reconocido: ${command}`);
  },
};

// /bajada-tickets — dispara el workflow
async function handleTrigger(env) {
  const result = await triggerWorkflow(env.GITHUB_TOKEN);
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

// /estado-bot — muestra el estado de las últimas corridas
async function handleEstado(env) {
  const runs = await getRecentRuns(env.GITHUB_TOKEN, 5);
  if (!runs) {
    return slackResponse("❌ No pude consultar el estado (error al llamar a GitHub).", true);
  }

  const lines = runs.map(r => {
    const icon = r.conclusion === "success" ? "✅" : r.conclusion === "failure" ? "❌" : "⚠️";
    const when = timeAgo(r.created_at);
    const label = r.event === "schedule" ? "automático" : "manual";
    return `${icon} ${when} (${label})`;
  });

  const last = runs[0];
  const lastOk = runs.find(r => r.conclusion === "success");
  let header = "📊 *Estado del bot BA Colaborativa*";
  if (last.conclusion === "success") {
    header += " — todo bien :white_check_mark:";
  } else {
    const failCount = runs.filter(r => r.conclusion !== "success").length;
    header += failCount >= 3
      ? " — *lleva varios fallos seguidos, avisá a quien administra el bot* :rotating_light:"
      : " — falló la última corrida, pero el cron sigue activo :warning:";
  }

  const detail = lines.join("\n");
  const hint = last.conclusion !== "success"
    ? "\nPodés intentar ahora con */bajada-tickets*"
    : "";

  return slackResponse(`${header}\n${detail}${hint}`, true);
}

async function triggerWorkflow(token) {
  const url = `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: githubHeaders(token),
    body: JSON.stringify({ ref: GITHUB_BRANCH }),
  });
  return { ok: resp.status === 204, status: resp.status };
}

async function getRecentRuns(token, limit = 5) {
  const url = `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/runs?per_page=${limit}`;
  const resp = await fetch(url, { headers: githubHeaders(token) });
  if (!resp.ok) return null;
  const data = await resp.json();
  return (data.workflow_runs || []).slice(0, limit);
}

function githubHeaders(token) {
  return {
    "Authorization": `Bearer ${token}`,
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
    "User-Agent": "slack-ba-worker/1.0",
  };
}

function slackResponse(text, ephemeral = false) {
  return new Response(
    JSON.stringify({ response_type: ephemeral ? "ephemeral" : "in_channel", text }),
    { headers: { "Content-Type": "application/json" } }
  );
}

function timeAgo(isoDate) {
  const diffMs  = Date.now() - new Date(isoDate).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 60)  return `hace ${diffMin} min`;
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24)    return `hace ${diffH}h`;
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
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(sigBase));
  const expected = "v0=" + Array.from(new Uint8Array(mac))
    .map(b => b.toString(16).padStart(2, "0")).join("");
  return expected === signature;
}
