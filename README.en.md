*[🇷🇺 Русская версия](README.md)*

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
1. Confirms in the terminal that it's switching to background mode, and sends a confirmation to Telegram:
   ```
   🔔 [my-session] Background mode enabled
   ```
2. Keeps working. When a decision is needed, it sends the question straight to Telegram, with the options
   formatted right in the text:
   ```
   ❓ What should we call the new feature?

   1. Option A — shorter
   2. Option B — more descriptive

   Reply with a number or your own text.
   ```
   You reply directly in Telegram (`2` or free text) — Claude picks up the answer and continues.
3. If you closed the terminal and stepped away for a few hours — Claude periodically (roughly every ~10
   minutes, as long as the session is still open) checks Telegram for new messages on its own. Message the
   bot without ever opening the chat with Claude, and within a few minutes Claude notices and reacts.
4. When you're done — `/claude_to_telegram off`, or just say "stop, I'll be back" — Claude returns to
   normal behavior with no more notifications.

## Setup

1. Copy this folder to `~/.claude/skills/claude_to_telegram/`
2. In Claude Code: `/claude_to_telegram install` — it will ask for a bot token and chat_id, validate both
   live, and save them

For how to get a bot token and chat_id, see `SKILL.md` (or `SKILL.en.md`) → "Setup".

## Commands

| Command | Effect |
|---|---|
| `/claude_to_telegram install` | Initial setup (bot token + chat_id) |
| `/claude_to_telegram on [session_id]` | Enable background mode |
| `/claude_to_telegram off [session_id]` | Disable background mode |
| `/claude_to_telegram` | Show what's available without changing state |

Full documentation (protocol, limitations, internals) lives in [`SKILL.md`](SKILL.md) — the same file
Claude reads when the skill is invoked. An English reference copy is at [`SKILL.en.md`](SKILL.en.md).

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
