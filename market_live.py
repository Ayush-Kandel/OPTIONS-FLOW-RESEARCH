"""Live stock board data for FlowDesk's Market screen - READ-ONLY market data.

Runs inside the IBKR live tracker (ibkr.run_live, client 18) and, every few seconds during the
session, saves the latest values for your tickers to market_now, which FlowDesk reads:

  - 1-minute bars as they form (IBKR updates the current bar every few seconds): price, day
    open/high/low, volume, VWAP. Works with the data the account has today.
  - Live quotes and every trade at the bid or the ask (buying vs selling pressure): only with a
    real-time US stock subscription for the API. Without one IBKR answers 10089/354/2186 and
    this part switches itself off for the session.
  - The order book (Level 2) of the ticker open in FlowDesk: only with depth subscriptions
    (IBKR answered 2152 "Need additional market data permissions - Depth: NASDAQ ..." on Oct 3).
  - Each options exchange's best bid and ask for the option contract open in FlowDesk (IBKR's SMART
    depth over OPRA top-of-book: 13 exchanges on Oct 4 - the other ~4 would need depth subscriptions).
  - Once a day, 30 daily bars per ticker: the previous close and the normal daily volume.

Completed minutes also go into ib_stock_bars (the same table the nightly job fills), so the
models get them too. Nothing here can place orders: every call goes through ibkr.IbData.
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from ibkr import ET, _num

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_now (        -- latest values per ticker, rewritten every few seconds
    ticker     TEXT PRIMARY KEY,
    at_utc     TEXT NOT NULL,
    feed       TEXT NOT NULL,      -- 'ticks' (live quotes + trades), 'bars' (1-minute bars as they form)
    bar_minute TEXT,               -- 'YYYY-MM-DD HH:MM' ET of the forming bar
    price REAL,                    -- last trade
    bar_open REAL, bar_high REAL, bar_low REAL, bar_volume REAL, bar_vwap REAL,   -- the forming minute
    bid REAL, ask REAL, bid_size REAL, ask_size REAL,                              -- 'ticks' feed only
    buy_volume REAL, sell_volume REAL, mid_volume REAL                            -- today, 'ticks' feed only
);
CREATE TABLE IF NOT EXISTS ib_stock_daily (    -- daily bars (regular hours)
    ticker TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (ticker, trade_date)
);
CREATE TABLE IF NOT EXISTS ib_stock_flow (     -- per minute: shares traded at the ask / bid / in between
    ticker TEXT NOT NULL, minute_et TEXT NOT NULL,
    buy_volume REAL, sell_volume REAL, mid_volume REAL,
    PRIMARY KEY (ticker, minute_et)
);
CREATE TABLE IF NOT EXISTS market_book (       -- order book of the ticker open in FlowDesk
    ticker TEXT NOT NULL, side TEXT NOT NULL, level INTEGER NOT NULL,
    price REAL, size REAL, venue TEXT, at_utc TEXT NOT NULL,
    PRIMARY KEY (ticker, side, level)
);
CREATE TABLE IF NOT EXISTS option_book (       -- each exchange's quote for the option contract open in FlowDesk
    ticker TEXT NOT NULL, strike REAL NOT NULL, put_call TEXT NOT NULL, expiration TEXT NOT NULL,
    side TEXT NOT NULL, level INTEGER NOT NULL,
    price REAL, size REAL, venue TEXT, at_utc TEXT NOT NULL,
    PRIMARY KEY (side, level)
);
CREATE TABLE IF NOT EXISTS market_feed (       -- what the account's subscriptions allow
    item TEXT PRIMARY KEY,         -- 'quotes', 'book', 'option_book', 'bars'
    status TEXT NOT NULL,          -- 'ok', 'not_subscribed', 'stale', 'off'
    detail TEXT, at_utc TEXT NOT NULL
);
"""

