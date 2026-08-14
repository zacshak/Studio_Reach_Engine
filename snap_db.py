"""SRE --snap-db: dump both live Turso DBs into local SQLite files you can open in
DB Browser for SQLite. Read-only point-in-time snapshots — never write to Turso.

    python snap_db.py            # -> last_cache.sqlite + last_epic_cache.sqlite

It copies schema + rows for every table into a fresh standalone SQLite file (tables
first, then indexes/triggers/views — so triggers don't fire during the bulk load).
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "Claude_Lead_Discovery_Engine"))
sys.path.insert(0, os.path.join(HERE, "EpicGamesScraper"))
import pipeline  # noqa: E402
import epic_db  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OUT = os.path.join(HERE, "last_cache.sqlite")
EPIC_OUT = os.path.join(HERE, "last_epic_cache.sqlite")


def snapshot(src, out, label):
    """Copy one remote SQLite-compatible database into a standalone file."""
    tmp = out + ".new"
    if os.path.exists(tmp):
        os.remove(tmp)

    dst = sqlite3.connect(tmp)
    try:
        objs = src.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
            "WHEN 'view' THEN 2 ELSE 3 END").fetchall()
        tables = [name for typ, name, _ in objs if typ == "table"]

        for typ, name, sql in objs:
            if typ == "table":
                dst.execute(sql)

        counts = {}
        for table in tables:
            rows = src.execute(f'SELECT * FROM "{table}"').fetchall()
            if rows:
                placeholders = ",".join("?" * len(rows[0]))
                dst.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)
            counts[table] = len(rows)

        for typ, name, sql in objs:
            if typ != "table":
                try:
                    dst.execute(sql)
                except sqlite3.Error as exc:
                    raise RuntimeError(f"could not recreate {typ} {name!r}: {exc}") from exc
        dst.commit()
    finally:
        dst.close()

    dest = out
    try:
        os.replace(tmp, out)
    except PermissionError:
        from datetime import datetime
        dest = out.replace(".sqlite", datetime.now().strftime("_%Y%m%d_%H%M%S.sqlite"))
        os.replace(tmp, dest)
        print(f"NOTE: {os.path.basename(out)} is open — wrote to "
              f"{os.path.basename(dest)} instead.")

    print(f"{label} snapshot -> {dest}  ({os.path.getsize(dest) // 1024} KB)")
    for table in tables:
        print(f"  {table:<24} {counts[table]:>7} rows")


def main():
    if not pipeline.TURSO_URL:
        sys.exit("Steam TURSO credentials are missing — nothing remote to snapshot.")
    if not os.environ.get("EPIC_TURSO_DATABASE_URL") or not os.environ.get("EPIC_TURSO_AUTH_TOKEN"):
        sys.exit("Epic TURSO credentials are missing — set EPIC_TURSO_DATABASE_URL and EPIC_TURSO_AUTH_TOKEN in .env.")

    snapshot(pipeline._rw(), OUT, "Steam")
    epic = epic_db.connect()
    try:
        snapshot(epic, EPIC_OUT, "Epic")
    finally:
        epic.close()
    print("\nOpen last_cache.sqlite and last_epic_cache.sqlite in DB Browser for SQLite.")


if __name__ == "__main__":
    main()
