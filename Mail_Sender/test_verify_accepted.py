import unittest
from unittest.mock import Mock, patch

import verify_accepted as va


class VerifyAcceptedTest(unittest.TestCase):
    def run_main(self, rows, results, *, media="ok", cached=None):
        verifier = Mock()
        verifier.verify.side_effect = lambda e: {"email": e, **results[e]}
        calls = {"reject": [], "finish": [], "returned": []}
        media_effect = media if callable(media) else (lambda appid, client: media)
        with patch.object(va.pipeline, "verifying_rows", return_value=rows), \
                patch.object(va.pipeline, "get_email_verification",
                             side_effect=lambda e: (cached or {}).get(e)), \
                patch.object(va.pipeline, "cache_email_verification"), \
                patch.object(va.pipeline, "reject_email",
                             side_effect=lambda a, **kw: calls["reject"].append((a, kw)) or "no_mail"), \
                patch.object(va.pipeline, "finish_verification",
                             side_effect=lambda a, **kw: calls["finish"].append(a) or True), \
                patch.object(va.pipeline, "return_unverified",
                             side_effect=lambda a, **kw: calls["returned"].append((a, kw["reason"])) or True), \
                patch.object(va.EmailVerifier, "from_env", return_value=verifier), \
                patch.object(va.media_store, "write_enabled", return_value=True), \
                patch.object(va.media_store, "_client", return_value=object()), \
                patch.object(va, "ensure_media", side_effect=media_effect):
            code = va.main()
        return code, calls, verifier

    def test_valid_is_accepted_unknown_is_not_and_invalid_goes_to_no_mail(self):
        rows = [(1, "good@x.test", "seeded"), (2, "maybe@x.test", "seeded"),
                (3, "dead@x.test", "scraped")]
        results = {"good@x.test": {"result": "valid"}, "maybe@x.test": {"result": "unknown"},
                   "dead@x.test": {"result": "invalid", "reason": "mailbox_does_not_exist"}}
        code, calls, _ = self.run_main(rows, results)
        self.assertEqual(code, 0)
        self.assertEqual(calls["finish"], [1])
        # Unknown: not accepted, back to Triaged with the email kept.
        self.assertEqual([a for a, _ in calls["returned"]], [2])
        self.assertIn("could not verify", calls["returned"][0][1])
        [(appid, kw)] = calls["reject"]
        self.assertEqual(appid, 3)
        # Accept-time: always back to "no email", even for a researched address.
        self.assertEqual((kw["from_statuses"], kw["researched_to_invalid"], kw["source"]),
                         (("Verifying",), False, "accept"))

    def test_cached_result_is_reused(self):
        code, calls, verifier = self.run_main(
            [(1, "good@x.test", "seeded")], {}, cached={"good@x.test": {"result": "valid"}})
        verifier.verify.assert_not_called()
        self.assertEqual(calls["finish"], [1])

    def test_media_failure_returns_game_with_email(self):
        def fail(appid, client):
            raise va.MediaError("Steam lists no screenshots for this game")
        code, calls, _ = self.run_main([(1, "good@x.test", "seeded")],
                                       {"good@x.test": {"result": "valid"}}, media=fail)
        self.assertEqual(calls["finish"], [])
        self.assertIn("no screenshots", calls["returned"][0][1])

    def test_verifier_crash_returns_game_instead_of_leaving_it_stuck(self):
        verifier_results = {"good@x.test": {"result": "weird"}}
        code, calls, _ = self.run_main([(1, "good@x.test", "seeded")], verifier_results)
        self.assertEqual(code, 1)
        self.assertEqual(calls["returned"][0][0], 1)

    def test_rebuilt_media_still_accepts(self):
        code, calls, _ = self.run_main([(1, "good@x.test", "seeded")],
                                       {"good@x.test": {"result": "valid"}}, media="rebuilt")
        self.assertEqual(calls["finish"], [1])


class DeadlineTest(unittest.TestCase):
    def test_hanging_check_is_cut_off_as_unknown_and_not_accepted(self):
        import time as _time
        verifier = Mock()
        verifier.verify.side_effect = lambda e: _time.sleep(5)
        calls = []
        with patch.object(va, "VERIFY_SECONDS", 1), \
                patch.object(va.pipeline, "verifying_rows", return_value=[(1, "slow@x.test", "seeded")]), \
                patch.object(va.pipeline, "get_email_verification", return_value=None), \
                patch.object(va.pipeline, "cache_email_verification") as cache, \
                patch.object(va.pipeline, "finish_verification") as finish, \
                patch.object(va.pipeline, "return_unverified",
                             side_effect=lambda a, **kw: calls.append((a, kw["reason"])) or True), \
                patch.object(va.EmailVerifier, "from_env", return_value=verifier), \
                patch.object(va.media_store, "write_enabled", return_value=True), \
                patch.object(va.media_store, "_client", return_value=object()), \
                patch.object(va, "ensure_media", return_value="ok"):
            started = _time.monotonic()
            self.assertEqual(va.main(), 0)
            self.assertLess(_time.monotonic() - started, 4)
        finish.assert_not_called()
        self.assertEqual(calls[0][0], 1)
        self.assertIn("longer than 1s", calls[0][1])
        cache.assert_not_called()  # an inconclusive timeout is never cached


class MediaReadyTest(unittest.TestCase):
    def test_requires_index_manifest_and_nonempty_sprite(self):
        client = Mock()
        client.head_object.return_value = {"ContentLength": 10}
        with patch.object(va.media_store, "fetch_index", return_value={"1": "Game_1"}), \
                patch.object(va.media_store, "fetch_manifest", return_value={"images": [va.SPRITE]}):
            self.assertTrue(va.media_ready(1, client))
            client.head_object.return_value = {"ContentLength": 0}
            self.assertFalse(va.media_ready(1, client))
        with patch.object(va.media_store, "fetch_index", return_value={}):
            self.assertFalse(va.media_ready(1, client))
        with patch.object(va.media_store, "fetch_index", return_value={"1": "Game_1"}), \
                patch.object(va.media_store, "fetch_manifest", return_value={"images": []}):
            self.assertFalse(va.media_ready(1, client))


if __name__ == "__main__":
    unittest.main()
