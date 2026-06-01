# 📡 Trading Sentinel

An AI-powered US stock portfolio monitor with real-time technical analysis, Gemini AI chat, automated WhatsApp alerts, and a live web dashboard.

---

## Features

- **Live Dashboard** — Portfolio and watchlist with real-time prices, RSI, MACD, Bollinger Bands, P&L
- **AI Chat** — Ask anything about your portfolio using Google Gemini 2.5 Flash
- **WhatsApp Alerts** — Automatic alerts for RSI extremes, MACD crossovers, price drops/surges, volume spikes
- **Auto Screener** — Scans 200+ US stocks across 13 sectors, AI picks the best 5–6 mid-term setups daily
- **Scheduled Jobs** — Daily digest, warning monitor every 15 min, after-close summary
- **Charts** — Portfolio allocation donut, P&L bar chart, day change chart
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

## Automated Schedule (GMT+7)

| Time | Job |
|---|---|
| 8:00 PM Mon–Fri | AI screener — scan 200+ stocks, update watchlist, send WhatsApp |
| 8:30 PM Mon–Fri | Morning daily digest to WhatsApp |
| Every 15 min, 9:30 PM–4:00 AM | Warning monitor — scan holdings for alerts |
| 4:05 AM Mon–Fri | After-close summary to WhatsApp |

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

## Stock Universe (200+ tickers)

Covers 13 sectors: Tech, AI/Semis, Finance, Healthcare, Energy, Consumer/Retail, Food/Bev, Defense/Aero, Industrial, Real Estate/REIT, Utilities, Materials, ETFs.

---

## License

MIT
