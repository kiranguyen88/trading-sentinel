"""Trade ledger — FIFO cost basis and realized P&L.

The ledger is the source of truth for positions. `holdings` in user_state is a
projection of whatever open lots remain after replaying every entry, so nothing
derived is ever stored: deleting or back-dating an entry simply recomputes. That
is also forced by the storage layer, where `save_journal()` is a no-op and
entries can only be added and removed one row at a time.

Everything above the "I/O tail" marker is pure — it imports nothing from this
project, takes plain dicts and returns dataclasses. The FIFO maths is the part
that has to be right and there is no test suite here, so it stays exercisable
from a REPL without a database:

    >>> import journal
    >>> es = [{"side":"buy","ticker":"X","shares":10,"price":100,"date":"2026-01-01"},
    ...       {"side":"buy","ticker":"X","shares":10,"price":120,"date":"2026-02-01"},
    ...       {"side":"sell","ticker":"X","shares":15,"price":130,"date":"2026-03-01"}]
    >>> journal.replay(es).totals["realized_pnl"]
    350.0
"""

import math
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_GMT7 = timezone(timedelta(hours=7))

# Share counts are floats (the holdings editor accepts fractional quantities), so
# "is this lot used up?" has to be a tolerance rather than == 0. Without it a lot
# lingers at 3e-16 shares forever and renders as a dead "0sh" card.
EPS = 1e-9

SIDES = ("open", "buy", "sell", "adjust")
# Sides that create inventory. `open` is a backfilled pre-journal position; only
# the backfill route may emit one.
OPENING = ("open", "buy")


# ---------------------------------------------------------------------------
# Pure core
# ---------------------------------------------------------------------------

@dataclass
class Lot:
    """One parcel of shares bought at one price. `cost_per_share` carries the
    capitalised entry fee; `price_per_share` is the raw execution price, kept so
    realized P&L can be reported both gross and net of fees."""
    lot_id: str
    ticker: str
    date: str
    shares: float
    cost_per_share: float
    price_per_share: float
    orig_shares: float
    # Which side opened this lot, so the UI can label a backfilled position as an
    # opening balance rather than presenting an assumed cost basis as a real buy.
    origin: str = "buy"


@dataclass
class Leg:
    """The slice of one lot that a sell consumed."""
    lot_id: str
    shares: float
    cost_per_share: float
    lot_date: str


@dataclass
class RealizedTrade:
    sell_id: str
    ticker: str
    sell_date: str
    shares: float
    sell_price: float
    proceeds: float
    cost: float
    pnl: float
    pnl_gross: float
    fees: float
    pnl_pct: float
    hold_days: int
    legs: list = field(default_factory=list)


