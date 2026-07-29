#!/usr/bin/env python3
"""
SQLite inbox shared by all sessions of the claude-to-telegram skill.

Why a DB: `getUpdates` is a single-consumer API — several sessions polling one
bot can't each safely advance the offset. The fix: whichever session polls,
routes EVERY message (by its "$tag") into this DB, then advances the offset. Once
a message is durably here, Telegram no longer needs to hold it, so the queue
stays drained and the "newest messages fall outside the getUpdates window"
failure can't happen. Each session then processes only its own inbox rows.

One row per Telegram `update_id` (PRIMARY KEY) — ingest is idempotent, so
concurrent/duplicate polls from parallel sessions never double-insert. WAL mode
lets those parallel session processes read/write concurrently.

status: not_processed -> read  (delivered to the owning session's model).
A message for a session whose poller is dead simply waits as not_processed until
that session runs again — so nothing is lost when a session is closed/crashes.
"""
import os
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "messages.db")
PRUNE_AGE_SEC = 7 * 24 * 3600  # drop anything older than 7 days regardless of status


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages(
            update_id   INTEGER PRIMARY KEY,
            session_id  TEXT    NOT NULL,
            text        TEXT    NOT NULL,
            tg_date     INTEGER,
            received_at INTEGER NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'not_processed'
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sess_status ON messages(session_id, status, update_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_received ON messages(received_at)")
    conn.commit()


def store(conn, update_id, session_id, text, tg_date, received_at):
    """Idempotent insert (dedup by update_id). Returns True if a new row was added."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages(update_id, session_id, text, tg_date, received_at) VALUES(?,?,?,?,?)",
        (update_id, session_id, text, tg_date, received_at),
    )
    return cur.rowcount > 0


def inbox(conn, session_id):
    """Unprocessed messages for this session, in arrival order."""
    cur = conn.execute(
        "SELECT update_id, text FROM messages WHERE session_id=? AND status='not_processed' ORDER BY update_id",
        (session_id,),
    )
    return cur.fetchall()


def mark(conn, update_id, status):
    conn.execute("UPDATE messages SET status=? WHERE update_id=?", (status, update_id))


def prune(conn, now_epoch):
    conn.execute("DELETE FROM messages WHERE received_at < ?", (now_epoch - PRUNE_AGE_SEC,))
