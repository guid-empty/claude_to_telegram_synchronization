---
name: claude-to-telegram
description: Two-way communication with the user over Telegram when they explicitly ask to work in the background or remotely — send status and questions to a Telegram bot and receive their replies; several parallel sessions can share one bot. Trigger when the user says "work in the background", "send questions/status to Telegram", "let's talk through the bot from now on", gives a session_id to use, explicitly invokes /claude-to-telegram, or /claude-to-telegram on|off. Do NOT apply as a standing behavior — only on explicit request, for one specific session.
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

- `common.py` — config load, Telegram calls, `$tag` parsing/stripping, attachment download
- `db.py` — the shared SQLite inbox (schema, store, inbox, mark, prune; WAL mode)
- `ingest.py` — pull from Telegram → route each message by its `$tag` → store → advance the offset **only up
  to what was stored** → prune >7 days
- `check_new.py` — one background poll for a session (engine=cron): ingest, then deliver this session's own inbox
- `watch.py` — long-running delivery loop for `engine=monitor`: same ingest+deliver, but prints only
  actual messages (and its own failures), so every line is a notification worth reading
- `ask.py` — send a question, block until this session's reply arrives (via the inbox)
- `notify.py` — fire-and-forget status message
- `install.py` — write + validate `config.json`
- `config.json`, `messages.db`, `.backoff_*.json`, `media/` — local only, gitignored

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

Syntax: `/claude-to-telegram on|off [session_id] [engine=cron|monitor]` — `session_id` optional third
word, `engine` optional and **defaults to `cron`**.

**Which engine to use.** `cron` fires only while the REPL is idle, so anything that arrives mid-task waits
until the task ends — measured on a live day of use: median 2 min, 90th percentile 14 min, worst 20 min.
That gap is invisible to the sender and reads as being ignored. `monitor` streams instead of polling on a
schedule: the message surfaces during the work, like a message typed into the CLI. Use `cron` for
long quiet stretches (it costs nothing while silent), `monitor` when the user is actively sending
follow-ups to work already in progress.

Never run both for the same session — two readers drain the same inbox and each message lands twice.

`/claude-to-telegram on [session_id] engine=monitor`:
1. Determine `session_id` exactly as for `cron` (see below), tell the user, `notify.py … "Background mode
   enabled"`.
2. Do NOT create a cron job and do NOT touch `.backoff_*` — the ladder belongs to the cron engine.
3. Start the watcher and keep its task id for the later stop:
   ```
   Monitor({
     command: "python3 ~/.claude/skills/claude-to-telegram/watch.py --session <id>",
     description: "Telegram messages for <id>",
     persistent: true,
   })
   ```
4. Every emitted line is a message from the user (or a `WATCH_ERROR`/`WATCH_FATAL` line about the watcher
   itself). Treat text lines exactly as `check_new.py` output: say "Received from Telegram: …" and act.
   There is no `NOTHING_NEW` and no `RESCHEDULE` — silence means no mail.
5. Follow the "Background mode protocol" as usual. On `off`: `notify.py … "Background mode disabled"`,
   then `TaskStop` the monitor (not `CronDelete`).

`/claude-to-telegram on [session_id]`:
1. Determine `session_id`: use the one given (command arg, or named earlier in the conversation); else
   generate `<project-name>-<first 8 chars of the session UUID>` (project = last component of `cwd`; UUID
   from the "Scratchpad Directory" path in your system prompt, not a separate tool call).
2. Tell the user (in the normal interface) the final `session_id`, and that to reach this session they
   prefix a Telegram message with `$<session_id>` (e.g. `$my-session do X`).
3. `notify.py --session <id> --message "Background mode enabled"`.
4. **Delete `~/.claude/skills/claude-to-telegram/.backoff_<session_id>.json` if it exists.** It survives
   `off` and still holds the interval this session had reached last time; the cron you are about to create
   starts at 2 min, so a stale file makes `check_new.py` compare against the wrong interval and print
   `RESCHEDULE=none` forever — the ladder never climbs and every tick burns an inference call at the most
   expensive rung.
