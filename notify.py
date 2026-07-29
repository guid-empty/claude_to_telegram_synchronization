#!/usr/bin/env python3
"""
Fire-and-forget status message to Telegram — no reply expected.

Usage:
  python3 notify.py --session <session_id> --message "<status text>"
"""
import argparse

import common


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    parser.add_argument("--message", required=True)
    args = parser.parse_args()

    config = common.load_config()
    common.send_message(config["token"], str(config["chat_id"]), f"🔔 [{args.session}] {args.message}")


if __name__ == "__main__":
    main()
