import json
import os
import sys
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dev_counts as dc  # noqa: E402


class DevCountsTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        self.conn = sqlite3.connect(self.db)
        self.conn.execute("CREATE TABLE scrape_tracker (appid INTEGER PRIMARY KEY, Mail_status TEXT, developers TEXT)")
        self.conn.execute("CREATE TABLE newly_added (appid INTEGER PRIMARY KEY, developers TEXT)")
        self.conn.executemany("INSERT INTO scrape_tracker VALUES (?,?,?)", [
            (1, "Pending", "Red Corn"), (2, "Triaged", "Compile Heart, Idea Factory"),
            (3, "Sent", "Old Studio"), (4, "Accepted", " Solo Dev "), (5, "Drafted", "")])
        self.conn.executemany("INSERT INTO newly_added VALUES (?,?)", [
            (1, '["Red Corn"]'), (2, '["Compile Heart", "Idea Factory"]'), (3, '["Old Studio"]')])

    def tearDown(self):
        self.conn.close()
        os.remove(self.db)

    def test_developers_come_from_steam_list_with_fallback(self):
        self.assertEqual(sorted(dc.due(self.conn)), ["Compile Heart", "Idea Factory", "Red Corn", "Solo Dev"])

    def test_fresh_counts_are_skipped_until_stale(self):
        dc.due(self.conn)
        dc.save(self.conn, "Red Corn", 3)
        self.assertNotIn("Red Corn", dc.due(self.conn))
        self.assertIn("Red Corn", dc.due(self.conn, refresh_all=True))
        self.conn.execute("UPDATE developer_games SET checked_at=datetime('now','-15 days')")
        self.assertIn("Red Corn", dc.due(self.conn))
        dc.save(self.conn, "Red Corn", 4)
        self.assertEqual(self.conn.execute("SELECT games FROM developer_games").fetchall(), [(4,)])

    def test_search_asks_for_exact_developer_games_only(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(dc.search_url("Red Corn & Co")).query)
        self.assertEqual(q["developer"], ["Red Corn & Co"])
        self.assertEqual(q["category1"], ["998"])

    def test_game_count_reads_total_and_retries(self):
        answers = [urllib.error.URLError("reset"), json.dumps({"success": 1, "total_count": 7})]
        def fetch(_url):
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        self.assertEqual(dc.game_count("X", fetch=fetch, sleep=lambda s: None), 7)

    def test_game_count_gives_up_with_the_name(self):
        with self.assertRaises(RuntimeError) as ctx:
            dc.game_count("X", fetch=lambda u: '{"success": 2}', sleep=lambda s: None)
        self.assertIn("'X'", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
