#!/usr/bin/env python3
"""
Non-blocking check for new Telegram messages addressed to a session id.

A message is addressed to this session when it contains a word that starts
with "$" followed by the session id, e.g. "$claude_communication done".
Bare mentions without the "$" are ignored — this keeps parallel sessions
from grabbing each other's replies.

Never advances the server-side getUpdates offset (always queries offset=0)
— see SKILL.md "Multi-session safety". "What have we already seen" is
tracked locally in .last_offset_<session>.json so repeated calls don't
re-report the same message.

Progressive back-off: after several consecutive empty checks the
recommended poll interval grows along INTERVAL_LADDER (2 -> 5 -> 10 -> 20
min). The moment a real message arrives it resets to the shortest
interval. State lives in .backoff_<session>.json.

Usage:
  python3 check_new.py --session <session_id>

Output (stdout):
  - one line per new matching message (marker stripped), or "NOTHING_NEW"
  - always a final line: "RESCHEDULE=<minutes>" when the recommended cron
    interval changed since the previous run, otherwise "RESCHEDULE=none".
    The caller reschedules the polling cron only when a number is given.
"""
import argparse
import json
import os
import re
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# Progressive back-off ladder (minutes) and how many consecutive empty
# checks trigger one step up the ladder.
INTERVAL_LADDER = [2, 5, 10, 20]
EMPTY_STREAK_PER_STEP = 3


def offset_state_path(session):
    return os.path.join(SCRIPT_DIR, f".last_offset_{session}.json")


def backoff_state_path(session):
    return os.path.join(SCRIPT_DIR, f".backoff_{session}.json")


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
    path = offset_state_path(session)
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            return json.load(f).get("last_update_id", 0)
    except (json.JSONDecodeError, IOError):
        return 0


def save_last_offset(session, update_id):
    with open(offset_state_path(session), "w") as f:
        json.dump({"last_update_id": update_id}, f)


def load_backoff(session):
    path = backoff_state_path(session)
    if not os.path.exists(path):
        return {"level": 0, "empty_streak": 0}
    try:
        with open(path) as f:
            s = json.load(f)
        return {"level": int(s.get("level", 0)), "empty_streak": int(s.get("empty_streak", 0))}
    except (json.JSONDecodeError, IOError, ValueError):
        return {"level": 0, "empty_streak": 0}


def save_backoff(session, state):
    with open(backoff_state_path(session), "w") as f:
        json.dump(state, f)


def interval_for_level(level):
    return INTERVAL_LADDER[min(max(level, 0), len(INTERVAL_LADDER) - 1)]


def marker_re(session):
    # A word starting with "$" whose remainder equals the session id.
    return re.compile(r'(?<!\S)\$' + re.escape(session) + r'\b')


def message_matches(text, session):
    return marker_re(session).search(text) is not None


def strip_marker(text, session):
    return marker_re(session).sub('', text, count=1).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", required=True)
    args = parser.parse_args()

    config = load_config()
    token = config["token"]
    chat_id = str(config["chat_id"])

    last_seen = load_last_offset(args.session)
    backoff = load_backoff(args.session)
    prev_interval = interval_for_level(backoff["level"])

    try:
        result = telegram_request(token, "getUpdates", {"offset": 0, "limit": 50})
    except Exception:
        # Transient error — don't disturb back-off state or reschedule.
        print("NOTHING_NEW")
        print("RESCHEDULE=none")
        return

    found = []
    max_update_id = last_seen
    if result.get("ok"):
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
            if message_matches(text, args.session):
                cleaned = strip_marker(text, args.session)
                found.append(cleaned if cleaned else text)

    save_last_offset(args.session, max_update_id)

    if found:
        # Real message -> reset to the shortest interval.
        backoff = {"level": 0, "empty_streak": 0}
        for line in found:
            print(line)
    else:
        backoff["empty_streak"] += 1
        if backoff["empty_streak"] >= EMPTY_STREAK_PER_STEP and backoff["level"] < len(INTERVAL_LADDER) - 1:
            backoff["level"] += 1
            backoff["empty_streak"] = 0
        print("NOTHING_NEW")

    save_backoff(args.session, backoff)

    new_interval = interval_for_level(backoff["level"])
    print(f"RESCHEDULE={new_interval}" if new_interval != prev_interval else "RESCHEDULE=none")


if __name__ == "__main__":
    main()
