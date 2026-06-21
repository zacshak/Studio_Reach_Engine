@echo off
REM Opens cache.sqlite in DB Browser READ-ONLY (-R) so you can keep the window
REM open while pipeline.py / run_daily.py / Hermes write to it. Read-only takes
REM no write lock, so it never blocks our writer. Press F5 in DB Browser to
REM refresh the view after a run (read-only doesn't auto-update).
start "" "C:\Program Files\DB Browser for SQLite\DB Browser for SQLite.exe" -R "%~dp0cache.sqlite"
