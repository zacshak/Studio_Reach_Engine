import unittest

import hermes


class HermesTest(unittest.TestCase):
    def test_same_site_rejects_suffix_spoofing(self):
        self.assertTrue(hermes._same_site("https://studio.com", "/contact"))
        self.assertTrue(hermes._same_site("https://studio.com", "https://jobs.studio.com"))
        self.assertFalse(hermes._same_site("https://studio.com", "https://evilstudio.com"))
        self.assertFalse(hermes._same_site("https://studio.com", "//evil.example/contact"))


if __name__ == "__main__":
    unittest.main()
