"""
screener.py — Scans a universe of top US stocks, filters for strong
mid-term setups, then uses Gemini + news to rank and explain the best picks.
"""
import os
import json
import requests as _requests
import concurrent.futures
from trading_bot import get_stock_data, get_market_news, load_portfolio

GEMINI_MODEL   = "gemini-1.5-flash-latest"
_GEMINI_API_V1 = "https://generativelanguage.googleapis.com/v1/models"


def _gemini_generate(prompt: str, temperature: float = 0.2) -> str:
    """Call Gemini REST API directly on v1 (bypasses SDK version issues)."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    url = f"{_GEMINI_API_V1}/{GEMINI_MODEL}:generateContent?key={api_key}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    resp = _requests.post(url, json=body, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]

# ---------------------------------------------------------------------------
# Universe — 60+ liquid US stocks across sectors
# ---------------------------------------------------------------------------
UNIVERSE = {
    "Tech":        ["NVDA", "MSFT", "AAPL", "META", "GOOGL", "AMZN", "AMD", "AVGO", "ORCL", "CRM",
                    "PLTR", "SNOW", "NOW", "MSTR", "SMCI"],
    "AI/Semis":    ["ARM", "AMAT", "LRCX", "KLAC", "MU", "TSM", "INTC", "QCOM"],
    "Finance":     ["JPM", "GS", "V", "MA", "BRK-B", "BAC", "MS", "COIN"],
    "Healthcare":  ["LLY", "NVO", "UNH", "ABBV", "JNJ", "MRK", "ISRG"],
    "Energy":      ["XOM", "CVX", "SLB", "OXY"],
    "Consumer":    ["TSLA", "AMZN", "NKE", "SBUX", "HD", "MCD"],
    "Defense/Aero":["LMT", "RTX", "NOC", "GE"],
    "ETFs":        ["QQQ", "SPY", "SOXX", "XLK", "ARKK"],
}

ALL_TICKERS = [t for tickers in UNIVERSE.values() for t in tickers]


# ---------------------------------------------------------------------------
# Technical screener filters
# ---------------------------------------------------------------------------

def score_ticker(data: dict) -> float:
    """
    Score a ticker for mid-term swing trade potential (0–100).
    Higher = better setup.
    """
    if "error" in data:
        return -1

    score = 50.0
    rsi   = data.get("rsi_14", 50)
    macd  = data.get("macd", {})
    bb    = data.get("bollinger_bands", {})
    price = data.get("current_price", 0)
    vol   = data.get("volume", {})
    ma    = data.get("moving_averages", {})
    chg1m = data.get("change_1m_pct", 0) or 0
    chg3m = data.get("change_3m_pct", 0) or 0

    # RSI sweet spot for mid-term entry: 40–60 = momentum building without overbought
    if 40 <= rsi <= 60:   score += 15
    elif 35 <= rsi < 40:  score += 10   # slightly oversold — good entry
    elif 60 < rsi <= 68:  score += 5    # bullish but watch
    elif rsi > 75:        score -= 20   # overbought
    elif rsi < 25:        score -= 10   # too beaten down

    # MACD signals
    if macd.get("bullish_crossover"):    score += 20   # fresh buy signal
    elif macd.get("histogram", 0) > 0:  score += 8    # positive momentum
    if macd.get("bearish_crossunder"):   score -= 25

    # Price vs Bollinger mid — near or just above mid is healthy
    if bb:
        mid = bb.get("mid", price)
        pct_from_mid = (price - mid) / mid * 100 if mid else 0
        if -5 <= pct_from_mid <= 10:   score += 10
        elif pct_from_mid > 15:        score -= 10   # extended

    # Price vs MAs — above MA50 and MA200 is bullish structure
    if ma.get("ma50")  and price > ma["ma50"]:   score += 8
    if ma.get("ma200") and price > ma["ma200"]:  score += 7

    # Volume — above average = conviction
    if vol.get("ratio", 1) >= 1.5:  score += 5

    # Momentum: 1M and 3M performance
    if 3 <= chg1m <= 20:   score += 8   # steady uptrend
    if 5 <= chg3m <= 40:   score += 7
    if chg1m < -10:        score -= 10  # recent weakness

    return round(score, 1)


def run_screener(top_n: int = 12) -> list[dict]:
    """Fetch data for all tickers in parallel, score, return top_n."""
    portfolio   = load_portfolio()
    already_own = {h["ticker"] for h in portfolio.get("holdings", [])}

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(get_stock_data, ticker): ticker for ticker in ALL_TICKERS}
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                data  = future.result()
                score = score_ticker(data)
                if score >= 0:
                    # Find sector
                    sector = next((s for s, tickers in UNIVERSE.items() if ticker in tickers), "Other")
                    results.append({
                        "ticker": ticker,
                        "sector": sector,
                        "score":  score,
                        "data":   data,
                        "in_portfolio": ticker in already_own,
                    })
            except Exception:
                pass

    # Sort by score, exclude already-owned, return top_n
    results.sort(key=lambda x: x["score"], reverse=True)
    candidates = [r for r in results if not r["in_portfolio"]]
    return candidates[:top_n]


# ---------------------------------------------------------------------------
# AI ranking — Gemini picks the best 5 with full reasoning + news
# ---------------------------------------------------------------------------

def ai_suggest_watchlist(top_n_final: int = 6) -> dict:
    """
    1. Run technical screener to get top 12 candidates
    2. Fetch news for each
    3. Ask Gemini to pick the best 5-6 with reasoning
    4. Return structured result
    """
    print("[Screener] Scanning universe...")
    candidates = run_screener(top_n=12)

    if not candidates:
        return {"suggestions": [], "summary": "No strong setups found right now."}

    # Fetch news for top candidates
    print(f"[Screener] Fetching news for {len(candidates)} candidates...")
    enriched = []
    for c in candidates:
        news = get_market_news(c["ticker"], max_articles=3)
        enriched.append({
            "ticker":  c["ticker"],
            "sector":  c["sector"],
            "score":   c["score"],
            "technical": c["data"],
            "news":    news,
        })

    # Build prompt for Gemini
    data_txt = json.dumps(enriched, default=str, indent=2)

    prompt = f"""You are a mid-term US stock trading expert.

