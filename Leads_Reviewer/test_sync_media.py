import io
import json
import os
import sys
import sqlite3
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sync_media


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
