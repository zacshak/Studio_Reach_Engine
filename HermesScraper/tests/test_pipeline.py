"""Unit tests for pipeline.py — fully isolated.

Each test runs against a fresh temporary SQLite DB (never the real cache.sqlite),
with a minimal `newly_added` table standing in for System 1's output.

Run:  python -m unittest discover -s tests   (from the project root)
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pipeline  # noqa: E402

# the newly_added columns the pipeline actually reads / the trigger references
NA_COLS = ("appid", "fetched_at", "success", "name", "short_description",
           "website", "developers", "publishers", "genres", "support_info")


def make_newly_added(conn):
    conn.execute(
        "CREATE TABLE newly_added ("
        "appid INTEGER PRIMARY KEY, fetched_at TEXT, success INTEGER, name TEXT, "
        "short_description TEXT, website TEXT, developers TEXT, publishers TEXT, "
        "genres TEXT, support_info TEXT)")
    conn.commit()


def insert_lead(conn, appid, name="Game", brief="brief", website="http://x.com",
                devs=("DevA",), pubs=("PubA",), genres=("Action", "Indie"),
                support_email="", replace=False):
    verb = "INSERT OR REPLACE" if replace else "INSERT"
    conn.execute(
        f"{verb} INTO newly_added "
        f"({','.join(NA_COLS)}) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (appid, "t", 1, name, brief, website,
         json.dumps(list(devs)), json.dumps(list(pubs)),
         json.dumps([{"description": g} for g in genres]),
         json.dumps({"email": support_email, "url": website})))
    conn.commit()


class PipelineTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        pipeline.DB_PATH = self.db          # redirect pipeline at the temp DB
        pipeline._ensured = False           # force re-init against it
        with sqlite3.connect(self.db) as c:
            make_newly_added(c)
        pipeline.init_tracker()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suffix)
            except OSError:
                pass

    def _raw(self):
        return sqlite3.connect(self.db)

    # ---- schema / init -------------------------------------------------
    def test_init_creates_table_and_trigger(self):
        with self._raw() as c:
            tabs = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            trigs = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertIn("scrape_tracker", tabs)
        self.assertIn("trg_sync_scrape_tracker", trigs)

    # ---- trigger auto-sync --------------------------------------------
    def test_trigger_seeds_row_with_transforms(self):
        with self._raw() as c:
            insert_lead(c, 100, name="Foo", devs=("DevA", "DevB"),
                        genres=("Action", "RPG"))
            row = c.execute(
                "SELECT game_name,short_descript,scrape_status,steam_url,website,"
                "developers,publishers,genres FROM scrape_tracker WHERE appid=100"
            ).fetchone()
        self.assertEqual(row[0], "Foo")
        self.assertEqual(row[2], "pending")
        self.assertEqual(row[3], "https://store.steampowered.com/app/100/")
        self.assertEqual(row[5], "DevA, DevB")
        self.assertEqual(row[7], "Action, RPG")

    def test_trigger_seeds_email_from_support_info(self):
        # a lead with a support_info email is born 'seeded' with email pre-filled;
        # one without stays 'pending'
        with self._raw() as c:
            insert_lead(c, 110, support_email="careers@studio.com")
            insert_lead(c, 111, support_email="")
            rows = dict(c.execute(
                "SELECT appid, scrape_status FROM scrape_tracker WHERE appid IN (110,111)"))
            email = c.execute(
                "SELECT emails FROM scrape_tracker WHERE appid=110").fetchone()[0]
        self.assertEqual(rows[110], "seeded")
        self.assertEqual(email, "careers@studio.com")
        self.assertEqual(rows[111], "pending")
        self.assertNotIn(110, pipeline.get_pending())   # seeded skips Hermes
        self.assertIn(111, pipeline.get_pending())

    def test_trigger_handles_null_json(self):
        # a lead with no developers/genres must not break the trigger
        with self._raw() as c:
            c.execute("INSERT INTO newly_added (appid,success,name) VALUES (101,1,'Bare')")
            c.commit()
            row = c.execute("SELECT game_name,developers,genres FROM scrape_tracker "
                            "WHERE appid=101").fetchone()
        self.assertEqual(row[0], "Bare")
        self.assertIsNone(row[1])

    def test_trigger_survives_minimal_newly_added(self):
        # System 1 adds rich columns lazily; if init_tracker runs first, the trigger
        # must not abort the next insert with "no such column: NEW.x"
        with self._raw() as c:
            c.execute("DROP TABLE newly_added")
            c.execute("CREATE TABLE newly_added (appid INTEGER PRIMARY KEY, "
                      "fetched_at TEXT, discovered_on TEXT, success INTEGER)")
            c.commit()
        pipeline._ensured = False
        pipeline.init_tracker()                 # must backfill the columns it reads
        with self._raw() as c:
            c.execute("INSERT INTO newly_added (appid, success, name) "
                      "VALUES (42, 1, 'Bare')")  # must NOT abort
            c.commit()
            row = c.execute("SELECT game_name, scrape_status FROM scrape_tracker "
                            "WHERE appid=42").fetchone()
        self.assertEqual(row, ("Bare", "pending"))

    def test_refetch_preserves_scrape_progress(self):
        # THE regression: INSERT OR REPLACE on newly_added must NOT reset tracker
        with self._raw() as c:
            insert_lead(c, 200, name="V1")
        pipeline.write_result(200, scrape_status="SCRAPED", emails="e@x.com",
                              country="Japan")
        with self._raw() as c:
            insert_lead(c, 200, name="V2", replace=True)   # re-fetch
            row = c.execute("SELECT scrape_status,emails,country FROM scrape_tracker "
                            "WHERE appid=200").fetchone()
        self.assertEqual(row, ("SCRAPED", "e@x.com", "Japan"))

    # ---- seed_pending --------------------------------------------------
    def test_seed_pending_backfills_and_is_idempotent(self):
        # rows inserted directly are already trigger-seeded; drop tracker to
        # simulate a pre-existing newly_added that needs backfilling
        with self._raw() as c:
            insert_lead(c, 300)
            insert_lead(c, 301)
            c.execute("DELETE FROM scrape_tracker")
            c.commit()
        self.assertEqual(pipeline.seed_pending(), 2)
        self.assertEqual(pipeline.seed_pending(), 0)   # idempotent

    # ---- get_pending ---------------------------------------------------
    def test_get_pending_only_pending_and_limit(self):
        with self._raw() as c:
            for a in (400, 401, 402):
                insert_lead(c, a)
        pipeline.write_result(401, scrape_status="SCRAPED")
        self.assertEqual(pipeline.get_pending(), [400, 402])
        self.assertEqual(pipeline.get_pending(limit=1), [400])

    # ---- read_lead -----------------------------------------------------
    def test_read_lead_full_row_and_missing(self):
        with self._raw() as c:
            insert_lead(c, 500, name="Full", support_email="hi@s.com")
        lead = pipeline.read_lead(500)
        self.assertEqual(set(NA_COLS), set(lead.keys()))
        self.assertEqual(lead["name"], "Full")
        self.assertIn("hi@s.com", lead["support_info"])   # raw JSON returned
        self.assertIsNone(pipeline.read_lead(999999))

    def test_newly_added_is_read_only(self):
        with self._raw() as c:
            insert_lead(c, 501)
        with self.assertRaises(sqlite3.OperationalError):
            pipeline._ro().execute("UPDATE newly_added SET name='x' WHERE appid=501")

    # ---- write_result --------------------------------------------------
    def test_write_result_partial_update_preserves(self):
        with self._raw() as c:
            insert_lead(c, 600, name="Keep")
        pipeline.write_result(600, scrape_status="SCRAPED", emails="a@b.com")
        pipeline.write_result(600, scrape_status="no_email", country="US")  # no emails
        with self._raw() as c:
            row = c.execute("SELECT scrape_status,emails,country,game_name "
                            "FROM scrape_tracker WHERE appid=600").fetchone()
        self.assertEqual(row[0], "no_email")
        self.assertEqual(row[1], "a@b.com")   # prior emails untouched
        self.assertEqual(row[2], "US")
        self.assertEqual(row[3], "Keep")      # seeded data intact

    def test_write_result_rejects_bad_status(self):
        with self.assertRaises(ValueError):
            pipeline.write_result(700, scrape_status="bogus")

    def test_write_result_autocreates_untracked(self):
        pipeline.write_result(800, scrape_status="failed")  # never seeded
        with self._raw() as c:
            row = c.execute("SELECT scrape_status FROM scrape_tracker "
                            "WHERE appid=800").fetchone()
        self.assertEqual(row[0], "failed")

    def test_write_result_stamps_scraped_at(self):
        with self._raw() as c:
            insert_lead(c, 900)
        pipeline.write_result(900, scrape_status="SCRAPED")
        with self._raw() as c:
            ts = c.execute("SELECT scraped_at FROM scrape_tracker "
                           "WHERE appid=900").fetchone()[0]
        self.assertTrue(ts and "T" in ts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
