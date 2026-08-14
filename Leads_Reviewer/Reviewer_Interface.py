"""The Leads Reviewer's interface to the lead database + media store.

The GUI imports ONLY this module — never pipeline.py directly (same pattern as
the Hermes scraper_interface). It exposes the review actions and the list of
games still awaiting review; the DB schema and connections stay hidden in
pipeline.py.

    import Reviewer_Interface as review

    for g in review.games_to_review():   # Mail_status == 'Pending' AND has email AND media
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
from concurrent.futures import ThreadPoolExecutor

# pipeline.py lives in the System 1 folder (the DB owner); make it importable.
_PIPELINE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Claude_Lead_Discovery_Engine")
sys.path.insert(0, _PIPELINE_DIR)
import pipeline  # noqa: E402
import media_store  # noqa: E402  (R2 bridge; read side is boto3-free)

# media folders (screenshots + curated JSON) live inside this reviewer folder
_HERE = os.path.dirname(os.path.abspath(__file__))
_REVIEW_DIR = os.path.join(_HERE, "Studios_To_Review")
MEDIA_DIR = os.path.join(_REVIEW_DIR, "Approval_Pending_Games")   # seeded leads
NOMAIL_DIR = os.path.join(_REVIEW_DIR, "No_Mail_Games")           # pending (no-email) leads

# Cloud mode: serve cards from R2 when its public base is set AND there's no local
# media tree (i.e. running somewhere other than the machine sync_media.py ran on,
# where the gitignored folders don't exist). Locally the tree exists, so the
# desktop GUI keeps reading from disk.
_CLOUD = media_store.read_enabled() and not os.path.isdir(_REVIEW_DIR)


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


def _load_remote(appid, folder):
    """Card data from the R2 manifest (cloud mode), looked up by `folder` (the
    '<GameName>_<appid>' prefix from the index). Same shape as _load_folder, plus
    'mail'. None if the lead has no media in R2 yet."""
    m = media_store.fetch_manifest(folder)
    if not m:
        return None
    return {
        "appid": appid,
        "name": m.get("name") or str(appid),
        "desc": m.get("desc") or "(no short description)",
        "meta": m.get("meta") or "—",
        "shots": [media_store.public_url(folder, f) for f in m.get("images", [])],
        "mail": m.get("mail") or "",
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
    """Games to show: those with Mail_status 'Pending' AND an actual email on file
    (scrape_status 'seeded'/'scraped') that still have a media folder. Sorted by
    name. Each item: {appid, name, desc, meta, shots[]}."""
    if _CLOUD:
        return _lazy_list(pipeline.approval_ready_appids())
    return _local_queue(pipeline.approval_ready_appids(), MEDIA_DIR)


def nomail_games_to_review():
    """Leads awaiting scraping, with no email, or with a scraper error."""
    if _CLOUD:
        return _lazy_list(pipeline.nomail_ready_appids())
    return _local_queue(pipeline.nomail_ready_appids(), NOMAIL_DIR)


def _local_queue(appids, base):
    """Eager card list from local folders (desktop GUI / local run) — filesystem is
    cheap, so build everything up front. Leads without a media folder are skipped."""
    out = []
    for appid in appids:
        folder = _folder_for(appid, base)
        item = _load_folder(folder, appid) if folder else None
        if item:
            out.append(item)
    out.sort(key=lambda g: g["name"].lower())
    return out


def _lazy_list(appids, with_email=False):
    """Cloud: an INSTANT queue built from index.json alone — NO per-card fetches. Each
    entry is {appid, folder, name(from folder), shots:None}; hydrate() pulls the real
    card (images/desc/mail) from R2 only when the card is actually viewed. This is what
    makes the first paint fast instead of fetching hundreds of manifests up front.
    with_email tags mail cards so hydrate() also attaches the recipient (a Turso query)."""
    index = media_store.fetch_index()
    out = []
    for appid in appids:
        folder = index.get(str(appid))
        if folder:
            item = {"appid": appid, "folder": folder, "shots": None,
                    "name": folder.rsplit("_", 1)[0] or str(appid)}   # for sort/title
            if with_email:
                item["_email"] = True
            out.append(item)
    out.sort(key=lambda g: g["name"].lower())
    return out


def hydrate(item):
    """Fill a lazy item's card data from its R2 manifest, once. No-op for already-loaded
    items (local full dicts, mail cards, or a card viewed before — all have shots set).
    Returns the same dict, so the caller can cache it. Cloud-only work; cheap locally."""
    if item.get("shots") is not None:
        return item
    full = _load_remote(item["appid"], item.get("folder"))
    if full:
        item.update(full)                                   # real name/desc/meta/shots/mail
    else:
        item.update({"shots": [], "desc": "(media unavailable)", "meta": "—"})
    if item.get("_email"):                                  # mail card — attach recipient now
        item["emails"] = pipeline.get_emails(item["appid"])
    return item


def has_pending_drafts():
    """True if any accepted lead is still awaiting a cold-mail draft (Mail_status
    'Writing'). One cheap Turso query — used to gate the app's Draft button."""
    return bool(pipeline.mail_status_appids("Writing"))


