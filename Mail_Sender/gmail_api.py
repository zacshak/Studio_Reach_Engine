"""Gmail API backend for the Google Workspace inboxes (service account, domain-wide delegation).

The service account sre-mailer@sre-mail.iam.gserviceaccount.com impersonates each mailbox
(.with_subject). Its JSON key is read from the file named by GOOGLE_SA_KEY_FILE; the key is
never logged or printed. Which mailboxes use this backend is set in mail_backends.json.

    python Mail_Sender/gmail_api.py --check                  # getProfile for every Workspace inbox
    python Mail_Sender/gmail_api.py --thread-test FROM TO    # send FROM -> TO, reply TO -> FROM, same thread?
"""
import base64
import json
import os
import random
import socket
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "mail_backends.json")
SCOPES = ["https://mail.google.com/"]
GMAIL_API, APP_PASSWORD = "gmail_api", "app_password"
BACKENDS = (GMAIL_API, APP_PASSWORD)
RETRIES = 5                        # for 429 / 5xx / network errors
BACKOFF_MAX = 60
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded",
                "quotaExceeded", "backendError"}

_clients = {}   # mailbox -> gmail service, one per mailbox for this process
_config = None


class GmailAuthError(RuntimeError):
    """Google refused to act as the mailbox: delegation, scopes, key or user problem."""


class GmailNotSent(RuntimeError):
    """The send was refused (rate limit after retries); nothing was delivered."""


class GmailUncertain(RuntimeError):
    """The send got no clear answer and the message is not in Sent: it may or may not be out."""


# -- config ------------------------------------------------------------------
def config():
    global _config
    if _config is None:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        domains = [d.strip().lower() for d in data.get("gmail_api_domains") or []]
        boxes = {k.strip().lower(): (v or {}) for k, v in (data.get("mailboxes") or {}).items()}
        for box, entry in boxes.items():
            backend = entry.get("backend")
            if backend is not None and backend not in BACKENDS:
                raise ValueError(f"mail_backends.json: {box} backend must be one of {BACKENDS}")
            if backend == GMAIL_API and box.rsplit("@", 1)[-1] not in domains:
                raise ValueError(f"mail_backends.json: {box} is not on a gmail_api domain")
        _config = dict(domains=domains, mailboxes=boxes, failover=bool(data.get("failover")))
    return _config


def workspace(email):
    """True for an address on one of the Workspace domains the service account can impersonate."""
    return (email or "").strip().lower().rsplit("@", 1)[-1] in config()["domains"]


def backend_for(email):
    """The configured backend for a mailbox: its own setting, else the domain default."""
    entry = config()["mailboxes"].get((email or "").strip().lower(), {})
    return entry.get("backend") or (GMAIL_API if workspace(email) else APP_PASSWORD)


def failover_enabled():
    return config()["failover"]


def workspace_inboxes():
    return [box for box in config()["mailboxes"] if workspace(box)]


def key_file():
    """Path of the service account key, or '' when it is not configured or missing."""
    path = os.environ.get("GOOGLE_SA_KEY_FILE", "").strip()
    return path if path and os.path.isfile(path) else ""


# -- client + calls ------------------------------------------------------------
def client(mailbox):
    mailbox = mailbox.strip().lower()
    if mailbox not in _clients:
        if not workspace(mailbox):
            raise GmailAuthError(f"{mailbox} [gmail_api]: not on a Workspace domain in "
                                 "mail_backends.json; the service account cannot act as it")
        key = key_file()
        if not key:
            raise GmailAuthError(f"{mailbox} [gmail_api]: GOOGLE_SA_KEY_FILE is not set or the "
                                 "file does not exist")
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        try:
            creds = service_account.Credentials.from_service_account_file(key, scopes=SCOPES)
        except (ValueError, KeyError, OSError) as exc:
            # The message names the problem, never the key's contents.
            raise GmailAuthError(f"{mailbox} [gmail_api]: the service account key file cannot be "
                                 f"read ({type(exc).__name__})") from None
        _clients[mailbox] = build("gmail", "v1", credentials=creds.with_subject(mailbox),
                                  cache_discovery=False)
    return _clients[mailbox]


def _auth_message(mailbox, exc):
    text = str(exc.args[0] if getattr(exc, "args", None) else exc)
    if "unauthorized_client" in text:
        return (f"{mailbox} [gmail_api]: unauthorized_client. Domain-wide delegation for the "
                "service account has not taken effect yet (it can take up to 24 hours), or the "
                "scopes authorised in Google Admin do not include https://mail.google.com/.")
    if "invalid_grant" in text:
        return (f"{mailbox} [gmail_api]: invalid_grant. The mailbox may not exist or be suspended, "
                f"or the key was revoked: {text[:200]}")
    return f"{mailbox} [gmail_api]: Google sign-in failed: {text[:200]}"


def _status(exc):
    return int(getattr(getattr(exc, "resp", None), "status", 0) or 0)


def _reason(exc):
    try:
        details = json.loads(exc.content.decode())["error"]["errors"]
        return details[0].get("reason", "")
    except Exception:
        return ""


