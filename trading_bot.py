import os
import json
import math
import re
import secrets
import time
from contextvars import ContextVar
from datetime import datetime, date, timezone, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

_GMT7 = timezone(timedelta(hours=7))
_ET   = ZoneInfo("America/New_York")
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
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

# Durable storage is Supabase (Postgres) — strongly consistent, survives deploys.
# On Vercel the filesystem is ephemeral (/tmp wiped on cold start), so the local
# file is only a dev/offline cache. The single source of truth is one jsonb row
# in the `app_state` table, read/written via the PostgREST API with the service
# key (RLS is on with no policies, so only the service key can access it).
_default_data_dir = "/tmp" if os.getenv("VERCEL") else "."
_PORTFOLIO_PATH = os.path.join(os.getenv("DATA_DIR", _default_data_dir), "portfolio.json")

# URL is not secret, so default it here — the only required env var is the
# secret service key (SUPABASE_SERVICE_KEY). Accepts a few common key names.
_SUPABASE_URL = (os.getenv("SUPABASE_URL") or "https://fcwpjsezrwnjxrqpuwvc.supabase.co").rstrip("/")
_SUPABASE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
                 or os.getenv("SUPABASE_KEY"))
_SUPABASE_OK = bool(_SUPABASE_URL and _SUPABASE_KEY)
_STATE_ID = "portfolio"   # legacy app_state row, kept for the one-time migration


# ---------------------------------------------------------------------------
# Current user
#
# Portfolio data is per-user, but load_portfolio() is called from nine places
# across three modules — including chat tools and the screener, which have no
# request context to pass a user id through. Threading a parameter through all
# of them would mean changing every tool signature, so the request's user is
# carried in a context variable instead and every existing caller stays as-is.
# ---------------------------------------------------------------------------
_current_user: ContextVar[str | None] = ContextVar("current_user", default=None)


def set_current_user(uid: str | None) -> None:
    _current_user.set(uid)


def current_user_id() -> str | None:
    return _current_user.get()


def _sb_headers() -> dict:
    return {
        "apikey": _SUPABASE_KEY,
        "authorization": f"Bearer {_SUPABASE_KEY}",
        "content-type": "application/json",
    }


def _sb_load(attempts: int = 3) -> dict | None:
    """Read the LEGACY global app_state row. Only bootstrap_owner() uses this
    now; live reads go through _sb_load_user(). Kept so the migration can copy
    the pre-accounts portfolio across."""
    last_err = None
    for i in range(attempts):
        try:
            r = requests.get(
                f"{_SUPABASE_URL}/rest/v1/app_state",
                params={"id": f"eq.{_STATE_ID}", "select": "data"},
                headers=_sb_headers(), timeout=15,
            )
            r.raise_for_status()
            rows = r.json()
            return rows[0]["data"] if rows else None
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(0.5 * (2 ** i))
    raise last_err


def _sb_load_user(uid: str, attempts: int = 3) -> dict | None:
    """Read one user's portfolio JSON from Supabase.

    Returns the stored dict, or None when the row genuinely does not exist.
    Raises if Supabase could not be reached after `attempts` tries — the
    caller MUST treat that as "unknown", never as "empty", because the two
    are indistinguishable to a naive caller and only one of them is safe to
    overwrite.
    """
    last_err = None
    for i in range(attempts):
        try:
            r = requests.get(
                f"{_SUPABASE_URL}/rest/v1/user_state",
                params={"user_id": f"eq.{uid}", "select": "data"},
                headers=_sb_headers(), timeout=15,
            )
            r.raise_for_status()
            rows = r.json()
            return rows[0]["data"] if rows else None
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(0.5 * (2 ** i))
    raise last_err


def _sb_save_user(uid: str, data: dict) -> None:
    """Upsert one user's portfolio JSON (one row per user, strongly consistent)."""
    r = requests.post(
        f"{_SUPABASE_URL}/rest/v1/user_state",
        params={"on_conflict": "user_id"},
        headers={**_sb_headers(), "prefer": "resolution=merge-duplicates"},
        json=[{"user_id": uid, "data": data,
               "updated_at": datetime.now(timezone.utc).isoformat()}],
        timeout=15,
    )
    r.raise_for_status()


