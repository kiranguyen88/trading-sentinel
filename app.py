import os
import json
import queue as _queue
import threading as _threading
import pandas as _pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_GMT7 = timezone(timedelta(hours=7))
from flask import Flask, render_template, request, Response, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from trading_bot import (
    chat_stream, get_portfolio_snapshot, get_watchlist_snapshot,
    run_daily_digest, load_portfolio, save_portfolio, send_alert, get_stock_data, get_market_news,
    get_market_breadth, load_journal, add_journal_entry, delete_journal_entry,
)
from screener import ai_suggest_watchlist

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Simple TTL cache — prevents hammering yfinance on every 60-second refresh
# ---------------------------------------------------------------------------
import time as _time

_cache: dict = {}          # key → {"data": ..., "ts": float}
_CACHE_TTL  = 120          # seconds before a fresh fetch is attempted
_CACHE_HARD = 600          # seconds before stale data is considered too old

def _cache_get(key):
    """Return (data, is_stale). data=None if nothing cached."""
    entry = _cache.get(key)
    if not entry:
        return None, False
    age = _time.time() - entry["ts"]
    return entry["data"], age > _CACHE_TTL   # stale = TTL expired but data exists

def _cache_set(key, data):
    _cache[key] = {"data": data, "ts": _time.time()}

def _cached_fetch(key, fetch_fn):
    """Try fetch_fn(); on error return stale cache if available."""
    data, stale = _cache_get(key)
    if data is not None and not stale:
        return data, False                   # fresh cache hit
    try:
        fresh = fetch_fn()
        _cache_set(key, fresh)
        return fresh, False
    except Exception as e:
        if data is not None:
            print(f"[Cache] {key} fetch failed ({e}), serving stale data")
            return data, True                # stale fallback
        raise                                # nothing cached — re-raise

# ---------------------------------------------------------------------------
# Real-time price streaming via SSE
# ---------------------------------------------------------------------------
import yfinance as _yf

_sse_lock       = _threading.Lock()
_sse_queues:list= []
_fast_prices:dict = {}      # ticker → price payload, updated every 15 s

def _push_prices(payload: str):
    """Broadcast SSE message to all connected clients; drop dead queues."""
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(payload)
            except _queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)

def _fetch_live_prices() -> list:
    """Batch-fetch latest price + day-change for portfolio holdings AND watchlist."""
    global _fast_prices
    portfolio  = load_portfolio()
    holdings   = portfolio.get("holdings", [])
    raw_wl     = portfolio.get("watchlist", [])
    wl_tickers = [w if isinstance(w, str) else w.get("ticker", "") for w in raw_wl]
    wl_tickers = [t for t in wl_tickers if t]

    holding_set  = {h["ticker"] for h in holdings}
    all_tickers  = list(holding_set | set(wl_tickers))
    if not all_tickers:
        return []

    try:
        raw = _yf.download(
            " ".join(all_tickers),
            period="5d", interval="1m",
            progress=False, auto_adjust=True,
        )
        # Normalise: single ticker → DataFrame with one column level
        closes = raw["Close"] if "Close" in raw.columns else raw.xs("Close", axis=1, level=1)
        if isinstance(closes, _pd.Series):
            closes = closes.to_frame(all_tickers[0])

        def _price_and_chg(t):
            try:
                s = closes[t].dropna() if t in closes.columns else closes.iloc[:, 0].dropna()
                if s.empty:
                    return None, None, None
                current    = float(s.iloc[-1])
                today      = s.index[-1].normalize()
                prev_s     = s[s.index.normalize() < today]
                prev_close = float(prev_s.iloc[-1]) if not prev_s.empty else current
                day_pct    = round((current - prev_close) / prev_close * 100, 2) if prev_close else 0
                return current, day_pct, prev_close
            except Exception as e:
                print(f"[LivePrice] {t}: {e}")
                return None, None, None

        results = []

        # ── Portfolio holdings (include PnL fields) ──
        for h in holdings:
            t = h["ticker"]
            current, day_pct, prev_close = _price_and_chg(t)
            if current is None:
                continue
            payload = {
                "type":             "portfolio",
                "ticker":           t,
                "current_price":    round(current, 2),
                "day_change_pct":   day_pct,
                "day_change_dollar":round(current - prev_close, 2),
                "unrealized_pnl":   round((current - h["avg_buy_price"]) * h["quantity"], 2),
                "pnl_pct":          round((current - h["avg_buy_price"]) / h["avg_buy_price"] * 100, 2),
            }
            results.append(payload)
            _fast_prices[t] = payload

        # ── Watchlist tickers (price + day change only) ──
        for t in wl_tickers:
            if t in holding_set:
                continue   # already handled above
            current, day_pct, prev_close = _price_and_chg(t)
            if current is None:
                continue
            payload = {
                "type":             "watchlist",
                "ticker":           t,
                "current_price":    round(current, 2),
                "day_change_pct":   day_pct,
                "day_change_dollar":round(current - prev_close, 2),
            }
            results.append(payload)
            _fast_prices[t] = payload

        return results
    except Exception as e:
        print(f"[LivePrice] batch error: {e}")
        return list(_fast_prices.values())   # serve stale on error

