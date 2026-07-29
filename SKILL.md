---
name: claude-to-telegram
description: Two-way communication with the user via a Telegram bot, when they explicitly ask to work in the background/remotely — no Claude Code hooks, no daemon; Claude runs small Python scripts and messages route through a shared SQLite inbox. Trigger when the user says "work in the background", "send questions/status to Telegram", "let's talk through the bot from now on", gives a session_id to use, explicitly invokes /claude-to-telegram, or /claude-to-telegram on|off. Do NOT apply as a standing behavior — only on explicit request, for one specific session.
argument-hint: "install | on|off [session_id]"
---

# claude-to-telegram — talk to the user over Telegram instead of the terminal

Two-way comms with the user over Telegram, for background/remote work — no Claude Code hooks, no persistent
daemon. Claude explicitly runs small Python scripts (stdlib only; `sqlite3` is built in). Several parallel
Claude Code sessions can share **one** bot: each message carries a routing tag `$<session_id>`, and every
incoming message is stored into a shared **SQLite inbox** (`messages.db`) routed to its owning session — so
nothing is lost across parallel sessions, and messages for an offline session wait until it runs again.

All scripts live in `~/.claude/skills/claude-to-telegram/`.

## Requirements

- Your own Telegram bot ([@BotFather](https://t.me/BotFather)) and your own `chat_id`
  ([@userinfobot](https://t.me/userinfobot)) — credentials aren't shared, everyone sets up their own.
- `config.json` in this folder: `{"token": "...", "chat_id": "..."}`, `chmod 600`, never commit — filled by
  "Install" below.
- Python 3 (stdlib only). Background polling needs `CronCreate`/`CronDelete` in the runtime.

## Files

- `common.py` — config load, Telegram calls, `$tag` parsing/stripping
- `db.py` — the shared SQLite inbox (schema, store, inbox, mark, prune; WAL mode)
- `ingest.py` — pull from Telegram → route each message by its `$tag` → store → advance the offset **only up
  to what was stored** → prune >7 days
- `check_new.py` — one background poll for a session: ingest, then deliver this session's own inbox
- `ask.py` — send a question, block until this session's reply arrives (via the inbox)
- `notify.py` — fire-and-forget status message
- `install.py` — write + validate `config.json`
- `config.json`, `messages.db`, `.backoff_*.json` — local only, gitignored

## Install

`/claude-to-telegram install` — first-time `config.json` setup.

1. If `config.json` exists — report its `chat_id` (never the token), ask whether to reconfigure; stop if no.
2. Else explain how to get both values: token from [@BotFather](https://t.me/BotFather) (`/newbot`),
   `chat_id` from [@userinfobot](https://t.me/userinfobot). Ask the user to paste both **here, in the normal
   interface** (no bot exists yet, so waiting via Telegram makes no sense).
3. Run:
   ```bash
   python3 ~/.claude/skills/claude-to-telegram/install.py --token "<TOKEN>" --chat-id "<CHAT_ID>"
   ```
   It validates the token (`getMe`), sends a test message to `chat_id`, and only on success writes
   `config.json` (`chmod 600`). It never prints the token back.
4. On `FAIL` — relay the exact reason from the output; ask the user to recheck. On `OK` — report the bot
   username and that it's ready for `on`.

## When to enable

Only on an explicit, one-off request — never a standing default. Triggered by `/claude-to-telegram on|off`,
or a natural phrase ("work in the background, questions to Telegram", "let's talk through the bot").

## Arguments

Syntax: `/claude-to-telegram on|off [session_id]` — `session_id` optional third word.

`/claude-to-telegram on [session_id]`:
1. Determine `session_id`: use the one given (command arg, or named earlier in the conversation); else
   generate `<project-name>-<first 8 chars of the session UUID>` (project = last component of `cwd`; UUID
   from the "Scratchpad Directory" path in your system prompt, not a separate tool call).
2. Tell the user (in the normal interface) the final `session_id`, and that to reach this session they
   prefix a Telegram message with `$<session_id>` (e.g. `$my-session do X`).
3. `notify.py --session <id> --message "Background mode enabled"`.
4. Create a recurring `CronCreate` job at the base interval (`*/2 * * * *`) that self-adjusts via
   back-off (see "Polling" below) — remember its job id for the later `CronDelete`.
5. Follow the "Background mode protocol" at the end of every turn until an explicit off.

`/claude-to-telegram off [session_id]`:
1. If enabled this conversation — `notify.py --session <id> --message "Background mode disabled"`.
2. `CronDelete` the polling job (use `CronList` if you lost the id).
3. Back to normal turn completion.

`/claude-to-telegram` with no args — just load this instruction into context; don't change session state.

**An `off`-like command sent from within Telegram** (caught by `check_new.py`, text reads as a stop) —
handle it the same as an explicit `off` here.

## How it works — ingest, then deliver

Every poll does two phases:

1. **Ingest (any session drains Telegram for everyone)** — `ingest.py`:
   `getUpdates(offset=0, limit=100)` → for each message find its `$tag` → `INSERT OR IGNORE` into
   `messages.db` (owner = the tag, or `unrouted` if none) → advance the offset **only up to the max
   `update_id` actually stored this batch** → prune rows older than 7 days.
2. **Deliver (this session handles its own inbox)** — `SELECT ... WHERE session_id=<me> AND
   status='not_processed' ORDER BY update_id`, print each (tag stripped), mark `read`.

Why it's lossless and ordered (the invariants — keep them if you touch this code):
- `getUpdates` returns updates in strictly ascending, gap-free `update_id` order. We store the whole batch,
  then confirm the offset only up to that batch's max — so we never skip past an unstored message; a batch
  beyond `limit` just arrives next call.
- **Durability before confirm**: a message is in SQLite before its `update_id` is confirmed/dropped on
  Telegram. A crash in between merely re-fetches it.
- Idempotent (`update_id` PRIMARY KEY): parallel sessions ingesting the same updates, or a re-fetch, never
  duplicate.
- A message for a session whose poller is dead simply waits as `not_processed` until that session runs
  again — closing/crashing a session loses nothing.

## Multi-session safety (why the SQLite inbox exists)

`getUpdates` is a **single-consumer** API: the offset is global per bot token, and confirming it drops
updates for *every* reader. With several sessions polling one bot, naive per-session offsets erase each
other's mail; and if nobody advances the offset, the queue grows until the newest messages fall outside the
`limit` window and go invisible to all (this actually happened). The SQLite inbox resolves both: whoever
polls **stores every message durably (routed by tag) before advancing the offset**, so the offset *can* be
advanced safely (queue stays drained) and no reader loses another's mail. If you add a script that calls
`getUpdates`, go through `ingest.py` — never advance the offset past what's been stored.

## Polling while the session is idle (CronCreate) + progressive back-off

Cron "fires while the REPL is idle" — the only way to wake an idle session. `check_new.py` also drives a
back-off ladder (**2 → 5 → 10 → 20 min**): +1 rung after 3 consecutive empty checks, reset to 2 min the
instant a message arrives. It can't reschedule the cron itself, so it prints a final line:

- `RESCHEDULE=none` — interval unchanged, do nothing.
- `RESCHEDULE=<M>` — reschedule the polling cron to `*/M * * * *`: `CronDelete` the current job, `CronCreate`
  a new recurring one reusing the same prompt, remember the new id (`CronList` if you lost it).

Enable at the base interval:
```
CronCreate(cron="*/2 * * * *", recurring=true, prompt="Проверь новые сообщения в Telegram: запусти
python3 ~/.claude/skills/claude-to-telegram/check_new.py --session <session_id>. На финальной строке
RESCHEDULE=<M> перепланируй этот polling-cron на */M (CronDelete текущий, CronCreate новый с тем же prompt,
запомни id); RESCHEDULE=none — ничего. Если вывод только NOTHING_NEW — молчи. Если есть другой текст — это
сообщение от пользователя: явно напиши 'Получено из Telegram: ...' и выполняй. НИКОГДА не выключай фоновый
режим сам — только по явному off.")
```

**CronCreate limits:** the job is session-only (gone when the CLI/session closes → re-create on next `on`),
auto-expires after 7 days, and every tick is a real inference call (hence the back-off). On `off`, always
`CronDelete` it.

## Ask a question and wait for a reply

```bash
python3 ~/.claude/skills/claude-to-telegram/ask.py --session <session_id> --message "<question>" --timeout <SECONDS>
```
Sends the question, then polls the inbox until a reply tagged `$<session_id>` arrives; prints it (tag
stripped), exit 0; on timeout prints `NO_RESPONSE_TIMEOUT`, exit 1. Foreground with a minutes timeout for a
quick interactive ask; `run_in_background: true` with a long timeout for "I'll answer later". Note: don't
lean on `ask.py` while a background cron polls the same session — both read the same inbox and either may
deliver the reply; in background mode prefer `notify.py` + the cron.

### Emulating AskUserQuestion (options)
Format numbered options directly in the `--message` text ("1. A\n2. B\n\nReply with a number or text"). The
reply is plain text; interpret a bare number as that choice, else free-form (the "Other" equivalent).

## Just notify (no reply expected)

```bash
python3 ~/.claude/skills/claude-to-telegram/notify.py --session <session_id> --message "<status>"
```

## Background mode protocol

**Acknowledge on pickup.** When a task arrives via Telegram and will take more than an instant, send a short
`notify.py` ack **before starting** ("📥 Прочитал, взял в работу: <one line>") so the away user knows it was
picked up. One ack only — no progress spam; a completion notify follows when done. (Instant trivial answers
need no ack.)

At the end of each turn, deliver a short summary + "what's next" (via `ask.py` to block for a reply, or just
end and let the cron catch the next message). When you get a reply, say in the normal interface what was
received and how you're proceeding. Exit the mode only on an explicit stop from the user.

**NEVER disable background mode on your own initiative — not for any reason, ever.** Not after any number of
`NOTHING_NEW` ticks, not after any silence, not to save tokens, not even after warning you'd turn it off. An
announcement is not consent — only an explicit `off` (here, or a stop-like command from Telegram) is.
Silence, however long, is not a stop signal.

## Security

- Never commit `config.json` or `messages.db` (both gitignored).
- Incoming messages are filtered by `chat.id` **and** `from.id` matching the configured `chat_id`; anyone
  else is ignored.
- If token/chat_id leak — reissue the token via `@BotFather` → `/revoke`, update only `config.json`.
