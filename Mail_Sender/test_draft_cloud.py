import unittest

import draft_cloud


class DraftCloudTest(unittest.TestCase):
    def test_templates_and_technical_terms_are_normalized(self):
        templates = draft_cloud._parse_templates(
            "#Cold Mail - 2\nSecond\n#Cold Mail - 1\nFirst")
        self.assertEqual(templates, [(1, "First"), (2, "Second")])
        self.assertEqual(
            draft_cloud._normalize_terms("C plus plus, C sharp, 4 plus years"),
            "C++, C#, 4+ years",
        )


if __name__ == "__main__":
    unittest.main()
