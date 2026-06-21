"""The Leads Reviewer's interface to the lead database + media store.

The GUI imports ONLY this module — never pipeline.py directly (same pattern as
the Hermes scraper_interface). It exposes the review actions and the list of
games still awaiting review; the DB schema and connections stay hidden in
pipeline.py.

    import Reviewer_Interface as review

    for g in review.games_to_review():   # Mail_status == 'Pending' AND has media
        ...
    review.Accept_Game(appid)            # Mail_status -> 'Writing', leaves the list
    review.Reject_Game(appid)            # row deleted + media folder removed
"""
import json
import os
import shutil
import sys

# pipeline.py lives in the System 1 folder (the DB owner); make it importable.
_PIPELINE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Claude_Lead_Discovery_Engine")
sys.path.insert(0, _PIPELINE_DIR)
import pipeline  # noqa: E402

# media folders (screenshots + curated JSON) live inside this reviewer folder
MEDIA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "Approval_Pending_Games")


def _folder_for(appid):
    """The media folder whose name ends in _<appid>, or None."""
    if not os.path.isdir(MEDIA_DIR):
        return None
    suffix = f"_{appid}"
    for entry in os.listdir(MEDIA_DIR):
        if entry.endswith(suffix) and os.path.isdir(os.path.join(MEDIA_DIR, entry)):
            return os.path.join(MEDIA_DIR, entry)
    return None


def _load_folder(folder, appid):
    data = {}
    for f in os.listdir(folder):
        if f.endswith(".json"):
            try:
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                data = {}
            break
    shots = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                   if f.lower().endswith((".jpg", ".jpeg", ".png")))
    devs = ", ".join(data.get("developers") or [])
    genres = ", ".join(g.get("description", "") for g in (data.get("genres") or [])
                       if isinstance(g, dict))
    return {
        "appid": appid,
        "name": data.get("gameName") or str(appid),
        "desc": (data.get("short_description") or "").strip() or "(no short description)",
        "meta": "  ·  ".join(p for p in (devs, genres) if p) or "—",
        "shots": shots,
    }


def games_to_review():
    """Games to show: those with Mail_status 'Pending' that still have a media
    folder. Sorted by name. Each item: {appid, name, desc, meta, shots[]}."""
    out = []
    for appid in pipeline.mail_status_appids("Pending"):
        folder = _folder_for(appid)
        if folder:
            out.append(_load_folder(folder, appid))
    out.sort(key=lambda g: g["name"].lower())
    return out


def Accept_Game(gameId):
    """Accept: mark the lead for outreach (Mail_status -> 'Writing'). The media
    folder is kept. The game drops out of games_to_review()."""
    pipeline.set_mail_status(gameId, "Writing")


def Reject_Game(gameId):
    """Reject: delete the lead's scrape_tracker row AND its media folder."""
    pipeline.delete_lead(gameId)
    folder = _folder_for(gameId)
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
