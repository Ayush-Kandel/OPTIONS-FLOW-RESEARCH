"""IBKR (TWS) price data for picks - READ-ONLY market data, nothing else.

For every pick whose contract is still listed, pulls the 1-minute TRADES and BID bars of the
print day and records what happened AFTER the fill minute (Trade Echo's daily high includes
prices from before the print, which nobody could have traded at):
  - best/worst trade price after the print, and the 4 PM close
  - best BID after the print = the best price you could actually have sold at
Raw bars are kept in ib_bars so every number traces back to a real IBKR response.

Safety: only the bare API handshake is used (no account, position, order or execution
requests), every account/order call is blocked on the client, and only the market-data
methods in ALLOWED can be called.
Also tick "Read-Only API" in TWS (Global Configuration -> API -> Settings) so TWS itself
refuses orders. IBKR has no data for expired options, so expired picks are skipped.
"""

import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HOST = "127.0.0.1"
PORT = int(os.environ.get("IBKR_PORT", "7496"))
CLIENT_ID = 17
ALLOWED = {"qualifyContracts", "reqHistoricalData", "reqMarketDataType", "reqMktData", "cancelMktData"}
HIST_GAP_SEC = 10.5   # IBKR pacing: at most 60 historical requests per 10 minutes

SCHEMA = """
CREATE TABLE IF NOT EXISTS ib_bars (
    pick_id   INTEGER NOT NULL,
    kind      TEXT NOT NULL,            -- 'TRADES' or 'BID'
    minute_et TEXT NOT NULL,            -- 'YYYY-MM-DD HH:MM' (bar start, ET)
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (pick_id, kind, minute_et)
);
CREATE TABLE IF NOT EXISTS ib_stock_bars (
    ticker    TEXT NOT NULL,
    minute_et TEXT NOT NULL,            -- 'YYYY-MM-DD HH:MM' (bar start, ET), regular hours
    open REAL, high REAL, low REAL, close REAL, volume REAL, vwap REAL,
    PRIMARY KEY (ticker, minute_et)
);
CREATE TABLE IF NOT EXISTS ib_stock_days (
    ticker     TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    status     TEXT NOT NULL,           -- 'ok', 'no_contract', 'no_bars', 'partial'
    n_bars     INTEGER,
    fetched_utc TEXT NOT NULL,
    PRIMARY KEY (ticker, trade_date)
);
CREATE TABLE IF NOT EXISTS ib_greeks_path (
    pick_id   INTEGER NOT NULL,
    minute_et TEXT NOT NULL,            -- minute with real option trades, after the fill minute
    opt_price REAL,                     -- last option trade in that minute (IBKR TRADES bar close)
    spot      REAL,                     -- stock's last price in the same minute (IBKR bar close)
    iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
    PRIMARY KEY (pick_id, minute_et)
);
CREATE TABLE IF NOT EXISTS ib_entries (
    pick_id     INTEGER PRIMARY KEY,
    at_utc      TEXT NOT NULL,          -- when the quote was taken
    minute_et   TEXT,                   -- 'YYYY-MM-DD HH:MM' of the quote (ET)
    delay_sec   REAL,                   -- seconds after the Discord ping
    status      TEXT NOT NULL,          -- 'ok' (live quote), 'no_subscription', 'no_quote', 'no_contract'
    market_data_type INTEGER,           -- 1 live, 2 frozen, 3 delayed, 4 delayed-frozen
    bid REAL, ask REAL, last REAL,
    iv REAL, delta REAL, gamma REAL, theta REAL, vega REAL, und_price REAL
);
CREATE TABLE IF NOT EXISTS ib_stats (
    pick_id          INTEGER PRIMARY KEY,
    status           TEXT NOT NULL,     -- 'ok', 'no_contract', 'no_bars', 'expired', 'error'
    detail           TEXT,
    con_id           INTEGER,
    n_trade_bars     INTEGER,
    n_bid_bars       INTEGER,
    after_high       REAL,              -- highest trade after the fill minute
    after_low        REAL,
    after_high_time  TEXT,
    after_low_time   TEXT,
    day_close        REAL,              -- last trade bar close of the session
    after_bid_high   REAL,              -- best bid after the fill minute (sellable price)
    bid_close        REAL,
    max_gain_pct     REAL,              -- after_high / fill - 1
    max_loss_pct     REAL,              -- after_low / fill - 1
    close_pct        REAL,
    bid_max_gain_pct REAL,              -- after_bid_high / fill - 1
    bid_close_pct    REAL,
    fetched_utc      TEXT NOT NULL
);
"""


