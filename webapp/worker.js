// SRE review app — the entire backend, one Cloudflare Worker. No npm, no build step.
//
// The page (public/index.html) is served by entry.js, the deployed entry point, which
// wraps this handler; review actions update the page after server confirmation.
//
//   GET  /api/state   -> Home counts + all review views (1 DB round-trip + 1 R2 get)
//   POST /api/act     -> {action, ...} — triage_pass / pass / flag / accept / accept_many /
//                        verify_status /
//                        reject / reject_all / approve / approve_all / trigger
//                        (409 + {refused: true} when the game's state does not allow it)
//   POST /api/triage/save -> {criteria, results} AI recommendations (MCP submit_triage)
//   GET  /media/<key> -> R2 passthrough (images + manifests), same-origin so no CORS/bot-block
//
// Public dashboard, including review actions and workflow dispatch (owner requested).
// Secrets (wrangler secret put): TURSO_URL, TURSO_TOKEN, GH_REPO, GH_PAT.
// Bindings (wrangler.jsonc): MEDIA -> the existing sre-media R2 bucket.
//
// SQL here mirrors Claude_Lead_Discovery_Engine/pipeline.py exactly (same tables, same
// status strings); R2 layout mirrors Leads_Reviewer/media_store.py (index.json,
// <GameName>_<appid>/manifest.json + images). Triage lives in scrape_tracker.

import { triageSnapshot, saveTriage, setDecision, TRIAGE_SQL, TRIAGE_PASS_SQL } from './triage.js';
import { rejectRows, cleanupJobs } from './deletion.js';
import { draftRequest } from './drafting.js';
import { queueSocialsStatement, saveSocials, readSocialRequestBody } from './socials.js';
import { queues, keepMany, acceptOne, acceptMany, startVerification, dispatchWorkflow, verifyStatus, parseIssue, approveMany, returnToAccepted, saveEmail, triageDetails, workflowRuns, restoreGames, restoreStatus, Refused } from './agent-actions.js';

// ---- Turso over HTTP (Hrana v2 pipeline; one fetch = N statements, no client lib) ----
const hranaStmt = ([q, ...args]) => ({
  sql: q,
  args: args.map((v) =>
    typeof v === "number"
      ? { type: "integer", value: String(v) }
      : { type: "text", value: String(v) }),
});

