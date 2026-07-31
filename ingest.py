#!/usr/bin/env python3
"""
Ingest: pull messages from Telegram, route each into the shared SQLite inbox by
its "$tag", then advance the Telegram offset — but only up to what we actually
stored, never beyond.

Correctness invariants (why nothing is lost or reordered):
- getUpdates returns updates in strictly ascending, gap-free update_id order.
- We store the WHOLE returned batch, then confirm the offset only up to the max
  update_id of that batch. So we never confirm past a message we haven't stored;
  a batch beyond `limit` simply arrives on the next call. No skips.
- Durability-before-confirm: a message is written to SQLite before its update_id
  is confirmed/dropped on Telegram's side. A crash in between just re-fetches it.
- Idempotent (INSERT OR IGNORE on update_id), so parallel sessions ingesting the
  same updates, or a re-fetch after a crash, never duplicate.
"""
import time

import common
import db

GETUPDATES_LIMIT = 100  # Telegram max


def _attachment_file_id(msg):
    """file_id of an image in this message, or None.

    Two shapes matter: "photo" (compressed — Telegram sends an array of sizes,
    the last is the largest) and "document" (sent as a file, i.e. uncompressed —
    which is how screenshots usually arrive when quality matters).
    """
    photo = msg.get("photo")
    if photo:
        return photo[-1]["file_id"]
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        return doc.get("file_id")
    return None


def ingest(conn, token, chat_id):
    """Best-effort: on any error, return quietly — the caller still processes the
    inbox, and the next tick re-ingests. Returns number of new rows stored."""
    try:
        result = common.telegram_request(
            token, "getUpdates", {"offset": 0, "limit": GETUPDATES_LIMIT, "timeout": 0}
        )
    except Exception:
        return 0
    if not result.get("ok"):
        return 0

    updates = result.get("result", [])
    now = int(time.time())
    stored_max = 0
    new_count = 0

    for u in updates:
        uid = u["update_id"]
        stored_max = max(stored_max, uid)
        msg = u.get("message") or {}
        # Only accept messages from the configured owner; ignore anything else,
        # but still let the offset advance past it (foreign chatter / spam).
        if str(msg.get("chat", {}).get("id", "")) != chat_id:
            continue
        if str(msg.get("from", {}).get("id", "")) != chat_id:
            continue
        # A photo carries its text in "caption", not "text" — reading only the
        # latter would strip the routing tag off every image.
        text = msg.get("text") or msg.get("caption") or ""
        group_id = msg.get("media_group_id")
        tag = common.find_tag(text)
        if tag:
            owner, clean = tag, common.strip_tag(text, tag)
        else:
            # Albums arrive as one update per photo with the caption on the first
            # only, so inherit the owner the album was already routed to.
            owner, clean = db.owner_of_media_group(conn, group_id) or "unrouted", text

        media_path = None
        file_id = _attachment_file_id(msg)
        if file_id:
            media_path = common.download_file(token, file_id, str(uid))

        if db.store(conn, uid, owner, clean if clean else text, msg.get("date"), now,
                    media_path, group_id):
            new_count += 1

    conn.commit()
    db.prune(conn, now)
    conn.commit()

    # Confirm/drain everything we just processed (stored or intentionally ignored).
    if stored_max > 0:
        try:
            common.telegram_request(
                token, "getUpdates", {"offset": stored_max + 1, "limit": 1, "timeout": 0}
            )
        except Exception:
            pass

    return new_count