@dataclass
class ReplayResult:
    open_lots: list = field(default_factory=list)
    realized: list = field(default_factory=list)
    per_ticker: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def _f(value, default=0.0) -> float:
    """Coerce to a finite float. Journal entries are free-form jsonb, so a NaN or
    a string can genuinely arrive here."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) or math.isinf(out) else out


def valid_date(text) -> bool:
    try:
        datetime.strptime(str(text), "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def _days_between(start: str, end: str) -> int:
    try:
        a = datetime.strptime(start, "%Y-%m-%d")
        b = datetime.strptime(end, "%Y-%m-%d")
    except (TypeError, ValueError):
        return 0
    return max(0, (b - a).days)


def today() -> str:
    return datetime.now(_GMT7).strftime("%Y-%m-%d")


def normalize(entry: dict) -> dict:
    """Return a cleaned copy. Tickers are upper-cased here and nowhere else —
    'aapl' and 'AAPL' would otherwise become two independent FIFO queues."""
    e = dict(entry or {})
    e["id"] = str(e.get("id") or "")
    e["ticker"] = str(e.get("ticker") or "").strip().upper()
    # Keep an unrecognised side as-is rather than defaulting it to "buy" —
    # replay() warns and skips, instead of silently inventing inventory.
    e["side"] = str(e.get("side") or "").strip().lower()
    e["shares"] = abs(_f(e.get("shares")))
    e["price"] = _f(e.get("price"))
    e["fees"] = abs(_f(e.get("fees")))
    e["date"] = str(e.get("date") or "")
    e["time"] = str(e.get("time") or "")
    e["created_at"] = str(e.get("created_at") or "")
    e["notes"] = str(e.get("notes") or "")
    e["no_pnl"] = bool(e.get("no_pnl"))
    if e.get("target_shares") is not None:
        e["target_shares"] = abs(_f(e.get("target_shares")))
    return e


def sort_key(e: dict) -> tuple:
    """Total order for replay. The database order cannot be trusted: rows come
    back ordered by the *table* created_at column, which is not the same as the
    trade date, and FIFO is only correct against a stable chronological order."""
    return (
        # Backfilled opening balances predate everything in the ledger. Ranking
        # them first is honest, where giving them a fake old date would not be,
        # and it stops a back-dated buy consuming the wrong lot.
        0 if e.get("side") == "open" else 1,
        e.get("date") or "",          # YYYY-MM-DD sorts chronologically as text
        e.get("time") or "",          # optional HH:MM; missing sorts first
        # Same date: buys before sells, so logging a sell and only then
        # remembering the buy does not look like an oversell.
        0 if e.get("side") in OPENING else 1,
        e.get("created_at") or "",    # insertion order
        e.get("id") or "",
    )


def validate_trade(payload: dict):
    """-> (clean_entry, error). Used by the add route before anything is written."""
    e = normalize(payload)
    if not e["ticker"]:
        return None, "ticker required"
    if e["side"] not in ("buy", "sell"):
        return None, "side must be buy or sell"
    if e["shares"] <= 0:
        return None, "shares must be greater than 0"
    # A zero price reaching holdings as avg_buy_price is a ZeroDivisionError
    # inside a ThreadPoolExecutor future, which kills the whole /portfolio fetch.
    if e["price"] <= 0:
        return None, "price must be greater than 0"
    e["date"] = e["date"] or today()
    if not valid_date(e["date"]):
        return None, "date must be YYYY-MM-DD"
    if e["time"] and not _valid_time(e["time"]):
        return None, "time must be HH:MM"
    e.pop("target_shares", None)
    e["v"] = 1
    return e, None


def validate_adjust(payload: dict):
    """-> (clean_entry, error). A declared target share count for one ticker."""
    e = normalize(payload)
    if not e["ticker"]:
        return None, "ticker required"
    if e.get("target_shares") is None:
        return None, "target_shares required"
    if e["price"] <= 0:
        return None, "price must be greater than 0"
    e["date"] = e["date"] or today()
    if not valid_date(e["date"]):
        return None, "date must be YYYY-MM-DD"
    e["side"] = "adjust"
    e["shares"] = 0.0
    e["v"] = 1
    return e, None


def _valid_time(text) -> bool:
    try:
        datetime.strptime(str(text), "%H:%M")
        return True
    except (TypeError, ValueError):
        return False


def _push_lot(queue: list, e: dict, shares: float, price: float, fees: float):
    if shares <= EPS:
        return
    queue.append(Lot(
        lot_id=e["id"], ticker=e["ticker"], date=e["date"],
        shares=shares,
        cost_per_share=(shares * price + fees) / shares,
        price_per_share=price,
        orig_shares=shares,
        origin=e["side"],
    ))


def _consume(queue: list, want: float):
    """Pop `want` shares off the front of the FIFO queue, splitting the head lot
    when it is larger than the sale. -> (legs, consumed, cost, cost_gross)."""
    legs, consumed, cost, cost_gross = [], 0.0, 0.0, 0.0
    while want > EPS and queue:
        lot = queue[0]
        take = min(lot.shares, want)
        legs.append(Leg(lot.lot_id, take, lot.cost_per_share, lot.date))
        cost += take * lot.cost_per_share
        cost_gross += take * lot.price_per_share
        lot.shares -= take
        consumed += take
        want -= take
        if lot.shares <= EPS:
            queue.pop(0)
    return legs, consumed, cost, cost_gross


def _book(e: dict, legs, consumed, cost, cost_gross, sell_fee) -> RealizedTrade:
    gross = consumed * e["price"] - cost_gross
    fees = (cost - cost_gross) + sell_fee      # capitalised entry fees + exit fee
    return RealizedTrade(
        sell_id=e["id"], ticker=e["ticker"], sell_date=e["date"],
        shares=consumed, sell_price=e["price"],
        proceeds=consumed * e["price"] - sell_fee,
        cost=cost,
        pnl=gross - fees,
        pnl_gross=gross,
        fees=fees,
        pnl_pct=((gross - fees) / cost * 100) if cost > EPS else 0.0,
        hold_days=_days_between(legs[0].lot_date, e["date"]) if legs else 0,
        legs=legs,
    )


def _sell(queue, e, want, warnings, realized, sell_fee, book_pnl=True):
    legs, consumed, cost, cost_gross = _consume(queue, want)
    if consumed < want - EPS:
        warnings.append({
            "code": "oversell", "entry_id": e["id"], "ticker": e["ticker"],
            "available": round(consumed, 6),
            "message": (f"{e['ticker']}: tried to sell {want:g} shares on "
                        f"{e['date'] or 'an unknown date'} but only {consumed:g} "
                        f"were open"),
        })
    if consumed <= EPS or not book_pnl:
        return
    realized.append(_book(e, legs, consumed, cost, cost_gross, sell_fee))


def _adjust(queue, e, warnings, realized):
    """A declared target share count. Never inferred — the user typed it."""
    target = e.get("target_shares")
    current = sum(lot.shares for lot in queue)
    delta = target - current

    if abs(delta) <= EPS:
        # Quantity unchanged, so this is a cost-basis correction. Scale the lots
        # rather than flattening them to one price, which keeps distinct lots
        # distinct while making their blended average the number that was typed.
        cost = sum(lot.shares * lot.cost_per_share for lot in queue)
        if current > EPS and cost > EPS and e["price"] > 0:
            factor = (e["price"] * current) / cost
            for lot in queue:
                lot.cost_per_share *= factor
                lot.price_per_share *= factor
        return

    if delta > 0:
        _push_lot(queue, e, delta, e["price"], e["fees"])
        return

    # Shares leaving. `no_pnl` means "I am fixing a typo", so the inventory goes
    # but nothing is booked as a trade.
    _sell(queue, e, -delta, warnings, realized, e["fees"], book_pnl=not e["no_pnl"])


def replay(entries: list) -> ReplayResult:
    """Rebuild every open lot and realized trade from the ledger.

    Total by construction: it never raises. A malformed or over-selling entry is
    clamped and recorded in `warnings`, because one bad row must not make the
    journal unloadable.
    """
    lots: dict = {}
    realized: list = []
    warnings: list = []

    for e in sorted((normalize(x) for x in entries or []), key=sort_key):
        if not e["ticker"] or e["side"] not in SIDES:
            warnings.append({
                "code": "bad_entry", "entry_id": e["id"], "ticker": e["ticker"],
                "message": f"skipped an entry with side {e['side']!r}",
            })
            continue

        queue = lots.setdefault(e["ticker"], [])
        if e["side"] in OPENING:
            _push_lot(queue, e, e["shares"], e["price"], e["fees"])
        elif e["side"] == "sell":
            _sell(queue, e, e["shares"], warnings, realized, e["fees"])
        else:
            _adjust(queue, e, warnings, realized)

    open_lots = [lot for queue in lots.values() for lot in queue if lot.shares > EPS]
    return ReplayResult(
        open_lots=open_lots,
        realized=realized,
        per_ticker=_per_ticker(lots, realized),
        totals=_totals(realized, open_lots),
        warnings=warnings,
    )


def _blank_ticker(ticker: str) -> dict:
    return {"ticker": ticker, "realized_pnl": 0.0, "fees": 0.0, "trades": 0,
            "wins": 0, "losses": 0, "open_shares": 0.0, "avg_cost": 0.0}


def _per_ticker(lots: dict, realized: list) -> dict:
    out: dict = {}
    for ticker, queue in lots.items():
        row = out.setdefault(ticker, _blank_ticker(ticker))
        shares = sum(lot.shares for lot in queue)
        cost = sum(lot.shares * lot.cost_per_share for lot in queue)
        row["open_shares"] = shares if shares > EPS else 0.0
        row["avg_cost"] = (cost / shares) if shares > EPS else 0.0
    for trade in realized:
        row = out.setdefault(trade.ticker, _blank_ticker(trade.ticker))
        row["realized_pnl"] += trade.pnl
        row["fees"] += trade.fees
        row["trades"] += 1
        if trade.pnl > 0:
            row["wins"] += 1
        elif trade.pnl < 0:
            row["losses"] += 1
    return out


def _totals(realized: list, open_lots: list) -> dict:
    wins = [t.pnl for t in realized if t.pnl > 0]
    losses = [t.pnl for t in realized if t.pnl < 0]
    loss_sum = abs(sum(losses))
    return {
        "realized_pnl": sum(t.pnl for t in realized),
        "realized_pnl_gross": sum(t.pnl_gross for t in realized),
        "fees_total": sum(t.fees for t in realized),
        "trade_count": len(realized),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(realized) * 100) if realized else 0.0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": (sum(wins) / loss_sum) if loss_sum > EPS else None,
        "open_cost_basis": sum(l.shares * l.cost_per_share for l in open_lots),
    }


def derive_holdings(open_lots: list) -> list:
    """Project open lots onto the holdings shape the rest of the app expects.

    One row per ticker, not per lot. Blended-average unrealized P&L is identical
    to summing per-lot P&L, and a row per lot would make the holdings index
    unstable: selling out of lot 0 of 3 renumbers the rest, and both the price
    cache key (`TICKER#i`) and the DOM's data-lot attribute would then point at a
    different lot until the next full reload.
    """
    order: list = []
    agg: dict = {}
    for lot in open_lots:
        if lot.shares <= EPS:
            continue
        if lot.ticker not in agg:
            agg[lot.ticker] = [0.0, 0.0]
            order.append(lot.ticker)
        agg[lot.ticker][0] += lot.shares
        agg[lot.ticker][1] += lot.shares * lot.cost_per_share
    return [
        {"ticker": t,
         "quantity": round(agg[t][0], 6),
         "avg_buy_price": round(agg[t][1] / agg[t][0], 4)}
        for t in order if agg[t][0] > EPS
    ]


def _r(value, places=2):
    return None if value is None else round(value, places)


def summarize(res: ReplayResult, entries: list, limit: int = 200) -> dict:
    """Shape a ReplayResult for the API. Rounding happens here, at the boundary,
    and totals are rounded once rather than summed from rounded parts."""
    totals = {k: _r(v) for k, v in res.totals.items()}
    totals["win_rate"] = _r(res.totals["win_rate"], 1)
    totals["profit_factor"] = _r(res.totals["profit_factor"])

    per_ticker = sorted(
        ({**row,
          "realized_pnl": _r(row["realized_pnl"]),
          "fees": _r(row["fees"]),
          "open_shares": _r(row["open_shares"], 6),
          "avg_cost": _r(row["avg_cost"], 4)}
         for row in res.per_ticker.values()),
        key=lambda r: (-abs(r["realized_pnl"] or 0), r["ticker"]),
    )

    open_lots = [{"lot_id": l.lot_id, "ticker": l.ticker, "date": l.date,
                  "origin": l.origin,
                  "shares": _r(l.shares, 6),
                  "cost_per_share": _r(l.cost_per_share, 4),
                  "remaining_cost": _r(l.shares * l.cost_per_share)}
                 for l in res.open_lots]

    realized = [{"sell_id": t.sell_id, "ticker": t.ticker, "sell_date": t.sell_date,
                 "shares": _r(t.shares, 6), "sell_price": _r(t.sell_price, 4),
                 "proceeds": _r(t.proceeds), "cost": _r(t.cost),
                 "pnl": _r(t.pnl), "pnl_gross": _r(t.pnl_gross),
                 "fees": _r(t.fees), "pnl_pct": _r(t.pnl_pct),
                 "hold_days": t.hold_days,
                 "legs": [{"lot_id": g.lot_id, "shares": _r(g.shares, 6),
                           "cost_per_share": _r(g.cost_per_share, 4),
                           "lot_date": g.lot_date} for g in t.legs]}
                for t in reversed(res.realized)]

    # Newest first for display, which is the reverse of replay order.
    recent = sorted((normalize(e) for e in entries or []), key=sort_key, reverse=True)

    return {
        "totals": totals,
        "per_ticker": per_ticker,
        "open_lots": open_lots,
        "realized_trades": realized,
        "entries": recent[:limit],
        "entry_count": len(recent),
        "warnings": res.warnings,
    }


# ---------------------------------------------------------------------------
# I/O tail — everything below here talks to Supabase through trading_bot
# ---------------------------------------------------------------------------

import trading_bot as tb  # noqa: E402  (kept below the pure core deliberately)


def _new_id() -> str:
    now = datetime.now(_GMT7)
    return f"trade_{now.strftime('%Y%m%d%H%M%S%f')}_{secrets.token_hex(2)}"


def _require_user():
    """A journal call with no bound user, while the database is live, means we
    are on a background thread — APScheduler jobs don't inherit the request
    ContextVar. Left alone it would quietly read and write the local dev file
    instead of the account's ledger."""
    uid = tb.current_user_id()
    if not uid and tb._SUPABASE_OK:
        raise RuntimeError("journal operations require a bound user")
    return uid


