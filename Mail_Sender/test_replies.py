"""Replies view sync: a fake IMAP mailbox and a temp SQLite database. No network."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import mailer
import replies

OURS = "<ours.sre.1@studio.test>"
STARTED = "2026-11-12 12:30:00"


def mail(sender, to, subject, date, body, message_id, in_reply_to=None):
    head = (f"From: {sender}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: {date}\r\n"
            f"Message-ID: {message_id}\r\n")
    if in_reply_to:
        head += f"In-Reply-To: {in_reply_to}\r\nReferences: {in_reply_to}\r\n"
    return (head + "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body).encode()


OUR_MAIL = mail("Meshak <me@studio.test>", "dev@game.test", "FixIt: chain bonuses?",
                "Thu, 12 Nov 2026 12:30:34 +0000", "hey WilDanger Studio,\r\nwhat if mods chained?", OURS)
REPLY = mail("Michal <dev@game.test>", "me@studio.test", "Re: FixIt: chain bonuses?",
             "Thu, 19 Nov 2026 14:32:54 +0000",
             "Hey Meshak,\r\n\r\nSend me your portfolio.\r\n\r\nOn Thu, 12 Nov 2026, Meshak wrote:\r\n> hey WilDanger Studio,\r\n",
             "<reply.1@game.test>", OURS)
UNRELATED = mail("Michal <dev@game.test>", "me@studio.test", "Hello", "Fri, 20 Nov 2026 10:00:00 +0000",
                 "Different thread", "<other@game.test>")
SECOND = mail("Michal <dev@game.test>", "me@studio.test", "Re: Re: FixIt", "Sat, 21 Nov 2026 10:00:00 +0100",
              "Thanks, looking at it now.", "<reply.2@game.test>", "<followup@studio.test>")
FOLLOW_UP = mail("Meshak <me@studio.test>", "dev@game.test", "Re: FixIt", "Fri, 20 Nov 2026 09:00:00 +0000",
                 "Here it is", "<followup@studio.test>", "<reply.1@game.test>")


class FakeImap:
    """INBOX and Sent with FLAGS. Like Outlook, HEADER searches return nothing."""

    def __init__(self, inbox, sent):
        self.boxes = {"INBOX": inbox, '"Sent Items"': sent}
        self.current = None
        self.peeked_only = True

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent Items"']

    def select(self, name, readonly=False):
        assert readonly, "the sync must never open a mailbox read-write"
        self.current = self.boxes[name]
        return "OK", [str(len(self.current)).encode()]

    def search(self, charset, *criteria):
        if "HEADER" in criteria:
            return "OK", [b""]
        key, value = criteria[0], criteria[1].strip('"')
        field = "From" if key == "FROM" else "To"
        hits = [str(i + 1).encode() for i, (raw, _) in enumerate(self.current)
                if value in raw.decode().split(f"{field}: ", 1)[1].split("\r\n", 1)[0]]
        return "OK", [b" ".join(hits)]

    def fetch(self, number, what):
        if "PEEK" not in what:
            self.peeked_only = False
        raw, seen = self.current[int(number) - 1]
        flags = b"(\\Seen)" if seen else b"()"
        return "OK", [(number + b" (FLAGS " + flags + b" BODY[] {%d}" % len(raw), raw), b")"]

    def logout(self):
        pass


class Account:
    gmail, email, imap = False, "me@studio.test", "outlook.office365.com"

    def imap_login(self, imap):
        pass


class RepliesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = os.path.join(temp.name, "t.sqlite")
        for target, value in (("TURSO_URL", ""), ("DB_PATH", self.db), ("_ensured", False)):
            p = patch.object(mailer.pipeline, target, value)
            p.start()
            self.addCleanup(p.stop)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE newly_added (appid INTEGER PRIMARY KEY, success INTEGER, fetched_at TEXT)")
        mailer.pipeline.init_tracker()

    def sync(self, inbox, sent):
        imap = FakeImap(inbox, sent)
        n = replies.sync_account(Account(), [(7, "dev@game.test", OURS, STARTED)],
                                 matches=mailer._matches_reply, since=mailer._imap_since,
                                 recipient=mailer._recipient, connect=lambda host: imap)
        self.assertTrue(imap.peeked_only, "reading must not mark mail as read")
        return n

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM mail_replies")]

    def test_stores_reply_text_without_quotes_and_our_mail(self):
        self.assertEqual(self.sync([(REPLY, False), (UNRELATED, False)], [(OUR_MAIL, True)]), 1)
        [row] = self.rows()
        self.assertEqual(row["reply_id"], "<reply.1@game.test>")
        self.assertEqual(row["appid"], 7)
        self.assertEqual(row["from_name"], "Michal")
        self.assertEqual(row["from_addr"], "dev@game.test")
        self.assertEqual(row["received_at"], "2026-11-19T14:32:54Z")
        self.assertEqual(row["body"], "Hey Meshak,\n\nSend me your portfolio.")
        self.assertEqual(row["status"], "unread")
        self.assertEqual(row["our_subject"], "FixIt: chain bonuses?")
        self.assertIn("what if mods chained?", row["our_body"])
        self.assertEqual(row["mailbox"], "me@studio.test")

    def test_status_follows_the_read_flag_then_a_later_mail_from_us(self):
        self.sync([(REPLY, True)], [(OUR_MAIL, True)])
        self.assertEqual(self.rows()[0]["status"], "read")
        self.sync([(REPLY, True)], [(OUR_MAIL, True), (FOLLOW_UP, True)])
        [row] = self.rows()                       # same reply updated, not duplicated
        self.assertEqual(row["status"], "replied")

    def test_a_follow_up_reply_in_the_conversation_is_stored_too(self):
        self.assertEqual(self.sync([(REPLY, True), (SECOND, False), (UNRELATED, False)],
                                   [(OUR_MAIL, True), (FOLLOW_UP, True)]), 2)
        rows = {r["reply_id"]: r for r in self.rows()}
        self.assertEqual(set(rows), {"<reply.1@game.test>", "<reply.2@game.test>"})
        self.assertEqual(rows["<reply.2@game.test>"]["received_at"], "2026-11-21T09:00:00Z")  # UTC
        self.assertEqual(rows["<reply.2@game.test>"]["status"], "unread")
        self.assertEqual(rows["<reply.1@game.test>"]["status"], "replied")

    def test_mail_outside_the_thread_is_never_stored(self):
        self.assertEqual(self.sync([(UNRELATED, False)], [(OUR_MAIL, True)]), 0)
        self.assertEqual(self.rows(), [])

    def test_html_only_reply_becomes_text(self):
        msg = REPLY.replace(b"Content-Type: text/plain", b"Content-Type: text/html").replace(
            b"Send me your portfolio.", b"<p>Send me your <b>portfolio</b>.</p>")
        self.sync([(msg, False)], [(OUR_MAIL, True)])
        self.assertIn("Send me your  portfolio .", self.rows()[0]["body"].replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
