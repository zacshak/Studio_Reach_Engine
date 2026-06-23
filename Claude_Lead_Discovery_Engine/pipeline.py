"""Pipeline contract between System 1 (newly_added) and the Hermes scraper.

This is the IMPLEMENTATION. Hermes never imports it directly — it goes through
HermesScraper/scraper_interface.py, which re-exports only get_pending / read_lead
/ write_result and hides everything below (schema, triggers, connections, seeding).

The contract:
  - reads of newly_added go through a read-only connection (cannot write it)
  - writes go ONLY to scrape_tracker
scrape_tracker carries the game data (copied from newly_added by a trigger) PLUS the
scraped results, so it is self-contained for export.
"""
import json
import os
import sqlite3
from contextlib import closing

# pipeline.py lives in the System 1 folder, alongside the DB it owns
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.sqlite")
TIMEOUT = 15  # seconds to wait on a locked DB before raising

STATUSES = ("pending", "seeded", "SCRAPED", "no_email", "failed")
# outreach state; Mail_status defaults to 'Pending'. Review moves it Pending ->
# Writing (accepted) ; the mailer later moves Writing -> Scheduled -> Sent -> Replied.
MAIL_STATUSES = ("Pending", "Writing", "Scheduled", "Sent", "Replied")

# Canonical scrape_tracker schema — ONE definition, used both for fresh DBs and
# for the rebuild migration below (SQLite's ALTER can only append a column, so
# positioning Mail_status after short_descript requires a table rebuild).
_SCHEMA = """(
    appid          INTEGER PRIMARY KEY,
    game_name      TEXT,
    short_descript TEXT,
    Mail_status    TEXT NOT NULL DEFAULT 'Pending',
    mail_template  INTEGER,
    scrape_status  TEXT NOT NULL DEFAULT 'pending',
    emails         TEXT,
    steam_url      TEXT,
    website        TEXT,
    support_info   TEXT,
    developers     TEXT,
    publishers     TEXT,
    genres         TEXT,
    added_at       TEXT,
    sent_at        TEXT
)"""
# columns carried over on rebuild (all but Mail_status / mail_template, which take
# their defaults)
_REBUILD_COLS = ("appid, game_name, short_descript, scrape_status, emails, "
                 "steam_url, website, support_info, developers, publishers, "
                 "genres, added_at")
# canonical column order (matches _SCHEMA) — used to detect/fix layout drift
_SCHEMA_COLS = ("appid", "game_name", "short_descript", "Mail_status",
                "mail_template", "scrape_status", "emails", "steam_url", "website",
                "support_info", "developers", "publishers", "genres", "added_at",
                "sent_at")

# tracker columns seeded from newly_added (the rest are filled by the scraper)
SEED_COLS = ("appid", "game_name", "short_descript", "steam_url",
             "website", "support_info", "developers", "publishers", "genres")

# newly_added columns the trigger reads. System 1 adds these lazily (only when a
# lead carries that field), so the trigger could reference a not-yet-created
# column and abort System 1's insert at fire time. init_tracker guarantees they
# exist first. (TEXT is harmless: System 1 stores everything as TEXT too.)
_TRIGGER_NEEDS = ("name", "short_description", "website", "support_info",
                  "developers", "publishers", "genres")

# Auto-sync trigger. Uses INSERT ... WHERE NOT EXISTS (NOT "INSERT OR IGNORE"):
# an outer `INSERT OR REPLACE` on newly_added (cache_put does this on re-fetch)
# would override an inner IGNORE and wipe scrape progress — the NOT EXISTS guard
# avoids any conflict, so an existing tracker row is left completely untouched.
# A lead whose support_info already carries an email is born 'seeded' with that
# email pre-filled, so Hermes never has to scrape it. The rest start 'pending'.
_TRIGGER_SQL = """
CREATE TRIGGER trg_sync_scrape_tracker
AFTER INSERT ON newly_added
BEGIN
  INSERT INTO scrape_tracker
    (appid, game_name, short_descript, scrape_status, emails,
     steam_url, website, support_info, developers, publishers, genres, added_at)
  SELECT
    NEW.appid,
    NEW.name,
    NEW.short_description,
    CASE WHEN nullif(json_extract(coalesce(NEW.support_info,'{}'),'$.email'),'') IS NOT NULL
         THEN 'seeded' ELSE 'pending' END,
    nullif(json_extract(coalesce(NEW.support_info,'{}'),'$.email'),''),
    'https://store.steampowered.com/app/' || NEW.appid || '/',
    NEW.website,
    NEW.support_info,
    (SELECT group_concat(value, ', ')
       FROM json_each(coalesce(NEW.developers,'[]'))),
    (SELECT group_concat(value, ', ')
       FROM json_each(coalesce(NEW.publishers,'[]'))),
    (SELECT group_concat(json_extract(value,'$.description'), ', ')
       FROM json_each(coalesce(NEW.genres,'[]'))),
    NEW.fetched_at
  WHERE NOT EXISTS (SELECT 1 FROM scrape_tracker WHERE appid = NEW.appid);
END
"""

