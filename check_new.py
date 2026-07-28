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
import glob
import json
import os
import re
import time
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# Progressive back-off ladder (minutes) and how many consecutive empty
# checks trigger one step up the ladder.
INTERVAL_LADDER = [2, 5, 10, 20]
EMPTY_STREAK_PER_STEP = 3

# getUpdates window. We deliberately never advance the offset per-session
# (see "Multi-session safety"), so the shared queue on Telegram's side would
# otherwise grow without bound. With a fixed limit, once the backlog exceeds
# it, getUpdates?offset=0 returns only the OLDEST `limit` updates and the
# newest ones fall off the end — invisible to every session. So: query the
# Telegram max (100), and drain the queue under pressure (below).
GETUPDATES_LIMIT = 100
DRAIN_WHEN_QUEUE_OVER = 80          # start draining before we near the blind spot
ACTIVE_SESSION_WINDOW_SEC = 3600    # a session counts as "active" if its offset file changed this recently


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


def confirm_up_to(token, update_id):
    """Advance the server-side offset so Telegram drops every update <= update_id.
    Only ever call this with an update_id that EVERY active session has already seen
    (see safe_drain_watermark) — otherwise it would erase another session's unread mail."""
    try:
        telegram_request(token, "getUpdates", {"offset": update_id + 1, "limit": 1, "timeout": 0})
    except Exception:
        pass


def safe_drain_watermark(current_last_seen):
    """Highest update_id that is safe to confirm/drop: the minimum last-seen offset
    across all sessions whose offset file was touched within ACTIVE_SESSION_WINDOW_SEC.
    Draining only up to this value never drops a message an active session hasn't read;
    stale (likely-dead) sessions are excluded so they can't wedge the drain forever."""
    now = time.time()
    values = [current_last_seen]
    for path in glob.glob(os.path.join(SCRIPT_DIR, ".last_offset_*.json")):
        try:
            if now - os.path.getmtime(path) > ACTIVE_SESSION_WINDOW_SEC:
                continue
            with open(path) as f:
                v = int(json.load(f).get("last_update_id", 0))
            if v > 0:
                values.append(v)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return min(values) if values else 0


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
        result = telegram_request(token, "getUpdates", {"offset": 0, "limit": GETUPDATES_LIMIT})
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

    # Keep the shared queue from growing past the getUpdates window (which would hide
    # the newest messages from every session). Drain only under pressure, and only up to
    # what every currently-active session has already seen — never dropping unread mail.
    queue_len = len(result.get("result", [])) if result.get("ok") else 0
    if queue_len >= DRAIN_WHEN_QUEUE_OVER:
        watermark = safe_drain_watermark(max_update_id)
        if watermark > 0:
            confirm_up_to(token, watermark)

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
