"""R2 media store — the bridge that lets the cloud review app show game art.

Local media (screenshots + sprite sheet + curated JSON) lives in gitignored
folders, so it isn't in the GitHub repo the cloud app deploys from. This module
mirrors each lead's media to a Cloudflare R2 bucket and serves it back by public
URL, so Streamlit Community Cloud can render it.

Two halves, deliberately split so the CLOUD app stays dependency-light:
  - READ  (public_url / fetch_manifest): plain urllib, no boto3. Used by the app.
  - WRITE (upload_dir): boto3 (S3-compatible R2 API), imported lazily. Used only
    by sync_media.py on the local machine.

Env (repo-root .env locally, Streamlit Secrets in cloud):
    R2_PUBLIC_BASE=https://pub-xxxx.r2.dev   # bucket public URL (read side)
    R2_BUCKET=sre-media                       # write side ↓
    R2_ACCOUNT_ID=...
    R2_ACCESS_KEY_ID=...
    R2_SECRET_ACCESS_KEY=...

Layout in the bucket:  <appid>/<image files> + <appid>/manifest.json
The manifest is self-contained (card text + image list + mail draft), so a card
is one HTTP GET — no DB round-trip for display.
"""
import json
import os
import re
import urllib.parse
import urllib.request


def _load_env():
    """Pull KEY=VALUE from the repo-root .env into os.environ (local convenience;
    doesn't override anything already set — e.g. Streamlit Secrets in cloud)."""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()
PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE", "").rstrip("/")
BUCKET = os.environ.get("R2_BUCKET", "")
_ACCOUNT = os.environ.get("R2_ACCOUNT_ID", "")
_KEY = os.environ.get("R2_ACCESS_KEY_ID", "")
_SECRET = os.environ.get("R2_SECRET_ACCESS_KEY", "")

_IMG_EXT = (".jpg", ".jpeg", ".png")


# Objects are keyed by the local folder basename ("<GameName>_<appid>/") to mirror
# the on-disk layout. The cloud app only knows the appid, so a root index.json maps
# appid -> folder; it resolves there before fetching a lead's manifest.
INDEX_KEY = "index.json"
IRRELEVANT_KEY = "irrelevant.json"    # appids the cloud triage flagged as irrelevant —
                                      # the app gates on this list before normal review
_UA = {"User-Agent": "Mozilla/5.0"}   # r2.dev 403s default urllib (CF bot block, err 1010)


# -- read side (cloud app) ------------------------------------------------
def read_enabled():
    """True if cards can be served from R2 (public base configured)."""
    return bool(PUBLIC_BASE)


def _enc(seg):
    # percent-encode a single path segment: game names have spaces + non-ASCII, which
    # raw urllib won't send and r2.dev won't match without encoding.
    return urllib.parse.quote(seg, safe="")


def public_url(folder, filename):
    return f"{PUBLIC_BASE}/{_enc(folder)}/{_enc(filename)}"


def _get_json(path):
    """GET a JSON object by its already-built (encoded) object path."""
    try:
        req = urllib.request.Request(f"{PUBLIC_BASE}/{path}", headers=_UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception:
        return None


def fetch_index():
    """{ '<appid>': '<GameName>_<appid>' } — appid→folder map, or {} if absent."""
    return _get_json(INDEX_KEY) or {}


def fetch_manifest(folder):
    """The lead's manifest dict from R2 (by folder name), or None if unreachable."""
    return _get_json(f"{_enc(folder)}/manifest.json")


def fetch_irrelevant():
    """List of appids the cloud triage flagged as irrelevant, or [] if none/absent.
    The app shows a reject-review gate while this is non-empty."""
    return _get_json(IRRELEVANT_KEY) or []


# -- write side (boto3, imported lazily) ----------------------------------
# Used by sync_media (local push) AND by the cloud app's triage-review actions
# (write_irrelevant / delete_lead_media), so the deployed app needs boto3 + R2 keys.
def write_enabled():
    return bool(BUCKET and _ACCOUNT and _KEY and _SECRET)


_CLIENT = None


def _client():
    """One cached, thread-safe S3 client per process — recreating it per call cost
    real time. botocore clients are safe to share across threads."""
    global _CLIENT
    if _CLIENT is None:
        import boto3  # lazy
        _CLIENT = boto3.client(
            "s3",
            endpoint_url=f"https://{_ACCOUNT}.r2.cloudflarestorage.com",
            aws_access_key_id=_KEY,
            aws_secret_access_key=_SECRET,
            region_name="auto",
        )
    return _CLIENT


def build_manifest(folder, appid, card):
    """Self-contained manifest: precomputed card strings (matching
    Reviewer_Interface._load_folder), the image filenames, and the mail draft."""
    images = sorted(f for f in os.listdir(folder) if f.lower().endswith(_IMG_EXT))
    mail, template = None, None
    for f in os.listdir(folder):
        match = re.fullmatch(rf"mail_{appid}_(\d+)\.txt", f)
        if match:
            with open(os.path.join(folder, f), encoding="utf-8") as fh:
                mail = fh.read().strip()
            template = int(match.group(1))     # the chosen template id, for Approve_Mail
            break
    return {**card, "images": images, "mail": mail, "mail_template": template}


def upload_dir(folder, appid, card, client=None):
    """Mirror one lead folder to R2 under its basename ('<GameName>_<appid>/'): the
    images + a derived manifest.json (card text + image list + mail draft) the cloud
    app reads. Idempotent — re-uploads overwrite. Returns the R2 folder prefix used."""
    cli = client or _client()
    prefix = os.path.basename(folder.rstrip("/\\"))      # "Boompaw_4889770"
    manifest = build_manifest(folder, appid, card)
    for name in manifest["images"]:
        ctype = "image/png" if name.lower().endswith(".png") else "image/jpeg"
        with open(os.path.join(folder, name), "rb") as fh:
            cli.put_object(Bucket=BUCKET, Key=f"{prefix}/{name}", Body=fh.read(),
                           ContentType=ctype)
    cli.put_object(Bucket=BUCKET, Key=f"{prefix}/manifest.json",
                   Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                   ContentType="application/json")
    return prefix


def write_index(mapping, client=None):
    """Upload the appid->folder map to the bucket root so the cloud app can resolve
    a lead's folder from its appid."""
    cli = client or _client()
    cli.put_object(Bucket=BUCKET, Key=INDEX_KEY,
                   Body=json.dumps(mapping, ensure_ascii=False).encode("utf-8"),
                   ContentType="application/json")


def write_irrelevant(appids, client=None):
    """Write the triage-flagged appid list to irrelevant.json at the bucket root."""
    cli = client or _client()
    cli.put_object(Bucket=BUCKET, Key=IRRELEVANT_KEY,
                   Body=json.dumps(list(appids)).encode("utf-8"),
                   ContentType="application/json")


def delete_lead_media(appid, client=None):
    """Purge one lead's media from R2: delete every object under its folder and drop it
    from the index. Used by the app's Reject. No-op if the appid isn't indexed."""
    cli = client or _client()
    index = fetch_index()
    folder = index.pop(str(appid), None)
    if folder:
        keys = [o["Key"] for page in cli.get_paginator("list_objects_v2")
                .paginate(Bucket=BUCKET, Prefix=f"{folder}/")
                for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            cli.delete_objects(Bucket=BUCKET,
                               Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
        write_index(index, client=cli)
    return bool(folder)
