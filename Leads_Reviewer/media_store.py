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


# -- read side (cloud app) ------------------------------------------------
def read_enabled():
    """True if cards can be served from R2 (public base configured)."""
    return bool(PUBLIC_BASE)


def public_url(appid, filename):
    return f"{PUBLIC_BASE}/{appid}/{filename}"


def fetch_manifest(appid):
    """The lead's manifest dict from R2, or None if absent/unreachable.
    A browser-ish User-Agent is required — r2.dev's edge 403s default urllib (CF
    error 1010, bot-signature block)."""
    try:
        req = urllib.request.Request(public_url(appid, "manifest.json"),
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception:
        return None


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


def upload_dir(folder, appid, card, client=None):
    """Mirror one lead folder to R2: every image + a fresh manifest.json.
    Idempotent-ish — re-uploads overwrite (cheap; R2 has no per-PUT charge worth
    optimizing for a few hundred small files). Returns the manifest."""
    cli = client or _client()
    manifest = build_manifest(folder, appid, card)
    for name in manifest["images"]:
        ctype = "image/png" if name.lower().endswith(".png") else "image/jpeg"
        with open(os.path.join(folder, name), "rb") as fh:
            cli.put_object(Bucket=BUCKET, Key=f"{appid}/{name}", Body=fh.read(),
                           ContentType=ctype)
    cli.put_object(Bucket=BUCKET, Key=f"{appid}/manifest.json",
                   Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                   ContentType="application/json")
    return manifest