# Last value successfully read out of Supabase in this process, per user. Used
# as the fallback when Supabase is briefly unreachable, so a blip cannot demote
# a warm instance all the way down to the bundled seed.
_last_good_portfolio: dict[str, dict] = {}

_EMPTY_PORTFOLIO = {"holdings": [], "watchlist": []}


def load_portfolio() -> dict:
    """Read the current user's portfolio. NEVER writes — a read path that can
    write is how the live portfolio got replaced by the bundled seed (a single
    Supabase timeout was enough). Seeding happens only in
    seed_portfolio_if_empty()."""
    uid = current_user_id()

    # 0. Supabase — durable source of truth when configured.
    if _SUPABASE_OK and uid:
        try:
            data = _sb_load_user(uid)
            if data:
                _last_good_portfolio[uid] = data
                return data
            # Reached Supabase and the row is genuinely absent: this is a new
            # account with nothing in it yet. Empty, not "fall back to someone
            # else's seed data".
            return dict(_EMPTY_PORTFOLIO)
        except Exception as e:
            # Unreachable != empty. Serve the best thing we already have and
            # leave the stored row completely untouched.
            print(f"[portfolio] Supabase unreachable, serving cached copy: {e}")
            if uid in _last_good_portfolio:
                return _last_good_portfolio[uid]
            return dict(_EMPTY_PORTFOLIO)

    # Below here we have no user (offline dev, or a script run outside a
    # request). Fall back to the old single-tenant chain, read-only as before.

    # 1. Local file (dev source of truth; ephemeral cache on Vercel)
    if os.path.exists(_PORTFOLIO_PATH):
        try:
            with open(_PORTFOLIO_PATH) as f:
                data = json.load(f)
            if data.get("holdings") or data.get("watchlist"):
                return data
        except Exception:
            pass

    # 2. PORTFOLIO_JSON env var (legacy)
    env_json = os.getenv("PORTFOLIO_JSON")
    if env_json:
        try:
            return json.loads(env_json)
        except Exception:
            pass

    # 3. Bundled defaults (portfolio.json committed in git) — served, not stored.
    with open("portfolio.json") as f:
        return json.load(f)


def seed_portfolio_if_empty() -> bool:
    """Write the bundled defaults for the current user only if their row is
    truly absent.

    Explicit and separate from load_portfolio() so no read path can ever write.
    Returns True if a seed was written. Any Supabase error aborts the seed.
    """
    uid = current_user_id()
    if not _SUPABASE_OK or not uid:
        return False
    if _sb_load_user(uid):     # raises if unreachable — abort rather than guess
        return False
    with open("portfolio.json") as f:
        data = json.load(f)
    _sb_save_user(uid, data)
    print(f"[portfolio] seeded empty user_state row for {uid} with bundled defaults")
    return True


