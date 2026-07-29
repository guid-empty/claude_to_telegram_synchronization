#!/usr/bin/env python3
"""
Background poll for one session: ingest Telegram into the shared SQLite inbox,
then deliver this session's own unprocessed messages.

Usage:
  python3 check_new.py --session <session_id>

Output (stdout):
  - one line per new message addressed to this session (routing tag stripped),
    or "NOTHING_NEW"
  - always a final line "RESCHEDULE=<minutes>" or "RESCHEDULE=none" (progressive
    back-off — the caller reschedules the polling cron only when a number is given).
"""
import argparse
import json
import os

import common
import db
from ingest import ingest

INTERVAL_LADDER = [2, 5, 10, 20]
EMPTY_STREAK_PER_STEP = 3


def backoff_path(session):
    return os.path.join(common.SCRIPT_DIR, f".backoff_{session}.json")


def load_backoff(session):
    path = backoff_path(session)
    if not os.path.exists(path):
        return {"level": 0, "empty_streak": 0}
    try:
        with open(path) as f:
            s = json.load(f)
        return {"level": int(s.get("level", 0)), "empty_streak": int(s.get("empty_streak", 0))}
    except (json.JSONDecodeError, IOError, ValueError):
        return {"level": 0, "empty_streak": 0}


def save_backoff(session, state):
    with open(backoff_path(session), "w") as f:
        json.dump(state, f)


def interval_for_level(level):
    return INTERVAL_LADDER[min(max(level, 0), len(INTERVAL_LADDER) - 1)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    args = parser.parse_args()

    config = common.load_config()
    token = config["token"]
    chat_id = str(config["chat_id"])

    conn = db.get_conn()
    db.init(conn)

    backoff = load_backoff(args.session)
    prev_interval = interval_for_level(backoff["level"])

    # 1) Ingest is best-effort (drains Telegram into the DB for ALL sessions).
    ingest(conn, token, chat_id)

    # 2) Always process our own inbox, even if the ingest above failed.
    rows = db.inbox(conn, args.session)
    if rows:
        backoff = {"level": 0, "empty_streak": 0}  # message arrived -> poll fast again
        for update_id, text in rows:
            print(text)
            db.mark(conn, update_id, "read")
        conn.commit()
    else:
        backoff["empty_streak"] += 1
        if backoff["empty_streak"] >= EMPTY_STREAK_PER_STEP and backoff["level"] < len(INTERVAL_LADDER) - 1:
            backoff["level"] += 1
            backoff["empty_streak"] = 0
        print("NOTHING_NEW")

    conn.close()
    save_backoff(args.session, backoff)

    new_interval = interval_for_level(backoff["level"])
    print(f"RESCHEDULE={new_interval}" if new_interval != prev_interval else "RESCHEDULE=none")


if __name__ == "__main__":
    main()