def has_scheduled():
    """True if any lead is approved and awaiting send (Mail_status 'Scheduled').
    One cheap Turso query — gates the app's Send button."""
    return bool(pipeline.mail_status_appids("Scheduled"))


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
    email + flip scrape_status 'pending'->'scraped', and move its media folder
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
        pipeline.write_result(appid, scrape_status="scraped", emails=email)
        if pipeline.email_state(email) != "valid":
            skipped.append(url)
            continue
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
    """Games with Mail_status 'Drafted' (accepted AND the drafter has actually written
    the mail — 'Writing' alone means still awaiting a draft). Same shape as
    games_to_review() plus 'mail' (the drafted message) and 'emails' (recipient).
    Cloud: lazy like the other views — instant from index.json, with hydrate()
    pulling the manifest + recipient on view."""
    if _CLOUD:
        return _lazy_list(pipeline.mail_status_appids("Drafted"), with_email=True)
    # local: filesystem is cheap, so build eagerly
    out = []
    for appid in pipeline.mail_status_appids("Drafted"):
        folder = _folder_for(appid)
        item = _load_folder(folder, appid) if folder else None
        if not item:
            continue
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
    of mails_to_review() (only 'Drafted' renders there)."""
    if _CLOUD:
        folder = media_store.fetch_index().get(str(gameId))
        m = media_store.fetch_manifest(folder) if folder else None
        tpl = m.get("mail_template") if m else None
    else:
        folder = _folder_for(gameId)
        path = _mail_file(folder, gameId) if folder else None
        tpl = _template_of(path) if path else None
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


# -- cloud triage review (the irrelevant.json gate) -----------------------
def irrelevant_to_review():
    """Lightweight queue of triage-flagged leads (cloud), built from R2's irrelevant.json
    + the index. [] when nothing's flagged. Cards hydrate on view like the normal queue."""
    appids = media_store.fetch_irrelevant()
    if not appids:
        return []
    index = media_store.fetch_index()
    out = []
    for appid in appids:
        folder = index.get(str(appid))
        if folder:
            out.append({"appid": appid, "folder": folder, "shots": None,
                        "name": folder.rsplit("_", 1)[0] or str(appid)})
    out.sort(key=lambda g: g["name"].lower())
    return out


def _drop_irrelevant(appid):
    """Remove one appid from R2's irrelevant.json."""
    media_store.update_irrelevant(
        lambda current: [a for a in current if int(a) != int(appid)])


def Keep_Irrelevant(appid):
    """The AI was wrong — keep the lead: just unflag it. It rejoins the normal queue."""
    _drop_irrelevant(appid)


def Reject_Irrelevant(appid):
    """Confirm irrelevant — purge it everywhere. The two R2 purges (unflag in
    irrelevant.json, delete media+index) are independent and use the thread-safe boto3
    client, so run them CONCURRENTLY. The Turso delete stays on this thread — the shared
    libSQL connection is thread-bound."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(_drop_irrelevant, appid),
                ex.submit(media_store.delete_lead_media, appid)]
        for f in futs:
            f.result()      # surface any error
    pipeline.delete_lead(appid)
