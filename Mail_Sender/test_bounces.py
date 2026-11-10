import email
import unittest
from datetime import datetime, timezone
from email import policy
from unittest.mock import patch

import bounces


def dsn(recipient, status, diagnostic, action="failed", text="Delivery failed."):
    raw = f"""From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: me@example.com
Subject: Delivery Status Notification (Failure)
Message-ID: <bounce-{recipient}-{status}@mx.google.com>
Date: Tue, 10 Nov 2026 10:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="B"

--B
Content-Type: text/plain; charset=utf-8

{text}

--B
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com
Arrival-Date: Tue, 10 Nov 2026 09:59:00 +0000

Final-Recipient: rfc822; {recipient}
Action: {action}
Status: {status}
Diagnostic-Code: smtp; {diagnostic}

--B--
""".replace("\n", "\r\n").encode()
    return raw


def gmail_text_only(recipient, body):
    return f"""From: mailer-daemon@googlemail.com
Subject: Delivery Status Notification (Failure)
X-Failed-Recipients: {recipient}
Message-ID: <plain-{recipient}@mx.google.com>
Date: Tue, 10 Nov 2026 10:00:00 +0000
Content-Type: text/plain; charset=utf-8

{body}
""".encode()


def parse(raw):
    return email.message_from_bytes(raw, policy=policy.default)


class ParseTest(unittest.TestCase):
    def check(self, raw):
        msg = parse(raw)
        body = bounces._body(msg)
        return [(r, bounces.is_dead(s, d, a, body)) for r, s, d, a in bounces.failures(msg)]

    def test_dead_addresses(self):
        cases = [
            dsn("a@studio.test", "5.1.1", "550-5.1.1 The email account that you tried to reach does not exist."),
            dsn("b@gone.test", "5.1.2", "DNS Error: Domain name not found"),
            dsn("c@gmail.com", "5.2.1", "550 5.2.1 The email account that you tried to reach is disabled."),
            dsn("d@x.test", "5.0.0", "550 No such user here"),
        ]
        for raw in cases:
            with self.subTest(raw=raw[:0]):
                [(_, dead)] = self.check(raw)
                self.assertTrue(dead)

    def test_not_a_dead_address(self):
        cases = [
            dsn("a@x.test", "5.7.1", "550 5.7.1 Service unavailable; client blocked using Spamhaus"),
            dsn("b@x.test", "5.7.0", "550 High probability of spam"),
            dsn("c@x.test", "5.2.2", "552 5.2.2 Mailbox full"),
            dsn("d@x.test", "4.4.1", "Connection timed out", action="delayed"),
            dsn("e@x.test", "5.4.4", "DNS Error: remote server is misconfigured"),
        ]
        for raw in cases:
            with self.subTest():
                [(_, dead)] = self.check(raw)
                self.assertFalse(dead)

    def test_gmail_text_fallback(self):
        dead = gmail_text_only("x@studio.test",
                               "Address not found. Your message wasn't delivered to x@studio.test because "
                               "the address couldn't be found. The response was: 550 5.1.1 The email "
                               "account that you tried to reach does not exist.")
        self.assertEqual(self.check(dead), [("x@studio.test", True)])
        blocked = gmail_text_only("y@studio.test", "Message blocked. 550 5.7.1 Service unavailable")
        self.assertEqual(self.check(blocked), [("y@studio.test", False)])


class ProcessTest(unittest.TestCase):
    def run_process(self, raws, sent, apply=True):
        msgs = [(bounces.message_key(parse(r), r), parse(r), r) for r in raws]
        calls = {"reject": [], "cache": [], "log": []}

        def reject(appid, **kw):
            calls["reject"].append((appid, kw))
            return "invalid" if kw["researched_to_invalid"] and dict(
                (a, s) for a, _e, s in sent)[appid] == "scraped" else "no_mail"

        with patch.object(bounces, "fetch_bounces", return_value=msgs), \
                patch.object(bounces.pipeline, "bounce_seen", return_value=False), \
                patch.object(bounces.pipeline, "sent_rows", return_value=sent), \
                patch.object(bounces.pipeline, "reject_email", side_effect=reject), \
                patch.object(bounces.pipeline, "cache_email_verification",
                             side_effect=lambda e, r: calls["cache"].append((e, r["result"]))), \
                patch.object(bounces.pipeline, "record_bounce",
                             side_effect=lambda k, **kw: calls["log"].append(kw["outcome"])):
            stats = bounces.process("u", "p", since=datetime(2026, 1, 1, tzinfo=timezone.utc),
                                    apply=apply, log=lambda *_: None)
        return stats, calls

    def test_seeded_goes_to_no_mail_and_scraped_to_invalid(self):
        sent = [(1, "Hello@Studio.test", "seeded"), (2, "info@other.test", "scraped"),
                (3, "fine@ok.test", "seeded")]
        stats, calls = self.run_process(
            [dsn("hello@studio.test", "5.1.1", "550 5.1.1 user unknown"),
             dsn("info@other.test", "5.1.1", "550 5.1.1 user unknown"),
             dsn("fine@ok.test", "5.7.1", "550 5.7.1 blocked")], sent)
        self.assertEqual([a for a, _ in calls["reject"]], [1, 2])
        self.assertTrue(all(kw["from_statuses"] == ("Sent",) for _, kw in calls["reject"]))
        self.assertEqual((stats["no_mail"], stats["invalid"], stats["ignored"]), (1, 1, 1))
        self.assertEqual(calls["cache"], [("hello@studio.test", "invalid"), ("info@other.test", "invalid")])
        self.assertEqual(calls["log"], ["no_mail", "invalid", "ignored"])

    def test_unmatched_bounce_is_logged_not_applied(self):
        stats, calls = self.run_process([dsn("nobody@x.test", "5.1.1", "550 5.1.1 unknown")], [])
        self.assertEqual((stats["unmatched"], calls["reject"]), (1, []))

    def test_report_mode_writes_nothing(self):
        stats, calls = self.run_process([dsn("a@x.test", "5.1.1", "550 5.1.1 unknown")],
                                        [(1, "a@x.test", "seeded")], apply=False)
        self.assertEqual((calls["reject"], calls["cache"], calls["log"]), ([], [], []))
        self.assertEqual(stats["no_mail"], 1)


if __name__ == "__main__":
    unittest.main()