5. Create a recurring `CronCreate` job at the base interval (`*/2 * * * *`) that self-adjusts via
   back-off (see "Polling" below) — remember its job id for the later `CronDelete`.
6. Follow the "Background mode protocol" at the end of every turn until an explicit off.

`/claude-to-telegram off [session_id]`:
1. If enabled this conversation — `notify.py --session <id> --message "Background mode disabled"`.
2. Stop the delivery: `CronDelete` the polling job for `engine=cron` (use `CronList` if you lost the id),
   or `TaskStop` the watcher for `engine=monitor`.
3. Back to normal turn completion.

`/claude-to-telegram` with no args — just load this instruction into context; don't change session state.

**An `off`-like command sent from within Telegram** (caught by `check_new.py`, text reads as a stop) —
handle it the same as an explicit `off` here.

## How it works

Every poll runs two phases. **Ingest** (`ingest.py`) drains Telegram into the shared inbox, filing each
message under the session its `$tag` names, and only then advances the read cursor. **Deliver** prints this
session's own unread messages (tag stripped) and marks them read. Doing it in that order is what keeps
several sessions on one bot from erasing each other's mail, and a message for a closed session simply waits
until that session runs again.

**Delivered ≠ handled.** By default delivery marks a message `read` the moment it is printed, so the database
cannot tell "the session picked this up" from "the session already finished it". Pass `--defer-read` to
`check_new.py` and delivery marks the message `in_progress` instead — the request stays visibly open until
the session reports back with `notify.py … --done`, which flips this session's `in_progress` rows to `read`
(and prints how many it closed). The full lifecycle is `not_processed` → `in_progress` → `read`. That middle
state is worth having twice over: it answers "what is this session actually working on right now" when you
are debugging, and it tells the away user their request wasn't quietly dropped.

Closing happens **only after the message is sent successfully** — if the send fails the request stays open,
which is more honest than losing it silently. An `in_progress` message is never re-delivered (the inbox
selects only `not_processed`), so there are no duplicates, and `--done` touches its own session and nobody
else's. Both flags are optional by design: a parallel session that passes neither behaves exactly as it did
before, so adding this mode breaks nothing that already runs.

One rule if you touch the code: any new script that calls `getUpdates` must go through `ingest.py` — never
advance the offset past what has been stored. The full set of invariants is in the README.

**Images.** A picture sent with a caption is routed by the tag in that caption, downloaded into `media/`, and
delivered as a line `[image: /abs/path.jpg]` after the text. **Open that path with the `Read` tool** — stdout
cannot carry a picture, so the path is the only way you actually see it; treat it as part of the message, not
as a file reference to mention back. Both compressed photos and images sent as files are handled. An album
arrives as one update per picture with the caption on the first only, so the rest inherit the owner via
`media_group_id` — expect several `[image: …]` lines under a single caption. A download that fails is
skipped, never costing the message itself.

## Polling while the session is idle (CronCreate) + progressive back-off

Cron "fires while the REPL is idle" — the only way to wake an idle session. `check_new.py` also drives a
back-off ladder (**2 → 5 → 10 → 20 min**): +1 rung after 3 consecutive empty checks, reset to 2 min the
instant a message arrives. It can't reschedule the cron itself, so it prints a final line:

- `RESCHEDULE=none` — interval unchanged, do nothing.
- `RESCHEDULE=<M>` — reschedule the polling cron to `*/M * * * *`: `CronDelete` the current job, `CronCreate`
  a new recurring one reusing the same prompt, remember the new id (`CronList` if you lost it).

