/**
 * wnba-milestones-trigger
 *
 * Cloudflare Worker that pings GitHub repository_dispatch endpoints every
 * minute. Triggers two workflows:
 *   - live_tracker_tick → aderoa/wnba-milestones (every minute)
 *   - injury_pulse_tick → aderoa/wnba-injury-pulse (every 15 min, on the :00/:15/:30/:45 marks)
 *
 * The Worker doesn't poll ESPN or the WNBA injury PDFs itself yet — it
 * dispatches workflows in the existing repos, which keep running their
 * existing track.py / poll_injuries.py logic unchanged. Workers cron is far
 * more reliable than GitHub Actions' built-in cron, hence this layer.
 *
 * Required env (set in Cloudflare Dashboard → Settings → Variables):
 *   GITHUB_OWNER          "aderoa"
 *   GITHUB_TOKEN          (encrypted) GitHub PAT with `repo` scope
 *   MILESTONES_REPO       "wnba-milestones"
 *   INJURY_PULSE_REPO     "wnba-injury-pulse"
 *
 * Cron trigger: every minute (`* * * * *`).
 */

const USER_AGENT = "wnba-milestones-trigger/2.0";
const DISPATCH_TARGETS = [
  // {repoEnvName, eventType, everyMinutes}
  { repoEnvName: "MILESTONES_REPO", eventType: "live_tracker_tick", everyMinutes: 1 },
  { repoEnvName: "INJURY_PULSE_REPO", eventType: "injury_pulse_tick", everyMinutes: 15 },
];

async function dispatch(env, repo, eventType) {
  const owner = env.GITHUB_OWNER;
  const token = env.GITHUB_TOKEN;
  if (!owner || !repo || !token) {
    return { ok: false, status: 0, detail: "Missing GITHUB_OWNER / repo / GITHUB_TOKEN env" };
  }
  const url = `https://api.github.com/repos/${owner}/${repo}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
    },
    body: JSON.stringify({ event_type: eventType }),
  });
  if (resp.status === 204) return { ok: true, status: 204, repo, eventType };
  let detail;
  try { detail = await resp.text(); } catch { detail = "(no body)"; }
  return { ok: false, status: resp.status, repo, eventType, detail };
}

function shouldFire(target, now) {
  // Fire each minute if everyMinutes=1; fire on the :00/:15/:30/:45 marks if =15.
  if (target.everyMinutes <= 1) return true;
  return now.getUTCMinutes() % target.everyMinutes === 0;
}

export default {
  async scheduled(event, env, ctx) {
    const now = new Date();
    const results = [];
    for (const target of DISPATCH_TARGETS) {
      if (!shouldFire(target, now)) continue;
      const repo = env[target.repoEnvName];
      if (!repo) {
        console.log(`Skipping ${target.eventType}: ${target.repoEnvName} env not set`);
        continue;
      }
      try {
        const r = await dispatch(env, repo, target.eventType);
        results.push(r);
        if (r.ok) console.log(`[${event.cron}] dispatched ${r.eventType} → ${r.repo}`);
        else console.error(`[${event.cron}] dispatch failed ${target.eventType} (${r.status}): ${r.detail}`);
      } catch (exc) {
        console.error(`[${event.cron}] exception dispatching ${target.eventType}: ${exc}`);
      }
    }
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/" || url.pathname === "/status") {
      return new Response(JSON.stringify({
        worker: "wnba-milestones-trigger",
        version: 2,
        owner: env.GITHUB_OWNER || "(unset)",
        targets: DISPATCH_TARGETS.map(t => ({
          repo: env[t.repoEnvName] || "(unset)",
          event_type: t.eventType,
          every_minutes: t.everyMinutes,
        })),
        token_configured: !!env.GITHUB_TOKEN,
      }, null, 2), { headers: { "Content-Type": "application/json" } });
    }

    if (url.pathname === "/trigger" && request.method === "POST") {
      // Force-fire all targets right now, regardless of cron schedule
      const results = [];
      for (const target of DISPATCH_TARGETS) {
        const repo = env[target.repoEnvName];
        if (!repo) {
          results.push({ ok: false, target: target.eventType, detail: "repo env not set" });
          continue;
        }
        results.push(await dispatch(env, repo, target.eventType));
      }
      const allOk = results.every(r => r.ok);
      return new Response(JSON.stringify(results, null, 2), {
        status: allOk ? 200 : 502,
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response("Not found", { status: 404 });
  },
};
