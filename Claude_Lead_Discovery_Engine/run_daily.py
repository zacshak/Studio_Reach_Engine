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
    rc = sync_media()
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
    return 2 if finder_rc == 2 else (1 if remaining else 0)


if __name__ == "__main__":
    raise SystemExit(main())
