"""v0.1 - Steam single-app fetcher.

Given a Steam appid, fetch the public storefront `appdetails` and print the
fields we can get directly from Steam. No API key, stdlib only.

Usage:
    python fetch_app.py 413150
    python fetch_app.py 413150 --json   # also dump raw + normalized JSON
"""
import argparse
import json
import sys
import urllib.request
import urllib.error

# Windows consoles default to cp1252 and choke on em-dashes / non-ASCII studio
# names and game briefs. Force UTF-8 so output is faithful everywhere.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

APPDETAILS = "https://store.steampowered.com/api/appdetails"
UA = "claude-steam-search/0.1 (research; contact via project owner)"


def fetch_appdetails(appid: int, cc: str = "us", lang: str = "english") -> dict | None:
    """Return the `data` dict for an appid, or None if Steam says success=false."""
    url = f"{APPDETAILS}?appids={appid}&cc={cc}&l={lang}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    entry = payload.get(str(appid))
    if not entry or not entry.get("success"):
        return None
    return entry.get("data")


def normalize(appid: int, data: dict) -> dict:
    """Pull just the fields our pipeline cares about into a flat dict."""
    rd = data.get("release_date") or {}
    support = data.get("support_info") or {}
    genres = [g.get("description") for g in (data.get("genres") or [])]
    return {
        "appid": appid,
        "type": data.get("type"),
        "game_name": data.get("name"),
        "brief": data.get("short_description"),
        "genres": genres,
        "developers": data.get("developers") or [],
        "publishers": data.get("publishers") or [],
        "studio_website": data.get("website"),
        "support_email": support.get("email") or "",
        "support_url": support.get("url") or "",
        "release_date": rd.get("date") or "",
        "coming_soon": bool(rd.get("coming_soon")),
        "page_link": f"https://store.steampowered.com/app/{appid}/",
        "is_free": data.get("is_free"),
    }


def print_summary(n: dict) -> None:
    print("=" * 64)
    print(f"  {n['game_name']}  (appid {n['appid']})")
    print("=" * 64)
    rows = [
        ("Type", n["type"]),
        ("Coming soon", n["coming_soon"]),
        ("Release date", n["release_date"]),
        ("Studio (dev)", ", ".join(n["developers"]) or "—"),
        ("Publisher", ", ".join(n["publishers"]) or "—"),
        ("Genres", ", ".join(n["genres"]) or "—"),
        ("Website", n["studio_website"] or "—"),
        ("Support email", n["support_email"] or "— (none on Steam)"),
        ("Support url", n["support_url"] or "—"),
        ("Page link", n["page_link"]),
        ("Brief", (n["brief"] or "")[:160]),
    ]
    for label, val in rows:
        print(f"  {label:<14}: {val}")
    print("-" * 64)
    # quick gut-check on what's still missing for the full task
    missing = [f for f in ("support_email",) if not n[f]]
    print(f"  Steam-derived OK. Still needs enrichment: email{'(no Steam email)' if missing else ''}, country, engine.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("appid", type=int)
    ap.add_argument("--json", action="store_true", help="dump raw + normalized JSON")
    ap.add_argument("--cc", default="us")
    args = ap.parse_args()

    try:
        data = fetch_appdetails(args.appid, cc=args.cc)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} from Steam (rate limit? try later).", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"Request failed: {e}", file=sys.stderr)
        return 2

    if data is None:
        print(f"No store data for appid {args.appid} (dead/region-locked/not a store app).")
        return 1

    n = normalize(args.appid, data)
    print_summary(n)
    if args.json:
        print("\n--- normalized ---")
        print(json.dumps(n, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