def read_ledger() -> list:
    """Every entry for the current user.

    Raises rather than returning a partial or empty list: an unreachable
    database is not an empty ledger, and deriving holdings from one we failed to
    read is exactly how live data gets replaced by nothing.
    """
    _require_user()
    return tb.load_journal()


def current_state():
    entries = read_ledger()
    return entries, replay(entries)


def sync_holdings(entries: list, res: ReplayResult, *, allow_empty: bool = False) -> bool:
    """Write the ledger's open lots back to user_state. -> True if it changed."""
    # An empty ledger derives an empty portfolio. Refusing here is what keeps a
    # pre-backfill page load from wiping real holdings.
    if not entries:
        return False
    holdings = derive_holdings(res.open_lots)
    # Nothing open and nothing ever closed is not a portfolio that was sold down
    # — it is a ledger that makes no sense. Don't act on it.
    if not holdings and not (allow_empty or res.realized):
        return False
    portfolio = tb.load_portfolio()
    if portfolio.get("holdings") == holdings:
        return False
    portfolio["holdings"] = holdings
    tb.save_portfolio(portfolio)
    return True


def _resync() -> dict:
    """Re-read the ledger and project it onto holdings.

    Deliberately re-reads instead of reusing the caller's view: another tab may
    have logged a trade in between, and deriving from a stale list would drop it.
    """
    try:
        entries, res = current_state()
        changed = sync_holdings(entries, res)
        return {"holdings_synced": True, "holdings_changed": changed,
                "totals": summarize(res, entries, limit=0)["totals"],
                "warnings": res.warnings}
    except Exception as exc:
        # The entry itself is already durably written. Holdings are only a cache,
        # and reporting failure here would make the user log the trade twice.
        return {"holdings_synced": False, "holdings_changed": False,
                "warning": f"Recorded, but holdings did not sync ({exc}). "
                           f"Use Resync holdings."}


