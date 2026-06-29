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


# -- write side (local sync only; boto3 imported lazily) ------------------
def write_enabled():
    return bool(BUCKET and _ACCOUNT and _KEY and _SECRET)


def _client():
    import boto3  # lazy: keeps the cloud app boto3-free
    return boto3.client(
        "s3",
        endpoint_url=f"https://{_ACCOUNT}.r2.cloudflarestorage.com",
        aws_access_key_id=_KEY,
        aws_secret_access_key=_SECRET,
        region_name="auto",
    )


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


_CTYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".json": "application/json", ".txt": "text/plain; charset=utf-8"}


def _ctype(name):
    return _CTYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def upload_dir(folder, appid, card, store, client=None):
    """Mirror an ENTIRE lead folder to R2 under its basename ('<GameName>_<appid>/'):
    every file (curated JSON, images, mail draft) verbatim — so a pull can restore it
    faithfully — plus a derived manifest.json the cloud app reads. `store` (the parent
    dir name) is recorded in the manifest so a pull knows which store to restore into.
    Idempotent — re-uploads overwrite. Returns the R2 folder prefix used."""
    cli = client or _client()
    prefix = os.path.basename(folder.rstrip("/\\"))      # "Boompaw_4889770"
    manifest = build_manifest(folder, appid, card)
    manifest["store"] = store
    for name in os.listdir(folder):
        fp = os.path.join(folder, name)
        if os.path.isfile(fp):
            with open(fp, "rb") as fh:
                cli.put_object(Bucket=BUCKET, Key=f"{prefix}/{name}", Body=fh.read(),
                               ContentType=_ctype(name))
    cli.put_object(Bucket=BUCKET, Key=f"{prefix}/manifest.json",
                   Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                   ContentType="application/json")
    return prefix


def download_dir(prefix, dest_folder, client=None):
    """Download every object under '<prefix>/' into dest_folder (skipping the R2-only
    manifest.json) — restores a cloud-discovered lead's folder locally. Returns count."""
    cli = client or _client()
    os.makedirs(dest_folder, exist_ok=True)
    n = 0
    for page in cli.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{prefix}/"):
        for o in page.get("Contents", []):
            name = o["Key"][len(prefix) + 1:]
            if not name or name == "manifest.json":
                continue
            body = cli.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read()
            with open(os.path.join(dest_folder, name), "wb") as f:
                f.write(body)
            n += 1
    return n


def write_index(mapping, client=None):
    """Upload the appid->folder map to the bucket root so the cloud app can resolve
    a lead's folder from its appid."""
    cli = client or _client()
    cli.put_object(Bucket=BUCKET, Key=INDEX_KEY,
                   Body=json.dumps(mapping, ensure_ascii=False).encode("utf-8"),
                   ContentType="application/json")
