"""Collect studio replies for the dashboard's Replies view (table mail_replies).

Runs in the nightly reply check (mailer.review) and on demand from the dashboard's Refresh
button (review-mails.yml). For every game marked Replied it reads the sending mailbox and
stores each reply in the thread (sender, date, subject, text), its status and the outreach
mail it answers:
  unread / read  the mailbox's own read flag on the reply (read with PEEK, never changed)
  replied        a mail to the studio's address in Sent after that reply
Matching uses the same strict rule as reply detection (mailer._matches_reply): exact sender,
our Message-ID in In-Reply-To/References, dated after the send. Read-only on the mailboxes.
"""
import email
import hashlib
import html
import re
from datetime import timezone
from email import policy
from email.utils import getaddresses, parsedate_to_datetime

import pipeline

BODY_LIMIT = 8000
# Lines where a mail client starts quoting the previous message (several languages).
_QUOTE = re.compile(
    r"^\s*(On .{3,200}(wrote|écrit|escribió|schrieb|scritto):?\s*$"
    r"|>.*$"
    r"|-{2,}\s*Original Message\s*-{2,}"
    r"|_{10,}\s*$"
    r"|From:\s.+$"
    r"|.{0,120}(napisał\(a\)|yazdı|napsal|пише|пишет|έγραψε|작성|写道|wrote):?\s*$"
    r"|\d{4}年.{0,60}$"
    r"|Στις .{0,80}$)", re.I)


def text_of(msg):
    """Plain text of a mail: the text part, else the HTML part without tags."""
    part = msg.get_body(preferencelist=("plain", "html")) if msg.is_multipart() else msg
    try:
        content = part.get_content() if part is not None else ""
    except Exception:
        payload = part.get_payload(decode=True) if part is not None else b""
        content = (payload or b"").decode("utf-8", "replace")
    if part is not None and part.get_content_subtype() == "html":
        content = re.sub(r"(?is)<(script|style).*?</\1>", " ", content)
        content = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", content)
        content = html.unescape(re.sub(r"<[^>]+>", " ", content))
    return content.replace("\r", "")


def reply_text(msg):
    """The new part of a reply: everything above the quoted earlier message."""
    lines = []
    for line in text_of(msg).split("\n"):
        if _QUOTE.match(line):
            break
        lines.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return (text or text_of(msg).strip())[:BODY_LIMIT]


def _when(msg):
    try:
        return parsedate_to_datetime(str(msg["Date"]))
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _iso(when):
    if not when:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _thread_replies(candidates, sent_ids, addr, message_id, started_at, matches):
    """Replies in the conversation: mail from the studio that answers our outreach mail, a
    later mail of ours to them, or one of their earlier replies (a follow-up)."""
    known, picked = {message_id, *sent_ids}, []
    changed = True
    while changed:
        changed = False
        for raw, read in candidates:
            if any(raw is p for p, _ in picked):
                continue
            if any(matches(raw, addr, ref, started_at) for ref in known):
                picked.append((raw, read))
                mid = str(email.message_from_bytes(raw, policy=policy.default).get("Message-ID") or "").strip()
                if mid:
                    known.add(mid)
                changed = True
    return picked


def _sender(msg):
    pairs = getaddresses([str(msg.get("From") or "")])
    return pairs[0] if pairs else ("", "")


def reply_row(appid, mailbox, raw, read, replied, ours):
    msg = email.message_from_bytes(raw, policy=policy.default)
    name, addr = _sender(msg)
    reply_id = str(msg.get("Message-ID") or "").strip() or "sha256:" + hashlib.sha256(raw).hexdigest()
    return dict(reply_id=reply_id, appid=int(appid), mailbox=mailbox, from_name=name or None,
                from_addr=addr.lower() or None, subject=str(msg.get("Subject") or ""),
                received_at=_iso(_when(msg)), body=reply_text(msg),
                status="replied" if replied else "read" if read else "unread",
                our_subject=ours[0] if ours else None, our_body=ours[1] if ours else None,
                our_sent_at=ours[2] if ours else None)


def our_message(raw):
    msg = email.message_from_bytes(raw, policy=policy.default)
    return (str(msg.get("Subject") or ""), text_of(msg).strip()[:BODY_LIMIT], _iso(_when(msg)))


# -- IMAP mailboxes (app passwords, Microsoft) -----------------------------------------
def _sent_folder(imap):
    typ, folders = imap.list()
    for folder in (folders or []) if typ == "OK" else []:
        match = re.match(rb'\([^)]*\\Sent\b[^)]*\)\s+(?:"[^"]*"|NIL)\s+(.+)', folder or b"", re.I)
        if match:
            return match.group(1).decode("ascii")
    return '"[Gmail]/Sent Mail"'


def _fetch(imap, number):
    """(raw bytes, read flag) without marking the message read."""
    typ, parts = imap.fetch(number, "(FLAGS BODY.PEEK[])")
    if typ != "OK":
        return None, False
    raw, flags = None, b""
    for item in parts or []:
        if isinstance(item, tuple):
            flags += item[0] or b""
            raw = item[1]
        elif isinstance(item, bytes):
            flags += item
    return raw, b"\\Seen" in flags


