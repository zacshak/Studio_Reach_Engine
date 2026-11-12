"""Restore games that were deleted by mistake, without a discovery run.

The MCP tool restore_games (or the dashboard API) queues appids in restore_requests and
starts restore.yml, which runs this. For each queued appid:
  1. Re-fetch the game's Steam data and re-create its newly_added row. The database
     trigger re-creates the scrape_tracker row as Pending, with the Steam support
     email pre-filled when there is one.
  2. Rebuild its review media (screenshots, sprite sheet, manifest) in R2.
  3. If the request asked for it, save the triage decision 'pass' (-> Passed view).
Each request ends as done or failed with a reason. Released games, non-games and pages
without screenshots cannot be restored (the same rules discovery uses).

    python SRE.py --restore
"""
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, os.path.join(ROOT, "Leads_Reviewer"))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import pipeline       # noqa: E402
import batch_fetch    # noqa: E402
import media_store    # noqa: E402

SCHEMA = """CREATE TABLE IF NOT EXISTS restore_requests (
  appid INTEGER PRIMARY KEY,
  decision TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  detail TEXT,
  requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT
)"""
RESTORE_CRITERIA = "Restored by the owner after a mistaken delete."
RESTORE_REASON = "Restored by user (was deleted by mistake)"
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


class RestoreError(RuntimeError):
    """A request that cannot be restored; the reason is shown to the user."""


def queued(conn):
    conn.execute(SCHEMA)
    return [(int(a), d) for a, d in conn.execute(
        "SELECT appid, decision FROM restore_requests WHERE status='queued' ORDER BY requested_at, appid")]


def _finish(conn, appid, status, detail):
    conn.execute(f"UPDATE restore_requests SET status=?, detail=?, finished_at={NOW} WHERE appid=?",
                 (status, detail, appid))
    conn.commit()


def _steam_data(raw, appid):
    try:
        entry = json.loads(raw).get(str(appid)) or {}
    except (ValueError, AttributeError):
        raise RestoreError("Steam returned an unreadable answer")
    if not entry.get("success"):
        raise RestoreError("Steam has no store page for this game")
    return entry.get("data") or {}


def restore_one(conn, appid, decision, *, fetch=None, rebuild=None, client=None):
    """Restore one game. Returns a short description; raises RestoreError when it can't."""
    fetch = fetch or batch_fetch.http_get
    if rebuild is None:
        from verify_accepted import rebuild_media as rebuild
    row = conn.execute("SELECT Mail_status FROM scrape_tracker WHERE appid=?", (appid,)).fetchone()
    if row:
        return f"already in the tracker ({row[0]}); nothing restored"
    if conn.execute("SELECT 1 FROM media_cleanup_jobs WHERE appid=?", (appid,)).fetchone():
        raise RestoreError("its old images are still being deleted; try again in a few minutes")
    raw = fetch(appid)
    data = _steam_data(raw, appid)
    if not batch_fetch.is_prerelease_lead(data):
        rd = data.get("release_date") or {}
        why = (f"already released ({rd.get('date')})" if rd.get("coming_soon") is False
               else f"not a game or demo ({data.get('type')})" if data.get("type") not in ("game", "demo")
               else "Steam lists no name")
        raise RestoreError(why)
    if not batch_fetch.cache_put(conn, appid, True, raw):
        raise RestoreError("Steam lists no screenshots for this game")
    if not conn.execute("SELECT 1 FROM scrape_tracker WHERE appid=?", (appid,)).fetchone():
        raise RestoreError("the tracker row was not re-created")
    try:
        rebuild(appid, client)
    except Exception as exc:
        # Undo the re-created rows so the request can simply be queued again.
        conn.execute("DELETE FROM scrape_tracker WHERE appid=?", (appid,))
        conn.execute("DELETE FROM newly_added WHERE appid=?", (appid,))
        conn.commit()
        raise RestoreError(f"its images could not be rebuilt from Steam: {exc}")
    if decision == "pass":
        conn.execute("INSERT INTO triage_criteria(text) SELECT ? WHERE NOT EXISTS "
                     "(SELECT 1 FROM triage_criteria WHERE text=?)", (RESTORE_CRITERIA, RESTORE_CRITERIA))
        conn.execute(
            "UPDATE scrape_tracker SET Mail_status='Triaged', triage_decision='pass', triage_reason=?, "
            "triage_criteria_id=(SELECT id FROM triage_criteria WHERE text=? ORDER BY id LIMIT 1), "
            f"triaged_at={NOW} WHERE appid=? AND Mail_status='Pending'",
            (RESTORE_REASON, RESTORE_CRITERIA, appid))
    conn.execute("DELETE FROM pending_discovery WHERE appid=?", (appid,))
    conn.commit()
    status = conn.execute("SELECT Mail_status, emails FROM scrape_tracker WHERE appid=?", (appid,)).fetchone()
    where = "Passed" if status and status[0] == "Triaged" else status[0] if status else "?"
    return f"restored to {where}" + ("" if status and status[1] else " (no email on Steam)")


def main():
    conn = pipeline.connect()
    requests = queued(conn)
    print(f"{len(requests)} restore request(s)")
    if not requests:
        return 0
    client = media_store._client() if media_store.write_enabled() else None
    failures = 0
    for appid, decision in requests:
        try:
            if client is None:
                raise RestoreError("R2 write credentials are not set; media cannot be rebuilt")
            detail = restore_one(conn, appid, decision, client=client)
            _finish(conn, appid, "done", detail)
            print(f"DONE {appid}: {detail}")
        except RestoreError as exc:
            _finish(conn, appid, "failed", str(exc))
            print(f"FAILED {appid}: {exc}")
        except Exception as exc:  # unexpected: keep the reason, mark failed, carry on
            traceback.print_exc()
            failures += 1
            try:
                pipeline.reconnect()
                conn = pipeline.connect()
                _finish(conn, appid, "failed", f"unexpected error: {exc}")
            except Exception:
                traceback.print_exc()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
