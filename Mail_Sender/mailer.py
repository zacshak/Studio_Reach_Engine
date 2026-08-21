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
import os
import random
import shutil
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
sys.path.insert(0, ROOT)
import pipeline      # noqa: E402
import media_store   # noqa: E402  (cloud send: draft from R2 manifest, media purge in R2)
from Email_Verifier import QEVError, QuickEmailVerification, is_safe_to_send  # noqa: E402

MEDIA_DIR = os.path.join(ROOT, "Leads_Reviewer", "Studios_To_Review", "Approval_Pending_Games")
# Cloud (GHA send job): no local media folders — drafts + media live in R2. Same test
# the reviewer uses: R2 readable AND the staged dir absent.
_CLOUD = media_store.read_enabled() and not os.path.isdir(MEDIA_DIR)

SENDER_NAME = "Meshak"              # the From display name
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
IMAP_HOST = "imap.gmail.com"
MIN_GAP, MAX_GAP = 120, 240        # 2–4 min between sends, randomized


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
        folder = media_store.fetch_index(strict=True).get(str(appid))
        man = media_store.fetch_manifest(folder, strict=True) if folder else None
        text = (man or {}).get("mail")
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
def _send(user, password, to, subject, body):
    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{user}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, password)
        s.send_message(msg)


def _recipient(raw):
    """First stored address, normalized; empty means it is not deliverable."""
    return pipeline.normalize_email(raw)


def main(dry_run=False, limit=None):
    _load_env()
    user = os.environ.get("GMAIL_USER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user:
        sys.exit("GMAIL_USER not set. Add it to the repo-root .env or GHA secret.")
    if not dry_run and not password:
        sys.exit("GMAIL_APP_PASSWORD not set. Add it to the repo-root .env "
                 "(see this file's header). Or use --dry-run.")
    verifier = None if dry_run else QuickEmailVerification.from_env(timeout=60)

    uncertain = pipeline.mail_status_appids("Sending")
    if uncertain and not dry_run:
        sys.exit("Unresolved Sending mail(s): " + ", ".join(map(str, uncertain)) +
                 ". Check Gmail Sent, then run: python SRE.py --send-mails "
                 "--resolve-sending APPID sent|retry")

    scheduled = pipeline.mail_status_emails("Scheduled")
    valid = []
    invalid = []
    for appid, raw in scheduled:
        to = _recipient(raw)
        if to:
            valid.append((appid, to))
            continue
        invalid.append((appid, raw))
    for appid, raw in invalid:
        state = pipeline.email_state(raw).upper()
        print(f"  {state} {appid}: {raw!r} -> removed from outreach")
        if not dry_run:
            pipeline.quarantine_unusable(appid)
    room = limit
    sending = len(valid) if room is None else min(len(valid), room)
    print(f"scheduled: {len(scheduled)} ({len(valid)} syntactically valid) | "
          f"sending up to: {sending}{' (DRY RUN)' if dry_run else ''}")
    if not valid or room == 0:
        if scheduled and room == 0 and limit is not None:
            print("per-run limit reached.")
        return

    done = 0
    for appid, to in valid:
        if room is not None and done >= room:
            break
        subject, body, path = _load_mail(appid)
        if body is None:
            print(f"  SKIP {appid}: no mail draft found -> returned to Drafted")
            if not dry_run:
                pipeline.set_mail_status(appid, "Drafted")
            continue
        if dry_run:
            print(f"  [dry] {appid} -> {to} | {subject!r} | {os.path.basename(path)}")
            done += 1
            continue
        result = pipeline.get_email_verification(to)
        if result is None:
            try:
                result = verifier.verify(to)
            except QEVError as exc:
                if exc.status_code == 402:
                    print("QEV credits exhausted; stopping. Remaining mails stay Scheduled.")
                    return
                raise
            pipeline.cache_email_verification(to, result)
        else:
            print(f"  CACHED {appid} -> {to}: QEV result reused")
        verification = str(result.get("result", "")).lower()
        if verification == "unknown":
            print(f"  SKIP {appid} -> {to}: verification unknown "
                  f"({result.get('reason', 'no reason')}); left Scheduled")
            continue
        if verification not in ("valid", "invalid"):
            raise QEVError(f"unexpected verification result for {to}: {verification!r}")
        if verification == "invalid" or not is_safe_to_send(result):
            reason = result.get("reason") or "unsafe recipient"
            pipeline.quarantine_verified_invalid(appid)
            print(f"  INVALID {appid} -> {to}: {reason}; removed from outreach")
            continue
        if done:
            gap = random.randint(MIN_GAP, MAX_GAP)
            print(f"    waiting {gap}s before next…")
            time.sleep(gap)
        if not pipeline.claim_mail(appid):
            print(f"  SKIP {appid}: no longer Scheduled")
            continue
        try:
            _send(user, password, to, subject, body)
        except smtplib.SMTPRecipientsRefused as e:
            pipeline.reset_sending(appid, "Drafted")
            print(f"  FAIL {appid} -> {to}: {e} -> returned to Drafted")
            continue
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPHeloError,
                smtplib.SMTPSenderRefused, smtplib.SMTPNotSupportedError):
            pipeline.reset_sending(appid, "Scheduled")
            raise
        except Exception as e:
            print(f"  UNCERTAIN {appid} -> {to}: {e}; left as Sending to prevent a duplicate")
            raise
        pipeline.mark_sent(appid)
        try:
            _delete_media(appid)                # sent -> drop its media folder
        except Exception as e:
            print(f"  WARN {appid}: sent, but media cleanup failed: {e}")
        done += 1
        print(f"  sent {appid} -> {to} | {subject!r}")

    print(f"done: {done} {'previewed' if dry_run else 'sent'} this run.")


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
        main(dry_run="--dry-run" in args, limit=lim)
