"""v0.2 - Reliable batch Steam fetcher.

Fetches many appids from Steam's `appdetails`, with:
  * strict rate limiting (stay well under Steam's ~200 req / 5 min),
  * a local SQLite cache so we never re-download (fewer calls = less ban risk),
  * polite retries / back-off on 429 (slow down) and 403 (blocked).

Reliability over speed by design. Slow is fine.

Usage:
    python batch_fetch.py 413150 1145360 730
    python batch_fetch.py --file appids.txt
    python batch_fetch.py --file appids.txt --refresh   # ignore cache, re-fetch
    python batch_fetch.py 413150 --games-only           # only write type==game rows
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))  # India Standard Time

from fetch_app import normalize  # reuse the v0.1 field extractor
import pipeline  # shared DB connection (Turso when configured) — single source of truth

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
APPDETAILS = "https://store.steampowered.com/api/appdetails"
UA = "claude-steam-search/0.2 (research; contact via project owner)"

# --- Rate limit policy (conservative on purpose) -------------------------
MIN_INTERVAL = 2.0      # seconds between any two requests
MAX_PER_WINDOW = 175    # hard cap per rolling window (Steam allows ~200)
WINDOW = 300            # seconds (5 minutes)
MAX_RETRIES = 5
BACKOFF_429 = 30        # seconds to wait when told to slow down
BACKOFF_403 = 300       # seconds to wait when blocked
BACKOFF_NET = [5, 15, 45, 90, 120]  # network/timeout retries


class RateLimiter:
    """Enforces both a minimum gap between calls and a rolling-window cap."""

    def __init__(self, min_interval, max_per_window, window):
        self.min_interval = min_interval
        self.max_per_window = max_per_window
        self.window = window
        self.calls = deque()
        self.last = 0.0

    def _purge(self, now):
        while self.calls and now - self.calls[0] > self.window:
            self.calls.popleft()

    def wait(self):
        now = time.monotonic()
        gap = now - self.last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        now = time.monotonic()
        self._purge(now)
        if len(self.calls) >= self.max_per_window:
            sleep_for = self.window - (now - self.calls[0]) + 1
            if sleep_for > 0:
                print(f"  [rate] window full, pausing {sleep_for:.0f}s to stay safe...")
                time.sleep(sleep_for)
            self._purge(time.monotonic())

    def record(self):
        t = time.monotonic()
        self.last = t
        self.calls.append(t)


# Bookkeeping columns we own; everything else is a real Steam field.
BASE_COLS = {"appid", "fetched_at", "discovered_on", "success"}


def init_db(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS newly_added (
            appid         INTEGER PRIMARY KEY,
            fetched_at    TEXT NOT NULL,
            discovered_on TEXT,
            success       INTEGER NOT NULL
        )"""
    )
    conn.commit()


