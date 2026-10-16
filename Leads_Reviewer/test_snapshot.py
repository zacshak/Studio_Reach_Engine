import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import snap_db


class SnapshotTest(unittest.TestCase):
    def test_snapshot_uses_one_read_transaction_across_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, 'source.sqlite')
            out = os.path.join(directory, 'out.sqlite')
            reader = sqlite3.connect(source)
            writer = sqlite3.connect(source)
            self.addCleanup(reader.close)
            self.addCleanup(writer.close)
            reader.executescript('PRAGMA journal_mode=WAL; CREATE TABLE a(v); CREATE TABLE b(v); INSERT INTO a VALUES(1); INSERT INTO b VALUES(1);')
            class Source:
                def execute(self, query):
                    result = reader.execute(query)
                    if query == 'SELECT * FROM "a"':
                        writer.execute('UPDATE b SET v=2')
                        writer.commit()
                    return result
                def rollback(self):
                    reader.rollback()
            snap_db.snapshot(Source(), out, 'test')
            with sqlite3.connect(out) as copied:
                self.assertEqual(copied.execute('SELECT v FROM b').fetchone(), (1,))
            copied.close()
            reader.close()
            writer.close()
