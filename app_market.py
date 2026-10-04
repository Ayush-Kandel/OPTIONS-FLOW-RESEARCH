"""Read-only data for FlowDesk's Market screen: your tickers' price, VWAP, volume vs. normal,
momentum, buying vs. selling pressure and today's whale flow.

Prices come from what the IBKR live tracker saves (market_live.py): 1-minute bars as they form,
plus live quotes, trades and the order book when the account has those subscriptions. Whale flow
comes from the Trade Echo prints the logger already stored - no extra credits. Like app_data,
this never writes flow.db; the only state is which ticker is open on screen (FOCUS), which the
tracker asks for so it can stream that ticker's order book.
"""

import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from app_data import ET, _db, _market_open, _now

ORDER = ["SPY", "QQQ", "NVDA", "TSLA", "META", "GOOG", "MU", "INTC", "PLTR", "CRWV", "HIMS", "DRAM"]
FRESH_SEC = 20            # market_now older than this isn't "live"
BIG_WHALE = 350_000
MARKER_MIN = 250_000      # whale prints drawn on the chart
SPARK_POINTS = 78
FOCUS = {"ticker": None, "at": 0.0}


def tickers():
    import pipeline
    mine = set(pipeline.MY_TICKERS)
    return [t for t in ORDER if t in mine] + sorted(mine - set(ORDER))


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _age(at_utc):
    return (datetime.now(timezone.utc) - datetime.fromisoformat(at_utc)).total_seconds() if at_utc else None


def _session(db, now):
    """Today while the market is open (or after today's open), else the last day with bars."""
    today = now.date().isoformat()
    if _market_open(now):
        return today
    row = db.execute("SELECT MAX(minute_et) FROM ib_stock_bars WHERE ticker = 'SPY' AND minute_et <= ?",
                     (today + " 23:59",)).fetchone()
    return row[0][:10] if row and row[0] else today


# ---------- volume: what's normal ----------
@lru_cache(maxsize=4)
def _curve(session):
    """Share of a normal day's volume traded by each minute (09:30 ... 15:59), averaged over your
    tickers' complete days in the month before `session`. Returns {hhmm: (share, cumulative)}."""
    import pipeline
    mine = sorted(pipeline.MY_TICKERS)
    start = (date.fromisoformat(session) - timedelta(days=45)).isoformat()
    with closing(_db()) as db:
        rows = db.execute(
            f"SELECT ticker, substr(minute_et, 1, 10), substr(minute_et, 12, 5), volume FROM ib_stock_bars "
            f"WHERE ticker IN ({','.join('?' * len(mine))}) AND minute_et >= ? AND minute_et < ? AND volume > 0",
            (*mine, start, session)).fetchall()
    days = {}
    for t, d, hm, v in rows:
        if "09:30" <= hm <= "15:59":
            days.setdefault((t, d), {})[hm] = v
    share, n = {}, 0
    for bars in days.values():
        if len(bars) < 380:
            continue
        total = sum(bars.values())
        n += 1
        for hm, v in bars.items():
            share[hm] = share.get(hm, 0) + v / total
    if not n:
        return {}
    out, cum = {}, 0.0
    for i in range(390):
        hm = f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}"
        s = share.get(hm, 0) / n
        cum += s
        out[hm] = (s, cum)
    return out


def _normal_volume(db, t, session):
    """Normal full-day volume: average of the last 20 daily bars (IBKR), else of complete days of
    1-minute bars we already have."""
    if _has(db, "ib_stock_daily"):
        r = db.execute("SELECT AVG(volume), COUNT(*) FROM (SELECT volume FROM ib_stock_daily WHERE ticker = ? "
                       "AND trade_date < ? ORDER BY trade_date DESC LIMIT 20)", (t, session)).fetchone()
        if r[1] >= 5:
            return r[0]
    r = db.execute("SELECT AVG(v) FROM (SELECT SUM(volume) v FROM ib_stock_bars WHERE ticker = ? AND minute_et < ? "
                   "GROUP BY substr(minute_et, 1, 10) HAVING COUNT(*) >= 380 ORDER BY substr(minute_et, 1, 10) DESC "
                   "LIMIT 20)", (t, session)).fetchone()
    return r[0]


def _prev_close(db, t, session):
    if _has(db, "ib_stock_daily"):
        r = db.execute("SELECT close, trade_date FROM ib_stock_daily WHERE ticker = ? AND trade_date < ? "
                       "ORDER BY trade_date DESC LIMIT 1", (t, session)).fetchone()
        if r:
            return r[0]
    r = db.execute("SELECT close, minute_et FROM ib_stock_bars WHERE ticker = ? AND minute_et < ? "
                   "ORDER BY minute_et DESC LIMIT 1", (t, session)).fetchone()
    # only if those bars are from the session right before (a few calendar days at most)
    if r and (date.fromisoformat(session) - date.fromisoformat(r[1][:10])).days <= 4:
        return r[0]
    return None


