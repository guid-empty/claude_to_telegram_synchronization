---
name: claude_to_telegram
description: Two-way communication with the user via a Telegram bot, when they explicitly ask to work in the background/remotely — not Claude Code hooks, a direct Bash call to ask.py/notify.py. Trigger when the user says "work in the background", "send questions/status to Telegram", "let's talk through the bot from now on", gives a session_id to use, explicitly invokes /claude_to_telegram, or /claude_to_telegram on|off. Do NOT apply as a standing behavior — only on explicit request, for one specific session.
argument-hint: "install | on|off [session_id]"
---

# claude_to_telegram — talking to the user over Telegram instead of the normal interface

A minimal two-way communication service with the user over Telegram — no Claude Code hooks at all
(`PermissionRequest`/`AskUserQuestion`). Claude explicitly calls a plain Bash tool, which sends a message
and (optionally) blocks waiting for a reply via `getUpdates`. No persistent daemon required — each
invocation runs its own long-poll for the duration of the call and then exits.

## Requirements

- Your own Telegram bot (via [@BotFather](https://t.me/BotFather)) and your own `chat_id`
  (via [@userinfobot](https://t.me/userinfobot)) — credentials aren't shared, everyone sets up their own
- `config.json` in this same folder: `{"token": "...", "chat_id": "..."}`, `chmod 600`, never commit —
  see "Setup" below, populated automatically
- Periodic background checking (see below) needs access to `CronCreate`/`CronDelete`; availability depends
  on the Claude Code runtime environment

## Setup

`/claude_to_telegram install` — initial `config.json` setup.

1. If `config.json` already exists — report which `chat_id` it's configured for (never show the token
   itself), ask whether to actually reconfigure. If yes, continue; otherwise stop.
2. If not — briefly explain how to get both values: a token from
   [@BotFather](https://t.me/BotFather) (`/newbot`), a `chat_id` from
   [@userinfobot](https://t.me/userinfobot). Ask the user to send both values as text **right here, in the
   normal interface** — at this stage there's no working bot yet, so waiting for a reply via Telegram
   (`ask.py`) makes no sense.
3. Once both values are received:
   ```bash
   python3 ~/.claude/skills/claude_to_telegram/install.py --token "<TOKEN>" --chat-id "<CHAT_ID>"
   ```
   The script validates the token via `getMe`, sends a test message to `chat_id` (confirming the bot can
   actually message this user), and only on success writes `config.json` (`chmod 600`). It never prints
   the token back out.
4. On `FAIL` — report the exact reason from the script's output (invalid token / bot can't message that
   `chat_id`) and ask the user to double-check the values, rather than guessing what went wrong.
5. On `OK` — report the bot's username now configured, and that it's ready for `on`.

## When to enable

Only on an explicit, one-off request from the user — never as a default or standing behavior. Enabled
either via an explicit command, `/claude_to_telegram on` / `/claude_to_telegram off` (see "Arguments"
below), or a natural phrase: "work in the background, questions go to Telegram", "let's talk through the
bot from now on", "here's a session id — let's switch to talking over Telegram with it".

## Arguments

Syntax: `/claude_to_telegram on|off [session_id]` — `session_id` as a third word is optional.

`/claude_to_telegram on [session_id]`:
1. Determine the `session_id`:
   - if given explicitly (as a command argument, or named by the user earlier in this conversation) — use
     it;
   - otherwise — generate one automatically: `<project-name>-<first 8 chars of the session UUID>`. Project
     name is the last component of `cwd`. The session UUID comes from your own system prompt — it's
     present in the scratchpad directory path ("Scratchpad Directory" in the system prompt, of the form
     `.../claude-501/-Users-.../<UUID>/scratchpad`) — use `<UUID>` from there, don't look it up with a
     separate tool call.
2. Tell the user (in the normal interface) the final `session_id` you'll be using.
3. Send `notify.py --session <id> --message "Background mode enabled"` — this both confirms delivery and
   gives the user a clear starting point in Telegram.
4. Create a recurring `CronCreate` job for background polling (see "Polling while the session is idle"
   below) — remember its job id for the later `CronDelete`.
5. From this turn on — follow the protocol below ("Background mode protocol") at the end of every turn,
   until an explicit signal to stop arrives.

`/claude_to_telegram off [session_id]`:
1. If background mode was enabled in this conversation — send
   `notify.py --session <id> --message "Background mode disabled"`.
2. `CronDelete` the job created in the `on` step — otherwise it keeps ticking for nothing.
3. Return to normal turn completion, no more blocking `ask.py` calls.

`/claude_to_telegram` with no arguments — just load this instruction into context (explain/use it as
appropriate), don't change the session's current state.

**An `off` command sent from within Telegram itself** (caught by `check_new.py` on a cron tick, text
contains "off" or clearly reads as a stop command) — handle it the same way as an explicit
`/claude_to_telegram off` here: run steps 1-3 above, not as an ordinary message to act on and keep working.

## How to run it

Both scripts live directly in this skill's folder (`~/.claude/skills/claude_to_telegram/`), the config
(`config.json`, token + chat_id) is right there too, `chmod 600`, never committed anywhere.

### Ask a question and wait for a reply

```bash
python3 ~/.claude/skills/claude_to_telegram/ask.py \
  --session <session_id> --message "<question>" --timeout <SECONDS>
```

Prints the reply text (session_id stripped out) to stdout, exit 0. On timeout — `NO_RESPONSE_TIMEOUT`,
exit 1. The user's reply needs to contain the `session_id` somewhere in the text — that's what ties it to
this specific session (and keeps parallel Claude Code sessions replying through the same bot from getting
crossed wires).

**Choosing how to call it:**
- Quick interactive check (right now, mid-conversation) — a plain foreground Bash call, timeout in
  minutes.
- The real "I'm stepping away, I'll reply when I can" case — `run_in_background: true` with a large
  `--timeout` (hours) — don't block the current turn, wait for the background-task notification, then
  process the result and explicitly tell the user, in the normal interface, what was received and how
  you're proceeding.

**Known limitation:** short synchronous windows are unreliable if the person replies later than expected —
the message isn't lost (it stays queued in Telegram), it's just that the active `getUpdates` poll has
already stopped listening. If a window expires with no reply, don't treat that as a failure right away —
check manually whether something actually came in:
```bash
TOKEN=$(python3 -c "import json; print(json.load(open('$HOME/.claude/skills/claude_to_telegram/config.json'))['token'])")
curl -s "https://api.telegram.org/bot${TOKEN}/getUpdates?limit=20" | python3 -m json.tool
```

### Emulating AskUserQuestion (multiple-choice question)

The native `AskUserQuestion` (with buttons) isn't reachable from Telegram in background mode — hooks are
intentionally not used (see above). If you need a multiple-choice question rather than free text — format
the options directly in the `--message` text, numbered:

```
<question>

1. <option A> — <short description if useful>
2. <option B> — <short description>
3. <option C> — <short description>

Reply with a number or your own text.
```

The reply from `ask.py` is plain text (a number like "2", or free text). Interpreting "this is a choice of
option N" vs. "this is a free-form answer" happens on the model's side after getting the result, not in
the script itself: if the reply is a short number matching one of the options, treat it as that choice;
otherwise treat it as a free-form answer (the equivalent of "Other" in the native `AskUserQuestion`).

### Polling while the session is idle (CronCreate)

**A fundamental architectural limitation:** neither `ask.py` nor any hook can "wake up" a session that has
already finished its turn and is waiting for the user's next message in the normal interface — a message
sent to Telegram while nobody's listening just sits unread in the queue. This is inherent: hooks intercept
an interaction that's already in progress, they don't initiate a new one.

The only working approach found so far is `CronCreate` (a session-only scheduler, separate from hooks): a
job "fires while the REPL is idle", meaning it can genuinely enqueue a new prompt at the moment the session
goes quiet.

```
python3 ~/.claude/skills/claude_to_telegram/check_new.py --session <session_id>
```

Non-blocking (unlike `ask.py`) — checks `getUpdates` instantly, prints any new messages (text,
`session_id` stripped) or `NOTHING_NEW`. Keeps a last-seen offset in
`.last_offset_<session_id>.json` in this same folder — repeated calls are idempotent, the same message
never gets reported twice.

When enabling background mode — create a recurring cron job (example, every 10 minutes, offset from
:00/:30):
```
CronCreate(cron="7,17,27,37,47,57 * * * *", recurring=true, prompt="Check for new Telegram messages: run
python3 ~/.claude/skills/claude_to_telegram/check_new.py --session <session_id>. If the output is
NOTHING_NEW, do nothing and don't write anything to the user. If there's text — treat it as a new message
from the user, explicitly say what was received, then continue.")
```

**Known limitations of CronCreate:** the job only lives in this session (not on disk — if the
session/CLI closes, the job is gone, it has to be recreated the next time background mode is enabled);
auto-expires after 7 days; every tick is a real inference call (spends tokens) even when the result is
`NOTHING_NEW`. When disabling the mode (`off`) — always `CronDelete` this job, otherwise it keeps ticking
for nothing.

### Just notify (no reply expected)

```bash
python3 ~/.claude/skills/claude_to_telegram/notify.py --session <session_id> --message "<status>"
```

## Background mode protocol

At the end of every turn, instead of finishing normally — call `ask.py` with a short summary of what was
done and a "what's next" question (or just explicitly ask for the next step). Once a reply comes in,
explicitly write in the normal interface: "got the reply: ..., proceeding as follows" — the user should be
able to see, in the normal chat, what happened, not only in Telegram. Exit the mode as soon as the user
explicitly asks ("stop, I'll be back" or similar) — return to finishing turns normally, no more blocking
calls.

## Security

- Never commit `config.json` (it's in this folder's `.gitignore`)
- Incoming messages are filtered by `chat.id` **and** `from.id` matching the configured `chat_id`;
  messages from anyone else are ignored
- If the token/chat_id ever leak in plain sight — reissue the token via `@BotFather` → `/revoke`, then
  only update `config.json`
