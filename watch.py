#!/usr/bin/env python3
"""
Доставка сообщений для engine=monitor: длинный процесс под Monitor-тулом.

Отличие от check_new.py (engine=cron) — в том, КОГДА сессия узнаёт о
сообщении. Cron срабатывает только пока REPL простаивает, поэтому во время
длинной работы сообщения ждут окончания задачи: замеры на живой переписке
дали медиану 2 минуты и хвост до 20. Monitor стримит события по мере
появления, и уведомление приходит посреди работы.

Печатает ТОЛЬКО то, на что нужно реагировать: текст сообщений, пути к
картинкам и собственные сбои. Каждая строка stdout становится уведомлением,
поэтому «пусто» не печатается вовсе — иначе Monitor захлебнётся шумом и
будет остановлен автоматически.

Usage:
  python3 watch.py --session <session_id> [--interval 15] [--defer-read]
"""
import argparse
import sys
import time

import common
import db
from ingest import ingest

# Подряд идущие сбои печатаем не каждый раз: сеть моргает, а поток уведомлений
# должен оставаться читаемым. Первый сбой виден сразу, дальше — раз в N кругов.
FAILURE_REPEAT_EVERY = 20
# Сколько подряд заблокированных тиков терпеть молча. При интервале в 15 секунд
# это около минуты — дольше любой честной очереди за общей базой.
LOCKED_REPORT_AFTER = 4

# Как часто отмечать сессию живой. Круг вотчера — 15 секунд, но реестру нужна
# точность «жива на днях», а не посекундная: лишние записи только создают
# конкуренцию за общую базу.
TOUCH_EVERY_SEC = 120


def emit(line):
    """Строка stdout = уведомление. Флашим сразу: без этого события копятся
    в буфере и приходят пачкой, что убивает весь смысл стриминга."""
    print(line, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument(
        "--interval",
        type=float,
        default=15.0,
        help="секунд между опросами Telegram (по умолчанию 15)",
    )
    parser.add_argument(
        "--defer-read",
        action="store_true",
        help="помечать доставленное как in_progress; закрывает notify.py --done",
    )
    args = parser.parse_args()

    try:
        config = common.load_config()
        token = config["token"]
        chat_id = str(config["chat_id"])
    except Exception as exc:  # нет config.json / битый JSON — дальше идти незачем
        emit(f"WATCH_FATAL: не читается config.json: {exc}")
        return 1

    failures = 0
    last_touch = 0.0  # эпоха последней отметки в реестре сессий
    while True:
        try:
            conn = db.get_conn()
            db.init(conn)
            # Отмечаемся живыми ДО ingest: иначе собственный тег сессии на
            # первом круге выглядел бы неизвестным и ловил ложную тревогу.
            #
            # Коммит СРАЗУ, до ingest — это важно. Без него write-транзакция
            # оставалась бы открытой на всё время сетевого getUpdates внутри
            # ingest (до 20 секунд), и параллельные сессии всё это время
            # получали бы «database is locked».
            #
            # Реже, чем раз в TOUCH_EVERY_SEC, отмечаться незачем: реестр нужен
            # с точностью до «сессия жива на днях», а не до секунды, а каждая
            # запись — это конкуренция за общую базу.
            now = time.time()
            if now - last_touch >= TOUCH_EVERY_SEC:
                db.touch_session(conn, args.session)
                conn.commit()
                last_touch = now
            # Ingest общий для всех сессий: тянет апдейты в общий инбокс и
            # раскладывает их по тегам. Дедупликация — на INSERT OR IGNORE,
            # поэтому параллельные сессии друг другу не мешают.
            ingest(conn, token, chat_id)

            for update_id, text, media_path in db.inbox(conn, args.session):
                if text:
                    emit(text)
                # Картинку через stdout не передать — отдаём путь, его открывают
                # Read-тулом.
                if media_path:
                    emit(f"[image: {media_path}]")
                db.mark(conn, update_id, "in_progress" if args.defer_read else "read")
            conn.commit()
            conn.close()
            failures = 0
        except Exception as exc:
            # «database is locked» — не поломка, а очередь: соседняя сессия
            # держит общую базу. Следующий тик почти всегда проходит, поэтому
            # кричать на первом же случае незачем — иначе журнал забивается
            # ложной тревогой, и настоящие сбои в ней тонут. Сообщаем, только
            # если блокировка держится подряд достаточно долго.
            is_locked = "locked" in str(exc).lower()
            threshold = LOCKED_REPORT_AFTER if is_locked else 0
            if failures >= threshold and failures % FAILURE_REPEAT_EVERY == threshold % FAILURE_REPEAT_EVERY:
                emit(f"WATCH_ERROR: {type(exc).__name__}: {exc}")
            failures += 1

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main() or 0)
