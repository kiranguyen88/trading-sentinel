import os
import json
from datetime import datetime, timezone, timedelta

_GMT7 = timezone(timedelta(hours=7))
from flask import Flask, render_template, request, Response, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from trading_bot import (
    chat_stream, get_portfolio_snapshot, get_watchlist_snapshot,
    run_daily_digest, load_portfolio, send_whatsapp, get_stock_data, get_market_news
)
from screener import ai_suggest_watchlist

load_dotenv()

app = Flask(__name__)

# Track which alerts were already sent today so we don't spam
_sent_alerts: set = set()

def auto_update_watchlist():
    """Weekly: run screener, update portfolio.json watchlist, notify via WhatsApp."""
    try:
        print("[Screener] Running weekly auto-suggest...")
        result = ai_suggest_watchlist(top_n_final=6)
        suggestions = result.get("suggestions", [])
        if not suggestions:
            return

        new_tickers = [s["ticker"] for s in suggestions]

        # Update portfolio.json
        portfolio = load_portfolio()
        portfolio["watchlist"] = new_tickers
        with open("portfolio.json", "w") as f:
            json.dump(portfolio, f, indent=2)

        # Send WhatsApp summary
        lines = ["Trading Sentinel — Weekly Watchlist Update\n"]
        for s in suggestions:
            lines.append(
                f"{s['ticker']} ({s['sector']}) | Entry: {s['entry_range']} | "
                f"Target: {s['target']} | Stop: {s['stop_loss']} | {s['horizon']}\n"
                f"  {s['summary']}"
            )
        note = result.get("market_note", "")
        if note:
            lines.append(f"\nMarket: {note}")
        send_whatsapp("\n".join(lines))
        print(f"[Screener] Watchlist updated: {new_tickers}")
    except Exception as e:
        print(f"[Screener] Error: {e}")


def _alert_key(ticker: str, alert_type: str) -> str:
    """One alert per ticker per type per calendar day."""
    day = datetime.now().strftime("%Y-%m-%d")
    return f"{day}:{ticker}:{alert_type}"


# ---------------------------------------------------------------------------
# Immediate warning monitor — runs every 15 min on weekdays 9:30–16:00 ET
# ---------------------------------------------------------------------------

def check_warnings():
    """Scan all holdings and fire WhatsApp alerts for warning conditions."""
    now_et = datetime.now()  # APScheduler uses America/New_York timezone
    hour, minute = now_et.hour, now_et.minute

    # Only during US market hours 9:30–16:00
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
        send_whatsapp(msg)

    if alerts:
        print(f"[Monitor] {len(alerts)} alert(s) sent at {datetime.now(_GMT7).strftime('%H:%M GMT+7')}")


# ---------------------------------------------------------------------------
# Scheduler jobs
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone="America/New_York")

# Daily digest — 8:30 AM ET weekdays (before market open)
scheduler.add_job(
    run_daily_digest, "cron",
    day_of_week="mon-fri", hour=8, minute=30,
    id="daily_digest"
)

# Warning monitor — every 15 min during market hours, weekdays
scheduler.add_job(
    check_warnings, "cron",
    day_of_week="mon-fri",
    hour="9-15", minute="*/15",
    id="warning_monitor"
)

# After-close summary — 4:05 PM ET weekdays
scheduler.add_job(
    run_daily_digest, "cron",
    day_of_week="mon-fri", hour=16, minute=5,
    id="close_summary"
)

# Daily watchlist auto-suggest — 8:00 AM ET every weekday
scheduler.add_job(
    auto_update_watchlist, "cron",
    day_of_week="mon-fri", hour=8, minute=0,
    id="daily_watchlist"
)

scheduler.start()
print("Scheduler started:")
print("  Daily digest    -> 8:30 AM ET (Mon-Fri)")
print("  Warning monitor -> Every 15 min, 9:30-16:00 ET (Mon-Fri)")
print("  Close summary   -> 4:05 PM ET (Mon-Fri)")
print("  Watchlist scan  -> 8:00 AM ET (Mon-Fri, auto-update)")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/portfolio")
def portfolio():
    snapshot = get_portfolio_snapshot()
    return jsonify(snapshot)


@app.route("/watchlist")
def watchlist():
    snapshot = get_watchlist_snapshot()
    return jsonify(snapshot)


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
    check_warnings()
    return jsonify({"message": "Warning check complete. Alerts sent to WhatsApp if any found."})


@app.route("/portfolio-data")
def portfolio_data():
    return jsonify(load_portfolio())


@app.route("/portfolio-update", methods=["POST"])
def portfolio_update():
    data = request.json or {}
    portfolio = load_portfolio()
    portfolio["holdings"]         = data.get("holdings", portfolio.get("holdings", []))
    portfolio["watchlist"]        = data.get("watchlist", portfolio.get("watchlist", []))
    portfolio["whatsapp_number"]  = data.get("whatsapp_number", portfolio.get("whatsapp_number", ""))
    portfolio["whatsapp_numbers"] = data.get("whatsapp_numbers", portfolio.get("whatsapp_numbers", []))
    with open("portfolio.json", "w") as f:
        json.dump(portfolio, f, indent=2)
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
