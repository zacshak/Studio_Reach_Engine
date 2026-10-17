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
from contextlib import closing
from unittest.mock import Mock, call, patch

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

    def test_social_columns_default_and_survive_reinit(self):
        with self._raw() as c:
            insert_lead(c, 106)
            row = c.execute("SELECT Require_Socials, Socials_Data, Socials_Request_ID FROM scrape_tracker "
                            "WHERE appid=106").fetchone()
            c.execute("UPDATE scrape_tracker SET Require_Socials=1, Socials_Data=?, "
                      "Socials_Request_ID='request-106' WHERE appid=106",
                      ('{"Website":"https://studio.test"}',))
            c.commit()
        self.assertEqual(row, (0, "{}", None))
        pipeline._ensured = False
        pipeline.init_tracker()
        with self._raw() as c:
            saved = c.execute("SELECT Require_Socials, Socials_Data, Socials_Request_ID FROM scrape_tracker "
                              "WHERE appid=106").fetchone()
        self.assertEqual(saved, (1, '{"Website":"https://studio.test"}', "request-106"))

    def test_request_id_migration_is_additive_and_preserves_socials(self):
        with self._raw() as c:
            insert_lead(c, 107)
            c.execute("ALTER TABLE scrape_tracker DROP COLUMN Socials_Request_ID")
            c.execute("ALTER TABLE scrape_tracker ADD COLUMN custom_field TEXT")
            c.execute("UPDATE scrape_tracker SET Require_Socials=1, "
                      "Socials_Data='{}', custom_field='keep' WHERE appid=107")
            c.execute("CREATE INDEX custom_tracker_index ON scrape_tracker(custom_field)")
            c.execute("CREATE TRIGGER custom_tracker_trigger AFTER UPDATE ON scrape_tracker "
                      "BEGIN SELECT 1; END")
            rootpage = c.execute("SELECT rootpage FROM sqlite_master "
                                 "WHERE name='scrape_tracker'").fetchone()
        pipeline.init_tracker()
        pipeline.init_tracker()
        with self._raw() as c:
            column = next(r for r in c.execute("PRAGMA table_info(scrape_tracker)")
                          if r[1] == "Socials_Request_ID")
            self.assertEqual(column[2:5], ("TEXT", 0, None))
            self.assertEqual(c.execute("SELECT Require_Socials, Socials_Data, "
                                      "Socials_Request_ID, custom_field FROM scrape_tracker "
                                      "WHERE appid=107").fetchone(), (1, "{}", None, "keep"))
            self.assertEqual(c.execute("SELECT rootpage FROM sqlite_master "
                                      "WHERE name='scrape_tracker'").fetchone(), rootpage)
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name IN "
                                      "('custom_tracker_index','custom_tracker_trigger')").fetchone(), (2,))

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

    def test_additive_columns_migrate_without_losing_existing_data(self):
        with self._raw() as c:
            insert_lead(c, 8, support_email="a@b.com")
            c.execute("UPDATE scrape_tracker SET Mail_status='Sent', sent_at='2026-01-02' "
                      "WHERE appid=8")
            c.execute("ALTER TABLE scrape_tracker DROP COLUMN triage_kept")
            c.execute("ALTER TABLE scrape_tracker DROP COLUMN Require_Socials")
            c.execute("ALTER TABLE scrape_tracker DROP COLUMN Socials_Data")
            c.commit()
        pipeline._ensured = False
        pipeline.init_tracker()
        with self._raw() as c:
            row = c.execute("SELECT game_name, Mail_status, sent_at, triage_kept, "
                            "Require_Socials, Socials_Data "
                            "FROM scrape_tracker WHERE appid=8").fetchone()
        self.assertEqual(row, ("Game", "Sent", "2026-01-02", 0, 0, "{}"))

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

    def test_email_verification_cache_is_case_insensitive_and_idempotent(self):
        result = {"result": "valid", "safe_to_send": True}
        self.assertTrue(pipeline.cache_email_verification("Studio@Example.com", result))
        self.assertEqual(pipeline.get_email_verification("studio@example.com"), result)
        self.assertFalse(pipeline.cache_email_verification(
            "unknown@example.com", {"result": "unknown"}))
        pipeline._ensured = False
        pipeline.init_tracker()
        self.assertEqual(pipeline.get_email_verification("STUDIO@EXAMPLE.COM"), result)

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
        with self.assertRaises(ValueError):
            pipeline.write_result(600, scrape_status="no_email")  # stale result
        with self._raw() as c:
            row = c.execute("SELECT scrape_status,emails,game_name "
                            "FROM scrape_tracker WHERE appid=600").fetchone()
        self.assertEqual(row[0], "scraped")
        self.assertEqual(row[1], "a@b.com")     # a late miss cannot erase a contact
        self.assertEqual(row[2], "Keep")      # seeded data intact

    def test_delete_lead_removes_from_both_tables(self):
        with self._raw() as c:
            insert_lead(c, 650)                    # trigger seeds scrape_tracker
            c.execute("INSERT INTO pending_discovery VALUES (650,'Game','2026-10-16')")
        pipeline.delete_lead(650)
        with self._raw() as c:
            in_tracker = c.execute("SELECT COUNT(*) FROM scrape_tracker "
                                   "WHERE appid=650").fetchone()[0]
            in_added = c.execute("SELECT COUNT(*) FROM newly_added "
                                 "WHERE appid=650").fetchone()[0]
        self.assertEqual((in_tracker, in_added), (0, 0))
        with self._raw() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM pending_discovery WHERE appid=650")
                             .fetchone(), (0,))
        with self.assertRaises(ValueError):
            pipeline.write_result(650, scrape_status="scraped", emails="late@example.com")

    def test_delete_lead_rolls_back_discovery_queue_on_later_failure(self):
        with self._raw() as c:
            insert_lead(c, 651)
            c.execute("INSERT INTO pending_discovery VALUES (651,'Game','2026-10-16')")
            c.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON scrape_tracker "
                      "BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            pipeline.delete_lead(651)
        with self._raw() as c:
            for table in ("pending_discovery", "scrape_tracker", "newly_added"):
                self.assertEqual(c.execute(f"SELECT COUNT(*) FROM {table} WHERE appid=651")
                                 .fetchone(), (1,))

    def test_write_result_rejects_bad_status(self):
        with self.assertRaises(ValueError):
            pipeline.write_result(700, scrape_status="bogus")

    def test_write_result_rejects_missing_leads_without_resurrection(self):
        for status in ("failed", "no_email", "scraped"):
            with self.assertRaises(ValueError):
                pipeline.write_result(800, scrape_status=status, emails="late@example.com")
        with self._raw() as c:
            row = c.execute("SELECT scrape_status FROM scrape_tracker "
                            "WHERE appid=800").fetchone()
        self.assertIsNone(row)

    def test_init_preserves_additive_columns_indexes_and_triggers(self):
        with self._raw() as c:
            insert_lead(c, 801)
            c.execute("ALTER TABLE scrape_tracker ADD COLUMN custom_field TEXT")
            c.execute("UPDATE scrape_tracker SET custom_field='keep' WHERE appid=801")
            c.execute("CREATE INDEX custom_index ON scrape_tracker(custom_field)")
            c.execute("CREATE TRIGGER custom_trigger AFTER UPDATE OF game_name ON "
                      "scrape_tracker BEGIN UPDATE scrape_tracker SET custom_field='triggered' "
                      "WHERE appid=NEW.appid; END")
        pipeline.init_tracker()
        with self._raw() as c:
            self.assertEqual(c.execute("SELECT custom_field FROM scrape_tracker WHERE appid=801")
                             .fetchone(), ("keep",))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name IN "
                                       "('custom_index','custom_trigger')").fetchone(), (2,))
            c.execute("UPDATE scrape_tracker SET game_name='new' WHERE appid=801")
            self.assertEqual(c.execute("SELECT custom_field FROM scrape_tracker WHERE appid=801")
                             .fetchone(), ("triggered",))

    def test_failed_pending_leads_are_retried_but_advanced_rows_are_not(self):
        with self._raw() as c:
            for appid in (802, 803, 804):
                insert_lead(c, appid)
            c.execute("UPDATE scrape_tracker SET scrape_status='failed' WHERE appid IN (802,803)")
            c.execute("UPDATE scrape_tracker SET Mail_status='Writing' WHERE appid=803")
        self.assertEqual(pipeline.get_pending(), [802, 804])
        self.assertEqual(pipeline.get_pending(limit=1), [802])
        pipeline.write_result(802, scrape_status="scraped", emails="ok@example.com")
        self.assertEqual(pipeline.get_pending(), [804])

    def test_scrape_update_cannot_overwrite_concurrent_progress_or_resurrect_deleted_row(self):
        # Commit a competing change after the initial SELECT, before the guarded UPDATE.
        for change in (
            "UPDATE scrape_tracker SET Mail_status='Sending' WHERE appid=805",
            "UPDATE scrape_tracker SET Mail_status='Sent' WHERE appid=805",
            "UPDATE scrape_tracker SET Mail_status='Writing' WHERE appid=805",
            "UPDATE scrape_tracker SET scrape_status='scraped', emails='winner@example.com' WHERE appid=805",
            "DELETE FROM scrape_tracker WHERE appid=805",
        ):
            with self.subTest(change=change):
                with self._raw() as c:
                    c.execute("DELETE FROM scrape_tracker WHERE appid=805")
                    c.execute("INSERT INTO scrape_tracker (appid) VALUES (805)")
                raw = self._raw()
                test = self
                class Interleaved:
                    def execute(self, sql, args=()):
                        cursor = raw.execute(sql, args)
                        if sql.startswith("SELECT emails, Mail_status"):
                            rows = cursor.fetchall()
                            with test._raw() as competing:
                                competing.execute(change)
                            return Mock(fetchone=lambda: rows[0] if rows else None)
                        return cursor
                    def close(self):
                        raw.close()
                    def commit(self):
                        raw.commit()
                with patch.object(pipeline, "_rw", return_value=Interleaved()):
                    with self.assertRaises(ValueError):
                        pipeline.write_result(805, scrape_status="no_email")
                with self._raw() as c:
                    actual = c.execute("SELECT Mail_status,scrape_status,emails FROM scrape_tracker "
                                       "WHERE appid=805").fetchone()
                if change.startswith("DELETE"):
                    self.assertIsNone(actual)
                elif "winner@example.com" in change:
                    self.assertEqual(actual, ("Pending", "scraped", "winner@example.com"))
                else:
                    self.assertEqual(actual[0], change.split("'")[1])

    def test_seeded_scraped_and_advanced_rows_reject_late_scrape_results(self):
        for scrape, mail in (("seeded", "Pending"), ("scraped", "Pending"),
                             ("pending", "Writing"), ("failed", "Drafted")):
            with self.subTest(scrape=scrape, mail=mail):
                with self._raw() as c:
                    c.execute("INSERT OR REPLACE INTO scrape_tracker "
                              "(appid,scrape_status,Mail_status,emails) VALUES (806,?,?,?)",
                              (scrape, mail, "keep@example.com"))
                for result in ("failed", "no_email", "scraped"):
                    with self.assertRaises(ValueError):
                        pipeline.write_result(806, scrape_status=result, emails="late@example.com")
                self.assertEqual(pipeline.get_emails(806), "keep@example.com")

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
        with self._raw() as c:
            c.execute("UPDATE scrape_tracker SET Require_Socials=1, "
                      "Socials_Data=?, Socials_Request_ID='send-950' WHERE appid=950",
                      ('{"Website":"https://studio.test"}',))
            c.commit()
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
            row = c.execute("SELECT Mail_status, sent_at, Require_Socials, Socials_Data, "
                            "Socials_Request_ID FROM scrape_tracker "
                            "WHERE appid=950").fetchone()
            cached = c.execute("SELECT COUNT(*) FROM newly_added WHERE appid=950").fetchone()[0]
        self.assertEqual(row[0], "Sent")
        self.assertIsNotNone(row[1])
        self.assertEqual(row[2:], (1, '{"Website":"https://studio.test"}', 'send-950'))
        self.assertEqual(cached, 0)

    def test_send_completion_and_resets_preserve_social_state(self):
        data = json.dumps(dict.fromkeys(("X", "LinkedIn", "Instagram", "Discord", "Website")))
        for flag in (0, 1):
            for outcome in ("Sent", "Scheduled", "Drafted"):
                with self.subTest(flag=flag, outcome=outcome):
                    appid = 960 + flag * 3 + ("Sent", "Scheduled", "Drafted").index(outcome)
                    with self._raw() as c:
                        insert_lead(c, appid, support_email="hello@example.com")
                        c.execute("UPDATE scrape_tracker SET Mail_status='Scheduled', "
                                  "Require_Socials=?, Socials_Data=?, Socials_Request_ID=? "
                                  "WHERE appid=?", (flag, data, str(appid), appid))
                    self.assertTrue(pipeline.claim_mail(appid))
                    if outcome == "Sent":
                        pipeline.mark_sent(appid)
                        pipeline.mark_sent(appid)
                    else:
                        pipeline.reset_sending(appid, outcome)
                    with self._raw() as c:
                        self.assertEqual(c.execute(
                            "SELECT Mail_status, Require_Socials, Socials_Data, Socials_Request_ID "
                            "FROM scrape_tracker WHERE appid=?", (appid,)).fetchone(),
                            (outcome, flag, data, str(appid)))

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

    def test_remote_reads_recover_from_closed_connection_and_expired_stream(self):
        for message in ("Hrana: http error: connection closed before message completed",
                        "Hrana: api error: status=404 stream not found: test"):
            for stage in ("execute", "fetchall"):
                with self.subTest(message=message, stage=stage):
                    broken, healthy, client = Mock(), Mock(), Mock()
                    if stage == "execute":
                        broken.execute.side_effect = ValueError(message)
                    else:
                        broken.execute.return_value.fetchall.side_effect = ValueError(message)
                    healthy.execute.return_value.fetchall.return_value = [(1,)]
                    client.connect.return_value = healthy
                    with patch.object(pipeline, "libsql", client), \
                            patch.object(pipeline.time, "sleep") as sleep:
                        conn = pipeline._Conn(broken)
                        self.assertEqual(conn.execute("SELECT ?", (1,)).fetchall(), [(1,)])
                        self.assertEqual(conn.execute("SELECT ?", (1,)).fetchall(), [(1,)])
                    broken.close.assert_called_once()
                    client.connect.assert_called_once_with(
                        pipeline.TURSO_URL, auth_token=pipeline.TURSO_TOKEN)
                    healthy.execute.assert_has_calls([call("SELECT ?", (1,)),
                                                     call("SELECT ?", (1,))], any_order=True)
                    sleep.assert_called_once_with(1)

    def test_remote_retries_are_bounded_and_never_replay_writes_or_sql_errors(self):
        cases = (("SELECT 1", "connection closed before message completed", 4),
                 ("UPDATE scrape_tracker SET Mail_status='Sent'", "connection closed", 1),
                 ("SELECT missing", "no such column: missing", 1))
        for sql, message, attempts in cases:
            with self.subTest(sql=sql, message=message):
                raw, client = Mock(), Mock()
                raw.execute.side_effect = ValueError(message)
                client.connect.return_value = raw
                with patch.object(pipeline, "libsql", client), \
                        patch.object(pipeline.time, "sleep") as sleep:
                    with self.assertRaisesRegex(ValueError, message):
                        pipeline._Conn(raw).execute(sql)
                self.assertEqual(raw.execute.call_count, attempts)
                self.assertEqual(client.connect.call_count, attempts - 1)
                self.assertEqual(sleep.call_args_list,
                                 [call(1), call(2), call(4)] if attempts == 4 else [])

    def test_remote_read_failure_inside_transaction_never_reconnects(self):
        for start in ("BEGIN IMMEDIATE", "UPDATE scrape_tracker SET game_name='new'"):
            for stage in ("execute", "fetchall"):
                with self.subTest(start=start, stage=stage):
                    raw, client = Mock(), Mock()
                    raw.execute.return_value.fetchall.return_value = []
                    conn = pipeline._Conn(raw)
                    conn.execute(start)
                    error = ValueError("Hrana: api error: status=404 stream not found")
                    if stage == "execute":
                        raw.execute.side_effect = error
                    else:
                        raw.execute.return_value.fetchall.side_effect = error
                    with patch.object(pipeline, "libsql", client), patch.object(pipeline.time, "sleep") as sleep:
                        with self.assertRaisesRegex(ValueError, "stream not found"):
                            conn.execute("SELECT appid FROM pending_discovery")
                    client.connect.assert_not_called()
                    raw.close.assert_not_called()
                    sleep.assert_not_called()

    def test_driver_transaction_flag_prevents_read_reconnect(self):
        raw, client = Mock(), Mock()
        raw.in_transaction = True
        raw.execute.side_effect = ValueError("connection closed")
        with patch.object(pipeline, "libsql", client):
            with self.assertRaises(ValueError):
                pipeline._Conn(raw).execute("SELECT 1")
        client.connect.assert_not_called()

    def test_transaction_end_restores_standalone_read_retry(self):
        for finish in ("commit", "rollback", "COMMIT", "ROLLBACK"):
            with self.subTest(finish=finish):
                raw, healthy, client = Mock(), Mock(), Mock()
                raw.execute.return_value.fetchall.return_value = []
                healthy.execute.return_value.fetchall.return_value = [(1,)]
                client.connect.return_value = healthy
                conn = pipeline._Conn(raw)
                conn.execute("BEGIN IMMEDIATE")
                if finish.islower():
                    getattr(conn, finish)()
                else:
                    conn.execute(finish)
                raw.execute.side_effect = ValueError("connection closed")
                with patch.object(pipeline, "libsql", client), patch.object(pipeline.time, "sleep"):
                    self.assertEqual(conn.execute("SELECT 1").fetchall(), [(1,)])
                client.connect.assert_called_once()

    def test_failed_write_scope_rolls_back_before_shared_handle_is_reused(self):
        raw = sqlite3.connect(":memory:")
        self.addCleanup(raw.close)
        raw.execute("CREATE TABLE changes (value TEXT UNIQUE)")
        conn = pipeline._Conn(raw)
        with patch.object(pipeline, "TURSO_URL", "libsql://test"), \
                patch.object(pipeline, "_turso", return_value=conn):
            with self.assertRaises(sqlite3.IntegrityError):
                with closing(pipeline._rw()) as writer:
                    writer.execute("INSERT INTO changes VALUES ('failed')")
                    writer.execute("INSERT INTO changes VALUES ('failed')")
            self.assertFalse(raw.in_transaction)
            with closing(pipeline._rw()) as writer:
                writer.execute("INSERT INTO changes VALUES ('next')")
                writer.commit()
        self.assertEqual(raw.execute("SELECT value FROM changes").fetchall(), [("next",)])
        self.assertIs(conn._raw, raw)

    def test_nested_read_close_does_not_rollback_outer_write(self):
        raw = sqlite3.connect(":memory:")
        self.addCleanup(raw.close)
        raw.execute("CREATE TABLE changes (value TEXT)")
        conn = pipeline._Conn(raw)
        with patch.object(pipeline, "TURSO_URL", "libsql://test"), \
                patch.object(pipeline, "_turso", return_value=conn):
            with closing(pipeline._rw()) as writer:
                writer.execute("INSERT INTO changes VALUES ('keep')")
                with closing(pipeline._ro()) as reader:
                    self.assertEqual(reader.execute("SELECT value FROM changes").fetchall(), [("keep",)])
                self.assertTrue(raw.in_transaction)
                writer.commit()
        self.assertEqual(raw.execute("SELECT value FROM changes").fetchall(), [("keep",)])

    def test_rollback_failure_discards_handle_and_preserves_original_error(self):
        raw, healthy, client = Mock(), Mock(), Mock()
        raw.execute.return_value.fetchall.return_value = []
        raw.rollback.side_effect = RuntimeError("rollback failed")
        healthy.execute.return_value.fetchall.return_value = [(1,)]
        client.connect.return_value = healthy
        conn = pipeline._Conn(raw)
        with self.assertRaisesRegex(ValueError, "original failure"):
            with closing(conn):
                conn.execute("BEGIN IMMEDIATE")
                raise ValueError("original failure")
        raw.rollback.assert_called_once()
        raw.close.assert_called_once()
        self.assertIsNone(conn._raw)
        with patch.object(pipeline, "libsql", client):
            self.assertEqual(conn.execute("SELECT 1").fetchall(), [(1,)])
        client.connect.assert_called_once()

    def test_clean_close_reuses_handle_and_driver_transaction_is_rolled_back(self):
        raw = Mock()
        raw.in_transaction = False
        conn = pipeline._Conn(raw)
        conn.close()
        raw.rollback.assert_not_called()
        raw.close.assert_not_called()
        raw.in_transaction = True
        conn.close()
        raw.rollback.assert_called_once()
        raw.close.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