def _write(entry: dict) -> dict:
    tb.add_journal_entry(entry)
    return {"ok": True, "entry": entry, **_resync()}


def record_trade(payload: dict) -> dict:
    entry, err = validate_trade(payload)
    if err:
        return {"ok": False, "code": "invalid", "error": err}
    entry["id"] = _new_id()

    # Validate against the whole ledger, not against today's holdings: a
    # back-dated sell can be fine now and impossible at its own date, and a
    # back-dated buy can rescue a sell that previously overdrew.
    entries = read_ledger()
    bad = [w for w in replay(entries + [entry]).warnings
           if w.get("entry_id") == entry["id"] and w["code"] == "oversell"]
    if bad:
        available = bad[0]["available"]
        return {"ok": False, "code": "oversell", "available": available,
                "error": (f"Selling {entry['shares']:g} {entry['ticker']} but only "
                          f"{available:g} shares are open as of {entry['date']}. "
                          f"Log the missing buy first.")}
    return _write(entry)


def record_adjust(payload: dict) -> dict:
    """A declared target share count for one ticker. Cannot oversell by
    construction — it consumes at most what is already open."""
    entry, err = validate_adjust(payload)
    if err:
        return {"ok": False, "code": "invalid", "error": err}
    entry["id"] = _new_id()
    return _write(entry)


