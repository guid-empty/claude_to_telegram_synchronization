#!/usr/bin/env python3
"""
Non-blocking check for new Telegram messages matching a session id.
Persists last-seen update_id to a local state file so repeated calls
(e.g. from a cron tick) don't re-report the same message.

Usage:
  python3 check_new.py --session <session_id>

Prints one line per new matching message and exits 0, or prints
"NOTHING_NEW" and exits 0 if there's nothing to report.
"""
import argparse
import json
import os
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")


def state_path(session):
    return os.path.join(SCRIPT_DIR, f".last_offset_{session}.json")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def telegram_request(token, method, params=None, http_timeout=20):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=http_timeout) as resp:
        return json.loads(resp.read().decode())


def load_last_offset(session):
    path = state_path(session)
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            return json.load(f).get("last_update_id", 0)
    except (json.JSONDecodeError, IOError):
        return 0


def save_last_offset(session, update_id):
    with open(state_path(session), "w") as f:
        json.dump({"last_update_id": update_id}, f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    args = parser.parse_args()

    config = load_config()
    token = config["token"]
    chat_id = str(config["chat_id"])

    last_seen = load_last_offset(args.session)
    result = telegram_request(token, "getUpdates", {"offset": 0, "limit": 50})

    if not result.get("ok"):
        print("NOTHING_NEW")
        return

    found = []
    max_update_id = last_seen
    for update in result.get("result", []):
        uid = update["update_id"]
        max_update_id = max(max_update_id, uid)
        if uid <= last_seen:
            continue
        msg = update.get("message")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id", "")) != chat_id:
            continue
        if str(msg.get("from", {}).get("id", "")) != chat_id:
            continue
        text = msg.get("text", "")
        if args.session in text:
            found.append(text.replace(args.session, "", 1).strip())

    save_last_offset(args.session, max_update_id)

    if found:
        for text in found:
            print(text)
    else:
        print("NOTHING_NEW")


if __name__ == "__main__":
    main()
