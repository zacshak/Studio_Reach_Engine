import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import batch_fetch as bf  # noqa: E402
import lead_discovery  # noqa: E402
import pipeline  # noqa: E402


class StudioGameCountTest(unittest.TestCase):
    def test_asks_steam_for_exact_developer_games_only(self):
        seen = []
        def get(url):
            seen.append(url)
            return json.dumps({"success": 1, "total_count": 3})
        self.assertEqual(bf.studio_game_count(" Red Corn & Co ", get=get), 3)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
        self.assertEqual((q["developer"], q["category1"]), (["Red Corn & Co"], ["998"]))

    def test_retries_then_gives_none(self):
        answers = [urllib.error.URLError("reset"), json.dumps({"success": 1, "total_count": 7})]
        def get(_url):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        self.assertEqual(bf.studio_game_count("X", get=get, sleep=lambda s: None), 7)
        self.assertIsNone(bf.studio_game_count("X", get=lambda u: '{"success": 2}', sleep=lambda s: None))
        self.assertIsNone(bf.studio_game_count("", get=lambda u: 1 / 0))


class SaveStudioCountsTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.saved = (pipeline.TURSO_URL, pipeline.DB_PATH)
        pipeline.TURSO_URL, pipeline.DB_PATH, pipeline._ensured = "", self.db, False
        with sqlite3.connect(self.db) as c:
            bf.init_db(c)
        pipeline.init_tracker()
        self.conn = sqlite3.connect(self.db)
        self.conn.executemany("INSERT INTO scrape_tracker(appid, game_name) VALUES (?, ?)",
                              [(1, "A"), (2, "B"), (3, "C"), (4, "D")])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        pipeline.TURSO_URL, pipeline.DB_PATH = self.saved
        pipeline._ensured = False
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)

    def test_every_new_game_gets_its_developer_count_once_per_developer(self):
        calls = []
        def count(dev):
            calls.append(dev)
            return {"Red Corn": 3, "Solo": 1}.get(dev)
        lead_discovery.save_studio_counts(self.conn, {1: "Red Corn", 2: "Red Corn", 3: "Solo", 4: None},
                                          count=count)
        self.assertEqual(calls, ["Red Corn", "Solo", None])
        rows = self.conn.execute("SELECT appid, dev_game_count FROM scrape_tracker ORDER BY appid").fetchall()
        self.assertEqual(rows, [(1, 3), (2, 3), (3, 1), (4, None)])


if __name__ == "__main__":
    unittest.main()
