import unittest
from types import SimpleNamespace
from unittest.mock import patch

import draft_cloud


class DraftCloudTest(unittest.TestCase):
    def test_templates_and_generated_terms_are_normalized(self):
        templates = draft_cloud._parse_templates(
            "#Cold Mail - 2\nSecond\n#Cold Mail - 1\nFirst")
        self.assertEqual(templates, [(1, "First"), (2, "Second")])
        self.assertEqual(
            draft_cloud._normalize_terms("C plus plus, C sharp, 4 plus years"),
            "C++, C#, 4+ years",
        )

    def test_draft_only_replaces_placeholders(self):
        template = (
            "Subject: <game name>\n\n"
            "hey <developer/studio name>,\n\n"
            "<specific observation>.\n\n"
            "Fixed C plus plus text and Xbox-funded wording for <game name>."
        )
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='```json\n{"observation_1":"C plus plus lighting looks readable"}\n```'))])
        completions = SimpleNamespace(create=lambda **kwargs: response)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        manifest = {
            "name": "Game-Name",
            "meta": "Studio Name  ·  Action",
            "images": ["SpriteSheet.png"],
            "__template": template,
        }

        with patch.object(draft_cloud, "_sheet_b64", return_value="image"):
            result = draft_cloud._draft(client, "vision-llms", manifest, "folder")

        self.assertEqual(result, (
            "Subject: Game-Name\n\n"
            "hey Studio Name,\n\n"
            "C++ lighting looks readable.\n\n"
            "Fixed C plus plus text and Xbox-funded wording for Game-Name."
        ))

    def test_observation_json_requires_exact_safe_values(self):
        self.assertEqual(
            draft_cloud._parse_observations('{"observation_1":"clear lighting"}', 1),
            ["clear lighting"],
        )
        for raw in (
            '{"observation_1":"value","extra":"value"}',
            '{"observation_1":"hyphen-like"}',
            '{"observation_1":""}',
        ):
            with self.assertRaises(ValueError):
                draft_cloud._parse_observations(raw, 1)


if __name__ == "__main__":
    unittest.main()