class NotAllowed(RuntimeError):
    pass


class IbData:
    """Read-only wrapper: exposes only the market-data calls in ALLOWED."""

    def __init__(self):
        from ib_async import IB
        self._ib = IB()
        # Block every account/order call at the wire level, so nothing (not even the library)
        # can send one. IB.connect() always asks for positions, so it is not used: the plain
        # client handshake below only opens the API session.
        client = self._ib.client
        for name in dir(client):
            if name.startswith(("placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions",
                                "reqPositions", "reqAccount", "reqOpenOrders", "reqAllOpenOrders",
                                "reqAutoOpenOrders", "reqCompletedOrders", "reqExecutions", "reqPnL",
                                "reqManagedAccts", "reqFamilyCodes", "reqUserInfo", "reqWshMetaData")):
                setattr(client, name, self._blocked(name))
        self._last_hist = 0.0
        self.idle_hook = None
        self.no_subscription = False   # set once IBKR says live option quotes aren't subscribed
        self._link_ok = True
        # TWS reports 1100 when its link to IBKR's servers drops, 1101/1102 when it's back
        self._ib.errorEvent += self._on_notice
        client.connect(HOST, PORT, CLIENT_ID, timeout=15)

    @staticmethod
    def _blocked(name):
        def refuse(*args, **kwargs):
            raise NotAllowed(f"{name} is blocked: this connection is market data only")
        return refuse

    def call(self, method, *args, **kwargs):
        if method not in ALLOWED:
            raise NotAllowed(f"{method} is not an allowed IBKR call (market data only)")
        if method == "reqHistoricalData":
            # pacing gap; the idle hook (live entry snapshots) runs while we wait
            while (wait := self._last_hist + HIST_GAP_SEC - time.time()) > 0:
                if self.idle_hook:
                    self.idle_hook()
                self._ib.sleep(min(wait, 2.0))
            self._last_hist = time.time()
        return getattr(self._ib, method)(*args, **kwargs)

    def _on_notice(self, req_id, code, msg, contract=None):
        if code in (354, 10091, 10089, 10090):  # market data not subscribed (delayed only)
            self.no_subscription = True
        if code == 1100:
            self._link_ok = False
        elif code in (1101, 1102):
            self._link_ok = True

    def data_ok(self):
        return self._ib.isConnected() and self._link_ok

    def close(self):
        self._ib.disconnect()


def available():
    """True if TWS is accepting API connections on this PC."""
    import socket
    try:
        with socket.create_connection((HOST, PORT), timeout=2):
            return True
    except OSError:
        return False