def _retryable(exc):
    from googleapiclient.errors import HttpError
    from google.auth.exceptions import TransportError
    if isinstance(exc, HttpError):
        status = _status(exc)
        return status == 429 or status >= 500 or (status == 403 and _reason(exc) in RATE_REASONS)
    return isinstance(exc, (TransportError, socket.timeout, ConnectionError, TimeoutError))


def _wait(attempt, sleep=time.sleep):
    sleep(min(BACKOFF_MAX, 2 ** attempt) + random.random())


def _raise_auth(mailbox, exc):
    """Turn a sign-in or permission failure into GmailAuthError; re-raise anything else."""
    from googleapiclient.errors import HttpError
    from google.auth.exceptions import RefreshError
    if isinstance(exc, RefreshError):
        raise GmailAuthError(_auth_message(mailbox, exc)) from None
    if isinstance(exc, HttpError) and _status(exc) in (401, 403):
        raise GmailAuthError(f"{mailbox} [gmail_api]: HTTP {_status(exc)} {_reason(exc)}: "
                             f"{str(exc)[:200]}") from None
    raise exc


def execute(mailbox, request, *, sleep=time.sleep):
    """Run a read/modify request, retrying 429, 5xx and network errors with backoff."""
    for attempt in range(RETRIES + 1):
        try:
            return request.execute()
        except Exception as exc:
            if _retryable(exc) and attempt < RETRIES:
                _wait(attempt, sleep)
                continue
            _raise_auth(mailbox, exc)


# -- send ---------------------------------------------------------------------
def build_message(mailbox, to, subject, body, *, name=None, message_id=None,
                  in_reply_to=None, references=None):
    msg = EmailMessage()
    msg["From"] = f"{name} <{mailbox}>" if name else mailbox
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or make_msgid(domain=mailbox.rsplit("@", 1)[-1])
    msg["Date"] = formatdate(usegmt=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(body)
    return msg


def send(mailbox, msg, *, thread_id=None, sleep=time.sleep):
    """users.messages.send as the mailbox. Returns {'id', 'threadId', 'messageId'}.

    Gmail replaces the Message-ID we set with its own; 'messageId' is the one it used (None if
    it could not be read back). 429 and rate-limit 403s are refusals, so they are retried.
    After a 5xx or network error the mail may already be out, so Sent is searched for it
    (recipient, subject, sent since this call) before each retry."""
    from googleapiclient.errors import HttpError
    sender = msg["From"].addresses[0].addr_spec.lower() if msg["From"] else ""
    if sender != mailbox.strip().lower():
        raise ValueError(f"From {sender or '(none)'} is not the impersonated mailbox {mailbox}")
    body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}
    if thread_id:
        body["threadId"] = thread_id
    request = client(mailbox).users().messages().send(userId="me", body=body)
    to = msg["To"].addresses[0].addr_spec if msg["To"] and msg["To"].addresses else ""
    started = time.time()
    for attempt in range(RETRIES + 1):
        try:
            return _with_message_id(mailbox, request.execute())
        except Exception as exc:
            if not _retryable(exc):
                if isinstance(exc, HttpError) and _status(exc) == 400:
                    raise ValueError(f"{mailbox} [gmail_api]: send rejected the message: "
                                     f"{str(exc)[:200]}") from None
                _raise_auth(mailbox, exc)
            refused = isinstance(exc, HttpError) and (
                _status(exc) == 429 or _reason(exc) in RATE_REASONS - {"backendError"})
            if not refused and to:
                found = find_sent(mailbox, to, started - 60, subject=str(msg["Subject"] or ""))
                if found:
                    return _with_message_id(mailbox, found)
            if attempt == RETRIES:
                if refused:
                    raise GmailNotSent(f"{mailbox} [gmail_api]: rate limited after "
                                       f"{RETRIES} retries: {str(exc)[:200]}") from None
                raise GmailUncertain(f"{mailbox} [gmail_api]: no answer from send and the mail "
                                     f"to {to} is not in Sent: {str(exc)[:200]}") from None
            _wait(attempt, sleep)


def _header(mailbox, gmail_id, name):
    data = execute(mailbox, client(mailbox).users().messages().get(
        userId="me", id=gmail_id, format="metadata", metadataHeaders=[name]))
    for h in (data.get("payload") or {}).get("headers") or []:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _with_message_id(mailbox, result):
    """Add the Message-ID Gmail actually used. The mail is already sent, so a failed lookup
    is logged, not raised."""
    try:
        result["messageId"] = _header(mailbox, result["id"], "Message-ID")
    except Exception as exc:
        result["messageId"] = None
        print(f"WARN {mailbox} [gmail_api]: sent {result.get('id')}, but its Message-ID could "
              f"not be read back: {exc}")
    return result


def find_sent(mailbox, to, since_epoch, *, subject=None):
    """A mail in Sent to `to` since `since_epoch` (with this exact subject, if given), or None."""
    for hit in list_ids(mailbox, f"in:sent to:{to} after:{int(since_epoch)}", limit=20):
        if subject is None or _header(mailbox, hit["id"], "Subject") == subject:
            return hit
    return None


