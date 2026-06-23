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
import glob
import json
import os
import re
import shutil
import sys

# pipeline.py lives in the System 1 folder (the DB owner); make it importable.
_PIPELINE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Claude_Lead_Discovery_Engine")
sys.path.insert(0, _PIPELINE_DIR)
import pipeline  # noqa: E402

# media folders (screenshots + curated JSON) live inside this reviewer folder
_HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(_HERE, "Approval_Pending_Games")   # seeded leads
NOMAIL_DIR = os.path.join(_HERE, "No_Mail_Games")           # pending (no-email) leads


def _folder_for(appid, base=MEDIA_DIR):
    """The media folder under `base` whose name ends in _<appid>, or None."""
    if not os.path.isdir(base):
        return None
    suffix = f"_{appid}"
    for entry in os.listdir(base):
        if entry.endswith(suffix) and os.path.isdir(os.path.join(base, entry)):
            return os.path.join(base, entry)
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


def _mail_file(folder, appid):
    """First existing mail_<appid>_<N>.txt path, or None."""
    files = sorted(glob.glob(os.path.join(folder, f"mail_{appid}_*.txt")))
    return files[0] if files else None


def _template_of(path):
    """The N (template id) from a mail_<appid>_<N>.txt filename, or None."""
    m = re.search(r"_(\d+)\.txt$", os.path.basename(path))
    return int(m.group(1)) if m else None


def _load_mail(folder, appid):
    """The written mail text for a game: first existing mail_<appid>_<N>.txt."""
    path = _mail_file(folder, appid)
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            pass
    return ""


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


def nomail_games_to_review():
    """Games with no email anywhere (scrape_status 'pending') that have a staged
    folder in No_Mail_Games. Same shape as games_to_review(). Reject-only in the GUI."""
    out = []
    for appid in pipeline.get_pending():                 # scrape_status == 'pending'
        folder = _folder_for(appid, NOMAIL_DIR)
        if folder:
            out.append(_load_folder(folder, appid))
    out.sort(key=lambda g: g["name"].lower())
    return out


def pending_website_urls():
    """Array of website URLs for leads with scrape_status=='pending' that have a
    website — the sites to scrape for an email."""
    return pipeline.pending_websites()


def _norm_url(u):
    """Loose URL key for matching: drop scheme, leading www., trailing slash."""
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def ingest_emails(items):
    """Take scraped [{"url":..,"email":..}, ..] back from the external scraper.
    For each item that HAS an email, match its url to a pending lead, write the
    email + flip scrape_status 'pending'->'seeded', and move its media folder
    No_Mail_Games -> Approval_Pending_Games (so it shows in Game Approval).
    Empty-email and unmatched items are left untouched. Returns a summary dict."""
    by_url = {_norm_url(w): a for a, w in pipeline.pending_leads() if w}
    updated, skipped, unmatched = [], [], []
    for it in items:
        url, email = it.get("url", ""), (it.get("email") or "").strip()
        if not email:                              # no mail found -> leave as pending
            skipped.append(url)
            continue
        appid = by_url.get(_norm_url(url))
        if appid is None:
            unmatched.append(url)
            continue
        pipeline.write_result(appid, scrape_status="seeded", emails=email)
        src = _folder_for(appid, NOMAIL_DIR)       # move local data to the seeded store
        if src:
            dst = os.path.join(MEDIA_DIR, os.path.basename(src))
            if not os.path.exists(dst):
                try:
                    shutil.move(src, dst)
                except (OSError, shutil.Error):
                    pass
        updated.append((appid, email))
    return {"updated": updated, "skipped": skipped, "unmatched": unmatched}


def leads_by_appids(appids):
    """Game-card data for an explicit appid list, from whichever store holds each
    (Approval_Pending_Games or No_Mail_Games). Ids with no folder are skipped.
    Used by the --delete review window. Same shape as games_to_review()."""
    out = []
    for appid in appids:
        folder = _folder_for(appid) or _folder_for(appid, NOMAIL_DIR)
        if folder:
            out.append(_load_folder(folder, appid))
    out.sort(key=lambda g: g["name"].lower())
    return out


def mails_to_review():
    """Games with Mail_status 'Writing' that still have a media folder. Same shape
    as games_to_review() plus a 'mail' field (the drafted message text)."""
    out = []
    for appid in pipeline.mail_status_appids("Writing"):
        folder = _folder_for(appid)
        if folder:
            item = _load_folder(folder, appid)
            item["mail"] = _load_mail(folder, appid)
            item["emails"] = pipeline.get_emails(appid)
            out.append(item)
    out.sort(key=lambda g: g["name"].lower())
    return out


def Accept_Game(gameId):
    """Accept: mark the lead for outreach (Mail_status -> 'Writing'). The media
    folder is kept. The game drops out of games_to_review()."""
    pipeline.set_mail_status(gameId, "Writing")


def Approve_Mail(gameId):
    """Approve the drafted mail: record the chosen template (N from the
    mail_<appid>_<N>.txt filename) and set Mail_status -> 'Scheduled'. Drops out
    of mails_to_review() (only 'Writing' renders there)."""
    folder = _folder_for(gameId)
    if folder:
        path = _mail_file(folder, gameId)
        if path:
            tpl = _template_of(path)
            if tpl is not None:
                pipeline.set_mail_template(gameId, tpl)
    pipeline.set_mail_status(gameId, "Scheduled")


def Reject_Game(gameId):
    """Reject: delete the lead's DB rows AND its media folder (in whichever store
    it lives — Approval_Pending_Games or No_Mail_Games). Returns a list of any
    folders that could NOT be removed (e.g. locked on Windows) so callers can warn
    instead of reporting a false success; empty list means a clean delete."""
    pipeline.delete_lead(gameId)
    leftover = []
    for base in (MEDIA_DIR, NOMAIL_DIR):
        folder = _folder_for(gameId, base)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
            if os.path.exists(folder):        # rmtree swallowed a lock/permission error
                leftover.append(folder)
    return leftover
