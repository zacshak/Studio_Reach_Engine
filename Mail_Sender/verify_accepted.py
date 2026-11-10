"""Verify games the dashboard accepted (Mail_status 'Verifying') -> Accepted or back.

Fired by the dashboard's Accept / Accept all (verify.yml). For each Verifying game:
  1. Verify its email: cached result, else own SMTP check, QEV only if inconclusive.
     invalid  -> email cleared, back to Triaged/Pending as "no email" (email_issue says why)
     valid / unknown -> continue (the send step re-checks unknown addresses before sending)
  2. Make sure its media (screenshots + sprite sheet + manifest) is in R2. Games whose
     media was purged after an earlier send (e.g. after a bounce) are rebuilt from Steam.
     If that fails the game returns to Triaged with its email kept and email_issue set.
  3. Verifying -> Accepted.

Needs outbound port 25 for the SMTP check (open on GitHub-hosted runners).

    python SRE.py --verify-accepted
"""
import json
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
sys.path.insert(0, ROOT)
import pipeline      # noqa: E402
import media_store   # noqa: E402
from Email_Verifier import EmailVerifier  # noqa: E402

SPRITE = "SpriteSheet_Screenshots.png"


class MediaError(RuntimeError):
    pass


def media_ready(appid, client=None):
    """Same test as the dashboard's Accept: indexed folder, manifest lists the sprite
    sheet, and the sprite object is non-empty."""
    folder = media_store.fetch_index(strict=True).get(str(appid))
    if not folder:
        return False
    manifest = media_store.fetch_manifest(folder, strict=True) or {}
    if SPRITE not in (manifest.get("images") or []):
        return False
    try:
        head = (client or media_store._client()).head_object(
            Bucket=media_store.BUCKET, Key=f"{folder}/{SPRITE}")
    except Exception as exc:
        if media_store._error_code(exc) in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return int(head.get("ContentLength") or 0) > 0


def rebuild_media(appid, client=None):
    """Rebuild a game's review media from Steam and publish it to R2."""
    from approval_export import ApprovalExporter
    from fetch_app import fetch_appdetails
    import sync_media

    lead = pipeline.read_lead(appid) or fetch_appdetails(int(appid))
    if not lead:
        raise MediaError("Steam has no store page for this game any more")
    if not lead.get("screenshots"):
        raise MediaError("Steam lists no screenshots for this game")
    with tempfile.TemporaryDirectory(prefix="sre-media-") as base:
        exporter = ApprovalExporter(base_dir=base, max_workers=1)
        try:
            if not exporter.export_lead(int(appid), lead):
                raise MediaError(f"media export failed: {exporter.errors[-1][1] if exporter.errors else 'unknown'}")
        finally:
            exporter.close()
        folder = next(os.path.join(base, d) for d in os.listdir(base) if d.endswith(f"_{appid}"))
        with open(os.path.join(folder, f"{appid}.json"), encoding="utf-8") as f:
            card = sync_media._card(json.load(f), appid)
        prefix = media_store.upload_dir(folder, int(appid), card, client=client)
    media_store.update_index(lambda current: {**current, str(appid): prefix}, client=client)


def ensure_media(appid, client=None):
    """Return 'ok' or 'rebuilt'; raise MediaError when it cannot be made ready."""
    if media_ready(appid, client):
        return "ok"
    rebuild_media(appid, client)
    if not media_ready(appid, client):
        raise MediaError("rebuilt media is still incomplete")
    return "rebuilt"


def _verify(verifier, email):
    result = pipeline.get_email_verification(email)
    if result is not None:
        return result, True
    result = verifier.verify(email)
    try:
        pipeline.cache_email_verification(email, result)
    except Exception as exc:
        print(f"  WARN cache write failed for {email}: {exc}")
        pipeline.reconnect()
    return result, False


def main():
    rows = pipeline.verifying_rows()
    print(f"{len(rows)} game(s) waiting in Verifying")
    stats = dict(accepted=0, media_rebuilt=0, no_mail=0, returned=0, skipped=0)
    if not rows:
        _summary(stats)
        return 0
    verifier = EmailVerifier.from_env()
    client = media_store._client() if media_store.write_enabled() else None
    if client is None:
        print("R2 write credentials are not set; media cannot be checked or rebuilt.")
    failures = 0
    for appid, raw, _scrape in rows:
        email = pipeline.normalize_email(raw)
        try:
            if not email:
                outcome = pipeline.reject_email(
                    appid, expected_email=raw, reason="no deliverable address", source="accept",
                    from_statuses=("Verifying",), researched_to_invalid=False)
                print(f"NO MAIL {appid}: stored email {raw!r} is not an address")
                stats["no_mail" if outcome else "skipped"] += 1
                continue
            result, cached = _verify(verifier, email)
            verdict = str(result.get("result", "")).lower() if isinstance(result, dict) else ""
            reason = result.get("reason", "") if isinstance(result, dict) else ""
            if "email" in result and str(result["email"]).strip().casefold() != email.casefold():
                raise RuntimeError(f"verification answered for a different address: {result['email']!r}")
            print(f"  {appid} {email}: {verdict or '?'} ({reason}){' [cached]' if cached else ''}")
            if verdict == "invalid":
                outcome = pipeline.reject_email(
                    appid, expected_email=raw, reason=reason or "invalid", source="accept",
                    from_statuses=("Verifying",), researched_to_invalid=False)
                stats["no_mail" if outcome else "skipped"] += 1
                continue
            if verdict not in ("valid", "unknown"):
                raise RuntimeError(f"unexpected verification result {verdict!r}")
            if client is None:
                raise MediaError("R2 credentials missing")
            try:
                state = ensure_media(appid, client)
            except Exception as exc:
                print(f"  RETURN {appid}: media not ready: {exc}")
                ok = pipeline.return_unverified(
                    appid, expected_email=raw, reason=f"media could not be rebuilt: {exc}")
                stats["returned" if ok else "skipped"] += 1
                continue
            if state == "rebuilt":
                stats["media_rebuilt"] += 1
                print(f"  {appid}: media rebuilt from Steam")
            if pipeline.finish_verification(appid, expected_email=raw):
                stats["accepted"] += 1
            else:
                stats["skipped"] += 1
                print(f"  SKIP {appid}: row changed while verifying")
        except Exception as exc:
            failures += 1
            traceback.print_exc()
            try:
                ok = pipeline.return_unverified(appid, expected_email=raw,
                                                reason=f"verification failed: {exc}")
                stats["returned" if ok else "skipped"] += 1
            except Exception:
                pipeline.reconnect()
    _summary(stats)
    return 1 if failures else 0


def _summary(stats):
    print("done: " + json.dumps(stats), flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write("### Verify accepted games\n\n| Outcome | Count |\n| --- | ---: |\n")
            for name, count in stats.items():
                out.write(f"| {name} | {count} |\n")


if __name__ == "__main__":
    raise SystemExit(main())
