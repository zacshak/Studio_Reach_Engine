"""Temporary (run 3): compare our SMTP verdicts with cached QEV verdicts. Prints masked addresses."""
import json, os, random, sys, collections
sys.path.insert(0, "."); sys.path.insert(0, "Claude_Lead_Discovery_Engine")
import pipeline
from contextlib import closing
from Email_Verifier.smtp_check import SMTPVerifier

def mask(e):
    local, _, dom = e.partition("@"); return f"{local[:2]}***@{dom}"

with closing(pipeline._ro()) as c:
    rows = c.execute("SELECT email, result_json FROM email_verification_cache").fetchall()
qev = [(e, json.loads(j)) for e, j in rows]
print("cache rows:", len(qev), collections.Counter(r.get("result") for _, r in qev))
random.seed(7)
inv = [x for x in qev if x[1].get("result") == "invalid"]
val = [x for x in qev if x[1].get("result") == "valid"]
sample = random.sample(inv, min(25, len(inv))) + random.sample(val, min(35, len(val)))
v = SMTPVerifier(mail_from=os.environ["GMAIL_USER"], timeout=15)
me = os.environ["GMAIL_USER"]
extra = [(me, {"result": "valid"}), (f"zz-no-such-{random.randint(10**8,10**9)}@gmail.com", {"result": "invalid"}),
         ("a@this-domain-should-not-exist-sre-7781.com", {"result": "invalid"})]
grid = collections.Counter()
for email, q in extra + sample:
    s = v.verify(email)
    grid[(q.get("result"), s["result"])] += 1
    print(f"{mask(email):40} qev={q.get('result'):8} acc_all={str(q.get('accept_all','')):6} "
          f"smtp={s['result']:8} {s['reason'][:90]} mx={s.get('mx','')}")
print("\n(QEV, SMTP) -> count")
for k, n in sorted(grid.items()): print(k, n)
