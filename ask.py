#!/usr/bin/env python3
"""
Send a question to Telegram, block until a reply mentioning the session id
arrives, print the reply text (session id stripped) and exit 0.

Usage:
  python3 ask.py --session <session_id> --message "<question text>" [--timeout SECONDS]

On timeout: prints "NO_RESPONSE_TIMEOUT" and exits 1.

IMPORTANT: never call getUpdates with an offset greater than 0. Telegram's
getUpdates is a single-consumer API — any call with offset > 0 confirms
(server-side "forgets") every prior update for ALL clients of this bot
token, not just this process. With multiple parallel Claude Code sessions
sharing one bot, that would silently drop messages meant for other
sessions. "What have we already seen" is tracked purely locally instead,
in a per-session state file shared with check_new.py.
"""
import argparse
import json
import os
import sys
import time
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


def send_message(token, chat_id, text):
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})


def load_last_seen(session):
    path = state_path(session)
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            return json.load(f).get("last_update_id", 0)
    except (json.JSONDecodeError, IOError):
        return 0


def save_last_seen(session, update_id):
    with open(state_path(session), "w") as f:
        json.dump({"last_update_id": update_id}, f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    config = load_config()
    token = config["token"]
    chat_id = str(config["chat_id"])

    last_seen = load_last_seen(args.session)

    prompt = (
        f"🤖 [{args.session}] {args.message}\n\n"
        f"(ответь любым текстом, упомянув \"{args.session}\" где угодно в сообщении)"
    )
    send_message(token, chat_id, prompt)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            result = telegram_request(token, "getUpdates", {"offset": 0, "limit": 50})
        except Exception:
            time.sleep(2)
            continue

        if not result.get("ok"):
            time.sleep(2)
            continue

        for update in result.get("result", []):
            uid = update["update_id"]
            if uid <= last_seen:
                continue
            last_seen = max(last_seen, uid)
            msg = update.get("message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id", "")) != chat_id:
                continue
            if str(msg.get("from", {}).get("id", "")) != chat_id:
                continue
            text = msg.get("text", "")
            if args.session in text:
                save_last_seen(args.session, last_seen)
                cleaned = text.replace(args.session, "", 1).strip()
                print(cleaned if cleaned else text)
                sys.exit(0)

        save_last_seen(args.session, last_seen)
        time.sleep(2)

    print("NO_RESPONSE_TIMEOUT")
    sys.exit(1)


if __name__ == "__main__":
    main()