def _price_worker():
    import time
    while True:
        ok = False
        try:
            prices = _fetch_live_prices()
            if prices:
                _push_prices(json.dumps(prices))
                ok = True
        except Exception as e:
            print(f"[PriceWorker] {e}")
        time.sleep(15 if ok else 5)

_threading.Thread(target=_price_worker, daemon=True, name="price-worker").start()

# Track which alerts were already sent today so we don't spam
_sent_alerts: set = set()

def auto_scan_watchlist():
    """Scan current watchlist tickers and send technical signals via Discord."""
    try:
        snapshot = get_watchlist_snapshot()
        if not snapshot:
            print("[Watchlist Scan] No watchlist tickers.")
            return

        lines = ["📋 Trading Sentinel — Watchlist Scan\n"]
        for s in snapshot:
            if "error" in s:
                lines.append(f"⚠️ {s['ticker']}: {s['error']}")
                continue
            ticker = s["ticker"]
            price  = s.get("current_price", 0)
            chg    = s.get("day_change_pct", 0)
            rsi    = s.get("rsi_14", 0)
            macd   = s.get("macd", {})
            vol    = s.get("volume", {})

            signals = []
            if rsi > 70:   signals.append(f"RSI {rsi:.0f} OB")
            elif rsi < 30: signals.append(f"RSI {rsi:.0f} OS")
            if macd.get("bullish_crossover"):  signals.append("MACD ↑ cross")
            if macd.get("bearish_crossunder"): signals.append("MACD ↓ cross")
            if vol.get("ratio", 1) >= 2:       signals.append(f"Vol ×{vol['ratio']:.1f}")
            if abs(chg) >= 3:                  signals.append(f"{'▲' if chg>0 else '▼'}{abs(chg):.1f}%")

            sig_txt = " | ".join(signals) if signals else "No alerts"
            chg_sym = "+" if chg >= 0 else ""
            lines.append(f"*{ticker}* ${price:,.2f} ({chg_sym}{chg:.2f}%) — {sig_txt}")

        send_alert("\n".join(lines))
        print(f"[Watchlist Scan] Sent for {len(snapshot)} tickers.")
    except Exception as e:
        print(f"[Watchlist Scan] Error: {e}")


def _alert_key(ticker: str, alert_type: str) -> str:
    """One alert per ticker per type per calendar day."""
    day = datetime.now().strftime("%Y-%m-%d")
    return f"{day}:{ticker}:{alert_type}"


# ---------------------------------------------------------------------------
# Immediate warning monitor — runs every 15 min (24/7); self-gates on ET market hours 9:30–16:00
# ---------------------------------------------------------------------------

