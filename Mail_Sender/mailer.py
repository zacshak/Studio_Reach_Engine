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
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, make_msgid, parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
sys.path.insert(0, ROOT)
import pipeline      # noqa: E402
import media_store   # noqa: E402  (cloud send: draft from R2 manifest, media purge in R2)

MEDIA_DIR = os.path.join(ROOT, "Leads_Reviewer", "Studios_To_Review", "Approval_Pending_Games")
# Cloud (GHA send job): no local media folders — drafts + media live in R2. Same test
# the reviewer uses: R2 readable AND the staged dir absent.
_CLOUD = media_store.read_enabled() and not os.path.isdir(MEDIA_DIR)

SENDER_NAME = "Meshak"              # the From display name
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
IMAP_HOST = "imap.gmail.com"
MIN_GAP, MAX_GAP = 120, 240        # 2–4 min between sends, randomized
SMTP_RETRY_DELAYS = (300, 900)     # explicit temporary rejections only, not a send-count cap


# Providers we can send from with an app password (Microsoft needs OAuth: not yet).
PROVIDERS = {"google": dict(smtp=("smtp.gmail.com", 587), imap="imap.gmail.com")}


class Account:
    """One sending mailbox. cap=None means no cap (single legacy GMAIL_USER account)."""

    def __init__(self, email, password, provider="google", cap=None, name=None):
        self.email, self.password, self.provider = email, password, provider
        self.cap, self.name = cap, name or SENDER_NAME
        hosts = PROVIDERS.get(provider) or {}
        self.smtp, self.imap = hosts.get("smtp"), hosts.get("imap")

    def __repr__(self):  # never print the password
        return f"Account({self.email}, {self.provider}, cap={self.cap})"


