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


def main():
    stamp = datetime.now(IST).strftime("%Y-%m-%d_%H%M")
    print(f"=== Daily run {stamp} IST ===\n")

    rc = run("find_new.py", "--stamp", stamp)
    if rc == 2:
        print("\nFinder FAILED — nothing built. Try again later.")
        return rc
    if rc == 10:
        print("\nBaseline memorised (first run). No leads to build yet — run again next time.")
        return 0
    if rc == 11:
        print("\nNo new pre-release pages this run. No leads file created.")
        return 0
    if rc != 0:
        print(f"\nFinder exited with code {rc}; stopping.")
        return rc

    rc = run("lead_discovery.py", "--stamp", stamp)
    if rc != 0:
        print(f"\nLead discovery exited with code {rc}.")
        return rc

    # Mirror the freshly-staged media to R2 so the cloud review app sees the night's
    # batch. No-op (with a one-line note) if R2 creds aren't set. ponytail: full mirror
    # each run — fine within R2's free tier; make it incremental if it ever drags.
    sync = os.path.join(os.path.dirname(HERE), "sync_media.py")
    if os.path.exists(sync):
        print("\nMirroring media to R2 ...")
        subprocess.call([PY, sync])

    xlsx = os.path.join(HERE, "out", f"leads_{stamp}.xlsx")
    if os.path.exists(xlsx):
        print(f"\nOpening {xlsx}")
        try:
            os.startfile(xlsx)  # Windows
        except Exception:
            pass  # non-Windows or no associated app — file is still written
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
