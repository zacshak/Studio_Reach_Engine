"""Push local review media (curated JSON + screenshots + sprite sheet + mail draft) to
R2 so the cloud review app can show game art it can't get from the gitignored folders.
Run it after a local discovery/triage batch (run_daily does this automatically).

    pip install boto3            # one-time, local only
    python sync_media.py         # push every staged lead
    python sync_media.py 12345   # push just these appids
    python sync_media.py --cleanup [--apply]  # audit/apply cleanup only

Local media is uploaded, then R2 is reconciled against active scrape_tracker rows.
Objects are keyed by "<GameName>_<appid>/"; index.json maps appid -> folder.
"""
import json
import os
import sys
from contextlib import closing

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # Windows console is cp1252; game names aren't

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
import media_store  # noqa: E402
import pipeline  # noqa: E402

BASES = [os.path.join(ROOT, "Leads_Reviewer", "Studios_To_Review", "Approval_Pending_Games"),
         os.path.join(ROOT, "Leads_Reviewer", "Studios_To_Review", "No_Mail_Games")]
ACTIVE_MAIL = {"Pending", "Writing", "Drafted", "Scheduled", "Sending"}


def _card(data, appid):
    """Precomputed card strings — identical to Reviewer_Interface._load_folder so the
    cloud card matches the desktop one exactly."""
    devs = ", ".join(data.get("developers") or [])
    genres = ", ".join(g.get("description", "") for g in (data.get("genres") or [])
                       if isinstance(g, dict))
    return {
        "name": data.get("gameName") or str(appid),
        "desc": (data.get("short_description") or "").strip() or "(no short description)",
        "meta": "  ·  ".join(p for p in (devs, genres) if p) or "—",
    }


def _lead_folders(only=None):
    """Yield (appid, folder) for every staged lead, optionally filtered to `only`."""
    for base in BASES:
        if not os.path.isdir(base):
            continue
        for entry in os.listdir(base):
            folder = os.path.join(base, entry)
            if not os.path.isdir(folder) or "_" not in entry:
                continue
            try:
                appid = int(entry.rsplit("_", 1)[1])
            except ValueError:
                continue
            if only and appid not in only:
                continue
            yield appid, folder


def _active_appids():
    if not pipeline.TURSO_URL or not pipeline.TURSO_TOKEN:
        raise RuntimeError("R2 cleanup requires the remote Turso configuration")
    with closing(pipeline._ro()) as conn:
        rows = conn.execute(
            "SELECT appid, Mail_status, scrape_status FROM scrape_tracker").fetchall()
    if not rows:
        raise RuntimeError("refusing R2 cleanup against an empty scrape_tracker")
    return {int(appid) for appid, mail, scrape in rows
            if mail in ACTIVE_MAIL and scrape != "invalid"}


def cleanup_r2(client, apply=False):
    """Remove media that no active scrape_tracker row owns. Objects are deleted
    before index entries so any interrupted cleanup remains retryable."""
    active = _active_appids()
    obj = client.get_object(Bucket=media_store.BUCKET, Key=media_store.INDEX_KEY)
    index = json.loads(obj["Body"].read())
    objects = [item for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=media_store.BUCKET) for item in page.get("Contents", [])]
    prefixes = {item["Key"].split("/", 1)[0] for item in objects if "/" in item["Key"]}
    keep = {folder for appid, folder in index.items()
            if appid.isdigit() and int(appid) in active and folder in prefixes}
    doomed_prefixes = prefixes - keep
    doomed = [item for item in objects
              if "/" in item["Key"] and item["Key"].split("/", 1)[0] in doomed_prefixes]
    missing_prefixes = set(index.values()) - prefixes
    print(f"R2 cleanup: {len(doomed_prefixes)} folder(s), {len(doomed)} object(s), "
          f"{sum(item['Size'] for item in doomed) / 1024 / 1024:.2f} MiB "
          f"{'to delete' if apply else 'would be deleted'}; "
          f"{len(missing_prefixes)} stale index entry/entries")
    if not apply:
        return len(doomed)
    for start in range(0, len(doomed), 1000):
        result = client.delete_objects(
            Bucket=media_store.BUCKET,
            Delete={"Objects": [{"Key": item["Key"]} for item in doomed[start:start + 1000]]})
        if result.get("Errors"):
            raise media_store.MediaStoreError(f"R2 cleanup failed: {result['Errors']}")

    def clean(current):
        return {appid: folder for appid, folder in current.items()
                if appid.isdigit() and int(appid) in active
                and folder not in doomed_prefixes and folder not in missing_prefixes}

    media_store.update_index(clean, client=client)
    media_store.update_irrelevant(
        lambda current: [appid for appid in current
                         if str(appid).isdigit() and int(appid) in active], client=client)
    return len(doomed)


def main(argv):
    if not media_store.write_enabled():
        # Quiet skip so it's safe to auto-call (run_daily) on a machine with no creds.
        print("R2 not configured — media sync skipped.")
        return 0
    client = media_store._client()        # reuse one client for the whole run
    if argv and argv[0] == "--cleanup":
        cleanup_r2(client, apply="--apply" in argv[1:])
        return 0
    only = {int(a) for a in argv} or None
    done = failed = 0
    uploaded = {}
    for appid, folder in _lead_folders(only):
        jpath = os.path.join(folder, f"{appid}.json")
        if not os.path.exists(jpath):
            continue
        try:
            with open(jpath, encoding="utf-8") as f:
                data = json.load(f)
            media_store.upload_dir(folder, appid, _card(data, appid), client=client)
            uploaded[str(appid)] = os.path.basename(folder)
            done += 1
            print(f"  synced {appid}  {data.get('gameName', '')[:48]}")
        except Exception as e:               # one bad lead shouldn't stop the mirror
            failed += 1
            print(f"  FAILED {appid}: {e!r}")
    # Merge only successful uploads; a failed folder must never enter the remote index.
    index = media_store.update_index(lambda current: {**current, **uploaded}, client=client)
    cleanup_r2(client, apply=True)
    print(f"\ndone: {done} lead(s) -> R2 bucket '{media_store.BUCKET}'; "
          f"index {len(index)}"
          + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
