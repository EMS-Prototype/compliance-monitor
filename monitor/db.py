"""SQLite storage for the compliance source monitor."""
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    authority TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'CENTRAL',
    url TEXT NOT NULL,
    selector TEXT NOT NULL DEFAULT '',          -- CSS selector for the region to watch; empty = whole body
    check_every_minutes INTEGER NOT NULL DEFAULT 360,
    respect_robots INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'new',         -- new | ok | blocked | error
    last_checked_at TEXT,
    last_hash TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    taken_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    text_path TEXT NOT NULL,
    screenshot_path TEXT
);
-- Review queue: a detected change waits here until a human approves or rejects it.
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    detected_at TEXT NOT NULL,
    old_snapshot_id INTEGER,
    new_snapshot_id INTEGER,
    summary TEXT NOT NULL,
    diff TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',     -- pending | approved | rejected
    reviewer TEXT,
    decided_at TEXT
);
-- Handoffs: the worker hit a CAPTCHA / OTP / login wall and stopped. A human takes over.
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,                         -- captcha | otp | login | blocked_http
    detail TEXT NOT NULL DEFAULT '',
    screenshot_path TEXT,
    status TEXT NOT NULL DEFAULT 'open',        -- open | resolved
    resolved_at TEXT,
    resolved_by TEXT,
    resolution TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # readers don't block the scheduler's writes
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


def add_source(conn, name, url, authority="", state="CENTRAL", selector="",
               check_every_minutes=360, respect_robots=True) -> int:
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    cur = conn.execute(
        "INSERT INTO sources (name, authority, state, url, selector, check_every_minutes,"
        " respect_robots, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (name, authority, state, url, selector, int(check_every_minutes),
         1 if respect_robots else 0, now()),
    )
    conn.commit()
    return cur.lastrowid
