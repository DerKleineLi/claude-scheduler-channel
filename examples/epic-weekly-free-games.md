# Example: weekly Epic Games free-game claim

Every Friday morning, drive a logged-in Chrome via `chrome-devtools` MCP to
claim the free-this-week games on Epic Games Store, then Telegram a summary.

## Setup

1. Run Chrome with remote debugging exposed and log into Epic Games Store.
2. Add the `chrome-devtools` MCP server (connecting to `http://127.0.0.1:9222`).
3. Add the Telegram channel plugin. Note your chat_id.
4. Launch Claude Code with scheduler + Telegram + chrome-devtools all attached.

## Registering the job

In any Claude session, say:

> "Every Friday at 8:47 AM, claim Epic Games Store's free games. Use the prompt in examples/epic-weekly-free-games.md."

Or invoke the tool directly:

```python
add_job(
    name="epic-weekly-free-games",
    cron="47 8 * * fri",
    prompt=OPEN_EXAMPLES_FILE_AND_PASTE_PROMPT_BELOW,
)
```

## The scheduled prompt

Write prompts as fully self-contained — they run with no prior conversation context.

```
Weekly Epic Games free-game claim — auto-triggered every Friday morning via scheduler.

## Goal
Claim this week's free games on Epic Games Store using the chrome-devtools MCP
(already attached to the user's logged-in Chrome). Always notify Telegram
chat_id <YOUR_CHAT_ID> with the outcome.

## Steps
1. Navigate to https://store.epicgames.com/en-US/free-games
2. Take a snapshot. Identify games in the "Free Now" section (NOT upcoming).
3. Cap at 3 games per run. For each game:
   a. Navigate to its store page.
   b. If "In Library" → mark "📦 already-owned", skip.
   c. If "Not available in your region" → mark "⛔ region-blocked", skip.
   d. Otherwise click "Get", then the final "Place Order" on checkout.
   e. If a captcha appears → STOP THIS GAME. Mark "🤖 captcha". DO NOT solve.
   f. If state doesn't progress after 2 attempts → mark "❌ unknown", skip.
4. Send ONE Telegram message to <YOUR_CHAT_ID> summarizing results.
5. Terminate.

## Hard constraints
- Never attempt captchas, robot checks, or puzzles.
- Never retry a game beyond 2 click attempts per action.
- Never schedule more jobs or modify this job.
- Always send exactly one Telegram message.
- Soft timeout: ~8 minutes.
```

## Why these constraints

- **Never solve captchas** — bypassing robot checks violates most ToS, and silent failures beat ban risk.
- **Cap retries** — prevents the job from looping on an unexpected UI state.
- **"Never schedule more jobs"** — important. Without this, a confused run could call `add_job` from within itself and create a runaway.
- **"Always send one Telegram message"** — silence is indistinguishable from "Claude crashed". A failure message is always better than nothing.