def _imap_thread(imap, sent_folder, appid, addr, message_id, started_at, matches, since):
    imap.select("INBOX", readonly=True)
    typ, data = imap.search(None, "FROM", f'"{addr}"', "SINCE", since(started_at))
    candidates = []
    for number in (data[0].split() if typ == "OK" and data and data[0] else []):
        raw, read = _fetch(imap, number)
        if raw:
            candidates.append((raw, read))
    if not candidates:
        return [], None, []
    imap.select(sent_folder, readonly=True)
    typ, data = imap.search(None, "TO", f'"{addr}"', "SINCE", since(started_at))
    sent = []
    for number in (data[0].split() if typ == "OK" and data and data[0] else []):
        raw, _ = _fetch(imap, number)
        if raw:
            sent.append(email.message_from_bytes(raw, policy=policy.default))
    ours = next((m for m in sent if str(m.get("Message-ID") or "").strip() == message_id), None)
    sent_ids = {str(m.get("Message-ID") or "").strip() for m in sent} - {""}
    replies = _thread_replies(candidates, sent_ids, addr, message_id, started_at, matches)
    later = [_when(m) for m in sent if m is not ours and _when(m)]
    return replies, ours, later


# -- shared ---------------------------------------------------------------------------
def _rows_for(appid, mailbox, replies, ours_msg, later_sends):
    stored = pipeline.stored_our_message(appid)
    ours = stored or (our_message(ours_msg.as_bytes()) if ours_msg is not None else None)
    rows = []
    for raw, read in replies:
        when = _when(email.message_from_bytes(raw, policy=policy.default))
        replied = bool(when and any(sent > when for sent in later_sends))
        rows.append(reply_row(appid, mailbox, raw, read, replied, ours))
    return rows


def sync_account(account, threads, *, matches, since, recipient, connect, log=print):
    """Store replies for one mailbox. threads: [(appid, emails, message_id, started_at)]."""
    saved = 0
    if account.gmail:
        import gmail_api
        for appid, raw_email, message_id, started_at in threads:
            addr = recipient(raw_email)
            ours = gmail_api.find_message_id(account.email, message_id) if addr else None
            if not ours:
                continue
            candidates, later, sent_ids, ours_msg = [], [], set(), None
            for m in gmail_api.thread_messages(account.email, ours["threadId"]):
                raw = gmail_api.get_raw(account.email, m["id"])
                parsed = email.message_from_bytes(raw, policy=policy.default)
                if m["id"] == ours["id"]:
                    ours_msg = parsed
                elif "SENT" in m["labelIds"]:
                    sent_ids.add(str(parsed.get("Message-ID") or "").strip())
                    if _when(parsed):
                        later.append(_when(parsed))
                else:
                    candidates.append((raw, "UNREAD" not in m["labelIds"]))
            replies = _thread_replies(candidates, sent_ids - {""}, addr, message_id, started_at, matches)
            for row in _rows_for(appid, account.email, replies, ours_msg, later):
                pipeline.save_reply(row)
                saved += 1
        return saved
    imap = connect(account.imap)
    try:
        account.imap_login(imap)
        sent_folder = _sent_folder(imap)
        for appid, raw_email, message_id, started_at in threads:
            addr = recipient(raw_email)
            if not addr:
                continue
            replies, ours_msg, later = _imap_thread(imap, sent_folder, appid, addr, message_id,
                                                    started_at, matches, since)
            for row in _rows_for(appid, account.email, replies, ours_msg, later):
                pipeline.save_reply(row)
                saved += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return saved


# -- dashboard "Mark as read" -> the mailbox's read flag ------------------------------------
def mark_read(account, items, *, connect, log=print):
    """Set the read flag on each (reply_id, from_addr) in this mailbox. Returns reply_ids done."""
    done = []
    if account.gmail:
        import gmail_api
        for reply_id, _addr in items:
            hit = gmail_api.find_message_id(account.email, reply_id)
            if hit:
                gmail_api.mark_read(account.email, hit["id"])
                done.append(reply_id)
            else:
                log(f"  not found in {account.email}: {reply_id}")
        return done
    imap = connect(account.imap)
    try:
        account.imap_login(imap)
        if imap.select("INBOX")[0] != "OK":       # read-write: this is the one write we make
            raise RuntimeError("cannot open INBOX")
        for reply_id, addr in items:
            # Outlook ignores HEADER searches: find by sender, then match the Message-ID exactly.
            typ, data = imap.search(None, "FROM", f'"{addr}"')
            found = False
            for number in (data[0].split() if typ == "OK" and data and data[0] else []):
                typ, parts = imap.fetch(number, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                header = next((p[1] for p in parts or [] if isinstance(p, tuple)), b"")
                msg = email.message_from_bytes(header or b"", policy=policy.default)
                if str(msg.get("Message-ID") or "").strip() == reply_id:
                    imap.store(number, "+FLAGS", "(\\Seen)")
                    found = True
            if found:
                done.append(reply_id)
            else:
                log(f"  not found in {account.email}: {reply_id}")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return done
