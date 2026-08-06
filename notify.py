#!/usr/bin/env python3
"""
Fire-and-forget status message to Telegram — no reply expected.

Usage:
  python3 notify.py --session <session_id> --message "<status text>"
  python3 notify.py --session <session_id> --message "<text>" --done
  python3 notify.py --session <session_id> --message "<html>" --format rich
  python3 notify.py --session <session_id> --message-file report.html --format rich

Форматированные отчёты удобнее передавать файлом: длинная разметка в аргументе
командной строки рвётся на кавычках и переносах.
"""
import argparse

import common
import db


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--message")
    parser.add_argument(
        "--message-file",
        help="прочитать текст сообщения из файла (для длинной разметки)",
    )
    parser.add_argument(
        "--format",
        choices=("plain", "html", "rich"),
        default="plain",
        help="plain (по умолчанию) | html (parse_mode=HTML) | rich (sendRichMessage)",
    )
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

    if not args.message and not args.message_file:
        parser.error("нужен --message или --message-file")
    text = args.message
    if args.message_file:
        with open(args.message_file, encoding="utf-8") as f:
            text = f.read()

    config = common.load_config()
    # Заголовок сессии тем же форматом, что и тело: в plain это просто текст,
    # в html/rich — жирный, иначе теги приехали бы буквально.
    head = f"🔔 [{args.session}]"
    if args.format in ("html", "rich"):
        head = f"<b>🔔 [{args.session}]</b>"
        body = f"{head}\n{text}" if args.format == "html" else f"{head}<br>{text}"
    else:
        body = f"{head} {text}"

    used = common.send_message(
        config["token"], str(config["chat_id"]), body, mode=args.format
    )
    # Печатаем реально применённый формат: если rich не прошёл и сообщение
    # ушло html/plain, об этом надо знать, а не гадать по виду в телефоне.
    if used != args.format:
        print(f"формат понижен: {args.format} → {used}")

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
