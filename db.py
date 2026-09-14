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
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "messages.db")
PRUNE_AGE_SEC = 7 * 24 * 3600  # drop anything older than 7 days regardless of status

# Сессия считается известной, если обращалась к инбоксу за это время. Порог
# щедрый намеренно: ложное «сессия неизвестна» пугает пользователя зря, а
# устаревшая запись в списке подсказок стоит одной лишней строки.
KNOWN_SESSION_AGE_SEC = 30 * 24 * 3600


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    # 60 секунд, а не 15. Писателей столько же, сколько параллельных сессий
    # (на 15.08.2026 их одновременно четыре), и удержание блокировки не всегда
    # короткое: watcher, запущенный со старой версией ingest, скачивает
    # присланное вложение ВНУТРИ транзакции, а download_file ждёт сеть до 30
    # секунд. Прежние 15 гарантированно не переживали такую картинку, и
    # доставка падала с «database is locked» у всех соседей. Ждать здесь
    # дешевле, чем терять сообщения.
    conn.execute("PRAGMA busy_timeout=60000")
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
    # message_id — id САМОГО сообщения, не апдейта: только по нему
    # ставится реакция (setMessageReaction). update_id для этого не годится.
    for column in ("media_path TEXT", "media_group_id TEXT", "message_id INTEGER"):
        if column.split()[0] not in existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {column}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sess_status ON messages(session_id, status, update_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_received ON messages(received_at)")
    # Реестр живых сессий. Нужен, чтобы отличить опечатку в теге от сессии,
    # которая просто сейчас не запущена: без этого списка `$intergation` и
    # `$integration` для ingest неразличимы, и сообщение молча оседает под
    # несуществующим адресатом.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions(
            session_id TEXT    PRIMARY KEY,
            last_seen  INTEGER NOT NULL
        )"""
    )
    conn.commit()


def touch_session(conn, session_id):
    """Отметить сессию живой.

    Вызывается всеми точками входа, где сессия себя проявляет: доставка
    (check_new/watch) и отправка (notify). Регистрация именно на отправке важна
    для первого запуска: `on` начинается с notify «Фоновый режим включён», и к
    приходу первого входящего сессия уже в списке — иначе первое же сообщение
    получило бы ложное «тег не найден».
    """
    if not session_id or session_id == "unrouted":
        return
    conn.execute(
        "INSERT INTO sessions(session_id, last_seen) VALUES(?,?)"
        " ON CONFLICT(session_id) DO UPDATE SET last_seen=excluded.last_seen",
        (session_id, int(time.time())),
    )


def known_sessions(conn, now_epoch=None, max_age_sec=KNOWN_SESSION_AGE_SEC):
    """Сессии, проявлявшие себя за последнее время, свежие первыми."""
    now = now_epoch if now_epoch is not None else int(time.time())
    cur = conn.execute(
        "SELECT session_id FROM sessions WHERE last_seen >= ? ORDER BY last_seen DESC",
        (now - max_age_sec,),
    )
    return [r[0] for r in cur.fetchall()]


def store(conn, update_id, session_id, text, tg_date, received_at, media_path=None,
          media_group_id=None, message_id=None):
    """Idempotent insert (dedup by update_id). Returns True if a new row was added."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages"
        "(update_id, session_id, text, tg_date, received_at, media_path, media_group_id,"
        " message_id)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (update_id, session_id, text, tg_date, received_at, media_path, media_group_id,
         message_id),
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


# Сообщение длиннее лимита Telegram клиент режет на части ~4096 символов, и
# тег остаётся только в первой. Порог ниже лимита: у клиента он плавает
# (граница ищется по словам), поэтому 4000 — а не 4096.
SPLIT_MIN_LEN = 4000
# Разрезанные части уходят одним нажатием, tg_date у них совпадает. Двух секунд
# хватает с запасом и не даёт зацепить следующее сообщение, набранное руками.
SPLIT_WINDOW_SEC = 2
# Насколько давним может быть предыдущий адресат, чтобы унаследовать его для
# сообщения без тега (картинка вдогонку, «ок», уточнение).
RECENT_OWNER_WINDOW_SEC = 5 * 60


def previous_message(conn, before_update_id):
    """Предыдущее сообщение владельца: (session_id, len(text), tg_date).

    Нужна и для склейки разрезанного сообщения, и для наследования адресата —
    оба случая отвечают на один вопрос: «кому шло то, что было прямо перед».
    """
    cur = conn.execute(
        "SELECT session_id, length(text), tg_date FROM messages"
        " WHERE update_id < ? ORDER BY update_id DESC LIMIT 1",
        (before_update_id,),
    )
    return cur.fetchone()


def continuation_owner(conn, update_id, tg_date):
    """Владелец, если это сообщение — хвост разрезанного клиентом текста.

    Признак строгий и потому надёжный: предыдущее сообщение почти упёрлось в
    лимит длины, а это пришло той же секундой. Совпадение двух условий у
    набранных руками сообщений практически невозможно — человек не печатает
    четыре тысячи символов и следом ещё одно сообщение за ту же секунду.
    """
    prev = previous_message(conn, update_id)
    if not prev or tg_date is None:
        return None
    owner, prev_len, prev_date = prev
    if owner == "unrouted" or prev_date is None:
        return None
    if prev_len < SPLIT_MIN_LEN:
        return None
    if abs(tg_date - prev_date) > SPLIT_WINDOW_SEC:
        return None
    return owner


def recent_owner(conn, update_id, tg_date, known, window_sec=RECENT_OWNER_WINDOW_SEC):
    """Адресат предыдущего сообщения, если оно было только что.

    Это уже догадка, а не факт, поэтому она ограничена вдвойне: узким окном
    и требованием, чтобы сессия была активна. Наследовать адрес у мёртвой
    сессии — то же самое, что потерять сообщение, только молча.
    """
    prev = previous_message(conn, update_id)
    if not prev or tg_date is None:
        return None
    owner, _prev_len, prev_date = prev
    if owner == "unrouted" or owner not in known or prev_date is None:
        return None
    if tg_date - prev_date > window_sec or tg_date < prev_date:
        return None
    return owner


def inbox(conn, session_id):
    """Unprocessed messages for this session, in arrival order."""
    cur = conn.execute(
        "SELECT update_id, text, media_path, message_id FROM messages"
        " WHERE session_id=? AND status='not_processed' ORDER BY update_id",
        (session_id,),
    )
    return cur.fetchall()


def close_in_progress(conn, session_id):
    """Пометить взятые в работу сообщения сессии как обработанные.

    Возвращает (число закрытых, message_id закрытых). Вторым значением
    пользуется notify: по нему на исходных сообщениях владельца ставится
    реакция «готово». Собираем id ДО UPDATE — после него строки уже не
    отберутся по status='in_progress'.

    Трогает только СВОЮ сессию: у каждой свой рабочий цикл, и чужие
    незакрытые задачи закрывать нельзя.
    """
    message_ids = [
        r[0] for r in conn.execute(
            "SELECT message_id FROM messages"
            " WHERE session_id=? AND status='in_progress' AND message_id IS NOT NULL",
            (session_id,),
        )
    ]
    cur = conn.execute(
        "UPDATE messages SET status='read'"
        " WHERE session_id=? AND status='in_progress'",
        (session_id,),
    )
    return cur.rowcount, message_ids


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