def check_warnings(force: bool = False):
    """Scan all holdings and fire Discord alerts for warning conditions."""
    if not force:
        now_et = datetime.now(_ET)
        hour, minute = now_et.hour, now_et.minute
        market_open  = (hour > 9) or (hour == 9 and minute >= 30)
        market_close = hour < 16
        if not (market_open and market_close):
            return

    snapshot = get_portfolio_snapshot()
    alerts = []

    for s in snapshot:
        if "error" in s:
            continue

        ticker = s["ticker"]
        rsi    = s.get("rsi_14", 50)
        chg    = s.get("day_change_pct", 0)
        macd   = s.get("macd", {})
        bb     = s.get("bollinger_bands", {})
        price  = s.get("current_price", 0)
        vol    = s.get("volume", {})

        def send_once(key_suffix: str, msg: str):
            key = _alert_key(ticker, key_suffix)
            if key not in _sent_alerts:
                _sent_alerts.add(key)
                alerts.append(msg)

        if rsi > 75:
            send_once("rsi_ob", f"🔴 *{ticker}* RSI={rsi:.0f} — OVERBOUGHT\nPrice: ${price:,.2f} | Consider reducing position")
        elif rsi < 25:
            send_once("rsi_os", f"🟢 *{ticker}* RSI={rsi:.0f} — OVERSOLD\nPrice: ${price:,.2f} | Potential entry opportunity")

        if macd.get("bearish_crossunder"):
            send_once("macd_bear", f"🔴 *{ticker}* MACD Bearish Crossover\nPrice: ${price:,.2f} | Momentum turning DOWN — watch closely")

        if macd.get("bullish_crossover"):
            send_once("macd_bull", f"🟢 *{ticker}* MACD Bullish Crossover\nPrice: ${price:,.2f} | Momentum turning UP — potential buy signal")

        if chg <= -3:
            send_once("drop3", f"⚠️ *{ticker}* down {chg:.1f}% today\nPrice: ${price:,.2f} | Review your stop-loss")

        if chg <= -5:
            send_once("drop5", f"🚨 *{ticker}* down {chg:.1f}% — SHARP DROP\nPrice: ${price:,.2f} | Consider cutting losses")

        if chg >= 5:
            send_once("surge5", f"🚀 *{ticker}* up +{chg:.1f}% today\nPrice: ${price:,.2f} | Consider taking partial profit")

        if bb and price >= bb.get("upper", float("inf")) * 0.99:
            send_once("bb_upper", f"🔴 *{ticker}* at Bollinger Upper Band\nPrice: ${price:,.2f} | Overbought zone — possible reversal")

        if bb and price <= bb.get("lower", 0) * 1.01:
            send_once("bb_lower", f"🟢 *{ticker}* at Bollinger Lower Band\nPrice: ${price:,.2f} | Oversold zone — possible support/entry")

        if vol.get("ratio", 1) >= 2.5:
            send_once("vol_spike", f"📊 *{ticker}* Volume Spike ×{vol['ratio']:.1f}\nPrice: ${price:,.2f} | Unusual activity — check for news")

    for msg in alerts:
        send_alert(msg)

    if alerts:
        print(f"[Monitor] {len(alerts)} alert(s) sent at {datetime.now(_GMT7).strftime('%H:%M GMT+7')}")


# ---------------------------------------------------------------------------
# Scheduler jobs
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")

# Daily digest — 6:00 PM VN time weekdays
scheduler.add_job(
    run_daily_digest, "cron",
    day_of_week="mon-fri", hour=18, minute=0,
    id="daily_digest"
)

# Warning monitor — every 1 hour (function self-gates on ET market hours)
scheduler.add_job(
    check_warnings, "interval",
    hours=1,
    id="warning_monitor"
)

# After-close summary — 3:05 AM VN time (≈ 4:05 PM ET)
scheduler.add_job(
    run_daily_digest, "cron",
    day_of_week="tue-sat", hour=3, minute=5,
    id="close_summary"
)

# Watchlist scan — 5:55 PM VN time weekdays (just before digest)
scheduler.add_job(
    auto_scan_watchlist, "cron",
    day_of_week="mon-fri", hour=17, minute=55,
    id="daily_watchlist"
)

scheduler.start()
print("Scheduler started (Asia/Ho_Chi_Minh):")
print("  Daily digest    -> 6:00 PM VN (Mon-Fri)")
print("  Warning monitor -> Every 1 hour (self-gates on ET market hours)")
print("  Close summary   -> 3:05 AM VN (Tue-Sat ≈ after US close)")
print("  Watchlist scan  -> 5:55 PM VN (Mon-Fri)")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/portfolio")
def portfolio():
    try:
        data, stale = _cached_fetch("portfolio", get_portfolio_snapshot)
        resp = jsonify(data)
        if stale:
            resp.headers["X-Stale"] = "1"
        return resp
    except Exception as e:
        return jsonify([]), 503


