// SRE review app — the entire backend, one Cloudflare Worker. No npm, no build step.
//
// The static page (public/index.html) is served by Workers Assets from this same
// deployment; every click on the page is optimistic (instant) and lands here in
// the background.
//
//   GET  /api/state   -> everything needed to render all four views (1 DB round-trip + 2 R2 gets)
//   POST /api/act     -> {action, ...} — accept / reject / reject_all / approve / approve_all /
//                        keep / reject_irrelevant / reject_all_irrelevant / trigger
//   GET  /media/<key> -> R2 passthrough (images + manifests), same-origin so no CORS/bot-block
//
// Secrets (wrangler secret put): TURSO_URL, TURSO_TOKEN, AUTH_SECRET, GH_REPO, GH_PAT.
// Bindings (wrangler.jsonc): MEDIA -> the existing sre-media R2 bucket.
//
// SQL here mirrors Claude_Lead_Discovery_Engine/pipeline.py exactly (same tables, same
// status strings); R2 layout mirrors Leads_Reviewer/media_store.py (index.json,
// irrelevant.json, <GameName>_<appid>/manifest.json + images).

const WORKFLOWS = { send: "send.yml", draft: "draft.yml" }; // the only ones the UI may fire
const encoder = new TextEncoder();

async function digest(value) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)));
}

async function secureEqual(provided, expected) {
  if (!provided || !expected) return false;
  const [a, b] = await Promise.all([digest(provided), digest(expected)]);
  if (typeof crypto.subtle.timingSafeEqual === "function")
    return crypto.subtle.timingSafeEqual(a, b);
  let mismatch = 0; // Node's WebCrypto lacks timingSafeEqual; Workers uses the branch above.
  for (let i = 0; i < a.length; i++) mismatch |= a[i] ^ b[i];
  return mismatch === 0;
}