def _passwords():
    """{email(lower): app password} from SENDER_ACCOUNTS (JSON object) + GMAIL_USER."""
    out = {}
    raw = os.environ.get("SENDER_ACCOUNTS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            sys.exit("SENDER_ACCOUNTS is not valid JSON: expected {\"email\": \"app password\"}")
        if not isinstance(data, dict):
            sys.exit("SENDER_ACCOUNTS must be a JSON object: {\"email\": \"app password\"}")
        out = {str(k).strip().lower(): str(v).replace(" ", "") for k, v in data.items() if v}
    user, password = os.environ.get("GMAIL_USER", "").strip(), os.environ.get("GMAIL_APP_PASSWORD", "")
    if user and password:
        out.setdefault(user.lower(), password)
    return out


def load_accounts(*, record=False):
    """(senders, mailboxes): accounts that may send now, and every mailbox we can read.

    With no rows in sender_accounts this is the single legacy GMAIL_USER account with no
    cap. Otherwise each configured account sends up to its daily_cap per rolling 24h if
    its provider is supported and SENDER_ACCOUNTS has its app password."""
    passwords = _passwords()
    legacy = os.environ.get("GMAIL_USER", "").strip()
    configured = pipeline.sender_accounts()
    if not configured:
        if not legacy:
            return [], []
        acct = Account(legacy, passwords.get(legacy.lower(), ""))
        return [acct], [acct] if acct.password else []
    senders, mailboxes, seen = [], [], set()
    for row in configured:
        email = row["email"].strip()
        acct = Account(email, passwords.get(email.lower(), ""), row["provider"],
                       row["daily_cap"], row.get("display_name"))
        seen.add(email.lower())
        if acct.password and acct.imap:
            mailboxes.append(acct)
        problem = ("Microsoft sign-in not set up yet" if acct.smtp is None
                   else "no app password in SENDER_ACCOUNTS" if not acct.password
                   else "paused (daily cap 0)" if not acct.cap else "")
        if problem:
            if record:
                pipeline.set_sender_status(email, problem)
            continue
        senders.append(acct)
    # The legacy inbox still receives replies to mails sent before rotation.
    if legacy and legacy.lower() not in seen and passwords.get(legacy.lower()):
        mailboxes.append(Account(legacy, passwords[legacy.lower()]))
    return senders, mailboxes


def _pick_sender(senders, counts):
    """The account with the most room left in its rolling 24h cap (ties: the one that sent
    least, so mail spreads across accounts), or None when every account is full."""
    best, best_key = None, None
    for acct in senders:
        sent = counts.get(acct.email, 0)
        room = float("inf") if acct.cap is None else acct.cap - sent
        if room <= 0:
            continue
        key = (room, -sent)
        if best_key is None or key > best_key:
            best, best_key = acct, key
    return best


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
    if tpl and not os.path.exists(path):
        return None, None, None
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


def _send(user, password, to, subject, body, *, message_id=None, smtp=None, name=None):
    s = None
    try:
        msg = EmailMessage()
        msg["From"] = f"{name or SENDER_NAME} <{user}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = message_id or make_msgid(domain=user.rsplit("@", 1)[-1])
        msg["Date"] = formatdate(usegmt=True)
        msg.set_content(body)
        host, port = smtp or (SMTP_HOST, SMTP_PORT)
        s = smtplib.SMTP(host, port, timeout=30)
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


def _deliver(appid, to, subject, body, raw_email, account, deadline):
    user, password = account.email, account.password
    extra = {} if (account.smtp in (None, (SMTP_HOST, SMTP_PORT)) and account.name == SENDER_NAME) \
        else dict(smtp=account.smtp, name=account.name)
    token = uuid.uuid4().hex
    message_id = make_msgid(idstring=f"sre.{appid}", domain=user.rsplit("@", 1)[-1])
    for attempt in range(len(SMTP_RETRY_DELAYS) + 1):
        if time.monotonic() + 120 >= deadline:
            print(f"DEFER {appid}: runner time budget reached. Left Scheduled.")
            return "deferred"
        if not pipeline.claim_mail(appid, token=token, message_id=message_id,
                                   expected_email=raw_email, sender=user):
            print(f"SKIP {appid}: state or recipient changed before the send claim")
            return "skipped"
        try:
            _send(user, password, to, subject, body, message_id=message_id, **extra)
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
            print(f"  sent {appid} -> {to} from {user} | {subject!r}")
            return "sent"


def _reconcile_sending(mailboxes, legacy=None):
    """A positive exact Message-ID match in the sender's Sent folder is proof. Absence is not."""
    pending = pipeline.mail_status_appids("Sending")
    if not pending:
        return 0
    remaining = set(pending)
    by_email = {m.email.lower(): m for m in mailboxes}
    if len(mailboxes) == 1:
        groups = {mailboxes[0].email.lower(): list(pending)}
    else:
        senders = pipeline.attempt_senders(pending)
        groups = {}
        for appid in pending:
            sender = (senders.get(appid) or legacy or "").lower()
            groups.setdefault(sender, []).append(appid)
    for sender, appids in groups.items():
        account = by_email.get(sender)
        if account is None:
            print(f"WARN: cannot check Sent for {', '.join(map(str, appids))}: "
                  f"no password for sending account {sender or '(unknown)'}")
            continue
        _reconcile_mailbox(account, appids, remaining)
    if remaining:
        print("WARN: unresolved Sending rows require review: " +
              ", ".join(map(str, sorted(remaining))) + ". Other Scheduled mails can proceed.")
    return len(remaining)


def _reconcile_mailbox(account, pending, remaining):
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(account.imap or IMAP_HOST, timeout=30)
        imap.login(account.email, account.password)
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
            token, message_id = stored[0], stored[1]
            typ, data = imap.search(None, "HEADER", "Message-ID", f'"{message_id}"')
            if typ == "OK" and data and data[0].split():
                if _persist_delivery(pipeline.mark_sent, appid, token=token):
                    remaining.discard(appid)
                    print(f"RECOVERED {appid}: found its exact Message-ID in {account.email} Sent")
                    try:
                        _delete_media(appid)
                    except Exception as exc:
                        print(f"WARN {appid}: recovered Sent, but media cleanup failed: {exc}")
    except Exception as exc:
        print(f"WARN: Sent reconciliation failed for {account.email}: {exc}")
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass


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


def _scan_bounces(user, password):
    """Act on new bounce notices (nightly reply check); a failure never blocks the reply scan."""
    try:
        import bounces
        stats = bounces.process(user, password)
        print("bounces: " + json.dumps(stats))
    except Exception as exc:
        print(f"WARN: bounce scan failed: {exc}")
        with suppress(Exception):
            pipeline.reconnect()


def main(dry_run=False, limit=None):
    _load_env()
    legacy = os.environ.get("GMAIL_USER", "").strip()
    senders, mailboxes = load_accounts(record=not dry_run)
    if not senders:
        sys.exit("No sending account is ready. Set GMAIL_USER, or add rows to sender_accounts "
                 "with a daily_cap and their app passwords in SENDER_ACCOUNTS.")
    if not dry_run and not all(a.password for a in senders):
        sys.exit("GMAIL_APP_PASSWORD not set. Add it to the repo-root .env "
                 "(see this file's header). Or use --dry-run.")
    print("sending accounts: " + ", ".join(
        f"{a.email} (cap {'none' if a.cap is None else a.cap}/24h)" for a in senders))
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise ValueError("limit must be a nonnegative integer")
    budget = float(os.environ.get("SEND_TIME_BUDGET_SECONDS", "0"))
    if budget < 0 or not math.isfinite(budget):
        raise ValueError("SEND_TIME_BUDGET_SECONDS must be finite and nonnegative")
    deadline = time.monotonic() + budget if budget else float("inf")
    uncertain = 0 if dry_run else _reconcile_sending(mailboxes, legacy)

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
                 rejected=0, uncertain=uncertain, skipped=0)
    print(f"scheduled: {len(scheduled)} ({len(valid)} syntactically valid) | "
          f"sending up to: {sending}{' (DRY RUN)' if dry_run else ''}")
    if not valid or room == 0:
        if scheduled and room == 0 and limit is not None:
            print("per-run limit reached.")
        _summary(stats, len(valid), "")
        return int(bool(uncertain))

    processed, attempted, error = 0, False, ""
    confirmed = set()
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
                print(f"SKIP {appid}: missing or empty draft -> back to Accepted for drafting")
                if not dry_run:
                    if not pipeline.requeue_missing_draft(appid):
                        stats["skipped"] += 1
                        continue
                stats["rejected"] += 1
                continue
            if dry_run:
                print(f"  [dry] {appid} -> {to} | {subject!r} | {os.path.basename(path)}")
                stats["sent"] += 1
                continue
            # No mailbox check here: addresses are verified once, when the game is accepted
            # (verify.yml). A dead address found later is caught by the nightly bounce scan.
            account = _pick_sender(senders, pipeline.sent_last_24h())
            if account is None:
                print("All sending accounts reached their 24h caps. "
                      "Remaining mails stay Scheduled for the next Send.")
                processed -= 1
                break
            if attempted:
                gap = random.randint(MIN_GAP, MAX_GAP)
                if time.monotonic() + gap + 120 >= deadline:
                    stats["deferred"] += 1
                    break
                print(f"    waiting {gap}s before next…")
                time.sleep(gap)
            attempted = True
            outcome = _deliver(appid, to, subject, body, raw, account, deadline)
            if outcome == "interrupted":
                stats["uncertain"] += 1
                error = "interrupted during submission. Review its Sending row."
                break
            if outcome == "halted":
                stats["deferred"] += 1
                with suppress(Exception):
                    pipeline.set_sender_status(account.email, "login or sender rejected by SMTP")
                senders = [a for a in senders if a is not account]
                if not senders:
                    error = "SMTP account/setup error. Remaining mails stay Scheduled."
                    break
                print(f"Dropped {account.email} for this run; continuing with the other accounts.")
                continue
            if outcome == "sent" and account.email not in confirmed:
                confirmed.add(account.email)
                with suppress(Exception):
                    pipeline.set_sender_status(account.email, "ok")
            stats[outcome] += 1
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