Enable at the base interval:
```
CronCreate(cron="*/2 * * * *", recurring=true, prompt="Check for new Telegram messages: run
python3 ~/.claude/skills/claude-to-telegram/check_new.py --session <session_id> --defer-read. If the final line is
RESCHEDULE=<M>, reschedule this polling cron to */M (CronDelete the current job, CronCreate a new one with
the same prompt, remember its id); RESCHEDULE=none — do nothing. If the output is only NOTHING_NEW — stay
silent. Any other text is a message from the user: state 'Received from Telegram: ...' explicitly, then act
on it. NEVER disable background mode on your own — only on an explicit off.")
```

`--defer-read` is what keeps a picked-up task visible as `in_progress` until you answer it with
`notify.py … --done` (see "How it works"). Drop the flag and polling reverts to the old behaviour: everything
delivered is `read` at once.

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
python3 ~/.claude/skills/claude-to-telegram/notify.py --session <session_id> --message "<result>" --done
```

Add `--done` when the message is the **final answer** on a request that was delivered with `--defer-read`: it
closes this session's open requests once the send goes through. Interim messages — the pickup ack, progress
updates — go **without** `--done`, the task isn't finished yet.

## Background mode protocol

**NEVER call the native `AskUserQuestion` tool while background mode is active.** It is a *blocking* tool —
it suspends the turn waiting for a click in the terminal UI, so the REPL is no longer idle, so the polling
cron cannot fire. The result: polling silently stalls, the user's Telegram messages pile up unanswered for
as long as the block lasts, and it looks like you hung — a single blocking prompt can stall polling for
hours while messages accumulate unread. If you need the user to choose something, **ask in
Telegram** — send the numbered options via `notify.py`/`ask.py` (see "Emulating AskUserQuestion") and read
the reply from the inbox. Never the native tool, not even for a "quick" confirm.

**ALWAYS bound background commands with a `timeout` — and never trust "the notification will come."** A `Bash` call with `run_in_background: true` has NO built-in timeout (unlike a synchronous call,
which defaults to ~120s). If such a command hangs, it hangs *forever* and never notifies — so if you're
passively waiting on its completion event, you stall indefinitely and it looks exactly like the
`AskUserQuestion` freeze above. Rules: (1) wrap the command in a shell `timeout N`
(e.g. `timeout 600 chrome --headless … --screenshot …`) so it self-kills — see the unconditional list below;
(2) never conclude a turn "waiting"
on a single background task as your only continuation — poll its status actively (or set a fallback) and
`pkill`/`kill` the hung process instead of waiting; (3) headless-Chrome screenshots of Flutter-web pages that
init Firebase/network **hang** under `--virtual-time-budget` (virtual time never drains while Firebase waits
on the network) — don't rely on them for such pages, they will not complete. An unbounded background
screenshot of one can sit there for many hours before anyone notices.

**A *synchronous* call is not safe either — it's the same freeze, only harder to spot.** The ~120s default
protects you only until you override it; passing a generous `timeout` to a call that then hangs blocks the
turn for exactly that long, the REPL never goes idle, and the polling cron never fires. Nothing is printed
and the session answers normally in the terminal, so the only visible symptom is the user asking "are you
stuck?" — into a mailbox nobody is reading. Diagnosis, in order: `ls -la .backoff_<session>.json` (its mtime
is the last poll — stale mtime means polling is dead), then `ps -eo pid,etime,command | grep -i chrome` for a
process with a multi-hour `etime`, then `kill -9` it; the session unblocks and drains its inbox on the next
tick.

Note that a *local* page is no safer than a remote one: if the dev server serving it has already exited, the
request never resolves, `--virtual-time-budget` never drains, and the flag you added as an escape hatch
guarantees the hang instead of bounding it. Screenshot only what you have just verified is being served.

**Do not judge whether a command "looks long" — wrap it unconditionally** if it does any of the following,
however quick you expect it to be: starts a browser (headless or not), makes network requests, builds a
project, starts or queries a local server, or runs docker. This list is not a heuristic to weigh; if the
command matches, it gets a `timeout`.

The reason the rule is unconditional: **a program's own self-termination flags do not replace the wrapper.**
`--virtual-time-budget`, `--timeout`, `--max-time` and friends read like a built-in cap and make the wrapper
feel redundant — which is exactly the trap. Those flags are honoured by the program's own event loop, so a
request that never resolves means the flag is never evaluated and the "8-second" command runs until someone
kills it. A command that advertises a short bound is not evidence that it is bounded.

This applies **only while background mode is active**. In a normal interactive session the user is watching
the terminal and sees a stuck command within seconds, so wrapping everything is unnecessary ceremony. In the
background nobody is watching, the cost of a hang is measured in hours, and the wrapper is cheap.

**Default to `timeout 600`** (10 min) and override only when the operation clearly warrants it. The value is
a runaway *safety cap*, not a deadline, so it must sit generously ABOVE the realistic worst case — otherwise
a healthy-but-slow run gets killed. 600s covers a web build or CI step comfortably; a docker build may need
more, while a screenshot that *should* finish in ~30s can drop to ~120s. Too low a cap (60s on a 3-minute
build) just destroys good work. And when a command is known to hang rather than run slow (headless+Firebase),
a timeout only bounds the damage — prefer not running it at all.

**Acknowledge on pickup.** When a task arrives via Telegram and will take more than an instant, send a short
`notify.py` ack **before starting** ("📥 Got it, working on: <one line>") so the away user knows it was
picked up. One ack only — no progress spam; a completion notify follows when done. Send the ack **without**
`--done` and put `--done` on that completion notify — the request stays `in_progress` for exactly as long as
the work does. (Instant trivial answers need no ack.)

**Read the inbox immediately before every send.** Long work — a build, a deploy, a verification run — takes
minutes, and in those minutes the user keeps typing: corrections, refinements, answers to questions you asked
earlier. Firing off a message you composed before all that arrived puts the two of you out of sync: you ask
about something already answered, or report on a task the user has since redefined. It has happened for real —
a message went out saying "three options are waiting for your choice" when the choice had arrived long before
and the work was already done against it. So right before each `notify.py`/`ask.py` call, run `check_new.py`
for your session and actually read what came in; if there are corrections, fold them in first and only then
send — quite possibly a different message than the one you had drafted. One script call, and it costs nothing.

At the end of each turn, deliver a short summary + "what's next" (via `ask.py` to block for a reply, or just
end and let the cron catch the next message). When you get a reply, say in the normal interface what was
received and how you're proceeding. Exit the mode only on an explicit stop from the user.

**NEVER disable background mode on your own initiative — not for any reason, ever.** Not after any number of
`NOTHING_NEW` ticks, not after any silence, not to save tokens, not even after warning you'd turn it off. An
announcement is not consent — only an explicit `off` (here, or a stop-like command from Telegram) is.
Silence, however long, is not a stop signal.

## Working in a git worktree

Background work usually runs while the user is elsewhere in the same repo, so an isolated worktree is the
default — it keeps the session from touching the user's working tree.

**Name it `worktree-<session_id>-<YYYY-MM-DD>[-<suffix>]`** — session id first, then the date the work
started, then an optional suffix when the same session explores several approaches in parallel
(`worktree-bugs-2026-08-05-tables`, `worktree-bugs-2026-08-05-history`). Use the same string for the branch
and for the directory under `.claude/worktrees/`:

```bash
git worktree add .claude/worktrees/<session_id>-<YYYY-MM-DD> \
  -b worktree-<session_id>-<YYYY-MM-DD> main
```

The prefix is what makes a stray worktree identifiable months later: it says which session created it and
when, without opening a single file.

## Security

- Never commit `config.json` or `messages.db` (both gitignored).
- Incoming messages are filtered by `chat.id` **and** `from.id` matching the configured `chat_id`; anyone
  else is ignored.
- If token/chat_id leak — reissue the token via `@BotFather` → `/revoke`, update only `config.json`.