@app.route("/api/stream/prices")
def stream_prices():
    """SSE endpoint — pushes live price updates every 15 s."""
    q = _queue.Queue(maxsize=10)
    with _sse_lock:
        _sse_queues.append(q)
    # Send cached snapshot immediately so the client doesn't wait 15 s
    if _fast_prices:
        q.put_nowait(json.dumps(list(_fast_prices.values())))

    def generate():
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except _queue.Empty:
                    yield ": heartbeat\n\n"   # keep connection alive through proxies
        finally:
            with _sse_lock:
                if q in _sse_queues:
                    _sse_queues.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/watchlist")
def watchlist():
    try:
        data, stale = _cached_fetch("watchlist", get_watchlist_snapshot)
        resp = jsonify(data)
        if stale:
            resp.headers["X-Stale"] = "1"
        return resp
    except Exception as e:
        return jsonify([]), 503


@app.route("/daily-digest", methods=["POST"])
def daily_digest():
    msg = run_daily_digest()
    return jsonify({"message": msg})


@app.route("/suggest-watchlist", methods=["POST"])
def suggest_watchlist():
    result = ai_suggest_watchlist(top_n_final=6)
    suggestions = result.get("suggestions", [])

    # Auto-update portfolio.json watchlist
    if suggestions:
        portfolio = load_portfolio()
        portfolio["watchlist"] = [s["ticker"] for s in suggestions]
        with open("portfolio.json", "w") as f:
            json.dump(portfolio, f, indent=2)

    return jsonify(result)


@app.route("/check-now", methods=["POST"])
def check_now():
    """Manually trigger an immediate warning check."""
    _sent_alerts.clear()   # clear cooldowns so all current warnings fire
    check_warnings(force=True)
    return jsonify({"message": "Warning check complete. Alerts sent to Discord if any found."})


@app.route("/portfolio-data")
def portfolio_data():
    return jsonify(load_portfolio())


@app.route("/portfolio-update", methods=["POST"])
def portfolio_update():
    data = request.json or {}
    portfolio = load_portfolio()
    portfolio["holdings"]         = data.get("holdings", portfolio.get("holdings", []))
    portfolio["watchlist"]        = data.get("watchlist", portfolio.get("watchlist", []))
    save_portfolio(portfolio)
    return jsonify({"ok": True})


@app.route("/chat", methods=["POST"])
def chat():
    body = request.json or {}
    user_message = body.get("message", "").strip()
    history = body.get("history", [])

    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    def generate():
        yield from chat_stream(user_message, history)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Market Breadth
# ---------------------------------------------------------------------------

@app.route("/market-breadth")
def market_breadth():
    return jsonify(get_market_breadth())


# ---------------------------------------------------------------------------
# Position Sizer
# ---------------------------------------------------------------------------

@app.route("/position-size", methods=["POST"])
def position_size():
    d       = request.json or {}
    entry   = float(d.get("entry",   0))
    stop    = float(d.get("stop",    0))
    target  = float(d.get("target",  0))
    account = float(d.get("account", 10000))
    risk_pct = float(d.get("risk_pct", 1.0))

    if entry <= 0 or stop <= 0 or entry == stop:
        return jsonify({"error": "Invalid entry or stop price"}), 400

    risk_per_share = abs(entry - stop)
    risk_amount    = account * risk_pct / 100
    shares         = max(1, int(risk_amount / risk_per_share))
    position_value = round(shares * entry, 2)

    result = {
        "shares":          shares,
        "position_value":  position_value,
        "risk_amount":     round(risk_amount, 2),
        "risk_per_share":  round(risk_per_share, 2),
        "position_pct":    round(position_value / account * 100, 1),
    }
    if target > 0 and target != entry:
        reward = abs(target - entry)
        result["risk_reward"]      = round(reward / risk_per_share, 2)
        result["potential_profit"] = round(reward * shares, 2)
    return jsonify(result)


# ---------------------------------------------------------------------------
# Trade Journal
# ---------------------------------------------------------------------------

@app.route("/journal")
def journal():
    return jsonify(load_journal())

@app.route("/journal/add", methods=["POST"])
def journal_add():
    entry = request.json or {}
    if not entry.get("ticker"):
        return jsonify({"error": "ticker required"}), 400
    return jsonify(add_journal_entry(entry))

@app.route("/journal/delete/<entry_id>", methods=["DELETE"])
def journal_delete(entry_id):
    ok = delete_journal_entry(entry_id)
    return jsonify({"ok": ok})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