def _matches_reply(raw_headers, addr, message_id, started_at):
    """Require exact sender, exact thread ID and a dated message after this attempt."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_headers)
        if len(message.get_all("From", [])) != 1 or len(message.get_all("Date", [])) != 1:
            return False
        sender = message["From"]
        if sender.defects or len(sender.addresses) != 1:
            return False
        if sender.addresses[0].addr_spec.casefold() != addr.casefold():
            return False
        received = parsedate_to_datetime(str(message["Date"]))
        started = datetime.fromisoformat(started_at).replace(tzinfo=timezone.utc)
        if received.tzinfo is None or received < started:
            return False
        references = " ".join(str(value) for name in ("In-Reply-To", "References")
                              for value in message.get_all(name, []))
        return message_id in re.findall(r"<[^<>\s]+>", references)
    except (ValueError, TypeError, AttributeError, IndexError, OverflowError):
        return False


def review():
    """Nightly inbox scan of every sending mailbox: first bounce notices (dead addresses leave
    Sent, see bounces.py), then replies to retained outgoing Message-IDs (read-only)."""
    _load_env()
    legacy = os.environ.get("GMAIL_USER", "").strip()
    _senders, mailboxes = load_accounts()
    if not mailboxes:
        if not legacy:
            sys.exit("GMAIL_USER not set. Add it to the repo-root .env or GHA secret.")
        sys.exit("GMAIL_APP_PASSWORD not set. Add it to the repo-root .env "
                 "(see this file's header).")
    for account in mailboxes:
        _scan_bounces(account.email, account.password)
    sent = pipeline.reply_evidence()
    if not sent:
        print("no Sent leads with retained reply evidence to review.")
        return
    if len(mailboxes) == 1:
        groups = {mailboxes[0].email.lower(): sent}
    else:
        senders = pipeline.attempt_senders([row[0] for row in sent])
        groups = {}
        for row in sent:
            groups.setdefault((senders.get(row[0]) or legacy).lower(), []).append(row)
    by_email = {m.email.lower(): m for m in mailboxes}
    replied = 0
    for sender, rows in groups.items():
        account = by_email.get(sender)
        if account is None:
            print(f"WARN: {len(rows)} sent lead(s) from {sender or '(unknown)'} not checked: "
                  "no password for that mailbox")
            continue
        replied += _review_mailbox(account, rows)
    print(f"reviewed {len(sent)} sent lead(s): {replied} replied -> marked 'Replied'.")


def _review_mailbox(account, sent):
    imap = imaplib.IMAP4_SSL(account.imap or IMAP_HOST, timeout=30)
    replied = 0
    try:
        imap.login(account.email, account.password)
        if imap.select("INBOX", readonly=True)[0] != "OK":
            raise RuntimeError("cannot open Gmail inbox")
        for appid, raw_email, message_id, started_at in sent:
            addr = _recipient(raw_email)
            if not addr or not re.fullmatch(r'<[^<>\s"\\]+>', message_id):
                continue
            # Search is only a candidate filter. Fetch headers without setting Seen.
            try:
                typ, data = imap.search(None, "OR", "HEADER", "In-Reply-To",
                                        f'"{message_id}"', "HEADER", "References",
                                        f'"{message_id}"')
                if typ != "OK" or not data or not data[0]:
                    continue
                for number in data[0].split():
                    typ, headers = imap.fetch(number,
                        "(BODY.PEEK[HEADER.FIELDS (FROM DATE IN-REPLY-TO REFERENCES)])")
                    if typ != "OK":
                        continue
                    if any(isinstance(item, tuple) and isinstance(item[1], bytes) and
                           _matches_reply(item[1], addr, message_id, started_at)
                           for item in headers or []):
                        pipeline.set_mail_status(appid, "Replied")
                        replied += 1
                        print(f"  REPLIED {appid} <- {addr} (to {account.email})")
                        break
            except imaplib.IMAP4.error:
                continue
    finally:
        with suppress(Exception):
            imap.logout()
    return replied


def check_accounts():
    """Log in to every configured account's SMTP and IMAP without sending anything, and
    record the result for the dashboard. Returns 1 if any account that should send failed."""
    _load_env()
    passwords = _passwords()
    configured = pipeline.sender_accounts()
    if not configured:
        sys.exit("No rows in sender_accounts to check.")
    failed = 0
    for row in configured:
        email = row["email"].strip()
        acct = Account(email, passwords.get(email.lower(), ""), row["provider"], row["daily_cap"])
        if acct.smtp is None:
            status = "Microsoft sign-in not set up yet"
        elif not acct.password:
            status = "no app password in SENDER_ACCOUNTS"
        else:
            status = "ok (login checked)"
            try:
                smtp = smtplib.SMTP(*acct.smtp, timeout=30)
                try:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.login(acct.email, acct.password)
                finally:
                    with suppress(Exception):
                        smtp.quit()
                imap = imaplib.IMAP4_SSL(acct.imap, timeout=30)
                try:
                    imap.login(acct.email, acct.password)
                finally:
                    with suppress(Exception):
                        imap.logout()
            except Exception as exc:
                status = f"login failed: {type(exc).__name__}: {str(exc)[:120]}"
        if acct.cap and not status.startswith("ok") and acct.smtp is not None:
            failed += 1
        pipeline.set_sender_status(email, status)
        print(f"  {email} (cap {acct.cap}): {status}")
    return 1 if failed else 0


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
    elif "--check-accounts" in args:
        raise SystemExit(check_accounts())
    elif "--purge-sent" in args:
        purge_sent()
    else:
        lim = None
        if "--limit" in args:
            lim = int(args[args.index("--limit") + 1])
        raise SystemExit(main(dry_run="--dry-run" in args, limit=lim))
