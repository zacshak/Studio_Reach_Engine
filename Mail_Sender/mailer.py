"""Send the approved outreach mails from your Gmail, paced to dodge spam/soft-ban.

Picks up every lead the Leads Reviewer approved (Mail_status == 'Scheduled'),
reads its drafted mail (mail_<appid>_<template>.txt in the game's media folder),
sends it to the lead's email via Gmail SMTP, then flips Mail_status -> 'Sent'.

Spam / soft-ban guards:
  - a randomized gap between each send (MIN_GAP..MAX_GAP seconds)
  - plain-text mail, one recipient per message, real From name

Setup (one time):
  1. Google account -> Security -> 2-Step Verification ON -> App passwords ->
     generate one for "Mail". You get a 16-char password.
  2. Put it in a .env at the repo root (gitignored):
         GMAIL_USER=you@example.com
         GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
Run:
    python mailer.py --dry-run     # preview what WOULD send, send nothing
    python mailer.py               # actually send all scheduled mails, paced by gaps
    python mailer.py --limit 5     # send at most 5 this run
    python mailer.py --review      # check Sent leads for replies, flip -> 'Replied'
"""
import glob
import imaplib
import json
import math
import os
import random
import re
import shutil
import smtplib
import ssl
import sys
import time
import traceback
import uuid
from contextlib import suppress
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
sys.path.insert(0, ROOT)
import pipeline      # noqa: E402
import media_store   # noqa: E402  (cloud send: draft from R2 manifest, media purge in R2)
from Email_Verifier import QEVError, QuickEmailVerification  # noqa: E402

MEDIA_DIR = os.path.join(ROOT, "Leads_Reviewer", "Studios_To_Review", "Approval_Pending_Games")
# Cloud (GHA send job): no local media folders — drafts + media live in R2. Same test
# the reviewer uses: R2 readable AND the staged dir absent.
_CLOUD = media_store.read_enabled() and not os.path.isdir(MEDIA_DIR)

SENDER_NAME = "Meshak"              # the From display name
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
IMAP_HOST = "imap.gmail.com"
MIN_GAP, MAX_GAP = 120, 240        # 2–4 min between sends, randomized
SMTP_RETRY_DELAYS = (300, 900)     # explicit temporary rejections only, not a send-count cap


# -- env -------------------------------------------------------------------
def _load_env():
    """Read KEY=VALUE lines from the repo-root .env into the environment."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# -- mail loading ----------------------------------------------------------
def _folder_for(appid):
    suffix = f"_{appid}"
    for entry in os.listdir(MEDIA_DIR) if os.path.isdir(MEDIA_DIR) else []:
        full = os.path.join(MEDIA_DIR, entry)
        if entry.endswith(suffix) and os.path.isdir(full):
            return full
    return None


def _delete_media(appid):
    """Remove a lead's media (mail + screenshots + json) after a send. The DB row stays
    (it tracks Sent status + sent_at). Cloud: purge from R2; local: rmtree the folder.
    Returns True if something was removed."""
    if _CLOUD:
        return bool(media_store.delete_lead_media(appid))
    folder = _folder_for(appid)
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
        return True
    return False


def _split_subject(text):
    """First 'Subject: ...' line -> (subject, body). Falls back gracefully."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if line.strip().lower().startswith("subject"):
            subject = line.split(":", 1)[1].strip() if ":" in line else ""
            body = "\n".join(lines[i + 1:]).strip()
            return (subject or "Hello"), body
        break                                   # first real line isn't a subject
    return "Hello", text.strip()


def _load_mail(appid):
    """(subject, body, path) for a lead, using its approved template, else the
    first variant. Returns (None, None, None) if no mail file exists.

    Cloud: the chosen draft already lives in the lead's R2 manifest ('mail'), written at
    sync time — no local folder to read, so pull it from there."""
    if _CLOUD:
        index = media_store.fetch_index(strict=True)
        if not isinstance(index, dict):
            raise media_store.MediaStoreError("R2 index is not an object")
        folder = index.get(str(appid))
        man = media_store.fetch_manifest(folder, strict=True) if folder else None
        if man is not None and not isinstance(man, dict):
            raise media_store.MediaStoreError(f"R2 manifest for {appid} is not an object")
        text = (man or {}).get("mail")
        if text is not None and not isinstance(text, str):
            raise media_store.MediaStoreError(f"R2 draft for {appid} is not text")
        if not text:
            return None, None, None
        subject, body = _split_subject(text)
        return subject, body, f"R2:{folder}"
    folder = _folder_for(appid)
    if not folder:
        return None, None, None
    tpl = pipeline.get_mail_template(appid)
    path = os.path.join(folder, f"mail_{appid}_{tpl}.txt") if tpl else None
    if not path or not os.path.exists(path):
        hits = sorted(glob.glob(os.path.join(folder, f"mail_{appid}_*.txt")))
        path = hits[0] if hits else None
    if not path:
        return None, None, None
    with open(path, encoding="utf-8") as f:
        subject, body = _split_subject(f.read())
    return subject, body, path