def greeks_path(db, pick_id):
    """IV and Greeks for every minute after the print in which the option really traded, from that
    minute's last option trade and last stock price (both IBKR). Minutes with no option trade are
    skipped rather than filled in. Needs the pick's option bars and the stock bars for its day."""
    import greeks as bs
    row = db.execute("SELECT ticker, trade_date, trade_time_et, strike, put_call, expiration FROM picks "
                     "WHERE id = ?", (pick_id,)).fetchone()
    if not row:
        return 0
    ticker, tdate, hhmm, strike, pc, exp = row
    expiry = datetime.combine(date.fromisoformat(exp), datetime.min.time(), ET).replace(hour=16)
    rows = db.execute(
        "SELECT o.minute_et, o.close, s.close FROM ib_bars o JOIN ib_stock_bars s "
        "ON s.ticker = ? AND s.minute_et = o.minute_et WHERE o.pick_id = ? AND o.kind = 'TRADES' "
        "AND o.volume > 0 AND o.close > 0 AND s.close > 0 AND o.minute_et > ? ORDER BY o.minute_et",
        (ticker, pick_id, f"{tdate} {hhmm}")).fetchall()
    out = []
    for minute, price, spot in rows:
        at = datetime.strptime(minute, "%Y-%m-%d %H:%M").replace(tzinfo=ET) + timedelta(minutes=1)  # bar end
        years = max((expiry - at).total_seconds(), 600) / (365 * 86400)
        iv = bs.implied_vol(price, spot, strike, years, pc == "CALL")
        if iv is None:
            continue
        g = bs.greeks(spot, strike, years, iv, pc == "CALL")
        out.append((pick_id, minute, price, spot, iv, g["delta"], g["gamma"], g["theta"], g["vega"]))
    db.execute("DELETE FROM ib_greeks_path WHERE pick_id = ?", (pick_id,))
    db.executemany("INSERT INTO ib_greeks_path VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", out)
    db.commit()
    return len(out)


def refresh_greeks(db, log=print):
    """Recompute print-time Greeks with IBKR's exact print-minute stock price (all picks that have
    it) and the minute-by-minute Greeks paths (picks with IBKR option bars). No API calls."""
    import pipeline as pl
    db.executescript(SCHEMA)
    ids = [r[0] for r in db.execute(
        "SELECT DISTINCT p.id FROM picks p JOIN ib_stock_bars s ON s.ticker = p.ticker "
        "AND s.minute_et = p.trade_date || ' ' || p.trade_time_et WHERE p.source != 'algo' "
        "AND COALESCE(p.spot_source, '') != 'ibkr_minute' "
        "UNION SELECT id FROM picks WHERE iv IS NULL AND spot_source IS NULL")]  # never computed yet
    for pid in ids:
        pl.compute_greeks(db, pid)
    paths = [r[0] for r in db.execute("SELECT pick_id FROM ib_stats WHERE status = 'ok'")]
    n = sum(greeks_path(db, pid) for pid in paths)
    log(f"IBKR Greeks: {len(ids)} picks re-priced with the exact print-minute stock price; "
        f"{n} minute-by-minute Greeks points for {len(paths)} picks")


LIVE_CYCLE_SEC = 300      # refresh today's picks every 5 minutes during the session
LIVE_MAX_PICKS = 12       # newest picks on your tickers (keeps a cycle inside IBKR's pacing limit)
LIVE_CLIENT_ID = 18       # separate from the nightly connection


