"""SQLite storage: app settings (master password hash, secret key) and synopsis jobs.

The database file lives in DATA_DIR (on Railway: a mounted volume, e.g. /data), so it
survives restarts and redeploys. Files (uploads, Word output) live next to it in
DATA_DIR/jobs/<job id>/.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "app.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    finished TEXT,
    status TEXT NOT NULL,            -- queued | running | done | failed
    step TEXT DEFAULT '',            -- latest progress message
    title TEXT DEFAULT '',
    files TEXT DEFAULT '[]',         -- JSON list of uploaded file names
    mode TEXT DEFAULT 'detailed',
    double_check INTEGER DEFAULT 1,
    error TEXT DEFAULT '',
    usage TEXT DEFAULT '{}',         -- JSON (provider, tokens, seconds)
    counts TEXT DEFAULT '{}',        -- JSON {"error": n, "warning": n, "info": n}
    outputs TEXT DEFAULT '{}'        -- JSON {"docx": name, "review": name, "json": name}
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init() -> None:
    with _lock, connect() as con:
        con.executescript(SCHEMA)
        con.execute("PRAGMA journal_mode=WAL")


# ---------------------------------------------------------------- settings
def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock, connect() as con:
        con.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---------------------------------------------------------------- jobs
JSON_COLS = ("files", "usage", "counts", "outputs")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for c in JSON_COLS:
        try:
            d[c] = json.loads(d.get(c) or ("[]" if c == "files" else "{}"))
        except ValueError:
            d[c] = [] if c == "files" else {}
    return d


def create_job(job_id: str, files: list[str], mode: str, double_check: bool) -> None:
    with _lock, connect() as con:
        con.execute("INSERT INTO jobs(id, created, status, step, files, mode, double_check) "
                    "VALUES(?, ?, 'queued', 'Waiting to start…', ?, ?, ?)",
                    (job_id, now(), json.dumps(files), mode, int(double_check)))


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    for c in JSON_COLS:
        if c in fields and not isinstance(fields[c], str):
            fields[c] = json.dumps(fields[c])
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, connect() as con:
        con.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))


def get_job(job_id: str) -> dict | None:
    with connect() as con:
        return _row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def list_jobs(limit: int = 200) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def delete_job(job_id: str) -> None:
    with _lock, connect() as con:
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def mark_interrupted() -> None:
    """Jobs left 'running' by a restart can never finish - mark them failed."""
    with _lock, connect() as con:
        con.execute("UPDATE jobs SET status='failed', error='The server restarted while this "
                    "synopsis was being made. Run it again.', finished=? "
                    "WHERE status IN ('queued', 'running')", (now(),))


def job_dir(job_id: str) -> Path:
    return DATA_DIR / "jobs" / job_id
