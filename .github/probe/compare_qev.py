"""Temporary: our SMTP verifier vs QuickEmailVerification on 90 Triaged-pass games.
Read-only: nothing is cached or written to the database. Prints masked addresses."""
import collections, json, os, random, sys, time
sys.path.insert(0, "."); sys.path.insert(0, "Claude_Lead_Discovery_Engine")
from contextlib import closing
import pipeline
from Email_Verifier.smtp_check import SMTPVerifier
from Email_Verifier.client import QuickEmailVerification, QEVError

def mask(e):
    local, _, dom = e.partition("@"); return f"{local[:2]}***@{dom}"

with closing(pipeline._ro()) as c:
    cached = {r[0] for r in c.execute("SELECT email FROM email_verification_cache")}
    rows = c.execute("SELECT appid, emails FROM scrape_tracker WHERE Mail_status='Triaged' "
                     "AND triage_decision='pass' AND scrape_status IN ('seeded','scraped') ORDER BY appid").fetchall()
pool = [(a, pipeline.normalize_email(e)) for a, e in rows]
pool = [(a, e) for a, e in pool if e and e.casefold() not in cached]
random.seed(20261110)
sample = random.sample(pool, 90)
print(f"pool {len(pool)} uncached; sample {len(sample)}")

smtp = SMTPVerifier(mail_from=os.environ["GMAIL_USER"], timeout=15)
qev = QuickEmailVerification.from_env(timeout=60)
out, grid = [], collections.Counter()
qev_stop = None
for appid, email in sample:
    t = time.time(); s = smtp.verify(email); ts = time.time() - t
    if qev_stop is None:
        try:
            q = qev.verify(email)
        except QEVError as exc:
            qev_stop = str(exc); q = {"result": "error", "reason": str(exc)}
    else:
        q = {"result": "skipped", "reason": qev_stop}
    qr = str(q.get("result", "")).lower()
    grid[(qr, s["result"])] += 1
    out.append({"appid": appid, "email": mask(email), "smtp": s["result"], "smtp_reason": s["reason"],
                "mx": s.get("mx", ""), "smtp_s": round(ts, 1), "qev": qr, "qev_reason": q.get("reason", ""),
                "qev_accept_all": q.get("accept_all"), "qev_disposable": q.get("disposable"),
                "qev_role": q.get("role"), "qev_safe": q.get("safe_to_send")})
    r = out[-1]
    print(f"{appid:>8} {r['email']:38} qev={qr:8} acc_all={str(r['qev_accept_all']):6} | smtp={s['result']:8} "
          f"{s['reason'][:70]} [{r['mx'][:30]}] {r['smtp_s']}s")
print("\n(QEV, ours) -> count")
for k, n in sorted(grid.items()): print(k, n)
print("JSON:" + json.dumps(out))
