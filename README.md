# 📡 Trading Sentinel

An AI-powered US stock portfolio monitor with real-time technical analysis, Gemini AI chat, automated WhatsApp alerts, and a live web dashboard.

---

## Features

- **Live Dashboard** — Portfolio and watchlist with real-time prices, RSI, MACD, Bollinger Bands, P&L
- **AI Chat** — Ask anything about your portfolio using Google Gemini 2.5 Flash (short-term trading focus: breakouts, momentum, catalyst plays)
- **WhatsApp Alerts** — Automatic alerts for RSI extremes, MACD crossovers, price drops/surges, volume spikes
- **Auto Screener** — Scans 400+ US stocks across 20 sectors, AI picks the best 5–6 mid-term setups daily
- **Scheduled Jobs** — Daily digest at 6 PM VN, warning monitor every 15 min, after-close summary
- **Mobile Responsive** — Bottom tab navigation for phone use

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask |
| AI | Google Gemini 2.5 Flash (google-genai SDK) |
| Market Data | yfinance (Yahoo Finance) |
| Alerts | Twilio WhatsApp API |
| Scheduler | APScheduler |
| Frontend | HTML/CSS/JS, Chart.js, Marked.js |
| Deployment | Railway |

---

## Project Structure

```
Trading/
├── app.py              # Flask server, routes, scheduler
├── trading_bot.py      # Core engine: data, AI chat, alerts, digest
├── screener.py         # 200+ stock screener + AI watchlist suggestions
├── portfolio.json      # Holdings, watchlist, WhatsApp numbers
├── requirements.txt    # Python dependencies
├── .env                # API keys (never commit)
└── templates/
    └── index.html      # Single-page frontend
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/kiranguyen88/trading-sentinel.git
cd trading-sentinel
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create `.env` file

```env
GEMINI_API_KEY=your_google_ai_studio_key
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
PORT=5000
```

### 4. Configure your portfolio

Edit `portfolio.json`:

```json
{
  "holdings": [
    {"ticker": "AAPL", "quantity": 10, "avg_buy_price": 175.0},
    {"ticker": "NVDA", "quantity": 5,  "avg_buy_price": 800.0}
  ],
  "watchlist": ["META", "AMD", "PLTR"],
  "whatsapp_number": "whatsapp:+1234567890",
  "whatsapp_numbers": ["whatsapp:+1234567890"]
}
```

### 5. Run locally

```bash
python app.py
```

Open `http://localhost:5000` in your browser.

---

## Deploy to Railway

1. Push code to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables in Railway → Variables tab
4. Railway auto-deploys on every `git push`

Your app will be live at `https://your-app.up.railway.app`

---

## API Keys

### Google Gemini
1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Create API key
3. Enable billing for paid tier (free tier = 50 req/day)

### Twilio WhatsApp
1. Go to [console.twilio.com](https://console.twilio.com)
2. Messaging → Try it out → Send a WhatsApp message
3. Join the sandbox by sending `join <keyword>` to `+1 415 523 8886`
4. Note: sandbox requires re-joining every 72 hours

---

## Automated Schedule (GMT+7 / Vietnam Time)

| Time (VN) | Job |
|---|---|
| 5:55 PM Mon–Fri | AI screener — scan 400+ stocks, update watchlist, send WhatsApp |
| 6:00 PM Mon–Fri | Daily digest to WhatsApp |
| Every 15 min (all day) | Warning monitor — scans holdings, fires only during US market hours (9:30–16:00 ET) |
| 3:05 AM Tue–Sat | After-close summary to WhatsApp (≈ 4:05 PM ET) |

---

## WhatsApp Alert Conditions

| Condition | Alert |
|---|---|
| RSI > 75 | Overbought — consider reducing |
| RSI < 25 | Oversold — potential entry |
| MACD bearish crossover | Momentum turning down |
| MACD bullish crossover | Momentum turning up |
| Day drop >= 3% | Review stop-loss |
| Day drop >= 5% | Sharp drop — consider cutting |
| Day surge >= 5% | Consider taking profit |
| Price at Bollinger Upper | Overbought zone |
| Price at Bollinger Lower | Oversold / support zone |
| Volume spike >= 2.5x avg | Unusual activity |

Each alert fires once per ticker per type per day.

---

## Stock Universe (400+ tickers)

Covers 20 sectors:

| Sector | Examples |
|---|---|
| Tech MegaCap | NVDA, MSFT, AAPL, META, GOOGL, AMZN |
| Tech Growth | PLTR, SNOW, CRWD, DDOG, NET, SHOP |
| Tech Infrastructure | CSCO, ANET, PSTG, SPLK, DT |
| AI / Semis | ARM, AMAT, MU, QCOM, MRVL, ASML |
| Finance Banks | JPM, GS, BAC, MS, C, WFC |
| Finance Markets | V, MA, COIN, PYPL, BLK, CME |
| Finance Insurance | AFL, PRU, PGR, TRV, CB |
| Healthcare Pharma | LLY, ABBV, MRNA, VRTX, REGN |
| Healthcare Devices | ISRG, DXCM, TMO, SYK, MDT |
| Healthcare Services | UNH, CVS, HUM, CI, HCA |
| Energy Oil | XOM, CVX, COP, OXY, HAL, SLB |
| Energy Gas | LNG, EQT, AR, WMB, KMI |
| Energy Clean | ENPH, FSLR, NEE, PLUG, CHPT |
| Consumer Discretionary | TSLA, NKE, COST, LULU, CROX |
| Consumer Auto / EV | RIVN, LCID, NIO, LI, XPEV |
| Food & Bev | MCD, CMG, SBUX, KO, PEP, MNST |
| Defense / Aero | LMT, RTX, NOC, BA, AXON |
| Industrial | CAT, DE, HON, ROK, ODFL |
| Real Estate / REIT | AMT, EQIX, PLD, SPG, PSA |
| Utilities | NEE, DUK, VST, CEG, NRG |
| Materials | LIN, FCX, NEM, ALB, MP |
| ETFs | QQQ, SPY, SOXX, ARKK, GLD |

---

## License

MIT