WRITE_EVERY_SEC = 2
BOOK_ROWS = 10
OPTION_BOOK_ROWS = 20    # SMART depth for an option = one row per exchange quoting it
BOOK_WAIT_SEC = 15       # no levels this long after asking -> the account can't see this book
FOCUS_URL = f"http://127.0.0.1:{os.environ.get('FLOWDESK_PORT', '8060')}/api/focus"   # what FlowDesk has open
FOCUS_EVERY_SEC = 4
FOCUS_MAX_AGE_SEC = 60   # FlowDesk re-sends its focus every few seconds while a page is open
STALE_SEC = 180          # no bar update for this long in the session -> the bar stream isn't live
NO_QUOTES = (354, 10089, 10090, 10168, 2186)
# depth answers: 2152 lists which exchanges send top of book and which need depth permissions (a warning -
# the listed "Top" exchanges still arrive); 354/10092 = nothing for this book; 309 = too many books open
DEPTH_NOTES = (2152, 354, 10092, 309)


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _minute(b):
    t = b.date if isinstance(b.date, datetime) else datetime.fromtimestamp(int(b.date), timezone.utc)
    return t.astimezone(ET).strftime("%Y-%m-%d %H:%M")


DAILY_FIRST, DAILY_MIN = "90 D", 25    # first fetch per ticker (20 normal days before the oldest picks)


def needs_daily(db, ticker, before):
    """True if `ticker` lacks daily bars up to the weekday before `before` (a date), or has too few."""
    prev = before - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    last, n = db.execute("SELECT MAX(trade_date), COUNT(*) FROM ib_stock_daily WHERE ticker = ? AND trade_date < ?",
                         (ticker, before.isoformat())).fetchone()
    return not last or last < prev.isoformat() or n < DAILY_MIN


def fetch_daily_bars(db, ib, stocks, log=print, deadline=None):
    """Daily TRADES bars (regular hours) for {ticker: qualified Stock}: 90 days the first time, then the
    last 5. Today's bar only once the session is over. One paced historical request per ticker."""
    now = datetime.now(ET)
    cutoff = (now.date() + timedelta(days=1) if now.strftime("%H:%M") >= "16:05" else now.date()).isoformat()
    done = 0
    for t, s in stocks.items():
        if deadline and time.time() > deadline:
            log(f"daily bars: stopped at {done}/{len(stocks)} (deadline)")
            break
        n = db.execute("SELECT COUNT(*) FROM ib_stock_daily WHERE ticker = ?", (t,)).fetchone()[0]
        try:
            bars = ib.call("reqHistoricalData", s, "", DAILY_FIRST if n < DAILY_MIN else "5 D", "1 day",
                           "TRADES", True, 1, False, [])
        except Exception as e:
            log(f"daily bars: {t} failed: {type(e).__name__}: {e}")
            continue
        db.executemany("INSERT OR REPLACE INTO ib_stock_daily VALUES (?, ?, ?, ?, ?, ?, ?)",
                       [(t, str(b.date)[:10], b.open, b.high, b.low, b.close, b.volume) for b in bars or []
                        if str(b.date)[:10] < cutoff])
        db.commit()
        done += 1
    return done