async function sql(env, stmts, atomic = false) {
  if (!stmts.length) return [];
  const url = env.TURSO_URL.replace(/^libsql:/, "https:").replace(/\/$/, "") + "/v2/pipeline";
  let requests;
  if (atomic && stmts.length > 1) {
    const steps = [{ stmt: { sql: "BEGIN IMMEDIATE" } }];
    stmts.forEach((statement, i) => steps.push({
      condition: { type: "ok", step: i },
      stmt: hranaStmt(statement),
    }));
    steps.push({ condition: { type: "ok", step: stmts.length }, stmt: { sql: "COMMIT" } });
    steps.push({
      condition: {
        type: "or",
        conds: [...stmts.map((_, i) => ({ type: "error", step: i + 1 })),
                { type: "error", step: stmts.length + 1 }],
      },
      stmt: { sql: "ROLLBACK" },
    });
    requests = [{ type: "batch", batch: { steps } }];
  } else {
    requests = stmts.map((statement) => ({ type: "execute", stmt: hranaStmt(statement) }));
  }
  requests.push({ type: "close" });
  const r = await fetch(url, {
    method: "POST",
    signal: AbortSignal.timeout(25000),
    headers: { Authorization: `Bearer ${env.TURSO_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ requests }),
  });
  if (!r.ok) throw new Error(`turso http ${r.status}: ${await r.text()}`);
  const { results } = await r.json();
  if (atomic && stmts.length > 1) {
    const res = results[0];
    if (res.type === "error") throw new Error(res.error?.message || "turso batch error");
    const batch = res.response?.result || {};
    const errors = (batch.step_errors || []);
    const failed = errors.find(Boolean);
    if (failed) throw new Error(failed.message || "turso statement error");
    if (!batch.step_results?.[0] || !batch.step_results?.[stmts.length + 1])
      throw new Error('turso transaction did not confirm BEGIN and COMMIT');
    return (batch.step_results || []).slice(1, stmts.length + 1).map((result) =>
      (result?.rows || []).map((row) => row.map((cell) => cell.value)));
  }
  return results.slice(0, stmts.length).map((res) => {
    if (res.type === "error") throw new Error(res.error?.message || "turso error");
    const rows = res.response?.result?.rows || [];
    return rows.map((row) => row.map((cell) => cell.value)); // cells arrive as {type,value}
  });
}

// ---- R2 helpers (mirror media_store.py's layout) ----
const getJSON = async (env, key) => {
  const o = await env.MEDIA.get(key);
  return o ? o.json() : null;
};
async function updateJSON(env, key, fallback, mutate) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const object = await env.MEDIA.get(key);
    const current = object ? await object.json() : fallback;
    const written = await env.MEDIA.put(key, JSON.stringify(mutate(current)), {
      onlyIf: object ? { etagMatches: object.etag } : { etagDoesNotMatch: "*" },
      httpMetadata: { contentType: "application/json" },
    });
    if (written) return;
  }
  throw new Error(`concurrent R2 update did not settle for ${key}`);
}


// ---- the snapshot the page renders from: Home counts + every view ----
const HAS_EMAIL = "scrape_status IN ('seeded','scraped')";
export const VIEW_SQL = {
  pending: "SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' ORDER BY appid",
  passed: "SELECT appid FROM scrape_tracker WHERE Mail_status='Triaged' AND triage_decision='pass' ORDER BY appid",
  flagged: "SELECT appid FROM scrape_tracker WHERE Mail_status='Triaged' AND triage_decision='flag' ORDER BY appid",
  // games not yet accepted that have no usable email (badged on their cards)
  noMail: `SELECT appid FROM scrape_tracker WHERE Mail_status IN ('Pending','Triaged') AND NOT (${HAS_EMAIL}) ORDER BY appid`,
  // why a game's last email was dropped (failed verification or bounce), for its badge
  issues: "SELECT appid, email_issue, game_name FROM scrape_tracker WHERE Mail_status IN ('Pending','Triaged') AND email_issue IS NOT NULL ORDER BY appid",
  verifying: "SELECT appid FROM scrape_tracker WHERE Mail_status='Verifying' ORDER BY appid",
  counts: `SELECT Mail_status, coalesce(triage_decision,''), ${HAS_EMAIL}, count(*) FROM scrape_tracker GROUP BY 1, 2, 3`,
  // sending accounts: daily cap (rolling 24h), mails sent in the last 24h, last status from the mailer
  senders: `SELECT a.email, a.provider, a.daily_cap, a.last_status, a.last_checked_at,
    (SELECT count(*) FROM mail_send_log l WHERE l.sender=a.email AND l.sent_at >= datetime('now','-1 day'))
    FROM sender_accounts a ORDER BY a.daily_cap DESC, a.email`,
  // Steam release date shown on each review card, next to the appid
  releases: "SELECT appid, release_date FROM scrape_tracker WHERE Mail_status IN ('Pending','Triaged','Drafted') AND coalesce(release_date,'')<>'' ORDER BY appid",
  // developers of each card with how many games they have on Steam (developer_games is
  // filled by the nightly discovery run; NULL = not looked up yet). Same name expression
  // as Claude_Lead_Discovery_Engine/dev_counts.py.
  developers: `SELECT s.appid, trim(j.value), g.games FROM scrape_tracker s
    LEFT JOIN newly_added n ON n.appid = s.appid,
    json_each(CASE WHEN json_valid(n.developers) AND json_type(n.developers) = 'array'
                   THEN n.developers ELSE json_array(coalesce(s.developers, '')) END) j
    LEFT JOIN developer_games g ON g.developer = trim(j.value)
    WHERE s.Mail_status IN ('Pending','Triaged','Drafted') AND trim(j.value) <> ''
    ORDER BY s.appid, j.key`,
};

// rows [appid, developer, games] -> { appid: [{ name, games }] } in Steam's order
export function developersByApp(rows) {
  const out = {};
  for (const [appid, name, games] of rows || []) {
    const n = games == null || games === '' ? null : Number(games);
    (out[Number(appid)] ||= []).push({ name: String(name), games: Number.isFinite(n) ? n : null });
  }
  return out;
}

export function homeCounts(rows) {
  const c = { pending: 0, triaged: 0, passed: 0, flagged: 0, no_mail: 0, verifying: 0, accepted: 0, drafted: 0,
    scheduled: 0, sending: 0, sent: 0, replied: 0, invalid: 0 };
  for (const [status, decision, hasEmail, n] of rows) {
    const count = Number(n) || 0;
    const key = String(status).toLowerCase();
    if (key in c && key !== 'triaged') c[key] += count;
    if (status === 'Triaged') {
      c.triaged += count;
      if (decision === 'pass') c.passed += count;
      if (decision === 'flag') c.flagged += count;
    }
    if ((status === 'Pending' || status === 'Triaged') && !Number(hasEmail)) c.no_mail += count;
  }
  return c;
}

async function state(env) {
  const [index, db] = await Promise.all([
    getJSON(env, "index.json"),
    sql(env, [
      [VIEW_SQL.pending], [VIEW_SQL.passed], [VIEW_SQL.flagged], [VIEW_SQL.noMail],
      ["SELECT appid, emails FROM scrape_tracker WHERE Mail_status='Drafted' ORDER BY appid"],
      [VIEW_SQL.counts],
      [TRIAGE_SQL],
      [VIEW_SQL.issues],
      [VIEW_SQL.verifying],
      [VIEW_SQL.senders],
      [VIEW_SQL.releases],
      [VIEW_SQL.developers],
    ]),
  ]);
  const [pending, passed, flagged, noMail, drafted, counts, reviews, issues, verifying, senders, releases, developers] = db;
  const ids = rows => rows.map(r => Number(r[0]));
  const home = homeCounts(counts);
  return {
    index: index || {},
    counts: home,
    pending: ids(pending),
    passed: ids(passed),
    flagged: ids(flagged),
    noMail: ids(noMail),
    verifying: ids(verifying),
    releases: Object.fromEntries((releases || []).map(([appid, date]) => [Number(appid), String(date)])),
    developers: developersByApp(developers),
    issues: Object.fromEntries(issues.map(([appid, raw, game]) => [Number(appid), { ...parseIssue(raw), game: game ?? null }])),
    mail: drafted.map((r) => ({ appid: Number(r[0]), emails: r[1] || "" })),
    triageReviews: triageSnapshot(reviews).triageReviews,
    scheduled: home.scheduled > 0,
    pendingDrafts: home.accepted > 0,
    senders: senders.map(([email, provider, cap, status, checked, sent]) => ({ email, provider,
      daily_cap: Number(cap) || 0, sent_24h: Number(sent) || 0, status: status ?? null, checked_at: checked ?? null })),
  };
}

// ---- confirmed actions ----
const approveStmt = (appid) => [
  "UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid=? AND Mail_status='Drafted' AND scrape_status IN ('seeded','scraped')",
  appid,
];







async function rejectGames(env, appids) {
  return rejectRows(env, appids, sql, getJSON);
}

function oneId(body) {
  const value = Number(body.appid);
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error("invalid appid");
  return value;
}

function manyIds(body) {
  if (!Array.isArray(body.appids) || body.appids.length > 10000)
    throw new Error("appids must be an array of at most 10000 values per request");
  return [...new Set(body.appids.map((value) => {
    value = Number(value);
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error("invalid appid");
    return value;
  }))];
}

async function act(env, body) {
  switch (body.action) {
    case "triage_pass": { // Pending view ✅: Pending -> Triaged as pass
      const appid = oneId(body);
      const [moved] = await sql(env, [[TRIAGE_PASS_SQL, appid]]);
      if (!moved.length) throw new Refused(`Cannot pass ${appid}: it is no longer Pending.`);
      break;
    }
    case "pass": case "keep": case "flag": { // Flagged ✅ Pass / Passed 🚩 Flag
      const appid = oneId(body);
      const target = body.action === 'flag' ? 'flag' : 'pass';
      const { changed } = await setDecision(env, [appid], target, sql);
      if (!changed.length) throw new Refused(`Cannot ${target} ${appid}: it is not a ${target === 'pass' ? 'flagged' : 'passed'} Triaged game.`);
      break;
    }
    case "accept": { // Passed ✅ -> 'Verifying'; verify.yml moves it to Accepted or back
      const appid = oneId(body);
      try { await acceptOne(env, appid, sql); }
      catch (error) { throw error.refused ? new Refused(`Cannot accept ${appid}: ${error.message}.`) : error; }
      await startVerification(env, [appid], sql);
      return { ok: true, verifying: [appid] };
    }
    case "verify_status": return verifyStatus(env, body, sql);
    case "reject": { // ❌ anywhere -> delete both tables + purge media
      const appid = oneId(body);
      return rejectGames(env, [appid]);
    }
    case "reject_all": { // No-Mail bulk purge — same as reject
      const appids = manyIds(body);
      return rejectGames(env, appids);
    }
    case "approve": { // Mail Approval ✅ -> 'Scheduled'
      const appid = oneId(body);
      await sql(env, [approveStmt(appid)]);
      break;
    }
    case "keep_many": return keepMany(env, body, sql);
    case "accept_many": return acceptMany(env, body, sql);
    case "restore": return restoreGames(env, body, sql);
    case "restore_status": return restoreStatus(env, body, sql);
    case "approve_many": return approveMany(env, body, sql);
    case "return_to_accepted": return returnToAccepted(env, body, sql, getJSON);
    case "approve_all": {
      const appids = manyIds(body);
      await sql(env, [["UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid IN (SELECT value FROM json_each(?)) AND Mail_status='Drafted' AND scrape_status IN ('seeded','scraped')", JSON.stringify(appids)]]);
      break;
    }
    case "reject_irrelevant": { // Triage ❌: full delete (drops the recommendation with the row)
      const appid = oneId(body);
      return rejectGames(env, [appid]);
    }
    case "reject_all_irrelevant": {
      const appids = manyIds(body);
      return rejectGames(env, appids);
    }
    case "trigger": { // fire a GHA workflow (send / draft / verify ...) on master
      if (body.workflow === 'send') {
        const [queued] = await sql(env, [queueSocialsStatement()]);
        console.log(JSON.stringify({event:'social_requests_queued',count:queued.length}));
      }
      await dispatchWorkflow(env, body.workflow);
      break;
    }
    default:
      throw new Error(`unknown action ${body.action}`);
  }
  return { ok: true };
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });

export {
  approveStmt,
  sql, updateJSON,
};

export default {
  async scheduled(_event, env, ctx) {
    if (env.ROLLOUT_PAUSED === 'true') return;
    ctx.waitUntil(cleanupJobs(env, sql, updateJSON).catch(error => {
      console.error(JSON.stringify({ event: 'media_cleanup_failed', message: error.message }));
      throw error;
    }));
  },
  async fetch(req, env, ctx) {
    const path = new URL(req.url).pathname;
    if (env.ROLLOUT_PAUSED === 'true' && req.method === 'POST' && path.startsWith('/api/'))
      return json({error:'Deployment in progress. Please retry shortly.'},503);
    // Public R2 passthrough.
    if (path.startsWith("/media/")) {
      let key;
      try {
        key = decodeURIComponent(path.slice("/media/".length));
      } catch {
        return new Response("invalid media path", { status: 400 });
      }
      const obj = await env.MEDIA.get(key);
      if (!obj) return new Response("not found", { status: 404 });
      return new Response(obj.body, {
        headers: {
          "Content-Type": obj.httpMetadata?.contentType || "application/octet-stream",
          // manifests change (drafts get written); images are immutable per lead
          "Cache-Control": key.endsWith(".json") ? "no-store" : /\/(?:preview|thumb)-[a-f0-9]{32}\.webp$/.test(key)
            ? "public, max-age=31536000, immutable" : "public, max-age=86400",
        },
      });
    }

    if (path.startsWith("/api/")) {
      let response;
      try {
        if (req.method === 'POST' && path === '/api/socials/save')
          response = json(await saveSocials(env, await readSocialRequestBody(req), sql));
        if (req.method === 'POST' && ['/api/draft/context','/api/draft/save','/api/draft/list'].includes(path))
          response = json(await draftRequest(req, env, sql, path.split('/').pop()));
        if (req.method === 'POST' && path === '/api/triage/save')
          response = json(await saveTriage(env, await req.json(), sql, getJSON));
        if (req.method === 'POST' && path === '/api/triage/get')
          response = json(await triageDetails(env, await req.json(), sql));
        if (req.method === 'POST' && path === '/api/queues') response = json(await queues(env, sql));
        if (req.method === 'POST' && path === '/api/email/save')
          response = json(await saveEmail(env, await req.json(), sql));
        if (req.method === 'POST' && path === '/api/workflows/runs')
          response = json(await workflowRuns(env, await req.json()));
        if (path === "/api/state") response = json(await state(env));
        if (path === "/api/act" && req.method === "POST") {
          const result = await act(env, await req.json());
          response = json(result);
          if (result.cleanup === 'queued' && ctx?.waitUntil) {
            ctx.waitUntil(cleanupJobs(env, sql, updateJSON).catch(error =>
              console.error(JSON.stringify({ event: 'media_cleanup_deferred', message: error.message }))));
          }
        }
        response ||= json({ error: "not found" }, 404);
      } catch (e) {
        if (e?.refused) response = json({ error: e.message, refused: true }, 409);
        else {
          console.error(JSON.stringify({ event: 'api_failed', path, message: String(e.message || e) }));
          response = json({ error: String((e && e.message) || e) }, 500);
        }
      }
      return response;
    }

    // "/" (the page) is served by entry.js before this handler runs.
    return new Response("not found", { status: 404 });
  },
};

