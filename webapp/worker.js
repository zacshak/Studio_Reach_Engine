// SRE review app — the entire backend, one Cloudflare Worker. No npm, no build step.
//
// Replaces the Streamlit review UI's server side. The static page (public/index.html)
// is served by Workers Assets from this same deployment; every click on the page is
// optimistic (instant) and lands here in the background.
//
//   GET  /api/state   -> everything needed to render all four views (1 DB round-trip + 2 R2 gets)
//   POST /api/act     -> {action, ...} — accept / reject / approve / approve_all / keep /
//                        reject_irrelevant / reject_all_irrelevant / trigger
//   GET  /media/<key> -> R2 passthrough (images + manifests), same-origin so no CORS/bot-block
//
// Secrets (wrangler secret put): TURSO_URL, TURSO_TOKEN, AUTH_SECRET, GH_PAT.
// Bindings (wrangler.jsonc): MEDIA -> the existing sre-media R2 bucket.
//
// SQL here mirrors Claude_Lead_Discovery_Engine/pipeline.py exactly (same tables, same
// status strings); R2 layout mirrors Leads_Reviewer/media_store.py (index.json,
// irrelevant.json, <GameName>_<appid>/manifest.json + images).

const GH_REPO = "Meshak2002/Studio_Reach_Engine";
const WORKFLOWS = { send: "send.yml", draft: "draft.yml" }; // the only ones the UI may fire

// ---- Turso over HTTP (Hrana v2 pipeline; one fetch = N statements, no client lib) ----
async function sql(env, stmts) {
  const url = env.TURSO_URL.replace(/^libsql:/, "https:").replace(/\/$/, "") + "/v2/pipeline";
  const requests = stmts.map(([q, ...args]) => ({
    type: "execute",
    stmt: {
      sql: q,
      args: args.map((v) =>
        typeof v === "number"
          ? { type: "integer", value: String(v) }
          : { type: "text", value: String(v) }),
    },
  }));
  requests.push({ type: "close" });
  const r = await fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.TURSO_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify({ requests }),
  });
  if (!r.ok) throw new Error(`turso http ${r.status}: ${await r.text()}`);
  const { results } = await r.json();
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
const putJSON = (env, key, obj) =>
  env.MEDIA.put(key, JSON.stringify(obj), {
    httpMetadata: { contentType: "application/json" },
  });

// Purge each appid's media folder and drop it from index.json (read index once,
// write once — avoids the read-modify-write race media_store.delete_lead_media has
// when two rejects land close together).
async function purgeMedia(env, appids) {
  const index = (await getJSON(env, "index.json")) || {};
  let dirty = false;
  for (const a of appids) {
    const folder = index[String(a)];
    if (!folder) continue;
    delete index[String(a)];
    dirty = true;
    let cursor;
    do {
      const l = await env.MEDIA.list({ prefix: folder + "/", cursor });
      if (l.objects.length) await env.MEDIA.delete(l.objects.map((o) => o.key));
      cursor = l.truncated ? l.cursor : undefined;
    } while (cursor);
  }
  if (dirty) await putJSON(env, "index.json", index);
}

// ponytail: module-level chain serializes index.json writers within one isolate —
// enough for a single-user app; move to a Durable Object if it ever multi-users.
let purgeChain = Promise.resolve();
function queuePurge(env, ctx, appids) {
  purgeChain = purgeChain
    .then(() => purgeMedia(env, appids))
    .catch((e) => console.error("media purge failed", e));
  ctx.waitUntil(purgeChain);
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
      ["SELECT appid FROM scrape_tracker WHERE scrape_status IN ('no_email','failed') ORDER BY appid"],
      ["SELECT EXISTS(SELECT 1 FROM scrape_tracker WHERE Mail_status='Scheduled')"],
      // accepted but the drafter hasn't written the mail yet — gates the Draft button
      ["SELECT EXISTS(SELECT 1 FROM scrape_tracker WHERE Mail_status='Writing')"],
    ]),
  ]);
  const [pending, drafted, nomail, sched, writing] = db;
  const triage = (irrelevant || []).map(Number);
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
async function approveStmts(env, index, appid) {
  const folder = index[String(appid)];
  const m = folder ? await getJSON(env, `${folder}/manifest.json`) : null;
  const stmts = [];
  if (m && m.mail_template != null)
    stmts.push(["UPDATE scrape_tracker SET mail_template=? WHERE appid=?", m.mail_template, appid]);
  stmts.push(["UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid=?", appid]);
  return stmts;
}

