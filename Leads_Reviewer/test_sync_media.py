import io
import json
import os
import sys
import sqlite3
import unittest
import tempfile
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync_media


class SyncDeferralTest(unittest.TestCase):
    def test_missing_media_is_not_uploaded_or_acknowledged_but_r2_errors_still_fail(self):
        for upload_error in (None, RuntimeError('R2 unavailable')):
            with self.subTest(upload_error=upload_error), tempfile.TemporaryDirectory() as root:
                folders=[]
                for appid in (1,2):
                    folder=os.path.join(root,f'Game_{appid}');os.mkdir(folder)
                    with open(os.path.join(folder,f'{appid}.json'),'w') as f:json.dump({},f)
                    folders.append((appid,folder))
                with open(os.path.join(folders[0][1],'.media-unavailable.json'),'w') as f:
                    json.dump({'appid':1,'http_status':404},f)
                report=os.path.join(root,'report.json')
                with patch.object(sync_media.media_store,'write_enabled',return_value=True), \
                     patch.object(sync_media.media_store,'_client'), \
                     patch.object(sync_media,'_lead_folders',return_value=folders), \
                     patch.object(sync_media.media_store,'upload_dir',side_effect=upload_error) as upload, \
                     patch.object(sync_media.media_store,'update_index',return_value={}) as index, \
                     patch.object(sync_media.pipeline,'_rw',return_value=Mock()), \
                     patch.object(sync_media.discovery_recovery,'init_db'), \
                     patch.object(sync_media.discovery_recovery,'ack_published') as ack, \
                     patch.object(sync_media,'cleanup_r2'):
                    self.assertEqual(sync_media.main(['--report',report]),1 if upload_error else 0)
                    self.assertEqual(upload.call_count,1)
                    self.assertEqual(upload.call_args.args[1],2)
                    merged=index.call_args.args[0]({})
                    self.assertNotIn('1',merged)
                    if upload_error:ack.assert_not_called()
                    else:self.assertEqual(list(ack.call_args.args[1]),['2'])
                with open(report) as f:result=json.load(f)
                self.assertEqual(result['deferred'],[1])
                self.assertEqual(result['failed'],int(upload_error is not None))


class CleanupTest(unittest.TestCase):
    def test_queue_rechecks_state_atomically_and_requires_deletion_guards(self):
        class Connection(sqlite3.Connection):
            def close(self):
                pass
        conn = sqlite3.connect(':memory:', factory=Connection)
        self.addCleanup(lambda: sqlite3.Connection.close(conn))
        conn.executescript("CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY, Mail_status TEXT);"
                           "CREATE TABLE newly_added(appid INTEGER PRIMARY KEY);"
                           "CREATE TABLE media_cleanup_jobs(appid INTEGER PRIMARY KEY, folder TEXT);"
                           "INSERT INTO scrape_tracker VALUES(1,'Pending'),(2,'Sent');")
        with patch.object(sync_media.pipeline, '_rw', return_value=conn):
            with self.assertRaisesRegex(RuntimeError, 'migrate-cleanup'):
                sync_media._queue_cleanup([(1, 'Game_1')])
            for name, table in [('cleanup_blocks_scrape_tracker_insert', 'scrape_tracker'),
                                ('cleanup_blocks_newly_added_insert', 'newly_added'),
                                ('cleanup_blocks_reactivation', 'scrape_tracker')]:
                conn.execute(f'CREATE TRIGGER {name} BEFORE INSERT ON {table} BEGIN SELECT 1; END')
            self.assertEqual(sync_media._queue_cleanup([(1,'Game_1'),(2,'Game_2'),(3,'Game_3')]), 2)
        self.assertEqual(conn.execute('SELECT appid FROM media_cleanup_jobs ORDER BY appid').fetchall(), [(2,), (3,)])

    def test_cleanup_preserves_concurrent_index_entries_and_unindexed_active_media(self):
        index = {"1": "keep_1", "2": "old_2"}
        written = {}
        deleted = []

        class Client:
            def get_object(self, **kwargs):
                return {"Body": io.BytesIO(json.dumps(index).encode())}

            def get_paginator(self, _name):
                return self

            def paginate(self, **kwargs):
                return [{"Contents": [
                    {"Key": "keep_1/image.jpg", "Size": 1},
                    {"Key": "old_2/image.jpg", "Size": 1},
                    {"Key": "uploading_3/image.jpg", "Size": 1},
                ]}]

            def delete_objects(self, **kwargs):
                deleted.extend(o["Key"] for o in kwargs["Delete"]["Objects"])
                return {}

        def update(mutate, **kwargs):
            written.update(mutate({"1": "keep_1", "2": "replacement_2", "4": "new_4"}))

        with patch.object(sync_media, "_active_appids", return_value={1, 3}), \
             patch.object(sync_media, "_queue_cleanup", return_value=1) as queue, \
             patch.object(sync_media.media_store, "update_index", side_effect=update), \
             patch.object(sync_media.media_store, "update_irrelevant"):
            sync_media.cleanup_r2(Client(), apply=True)
        self.assertEqual(deleted, [])
        self.assertEqual(written, {})
        queue.assert_called_once_with([(2, 'old_2')])

    def test_cleanup_deletes_unowned_objects_before_rewriting_index(self):
        events = []
        index = {"1": "keep_1", "2": "drop_2", "3": "missing_3"}

        class Paginator:
            def paginate(self, **_kwargs):
                return [{"Contents": [
                    {"Key": "index.json", "Size": 20},
                    {"Key": "keep_1/manifest.json", "Size": 10},
                    {"Key": "drop_2/manifest.json", "Size": 11},
                    {"Key": "orphan_4/image.jpg", "Size": 12},
                ]}]

        class Client:
            def get_object(self, **kwargs):
                value = index if kwargs["Key"] == "index.json" else []
                return {"Body": io.BytesIO(json.dumps(value).encode()), "ETag": '"v1"'}

            def get_paginator(self, _name):
                return Paginator()

            def delete_objects(self, **kwargs):
                events.append(("delete", [item["Key"] for item in kwargs["Delete"]["Objects"]]))
                return {}

            def put_object(self, **kwargs):
                events.append(("put", kwargs["Key"], json.loads(kwargs["Body"])))
                return {}

        with patch.object(sync_media, "_active_appids", return_value={1}), \
             patch.object(sync_media, "_queue_cleanup", return_value=3) as queue:
            self.assertEqual(sync_media.cleanup_r2(Client(), apply=True), 2)
        self.assertEqual(events, [])
        queue.assert_called_once_with([(2, 'drop_2'), (3, 'missing_3'), (4, 'orphan_4')])


if __name__ == "__main__":
    unittest.main()
