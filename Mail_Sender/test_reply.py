"""Reply attribution tests. IMAP and database writes are always mocked."""
import os
import unittest
from unittest.mock import Mock, patch

import mailer


class ReplyTest(unittest.TestCase):
    started = "2026-10-16 10:00:00"
    message_id = "<outgoing@example.com>"

    def headers(self, sender="Studio <studio@example.com>",
                date="Fri, 16 Oct 2026 10:01:00 +0000",
                thread="In-Reply-To: <outgoing@example.com>"):
        return f"From: {sender}\r\nDate: {date}\r\n{thread}\r\n\r\n".encode()

    def test_exact_sender_thread_and_date_are_all_required(self):
        self.assertTrue(mailer._matches_reply(
            self.headers(), "studio@example.com", self.message_id, self.started))
        for headers in (
            self.headers(sender="otherstudio@example.com"),
            self.headers(sender="studio@example.com.evil.test"),
            self.headers(sender="studio@example.com, other@example.com"),
            self.headers(date="Thu, 15 Oct 2026 10:01:00 +0000"),
            self.headers(date="not a date"),
            self.headers(date="Fri, 16 Oct 2026 10:01:00 -0000"),
            self.headers(thread="In-Reply-To: <unrelated@example.com>"),
            self.headers(thread="In-Reply-To: <prefixoutgoing@example.com>"),
            self.headers(thread=""),
        ):
            with self.subTest(headers=headers):
                self.assertFalse(mailer._matches_reply(
                    headers, "studio@example.com", self.message_id, self.started))
        duplicate = self.headers().replace(b"\r\n\r\n", b"\r\nFrom: studio@example.com\r\n\r\n")
        self.assertFalse(mailer._matches_reply(
            duplicate, "studio@example.com", self.message_id, self.started))

    def test_references_and_timezone_conversion_accept_a_genuine_reply(self):
        headers = self.headers(sender="STUDIO@EXAMPLE.COM",
                               date="Fri, 16 Oct 2026 15:31:00 +0530",
                               thread="References: <older@example.com>\r\n <outgoing@example.com>")
        self.assertTrue(mailer._matches_reply(
            headers, "studio@example.com", self.message_id, self.started))

    def review(self, evidence, headers, select="OK", fetch="OK"):
        imap = Mock()
        imap.select.return_value = (select, [b"1"])
        imap.search.return_value = ("OK", [b"7"])
        imap.fetch.return_value = (fetch, [(b"7 (BODY[HEADER] {100}", headers), b")"])
        with patch.object(mailer, "_load_env"), \
                patch.dict(os.environ, {"GMAIL_USER": "sender@example.com", "GMAIL_APP_PASSWORD": "fake"}), \
                patch.object(mailer.pipeline, "reply_evidence", return_value=evidence), \
                patch.object(mailer.pipeline, "set_mail_status") as status, \
                patch.object(mailer.imaplib, "IMAP4_SSL", return_value=imap) as connect:
            if select != "OK":
                with self.assertRaises(RuntimeError):
                    mailer.review()
            else:
                mailer.review()
        return imap, status, connect

    def test_shared_recipient_only_marks_the_matching_thread(self):
        evidence = [(1, "studio@example.com", self.message_id, self.started),
                    (2, "studio@example.com", "<second@example.com>", self.started)]
        imap, status, _ = self.review(evidence, self.headers())
        status.assert_called_once_with(1, "Replied")
        imap.select.assert_called_once_with("INBOX", readonly=True)
        self.assertEqual(imap.fetch.call_args.args[1],
                         "(BODY.PEEK[HEADER.FIELDS (FROM DATE IN-REPLY-TO REFERENCES)])")
        self.assertEqual(imap.search.call_args.args[1:4], ("OR", "HEADER", "In-Reply-To"))
        imap.logout.assert_called_once()

    def test_old_or_unrelated_message_never_marks_replied(self):
        evidence = [(1, "studio@example.com", self.message_id, self.started)]
        for headers in (self.headers(date="Thu, 15 Oct 2026 10:00:00 +0000"),
                        self.headers(thread="In-Reply-To: <other@example.com>")):
            with self.subTest(headers=headers):
                _, status, _ = self.review(evidence, headers)
                status.assert_not_called()

    def test_no_evidence_never_opens_imap(self):
        _, status, connect = self.review([], self.headers())
        status.assert_not_called()
        connect.assert_not_called()

    def test_failed_select_or_fetch_does_not_mark_replied_and_logs_out(self):
        evidence = [(1, "studio@example.com", self.message_id, self.started)]
        for select, fetch in (("NO", "OK"), ("OK", "NO")):
            imap, status, _ = self.review(evidence, self.headers(), select, fetch)
            status.assert_not_called()
            imap.logout.assert_called_once()


if __name__ == "__main__":
    unittest.main()
