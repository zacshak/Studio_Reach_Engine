"""Manual backfill: python Leads_Reviewer/backfill_web_previews.py DASHBOARD_URL [--apply]."""
import argparse
import json
from pathlib import Path
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Leads_Reviewer"))
import media_store


def queue_folders(state):
    ids = set()
    for queue in ("approval", "mail", "nomail", "triage"):
        for item in state.get(queue, []):
            ids.add(str(item["appid"] if isinstance(item, dict) else item))
    return sorted({state["index"][appid] for appid in ids if appid in state["index"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dashboard_url")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    url = args.dashboard_url.rstrip("/") + "/api/state"
    request = urllib.request.Request(url, headers={'User-Agent':'SRE-preview-backfill'})
    with urllib.request.urlopen(request, timeout=30) as response:
        folders = queue_folders(json.load(response))
    if args.apply and not media_store.write_enabled():
        raise RuntimeError("Existing R2 credentials are required")
    def process(folder):
        if not args.apply:
            print(f"Would backfill {folder}")
            return
        # Refresh queue membership before each lead, not only at run start.
        with urllib.request.urlopen(request, timeout=30) as response:
            if folder not in queue_folders(json.load(response)):
                print(f"Skipped inactive {folder}")
                return
        result = media_store.backfill_previews(folder)
        print(f"{'Updated' if result is not None else 'Skipped deleted'} {folder}")
    media_store._client() if args.apply else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(process, folders))


if __name__ == "__main__":
    main()
