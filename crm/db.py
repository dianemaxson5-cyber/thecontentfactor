"""SQLite storage: connection handling, schema, and small query helpers."""
import os
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

STAGES = ["lead", "prospect", "customer", "other"]
DEAL_STAGES = ["new", "qualified", "proposal", "won", "lost"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'lead',
    source TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    unsubscribed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_contact ON activities(contact_id, created_at);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'new',
    close_date TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    segment_stage TEXT NOT NULL DEFAULT '',
    segment_tag TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    sent_count INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequences (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    trigger_stage TEXT NOT NULL DEFAULT '',
    trigger_tag TEXT NOT NULL DEFAULT '',
    stop_on_customer INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_steps (
    id INTEGER PRIMARY KEY,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    delay_days INTEGER NOT NULL DEFAULT 0,
    subject TEXT NOT NULL,
    body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    next_step INTEGER NOT NULL DEFAULT 0,
    next_send_at TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    enrolled_at TEXT NOT NULL,
    UNIQUE (sequence_id, contact_id)
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    to_email TEXT NOT NULL,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    sequence_id INTEGER REFERENCES sequences(id) ON DELETE SET NULL,
    subject TEXT NOT NULL,
    body_html TEXT NOT NULL,
    body_text TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL,
    open_count INTEGER NOT NULL DEFAULT 0,
    opened_at TEXT,
    click_count INTEGER NOT NULL DEFAULT 0,
    clicked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_contact ON emails(contact_id);
"""


def now():
    """Current UTC time as a sortable string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    if "db" not in g:
        path = current_app.config["DATABASE"]
        g.db = sqlite3.connect(path)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    path = app.config["DATABASE"]
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    app.teardown_appcontext(close_db)


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


def get_setting(key, default=""):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def log_activity(contact_id, kind, subject="", detail=""):
    execute(
        "INSERT INTO activities (contact_id, kind, subject, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (contact_id, kind, subject, detail, now()),
    )


def normalize_tags(raw):
    """'VIP, Newsletter ,vip' -> 'vip,newsletter'."""
    seen = []
    for tag in (raw or "").split(","):
        tag = tag.strip().lower()
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen)


def segment_where(stage="", tag=""):
    """SQL filter for a contact segment. Returns (clause, args)."""
    clauses, args = [], []
    if stage:
        clauses.append("stage = ?")
        args.append(stage)
    if tag:
        clauses.append("(',' || tags || ',') LIKE ?")
        args.append(f"%,{tag.strip().lower()},%")
    return (" AND ".join(clauses) or "1=1"), args
