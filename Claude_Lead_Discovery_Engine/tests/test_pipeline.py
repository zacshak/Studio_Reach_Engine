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
        pipeline.TURSO_URL = ""             # force the local sqlite path (never prod Turso!)
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

    def test_mail_status_default_and_position(self):
        cols = [c[1] for c in self._raw().execute("PRAGMA table_info(scrape_tracker)")]
        self.assertEqual(cols[cols.index("short_descript") + 1], "Mail_status")
        with self._raw() as c:
            insert_lead(c, 105)
            ms = c.execute("SELECT Mail_status FROM scrape_tracker "
                           "WHERE appid=105").fetchone()[0]
        self.assertEqual(ms, "Pending")

    def test_rebuild_migration_adds_mail_status_preserving_data(self):
        # simulate an OLD scrape_tracker (no Mail_status), with a row, then re-init:
        # the rebuild must add Mail_status in position AND keep the existing row
        with self._raw() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_sync_scrape_tracker")
            c.execute("DROP TABLE scrape_tracker")
            # the real pre-Mail_status schema (everything except Mail_status)
            c.execute("CREATE TABLE scrape_tracker (appid INTEGER PRIMARY KEY, "
                      "game_name TEXT, short_descript TEXT, scrape_status TEXT, "
                      "country TEXT, emails TEXT, engine TEXT, steam_url TEXT, "
                      "website TEXT, support_info TEXT, developers TEXT, "
                      "publishers TEXT, genres TEXT, added_at TEXT)")
            c.execute("INSERT INTO scrape_tracker (appid, game_name, short_descript, "
                      "scrape_status, emails) VALUES (7, 'Old', 'd', 'seeded', 'a@b.com')")
            c.commit()
        pipeline._ensured = False
        pipeline.init_tracker()                 # must rebuild, not crash
        with self._raw() as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(scrape_tracker)")]
            row = c.execute("SELECT scrape_status, emails, Mail_status "
                            "FROM scrape_tracker WHERE appid=7").fetchone()
        self.assertEqual(cols[cols.index("short_descript") + 1], "Mail_status")
        self.assertEqual(row, ("seeded", "a@b.com", "Pending"))  # data kept, default set

    def test_triage_keep_migration_appends_without_losing_existing_data(self):
        with self._raw() as c:
            insert_lead(c, 8, support_email="a@b.com")
            c.execute("UPDATE scrape_tracker SET Mail_status='Sent', sent_at='2026-01-02' "
                      "WHERE appid=8")
            c.execute("ALTER TABLE scrape_tracker DROP COLUMN triage_kept")
            c.commit()
        pipeline._ensured = False
        pipeline.init_tracker()
        with self._raw() as c:
            row = c.execute("SELECT game_name, Mail_status, sent_at, triage_kept "
                            "FROM scrape_tracker WHERE appid=8").fetchone()
        self.assertEqual(row, ("Game", "Sent", "2026-01-02", 0))

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
        # one without stays 'pending'; malformed contact data is quarantined
        with self._raw() as c:
            insert_lead(c, 110, support_email="careers@studio.com")
            insert_lead(c, 111, support_email="")
            insert_lead(c, 112, support_email="studio.example.com")
            rows = dict(c.execute(
                "SELECT appid, scrape_status FROM scrape_tracker WHERE appid IN (110,111,112)"))
            invalid_mail = c.execute(
                "SELECT Mail_status FROM scrape_tracker WHERE appid=112").fetchone()[0]
            email = c.execute(
                "SELECT emails FROM scrape_tracker WHERE appid=110").fetchone()[0]
        self.assertEqual(rows[110], "seeded")
        self.assertEqual(email, "careers@studio.com")
        self.assertEqual(rows[111], "pending")
        self.assertEqual(rows[112], "invalid")
        self.assertEqual(invalid_mail, "Invalid")
        self.assertNotIn(110, pipeline.get_pending())   # seeded skips Hermes
        self.assertIn(111, pipeline.get_pending())

    def test_email_validation_rejects_blank_urls_and_bare_domains(self):
        self.assertEqual(pipeline.normalize_email("hello@example.com"), "hello@example.com")
        for value in (None, "", "*", "https://example.com/", "example.com"):
            self.assertEqual(pipeline.normalize_email(value), "")

    def test_scraped_invalid_email_becomes_invalid_and_cannot_enter_mail_flow(self):
        with self._raw() as c:
            insert_lead(c, 113)
        pipeline.write_result(113, scrape_status="scraped", emails="studio.example.com")
        with self._raw() as c:
            row = c.execute("SELECT scrape_status, Mail_status FROM scrape_tracker "
                            "WHERE appid=113").fetchone()
        self.assertEqual(row, ("invalid", "Invalid"))
        with self.assertRaises(ValueError):
            pipeline.set_mail_status(113, "Writing")

    def test_claim_mail_rejects_invalid_row_even_if_scheduled_directly(self):
        with self._raw() as c:
            insert_lead(c, 114, support_email="bad.example.com")
            c.execute("UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid=114")
            c.commit()
        self.assertFalse(pipeline.claim_mail(114))

    def test_email_repair_separates_missing_malformed_and_corrected_values(self):
        with self._raw() as c:
            insert_lead(c, 115)
            insert_lead(c, 116, support_email="bad.example.com")
            insert_lead(c, 117, support_email="fixed@example.com")
            c.execute("UPDATE scrape_tracker SET scrape_status='seeded', Mail_status='Writing' WHERE appid=115")
            c.execute("UPDATE scrape_tracker SET Mail_status='Writing' WHERE appid=116")
            c.execute("UPDATE scrape_tracker SET scrape_status='invalid', Mail_status='Invalid' WHERE appid=117")
            c.commit()
        self.assertEqual(pipeline.repair_invalid_rows(), [115, 116, 117])
        with self._raw() as c:
            rows = dict(c.execute(
                "SELECT appid, scrape_status || '/' || Mail_status FROM scrape_tracker "
                "WHERE appid IN (115,116,117)"))
        self.assertEqual(rows, {
            115: "pending/Pending",
            116: "invalid/Invalid",
            117: "scraped/Pending",
        })

    def test_sender_quarantine_returns_missing_email_to_scraping(self):
        with self._raw() as c:
            insert_lead(c, 118)
            c.execute("UPDATE scrape_tracker SET scrape_status='seeded', Mail_status='Scheduled' WHERE appid=118")
            c.commit()
        pipeline.quarantine_unusable(118)
        with self._raw() as c:
            row = c.execute("SELECT scrape_status, Mail_status FROM scrape_tracker "
                            "WHERE appid=118").fetchone()
        self.assertEqual(row, ("pending", "Pending"))

    def test_external_verifier_quarantines_only_scheduled_mail(self):
        with self._raw() as c:
            insert_lead(c, 119, support_email="dead@example.com")
            insert_lead(c, 120, support_email="already@example.com")
            c.execute("UPDATE scrape_tracker SET Mail_status='Scheduled' WHERE appid=119")
            c.execute("UPDATE scrape_tracker SET Mail_status='Sent' WHERE appid=120")
            c.commit()
        self.assertTrue(pipeline.quarantine_verified_invalid(119))
        self.assertFalse(pipeline.quarantine_verified_invalid(120))
        with self._raw() as c:
            rows = dict(c.execute(
                "SELECT appid, scrape_status || '/' || Mail_status FROM scrape_tracker "
                "WHERE appid IN (119,120)"))
        self.assertEqual(rows, {119: "invalid/Invalid", 120: "seeded/Sent"})

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
        pipeline.write_result(200, scrape_status="scraped", emails="e@x.com")
        with self._raw() as c:
            insert_lead(c, 200, name="V2", replace=True)   # re-fetch
            row = c.execute("SELECT scrape_status,emails FROM scrape_tracker "
                            "WHERE appid=200").fetchone()
        self.assertEqual(row, ("scraped", "e@x.com"))

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
        pipeline.write_result(401, scrape_status="scraped")
        self.assertEqual(pipeline.get_pending(), [400, 402])
        self.assertEqual(pipeline.get_pending(limit=1), [400])

    def test_nomail_queue_requires_pending_mail_and_unresolved_scrape_state(self):
        with self._raw() as c:
            for appid in range(410, 416):
                insert_lead(c, appid)
            c.execute("UPDATE scrape_tracker SET scrape_status='no_email' WHERE appid=411")
            c.execute("UPDATE scrape_tracker SET scrape_status='failed' WHERE appid=412")
            c.execute("UPDATE scrape_tracker SET scrape_status='invalid', Mail_status='Invalid' WHERE appid=413")
            c.execute("UPDATE scrape_tracker SET scrape_status='seeded' WHERE appid=414")
            c.execute("UPDATE scrape_tracker SET scrape_status='failed', Mail_status='Writing' WHERE appid=415")
            c.commit()
        self.assertEqual(pipeline.nomail_ready_appids(), [410, 411, 412])

    def test_human_kept_lead_never_reenters_ai_triage(self):
        with self._raw() as c:
            insert_lead(c, 420)
            insert_lead(c, 421)
        self.assertEqual(pipeline.triage_pending_appids(), [420, 421])
        self.assertTrue(pipeline.mark_triage_kept(420))
        self.assertEqual(pipeline.triage_pending_appids(), [421])
        self.assertEqual(pipeline.triage_kept_appids(), [420])
        self.assertEqual(pipeline.mail_status_appids("Pending"), [420, 421])

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
        pipeline.write_result(600, scrape_status="scraped", emails="a@b.com")
        pipeline.write_result(600, scrape_status="no_email")  # no emails
        with self._raw() as c:
            row = c.execute("SELECT scrape_status,emails,game_name "
                            "FROM scrape_tracker WHERE appid=600").fetchone()
        self.assertEqual(row[0], "no_email")
        self.assertEqual(row[1], "")            # no_email cannot retain a recipient
        self.assertEqual(row[2], "Keep")      # seeded data intact

    def test_delete_lead_removes_from_both_tables(self):
        with self._raw() as c:
            insert_lead(c, 650)                    # trigger seeds scrape_tracker
        pipeline.delete_lead(650)
        with self._raw() as c:
            in_tracker = c.execute("SELECT COUNT(*) FROM scrape_tracker "
                                   "WHERE appid=650").fetchone()[0]
            in_added = c.execute("SELECT COUNT(*) FROM newly_added "
                                 "WHERE appid=650").fetchone()[0]
        self.assertEqual((in_tracker, in_added), (0, 0))

    def test_write_result_rejects_bad_status(self):
        with self.assertRaises(ValueError):
            pipeline.write_result(700, scrape_status="bogus")

    def test_write_result_autocreates_untracked(self):
        pipeline.write_result(800, scrape_status="failed")  # never seeded
        with self._raw() as c:
            row = c.execute("SELECT scrape_status FROM scrape_tracker "
                            "WHERE appid=800").fetchone()
        self.assertEqual(row[0], "failed")

    def test_added_at_copies_fetched_at(self):
        # added_at is the lead's newly_added.fetched_at, set at row creation...
        with self._raw() as c:
            insert_lead(c, 900)   # insert_lead stores fetched_at='t'
            added = c.execute("SELECT added_at FROM scrape_tracker "
                              "WHERE appid=900").fetchone()[0]
        self.assertEqual(added, "t")
        # ...and write_result must NOT touch it
        pipeline.write_result(900, scrape_status="scraped")
        with self._raw() as c:
            added2 = c.execute("SELECT added_at FROM scrape_tracker "
                               "WHERE appid=900").fetchone()[0]
        self.assertEqual(added2, "t")

    def test_mail_send_claim_and_completion_are_idempotent(self):
        with self._raw() as c:
            insert_lead(c, 950, support_email="hello@example.com")
        with self.assertRaises(ValueError):
            pipeline.set_mail_template(950, 1)
        with self.assertRaises(ValueError):
            pipeline.set_mail_status(950, "Scheduled")
        pipeline.set_mail_status(950, "Writing")
        pipeline.set_mail_template(950, 1)
        pipeline.set_mail_status(950, "Drafted")
        pipeline.set_mail_template(950, 2)
        pipeline.set_mail_status(950, "Scheduled")
        with self.assertRaises(ValueError):
            pipeline.set_mail_template(950, 3)
        self.assertEqual(pipeline.get_mail_template(950), 2)
        self.assertEqual(pipeline.mail_status_emails("Scheduled"),
                         [(950, "hello@example.com")])
        self.assertTrue(pipeline.claim_mail(950))
        self.assertFalse(pipeline.claim_mail(950))
        pipeline.mark_sent(950)
        pipeline.mark_sent(950)
        with self.assertRaises(ValueError):
            pipeline.write_result(950, scrape_status="failed")
        with self._raw() as c:
            row = c.execute("SELECT Mail_status, sent_at FROM scrape_tracker "
                            "WHERE appid=950").fetchone()
            cached = c.execute("SELECT COUNT(*) FROM newly_added WHERE appid=950").fetchone()[0]
        self.assertEqual(row[0], "Sent")
        self.assertIsNotNone(row[1])
        self.assertEqual(cached, 0)

    def test_sending_can_only_be_reset_explicitly(self):
        with self._raw() as c:
            insert_lead(c, 951, support_email="hello@example.com")
        for status in ("Writing", "Drafted", "Scheduled"):
            pipeline.set_mail_status(951, status)
        self.assertTrue(pipeline.claim_mail(951))
        pipeline.reset_sending(951, "Drafted")
        self.assertEqual(pipeline.mail_status_appids("Drafted"), [951])
        with self.assertRaises(ValueError):
            pipeline.reset_sending(951, "Sent")

    def test_remote_reads_reconnect_after_transient_dns_failure(self):
        class Cursor:
            rowcount = -1

            def fetchall(self):
                return [(1,)]

        class Broken:
            def execute(self, *_):
                raise ValueError("dns error: Temporary failure in name resolution")

            def close(self):
                pass

        class Libsql:
            @staticmethod
            def connect(*_, **__):
                return type("Healthy", (), {"execute": lambda self, *_: Cursor()})()

        old_url, old_libsql, old_sleep = pipeline.TURSO_URL, pipeline.libsql, pipeline.time.sleep
        try:
            pipeline.TURSO_URL = "libsql://test"
            pipeline.libsql = Libsql
            pipeline.time.sleep = lambda _: None
            self.assertEqual(pipeline._Conn(Broken()).execute("SELECT 1").fetchall(), [(1,)])
        finally:
            pipeline.TURSO_URL, pipeline.libsql = old_url, old_libsql
            pipeline.time.sleep = old_sleep


if __name__ == "__main__":
    unittest.main(verbosity=2)
