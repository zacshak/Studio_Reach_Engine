"""Read bounce notices from the sending Gmail account and act on dead addresses.

Runs in the nightly reply check (review-mails.yml -> mailer.review), before replies. Only bounces received after bounce
handling was first switched on are processed (pipeline.bounce_since), and each bounce
message is handled once (mail_bounce_log).

Only a permanent "this mailbox / domain does not exist" failure counts (DSN status 5.1.x
or 5.2.1, or that wording). Spam/policy blocks, full mailboxes, server misconfiguration
and delay notices are logged and ignored: the address itself may be fine.

For a dead address on a Sent game:
  scrape_status 'seeded' (Steam-provided) -> email cleared, back to "no email" so a new
      address can be researched and the game accepted again (its media is rebuilt from
      Steam by the verify job)
  scrape_status 'scraped' (already researched) -> Invalid, so it cannot loop
The address is also cached as invalid, so research cannot save it again.

    python Mail_Sender/bounces.py --report [--since 2026-08-01]   # read-only check
"""
import argparse
import email
import hashlib
import imaplib
import os
import re
import sys
import time
from datetime import datetime, timezone
from email import policy
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, ROOT)
import pipeline  # noqa: E402
from Email_Verifier.smtp_check import classify_rcpt  # noqa: E402

IMAP_HOST = "imap.gmail.com"
IMAP_CONNECT_TRIES = 3
IMAP_RETRY_SECONDS = (5, 15)


def _imap_connect(host):
    """Open an IMAP SSL connection, retrying brief network failures (reset, TLS timeout)."""
    for attempt in range(IMAP_CONNECT_TRIES):
        try:
            return imaplib.IMAP4_SSL(host, timeout=30)
        except OSError:  # includes TimeoutError and connection resets
            if attempt == IMAP_CONNECT_TRIES - 1:
                raise
            time.sleep(IMAP_RETRY_SECONDS[min(attempt, len(IMAP_RETRY_SECONDS) - 1)])
_STATUS = re.compile(r"\b([245])\.(\d{1,3})\.(\d{1,3})\b")
_CODE = re.compile(r"\b([245]\d\d)\b")
_ADDR = re.compile(r"[^\s<>;,\"']+@[^\s<>;,\"']+\.[A-Za-z]{2,}")
DEAD_STATUSES = {"5.1.0", "5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.1.10", "5.2.1"}
# Gmail's own human-readable wording for the two dead-address cases.
_GMAIL_DEAD = re.compile(r"address couldn.t be found|domain .{1,253} couldn.t be found", re.I)


def _text(part):
    try:
        return part.get_content() if part.get_content_maintype() == "text" else ""
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", "replace")


def failures(msg):
    """[(recipient, status, diagnostic, action)] for each failed recipient in a bounce."""
    found = []
    for part in msg.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        blocks = part.get_payload()
        blocks = blocks if isinstance(blocks, list) else []
        for block in blocks[1:]:  # the first block is per-message, the rest per-recipient
            recipient = str(block.get("Final-Recipient") or block.get("Original-Recipient") or "")
            recipient = recipient.split(";", 1)[-1].strip()
            if recipient:
                found.append((recipient, str(block.get("Status") or "").strip(),
                              " ".join(str(block.get("Diagnostic-Code") or "").split()),
                              str(block.get("Action") or "").strip().lower()))
    if found:
        return found
    # No machine-readable report: fall back to Gmail's X-Failed-Recipients and the text.
    body = " ".join(_text(p) for p in msg.walk() if p.get_content_type() == "text/plain")
    body = " ".join(body.split())
    status = _STATUS.search(body)
    for recipient in str(msg.get("X-Failed-Recipients") or "").split(","):
        if recipient.strip():
            found.append((recipient.strip(), ".".join(status.groups()) if status else "",
                          body[:400], "failed"))
    return found


def is_dead(status, diagnostic, action, body=""):
    """True only for a permanent failure that says the address/domain does not exist."""
    if action and action != "failed":
        return False  # delayed / relayed / delivered notices
    if status and not status.startswith("5."):
        return False
    if status in DEAD_STATUSES:
        return True
    code = _CODE.search(diagnostic or "")
    if code and classify_rcpt(int(code.group(1)), diagnostic) == "invalid":
        return True
    return not status and bool(_GMAIL_DEAD.search(body or ""))


def message_key(msg, raw):
    mid = str(msg.get("Message-ID") or "").strip()
    return mid or "sha256:" + hashlib.sha256(raw).hexdigest()


def _received(msg):
    try:
        when = parsedate_to_datetime(str(msg.get("Date")))
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _all_mail(imap):
    typ, folders = imap.list()
    for folder in (folders or []) if typ == "OK" else []:
        match = re.match(rb'\([^)]*\\All\b[^)]*\)\s+(?:"[^"]*"|NIL)\s+(.+)', folder or b"", re.I)
        if match:
            return match.group(1).decode("ascii")
    return "INBOX"