class MarketFeed:
    def __init__(self, db, ib, tickers, log=print):
        self.db, self.ib, self.log = db, ib, log
        self.tickers = sorted(set(tickers))
        self.stocks, self.bars, self.quotes = {}, {}, {}
        self.seen = {}           # ticker -> (last bar signature, when it last changed)
        self.saved = {}          # ticker -> last completed minute written to ib_stock_bars
        self.flow = {}           # ticker -> {minute: [buy, sell, mid]} (ticks feed)
        self.quotes_status, self.bars_status = None, None
        self.book, self.book_status, self.book_detail = None, None, None
        self.book_since = 0.0
        # the option contract open in FlowDesk: (key, qualified contract, depth Ticker, asked at)
        self.obook, self.obook_status, self.obook_detail = None, None, None
        self.focus, self.focus_checked = None, 0.0
        self.last_write = 0.0
        ensure_schema(db)
        ib._ib.errorEvent += self._on_error

    # ---------- start ----------
    def start(self):
        """Fast part (seconds): qualify the stocks and try live quotes. Run it before the option
        streams so the market-data lines it takes are known."""
        from ib_async import Stock
        for t in self.tickers:
            s = Stock(t, "SMART", "USD")
            if self.ib.call("qualifyContracts", s) and s.conId:
                self.stocks[t] = s
        self._start_quotes()
        return self.lines()

    def start_bars(self):
        """Slow part (~10 s per request, IBKR's historical pacing): the forming-bar streams, then
        the daily bars. The live entries/option quotes keep running meanwhile (IbData idle hook)."""
        for t, s in self.stocks.items():
            if t in self.bars:
                continue
            try:
                bars = self.ib.call("reqHistoricalData", s, "", "1 D", "1 min", "TRADES", True, 2, True, [])
            except Exception as e:
                self.log(f"market: {t} bar stream failed: {type(e).__name__}: {e}")
                continue
            self.bars[t] = bars
            self.seen[t] = (None, time.time())
            self.snapshot(force=True)   # the board fills in ticker by ticker
        self.bars_status = "ok"
        self._status("bars", "ok", f"{len(self.bars)} of {len(self.tickers)} tickers streaming 1-minute bars")
        self.daily_bars()
        return len(self.bars)

    def _start_quotes(self):
        """Live quotes + every trade (RTVolume): needs a real-time US stock subscription for the API."""
        if self.quotes_status == "not_subscribed":
            return
        self.ib.call("reqMarketDataType", 1)
        for t, s in self.stocks.items():
            tk = self.ib.call("reqMktData", s, "233", False, False)
            tk.updateEvent += self._on_ticks
            self.quotes[t] = tk
        end = time.time() + 6
        while time.time() < end and self.quotes_status != "not_subscribed" and not any(
                (_num(q.bid) or 0) > 0 for q in self.quotes.values()):
            self.ib._ib.sleep(0.25)
        if self.quotes_status != "not_subscribed" and any((_num(q.bid) or 0) > 0 for q in self.quotes.values()):
            self.quotes_status = "ok"
            self._status("quotes", "ok", "live stock quotes and trades streaming")
        else:
            self._stop_quotes()
            if self.quotes_status != "not_subscribed":
                self._status("quotes", "off", "no live stock quotes arrived")

    def _stop_quotes(self):
        for t, tk in self.quotes.items():
            try:
                tk.updateEvent -= self._on_ticks
                self.ib.call("cancelMktData", self.stocks[t])
            except Exception:
                pass
        self.quotes = {}

    def lines(self):
        """IBKR market-data lines this feed holds (live quotes only; bars and depth don't count)."""
        return len(self.quotes)

    def daily_bars(self):
        """Once a day: daily bars up to the last session (previous close, normal daily volume)."""
        before = datetime.now(ET).date()
        stale = {t: s for t, s in self.stocks.items() if needs_daily(self.db, t, before)}
        return fetch_daily_bars(self.db, self.ib, stale, log=self.log)

    # ---------- IBKR callbacks ----------
    def _on_error(self, req_id, code, msg, contract=None):
        stock = contract is not None and getattr(contract, "secType", "") == "STK"
        if stock and code in NO_QUOTES and self.quotes_status != "ok":
            self.quotes_status = "not_subscribed"
            self._status("quotes", "not_subscribed", f"IBKR {code}: {msg[:300]}")
        if code in DEPTH_NOTES:
            note = f"IBKR {code}: {msg[:400]}"
            if stock:
                self.book_detail = note
            elif contract is not None and getattr(contract, "secType", "") == "OPT":
                self.obook_detail = note

    def _on_ticks(self, tk):
        """Each trade: at/above the ask = buying, at/below the bid = selling, in between = unclear."""
        bid, ask = _num(tk.bid), _num(tk.ask)
        sym = tk.contract.symbol
        for tick in tk.ticks:
            if tick.tickType != 48 or not tick.size:   # 48 = RTVolume: one entry per trade report
                continue
            minute = datetime.now(ET).strftime("%Y-%m-%d %H:%M")
            row = self.flow.setdefault(sym, {}).setdefault(minute, [0.0, 0.0, 0.0])
            p, size = tick.price, float(tick.size)
            if bid and ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                side = 0 if p >= ask or p > mid else 1 if p <= bid or p < mid else 2
            else:
                side = 2
            row[side] += size

    # ---------- order books (IBKR allows 3 at once: at most one stock + one option here) ----------
    def _check_focus(self):
        """Ask FlowDesk what's on screen: a ticker page (that stock's book) and/or an option contract's
        page (each exchange's quote for that contract)."""
        if time.time() - self.focus_checked < FOCUS_EVERY_SEC:
            return
        self.focus_checked = time.time()
        try:
            with urllib.request.urlopen(FOCUS_URL, timeout=0.5) as r:
                f = json.loads(r.read().decode("utf-8"))
        except Exception:
            f = {}                 # FlowDesk closed: no books
        fresh = lambda key: time.time() - (f.get(key) or 0) < FOCUS_MAX_AGE_SEC
        want = f.get("ticker") if fresh("at") else None
        self._focus_stock(want if want in self.stocks else None)
        c = f.get("contract") if fresh("contract_at") else None
        self._focus_option((c["ticker"], float(c["strike"]), c["put_call"], c["expiration"]) if c else None)

    def _focus_stock(self, want):
        if want == self.focus:
            return
        if self.book is not None:
            self._cancel_depth(self.stocks[self.focus])
            self.book = None
        self.focus = want
        if want and self.book_status != "not_subscribed":
            self.book = self.ib.call("reqMktDepth", self.stocks[want], BOOK_ROWS, True)
            self.book_status, self.book_since = "waiting", time.time()

    def _focus_option(self, key):
        if (self.obook or {}).get("key") == key:
            return
        if self.obook and self.obook["contract"] is not None:
            self._cancel_depth(self.obook["contract"])
        self.obook = None
        if not key:
            return
        from ibkr import _option
        c = _option(key[0], key[3], key[1], key[2])
        if self.ib.call("qualifyContracts", c) and c.conId:
            self.obook = {"key": key, "contract": c, "since": time.time(),
                          "ticker": self.ib.call("reqMktDepth", c, OPTION_BOOK_ROWS, True)}
            self.obook_status, self.obook_detail = "waiting", None
        else:   # remembered, so it isn't looked up again every few seconds
            self.obook = {"key": key, "contract": None, "ticker": None, "since": time.time()}
            self.obook_status, self.obook_detail = "no_contract", "IBKR doesn't list this contract (expired?)"

    def _cancel_depth(self, contract):
        try:
            self.ib.call("cancelMktDepth", contract, True)
        except Exception:
            pass

    def _write_books(self, now_utc):
        # the stock's book: none arriving within BOOK_WAIT_SEC = not visible with this account's data
        self.db.execute("DELETE FROM market_book")
        if self.book is not None and self.focus:
            rows = [(self.focus, side, i, _num(lv.price), _num(lv.size), lv.marketMaker or "", now_utc)
                    for side, levels in (("bid", self.book.domBids), ("ask", self.book.domAsks))
                    for i, lv in enumerate(levels[:BOOK_ROWS])]
            self.db.executemany("INSERT INTO market_book VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            if rows:
                self.book_status = "ok"
            elif time.time() - self.book_since > BOOK_WAIT_SEC:
                self.book_status = "not_subscribed"           # stop asking for this connection and free the slot
                self._cancel_depth(self.stocks[self.focus])
                self.book = None
        if self.book_status in ("ok", "not_subscribed"):
            self._status("book", self.book_status, self.book_detail if self.book_status != "ok" else self.focus,
                         commit=False)
        # each exchange's quote for the option contract (kept open: quotes can appear later, e.g. at the open)
        self.db.execute("DELETE FROM option_book")
        ob = self.obook
        if ob and ob["ticker"] is not None:
            rows = [(*ob["key"], side, i, _num(lv.price), _num(lv.size), lv.marketMaker or "", now_utc)
                    for side, levels in (("bid", ob["ticker"].domBids), ("ask", ob["ticker"].domAsks))
                    for i, lv in enumerate(levels[:OPTION_BOOK_ROWS])]
            self.db.executemany("INSERT INTO option_book VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            if rows:
                self.obook_status = "ok"
            elif time.time() - ob["since"] > BOOK_WAIT_SEC:
                self.obook_status = "no_quotes"
        if ob:
            detail = (f"{len(set(r[8] for r in rows))} exchanges quoting" if ob["ticker"] is not None and rows
                      else self.obook_detail)
            self._status("option_book", self.obook_status, detail, commit=False)

    # ---------- every few seconds ----------
    def snapshot(self, force=False):
        if not force and time.time() - self.last_write < WRITE_EVERY_SEC:
            return 0
        self.last_write = time.time()
        if self.quotes_status == "not_subscribed" and self.quotes:
            self._stop_quotes()
        try:
            self._check_focus()
        except Exception as e:
            self.log(f"market: order book check failed: {type(e).__name__}: {e}")
        now_utc = _utc()
        forming = datetime.now(ET).strftime("%Y-%m-%d %H:%M")
        rows, stale = [], 0
        for t, bars in self.bars.items():
            if not bars:
                continue
            b = bars[-1]
            sig = (_minute(b), b.close, b.volume)
            if sig != self.seen[t][0]:
                self.seen[t] = (sig, time.time())
            elif time.time() - self.seen[t][1] > STALE_SEC:
                stale += 1
            # completed minutes -> the same table the nightly job fills (all of today's on the first write)
            recent = bars[-30:] if t in self.saved else bars
            done = [x for x in recent if _minute(x) < forming and _minute(x) > self.saved.get(t, "")]
            if done:
                self.db.executemany("INSERT OR REPLACE INTO ib_stock_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    [(t, _minute(x), x.open, x.high, x.low, x.close, x.volume, x.average)
                                     for x in done])
                self.saved[t] = _minute(done[-1])
            q = self.quotes.get(t)
            last = _num(q.last) if q is not None else None
            flow = self.flow.get(t, {})
            today = forming[:10]
            day_flow = [sum(v[i] for m, v in flow.items() if m.startswith(today)) for i in range(3)]
            rows.append((t, now_utc, "ticks" if q is not None else "bars", _minute(b),
                         last if last and last > 0 else b.close, b.open, b.high, b.low, b.volume, b.average,
                         _num(q.bid) if q is not None else None, _num(q.ask) if q is not None else None,
                         _num(q.bidSize) if q is not None else None, _num(q.askSize) if q is not None else None,
                         *(day_flow if q is not None else (None, None, None))))
        self.db.executemany("INSERT OR REPLACE INTO market_now VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            rows)
        for t, flow in self.flow.items():
            self.db.executemany("INSERT OR REPLACE INTO ib_stock_flow VALUES (?, ?, ?, ?, ?)",
                                [(t, m, *v) for m, v in flow.items()])
            for m in [m for m in flow if m < forming]:   # finished minutes are saved; keep only the current one
                del flow[m]
        self._write_books(now_utc)
        status = "stale" if self.bars and stale == len(self.bars) else "ok" if self.bars else None
        if status and status != self.bars_status:
            self.bars_status = status
            self._status("bars", status, f"no bar updates for {STALE_SEC // 60}+ min" if status == "stale"
                         else f"{len(self.bars)} tickers streaming 1-minute bars", commit=False)
        self.db.commit()
        return len(rows)

    def live_bars(self, ticker):
        """True while this ticker's forming-bar stream is updating (then the 5-minute bar pull can skip it)."""
        s = self.seen.get(ticker)
        return bool(s and s[0] and time.time() - s[1] < STALE_SEC)

    def _status(self, item, status, detail, commit=True):
        self.db.execute("INSERT OR REPLACE INTO market_feed VALUES (?, ?, ?, ?)", (item, status, detail, _utc()))
        if commit:
            self.db.commit()

    def close(self):
        self._stop_quotes()
        for bars in self.bars.values():
            try:
                self.ib.call("cancelHistoricalData", bars)
            except Exception:
                pass
        if self.book is not None and self.focus:
            self._cancel_depth(self.stocks[self.focus])
        if self.obook and self.obook["contract"] is not None:
            self._cancel_depth(self.obook["contract"])
        self.bars, self.book, self.obook = {}, None, None
