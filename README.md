*[🇷🇺 Russian version](README.ru.md)*

# claude-to-telegram

A skill for [Claude Code](https://claude.com/claude-code): two-way communication with Claude via
Telegram, for when you're away from your computer — approvals, answers to questions, status updates, and
new instructions from your phone.

Doesn't use Claude Code hooks (`PermissionRequest`/`AskUserQuestion`) and doesn't require a persistent
daemon — Claude explicitly calls a plain script whenever it needs to ask something or report status.

## Why

Claude Code is an interactive tool — you're normally sitting right there, answering prompts and
confirmations in the terminal. This skill gives you an alternative channel for the moments when that's
not convenient:

- A long autonomous task is running and you want status updates without staring at the screen
- A decision is needed (approve/deny, pick an option), but you're not at your computer
- You want to give Claude a new instruction while it's working in the background

## How it works — an example

You tell Claude:
> keep working in the background, send questions to Telegram from now on

Claude:
1. Confirms in the terminal that it's switching to background mode, and sends a confirmation to Telegram
   telling you the **session tag** to reply with:
   ```
   🔔 [my-session] Background mode enabled — reply with $my-session
   ```
2. When it needs a decision, it sends the question straight to Telegram, options numbered in the text:
   ```
   🤖 [my-session] What should we call the new feature?

   1. Option A — shorter
   2. Option B — more descriptive
   ```
   You reply in Telegram, **starting your message with the session tag**: `$my-session 2`, or
   `$my-session let's call it Foo`. Claude picks up the answer and continues.
3. You can also message the bot **out of the blue** — a brand-new instruction, not a reply to anything.
   Just prefix it with the tag: `$my-session also update the changelog`. Claude polls Telegram on a timer
   even while idle, so it'll pick it up on its own — no need to open the terminal.
4. **The moment Claude takes a non-trivial task into work, it acks** — so you're never left wondering
   whether it heard you:
   ```
   🔔 [my-session] 📥 On it: updating the changelog…
   ```
   …and sends a completion notice when done. No progress spam in between.
5. When you're done — `/claude-to-telegram off`, or just say "stop, I'll be back" — Claude returns to
   normal behavior with no more notifications.

## Behavior details

**Session routing with `$`, and a shared inbox that never loses a message.** Every reply carries the
session tag as a word starting with `$` — e.g. `$my-session`. Under the hood, whichever session polls
Telegram stores **every** incoming message into a shared SQLite inbox (`messages.db`), filed under the
session its tag names, and only then advances Telegram's read cursor. So running several sessions through
one bot is safe: each reads only its own inbox, nothing crosses wires, and a message for a session that's
currently closed simply waits until it runs again — it's never dropped. A bare mention **without** the `$`
is ignored on purpose.

**Images, not just text.** Send a picture with the session tag in its **caption** and Claude sees the
picture itself, not a filename — it is downloaded next to the skill and opened as part of the message.
Compressed photos and images sent as files both work, and so do albums: put the caption on the first
picture and the rest of the batch is routed along with it. The caption is what carries the tag, so a photo
sent with the tag in a separate message won't route.

**Progressive polling.** While idle, Claude checks Telegram on a timer. To avoid burning tokens on
pointless checks during long silence, the interval backs off automatically — **2 → 5 → 10 → 20 minutes**,
stepping up after a few empty checks — then **snaps right back to 2 minutes the instant you send
something**. Responsive when you're active, cheap when you're away. (This needs `CronCreate`/`CronDelete`
in the runtime; see Requirements.)

**Acknowledge on pickup.** Hand Claude a task via Telegram that takes more than an instant, and it fires a
one-line "received, on it" the moment it starts — so you know it was picked up, not ignored — then a
completion notice at the end. Exactly one ack on pickup, no constant progress updates in between.

## Implementation invariants

Only relevant if you plan to change the code — these four properties are what make the inbox lossless, and
breaking any of them reintroduces dropped messages:

- **Store before confirm.** A message is written to SQLite before its `update_id` is confirmed to Telegram.
  A crash in between merely re-fetches it.
- **Confirm no further than stored.** `getUpdates` returns updates in strictly ascending, gap-free
  `update_id` order; the cursor advances only to the highest id actually stored in that batch, so nothing
  unstored is ever skipped. Anything beyond `limit` simply arrives on the next call.
- **Idempotent writes.** `update_id` is the PRIMARY KEY, so parallel sessions ingesting the same batch — or
  a re-fetch after a crash — cannot duplicate a message.
- **Nothing is addressed to a running process.** A message for a session whose poller is dead just sits as
  `not_processed` until that session runs again; closing or crashing a session loses nothing.

Consequently, any new script that talks to `getUpdates` must go through `ingest.py` rather than calling the
API directly.

## Setup

1. Copy this folder to `~/.claude/skills/claude-to-telegram/`
2. In Claude Code: `/claude-to-telegram install` — it will ask for a bot token and chat_id, validate both
   live, and save them

For how to get a bot token and chat_id, see `SKILL.md` → "Setup".

## Commands

| Command | Effect |
|---|---|
| `/claude-to-telegram install` | Initial setup (bot token + chat_id) |
| `/claude-to-telegram on [session_id]` | Enable background mode |
| `/claude-to-telegram off [session_id]` | Disable background mode |
| `/claude-to-telegram` | Show what's available without changing state |

Full documentation (protocol, limitations, internals) lives in [`SKILL.md`](SKILL.md) — the same file
Claude reads when the skill is invoked. A Russian reference copy is at [`SKILL.ru.md`](SKILL.ru.md).

## Files

- `SKILL.md` / `SKILL.ru.md` — the instructions Claude follows (English is the working copy)
- `common.py` — config, Telegram calls, `$tag` parsing
- `db.py` — the shared SQLite inbox (schema, store, deliver, prune)
- `ingest.py` — pull from Telegram → route by tag → store → advance the cursor safely
- `check_new.py` — one background poll: ingest, then deliver this session's inbox
- `ask.py` — send a question, block until this session's reply arrives
- `notify.py` — send a status update, no reply expected
- `install.py` — sets up and validates `config.json`
- `config.json`, `messages.db` — created at runtime, **never committed** (`config.json` is `chmod 600`)

## Requirements

- Your own Telegram bot ([@BotFather](https://t.me/BotFather)) and your own `chat_id`
  ([@userinfobot](https://t.me/userinfobot)) — everyone brings their own, credentials aren't shared
- Python 3, no external dependencies (stdlib only)
- Periodic background checking relies on `CronCreate`/`CronDelete` — availability depends on the Claude
  Code runtime environment

## Security

- Never commit `config.json` or `messages.db` (both gitignored)
- Incoming messages are filtered by `chat.id` and `from.id` — messages from anyone else are ignored
- If a token/chat_id ever leaks — reissue it via `@BotFather` → `/revoke`