def remove_trade(entry_id: str) -> dict:
    _require_user()
    if not tb.delete_journal_entry(entry_id):
        return {"ok": False, "code": "not_found", "error": "no such entry"}
    # Unlike adding, a delete is never blocked by the warnings it creates —
    # refusing would trap the user with an entry they cannot remove.
    return {"ok": True, **_resync()}


def resync() -> dict:
    entries, res = current_state()
    changed = sync_holdings(entries, res)
    return {"ok": True, "changed": changed,
            "holdings": derive_holdings(res.open_lots),
            "warnings": res.warnings}


def backfill_from_holdings(*, force: bool = False) -> dict:
    """Give pre-journal positions a cost basis, one `open` entry per holdings
    row — per row, not per ticker, so today's duplicate-ticker rows survive as
    the distinct FIFO lots they represent."""
    _require_user()
    entries = read_ledger()
    if entries and not force:
        return {"ok": False, "code": "already_backfilled",
                "error": f"the ledger already has {len(entries)} entries"}

    holdings = tb.load_portfolio().get("holdings") or []
    if not holdings:
        return {"ok": False, "code": "no_holdings",
                "error": "there are no holdings to backfill"}

    stamp = today()
    created = []
    skipped = []
    for row in holdings:
        ticker = str(row.get("ticker") or "").strip().upper()
        shares = abs(_f(row.get("quantity")))
        price = _f(row.get("avg_buy_price"))
        if not ticker or shares <= EPS or price <= 0:
            skipped.append(row)
            continue
        created.append(tb.add_journal_entry({
            "id": _new_id(), "side": "open", "ticker": ticker,
            "shares": shares, "price": price, "fees": 0.0,
            "date": stamp, "time": "", "notes": "opening balance", "v": 1,
        }))

    return {"ok": True, "created": len(created), "entries": created,
            "skipped": len(skipped), **_resync()}