# -- sending ---------------------------------------------------------------
class MailNotSubmittedError(RuntimeError):
    """A failure before submitting DATA, so retry cannot duplicate a delivery."""


def _send(user, password, to, subject, body, *, message_id=None):
    s = None
    try:
        msg = EmailMessage()
        msg["From"] = f"{SENDER_NAME} <{user}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = message_id or make_msgid(domain=user.rsplit("@", 1)[-1])
        msg["Date"] = formatdate(usegmt=True)
        msg.set_content(body)
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
    except BaseException as exc:
        if s is not None:
            with suppress(Exception):
                s.close()
        raise MailNotSubmittedError(str(exc)) from exc
    try:
        refused = s.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    finally:
        # DATA's reply determines delivery. A failed QUIT must not mask that reply.
        try:
            s.quit()
        except Exception:
            with suppress(Exception):
                s.close()


def _persist_delivery(operation, *args, **kwargs):
    """Only ownership-guarded resets and idempotent Sent completion use this retry."""
    for attempt in range(4):
        try:
            operation(*args, **kwargs)
            return True
        except Exception as exc:
            if not pipeline.retryable_db_error(exc) or attempt == 3:
                print(f"WARN {args[0]}: delivery state write failed: {exc}")
                return False
            pipeline.reconnect()
            time.sleep(2 ** attempt)


def _deliver(appid, to, subject, body, raw_email, user, password, deadline):
    token = uuid.uuid4().hex
    message_id = make_msgid(idstring=f"sre.{appid}", domain=user.rsplit("@", 1)[-1])
    for attempt in range(len(SMTP_RETRY_DELAYS) + 1):
        if time.monotonic() + 120 >= deadline:
            print(f"DEFER {appid}: runner time budget reached. Left Scheduled.")
            return "deferred"
        if not pipeline.claim_mail(appid, token=token, message_id=message_id,
                                   expected_email=raw_email):
            print(f"SKIP {appid}: state or recipient changed before the send claim")
            return "skipped"
        try:
            _send(user, password, to, subject, body, message_id=message_id)
        except (MailNotSubmittedError, smtplib.SMTPResponseException,
                smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError,
                ValueError, TypeError, UnicodeError) as exc:
            cause = exc.__cause__ if isinstance(exc, MailNotSubmittedError) else exc
            if isinstance(cause, (KeyboardInterrupt, SystemExit)):
                _persist_delivery(pipeline.reset_sending, appid, "Scheduled", token=token)
                raise KeyboardInterrupt from cause
            codes = ([code for code, _ in cause.recipients.values()]
                     if isinstance(cause, smtplib.SMTPRecipientsRefused)
                     else [getattr(cause, "smtp_code", 0)])
            setup_failure = isinstance(exc, MailNotSubmittedError)
            draft_failure = isinstance(cause, (ValueError, TypeError, UnicodeError))
            account_failure = isinstance(cause, (
                smtplib.SMTPAuthenticationError, smtplib.SMTPHeloError,
                smtplib.SMTPSenderRefused)) or (
                    setup_failure and isinstance(cause, smtplib.SMTPNotSupportedError))
            temporary = any(400 <= code < 500 for code in codes) or (
                setup_failure and not draft_failure and not account_failure)
            target = "Scheduled" if temporary or account_failure else "Drafted"
            if not _persist_delivery(pipeline.reset_sending, appid, target, token=token):
                print(f"UNRECORDED {appid}: SMTP did not accept the mail. Check its DB state.")
                return "uncertain"
            print(f"REJECTED {appid}: {cause}. Returned to {target}.")
            if account_failure:
                return "halted"
            if not temporary:
                return "rejected"
            if attempt == len(SMTP_RETRY_DELAYS):
                return "deferred"
            delay = SMTP_RETRY_DELAYS[attempt]
            if time.monotonic() + delay + 120 >= deadline:
                return "deferred"
            print(f"RETRY {appid}: retrying known-unsent mail in {delay}s")
            time.sleep(delay)
        except BaseException as exc:
            print(f"UNCERTAIN {appid}: no SMTP acceptance/rejection could be established: {exc}. "
                  f"Left Sending, Message-ID {message_id}.")
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                return "interrupted"
            return "uncertain"
        else:
            if not _persist_delivery(pipeline.mark_sent, appid, token=token):
                print(f"ACCEPTED {appid}: SMTP accepted Message-ID {message_id}. "
                      "DB completion is unconfirmed. Do not resend.")
                return "accepted_unrecorded"
            try:
                _delete_media(appid)
            except Exception as exc:
                print(f"WARN {appid}: Sent recorded, but media cleanup failed: {exc}")
            print(f"  sent {appid} -> {to} | {subject!r}")
            return "sent"


