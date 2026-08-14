"""Steam SRE - Studio Reach Engine. One launcher for the whole pipeline.

Each subcommand just shells out to the script that already does the work
(forwarding extra args + the child's exit code). No logic lives here — it's
the single front door so you don't memorise six script paths.

    python SRE.py --discover                 # find new pre-release studios -> stage folders
    python SRE.py --delete [1231,1321,..]    # GUI: review-before-delete those appids
    python SRE.py --review                    # GUI: Game / No-Mail / Mail approval
    python SRE.py --irrelevants-list          # AI-triage staged games -> Rejected Games = [..]
    python SRE.py --noseed-urls               # print websites of pending (no-email) leads
    python SRE.py --ingest-mailids '{"url":..,"email":..}, ..'   # feed scraped emails back
    python SRE.py --draft-mails               # AI-draft cold mails for 'Writing' leads -> R2
    python SRE.py --sync-templates            # push cold-mail templates -> R2 (drafter source)
    python SRE.py --send-mails                # send scheduled cold mails (--dry-run, --limit N)
    python SRE.py --review-mails              # check Sent leads for replies -> mark 'Replied'
    python SRE.py --repair-invalid            # quarantine existing invalid unsent recipients
    python SRE.py --sync-media                # mirror staged media -> R2 (cloud review app)
    python SRE.py --snap-db                   # dump Steam + Epic Turso DBs -> local SQLite files
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# flag -> (script relative to repo root, args to prepend before the user's extras)
ROUTES = {
    "--discover":      ("Claude_Lead_Discovery_Engine/run_daily.py", []),
    "--delete":        ("Leads_Reviewer/reviewer.py", ["--delete"]),
    "--review":        ("Leads_Reviewer/reviewer.py", []),
    "--irrelevants-list": ("Leads_Reviewer/triage.py", []),
    "--noseed-urls":   ("Leads_Reviewer/reviewer.py", ["--pending-urls"]),
    "--ingest-mailids": ("Leads_Reviewer/reviewer.py", ["--ingest-mailids"]),
    "--draft-mails":   ("Mail_Sender/draft_cloud.py", []),
    "--sync-templates": ("Mail_Sender/sync_templates.py", []),
    "--send-mails":    ("Mail_Sender/mailer.py", []),
    "--review-mails":  ("Mail_Sender/mailer.py", ["--review"]),
    "--repair-invalid": ("Claude_Lead_Discovery_Engine/repair_invalid.py", []),
    "--sync-media":    ("sync_media.py", []),
    "--snap-db":       ("snap_db.py", []),
}


def main(argv):
    if not argv or argv[0] not in ROUTES:
        sys.exit(__doc__)
    script, prefix = ROUTES[argv[0]]
    cmd = [sys.executable, os.path.join(HERE, *script.split("/"))] + prefix + argv[1:]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