def _store_today(db, ib, contract, kind, pick_id=None, ticker=None):
    """Today's 1-minute bars so far, minus the still-forming current minute."""
    now = datetime.now(ET)
    forming = now.strftime("%Y-%m-%d %H:%M")
    rows = [r for r in _bars(ib, contract, now.date(), kind, with_vwap=ticker is not None) if r[0] < forming]
    if ticker is not None:
        db.executemany("INSERT OR REPLACE INTO ib_stock_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       [(ticker, *r) for r in rows])
    else:
        db.executemany("INSERT OR REPLACE INTO ib_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       [(pick_id, kind, *r) for r in rows])
    db.commit()
    return len(rows)


ENTRY_MAX_AGE_SEC = 600   # take an entry quote only for pings from the last 10 minutes
ENTRY_WAIT_SEC = 6        # how long to wait for a live bid/ask


def _num(x):
    import math
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def record_entries(db, ib, contracts, log=print):
    """Your real entry: the live bid/ask (and IBKR's own IV/Greeks) right after each Discord ping.
    Needs the OPRA subscription; without it the row says 'no_subscription' and grading falls back
    to the whale's fill."""
    from ib_async import Option
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ENTRY_MAX_AGE_SEC)).isoformat(timespec="seconds")
    todo = db.execute("SELECT p.id, p.ticker, p.strike, p.put_call, p.expiration, p.pinged_utc FROM picks p "
                      "LEFT JOIN ib_entries e ON e.pick_id = p.id WHERE p.pinged_utc >= ? AND e.pick_id IS NULL",
                      (cutoff,)).fetchall()
    for pid, ticker, strike, pc, exp, pinged in todo:
        now_utc = datetime.now(timezone.utc)
        row = dict(pick_id=pid, at_utc=now_utc.isoformat(timespec="seconds"),
                   minute_et=now_utc.astimezone(ET).strftime("%Y-%m-%d %H:%M"),
                   delay_sec=(now_utc - datetime.fromisoformat(pinged)).total_seconds())
        c = contracts.get(("OPT", pid))
        if c is None:
            c = Option(ticker, exp.replace("-", ""), strike, pc[0], "SMART", currency="USD")
            c = c if ib.call("qualifyContracts", c) and c.conId else None
            contracts[("OPT", pid)] = c
        if c is None:
            row["status"] = "no_contract"
        else:
            ib.no_subscription = False
            ib.call("reqMarketDataType", 1)
            t = ib.call("reqMktData", c, "106", False, False)
            end = time.time() + ENTRY_WAIT_SEC
            while time.time() < end and not ib.no_subscription and not (
                    (_num(t.bid) or 0) > 0 and (_num(t.ask) or 0) > 0):
                ib._ib.sleep(0.25)
            ib.call("cancelMktData", c)
            g = t.modelGreeks
            row.update(market_data_type=t.marketDataType, bid=_num(t.bid), ask=_num(t.ask), last=_num(t.last),
                       iv=_num(g.impliedVol) if g else None, delta=_num(g.delta) if g else None,
                       gamma=_num(g.gamma) if g else None, theta=_num(g.theta) if g else None,
                       vega=_num(g.vega) if g else None, und_price=_num(g.undPrice) if g else None)
            live = (row["bid"] or 0) > 0 and (row["ask"] or 0) > 0 and t.marketDataType == 1
            row["status"] = "ok" if live else ("no_subscription" if ib.no_subscription else "no_quote")
        cols = list(row)
        db.execute(f"INSERT OR REPLACE INTO ib_entries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   tuple(row.values()))
        db.commit()
        log(f"{datetime.now(ET):%H:%M:%S} IBKR entry {ticker} {strike:g}{pc[0]} {exp}: {row['status']}"
            + (f" bid {row['bid']} / ask {row['ask']} ({row['delay_sec']:.0f}s after the ping)"
               if row["status"] == "ok" else ""))


def run_live(db, in_session, next_open, my_tickers, log=print):
    """During market hours: record your entry quote right after each ping, and every 5 minutes pull
    today's 1-minute bars for all your tickers + SPY (market context for live picks) and for the
    newest picks (minute-by-minute Greeks). Runs as its own process so it never slows the logger.
    Read-only market data, like everything in this module."""
    from ib_async import Option, Stock
    global CLIENT_ID
    CLIENT_ID = LIVE_CLIENT_ID
    db.executescript(SCHEMA)
    ib, contracts = None, {}
    busy = {"entries": False}

    def entries_hook():
        if busy["entries"] or ib is None:
            return
        busy["entries"] = True
        try:
            record_entries(db, ib, contracts, log)
        except NotAllowed:
            raise
        except Exception as e:
            log(f"{datetime.now(ET):%H:%M:%S} IBKR entry check failed: {type(e).__name__}: {e}")
        finally:
            busy["entries"] = False

    while True:
        now = datetime.now(ET)
        if not in_session(now):
            if ib:
                ib.close()
                ib, contracts = None, {}
            nxt = next_open(now)
            log(f"{now:%H:%M:%S} IBKR live: market closed, next session {nxt:%a %H:%M} ET")
            time.sleep(max(60, (nxt - datetime.now(ET)).total_seconds() + 60))
            continue
        started = time.time()
        try:
            if ib is None or not ib.data_ok():
                if ib:
                    ib.close()
                if not available():
                    log(f"{now:%H:%M:%S} IBKR live: TWS not open - retrying in 5 min")
                    time.sleep(LIVE_CYCLE_SEC)
                    continue
                ib, contracts = IbData(), {}
                ib.idle_hook = entries_hook
            entries_hook()
            marks = ",".join("?" * len(my_tickers))
            picks = db.execute(
                f"SELECT id, ticker, strike, put_call, expiration FROM picks WHERE trade_date = ? "
                f"AND ticker IN ({marks}) ORDER BY trade_time_et DESC LIMIT ?",
                (now.date().isoformat(), *sorted(my_tickers), LIVE_MAX_PICKS)).fetchall()
            for ticker in sorted(set(my_tickers) | {"SPY"}):
                key = ("STK", ticker)
                if key not in contracts:
                    s = Stock(ticker, "SMART", "USD")
                    contracts[key] = s if ib.call("qualifyContracts", s) and s.conId else None
                if contracts[key]:
                    _store_today(db, ib, contracts[key], "TRADES", ticker=ticker)
            points = 0
            for pid, ticker, strike, pc, exp in picks:
                key = ("OPT", pid)
                if key not in contracts:
                    c = Option(ticker, exp.replace("-", ""), strike, pc[0], "SMART", currency="USD")
                    contracts[key] = c if ib.call("qualifyContracts", c) and c.conId else None
                if contracts[key]:
                    _store_today(db, ib, contracts[key], "TRADES", pick_id=pid)
                    points += greeks_path(db, pid)
            log(f"{datetime.now(ET):%H:%M:%S} IBKR live: {len(picks)} picks updated, {points} Greeks points "
                f"({time.time() - started:.0f}s)")
        except NotAllowed:
            raise
        except Exception as e:
            log(f"{datetime.now(ET):%H:%M:%S} IBKR live: cycle failed: {type(e).__name__}: {e}")
            if ib:
                ib.close()
            ib, contracts = None, {}
        # until the next cycle, keep checking for new pings every few seconds
        while time.time() - started < LIVE_CYCLE_SEC:
            if ib is not None and ib.data_ok():
                entries_hook()
                ib._ib.sleep(3)
            else:
                time.sleep(3)


def price_underlyings(db, deadline=None, log=print):
    """1-minute stock bars for every (ticker, day) that has a pick - works for any past date,
    including picks whose options have expired. Gives the exact stock price at each print."""
    from ib_async import Stock
    db.executescript(SCHEMA)
    if not available():
        log("IBKR: TWS is not accepting API connections - skipped (open TWS to enable)")
        return {}
    now = datetime.now(ET)
    last_day = now.date() if now.hour * 60 + now.minute >= 16 * 60 + 5 else now.date() - timedelta(days=1)
    todo = db.execute(
        "SELECT DISTINCT p.ticker, p.trade_date FROM (SELECT ticker, trade_date FROM picks UNION "
        "SELECT 'SPY', trade_date FROM picks) p LEFT JOIN ib_stock_days d "  # SPY = market context
        "ON d.ticker = p.ticker AND d.trade_date = p.trade_date WHERE p.trade_date <= ? "
        "AND (d.status IS NULL OR d.status = 'partial') ORDER BY p.trade_date, p.ticker",
        (last_day.isoformat(),)).fetchall()
    if not todo:
        log("IBKR: stock bars up to date")
        return {}
    log(f"IBKR: stock 1-minute bars for {len(todo)} ticker-days (~{len(todo) * HIST_GAP_SEC / 60:.0f} min)")
    counts, contracts = {}, {}
    ib = IbData()
    try:
        for i, (ticker, tdate) in enumerate(todo, 1):
            if deadline and time.time() > deadline:
                log(f"IBKR: stopped at {i - 1}/{len(todo)} (deadline)")
                break
            if not ib.data_ok():
                log(f"IBKR: TWS lost its link to IBKR's servers - stopped at {i - 1}/{len(todo)}")
                break
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if ticker not in contracts:
                s = Stock(ticker, "SMART", "USD")
                contracts[ticker] = s if ib.call("qualifyContracts", s) and s.conId else None
            s = contracts[ticker]
            if s is None:  # index options (NDXP, RUTW, SPXW...) have no stock - their index is a different contract
                st, rows = "no_contract", []
            else:
                rows = _bars(ib, s, date.fromisoformat(tdate), "TRADES", with_vwap=True)
                st = "ok" if len(rows) >= 200 else ("partial" if not rows else "no_bars")
            db.executemany("INSERT OR REPLACE INTO ib_stock_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           [(ticker, *r) for r in rows])
            db.execute("INSERT OR REPLACE INTO ib_stock_days VALUES (?, ?, ?, ?, ?)",
                       (ticker, tdate, st, len(rows), stamp))
            db.commit()
            counts[st] = counts.get(st, 0) + 1
            if i % 20 == 0 or i == len(todo):
                log(f"  stock bars {i}/{len(todo)}  {counts}")
    finally:
        ib.close()
    return counts


def _bars(ib, contract, trade_date, kind, with_vwap=False):
    end = datetime.combine(trade_date, datetime.min.time(), ET).replace(hour=16, minute=5)
    bars = ib.call("reqHistoricalData", contract,
                   endDateTime=end.astimezone(timezone.utc).strftime("%Y%m%d-%H:%M:%S"),
                   durationStr="1 D", barSizeSetting="1 min", whatToShow=kind, useRTH=True, formatDate=2)
    out = []
    for b in bars or []:
        t = b.date if isinstance(b.date, datetime) else datetime.fromtimestamp(int(b.date), timezone.utc)
        t = t.astimezone(ET)
        if t.date() == trade_date:
            out.append((t.strftime("%Y-%m-%d %H:%M"), b.open, b.high, b.low, b.close, b.volume)
                       + ((b.average,) if with_vwap else ()))
    return out


def _price_pick(db, ib, pick):
    from ib_async import Option
    pid, ticker, strike, pc, exp, tdate, hhmm, fill = pick
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def save(status, detail=None, **v):
        cols = ["pick_id", "status", "detail", "fetched_utc"] + list(v)
        db.execute(f"INSERT OR REPLACE INTO ib_stats ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   (pid, status, detail, now, *v.values()))
        db.commit()
        return status

    if date.fromisoformat(exp) < datetime.now(ET).date():
        return save("expired", "IBKR has no data for expired options")
    c = Option(ticker, exp.replace("-", ""), strike, pc[0], "SMART", currency="USD")
    if not ib.call("qualifyContracts", c) or not c.conId:
        return save("no_contract", f"IBKR doesn't list {ticker} {strike:g}{pc[0]} {exp}")
    td = date.fromisoformat(tdate)
    trades, bids = _bars(ib, c, td, "TRADES"), _bars(ib, c, td, "BID")
    for kind, rows in (("TRADES", trades), ("BID", bids)):
        db.executemany("INSERT OR REPLACE INTO ib_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       [(pid, kind, *r) for r in rows])
    fill_min = f"{tdate} {hhmm}"
    after = [r for r in trades if r[0] > fill_min]       # bars that start after the fill minute
    bid_after = [r for r in bids if r[0] > fill_min and r[2] > 0]
    if not trades and len(bids) >= 300:  # quoted all session but never traded: final, not a timeout
        return save("no_trades", f"no trades all session ({len(bids)} bid bars)", con_id=c.conId,
                    n_trade_bars=0, n_bid_bars=len(bids))
    if not trades or not bids:  # a request timed out or TWS lost its server link: retry later
        return save("partial", f"{len(trades)} trade bars, {len(bids)} bid bars", con_id=c.conId,
                    n_trade_bars=len(trades), n_bid_bars=len(bids))
    if not after:
        return save("no_bars", f"{len(trades)} trade bars, none after {hhmm}", con_id=c.conId,
                    n_trade_bars=len(trades), n_bid_bars=len(bids))
    hi = max(after, key=lambda r: r[2])
    lo = min(after, key=lambda r: r[3])
    bid_hi = max(bid_after, key=lambda r: r[2]) if bid_after else None
    bid_close = bids[-1][4] if bids else None
    pct = lambda x: None if x is None else x / fill - 1
    return save("ok", con_id=c.conId, n_trade_bars=len(trades), n_bid_bars=len(bids),
                after_high=hi[2], after_low=lo[3], after_high_time=hi[0][11:], after_low_time=lo[0][11:],
                day_close=trades[-1][4], after_bid_high=bid_hi[2] if bid_hi else None, bid_close=bid_close,
                max_gain_pct=pct(hi[2]), max_loss_pct=pct(lo[3]), close_pct=pct(trades[-1][4]),
                bid_max_gain_pct=pct(bid_hi[2]) if bid_hi else None, bid_close_pct=pct(bid_close))


def price_picks(db, only_missing=True, deadline=None, log=print):
    """Record IBKR after-print stats for every finished-session pick whose contract is still listed."""
    db.executescript(SCHEMA)
    if not available():
        log("IBKR: TWS is not accepting API connections - skipped (open TWS to enable)")
        return {}
    today = datetime.now(ET)
    done_today = today.hour * 60 + today.minute >= 16 * 60 + 5
    last_day = today.date() if done_today else today.date() - timedelta(days=1)
    picks = db.execute(
        "SELECT p.id, p.ticker, p.strike, p.put_call, p.expiration, p.trade_date, p.trade_time_et, p.fill_price "
        "FROM picks p LEFT JOIN ib_stats s ON s.pick_id = p.id WHERE p.trade_date <= ? AND p.expiration >= ? "
        + ("AND (s.pick_id IS NULL OR s.status IN ('error', 'partial')) " if only_missing else "")
        + "ORDER BY p.trade_date, p.trade_time_et", (last_day.isoformat(), today.date().isoformat())).fetchall()
    if not picks:
        log("IBKR: no picks to price")
        return {}
    log(f"IBKR: pricing {len(picks)} picks from 1-minute bars (~{len(picks) * 2 * HIST_GAP_SEC / 60:.0f} min)")
    counts = {}
    ib = IbData()
    try:
        for i, pick in enumerate(picks, 1):
            if deadline and time.time() > deadline:
                log(f"IBKR: stopped at {i - 1}/{len(picks)} (deadline)")
                break
            if not ib.data_ok():
                log(f"IBKR: TWS lost its link to IBKR's servers - stopped at {i - 1}/{len(picks)}, "
                    "the rest retries next run")
                break
            try:
                st = _price_pick(db, ib, pick)
            except NotAllowed:
                raise
            except Exception as e:  # one bad contract mustn't stop the rest
                db.execute("INSERT OR REPLACE INTO ib_stats (pick_id, status, detail, fetched_utc) VALUES (?, ?, ?, ?)",
                           (pick[0], "error", f"{type(e).__name__}: {e}"[:300],
                            datetime.now(timezone.utc).isoformat(timespec="seconds")))
                db.commit()
                st = "error"
            counts[st] = counts.get(st, 0) + 1
            if st == "ok":
                r = db.execute("SELECT max_gain_pct, bid_max_gain_pct, max_loss_pct, close_pct FROM ib_stats "
                               "WHERE pick_id = ?", (pick[0],)).fetchone()
                f = lambda x: f"{x:+.0%}" if x is not None else "-"
                log(f"  ibkr {pick[1]} {pick[2]:g}{pick[3][0]} {pick[4]} @ {pick[7]} ({pick[5]} {pick[6]}): "
                    f"after-print best {f(r[0])} (bid {f(r[1])}) worst {f(r[2])} close {f(r[3])}")
            else:
                log(f"  ibkr {pick[1]} {pick[2]:g}{pick[3][0]} {pick[4]}: {st}")
    finally:
        ib.close()
    log(f"IBKR: {counts}")
    return counts
