# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

```bash
# Run locally
python app.py          # starts Flask on http://localhost:5000

# Install dependencies
pip install -r requirements.txt

# Deploy — Railway auto-deploys on every push to main
git push origin main
```

There are no tests, no lint config, and no build step. The only runtime requirement is a valid `.env` (see `.env.example`).

---

## Architecture

This is a single-process Python Flask server with a pure-JS single-page frontend. There are three Python files and one HTML template — no framework beyond Flask.

### File responsibilities

| File | Role |
|---|---|
| `app.py` | Flask routes, SSE price stream, APScheduler jobs, TTL cache |
| `trading_bot.py` | Market data (yfinance), technical indicators, Gemini AI chat, WhatsApp alerts, portfolio/journal persistence |
| `screener.py` | 400+ stock universe, batch technical scan, Gemini ranking of best setups |
| `templates/index.html` | Entire frontend — CSS, HTML, vanilla JS (no build toolchain) |

### Data flow

```
Browser ──SSE──► /api/stream/prices  (15 s ticks, price-worker thread)
Browser ──GET──► /portfolio          (full snapshot with technicals, TTL-cached 120 s)
Browser ──GET──► /watchlist          (watchlist snapshot, TTL-cached 120 s)
Browser ──POST─► /chat               (streaming SSE, Gemini 2.5 Flash)
```

The SSE price stream (`_price_worker` thread in `app.py`) runs every 15 s, batches all portfolio + watchlist tickers via `yfinance.download()`, and broadcasts to every connected client via a per-client `queue.Queue`. New clients immediately receive the cached `_fast_prices` snapshot so they don't wait.

### Portfolio persistence — 3-tier priority

`load_portfolio()` in `trading_bot.py` checks in order:
1. Local file at `DATA_DIR/portfolio.json` (Railway persistent volume if `DATA_DIR` is set)
2. `PORTFOLIO_JSON` environment variable (set in Railway → Variables to survive redeploys)
3. Committed `portfolio.json` fallback

**Important:** `portfolio.json` in git is a default/seed only. Live data is stored in `PORTFOLIO_JSON` env var on Railway. When the user edits holdings or watchlist via the UI, the change is saved to the local path; the UI also offers "Copy JSON" to paste into the Railway Variable.

### Technical indicators

All indicators are computed in `get_stock_data()` (`trading_bot.py`):
- RSI(14), MACD(12,26,9), Bollinger Bands(20,2), MA20/MA50/MA200
- Volume ratio (today vs 20-day avg)
- 52-week high/low from `yfinance fast_info`

`get_portfolio_snapshot()` fetches all holdings in parallel using `ThreadPoolExecutor`. `get_watchlist_snapshot()` does the same for watchlist tickers.

### Scheduled jobs (APScheduler, Asia/Ho_Chi_Minh timezone)

| Schedule | Job |
|---|---|
| 6:00 PM VN Mon–Fri | `run_daily_digest()` → WhatsApp |
| 3:05 AM VN Tue–Sat | `run_daily_digest()` → WhatsApp (≈ after US close) |
| Every 1 hour | `check_warnings()` — self-gates on ET market hours 9:30–16:00 |
| 5:55 PM VN Mon–Fri | `auto_scan_watchlist()` → WhatsApp |

Alert deduplication: `_sent_alerts` set uses `{date}:{ticker}:{alert_type}` keys; one alert per ticker per type per calendar day. `/check-now` clears this set before running.

### Frontend JS architecture (`templates/index.html`)

The frontend is a single HTML file with vanilla JS. Key globals:

- `_lastPortfolio` — latest holdings array; mutated in place by SSE ticks
- `_lastWatchlist` — latest watchlist array
- `_sse` — active `EventSource` instance with exponential-backoff reconnect (1 s → 30 s max)
- `_fast_prices` — (backend) per-ticker price payload cache; served immediately to new SSE clients

Price updates flow: `_price_worker` → SSE queue → `applyPriceUpdate()` → DOM patches + `renderCharts()` + `updateMobileSummary()`.

Mobile layout uses a bottom tab nav (Portfolio / Chat / Watchlist / Actions) with `switchTab()`. Desktop always shows all three columns.

### TTL cache in `app.py`

`_cached_fetch(key, fetch_fn)` wraps expensive calls to `get_portfolio_snapshot` and `get_watchlist_snapshot`:
- Fresh if age < 120 s
- Stale (serves old data + sets `X-Stale` response header) if age 120–600 s
- Re-raises if nothing cached yet

### AI chat

`chat_stream()` in `trading_bot.py` uses the Gemini REST API directly (not the SDK's streaming interface) to yield SSE chunks. The system prompt is trading-focused: short-term momentum, breakouts, catalyst plays. Chat history is passed from the browser on every request (stateless backend).

### Screener

`screener.py` defines a `UNIVERSE` dict of ~400 tickers grouped by sector. `ai_suggest_watchlist()` scans all tickers with `get_stock_data()` in parallel threads, filters by technical criteria, then calls Gemini to rank and explain the top 5–6 picks. Results auto-replace the user's watchlist.

---

## Key environment variables

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Google AI Studio key for Gemini 2.5 Flash |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | WhatsApp alerts via Twilio sandbox |
| `TWILIO_WHATSAPP_FROM` | Sender number, default `whatsapp:+14155238886` |
| `PORTFOLIO_JSON` | JSON blob of portfolio; set in Railway to persist across redeploys |
| `DATA_DIR` | Optional path for persistent volume (Railway) |
| `PORT` | Flask port, default `5000` |