const deleteStmts = (appid) => [
  ["DELETE FROM scrape_tracker WHERE appid=?", appid],
  ["DELETE FROM newly_added WHERE appid=?", appid],
];

async function dropIrrelevant(env, appids) {
  const gone = new Set(appids.map(Number));
  const keep = ((await getJSON(env, "irrelevant.json")) || []).filter((a) => !gone.has(Number(a)));
  await putJSON(env, "irrelevant.json", keep);
}

async function act(env, ctx, body) {
  const appid = Number(body.appid);
  const appids = (body.appids || []).map(Number);
  switch (body.action) {
    case "accept": // Game Approval ✅ -> Mail_status 'Writing'
      await sql(env, [["UPDATE scrape_tracker SET Mail_status='Writing' WHERE appid=?", appid]]);
      break;
    case "reject": // ❌ anywhere -> delete both tables + purge media
      await sql(env, deleteStmts(appid));
      queuePurge(env, ctx, [appid]);
      break;
    case "approve": { // Mail Approval ✅ -> record template + 'Scheduled'
      const index = (await getJSON(env, "index.json")) || {};
      await sql(env, await approveStmts(env, index, appid));
      break;
    }
    case "approve_all": {
      const index = (await getJSON(env, "index.json")) || {};
      const nested = await Promise.all(appids.map((a) => approveStmts(env, index, a)));
      await sql(env, nested.flat());
      break;
    }
    case "keep": // Triage: AI was wrong — unflag, lead rejoins the normal queue
      await dropIrrelevant(env, [appid]);
      break;
    case "reject_irrelevant": // Triage ❌: unflag + full delete
      await dropIrrelevant(env, [appid]);
      await sql(env, deleteStmts(appid));
      queuePurge(env, ctx, [appid]);
      break;
    case "reject_all_irrelevant":
      await dropIrrelevant(env, appids);
      await sql(env, appids.flatMap(deleteStmts));
      queuePurge(env, ctx, appids);
      break;
    case "trigger": { // fire a GHA workflow (send / draft) on master
      const file = WORKFLOWS[body.workflow];
      if (!file) throw new Error(`unknown workflow ${body.workflow}`);
      const r = await fetch(
        `https://api.github.com/repos/${GH_REPO}/actions/workflows/${file}/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${env.GH_PAT}`,
            Accept: "application/vnd.github+json",
            "User-Agent": "sre-review-worker",
          },
          body: JSON.stringify({ ref: "master" }),
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

export default {
  async fetch(req, env, ctx) {
    const path = new URL(req.url).pathname;

    // R2 passthrough. No auth: the bucket is already world-readable via its r2.dev URL.
    if (path.startsWith("/media/")) {
      const key = decodeURIComponent(path.slice("/media/".length));
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
      if (req.headers.get("x-auth") !== env.AUTH_SECRET)
        return json({ error: "unauthorized" }, 401);
      try {
        if (path === "/api/state") return json(await state(env));
        if (path === "/api/act" && req.method === "POST")
          return json(await act(env, ctx, await req.json()));
        return json({ error: "not found" }, 404);
      } catch (e) {
        return json({ error: String((e && e.message) || e) }, 500);
      }
    }

    // "/" and static files are served by Workers Assets before this handler runs.
    return new Response("not found", { status: 404 });
  },
};