def _reconcile_sending(user, password):
    """A positive exact Message-ID match in Gmail Sent is proof. Absence is not."""
    pending = pipeline.mail_status_appids("Sending")
    if not pending:
        return 0
    imap = None
    remaining = set(pending)
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
        imap.login(user, password)
        typ, folders = imap.list()
        if typ != "OK":
            raise RuntimeError("cannot list Gmail mailboxes")
        sent_folder = '"[Gmail]/Sent Mail"'
        for folder in folders or []:
            match = re.match(rb'\([^)]*\\Sent\b[^)]*\)\s+(?:"[^"]*"|NIL)\s+(.+)',
                             folder or b"", re.IGNORECASE)
            if match:
                sent_folder = match.group(1).decode("ascii")
                break
        if imap.select(sent_folder, readonly=True)[0] != "OK":
            raise RuntimeError("cannot open Gmail Sent")
        for appid in pending:
            stored = pipeline.get_send_attempt(appid)
            if not stored or not stored[1]:
                continue
            token, message_id = stored
            typ, data = imap.search(None, "HEADER", "Message-ID", f'"{message_id}"')
            if typ == "OK" and data and data[0].split():
                if _persist_delivery(pipeline.mark_sent, appid, token=token):
                    remaining.discard(appid)
                    print(f"RECOVERED {appid}: found its exact Message-ID in Gmail Sent")
                    try:
                        _delete_media(appid)
                    except Exception as exc:
                        print(f"WARN {appid}: recovered Sent, but media cleanup failed: {exc}")
    except Exception as exc:
        print(f"WARN: Gmail Sent reconciliation failed: {exc}")
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass
    if remaining:
        print("WARN: unresolved Sending rows require review: " +
              ", ".join(map(str, sorted(remaining))) + ". Other Scheduled mails can proceed.")
    return len(remaining)


def _recipient(raw):
    """First stored address, normalized; empty means it is not deliverable."""
    return pipeline.normalize_email(raw)