_ensured = False  # init_tracker() runs once per process, not on every call


def _rw():
    return sqlite3.connect(DB_PATH, timeout=TIMEOUT)


def _ro():
    # read-only handle: any write raises sqlite3.OperationalError
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=TIMEOUT)


def init_tracker():
    """Create the tracker table + auto-sync trigger; enable WAL. Idempotent."""
    global _ensured
    with closing(_rw()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        conn.execute(f"CREATE TABLE IF NOT EXISTS scrape_tracker {_SCHEMA}")
        # migrate older DBs whose table predates a column (IF NOT EXISTS won't add it)
        have = {c[1] for c in conn.execute("PRAGMA table_info(scrape_tracker)")}
        if "support_info" not in have:
            conn.execute("ALTER TABLE scrape_tracker ADD COLUMN support_info TEXT")
        if "scraped_at" in have and "added_at" not in have:
            conn.execute("ALTER TABLE scrape_tracker RENAME COLUMN scraped_at TO added_at")
        # add Mail_status IN POSITION (after short_descript) via a one-time rebuild —
        # ALTER ADD COLUMN can only append, so we recreate the table from _SCHEMA and
        # copy the existing rows over (Mail_status takes its 'Pending' default). The
        # trigger must be dropped first — it references scrape_tracker, so the table
        # can't be dropped while it exists; it's recreated below regardless.
        if "Mail_status" not in {c[1] for c in conn.execute("PRAGMA table_info(scrape_tracker)")}:
            conn.executescript(
                "DROP TRIGGER IF EXISTS trg_sync_scrape_tracker;"
                "DROP TABLE IF EXISTS _scrape_tracker_new;"
                f"CREATE TABLE _scrape_tracker_new {_SCHEMA};"
                f"INSERT INTO _scrape_tracker_new ({_REBUILD_COLS}) "
                f"SELECT {_REBUILD_COLS} FROM scrape_tracker;"
                "DROP TABLE scrape_tracker;"
                "ALTER TABLE _scrape_tracker_new RENAME TO scrape_tracker;")
        # drop the unused country/engine columns; add mail_template (the chosen mail
        # variant, set on mail approval). ALTER DROP/ADD COLUMN needs SQLite >= 3.35.
        cols = {c[1] for c in conn.execute("PRAGMA table_info(scrape_tracker)")}
        for dead in ("country", "engine"):
            if dead in cols:
                conn.execute(f"ALTER TABLE scrape_tracker DROP COLUMN {dead}")
        if "mail_template" not in cols:
            conn.execute("ALTER TABLE scrape_tracker ADD COLUMN mail_template INTEGER")
        if "sent_at" not in cols:
            conn.execute("ALTER TABLE scrape_tracker ADD COLUMN sent_at TEXT")
        # fix column order if it drifted (ALTER only appends, so mail_template lands
        # last). Rebuild from _SCHEMA, preserving every existing value. No-op once aligned.
        ordered = [c[1] for c in conn.execute("PRAGMA table_info(scrape_tracker)")]
        if ordered != list(_SCHEMA_COLS):
            shared = ", ".join(c for c in _SCHEMA_COLS if c in ordered)
            conn.executescript(
                "DROP TRIGGER IF EXISTS trg_sync_scrape_tracker;"
                "DROP TABLE IF EXISTS _scrape_tracker_new;"
                f"CREATE TABLE _scrape_tracker_new {_SCHEMA};"
                f"INSERT INTO _scrape_tracker_new ({shared}) "
                f"SELECT {shared} FROM scrape_tracker;"
                "DROP TABLE scrape_tracker;"
                "ALTER TABLE _scrape_tracker_new RENAME TO scrape_tracker;")
        # guarantee the columns the trigger reads exist on newly_added, so an insert
        # can never abort with "no such column: NEW.x" (see _TRIGGER_NEEDS)
        na = {c[1] for c in conn.execute("PRAGMA table_info(newly_added)")}
        if na:  # newly_added exists (System 1 owns it); add only what's missing
            for col in _TRIGGER_NEEDS:
                if col not in na:
                    conn.execute(f'ALTER TABLE newly_added ADD COLUMN "{col}" TEXT')
        # recreate the trigger so the latest definition always wins
        conn.execute("DROP TRIGGER IF EXISTS trg_sync_scrape_tracker")
        conn.execute(_TRIGGER_SQL)
        conn.commit()
    _ensured = True


def _ensure():
    if not _ensured:
        init_tracker()


def _jload(v):
    try:
        return json.loads(v) if isinstance(v, str) else v
    except json.JSONDecodeError:
        return None


def _names(v):
    """developers/publishers JSON ['A','B'] -> 'A, B'."""
    return ", ".join(str(x) for x in (_jload(v) or []))


def _genres(v):
    """genres JSON [{id,description}] -> 'Action, Indie'."""
    return ", ".join(g.get("description", "") for g in (_jload(v) or [])
                     if isinstance(g, dict))


def _support_email(v):
    """support_info JSON {'email','url'} -> the email (or '' if none)."""
    d = _jload(v)
    return (d.get("email") or "").strip() if isinstance(d, dict) else ""


def seed_pending():
    """Backfill: add a tracker row for every newly_added lead not already tracked.
    The trigger handles new inserts going forward; this catches up existing rows.
    Idempotent. Returns count added."""
    _ensure()
    with closing(_ro()) as ro:
        src = ro.execute(
            "SELECT appid, name, short_description, website, support_info, "
            "developers, publishers, genres, fetched_at FROM newly_added").fetchall()
    with closing(_rw()) as conn:
        have = {r[0] for r in conn.execute("SELECT appid FROM scrape_tracker")}
        added = 0
        for appid, name, brief, website, support, devs, pubs, genres, fetched_at in src:
            if appid in have:
                continue
            email = _support_email(support)            # Steam-listed email, if any
            status = "seeded" if email else "pending"   # seeded skips Hermes
            conn.execute(
                f"INSERT INTO scrape_tracker ({','.join(SEED_COLS)}, emails, scrape_status, added_at) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (appid, name, brief, f"https://store.steampowered.com/app/{appid}/",
                 website or "", support or "", _names(devs), _names(pubs), _genres(genres),
                 email or None, status, fetched_at),
            )
            added += 1
        conn.commit()
    return added


