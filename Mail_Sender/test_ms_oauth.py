import base64
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from unittest.mock import Mock, patch

import mailer
import ms_oauth


def id_token(user):
    body = base64.urlsafe_b64encode(json.dumps({"preferred_username": user}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class MSOAuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        enter = self.stack.enter_context
        enter(patch.dict(os.environ, {"MS_TOKEN_KEY": "k" * 40, "SENDER_ACCOUNTS": "{}"}, clear=True))
        self.db = os.path.join(self.temp.name, "t.sqlite")
        enter(patch.object(mailer.pipeline, "TURSO_URL", ""))
        enter(patch.object(mailer.pipeline, "DB_PATH", self.db))
        enter(patch.object(mailer.pipeline, "_ensured", False))
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("CREATE TABLE newly_added (appid INTEGER PRIMARY KEY, success INTEGER, fetched_at TEXT)")
            c.commit()
        mailer.pipeline.init_tracker()
        ms_oauth._cache.clear()
        enter(patch("builtins.print"))

    def sql(self, q, a=()):
        with closing(sqlite3.connect(self.db)) as c:
            r = c.execute(q, a).fetchall()
            c.commit()
            return r

    def test_connect_stores_encrypted_token_and_refresh_rotates_it(self):
        replies = [{"device_code": "dc", "user_code": "ABC", "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 900, "interval": 1},
                   {"error": "authorization_pending"},
                   {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600,
                    "id_token": id_token("Me@X.test")}]
        with patch.object(ms_oauth, "_post", side_effect=replies):
            self.assertTrue(ms_oauth.connect("me@x.test", log=lambda *_: None, sleep=lambda _: None))
        stored = self.sql("SELECT token_enc FROM oauth_tokens")[0][0]
        self.assertNotIn("rt1", stored)  # encrypted at rest
        ms_oauth._cache.clear()
        with patch.object(ms_oauth, "_post", return_value={"access_token": "at2", "refresh_token": "rt2",
                                                           "expires_in": 3600}) as post:
            self.assertEqual(ms_oauth.access_token("me@x.test"), "at2")
            self.assertEqual(post.call_args.args[1]["refresh_token"], "rt1")
            self.assertEqual(ms_oauth.access_token("me@x.test"), "at2")  # cached
            post.assert_called_once()
        self.assertEqual(ms_oauth._fernet().decrypt(
            self.sql("SELECT token_enc FROM oauth_tokens")[0][0].encode()).decode(), "rt2")
        self.assertEqual(ms_oauth.xoauth2("me@x.test"), "user=me@x.test\x01auth=Bearer at2\x01\x01")

    def test_connect_refuses_a_different_signed_in_account(self):
        replies = [{"device_code": "dc", "user_code": "ABC", "verification_uri": "u", "interval": 1},
                   {"access_token": "a", "refresh_token": "r", "id_token": id_token("other@x.test")}]
        with patch.object(ms_oauth, "_post", side_effect=replies), \
                self.assertRaises(ms_oauth.MSAuthError):
            ms_oauth.connect("me@x.test", log=lambda *_: None, sleep=lambda _: None)
        self.assertEqual(self.sql("SELECT count(*) FROM oauth_tokens")[0][0], 0)

    def test_browser_sign_in_redeems_the_pasted_address(self):
        url = ms_oauth.auth_url("me@x.test")
        self.assertIn("response_type=code", url)
        self.assertIn("login_hint=me%40x.test", url)
        pasted = "https://login.microsoftonline.com/common/oauth2/nativeclient?code=ABC123&session_state=x"
        with patch.object(ms_oauth, "_post", return_value={
                "access_token": "at", "refresh_token": "rt", "id_token": id_token("me@x.test")}) as post:
            self.assertTrue(ms_oauth.redeem("me@x.test", pasted, log=lambda *_: None))
        fields = post.call_args.args[1]
        self.assertEqual((fields["grant_type"], fields["code"], fields["redirect_uri"]),
                         ("authorization_code", "ABC123", ms_oauth.REDIRECT_URI))
        self.assertEqual(self.sql("SELECT count(*) FROM oauth_tokens")[0][0], 1)

    def test_pasted_error_or_missing_code_is_explained(self):
        with patch.object(ms_oauth, "_post") as post:
            with self.assertRaisesRegex(ms_oauth.MSAuthError, "access_denied"):
                ms_oauth.redeem("me@x.test", "https://x/nativeclient?error=access_denied&error_description=no")
            with self.assertRaisesRegex(ms_oauth.MSAuthError, "no .code="):
                ms_oauth.redeem("me@x.test", "https://x/nativeclient?state=1")
            post.assert_not_called()

    def test_missing_key_fails_before_showing_a_code(self):
        with patch.dict(os.environ, {"MS_TOKEN_KEY": ""}), patch.object(ms_oauth, "_post") as post, \
                self.assertRaises(ms_oauth.MSAuthError):
            ms_oauth.connect("me@x.test")
        post.assert_not_called()

    def test_connected_microsoft_account_sends_with_xoauth2(self):
        self.sql("INSERT INTO sender_accounts (email, provider, daily_cap) VALUES ('me@x.test','microsoft',10)")
        mailer.pipeline.set_oauth_token("me@x.test", ms_oauth._fernet().encrypt(b"rt").decode())
        self.sql("INSERT INTO scrape_tracker (appid,emails,scrape_status,Mail_status) "
                 "VALUES (1,'s@example.com','seeded','Scheduled')")
        smtp = Mock()
        smtp.send_message.return_value = {}
        with patch.object(mailer.smtplib, "SMTP", return_value=smtp) as connect, \
                patch.object(mailer.ssl, "create_default_context"), \
                patch.object(mailer, "_load_mail", return_value=("S", "B", "d")), \
                patch.object(mailer, "_delete_media"), \
                patch.object(ms_oauth, "access_token", return_value="tok"):
            self.assertEqual(mailer.main(), 0)
            mech, auth = smtp.auth.call_args.args[:2]
            self.assertEqual((mech, auth()), ("XOAUTH2", "user=me@x.test\x01auth=Bearer tok\x01\x01"))
        connect.assert_called_once_with("smtp.office365.com", 587, timeout=30)
        smtp.login.assert_not_called()
        self.assertEqual(self.sql("SELECT sender FROM mail_send_log"), [("me@x.test",)])


if __name__ == "__main__":
    unittest.main()
