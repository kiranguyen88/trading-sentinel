# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

```bash
# Run locally
python app.py          # starts Flask on http://localhost:5000

# Install dependencies
pip install -r requirements.txt

# Create an account (needed once — sign-up is closed by default)
python create_user.py you@example.com your-password --owner

# Deploy — Vercel auto-deploys on every push to main
git push origin main
```

There are no tests, no lint config, and no build step. The only runtime requirement is a valid `.env` (see `.env.example`).

---

## Architecture

This is a single-process Python Flask server with a pure-JS single-page frontend. There are three Python files and one HTML template — no framework beyond Flask.

### File responsibilities

| File | Role |
|---|---|
| `app.py` | Flask routes, SSE price stream, cron job bodies, TTL cache |
| `auth.py` | Accounts (hash/verify, `login_required`, `change_password`), chat logging, owner bootstrap |
| `create_user.py` | One-off CLI to create an account (the first one has to come from somewhere) |
| `check_supabase.py` | Diagnostic: is the service key valid and do the tables exist? |
| `trading_bot.py` | Market data (yfinance), technical indicators, Gemini AI chat, Discord alerts, portfolio/journal persistence |
| `journal.py` | Trade ledger — FIFO replay, realized P&L, and the holdings projection |
| `screener.py` | 400+ stock universe, batch technical scan, Gemini ranking of best setups |
| `templates/index.html` | Entire frontend — CSS, HTML, vanilla JS (no build toolchain) |
| `templates/login.html` | Login / register page |

### Data flow

```
Browser ──SSE──► /api/stream/prices  (15 s ticks, price-worker thread)
Browser ──GET──► /portfolio          (full snapshot with technicals, TTL-cached 120 s)
Browser ──GET──► /watchlist          (watchlist snapshot, TTL-cached 120 s)
Browser ──POST─► /chat               (streaming SSE, Gemini 2.5 Flash)
```

The SSE price stream (`_price_worker` thread in `app.py`) runs every 15 s, batches all portfolio + watchlist tickers via `yfinance.download()`, and broadcasts to every connected client via a per-client `queue.Queue`. New clients immediately receive the cached `_fast_prices` snapshot so they don't wait.

### Accounts and auth

Email + password in our own `users` table — not Supabase Auth, so the browser never
needs an anon key and the server has no JWTs to verify. Passwords are hashed with
`werkzeug.security`; the session is a signed Flask cookie (`SECRET_KEY`, 30 days).

- `auth.login_required` guards every app route. Browsers get a redirect to `/login`;
  JSON/SSE endpoints get `401 {"error":"auth"}`, and a `fetch` wrapper in the frontend
  bounces the page to `/login` on any 401.
- `/cron/*` keeps its own `CRON_SECRET` guard instead, and binds the **owner** account
  via `_bind_owner()`. Scheduled alerts go to one Discord webhook from a global env var,
  so they run for the owner only — other accounts get the web app but no alerts.
- `POST /admin/bootstrap` (CRON_SECRET-guarded, idempotent) creates the owner from
  `OWNER_EMAIL`/`OWNER_PASSWORD` and copies the pre-accounts `app_state` row into their
  `user_state`. It never modifies or deletes `app_state`, which stays as a rollback path.
- Registration is closed unless `ALLOW_REGISTRATION` is truthy. With it closed, create
  accounts with `python create_user.py <email> <password> [--owner]`.
- `POST /account/password` changes the signed-in user's password. It requires the current
  password even though the caller already holds a session — otherwise anyone at an
  unlocked browser could lock the real owner out.

### Supabase tables

| Table | Holds |
|---|---|
| `users` | id, email, password_hash, display_name, is_owner, last_login_at |
| `user_state` | one jsonb row per user: `{holdings, watchlist}` |
| `chat_messages` | write-only log of every chat turn (user_id, role, content) |
| `journal_entries` | trade journal, jsonb body, scoped by user_id |
| `app_state` | **legacy** single global portfolio row — superseded, kept for rollback |

RLS is on with no policies on all of them, so only the service key reaches them.

### Portfolio persistence — per user

