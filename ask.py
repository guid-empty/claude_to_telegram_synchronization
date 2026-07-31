#!/usr/bin/env python3
"""
Send a question to Telegram, block until a reply addressed to this session
arrives (via the shared SQLite inbox), print the reply text and exit 0.

Usage:
  python3 ask.py --session <session_id> --message "<question text>" [--timeout SECONDS]

On timeout: prints "NO_RESPONSE_TIMEOUT" and exits 1.

The reply must carry this session's routing tag — a word "$<session_id>" (e.g.
"$my-session ok"). Note: if a background polling cron is running for the same
session, it and this loop both read the same inbox; prefer this only for
foreground/interactive asks, or accept that either may deliver the reply.
"""
import argparse
import sys
import time

import common
import db
from ingest import ingest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    config = common.load_config()
    token = config["token"]
    chat_id = str(config["chat_id"])

    conn = db.get_conn()
    db.init(conn)

    # Ignore anything already waiting from before the question was asked.
    for update_id, _, _ in db.inbox(conn, args.session):
        db.mark(conn, update_id, "read")
    conn.commit()

    common.send_message(
        token, chat_id,
        f"🤖 [{args.session}] {args.message}\n\n(в ответе укажи ${args.session} — например в начале сообщения)",
    )

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        ingest(conn, token, chat_id)
        rows = db.inbox(conn, args.session)
        if rows:
            update_id, text, media_path = rows[0]
            db.mark(conn, update_id, "read")
            conn.commit()
            conn.close()
            if text:
                print(text)
            if media_path:
                print(f"[image: {media_path}]")
            sys.exit(0)
        time.sleep(2)

    conn.close()
    print("NO_RESPONSE_TIMEOUT")
    sys.exit(1)


if __name__ == "__main__":
    main()
