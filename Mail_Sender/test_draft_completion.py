import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from unittest.mock import patch

import draft_cloud


class DraftCompletionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'test.sqlite')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in [('TURSO_URL',''), ('DB_PATH',self.path), ('_ensured',True)]:
            self.stack.enter_context(patch.object(draft_cloud.pipeline,name,value))
        self.sleep = self.stack.enter_context(patch.object(draft_cloud.time,'sleep'))
        self.reconnect = self.stack.enter_context(patch.object(draft_cloud.pipeline,'reconnect'))
        with closing(sqlite3.connect(self.path)) as c:
            c.executescript("CREATE TABLE scrape_tracker(appid INTEGER PRIMARY KEY,Mail_status TEXT,mail_template INTEGER,emails TEXT,scrape_status TEXT); INSERT INTO scrape_tracker VALUES(1,'Writing',NULL,'test@example.invalid','seeded');")

    def row(self):
        with closing(sqlite3.connect(self.path)) as c:
            return c.execute('SELECT Mail_status,mail_template FROM scrape_tracker WHERE appid=1').fetchone()

    def test_connection_reset_before_and_after_commit_recovers_without_rewriting_draft(self):
        for committed in (False,True):
            with self.subTest(committed=committed):
                with closing(sqlite3.connect(self.path)) as c:
                    c.execute("UPDATE scrape_tracker SET Mail_status='Writing',mail_template=NULL")
                    c.commit()
                opens = []
                class Fault:
                    def __init__(self, conn): self.conn=conn
                    def __getattr__(self, name): return getattr(self.conn,name)
                    def commit(self):
                        if committed: self.conn.commit()
                        raise RuntimeError('Hrana: cursor error: Connection reset by peer (os error 104)')
                def connect():
                    c=sqlite3.connect(self.path); opens.append(c)
                    return Fault(c) if len(opens)==1 else c
                with patch.object(draft_cloud.pipeline,'_rw',side_effect=connect), \
                     patch.object(draft_cloud.media_store,'write_manifest') as write, \
                     patch.object(draft_cloud,'_draft') as infer:
                    draft_cloud._complete_draft(1,3,{'mail':'saved','mail_template':3})
                self.assertEqual(self.row(),('Drafted',3))
                self.assertEqual(len(opens),2)
                write.assert_not_called(); infer.assert_not_called()

    def test_retry_is_bounded_and_permanent_errors_are_not_retried(self):
        for error,count in [(RuntimeError('connection reset'),4),(ValueError('row changed'),1)]:
            with patch.object(draft_cloud.pipeline,'complete_saved_draft',side_effect=error) as complete:
                with self.assertRaises(type(error)):
                    draft_cloud._complete_draft(1,3,{})
                self.assertEqual(complete.call_count,count)
            self.assertEqual(self.row(),('Writing',None))

    def test_already_advanced_state_is_confirmed_without_rewinding(self):
        for state in ('Drafted','Scheduled','Sending','Sent','Replied'):
            with closing(sqlite3.connect(self.path)) as c:
                c.execute('UPDATE scrape_tracker SET Mail_status=?,mail_template=3',(state,));c.commit()
            draft_cloud._complete_draft(1,3,{})
            self.assertEqual(self.row(),(state,3))
            with self.assertRaises(ValueError): draft_cloud._complete_draft(1,4,{})

    def test_mcp_recipient_guard_and_deleted_row_are_preserved(self):
        submission={'email':'changed@example.invalid','scrape_status':'seeded','selected_template':''}
        with patch.object(draft_cloud.media_store,'check_draft_inputs'):
            with self.assertRaises(ValueError): draft_cloud._complete_draft(1,3,{'draft_submission':submission})
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('DELETE FROM scrape_tracker');c.commit()
        with self.assertRaises(ValueError): draft_cloud._complete_draft(1,3,{})
        self.assertIsNone(self.row())

    def test_saved_draft_run_finishes_without_calling_model_or_republishing(self):
        with patch.object(draft_cloud,'_load_templates',return_value=[(3,'template')]), \
             patch.object(draft_cloud.media_store,'fetch_index',return_value={'1':'Synthetic_1'}), \
             patch.object(draft_cloud.media_store,'fetch_manifest',return_value={'mail':'saved','mail_template':3}), \
             patch.object(draft_cloud.pipeline,'mail_status_appids',return_value=[1]), \
             patch.object(draft_cloud,'_draft') as infer, \
             patch.object(draft_cloud.media_store,'write_manifest') as write:
            self.assertEqual(draft_cloud.run_queue(None,'unused'),0)
        self.assertEqual(self.row(),('Drafted',3))
        infer.assert_not_called();write.assert_not_called()
