import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import restore_games as rg
from restore_games import pipeline, batch_fetch


def steam(appid, *, coming_soon=True, date="Coming soon", kind="game", screenshots=True, email="hi@studio.test"):
    data = {"type": kind, "name": f"Game {appid}", "short_description": "brief",
            "release_date": {"coming_soon": coming_soon, "date": date},
            "support_info": {"email": email, "url": ""}, "developers": ["Dev"], "publishers": ["Pub"],
            "genres": [{"id": "1", "description": "Action"}]}
    if screenshots:
        data["screenshots"] = [{"id": 0, "path_full": "https://x.test/0.jpg"}]
    return json.dumps({str(appid): {"success": True, "data": data}})


class RestoreTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        pipeline.TURSO_URL = ""          # never production
        pipeline.DB_PATH = self.db
        pipeline._ensured = False
        with sqlite3.connect(self.db) as c:
            batch_fetch.init_db(c)
            c.execute("CREATE TABLE triage_criteria (id INTEGER PRIMARY KEY, text TEXT NOT NULL, "
                      "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            c.execute("CREATE TABLE media_cleanup_jobs (appid INTEGER PRIMARY KEY, folder TEXT)")
            c.execute("CREATE TABLE pending_discovery (appid INTEGER PRIMARY KEY, name TEXT, discovered_on TEXT)")
        pipeline.init_tracker()
        self.conn = sqlite3.connect(self.db)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db + suffix):
                os.remove(self.db + suffix)

    def restore(self, appid, decision=None, raw=None, rebuild=None):
        return rg.restore_one(self.conn, appid, decision, fetch=lambda a: raw or steam(a),
                              rebuild=rebuild or Mock())

    def row(self, appid):
        return self.conn.execute(
            "SELECT Mail_status, triage_decision, triage_reason, emails, scrape_status, release_date "
            "FROM scrape_tracker WHERE appid=?", (appid,)).fetchone()

    def test_restore_recreates_pending_row_with_email_and_media(self):
        rebuild = Mock()
        self.conn.execute("INSERT INTO pending_discovery VALUES (5, 'Game 5', '2026-11-12')")
        detail = self.restore(5, rebuild=rebuild)
        self.assertEqual(detail, "restored to Pending")
        self.assertEqual(self.row(5), ("Pending", None, None, "hi@studio.test", "seeded", "Coming soon"))
        rebuild.assert_called_once_with(5, None)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM pending_discovery WHERE appid=5").fetchone())

    def test_restore_with_pass_lands_in_passed_with_a_reason(self):
        self.assertEqual(self.restore(6, "pass"), "restored to Passed")
        status, decision, reason, *_ = self.row(6)
        self.assertEqual((status, decision, reason), ("Triaged", "pass", rg.RESTORE_REASON))
        crit = self.conn.execute("SELECT c.text FROM scrape_tracker s JOIN triage_criteria c "
                                 "ON c.id=s.triage_criteria_id WHERE s.appid=6").fetchone()
        self.assertEqual(crit, (rg.RESTORE_CRITERIA,))
        self.restore(7, "pass")   # the criteria row is reused, not duplicated
        self.assertEqual(self.conn.execute("SELECT count(*) FROM triage_criteria").fetchone(), (1,))

    def test_no_support_email_is_reported(self):
        self.assertEqual(self.restore(8, raw=steam(8, email="")), "restored to Pending (no email on Steam)")

    def test_malformed_steam_email_restores_as_no_email(self):
        for appid, bad in ((30, "https://studio.test/"), (31, "&lt;redacted&gt;")):
            self.assertEqual(self.restore(appid, raw=steam(appid, email=bad)),
                             "restored to Pending (no email on Steam)")
            self.assertEqual(self.row(appid)[:5], ("Pending", None, None, None, "pending"))

    def test_existing_game_is_left_alone(self):
        self.restore(9, "pass")
        fetch = Mock()
        out = rg.restore_one(self.conn, 9, None, fetch=fetch, rebuild=Mock())
        self.assertIn("already in the tracker (Triaged)", out)
        fetch.assert_not_called()

    def test_refusals_explain_why(self):
        cases = [(steam(10, coming_soon=False, date="1 Jan, 2026"), "already released (1 Jan, 2026)"),
                 (steam(11, kind="dlc"), "not a game or demo (dlc)"),
                 (steam(12, screenshots=False), "no screenshots"),
                 (json.dumps({"13": {"success": False}}), "no store page")]
        for raw, why in cases:
            appid = int(next(iter(json.loads(raw))))
            with self.assertRaises(rg.RestoreError) as ctx:
                self.restore(appid, raw=raw)
            self.assertIn(why, str(ctx.exception))
            self.assertIsNone(self.row(appid))

    def test_pending_image_cleanup_blocks_restore(self):
        self.conn.execute("INSERT INTO media_cleanup_jobs VALUES (14, 'f')")
        with self.assertRaises(rg.RestoreError) as ctx:
            self.restore(14)
        self.assertIn("still being deleted", str(ctx.exception))

    def test_media_failure_undoes_the_rows_so_it_can_be_retried(self):
        with self.assertRaises(rg.RestoreError) as ctx:
            self.restore(15, rebuild=Mock(side_effect=RuntimeError("steam cdn down")))
        self.assertIn("steam cdn down", str(ctx.exception))
        self.assertIsNone(self.row(15))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM newly_added WHERE appid=15").fetchone())
        self.assertEqual(self.restore(15), "restored to Pending")

    def test_main_drains_the_queue_and_records_outcomes(self):
        self.conn.execute(rg.SCHEMA)
        self.conn.executemany("INSERT INTO restore_requests(appid, decision) VALUES (?,?)",
                              [(20, "pass"), (21, None)])
        self.conn.commit()
        raws = {20: steam(20), 21: steam(21, coming_soon=False, date="2020")}
        with patch.object(rg.batch_fetch, "http_get", side_effect=lambda a: raws[a]), \
                patch.object(rg.media_store, "write_enabled", return_value=True), \
                patch.object(rg.media_store, "_client", return_value=object()), \
                patch("verify_accepted.rebuild_media"):
            code = rg.main()
        self.assertEqual(code, 0)
        rows = dict((a, (s, d)) for a, s, d in self.conn.execute(
            "SELECT appid, status, detail FROM restore_requests ORDER BY appid"))
        self.assertEqual(rows[20], ("done", "restored to Passed"))
        self.assertEqual(rows[21], ("failed", "already released (2020)"))


if __name__ == "__main__":
    unittest.main()
