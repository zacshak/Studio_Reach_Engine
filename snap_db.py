"""SRE --snap-db: dump the live Turso DB into a local SQLite file you can open in
DB Browser for SQLite. Read-only point-in-time snapshot — never writes to Turso.

    python snap_db.py            # -> last_cache.sqlite (gitignored)

It copies schema + rows for every table into a fresh standalone SQLite file (tables
first, then indexes/triggers/views — so triggers don't fire during the bulk load).
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "Claude_Lead_Discovery_Engine"))
import pipeline  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OUT = os.path.join(HERE, "last_cache.sqlite")


def main():
    if not pipeline.TURSO_URL:
        sys.exit("TURSO not configured in .env — nothing remote to snapshot.")
    if os.path.exists(OUT):
        os.remove(OUT)

    src = pipeline._rw()                      # shared Turso connection (read-only use here)
    dst = sqlite3.connect(OUT)

    # All schema objects with their DDL; tables first so data can load before
    # indexes/triggers/views are recreated.
    objs = src.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
        "WHEN 'view' THEN 2 ELSE 3 END").fetchall()
    tables = [name for typ, name, _ in objs if typ == "table"]

    for typ, name, sql in objs:              # create tables
        if typ == "table":
            dst.execute(sql)

    counts = {}
    for t in tables:                         # copy rows (positional INSERT)
        rows = src.execute(f'SELECT * FROM "{t}"').fetchall()
        if rows:
            placeholders = ",".join("?" * len(rows[0]))
            dst.executemany(f'INSERT INTO "{t}" VALUES ({placeholders})', rows)
        counts[t] = len(rows)

    for typ, name, sql in objs:              # then indexes / triggers / views
        if typ != "table":
            try:
                dst.execute(sql)
            except sqlite3.Error:
                pass                          # skip anything plain sqlite won't accept

    dst.commit()
    dst.close()

    print(f"snapshot -> {OUT}  ({os.path.getsize(OUT) // 1024} KB)")
    for t in tables:
        print(f"  {t:<24} {counts.get(t, 0):>7} rows")
    print("\nopen it in DB Browser for SQLite.")


if __name__ == "__main__":
    main()
