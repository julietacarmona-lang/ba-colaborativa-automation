/**
 * Cloudflare Worker — Slack slash command /bajada-tickets
 *
 * Cuando alguien escribe /bajada-tickets en Slack, este worker:
 *  1. Verifica que el request viene realmente de Slack (firma HMAC)
 *  2. Dispara el workflow de GitHub Actions vía workflow_dispatch
 *  3. Responde inmediatamente al canal con un mensaje de confirmación
 *
 * Secrets que hay que configurar en Cloudflare (Workers > Settings > Variables > Secrets):
 *   SLACK_SIGNING_SECRET  — de https://api.slack.com/apps → Basic Information → Signing Secret
 *   GITHUB_TOKEN          — PAT de julietacarmona-lang con scope "workflow"
 *
 * Variables de entorno (no secretas, se pueden poner como plain text):
 *   GITHUB_REPO     = "julietacarmona-lang/ba-colaborativa-automation"
 *   WORKFLOW_FILE   = "daily.yml"
 *   GITHUB_BRANCH   = "main"
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

    // Verificar firma de Slack (previene requests falsos)
    const timestamp = request.headers.get("x-slack-request-timestamp") || "";
    const slackSig  = request.headers.get("x-slack-signature") || "";

    if (!await verifySlackSignature(env.SLACK_SIGNING_SECRET, timestamp, body, slackSig)) {
      return new Response("Unauthorized", { status: 401 });
    }

    // Responder rápido a Slack (tiene timeout de 3s) y disparar workflow en background
    const ctx = request.cf?.waitUntil
      ? { waitUntil: fn => request.cf.waitUntil(fn) }
      : null;

    // Disparar el workflow de GitHub Actions
    const triggerResult = await triggerWorkflow(env.GITHUB_TOKEN);

    if (triggerResult.ok) {
      return slackResponse(
        "▶️ *Bot BA Colaborativa arrancando!*\nEn ~5 minutos llega la notificación de resultado. :hourglass_flowing_sand:"
      );
    } else {
      return slackResponse(
        `❌ No pude arrancar el bot (GitHub respondió ${triggerResult.status}).\nRevisá los logs en GitHub Actions.`,
        true // ephemeral = solo lo ve quien escribió el comando
      );
    }
  },
};

async function triggerWorkflow(githubToken) {
  const url = `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${githubToken}`,
      "Accept": "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "slack-trigger-worker/1.0",
    },
    body: JSON.stringify({ ref: GITHUB_BRANCH }),
  });
  return { ok: resp.status === 204, status: resp.status };
}

function slackResponse(text, ephemeral = false) {
  return new Response(
    JSON.stringify({
      response_type: ephemeral ? "ephemeral" : "in_channel",
      text,
    }),
    { headers: { "Content-Type": "application/json" } }
  );
}

async function verifySlackSignature(secret, timestamp, body, signature) {
  if (!secret || !timestamp || !signature) return false;

  // Rechazar si el timestamp tiene más de 5 minutos (replay attack)
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - parseInt(timestamp)) > 300) return false;

  const sigBase = `v0:${timestamp}:${body}`;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(sigBase));
  const expected = "v0=" + Array.from(new Uint8Array(mac))
    .map(b => b.toString(16).padStart(2, "0"))
    .join("");

  return expected === signature;
}
