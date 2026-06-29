"""Reconcile the local review media (curated JSON + screenshots + sprite sheet +
mail draft) with R2. R2 is the shared hub: cloud-discovered batches land there, and
the cloud Streamlit app reads from there. Local scripts (review/triage/send) read
files off disk, so local must hold a copy too — this keeps the two in step.

    pip install boto3            # one-time, local only
    python sync_media.py         # reconcile: pull what's missing locally, then push all
    python sync_media.py --pull  # only download R2 leads missing locally
    python sync_media.py --push  # only upload local leads to R2
    python sync_media.py 12345   # push just these appids

Existence is governed by Turso (a Reject deletes the DB row; the queue is DB-driven),
so sync only ever ADDS media — it never deletes. Orphaned media is harmless.

R2 creds come from .env (see media_store.py). Objects are keyed by the local folder
basename "<GameName>_<appid>/"; a root index.json maps appid -> folder.
"""
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # Windows console is cp1252; game names aren't

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
import media_store  # noqa: E402

_STORE_DIR = os.path.join(ROOT, "Leads_Reviewer", "Studios_To_Review")
BASES = [os.path.join(_STORE_DIR, "Approval_Pending_Games"),
         os.path.join(_STORE_DIR, "No_Mail_Games")]


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


def _push(only, client):
    """Upload local leads to R2 (all, or just `only`), and rewrite the full index."""
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
            store = os.path.basename(os.path.dirname(folder))   # which store to restore into
            media_store.upload_dir(folder, appid, _card(data, appid), store, client=client)
            done += 1
            print(f"  pushed {appid}  {data.get('gameName', '')[:48]}")
        except Exception as e:               # one bad lead shouldn't stop the mirror
            failed += 1
            print(f"  FAILED push {appid}: {e!r}")
    media_store.write_index(index, client=client)
    print(f"  push: {done} up, index {len(index)} lead(s)"
          + (f", {failed} failed" if failed else ""))
    return failed


def _pull(client):
    """Download leads that are in R2 but missing locally, into the right store."""
    index = media_store.fetch_index()
    local = {appid for appid, _ in _lead_folders()}
    pulled = failed = 0
    for appid_s, folder in index.items():
        try:
            if int(appid_s) in local:
                continue                      # already have it
        except ValueError:
            continue
        try:
            m = media_store.fetch_manifest(folder) or {}
            store = m.get("store") or "Approval_Pending_Games"   # default for old manifests
            dest = os.path.join(_STORE_DIR, store, folder)
            media_store.download_dir(folder, dest, client=client)
            pulled += 1
            print(f"  pulled {appid_s}  {folder[:48]}")
        except Exception as e:
            failed += 1
            print(f"  FAILED pull {appid_s}: {e!r}")
    print(f"  pull: {pulled} down" + (f", {failed} failed" if failed else ""))
    return failed


def main(argv):
    if not media_store.write_enabled():
        # Quiet success so it's safe to auto-call (SRE pre-hook) on a machine with no
        # R2 creds — the command that follows just runs against local files as before.
        print("R2 not configured — media sync skipped.")
        return 0
    mode, only = "reconcile", None
    if argv and argv[0] == "--pull":
        mode = "pull"
    elif argv and argv[0] == "--push":
        mode = "push"
    elif argv:
        only, mode = {int(a) for a in argv}, "push"
    client = media_store._client()           # reuse one client for the whole run
    print(f"sync_media: {mode}  (bucket '{media_store.BUCKET}')")
    rc = 0
    if mode in ("pull", "reconcile"):
        rc |= _pull(client)
    if mode in ("push", "reconcile"):
        rc |= _push(only, client)
    return 1 if rc else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
