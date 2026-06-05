import os
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_GMT7 = timezone(timedelta(hours=7))
from flask import Flask, render_template, request, Response, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from trading_bot import (
    chat_stream, get_portfolio_snapshot, get_watchlist_snapshot,
    run_daily_digest, load_portfolio, send_whatsapp, get_stock_data, get_market_news,
    get_market_breadth, load_journal, add_journal_entry, delete_journal_entry,
)
from screener import ai_suggest_watchlist

load_dotenv()

app = Flask(__name__)

# Track which alerts were already sent today so we don't spam
_sent_alerts: set = set()

def auto_scan_watchlist():
    """Scan current watchlist tickers and send technical signals to WhatsApp."""
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

        send_whatsapp("\n".join(lines))
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

def check_warnings():
    """Scan all holdings and fire WhatsApp alerts for warning conditions."""
    now_et = datetime.now(_ET)   # always ET regardless of scheduler timezone
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
    snapshot = get_portfolio_snapshot()
    return jsonify(snapshot)


@app.route("/watchlist")
def watchlist():
    try:
        snapshot = get_watchlist_snapshot()
        return jsonify(snapshot)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
