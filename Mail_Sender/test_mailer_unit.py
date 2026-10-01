import os
import unittest
from unittest.mock import ANY, Mock, patch

import mailer
import SRE


class MailerTest(unittest.TestCase):
    def test_launcher_runs_mailer_unbuffered_and_preserves_arguments_and_exit_code(self):
        with patch.object(SRE.subprocess, "call", return_value=7) as launch:
            self.assertEqual(SRE.main(["--send-mails", "--dry-run", "--limit", "3"]), 7)
        launch.assert_called_once_with([
            SRE.sys.executable, "-u", os.path.join(SRE.HERE, "Mail_Sender", "mailer.py"),
            "--dry-run", "--limit", "3",
        ])

    def test_cache_read_failure_reports_partial_batch_without_claiming_remaining_mail(self):
        verifier = Mock()
        cached = {"result": "valid"}
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "one@example.com"), (2, "two@example.com"),
                                       (3, "three@example.com")]),
            patch.object(mailer.pipeline, "get_email_verification",
                         side_effect=[cached, ValueError("connection closed")]),
            patch.object(mailer.pipeline, "claim_mail", return_value=True) as claim,
            patch.object(mailer.pipeline, "mark_sent") as mark_sent,
            patch.object(mailer.pipeline, "quarantine_verified_invalid") as quarantine,
            patch.object(mailer.QuickEmailVerification, "from_env", return_value=verifier),
            patch.object(mailer, "_load_mail", return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
            patch.object(mailer, "_delete_media"),
            patch("builtins.print") as output,
        ):
            self.assertEqual(mailer.main(), 1)
        claim.assert_called_once_with(1, token=ANY, message_id=ANY, expected_email="one@example.com")
        mark_sent.assert_called_once_with(1, token=ANY)
        send.assert_called_once()
        quarantine.assert_not_called()
        verifier.verify.assert_not_called()
        output.assert_any_call(
            "STOP: ValueError: connection closed. No further sends attempted.")

    def test_invalid_recipient_does_not_consume_send_limit(self):
        verifier = Mock()
        verifier.verify.return_value = {"result": "valid", "safe_to_send": True}
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "not an email"), (2, "studio@example.com")]),
            patch.object(mailer.pipeline, "get_email_verification", return_value=None),
            patch.object(mailer.pipeline, "cache_email_verification"),
            patch.object(mailer.pipeline, "quarantine_unusable") as quarantine,
            patch.object(mailer.pipeline, "claim_mail", return_value=True),
            patch.object(mailer.pipeline, "mark_sent") as mark_sent,
            patch.object(mailer.QuickEmailVerification, "from_env",
                         return_value=verifier),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
            patch.object(mailer, "_delete_media"),
        ):
            mailer.main(limit=1)

        quarantine.assert_called_once_with(1)
        send.assert_called_once_with("sender@example.com", "secret",
                                     "studio@example.com", "subject", "body", message_id=ANY)
        mark_sent.assert_called_once_with(2, token=ANY)

    def test_only_invalid_result_is_quarantined(self):
        verifier = Mock()
        verifier.verify.side_effect = [
            {"result": "invalid", "reason": "rejected_email", "safe_to_send": False},
            {"result": "valid", "reason": "accepted_email", "safe_to_send": False},
        ]
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "dead@example.com"),
                                       (2, "studio@example.com")]),
            patch.object(mailer.pipeline, "get_email_verification", return_value=None),
            patch.object(mailer.pipeline, "cache_email_verification"),
            patch.object(mailer.pipeline, "quarantine_verified_invalid") as quarantine,
            patch.object(mailer.pipeline, "claim_mail", return_value=True),
            patch.object(mailer.pipeline, "mark_sent") as mark_sent,
            patch.object(mailer.QuickEmailVerification, "from_env",
                         return_value=verifier),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
            patch.object(mailer, "_delete_media"),
        ):
            mailer.main(limit=1)

        quarantine.assert_called_once_with(1, expected_email="dead@example.com")
        send.assert_called_once_with("sender@example.com", "secret",
                                     "studio@example.com", "subject", "body", message_id=ANY)
        mark_sent.assert_called_once_with(2, token=ANY)

    def test_r2_cleanup_failure_does_not_stop_after_quarantine(self):
        verifier = Mock()
        verifier.verify.side_effect = [
            {"result": "invalid", "reason": "rejected_email", "safe_to_send": False},
            {"result": "valid", "reason": "accepted_email", "safe_to_send": True},
        ]
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "dead@example.com"),
                                       (2, "studio@example.com")]),
            patch.object(mailer.pipeline, "get_email_verification", return_value=None),
            patch.object(mailer.pipeline, "cache_email_verification"),
            patch.object(mailer.pipeline, "quarantine_verified_invalid") as quarantine,
            patch.object(mailer.pipeline, "claim_mail", return_value=True),
            patch.object(mailer.pipeline, "mark_sent") as mark_sent,
            patch.object(mailer.QuickEmailVerification, "from_env",
                         return_value=verifier),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
            patch.object(mailer, "_delete_media",
                         side_effect=[RuntimeError("R2 unavailable"), False]),
        ):
            mailer.main()

        quarantine.assert_called_once_with(1, expected_email="dead@example.com")
        send.assert_called_once_with("sender@example.com", "secret",
                                     "studio@example.com", "subject", "body", message_id=ANY)
        mark_sent.assert_called_once_with(2, token=ANY)

    def test_verifier_failure_aborts_before_claim_or_send(self):
        verifier = Mock()
        verifier.verify.side_effect = mailer.QEVError("service unavailable")
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "studio@example.com")]),
            patch.object(mailer.pipeline, "get_email_verification", return_value=None),
            patch.object(mailer.pipeline, "cache_email_verification"),
            patch.object(mailer.pipeline, "claim_mail") as claim,
            patch.object(mailer.QuickEmailVerification, "from_env",
                         return_value=verifier),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
        ):
            self.assertEqual(mailer.main(limit=1), 1)

        claim.assert_not_called()
        send.assert_not_called()

    def test_cached_verification_skips_qev(self):
        verifier = Mock()
        cached = {"result": "valid", "safe_to_send": True}
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "studio@example.com")]),
            patch.object(mailer.pipeline, "get_email_verification", return_value=cached),
            patch.object(mailer.pipeline, "claim_mail", return_value=True),
            patch.object(mailer.pipeline, "mark_sent") as mark_sent,
            patch.object(mailer.QuickEmailVerification, "from_env",
                         return_value=verifier),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
            patch.object(mailer, "_delete_media"),
        ):
            mailer.main(limit=1)

        verifier.verify.assert_not_called()
        send.assert_called_once()
        mark_sent.assert_called_once_with(1, token=ANY)

    def test_quota_error_stops_without_failing_or_claiming(self):
        verifier = Mock()
        verifier.verify.side_effect = mailer.QEVError("low credit", status_code=402)
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "studio@example.com")]),
            patch.object(mailer.pipeline, "get_email_verification", return_value=None),
            patch.object(mailer.pipeline, "claim_mail") as claim,
            patch.object(mailer.QuickEmailVerification, "from_env",
                         return_value=verifier),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
        ):
            mailer.main(limit=1)

        claim.assert_not_called()
        send.assert_not_called()

    def test_no_automatic_daily_send_cap(self):
        verifier = Mock()
        cached = {"result": "valid", "safe_to_send": True}
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "one@example.com"),
                                       (2, "two@example.com")]),
            patch.object(mailer.pipeline, "get_email_verification", return_value=cached),
            patch.object(mailer.pipeline, "claim_mail", return_value=True),
            patch.object(mailer.pipeline, "mark_sent"),
            patch.object(mailer.QuickEmailVerification, "from_env",
                         return_value=verifier),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
            patch.object(mailer, "_delete_media"),
            patch.object(mailer.time, "sleep"),
        ):
            mailer.main()

        verifier.verify.assert_not_called()
        self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
