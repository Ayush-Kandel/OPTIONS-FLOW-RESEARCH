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
CREATE TABLE IF NOT EXISTS ib_contract_bars (   -- every session from the print day to expiry
    ticker TEXT NOT NULL, strike REAL NOT NULL, put_call TEXT NOT NULL, expiration TEXT NOT NULL,
    kind      TEXT NOT NULL,            -- 'TRADES' or 'BID'
    minute_et TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (ticker, strike, put_call, expiration, kind, minute_et)
);
CREATE TABLE IF NOT EXISTS ib_live_quotes (     -- streamed during market hours, one row per minute
    ticker TEXT NOT NULL, strike REAL NOT NULL, put_call TEXT NOT NULL, expiration TEXT NOT NULL,
    minute_et TEXT NOT NULL,
    bid REAL, ask REAL, last REAL, volume REAL, iv REAL, delta REAL, und_price REAL,
    PRIMARY KEY (ticker, strike, put_call, expiration, minute_et)
);
CREATE TABLE IF NOT EXISTS ib_ping_alerts (     -- live Discord follow-ups already sent
    pick_id INTEGER NOT NULL, level TEXT NOT NULL, sent_utc TEXT NOT NULL,
    PRIMARY KEY (pick_id, level)
);
-- companion contracts of big whales on your tickers, so spread strategies can be tested on real quotes:
-- 'vertical' = next strike further out (same expiry), 'calendar' = same strike, next expiry,
-- 'straddle' = the other side (put for a call) at the same strike and expiry
CREATE TABLE IF NOT EXISTS pick_legs (
    pick_id INTEGER NOT NULL, leg TEXT NOT NULL,
    ticker TEXT, strike REAL, put_call TEXT, expiration TEXT, con_id INTEGER,
    status TEXT NOT NULL,                 -- 'ok', 'no_contract', 'no_bars'
    fetched_utc TEXT NOT NULL,
    PRIMARY KEY (pick_id, leg)
);
CREATE TABLE IF NOT EXISTS ib_leg_bars (
    pick_id INTEGER NOT NULL, leg TEXT NOT NULL,
    kind TEXT NOT NULL,                   -- 'BID' or 'ASK'
    minute_et TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (pick_id, leg, kind, minute_et)
);
CREATE TABLE IF NOT EXISTS ib_oi (
    ticker TEXT NOT NULL, strike REAL NOT NULL, put_call TEXT NOT NULL, expiration TEXT NOT NULL,
    snap_date     TEXT NOT NULL,        -- ET date of the snapshot (OI = as of the previous close)
    open_interest REAL,
    at_utc        TEXT NOT NULL,
    PRIMARY KEY (ticker, strike, put_call, expiration, snap_date)
);
CREATE TABLE IF NOT EXISTS ib_stock_iv (
    ticker TEXT NOT NULL,
    day    TEXT NOT NULL,               -- trading day
    iv     REAL,                        -- IBKR's 30-day implied volatility of the stock, at that day's close
    PRIMARY KEY (ticker, day)
);
CREATE TABLE IF NOT EXISTS ib_exp_stats (
    pick_id      INTEGER PRIMARY KEY,
    status       TEXT NOT NULL,         -- 'ok', 'expired' (gone from IBKR), 'no_bars', 'partial', 'error'
    detail       TEXT,
    exp_date     TEXT,
    n_trade_bars INTEGER, n_bid_bars INTEGER,
    open REAL, high REAL, low REAL, close REAL, vwap REAL, bid_close REAL, bid_high REAL,
    high_pct REAL, low_pct REAL, close_pct REAL, vwap_pct REAL, bid_close_pct REAL, bid_high_pct REAL,
    fetched_utc  TEXT NOT NULL
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


EXTRA_COLUMNS = {
    "ib_stats": {"n_ask_bars": "INTEGER", "spread_at_print": "REAL", "spread_pct_at_print": "REAL",
                 "median_spread_pct": "REAL"},
    "ib_entries": {"open_interest": "REAL"},
}

# Index option classes are listed under their index's symbol (an NDXP option is an NDX option of
# class NDXP); asking IBKR for symbol 'NDXP' finds nothing (50 picks were 'no_contract' on Oct 2).
INDEX_CLASSES = {"SPXW": "SPX", "SPX": "SPX", "NDXP": "NDX", "NDX": "NDX", "RUTW": "RUT", "RUT": "RUT",
                 "XSP": "XSP", "VIXW": "VIX", "VIX": "VIX"}


def _option(ticker, exp, strike, put_call):
    from ib_async import Option
    parent = INDEX_CLASSES.get(ticker)
    if parent:
        return Option(parent, exp.replace("-", ""), strike, put_call[0], "SMART", currency="USD",
                      tradingClass=ticker)
    return Option(ticker, exp.replace("-", ""), strike, put_call[0], "SMART", currency="USD")


def ensure_schema(db):
    db.executescript(SCHEMA)
    for table, cols in EXTRA_COLUMNS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in cols.items():
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    db.commit()


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
        # 354 = not subscribed at all. (10091 "part of the data needs another subscription" is about
        # the underlying stock's feed - the option's own bid/ask still arrive, so it isn't counted.)
        if code in (354, 10089, 10090):
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
    ensure_schema(db)
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


LEG_STRIKE_STEPS = [0.5, 1, 2.5, 5, 10, 25, 50]   # tried in order: the nearest listed strike wins
LEGS_LOOKBACK_DAYS = 3


def _find_leg(ib, tk, k, pc, exp, leg):
    """The companion contract IBKR actually lists, or None."""
    tries = []
    if leg == "vertical":
        sign = 1 if pc == "CALL" else -1
        tries = [(k + sign * step, pc, exp) for step in LEG_STRIKE_STEPS]
    elif leg == "straddle":
        tries = [(k, "PUT" if pc == "CALL" else "CALL", exp)]
    elif leg == "calendar":
        d = date.fromisoformat(exp)
        tries = [(k, pc, (d + timedelta(days=i)).isoformat()) for i in range(1, 15)
                 if (d + timedelta(days=i)).weekday() < 5]
    for strike, right, expiry in tries:
        c = _option(tk, expiry, strike, right)
        if ib.call("qualifyContracts", c) and c.conId:
            return c, strike, right, expiry
    return None


def price_legs(db, deadline=None, log=print):
    """Real 1-minute BID/ASK bars of each big whale's companion contracts (vertical, calendar,
    straddle legs) on its print day - recorded now so spread strategies can be backtested on real
    prices later. Your tickers' pings and $350K+ noteworthy picks from the last few days."""
    import pipeline
    ensure_schema(db)
    if not available():
        return {}
    today = datetime.now(ET).date()
    marks = ",".join("?" * len(pipeline.MY_TICKERS))
    todo = db.execute(
        f"SELECT p.id, p.ticker, p.strike, p.put_call, p.expiration, p.trade_date FROM picks p WHERE p.ticker IN ({marks}) "
        "AND (p.pinged_utc IS NOT NULL OR (p.score IS NOT NULL AND p.premium >= ?)) AND p.trade_date >= ? "
        "AND p.expiration >= ? AND NOT EXISTS (SELECT 1 FROM pick_legs l WHERE l.pick_id = p.id) "
        "ORDER BY p.trade_date DESC, p.premium DESC",
        (*sorted(pipeline.MY_TICKERS), pipeline.NOTEWORTHY_MIN_PREMIUM,
         (today - timedelta(days=LEGS_LOOKBACK_DAYS)).isoformat(), (today - timedelta(days=1)).isoformat())).fetchall()
    if not todo:
        return {}
    log(f"IBKR: spread legs for {len(todo)} picks (~{len(todo) * 6 * HIST_GAP_SEC / 60:.0f} min)")
    counts, ib = {}, IbData()
    try:
        for pid, tk, k, pc, exp, td in todo:
            if deadline and time.time() > deadline:
                log("IBKR: spread legs stopped (deadline)")
                break
            if not ib.data_ok():
                log("IBKR: TWS lost its link - spread legs retry next run")
                break
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for leg in ("vertical", "calendar", "straddle"):
                found = _find_leg(ib, tk, k, pc, exp, leg)
                status = "no_contract"
                if found:
                    c, strike, right, expiry = found
                    rows = []
                    for kind in ("BID", "ASK"):
                        rows += [(pid, leg, kind, *r) for r in _bars(ib, c, date.fromisoformat(td), kind)]
                    db.executemany("INSERT OR REPLACE INTO ib_leg_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                    status = "ok" if rows else "no_bars"
                    db.execute("INSERT OR REPLACE INTO pick_legs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               (pid, leg, tk, strike, right, expiry, c.conId, status, now))
                else:
                    db.execute("INSERT OR REPLACE INTO pick_legs (pick_id, leg, status, fetched_utc) VALUES (?, ?, ?, ?)",
                               (pid, leg, status, now))
                db.commit()
                counts[status] = counts.get(status, 0) + 1
    finally:
        ib.close()
    log(f"IBKR spread legs: {counts}")
    return counts


def price_spreads(db, deadline=None, log=print):
    """Backfill the ask side (spread) for IBKR-priced picks whose contract is still listed."""
    from ib_async import Option
    ensure_schema(db)
    if not available():
        return {}
    today = datetime.now(ET).date().isoformat()
    todo = db.execute("SELECT p.id, p.ticker, p.strike, p.put_call, p.expiration, p.trade_date, p.trade_time_et "
                      "FROM ib_stats s JOIN picks p ON p.id = s.pick_id WHERE s.status = 'ok' "
                      "AND s.n_ask_bars IS NULL AND p.expiration >= ? ORDER BY p.trade_date", (today,)).fetchall()
    if not todo:
        return {}
    log(f"IBKR: bid/ask spreads for {len(todo)} picks (~{len(todo) * HIST_GAP_SEC / 60:.0f} min)")
    done = 0
    ib = IbData()
    try:
        for pid, ticker, strike, pc, exp, tdate, hhmm in todo:
            if deadline and time.time() > deadline or not ib.data_ok():
                break
            c = _option(ticker, exp, strike, pc)
            if ib.call("qualifyContracts", c) and c.conId:
                _price_spread(db, ib, pid, c, date.fromisoformat(tdate), f"{tdate} {hhmm}")
                done += 1
    finally:
        ib.close()
    log(f"IBKR: spreads recorded for {done} picks")
    return {"spreads": done}


HOLD_MAX_DAYS = 5   # one IBKR request returns up to 5 sessions of 1-minute bars


def price_holding_days(db, deadline=None, log=print):
    """Every session's 1-minute trades and bids for each pick contract still listed, from the day
    after its first print through the last finished session (the days between print and expiry).
    Stored once per contract, shared by all picks on it."""
    from ib_async import Option
    ensure_schema(db)
    if not available():
        log("IBKR: TWS is not accepting API connections - holding days skipped")
        return {}
    now = datetime.now(ET)
    last_day = now.date() if now.hour * 60 + now.minute >= 16 * 60 + 5 else now.date() - timedelta(days=1)
    contracts = db.execute(
        "SELECT ticker, strike, put_call, expiration, MIN(trade_date) FROM picks WHERE expiration >= ? "
        "GROUP BY ticker, strike, put_call, expiration", (last_day.isoformat(),)).fetchall()
    todo = []
    for tk, k, pc, exp, first in contracts:
        have = db.execute("SELECT MAX(substr(minute_et, 1, 10)) FROM ib_contract_bars WHERE ticker = ? "
                          "AND strike = ? AND put_call = ? AND expiration = ? AND kind = 'BID'",
                          (tk, k, pc, exp)).fetchone()[0]
        start = max(first, have) if have else first
        if start < last_day.isoformat():
            todo.append((tk, k, pc, exp, start))
    if not todo:
        return {}
    log(f"IBKR: holding-day prices for {len(todo)} contracts (~{len(todo) * 2 * HIST_GAP_SEC / 60:.0f} min)")
    n = 0
    ib = IbData()
    try:
        for tk, k, pc, exp, start in todo:
            if deadline and time.time() > deadline or not ib.data_ok():
                log(f"IBKR: holding days stopped at {n}/{len(todo)} - the rest continues next run")
                break
            c = _option(tk, exp, k, pc)
            if not (ib.call("qualifyContracts", c) and c.conId):
                continue
            days = min(HOLD_MAX_DAYS, max(1, (last_day - date.fromisoformat(start)).days + 1))
            end = datetime.combine(last_day, datetime.min.time(), ET).replace(hour=16, minute=5)
            for kind in ("TRADES", "BID"):
                bars = ib.call("reqHistoricalData", c, endDateTime=end.astimezone(timezone.utc).strftime("%Y%m%d-%H:%M:%S"),
                               durationStr=f"{days} D", barSizeSetting="1 min", whatToShow=kind, useRTH=True, formatDate=2)
                rows = []
                for b in bars or []:
                    t = b.date if isinstance(b.date, datetime) else datetime.fromtimestamp(int(b.date), timezone.utc)
                    m = t.astimezone(ET).strftime("%Y-%m-%d %H:%M")
                    if m[:10] > start:   # sessions AFTER the print day (the print day itself is in ib_bars)
                        rows.append((tk, k, pc, exp, kind, m, b.open, b.high, b.low, b.close, b.volume))
                db.executemany("INSERT OR REPLACE INTO ib_contract_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                db.commit()
            n += 1
    finally:
        ib.close()
    log(f"IBKR: holding-day prices recorded for {n} contracts")
    return {"holding_days": n}


LIVE_STREAM_MAX = 70     # contracts streamed live at once; + OI batch (20) + entry quote stays under
                         # IBKR's 100 market-data lines
WHALE_STREAM_MAX = 20    # of those, at most this many whale-watch sightings (newest first)


class LiveQuotes:
    """Streams live bid/ask/last for the most important open pick contracts during market hours
    (OPRA) and saves the latest values once a minute. Priority: pinged contracts, then your
    tickers' newest picks, then everything else still listed."""

    def __init__(self, db, ib, my_tickers):
        self.db, self.ib, self.my = db, ib, set(my_tickers)
        self.streams, self.last_minute = {}, None

    def refresh(self):
        today = datetime.now(ET).date().isoformat()
        marks = ",".join("?" * len(self.my))
        picks = self.db.execute(
            f"SELECT ticker, strike, put_call, expiration, MAX(pinged_utc IS NOT NULL) AS pinged, "
            f"MAX(ticker IN ({marks})) AS mine, MAX(trade_date || trade_time_et) AS latest FROM picks "
            f"WHERE expiration >= ? GROUP BY ticker, strike, put_call, expiration "
            f"ORDER BY pinged DESC, mine DESC, latest DESC LIMIT ?",
            (*sorted(self.my), today, LIVE_STREAM_MAX)).fetchall()
        # whales the whale watch spotted today on your tickers: priced from first sight,
        # before Trade Echo's scored list (and our ping) catch up
        whales = [tuple(r) for r in self.db.execute(
            f"SELECT p.ticker, p.strike, p.put_call, p.expiration FROM prints p JOIN polls q ON q.id = p.poll_id "
            f"WHERE q.kind = 'whale' AND p.trade_date = ? AND p.ticker IN ({marks}) AND p.expiration >= ? "
            f"GROUP BY 1, 2, 3, 4 ORDER BY MAX(p.trade_time_et) DESC LIMIT ?",
            (today, *sorted(self.my), today, WHALE_STREAM_MAX))]
        ordered = [tuple(r[:4]) for r in picks if r[4]] + whales + [tuple(r[:4]) for r in picks if not r[4]]
        wanted = list(dict.fromkeys(ordered))[:LIVE_STREAM_MAX]
        for key in [k for k in self.streams if k not in wanted]:
            self.ib.call("cancelMktData", self.streams.pop(key)[0])
        new = [k for k in wanted if k not in self.streams]
        if new:
            self.ib.call("reqMarketDataType", 1)
        for key in new:
            tk, k, pc, exp = key
            c = _option(tk, exp, k, pc)
            if self.ib.call("qualifyContracts", c) and c.conId:
                self.streams[key] = (c, self.ib.call("reqMktData", c, "100,106", False, False))
        return len(self.streams)

    def record(self):
        """Once per minute: save each stream's latest values under the minute that just ended."""
        now = datetime.now(ET)
        minute = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
        if minute == self.last_minute or not self.streams:
            return 0
        self.last_minute = minute
        rows = []
        for key, (c, t) in self.streams.items():
            g = t.modelGreeks
            bid, ask, last = _num(t.bid), _num(t.ask), _num(t.last)
            if (bid or 0) <= 0 and (ask or 0) <= 0 and not last:
                continue
            rows.append((*key, minute, bid if bid and bid > 0 else None, ask if ask and ask > 0 else None, last,
                         _num(t.volume), _num(g.impliedVol) if g else None, _num(g.delta) if g else None,
                         _num(g.undPrice) if g else None))
        self.db.executemany("INSERT OR REPLACE INTO ib_live_quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.db.commit()
        return len(rows)

    def close(self):
        for c, _ in self.streams.values():
            try:
                self.ib.call("cancelMktData", c)
            except Exception:
                pass
        self.streams = {}


ALERT_LEVELS = [(1.00, "+100%"), (0.50, "+50%"), (0.30, "+30%"), (-0.50, "-50%")]
SUMMARY_EVERY_MIN = 15
# Entry used for live tracking and backtests: "whale" = the whale's fill at the print time (for now,
# while latency is worked on); "ask" = your real ask right after the ping (still recorded either way)
ENTRY_MODE = "whale"


def ping_values(db):
    """Today's pings: entry (the whale's fill at the print time, or your ask after the ping when
    ENTRY_MODE = 'ask'), the latest streamed bid (what you could sell at now) and the best bid since."""
    today = datetime.now(ET).date().isoformat()
    out = []
    for pid, t, tk, k, pc, exp, fill, ask, emin in db.execute(
            "SELECT p.id, p.trade_time_et, p.ticker, p.strike, p.put_call, p.expiration, p.fill_price, e.ask, "
            "e.minute_et FROM picks p LEFT JOIN ib_entries e ON e.pick_id = p.id AND e.status = 'ok' "
            "WHERE p.trade_date = ? AND p.pinged_utc IS NOT NULL ORDER BY p.trade_time_et", (today,)).fetchall():
        if ENTRY_MODE != "ask":
            ask = emin = None
        entry = ask or fill
        q = db.execute("SELECT minute_et, bid FROM ib_live_quotes WHERE ticker = ? AND strike = ? AND put_call = ? "
                       "AND expiration = ? AND minute_et >= ? AND bid > 0 ORDER BY minute_et",
                       (tk, k, pc, exp, emin or f"{today} {t}")).fetchall()
        out.append(dict(pid=pid, time=t, name=f"{tk} ${k:g} {'CALL' if pc == 'CALL' else 'PUT'} {exp}", entry=entry,
                        from_fill=ask is None, bid=q[-1][1] if q else None, at=q[-1][0][11:] if q else None,
                        best=max(r[1] for r in q) if q else None))
    return out


def ping_updates(db, state, log=print):
    """Discord follow-ups on today's pings (Herald channel): an alert the first time a ping's bid
    crosses +30/+50/+100% or -50% from your entry, and a summary of all open pings every 15 min."""
    import pipeline as pl
    vals = [v for v in ping_values(db) if v["bid"]]
    now = datetime.now(timezone.utc)
    for v in vals:
        pnl = v["bid"] / v["entry"] - 1
        for level, label in ALERT_LEVELS:
            hit = pnl >= level if level > 0 else pnl <= level
            if hit and not db.execute("SELECT 1 FROM ib_ping_alerts WHERE pick_id = ? AND level = ?",
                                      (v["pid"], label)).fetchone():
                # mark every lower level as sent too, so a jump straight to +100% posts once
                for lv, lb in ALERT_LEVELS:
                    if (lv > 0 and level > 0 and lv <= level) or lb == label:
                        db.execute("INSERT OR IGNORE INTO ib_ping_alerts VALUES (?, ?, ?)",
                                   (v["pid"], lb, now.isoformat(timespec="seconds")))
                db.commit()
                icon = "🚀" if level > 0 else "🔻"
                src = f"whale's fill at {v['time']} ET" if v["from_fill"] else "your ask at the ping"
                pl.discord_send(f"{icon} **{v['name']}** is **{pnl:+.0%}**: bid ${v['bid']:.2f} vs "
                                f"${v['entry']:.2f} ({src}). _Live IBKR quote, not advice._")
                log(f"{datetime.now(ET):%H:%M:%S} alert {v['name']} {label} ({pnl:+.0%})")
                break
    if vals and (state.get("summary") is None or (now - state["summary"]).total_seconds() >= SUMMARY_EVERY_MIN * 60):
        state["summary"] = now
        lines = [f"📡 **Live ping tracker - {datetime.now(ET):%H:%M} ET** (from the whale's fill at the print "
                 "time; sell price = live bid)" if ENTRY_MODE != "ask" else
                 f"📡 **Live ping tracker - {datetime.now(ET):%H:%M} ET** (from your ask at the ping; sell = bid)"]
        for v in vals:
            lines.append(f"{'🟢' if v['bid'] >= v['entry'] else '🔴'} {v['name']} ({v['time']}): "
                         f"fill ${v['entry']:.2f} -> "
                         f"bid ${v['bid']:.2f} (**{v['bid'] / v['entry'] - 1:+.0%}**), best "
                         f"{v['best'] / v['entry'] - 1:+.0%}")
        pl.discord_send("\n".join(lines))


OI_WAIT_SEC = 8     # IBKR usually sends open interest within a second or two
OI_BATCH = 20         # contracts requested at once (live streams use most of the 100 lines)


def _snapshot_oi_batch(db, ib, items, snap_date):
    """Open interest for a batch of (contract, key): request all, wait, read, cancel.
    IBKR updates OI once a day, overnight."""
    ib.call("reqMarketDataType", 1)          # live: IBKR sends OI only in live mode (not frozen)
    field = lambda key: "callOpenInterest" if key[2] == "CALL" else "putOpenInterest"
    tickers = [(ib.call("reqMktData", c, "100,101", False, False), key) for c, key in items]
    end = time.time() + OI_WAIT_SEC
    while time.time() < end and any(_num(getattr(t, field(k))) is None for t, k in tickers):
        ib._ib.sleep(0.5)
    n = 0
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for (t, key), (c, _) in zip(tickers, items):
        ib.call("cancelMktData", c)
        oi = _num(getattr(t, field(key)))
        if oi is not None:
            db.execute("INSERT OR REPLACE INTO ib_oi VALUES (?, ?, ?, ?, ?, ?, ?)", (*key, snap_date, oi, stamp))
            n += 1
    db.commit()
    return n


def snapshot_open_interest(db, ib=None, only_today_picks=False, log=print):
    """Today's open interest for every pick contract still listed (or only today's new picks).
    One snapshot per contract per day: comparing days shows whether a whale print OPENED a new
    position (OI rises by about its size the next day) or closed one."""
    from ib_async import Option
    ensure_schema(db)
    own = ib is None
    if own:
        if not available():
            return 0
        ib = IbData()
    today = datetime.now(ET).date().isoformat()
    try:
        rows = db.execute(
            "SELECT DISTINCT p.ticker, p.strike, p.put_call, p.expiration FROM picks p LEFT JOIN ib_oi o "
            "ON o.ticker = p.ticker AND o.strike = p.strike AND o.put_call = p.put_call "
            "AND o.expiration = p.expiration AND o.snap_date = ? WHERE p.expiration >= ? AND o.snap_date IS NULL"
            + (" AND p.trade_date = ?" if only_today_picks else ""),
            (today, today, today) if only_today_picks else (today, today)).fetchall()
        n = 0
        for i in range(0, len(rows), OI_BATCH):
            if ib.idle_hook:      # a new ping's entry quote comes first
                ib.idle_hook()
            items = []
            for key in rows[i:i + OI_BATCH]:
                ticker, strike, pc, exp = key
                c = _option(ticker, exp, strike, pc)
                if ib.call("qualifyContracts", c) and c.conId:
                    items.append((c, key))
            if items:
                n += _snapshot_oi_batch(db, ib, items, today)
        if rows and not only_today_picks:
            log(f"IBKR: open interest recorded for {n} of {len(rows)} contracts")
        return n
    finally:
        if own:
            ib.close()


def price_stock_iv(db, deadline=None, log=print):
    """IBKR's daily 30-day implied volatility for every stock with picks (1 year the first time,
    then the last few days). Gives 'were options cheap or expensive vs normal' context."""
    from ib_async import Stock
    ensure_schema(db)
    if not available():
        return {}
    tickers = [r[0] for r in db.execute("SELECT DISTINCT ticker FROM picks UNION SELECT 'SPY' ORDER BY 1")]
    have = dict(db.execute("SELECT ticker, MAX(day) FROM ib_stock_iv GROUP BY ticker").fetchall())
    yesterday = (datetime.now(ET).date() - timedelta(days=1)).isoformat()
    todo = [t for t in tickers if have.get(t, "") < yesterday and
            not db.execute("SELECT 1 FROM ib_stock_days WHERE ticker = ? AND status = 'no_contract'", (t,)).fetchone()]
    if not todo:
        return {}
    log(f"IBKR: stock implied-volatility history for {len(todo)} tickers (~{len(todo) * HIST_GAP_SEC / 60:.0f} min)")
    n = 0
    ib = IbData()
    try:
        for t in todo:
            if deadline and time.time() > deadline or not ib.data_ok():
                break
            s = Stock(t, "SMART", "USD")
            if not (ib.call("qualifyContracts", s) and s.conId):
                continue
            bars = ib.call("reqHistoricalData", s, endDateTime="", durationStr="1 Y" if t not in have else "10 D",
                           barSizeSetting="1 day", whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True, formatDate=1)
            rows = [(t, str(b.date)[:10], b.close) for b in bars or [] if b.close and b.close > 0]
            db.executemany("INSERT OR REPLACE INTO ib_stock_iv VALUES (?, ?, ?)", rows)
            db.commit()
            n += bool(rows)
    finally:
        ib.close()
    log(f"IBKR: stock IV history for {n} tickers")
    return {"stock_iv": n}


EXP_LOOKBACK_DAYS = 4   # try contracts that expired this recently (IBKR drops them soon after)


def price_expiries(db, deadline=None, log=print):
    """Expiry-day stats from IBKR: the full expiry session's 1-minute trades and bids, taken the
    evening the contract expires (while IBKR still lists it). Replaces Trade Echo's expiry-day
    stats, which cost credits and vanish for expired contracts."""
    from ib_async import Option
    ensure_schema(db)
    if not available():
        log("IBKR: TWS is not accepting API connections - expiry stats skipped")
        return {}
    now = datetime.now(ET)
    last_day = now.date() if now.hour * 60 + now.minute >= 16 * 60 + 5 else now.date() - timedelta(days=1)
    first = (last_day - timedelta(days=EXP_LOOKBACK_DAYS)).isoformat()
    todo = db.execute(
        "SELECT p.id, p.ticker, p.strike, p.put_call, p.expiration, p.trade_date, p.fill_price FROM picks p "
        "LEFT JOIN ib_exp_stats x ON x.pick_id = p.id WHERE p.expiration BETWEEN ? AND ? "
        "AND (x.pick_id IS NULL OR x.status IN ('partial', 'error')) ORDER BY p.expiration",
        (first, last_day.isoformat())).fetchall()
    if not todo:
        return {}
    log(f"IBKR: expiry-day prices for {len(todo)} picks")
    counts, contracts = {}, {}
    ib = IbData()
    try:
        for pid, ticker, strike, pc, exp, tdate, fill in todo:
            if deadline and time.time() > deadline or not ib.data_ok():
                break
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            key = (ticker, strike, pc, exp)
            try:
                if exp == tdate and db.execute("SELECT 1 FROM ib_stats WHERE pick_id = ? AND status = 'ok'",
                                               (pid,)).fetchone():
                    # 0DTE: the print day IS the expiry day - reuse the bars already stored
                    q = ("SELECT minute_et, open, high, low, close, volume FROM ib_bars WHERE pick_id = ? "
                         "AND kind = ? ORDER BY minute_et")
                    trades = [tuple(r) + (None,) for r in db.execute(q, (pid, "TRADES"))]  # no VWAP stored
                    bids = [tuple(r) for r in db.execute(q, (pid, "BID"))]
                else:
                    if key not in contracts:
                        c = _option(ticker, exp, strike, pc)
                        contracts[key] = c if ib.call("qualifyContracts", c) and c.conId else None
                    c = contracts[key]
                    if c is None:
                        db.execute("INSERT OR REPLACE INTO ib_exp_stats (pick_id, status, detail, exp_date, "
                                   "fetched_utc) VALUES (?, 'expired', 'IBKR no longer lists it', ?, ?)",
                                   (pid, exp, stamp))
                        db.commit()
                        counts["expired"] = counts.get("expired", 0) + 1
                        continue
                    trades = _bars(ib, c, date.fromisoformat(exp), "TRADES", with_vwap=True)
                    bids = _bars(ib, c, date.fromisoformat(exp), "BID")
                db.executemany("INSERT OR REPLACE INTO ib_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               [(pid, "EXP_TRADES", *r[:6]) for r in trades]
                               + [(pid, "EXP_BID", *r[:6]) for r in bids])
                traded = [r for r in trades if (r[5] or 0) > 0]
                if not trades or not bids:
                    st, vals = "partial", {}
                elif not traded:
                    st, vals = "no_bars", {}
                else:
                    vw_rows = [r for r in traded if len(r) > 6 and r[6]]
                    vwap = (sum(r[6] * r[5] for r in vw_rows) / sum(r[5] for r in vw_rows)) if vw_rows else None
                    bid_ok = [r for r in bids if r[2] > 0]
                    vals = dict(open=traded[0][1], high=max(r[2] for r in traded), low=min(r[3] for r in traded),
                                close=traded[-1][4], vwap=vwap, bid_close=bids[-1][4],
                                bid_high=max(r[2] for r in bid_ok) if bid_ok else None)
                    vals.update({f"{k}_pct": (v / fill - 1 if v is not None else None)
                                 for k, v in list(vals.items()) if k != "open"})
                    st = "ok"
                cols = ["pick_id", "status", "exp_date", "n_trade_bars", "n_bid_bars", "fetched_utc", *vals]
                db.execute(f"INSERT OR REPLACE INTO ib_exp_stats ({', '.join(cols)}) VALUES "
                           f"({', '.join('?' * len(cols))})",
                           (pid, st, exp, len(trades), len(bids), stamp, *vals.values()))
                db.commit()
            except NotAllowed:
                raise
            except Exception as e:
                db.execute("INSERT OR REPLACE INTO ib_exp_stats (pick_id, status, detail, exp_date, fetched_utc) "
                           "VALUES (?, 'error', ?, ?, ?)", (pid, f"{type(e).__name__}: {e}"[:300], exp, stamp))
                db.commit()
                st = "error"
            counts[st] = counts.get(st, 0) + 1
    finally:
        ib.close()
    log(f"IBKR expiry-day prices: {counts}")
    return counts


LIVE_CYCLE_SEC = 300     # refresh today's picks every 5 minutes during the session
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
            c = _option(ticker, exp, strike, pc)
            c = c if ib.call("qualifyContracts", c) and c.conId else None
            contracts[("OPT", pid)] = c
        if c is None:
            row["status"] = "no_contract"
        else:
            ib.no_subscription = False
            ib.call("reqMarketDataType", 1)
            t = ib.call("reqMktData", c, "101,106", False, False)   # open interest, implied vol
            end = time.time() + ENTRY_WAIT_SEC
            while time.time() < end and not ib.no_subscription and not (
                    (_num(t.bid) or 0) > 0 and (_num(t.ask) or 0) > 0):
                ib._ib.sleep(0.25)
            ib.call("cancelMktData", c)
            g = t.modelGreeks
            row.update(market_data_type=t.marketDataType, bid=_num(t.bid), ask=_num(t.ask), last=_num(t.last),
                       iv=_num(g.impliedVol) if g else None, delta=_num(g.delta) if g else None,
                       gamma=_num(g.gamma) if g else None, theta=_num(g.theta) if g else None,
                       vega=_num(g.vega) if g else None, und_price=_num(g.undPrice) if g else None,
                       open_interest=_num(t.callOpenInterest if pc == "CALL" else t.putOpenInterest))
            live = (row["bid"] or 0) > 0 and (row["ask"] or 0) > 0 and t.marketDataType in (1, 2)
            row["status"] = "ok" if live else ("no_subscription" if ib.no_subscription else "no_quote")
        cols = list(row)
        db.execute(f"INSERT OR REPLACE INTO ib_entries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   tuple(row.values()))
        db.commit()
        log(f"{datetime.now(ET):%H:%M:%S} IBKR entry {ticker} {strike:g}{pc[0]} {exp}: {row['status']}"
            + (f" bid {row['bid']} / ask {row['ask']} ({row['delay_sec']:.0f}s after the ping)"
               if row["status"] == "ok" else ""))
    return len(todo)


def run_live(db, in_session, next_open, my_tickers, log=print):
    """During market hours: record your entry quote right after each ping, and every 5 minutes pull
    today's 1-minute bars for all your tickers + SPY (market context for live picks) and for the
    newest picks (minute-by-minute Greeks). Runs as its own process so it never slows the logger.
    Read-only market data, like everything in this module."""
    from ib_async import Option, Stock
    global CLIENT_ID
    CLIENT_ID = LIVE_CLIENT_ID
    ensure_schema(db)
    ib, contracts = None, {}
    busy = {"entries": False}
    oi_day = {"date": None}
    live = {"quotes": None, "whale_poll": None}
    alerts = {"summary": None}

    def entries_hook():
        """Runs every few seconds: entry quotes for new pings, then the once-a-minute live save."""
        if busy["entries"] or ib is None:
            return
        busy["entries"] = True
        try:
            new_pings = record_entries(db, ib, contracts, log)
            if live["quotes"]:
                # the whale watch found new prints -> stream any new whale on your tickers right away
                whale_poll = db.execute("SELECT MAX(id) FROM polls WHERE kind = 'whale' AND new_rows > 0").fetchone()[0]
                if new_pings or whale_poll != live["whale_poll"]:
                    live["whale_poll"] = whale_poll
                    live["quotes"].refresh()
                if live["quotes"].record():   # a new minute was saved -> check follow-ups
                    ping_updates(db, alerts, log)
        except NotAllowed:
            raise
        except Exception as e:
            log(f"{datetime.now(ET):%H:%M:%S} IBKR entry/live check failed: {type(e).__name__}: {e}")
        finally:
            busy["entries"] = False

    def drop_connection():
        nonlocal ib, contracts
        if live["quotes"]:
            live["quotes"].close()
            live["quotes"] = None
        if ib:
            ib.close()
        ib, contracts = None, {}

    while True:
        now = datetime.now(ET)
        if not in_session(now):
            if ib:
                drop_connection()
            nxt = next_open(now)
            log(f"{now:%H:%M:%S} IBKR live: market closed, next session {nxt:%a %H:%M} ET")
            time.sleep(max(60, (nxt - datetime.now(ET)).total_seconds() + 60))
            continue
        started = time.time()
        try:
            if ib is None or not ib.data_ok():
                drop_connection()
                if not available():
                    log(f"{now:%H:%M:%S} IBKR live: TWS not open - retrying in 5 min")
                    time.sleep(LIVE_CYCLE_SEC)
                    continue
                ib, contracts = IbData(), {}
                ib.idle_hook = entries_hook
                live["quotes"] = LiveQuotes(db, ib, my_tickers)
            entries_hook()
            n_streams = live["quotes"].refresh()   # stream every open pick contract (pinged first)
            if oi_day["date"] != now.date():   # once per session: OI of every listed pick contract
                snapshot_open_interest(db, ib, log=log)
                oi_day["date"] = now.date()
            snapshot_open_interest(db, ib, only_today_picks=True, log=log)   # new picks' OI right away
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
                    c = _option(ticker, exp, strike, pc)
                    contracts[key] = c if ib.call("qualifyContracts", c) and c.conId else None
                if contracts[key]:
                    _store_today(db, ib, contracts[key], "TRADES", pick_id=pid)
                    points += greeks_path(db, pid)
            log(f"{datetime.now(ET):%H:%M:%S} IBKR live: {n_streams} contracts streaming, {len(picks)} picks' "
                f"bars updated, {points} Greeks points ({time.time() - started:.0f}s)")
        except NotAllowed:
            raise
        except Exception as e:
            log(f"{datetime.now(ET):%H:%M:%S} IBKR live: cycle failed: {type(e).__name__}: {e}")
            drop_connection()
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
    ensure_schema(db)
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

    expired = date.fromisoformat(exp) < datetime.now(ET).date()
    if expired and date.fromisoformat(exp) < datetime.now(ET).date() - timedelta(days=1):
        return save("expired", "IBKR has no data for expired options")
    # a contract that expired yesterday is often still listed for a few hours after midnight
    c = _option(ticker, exp, strike, pc)
    if not ib.call("qualifyContracts", c) or not c.conId:
        if expired:
            return save("expired", "IBKR has no data for expired options")
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
    status = save("ok", con_id=c.conId, n_trade_bars=len(trades), n_bid_bars=len(bids),
                  after_high=hi[2], after_low=lo[3], after_high_time=hi[0][11:], after_low_time=lo[0][11:],
                  day_close=trades[-1][4], after_bid_high=bid_hi[2] if bid_hi else None, bid_close=bid_close,
                  max_gain_pct=pct(hi[2]), max_loss_pct=pct(lo[3]), close_pct=pct(trades[-1][4]),
                  bid_max_gain_pct=pct(bid_hi[2]) if bid_hi else None, bid_close_pct=pct(bid_close))
    _price_spread(db, ib, pid, c, td, fill_min, bids)
    return status


def _price_spread(db, ib, pid, contract, trade_date, fill_min, bids=None):
    """ASK bars for the print day -> the bid/ask spread right after the print and over the day
    (what entering and exiting really costs). Stored with the other bars; never used to grade."""
    asks = _bars(ib, contract, trade_date, "ASK")
    db.executemany("INSERT OR REPLACE INTO ib_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   [(pid, "ASK", *r) for r in asks])
    if bids is None:
        bids = [tuple(r) for r in db.execute("SELECT minute_et, open, high, low, close, volume FROM ib_bars "
                                             "WHERE pick_id = ? AND kind = 'BID' ORDER BY minute_et", (pid,))]
    bid_by_min = {r[0]: r[4] for r in bids if r[4] and r[4] > 0}
    pairs = [(m, bid_by_min[m], a[4]) for a in asks if (m := a[0]) in bid_by_min and a[4] and a[4] >= bid_by_min[m]]
    spreads = sorted((ask - bid) / ((ask + bid) / 2) for _, bid, ask in pairs)
    first = next(((ask - bid, (ask - bid) / ((ask + bid) / 2)) for m, bid, ask in pairs if m > fill_min), (None, None))
    db.execute("UPDATE ib_stats SET n_ask_bars = ?, spread_at_print = ?, spread_pct_at_print = ?, "
               "median_spread_pct = ? WHERE pick_id = ?",
               (len(asks), first[0], first[1], spreads[len(spreads) // 2] if spreads else None, pid))
    db.commit()


def price_picks(db, only_missing=True, deadline=None, log=print):
    """Record IBKR after-print stats for every finished-session pick whose contract is still listed."""
    ensure_schema(db)
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
        + "ORDER BY p.trade_date, p.trade_time_et",
        (last_day.isoformat(), (today.date() - timedelta(days=1)).isoformat())).fetchall()
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