`load_portfolio()` reads `user_state` for the **current user**, which comes from a
`ContextVar` in `trading_bot.py` (`set_current_user` / `current_user_id`). It is set by
`@app.before_request` from the session, and by `_bind_owner()` on cron routes. This is
why `load_portfolio()` takes no `user_id` argument — its nine call sites across
`app.py`, `trading_bot.py` and `screener.py` stay unchanged. **A streamed response is
consumed after the request context ends, so `/chat`'s generator re-binds the user
itself.**

`load_portfolio()` never writes. Supabase being *unreachable* is not the same as the row
being *empty* — only one of those is safe to overwrite, and conflating them is what once
replaced the live portfolio with the bundled seed. Seeding happens only in
`seed_portfolio_if_empty()`.

With no user and no database (offline dev), it falls back to the old chain: local
`DATA_DIR/portfolio.json` → `PORTFOLIO_JSON` env var → committed `portfolio.json` seed.

**Anything cached per process must be keyed by user id** — `_cached_fetch`, `_fast_prices`
and `_alert_key` all are. A missed key leaks one account's positions to another.

### Trade journal — realized P&L

`journal.py` turns `journal_entries` into a real ledger, and **the ledger is the only
writer of `holdings`**. A logged buy opens a position, a sell closes shares FIFO and
books realized P&L, and `user_state.holdings` is a *projection* of whatever lots remain.

The top half of the file is pure — no project imports, plain dicts in, dataclasses out —
so the FIFO maths can be exercised in a REPL without a database. Only the I/O tail below
the marker touches Supabase.

**Nothing derived is ever stored.** Realized P&L, open lots and totals are recomputed by
replaying the whole ledger on each read, so deleting or back-dating an entry self-heals.
That is also forced by the storage layer: `save_journal()` is a no-op on the DB path, so
entries can only be added and deleted one row at a time — there is no bulk rewrite and
therefore no edit-in-place.

Invariants worth keeping:

- **Sort in Python, never trust the DB order.** `load_journal()` orders by the *table*
  `created_at`, which is not the trade date. `sort_key()` orders by
  `(open-first, date, time, buys-before-sells, created_at, id)`. Opening balances rank
  first rather than being given a fake old date, and buys precede sells on the same date
  so logging a sell before remembering the buy isn't read as an oversell.