Below are the top technically-screened US stock candidates with live data and recent news.
Your job: pick the BEST {top_n_final} for a mid-term swing trade (1–8 weeks).

SELECTION CRITERIA:
- Strong technical setup (momentum, trend, key levels)
- Positive news catalyst or upcoming catalyst (earnings, product launch, sector tailwind)
- Good risk/reward for mid-term
- Avoid if: earnings in next 3 days (too risky), sector headwinds, or news is negative

For each pick, provide:
1. Why technically strong
2. Key news catalyst
3. Entry price range
4. Stop-loss level
5. Target price
6. Time horizon (weeks)
7. Risk level (Low/Medium/High)

Respond in this EXACT JSON format:
{{
  "suggestions": [
    {{
      "ticker": "XXXX",
      "sector": "...",
      "verdict": "BUY SETUP",
      "entry_range": "$XXX–$XXX",
      "stop_loss": "$XXX",
      "target": "$XXX",
      "horizon": "X–X weeks",
      "risk": "Low/Medium/High",
      "technical_reason": "...",
      "news_catalyst": "...",
      "summary": "One sentence why this is a good buy now"
    }}
  ],
  "market_note": "Brief overall market context note"
}}

CANDIDATE DATA:
{data_txt}"""

    try:
        raw = _gemini_generate(prompt, temperature=0.2)
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            return {"suggestions": [], "market_note": "Gemini quota exhausted. Enable billing at aistudio.google.com to use AI watchlist suggestions."}
        return {"suggestions": [], "market_note": f"AI error: {err[:200]}"}

    raw = raw.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        result = json.loads(raw)
    except Exception:
        result = {"suggestions": [], "summary": raw, "parse_error": True}

    return result
