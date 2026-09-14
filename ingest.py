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
import difflib
import time

import common
import db

GETUPDATES_LIMIT = 100  # Telegram max

# Насколько похожим должен быть кандидат, чтобы предложить его как исправление
# опечатки. 0.6 — стандартный порог difflib: ловит `intergation`→`integration`
# и не предлагает `bugs` вместо `tariffs`.
TYPO_CUTOFF = 0.6


def _attachment_file_id(msg):
    """(file_id, file_name) of an attachment in this message, or (None, None).

    Three shapes matter: "photo" (compressed — Telegram sends an array of sizes,
    the last is the largest), "document" (sent as a file — screenshots when
    quality matters, and ANY other file: json, csv, logs, archives) and the
    audio/video kinds that carry a file_id just the same.

    Documents used to be taken only when mime_type started with "image/", so a
    json sent to a session arrived as an empty message and the sender saw it
    delivered — the work it was meant to unblock simply stalled. A file is a
    file: whatever the user attached is what they wanted to hand over.
    """
    photo = msg.get("photo")
    if photo:
        return photo[-1]["file_id"], None
    for key in ("document", "audio", "video", "voice", "video_note",
                "animation"):
        item = msg.get(key) or {}
        if item.get("file_id"):
            return item["file_id"], item.get("file_name")
    return None, None