# -- read ---------------------------------------------------------------------
def list_ids(mailbox, q, *, limit=500):
    """[{'id', 'threadId'}] for messages matching a Gmail search (spam and trash excluded)."""
    out, token = [], None
    users = client(mailbox).users()
    while len(out) < limit:
        page = execute(mailbox, users.messages().list(
            userId="me", q=q, pageToken=token, maxResults=min(500, limit - len(out))))
        out.extend(page.get("messages") or [])
        token = page.get("nextPageToken")
        if not token:
            break
    return out


def get_raw(mailbox, message_id):
    data = execute(mailbox, client(mailbox).users().messages().get(
        userId="me", id=message_id, format="raw"))
    return base64.urlsafe_b64decode(data["raw"] + "=" * (-len(data["raw"]) % 4))


def find_message_id(mailbox, rfc_message_id):
    """The mailbox's {'id', 'threadId'} for an RFC Message-ID, or None."""
    mid = rfc_message_id.strip().strip("<>")
    hits = list_ids(mailbox, f"rfc822msgid:{mid}", limit=1)
    return hits[0] if hits else None


def thread_headers(mailbox, thread_id, names=("From", "Date", "In-Reply-To", "References")):
    """Raw header block (bytes) of every message in a thread, for header parsing."""
    data = execute(mailbox, client(mailbox).users().threads().get(
        userId="me", id=thread_id, format="metadata", metadataHeaders=list(names)))
    out = []
    for message in data.get("messages") or []:
        headers = (message.get("payload") or {}).get("headers") or []
        out.append("".join(f"{h['name']}: {h['value']}\r\n" for h in headers).encode("utf-8"))
    return out


def thread_messages(mailbox, thread_id):
    """[{'id', 'labelIds'}] for every message in a thread, oldest first."""
    data = execute(mailbox, client(mailbox).users().threads().get(
        userId="me", id=thread_id, format="minimal"))
    return [{"id": m["id"], "labelIds": m.get("labelIds") or []} for m in data.get("messages") or []]


def fetch_unread(mailbox, q="is:unread in:inbox", *, limit=100):
    """[(gmail id, threadId, raw bytes)] for matching messages. Does not mark them read."""
    return [(m["id"], m["threadId"], get_raw(mailbox, m["id"]))
            for m in list_ids(mailbox, q, limit=limit)]


def mark_read(mailbox, message_id):
    execute(mailbox, client(mailbox).users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}))


def profile(mailbox):
    return execute(mailbox, client(mailbox).users().getProfile(userId="me"))


# -- verification CLI ---------------------------------------------------------------
def check(log=print):
    """getProfile for every Workspace inbox in mail_backends.json. Returns the failure count."""
    failed = 0
    for box in workspace_inboxes():
        try:
            p = profile(box)
            log(f"  OK   {box} [{backend_for(box)}]: {p['emailAddress']} "
                f"messagesTotal={p['messagesTotal']}")
        except Exception as exc:
            failed += 1
            log(f"  FAIL {box} [gmail_api]: {type(exc).__name__}: {exc}")
    return failed


def _await(mailbox, rfc_message_id, timeout=120, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = find_message_id(mailbox, rfc_message_id)
        if found:
            return found
        sleep(5)
    raise RuntimeError(f"{rfc_message_id} did not arrive in {mailbox} within {timeout}s")


def thread_test(sender, recipient, log=print):
    """Send sender -> recipient, reply recipient -> sender in its thread, then check that the
    reply joined the original thread in the sender's mailbox. Returns 0 on success."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    subject = f"SRE Gmail API thread test {stamp}"
    first = build_message(sender, recipient, subject, "Gmail API test. Safe to delete.",
                          name="Meshak")
    sent = send(sender, first)
    first_id = sent["messageId"]
    if not first_id:
        raise RuntimeError("could not read back the Message-ID Gmail used")
    log(f"sent    {sender} -> {recipient}: thread {sent['threadId']} {first_id}")
    received = _await(recipient, first_id)
    unread = fetch_unread(recipient, f"is:unread rfc822msgid:{first_id.strip('<>')}")
    log(f"read    {recipient}: {len(unread)} unread copy found")
    for gmail_id, _thread, _raw in unread:
        mark_read(recipient, gmail_id)
    reply = build_message(recipient, sender, "Re: " + subject, "Reply in the same thread.",
                          name="Meshak", in_reply_to=first_id, references=first_id)
    replied = send(recipient, reply, thread_id=received["threadId"])
    log(f"replied {recipient} -> {sender}: {replied['messageId']}")
    back = _await(sender, replied["messageId"])
    same = back["threadId"] == sent["threadId"]
    log(f"thread  reply in {sender}: thread {back['threadId']} -> "
        f"{'SAME THREAD' if same else 'DIFFERENT THREAD'}")
    return 0 if same else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--check"]:
        raise SystemExit(1 if check() else 0)
    if len(args) == 3 and args[0] == "--thread-test":
        raise SystemExit(thread_test(args[1].strip().lower(), args[2].strip().lower()))
    sys.exit(__doc__)
