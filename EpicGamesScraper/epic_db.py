"""Storage for the independent Epic Games pipeline."""
from __future__ import annotations

import json
import os
import sqlite3

try:
    import libsql
except ImportError:  # Local SQLite development does not need the remote driver.
    libsql = None


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(HERE, "epic_cache.sqlite")


class _Rows:
    """Make libsql cursors behave like the sqlite cursors used by this module."""

    def __init__(self, cursor):
        self._rows = cursor.fetchall()

    def __iter__(self):
        return iter(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _RemoteConnection:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, *args):
        return _Rows(self._raw.execute(*args))

    def commit(self):
        return self._raw.commit()

    def rollback(self):
        return self._raw.rollback()

    def close(self):
        return self._raw.close()


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def connect(path: str | None = None):
    """Open Epic-only SQLite, or Epic-only Turso when explicitly configured."""
    local_path = path or os.environ.get("EPIC_DB_PATH")
    if local_path:
        conn = sqlite3.connect(local_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    url = os.environ.get("EPIC_TURSO_DATABASE_URL")
    token = os.environ.get("EPIC_TURSO_AUTH_TOKEN")
    if bool(url) != bool(token):
        raise RuntimeError("EPIC_TURSO_DATABASE_URL and EPIC_TURSO_AUTH_TOKEN must be set together")
    if url:
        if libsql is None:
            raise RuntimeError("libsql is required for EPIC_TURSO_DATABASE_URL")
        return _RemoteConnection(libsql.connect(url, auth_token=token))
    if _enabled(os.environ.get("EPIC_TURSO_REQUIRED")):
        raise RuntimeError("Epic Turso credentials are required but were not provided")

    conn = sqlite3.connect(DEFAULT_DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn) -> None:
    for statement in (
        """
        CREATE TABLE IF NOT EXISTS known_comingsoon (
            epic_key       TEXT PRIMARY KEY,
            namespace      TEXT NOT NULL,
            offer_id       TEXT NOT NULL,
            title          TEXT NOT NULL,
            first_seen     TEXT NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS snapshot_runs (
            run_at          TEXT PRIMARY KEY,
            total_games     INTEGER NOT NULL,
            new_games       INTEGER NOT NULL,
            mode            TEXT NOT NULL,
            source          TEXT NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS epic_tracker (
            epic_key          TEXT PRIMARY KEY,
            namespace         TEXT NOT NULL,
            offer_id          TEXT NOT NULL,
            title             TEXT NOT NULL,
            short_description TEXT,
            developers        TEXT,
            publishers        TEXT,
            genres            TEXT,
            tags              TEXT,
            release_date      TEXT,
            pc_release_date   TEXT,
            store_url         TEXT,
            source            TEXT NOT NULL,
            scrape_status     TEXT NOT NULL DEFAULT 'pending',
            emails            TEXT,
            Mail_status       TEXT NOT NULL DEFAULT 'Pending',
            discovered_on     TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            raw_json          TEXT NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS epic_details (
            epic_key          TEXT PRIMARY KEY,
            offer_id          TEXT NOT NULL,
            long_description  TEXT,
            key_images        TEXT,
            viewable_date     TEXT,
            fetched_at        TEXT NOT NULL,
            status            TEXT NOT NULL,
            error             TEXT,
            raw_json          TEXT
        );
        """
    ):
        conn.execute(statement)
    conn.commit()


def apply_snapshot(
    conn,
    products: list[dict],
    run_at: str,
    source: str,
    bootstrap: bool = False,
) -> list[dict]:
    """Persist one complete snapshot and return newly observed products."""
    init_db(conn)
    known = {row[0] for row in conn.execute("SELECT epic_key FROM known_comingsoon")}
    new_products = [p for p in products if p["epic_key"] not in known]
    mode = "bootstrap" if bootstrap else "diff"

    try:
        for product in products:
            key = product["epic_key"]
            conn.execute(
                """INSERT OR IGNORE INTO known_comingsoon
                   (epic_key, namespace, offer_id, title, first_seen)
                   VALUES (?, ?, ?, ?, ?)""",
                (key, product["namespace"], product["offer_id"], product["title"], run_at),
            )
            conn.execute(
                """INSERT INTO epic_tracker
                   (epic_key, namespace, offer_id, title, short_description,
                    developers, publishers, genres, tags, release_date,
                    pc_release_date, store_url, source, discovered_on, updated_at, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(epic_key) DO UPDATE SET
                    namespace=excluded.namespace,
                    offer_id=excluded.offer_id,
                    title=excluded.title,
                    short_description=excluded.short_description,
                    developers=excluded.developers,
                    publishers=excluded.publishers,
                    genres=excluded.genres,
                    tags=excluded.tags,
                    release_date=excluded.release_date,
                    pc_release_date=excluded.pc_release_date,
                    store_url=excluded.store_url,
                    source=excluded.source,
                    updated_at=excluded.updated_at,
                    raw_json=excluded.raw_json""",
                (
                    key, product["namespace"], product["offer_id"], product["title"],
                    product["short_description"], product["developers"], product["publishers"],
                    json.dumps(product["genres"], ensure_ascii=False),
                    json.dumps(product["tags"], ensure_ascii=False), product["release_date"],
                    product["pc_release_date"], product["store_url"], source, run_at, run_at,
                    json.dumps(product["raw"], ensure_ascii=False),
                ),
            )
        conn.execute(
            "INSERT INTO snapshot_runs(run_at, total_games, new_games, mode, source) VALUES (?, ?, ?, ?, ?)",
            (run_at, len(products), len(new_products), mode, source),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return new_products


def detail_keys_needed(conn, products: list[dict]) -> list[dict]:
    """Return products without a successful detail record."""
    statuses = {
        row[0]: row[1]
        for row in conn.execute("SELECT epic_key, status FROM epic_details").fetchall()
    }
    return [p for p in products if statuses.get(p["epic_key"]) != "complete"]


def record_detail(
    conn,
    product: dict,
    detail: dict | None,
    fetched_at: str,
    error: str | None = None,
) -> None:
    """Persist one detail response or a retryable failure."""
    raw = detail or {}
    conn.execute(
        """INSERT INTO epic_details
           (epic_key, offer_id, long_description, key_images, viewable_date,
            fetched_at, status, error, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(epic_key) DO UPDATE SET
            offer_id=excluded.offer_id,
            long_description=excluded.long_description,
            key_images=excluded.key_images,
            viewable_date=excluded.viewable_date,
            fetched_at=excluded.fetched_at,
            status=excluded.status,
            error=excluded.error,
            raw_json=excluded.raw_json""",
        (
            product["epic_key"], product["offer_id"],
            raw.get("longDescription"),
            json.dumps(raw.get("keyImages") or [], ensure_ascii=False),
            raw.get("viewableDate"), fetched_at,
            "complete" if detail else "failed", error,
            json.dumps(raw, ensure_ascii=False) if detail else None,
        ),
    )
    conn.commit()
