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
import sys
import threading
import time
from contextlib import closing
from email.errors import HeaderParseError
from email.headerregistry import Address

try:
    import libsql                      # Turso/libSQL client (sqlite3 DBAPI drop-in)
except ImportError:                    # not installed locally -> local sqlite path only
    libsql = None

# pipeline.py lives in the System 1 folder, alongside the DB it owns
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.sqlite")
TIMEOUT = 15  # seconds to wait on a locked DB before raising


def _load_env():
    """Pull KEY=VALUE from the repo-root .env into os.environ (local convenience;
    in the cloud the host injects these as real env vars). Doesn't override existing."""
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
# When TURSO_DATABASE_URL is set, the whole pipeline talks to the remote Turso DB
# (cloud-shared). When it's empty, everything falls back to the local cache.sqlite —
# byte-identical to the pre-Turso behaviour, so local dev needs no creds.
TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")

STATUSES = ("pending", "seeded", "scraped", "no_email", "invalid", "failed")
# 'scraped' = email recovered by scraping the studio site (higher provenance than
# 'seeded', which is Steam-provided). Treated exactly like 'seeded' downstream:
# it has an email, so it's out of the 'pending' work queue and into the mail flow.
# 'invalid' means a non-empty email-like value failed validation. It is quarantined
# from the outreach state machine and hidden from user-facing queues.
# Visible outreach states, in dashboard order. 'Sending' is an internal atomic
# claim state and is intentionally not exposed as a user-facing status.
MAIL_STATUSES = ("Pending", "Invalid", "Writing", "Drafted", "Scheduled", "Sent", "Replied")
INTERNAL_MAIL_STATUSES = ("Sending",)
MAIL_TRANSITIONS = {
    "Writing": "Pending",
    "Drafted": "Writing",
    "Scheduled": "Drafted",
    "Replied": "Sent",
}

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
# A lead whose support_info already carries a valid email is born 'seeded' with that
# email pre-filled, so Hermes never has to scrape it. Invalid values are quarantined.
_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_sync_scrape_tracker
AFTER INSERT ON newly_added
BEGIN
  INSERT INTO scrape_tracker
    (appid, game_name, short_descript, scrape_status, emails,
     Mail_status, steam_url, website, support_info, developers, publishers, genres, added_at)
  SELECT
    NEW.appid,
    NEW.name,
    NEW.short_description,
    CASE
      WHEN nullif(trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')),'') IS NULL
        THEN 'pending'
      WHEN instr(trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')), '@') > 1
       AND instr(substr(trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')),
                        instr(trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')), '@') + 1), '.') > 1
       AND trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')) NOT LIKE '% %'
        THEN 'seeded'
      ELSE 'invalid'
    END,
    nullif(json_extract(coalesce(NEW.support_info,'{}'),'$.email'),''),
    CASE
      WHEN nullif(trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')),'') IS NOT NULL
       AND NOT (
         instr(trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')), '@') > 1
         AND instr(substr(trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')),
                          instr(trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')), '@') + 1), '.') > 1
         AND trim(json_extract(coalesce(NEW.support_info,'{}'),'$.email')) NOT LIKE '% %'
       )
        THEN 'Invalid'
      ELSE 'Pending'
    END,
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


class _Rows:
    """Buffers a libsql cursor so sqlite3-style call sites keep working: libsql
    cursors aren't directly iterable and exhaust on read, so we fetch up front."""
    def __init__(self, cur):
        self.rowcount = getattr(cur, "rowcount", -1)
        self._rows = cur.fetchall()
        self._i = 0

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        if self._i < len(self._rows):
            self._i += 1
            return self._rows[self._i - 1]
        return None


class _Conn:
    """Thin libsql connection proxy: execute() returns a buffered, iterable result
    (the only API gap vs sqlite3). commit/executescript/close pass through."""
    def __init__(self, raw):
        self._raw = raw

    def execute(self, *a):
        read = str(a[0]).lstrip().upper().startswith(("SELECT", "PRAGMA"))
        for attempt in range(4):
            try:
                if self._raw is None:
                    self._raw = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
                return _Rows(self._raw.execute(*a))
            except Exception as exc:
                msg = str(exc).lower()
                transient = any(s in msg for s in (
                    "dns error", "failed to lookup", "error trying to connect",
                    "connection refused", "connection reset", "timed out", "timeout"))
                if not read or not transient or attempt == 3:
                    raise
                delay = 2 ** attempt
                print(f"Turso read failed; reconnecting in {delay}s ({exc})", file=sys.stderr)
                try:
                    if self._raw is not None:
                        self._raw.close()
                except Exception:
                    pass
                self._raw = None
                time.sleep(delay)

    def close(self):
        pass  # shared process-wide connection (see _turso); real close is at exit.

    def __getattr__(self, n):
        return getattr(self._raw, n)


# ponytail: one cached remote connection PER THREAD — a fresh libsql.connect() is a
# ~1.1s handshake to ap-south-1, and every query paid it. libsql connections are
# thread-bound (can't be used off the thread that opened them), so the web app's
# optimistic-UI background threads each need their own. thread-local gives that: the
# main script thread reuses one, each daemon worker lazily opens its own. close() is a
# no-op so the closing() wrappers don't tear it down.
_TLS = threading.local()


def _turso():
    conn = getattr(_TLS, "conn", None)
    if conn is None:
        conn = _Conn(libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN))
        _TLS.conn = conn
    return conn


def _rw():
    if TURSO_URL:
        return _turso()
    return sqlite3.connect(DB_PATH, timeout=TIMEOUT)


def _ro():
    if TURSO_URL:
        # Turso has no local mode=ro handle; the read-only contract (guarding
        # newly_added from Hermes) is relaxed in cloud mode — Hermes runs locally.
        return _turso()
    # read-only handle: any write raises sqlite3.OperationalError
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=TIMEOUT)


def connect():
    """A writable connection for the discovery scripts (batch_fetch / find_new /
    lead_discovery), so they write to the SAME store as the rest of the pipeline —
    Turso when configured, else the local file (tests). They own newly_added /
    known_comingsoon; this just hands them the right connection."""
    return _rw()


def init_tracker():
    """Create the tracker table + auto-sync trigger; enable WAL. Idempotent."""
    global _ensured
    with closing(_rw()) as conn:
        if not TURSO_URL:
            conn.execute("PRAGMA journal_mode=WAL")  # local: readers don't block writer
            # (Turso manages its own storage/concurrency — WAL pragma N/A there)
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
        # recreate the trigger so the latest definition always wins. COMMIT the DROP before
        # the CREATE: on some libsql clients the two share an uncommitted implicit tx and the
        # CREATE runs while the trigger still exists server-side -> "trigger already exists".
        # CREATE ... IF NOT EXISTS is a further guard so this can never abort the run.
        conn.execute("DROP TRIGGER IF EXISTS trg_sync_scrape_tracker")
        conn.commit()
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


def normalize_email(raw):
    """Return the first deliverable address, or '' for missing/malformed input."""
    addr = ((raw or "").split(",", 1)[0] or "").strip()
    if not addr or any(ch.isspace() for ch in addr):
        return ""
    try:
        parsed = Address(addr_spec=addr)
    except (ValueError, HeaderParseError):
        return ""
    if not parsed.username or not parsed.domain or "." not in parsed.domain:
        return ""
    return addr


def email_state(raw):
    """Classify a stored email as missing, valid, or invalid."""
    value = (raw or "").strip()
    if not value:
        return "missing"
    return "valid" if normalize_email(value) else "invalid"


def discovery_status(raw):
    """Map a Steam contact value to its initial scrape_tracker state."""
    return {"missing": "pending", "valid": "seeded", "invalid": "invalid"}[email_state(raw)]


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
    """Backfill and reconcile discovery states from newly_added.

    Only untouched leads are reconciled; accepted/sent leads keep their outreach
    history. Returns the number of rows newly added to scrape_tracker.
    """
    _ensure()
    with closing(_ro()) as ro:
        src = ro.execute(
            "SELECT appid, name, short_description, website, support_info, "
            "developers, publishers, genres, fetched_at FROM newly_added").fetchall()
    with closing(_rw()) as conn:
        have = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT appid, scrape_status, Mail_status FROM scrape_tracker")}
        added = 0
        for appid, name, brief, website, support, devs, pubs, genres, fetched_at in src:
            email = _support_email(support)
            status = discovery_status(email)
            stored_email = email or None
            if appid in have:
                current_scrape, current_mail = have[appid]
                if (current_mail in ("Pending", "Invalid")
                        and current_scrape in ("pending", "seeded", "invalid")):
                    conn.execute(
                        "UPDATE scrape_tracker SET scrape_status=?, emails=?, Mail_status=? "
                        "WHERE appid=?",
                        (status, stored_email,
                         "Invalid" if status == "invalid" else "Pending", appid))
                continue
            conn.execute(
                f"INSERT INTO scrape_tracker ({','.join(SEED_COLS)}, emails, scrape_status, "
                "Mail_status, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (appid, name, brief, f"https://store.steampowered.com/app/{appid}/",
                 website or "", support or "", _names(devs), _names(pubs), _genres(genres),
                 stored_email, status,
                 "Invalid" if status == "invalid" else "Pending", fetched_at),
            )
            have[appid] = (status, "Invalid" if status == "invalid" else "Pending")
            added += 1
        conn.commit()
    return added


def get_pending(limit=None):
    """Appids whose scrape_status is 'pending'. Hermes's work queue."""
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    _ensure()
    with closing(_ro()) as conn:
        q = "SELECT appid FROM scrape_tracker WHERE scrape_status='pending' ORDER BY appid"
        if limit:
            rows = conn.execute(q + " LIMIT ?", (limit,)).fetchall()
        else:
            rows = conn.execute(q).fetchall()
    return [r[0] for r in rows]


def scrape_status_appids(*statuses):
    """Appids at any of the given scrape_status values ('pending'|'seeded'|'scraped'|
    'no_email'|'invalid'|'failed')."""
    if not statuses or any(status not in STATUSES for status in statuses):
        raise ValueError(f"statuses must be one or more of {STATUSES}")
    _ensure()
    placeholders = ",".join("?" * len(statuses))
    with closing(_ro()) as conn:
        rows = conn.execute(
            f"SELECT appid FROM scrape_tracker WHERE scrape_status IN ({placeholders}) "
            "ORDER BY appid", statuses).fetchall()
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
    with closing(_rw()) as conn:
        current = conn.execute(
            "SELECT emails, Mail_status FROM scrape_tracker WHERE appid=?",
            (int(appid),)).fetchone()
        if current and current[1] in ("Sent", "Replied"):
            raise ValueError(f"cannot alter completed outreach for appid {appid}")
        candidate = emails if emails is not None else (current[0] if current else None)
        if scrape_status in ("scraped", "invalid"):
            scrape_status = "scraped" if normalize_email(candidate) else (
                "invalid" if (candidate or "").strip() else "no_email")
        if scrape_status == "no_email" and emails is None:
            emails = ""
        sets = ["scrape_status=?"]
        vals = [scrape_status]
        if not current or current[1] not in ("Sent", "Replied"):
            if scrape_status == "invalid":
                sets.append("Mail_status='Invalid'")
            elif current and current[1] == "Invalid":
                sets.append("Mail_status='Pending'")
        for col, val in (("emails", emails), ("website", website)):
            if val is not None:
                sets.append(f"{col}=?")
                vals.append(val)
        vals.append(appid)
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
    if status not in MAIL_STATUSES + INTERNAL_MAIL_STATUSES:
        raise ValueError(f"bad mail status {status!r}")
    _ensure()
    with closing(_ro()) as conn:
        return [r[0] for r in conn.execute(
            "SELECT appid FROM scrape_tracker WHERE Mail_status=? ORDER BY appid",
            (status,))]


def mail_status_emails(status):
    """(appid, emails) for one mail state, fetched in a single remote read."""
    if status not in MAIL_STATUSES + INTERNAL_MAIL_STATUSES:
        raise ValueError(f"bad mail status {status!r}")
    _ensure()
    with closing(_ro()) as conn:
        return [(r[0], r[1] or "") for r in conn.execute(
            "SELECT appid, emails FROM scrape_tracker WHERE Mail_status=? ORDER BY appid",
            (status,))]


def approval_ready_appids():
    """Appids ready for Game Approval: Mail_status still 'Pending' AND scrape_status
    has an actual email ('seeded' or 'scraped'). A bare Mail_status='Pending' check
    also matched 'no_email'/'failed'/still-'pending' leads (they never move Mail_status
    on their own), which leaked them into Approval alongside No-Mail."""
    _ensure()
    with closing(_ro()) as conn:
        return [r[0] for r in conn.execute(
            "SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' "
            "AND scrape_status IN ('seeded','scraped') ORDER BY appid")]


def nomail_ready_appids():
    """Appids awaiting scraping or settled without a usable email."""
    _ensure()
    with closing(_ro()) as conn:
        return [r[0] for r in conn.execute(
            "SELECT appid FROM scrape_tracker WHERE Mail_status='Pending' "
            "AND scrape_status IN ('pending','no_email','failed') ORDER BY appid")]


def set_mail_status(appid, status):
    """Apply one legal user-facing outreach transition."""
    _ensure()
    with closing(_rw()) as conn:
        if status == "Invalid":
            row = conn.execute(
                "UPDATE scrape_tracker SET Mail_status='Invalid' "
                "WHERE appid=? AND scrape_status='invalid' "
                "AND Mail_status NOT IN ('Sent','Replied') RETURNING appid",
                (int(appid),)).fetchone()
        elif status in MAIL_TRANSITIONS:
            previous = (("Writing", "Scheduled") if status == "Drafted"
                        else (MAIL_TRANSITIONS[status],))
            placeholders = ",".join("?" * len(previous))
            email_guard = (" AND scrape_status IN ('seeded','scraped')"
                           if status in ("Writing", "Drafted", "Scheduled") else "")
            row = conn.execute(
                f"UPDATE scrape_tracker SET Mail_status=? WHERE appid=? "
                f"AND Mail_status IN ({placeholders}){email_guard} RETURNING appid",
                (status, int(appid), *previous)).fetchone()
        else:
            raise ValueError(f"{status!r} is not a direct outreach transition")
        conn.commit()
    if row is None:
        raise ValueError(f"illegal transition to {status!r} for appid {appid}")


def get_emails(appid):
    """The emails string stored for one lead (comma-separated, '' if none)."""
    _ensure()
    with closing(_ro()) as conn:
        row = conn.execute("SELECT emails FROM scrape_tracker WHERE appid=?",
                           (int(appid),)).fetchone()
    return (row[0] or "") if row else ""


def set_mail_template(appid, template):
    """Record the mail variant while a draft is being written or reviewed."""
    _ensure()
    with closing(_rw()) as conn:
        row = conn.execute(
            "UPDATE scrape_tracker SET mail_template=? "
            "WHERE appid=? AND Mail_status IN ('Writing','Drafted') RETURNING appid",
            (int(template), int(appid)),
        ).fetchone()
        conn.commit()
    if row is None:
        raise ValueError(f"cannot set mail template for appid {appid} in its current state")


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


def claim_mail(appid):
    """Atomically reserve one Scheduled mail. False means another run/state owns it."""
    _ensure()
    with closing(_rw()) as conn:
        row = conn.execute(
            "UPDATE scrape_tracker SET Mail_status='Sending' "
            "WHERE appid=? AND Mail_status='Scheduled' "
            "AND scrape_status IN ('seeded','scraped') RETURNING appid",
            (int(appid),)).fetchone()
        conn.commit()
    return row is not None


def repair_invalid_rows():
    """Reconcile unsent rows whose stored email and pipeline states disagree."""
    _ensure()
    changed = []
    with closing(_rw()) as conn:
        rows = conn.execute(
            "SELECT appid, scrape_status, emails, Mail_status FROM scrape_tracker"
        ).fetchall()
        for appid, scrape_status, emails, mail_status in rows:
            state = email_state(emails)
            outreach_state = mail_status in ("Writing", "Drafted", "Scheduled", "Sending")
            source_state = scrape_status in ("seeded", "scraped")
            if mail_status in ("Sent", "Replied"):
                continue
            if state == "invalid":
                target = ("invalid", "Invalid")
            elif state == "missing" and (outreach_state or source_state
                                         or scrape_status == "invalid"):
                target = ("pending", "Pending")
            elif state == "valid" and (scrape_status == "invalid"
                                       or mail_status == "Invalid"):
                target = ("scraped", "Pending")
            else:
                continue
            if target == (scrape_status, mail_status):
                continue
            conn.execute(
                "UPDATE scrape_tracker SET scrape_status=?, Mail_status=? WHERE appid=?",
                (*target, appid))
            changed.append(appid)
        conn.commit()
    return changed


def quarantine_unusable(appid):
    """Remove an unusable recipient from outreach, preserving missing vs malformed."""
    _ensure()
    with closing(_rw()) as conn:
        row = conn.execute(
            "SELECT emails, Mail_status FROM scrape_tracker WHERE appid=?", (int(appid),)
        ).fetchone()
        if row and row[1] not in ("Sent", "Replied"):
            state = email_state(row[0])
            if state != "valid":
                target = ("pending", "Pending") if state == "missing" else (
                    "invalid", "Invalid")
                conn.execute(
                    "UPDATE scrape_tracker SET scrape_status=?, Mail_status=? WHERE appid=?",
                    (*target, int(appid)))
        conn.commit()


def quarantine_verified_invalid(appid):
    """Quarantine a Scheduled recipient rejected by external mailbox verification."""
    _ensure()
    with closing(_rw()) as conn:
        row = conn.execute(
            "UPDATE scrape_tracker SET scrape_status='invalid', Mail_status='Invalid' "
            "WHERE appid=? AND Mail_status='Scheduled' RETURNING appid",
            (int(appid),),
        ).fetchone()
        conn.commit()
    return row is not None


def reset_sending(appid, status):
    """Resolve a definitely-unsent claim without touching any other state."""
    if status not in ("Scheduled", "Drafted"):
        raise ValueError("Sending can only be reset to Scheduled or Drafted")
    _ensure()
    with closing(_rw()) as conn:
        conn.execute("UPDATE scrape_tracker SET Mail_status=? "
                     "WHERE appid=? AND Mail_status='Sending'", (status, int(appid)))
        conn.commit()


def mark_sent(appid):
    """Mail was sent: Mail_status -> 'Sent', stamp sent_at (UTC) for the daily cap,
    and drop the newly_added row (the scrape_tracker row stays as the record)."""
    _ensure()
    with closing(_rw()) as conn:
        row = conn.execute(
            "UPDATE scrape_tracker SET Mail_status='Sent', "
            "sent_at=COALESCE(sent_at,CURRENT_TIMESTAMP) "
            "WHERE appid=? AND Mail_status='Sending' RETURNING appid",
            (int(appid),)).fetchone()
        if row is None:
            current = conn.execute("SELECT Mail_status FROM scrape_tracker WHERE appid=?",
                                   (int(appid),)).fetchone()
            if not current or current[0] != "Sent":
                raise RuntimeError(f"cannot mark {appid} Sent from {current[0] if current else 'missing'}")
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