def fetch_bounces(user, password, since, account=None):
    """Yield (key, msg, raw) for bounce notices received at/after `since` (aware datetime).
    `account` (a mailer.Account) selects its host and sign-in (Microsoft: OAuth, INBOX)."""
    imap = _imap_connect(getattr(account, "imap", None) or IMAP_HOST)
    try:
        if account is not None:
            account.imap_login(imap)
        else:
            imap.login(user, password)
        if imap.select(_all_mail(imap), readonly=True)[0] != "OK":
            raise RuntimeError("cannot open the mailbox for bounces")
        day = since.strftime("%d-%b-%Y")
        # Gmail: mailer-daemon; Exchange Online: postmaster@ or MicrosoftExchange...@
        typ, data = imap.search(None, "SINCE", day, "OR", "OR", "FROM", '"mailer-daemon"',
                                "FROM", '"postmaster"', "FROM", '"MicrosoftExchange"')
        if typ != "OK":
            raise RuntimeError("Gmail bounce search failed")
        for number in (data[0] or b"").split():
            typ, parts = imap.fetch(number, "(BODY.PEEK[])")  # PEEK: never marks it read
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if typ != "OK" or not raw:
                continue
            msg = email.message_from_bytes(raw, policy=policy.default)
            received = _received(msg)
            if received is not None and received < since:
                continue
            yield message_key(msg, raw), msg, raw
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _body(msg):
    return " ".join(" ".join(_text(p) for p in msg.walk()
                             if p.get_content_type() == "text/plain").split())


def process(user, password, *, since=None, apply=True, log=print, account=None):
    """Handle new bounces. Returns a dict of counts. Never raises for one bad bounce."""
    if since is None:
        since = datetime.strptime(pipeline.bounce_since(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    sent = None
    stats = dict(bounces=0, no_mail=0, invalid=0, ignored=0, unmatched=0, errors=0)
    for key, msg, _raw in (fetch_bounces(user, password, since) if account is None
                           else fetch_bounces(user, password, since, account)):
        if apply and pipeline.bounce_seen(key):
            continue
        stats["bounces"] += 1
        body = _body(msg)
        dead = [(r, s, d) for r, s, d, a in failures(msg) if is_dead(s, d, a, body)]
        if not dead:
            stats["ignored"] += 1
            reasons = "; ".join(f"{r} {s}" for r, s, _d, _a in failures(msg)) or msg.get("Subject")
            log(f"  bounce ignored (not a dead address): {reasons}")
            if apply:
                pipeline.record_bounce(key, recipient="", appids=[], outcome="ignored",
                                       detail=reasons)
            continue
        if sent is None:
            sent = pipeline.sent_rows()
        for recipient, status, diagnostic in dead:
            address = recipient.strip().strip("<>")
            matches = [(a, raw, scrape) for a, raw, scrape in sent
                       if pipeline.normalize_email(raw).casefold() == address.casefold()]
            reason = f"bounced {status} {diagnostic}".strip()[:300]
            if not matches:
                stats["unmatched"] += 1
                log(f"  bounce for {address} matches no Sent game")
                if apply:
                    pipeline.record_bounce(key, recipient=address, appids=[], outcome="unmatched",
                                           detail=reason)
                continue
            outcomes = []
            for appid, raw, scrape in matches:
                if not apply:
                    outcomes.append("invalid" if scrape == "scraped" else "no_mail")
                    log(f"  would mark {appid} ({address}, {scrape}) -> {outcomes[-1]}")
                    continue
                try:
                    outcome = pipeline.reject_email(
                        appid, expected_email=raw, reason=reason, source="bounce",
                        from_statuses=("Sent",), researched_to_invalid=True)
                except Exception as exc:  # e.g. a media cleanup still pending for the row
                    stats["errors"] += 1
                    log(f"  WARN bounce for {appid} not applied: {exc}")
                    pipeline.reconnect()
                    continue
                outcomes.append(outcome or "moved")
                log(f"  BOUNCE {appid} {address}: {outcome or 'row already moved'}")
            for outcome in outcomes:
                if outcome in ("no_mail", "invalid"):
                    stats[outcome] += 1
            if apply:
                pipeline.cache_email_verification(address, {
                    "email": address, "result": "invalid", "reason": reason, "source": "bounce"})
                pipeline.record_bounce(key, recipient=address, appids=[m[0] for m in matches],
                                       outcome=",".join(outcomes) or "error", detail=reason)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", action="store_true", help="read-only: show what would change")
    parser.add_argument("--since", help="YYYY-MM-DD (with --report)")
    args = parser.parse_args(argv)
    user, password = os.environ.get("GMAIL_USER", ""), os.environ.get("GMAIL_APP_PASSWORD", "")
    if not user or not password:
        sys.exit("GMAIL_USER / GMAIL_APP_PASSWORD not set")
    since = (datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
             if args.since else None)
    if args.report != bool(since):
        sys.exit("--report needs --since, and --since is only allowed with --report")
    print("bounces: " + str(process(user, password, since=since, apply=not args.report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
