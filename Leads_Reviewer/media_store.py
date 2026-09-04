"""R2 media store — the bridge that lets the cloud review app show game art.

Local media (screenshots + sprite sheet + curated JSON) lives in gitignored
folders, so it isn't in the GitHub repo the cloud app deploys from. This module
mirrors each lead's media to a Cloudflare R2 bucket and serves it back by public
URL, so the review app (webapp/, a Cloudflare Worker) can render it.

Two halves, deliberately split so the CLOUD app stays dependency-light:
  - READ  (public_url / fetch_manifest): authenticated S3 when credentials exist,
    with the legacy public URL retained only as a local fallback.
  - WRITE (upload_dir): boto3 (S3-compatible R2 API), imported lazily. Used only
    by sync_media.py on the local machine.

Env (repo-root .env locally, GHA/Worker secrets in cloud):
    R2_PUBLIC_BASE=https://pub-xxxx.r2.dev   # bucket public URL (read side)
    R2_BUCKET=sre-media                       # write side ↓
    R2_ACCOUNT_ID=...
    R2_ACCESS_KEY_ID=...
    R2_SECRET_ACCESS_KEY=...

Layout in the bucket: each lead folder contains its images + manifest.json; root
index.json maps appids to folders. A manifest is one HTTP GET — no DB round-trip
for display.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def _load_env():
    """Pull KEY=VALUE from the repo-root .env into os.environ (local convenience;
    doesn't override anything already set — e.g. secrets already in the environment
    in CI/cloud)."""
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
TEMPLATES_KEY = "cold_mails.txt"      # the cold-mail templates, persisted in R2 so they
                                      # can be edited without a redeploy (the drafter reads here)
_UA = {"User-Agent": "Mozilla/5.0"}   # r2.dev 403s default urllib (CF bot block, err 1010)


class MediaStoreError(RuntimeError):
    """Raised when a write path cannot safely read its current R2 state."""


# -- read side (cloud app) ------------------------------------------------
def read_enabled():
    """True if cards can be served from R2."""
    return write_enabled() or bool(PUBLIC_BASE)


def _enc(seg):
    # percent-encode a single path segment: game names have spaces + non-ASCII, which
    # raw urllib won't send and r2.dev won't match without encoding.
    return urllib.parse.quote(seg, safe="")


def public_url(folder, filename):
    if write_enabled():
        return _client().generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET, "Key": f"{folder}/{filename}"},
            ExpiresIn=3600)
    return f"{PUBLIC_BASE}/{_enc(folder)}/{_enc(filename)}"


def fetch_bytes(folder, filename, strict=False):
    """Read one object privately when credentials are configured."""
    key = f"{folder}/{filename}" if folder else filename
    try:
        if write_enabled():
            return _client().get_object(Bucket=BUCKET, Key=key)["Body"].read()
        path = "/".join(_enc(segment) for segment in key.split("/"))
        req = urllib.request.Request(f"{PUBLIC_BASE}/{path}", headers=_UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        if exc.code != 404 and strict:
            raise MediaStoreError(f"R2 read failed for {key}: HTTP {exc.code}") from exc
        return None
    except Exception as exc:
        if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
            return None
        if strict:
            raise MediaStoreError(f"R2 read failed for {key}: {exc}") from exc
        return None


def _get_json(key, strict=False):
    """Read and decode a JSON object."""
    raw = fetch_bytes(None, key, strict)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        if strict:
            raise MediaStoreError(f"R2 JSON invalid for {key}: {exc}") from exc
        return None


def _get_text(key, strict=False):
    raw = fetch_bytes(None, key, strict)
    return raw.decode("utf-8") if raw is not None else None


def fetch_index(strict=False):
    """{ '<appid>': '<GameName>_<appid>' } — appid→folder map, or {} if absent."""
    value = _get_json(INDEX_KEY, strict)
    if value is None:
        return {}
    if not isinstance(value, dict):
        if strict:
            raise MediaStoreError("R2 index.json is not an object")
        return {}
    return value


def fetch_templates(strict=False):
    """The cold-mail templates text stored in R2, or None if not uploaded yet."""
    return _get_text(TEMPLATES_KEY, strict)


def fetch_manifest(folder, strict=False):
    """The lead's manifest dict from R2 (by folder name), or None if unreachable."""
    return _get_json(f"{folder}/manifest.json", strict)


