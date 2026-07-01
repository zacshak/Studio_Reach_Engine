"""Kimchi's safe DB interface — the ONLY way the kimchi agent touches the database.

The kimchi CLI is a fully-agentic scraper: it drives Playwright itself. But it must NOT
be trusted with a raw DB handle (it could clobber newly_added). So the agent gets exactly
two commands, both routed through scraper_interface -> pipeline (which owns the schema and
keeps newly_added read-only):

    python kimchi_db.py pending
        -> prints one JSON object per line, each:
           {"appid": 123, "studio": "Foo Games", "urls": ["https://foo.gg", ...]}
           (urls = website + Steam support url; empty list means "search the web for it")

    python kimchi_db.py write <appid> <status> [email] [website]
        status in: scraped | no_email | failed
        e.g.  python kimchi_db.py write 123 scraped jobs@foo.gg https://foo.gg
              python kimchi_db.py write 456 no_email

That's the whole contract. The agent scrapes; these two calls read the queue and record
results. Deleting the KimchiScraper folder removes kimchi entirely (pipeline is shared).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scraper_interface as leads  # noqa: E402  (re-exports pipeline; also loads .env)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

VALID = {"scraped", "no_email", "failed"}


def _candidate_urls(lead):
    """Site hints for a lead: its website, then Steam's support url (Steam pages dropped)."""
    urls, support = [], lead.get("support_info") or ""
    if lead.get("website"):
        urls.append(lead["website"])
    m = re.search(r'"url"\s*:\s*"([^"]+)"', support)   # support_info is a raw JSON string
    if m and m.group(1) not in urls:
        urls.append(m.group(1))
    return [u for u in urls if u and "store.steampowered.com" not in u]


def cmd_pending():
    for appid in leads.get_pending():
        lead = leads.read_lead(appid) or {}
        studio = lead.get("game_name") or lead.get("name") or str(appid)
        print(json.dumps({"appid": appid, "studio": studio,
                          "urls": _candidate_urls(lead)}, ensure_ascii=False))


def cmd_write(argv):
    if len(argv) < 2:
        sys.exit("usage: write <appid> <status> [email] [website]")
    appid, status = argv[0], argv[1]
    if status not in VALID:
        sys.exit(f"status must be one of {sorted(VALID)}")
    email = argv[2] if len(argv) > 2 else None
    website = argv[3] if len(argv) > 3 else None
    leads.write_result(int(appid), scrape_status=status, emails=email, website=website)
    print(f"wrote {appid} -> {status}" + (f" {email}" if email else ""))


def main(argv):
    if not argv or argv[0] not in ("pending", "write"):
        sys.exit(__doc__)
    if argv[0] == "pending":
        return cmd_pending()
    return cmd_write(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
