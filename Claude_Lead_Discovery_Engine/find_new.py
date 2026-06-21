"""v0.4 - The daily finder (PRE-RELEASE pages only).

Finds Steam pages that NEWLY appeared in Steam's "Coming Soon" list since the
last run. By design this captures ONLY not-yet-released pages:
    * coming-soon / waitlist games
    * demos (type=demo)
    * playtests (coming-soon games)
...and EXCLUDES already-released games (they aren't in Coming Soon).

Source: Steam store search  filter=comingsoon  (no API key needed).
No fallback: if the source fails, it reports the failure and stops.

Mechanics (unchanged): daily snapshot -> diff vs everything seen before.
First run = bootstrap (memorise baseline, report nothing new).

Usage:
    python find_new.py                 # normal run (auto-bootstraps first time)
    python find_new.py --bootstrap     # force baseline-only
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))  # India Standard Time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "cache.sqlite")
OUT_DIR = os.path.join(HERE, "out")
SEARCH_URL = "https://store.steampowered.com/search/results/"
UA = "claude-steam-search/0.4 (research; contact via project owner)"

PAGE = 100              # Steam hard-caps search results at 100 per request
PAGE_INTERVAL = 2.0     # seconds between page requests (politeness)
MAX_RETRIES = 4
BACKOFF = [10, 30, 60, 120]
ROW_RE = re.compile(r'data-ds-appid="(\d+)".*?<span class="title">(.*?)</span>', re.S)


class FinderError(Exception):
    """Raised when the coming-soon source cannot be read reliably."""


def init_db(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS known_comingsoon (
            appid      INTEGER PRIMARY KEY,
            name       TEXT,
            first_seen TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshot_runs (
            run_at     TEXT NOT NULL,
            total_apps INTEGER,
            new_apps   INTEGER,
            mode       TEXT
        )"""
    )
    conn.commit()


def get_page(start):
    """Fetch one coming-soon page; return (total_count, [(appid, name), ...])."""
    # sort_by=Name_ASC is STABLE: pages don't re-rank between requests, so deep
    # pagination has no overlaps/gaps. The default sort is unstable and silently
    # drops items across pages.
    url = (f"{SEARCH_URL}?filter=comingsoon&json=1&infinite=1"
           f"&start={start}&count={PAGE}&sort_by=Name_ASC")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError) as e:
            if attempt == MAX_RETRIES - 1:
                raise FinderError(f"page start={start} failed after retries: {e}")
            wait = BACKOFF[attempt]
            print(f"  [retry] start={start}: {e} -> waiting {wait}s")
            time.sleep(wait)
    total = payload.get("total_count")
    rows = ROW_RE.findall(payload.get("results_html", "") or "")
    items = [(int(a), html.unescape(n.strip())) for a, n in rows]
    return total, items


def fetch_comingsoon():
    """Walk the whole Coming Soon list. Returns (total_count, [(appid, name)])."""
    print("Reading Steam 'Coming Soon' list (pre-release pages only)...")
    total0, first = get_page(0)
    if not total0 or total0 <= 0:
        raise FinderError("Coming Soon returned total_count=0 (source changed or blocked?)")
    items, seen = list(first), {a for a, _ in first}
    start = len(first)
    pages = 1
    while start < total0:
        time.sleep(PAGE_INTERVAL)
        _, page_items = get_page(start)
        pages += 1
        if not page_items:
            break
        for appid, name in page_items:
            if appid not in seen:
                seen.add(appid)
                items.append((appid, name))
        start += PAGE
        if pages % 20 == 0:
            print(f"  ...{len(items)} / ~{total0} so far ({pages} pages)")
    # reliability guard: refuse a silently-truncated snapshot
    if len(items) < total0 * 0.9:
        raise FinderError(
            f"only collected {len(items)} of {total0} expected — refusing partial snapshot")
    print(f"  done: {len(items)} pre-release pages across {pages} pages.")
    return total0, items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bootstrap", action="store_true", help="force baseline-only")
    ap.add_argument("--stamp", help="shared run stamp (YYYY-MM-DD_HHMM); the runner "
                                    "passes this so finder + lead files line up")
    args = ap.parse_args()

    stamp = args.stamp or datetime.now(IST).strftime("%Y-%m-%d_%H%M")
    os.makedirs(OUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    known_before = conn.execute("SELECT COUNT(*) FROM known_comingsoon").fetchone()[0]
    bootstrap = args.bootstrap or known_before == 0

    try:
        total, items = fetch_comingsoon()
    except FinderError as e:
        print("\n" + "=" * 56)
        print(f"  FAILED: {e}")
        print("  No fallback. Nothing was changed. Try again later.")
        print("=" * 56)
        conn.close()
        return 2

    existing = {row[0] for row in conn.execute("SELECT appid FROM known_comingsoon")}
    new_apps = [(appid, name) for appid, name in items if appid not in existing]

    today = datetime.now(IST).date().isoformat()
    now_iso = datetime.now(IST).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR IGNORE INTO known_comingsoon(appid, name, first_seen) VALUES (?,?,?)",
        [(appid, name, today) for appid, name in new_apps],
    )
    conn.execute(
        "INSERT INTO snapshot_runs(run_at, total_apps, new_apps, mode) VALUES (?,?,?,?)",
        (now_iso, total, len(new_apps), "bootstrap" if bootstrap else "diff"),
    )
    conn.commit()

    print("\n" + "=" * 56)
    if bootstrap:
        print(f"  BOOTSTRAP done. Baseline of {len(items)} pre-release pages memorised.")
        print(f"  No 'new' list this run (need a prior run to compare).")
        print("=" * 56)
        conn.close()
        return 10  # runner: baseline-only, no leads to build
    if not new_apps:
        print("  No new pre-release pages since the last run.")
        print("=" * 56)
        conn.close()
        return 11  # runner: nothing new, skip lead discovery

    out_file = os.path.join(OUT_DIR, f"new_appids_{stamp}.txt")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(f"# new pre-release pages found {now_iso} (since last run)\n")
        for appid, name in new_apps:
            f.write(f"{appid}  # {name}\n")
    print(f"  NEW pre-release pages since last run: {len(new_apps)}")
    for appid, name in new_apps[:15]:
        print(f"    {appid}  {name}")
    if len(new_apps) > 15:
        print(f"    ... and {len(new_apps) - 15} more")
    print(f"\n  Written: {out_file}")
    print(f"  Next:  python lead_discovery.py --file \"{out_file}\"")
    print("=" * 56)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
