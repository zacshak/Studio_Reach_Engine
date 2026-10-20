"""Offline recovery tests using only disposable SQLite databases and fake I/O."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discovery_recovery as recovery
import find_new
import lead_discovery
import pipeline
import run_daily


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = os.path.join(self.temp.name, "synthetic.sqlite")
        for target, value in (("TURSO_URL", ""), ("DB_PATH", self.db), ("_ensured", False)):
            p = patch.object(pipeline, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden"))
        p.start()
        self.addCleanup(p.stop)
        with self.connect() as conn:
            find_new.init_db(conn)
            lead_discovery.bf.init_db(conn)
        pipeline.init_tracker()

    def connect(self):
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def snapshot(self, items, bootstrap=False):
        with self.connect() as conn:
            return recovery.record_snapshot(conn, len(items), items, "2026-10-15", "now",
                                            bootstrap=bootstrap)

    def pending(self):
        with self.connect() as conn:
            return recovery.pending_appids(conn)

    def queue(self):
        self.snapshot([(1, "Historic")])
        self.snapshot([(1, "Historic"), (2, "New")])

    def payload(self):
        data = {"type": "game", "name": "New", "release_date": {"coming_soon": True},
                "screenshots": [{"path_thumbnail": "https://invalid.test/image.jpg"}],
                "support_info": {"email": "hello@example.com"}}
        raw = json.dumps({"2": {"success": True, "data": data}})
        return True, lead_discovery.bf.normalize(2, data), raw

    def discover(self, fetch, *, stage_failure=False, extra_args=()):
        # New output directory each invocation models a fresh ephemeral runner.
        with tempfile.TemporaryDirectory(dir=self.temp.name) as output:
            exporter = Mock()
            exporter.close.return_value = (0, [(2, "media failure")]) if stage_failure else (1, [])
            with patch.object(lead_discovery.bf, "OUT_DIR", output), \
                 patch.object(lead_discovery.bf, "fetch_one", side_effect=fetch) as fetched, \
                 patch.object(lead_discovery, "ApprovalExporter", return_value=exporter), \
                 patch.object(lead_discovery, "write_excel"), \
                 patch.object(sys, "argv", ["lead_discovery", "--stamp", "fresh", *extra_args]):
                result = lead_discovery.main()
            return result, fetched, exporter

    def test_first_baseline_and_forced_bootstrap_never_enqueue_history(self):
        self.assertTrue(self.snapshot([(1, "Historic")])[0])
        self.assertEqual(self.pending(), [])
        self.snapshot([(1, "Historic"), (2, "New")], bootstrap=True)
        self.assertEqual(self.pending(), [])
        self.assertFalse(self.snapshot([(1, "Historic"), (2, "New")])[0])
        self.assertEqual(self.pending(), [])

    def test_bootstrap_does_not_discard_existing_pending_work(self):
        self.queue()
        self.snapshot([(1, "Historic"), (2, "New"), (3, "Baseline")], bootstrap=True)
        self.assertEqual(self.pending(), [2])

    def test_queue_and_known_ids_rollback_together(self):
        self.snapshot([(1, "Historic")])
        with self.connect() as conn:
            conn.execute("CREATE TRIGGER fail_queue BEFORE INSERT ON pending_discovery "
                         "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
            conn.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic failure"):
            self.snapshot([(1, "Historic"), (2, "New")])
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT appid FROM known_comingsoon").fetchall(), [(1,)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM snapshot_runs").fetchone()[0], 1)
        self.assertEqual(self.pending(), [])

    def test_failed_fetch_resumes_after_same_snapshot_without_local_file(self):
        self.queue()
        self.discover(lambda *args: (False, None, None))
        self.assertEqual(self.pending(), [2])
        with patch.object(find_new, "OUT_DIR", self.temp.name), \
             patch.object(find_new, "fetch_comingsoon", return_value=(2, [(1, "Historic"), (2, "New")])), \
             patch.object(sys, "argv", ["find_new", "--stamp", "second"]):
            self.assertEqual(find_new.main(), 11)
        rc, fetched, exporter = self.discover(lambda *args: self.payload())
        self.assertEqual(rc, 0)
        self.assertEqual(fetched.call_args.args[0], 2)
        exporter.submit.assert_called_once_with(2)
        self.assertEqual(self.pending(), [2])  # Staging is not publication.

    def test_failed_media_refreshes_stale_urls_on_fresh_runner(self):
        self.queue()
        self.discover(lambda *args: self.payload(), stage_failure=True)
        self.assertEqual(self.pending(), [2])
        success, norm, raw = self.payload()
        fresh = json.loads(raw)
        fresh['2']['data']['screenshots'][0]['path_thumbnail'] = 'https://invalid.test/fresh.jpg'
        rc, fetched, exporter = self.discover(lambda *args: (success, norm, json.dumps(fresh)))
        self.assertEqual(rc, 0)
        fetched.assert_called_once()
        exporter.submit.assert_called_once_with(2)
        self.assertEqual(self.pending(), [2])
        with self.connect() as conn:
            self.assertIn('fresh.jpg', conn.execute(
                'SELECT screenshots FROM newly_added WHERE appid=2').fetchone()[0])
            self.assertEqual(conn.execute("SELECT discovered_on FROM newly_added WHERE appid=2").fetchone()[0],
                             "2026-10-15")
            recovery.ack_published(conn, [2])
            conn.commit()
        self.assertEqual(self.pending(), [])
        self.snapshot([(1, "Historic"), (2, "New")])
        self.assertEqual(self.pending(), [])

    def test_failed_refresh_keeps_job_without_staging_stale_media(self):
        self.queue()
        self.discover(lambda *args: self.payload(), stage_failure=True)
        _, fetched, exporter = self.discover(lambda *args: (False, None, None))
        fetched.assert_called_once()
        exporter.submit.assert_not_called()
        self.assertEqual(self.pending(), [2])
        with self.connect() as conn:
            self.assertIsNotNone(conn.execute('SELECT appid FROM newly_added WHERE appid=2').fetchone())

    def test_rejection_during_fetch_cannot_recreate_lead(self):
        self.queue()

        def reject_during_fetch(*args):
            with self.connect() as conn:
                conn.execute("DELETE FROM pending_discovery WHERE appid=2")
                conn.execute("DELETE FROM newly_added WHERE appid=2")
                conn.execute("DELETE FROM scrape_tracker WHERE appid=2")
            return self.payload()

        _, _, exporter = self.discover(reject_during_fetch)
        exporter.submit.assert_not_called()
        with self.connect() as conn:
            self.assertIsNone(conn.execute("SELECT appid FROM newly_added WHERE appid=2").fetchone())
            self.assertIsNone(conn.execute("SELECT appid FROM scrape_tracker WHERE appid=2").fetchone())
        self.snapshot([(1, "Historic"), (2, "New")])
        self.assertEqual(self.pending(), [])

    def test_ack_only_requested_ids_and_caller_controls_commit(self):
        self.queue()
        self.snapshot([(1, "Historic"), (2, "New"), (3, "Also new")])
        with self.connect() as conn:
            recovery.ack_published(conn, [2])
            conn.rollback()
        self.assertEqual(self.pending(), [2, 3])
        with self.connect() as conn:
            recovery.ack_published(conn, [2, 2, 999])
            conn.commit()
        self.assertEqual(self.pending(), [3])

    def test_cache_error_rolls_back_and_preserves_job(self):
        self.queue()
        with self.connect() as conn:
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                recovery.cache_if_pending(conn, 2, True, "raw", Mock(side_effect=RuntimeError("write failed")))
            self.assertFalse(conn.in_transaction)
        self.assertEqual(self.pending(), [2])

    def test_sample_does_not_consume_pending_or_persist_lead(self):
        self.queue()
        self.discover(lambda *args: self.payload(), extra_args=("--sample", "1"))
        self.assertEqual(self.pending(), [2])
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM newly_added").fetchone()[0], 0)

    def test_confirmed_released_or_non_game_finishes_without_media(self):
        for data in ({"type": "game", "release_date": {"coming_soon": False}},
                     {"type": "dlc", "release_date": {"coming_soon": True}}):
            with self.subTest(data=data):
                self.queue()
                with self.connect() as conn:
                    conn.execute("INSERT OR IGNORE INTO pending_discovery VALUES (2,'New','2026-10-15')")
                raw = json.dumps({"2": {"success": True, "data": data}})
                _, _, exporter = self.discover(lambda *args: (True, {}, raw))
                self.assertEqual(self.pending(), [])
                exporter.submit.assert_not_called()

    def test_bad_or_incomplete_source_evidence_does_not_finish(self):
        self.queue()
        for success, raw in ((False, None), (True, "bad json"), (True, "[]"),
                             (True, '{"2":{"success":true,"data":{}}}'),
                             (True, '{"2":{"success":false,"data":{"type":"dlc"}}}')):
            with self.subTest(raw=raw), self.connect() as conn:
                self.assertFalse(recovery.finish_ineligible(conn, 2, success, raw))
                self.assertEqual(recovery.pending_appids(conn), [2])

    def test_canonical_invalid_email_finishes_without_media(self):
        self.queue()
        success, norm, raw = self.payload()
        payload = json.loads(raw)
        payload['2']['data']['support_info']['email'] = 'not an email'
        raw = json.dumps(payload)
        _, _, exporter = self.discover(lambda *args: (success, norm, raw))
        exporter.submit.assert_not_called()
        self.assertEqual(self.pending(), [])
        with self.connect() as conn:
            self.assertEqual(conn.execute("SELECT scrape_status FROM scrape_tracker WHERE appid=2").fetchone()[0],
                             'invalid')

    def test_successful_source_without_screenshots_finishes_rejected_work(self):
        self.queue()
        success, norm, raw = self.payload()
        data = json.loads(raw)
        data['2']['data']['screenshots'] = []
        with self.connect() as conn:
            self.assertTrue(recovery.finish_ineligible(conn, 2, success, json.dumps(data)))
            self.assertEqual(conn.execute('SELECT appid FROM newly_added WHERE appid=2').fetchall(), [])
        self.assertEqual(self.pending(), [])
        self.snapshot([(1,'Historic'),(2,'New')])
        self.assertEqual(self.pending(), [])

    def test_failed_or_malformed_source_is_not_retired_as_missing_media(self):
        self.queue()
        for success, raw in [(False,None),(False,'{"2":{"success":false}}'),
                             (True,'{"2":{"success":true,"data":{}}}')]:
            with self.connect() as conn:
                self.assertFalse(recovery.finish_ineligible(conn,2,success,raw))
            self.assertEqual(self.pending(), [2])

    def test_invalid_without_canonical_row_does_not_finish(self):
        self.queue()
        with self.connect() as conn:
            recovery.finish_invalid(conn, 2)
        self.assertEqual(self.pending(), [2])

    def test_report_pending_reads_durable_jobs(self):
        self.queue()
        self.assertEqual(run_daily.report_pending(), 1)

    def test_partial_report_requires_every_pending_id_to_be_classified(self):
        self.queue()
        path = os.path.join(self.temp.name, 'sync.json')
        summary = os.path.join(self.temp.name, 'summary.md')
        for deferred, failed, expected in [([2], 0, True), ([], 0, False),
                                           ([3], 0, False), ([2], 1, False)]:
            with open(path, 'w') as f:
                json.dump({'uploaded': 335, 'deferred': deferred, 'failed': failed}, f)
            with patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': summary}):
                self.assertEqual(run_daily.report_partial(path), expected)
        self.assertEqual(self.pending(), [2])
        with open(summary) as f:
            self.assertIn('335 game(s) uploaded; 1 deferred', f.read())

    def test_stale_file_cannot_recreate_rejected_or_baseline_ids(self):
        self.queue()
        args = SimpleNamespace(sample=None, file="synthetic.txt", stamp=None)
        from unittest.mock import mock_open
        with self.connect() as conn, \
             patch.object(lead_discovery.os.path, "exists", return_value=True), \
             patch("builtins.open", mock_open(read_data="1 # baseline\n2 # new\n2\n")):
            self.assertEqual(lead_discovery.load_appids(args, conn, "stamp")[0], [2])
            conn.execute("DELETE FROM pending_discovery WHERE appid=2")
            conn.commit()
            self.assertEqual(lead_discovery.load_appids(args, conn, "stamp")[0], [])


class DailyRecoveryTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(run_daily, "report_pending", return_value=0)
        self.pending = p.start()
        self.addCleanup(p.stop)

    def test_expected_finder_results_all_drain_discovery_and_sync(self):
        for finder_rc in (0, 2, 10, 11):
            with self.subTest(finder_rc=finder_rc), \
                 patch.object(run_daily, "run", side_effect=[finder_rc, 0]) as run, \
                 patch.object(run_daily, "sync_media", return_value=0) as sync, \
                 patch.object(run_daily.os.path, "exists", return_value=False):
                self.assertEqual(run_daily.main(), 2 if finder_rc == 2 else 0)
                self.assertEqual([c.args[0] for c in run.call_args_list], ["find_new.py", "lead_discovery.py"])
                self.assertEqual(sync.call_args.args[0], "--report")

    def test_unexpected_finder_error_stops(self):
        with patch.object(run_daily, "run", return_value=7) as run, \
             patch.object(run_daily, "sync_media") as sync:
            self.assertEqual(run_daily.main(), 7)
            self.assertEqual(run.call_count, 1)
            sync.assert_not_called()

    def test_discovery_and_sync_failures_propagate(self):
        with patch.object(run_daily, "run", side_effect=[11, 4]), \
             patch.object(run_daily, "sync_media") as sync:
            self.assertEqual(run_daily.main(), 4)
            sync.assert_not_called()
        with patch.object(run_daily, "run", side_effect=[11, 0]), \
             patch.object(run_daily, "sync_media", return_value=5):
            self.assertEqual(run_daily.main(), 5)

    def test_sync_success_with_unfinished_jobs_is_not_complete(self):
        self.pending.return_value = 1
        with patch.object(run_daily, "run", side_effect=[11, 0]), \
             patch.object(run_daily, "sync_media", return_value=0), \
             patch.object(run_daily.os.path, "exists", return_value=False):
            self.assertEqual(run_daily.main(), 1)

    def test_classified_media_deferral_is_warning_but_finder_failure_is_not(self):
        self.pending.return_value = 1
        for finder, expected in [(11, 0), (2, 2)]:
            with patch.object(run_daily, 'run', side_effect=[finder, 0]), \
                 patch.object(run_daily, 'sync_media', return_value=0), \
                 patch.object(run_daily, 'report_partial', return_value=True), \
                 patch.object(run_daily.os.path, 'exists', return_value=False):
                self.assertEqual(run_daily.main(), expected)


if __name__ == "__main__":
    unittest.main()