# ---------- one ticker ----------
def _bars(db, t, session, now_row):
    rows = [list(r) for r in db.execute(
        "SELECT minute_et, open, high, low, close, volume, vwap FROM ib_stock_bars WHERE ticker = ? "
        "AND minute_et >= ? AND minute_et <= ? ORDER BY minute_et", (t, session + " 09:30", session + " 15:59"))]
    # the minute that is still forming (or the day's last one, which the tracker hasn't filed yet)
    if now_row and now_row["bar_minute"] and now_row["bar_minute"][:10] == session and \
            (not rows or now_row["bar_minute"] > rows[-1][0]) and now_row["bar_minute"][11:] <= "15:59":
        rows.append([now_row["bar_minute"], now_row["bar_open"], now_row["bar_high"], now_row["bar_low"],
                     now_row["price"], now_row["bar_volume"], now_row["bar_vwap"]])
    return rows


def _ema(values, n):
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    g, l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        g, l = (g * (n - 1) + gains[i]) / n, (l * (n - 1) + losses[i]) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def _whales(db, t, session):
    """Trade Echo prints on this ticker that day: bullish = calls bought / puts sold, bearish = puts
    bought / calls sold (the side comes from Trade Echo's sentiment)."""
    from trade_context import side_of
    out = []
    for pc, sent, prem, hhmm, size, k, exp, fill, act in db.execute(
            "SELECT put_call, sentiment, COALESCE(premium, size * fill_price * 100), trade_time_et, size, strike, "
            "expiration, fill_price, activity_type FROM prints WHERE ticker = ? AND trade_date = ? "
            "ORDER BY trade_time_et", (t, session)):
        side = side_of(pc, sent)
        lean = "unclear" if side == "mid" else "bull" if (side == "buy") == (pc == "CALL") else "bear"
        out.append({"time": hhmm[:8], "put_call": pc, "strike": k, "expiration": exp, "size": size,
                    "price": fill, "premium": prem or 0, "side": side, "lean": lean,
                    "activity": (act or "").title()})
    return out


def _flow_summary(prints, session, now):
    end = now.strftime("%H:%M:%S") if now.date().isoformat() == session and _market_open(now) else "16:00:00"
    start30 = (datetime.strptime(end, "%H:%M:%S") - timedelta(minutes=30)).strftime("%H:%M:%S")
    total = lambda rows, lean: sum(p["premium"] for p in rows if p["lean"] == lean)
    recent = [p for p in prints if p["time"] >= start30]
    return {"bull": total(prints, "bull"), "bear": total(prints, "bear"), "unclear": total(prints, "unclear"),
            "n": len(prints), "big": sum(p["premium"] >= BIG_WHALE for p in prints),
            "recent_bull": total(recent, "bull"), "recent_bear": total(recent, "bear"), "recent_n": len(recent)}