def _summary(stats, unprocessed, error):
    text = "done: " + json.dumps({**stats, "unprocessed": unprocessed, "error": error})
    print(text, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as output:
                output.write("### Send results\n\n| Outcome | Count |\n| --- | ---: |\n")
                for name, count in {**stats, "unprocessed": unprocessed}.items():
                    output.write(f"| {name} | {count} |\n")
                if error:
                    output.write(f"\nStopped: {error}\n")
        except OSError as exc:
            print(f"WARN: could not write the Actions summary: {exc}")


def main(dry_run=False, limit=None):
    _load_env()
    user = os.environ.get("GMAIL_USER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user:
        sys.exit("GMAIL_USER not set. Add it to the repo-root .env or GHA secret.")
    if not dry_run and not password:
        sys.exit("GMAIL_APP_PASSWORD not set. Add it to the repo-root .env "
                 "(see this file's header). Or use --dry-run.")
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a nonnegative integer")
    budget = float(os.environ.get("SEND_TIME_BUDGET_SECONDS", "0"))
    if budget < 0 or not math.isfinite(budget):
        raise ValueError("SEND_TIME_BUDGET_SECONDS must be finite and nonnegative")
    deadline = time.monotonic() + budget if budget else float("inf")
    verifier = None
    uncertain = 0 if dry_run else _reconcile_sending(user, password)

    scheduled = pipeline.mail_status_emails("Scheduled")
    valid = []
    invalid = []
    for appid, raw in scheduled:
        to = _recipient(raw)
        if to:
            valid.append((appid, to, raw))
            continue
        invalid.append((appid, raw))
    for appid, raw in invalid:
        state = pipeline.email_state(raw).upper()
        print(f"  {state} {appid}: {raw!r} -> removed from outreach")
        if not dry_run:
            pipeline.quarantine_unusable(appid)
            if state == "INVALID":
                try:
                    _delete_media(appid)
                except Exception as e:
                    print(f"  WARN {appid}: quarantined, but media cleanup failed: {e}")
    room = limit
    sending = len(valid) if room is None else min(len(valid), room)
    malformed = sum(pipeline.email_state(raw) == "invalid" for _, raw in invalid)
    stats = dict(sent=0, accepted_unrecorded=0, deferred=0, invalid=malformed,
                 missing_email=len(invalid) - malformed,
                 unknown=0, rejected=0, uncertain=uncertain, skipped=0)
    print(f"scheduled: {len(scheduled)} ({len(valid)} syntactically valid) | "
          f"sending up to: {sending}{' (DRY RUN)' if dry_run else ''}")
    if not valid or room == 0:
        if scheduled and room == 0 and limit is not None:
            print("per-run limit reached.")
        _summary(stats, len(valid), "")
        return int(bool(uncertain))

    processed, attempted, error = 0, False, ""
    verified = {}
    try:
        for appid, to, raw in valid:
            if room is not None and stats["sent"] + stats["accepted_unrecorded"] >= room:
                break
            if time.monotonic() + 120 >= deadline:
                print("Runner time budget reached. Unprocessed mails stay Scheduled.")
                break
            processed += 1
            try:
                subject, body, path = _load_mail(appid)
            except media_store.MediaStoreError as exc:
                print(f"DEFER {appid}: draft storage unavailable: {exc}")
                stats["deferred"] += 1
                continue
            if not isinstance(body, str) or not body.strip():
                print(f"SKIP {appid}: missing or empty draft -> returned to Drafted")
                if not dry_run:
                    pipeline.set_mail_status(appid, "Drafted")
                stats["rejected"] += 1
                continue
            if dry_run:
                print(f"  [dry] {appid} -> {to} | {subject!r} | {os.path.basename(path)}")
                stats["sent"] += 1
                continue
            key = to.casefold()
            result = verified.get(key)
            if result is None:
                result = pipeline.get_email_verification(to)
                if result is None:
                    verifier = verifier or QuickEmailVerification.from_env(timeout=60)
                    result = verifier.verify(to)
                    try:
                        pipeline.cache_email_verification(to, result)
                    except Exception as exc:
                        print(f"WARN {appid}: verification cache write failed: {exc}")
                        pipeline.reconnect()
                else:
                    print(f"  CACHED {appid} -> {to}: QEV result reused")
                verified[key] = result
            verification = str(result.get("result", "")).lower() if isinstance(result, dict) else ""
            if verification not in ("valid", "invalid", "unknown"):
                raise QEVError(f"unexpected verification result for {to}: {verification!r}")
            if "email" in result and (not isinstance(result["email"], str) or
                                      result["email"].strip().casefold() != key):
                raise QEVError(f"verification response does not match recipient {to}")
            if verification == "unknown":
                print(f"SKIP {appid}: verification unknown ({result.get('reason', 'no reason')}). "
                      "Left Scheduled.")
                stats["unknown"] += 1
                continue
            if verification == "invalid":
                if pipeline.quarantine_verified_invalid(appid, expected_email=raw):
                    try:
                        _delete_media(appid)
                    except Exception as exc:
                        print(f"WARN {appid}: invalid, but media cleanup failed: {exc}")
                    stats["invalid"] += 1
                else:
                    stats["skipped"] += 1
                continue
            if attempted:
                gap = random.randint(MIN_GAP, MAX_GAP)
                if time.monotonic() + gap + 120 >= deadline:
                    stats["deferred"] += 1
                    break
                print(f"    waiting {gap}s before next…")
                time.sleep(gap)
            attempted = True
            outcome = _deliver(appid, to, subject, body, raw, user, password, deadline)
            if outcome == "interrupted":
                stats["uncertain"] += 1
                error = "interrupted during submission. Review its Sending row."
                break
            if outcome == "halted":
                stats["deferred"] += 1
                error = "SMTP account/setup error. Remaining mails stay Scheduled."
                break
            stats[outcome] += 1
    except QEVError as exc:
        error = str(exc)
        print(f"STOP: {error}. Remaining mails stay Scheduled.")
        if exc.status_code == 402:
            error = ""  # quota exhaustion is an expected deferral, never a verification bypass
        stats["deferred"] += 1
    except KeyboardInterrupt:
        error = "interrupted. Review any Sending rows before resolving them."
        print(f"STOP: {error}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"STOP: {error}. No further sends attempted.")
        traceback.print_exc()
    finally:
        _summary(stats, len(valid) - processed, error)
    return int(bool(error or stats["uncertain"] or stats["accepted_unrecorded"]))


def purge_sent():
    """Sync: for every already-sent lead, remove its media folder and its
    newly_added row (the scrape_tracker row is kept)."""
    sent = pipeline.mail_status_appids("Sent")
    removed = 0
    for appid in sent:
        if _delete_media(appid):
            removed += 1
        pipeline.delete_newly_added(appid)
    print(f"purged {len(sent)} sent lead(s): {removed} media folder(s) removed, "
          f"newly_added rows dropped (scrape_tracker kept)")


def review():
    """For every already-sent lead (Mail_status == 'Sent'), look in the Gmail inbox
    for a message FROM that lead's address. A hit = they replied -> flip the lead's
    Mail_status to 'Replied'. Read-only on Gmail (IMAP), only the DB status changes.
    Uses the same App password as sending (works for IMAP too)."""
    _load_env()
    user = os.environ.get("GMAIL_USER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user:
        sys.exit("GMAIL_USER not set. Add it to the repo-root .env or GHA secret.")
    if not password:
        sys.exit("GMAIL_APP_PASSWORD not set. Add it to the repo-root .env "
                 "(see this file's header).")

    sent = pipeline.mail_status_appids("Sent")
    if not sent:
        print("no leads in 'Sent' to review.")
        return

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(user, password)
    imap.select("INBOX", readonly=True)

    replied = 0
    for appid in sent:
        addr = (pipeline.get_emails(appid).split(",")[0] or "").strip()
        if not addr:
            continue
        # ponytail: INBOX-only search; replies land here. Add 'All Mail' if some slip past.
        # bytes literal + UTF-8 charset so non-ASCII addresses (ø, IDN) don't crash imaplib.
        try:
            typ, data = imap.search("UTF-8", "FROM", f'"{addr}"'.encode("utf-8"))
        except imaplib.IMAP4.error:
            continue
        if typ == "OK" and data and data[0].split():
            pipeline.set_mail_status(appid, "Replied")
            replied += 1
            print(f"  REPLIED {appid} <- {addr}")
    imap.logout()
    print(f"reviewed {len(sent)} sent lead(s): {replied} replied -> marked 'Replied'.")


def _selftest():
    s, b = _split_subject("Subject : Hi there\n\nhey,\nbody line")
    assert s == "Hi there" and b == "hey,\nbody line", (s, b)
    s, b = _split_subject("no subject here\njust body")
    assert s == "Hello" and b.startswith("no subject"), (s, b)
    assert _recipient("hello@example.com, other@example.com") == "hello@example.com"
    assert not _recipient(None)
    assert not _recipient("")
    assert not _recipient("*")
    assert not _recipient("https://goodgamesnh.com/")
    assert not _recipient("nordvader email")
    assert not _recipient("cauchemargames.com")
    print("selftest ok")


def resolve_sending(appid, outcome):
    """Operator resolution for an SMTP result that could not be proven automatically."""
    if outcome == "sent":
        pipeline.mark_sent(appid)
    elif outcome == "retry":
        pipeline.reset_sending(appid, "Scheduled")
    else:
        sys.exit("resolution must be 'sent' or 'retry'")
    print(f"resolved {appid}: {'Sent' if outcome == 'sent' else 'Scheduled for retry'}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
    elif "--resolve-sending" in args:
        i = args.index("--resolve-sending")
        if len(args) <= i + 2:
            sys.exit("usage: --resolve-sending APPID sent|retry")
        resolve_sending(int(args[i + 1]), args[i + 2].lower())
    elif "--review" in args:
        review()
    elif "--purge-sent" in args:
        purge_sent()
    else:
        lim = None
        if "--limit" in args:
            lim = int(args[args.index("--limit") + 1])
        raise SystemExit(main(dry_run="--dry-run" in args, limit=lim))
