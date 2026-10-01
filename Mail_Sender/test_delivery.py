"""Failure-injection tests. All state is SQLite in a temp directory, all mail is mocked."""
import os
import smtplib
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from unittest.mock import Mock, patch

import mailer

REAL_SEND = mailer._send
REAL_LOAD = mailer._load_mail


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        enter = self.stack.enter_context
        enter(patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}, clear=True))
        self.db = os.path.join(self.temp.name, "test.sqlite")
        enter(patch.object(mailer.pipeline, "TURSO_URL", ""))
        enter(patch.object(mailer.pipeline, "DB_PATH", self.db))
        enter(patch.object(mailer.pipeline, "_ensured", False))
        self.sql("CREATE TABLE newly_added (appid INTEGER PRIMARY KEY, success INTEGER, fetched_at TEXT)")
        mailer.pipeline.init_tracker()
        self.send = enter(patch.object(mailer, "_send"))
        self.cleanup = enter(patch.object(mailer, "_delete_media"))
        self.drafts = enter(patch.object(mailer, "_load_mail", return_value=("Subject", "Body", "draft.txt")))
        self.verifier = Mock()
        self.verifier.verify.return_value = {"result": "valid"}
        enter(patch.object(mailer.QuickEmailVerification, "from_env", return_value=self.verifier))
        self.sleep = enter(patch.object(mailer.time, "sleep"))
        enter(patch.object(mailer.time, "monotonic", return_value=1000))
        enter(patch.object(mailer.random, "randint", return_value=120))
        self.output = enter(patch("builtins.print"))
        enter(patch.object(mailer.ssl, "create_default_context", return_value=Mock()))

    def sql(self, query, args=()):
        with closing(sqlite3.connect(self.db)) as conn:
            result = conn.execute(query, args).fetchall()
            conn.commit()
            return result

    def seed(self, appid, email=None):
        email = email or f"studio{appid}@example.com"
        self.sql("INSERT INTO scrape_tracker (appid,emails,scrape_status,Mail_status) "
                 "VALUES (?,?,'seeded','Scheduled')", (appid, email))
        return email

    def state(self, appid):
        return self.sql("SELECT Mail_status FROM scrape_tracker WHERE appid=?", (appid,))[0][0]

    def deliver(self, appid, email):
        return mailer._deliver(appid, email, "Subject", "Body", email,
                               "sender@example.com", "secret", float("inf"))

    def test_temporary_smtp_codes_retry_known_unsent_and_reuse_message_id(self):
        for code in (421, 450, 451, 452):
            with self.subTest(code=code):
                email = self.seed(code)
                self.send.reset_mock()
                self.send.side_effect = [smtplib.SMTPDataError(code, b"temporary"), None]
                self.assertEqual(self.deliver(code, email), "sent")
                self.assertEqual(self.state(code), "Sent")
                self.assertEqual(self.send.call_count, 2)
                ids = [c.kwargs["message_id"] for c in self.send.call_args_list]
                self.assertEqual(ids[0], ids[1])
                self.assertIsNone(mailer.pipeline.get_send_attempt(code))

    def test_exhausted_temporary_rejection_defers_and_other_rows_continue(self):
        self.seed(1)
        self.seed(2)
        self.send.side_effect = [smtplib.SMTPDataError(451, b"temporary")] * 3 + [None]
        self.assertEqual(mailer.main(), 0)
        self.assertEqual((self.state(1), self.state(2)), ("Scheduled", "Sent"))
        self.assertEqual(self.send.call_count, 4)
        self.assertIsNone(mailer.pipeline.get_send_attempt(1))

    def test_permanent_data_rejection_returns_to_drafted_not_invalid(self):
        self.seed(1)
        self.seed(2)
        self.send.side_effect = [smtplib.SMTPDataError(554, b"policy rejection"), None]
        self.assertEqual(mailer.main(), 0)
        self.assertEqual((self.state(1), self.state(2)), ("Drafted", "Sent"))
        self.assertEqual(self.send.call_count, 2)

    def test_temporary_recipient_rejection_is_retryable(self):
        email = self.seed(1)
        self.send.side_effect = [smtplib.SMTPRecipientsRefused({email: (450, b"try later")}), None]
        self.assertEqual(self.deliver(1, email), "sent")

    def test_authentication_error_stops_without_stranding_or_trying_other_rows(self):
        self.seed(1)
        self.seed(2)
        self.send.side_effect = smtplib.SMTPAuthenticationError(535, b"bad password")
        self.assertEqual(mailer.main(), 1)
        self.assertEqual((self.state(1), self.state(2)), ("Scheduled", "Scheduled"))
        self.send.assert_called_once()

    def test_setup_disconnect_is_provably_unsent_and_retryable(self):
        email = self.seed(1)
        failure = mailer.MailNotSubmittedError("setup timeout")
        failure.__cause__ = TimeoutError("connection timed out")
        self.send.side_effect = [failure, None]
        self.assertEqual(self.deliver(1, email), "sent")

    def test_data_disconnect_stays_sending_and_does_not_block_other_rows(self):
        self.seed(1)
        self.seed(2)
        self.send.side_effect = [smtplib.SMTPServerDisconnected("lost reply after DATA"), None]
        self.assertEqual(mailer.main(), 1)
        self.assertEqual((self.state(1), self.state(2)), ("Sending", "Sent"))
        self.assertTrue(mailer.pipeline.get_send_attempt(1)[1].startswith("<"))
        self.assertEqual(self.send.call_count, 2)

    def test_failed_quit_after_acceptance_does_not_change_sent_outcome(self):
        self.seed(1)
        smtp = Mock()
        smtp.send_message.return_value = {}
        smtp.quit.side_effect = smtplib.SMTPServerDisconnected("lost QUIT reply")
        smtp.close.side_effect = OSError("cleanup failure")
        self.send.side_effect = REAL_SEND
        with patch.object(mailer.smtplib, "SMTP", return_value=smtp):
            self.assertEqual(mailer.main(), 0)
        self.assertEqual(self.state(1), "Sent", self.output.call_args_list)
        smtp.send_message.assert_called_once()
        message = smtp.send_message.call_args.args[0]
        self.assertTrue(str(message["Message-ID"]).endswith("@example.com>"))
        self.assertTrue(message["Date"])

    def test_setup_error_never_submits_data(self):
        smtp = Mock()
        smtp.login.side_effect = TimeoutError("login response lost")
        with patch.object(mailer.smtplib, "SMTP", return_value=smtp):
            with self.assertRaises(mailer.MailNotSubmittedError) as raised:
                REAL_SEND("sender@example.com", "secret", "studio@example.com", "Subject", "Body")
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        smtp.send_message.assert_not_called()

    def test_header_injection_never_opens_smtp(self):
        with patch.object(mailer.smtplib, "SMTP") as smtp:
            with self.assertRaises(mailer.MailNotSubmittedError):
                REAL_SEND("sender@example.com", "secret", "studio@example.com", "Hello\nBcc: other@example.com", "Body")
        smtp.assert_not_called()

    def test_completion_write_retry_does_not_resend_smtp(self):
        email = self.seed(1)
        complete = mailer.pipeline.mark_sent
        with patch.object(mailer.pipeline, "mark_sent", side_effect=[
                ValueError("connection closed"), None]) as saved, \
                patch.object(mailer.pipeline, "reconnect"):
            self.assertEqual(self.deliver(1, email), "sent")
        self.send.assert_called_once()
        self.assertEqual(saved.call_count, 2)
        complete(1)  # the second mocked completion above stands in for this actual operation

    def test_accepted_but_unrecorded_mail_recovers_from_gmail_without_resending(self):
        self.seed(1)
        with patch.object(mailer.pipeline, "mark_sent", side_effect=ValueError("connection closed")), \
                patch.object(mailer.pipeline, "reconnect"):
            self.assertEqual(mailer.main(), 1)
        self.assertEqual(self.state(1), "Sending")
        message_id = mailer.pipeline.get_send_attempt(1)[1]
        self.cleanup.assert_not_called()
        imap = self.sent_mailbox(b"7")
        with patch.object(mailer.imaplib, "IMAP4_SSL", return_value=imap):
            self.assertEqual(mailer.main(), 0)  # only reconciliation work remains
        imap.search.assert_called_once_with(None, "HEADER", "Message-ID", f'"{message_id}"')
        self.assertEqual(self.state(1), "Sent")
        self.send.assert_called_once()

    def sent_mailbox(self, ids):
        imap = Mock()
        imap.list.return_value = ("OK", [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"'])
        imap.select.return_value = ("OK", [b"1"])
        imap.search.return_value = ("OK", [ids])
        return imap

    def test_absence_in_gmail_sent_never_automatically_resends(self):
        email = self.seed(1)
        mailer.pipeline.claim_mail(1, token="owner", message_id="<unknown@example.com>", expected_email=email)
        self.seed(2)
        with patch.object(mailer.imaplib, "IMAP4_SSL", return_value=self.sent_mailbox(b"")):
            self.assertEqual(mailer.main(), 1)
        self.assertEqual((self.state(1), self.state(2)), ("Sending", "Sent"))
        self.send.assert_called_once()

    def test_claim_response_loss_never_sends_without_confirmed_ownership(self):
        email = self.seed(1)
        claim = mailer.pipeline.claim_mail

        def claim_then_disconnect(*args, **kwargs):
            claim(*args, **kwargs)
            raise ValueError("connection closed")

        with patch.object(mailer.pipeline, "claim_mail", side_effect=claim_then_disconnect):
            self.assertEqual(mailer.main(), 1)
        self.assertEqual(self.state(1), "Sending")
        self.send.assert_not_called()

    def test_stale_owner_and_changed_recipient_cannot_modify_or_send_another_claim(self):
        email = self.seed(1)
        self.assertFalse(mailer.pipeline.claim_mail(1, expected_email="changed@example.com"))
        self.assertTrue(mailer.pipeline.claim_mail(1, token="old", expected_email=email))
        mailer.pipeline.reset_sending(1, "Scheduled", token="wrong")
        self.assertEqual(self.state(1), "Sending")
        mailer.pipeline.reset_sending(1, "Scheduled", token="old")
        self.assertTrue(mailer.pipeline.claim_mail(1, token="new", expected_email=email))
        mailer.pipeline.reset_sending(1, "Scheduled", token="old")
        with self.assertRaises(RuntimeError):
            mailer.pipeline.mark_sent(1, token="old")
        self.assertEqual(self.state(1), "Sending")
        self.assertEqual(mailer.pipeline.get_send_attempt(1)[0], "new")

    def test_same_address_is_verified_only_once_even_when_cache_write_fails(self):
        self.seed(1, "shared@example.com")
        self.seed(2, "shared@example.com")
        with patch.object(mailer.pipeline, "cache_email_verification", side_effect=ValueError("connection closed")), \
                patch.object(mailer.pipeline, "reconnect"):
            self.assertEqual(mailer.main(), 0)
        self.verifier.verify.assert_called_once()
        self.assertEqual(self.send.call_count, 2)

    def test_unknown_verification_is_not_invalid_and_is_not_repeated_in_the_batch(self):
        self.seed(1, "shared@example.com")
        self.seed(2, "shared@example.com")
        self.verifier.verify.return_value = {"result": "unknown"}
        self.assertEqual(mailer.main(), 0)
        self.assertEqual((self.state(1), self.state(2)), ("Scheduled", "Scheduled"))
        self.verifier.verify.assert_called_once()
        self.send.assert_not_called()

    def test_quota_exhaustion_never_bypasses_verification(self):
        self.seed(1)
        self.seed(2)
        self.verifier.verify.side_effect = mailer.QEVError("quota", status_code=402)
        self.assertEqual(mailer.main(), 0)
        self.assertEqual((self.state(1), self.state(2)), ("Scheduled", "Scheduled"))
        self.send.assert_not_called()

    def test_wrong_recipient_and_malformed_verification_fail_closed(self):
        self.seed(1)
        for result in ({"result": "valid", "email": "other@example.com"}, [], {"result": "unexpected"}):
            with self.subTest(result=result):
                self.verifier.verify.return_value = result
                self.assertEqual(mailer.main(), 1)
                self.assertEqual(self.state(1), "Scheduled")
                self.send.assert_not_called()

    def test_missing_draft_and_r2_failure_do_not_affect_other_rows(self):
        for appid in (1, 2, 3):
            self.seed(appid)
        self.drafts.side_effect = [(None, None, None), mailer.media_store.MediaStoreError("R2 down"),
                                   ("Subject", "Body", "draft.txt")]
        self.assertEqual(mailer.main(), 0)
        self.assertEqual([self.state(i) for i in (1, 2, 3)], ["Drafted", "Scheduled", "Sent"])

    def test_cleanup_failure_cannot_undo_sent_or_stop_other_rows(self):
        self.seed(1)
        self.seed(2)
        self.cleanup.side_effect = RuntimeError("R2 down")
        self.assertEqual(mailer.main(), 0)
        self.assertEqual((self.state(1), self.state(2)), ("Sent", "Sent"))

    def test_runner_deadline_stops_before_claim_and_preserves_remaining_rows(self):
        self.seed(1)
        self.seed(2)
        with patch.dict(os.environ, {"SEND_TIME_BUDGET_SECONDS": "121"}):
            self.assertEqual(mailer.main(), 0)
        self.assertEqual((self.state(1), self.state(2)), ("Sent", "Scheduled"))
        self.send.assert_called_once()

    def test_there_is_no_50_or_100_daily_send_cap(self):
        for appid in range(1, 102):
            self.seed(appid)
        self.assertEqual(mailer.main(), 0)
        self.assertEqual(self.send.call_count, 101)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM scrape_tracker WHERE Mail_status='Sent'"), [(101,)])

    def test_interrupt_during_submission_keeps_sending_and_stops_other_sends(self):
        self.seed(1)
        self.seed(2)
        self.send.side_effect = KeyboardInterrupt()
        self.assertEqual(mailer.main(), 1)
        self.assertEqual((self.state(1), self.state(2)), ("Sending", "Scheduled"))
        self.send.assert_called_once()

    def test_interrupt_during_retry_delay_leaves_known_unsent_row_scheduled(self):
        self.seed(1)
        self.seed(2)
        self.send.side_effect = smtplib.SMTPDataError(451, b"temporary")
        self.sleep.side_effect = KeyboardInterrupt()
        self.assertEqual(mailer.main(), 1)
        self.assertEqual((self.state(1), self.state(2)), ("Scheduled", "Scheduled"))

    def test_failed_gmail_reconciliation_does_not_block_unrelated_mail(self):
        self.seed(1)
        mailer.pipeline.claim_mail(1, token="owner", message_id="<unknown@example.com>")
        self.seed(2)
        with patch.object(mailer.imaplib, "IMAP4_SSL", side_effect=TimeoutError("IMAP down")):
            self.assertEqual(mailer.main(), 1)
        self.assertEqual((self.state(1), self.state(2)), ("Sending", "Sent"))
        self.send.assert_called_once()

    def test_additive_metadata_migration_preserves_existing_rows_and_claims(self):
        email = self.seed(1)
        mailer.pipeline.claim_mail(1, token="owner", message_id="<mail@example.com>", expected_email=email)
        before = self.sql("SELECT * FROM scrape_tracker")
        mailer.pipeline.init_tracker()
        self.assertEqual(self.sql("SELECT * FROM scrape_tracker"), before)
        self.assertEqual(mailer.pipeline.get_send_attempt(1), ("owner", "<mail@example.com>"))
        mailer.pipeline.delete_lead(1)
        self.assertIsNone(mailer.pipeline.get_send_attempt(1))

    def test_bad_verification_cache_is_ignored_instead_of_authorizing_a_send(self):
        email = self.seed(1)
        self.sql("INSERT INTO email_verification_cache (email,result_json) VALUES (?,?)",
                 (email, '{"result":"valid","email":123}'))
        self.assertIsNone(mailer.pipeline.get_email_verification(email))
        self.assertFalse(mailer.pipeline.cache_email_verification(email, {"result": "valid", "email": 123}))

    def test_dry_run_never_verifies_claims_sends_or_deletes_media(self):
        self.seed(1)
        self.assertEqual(mailer.main(dry_run=True), 0)
        self.assertEqual(self.state(1), "Scheduled")
        self.assertIsNone(mailer.pipeline.get_send_attempt(1))
        self.verifier.verify.assert_not_called()
        self.send.assert_not_called()
        self.cleanup.assert_not_called()

    def test_recipient_change_during_verification_is_not_quarantined_or_sent(self):
        original = self.seed(1)

        def verify_and_change(email):
            self.sql("UPDATE scrape_tracker SET emails=? WHERE appid=1", ("new@example.com",))
            return {"result": "invalid", "email": original}

        self.verifier.verify.side_effect = verify_and_change
        self.assertEqual(mailer.main(), 0)
        self.assertEqual(self.state(1), "Scheduled")
        self.send.assert_not_called()
        self.cleanup.assert_not_called()

    def test_missing_email_returns_to_pending_without_deleting_review_media(self):
        self.seed(1)
        self.sql("UPDATE scrape_tracker SET emails=NULL WHERE appid=1")
        self.assertEqual(mailer.main(), 0)
        self.assertEqual(self.state(1), "Pending")
        self.cleanup.assert_not_called()

    def test_scrapers_and_repair_cannot_rewind_an_inflight_send(self):
        self.seed(1)
        mailer.pipeline.claim_mail(1, token="owner", message_id="<pending@example.com>")
        self.sql("UPDATE scrape_tracker SET emails='invalid contact' WHERE appid=1")
        with self.assertRaises(ValueError):
            mailer.pipeline.write_result(1, scrape_status="scraped", emails="other@example.com")
        self.assertEqual(mailer.pipeline.repair_invalid_rows(), [])
        mailer.pipeline.quarantine_unusable(1)
        self.assertEqual(self.state(1), "Sending")
        self.assertEqual(mailer.pipeline.get_send_attempt(1)[0], "owner")

    def test_malformed_r2_records_cannot_be_sent(self):
        for index, manifest in ((["bad"], {}), ({"1": "folder"}, ["bad"]),
                                ({"1": "folder"}, {"mail": 123})):
            with self.subTest(index=index, manifest=manifest), \
                    patch.object(mailer, "_CLOUD", True), \
                    patch.object(mailer.media_store, "fetch_index", return_value=index), \
                    patch.object(mailer.media_store, "fetch_manifest", return_value=manifest):
                with self.assertRaises(mailer.media_store.MediaStoreError):
                    REAL_LOAD(1)
        self.send.assert_not_called()

    def test_non_text_email_values_are_invalid_but_null_is_missing(self):
        for value in (123, b"studio@example.com", {"email": "studio@example.com"}):
            with self.subTest(value=value):
                self.assertEqual(mailer.pipeline.normalize_email(value), "")
                self.assertEqual(mailer.pipeline.email_state(value), "invalid")
        self.assertEqual(mailer.pipeline.email_state(None), "missing")


if __name__ == "__main__":
    unittest.main()
