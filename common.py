#!/usr/bin/env python3
"""Shared helpers for the claude-to-telegram skill: config, Telegram calls, tags."""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
MEDIA_DIR = os.path.join(SCRIPT_DIR, "media")

# A routing tag is a word starting with "$": "$my-session done" routes to session
# "my-session". Session ids may contain letters, digits, underscores and hyphens.
TAG_RE = re.compile(r"(?<!\S)\$([A-Za-z0-9_\-\u0400-\u04FF]+)")

# ЙЦУКЕН → QWERTY: тег, набранный в русской раскладке. В инбоксе лежит живой
# пример — «$Ы6 оплата прошла», то есть $S6, не дошедшее ни до кого: старая
# регулярка кириллицу вообще не видела, и сообщение считалось безадресным.
_LAYOUT = str.maketrans(
    "йцукенгшщзхъфывапролджэячсмитьбю"
    "ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ",
    "qwertyuiop[]asdfghjkl;'zxcvbnm,."
    "QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>",
)


def layout_fix(text):
    """Тот же текст, как если бы его набрали в латинской раскладке."""
    return (text or "").translate(_LAYOUT)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def telegram_request(token, method, params=None, http_timeout=20):
    url = f"https://api.telegram.org/bot{token}/{method}"
    params = params or {}
    # Вложенные объекты (rich_message, media) form-кодирование превращает в
    # питоновский repr со схлопнутыми кавычками — Telegram такое не парсит и
    # отвечает 400. Поэтому такие запросы уходят JSON-телом.
    if any(isinstance(v, (dict, list)) for v in params.values()):
        data = json.dumps(params).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
    else:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=http_timeout) as resp:
        return json.loads(resp.read().decode())


# Лимиты длины одного сообщения. Значения не из документации, а из живого
# API (27.08.2026): sendMessage отвечает «message is too long» уже на 5011
# символов, sendRichMessage принимает 20000 и отклоняет 40000 с
# RICH_MESSAGE_TEXT_TOO_LONG. Берём проверенные значения, а не границу.
PLAIN_LIMIT = 4096
RICH_LIMIT = 20000


def split_for_send(text, limit):
    """Разбить текст на куски не длиннее limit, по границам строк.

    Резать нужно ДО отправки, а не надеяться на Telegram: длинное сообщение он
    не обрезает, а отклоняет с 400 — и отчёт пропадает целиком. Границы строк
    важнее ровной длины: разрез посреди строки рвёт таблицу или тег.
    """
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        # Строка сама длиннее лимита (одна простыня без переносов) — тут уже
        # ничего не поделать, режем по символам.
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur += "\n" + line
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


def send_message(token, chat_id, text, mode="plain"):
    """Send a status message.

    mode:
      "plain" — text as-is (default, unchanged behaviour);
      "html"  — classic sendMessage with parse_mode=HTML (bold, code, links,
                <blockquote expandable> collapsible quotes, <tg-spoiler>);
      "rich"  — sendRichMessage (Bot API 10.1): everything HTML has plus
                headings, real tables, <details>, collages.

    Formatting degrades instead of failing: a rejected rich message is retried
    as HTML, a rejected HTML message is retried as plain text. A report that
    arrives ugly is still a delivered report; one that errors out is lost.

    Длина тоже деградирует, а не роняет отправку: текст длиннее лимита режется
    на несколько сообщений. Возвращается формат, которым ушёл ПОСЛЕДНИЙ кусок.
    """
    limit = RICH_LIMIT if mode == "rich" else PLAIN_LIMIT
    used = mode
    for chunk in split_for_send(text, limit):
        used = _send_chunk(token, chat_id, chunk, mode)
    return used


def _log_downgrade(mode, exc):
    """Причина понижения формата — в stderr. Без неё «формат понижен» не
    отличить: битая разметка это была или сетевой чих (оба уже случались)."""
    detail = ""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read().decode()[:300]
        except Exception:
            pass
    print(
        f"[send] {mode} не прошёл: {type(exc).__name__}: {exc} {detail}".rstrip(),
        file=sys.stderr,
    )


def _send_chunk(token, chat_id, text, mode):
    """Отправка одного куска, уже укладывающегося в лимит своего формата."""
    if mode == "rich":
        try:
            telegram_request(
                token, "sendRichMessage",
                {"chat_id": chat_id, "rich_message": {"html": _rich_breaks(text)}},
            )
            return "rich"
        except Exception as e:
            # Старый Bot API / метод недоступен. Перепосылаем через
            # send_message, а не напрямую: html-лимит в пять раз меньше
            # rich-овского, и кусок, законный для rich, надо перерезать.
            _log_downgrade("rich", e)
            return send_message(token, chat_id, text, mode="html")

    if mode == "html":
        try:
            telegram_request(
                token, "sendMessage",
                {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            return "html"
        except Exception as e:
            # Чаще всего это битая разметка (незакрытый тег, «<» в тексте).
            # Тогда сообщение важнее вёрстки — снимаем теги и шлём как есть.
            _log_downgrade("html", e)
            return send_message(token, chat_id, strip_html(text), mode="plain")

    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})
    return "plain"


_TAG_RE = re.compile(r"<[^>]+>")

_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&"))


_PRE_RE = re.compile(r"(<pre.*?</pre>|<code.*?</code>)", re.DOTALL | re.IGNORECASE)

