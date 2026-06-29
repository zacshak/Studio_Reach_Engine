"""Mirror the local review media (screenshots + sprite sheet + mail draft) to R2,
so the cloud Streamlit app can show game art it can't get from the gitignored
folders. Backfill once now; re-run after each local discovery/triage batch (the
Phase 3 nightly job calls this).

    pip install boto3            # one-time, local only
    python sync_media.py         # mirror every staged lead
    python sync_media.py 12345   # just these appids

R2 creds come from .env (see media_store.py). Keyed by appid in the bucket, so
both stores (Approval_Pending_Games + No_Mail_Games) land under <appid>/.
"""
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # Windows console is cp1252; game names aren't

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "Leads_Reviewer"))
import media_store  # noqa: E402

BASES = [
    os.path.join("Leads_Reviewer", "Studios_To_Review", "Approval_Pending_Games"),
    os.path.join("Leads_Reviewer", "Studios_To_Review", "No_Mail_Games"),
]


def _card(data, appid):
    """Precomputed card strings — identical to Reviewer_Interface._load_folder so
    the cloud card matches the desktop one exactly."""
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


def main(argv):
    if not media_store.write_enabled():
        sys.exit("R2 write creds not set (R2_BUCKET / R2_ACCOUNT_ID / R2_ACCESS_KEY_ID "
                 "/ R2_SECRET_ACCESS_KEY in .env). See media_store.py.")
    only = {int(a) for a in argv} or None
    client = media_store._client()        # reuse one client for the whole run
    done = failed = 0
    # Always build the FULL appid->folder index from local folders (cheap, no upload),
    # even on a single-appid run, so the index never goes stale/partial.
    index = {str(appid): os.path.basename(folder) for appid, folder in _lead_folders()}
    for appid, folder in _lead_folders(only):
        jpath = os.path.join(folder, f"{appid}.json")
        if not os.path.exists(jpath):
            continue
        try:
            with open(jpath, encoding="utf-8") as f:
                data = json.load(f)
            media_store.upload_dir(folder, appid, _card(data, appid), client=client)
            done += 1
            print(f"  synced {appid}  {data.get('gameName', '')[:48]}")
        except Exception as e:               # one bad lead shouldn't stop the mirror
            failed += 1
            print(f"  FAILED {appid}: {e!r}")
    media_store.write_index(index, client=client)
    print(f"\ndone: {done} lead(s) mirrored to R2 bucket '{media_store.BUCKET}'"
          f"; index has {len(index)} lead(s)"
          + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