def fetch_irrelevant(strict=False):
    """List of appids the cloud triage flagged as irrelevant, or [] if none/absent.
    The app shows a reject-review gate while this is non-empty."""
    value = _get_json(IRRELEVANT_KEY, strict)
    if value is None:
        return []
    if not isinstance(value, list):
        if strict:
            raise MediaStoreError("R2 irrelevant.json is not an array")
        return []
    return value


# -- write side (boto3, imported lazily) ----------------------------------
# Used by sync_media and the Python triage/reviewer actions, so those processes need
# boto3 and R2 write credentials.
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
            cli.put_object(Bucket=BUCKET, Key=f"{prefix}/{name}", Body=fh,
                           ContentType=ctype)
    cli.put_object(Bucket=BUCKET, Key=f"{prefix}/manifest.json",
                   Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                   ContentType="application/json")
    return prefix


def _error_code(exc):
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


def _update_json(key, fallback, mutate, client=None):
    """Conditionally update one shared JSON object without losing concurrent writes."""
    cli = client or _client()
    for _ in range(5):
        try:
            obj = cli.get_object(Bucket=BUCKET, Key=key)
            current = json.loads(obj["Body"].read())
            condition = {"IfMatch": obj["ETag"]}
        except Exception as exc:
            if _error_code(exc) not in ("404", "NoSuchKey", "NotFound"):
                raise MediaStoreError(f"R2 read failed for {key}: {exc}") from exc
            current = fallback.copy()
            condition = {"IfNoneMatch": "*"}
        if not isinstance(current, type(fallback)):
            raise MediaStoreError(f"R2 {key} has the wrong JSON type")
        updated = mutate(current.copy())
        if not isinstance(updated, type(fallback)):
            raise TypeError(f"R2 {key} update returned the wrong JSON type")
        try:
            cli.put_object(
                Bucket=BUCKET,
                Key=key,
                Body=json.dumps(updated, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
                **condition,
            )
            return updated
        except Exception as exc:
            if _error_code(exc) not in ("409", "412", "ConditionalRequestConflict",
                                        "PreconditionFailed"):
                raise MediaStoreError(f"R2 write failed for {key}: {exc}") from exc
    raise MediaStoreError(f"concurrent R2 update did not settle for {key}")


def update_index(mutate, client=None):
    return _update_json(INDEX_KEY, {}, mutate, client)


def update_irrelevant(mutate, client=None):
    return _update_json(IRRELEVANT_KEY, [], mutate, client)


def write_manifest(folder, manifest, client=None):
    """Overwrite a lead's manifest.json in R2 (by folder name). Used by the cloud mail
    drafter to write the generated 'mail' + 'mail_template' back into the manifest the
    review app and the sender both read."""
    cli = client or _client()
    cli.put_object(Bucket=BUCKET, Key=f"{os.path.basename(folder.rstrip('/'))}/manifest.json",
                   Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
                   ContentType="application/json")


def write_templates(text, client=None):
    """Upload the cold-mail templates text to R2 (the drafter's source of truth)."""
    cli = client or _client()
    cli.put_object(Bucket=BUCKET, Key=TEMPLATES_KEY, Body=text.encode("utf-8"),
                   ContentType="text/plain; charset=utf-8")


def delete_lead_media(appid, client=None):
    """Purge one lead's media from R2: delete every object under its folder and drop it
    from the index. Used by the app's Reject. No-op if the appid isn't indexed."""
    cli = client or _client()
    obj = cli.get_object(Bucket=BUCKET, Key=INDEX_KEY)
    folder = json.loads(obj["Body"].read()).get(str(appid))
    if folder:
        keys = [o["Key"] for page in cli.get_paginator("list_objects_v2")
                .paginate(Bucket=BUCKET, Prefix=f"{folder}/")
                for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            result = cli.delete_objects(
                Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
            if result.get("Errors"):
                raise MediaStoreError(f"R2 delete failed: {result['Errors']}")

        def remove(index):
            if index.get(str(appid)) == folder:
                index.pop(str(appid))
            return index

        update_index(remove, client=cli)
    return bool(folder)
