import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import epic_db
from epic_client import EpicCatalogClient, normalize_product


class EpicTests(unittest.TestCase):
    def test_normalize_uses_namespace_as_game_key_and_extracts_metadata(self):
        product = normalize_product({
            "id": "offer-1",
            "namespace": "game-1",
            "title": "Example Game",
            "description": "A brief.",
            "customAttributes": [
                {"key": "developerName", "value": "Studio"},
                {"key": "publisherName", "value": "Publisher"},
            ],
            "categories": ["games/edition/base"],
            "tags": [{"name": "Action"}],
            "productSlug": "example-game",
            "releaseDate": "2030-01-01T00:00:00Z",
        })
        self.assertEqual(product["epic_key"], "namespace:game-1")
        self.assertEqual(product["developers"], "Studio")
        self.assertEqual(product["store_url"], "https://store.epicgames.com/en-US/p/example-game")

    def test_normalize_prefers_product_home_page_slug(self):
        product = normalize_product({
            "id": "offer-1",
            "namespace": "game-1",
            "title": "Example",
            "productSlug": "internal-offer-hash",
            "offerMappings": [{"pageType": "productHome", "pageSlug": "example-123abc"}],
        })
        self.assertEqual(product["store_url"], "https://store.epicgames.com/en-US/p/example-123abc")

    def test_fetch_upcoming_follows_server_page_limit(self):
        client = EpicCatalogClient()
        calls = []

        def fake_post(page, limit):
            calls.append((page, limit))
            count = 50 if page == 1 else 3
            start = 0 if page == 1 else 50
            return {
                "page": page,
                "limit": 50,
                "elements": [
                    {"id": f"offer-{i}", "title": f"Game {i}"}
                    for i in range(start, start + count)
                ],
            }

        client._post = fake_post
        products = client.fetch_upcoming(page_size=100)

        self.assertEqual(len(products), 53)
        self.assertEqual(calls, [(1, 100), (2, 100)])

    def test_snapshot_isolated_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(os.path.join(tmp, "epic.sqlite"))
            product = normalize_product({"id": "offer-1", "namespace": "game-1", "title": "Example"})
            first = epic_db.apply_snapshot(conn, [product], "2030-01-01T00:00:00+00:00", "test", bootstrap=True)
            second = epic_db.apply_snapshot(conn, [product], "2030-01-02T00:00:00+00:00", "test")
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM epic_tracker").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='scrape_tracker'").fetchone()[0], 0)
            conn.close()

    def test_detail_record_is_separate_and_retryable(self):
        conn = sqlite3.connect(":memory:")
        epic_db.init_db(conn)
        product = normalize_product({"id": "offer-1", "namespace": "game-1", "title": "Example"})
        self.assertEqual(epic_db.detail_keys_needed(conn, [product]), [product])
        epic_db.record_detail(conn, product, {
            "id": "offer-1",
            "longDescription": "Full detail",
            "keyImages": [{"type": "Thumbnail", "url": "https://example.test/a.jpg"}],
            "viewableDate": "2030-01-01T00:00:00Z",
        }, "2030-01-01T00:00:00+00:00")
        self.assertEqual(epic_db.detail_keys_needed(conn, [product]), [])
        row = conn.execute("SELECT long_description, status FROM epic_details").fetchone()
        self.assertEqual(row, ("Full detail", "complete"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM epic_tracker").fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
