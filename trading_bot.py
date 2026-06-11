import os
import json
import math
from datetime import datetime, timezone, timedelta

_GMT7 = timezone(timedelta(hours=7))
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# Browser-like session with per-request timeout — prevents Yahoo Finance IP blocking
# and stops threads from hanging indefinitely (which causes OOM on Railway).
class _TimeoutHTTPAdapter(HTTPAdapter):
    def send(self, *args, **kwargs):
        kwargs.setdefault("timeout", 10)
        return super().send(*args, **kwargs)

_yf_session = requests.Session()
_yf_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})
_yf_session.mount("https://", _TimeoutHTTPAdapter())
_yf_session.mount("http://", _TimeoutHTTPAdapter())


def _clean_floats(obj):
    """Recursively replace NaN/Inf floats with None so jsonify never emits invalid JSON."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_floats(i) for i in obj]
    return obj

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-2.5-flash"
_GEMINI_BASE   = "https://generativelanguage.googleapis.com/v1/models"

# Keep client for any legacy references
gemini_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={"api_version": "v1alpha"},
)


# ---------------------------------------------------------------------------
# Portfolio helpers
# ---------------------------------------------------------------------------

# Persist to DATA_DIR if set (Railway persistent volume), else local file.
# NOTE: on Vercel the filesystem is ephemeral (/tmp is per-instance and wiped on
# cold start), so file writes do NOT survive. When BLOB_READ_WRITE_TOKEN is set
# (auto-added by creating a Blob store in the Vercel dashboard), Vercel Blob is
# the durable source of truth and edits persist across instances/redeploys.
_default_data_dir = "/tmp" if os.getenv("VERCEL") else "."
_PORTFOLIO_PATH = os.path.join(os.getenv("DATA_DIR", _default_data_dir), "portfolio.json")

_BLOB_TOKEN = os.getenv("BLOB_READ_WRITE_TOKEN")
_BLOB_KEY = "portfolio.json"   # stable pathname in the Blob store (overwritten on save)


def _blob_load() -> dict | None:
    """Read the portfolio JSON from Vercel Blob. None if absent or unavailable."""
    import vercel_blob
    listing = vercel_blob.list({"prefix": _BLOB_KEY})
    for b in listing.get("blobs", []):
        if b.get("pathname") == _BLOB_KEY:
            url = b.get("downloadUrl") or b.get("url")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return json.loads(resp.content)
    return None


def _blob_save(data: dict) -> None:
    """Write the portfolio JSON to Vercel Blob, overwriting the existing key."""
    import vercel_blob
    payload = json.dumps(data, indent=2).encode("utf-8")
    vercel_blob.put(_BLOB_KEY, payload, {
        "addRandomSuffix": "false",
        "allowOverwrite": "true",
        "contentType": "application/json",
    })


def load_portfolio() -> dict:
    # 0. Vercel Blob — durable source of truth when configured.
    if _BLOB_TOKEN:
        try:
            data = _blob_load()
            if data:
                return data
        except Exception as e:
            print(f"[portfolio] Blob load failed, falling back: {e}")

    # 1. Persistent volume / local file written by save_portfolio
    if os.path.exists(_PORTFOLIO_PATH):
        try:
            with open(_PORTFOLIO_PATH) as f:
                data = json.load(f)
            # If this has real customisation (non-null watchlist entries), trust it over the env-var snapshot.
            wl = data.get("watchlist", [])
            has_custom = any(
                isinstance(w, dict) and (w.get("entry") or w.get("stop") or w.get("notes"))
                for w in wl
            )
            if has_custom:
                return data
        except Exception:
            pass

    # 2. PORTFOLIO_JSON env var — set this in Railway Variables to survive redeploys
    env_json = os.getenv("PORTFOLIO_JSON")
    if env_json:
        try:
            data = json.loads(env_json)
            save_portfolio(data)   # seeds the Blob (and local cache) from the env snapshot
            return data
        except Exception:
            pass

    # 3. Bundled defaults (portfolio.json committed in git)
    with open("portfolio.json") as f:
        data = json.load(f)
    if _BLOB_TOKEN:
        try:
            _blob_save(data)       # seed the Blob so the first edit has a baseline
        except Exception as e:
            print(f"[portfolio] Blob seed failed: {e}")
    return data


def save_portfolio(data: dict) -> None:
    # Durable store first (Vercel). Raise on failure so callers can report it
    # instead of silently losing the user's edit.
    blob_ok = False
    if _BLOB_TOKEN:
        _blob_save(data)
        blob_ok = True

    # Local file: source of truth off-Vercel (Railway volume / local dev),
    # ephemeral cache on Vercel. Best-effort only when the Blob save succeeded.
    try:
        dir_ = os.path.dirname(_PORTFOLIO_PATH)
        if dir_:
            os.makedirs(dir_, exist_ok=True)
        with open(_PORTFOLIO_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        if not blob_ok:
            raise   # nothing persisted anywhere → a real failure


# ---------------------------------------------------------------------------
# Market data & indicators
# ---------------------------------------------------------------------------

def get_stock_data(ticker: str, period: str = "3mo") -> dict:
    try:
        tk = yf.Ticker(ticker, session=_yf_session)
        hist = tk.history(period=period)
        hist = hist.dropna(subset=["Close"])   # drop incomplete trailing row (market closed)
        if hist.empty:
            return {"error": f"No data for {ticker}", "ticker": ticker}

        close = hist["Close"]
        current_price  = float(close.iloc[-1])
        prev_close     = float(close.iloc[-2]) if len(close) > 1 else current_price
        day_change_pct = (current_price - prev_close) / prev_close * 100

        # RSI(14)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = float((100 - 100 / (1 + gain / loss)).iloc[-1])

        # MACD(12,26,9)
        ema12  = close.ewm(span=12, adjust=False).mean()
        ema26  = close.ewm(span=26, adjust=False).mean()
        macd   = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        hist_m = macd - signal
        histogram      = float(hist_m.iloc[-1])
        prev_histogram = float(hist_m.iloc[-2]) if len(hist_m) > 1 else histogram

        # Bollinger Bands(20,2)
        ma20     = close.rolling(20).mean()
        std20    = close.rolling(20).std()
        bb_upper = float((ma20 + 2 * std20).iloc[-1])
        bb_lower = float((ma20 - 2 * std20).iloc[-1])
        bb_mid   = float(ma20.iloc[-1])

        ma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None
        ma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None

        avg_vol   = float(hist["Volume"].rolling(20).mean().iloc[-1])
        today_vol = float(hist["Volume"].iloc[-1])

        change_1m = round((current_price - float(close.iloc[-22])) / float(close.iloc[-22]) * 100, 2) if len(close) >= 22 else None
        change_3m = round((current_price - float(close.iloc[0]))  / float(close.iloc[0])  * 100, 2)

        try:
            info = tk.fast_info
            w52_high = float(info.year_high) if hasattr(info, "year_high") else None
            w52_low  = float(info.year_low)  if hasattr(info, "year_low")  else None
        except Exception:
            w52_high = w52_low = None

        result = {
            "ticker": ticker,
            "current_price": round(current_price, 4),
            "day_change_pct": round(day_change_pct, 2),
            "change_1m_pct": change_1m,
            "change_3m_pct": change_3m,
            "rsi_14": round(rsi, 2),
            "macd": {
                "macd":     round(float(macd.iloc[-1]), 4),
                "signal":   round(float(signal.iloc[-1]), 4),
                "histogram": round(histogram, 4),
                "bullish_crossover":  bool(prev_histogram < 0 and histogram > 0),
                "bearish_crossunder": bool(prev_histogram > 0 and histogram < 0),
            },
            "bollinger_bands": {
                "upper": round(bb_upper, 4),
                "mid":   round(bb_mid, 4),
                "lower": round(bb_lower, 4),
            },
            "moving_averages": {
                "ma20":  round(bb_mid, 4),
                "ma50":  round(ma50, 4)  if ma50  else None,
                "ma200": round(ma200, 4) if ma200 else None,
            },
            "volume": {
                "today":   int(today_vol),
                "avg_20d": int(avg_vol),
                "ratio":   round(today_vol / avg_vol if avg_vol else 1.0, 2),
            },
            "week_52": {"high": w52_high, "low": w52_low},
        }
        return _clean_floats(result)
    except Exception as e:
        return {"error": str(e), "ticker": ticker}


def get_portfolio_snapshot() -> list:
    """Holdings with full P&L — fetched in parallel."""
    holdings = load_portfolio().get("holdings", [])
    if not holdings:
        return []

    def fetch(h):
        data = get_stock_data(h["ticker"], period="1mo")
        if "error" not in data:
            data["quantity"]       = h["quantity"]
            data["avg_buy_price"]  = h["avg_buy_price"]
            data["position_value"] = round(data["current_price"] * h["quantity"], 2)
            data["unrealized_pnl"] = round((data["current_price"] - h["avg_buy_price"]) * h["quantity"], 2)
            data["pnl_pct"]        = round((data["current_price"] - h["avg_buy_price"]) / h["avg_buy_price"] * 100, 2)
        else:
            # yfinance failed — include static data so the UI shows something instead of loading forever
            data.update({"ticker": h["ticker"], "quantity": h["quantity"], "avg_buy_price": h["avg_buy_price"]})
        return data

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(fetch, h): i for i, h in enumerate(holdings)}
        results = [None] * len(holdings)
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def _wl_ticker(item) -> str:
    """Extract ticker string from a watchlist item (string or dict)."""
    return item["ticker"] if isinstance(item, dict) else item

def get_watchlist_snapshot() -> list:
    """Watchlist tickers — technical data only, fetched in parallel."""
    watchlist = load_portfolio().get("watchlist", [])
    if not watchlist:
        return []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(get_stock_data, _wl_ticker(item), "1mo"): (i, item)
                   for i, item in enumerate(watchlist)}
        results = [None] * len(watchlist)
        for future in as_completed(futures):
            idx, item = futures[future]
            data = future.result()
            if isinstance(item, dict):
                data["entry_target"] = item.get("entry")
                data["stop_target"]  = item.get("stop")
                data["notes"]        = item.get("notes", "")
            results[idx] = data
    # Fill any slots that never completed (shouldn't happen, but guard just in case)
    for i, item in enumerate(watchlist):
        if results[i] is None:
            results[i] = {"error": "Data unavailable", "ticker": _wl_ticker(item)}
    return results


def get_market_news(query: str, max_articles: int = 5) -> list:
    try:
        encoded = requests.utils.quote(query)
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={encoded}&region=US&lang=en-US"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        articles = []
        for item in root.iter("item"):
            articles.append({
                "title":       item.findtext("title", ""),
                "description": item.findtext("description", ""),
                "published":   item.findtext("pubDate", ""),
            })
            if len(articles) >= max_articles:
                break
        return articles
    except Exception as e:
        return [{"error": str(e)}]


def get_ticker_full_report(ticker: str) -> dict:
    """Technical data + news combined for one ticker."""
    tech = get_stock_data(ticker)
    news = get_market_news(ticker, max_articles=5)
    return {"technical": tech, "news": news}


def get_all_tickers_report() -> dict:
    """Full report for holdings + watchlist: technicals + news — fully parallel."""
    portfolio = load_portfolio()
    holdings  = portfolio.get("holdings", [])
    watchlist = portfolio.get("watchlist", [])

    def fetch_holding(h):
        tech = get_stock_data(h["ticker"], period="1mo")
        if "error" not in tech:
            tech["quantity"]       = h["quantity"]
            tech["avg_buy_price"]  = h["avg_buy_price"]
            tech["position_value"] = round(tech["current_price"] * h["quantity"], 2)
            tech["unrealized_pnl"] = round((tech["current_price"] - h["avg_buy_price"]) * h["quantity"], 2)
            tech["pnl_pct"]        = round((tech["current_price"] - h["avg_buy_price"]) / h["avg_buy_price"] * 100, 2)
        news = get_market_news(h["ticker"], max_articles=3)
        return {"technical": tech, "news": news}

    def fetch_watch(ticker):
        tech = get_stock_data(ticker, period="1mo")
        news = get_market_news(ticker, max_articles=3)
        return {"ticker": ticker, "technical": tech, "news": news}

    result = {"holdings": [None]*len(holdings), "watchlist": [None]*len(watchlist)}
    with ThreadPoolExecutor(max_workers=12) as ex:
        h_futures = {ex.submit(fetch_holding, h): i for i, h in enumerate(holdings)}
        w_futures = {ex.submit(fetch_watch, t):   i for i, t in enumerate(watchlist)}
        for f in as_completed(h_futures): result["holdings"][h_futures[f]] = f.result()
        for f in as_completed(w_futures): result["watchlist"][w_futures[f]] = f.result()

    return result


# ---------------------------------------------------------------------------
# Discord alerts
# ---------------------------------------------------------------------------

_DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")

def send_discord(message: str) -> bool:
    if not _DISCORD_WEBHOOK:
        return False
    # Convert *text* bold markers to Discord **text** format
    discord_msg = message.replace("*", "**")
    # Discord limit: 2000 chars per message
    chunks = [discord_msg[i:i+1990] for i in range(0, len(discord_msg), 1990)]
    try:
        for chunk in chunks:
            resp = requests.post(_DISCORD_WEBHOOK, json={"content": chunk}, timeout=10)
            resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[Discord error] {e}")
        return False


def send_alert(message: str) -> bool:
    return send_discord(message)


# ---------------------------------------------------------------------------
# Gemini native tool definitions
# ---------------------------------------------------------------------------

def _get_stock_data_tool(ticker: str, period: str = "1mo") -> dict:
    """Fetch live price and technical indicators (RSI, MACD, Bollinger Bands, MAs, volume) for a US stock ticker."""
    return get_stock_data(ticker, period)

def _get_portfolio_snapshot_tool() -> list:
    """Get live technical data and P&L for ALL holdings in the user's current portfolio."""
    return get_portfolio_snapshot()

