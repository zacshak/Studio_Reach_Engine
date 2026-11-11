import json
import os
import smtplib
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from unittest.mock import patch

import mailer


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        enter = self.stack.enter_context
        enter(patch.dict(os.environ, {
            "GMAIL_USER": "old@gmail.test", "GMAIL_APP_PASSWORD": "oldpw",
            "SENDER_ACCOUNTS": json.dumps({"a@studio.test": "pw a", "b@studio.test": "pwb",
                                           "p@gmail.test": "pwp", "m@ms.test": "pwm"})}, clear=True))
        self.db = os.path.join(self.temp.name, "t.sqlite")
        enter(patch.object(mailer.pipeline, "TURSO_URL", ""))
        enter(patch.object(mailer.pipeline, "DB_PATH", self.db))
        enter(patch.object(mailer.pipeline, "_ensured", False))
        self.sql("CREATE TABLE newly_added (appid INTEGER PRIMARY KEY, success INTEGER, fetched_at TEXT)")
        mailer.pipeline.init_tracker()
        for email, provider, cap in (("a@studio.test", "google", 2), ("b@studio.test", "google", 1),
                                     ("p@gmail.test", "google", 0), ("m@ms.test", "microsoft", 10),
                                     ("nopw@studio.test", "google", 5)):
            self.sql("INSERT INTO sender_accounts (email, provider, daily_cap) VALUES (?,?,?)",
                     (email, provider, cap))
        self.send = enter(patch.object(mailer, "_send"))
        enter(patch.object(mailer, "_delete_media"))
        enter(patch.object(mailer, "_load_mail", return_value=("Subject", "Body", "draft.txt")))
        enter(patch.object(mailer.time, "sleep"))
        enter(patch.object(mailer.time, "monotonic", return_value=1000))
        enter(patch("builtins.print"))

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute(query, args).fetchall()
            conn.commit()
            return rows

    def seed(self, n):
        for appid in range(1, n + 1):
            self.sql("INSERT INTO scrape_tracker (appid,emails,scrape_status,Mail_status) "
                     "VALUES (?,?,'seeded','Scheduled')", (appid, f"s{appid}@example.com"))

    def senders(self):
        return [c.args[0] for c in self.send.call_args_list]

    def test_rotates_within_caps_and_leaves_the_rest_scheduled(self):
        self.seed(5)
        self.assertEqual(mailer.main(), 0)
        self.assertEqual(sorted(self.senders()), ["a@studio.test", "a@studio.test", "b@studio.test"])
        self.assertEqual(self.senders(), ["a@studio.test", "b@studio.test", "a@studio.test"])
        states = [r[0] for r in self.sql("SELECT Mail_status FROM scrape_tracker ORDER BY appid")]
        self.assertEqual(states, ["Sent", "Sent", "Sent", "Scheduled", "Scheduled"])
        log = self.sql("SELECT sender, count(*) FROM mail_send_log GROUP BY sender ORDER BY sender")
        self.assertEqual(log, [("a@studio.test", 2), ("b@studio.test", 1)])
        # Passwords are passed without spaces (Google shows app passwords in groups of 4).
        self.assertEqual(self.send.call_args_list[0].args[1], "pwa")
        # The next run within 24h sends nothing more.
        self.send.reset_mock()
        self.assertEqual(mailer.main(), 0)
        self.send.assert_not_called()

    def test_status_shows_why_an_account_is_not_sending(self):
        self.seed(1)
        mailer.main()
        status = dict(self.sql("SELECT email, last_status FROM sender_accounts"))
        self.assertEqual(status["p@gmail.test"], "paused (daily cap 0)")
        self.assertEqual(status["m@ms.test"], "Microsoft sign-in not set up yet")
        self.assertEqual(status["nopw@studio.test"], "no app password in SENDER_ACCOUNTS")
        self.assertEqual(status["a@studio.test"], "ok")

    def test_login_failure_drops_only_that_account(self):
        self.seed(3)
        def send(user, *a, **k):
            if user == "a@studio.test":
                raise mailer.MailNotSubmittedError("auth") from smtplib.SMTPAuthenticationError(535, b"bad")
        self.send.side_effect = send
        self.assertEqual(mailer.main(), 0)
        self.assertEqual(self.senders(), ["a@studio.test", "b@studio.test"])
        states = [r[0] for r in self.sql("SELECT Mail_status FROM scrape_tracker ORDER BY appid")]
        # a's mail went back to Scheduled; b sent the next one and is then at its cap of 1.
        self.assertEqual(states, ["Scheduled", "Sent", "Scheduled"])
        status = dict(self.sql("SELECT email, last_status FROM sender_accounts"))
        self.assertIn("rejected", status["a@studio.test"])

    def test_review_reads_each_senders_inbox_for_its_own_mails(self):
        self.seed(2)
        mailer.main()
        opened = []
        class Imap:
            def __init__(self, host, timeout):
                self.host = host
            def login(self, user, pw):
                opened.append(user)
            def select(self, *a, **k):
                return ("OK", [b"0"])
            def search(self, *a):
                return ("OK", [b""])
            def logout(self):
                pass
        with patch.object(mailer.imaplib, "IMAP4_SSL", Imap), \
                patch.object(mailer, "_scan_bounces") as bounces:
            mailer.review()
        scanned = sorted(c.args[0] for c in bounces.call_args_list)
        self.assertEqual(scanned, ["a@studio.test", "b@studio.test", "old@gmail.test", "p@gmail.test"])
        self.assertEqual(sorted(opened), ["a@studio.test", "b@studio.test"])

    def test_check_accounts_logs_in_without_sending(self):
        from unittest.mock import Mock
        smtp, imap = Mock(), Mock()
        def login(user, pw):
            if user == "b@studio.test":
                raise smtplib.SMTPAuthenticationError(535, b"bad")
        smtp.login.side_effect = login
        with patch.object(mailer.smtplib, "SMTP", return_value=smtp), \
                patch.object(mailer.imaplib, "IMAP4_SSL", return_value=imap), \
                patch.object(mailer.ssl, "create_default_context"):
            self.assertEqual(mailer.check_accounts(), 1)
        smtp.send_message.assert_not_called()
        status = dict(self.sql("SELECT email, last_status FROM sender_accounts"))
        self.assertEqual(status["a@studio.test"], "ok (login checked)")
        self.assertIn("login failed", status["b@studio.test"])
        self.assertEqual(status["m@ms.test"], "Microsoft sign-in not set up yet")
        self.assertEqual(status["nopw@studio.test"], "no app password in SENDER_ACCOUNTS")


if __name__ == "__main__":
    unittest.main()
