"""ONE COMMAND for the daily (or twice-daily) routine.

Runs the finder, then lead discovery, sharing a single timestamp so the
output files line up, then opens the resulting Excel.

    python run_daily.py

Each run produces its own timestamped files (never overwrites a prior run):
    out/new_appids_<date>_<HHMM>.txt
    out/leads_<date>_<HHMM>.xlsx  (+ .csv)

So running it morning and night gives you two separate, complete leads files.
"""
import os
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def run(script, *args):
    """Run a child script, streaming its output live; return its exit code."""
    return subprocess.call([PY, os.path.join(HERE, script), *args])


def sync_media(*args):
    script = os.path.join(os.path.dirname(HERE), "sync_media.py")
    return subprocess.call([PY, script, *args]) if os.path.exists(script) else 0


def report_pending():
    from contextlib import closing
    import pipeline

    with closing(pipeline.connect()) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM pending_discovery").fetchone()[0]
    print(f"Pending discovery after media sync: {remaining} job(s).")
    if remaining:
        print("Unfinished work remains durable and will retry on the next run.")
    return remaining


def report_partial(path):
    """Only known missing Steam images can downgrade unfinished work to a warning."""
    from contextlib import closing
    import pipeline
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    with closing(pipeline.connect()) as conn:
        pending = {int(row[0]) for row in conn.execute("SELECT appid FROM pending_discovery")}
    deferred = set(report["deferred"])
    if report["failed"] or not pending or not pending <= deferred:
        return False
    message = (f"Partial completion: {report['uploaded']} game(s) uploaded; "
               f"{len(pending)} deferred because Steam images are unavailable. "
               f"Pending appids: {', '.join(map(str, sorted(pending)))}. "
               "Not published to review; retained for the next discovery run.")
    print(f"::warning::{message}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"\n## Discovery: partial completion\n\n{message}\n")
    return True


def main():
    stamp = datetime.now(IST).strftime("%Y-%m-%d_%H%M")
    print(f"=== Daily run {stamp} IST ===\n")

    rc = run("find_new.py", "--stamp", stamp)
    finder_rc = rc
    if rc == 2:
        print("\nFinder failed. Draining previously queued discovery work.")
    if rc == 10:
        print("\nBaseline memorised. Checking existing pending discovery work.")
    if rc == 11:
        print("\nNo new pages. Draining pending discovery work.")
    if rc not in (0, 2, 10, 11):
        print(f"\nFinder exited with code {rc}; stopping.")
        return rc

    rc = run("lead_discovery.py", "--stamp", stamp)
    if rc != 0:
        print(f"\nLead discovery exited with code {rc}.")
        return rc

    # Mirror the freshly-staged media to R2 so the cloud review app sees the night's
    # batch, then remove media no active tracker row owns. No-op if creds aren't set.
    print("\nMirroring media to R2 ...")
    report = os.path.join(HERE, "out", f"media_sync_{stamp}.json")
    if os.path.isfile(report):
        os.remove(report)  # Never classify this run using an older report.
    rc = sync_media("--report", report)
    remaining = report_pending()
    if rc:
        print(f"Media sync exited with code {rc}; stopping before triage.")
        return rc

    xlsx = os.path.join(HERE, "out", f"leads_{stamp}.xlsx")
    if os.path.exists(xlsx):
        print(f"\nOpening {xlsx}")
        try:
            os.startfile(xlsx)  # Windows
        except Exception:
            pass  # non-Windows or no associated app — file is still written
    if finder_rc == 2:
        return 2
    return 0 if not remaining or report_partial(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
