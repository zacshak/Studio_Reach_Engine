import unittest
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import socials


class SocialsTest(unittest.TestCase):
    def test_parse_accepts_supported_complete_values_present_in_evidence(self):
        evidence = "https://x.com/real https://discord.gg/team contact@studio.test"
        result = socials._parse(
            '{"X":"https://x.com/real","LinkedIn":null,'
            '"Instagram":null,"Discord":"https://discord.gg/team",'
            '"Email":"contact@studio.test"}', evidence)
        self.assertEqual(result, {
            "X": "https://x.com/real",
            "LinkedIn": None,
            "Instagram": None,
            "Discord": "https://discord.gg/team",
            "Email": "contact@studio.test",
        })

    def test_explicit_all_null_is_valid(self):
        values = dict.fromkeys(socials.FIELDS)
        self.assertEqual(socials._parse(json.dumps(values), ""), values)

    def test_invalid_structure_raises(self):
        valid = dict.fromkeys(socials.FIELDS)
        cases = ["{}", "[]", 'prefix ' + json.dumps(valid),
                 json.dumps({**valid, "extra": None}),
                 json.dumps({k: v for k, v in valid.items() if k != "X"}),
                 '{"X":null,' + json.dumps(valid)[1:]]
        cases += [json.dumps({**valid, "X": v}) for v in (1, True, [], {}, "", " ")]
        for answer in cases:
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                socials._parse(answer, "")

    def test_evidence_requires_complete_tokens_and_supported_hosts(self):
        cases = [
            ("X", "https://x.com/team", "https://x.com/team_other"),
            ("X", "https://x.com/Team", "https://x.com/team"),
            ("X", "https://x.com/team", "https://x.com/team?other=1"),
            ("Email", "jobs@studio.test", "otherjobs@studio.test"),
            ("Email", "jobs@studio.test", "jobs@studio.test.evil"),
            ("Email", "jobs@studio.test", "jobs@studio.test1"),
            ("X", "https://evil.test/team", "https://evil.test/team"),
        ]
        for field, value, evidence in cases:
            with self.subTest(value=value, evidence=evidence), self.assertRaises(ValueError):
                socials._valid(field, value, evidence)
        self.assertEqual(socials._valid("X", "https://x.com/team",
                         '<a href="https://x.com/team">Team</a>'), "https://x.com/team")

    def test_extractor_retries_invalid_response_and_ignores_row_as_evidence(self):
        values = dict.fromkeys(socials.FIELDS)
        response = lambda text: SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=text))])
        create = Mock(side_effect=[response(json.dumps({**values, "X": "https://x.com/team"})),
                                   response(json.dumps(values))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(socials.time, "sleep"):
            self.assertEqual(socials._extract(client, "fake", {"website": "https://x.com/team"}, ""), values)
        self.assertEqual(create.call_count, 2)

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
