*[🇷🇺 Russian version](README.ru.md)*

# claude_to_telegram

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
5. When you're done — `/claude_to_telegram off`, or just say "stop, I'll be back" — Claude returns to
   normal behavior with no more notifications.

## Behavior details

**Session routing with `$`.** Every reply you send must contain the session tag as a word starting with
`$` — e.g. `$my-session`. That's how Claude knows the message is meant for *this* session. Run several
Claude Code sessions through the same bot and each gets its own tag, so replies never cross wires. A bare
mention of the session name **without** the `$` is ignored on purpose.

**Progressive polling.** While idle, Claude checks Telegram on a timer. To avoid burning tokens on
pointless checks during long silence, the interval backs off automatically — **2 → 5 → 10 → 20 minutes**,
stepping up after a few empty checks — then **snaps right back to 2 minutes the instant you send
something**. Responsive when you're active, cheap when you're away. (This needs `CronCreate`/`CronDelete`
in the runtime; see Requirements.)

**Acknowledge on pickup.** Hand Claude a task via Telegram that takes more than an instant, and it fires a
one-line "received, on it" the moment it starts — so you know it was picked up, not ignored — then a
completion notice at the end. Exactly one ack on pickup, no constant progress updates in between.

## Setup

1. Copy this folder to `~/.claude/skills/claude_to_telegram/`
2. In Claude Code: `/claude_to_telegram install` — it will ask for a bot token and chat_id, validate both
   live, and save them

For how to get a bot token and chat_id, see `SKILL.md` → "Setup".

## Commands

| Command | Effect |
|---|---|
| `/claude_to_telegram install` | Initial setup (bot token + chat_id) |
| `/claude_to_telegram on [session_id]` | Enable background mode |
| `/claude_to_telegram off [session_id]` | Disable background mode |
| `/claude_to_telegram` | Show what's available without changing state |

Full documentation (protocol, limitations, internals) lives in [`SKILL.md`](SKILL.md) — the same file
Claude reads when the skill is invoked. A Russian reference copy is at [`SKILL.ru.md`](SKILL.ru.md).

## Files

- `SKILL.md` — the instructions Claude follows (what to do for each command)
- `install.py` — sets up and validates `config.json`
- `ask.py` — send a question, block until a reply arrives
- `notify.py` — send a status update, no reply expected
- `check_new.py` — non-blocking check for new messages (used by the periodic background check)
- `config.json` — bot token + chat_id (created during setup, **never committed**, `chmod 600`)

## Requirements

- Your own Telegram bot ([@BotFather](https://t.me/BotFather)) and your own `chat_id`
  ([@userinfobot](https://t.me/userinfobot)) — everyone brings their own, credentials aren't shared
- Python 3, no external dependencies (stdlib only)
- Periodic background checking relies on `CronCreate`/`CronDelete` — availability depends on the Claude
  Code runtime environment

## Security

- Never commit `config.json` (it's in `.gitignore`)
- Incoming messages are filtered by `chat.id` and `from.id` — messages from anyone else are ignored
- If a token/chat_id ever leaks — reissue it via `@BotFather` → `/revoke`
