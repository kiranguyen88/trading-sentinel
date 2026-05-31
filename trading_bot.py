import os
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import yfinance as yf
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient

load_dotenv()

# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-2.0-flash"
_GEMINI_BASE   = "https://generativelanguage.googleapis.com/v1/models"

# Keep client for any legacy references
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN"),
)
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")


# ---------------------------------------------------------------------------
# Portfolio helpers
# ---------------------------------------------------------------------------

def load_portfolio() -> dict:
    with open("portfolio.json") as f:
        return json.load(f)

def get_whatsapp_numbers() -> list[str]:
    p = load_portfolio()
    # Support both old single number and new list
    numbers = p.get("whatsapp_numbers", [])
    if not numbers:
        single = p.get("whatsapp_number", "")
        numbers = [single] if single else []
    return [n for n in numbers if n]


# ---------------------------------------------------------------------------
# Market data & indicators
# ---------------------------------------------------------------------------

def get_stock_data(ticker: str, period: str = "3mo") -> dict:
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period=period)
        if hist.empty:
            return {"error": f"No data for {ticker}"}

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

        return {
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
        return data

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch, h): i for i, h in enumerate(holdings)}
        results = [None] * len(holdings)
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def get_watchlist_snapshot() -> list:
    """Watchlist tickers — technical data only, fetched in parallel."""
    watchlist = load_portfolio().get("watchlist", [])
    if not watchlist:
        return []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(get_stock_data, t, "1mo"): i for i, t in enumerate(watchlist)}
        results = [None] * len(watchlist)
        for future in as_completed(futures):
            results[futures[future]] = future.result()
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
# WhatsApp
# ---------------------------------------------------------------------------

def send_whatsapp(message: str, to: str | None = None) -> bool:
    targets = [to] if to else get_whatsapp_numbers()
    if not targets:
        return False
    success = False
    for number in targets:
        try:
            twilio_client.messages.create(body=message, from_=TWILIO_FROM, to=number)
            success = True
        except Exception as e:
            print(f"[WhatsApp error] {number}: {e}")
    return success


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

def _send_whatsapp_alert_tool(message: str) -> dict:
    """Send an urgent alert to the trader's WhatsApp when warning conditions are detected."""
    return {"sent": send_whatsapp(message), "message": message}

GEMINI_TOOLS = [
    _get_stock_data_tool,
    _get_portfolio_snapshot_tool,
    _get_watchlist_snapshot_tool,
    _get_ticker_full_report_tool,
    _get_all_tickers_report_tool,
    _get_market_news_tool,
    _send_whatsapp_alert_tool,
]


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    portfolio = load_portfolio()
    today     = datetime.now().strftime("%A, %B %d, %Y %H:%M ET")

    holdings_txt = "\n".join(
        f"  - {h['ticker']}: {h['quantity']} shares @ avg ${h['avg_buy_price']}"
        for h in portfolio.get("holdings", [])
    )
    watchlist_txt = ", ".join(portfolio.get("watchlist", [])) or "none"

    return f"""You are **Trading Sentinel** — an elite AI trading assistant for a mid-term US stock market trader.

TODAY: {today}

## USER'S PORTFOLIO (currently holding)
{holdings_txt}

## WATCHLIST (researching as potential buys — no position yet)
{watchlist_txt}

## YOUR ROLE
1. **Technical Analysis** — RSI, MACD, Bollinger Bands, MAs, volume. Identify momentum, crossovers, breakouts.
2. **News & Sentiment** — Always include recent news when analyzing any ticker. Flag earnings dates, analyst upgrades/downgrades, product launches, macro events (Fed, CPI, jobs) that could move the stock.
3. **Watchlist Opportunities** — For watchlist tickers, give a BUY SETUP analysis: is there a good entry point? What price level to enter? What's the catalyst? What's the risk/reward?
4. **Warnings** — Alert immediately for holdings: RSI extremes, MACD crossovers, sharp price drops, Bollinger extremes.
5. **WhatsApp Alerts** — Send automatically when urgent conditions are detected.

## REPORT FORMAT
For every ticker report, always include ALL of these sections:

**[TICKER] — $price (day change%)**
- **Trend:** (bullish/bearish/neutral based on MAs and MACD)
- **Momentum:** RSI=X — interpretation
- **Key Levels:** support at $X, resistance at $X (use Bollinger Bands + MAs)
- **News:** bullet points of 2-3 most relevant recent headlines + brief impact assessment
- **Verdict:** BUY / HOLD / REDUCE / WATCH — with specific reasoning and time horizon
- **Entry/Stop:** suggested entry price range, stop-loss level, target price (for BUY/WATCH)

## TRADING STYLE
- Mid-term horizon: 1 week to 3 months
- Focus on swing trades, momentum, sector rotation, macro catalysts
- Risk-conscious: always include stop-loss levels

Respond in the same language the user writes in (English or Vietnamese).
Always use tools for live data — never guess prices or news from memory."""


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
    elif name in ("_send_whatsapp_alert_tool",    "send_whatsapp_alert"):
        raw = {"sent": send_whatsapp(args.get("message","")), "message": args.get("message","")}
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
    """Inner streaming logic — direct HTTP to Gemini v1 REST API."""
    system = build_system_prompt()

    # Build contents array
    contents = []
    for turn in history[-20:]:
        role = "model" if turn["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    contents.append({"role": "user", "parts": [{"text": user_message}]})

    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:streamGenerateContent?alt=sse&key={GEMINI_API_KEY}"
    # Prepend system prompt as first exchange so it works across all model versions
    full_contents = [
        {"role": "user",  "parts": [{"text": f"[SYSTEM INSTRUCTIONS]\n{system}"}]},
        {"role": "model", "parts": [{"text": "Understood. I am Trading Sentinel, ready to assist with your portfolio."}]},
    ] + contents
    body = {
        "contents": full_contents,
        "generationConfig": {"temperature": 0.3},
    }

    with requests.post(url, json=body, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            line = line.decode("utf-8") if isinstance(line, bytes) else line
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if raw in ("[DONE]", ""):
                continue
            try:
                chunk = json.loads(raw)
                candidates = chunk.get("candidates", [])
                if not candidates:
                    continue
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "text" in part and part["text"]:
                        yield f"data: {json.dumps({'type': 'text', 'text': part['text']})}\n\n"
            except Exception:
                continue

    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Daily digest (called by scheduler)
# ---------------------------------------------------------------------------

def run_daily_digest() -> str:
    """Quick rule-based digest for WhatsApp — no AI call, just raw signals."""
    snapshot  = get_portfolio_snapshot()
    watchlist = get_watchlist_snapshot()
    lines     = []

    now = datetime.now().strftime("%b %d %H:%M")
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
    send_whatsapp(msg)
    return msg