async function sessionToken(secret) {
  return Array.from(await digest(`sre-session:${secret}`),
    (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function cookie(req, name) {
  const prefix = `${name}=`;
  return (req.headers.get("Cookie") || "").split(";").map((part) => part.trim())
    .find((part) => part.startsWith(prefix))?.slice(prefix.length) || "";
}

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
    headers: { Authorization: `Bearer ${env.TURSO_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ requests }),
  });
  if (!r.ok) throw new Error(`turso http ${r.status}: ${await r.text()}`);
  const { results } = await r.json();
  if (atomic && stmts.length > 1) {
    const res = results[0];
    if (res.type === "error") throw new Error(res.error?.message || "turso batch error");
    const batch = res.response?.result || {};
    const errors = (batch.step_errors || []).slice(1, stmts.length + 1);
    const failed = errors.find(Boolean);
    if (failed) throw new Error(failed.message || "turso statement error");
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

// Delete objects first so a failed purge remains indexed and retryable.
async function purgeMedia(env, appids) {
  const index = (await getJSON(env, "index.json")) || {};
  const targets = new Map(appids.map((appid) => [String(appid), index[String(appid)]]));
  for (const folder of new Set([...targets.values()].filter(Boolean))) {
    let cursor;
    do {
      const l = await env.MEDIA.list({ prefix: folder + "/", cursor });
      if (l.objects.length) await env.MEDIA.delete(l.objects.map((o) => o.key));
      cursor = l.truncated ? l.cursor : undefined;
    } while (cursor);
  }
  await updateJSON(env, "index.json", {}, (current) => {
    const next = { ...current };
    for (const [appid, folder] of targets)
      if (next[appid] === folder) delete next[appid];
    return next;
  });
}

// ---- the four-view snapshot the page renders from ----
async function state(env) {
  const [index, irrelevant, db] = await Promise.all([
    getJSON(env, "index.json"),
    getJSON(env, "irrelevant.json"),
    sql(env, [
      // has-email states only ('pending'/'no_email'/'failed' belong in No-Mail or
      // nowhere yet — a bare Mail_status check let those leak in here too, GH-bug).
      ["SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' AND scrape_status IN ('seeded','scraped') ORDER BY appid"],
      ["SELECT appid, emails FROM scrape_tracker WHERE Mail_status='Drafted' ORDER BY appid"],
      [NOMAIL_SQL],
      ["SELECT EXISTS(SELECT 1 FROM scrape_tracker WHERE Mail_status='Scheduled')"],
      // accepted but the drafter hasn't written the mail yet — gates the Draft button
      ["SELECT EXISTS(SELECT 1 FROM scrape_tracker WHERE Mail_status='Writing')"],
      [TRIAGE_KEPT_SQL],
    ]),
  ]);
  const [pending, drafted, nomail, sched, writing, kept] = db;
  const keptIds = new Set(kept.map((r) => Number(r[0])));
  const triage = (irrelevant || []).map(Number).filter((a) => !keptIds.has(a));
  const flagged = new Set(triage);
  return {
    index: index || {},
    triage,
    approval: pending.map((r) => Number(r[0])).filter((a) => !flagged.has(a)),
    mail: drafted.map((r) => ({ appid: Number(r[0]), emails: r[1] || "" })),
    nomail: nomail.map((r) => Number(r[0])).filter((a) => !flagged.has(a)),
    scheduled: Number(sched[0]?.[0]) === 1,
    pendingDrafts: Number(writing[0]?.[0]) === 1,
  };
}

// ---- actions (all optimistic on the client; errors surface in its banner) ----
const approveStmt = (appid) => [
  "UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid=? AND Mail_status='Drafted' AND scrape_status IN ('seeded','scraped')",
  appid,
];

const NOMAIL_SQL = "SELECT appid FROM scrape_tracker WHERE scrape_status IN ('pending','no_email','failed') AND Mail_status='Pending' ORDER BY appid";
const TRIAGE_KEPT_SQL = "SELECT appid FROM scrape_tracker WHERE triage_kept=1";

const keepStmt = (appid) => [
  "UPDATE scrape_tracker SET triage_kept=1 WHERE appid=? AND Mail_status='Pending'",
  appid,
];

const deleteStmts = (appid) => [
  ["DELETE FROM scrape_tracker WHERE appid=?", appid],
  ["DELETE FROM newly_added WHERE appid=?", appid],
];

async function dropIrrelevant(env, appids) {
  const gone = new Set(appids.map(Number));
  await updateJSON(env, "irrelevant.json", [], (current) =>
    current.filter((a) => !gone.has(Number(a))));
}

function oneId(body) {
  const value = Number(body.appid);
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error("invalid appid");
  return value;
}

function manyIds(body) {
  if (!Array.isArray(body.appids) || body.appids.length > 500)
    throw new Error("appids must be an array of at most 500 values");
  return [...new Set(body.appids.map((value) => {
    value = Number(value);
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error("invalid appid");
    return value;
  }))];
}

async function act(env, body) {
  switch (body.action) {
    case "accept": { // Game Approval ✅ -> Mail_status 'Writing'
      const appid = oneId(body);
      await sql(env, [["UPDATE scrape_tracker SET Mail_status='Writing' WHERE appid=? AND Mail_status='Pending' AND scrape_status IN ('seeded','scraped')", appid]]);
      break;
    }
    case "reject": { // ❌ anywhere -> delete both tables + purge media
      const appid = oneId(body);
      await purgeMedia(env, [appid]);
      await sql(env, deleteStmts(appid), true);
      break;
    }
    case "reject_all": { // No-Mail bulk purge — same as reject, just no irrelevant.json involved
      const appids = manyIds(body);
      await purgeMedia(env, appids);
      await sql(env, appids.flatMap(deleteStmts), true);
      break;
    }
    case "approve": { // Mail Approval ✅ -> 'Scheduled'
      const appid = oneId(body);
      await sql(env, [approveStmt(appid)]);
      break;
    }
    case "approve_all": {
      const appids = manyIds(body);
      await sql(env, appids.map(approveStmt), true);
      break;
    }
    case "keep": { // Triage: persist human Keep, then unflag into the normal queue
      const appid = oneId(body);
      await sql(env, [keepStmt(appid)]);
      await dropIrrelevant(env, [appid]);
      break;
    }
    case "reject_irrelevant": { // Triage ❌: unflag + full delete
      const appid = oneId(body);
      await purgeMedia(env, [appid]);
      await dropIrrelevant(env, [appid]);
      await sql(env, deleteStmts(appid), true);
      break;
    }
    case "reject_all_irrelevant": {
      const appids = manyIds(body);
      await purgeMedia(env, appids);
      await dropIrrelevant(env, appids);
      await sql(env, appids.flatMap(deleteStmts), true);
      break;
    }
    case "trigger": { // fire a GHA workflow (send / draft) on master
      const file = WORKFLOWS[body.workflow];
      if (!file) throw new Error(`unknown workflow ${body.workflow}`);
      if (!env.GH_REPO) throw new Error("GH_REPO secret is not set");
      if (!env.GH_PAT) throw new Error("GH_PAT secret is not set");
      const r = await fetch(
        `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/${file}/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${env.GH_PAT}`,
            Accept: "application/vnd.github+json",
            "User-Agent": "sre-review-worker",
          },
          body: JSON.stringify({ ref: env.GH_REF || "master" }),
        },
      );
      if (r.status !== 204) throw new Error(`github ${r.status}: ${await r.text()}`);
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
  approveStmt, keepStmt, NOMAIL_SQL, TRIAGE_KEPT_SQL, purgeMedia, secureEqual,
  sessionToken, sql, updateJSON,
};

export default {
  async fetch(req, env) {
    const path = new URL(req.url).pathname;
    const headerAuth = await secureEqual(req.headers.get("x-auth") || "", env.AUTH_SECRET);
    const cookieAuth = env.AUTH_SECRET ? await secureEqual(
      cookie(req, "sre_session"), await sessionToken(env.AUTH_SECRET)) : false;
    const authorized = headerAuth || cookieAuth;

    // Authenticated R2 passthrough. The browser receives its HttpOnly session below.
    if (path.startsWith("/media/")) {
      if (!authorized) return new Response("unauthorized", { status: 401 });
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
          "Cache-Control": key.endsWith(".json") ? "no-store" : "public, max-age=86400",
        },
      });
    }

    if (path.startsWith("/api/")) {
      if (!authorized) return json({ error: "unauthorized" }, 401);
      let response;
      try {
        if (path === "/api/state") response = json(await state(env));
        if (path === "/api/act" && req.method === "POST")
          response = json(await act(env, await req.json()));
        response ||= json({ error: "not found" }, 404);
      } catch (e) {
        response = json({ error: String((e && e.message) || e) }, 500);
      }
      if (headerAuth) response.headers.set(
        "Set-Cookie",
        `sre_session=${await sessionToken(env.AUTH_SECRET)}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=604800`,
      );
      return response;
    }

    // "/" and static files are served by Workers Assets before this handler runs.
    return new Response("not found", { status: 404 });
  },
};