def _get_watchlist_snapshot_tool() -> list:
    """Get live technical data for ALL tickers on the user's watchlist (potential buys)."""
    return get_watchlist_snapshot()

def _get_ticker_full_report_tool(ticker: str) -> dict:
    """Get combined technical analysis AND latest market news for a single ticker."""
    return get_ticker_full_report(ticker)

def _get_all_tickers_report_tool() -> dict:
    """Get full report (technicals + news) for ALL holdings AND watchlist at once."""
    return get_all_tickers_report()

def _get_market_news_tool(query: str, max_articles: int = 6) -> list:
    """Fetch latest news headlines from Yahoo Finance for a ticker or topic."""
    return get_market_news(query, max_articles)

def _send_discord_alert_tool(message: str) -> dict:
    """Send an urgent alert to the trader via Discord when warning conditions are detected."""
    return {"sent": send_alert(message), "message": message}

GEMINI_TOOLS = [
    _get_stock_data_tool,
    _get_portfolio_snapshot_tool,
    _get_watchlist_snapshot_tool,
    _get_ticker_full_report_tool,
    _get_all_tickers_report_tool,
    _get_market_news_tool,
    _send_discord_alert_tool,
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def get_market_breadth() -> dict:
    """Get market breadth: SPY/QQQ/IWM vs MAs, VIX, HYG, regime score."""
    symbols = {
        "SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Small Caps",
        "^VIX": "VIX Fear Index", "HYG": "HY Bonds", "TLT": "Long Bonds",
    }
    result = {}
    for sym, name in symbols.items():
        try:
            hist = yf.Ticker(sym).history(period="1y")
            if hist.empty:
                continue
            close  = hist["Close"]
            price  = float(close.iloc[-1])
            ma50   = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else None
            ma200  = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
            chg1d  = float((close.iloc[-1] - close.iloc[-2])  / close.iloc[-2]  * 100)
            chg1m  = float((close.iloc[-1] - close.iloc[-22]) / close.iloc[-22] * 100) if len(close) >= 22 else None
            result[sym] = {
                "name": name, "price": round(price, 2),
                "day_change_pct":  round(chg1d, 2),
                "change_1m_pct":   round(chg1m, 2) if chg1m else None,
                "above_ma50":      bool(price > ma50)  if ma50  else None,
                "above_ma200":     bool(price > ma200) if ma200 else None,
                "ma50":  round(ma50,  2) if ma50  else None,
                "ma200": round(ma200, 2) if ma200 else None,
            }
        except Exception:
            pass

    # Regime score
    score, checks = 0, 0
    for sym in ("SPY", "QQQ", "IWM"):
        if sym in result:
            if result[sym].get("above_ma200"): score += 2
            if result[sym].get("above_ma50"):  score += 1
            checks += 3

    vix = result.get("^VIX", {}).get("price", 20)
    if   vix < 15: score += 2; vix_label = "Very Low Fear"
    elif vix < 20: score += 1; vix_label = "Low Fear"
    elif vix < 25:              vix_label = "Moderate Fear"
    elif vix < 30: score -= 1; vix_label = "High Fear"
    else:          score -= 2; vix_label = "Extreme Fear"

    pct = max(0, min(100, int(score / (checks + 2) * 100))) if (checks + 2) > 0 else 50
    regime = "BULL" if pct >= 70 else "BEAR" if pct < 40 else "NEUTRAL"
    result["_regime"] = {"regime": regime, "score": pct, "vix": round(vix, 2), "vix_label": vix_label}
    return result


def build_system_prompt() -> str:
    portfolio = load_portfolio()
    today     = datetime.now(_GMT7).strftime("%A, %B %d, %Y %H:%M GMT+7")

    holdings_txt = "\n".join(
        f"  - {h['ticker']}: {h['quantity']} shares @ avg ${h['avg_buy_price']}"
        for h in portfolio.get("holdings", [])
    )
    watchlist_txt = ", ".join(_wl_ticker(t) for t in portfolio.get("watchlist", [])) or "none"

    return f"""You are **Trading Sentinel** — an elite AI trading assistant for a short-term US stock market trader.

TODAY: {today}

## USER'S PORTFOLIO (currently holding)
{holdings_txt}

## WATCHLIST (researching as potential buys — no position yet)
{watchlist_txt}

## YOUR ROLE
1. **Technical Analysis** — RSI, MACD, Bollinger Bands, MAs, volume. Focus on short-term momentum, intraday breakouts, gap fills, and high-volume moves.
2. **News & Sentiment** — Always include recent news. Flag same-day catalysts, earnings releases, analyst upgrades/downgrades, macro events (Fed, CPI, jobs). Pre/post-market moves matter.
3. **Market Regime Awareness** — Check whether broader market is in BULL/NEUTRAL/BEAR regime. In BEAR regime, stay mostly cash, only trade the strongest setups short-side or avoid.
4. **Position Sizing** — Use 1% account risk rule with tight stops. Risk Amount = Account × 1% / (Entry − Stop). Stops are tighter for short-term — typically 1–3% from entry.
5. **Watchlist Opportunities** — Give BUY SETUP with specific intraday or next-day entry trigger, tight stop, and realistic 1–5 day target.
6. **Warnings** — Alert immediately for: RSI extremes, MACD crossovers, sharp drops, Bollinger band extremes, volume spikes, gap-downs.
7. **Exit Discipline** — Short-term trades must have a defined exit: time stop (exit if no move in 2 days), profit target, and hard stop. Do not hold losers hoping for recovery.
8. **Trade Journal Awareness** — If user mentions past trades, analyze patterns and suggest improvements.

## TECHNICAL FRAMEWORKS

### Momentum Breakout (primary short-term setup)
Look for: Price consolidating near resistance → volume surge → breakout above key level.
Entry: On breakout candle or first pullback. Stop: Below breakout level or prior day low.
Target: 1:2 minimum R:R. Exit within 1–5 days if target not hit.

### MACD Crossover System
- Bullish: MACD crosses above signal + histogram turns positive → Buy signal
- Bearish: MACD crosses below signal → Exit / reduce immediately
- Confirm with RSI: RSI 45–60 on bullish cross = strong short-term setup

### Bollinger Band Mean Reversion (counter-trend)
- Price at lower band + RSI < 35 + volume spike = oversold bounce (1–3 day trade)
- Price at upper band + RSI > 70 = overbought, take profit or reduce
- BB squeeze → breakout incoming within 1–2 days

### Gap & Catalyst Plays
- Gap up >3% on news + volume >2× avg = continuation candidate (buy pullback to VWAP)
- Gap down >3% = avoid or short; re-evaluate thesis if holding

### Market Regime Rules
- BULL (SPY/QQQ above MA20 + MA50, VIX < 18): Full size, ride momentum
- NEUTRAL (mixed signals, VIX 18–25): 50–75% size, tighter stops, faster exits
- BEAR (below MA50, VIX > 25): 25% size max or flat; only high-conviction setups

## REPORT FORMAT
For every ticker, always include:

### [TICKER] — $price (day change%)
- **Trend:** bullish/bearish/neutral (MA20/MA50 + MACD)
- **Momentum:** RSI=X — overbought/oversold/neutral
- **Pattern:** breakout, gap fill, bounce, breakdown, consolidation
- **Key Levels:** Support $X | Resistance $X | Today's range $X–$X
- **News:** 1–2 most relevant same-day or recent headlines + market impact
- **Verdict:** BUY NOW / BUY ON DIP / HOLD / EXIT / AVOID
- **Trade Plan:** Entry $X | Stop $X (X%) | Target $X | R:R = X:1 | Hold: 1–5 days
- **Position Size:** e.g. "Risk 1% of $50k = $500 → X shares at $X stop"

## TRADING STYLE
- Short-term horizon: 1 day to 2 weeks
- Momentum trades, breakouts, gap plays, catalyst-driven moves
- Risk-first: tight stops, never more than 1–2% account risk per trade
- Exit if thesis is wrong — do not hold losers; time is money in short-term trading
- Take partial profits at 1:1 R:R, let remainder run to target

## POSITION SIZING FORMULA
shares = (account_size × risk_pct / 100) / (entry_price − stop_loss)
Default risk per trade: 1% of account. Max: 2%.

## STRICT BEHAVIOR RULES
- **Always execute the request directly** — never deflect, never suggest the user ask about something else.
- **Default scope is always the user's portfolio and watchlist** — if the user asks about "the market" or "opportunities" or "news", fetch data for their holdings and watchlist tickers, not generic market-wide commentary.
- **Never say** "I couldn't find...", "Perhaps you'd be interested in...", "You might want to ask about...", or any variant that avoids answering. Just answer using the available tickers.
- If a request is vague (e.g. "any news?"), interpret it as "news for my portfolio and watchlist" and use the tools to fetch it.
- Do not ask clarifying questions when the user's portfolio and watchlist provide enough scope to answer.

Respond in the same language the user writes in (English or Vietnamese).
Always use tools for live data — never guess prices or news from memory."""


# ---------------------------------------------------------------------------
# Trade Journal
# ---------------------------------------------------------------------------

JOURNAL_FILE = "journal.json"

def load_journal() -> list:
    if not os.path.exists(JOURNAL_FILE):
        return []
    with open(JOURNAL_FILE) as f:
        return json.load(f)

def save_journal(entries: list):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(entries, f, indent=2)

def add_journal_entry(entry: dict) -> dict:
    entries = load_journal()
    entry["id"]   = f"trade_{len(entries)+1}_{datetime.now(_GMT7).strftime('%Y%m%d%H%M%S')}"
    entry["date"] = entry.get("date") or datetime.now(_GMT7).strftime("%Y-%m-%d")
    entry["created_at"] = datetime.now(_GMT7).isoformat()
    entries.append(entry)
    save_journal(entries)
    return entry

def delete_journal_entry(entry_id: str) -> bool:
    entries = load_journal()
    new = [e for e in entries if e.get("id") != entry_id]
    if len(new) == len(entries):
        return False
    save_journal(new)
    return True


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _run_tool(name: str, args: dict) -> str:
    if   name in ("_get_stock_data_tool",        "get_stock_data"):
        raw = get_stock_data(args.get("ticker",""), args.get("period","1mo"))
    elif name in ("_get_portfolio_snapshot_tool", "get_portfolio_snapshot"):
        raw = get_portfolio_snapshot()
    elif name in ("_get_watchlist_snapshot_tool", "get_watchlist_snapshot"):
        raw = get_watchlist_snapshot()
    elif name in ("_get_ticker_full_report_tool", "get_ticker_full_report"):
        raw = get_ticker_full_report(args.get("ticker",""))
    elif name in ("_get_all_tickers_report_tool", "get_all_tickers_report"):
        raw = get_all_tickers_report()
    elif name in ("_get_market_news_tool",        "get_market_news"):
        raw = get_market_news(args.get("query",""), args.get("max_articles",6))
    elif name in ("_send_discord_alert_tool", "send_discord_alert"):
        raw = {"sent": send_alert(args.get("message", "")), "message": args.get("message", "")}
    else:
        raw = {"error": f"Unknown tool: {name}"}
    return json.dumps(raw, default=str)


# ---------------------------------------------------------------------------
# Streaming chat with Gemini tool use
# ---------------------------------------------------------------------------

def chat_stream(user_message: str, history: list[dict]):
    """Yields SSE strings. Runs Gemini tool-use loop with streaming."""
    try:
        yield from _chat_stream_inner(user_message, history)
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            msg = ("Gemini API quota exhausted. The free tier allows 50 requests/day. "
                   "To fix: go to aistudio.google.com, enable billing on your API key "
                   "(costs ~$0.01/day for normal usage). Quota resets at 3 PM Vietnam time.")
        else:
            msg = f"Error: {err[:300]}"
        yield f"data: {json.dumps({'type': 'text', 'text': msg})}\n\n"
        yield "data: [DONE]\n\n"


def _chat_stream_inner(user_message: str, history: list[dict]):
    """Inner logic — uses google-genai SDK with tool use loop for live data."""
    system = build_system_prompt()

    # Build contents array
    contents = []
    for turn in history[-20:]:
        role = "model" if turn["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    # Prepend system prompt as first exchange
    full_contents = [
        {"role": "user",  "parts": [{"text": f"[SYSTEM INSTRUCTIONS]\n{system}"}]},
        {"role": "model", "parts": [{"text": "Understood. I am Trading Sentinel, ready to assist with your portfolio."}]},
    ] + contents

    # Tool-use loop — keep calling until no more tool calls
    MAX_ROUNDS = 5
    for _ in range(MAX_ROUNDS):
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=full_contents,
            config={"temperature": 0.3, "tools": GEMINI_TOOLS},
        )

        # Check if model wants to call tools
        candidate = response.candidates[0] if response.candidates else None
        if not candidate:
            break

        tool_calls = [p for p in candidate.content.parts if hasattr(p, "function_call") and p.function_call]
        if not tool_calls:
            # No tool calls — final text response
            break

        # Execute each tool call and feed results back
        full_contents.append({"role": "model", "parts": [
            {"function_call": {"name": p.function_call.name, "args": dict(p.function_call.args)}}
            for p in tool_calls
        ]})

        tool_results = []
        for p in tool_calls:
            fc   = p.function_call
            name = fc.name
            args = dict(fc.args)
            result = _run_tool(name, args)
            tool_results.append({"function_response": {"name": name, "response": {"result": result}}})

        full_contents.append({"role": "user", "parts": tool_results})

    # Extract final text
    full_text = ""
    if response.candidates:
        for part in response.candidates[0].content.parts:
            if hasattr(part, "text") and part.text:
                full_text += part.text

    if not full_text:
        full_text = "Sorry, I could not generate a response. Please try again."

    # Stream in chunks
    chunk_size = 80
    for i in range(0, len(full_text), chunk_size):
        yield f"data: {json.dumps({'type': 'text', 'text': full_text[i:i+chunk_size]})}\n\n"

    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Daily digest (called by scheduler)
# ---------------------------------------------------------------------------

def run_daily_digest() -> str:
    """Quick rule-based digest — no AI call, just raw signals. Sends via Discord."""
    snapshot  = get_portfolio_snapshot()
    watchlist = get_watchlist_snapshot()
    lines     = []

    now = datetime.now(_GMT7).strftime("%b %d %H:%M GMT+7")
    lines.append(f"Trading Sentinel — {now}\n")

    lines.append("== HOLDINGS ==")
    for s in snapshot:
        if "error" in s:
            continue
        ticker = s["ticker"]
        price  = s["current_price"]
        chg    = s["day_change_pct"]
        rsi    = s["rsi_14"]
        pnl    = s.get("unrealized_pnl", 0)
        signals = []
        if rsi > 75:      signals.append(f"RSI={rsi:.0f} OVERBOUGHT")
        elif rsi < 25:    signals.append(f"RSI={rsi:.0f} OVERSOLD")
        if s["macd"].get("bearish_crossunder"): signals.append("MACD bear cross")
        if s["macd"].get("bullish_crossover"):  signals.append("MACD bull cross")
        if chg <= -3:     signals.append(f"DROP {chg:.1f}%")
        if chg >= 5:      signals.append(f"SURGE +{chg:.1f}%")
        flag  = "!" if signals else " "
        sig_txt = " | " + ", ".join(signals) if signals else ""
        lines.append(f"{flag} {ticker} ${price:,.2f} ({chg:+.1f}%) PnL:${pnl:+,.0f}{sig_txt}")

    lines.append("\n== WATCHLIST ==")
    for s in watchlist:
        if "error" in s:
            continue
        ticker = s["ticker"]
        price  = s["current_price"]
        chg    = s["day_change_pct"]
        rsi    = s["rsi_14"]
        signals = []
        if rsi < 35:      signals.append(f"RSI={rsi:.0f} low - possible entry")
        if s["macd"].get("bullish_crossover"): signals.append("MACD bull cross - momentum up")
        if chg >= 4:      signals.append(f"breakout +{chg:.1f}%")
        sig_txt = " >> " + ", ".join(signals) if signals else ""
        lines.append(f"  {ticker} ${price:,.2f} ({chg:+.1f}%){sig_txt}")

    msg = "\n".join(lines)
    send_alert(msg)
    return msg
