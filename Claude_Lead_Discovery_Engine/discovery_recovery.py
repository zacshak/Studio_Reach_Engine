"""Durable Steam discovery outbox. No network or implicit database connections.

Deploy SCHEMA before rejection writers delete these jobs. Rejection must delete
the job atomically with the lead. Publication callers acknowledge only IDs whose
validated media AND remote index entry have been successfully published.
"""
import json

SCHEMA = """CREATE TABLE IF NOT EXISTS pending_discovery (
    appid INTEGER PRIMARY KEY,
    name TEXT,
    discovered_on TEXT NOT NULL
)"""


def init_db(conn):
    conn.execute(SCHEMA)
    conn.commit()


def record_snapshot(conn, total, items, today, now_iso, bootstrap=False):
    """Commit the snapshot and its work together, never enqueue the baseline."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = {r[0] for r in conn.execute("SELECT appid FROM known_comingsoon")}
        bootstrap = bootstrap or not existing
        new_apps = [(appid, name) for appid, name in items if appid not in existing]
        for appid, name in new_apps:
            conn.execute(
                "INSERT OR IGNORE INTO known_comingsoon(appid,name,first_seen) VALUES (?,?,?)",
                (appid, name, today))
            if not bootstrap:
                conn.execute(
                    "INSERT OR IGNORE INTO pending_discovery(appid,name,discovered_on) VALUES (?,?,?)",
                    (appid, name, today))
        conn.execute(
            "INSERT INTO snapshot_runs(run_at,total_apps,new_apps,mode) VALUES (?,?,?,?)",
            (now_iso, total, len(new_apps), "bootstrap" if bootstrap else "diff"))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return bootstrap, new_apps


def pending_appids(conn):
    return [r[0] for r in conn.execute(
        "SELECT appid FROM pending_discovery ORDER BY discovered_on,appid")]


def finish_ineligible(conn, appid, success, raw):
    """Retire only explicit successful source evidence, never missing/bad data."""
    if not success or not raw:
        return False
    try:
        entry = json.loads(raw).get(str(appid)) or {}
        data = entry.get("data") or {}
        kind = data.get("type")
        released = (data.get("release_date") or {}).get("coming_soon") is False
        # Match cache_put's existing admission rules. A successful source response
        # rejected for missing name/media is not an unfinished upload to retry.
        unreviewable = kind in ('game', 'demo') and (
            not (data.get('name') or '').strip() or not data.get('screenshots'))
        ineligible = (isinstance(kind, str) and bool(kind) and kind not in ("game", "demo")) or released or unreviewable
        if not entry.get("success") or not ineligible:
            return False
    except (ValueError, AttributeError, TypeError):
        return False
    conn.execute("DELETE FROM pending_discovery WHERE appid=?", (appid,))
    conn.commit()
    return True


def finish_invalid(conn, appid):
    """Quarantined canonical rows need no review media, but must exist durably."""
    conn.execute(
        "DELETE FROM pending_discovery WHERE appid=? AND EXISTS "
        "(SELECT 1 FROM scrape_tracker WHERE appid=? AND scrape_status='invalid')",
        (appid, appid))
    conn.commit()


def cache_if_pending(conn, appid, success, raw, cache_put):
    """Prevent an in-flight fetch from recreating a concurrently rejected lead.

    cache_put commits its own write. Hold the write lock from the ownership check
    through that commit, but never across the network fetch.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT discovered_on FROM pending_discovery WHERE appid=?", (appid,)).fetchone()
        saved = bool(row and cache_put(conn, appid, success, raw, discovered_on=row[0]))
        conn.commit()
        return saved
    except BaseException:
        conn.rollback()
        raise


def ack_published(conn, appids):
    """Delete ONLY confirmed published IDs. Caller owns the commit.

    Call after upload_dir validation/upload and successful index publication.
    A local .complete file or sync exit code zero is not publication evidence.
    Repeating this after an ambiguous commit is safe.
    """
    for appid in appids:
        conn.execute("DELETE FROM pending_discovery WHERE appid=?", (int(appid),))
