"""Background export of SEEDED leads for human approval.

When a lead is seeded (Steam already gave us an email), we stage it for review:
a folder per game under Approval_Pending_Games/, containing the full newly_added
row as JSON plus its screenshot thumbnails. This is pure I/O (mkdir + downloads),
so it runs on a thread pool and never blocks the fetch loop that submits to it.

Usage (from the fetch loop):
    exp = ApprovalExporter()
    ...
    exp.submit(appid)        # fire-and-forget, returns immediately
    ...
    exp.close()              # drain + log at end of run

Standalone (export one appid, e.g. to test):
    python approval_export.py 1515540
"""
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pipeline  # same folder; the DB contract (read_lead + connections)

HERE = os.path.dirname(os.path.abspath(__file__))
# repo-root/Approval_Pending_Games (one level up from this engine folder)
DEFAULT_BASE = os.path.join(os.path.dirname(HERE), "Approval_Pending_Games")
UA = "claude-lead-discovery/1.0 (approval-export)"

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name):
    """A filesystem-safe folder segment from a game name."""
    cleaned = _ILLEGAL.sub("_", (name or "").strip()).rstrip(". ")
    return cleaned[:80] or "game"


def _jload(v):
    try:
        return json.loads(v) if isinstance(v, str) else v
    except json.JSONDecodeError:
        return None


def _curate(lead, appid):
    """The fields wanted in the approval JSON. JSON columns are parsed back to
    real nested JSON; steampage_url is built from the appid."""
    return {
        "gameName": lead.get("name"),
        "detailed": lead.get("detailed_description"),
        "about": lead.get("about_the_game"),
        "short_description": lead.get("short_description"),
        "supported_languages": lead.get("supported_languages"),
        "developers": _jload(lead.get("developers")),
        "publishers": _jload(lead.get("publishers")),
        "steampage_url": f"https://store.steampowered.com/app/{appid}/",
        "genres": _jload(lead.get("genres")),
        "release_date": _jload(lead.get("release_date")),
    }


class ApprovalExporter:
    """Thread-pool exporter. Thread-safe to submit() from the main loop.

    One task per appid; tasks run concurrently across the pool. A task is a no-op
    if the lead isn't seeded or was already exported (so re-runs are cheap and
    safe). A failing task is recorded, never raised — one bad lead can't stall
    the others or the run.
    """

    def __init__(self, base_dir=DEFAULT_BASE, max_workers=8, timeout=20, retries=3):
        self.base_dir = base_dir
        self.timeout = timeout
        self.retries = retries
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="approval")
        self._futures = []
        self.errors = []        # (appid, repr(exception))

    # -- public ----------------------------------------------------------
    def submit(self, appid):
        """Queue one appid for export. Returns immediately (non-blocking)."""
        self._futures.append(self._pool.submit(self._export_one, appid))

    def close(self, wait=True):
        """Drain the pool. Returns (exported, errors). Call once at run end.
        The count is derived from task results, so it can't race."""
        self._pool.shutdown(wait=wait)
        exported = sum(1 for f in self._futures if f.result() is True)
        return exported, self.errors

    # -- worker ----------------------------------------------------------
    def _export_one(self, appid):
        """Returns True if it staged a folder, False if skipped/failed."""
        try:
            if _scrape_status(appid) != "seeded":
                return False                # only seeded leads get staged
            lead = pipeline.read_lead(appid)
            if not lead:
                return False
            folder = os.path.join(self.base_dir, f"{_safe(lead.get('name'))}_{appid}")
            marker = os.path.join(folder, ".complete")
            if os.path.exists(marker):
                return False                # already exported — idempotent skip
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"{appid}.json"), "w", encoding="utf-8") as f:
                json.dump(_curate(lead, appid), f, ensure_ascii=False, indent=2)
            for i, shot in enumerate(_jload(lead.get("screenshots")) or []):
                url = shot.get("path_thumbnail")
                if url:
                    self._download(url, os.path.join(folder, f"screenshot_{i:02d}.jpg"))
            open(marker, "w").close()       # write LAST: marks the folder complete
            return True
        except Exception as e:              # never let one lead kill the pool
            self.errors.append((appid, repr(e)))
            return False

    def _download(self, url, dest):
        if os.path.exists(dest):
            return
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r, \
                        open(dest, "wb") as f:
                    shutil.copyfileobj(r, f)
                return
            except Exception:
                if attempt == self.retries - 1:
                    if os.path.exists(dest):
                        os.remove(dest)     # don't leave a half-written file
                    raise
                time.sleep(1.5 * (attempt + 1))


def _scrape_status(appid):
    """Read scrape_status for one appid via pipeline's read-only handle."""
    from contextlib import closing
    with closing(pipeline._ro()) as conn:
        row = conn.execute(
            "SELECT scrape_status FROM scrape_tracker WHERE appid=?", (appid,)).fetchone()
    return row[0] if row else None


def _all_seeded():
    from contextlib import closing
    with closing(pipeline._ro()) as conn:
        return [r[0] for r in conn.execute(
            "SELECT appid FROM scrape_tracker WHERE scrape_status='seeded' ORDER BY appid")]


if __name__ == "__main__":
    # Manual run: export specific appids, or every seeded lead with --all.
    #   python approval_export.py --all
    #   python approval_export.py 1515540 1778510
    args = sys.argv[1:]
    if not args:
        sys.exit("usage: python approval_export.py --all | <appid> [appid ...]")
    appids = _all_seeded() if args[0] == "--all" else [int(a) for a in args]
    print(f"exporting {len(appids)} lead(s)...")
    exp = ApprovalExporter()
    for a in appids:
        exp.submit(a)
    done, errs = exp.close()
    print(f"staged {done} new lead(s) to {exp.base_dir} "
          f"({len(appids) - done - len(errs)} already done, {len(errs)} error(s))")
    for appid, err in errs:
        print(f"  ERROR {appid}: {err}")
