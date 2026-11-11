import os
import unittest
from unittest.mock import ANY, Mock, patch, mock_open

import mailer
import SRE


class MailerTest(unittest.TestCase):
    def test_missing_selected_template_never_falls_back_to_another_variant(self):
        with patch.object(mailer, "_CLOUD", False), \
                patch.object(mailer, "_folder_for", return_value="folder"), \
                patch.object(mailer.pipeline, "get_mail_template", return_value=2), \
                patch.object(mailer.os.path, "exists", return_value=False), \
                patch.object(mailer.glob, "glob", return_value=["folder/mail_1_1.txt"]) as glob, \
                patch("builtins.open", mock_open(read_data="Subject: Wrong\n\nUnapproved")) as opened:
            self.assertEqual(mailer._load_mail(1), (None, None, None))
        glob.assert_not_called()
        opened.assert_not_called()

    def test_launcher_runs_mailer_unbuffered_and_preserves_arguments_and_exit_code(self):
        with patch.object(SRE.subprocess, "call", return_value=7) as launch:
            self.assertEqual(SRE.main(["--send-mails", "--dry-run", "--limit", "3"]), 7)
        launch.assert_called_once_with([
            SRE.sys.executable, "-u", os.path.join(SRE.HERE, "Mail_Sender", "mailer.py"),
            "--dry-run", "--limit", "3",
        ])

    def test_invalid_recipient_does_not_consume_send_limit(self):
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "sender_accounts", return_value=[]),
            patch.object(mailer.pipeline, "sent_last_24h", return_value={}),
            patch.object(mailer.pipeline, "set_sender_status"),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "not an email"), (2, "studio@example.com")]),
            patch.object(mailer.pipeline, "quarantine_unusable") as quarantine,
            patch.object(mailer.pipeline, "claim_mail", return_value=True),
            patch.object(mailer.pipeline, "mark_sent") as mark_sent,
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

    def test_no_automatic_daily_send_cap(self):
        with (
            patch.dict(os.environ, {"GMAIL_USER": "sender@example.com",
                                     "GMAIL_APP_PASSWORD": "secret"}),
            patch.object(mailer.pipeline, "mail_status_appids", return_value=[]),
            patch.object(mailer.pipeline, "sender_accounts", return_value=[]),
            patch.object(mailer.pipeline, "sent_last_24h", return_value={}),
            patch.object(mailer.pipeline, "set_sender_status"),
            patch.object(mailer.pipeline, "mail_status_emails",
                         return_value=[(1, "one@example.com"),
                                       (2, "two@example.com")]),
            patch.object(mailer.pipeline, "claim_mail", return_value=True),
            patch.object(mailer.pipeline, "mark_sent"),
            patch.object(mailer, "_load_mail",
                         return_value=("subject", "body", "draft.txt")),
            patch.object(mailer, "_send") as send,
            patch.object(mailer, "_delete_media"),
            patch.object(mailer.time, "sleep"),
        ):
            mailer.main()
        self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
