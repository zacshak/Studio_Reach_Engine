import os
import unittest
from unittest.mock import patch

import mailer


class MailerTest(unittest.TestCase):
    def test_invalid_recipient_does_not_consume_send_limit(self):
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


if __name__ == "__main__":
    unittest.main()