def get_pending(limit=None):
    """Appids whose scrape_status is 'pending'. Hermes's work queue."""
    _ensure()
    with closing(_ro()) as conn:
        q = "SELECT appid FROM scrape_tracker WHERE scrape_status='pending' ORDER BY appid"
        if limit:
            rows = conn.execute(q + " LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(q).fetchall()
    return [r[0] for r in rows]


def pending_websites():
    """Website URLs of every 'pending' lead that actually has one (non-empty),
    as a flat list. The scrape target list for fishing emails off studio sites."""
    return [w for _, w in pending_leads()]


def pending_leads():
    """(appid, website) for every 'pending' lead that has a non-empty website.
    Lets a caller map a scraped URL back to the lead it belongs to."""
    _ensure()
    with closing(_ro()) as conn:
        rows = conn.execute(
            "SELECT appid, website FROM scrape_tracker "
            "WHERE scrape_status='pending' AND website IS NOT NULL AND website<>'' "
            "ORDER BY appid").fetchall()
    return [(r[0], r[1]) for r in rows]


def read_lead(appid):
    """The complete newly_added row for one appid, as a {column: value} dict
    (read-only). Returns None if the appid isn't in newly_added."""
    with closing(_ro()) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(newly_added)")]
        row = conn.execute("SELECT * FROM newly_added WHERE appid=?", (appid,)).fetchone()
    return dict(zip(cols, row)) if row is not None else None


def write_result(appid, *, scrape_status, emails=None, website=None):
    """Write scrape results for ONE appid into scrape_tracker (and nowhere else).
    Only the fields you pass are touched; seeded game data (incl. added_at) is
    left intact."""
    if scrape_status not in STATUSES:
        raise ValueError(f"bad status {scrape_status!r}; expected one of {STATUSES}")
    _ensure()
    sets = ["scrape_status=?"]
    vals = [scrape_status]
    for col, val in (("emails", emails), ("website", website)):
        if val is not None:
            sets.append(f"{col}=?")
            vals.append(val)
    vals.append(appid)
    with closing(_rw()) as conn:
        cur = conn.execute(
            f"UPDATE scrape_tracker SET {','.join(sets)} WHERE appid=?", vals)
        if cur.rowcount == 0:  # appid not tracked yet -> create then set
            conn.execute("INSERT INTO scrape_tracker (appid) VALUES (?)", (appid,))
            conn.execute(
                f"UPDATE scrape_tracker SET {','.join(sets)} WHERE appid=?", vals)
        conn.commit()


# --- review actions (used by the Leads Reviewer) ------------------------
def mail_status_appids(status):
    """Appids whose Mail_status equals `status` (e.g. 'Pending'). Read-only."""
    _ensure()
    with closing(_ro()) as conn:
        return [r[0] for r in conn.execute(
            "SELECT appid FROM scrape_tracker WHERE Mail_status=? ORDER BY appid",
            (status,))]


def set_mail_status(appid, status):
    """Set one lead's Mail_status (e.g. 'Writing' on acceptance)."""
    if status not in MAIL_STATUSES:
        raise ValueError(f"bad mail status {status!r}; expected one of {MAIL_STATUSES}")
    _ensure()
    with closing(_rw()) as conn:
        conn.execute("UPDATE scrape_tracker SET Mail_status=? WHERE appid=?",
                     (status, int(appid)))
        conn.commit()


def get_emails(appid):
    """The emails string stored for one lead (comma-separated, '' if none)."""
    _ensure()
    with closing(_ro()) as conn:
        row = conn.execute("SELECT emails FROM scrape_tracker WHERE appid=?",
                           (int(appid),)).fetchone()
    return (row[0] or "") if row else ""


def set_mail_template(appid, template):
    """Record which mail variant was approved (the N in mail_<appid>_<N>.txt)."""
    _ensure()
    with closing(_rw()) as conn:
        conn.execute("UPDATE scrape_tracker SET mail_template=? WHERE appid=?",
                     (int(template), int(appid)))
        conn.commit()


def get_mail_template(appid):
    """The approved mail variant N for a lead, or None."""
    _ensure()
    with closing(_ro()) as conn:
        row = conn.execute("SELECT mail_template FROM scrape_tracker WHERE appid=?",
                           (int(appid),)).fetchone()
    return row[0] if row else None


def delete_newly_added(appid):
    """Drop a lead from newly_added ONLY (its scrape_tracker row is kept)."""
    _ensure()
    with closing(_rw()) as conn:
        conn.execute("DELETE FROM newly_added WHERE appid=?", (int(appid),))
        conn.commit()


def mark_sent(appid):
    """Mail was sent: Mail_status -> 'Sent', stamp sent_at (UTC) for the daily cap,
    and drop the newly_added row (the scrape_tracker row stays as the record)."""
    _ensure()
    with closing(_rw()) as conn:
        conn.execute("UPDATE scrape_tracker SET Mail_status='Sent', "
                     "sent_at=CURRENT_TIMESTAMP WHERE appid=?", (int(appid),))
        conn.execute("DELETE FROM newly_added WHERE appid=?", (int(appid),))
        conn.commit()


def sent_today():
    """How many mails were sent today (UTC) — enforces the daily cap across reruns."""
    _ensure()
    with closing(_ro()) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM scrape_tracker "
            "WHERE sent_at IS NOT NULL AND date(sent_at)=date('now')").fetchone()[0]


def delete_lead(appid):
    """Permanently remove a rejected lead from BOTH tables — scrape_tracker and
    the canonical newly_added cache — so it won't reappear (a re-fetch would
    otherwise re-insert newly_added and the trigger would re-create the row)."""
    _ensure()
    with closing(_rw()) as conn:
        conn.execute("DELETE FROM scrape_tracker WHERE appid=?", (int(appid),))
        conn.execute("DELETE FROM newly_added WHERE appid=?", (int(appid),))
        conn.commit()


if __name__ == "__main__":
    added = seed_pending()
    print(f"seeded {added} new pending; {len(get_pending())} leads in the queue")
