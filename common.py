#!/usr/bin/env python3
"""Shared helpers for the claude-to-telegram skill: config, Telegram calls, tags."""
import json
import os
import re
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
MEDIA_DIR = os.path.join(SCRIPT_DIR, "media")

# A routing tag is a word starting with "$": "$my-session done" routes to session
# "my-session". Session ids may contain letters, digits, underscores and hyphens.
TAG_RE = re.compile(r"(?<!\S)\$([A-Za-z0-9_\-]+)")


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
    """
    if mode == "rich":
        try:
            telegram_request(
                token, "sendRichMessage",
                {"chat_id": chat_id, "rich_message": {"html": text}},
            )
            return "rich"
        except Exception:
            mode = "html"  # старый Bot API / метод недоступен

    if mode == "html":
        try:
            telegram_request(
                token, "sendMessage",
                {"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
            return "html"
        except Exception:
            # Чаще всего это битая разметка (незакрытый тег, «<» в тексте).
            # Тогда сообщение важнее вёрстки — снимаем теги и шлём как есть.
            text = strip_html(text)

    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})
    return "plain"


_TAG_RE = re.compile(r"<[^>]+>")

_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&"))


def strip_html(text):
    """Разметка → плоский текст: аварийный путь, когда Telegram её не принял."""
    plain = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.IGNORECASE)
    plain = re.sub(r"</(p|div|tr|h[1-6]|li|blockquote)>", "\n", plain, flags=re.IGNORECASE)
    plain = _TAG_RE.sub("", plain)
    for entity, char in _ENTITIES:
        plain = plain.replace(entity, char)
    return plain.strip()


def download_file(token, file_id, dest_stem, http_timeout=30):
    """Resolve a Telegram file_id and save it under MEDIA_DIR as <dest_stem>.<ext>.

    Returns the absolute path, or None if anything fails — media is a nice-to-have,
    so a failed download must never cost us the message itself.
    """
    try:
        info = telegram_request(token, "getFile", {"file_id": file_id}, http_timeout)
        if not info.get("ok"):
            return None
        remote = info["result"]["file_path"]  # e.g. "photos/file_12.jpg"
        ext = os.path.splitext(remote)[1] or ".bin"
        os.makedirs(MEDIA_DIR, exist_ok=True)
        dest = os.path.join(MEDIA_DIR, f"{dest_stem}{ext}")
        url = f"https://api.telegram.org/file/bot{token}/{remote}"
        with urllib.request.urlopen(url, timeout=http_timeout) as resp, open(dest, "wb") as f:
            f.write(resp.read())
        return dest
    except Exception:
        return None


def find_tag(text):
    """Return the session id from the first "$tag" word in the text, or None."""
    m = TAG_RE.search(text or "")
    return m.group(1) if m else None


def strip_tag(text, tag):
    """Remove the first "$tag" occurrence, return the cleaned text."""
    return re.sub(r"(?<!\S)\$" + re.escape(tag) + r"\b", "", text or "", count=1).strip()
