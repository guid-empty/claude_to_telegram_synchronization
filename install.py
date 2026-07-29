#!/usr/bin/env python3
"""
Write config.json from a bot token + chat_id, then validate both live
against the Telegram API before declaring success.

Usage:
  python3 install.py --token "<BOT_TOKEN>" --chat-id "<CHAT_ID>"

Never prints the token back. Exits 1 with a clear reason if the token is
invalid or the bot can't message the given chat_id.
"""
import argparse
import json
import os
import sys

import common


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--chat-id", required=True)
    args = parser.parse_args()

    try:
        me = common.telegram_request(args.token, "getMe")
    except Exception as e:
        print(f"FAIL: token invalid or unreachable ({e})")
        sys.exit(1)

    if not me.get("ok"):
        print(f"FAIL: getMe rejected the token: {me.get('description', me)}")
        sys.exit(1)

    bot_username = me["result"].get("username", "?")

    try:
        sent = common.telegram_request(args.token, "sendMessage", {
            "chat_id": args.chat_id,
            "text": f"✅ claude-to-telegram: setup ok — this bot (@{bot_username}) can now message you.",
        })
    except Exception as e:
        print(f"FAIL: token is valid (@{bot_username}) but sendMessage to chat_id={args.chat_id} failed ({e})")
        sys.exit(1)

    if not sent.get("ok"):
        print(f"FAIL: token is valid (@{bot_username}) but sendMessage to chat_id={args.chat_id} was rejected: {sent.get('description', sent)}")
        sys.exit(1)

    with open(common.CONFIG_PATH, "w") as f:
        json.dump({"token": args.token, "chat_id": str(args.chat_id)}, f)
    os.chmod(common.CONFIG_PATH, 0o600)

    print(f"OK: bot=@{bot_username} chat_id={args.chat_id} config.json written and validated")


if __name__ == "__main__":
    main()