# Перенос рядом с блочным тегом лишний: тег и так начинает новую строку, а
# <br> между <tbody> и <tr> ломает таблицу пустой строкой.
_BLOCK = "p|div|h[1-6]|ul|ol|li|table|tbody|thead|tr|td|th|blockquote|details|summary"
_BLOCK_EDGE_RE = re.compile(
    rf"\s*\n\s*(?=</?(?:{_BLOCK})[\s>])|(?<=>)\s*\n(?=\s*<)",
    re.IGNORECASE,
)


def _rich_breaks(html):
    """Одиночные переносы → <br> для sendRichMessage.

    Rich HTML схлопывает переносы по правилам HTML, поэтому набранный
    построчно текст приезжает одним абзацем: «… fell back to localhost. 1.
    Point the suite … 2. Mark it …». Блочные теги свой перенос дают сами —
    после них <br> не нужен, иначе между строками таблицы появятся пустоты.
    Внутри <pre>/<code> переносы значимы и остаются нетронутыми.
    """
    parts = _PRE_RE.split(html or "")
    for i, part in enumerate(parts):
        if i % 2:  # чётные — обычный текст, нечётные — pre/code
            continue
        part = _BLOCK_EDGE_RE.sub("", part)
        parts[i] = part.replace("\n", "<br>")
    return "".join(parts)


def strip_html(text):
    """Разметка → плоский текст: аварийный путь, когда Telegram её не принял."""
    plain = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    plain = re.sub(r"</(p|div|tr|h[1-6]|li|blockquote)>", "\n", plain, flags=re.IGNORECASE)
    plain = _TAG_RE.sub("", plain)
    for entity, char in _ENTITIES:
        plain = plain.replace(entity, char)
    return plain.strip()


# Статусы обработки, которые бот вешает НА сообщение владельца.
#
# Набор эмодзи для реакций у ботов фиксирован Telegram, и галочки в нём НЕТ:
# ✅, ☑️ и ✔️ отклоняются как REACTION_INVALID (проверено живым API 25.08.2026).
# Поэтому «готово» — 👍, выбор владельца.
REACTION_READ = "\U0001F440"     # 👀 сообщение попало в инбокс
REACTION_WORKING = "\u270D"      # ✍ сессия взяла в работу
REACTION_DONE = "\U0001F44D"     # 👍 сессия отчиталась


def set_reaction(token, chat_id, message_id, emoji):
    """Повесить реакцию на сообщение владельца. Пустой emoji — снять.

    Best-effort: реакция — украшение статуса, и её отказ не должен стоить нам
    доставки. Поэтому любые ошибки гасятся, а вызывающий продолжает работу.

    Одновременно у бота может висеть ровно ОДНА реакция на сообщение: две
    сразу Telegram отклоняет (REACTIONS_TOO_MANY), поэтому лестница
    👀 → ✍ → 👍 работает перезаписью.
    """
    if not message_id:
        return False  # строки, записанные до появления колонки message_id
    try:
        payload = [{"type": "emoji", "emoji": emoji}] if emoji else []
        result = telegram_request(
            token, "setMessageReaction",
            {"chat_id": chat_id, "message_id": message_id, "reaction": payload},
        )
        return bool(result.get("ok"))
    except Exception:
        return False


def download_file(token, file_id, dest_stem, http_timeout=30, file_name=None):
    """Resolve a Telegram file_id and save it under MEDIA_DIR.

    Named "<dest_stem>-<original name>" when Telegram tells us the name, so a
    json stays recognisable on disk; otherwise "<dest_stem><ext>". The stem
    keeps names unique — two files called "data.json" from different messages
    must not overwrite each other.

    Returns the absolute path, or None if anything fails — an attachment is a
    nice-to-have, so a failed download must never cost us the message itself.
    """
    try:
        info = telegram_request(token, "getFile", {"file_id": file_id}, http_timeout)
        if not info.get("ok"):
            return None
        remote = info["result"]["file_path"]  # e.g. "photos/file_12.jpg"
        ext = os.path.splitext(remote)[1] or ".bin"
        os.makedirs(MEDIA_DIR, exist_ok=True)
        safe = re.sub(r"[^\w.-]+", "_", file_name).strip("_") if file_name else ""
        dest = os.path.join(
            MEDIA_DIR, f"{dest_stem}-{safe}" if safe else f"{dest_stem}{ext}")
        url = f"https://api.telegram.org/file/bot{token}/{remote}"
        with urllib.request.urlopen(url, timeout=http_timeout) as resp, open(dest, "wb") as f:
            f.write(resp.read())
        return dest
    except Exception:
        return None


IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic")


def attachment_line(path):
    """Строка доставки для вложения: картинка или файл."""
    is_image = str(path).lower().endswith(IMAGE_EXTS)
    return f"[image: {path}]" if is_image else f"[файл: {path}]"

def find_tag(text):
    """Return the session id from the first "$tag" word in the text, or None."""
    m = TAG_RE.search(text or "")
    return m.group(1) if m else None


def strip_tag(text, tag):
    """Remove the first "$tag" occurrence, return the cleaned text."""
    return re.sub(r"(?<!\S)\$" + re.escape(tag) + r"\b", "", text or "", count=1).strip()