def _existing_columns(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(newly_added)")}


def _ensure_columns(conn, keys):
    """Add a TEXT column for any appdetails field we haven't seen before."""
    have = _existing_columns(conn)
    for k in keys:
        if k not in have:
            conn.execute(f'ALTER TABLE newly_added ADD COLUMN "{k}" TEXT')
            have.add(k)


def _coerce(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v  # str/int/float/bool/None stored natively by sqlite


def _jload(v):
    if not isinstance(v, str):
        return v
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return None


def _columns_to_data(conn, appid):
    """Rebuild a partial appdetails `data` dict from the stored columns."""
    cols = [c[1] for c in conn.execute("PRAGMA table_info(newly_added)")]
    row = conn.execute("SELECT * FROM newly_added WHERE appid=?", (appid,)).fetchone()
    if row is None:
        return None
    d = dict(zip(cols, row))
    if not d.get("success"):
        return None
    return {
        "type": d.get("type"),
        "name": d.get("name"),
        "short_description": d.get("short_description"),
        "genres": _jload(d.get("genres")) or [],
        "developers": _jload(d.get("developers")) or [],
        "publishers": _jload(d.get("publishers")) or [],
        "website": d.get("website"),
        "support_info": _jload(d.get("support_info")) or {},
        "release_date": _jload(d.get("release_date")) or {},
        "is_free": d.get("is_free"),
    }


def reconstruct_normalized(conn, appid):
    """Derive the flat shape from stored COLUMNS, reusing the SAME normalize()
    used for fresh fetches (single source of truth — no logic duplication)."""
    data = _columns_to_data(conn, appid)
    return normalize(appid, data) if data is not None else None


def cache_get(conn, appid):
    row = conn.execute(
        "SELECT success FROM newly_added WHERE appid=?", (appid,)).fetchone()
    if row is None:
        return None
    success = bool(row[0])
    return {"success": success,
            "normalized": reconstruct_normalized(conn, appid) if success else None}


def is_prerelease_lead(data):
    """A row belongs in newly_added only if it's a NAMED coming-soon game or
    demo. (Some coming-soon pages have no name set yet -> useless lead.)"""
    if not data:
        return False
    rd = data.get("release_date") or {}
    return (data.get("type") in ("game", "demo")
            and bool(rd.get("coming_soon"))
            and bool((data.get("name") or "").strip()))


def cache_put(conn, appid, success, raw_text, discovered_on=None):
    """Persist ONE pre-release lead with every Steam field as its own column.
    GUARD: released games, dead appids, and non-game/demo types are REJECTED
    (returns False) so newly_added can only ever hold real leads."""
    data = {}
    if success and raw_text:
        try:
            data = (json.loads(raw_text).get(str(appid)) or {}).get("data") or {}
        except (json.JSONDecodeError, AttributeError):
            data = {}
    if not is_prerelease_lead(data):
        return False
    if not data.get("screenshots"):       # no screenshots -> can't review it; drop
        return False
    cols = {
        "appid": appid,
        "fetched_at": datetime.now(IST).isoformat(timespec="seconds"),
        "discovered_on": discovered_on or datetime.now(IST).date().isoformat(),
        "success": 1,
    }
    for k, v in data.items():
        if k not in BASE_COLS:            # never clobber bookkeeping columns
            cols[k] = _coerce(v)
    _ensure_columns(conn, cols.keys())
    names = ",".join(f'"{c}"' for c in cols)
    placeholders = ",".join("?" * len(cols))
    conn.execute(
        f"INSERT OR REPLACE INTO newly_added ({names}) VALUES ({placeholders})",
        list(cols.values()),
    )
    conn.commit()
    return True


def http_get(appid, cc="us", lang="english"):
    url = f"{APPDETAILS}?appids={appid}&cc={cc}&l={lang}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def fetch_one(appid, limiter):
    """Fetch a single appid with rate limiting + retries.

    Returns (success: bool, normalized: dict|None, raw_text: str|None).
    """
    net_try = 0
    for attempt in range(1, MAX_RETRIES + 1):
        limiter.wait()
        try:
            raw_text = http_get(appid)
            limiter.record()
        except urllib.error.HTTPError as e:
            limiter.record()
            if e.code == 429:
                retry_after = e.headers.get("Retry-After")
                wait = int(retry_after) if (retry_after and retry_after.isdigit()) else BACKOFF_429
                print(f"  [429] Steam says slow down. Waiting {wait}s (attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue
            if e.code == 403:
                print(f"  [403] Blocked. Backing off {BACKOFF_403}s (attempt {attempt}/{MAX_RETRIES})...")
                time.sleep(BACKOFF_403)
                continue
            print(f"  [HTTP {e.code}] appid {appid}: giving up on this one.")
            return False, None, None
        except (urllib.error.URLError, TimeoutError) as e:
            wait = BACKOFF_NET[min(net_try, len(BACKOFF_NET) - 1)]
            net_try += 1
            print(f"  [net] {e} -> retry in {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        # parse
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            print(f"  [parse] bad JSON for appid {appid}, retrying...")
            time.sleep(BACKOFF_429)
            continue
        entry = payload.get(str(appid))
        if not entry or not entry.get("success"):
            return False, None, raw_text  # dead/region-locked/not a store app
        data = entry.get("data") or {}
        return True, normalize(appid, data), raw_text

    print(f"  [giveup] appid {appid} after {MAX_RETRIES} attempts.")
    return False, None, None


def read_appids(args):
    ids = list(args.appids)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    ids.append(int(line.split(",")[0].split()[0]))
    # de-dup, keep order
    seen, ordered = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered


CSV_FIELDS = [
    "appid", "type", "game_name", "developers", "publishers", "genres",
    "studio_website", "support_email", "support_url", "release_date",
    "coming_soon", "page_link", "is_free",
]


def to_csv_row(n):
    r = dict(n)
    r["developers"] = ", ".join(n.get("developers") or [])
    r["publishers"] = ", ".join(n.get("publishers") or [])
    r["genres"] = ", ".join(n.get("genres") or [])
    return {k: r.get(k, "") for k in CSV_FIELDS}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("appids", nargs="*", type=int)
    ap.add_argument("--file", help="text file of appids (one per line)")
    ap.add_argument("--refresh", action="store_true", help="ignore cache, re-fetch")
    ap.add_argument("--games-only", action="store_true", help="CSV: only type==game")
    ap.add_argument("--min-interval", type=float, default=MIN_INTERVAL)
    args = ap.parse_args()

    appids = read_appids(args)
    if not appids:
        ap.error("give appids on the command line or via --file")

    os.makedirs(OUT_DIR, exist_ok=True)
    conn = pipeline.connect()
    init_db(conn)
    limiter = RateLimiter(args.min_interval, MAX_PER_WINDOW, WINDOW)

    total = len(appids)
    est_net = sum(1 for a in appids if args.refresh or cache_get(conn, a) is None)
    eta_s = est_net * args.min_interval
    print(f"Processing {total} appids ({est_net} need fetching). "
          f"Throttle {args.min_interval}s/call -> ~{eta_s/60:.1f} min minimum.\n")

    rows, ok, dead, cached = [], 0, 0, 0
    for idx, appid in enumerate(appids, 1):
        hit = None if args.refresh else cache_get(conn, appid)
        if hit is not None:
            cached += 1
            if hit["success"] and hit["normalized"]:
                rows.append(hit["normalized"])
                ok += 1
            else:
                dead += 1
            print(f"[{idx}/{total}] {appid}: cache hit ({'ok' if hit['success'] else 'no-data'})")
            continue

        print(f"[{idx}/{total}] {appid}: fetching...")
        success, norm, raw = fetch_one(appid, limiter)
        cache_put(conn, appid, success, raw)  # guard: only pre-release leads persist
        if success:
            ok += 1
            rows.append(norm)
            print(f"           -> {norm['game_name']}  [{norm['type']}]  email={norm['support_email'] or 'none'}")
        else:
            dead += 1
            print(f"           -> no store data (dead/region-locked/non-store)")

    if args.games_only:
        rows = [r for r in rows if r.get("type") == "game"]

    stamp = datetime.now(IST).strftime("%Y-%m-%d")
    out_csv = os.path.join(OUT_DIR, f"steam_apps_{stamp}.csv")
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(to_csv_row(r))

    print("\n" + "=" * 56)
    print(f"  done: {ok} with data, {dead} no-data, {cached} from cache")
    print(f"  CSV: {out_csv}  ({len(rows)} rows)")
    print(f"  data store: {pipeline.TURSO_URL or 'local cache.sqlite'}")
    print("=" * 56)
    conn.close()


if __name__ == "__main__":
    main()
