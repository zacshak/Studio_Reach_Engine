"""How many games each studio has on Steam, for the dashboard cards.

For every developer of a game still in the pipeline (not yet sent), ask the Steam store
search for the games listed under that exact developer name (games only: no demos,
DLC or soundtracks; unreleased games included, so the count includes the game itself).
Counts are stored in developer_games and refreshed after REFRESH_DAYS. Runs as part of
the nightly discovery (run_daily.py), right after the night's games are added.

    python SRE.py --dev-counts            # missing or stale developers
    python SRE.py --dev-counts --all      # refresh every developer
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline  # noqa: E402

SEARCH = "https://store.steampowered.com/search/results/"
UA = "Mozilla/5.0 (compatible; studio-reach-engine/1.0)"
REFRESH_DAYS = 14
PAUSE_SECONDS = float(os.environ.get("DEV_COUNTS_PAUSE", "1.0"))
TRIES = 4

SCHEMA = """CREATE TABLE IF NOT EXISTS developer_games (
  developer TEXT PRIMARY KEY,
  games INTEGER NOT NULL,
  checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""

# One row per (game, developer). newly_added keeps the Steam list as JSON; rows without
# it fall back to the tracker's display string as a single name. The dashboard worker
# uses the same expression (webapp/worker.js VIEW_SQL.developers).
LIVE = "('Pending','Triaged','Verifying','Accepted','Drafted','Scheduled')"
DEVELOPERS_SQL = f"""SELECT DISTINCT trim(j.value) FROM scrape_tracker s
  LEFT JOIN newly_added n ON n.appid = s.appid,
  json_each(CASE WHEN json_valid(n.developers) AND json_type(n.developers) = 'array'
                 THEN n.developers ELSE json_array(coalesce(s.developers, '')) END) j
  WHERE s.Mail_status IN {LIVE} AND trim(j.value) <> ''"""


def search_url(developer):
    query = urllib.parse.urlencode({"developer": developer, "category1": 998, "infinite": 1,
                                    "count": 1, "ignore_preferences": 1, "cc": "us", "l": "english"})
    return f"{SEARCH}?{query}"


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def game_count(developer, fetch=http_get, sleep=time.sleep):
    """Number of games Steam lists for this exact developer name."""
    for attempt in range(TRIES):
        try:
            data = json.loads(fetch(search_url(developer)))
            if not data.get("success") or not isinstance(data.get("total_count"), int):
                raise ValueError(f"unexpected answer: {str(data)[:120]}")
            return data["total_count"]
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if attempt == TRIES - 1:
                raise RuntimeError(f"{developer!r}: {exc}") from exc
            sleep(10 * (attempt + 1))


def due(conn, refresh_all=False):
    conn.execute(SCHEMA)
    names = [r[0] for r in conn.execute(DEVELOPERS_SQL)]
    if refresh_all:
        return names
    fresh = {r[0] for r in conn.execute(
        "SELECT developer FROM developer_games WHERE checked_at >= datetime('now', ?)",
        (f"-{REFRESH_DAYS} days",))}
    return [n for n in names if n not in fresh]


def save(conn, developer, games):
    conn.execute("INSERT INTO developer_games(developer, games, checked_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                 "ON CONFLICT(developer) DO UPDATE SET games=excluded.games, checked_at=excluded.checked_at",
                 (developer, games))
    conn.commit()


def main(argv=()):
    conn = pipeline.connect()
    names = due(conn, refresh_all="--all" in argv)
    print(f"{len(names)} developer(s) to look up")
    failed = 0
    for i, name in enumerate(names, 1):
        try:
            games = game_count(name)
        except RuntimeError as exc:
            failed += 1
            print(f"  FAILED {exc}")
            continue
        try:
            save(conn, name, games)
        except Exception:
            pipeline.reconnect()
            conn = pipeline.connect()
            save(conn, name, games)
        if i % 50 == 0:
            print(f"  {i}/{len(names)} done")
        time.sleep(PAUSE_SECONDS)
    print(f"Done: {len(names) - failed} saved, {failed} failed")
    # A few lookups failing is normal (Steam hiccups); they are retried next run.
    return 1 if names and failed > len(names) // 10 else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
