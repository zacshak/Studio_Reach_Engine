import hashlib
import io
import json
import os
import sqlite3
import sys
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import epic_db  # noqa: E402
import epic_client  # noqa: E402
from epic_client import EpicCatalogClient, EpicClientError, normalize_product  # noqa: E402
import discover_epic


class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code, headers=None):
    return urllib.error.HTTPError(url, code, "error", headers or {}, io.BytesIO(b"{}"))


class FakeServer:
    """Scripted urlopen replacement recording each request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_client(server):
    client = EpicCatalogClient("https://catalog.test", request_interval=0)
    patcher = patch.object(epic_client.urllib.request, "urlopen", server)
    patcher.start()
    sleeper = patch.object(epic_client.time, "sleep")
    sleeper.start()
    return client, (patcher, sleeper)


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

    def test_store_slug_cannot_create_extra_path_segments(self):
        product = normalize_product({
            "id": "offer-1", "title": "Example", "productSlug": "example/edition",
        })
        self.assertEqual(
            product["store_url"],
            "https://store.epicgames.com/en-US/p/example%2Fedition",
        )

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
                "offers": [
                    {"id": f"offer-{i}", "title": f"Game {i}"}
                    for i in range(start, start + count)
                ],
            }

        client._search_page = fake_post
        products = client.fetch_upcoming(page_size=100)

        self.assertEqual(len(products), 53)
        self.assertEqual(calls, [(1, 100), (2, 100)])

    def test_snapshot_isolated_and_idempotent(self):
        with sqlite3.connect(":memory:") as conn:
            product = normalize_product({"id": "offer-1", "namespace": "game-1", "title": "Example"})
            first = epic_db.apply_snapshot(conn, [product], "2030-01-01T00:00:00+00:00", "test", bootstrap=True)
            second = epic_db.apply_snapshot(conn, [product], "2030-01-02T00:00:00+00:00", "test")
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM epic_tracker").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='scrape_tracker'").fetchone()[0], 0)

    def test_detail_record_is_separate_and_retryable(self):
        conn = sqlite3.connect(":memory:")
        epic_db.init_db(conn)
        product = normalize_product({"id": "offer-1", "namespace": "game-1", "title": "Example"})
        epic_db.apply_snapshot(conn, [product], "first", "test")
        self.assertEqual(epic_db.detail_keys_needed(conn),
                         [{"epic_key": product["epic_key"], "offer_id": "offer-1"}])
        epic_db.record_detail(conn, product, {
            "id": "offer-1",
            "longDescription": "Full detail",
            "keyImages": [{"type": "Thumbnail", "url": "https://example.test/a.jpg"}],
            "viewableDate": "2030-01-01T00:00:00Z",
        }, "2030-01-01T00:00:00+00:00")
        self.assertEqual(epic_db.detail_keys_needed(conn), [])
        row = conn.execute("SELECT long_description, status FROM epic_details").fetchone()
        self.assertEqual(row, ("Full detail", "complete"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM epic_tracker").fetchone()[0], 1)
        conn.close()

    def test_persisted_missing_and_failed_details_survive_absent_snapshot(self):
        with sqlite3.connect(":memory:") as conn:
            products = [normalize_product({"id": str(i), "title": "Game"}) for i in range(3)]
            epic_db.apply_snapshot(conn, products, "first", "test")
            epic_db.record_detail(conn, products[0], None, "first", "timeout")
            epic_db.record_detail(conn, products[2], {"id": "2"}, "first")
            epic_db.apply_snapshot(conn, [], "second", "test")
            self.assertEqual([p["offer_id"] for p in epic_db.detail_keys_needed(conn)], ["0", "1"])

    def test_changed_offer_invalidates_complete_detail(self):
        with sqlite3.connect(":memory:") as conn:
            first = normalize_product({"id": "A", "namespace": "same", "title": "Game"})
            second = normalize_product({"id": "B", "namespace": "same", "title": "Game"})
            epic_db.apply_snapshot(conn, [first], "first", "test")
            epic_db.record_detail(conn, first, {"id": "A"}, "first")
            epic_db.apply_snapshot(conn, [second], "second", "test")
            self.assertEqual(epic_db.detail_keys_needed(conn), [{"epic_key": "namespace:same", "offer_id": "B"}])
            epic_db.record_detail(conn, second, {"id": "B"}, "second")
            self.assertEqual(epic_db.detail_keys_needed(conn), [])

    def test_discovery_retries_persisted_product_absent_from_current_catalog(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        old = normalize_product({"id": "old", "title": "Old"})
        current = normalize_product({"id": "current", "title": "Current"})
        epic_db.apply_snapshot(conn, [old], "previous", "test")
        epic_db.record_detail(conn, old, None, "previous", "timeout")

        class Connection:
            def __getattr__(self, name):
                return getattr(conn, name)

            def close(self):
                pass

        with patch.object(discover_epic.epic_db, "connect", return_value=Connection()), \
                patch.object(EpicCatalogClient, "fetch_upcoming", return_value=[current]), \
                patch.object(EpicCatalogClient, "fetch_offer", side_effect=lambda offer: {"id": offer}) as fetch:
            self.assertEqual(discover_epic.main([]), 0)
        # The current product's detail comes from its search document; only the
        # product missing from the catalog needs a per-offer request.
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["old"])
        self.assertEqual(epic_db.detail_keys_needed(conn), [])

    def test_catalog_total_mismatch_never_opens_database(self):
        for elements in ([], [{"id": "1", "title": "Game"}]):
            with self.subTest(elements=elements), patch.object(EpicCatalogClient, "_search_page",
                    return_value={"total": 100, "limit": 100, "offers": elements}), \
                    patch.object(discover_epic.epic_db, "connect") as connect:
                self.assertEqual(discover_epic.main([]), 2)
                connect.assert_not_called()

    def test_catalog_counts_offers_before_namespace_deduplication(self):
        client = EpicCatalogClient()
        pages = [{"total": 2, "limit": 1, "offers": [{"id": x, "namespace": "same", "title": "Game"}]} for x in ("A", "B")]
        with patch.object(client, "_search_page", side_effect=pages) as post:
            self.assertEqual(len(client.fetch_upcoming(1)), 1)
            self.assertEqual(post.call_count, 2)

    def test_catalog_rejects_empty_tail_duplicate_offer_and_changing_total(self):
        first = {"total": 2, "limit": 1, "offers": [{"id": "A", "title": "Game"}]}
        cases = [
            [first, {"total": 2, "limit": 1, "offers": []}],
            [first, first, {"total": 2, "limit": 1, "offers": []}],
            [first, {"total": 3, "limit": 1, "offers": [{"id": "B", "title": "Game"}]}],
        ]
        for pages in cases:
            with self.subTest(pages=pages), patch.object(EpicCatalogClient, "_search_page", side_effect=pages):
                with self.assertRaises(EpicClientError):
                    EpicCatalogClient().fetch_upcoming(1)

    def test_catalog_rejects_invalid_or_underreported_total(self):
        for total in (True, -1, "2", 1.5, 0):
            with self.subTest(total=total), patch.object(EpicCatalogClient, "_search_page", return_value={
                    "total": total, "limit": 100, "offers": [{"id": "A", "title": "Game"}]}):
                with self.assertRaises(EpicClientError):
                    EpicCatalogClient().fetch_upcoming()


    def test_normalize_reads_keyed_custom_attributes(self):
        product = normalize_product({
            "id": "offer-1",
            "title": "Example",
            "customAttributes": {
                "developerName": {"type": "STRING", "value": "Studio"},
                "publisherName": {"type": "STRING", "value": "Publisher"},
            },
        })
        self.assertEqual((product["developers"], product["publishers"]), ("Studio", "Publisher"))

    def _stop(self, patchers):
        for patcher in patchers:
            self.addCleanup(patcher.stop)

    def test_search_uses_v2_get_route_with_flat_filters(self):
        server = FakeServer(FakeResponse({"total": 1, "page": 1, "limit": 100,
                                          "offers": [{"id": "A", "title": "Game"}]}))
        client, patchers = make_client(server)
        self._stop(patchers)
        self.assertEqual(len(client.fetch_upcoming()), 1)
        request = server.requests[0]
        url = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(url.path, "/search/v2/search")
        self.assertEqual(dict(urllib.parse.parse_qsl(url.query)), {
            "country": "US", "locale": "en-US", "offerType": "BASE_GAME",
            "sortBy": "upcoming", "facets": "none", "page": "1", "limit": "100",
        })

    def test_session_challenge_runs_pow_handshake_and_retries_once(self):
        challenge = {"challenge": "abc", "difficulty": 8, "algorithm": "SHA-256", "expiresAt": 1}
        solved = {}

        def verify(request):
            body = json.loads(request.data)
            digest = hashlib.sha256(f"{body['challenge']}:{body['nonce']}".encode()).digest()
            solved["ok"] = body["challenge"] == "abc" and digest[0] == 0
            return FakeResponse({"token": "tok", "expiresAt": 2})

        server = FakeServer(
            FakeResponse({"error": {"code": "SESSION_CHALLENGE_REQUIRED", "challengeUrl": "/auth/challenge"}}),
            FakeResponse(challenge),
            verify,
            FakeResponse({"id": "A", "title": "Game"}),
        )
        client, patchers = make_client(server)
        self._stop(patchers)
        self.assertEqual(client.fetch_offer("A")["id"], "A")
        self.assertTrue(solved["ok"])
        self.assertEqual([r.get_method() for r in server.requests], ["GET", "POST", "POST", "GET"])
        self.assertTrue(server.requests[1].full_url.endswith("/auth/challenge"))
        self.assertIsNone(server.requests[0].get_header("X-egdata-session"))
        self.assertEqual(server.requests[3].get_header("X-egdata-session"), "tok")

    def test_tarpit_and_rate_limit_are_retried(self):
        url = "https://catalog.test/offers/A"
        server = FakeServer(
            FakeResponse(b"", status=204),
            http_error(url, 429, {"Retry-After": "7"}),
            FakeResponse({"id": "A"}),
        )
        client, (patcher, sleeper) = make_client(server)
        self._stop((patcher,))
        try:
            self.assertEqual(client.fetch_offer("A"), {"id": "A"})
            delays = [call.args[0] for call in epic_client.time.sleep.call_args_list]
        finally:
            sleeper.stop()
        self.assertEqual(delays, [epic_client.TARPIT_BACKOFF, 7.0])

    def test_gone_endpoint_fails_without_retry(self):
        server = FakeServer(http_error("https://catalog.test/search/v2/search", 410))
        client, patchers = make_client(server)
        self._stop(patchers)
        with self.assertRaisesRegex(EpicClientError, "410"):
            client.fetch_upcoming()
        self.assertEqual(len(server.requests), 1)

    def test_pow_solver_meets_difficulty(self):
        nonce = epic_client.solve_pow("challenge", 12)
        digest = hashlib.sha256(f"challenge:{nonce}".encode()).digest()
        self.assertEqual(digest[0], 0)
        self.assertEqual(digest[1] >> 4, 0)
        with self.assertRaises(EpicClientError):
            epic_client.solve_pow("challenge", 64)

if __name__ == "__main__":
    unittest.main()