def save_portfolio(data: dict) -> None:
    # Durable store first. Raise on failure so callers can report it instead of
    # silently losing the user's edit.
    uid = current_user_id()
    db_ok = False
    if _SUPABASE_OK and uid:
        _sb_save_user(uid, data)
        _last_good_portfolio[uid] = data
        db_ok = True

    # Local file: source of truth for local dev only. With accounts in play it
    # would be one shared file across users, so it is written only when there is
    # no user to scope the data to.
    if uid and db_ok:
        return
    try:
        dir_ = os.path.dirname(_PORTFOLIO_PATH)
        if dir_:
            os.makedirs(dir_, exist_ok=True)
        with open(_PORTFOLIO_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        if not db_ok:
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

        close = hist["Close"].dropna()
        if close.empty:
            return {"error": f"No price data for {ticker}", "ticker": ticker}
        current_price  = float(close.iloc[-1])
        prev_close     = float(close.iloc[-2]) if len(close) > 1 else current_price
        day_change_pct = (current_price - prev_close) / prev_close * 100

        # RSI(14)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi_raw = (100 - 100 / (1 + gain / loss)).iloc[-1]
        rsi   = float(rsi_raw) if not math.isnan(rsi_raw) else None

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
            "rsi_14": round(rsi, 2) if rsi is not None else None,
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
                "today":   int(today_vol) if today_vol and not math.isnan(today_vol) else 0,
                "avg_20d": int(avg_vol)   if avg_vol   and not math.isnan(avg_vol)   else 0,
                "ratio":   round(today_vol / avg_vol if avg_vol and not math.isnan(avg_vol) else 1.0, 2),
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

    def fetch(h, lot):
        data = get_stock_data(h["ticker"], period="1mo")
        data["lot"] = lot   # position in holdings[]; disambiguates duplicate tickers
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
        futures = {ex.submit(fetch, h, i): i for i, h in enumerate(holdings)}
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


# ---------------------------------------------------------------------------
# Pre-open data — gaps, earnings dates, index futures
# ---------------------------------------------------------------------------

def _close_frame(raw, tickers: list):
    """Pull the Close columns out of a yf.download frame.

    yfinance returns three different shapes depending on ticker count and
    grouping — field-major MultiIndex, ticker-major MultiIndex, or a plain
    Series for one ticker. All three collapse to a ticker-keyed DataFrame here.
    """
    closes = raw["Close"] if "Close" in raw.columns else raw.xs("Close", axis=1, level=1)
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(tickers[0])
    return closes


def get_premarket_snapshot(tickers: list) -> dict:
    """Pre-open quote per ticker: today's last extended-hours print measured
    against the previous *regular* session close.

    The two halves deliberately come from different feeds. The reference close
    is read from daily bars dated strictly before today, and the pre-open price
    from today's 1-minute extended-hours bars. Taking the reference from a daily
    bar dated today instead would compare this morning against itself — during
    premarket that row is a partial bar that already contains the gap.

    A ticker with no bar dated today gets ``pre_price: None`` rather than
    yesterday's number dressed up as this morning's. Market holidays, weekends
    and names that simply do not trade pre-open all land there.

    Run after 9:30 ET this still works — ``gap_pct`` is then the move since the
    last close, which is what the caller wants from a manual trigger anyway.
    """
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        return {}
    joined = " ".join(tickers)

    try:
        daily  = yf.download(joined, period="7d", interval="1d", prepost=False,
                             progress=False, auto_adjust=False, threads=True,
                             session=_yf_session)
        dclose = _close_frame(daily, tickers)
    except Exception as e:
        print(f"[Premarket] daily bars failed: {e}")
        return {}

    try:
        intra  = yf.download(joined, period="2d", interval="1m", prepost=True,
                             progress=False, auto_adjust=False, threads=True,
                             session=_yf_session)
        iclose = _close_frame(intra, tickers)
    except Exception as e:
        # No pre-open prints is a degraded scan, not a failed one — the earnings
        # and news legs still have something to say.
        print(f"[Premarket] 1m extended-hours bars failed: {e}")
        iclose = None

    today = datetime.now(_ET).date()
    out   = {}

    for t in tickers:
        prev_close = None
        try:
            if t in dclose.columns:
                ser   = dclose[t].dropna()
                prior = ser[[ts.date() < today for ts in ser.index]]
                if not prior.empty:
                    prev_close = float(prior.iloc[-1])
        except Exception as e:
            print(f"[Premarket] {t} prev close: {e}")

        pre_price, as_of = None, None
        try:
            if iclose is not None and t in iclose.columns:
                ser = iclose[t].dropna()
                if not ser.empty:
                    idx  = ser.index.tz_convert(_ET) if ser.index.tz is not None else ser.index
                    mask = [ts.date() == today for ts in idx]
                    if any(mask):
                        pre_price = float(ser[mask].iloc[-1])
                        as_of     = idx[mask][-1].strftime("%H:%M ET")
        except Exception as e:
            print(f"[Premarket] {t} pre-open print: {e}")

        gap = None
        if prev_close and pre_price:
            gap = round((pre_price - prev_close) / prev_close * 100, 2)

        out[t] = {"prev_close": prev_close, "pre_price": pre_price,
                  "gap_pct": gap, "as_of": as_of}
    return out


def get_index_futures() -> dict:
    """Overnight index futures and VIX — or {} when the numbers would be stale.

    Both legs come from daily bars: the partial row dated today is the live
    overnight price and the row before it is the prior settle. With no row for
    today the market is shut, and the pair would describe *yesterday* while
    reading as this morning — so nothing comes back rather than a wrong number.
    """
    labels = {"ES=F": "S&P fut", "NQ=F": "Nasdaq fut", "^VIX": "VIX"}
    syms   = list(labels)
    try:
        raw    = yf.download(" ".join(syms), period="7d", interval="1d", prepost=False,
                             progress=False, auto_adjust=False, threads=True,
                             session=_yf_session)
        closes = _close_frame(raw, syms)
    except Exception as e:
        print(f"[Premarket] futures failed: {e}")
        return {}

    today = datetime.now(_ET).date()
    out   = {}
    for sym in syms:
        try:
            if sym not in closes.columns:
                continue
            ser = closes[sym].dropna()
            if len(ser) < 2 or ser.index[-1].date() != today:
                continue
            last, prev = float(ser.iloc[-1]), float(ser.iloc[-2])
            if not prev:
                continue
            out[sym] = {"label": labels[sym], "price": round(last, 2),
                        "change_pct": round((last - prev) / prev * 100, 2)}
        except Exception as e:
            print(f"[Premarket] futures {sym}: {e}")
    return out


def get_earnings_dates(tickers: list) -> dict:
    """Next scheduled earnings date per ticker, fetched in parallel.

    Anything Yahoo will not answer for maps to None. Yahoo drops the calendar
    for plenty of names, and a missing earnings date is not worth failing the
    whole pre-open scan over.
    """
    def fetch(ticker):
        try:
            cal   = yf.Ticker(ticker, session=_yf_session).calendar or {}
            dates = cal.get("Earnings Date") or []
            if not isinstance(dates, (list, tuple)):
                dates = [dates]
            # Yahoo returns a date here, but has returned datetimes in the past
            # and callers subtract these from a date — which raises on a mix.
            dates = [d.date() if isinstance(d, datetime) else d for d in dates if d]
            dates = [d for d in dates if isinstance(d, date)]
            return min(dates) if dates else None
        except Exception:
            return None

    if not tickers:
        return {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        return dict(zip(tickers, ex.map(fetch, tickers)))


# Company names change about as often as tickers do, so one lookup per process
# is plenty — and .info is the slowest call in this file.
_name_cache: dict = {}

_CORP_SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|plc|ltd|limited|holdings?|"
    r"group|technologies|the)\b\.?", re.I)


def _company_terms(ticker: str) -> set:
    """Lowercase names a headline about `ticker` is likely to use.

    Best-effort: Yahoo rate-limits .info fairly readily, and an empty set just
    falls back to matching the symbol itself.
    """
    if ticker in _name_cache:
        return _name_cache[ticker]
    terms = set()
    try:
        info = yf.Ticker(ticker, session=_yf_session).info or {}
        for field in ("displayName", "shortName", "longName"):
            name = (info.get(field) or "").strip()
            if not name:
                continue
            name = _CORP_SUFFIX.sub(" ", name)
            name = re.sub(r"[^\w\s&.-]", " ", name).strip(" .-")
            name = re.sub(r"\s+", " ", name)
            if len(name) >= 3:
                terms.add(name.lower())
    except Exception:
        pass
    _name_cache[ticker] = terms
    return terms


def _is_about(title: str, ticker: str, terms: set) -> bool:
    """Is this headline actually about the ticker?

    Yahoo's per-ticker feed pads itself out with generic market commentary.
    Printed underneath a gap alert that padding reads as the *explanation* for
    the gap, which is worse than showing no headline at all — so a headline
    naming neither the symbol nor the company is dropped. The symbol match is
    case-sensitive so "KO" catches Coca-Cola without catching "ok".
    """
    if re.search(rf"\b{re.escape(ticker)}\b", title):
        return True
    low = title.lower()
    return any(term in low for term in terms)


def get_fresh_news(ticker: str, within_hours: float = 16, limit: int = 2) -> list:
    """Headlines about `ticker` published inside the last `within_hours`.

    Undated items are dropped rather than kept: in a pre-open brief an undated
    headline reads as breaking news when it may be weeks old.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    terms  = _company_terms(ticker)
    fresh  = []
    for art in get_market_news(ticker, max_articles=10):
        if "error" in art:
            continue
        try:
            when = parsedate_to_datetime(art.get("published") or "")
        except (TypeError, ValueError):
            continue
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        title = (art.get("title") or "").strip()
        if when >= cutoff and title and _is_about(title, ticker, terms):
            fresh.append({"title": title, "published": when.isoformat()})
            if len(fresh) >= limit:
                break
    return fresh


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


def serpapi_finance(ticker: str, exchange: str = "NASDAQ") -> dict:
    """Fetch a Google-Finance snapshot for cross-source verification.

    Independent source from yfinance — pairs well with /verify and /swing.
    Returns a trimmed dict (price, day range, 52w range, market cap, P/E,
    related news headlines, related stocks). Skips silently if SERPAPI_KEY
    is unset, so the bot still works without it.
    """
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key:
        return {"error": "SERPAPI_KEY not configured", "source": "serpapi"}

    q = f"{ticker.upper()}:{exchange.upper()}"
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"engine": "google_finance", "q": q, "api_key": key},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"error": f"SerpApi request failed: {e}", "source": "serpapi"}

    summary = data.get("summary") or {}
    kg = data.get("knowledge_graph") or {}
    return {
        "source": "serpapi.google_finance",
        "query": q,
        "price": summary.get("price") or kg.get("price"),
        "currency": summary.get("currency"),
        "extracted_price": summary.get("extracted_price"),
        "price_movement": summary.get("price_movement"),
        "exchange": summary.get("exchange") or summary.get("stock"),
        "about": kg.get("about"),
        "key_stats": kg.get("key_stats") or {},
        "related_news": [
            {"title": n.get("title"), "source": n.get("source"), "date": n.get("date"), "link": n.get("link")}
            for n in (data.get("news_results") or [])[:5]
        ],
        "related_stocks": [
            {"ticker": s.get("stock"), "name": s.get("name"), "price": s.get("price"), "movement": s.get("price_movement")}
            for s in (data.get("discover_more") or data.get("related_stocks") or [])[:6]
        ],
    }


def _serpapi_finance_tool(ticker: str, exchange: str = "NASDAQ") -> dict:
    """Fetch Google Finance snapshot via SerpApi — INDEPENDENT source from yfinance.
    Use this as the second source for /verify cross-checks, or to pull related-stocks
    and same-day news for /swing setup analysis. Exchange is usually NASDAQ or NYSE."""
    return serpapi_finance(ticker, exchange)


GEMINI_TOOLS = [
    _get_stock_data_tool,
    _get_portfolio_snapshot_tool,
    _get_watchlist_snapshot_tool,
    _get_ticker_full_report_tool,
    _get_all_tickers_report_tool,
    _get_market_news_tool,
    _send_discord_alert_tool,
    _serpapi_finance_tool,
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

# Entries live in Supabase, scoped to the current user. They used to be written
# to a local journal.json, which Vercel wipes on every cold start — so entries
# silently vanished. JOURNAL_FILE remains only as the offline-dev fallback for
# when there is no user or no database.
JOURNAL_FILE = "journal.json"


def _journal_local_load() -> list:
    if not os.path.exists(JOURNAL_FILE):
        return []
    with open(JOURNAL_FILE) as f:
        return json.load(f)


def _journal_local_save(entries: list):
    with open(JOURNAL_FILE, "w") as f:
        json.dump(entries, f, indent=2)


def _journal_db_ok() -> str | None:
    """Return the user id to store entries under, or None to use the local file."""
    uid = current_user_id()
    return uid if (_SUPABASE_OK and uid) else None


class JournalTruncated(RuntimeError):
    """The ledger came back incomplete. Replaying a partial ledger would invent
    history — missing buys look like overselling — so callers must refuse rather
    than carry on with what arrived."""


# PostgREST caps how many rows it will return. Ask for far more than any personal
# trading history will reach, and treat hitting the cap as an error, not a page.
_JOURNAL_LIMIT = 10000


def _content_range_total(header: str | None) -> int | None:
    """Pull the total out of PostgREST's `Content-Range: 0-24/137`. None if the
    server didn't say."""
    if not header or "/" not in header:
        return None
    total = header.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


def load_journal() -> list:
    uid = _journal_db_ok()
    if not uid:
        return _journal_local_load()
    r = requests.get(
        f"{_SUPABASE_URL}/rest/v1/journal_entries",
        params={"user_id": f"eq.{uid}", "select": "data",
                "order": "created_at.asc", "limit": _JOURNAL_LIMIT},
        headers={**_sb_headers(), "prefer": "count=exact"}, timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    total = _content_range_total(r.headers.get("content-range"))
    if total is not None and total > len(rows):
        raise JournalTruncated(
            f"journal has {total} entries but only {len(rows)} came back — "
            "refusing to replay a partial ledger")
    return [row["data"] for row in rows]


def save_journal(entries: list):
    """Full-list replace. Only the local-file path needs it — the database path
    adds and deletes individual rows."""
    if not _journal_db_ok():
        _journal_local_save(entries)


def add_journal_entry(entry: dict) -> dict:
    now = datetime.now(_GMT7)
    # Timestamp first so ids stay lexicographically chronological; the random
    # suffix keeps two accounts from colliding on the same microsecond, which
    # would 409 — `id` is a global primary key and the insert has no on_conflict.
    entry["id"] = (entry.get("id")
                   or f"trade_{now.strftime('%Y%m%d%H%M%S%f')}_{secrets.token_hex(2)}")
    entry["date"] = entry.get("date") or now.strftime("%Y-%m-%d")
    entry["created_at"] = entry.get("created_at") or now.isoformat()

    uid = _journal_db_ok()
    if not uid:
        entries = _journal_local_load()
        entries.append(entry)
        _journal_local_save(entries)
        return entry

    r = requests.post(
        f"{_SUPABASE_URL}/rest/v1/journal_entries",
        headers=_sb_headers(),
        json=[{"id": entry["id"], "user_id": uid, "data": entry}],
        timeout=15,
    )
    r.raise_for_status()
    return entry


def delete_journal_entry(entry_id: str) -> bool:
    uid = _journal_db_ok()
    if not uid:
        entries = _journal_local_load()
        new = [e for e in entries if e.get("id") != entry_id]
        if len(new) == len(entries):
            return False
        _journal_local_save(new)
        return True

    # Scope the delete by user_id as well as id, so one account can never remove
    # another account's entry by guessing its id.
    r = requests.delete(
        f"{_SUPABASE_URL}/rest/v1/journal_entries",
        params={"id": f"eq.{entry_id}", "user_id": f"eq.{uid}"},
        headers={**_sb_headers(), "prefer": "return=representation"},
        timeout=15,
    )
    r.raise_for_status()
    return bool(r.json())


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
    elif name in ("_serpapi_finance_tool", "serpapi_finance"):
        raw = serpapi_finance(args.get("ticker", ""), args.get("exchange", "NASDAQ"))
    else:
        raw = {"error": f"Unknown tool: {name}"}
    return json.dumps(raw, default=str)


# ---------------------------------------------------------------------------
# Streaming chat with Gemini tool use
# ---------------------------------------------------------------------------

SWING_RESEARCH_PROMPT = """Run a **4-persona swing-trade research** on **__TICKER__** and produce a single Trade Card. Use get_stock_data / get_ticker_full_report / get_market_news for indicators and news, and **call serpapi_finance once** to pull Google-Finance related-stocks (sector peers) and cross-check the current price. Do NOT guess any number.

Reason internally as each persona in turn, then synthesize. Do NOT print each persona's reasoning in the final output — only the synthesized Trade Card.

**Persona A — Weinstein Stage Analyst**
Classify the weekly stage (1 base, 2 advance, 3 top, 4 decline). Check price vs 30-week MA (proxy: MA150 daily) — slope and position.

**Persona B — Minervini Setup Hunter**
Look for VCP / flat base / cup-handle / high-tight flag. Identify trigger price and required volume (≥1.5× 20-day avg). Score the Trend Template (8 criteria): price > 50MA > 150MA > 200MA, 200MA rising, price ≥ 25% above 52w low, within 25% of 52w high.

**Persona C — O'Neil CANSLIM / Catalyst Scout**
Score C-A-N-S-L-I-M letter by letter (Strong / Mixed / Weak). Note any catalyst (earnings, news, upgrade) within 0–60 days.

**Persona D — Risk Skeptic (adversarial)**
Argue the bear case for the next 4 weeks. Flag if extended >10% from 21-EMA, earnings within hold window, macro events, distribution days. Set the stop-loss that invalidates the setup.

**Hard rules** — REJECT the trade (verdict = Skip) if any of these fail:
- R:R < 2.0
- Weinstein Stage 4
- Trend Template fails by 3+ criteria
- Buying inside last 3 days before earnings (unless user explicitly wants an earnings play)

**Output format (markdown, exactly this shape):**

### __TICKER__ — Swing Research

**Setup Grade:** A / B / C / Pass
**Stage:** {weinstein stage}
**Pattern:** {VCP / flat base / cup-handle / breakout / extended / none}

| Field | Value |
|---|---|
| Trigger | $X |
| Stop | $X |
| Target 1 | $X |
| Target 2 | $X |
| R:R | x.x |
| Position size (1% risk) | N shares |
| Time stop | N days |
| Catalyst window | earnings date / event / none |

**Verdict:** Take / Watch / Skip — one-sentence reason.

**Key risks:**
- risk 1
- risk 2
- risk 3
"""

VERIFY_DATA_PROMPT = """Verify the following financial claim by cross-checking ≥2 independent sources. Use the available tools (get_stock_data, get_market_news) plus your knowledge of canonical sources.

Claim: **__CLAIM__**

Rules:
- Two pages of the same site = ONE source. Need two different organizations.
- For price / market-cap / 52w-range claims: use **get_stock_data (yfinance)** as source 1 and **serpapi_finance (Google Finance)** as source 2 — these are independent organizations.
- If sources disagree by >1%, do NOT pick the more plausible one — fetch a third and report all three.
- Never cite Reddit/Twitter/Stocktwits or another LLM as a primary source.

Output:

### Verification: __CLAIM__

| Source | Value | As of |
|---|---|---|
| source 1 | ... | timestamp |
| source 2 | ... | timestamp |

**Delta:** x.xx%  ✓ within 1%   /   ⚠ outside 1%
**Verified value:** {value with caveats if any}
"""


def expand_slash_command(message: str) -> str:
    """Rewrite /swing TICKER and /verify <claim> into full research prompts.

    Any other message (including /unknown) is returned untouched.
    """
    msg = message.strip()
    if msg.lower().startswith("/swing "):
        ticker = msg.split(None, 1)[1].strip().upper().split()[0]
        if ticker:
            return SWING_RESEARCH_PROMPT.replace("__TICKER__", ticker)
    elif msg.lower().startswith("/verify "):
        claim = msg.split(None, 1)[1].strip()
        if claim:
            return VERIFY_DATA_PROMPT.replace("__CLAIM__", claim)
    return message


def chat_stream(user_message: str, history: list[dict]):
    """Yields SSE strings. Runs Gemini tool-use loop with streaming."""
    user_message = expand_slash_command(user_message)
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
