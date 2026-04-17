# claude-scheduler-channel

A [Claude Code channel](https://code.claude.com/docs/en/channels) MCP server that pushes cron-scheduled prompts **into your running Claude Code session** — so scheduled jobs can use the same Telegram/Discord channel, the same Chrome DevTools connection, and the same MCP servers you already have attached.

Most scheduler MCPs spawn a fresh Claude process when a cron fires. That fresh process is blind to your live session's channels and tools. **This one doesn't** — it uses the channels protocol to deliver the prompt as a new turn in the session you already have open.

```
  ┌────────────────────┐       notifications/claude/channel
  │  scheduler (this)  │──────────────────────────────┐
  │  APScheduler cron  │                              │
  └────────────────────┘                              ▼
                                            ┌──────────────────┐
  ┌────────────────────┐                    │ Claude Code CLI  │
  │  telegram channel  │◀─── reply ────────▶│  (live session)  │
  └────────────────────┘                    └──────────────────┘
         ▲                                          │
         │ user chats                               │ chrome-devtools MCP,
         ▼                                          │ any other MCP server
      Telegram                                Tools / the web
```

## Why

If you're using Claude Code channels (Telegram, Discord, iMessage) and want to **fire recurring prompts through the same session** so Claude can do work and reply through your chat channel, the existing scheduler MCPs don't cut it — they spawn fresh Claude instances that don't have your channels attached.

Example workflow we use it for:

- **Weekly Epic Games claim** — every Friday morning, drive a logged-in Chrome via `chrome-devtools` MCP, add the free games to the library, Telegram-message the result.
- **Daily Steam free-to-keep check** — same pattern, different store.
- **Per-repo PR babysitter** — hourly, check GitHub for PR status, Telegram any that need attention.

## Requirements

- Claude Code **v2.1.80+** (channels feature)
- Python **3.10+**
- Connected claude.ai account (Console / API-key auth isn't supported by channels)

## Install

```bash
git clone https://github.com/DerKleineLi/claude-scheduler-channel
cd claude-scheduler-channel
pip install -r requirements.txt
```

Register the server at user scope so it's available from any project directory:

```bash
claude mcp add -s user scheduler -- python /absolute/path/to/claude-scheduler-channel/server.py
```

Verify:

```bash
claude mcp list
# ... scheduler: python /…/server.py - ✓ Connected
```

## Run

Launch Claude Code with the scheduler loaded as a channel:

```bash
claude --dangerously-load-development-channels server:scheduler
```

Combine with other channels in one invocation:

```bash
claude --dangerously-load-development-channels server:scheduler \
       --channels plugin:telegram@claude-plugins-official
```

A shell wrapper is the usual move:

```bash
# ~/bin/clive
#!/bin/bash
exec claude \
  --dangerously-load-development-channels server:scheduler \
  --channels plugin:telegram@claude-plugins-official \
  --dangerously-skip-permissions \
  "$@"
```

> `--dangerously-load-development-channels` is required until this server is on Anthropic's allowlist or your organization's `allowedChannelPlugins`. That's by design of the research preview.

> `--dangerously-skip-permissions` is optional but recommended for unattended runs — scheduled jobs can otherwise stall on permission prompts.

## How to use

From any live Claude session, ask Claude to schedule work:

> "Every Friday at 8:47 AM, open the Epic Games Store, claim whatever's free this week, and Telegram me a summary."

Claude will call the `add_job` tool with a cron expression and a self-contained prompt. When the cron fires, the scheduler pushes the prompt back into your session as a `<channel source="scheduler">` event, and Claude runs it.

### Tools exposed

| Tool         | Purpose                                                            |
|--------------|--------------------------------------------------------------------|
| `add_job`    | Schedule a recurring prompt via 5-field cron (local time, Unix convention) |
| `list_jobs`  | Show all jobs with next-fire time                                  |
| `remove_job` | Remove a job by id                                                 |
| `fire_now`   | Fire a job immediately for testing                                 |

### Cron format

Standard Unix 5-field cron in local time. Day-of-week uses Unix convention (`0=Sun, 1=Mon, …, 5=Fri, 6=Sat`, `7=Sun` also accepted). Names work too: `sun mon tue wed thu fri sat`.

```
*/15 9-17 * * mon-fri      # every 15 min, 9am-5pm weekdays
47 8 * * fri               # Fridays 08:47
17 9 * * *                 # daily 09:17
0 6 1 * *                  # 06:00 on the 1st of every month
```

Jobs persist to `~/.claude/scheduler_channel/jobs.json` across restarts.

### Writing good scheduled prompts

The prompt runs with **no prior session context** — a fresh turn in the session. Write prompts as if they'll be read cold by Claude 10 weeks from now:

- Say what tools / URLs to use
- Name hard constraints ("never solve captchas", "don't retry past 2 attempts")
- Say "don't schedule more jobs from within this run" (prevents self-replication)
- Say "always send exactly one Telegram message" (silence is a bug)
- Set a soft timeout so Claude stops iterating

See `examples/` for ready-to-use prompts.

## Caveats

- **Requires a running Claude CLI**. If no session is open, cron firings drop with a warning (the job still persists for next session).
- **Session capture is lazy** — the server captures the session reference on the first tool call in a session, because the MCP Python SDK doesn't expose a cleaner initialization hook. If a cron fires before any tool call in a fresh session, that single fire is dropped. In practice this doesn't bite because any `list_jobs` / `add_job` call captures the ref.
- **Custom notification hack** — the SDK's `ServerNotification` is a locked `RootModel` that doesn't accept custom methods like `notifications/claude/channel`. We bypass it by sending a raw `JSONRPCNotification` via `session.send_message()`.
- **APScheduler day-of-week quirk** — APScheduler treats the dow field with Mon=0 internally, even through `from_crontab` (which is supposed to map to Unix convention). We normalize numeric dow to names before handing it over. Future releases of APScheduler may fix this.
- **Research preview** — channels are still a research preview in Claude Code. The protocol surface (`claude/channel` capability, `notifications/claude/channel` method) may change. Pin your Claude Code version or watch for breaking changes.
- **Prompt injection risk**. Any MCP server that can push prompts into your session is a prompt-injection vector. The scheduler's input surface is the `add_job` tool, which only Claude can call from within your session — so injection would have to come from a compromised Claude session or a stored job. Don't run with `--dangerously-skip-permissions` in untrusted contexts.

## Comparison with alternatives

| Feature                                    | this       | [`CronCreate`][cc] (built-in) | [`phildougherty/claudecron`][pc] | [`jolks/mcp-cron`][jc] |
|--------------------------------------------|:----------:|:-----------------------------:|:--------------------------------:|:----------------------:|
| Fires into your **live** Claude session    | ✅         | ✅                            | ❌ spawns fresh                  | ❌ spawns fresh        |
| Persists across CLI restarts               | ✅         | ✅ with `durable: true`       | ✅                               | ✅                     |
| Cron + hooks + file-watch triggers         | ❌         | ❌                            | ✅                               | ❌                     |
| Additional trigger types planned           | webhook?   | —                             | ✅                               | ✅                     |
| Complexity                                 | ~250 LOC   | built-in                      | bigger                           | bigger                 |

If you don't need the live-session push, `CronCreate` is the simpler choice and it's already in Claude Code.

[cc]: https://code.claude.com/docs/en/scheduled-tasks
[pc]: https://github.com/phildougherty/claudecron
[jc]: https://github.com/jolks/mcp-cron

## Security

This server is a channel plugin. That means when enabled via `--channels` / `--dangerously-load-development-channels`, it can push user-turns into your live session. Consider:

- Don't run with `--dangerously-skip-permissions` in environments where a compromised scheduled prompt could cause harm.
- Review the `add_job` prompts other users (or your past self) have registered — they're in `~/.claude/scheduler_channel/jobs.json` in plain text.
- Channels aren't on the Anthropic allowlist yet; you must opt in with `--dangerously-load-development-channels`. That's a feature, not a bug — it means this plugin can't push to random users' sessions.

## Contributing

Issues and PRs welcome. Keep dependencies minimal; this is meant to stay small enough to audit in one sitting.

## License

MIT — see `LICENSE`.
