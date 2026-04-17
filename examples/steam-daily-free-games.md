# Example: daily Steam free-to-keep check

Every morning, check Steam Store for games that are 100% off (temporary
free-to-keep promotions), pre-filter out ones already in the library using the
search page's ownership markers, and claim the rest via the web.

## Setup

Same as the Epic example — need Chrome with Steam logged in, `chrome-devtools`
MCP configured, Telegram channel attached.

## The scheduled prompt

```
Daily check for free-to-keep Steam games — auto-triggered every morning.

## Goal
Check Steam Store for games 100% off, filter out already-owned ones on the
search page (saves product-page visits), and claim the rest. Always notify
Telegram chat_id <YOUR_CHAT_ID>.

## Why this URL
`specials=1&maxprice=free` returns items that actually have a discount and
are currently €0 — real limited-time freebies, not perm-free games or demos.

## Ownership pre-filter
Steam's search tiles get class `ds_owned` and an "IN LIBRARY" badge when
logged in and the game is owned. Skip those WITHOUT visiting the product
page — saves traffic.

## Steps
1. Navigate to https://store.steampowered.com/search/?specials=1&maxprice=free
2. Extract all `#search_resultsRows > a` tiles. For each, record title, appid,
   href, and whether its classList includes `ds_owned`.
3. Cap candidates (non-owned) at 6. For each:
   a. Navigate to product page.
   b. If it surprisingly shows "IN LIBRARY" → mark "📦 already-owned".
   c. If it's a DLC and base game not owned → mark "🔒 needs-base-game".
   d. If only "Install Steam" / "Play Now" and no web "Add to Account"
      button → mark "🚫 client-required".
   e. Otherwise click "Add to Account".
4. After clicking:
   - Success: mark "✅ claimed".
   - 2FA / Steam Mobile Authenticator: STOP. Mark "🔐 needs-2FA".
   - Captcha: STOP. Mark "🤖 captcha".
   - 2 failed attempts: mark "❌ unknown: <short>".
5. Telegram one message to <YOUR_CHAT_ID>:
   "🕹️ Steam free check (<date>): <N> already-owned, attempted <M>:
    - <Game>: <status>"
6. Terminate.

## Hard constraints
- Never attempt captchas, robot checks, 2FA.
- Never drive the Steam desktop client — browser only.
- Never retry a game beyond 2 click attempts per action.
- Never schedule more jobs or modify this job.
- Always send exactly one Telegram message.
- Soft timeout: ~8 minutes.
```
