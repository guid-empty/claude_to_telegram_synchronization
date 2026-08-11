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


def _delivery_warning(unknown_tags, untagged_count, auto_routed_to, known):
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

    if auto_routed_to:
        lines.append(
            f"ℹ️ Сообщение без тега доставлено единственной активной сессии: "
            f"{auto_routed_to}"
        )
        return "\n".join(lines)  # это не проблема, хвост про «укажите тег» лишний

    if not lines:
        return None

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

    # Список читается один раз на прогон: он не меняется, пока мы разбираем
    # пачку, а на каждое сообщение это был бы лишний запрос.
    known = db.known_sessions(conn, now)
    unknown_tags = set()
    untagged_count = 0
    auto_routed_to = None

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
        problem = None  # 'unknown_tag' | 'untagged' | 'auto_routed' | None
        if tag:
            owner, clean = tag, common.strip_tag(text, tag)
            # Сообщение сохраняем ПОД УКАЗАННЫМ тегом даже если такой сессии
            # нет: она может появиться позже и заберёт его. Предупреждение —
            # не отказ в приёме, а сигнал о вероятной опечатке.
            if tag not in known:
                problem = "unknown_tag"
        else:
            # Albums arrive as one update per photo with the caption on the first
            # only, so inherit the owner the album was already routed to.
            inherited = db.owner_of_media_group(conn, group_id)
            clean = text
            if inherited:
                owner = inherited
            elif len(known) == 1:
                # Двусмысленности нет: адресат ровно один, и «не доставить»
                # здесь было бы педантизмом — сообщение всё равно может уйти
                # только ему. При двух и более сессиях угадывать нельзя:
                # чужая сессия начнёт делать не свою работу.
                owner = known[0]
                problem = "auto_routed"
            else:
                owner = "unrouted"
                problem = "untagged"

        media_path = None
        file_id = _attachment_file_id(msg)
        if file_id:
            media_path = common.download_file(token, file_id, str(uid))

        if db.store(conn, uid, owner, clean if clean else text, msg.get("date"), now,
                    media_path, group_id):
            new_count += 1
            # Считаем только НОВЫЕ строки: иначе одно и то же сообщение
            # порождало бы предупреждение на каждом последующем прогоне.
            if problem == "unknown_tag":
                unknown_tags.add(tag)
            elif problem == "untagged":
                untagged_count += 1
            elif problem == "auto_routed":
                auto_routed_to = owner

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

    # Предупреждение уходит последним и в try: подсказка полезна, но потерять
    # из-за неё уже сохранённые сообщения нельзя. Отправляем своим же ботом —
    # исходящие в getUpdates не возвращаются, петли не будет.
    warning = _delivery_warning(unknown_tags, untagged_count, auto_routed_to, known)
    if warning:
        try:
            common.send_message(token, chat_id, warning)
        except Exception:
            pass

    return new_count
