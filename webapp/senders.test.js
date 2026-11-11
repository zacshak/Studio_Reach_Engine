import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {VIEW_SQL} from './worker.js';

test('senders view: cap, sent in the last 24h only, status', () => {
  const db = new DatabaseSync(':memory:');
  db.exec(`CREATE TABLE sender_accounts (email TEXT PRIMARY KEY, provider TEXT, daily_cap INTEGER, display_name TEXT,
             last_status TEXT, last_checked_at TEXT, added_at TEXT);
           CREATE TABLE mail_send_log (id INTEGER PRIMARY KEY, appid INTEGER, sender TEXT, sent_at TEXT DEFAULT CURRENT_TIMESTAMP);
           INSERT INTO sender_accounts (email, provider, daily_cap, last_status) VALUES
             ('a@x.test','google',10,'ok'), ('z@x.test','google',0,NULL), ('m@x.test','microsoft',10,'Microsoft sign-in not set up yet');
           INSERT INTO mail_send_log (appid, sender) VALUES (1,'a@x.test'), (2,'a@x.test');
           INSERT INTO mail_send_log (appid, sender, sent_at) VALUES (3,'a@x.test', datetime('now','-2 days'));`);
  const rows = db.prepare(VIEW_SQL.senders).all().map(r => Object.values(r));
  assert.deepEqual(rows.map(r => [r[0], r[2], r[5]]), [['a@x.test', 10, 2], ['m@x.test', 10, 0], ['z@x.test', 0, 0]]);
  db.close();
});