def _delivery_warning(unknown_tags, untagged_count, auto_routed_to, known,
                      continuation_count=0, inherited_to=None):
    """Текст предупреждения о недоставленных сообщениях, или None.

    Собирается ОДИН раз за прогон, а не на каждое сообщение: три подряд
    сообщения с одной опечаткой должны дать одно предупреждение, иначе
    механизм, задуманный как помощь, превращается в спам.
    """
    lines = []

    for tag in sorted(unknown_tags):
        near = difflib.get_close_matches(tag, known, n=1, cutoff=TYPO_CUTOFF)
        hint = f" Возможно, имелось в виду: ${near[0]}" if near else ""
        lines.append(f"⚠️ Тег ${tag} не найден среди активных сессий.{hint}")

    if untagged_count:
        word = "Сообщение" if untagged_count == 1 else f"Сообщений: {untagged_count} —"
        lines.append(
            f"⚠️ {word} без тега, доставить некому: активных сессий несколько, "
            f"и выбрать за вас нельзя."
        )

    if continuation_count:
        word = "часть" if continuation_count == 1 else f"частей: {continuation_count} —"
        lines.append(
            f"ℹ️ Продолжение длинного сообщения ({word} без тега, Telegram режет "
            f"текст по 4096 символов) доставлено тому же адресату."
        )

    if inherited_to:
        lines.append(
            f"ℹ️ Сообщение без тега доставлено ${inherited_to} — по предыдущему "
            f"сообщению. Если адресат другой, поставьте тег."
        )

    if auto_routed_to:
        lines.append(
            f"ℹ️ Сообщение без тега доставлено единственной активной сессии: "
            f"{auto_routed_to}"
        )

    # Доставленное — не проблема: хвост про «укажите тег» и список сессий здесь
    # только шумит. Он нужен, лишь когда что-то реально осталось недоставленным.
    if not unknown_tags and not untagged_count:
        return "\n".join(lines) if lines else None

    if known:
        lines.append("")
        lines.append("Активные сессии: " + ", ".join(f"${s}" for s in known))
        lines.append(f"Как адресовать: ${known[0]} текст задачи")
    else:
        lines.append("")
        lines.append("Активных сессий нет — некому доставить.")

    # Сообщение уже в базе под своим тегом: если сессия с таким именем
    # появится, она его заберёт. Это важно сказать, иначе выглядит как потеря.
    lines.append("")
    lines.append("Сообщение сохранено и будет доставлено, если такая сессия появится.")
    return "\n".join(lines)


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
    to_react = []

    # Вложения скачиваем ДО записи, а не по ходу цикла.
    #
    # download_file ходит в сеть с таймаутом до 30 секунд, а первая же вставка
    # открывает транзакцию SQLite — значит скачивание внутри цикла держало
    # общую базу заблокированной все эти секунды. Соседние сессии (у каждой
    # свой watcher, пишущий в тот же файл) ждали блокировку 15 секунд по
    # busy_timeout и падали с «database is locked»: доставка сообщений
    # прерывалась из-за одной картинки. Теперь сеть отработана заранее, а под
    # транзакцией остаются только быстрые INSERT'ы.
    media_by_uid = {}
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id", "")) != chat_id:
            continue
        if str(msg.get("from", {}).get("id", "")) != chat_id:
            continue
        file_id, file_name = _attachment_file_id(msg)
        if file_id:
            media_by_uid[u["update_id"]] = common.download_file(
                token, file_id, str(u["update_id"]), file_name=file_name
            )

    # Список читается один раз на прогон: он не меняется, пока мы разбираем
    # пачку, а на каждое сообщение это был бы лишний запрос.
    known = db.known_sessions(conn, now)
    unknown_tags = set()
    untagged_count = 0
    auto_routed_to = None
    continuation_count = 0
    inherited_to = None

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
        raw_tag = common.find_tag(text)
        tag = raw_tag
        # Тег, набранный в русской раскладке ($Ы6 вместо $S6), принимаем только
        # если после перевода он совпал с ЖИВОЙ сессией. Без этой оговорки
        # обычное русское слово после «$» превратилось бы в выдуманный адрес и
        # утащило сообщение в несуществующую сессию — хуже, чем не узнать тег.
        if raw_tag and not raw_tag.isascii():
            fixed = common.layout_fix(raw_tag)
            tag = fixed if fixed in known else None
        problem = None  # 'unknown_tag' | 'untagged' | 'auto_routed' | None
        if tag:
            owner, clean = tag, common.strip_tag(text, raw_tag)
            # Сообщение сохраняем ПОД УКАЗАННЫМ тегом даже если такой сессии
            # нет: она может появиться позже и заберёт его. Предупреждение —
            # не отказ в приёме, а сигнал о вероятной опечатке.
            if tag not in known:
                problem = "unknown_tag"
        else:
            clean = text
            tg_date = msg.get("date")
            # Порядок разрешения адресата — от факта к догадке. Первые два
            # шага опираются на признаки, у которых нет разумного другого
            # объяснения; последние два — предположения, и потому о них
            # сообщается в чат.
            #
            # 1. Albums arrive as one update per photo with the caption on the
            #    first only, so inherit the owner the album was already routed to.
            # 2. Хвост сообщения, разрезанного клиентом по лимиту в 4096
            #    символов: тег остался в первой части, продолжение приходило
            #    без него и оседало в unrouted. Проверено на живой базе —
            #    сообщение на 7882 символа приехало как 4077 + 3805 с одним и
            #    тем же tg_date, и вторая половина не дошла ни до кого.
            # 3. Сообщение сразу вслед за адресованным (картинка вдогонку,
            #    «ок», уточнение) — наследует того же адресата.
            # 4. Активна ровно одна сессия — двусмысленности нет.
            owner = db.owner_of_media_group(conn, group_id)
            if owner:
                pass
            elif db.continuation_owner(conn, uid, tg_date):
                owner = db.continuation_owner(conn, uid, tg_date)
                problem = "continuation"
            elif db.recent_owner(conn, uid, tg_date, known):
                owner = db.recent_owner(conn, uid, tg_date, known)
                problem = "inherited"
            elif len(known) == 1:
                # При двух и более сессиях угадывать нельзя: чужая сессия
                # начнёт делать не свою работу.
                owner = known[0]
                problem = "auto_routed"
            else:
                owner = "unrouted"
                problem = "untagged"

        media_path = media_by_uid.get(uid)

        if db.store(conn, uid, owner, clean if clean else text, msg.get("date"), now,
                    media_path, group_id, msg.get("message_id")):
            new_count += 1
            # Реакцию ставим ТОЛЬКО на новые строки: иначе каждый повторный
            # прогон дёргал бы API по уже отмеченным сообщениям. Копим здесь,
            # а шлём после commit — сеть внутри транзакции уже однажды роняла
            # соседей на «database is locked» (см. комментарий в db.get_conn).
            if msg.get("message_id"):
                to_react.append(msg["message_id"])
            # Считаем только НОВЫЕ строки: иначе одно и то же сообщение
            # порождало бы предупреждение на каждом последующем прогоне.
            if problem == "unknown_tag":
                unknown_tags.add(tag)
            elif problem == "untagged":
                untagged_count += 1
            elif problem == "auto_routed":
                auto_routed_to = owner
            elif problem == "continuation":
                continuation_count += 1
            elif problem == "inherited":
                inherited_to = owner

    conn.commit()
    db.prune(conn, now)
    conn.commit()

    # 👀 «сообщение принято»: транзакция уже закрыта, база свободна.
    for message_id in to_react:
        common.set_reaction(token, chat_id, message_id, common.REACTION_READ)

    # Confirm/drain everything we just processed (stored or intentionally ignored).
    if stored_max > 0:
        try:
            common.telegram_request(
                token, "getUpdates", {"offset": stored_max + 1, "limit": 1, "timeout": 0}
            )
        except Exception:
            pass

    # Предупреждение уходит последним и в try: подсказка полезна, но потерять
    # из-за неё уже сохранённые сообщения нельзя. Отправляем своим же ботом —
    # исходящие в getUpdates не возвращаются, петли не будет.
    warning = _delivery_warning(unknown_tags, untagged_count, auto_routed_to, known,
                                continuation_count, inherited_to)
    if warning:
        try:
            common.send_message(token, chat_id, warning)
        except Exception:
            pass

    return new_count
