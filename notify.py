#!/usr/bin/env python3
"""
Fire-and-forget status message to Telegram — no reply expected.

Usage:
  python3 notify.py --session <session_id> --message "<status text>"
"""
import argparse
import json
import os
import urllib.parse
import urllib.request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def send_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--message", required=True)
    args = parser.parse_args()

    config = load_config()
    send_message(config["token"], str(config["chat_id"]), f"🔔 [{args.session}] {args.message}")


if __name__ == "__main__":
    main()
