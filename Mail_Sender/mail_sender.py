"""Send the approved outreach mails from your Gmail, paced to dodge spam/soft-ban.

Picks up every lead the Leads Reviewer approved (Mail_status == 'Scheduled'),
reads its drafted mail (mail_<appid>_<template>.txt in the game's media folder),
sends it to the lead's email via Gmail SMTP, then flips Mail_status -> 'Sent'.

Spam / soft-ban guards:
  - hard daily cap (DAILY_CAP), enforced across reruns via scrape_tracker.sent_at
  - a randomized gap between each send (MIN_GAP..MAX_GAP seconds)
  - plain-text mail, one recipient per message, real From name

Setup (one time):
  1. Google account -> Security -> 2-Step Verification ON -> App passwords ->
     generate one for "Mail". You get a 16-char password.
  2. Put it in a .env at the repo root (gitignored):
         GMAIL_USER=you@example.com
         GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
Run:
    python mail_sender.py --dry-run     # preview what WOULD send, send nothing
    python mail_sender.py               # actually send, respecting the cap + gaps
    python mail_sender.py --limit 5     # send at most 5 this run
"""
import glob
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
import pipeline  # noqa: E402

MEDIA_DIR = os.path.join(ROOT, "Leads_Reviewer", "Approval_Pending_Games")

SENDER_NAME = "Meshak"              # the From display name
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
DAILY_CAP = 40                     # never send more than this per (UTC) day
MIN_GAP, MAX_GAP = 180, 480        # 3–8 min between sends, randomized


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
    """Remove a lead's media folder (mail + screenshots + json). The DB row stays
    (it tracks Sent status + sent_at). Returns True if a folder was removed."""
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
    first variant. Returns (None, None, None) if no mail file exists."""
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


def main(dry_run=False, limit=None):
    _load_env()
    user = os.environ.get("GMAIL_USER", "you@example.com")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not dry_run and not password:
        sys.exit("GMAIL_APP_PASSWORD not set. Add it to the repo-root .env "
                 "(see this file's header). Or use --dry-run.")

    scheduled = pipeline.mail_status_appids("Scheduled")
    sent_already = pipeline.sent_today()
    room = max(0, DAILY_CAP - sent_already)
    if limit is not None:
        room = min(room, limit)
    queue = scheduled[:room]

    print(f"scheduled: {len(scheduled)} | sent today: {sent_already}/{DAILY_CAP} | "
          f"sending now: {len(queue)}{' (DRY RUN)' if dry_run else ''}")
    if not queue:
        if scheduled and room == 0:
            print("daily cap reached — try again tomorrow.")
        return

    done = 0
    for i, appid in enumerate(queue):
        to = (pipeline.get_emails(appid).split(",")[0] or "").strip()
        subject, body, path = _load_mail(appid)
        if not to:
            print(f"  SKIP {appid}: no recipient email")
            continue
        if body is None:
            print(f"  SKIP {appid}: no mail draft found")
            continue
        if dry_run:
            print(f"  [dry] {appid} -> {to} | {subject!r} | {os.path.basename(path)}")
            continue
        try:
            _send(user, password, to, subject, body)
        except Exception as e:
            print(f"  FAIL {appid} -> {to}: {e}")
            continue
        pipeline.mark_sent(appid)
        _delete_media(appid)                    # sent -> drop its media folder
        done += 1
        print(f"  sent {appid} -> {to} | {subject!r}")
        if i < len(queue) - 1:                  # pace the next one
            gap = random.randint(MIN_GAP, MAX_GAP)
            print(f"    waiting {gap}s before next…")
            time.sleep(gap)

    if not dry_run:
        print(f"done: {done} sent this run.")


def purge_sent():
    """Sync: remove the media folder of every already-sent lead (rows kept)."""
    sent = pipeline.mail_status_appids("Sent")
    removed = 0
    for appid in sent:
        if _delete_media(appid):
            removed += 1
            print(f"  removed media for {appid}")
    print(f"purged {removed}/{len(sent)} sent folder(s)")


def _selftest():
    s, b = _split_subject("Subject : Hi there\n\nhey,\nbody line")
    assert s == "Hi there" and b == "hey,\nbody line", (s, b)
    s, b = _split_subject("no subject here\njust body")
    assert s == "Hello" and b.startswith("no subject"), (s, b)
    print("selftest ok")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        _selftest()
    elif "--purge-sent" in args:
        purge_sent()
    else:
        lim = None
        if "--limit" in args:
            lim = int(args[args.index("--limit") + 1])
        main(dry_run="--dry-run" in args, limit=lim)
