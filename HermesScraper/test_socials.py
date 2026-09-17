import unittest

import socials


class SocialsTest(unittest.TestCase):
    def test_parse_keeps_only_supported_values_present_in_evidence(self):
        evidence = "https://x.com/real https://discord.gg/team contact@studio.test"
        result = socials._parse(
            '{"X":"https://x.com/real","LinkedIn":"https://evil.test/fake",'
            '"Instagram":null,"Discord":"https://discord.gg/team",'
            '"Email":"contact@studio.test"}', evidence)
        self.assertEqual(result, {
            "X": "https://x.com/real",
            "LinkedIn": None,
            "Instagram": None,
            "Discord": "https://discord.gg/team",
            "Email": "contact@studio.test",
        })

    def test_no_reachable_web_source_stays_retryable(self):
        class OfflinePage:
            def goto(self, *_args, **_kwargs):
                raise TimeoutError

        with self.assertRaisesRegex(RuntimeError, "no web source"):
            socials._evidence(OfflinePage(), {
                "appid": 1, "game_name": "Game", "developers": "Studio",
                "website": "", "support_info": "{}",
            })


if __name__ == "__main__":
    unittest.main()
