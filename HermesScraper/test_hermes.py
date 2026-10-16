import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import hermes


class HermesTest(unittest.TestCase):
    def test_same_site_rejects_suffix_spoofing(self):
        self.assertTrue(hermes._same_site("https://studio.com", "/contact"))
        self.assertTrue(hermes._same_site("https://studio.com", "https://jobs.studio.com"))
        self.assertFalse(hermes._same_site("https://studio.com", "https://evilstudio.com"))
        self.assertFalse(hermes._same_site("https://studio.com", "//evil.example/contact"))

    def page(self):
        page = Mock()
        page.goto.return_value = SimpleNamespace(status=200)
        page.inner_text.return_value = "Studio"
        page.content.return_value = "<body>Studio</body>"
        page.eval_on_selector_all.return_value = []
        return page

    def test_navigation_failure_is_not_empty_evidence(self):
        for method in (hermes._scrape_site, hermes._find_site):
            with self.subTest(method=method.__name__):
                page = self.page()
                page.goto.side_effect = TimeoutError("unreachable")
                with self.assertRaises(TimeoutError):
                    method(page, "https://studio.test")

    def test_http_errors_are_retryable_failures(self):
        for method in (hermes._scrape_site, hermes._find_site):
            for status in (403, 429, 503):
                with self.subTest(method=method.__name__, status=status):
                    page = self.page()
                    page.goto.return_value.status = status
                    with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                        method(page, "https://studio.test")

    def test_failed_contact_page_does_not_certify_no_email(self):
        page = self.page()
        page.eval_on_selector_all.return_value = ["https://studio.test/contact"]
        page.goto.side_effect = [SimpleNamespace(status=200), TimeoutError("contact failed")]
        with self.assertRaises(TimeoutError):
            hermes._scrape_site(page, "https://studio.test")

    def test_extraction_failure_propagates_but_successful_empty_search_is_valid(self):
        page = self.page()
        self.assertEqual(hermes._find_site(page, "Studio"), [])
        self.assertIn("Studio", hermes._scrape_site(page, "https://studio.test"))
        page.eval_on_selector_all.side_effect = RuntimeError("browser closed")
        with self.assertRaisesRegex(RuntimeError, "browser closed"):
            hermes._scrape_site(page, "https://studio.test")

    def test_main_reports_failure_then_can_retry_to_success(self):
        playwright = MagicMock()
        browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
        modules = {"openai": SimpleNamespace(OpenAI=Mock()),
                   "playwright.sync_api": SimpleNamespace(sync_playwright=playwright)}
        with patch.dict(hermes.sys.modules, modules), \
             patch.dict(hermes.os.environ, {"TRIAGE_BASE_URL": "https://invalid.test", "TRIAGE_API_KEY": "fake",
                                           "TRIAGE_MODEL": "fake"}), \
             patch.object(hermes.leads, "get_pending", return_value=[2]), \
             patch.object(hermes.leads, "read_lead", return_value={"name": "Game", "website": "https://studio.test"}), \
             patch.object(hermes.leads, "write_result") as write, \
             patch.object(hermes, "_scrape_site", side_effect=[TimeoutError("navigation failed"), "hello@studio.test"]), \
             patch.object(hermes, "_pick_email", return_value="hello@studio.test") as pick:
            self.assertEqual(hermes.main(), 1)
            write.assert_called_once_with(2, scrape_status="failed")
            pick.assert_not_called()
            self.assertEqual(hermes.main(), 0)
            self.assertEqual(write.call_args_list[-1], call(2, scrape_status="scraped",
                                                           emails="hello@studio.test", website="https://studio.test"))
            self.assertEqual(browser.close.call_count, 2)
            # A rejected or advanced row refuses both the result and error update.
            write.side_effect = ValueError('stale scrape result')
            with patch.object(hermes, '_scrape_site', return_value='hello@studio.test'):
                self.assertEqual(hermes.main(), 1)
            self.assertEqual(browser.close.call_count, 3)

    def test_model_outage_propagates_after_retry_budget(self):
        client = Mock()
        client.chat.completions.create.side_effect = RuntimeError("model unavailable")
        with patch.object(hermes, "RETRIES", 2), patch.object(hermes.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                hermes._pick_email(client, "model", "Studio", "Game", "site evidence")
        self.assertEqual(client.chat.completions.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
