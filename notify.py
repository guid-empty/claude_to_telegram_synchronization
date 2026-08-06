#!/usr/bin/env python3
"""
Fire-and-forget status message to Telegram — no reply expected.

Usage:
  python3 notify.py --session <session_id> --message "<status text>"
  python3 notify.py --session <session_id> --message "<text>" --done
"""
import argparse

import common
import db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--message", required=True)
    # Закрыть запросы, взятые в работу через check_new.py --defer-read.
    # Смысл разделения: сообщение доставлено ≠ запрос отработан. Пока задача не
    # закрыта, она видна в базе как незакрытая, и по ней понятно, что сессия
    # взяла её и ещё не отчиталась.
    parser.add_argument(
        "--done",
        action="store_true",
        help="mark this session's in_progress messages as read after sending",
    )
    args = parser.parse_args()

    config = common.load_config()
    common.send_message(config["token"], str(config["chat_id"]), f"🔔 [{args.session}] {args.message}")

    # Закрываем ТОЛЬКО после успешной отправки: если сообщение не ушло, запрос
    # остаётся незакрытым — это честнее, чем потерять его молча.
    if args.done:
        conn = db.get_conn()
        db.init(conn)
        closed = db.close_in_progress(conn, args.session)
        conn.commit()
        conn.close()
        print(f"закрыто запросов: {closed}")


if __name__ == "__main__":
    main()
