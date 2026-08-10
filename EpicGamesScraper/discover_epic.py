"""Discover newly observed Epic Games upcoming titles.

Usage:
    python EpicGamesScraper/discover_epic.py
    python EpicGamesScraper/discover_epic.py --bootstrap
    python EpicGamesScraper/discover_epic.py --db path/to/epic.sqlite
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import epic_db  # noqa: E402
from epic_client import EpicCatalogClient, EpicClientError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Epic-only SQLite file")
    parser.add_argument("--country", default="US")
    parser.add_argument("--locale", default="en-US")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--source-url", help="Override the Epic catalog provider base URL")
    parser.add_argument("--bootstrap", action="store_true", help="Store a baseline without reporting new games")
    args = parser.parse_args(argv)

    client = EpicCatalogClient(args.source_url, args.country, args.locale)
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        products = client.fetch_upcoming(args.page_size)
    except (EpicClientError, ValueError) as exc:
        print(f"Epic discovery failed: {exc}", file=sys.stderr)
        return 2

    conn = epic_db.connect(args.db)
    try:
        epic_db.init_db(conn)
        known_before = conn.execute(
            "SELECT COUNT(*) FROM known_comingsoon"
        ).fetchone()[0]
        bootstrap = args.bootstrap or known_before == 0
        new_products = epic_db.apply_snapshot(
            conn, products, run_at, client.base_url, bootstrap=bootstrap
        )
        detail_products = epic_db.detail_keys_needed(conn, products)
        detail_ok = 0
        detail_failed = 0
        for product in detail_products:
            try:
                detail = client.fetch_offer(product["offer_id"])
                epic_db.record_detail(conn, product, detail, run_at)
                detail_ok += 1
            except EpicClientError as exc:
                epic_db.record_detail(conn, product, None, run_at, str(exc))
                detail_failed += 1
    finally:
        conn.close()

    print(f"Epic upcoming snapshot: {len(products)} unique base games")
    database = (
        args.db
        or os.environ.get("EPIC_DB_PATH")
        or os.environ.get("EPIC_TURSO_DATABASE_URL")
        or epic_db.DEFAULT_DB_PATH
    )
    print(f"Epic database: {database}")
    print(f"Epic details: {detail_ok} fetched, {detail_failed} failed")
    if bootstrap:
        print("Baseline stored. New-game reporting starts on the next run.")
        return 0
    if not new_products:
        print("No newly observed Epic upcoming games.")
        return 0

    print(f"New Epic games: {len(new_products)}")
    for product in new_products:
        print(f"  {product['title']} [{product['epic_key']}] {product['store_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
