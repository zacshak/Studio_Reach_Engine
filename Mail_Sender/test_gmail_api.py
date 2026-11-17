"""Gmail API backend: config, retries, send/reply/read, and mailer backend selection.
No network: the Gmail service is a fake, the database a temp SQLite file."""
import base64
import email
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from email import policy
from unittest.mock import MagicMock, Mock, patch

import httplib2
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

import gmail_api
import mailer

WS = "meshak@thaswin.com"


def http_error(status, reason=""):
    content = (b'{"error":{"errors":[{"reason":"%s"}]}}' % reason.encode()) if reason else b"{}"
    return HttpError(httplib2.Response({"status": status}), content)


class Request:
    """A fake googleapiclient request: each execute() takes the next outcome."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def execute(self):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ConfigTest(unittest.TestCase):
    def setUp(self):
        gmail_api._config = None
        self.addCleanup(setattr, gmail_api, "_config", None)

    def test_defaults_follow_the_domain_list(self):
        for box in ("meshak@thaswinmeshak.com", "meshakzac@thaswin.com", "x@heymeshak.com",
                    "NEW@HireMeshak.com"):
            self.assertEqual(gmail_api.backend_for(box), "gmail_api", box)
        for box in ("meshak@zacgamedev.com", "meshak@xrevise.cc", "meshakzac@gmail.com"):
            self.assertEqual(gmail_api.backend_for(box), "app_password", box)
        self.assertEqual(len(gmail_api.workspace_inboxes()), 8)
        self.assertFalse(gmail_api.failover_enabled())

    def _config_with(self, data):
        path = os.path.join(tempfile.mkdtemp(), "mail_backends.json")
        with open(path, "w") as f:
            import json
            json.dump(data, f)
        patcher = patch.object(gmail_api, "CONFIG_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        gmail_api._config = None

    def test_a_workspace_inbox_switches_back_to_app_password_by_config_alone(self):
        self._config_with({"gmail_api_domains": ["thaswin.com"],
                           "mailboxes": {WS: {"backend": "app_password"}}})
        self.assertEqual(gmail_api.backend_for(WS), "app_password")
        self.assertEqual(gmail_api.backend_for("meshakzac@thaswin.com"), "gmail_api")

    def test_gmail_api_is_refused_for_a_mailbox_outside_the_workspace_domains(self):
        self._config_with({"gmail_api_domains": ["thaswin.com"],
                           "mailboxes": {"meshak@zacgamedev.com": {"backend": "gmail_api"}}})
        with self.assertRaises(ValueError):
            gmail_api.config()

    def test_unknown_backend_is_refused(self):
        self._config_with({"gmail_api_domains": [], "mailboxes": {WS: {"backend": "smtp"}}})
        with self.assertRaises(ValueError):
            gmail_api.config()


class ClientTest(unittest.TestCase):
    def setUp(self):
        gmail_api._clients.clear()
        self.addCleanup(gmail_api._clients.clear)

    def test_missing_key_file_is_a_clear_auth_error(self):
        with patch.dict(os.environ, {"GOOGLE_SA_KEY_FILE": "/nope/key.json"}):
            with self.assertRaisesRegex(gmail_api.GmailAuthError, "GOOGLE_SA_KEY_FILE"):
                gmail_api.client(WS)

    def test_one_cached_client_per_mailbox_impersonating_it(self):
        key = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.addCleanup(os.unlink, key.name)
        creds = MagicMock()
        with patch.dict(os.environ, {"GOOGLE_SA_KEY_FILE": key.name}), \
                patch("google.oauth2.service_account.Credentials.from_service_account_file",
                      return_value=creds) as load, \
                patch("googleapiclient.discovery.build") as build:
            first = gmail_api.client(WS)
            self.assertIs(gmail_api.client(WS.upper()), first)
            gmail_api.client("meshakzac@thaswin.com")
        self.assertEqual(load.call_args.kwargs["scopes"], ["https://mail.google.com/"])
        self.assertEqual([c.args[0] for c in creds.with_subject.call_args_list],
                         [WS, "meshakzac@thaswin.com"])
        self.assertEqual(build.call_count, 2)

    def test_non_workspace_mailbox_is_never_impersonated(self):
        with self.assertRaises(gmail_api.GmailAuthError):
            gmail_api.client("meshak@zacgamedev.com")


class CallTest(unittest.TestCase):
    def setUp(self):
        self.sleep = Mock()

    def test_429_and_5xx_are_retried_with_backoff(self):
        req = Request(http_error(429), http_error(503), {"ok": 1})
        self.assertEqual(gmail_api.execute(WS, req, sleep=self.sleep), {"ok": 1})
        self.assertEqual(req.calls, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_retries_stop_after_the_limit(self):
        req = Request(*[http_error(500)] * (gmail_api.RETRIES + 1))
        with self.assertRaises(HttpError):
            gmail_api.execute(WS, req, sleep=self.sleep)
        self.assertEqual(req.calls, gmail_api.RETRIES + 1)

    def test_unauthorized_client_explains_delegation(self):
        req = Request(RefreshError("unauthorized_client: Client is unauthorized to retrieve "
                                   "access tokens using this method", {}))
        with self.assertRaisesRegex(gmail_api.GmailAuthError,
                                    r"meshak@thaswin.com \[gmail_api\]: unauthorized_client\. "
                                    r"Domain-wide delegation"):
            gmail_api.execute(WS, req, sleep=self.sleep)
        self.assertEqual(req.calls, 1)

    def test_403_permission_is_an_auth_error_but_403_rate_limit_retries(self):
        with self.assertRaises(gmail_api.GmailAuthError):
            gmail_api.execute(WS, Request(http_error(403, "insufficientPermissions")),
                              sleep=self.sleep)
        req = Request(http_error(403, "userRateLimitExceeded"), {"ok": 1})
        self.assertEqual(gmail_api.execute(WS, req, sleep=self.sleep), {"ok": 1})


class SendTest(unittest.TestCase):
    def setUp(self):
        self.service = MagicMock()
        patcher = patch.object(gmail_api, "client", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)
        header = patch.object(gmail_api, "_header", return_value="<real@mail.gmail.com>")
        self.header = header.start()
        self.addCleanup(header.stop)
        self.sleep = Mock()

    def sent_body(self):
        return self.service.users().messages().send.call_args.kwargs["body"]

    def test_send_is_base64url_raw_from_the_impersonated_mailbox(self):
        self.service.users().messages().send.return_value = Request({"id": "1", "threadId": "t"})
        msg = gmail_api.build_message(WS, "studio@example.com", "Hi", "Body", name="Meshak",
                                      message_id="<a@thaswin.com>")
        self.assertEqual(gmail_api.send(WS, msg),
                         {"id": "1", "threadId": "t", "messageId": "<real@mail.gmail.com>"})
        self.header.assert_called_once_with(WS, "1", "Message-ID")
        body = self.sent_body()
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]),
                                          policy=policy.default)
        self.assertEqual(parsed["From"].addresses[0].addr_spec, WS)
        self.assertEqual(parsed["Message-ID"], "<a@thaswin.com>")
        self.assertNotIn("threadId", body)
        self.assertEqual(self.service.users().messages().send.call_args.kwargs["userId"], "me")

    def test_reply_carries_thread_id_and_reply_headers(self):
        self.service.users().messages().send.return_value = Request({"id": "2", "threadId": "t"})
        msg = gmail_api.build_message(WS, "x@thaswin.com", "Re: Hi", "B",
                                      in_reply_to="<a@thaswin.com>")
        gmail_api.send(WS, msg, thread_id="t")
        body = self.sent_body()
        parsed = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
        self.assertEqual(body["threadId"], "t")
        self.assertEqual(parsed["In-Reply-To"], "<a@thaswin.com>")
        self.assertEqual(parsed["References"], "<a@thaswin.com>")

    def test_from_must_be_the_impersonated_mailbox(self):
        msg = gmail_api.build_message("other@thaswin.com", "x@example.com", "S", "B")
        with self.assertRaises(ValueError):
            gmail_api.send(WS, msg)

    def test_5xx_checks_sent_before_resending(self):
        self.service.users().messages().send.return_value = Request(http_error(502))
        msg = gmail_api.build_message(WS, "x@example.com", "S", "B", message_id="<m@thaswin.com>")
        with patch.object(gmail_api, "find_sent",
                          return_value={"id": "9", "threadId": "t"}) as find:
            self.assertEqual(gmail_api.send(WS, msg, sleep=self.sleep),
                             {"id": "9", "threadId": "t", "messageId": "<real@mail.gmail.com>"})
        self.assertEqual(find.call_args.args[:2], (WS, "x@example.com"))
        self.assertEqual(find.call_args.kwargs["subject"], "S")

    def test_5xx_until_the_limit_is_uncertain_not_resent_blindly(self):
        req = Request(*[http_error(500)] * (gmail_api.RETRIES + 1))
        self.service.users().messages().send.return_value = req
        msg = gmail_api.build_message(WS, "x@example.com", "S", "B")
        with patch.object(gmail_api, "find_sent", return_value=None) as find, \
                self.assertRaises(gmail_api.GmailUncertain):
            gmail_api.send(WS, msg, sleep=self.sleep)
        self.assertEqual(find.call_count, req.calls)

    def test_429_retries_without_searching_and_ends_not_sent(self):
        self.service.users().messages().send.return_value = Request(
            *[http_error(429)] * (gmail_api.RETRIES + 1))
        msg = gmail_api.build_message(WS, "x@example.com", "S", "B")
        with patch.object(gmail_api, "find_sent") as find, \
                self.assertRaises(gmail_api.GmailNotSent):
            gmail_api.send(WS, msg, sleep=self.sleep)
        find.assert_not_called()

    def test_a_failed_message_id_read_back_does_not_fail_the_send(self):
        self.header.side_effect = RuntimeError("down")
        self.service.users().messages().send.return_value = Request({"id": "1", "threadId": "t"})
        with patch("builtins.print"):
            got = gmail_api.send(WS, gmail_api.build_message(WS, "x@example.com", "S", "B"))
        self.assertIsNone(got["messageId"])

    def test_400_is_a_message_problem(self):
        self.service.users().messages().send.return_value = Request(http_error(400))
        with self.assertRaises(ValueError):
            gmail_api.send(WS, gmail_api.build_message(WS, "bad", "S", "B"), sleep=self.sleep)


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.service = MagicMock()
        patcher = patch.object(gmail_api, "client", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_get_and_mark_read(self):
        msgs = self.service.users().messages()
        msgs.list.side_effect = [Request({"messages": [{"id": "a", "threadId": "t"}],
                                          "nextPageToken": "p"}),
                                 Request({"messages": [{"id": "b", "threadId": "u"}]})]
        raw = base64.urlsafe_b64encode(b"Subject: x\r\n\r\nhi").decode().rstrip("=")
        msgs.get.return_value = Request({"raw": raw}, {"raw": raw})
        msgs.modify.return_value = Request({})
        got = gmail_api.fetch_unread(WS)
        self.assertEqual([(i, t) for i, t, _ in got], [("a", "t"), ("b", "u")])
        self.assertEqual(got[0][2], b"Subject: x\r\n\r\nhi")
        self.assertEqual(msgs.list.call_args_list[0].kwargs["q"], "is:unread in:inbox")
        self.assertEqual(msgs.get.call_args.kwargs["format"], "raw")
        msgs.modify.assert_not_called()
        gmail_api.mark_read(WS, "a")
        self.assertEqual(msgs.modify.call_args.kwargs["body"], {"removeLabelIds": ["UNREAD"]})

    def test_find_message_id_searches_rfc822msgid(self):
        self.service.users().messages().list.return_value = Request({"messages": []})
        self.assertIsNone(gmail_api.find_message_id(WS, "<abc@thaswin.com>"))
        self.assertEqual(self.service.users().messages().list.call_args.kwargs["q"],
                         "rfc822msgid:abc@thaswin.com")


class MailerBackendTest(unittest.TestCase):
    """The existing send path picks the backend per mailbox; no silent fallback."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        stack = ExitStack()
        self.addCleanup(stack.close)
        enter = stack.enter_context
        self.key = os.path.join(self.temp.name, "key.json")
        open(self.key, "w").close()
        enter(patch.dict(os.environ, {"GOOGLE_SA_KEY_FILE": self.key}, clear=True))
        self.db = os.path.join(self.temp.name, "test.sqlite")
        enter(patch.object(mailer.pipeline, "TURSO_URL", ""))
        enter(patch.object(mailer.pipeline, "DB_PATH", self.db))
        enter(patch.object(mailer.pipeline, "_ensured", False))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE newly_added (appid INTEGER PRIMARY KEY, success INTEGER, "
                         "fetched_at TEXT)")
        mailer.pipeline.init_tracker()
        self.smtp = enter(patch.object(mailer, "_send"))
        self.gsend = enter(patch.object(gmail_api, "send", return_value={
            "id": "1", "threadId": "t", "messageId": "<real@mail.gmail.com>"}))
        enter(patch.object(mailer, "_delete_media"))
        enter(patch.object(mailer.time, "sleep"))
        self.output = enter(patch("builtins.print"))
        self.failover = enter(patch.object(gmail_api, "failover_enabled", return_value=False))

    def seed(self, appid):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO scrape_tracker (appid,emails,scrape_status,Mail_status) "
                         "VALUES (?,?,'seeded','Scheduled')", (appid, "studio@example.com"))
            conn.commit()
        return "studio@example.com"

    def state(self, appid):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT Mail_status FROM scrape_tracker WHERE appid=?",
                                (appid,)).fetchone()[0]

    def deliver(self, appid, account):
        to = self.seed(appid)
        return mailer._deliver(appid, to, "Subject", "Body", to, account, float("inf"))

    def printed(self):
        return " ".join(" ".join(map(str, c.args)) for c in self.output.call_args_list)

    def test_workspace_inbox_sends_through_gmail_api_by_default(self):
        acct = mailer.Account(WS, "")
        self.assertEqual(acct.backend, "gmail_api")
        self.assertTrue(acct.ready)
        self.assertEqual(self.deliver(1, acct), "sent")
        self.assertEqual(self.state(1), "Sent")
        self.smtp.assert_not_called()
        msg = self.gsend.call_args.args[1]
        self.assertEqual(msg["From"].addresses[0].addr_spec, WS)
        self.assertTrue(msg["Message-ID"].startswith("<") and "sre.1" in msg["Message-ID"])
        # Gmail replaced the Message-ID: replies are matched on the one it used.
        self.assertEqual(mailer.pipeline.reply_evidence()[0][2], "<real@mail.gmail.com>")

    def test_other_mailboxes_keep_the_app_password_path(self):
        for box in ("meshak@zacgamedev.com", "meshakzac@gmail.com"):
            acct = mailer.Account(box, "secret")
            self.assertEqual(acct.backend, "app_password")
        acct = mailer.Account("meshak@zacgamedev.com", "secret")
        self.assertEqual(self.deliver(2, acct), "sent")
        self.gsend.assert_not_called()
        self.assertEqual(self.smtp.call_args.args[:2], ("meshak@zacgamedev.com", "secret"))

    def test_workspace_inbox_switched_to_app_password_uses_smtp(self):
        acct = mailer.Account(WS, "secret", backend="app_password")
        self.assertEqual(self.deliver(3, acct), "sent")
        self.gsend.assert_not_called()
        self.assertEqual(self.smtp.call_args.args[:2], (WS, "secret"))

    def test_auth_failure_halts_without_falling_back(self):
        self.gsend.side_effect = gmail_api.GmailAuthError(f"{WS} [gmail_api]: unauthorized_client")
        acct = mailer.Account(WS, "secret")
        self.assertEqual(self.deliver(4, acct), "halted")
        self.assertEqual(self.state(4), "Scheduled")
        self.smtp.assert_not_called()
        self.assertIn(f"{WS} [gmail_api]", self.printed())

    def test_failover_only_when_enabled_and_once(self):
        self.failover.return_value = True
        self.gsend.side_effect = gmail_api.GmailAuthError("unauthorized_client")
        acct = mailer.Account(WS, "secret")
        self.assertEqual(self.deliver(5, acct), "sent")
        self.assertEqual(self.smtp.call_count, 1)
        self.assertEqual(acct.backend, "app_password")
        self.assertIn("FAILOVER", self.printed())

    def test_failover_needs_the_other_backend_configured(self):
        self.failover.return_value = True
        self.gsend.side_effect = gmail_api.GmailAuthError("unauthorized_client")
        self.assertEqual(self.deliver(6, mailer.Account(WS, "")), "halted")
        self.smtp.assert_not_called()

    def test_uncertain_gmail_send_stays_sending(self):
        self.gsend.side_effect = gmail_api.GmailUncertain("no answer")
        self.assertEqual(self.deliver(7, mailer.Account(WS, "")), "uncertain")
        self.assertEqual(self.state(7), "Sending")

    def test_rate_limited_gmail_send_is_retried_later(self):
        self.gsend.side_effect = [gmail_api.GmailNotSent("rate limited"),
                                  {"id": "1", "threadId": "t", "messageId": None}]
        self.assertEqual(self.deliver(8, mailer.Account(WS, "")), "sent")
        self.assertEqual(self.gsend.call_count, 2)

    def test_gmail_inbox_without_key_is_not_ready(self):
        os.environ["GOOGLE_SA_KEY_FILE"] = ""
        acct = mailer.Account(WS, "")
        self.assertFalse(acct.ready)
        self.assertEqual(mailer._account_problem(acct), "gmail_api: GOOGLE_SA_KEY_FILE not set")

    def test_reconcile_finds_a_stuck_gmail_send_by_recipient_and_time(self):
        self.seed(10)
        mailer.pipeline.claim_mail(10, token="tok", message_id="<ours@thaswin.com>",
                                   expected_email="studio@example.com", sender=WS)
        acct = mailer.Account(WS, "")
        with patch.object(gmail_api, "find_message_id", return_value=None), \
                patch.object(gmail_api, "find_sent", return_value={"id": "s"}) as sent:
            remaining = {10}
            mailer._reconcile_mailbox(acct, [10], remaining)
        self.assertEqual(remaining, set())
        self.assertEqual(self.state(10), "Sent")
        self.assertEqual(sent.call_args.args[1], "studio@example.com")

    def test_reply_scan_reads_the_thread(self):
        acct = mailer.Account(WS, "")
        headers = (b"From: studio@example.com\r\nDate: Tue, 17 Nov 2026 10:00:00 +0000\r\n"
                   b"In-Reply-To: <m@thaswin.com>\r\n")
        with patch.object(gmail_api, "find_message_id", return_value={"id": "1", "threadId": "t"}), \
                patch.object(gmail_api, "thread_headers", return_value=[b"From: x\r\n", headers]) as th, \
                patch.object(mailer.pipeline, "set_mail_status") as status:
            n = mailer._review_mailbox(acct, [(9, "studio@example.com", "<m@thaswin.com>",
                                               "2026-11-16 10:00:00")])
        self.assertEqual(n, 1)
        th.assert_called_once_with(WS, "t")
        status.assert_called_once_with(9, "Replied")


if __name__ == "__main__":
    unittest.main()
