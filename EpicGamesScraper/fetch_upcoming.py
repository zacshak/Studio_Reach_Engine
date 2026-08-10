"""Print the current Epic upcoming catalog without touching the database."""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from epic_client import EpicCatalogClient, EpicClientError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--country", default="US")
    parser.add_argument("--locale", default="en-US")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--source-url")
    args = parser.parse_args(argv)
    try:
        products = EpicCatalogClient(args.source_url, args.country, args.locale).fetch_upcoming(args.page_size)
    except (EpicClientError, ValueError) as exc:
        print(f"Epic fetch failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(products, ensure_ascii=False, indent=2))
    else:
        for product in products:
            print(f"{product['title']}\t{product['release_date'] or 'TBA'}\t{product['store_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
