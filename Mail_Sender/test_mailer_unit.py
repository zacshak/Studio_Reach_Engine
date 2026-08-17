import os
import unittest
from unittest.mock import Mock, patch

import mailer


class MailerTest(unittest.TestCase):
    def test_invalid_recipient_does_not_consume_send_limit(self):
        verifier = Mock()
        verifier.verify.return_value = {"result": "valid", "safe_to_send": True}
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "not an email"), (2, "studio@example.com")]),
            patch.object(mailer.pipeline, "sent_today", return_value=0),
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
                                     "studio@example.com", "subject", "body")
        mark_sent.assert_called_once_with(2)

    def test_verifier_rejection_is_quarantined_without_sending(self):
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
            patch.object(mailer.pipeline, "sent_today", return_value=0),
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

        quarantine.assert_called_once_with(1)
        send.assert_called_once_with("sender@example.com", "secret",
                                     "studio@example.com", "subject", "body")
        mark_sent.assert_called_once_with(2)

    def test_verifier_failure_aborts_before_claim_or_send(self):
        verifier = Mock()
        verifier.verify.side_effect = mailer.QEVError("service unavailable")
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "studio@example.com")]),
            patch.object(mailer.pipeline, "sent_today", return_value=0),
            patch.object(mailer.pipeline, "claim_mail") as claim,
            patch.object(mailer.QuickEmailVerification, "from_env",
                         return_value=verifier),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
        ):
            with self.assertRaisesRegex(mailer.QEVError, "service unavailable"):
                mailer.main(limit=1)

        claim.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