def _pressure(db, t, bars, now_row, forming):
    """Share of volume that was buying over the last 15 minutes. With the live trade feed: trades at
    the ask vs. at the bid. Without it: an estimate from where each 1-minute bar closed within its
    range (close near the high = buyers in control)."""
    if now_row and now_row["feed"] == "ticks" and _has(db, "ib_stock_flow"):
        start = (datetime.strptime(forming, "%Y-%m-%d %H:%M") - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")
        b, s = db.execute("SELECT SUM(buy_volume), SUM(sell_volume) FROM ib_stock_flow WHERE ticker = ? AND "
                          "minute_et >= ?", (t, start)).fetchone()
        if b or s:
            day = (now_row["buy_volume"] or 0) + (now_row["sell_volume"] or 0)
            return {"method": "trades", "buy_share": b / (b + s),
                    "day_share": now_row["buy_volume"] / day if day else None}

    def clv(rows):
        num = den = 0.0
        for _, o, h, l, c, v, _w in rows:
            if v and h is not None and l is not None and h > l:
                num += v * ((c - l) - (h - c)) / (h - l)
                den += v
        return 0.5 + 0.5 * num / den if den else None

    done = [r for r in bars if r[0] < forming]
    return {"method": "estimate", "buy_share": clv(done[-15:]), "day_share": clv(done)}


def _stats(db, t, session, now, now_row):
    fresh = bool(now_row and _age(now_row["at_utc"]) is not None and _age(now_row["at_utc"]) < FRESH_SEC
                 and now_row["bar_minute"] and now_row["bar_minute"][:10] == session)
    bars = _bars(db, t, session, now_row)
    if not bars:
        return {"ticker": t, "bars": [], "state": "no_data"}
    forming = now.strftime("%Y-%m-%d %H:%M") if session == now.date().isoformat() else session + " 16:00"
    price = now_row["price"] if fresh and now_row["price"] else bars[-1][4]
    closes = [b[4] for b in bars]
    closes[-1] = price
    vol = sum(b[5] or 0 for b in bars)
    vw_num = sum((b[6] or (b[2] + b[3] + b[4]) / 3) * (b[5] or 0) for b in bars)
    vwap = vw_num / vol if vol else None
    prev = _prev_close(db, t, session)
    chg = lambda n: price / closes[-1 - n] - 1 if len(closes) > n and closes[-1 - n] else None

    curve = _curve(session)
    done = [b for b in bars if b[0] < forming]
    normal = _normal_volume(db, t, session)
    rvol = None
    if normal and curve and len(done) >= 3:
        expected = normal * curve.get(done[-1][0][11:16], (0, 0))[1]
        rvol = sum(b[5] or 0 for b in done) / expected if expected else None
    per_min = [(b[5] or 0) / curve[b[0][11:16]][0] for b in done if curve.get(b[0][11:16], (0,))[0]]
    trend_vol = None
    if len(per_min) >= 20 and sum(per_min[-20:-5]):
        trend_vol = (sum(per_min[-5:]) / 5) / (sum(per_min[-20:-5]) / 15)

    ema9, ema21 = (_ema(closes, 9), _ema(closes, 21)) if len(closes) >= 21 else (None, None)
    trend = None
    if ema9 is not None and vwap:
        trend = "up" if ema9 > ema21 and price > vwap else "down" if ema9 < ema21 and price < vwap else "mixed"

    age = _age(now_row["at_utc"]) if now_row else None
    is_open = _market_open(now) and session == now.date().isoformat()
    last_bar_age = (now.replace(tzinfo=None) - datetime.strptime(bars[-1][0], "%Y-%m-%d %H:%M")).total_seconds()
    state = ("live" if fresh else "delayed" if last_bar_age < 420 else "stale") if is_open else "closed"
    step = max(1, len(closes) // SPARK_POINTS)
    return {
        "ticker": t, "state": state, "feed": now_row["feed"] if fresh else "saved_bars",
        "updated_age": age, "price": price, "prev_close": prev,
        "change": price / prev - 1 if prev else None, "change_abs": price - prev if prev else None,
        "open": bars[0][1], "high": max(b[2] for b in bars if b[2] is not None),
        "low": min(b[3] for b in bars if b[3] is not None), "volume": vol, "normal_volume": normal,
        "vwap": vwap, "vs_vwap": price / vwap - 1 if vwap else None,
        "mom": {"1m": chg(1), "5m": chg(5), "15m": chg(15), "30m": chg(30)},
        "rsi": _rsi(closes), "trend": trend, "rvol": rvol, "vol_trend": trend_vol,
        "pressure": _pressure(db, t, bars, now_row if fresh else None, forming),
        "bid": now_row["bid"] if fresh else None, "ask": now_row["ask"] if fresh else None,
        "bid_size": now_row["bid_size"] if fresh else None, "ask_size": now_row["ask_size"] if fresh else None,
        "spark": [b[4] for b in bars[::step]] + ([price] if (len(bars) - 1) % step else []),
        "bar_minute": bars[-1][0], "bars": bars,
    }


def _now_rows(db):
    if not _has(db, "market_now"):
        return {}
    return {r["ticker"]: r for r in db.execute("SELECT * FROM market_now")}


def _feed(db):
    if not _has(db, "market_feed"):
        return {}
    return {r["item"]: {"status": r["status"], "detail": r["detail"], "at": r["at_utc"]}
            for r in db.execute("SELECT * FROM market_feed")}


def board():
    now = _now()
    with closing(_db()) as db:
        session = _session(db, now)
        rows = _now_rows(db)
        tiles = []
        for t in tickers():
            s = _stats(db, t, session, now, rows.get(t))
            s.pop("bars", None)
            s["flow"] = _flow_summary(_whales(db, t, session), session, now)
            tiles.append(s)
        feed = _feed(db)
    return {"session": session, "market_open": _market_open(now), "tiles": tiles, "feed": feed,
            "now_et": now.strftime("%I:%M:%S %p").lstrip("0")}


def _contracts(db, t, session):
    """Option contracts on this ticker that the system tracks (picks still listed): live quote,
    volume, open interest and how it changed since the day before."""
    has_now, has_ctx = _has(db, "ib_quotes_now"), _has(db, "pick_context")
    out = []
    for r in db.execute(
            "SELECT strike, put_call, expiration, MAX(id) AS pick_id, MAX(pinged_utc IS NOT NULL) AS pinged, "
            "SUM(premium) AS premium, COUNT(*) AS n, MAX(trade_date || ' ' || trade_time_et) AS latest "
            "FROM picks WHERE ticker = ? AND expiration >= ? GROUP BY strike, put_call, expiration "
            "ORDER BY pinged DESC, latest DESC LIMIT 40", (t, session)):
        key = (t, r["strike"], r["put_call"], r["expiration"])
        q, live = None, False
        if has_now:
            q = db.execute("SELECT bid, ask, last, volume, iv, at_utc FROM ib_quotes_now WHERE ticker = ? AND "
                           "strike = ? AND put_call = ? AND expiration = ?", key).fetchone()
            live = bool(q and _age(q["at_utc"]) < FRESH_SEC)
            q = q if live else None
        if q is None:   # the last saved minute that had a quote (after the close bid/ask go blank)
            q = db.execute("SELECT bid, ask, last, volume, iv, minute_et FROM ib_live_quotes WHERE ticker = ? AND "
                           "strike = ? AND put_call = ? AND expiration = ? AND minute_et LIKE ? "
                           "ORDER BY bid IS NULL, minute_et DESC LIMIT 1", (*key, session + "%")).fetchone()
        oi = db.execute("SELECT snap_date, open_interest FROM ib_oi WHERE ticker = ? AND strike = ? AND put_call = ? "
                        "AND expiration = ? AND snap_date <= ? ORDER BY snap_date DESC LIMIT 2",
                        (*key, session)).fetchall()
        side = None
        if has_ctx:
            c = db.execute("SELECT side FROM pick_context WHERE pick_id = ? AND status = 'ok'",
                           (r["pick_id"],)).fetchone()
            side = c["side"] if c else None
        vol = q["volume"] if q else None
        oi_now = oi[0]["open_interest"] if oi else None
        out.append({
            "pick_id": r["pick_id"], "strike": r["strike"], "put_call": r["put_call"], "expiration": r["expiration"],
            "pinged": bool(r["pinged"]), "premium": r["premium"], "n": r["n"], "side": side, "live": live,
            "bid": q["bid"] if q else None, "ask": q["ask"] if q else None, "last": q["last"] if q else None,
            "volume": vol, "iv": q["iv"] if q else None,
            "oi": oi_now, "oi_change": oi_now - oi[1]["open_interest"] if len(oi) == 2 and oi_now is not None
            and oi[1]["open_interest"] is not None else None,
            "vol_oi": vol / oi_now if vol and oi_now else None,
        })
    return out


def _book(db, t):
    feed = _feed(db).get("book")
    if not _has(db, "market_book"):
        return {"status": feed["status"] if feed else None, "detail": feed["detail"] if feed else None}
    rows = db.execute("SELECT side, level, price, size, venue, at_utc FROM market_book WHERE ticker = ? "
                      "ORDER BY side, level", (t,)).fetchall()
    fresh = [r for r in rows if _age(r["at_utc"]) < FRESH_SEC]
    return {"status": "ok" if fresh else feed["status"] if feed else None,
            "detail": feed["detail"] if feed else None,
            "bids": [[r["price"], r["size"], r["venue"]] for r in fresh if r["side"] == "bid"],
            "asks": [[r["price"], r["size"], r["venue"]] for r in fresh if r["side"] == "ask"]}


def ticker(t):
    t = (t or "").upper()
    FOCUS.update(ticker=t, at=time.time())   # the tracker streams this ticker's order book
    now = _now()
    with closing(_db()) as db:
        session = _session(db, now)
        s = _stats(db, t, session, now, _now_rows(db).get(t))
        prints = _whales(db, t, session)
        bars = s.pop("bars", [])
        vwap, num, den = [], 0.0, 0.0
        for b in bars:
            num += (b[6] or (b[2] + b[3] + b[4]) / 3) * (b[5] or 0)
            den += b[5] or 0
            vwap.append([b[0], num / den if den else None])
        s.update(session=session, market_open=_market_open(now), bars=bars, vwap_line=vwap,
                 flow=_flow_summary(prints, session, now),
                 prints=sorted(prints, key=lambda p: p["time"], reverse=True)[:80],
                 markers=[p for p in prints if p["premium"] >= MARKER_MIN][-40:],
                 contracts=_contracts(db, t, session), book=_book(db, t), feed=_feed(db))
    return s


def focus():
    return dict(FOCUS)
