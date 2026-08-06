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
    # Added later for attachments; ALTER on an existing inbox rather than a
    # rebuild, so a DB created by an older version keeps its pending messages.
    existing = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    for column in ("media_path TEXT", "media_group_id TEXT"):
        if column.split()[0] not in existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {column}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sess_status ON messages(session_id, status, update_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_received ON messages(received_at)")
    conn.commit()


def store(conn, update_id, session_id, text, tg_date, received_at, media_path=None, media_group_id=None):
    """Idempotent insert (dedup by update_id). Returns True if a new row was added."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages"
        "(update_id, session_id, text, tg_date, received_at, media_path, media_group_id)"
        " VALUES(?,?,?,?,?,?,?)",
        (update_id, session_id, text, tg_date, received_at, media_path, media_group_id),
    )
    return cur.rowcount > 0


def owner_of_media_group(conn, media_group_id):
    """Session that already owns this album, if any.

    Telegram splits an album into one update per photo and puts the caption
    (hence the routing tag) only on the first. Without this lookup every photo
    after the first would fall through to "unrouted".
    """
    if not media_group_id:
        return None
    cur = conn.execute(
        "SELECT session_id FROM messages WHERE media_group_id=? AND session_id!='unrouted'"
        " ORDER BY update_id LIMIT 1",
        (media_group_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def inbox(conn, session_id):
    """Unprocessed messages for this session, in arrival order."""
    cur = conn.execute(
        "SELECT update_id, text, media_path FROM messages"
        " WHERE session_id=? AND status='not_processed' ORDER BY update_id",
        (session_id,),
    )
    return cur.fetchall()


def close_in_progress(conn, session_id):
    """Пометить взятые в работу сообщения сессии как обработанные.

    Возвращает число закрытых. Трогает только СВОЮ сессию: у каждой свой
    рабочий цикл, и чужие незакрытые задачи закрывать нельзя.
    """
    cur = conn.execute(
        "UPDATE messages SET status='read'"
        " WHERE session_id=? AND status='in_progress'",
        (session_id,),
    )
    return cur.rowcount


def mark(conn, update_id, status):
    conn.execute("UPDATE messages SET status=? WHERE update_id=?", (status, update_id))


def prune(conn, now_epoch):
    cutoff = now_epoch - PRUNE_AGE_SEC
    # Delete the files before the rows, otherwise the paths are gone and the
    # downloads leak into MEDIA_DIR forever.
    for (path,) in conn.execute(
        "SELECT media_path FROM messages WHERE received_at < ? AND media_path IS NOT NULL", (cutoff,)
    ):
        try:
            os.remove(path)
        except OSError:
            pass
    conn.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
