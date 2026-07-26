#!/usr/bin/env python3
"""
Send a question to Telegram, block until a reply mentioning the session id
arrives, print the reply text (session id stripped) and exit 0.

Usage:
  python3 ask.py --session <session_id> --message "<question text>" [--timeout SECONDS]

On timeout: prints "NO_RESPONSE_TIMEOUT" and exits 1.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def telegram_request(token, method, params=None, http_timeout=35):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=http_timeout) as resp:
        return json.loads(resp.read().decode())


def send_message(token, chat_id, text):
    telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": text})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()

    config = load_config()
    token = config["token"]
    chat_id = str(config["chat_id"])

    # Start listening from "now" — fetch the latest update_id and skip history.
    primer = telegram_request(token, "getUpdates", {"offset": -1, "limit": 1})
    offset = 0
    if primer.get("ok") and primer.get("result"):
        offset = primer["result"][-1]["update_id"] + 1

    prompt = (
        f"🤖 [{args.session}] {args.message}\n\n"
        f"(ответь любым текстом, упомянув \"{args.session}\" где угодно в сообщении)"
    )
    send_message(token, chat_id, prompt)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        poll_timeout = min(25, max(1, int(deadline - time.time())))
        try:
            result = telegram_request(
                token, "getUpdates",
                {"offset": offset, "limit": 10, "timeout": poll_timeout},
                http_timeout=poll_timeout + 10,
            )
        except Exception:
            time.sleep(2)
            continue

        if not result.get("ok"):
            time.sleep(2)
            continue

        for update in result.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id", "")) != chat_id:
                continue
            if str(msg.get("from", {}).get("id", "")) != chat_id:
                continue  # only the configured owner counts as a reply
            text = msg.get("text", "")
            if args.session in text:
                cleaned = text.replace(args.session, "", 1).strip()
                print(cleaned if cleaned else text)
                sys.exit(0)

    print("NO_RESPONSE_TIMEOUT")
    sys.exit(1)


if __name__ == "__main__":
    main()