- **`replay()` is total — it never raises.** A malformed or over-selling entry is clamped
  and reported in `warnings`; one bad row must not make the journal unloadable. Callers
  decide what to do: the add route rejects a *new* oversell with 400, the delete route
  allows it (blocking would trap the user with an entry they can't remove).
- **Never derive holdings from an empty or failed ledger.** `read_ledger()` raises rather
  than returning a partial list, `load_journal()` refuses a truncated PostgREST response
  (`JournalTruncated` — losing the oldest rows would drop the buys FIFO matches against),
  and `sync_holdings()` refuses to write when the ledger is empty. Same reasoning as
  `load_portfolio()`: unreachable ≠ empty.
- **Ledger first, holdings second.** If `save_portfolio()` fails after the entry is
  written, the route still returns 200 with `holdings_synced: false` — the trade is
  durably recorded and claiming failure would make the user log it twice. `/journal/summary`
  repairs the drift on the next load; `/journal/resync` does it on demand.
- **Holdings are one row per ticker**, blended cost. A row per lot would make the lot
  index unstable — selling out of lot 0 renumbers the rest, and both the `TICKER#i` price
  cache key and the DOM's `data-lot` would then point at the wrong lot. The lots
  themselves stay visible in the journal.
- Fees are capitalised on entry and deducted on exit, so realized P&L is net; `totals`
  also carries `realized_pnl_gross` and `fees_total`.
- **Never call journal code from a background thread.** The `ContextVar` is unset
  off the request thread, so it would silently read and write the local dev
  `journal.json`. `_require_user()` raises instead. The `/cron/*` jobs are safe
  because they run on a real request and call `_bind_owner()` first — but a
  thread they spawn is not, and neither is a `/chat` generator, which is why
  that one re-binds the user itself.

Routes: `GET /journal/summary`, `POST /journal/trade`, `POST /journal/adjust`,
`POST /journal/backfill`, `POST /journal/resync`, `DELETE /journal/delete/<id>`. The
route function for `GET /journal` is `journal_list` — naming it `journal` would rebind
the imported module.

Hand-edits in the Edit Portfolio modal still work: `/portfolio-update` reads a submitted
holdings array as a *declared target per ticker* and records `adjust` entries, rather than
inferring a diff. `saveWatchlist()` must therefore never echo holdings back — it posts
`{watchlist}` only.

### Technical indicators

All indicators are computed in `get_stock_data()` (`trading_bot.py`):
- RSI(14), MACD(12,26,9), Bollinger Bands(20,2), MA20/MA50/MA200
- Volume ratio (today vs 20-day avg)
- 52-week high/low from `yfinance fast_info`

`get_portfolio_snapshot()` fetches all holdings in parallel using `ThreadPoolExecutor`. `get_watchlist_snapshot()` does the same for watchlist tickers.

### Scheduled jobs

Nothing schedules itself in-process — there is no APScheduler and no background
thread. Every job is an HTTP `GET /cron/*` fired from outside, which is why they
all start with `_verify_cron() or _bind_owner()`: the request arrives with no
session, so the owner has to be bound onto the `ContextVar` by hand.

Schedules are **UTC** (`vercel.json`), so they drift an hour against ET across
US daylight saving. VN does not observe DST, so the VN-anchored jobs are stable.

**Vercel gets exactly one cron slot, by choice.** Four jobs used to hold four,
and three of those fired inside the same 95-minute pre-open window —
`auto_scan_watchlist()` at 6:55 ET, `run_daily_digest()` at 7:00 ET and
`run_premarket_scan()` at 8:30 ET, all briefing on the same portfolio. The scan
and the watchlist now share one slot behind `/cron/pre-open`, and the post-close
digest is no longer scheduled at all. Adding a job means folding it into that
slot, not claiming a second.

| Cron (UTC) | Fired by | Job |
|---|---|---|
| `30 12 * * 1-5` | Vercel | `/cron/pre-open` — `run_premarket_scan()` **+** `auto_scan_watchlist()`, 8:30 ET in DST / 7:30 ET in winter |
| `0 13-21 * * 1-5` | GitHub Actions (`.github/workflows/hourly-warnings.yml`) | `check_warnings()` — self-gates on ET market hours 9:30–16:00 |

The two legs of `/cron/pre-open` are independently wrapped: losing the watchlist
scan is not a reason to lose the gap alert, so either can fail and the other
still sends.

`/cron/premarket`, `/cron/watchlist-scan` and `/cron/daily-digest` still exist
and still work — they just have no slot, and are how you run one leg on its own.
`/cron/close-summary` is gone; its body was a duplicate of `/cron/daily-digest`,
so the digest is still reachable there and from the UI button, just not on a
schedule.

Alert deduplication: `_sent_alerts` set uses `{date}:{user_id}:{ticker}:{alert_type}` keys; one alert per user per ticker per type per calendar day. `/check-now` clears only the calling user's keys before running.

The set is persisted to `/tmp`, which on Vercel lives on one warm instance —
a cold start loses it. Dedup is therefore best-effort suppression of repeats,
never a correctness guarantee, and nothing may depend on it having fired.

### Pre-open scan

`run_premarket_scan()` in `app.py` answers one question before the bell: what is
the portfolio walking into today. It sends **one** message for the whole
portfolio — gaps, earnings and headlines in one ordered list — because eight
separate pings at 8am answer that question worse than one list does.

The gap maths is the part that is easy to get wrong. `get_premarket_snapshot()`
takes the reference close from **daily** bars dated strictly *before* today and
the pre-open price from **today's 1-minute extended-hours** bars. Reading the
reference from a daily bar dated today instead would compare this morning
against itself — during premarket that row is a partial bar that already
contains the gap. `get_index_futures()` guards the same way: with no daily bar
dated today it returns `{}` rather than a number that describes yesterday while
reading as this morning.

A ticker with no bar dated today is reported as unpriced, never as flat. That is
what makes the job safe on market holidays — the scheduled path sees no prints
at all and stays silent instead of briefing you on stale prices.

Headlines are relevance-filtered (`_is_about()`), not just recency-filtered.
Yahoo's per-ticker feed pads itself with generic market commentary, and printed
under a gap that padding reads as the *explanation* for the gap. A headline
naming neither the symbol nor the company is dropped; a gap with no headline
under it is itself the signal.

Quiet mornings still send one line rather than nothing — a job that only speaks
up on bad days is a job you cannot tell has died. Thresholds are
`PREMARKET_GAP_PCT` (default 2) and `PREMARKET_EARNINGS_DAYS` (default 2).

Dedup is `{date}:{user_id}:PORTFOLIO:premarket`, and it is recorded **only after
Discord accepts the post**, so a cron retry cannot double-post but a failed post
stays retryable. `/premarket-now` passes `force=True`, which bypasses both the
dedup and the holiday skip — someone clicking the button wants to see what the
scan sees.

### Frontend JS architecture (`templates/index.html`)

The frontend is a single HTML file with vanilla JS. Key globals:

- `_lastPortfolio` — latest holdings array; mutated in place by SSE ticks
- `_lastWatchlist` — latest watchlist array
- `_sse` — active `EventSource` instance with exponential-backoff reconnect (1 s → 30 s max)
- `_fast_prices` — (backend) per-ticker price payload cache; served immediately to new SSE clients

Price updates flow: `_price_worker` → SSE queue → `applyPriceUpdate()` → DOM patches + `renderCharts()` + `updateMobileSummary()`.

Mobile layout uses a bottom tab nav (Portfolio / Chat / Watchlist / Actions) with `switchTab()`. Desktop always shows all three columns.

### TTL cache in `app.py`

`_cached_fetch(key, fetch_fn)` wraps expensive calls to `get_portfolio_snapshot` and `get_watchlist_snapshot`, under keys of the form `portfolio:{user_id}` / `watchlist:{user_id}`:
- Fresh if age < 120 s
- Stale (serves old data + sets `X-Stale` response header) if age 120–600 s
- Re-raises if nothing cached yet

### AI chat

`chat_stream()` in `trading_bot.py` uses the Gemini REST API directly (not the SDK's streaming interface) to yield SSE chunks. The system prompt is trading-focused: short-term momentum, breakouts, catalyst plays. Chat history is passed from the browser on every request (stateless backend).

The `/chat` route logs both turns to `chat_messages` by parsing the SSE frames it forwards. The log is **write-only** — nothing reads it back into the prompt, so it never affects a response, and a logging failure is swallowed rather than breaking the stream.

### Screener

`screener.py` defines a `UNIVERSE` dict of ~400 tickers grouped by sector. `ai_suggest_watchlist()` scans all tickers with `get_stock_data()` in parallel threads, filters by technical criteria, then calls Gemini to rank and explain the top 5–6 picks. Results auto-replace the user's watchlist.

---

## Key environment variables

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | **Required.** Signs the session cookie. Unset = random key, so every restart logs everyone out |
| `SUPABASE_SERVICE_KEY` | **Required.** Service-role key; the only way to reach any table |
| `OWNER_EMAIL` / `OWNER_PASSWORD` | Used once by `POST /admin/bootstrap` to create the owner account |
| `ALLOW_REGISTRATION` | Truthy opens `/register`; default is closed |
| `CRON_SECRET` | Guards `/cron/*` and `/admin/bootstrap` |
| `GEMINI_API_KEY` | Google AI Studio key for Gemini 2.5 Flash |
| `DISCORD_WEBHOOK_URL` | Webhook that every alert and digest is posted to |
| `PREMARKET_GAP_PCT` | Overnight move that counts as a mover, default `2` |
| `PREMARKET_EARNINGS_DAYS` | Flag earnings this many days out, default `2` |
| `PORTFOLIO_JSON` | Legacy single-tenant fallback; only read when there is no user |
| `DATA_DIR` | Optional path for a persistent volume. Unset on Vercel — there is no persistent disk, so the local-file fallbacks are offline-dev only |
| `PORT` | Flask port, default `5000` |
