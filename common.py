#!/usr/bin/env python3
"""Shared helpers for the claude-to-telegram skill: config, Telegram calls, tags."""
import json
import os
import re
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# A routing tag is a word starting with "$": "$my-session done" routes to session
# "my-session". Session ids may contain letters, digits, underscores and hyphens.
TAG_RE = re.compile(r"(?<!\S)\$([A-Za-z0-9_\-]+)")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def telegram_request(token, method, params=None, http_timeout=20):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=http_timeout) as resp:
        return json.loads(resp.read().decode())


def send_message(token, chat_id, text):
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})


def find_tag(text):
    """Return the session id from the first "$tag" word in the text, or None."""
    m = TAG_RE.search(text or "")
    return m.group(1) if m else None


def strip_tag(text, tag):
    """Remove the first "$tag" occurrence, return the cleaned text."""
    return re.sub(r"(?<!\S)\$" + re.escape(tag) + r"\b", "", text or "", count=1).strip()
