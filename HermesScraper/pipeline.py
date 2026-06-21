"""Pipeline contract between System 1 (newly_added) and the Hermes scraper.

Hermes imports this. It is the ONLY way Hermes touches the DB:
  - reads of newly_added go through a read-only connection (cannot write it)
  - writes go ONLY to scrape_tracker

    import pipeline
    for appid in pipeline.get_pending():
        lead = pipeline.read_lead(appid)          # full newly_added row, read-only
        ... scrape ...
        pipeline.write_result(appid, scrape_status="SCRAPED", emails="a@b.com", ...)

scrape_tracker carries the game data (copied from newly_added by a trigger) PLUS the
scraped results, so it is self-contained for export.
"""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
# pipeline.py lives in HermesScraper/; the DB lives in the sibling System 1 folder
DB_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "claude Steam Search", "cache.sqlite"))
TIMEOUT = 15  # seconds to wait on a locked DB before raising

STATUSES = ("pending", "seeded", "SCRAPED", "no_email", "failed")

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
     steam_url, website, support_info, developers, publishers, genres)
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
       FROM json_each(coalesce(NEW.genres,'[]')))
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scrape_tracker (
                appid          INTEGER PRIMARY KEY,
                game_name      TEXT,
                short_descript TEXT,
                scrape_status  TEXT NOT NULL DEFAULT 'pending',
                country        TEXT,
                emails         TEXT,
                engine         TEXT,
                steam_url      TEXT,
                website        TEXT,
                support_info   TEXT,
                developers     TEXT,
                publishers     TEXT,
                genres         TEXT,
                scraped_at     TEXT
            )"""
        )
        # migrate older DBs whose table predates a column (IF NOT EXISTS won't add it)
        have = {c[1] for c in conn.execute("PRAGMA table_info(scrape_tracker)")}
        if "support_info" not in have:
            conn.execute("ALTER TABLE scrape_tracker ADD COLUMN support_info TEXT")
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
            "developers, publishers, genres FROM newly_added").fetchall()
    with closing(_rw()) as conn:
        have = {r[0] for r in conn.execute("SELECT appid FROM scrape_tracker")}
        added = 0
        for appid, name, brief, website, support, devs, pubs, genres in src:
            if appid in have:
                continue
            email = _support_email(support)            # Steam-listed email, if any
            status = "seeded" if email else "pending"   # seeded skips Hermes
            conn.execute(
                f"INSERT INTO scrape_tracker ({','.join(SEED_COLS)}, emails, scrape_status) "
                f"VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (appid, name, brief, f"https://store.steampowered.com/app/{appid}/",
                 website or "", support or "", _names(devs), _names(pubs), _genres(genres),
                 email or None, status),
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


def read_lead(appid):
    """The complete newly_added row for one appid, as a {column: value} dict
    (read-only). Returns None if the appid isn't in newly_added."""
    with closing(_ro()) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(newly_added)")]
        row = conn.execute("SELECT * FROM newly_added WHERE appid=?", (appid,)).fetchone()
    return dict(zip(cols, row)) if row is not None else None


def write_result(appid, *, scrape_status, emails=None, country=None, engine=None,
                 website=None):
    """Write scrape results for ONE appid into scrape_tracker (and nowhere else).
    Only the fields you pass are touched; seeded game data is left intact.
    Auto-stamps scraped_at."""
    if scrape_status not in STATUSES:
        raise ValueError(f"bad status {scrape_status!r}; expected one of {STATUSES}")
    _ensure()
    now = datetime.now(IST).isoformat(timespec="seconds")
    sets = ["scrape_status=?", "scraped_at=?"]
    vals = [scrape_status, now]
    for col, val in (("emails", emails), ("country", country),
                     ("engine", engine), ("website", website)):
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


if __name__ == "__main__":
    added = seed_pending()
    print(f"seeded {added} new pending; {len(get_pending())} leads in the queue")
