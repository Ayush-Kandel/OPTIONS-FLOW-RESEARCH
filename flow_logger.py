"""Trade Echo options-flow capture logger (read-only).

Polls get_option_flow (0-14 DTE prints), get_dealer_edge_data (GEX, charm,
ATM IV, setups) and get_noteworthy_flow (picks, see pipeline.py) during market
hours and stores everything in flow.db. After each close it labels the day's
picks (get_market_data), retrains the model and prints a report.

Only the read-only tools in ALLOWED_TOOLS are ever called. The token is
read from TRADEECHO_TOKEN (falling back to the saved Windows user variable of
the same name) and is only ever sent in the Authorization header -- never
printed, logged or written to disk.

Usage:
    py flow_logger.py                        run until Ctrl+C
    py flow_logger.py --once SPY             one flow poll (1 credit)
    py flow_logger.py --once-dealer SPY      one Dealer Edge poll (2 credits)
    py flow_logger.py --once-noteworthy      one live noteworthy poll (2 credits)
    py flow_logger.py --after-close [DATE]   sweep, label and retrain for a day
    py flow_logger.py --train                retrain the model (no API calls)
    py flow_logger.py --report               picks/model report (no API calls)
    py flow_logger.py --test-discord         send one test message to Discord
    py flow_logger.py --status               summary from flow.db (no API calls)
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import pipeline
from tradeecho_probe import (ENDPOINT, PROTOCOL_VERSION, HttpError, McpClient,
                             RpcError, is_blocked)

# ---------------------------------------------------------------- settings --

FAST_TICKERS = ["SPY", "QQQ"]
SLOW_TICKERS = ["NVDA", "INTC", "DRAM", "MU", "META", "CRWV", "HIMS", "TSLA",
                "GOOG", "PLTR"]

MAX_DTE_DAYS = 14
MIN_PREMIUM = 25000
# Busy tickers print more than 50 rows per poll at $25K, so their oldest prints were cut off.
# Floors sized from Oct 1 rates: SPY/QQQ ~13 rows per 10 min at $75K, NVDA/MU ~30 per 45 min at $50K.
TICKER_MIN_PREMIUM = {"SPY": 75000, "QQQ": 75000, "NVDA": 50000, "MU": 50000}
FLOW_LIMIT = 50                # server cap per call

FAST_INTERVAL_SEC = 10 * 60    # SPY, QQQ flow        -> 12 credits/hr
SLOW_INTERVAL_SEC = 45 * 60    # other 10 flow        -> ~13 credits/hr
DEALER_HOURLY_TICKERS = []         # all 12 every 2 hours to free credits for fast picks
DEALER_FAST_INTERVAL_SEC = 3600
DEALER_INTERVAL_SEC = 2 * 3600     # 12 tickers, 2 cr       -> 12 credits/hr
NOTEWORTHY_INTERVAL_SEC = 120  # 2 cr every 2 min     -> 60 credits/hr  (total ~98)
FAST_STAGGER_SEC = 300
SLOW_STAGGER_SEC = 270         # 10 tickers spread across the 45 minutes
DEALER_STAGGER_SEC = 600       # one Dealer Edge call every 10 minutes

# Server limits seen in a 429: 100/hr per connection, 150/hr per user, burst 15.
# Hourly credits reset at the top of each UTC hour.
HOURLY_STOP = 100              # the per-connection cap; use all of it
DEALER_HOURLY_STOP = 92        # Dealer Edge stops earlier, leaving room for flow
BURST_WINDOW_SEC = 300
BURST_STOP = 14                # max credits in any 5 minutes, both connections together (server: 15)

# Whale watch on a SECOND connection, which gets the other 50 of the 150/hr user cap. Trade Echo
# receives raw prints within ~3 s, but its scored noteworthy list can lag 5-10 min at the open
# (Oct 2: TSLA 365C listed 13 min after the trade), so the raw tape is polled market-wide for
# whale-size prints. Silent for now: prints are stored and streamed on IBKR from first sight;
# pings still wait for the noteworthy score + the model.
WHALE_INTERVAL_SEC = 72        # 1 cr every 72 s       -> 50 credits/hr
WHALE_HOURLY_STOP = 50
WHALE_WINDOW_MIN = 3           # each poll asks for prints since 3 minutes ago
USER_HOURLY_STOP = 150

SESSION_OPEN = dtime(9, 30)
SESSION_CLOSE = dtime(16, 15)
HOLIDAY_CHECK_AT = dtime(9, 45)      # no SPY print today by now -> market closed
EARLY_CLOSE_CHECK_AFTER = dtime(13, 0)
EARLY_CLOSE_QUIET_MIN = 30           # SPY silent this long after 1 PM -> early close

ALLOWED_TOOLS = {"get_option_flow", "get_dealer_edge_data", "get_noteworthy_flow",
                 "get_market_data", "get_algo_edge_signals"}
TOOL_COST = {"get_option_flow": 1, "get_dealer_edge_data": 2, "get_noteworthy_flow": 2,
             "get_market_data": 2, "get_algo_edge_signals": 1}
DB_PATH = Path(__file__).with_name("flow.db")

# ------------------------------------------------------------------ schema --

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,          -- 'flow' or 'dealer_edge'
    ticker          TEXT NOT NULL,
    started_utc     TEXT NOT NULL,
    started_epoch   REAL NOT NULL,
    rows_returned   INTEGER,
    new_rows        INTEGER,
    credits_charged INTEGER NOT NULL DEFAULT 0,
    flag            TEXT,                   -- 'possible_gap' / 'startup_backlog'
    error           TEXT
);

CREATE TABLE IF NOT EXISTS prints (
    id              INTEGER PRIMARY KEY,
    ticker          TEXT NOT NULL,
    strike          REAL NOT NULL,
    expiration      TEXT NOT NULL,          -- YYYY-MM-DD
    put_call        TEXT NOT NULL,
    trade_date      TEXT NOT NULL,
    trade_time_et   TEXT NOT NULL,
    size            INTEGER NOT NULL,
    fill_price      REAL NOT NULL,
    premium         REAL,
    spot            REAL,
    dte_days        INTEGER,
    sentiment       TEXT,
    activity_type   TEXT,                   -- SWEEP / TRADE
    updated_utc     TEXT,
    first_seen_utc  TEXT NOT NULL,
    poll_id         INTEGER REFERENCES polls(id),
    UNIQUE (ticker, strike, expiration, put_call, trade_date, trade_time_et,
            size, fill_price)
);
CREATE INDEX IF NOT EXISTS prints_by_ticker_time
    ON prints (ticker, trade_date, trade_time_et);

CREATE TABLE IF NOT EXISTS dealer_edge_snapshots (
    id                  INTEGER PRIMARY KEY,
    poll_id             INTEGER REFERENCES polls(id),
    ticker              TEXT NOT NULL,
    fetched_utc         TEXT NOT NULL,
    data_as_of          TEXT,
    stale               INTEGER,
    spot                REAL,
    gex_rating          REAL,
    anchor              REAL,
    flip                REAL,
    charm_anchor        REAL,
    defense_lines       TEXT,               -- JSON list
    vanna_anchor        REAL,
    vanna_flip          REAL,
    vanna_walls         TEXT,               -- JSON list
    vanna_net_per_vol_pt  REAL,
    vanna_call_per_vol_pt REAL,
    vanna_put_per_vol_pt  REAL,
    atm_iv_nearest      REAL,
    raw_json            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dealer_edge_iv (
    snapshot_id     INTEGER NOT NULL REFERENCES dealer_edge_snapshots(id),
    ticker          TEXT NOT NULL,
    expiration      TEXT NOT NULL,
    atm_iv          REAL
);

CREATE TABLE IF NOT EXISTS setup_observations (
    snapshot_id     INTEGER NOT NULL REFERENCES dealer_edge_snapshots(id),
    ticker          TEXT NOT NULL,
    setup_key       TEXT NOT NULL,
    name            TEXT,
    state           TEXT
);

CREATE TABLE IF NOT EXISTS setup_events (
    id              INTEGER PRIMARY KEY,
    at_utc          TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    setup_key       TEXT NOT NULL,
    name            TEXT,
    event           TEXT NOT NULL,          -- appeared / disappeared / state_changed
    state           TEXT
);

CREATE TABLE IF NOT EXISTS rate_limit_events (
    id              INTEGER PRIMARY KEY,
    at_utc          TEXT NOT NULL,
    tool            TEXT,
    ticker          TEXT,
    retry_after_sec INTEGER,
    resets_at_utc   TEXT,
    used_conn       INTEGER,
    used_user       INTEGER,
    used_burst      INTEGER,
    limit_conn      INTEGER,
    limit_user      INTEGER,
    limit_burst     INTEGER,
    message         TEXT
);

-- Premium flow per ticker per 5-minute bucket, built from captured prints only
-- (0-10 DTE, >= MIN_PREMIUM), so it is not total market volume.
CREATE VIEW IF NOT EXISTS volume_flow_5m AS
SELECT ticker,
       trade_date,
       substr(trade_time_et, 1, 3) ||
           printf('%02d', (CAST(substr(trade_time_et, 4, 2) AS INTEGER) / 5) * 5)
           AS bucket_et,
       COUNT(*)  AS prints,
       SUM(size) AS contracts,
       ROUND(SUM(CASE WHEN put_call = 'CALL' THEN premium ELSE 0 END), 2) AS call_premium,
       ROUND(SUM(CASE WHEN put_call = 'PUT'  THEN premium ELSE 0 END), 2) AS put_premium,
       ROUND(SUM(CASE WHEN put_call = 'CALL' THEN premium ELSE -premium END), 2)
           AS net_call_minus_put,
       ROUND(SUM(CASE WHEN sentiment = 'BULLISH' THEN premium ELSE 0 END), 2) AS bullish_premium,
       ROUND(SUM(CASE WHEN sentiment = 'BEARISH' THEN premium ELSE 0 END), 2) AS bearish_premium,
       ROUND(SUM(CASE WHEN activity_type = 'SWEEP' THEN premium ELSE 0 END), 2) AS sweep_premium
FROM prints
GROUP BY ticker, trade_date, bucket_et;
"""


def open_db(path=DB_PATH):
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")  # lets you query while the logger runs
    db.execute("PRAGMA busy_timeout = 30000")
    db.executescript(SCHEMA)
    if "conn" not in {r[1] for r in db.execute("PRAGMA table_info(polls)")}:
        db.execute("ALTER TABLE polls ADD COLUMN conn INTEGER")   # 2 = second Trade Echo connection
    pipeline.ensure_schema(db)
    import bots
    import integrity
    db.executescript(bots.SCHEMA)
    db.executescript(integrity.SCHEMA)
    import ibkr
    ibkr.ensure_schema(db)
    return db


# ------------------------------------------------------------ eastern time --

def _nth_sunday(year, month, n):
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def _et_tz(utc_moment):
    """US Eastern: DST from 2nd Sunday of March 07:00 UTC to 1st Sunday of Nov 06:00 UTC."""
    year = utc_moment.year
    dst_start = datetime.combine(_nth_sunday(year, 3, 2), dtime(7), timezone.utc)
    dst_end = datetime.combine(_nth_sunday(year, 11, 1), dtime(6), timezone.utc)
    hours = -4 if dst_start <= utc_moment < dst_end else -5
    return timezone(timedelta(hours=hours))


def now_et():
    utc = datetime.now(timezone.utc)
    return utc.astimezone(_et_tz(utc))


def et_datetime(day, clock):
    noon_utc = datetime.combine(day, dtime(12), timezone.utc)
    return datetime.combine(day, clock, _et_tz(noon_utc))


def in_session(et):
    return et.weekday() < 5 and SESSION_OPEN <= et.time() < SESSION_CLOSE


def next_open_et(et, skip_today):
    for offset in range(10):
        day = et.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        if offset == 0 and (skip_today or et.time() >= SESSION_CLOSE):
            continue
        return et_datetime(day, SESSION_OPEN)
    raise RuntimeError("could not find next session")


def utc_iso(epoch=None):
    moment = datetime.fromtimestamp(epoch if epoch is not None else time.time(), timezone.utc)
    return moment.isoformat(timespec="seconds")


# ----------------------------------------------------------------- credits --

class CreditBudget:
    """Tracks credits per UTC clock hour and in a rolling burst window."""

    def __init__(self, recent=None):
        self.hour_key = None
        self.hour_used = 0
        # (epoch, credits); the whale-watch budget shares this deque so the burst cap covers both
        self.recent = recent if recent is not None else deque()

    @classmethod
    def from_db(cls, db, whale=False, recent=None):
        """This connection's credits in the last hour: the main one, or the second one (whale=True)."""
        budget = cls(recent)
        since = time.time() - 3600
        for epoch, credits in db.execute(
                "SELECT started_epoch, credits_charged FROM polls WHERE started_epoch >= ? AND "
                "credits_charged > 0 AND COALESCE(conn, 1) = ? ORDER BY started_epoch",
                (since, 2 if whale else 1)):
            budget.charge(credits, epoch)
        if recent is not None:   # keep the shared burst window in time order
            items = sorted(recent)
            recent.clear()
            recent.extend(items)
        return budget

    def _roll(self, now):
        key = int(now // 3600)
        if key != self.hour_key:
            self.hour_key, self.hour_used = key, 0
        while self.recent and self.recent[0][0] < now - BURST_WINDOW_SEC:
            self.recent.popleft()

    def burst_used(self, now):
        self._roll(now)
        return sum(c for _, c in self.recent)

    def allows(self, cost, hourly_cap, now):
        self._roll(now)
        return (self.hour_used + cost <= hourly_cap
                and self.burst_used(now) + cost <= BURST_STOP)

    def charge(self, credits, when):
        if int(when // 3600) == int(time.time() // 3600):
            self._roll(time.time())
            self.hour_used += credits
        if when >= time.time() - BURST_WINDOW_SEC:
            self.recent.append((when, credits))

    def sync_hour(self, server_used, now):
        self._roll(now)
        self.hour_used = max(self.hour_used, server_used)


# ------------------------------------------------------------------ server --

class RateLimited(Exception):
    def __init__(self, data):
        super().__init__(data.get("error") or "rate limited")
        self.data = data
        self.retry_after = int(data.get("retryAfterSec") or 60)


class ToolError(Exception):
    pass


def load_token():
    token = os.environ.get("TRADEECHO_TOKEN")
    if not token and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                token, _ = winreg.QueryValueEx(key, "TRADEECHO_TOKEN")
        except OSError:
            token = None
    if not token:
        sys.exit("TRADEECHO_TOKEN is not set.")
    return token.strip()


def _rate_limit_data(exc):
    """Return the 429 payload if exc is a credit-limit refusal, else None."""
    if isinstance(exc, HttpError) and exc.status == 429:
        try:
            error = json.loads(exc.body).get("error") or {}
        except ValueError:
            error = {}
        return error.get("data") or {"error": "HTTP 429"}
    if isinstance(exc, RpcError) and exc.error.get("code") == 429:
        return exc.error.get("data") or {"error": exc.error.get("message")}
    return None


class TradeEcho:
    def __init__(self, token):
        self._token = token
        self._client = None

    def reset(self):
        self._client = None

    def _connect(self):
        client = McpClient(ENDPOINT, self._token)
        client.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "options-flow-logger", "version": "1.0.0"},
        })
        client.notify("notifications/initialized")
        tools = {t["name"]: t for t in client.request("tools/list").get("tools", [])}
        for name in ALLOWED_TOOLS:
            tool = tools.get(name)
            if tool is None:
                sys.exit(f"Tool {name} is not offered by the server.")
            read_only = (tool.get("annotations") or {}).get("readOnlyHint") is True
            if is_blocked(tool) or not read_only:
                sys.exit(f"Refusing to use {name}: not marked read-only.")
        self._client = client

    def call(self, name, arguments):
        if name not in ALLOWED_TOOLS:
            raise ValueError(f"{name} is not an allowed tool")
        try:
            if self._client is None:
                self._connect()
            result = self._client.request("tools/call", {"name": name, "arguments": arguments})
        except (HttpError, RpcError) as e:
            data = _rate_limit_data(e)
            if data is not None:
                raise RateLimited(data) from None
            raise

        text = next((c.get("text", "") for c in result.get("content", [])
                     if c.get("type") == "text"), "")
        if result.get("isError"):
            raise ToolError(text[:500] or "tool returned isError")
        payload = result.get("structuredContent")
        if payload is None:
            payload = json.loads(text)
        return payload


# ------------------------------------------------------------------- polls --

def _insert_poll(db, kind, ticker, started, credits, rows_returned=None, error=None, conn=None):
    cur = db.execute(
        "INSERT INTO polls (kind, ticker, started_utc, started_epoch, rows_returned, "
        "credits_charged, error, conn) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, ticker, utc_iso(started), started, rows_returned, credits, error, conn))
    return cur.lastrowid


def record_failed_poll(db, kind, ticker, started, error):
    _insert_poll(db, kind, ticker, started, 0, error=str(error)[:1000])
    db.commit()


def record_rate_limit(db, tool, ticker, data):
    resets_ms = data.get("resetsAt")
    resets = utc_iso(resets_ms / 1000) if isinstance(resets_ms, (int, float)) else None
    db.execute(
        "INSERT INTO rate_limit_events (at_utc, tool, ticker, retry_after_sec, resets_at_utc, "
        "used_conn, used_user, used_burst, limit_conn, limit_user, limit_burst, message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (utc_iso(), tool, ticker, data.get("retryAfterSec"), resets,
         data.get("creditsUsedConn"), data.get("creditsUsedUser"), data.get("creditsUsedBurst"),
         data.get("creditsLimitConn"), data.get("creditsLimitUser"), data.get("creditsLimitBurst"),
         data.get("error")))
    db.commit()


def _store_prints(db, rows, poll_id, ticker=None):
    """Save raw flow prints; returns the rows that were new."""
    seen = utc_iso()
    new = []
    for r in rows:
        premium = r.get("premium")
        cur = db.execute(
            "INSERT OR IGNORE INTO prints (ticker, strike, expiration, put_call, trade_date, "
            "trade_time_et, size, fill_price, premium, spot, dte_days, sentiment, "
            "activity_type, updated_utc, first_seen_utc, poll_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r.get("ticker") or ticker, r.get("strike_price"),
             (r.get("date_expiration") or "")[:10], r.get("put_call"),
             r.get("trade_date"), r.get("trade_time_et"), r.get("size"),
             r.get("fill_price"),
             round(premium, 2) if isinstance(premium, (int, float)) else None,
             r.get("spot"), r.get("dteDays"), r.get("sentiment"),
             r.get("option_activity_type"), r.get("updated"), seen, poll_id))
        if cur.rowcount:
            new.append(r)
    return new


def poll_whales(te, db, budget, et):
    """Whale watch: whale-size raw prints across the whole tape since a few minutes ago."""
    started = time.time()
    t_from = max(et - timedelta(minutes=WHALE_WINDOW_MIN), et.replace(hour=9, minute=30, second=0, microsecond=0))
    payload = te.call("get_option_flow", {
        "min_premium": pipeline.NOTEWORTHY_MIN_PREMIUM,
        "max_dte_days": MAX_DTE_DAYS,
        "time_from": t_from.strftime("%H:%M"),
        "limit": FLOW_LIMIT,
    })
    rows = pipeline._rows(payload, "rows")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    credits = int(meta.get("creditsCharged", 1))
    budget.charge(credits, started)
    poll_id = _insert_poll(db, "whale", "*", started, credits, rows_returned=len(rows), conn=2)
    new = _store_prints(db, rows, poll_id)
    flag = "possible_gap" if len(rows) >= FLOW_LIMIT and len(new) >= FLOW_LIMIT else None
    db.execute("UPDATE polls SET new_rows = ?, flag = ? WHERE id = ?", (len(new), flag, poll_id))
    db.commit()
    mine = [r for r in new if r.get("ticker") in pipeline.MY_TICKERS]
    return rows, new, mine, flag, t_from.strftime("%H:%M")


def poll_flow(te, db, budget, ticker, first_today):
    started = time.time()
    payload = te.call("get_option_flow", {
        "ticker": ticker,
        "max_dte_days": MAX_DTE_DAYS,
        "min_premium": TICKER_MIN_PREMIUM.get(ticker, MIN_PREMIUM),
        "limit": FLOW_LIMIT,
    })
    rows = pipeline._rows(payload, "rows")
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    credits = int(meta.get("creditsCharged", 1))
    budget.charge(credits, started)

    poll_id = _insert_poll(db, "flow", ticker, started, credits, rows_returned=len(rows))
    new = len(_store_prints(db, rows, poll_id, ticker))

    flag = None
    if len(rows) >= FLOW_LIMIT and new >= FLOW_LIMIT:
        flag = "startup_backlog" if first_today else "possible_gap"
    db.execute("UPDATE polls SET new_rows = ?, flag = ? WHERE id = ?", (new, flag, poll_id))
    db.commit()
    return rows, new, flag, credits


def poll_dealer(te, db, budget, ticker):
    started = time.time()
    payload = te.call("get_dealer_edge_data", {"ticker": ticker})
    data = payload.get("data") or {}
    credits = int((payload.get("meta") or {}).get("creditsCharged", 2))
    budget.charge(credits, started)
    poll_id = _insert_poll(db, "dealer_edge", ticker, started, credits)

    md = data.get("metadata") or {}
    levels = md.get("keyLevels") or {}
    vanna = md.get("vanna") or {}
    ivs = vanna.get("atmIvByExp") or []
    fetched = utc_iso()
    cur = db.execute(
        "INSERT INTO dealer_edge_snapshots (poll_id, ticker, fetched_utc, data_as_of, stale, "
        "spot, gex_rating, anchor, flip, charm_anchor, defense_lines, vanna_anchor, "
        "vanna_flip, vanna_walls, vanna_net_per_vol_pt, vanna_call_per_vol_pt, "
        "vanna_put_per_vol_pt, atm_iv_nearest, raw_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (poll_id, ticker, fetched, data.get("dataAsOf"),
         None if data.get("stale") is None else int(bool(data.get("stale"))),
         md.get("currentPrice"), md.get("gexRating"), levels.get("anchorPoint"),
         levels.get("flipPoint"), levels.get("charmAnchor"),
         json.dumps(levels.get("defenseLines")), levels.get("vannaAnchor"),
         levels.get("vannaFlip"), json.dumps(levels.get("vannaWalls")),
         vanna.get("netPerVolPt"), vanna.get("callPerVolPt"), vanna.get("putPerVolPt"),
         ivs[0].get("atmIv") if ivs else None, json.dumps(data)))
    snapshot_id = cur.lastrowid

    db.executemany(
        "INSERT INTO dealer_edge_iv (snapshot_id, ticker, expiration, atm_iv) VALUES (?, ?, ?, ?)",
        [(snapshot_id, ticker, iv.get("expiration"), iv.get("atmIv")) for iv in ivs])

    setups = {(s.get("key") or s.get("name")): s for s in data.get("setups") or []}
    db.executemany(
        "INSERT INTO setup_observations (snapshot_id, ticker, setup_key, name, state) "
        "VALUES (?, ?, ?, ?, ?)",
        [(snapshot_id, ticker, k, s.get("name"), s.get("state")) for k, s in setups.items()])
    changes = _record_setup_changes(db, ticker, snapshot_id, setups, fetched)
    db.commit()
    return md, levels, ivs, setups, changes, credits


def _record_setup_changes(db, ticker, snapshot_id, setups, at):
    prev = db.execute(
        "SELECT id FROM dealer_edge_snapshots WHERE ticker = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (ticker, snapshot_id)).fetchone()
    if prev is None:
        old = {}
    else:
        old = {k: (n, s) for k, n, s in db.execute(
            "SELECT setup_key, name, state FROM setup_observations WHERE snapshot_id = ?",
            (prev[0],))}
    changes = []
    for k, s in setups.items():
        if k not in old:
            changes.append((k, s.get("name"), "appeared", s.get("state")))
        elif old[k][1] != s.get("state"):
            changes.append((k, s.get("name"), "state_changed", s.get("state")))
    for k, (name, state) in old.items():
        if k not in setups:
            changes.append((k, name, "disappeared", state))
    db.executemany(
        "INSERT INTO setup_events (at_utc, ticker, setup_key, name, event, state) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(at, ticker, k, n, e, st) for k, n, e, st in changes])
    return changes


def poll_noteworthy(te, db, budget, et):
    """Live poll of the last few minutes of noteworthy flow."""
    started = time.time()
    t_from, t_to = pipeline.live_window(et)
    payload = te.call("get_noteworthy_flow",
                      pipeline.noteworthy_args(et.date().isoformat(), t_from, t_to))
    credits = int((payload.get("meta") or {}).get("creditsCharged", 2))
    budget.charge(credits, started)
    total, mine, new_ids, outside = pipeline.store_picks(db, payload, "live")
    poll_id = _insert_poll(db, "noteworthy", "*", started, credits, rows_returned=total)
    db.execute("UPDATE polls SET new_rows = ? WHERE id = ?", (len(new_ids), poll_id))
    db.commit()
    return total, mine, new_ids, outside, (t_from, t_to)


def make_budgeted_call(te, db, budget, alt=None):
    """call(tool, args, kind, ticker) that waits for credit room and obeys 429s. With `alt` =
    (client, budget) of the second connection, calls spill onto it once the main connection's
    100 are spent, so nightly work and the harvest can use the whole 150/hr user cap."""
    def pick(cost):
        now = time.time()
        if budget.allows(cost, HOURLY_STOP, now):
            return te, budget, None
        if alt and alt[1].allows(cost, WHALE_HOURLY_STOP, now):
            return alt[0], alt[1], 2
        return None

    def call(tool, arguments, kind, ticker):
        cost = TOOL_COST.get(tool, 2)
        for attempt in range(4):
            noted = False
            while (chosen := pick(cost)) is None:
                if not noted:
                    print(f"{_stamp()} Credit guard: waiting ({_budget_text(budget)}).")
                    noted = True
                time.sleep(5)
            client, bud, conn = chosen
            started = time.time()
            try:
                payload = client.call(tool, arguments)
            except RateLimited as e:
                record_rate_limit(db, tool, ticker, e.data)
                used, limit = e.data.get("creditsUsedConn"), e.data.get("creditsLimitConn")
                if isinstance(used, int) and isinstance(limit, int) and used >= limit - cost:
                    # this connection's hour is spent: mark it full and let pick() use the other
                    bud.sync_hour(HOURLY_STOP if conn is None else WHALE_HOURLY_STOP, time.time())
                    print(f"{_stamp()} RATE LIMITED ({tool}, connection {conn or 1} full for this hour).")
                    continue
                print(f"{_stamp()} RATE LIMITED ({tool}). Waiting {e.retry_after}s.")
                time.sleep(e.retry_after + 2)
                continue
            except (HttpError, RpcError, urllib.error.URLError, OSError, ValueError) as e:
                record_failed_poll(db, kind, ticker, started, e)
                client.reset()
                if attempt == 3:
                    raise
                print(f"{_stamp()} ERROR ({tool} {ticker}): {str(e)[:200]} - retrying in 30s")
                time.sleep(30)
                continue
            credits = int((payload.get("meta") or {}).get("creditsCharged", cost))
            bud.charge(credits, started)
            _insert_poll(db, kind, ticker, started, credits, conn=conn)
            db.commit()
            return payload
        raise RuntimeError(f"{tool} kept failing")
    return call


# --------------------------------------------------------------- scheduler --

@dataclass
class Job:
    kind: str
    ticker: str
    interval: int
    priority: int      # 0 = SPY/QQQ flow + noteworthy, 1 = other flow, 2 = Dealer Edge
    cost: int
    next_due: float


@dataclass
class DayState:
    day: date
    jobs: list = None
    confirmed_open: bool = False
    closed: bool = False
    after_close_done: bool = False
    first_poll_done: set = field(default_factory=set)
    next_backtest: float = 0.0     # Simulator/Analyst/Judges re-run every 30 min in session
    scout_hour: int = -1           # last ET hour Scout reported
    whale_on: bool = True          # off for the day if the 2nd connection turns out to share the 100 cap


def build_jobs(start):
    jobs = [Job("noteworthy", "*", NOTEWORTHY_INTERVAL_SEC, 0, 2, start + 15),
            Job("whale", "*", WHALE_INTERVAL_SEC, 0, 1, start + 50)]
    for i, t in enumerate(FAST_TICKERS):
        jobs.append(Job("flow", t, FAST_INTERVAL_SEC, 0, 1, start + i * FAST_STAGGER_SEC))
    for i, t in enumerate(SLOW_TICKERS):
        jobs.append(Job("flow", t, SLOW_INTERVAL_SEC, 1, 1, start + 30 + i * SLOW_STAGGER_SEC))
    hourly = [t for t in FAST_TICKERS + SLOW_TICKERS if t in DEALER_HOURLY_TICKERS]
    two_hourly = [t for t in FAST_TICKERS + SLOW_TICKERS if t not in DEALER_HOURLY_TICKERS]
    for i, t in enumerate(hourly):          # one every 10 min across the hour
        jobs.append(Job("dealer_edge", t, DEALER_FAST_INTERVAL_SEC, 2, 2,
                        start + 45 + i * DEALER_STAGGER_SEC))
    step = DEALER_INTERVAL_SEC // max(len(two_hourly), 1)
    for i, t in enumerate(two_hourly):      # spread evenly across two hours
        jobs.append(Job("dealer_edge", t, DEALER_INTERVAL_SEC, 2, 2, start + 345 + i * step))
    return jobs


def _stamp():
    return now_et().strftime("%H:%M:%S")


_whale_budget = None   # set by run_logger when the whale watch connection exists


def _budget_text(budget):
    now = time.time()
    whale = (f" whale={_whale_budget.hour_used}/{WHALE_HOURLY_STOP}" if _whale_budget else "")
    return (f"credits hr={budget.hour_used}/{HOURLY_STOP}{whale} "
            f"5m={budget.burst_used(now)}/{BURST_STOP}")


def update_market_state(state, rows, et):
    """Use SPY prints to detect market holidays and early closes."""
    today = et.date().isoformat()
    today_times = sorted(r.get("trade_time_et") or "" for r in rows if r.get("trade_date") == today)
    if not state.confirmed_open:
        if today_times:
            state.confirmed_open = True
            print(f"{_stamp()} Market open confirmed (SPY printed today).")
            pipeline.discord_send(f"🛰️ **Scout:** market open confirmed for {et:%a %b %d} - capturing flow "
                                  "and noteworthy picks. Herald will ping qualifying picks here and in "
                                  "#herald-pings.", bot="scout")
        elif et.time() >= HOLIDAY_CHECK_AT:
            state.closed = True
            print(f"{_stamp()} No SPY prints today by 9:45 ET - treating as a market holiday.")
        return
    if et.time() >= EARLY_CLOSE_CHECK_AFTER:
        quiet = True
        if today_times:
            try:
                h, m, s = (int(x) for x in today_times[-1].split(":"))
                last = et_datetime(et.date(), dtime(h, m, s))
                quiet = et - last > timedelta(minutes=EARLY_CLOSE_QUIET_MIN)
            except ValueError:
                quiet = False
        if quiet:
            state.closed = True
            print(f"{_stamp()} SPY quiet for {EARLY_CLOSE_QUIET_MIN}+ min after 1 PM ET - "
                  "treating as an early close.")


def run_job(job, state, te, db, budget, whale=None):
    """Run one job. Returns an epoch to pause all polling until, or 0.
    `whale` = (client, budget) of the second connection used by the whale watch."""
    now = time.time()
    tool = {"flow": "get_option_flow", "dealer_edge": "get_dealer_edge_data",
            "noteworthy": "get_noteworthy_flow", "whale": "get_option_flow"}[job.kind]
    try:
        if job.kind == "whale":
            rows, new, mine, flag, t_from = poll_whales(whale[0], db, whale[1], now_et())
            print(f"{_stamp()} WHALES {t_from}-now rows={len(rows)} new={len(new)} yours={len(mine)}  "
                  f"{_budget_text(budget)}" + ("  << POSSIBLE GAP" if flag else ""))
            for r in mine:
                print(f"{_stamp()}   whale spotted: {r.get('ticker')} {r.get('strike_price')} {r.get('put_call')} "
                      f"{(r.get('date_expiration') or '')[:10]} at {r.get('trade_time_et')} "
                      f"${(r.get('premium') or 0) / 1e3:,.0f}K {r.get('option_activity_type') or ''}")
        elif job.kind == "noteworthy":
            total, mine, new_ids, outside, (t_from, t_to) = poll_noteworthy(te, db, budget, now_et())
            print(f"{_stamp()} NOTEWORTHY {t_from}-{t_to} picks={total} yours={mine} "
                  f"new={len(new_ids)}  {_budget_text(budget)}")
            if outside:
                print(f"{_stamp()} WARNING {outside} noteworthy rows were outside the requested "
                      "time window - the server may be ignoring time_from/time_to.")
            pipeline.handle_new_picks(db, new_ids, live=True)
        elif job.kind == "flow":
            first = job.ticker not in state.first_poll_done
            rows, new, flag, _ = poll_flow(te, db, budget, job.ticker, first)
            state.first_poll_done.add(job.ticker)
            note = {"possible_gap": "  << POSSIBLE GAP",
                    "startup_backlog": "  (startup backlog)"}.get(flag, "")
            print(f"{_stamp()} FLOW   {job.ticker:<5} rows={len(rows):>2} new={new:>2}  "
                  f"{_budget_text(budget)}{note}")
            if flag == "possible_gap":
                print(f"{_stamp()} WARNING possible gap for {job.ticker}: all {FLOW_LIMIT} rows "
                      "were new - poll it faster or raise its premium floor.")
            if job.ticker == "SPY":
                update_market_state(state, rows, now_et())
                if not state.confirmed_open and not state.closed:
                    job.next_due = now + 60  # re-check every minute so picks aren't held back
                    return 0
        else:
            md, levels, ivs, setups, changes, _ = poll_dealer(te, db, budget, job.ticker)
            iv = f"{ivs[0].get('atmIv')}" if ivs else "-"
            print(f"{_stamp()} DEALER {job.ticker:<5} spot={md.get('currentPrice')} "
                  f"gex={md.get('gexRating')} flip={levels.get('flipPoint')} "
                  f"charm={levels.get('charmAnchor')} atmIV={iv} setups={len(setups)}  "
                  f"{_budget_text(budget)}")
            for _, name, event, st in changes:
                print(f"{_stamp()}   setup {event}: {job.ticker} {name} ({st})")
        job.next_due = now + job.interval
        return 0
    except RateLimited as e:
        record_rate_limit(db, tool, job.ticker, e.data)
        used = e.data.get("creditsUsedConn")
        if job.kind == "whale":   # only the whale watch waits; the main connection carries on
            if isinstance(used, int):
                whale[1].sync_hour(used, time.time())
            print(f"{_stamp()} RATE LIMITED (whale watch): used conn/user = {used}/"
                  f"{e.data.get('creditsUsedUser')}; whale watch waits {e.retry_after}s")
            job.next_due = now + e.retry_after + 2
            return 0
        if isinstance(used, int) and used > budget.hour_used + 5 and state.whale_on:
            # the server counts more on this connection than it made: the whale watch's credits
            # land on the same counter, so it would starve the noteworthy polls - turn it off
            state.whale_on = False
            print(f"{_stamp()} Whale watch OFF for today: the server counts {used} credits on the main "
                  f"connection but it made {budget.hour_used} - both connections share one counter.")
            pipeline.discord_send("⚠️ **Whale watch turned off for today**: Trade Echo counts the second "
                                  "connection against the main one's 100 credits/hour, so it would slow the "
                                  "noteworthy polls. Pings are unaffected.", bot="auditor")
        if isinstance(used, int):
            budget.sync_hour(used, time.time())
        print(f"{_stamp()} RATE LIMITED on {job.ticker} ({e}). Server says wait "
              f"{e.retry_after}s. used conn/user/burst = {e.data.get('creditsUsedConn')}/"
              f"{e.data.get('creditsUsedUser')}/{e.data.get('creditsUsedBurst')}, limits = "
              f"{e.data.get('creditsLimitConn')}/{e.data.get('creditsLimitUser')}/"
              f"{e.data.get('creditsLimitBurst')}")
        job.next_due = now + e.retry_after + 2
        return now + e.retry_after + 2
    except ToolError as e:
        record_failed_poll(db, job.kind, job.ticker, now, e)
        print(f"{_stamp()} ERROR  {job.ticker} tool error: {e}")
        job.next_due = now + job.interval
        return 0
    except (HttpError, RpcError, urllib.error.URLError, OSError, ValueError) as e:
        record_failed_poll(db, job.kind, job.ticker, now, e)
        (whale[0] if job.kind == "whale" else te).reset()
        print(f"{_stamp()} ERROR  {job.kind} {job.ticker} {type(e).__name__}: {str(e)[:300]} "
              "- will reconnect and retry in 60s")
        job.next_due = now + 60
        return 0 if job.kind == "whale" else now + 15


def sleep_until(target):
    while True:
        remaining = target - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


_awake = None


def keep_awake(on):
    """Ask Windows not to idle-sleep while the logger is working (like a video player does).
    No system settings are changed; the request ends when `on` is False or the logger exits."""
    global _awake
    if on == _awake or sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    _awake = on
    print(f"{_stamp()} {'Keeping the PC awake while the logger runs' if on else 'PC may sleep again'}.")


_lock_handle = None


def acquire_single_instance_lock():
    """Exit if another logger is already running (two would double the credit use)."""
    global _lock_handle
    import msvcrt
    _lock_handle = open(Path(__file__).with_name("flow_logger.lock"), "a+")
    try:
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit("Another flow logger is already running - not starting a second one.")


def acquire_single_instance_lock_named(name):
    """Separate lock so two manual harvests can't run at once (doesn't block the logger)."""
    global _lock_handle
    import msvcrt
    _lock_handle = open(Path(__file__).with_name(f"{name}.lock"), "a+")
    try:
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit(f"Another {name} is already running.")


def run_logger():
    acquire_single_instance_lock()
    token = load_token()
    db = open_db()
    global _whale_budget
    budget = CreditBudget.from_db(db)
    _whale_budget = CreditBudget.from_db(db, whale=True, recent=budget.recent)
    te = TradeEcho(token)
    te._connect()  # fail fast if the server or tools are wrong
    te_whale = TradeEcho(token)   # second connection (its own session) for the whale watch
    print(f"Logger started. Tools verified read-only: {', '.join(sorted(ALLOWED_TOOLS))}.")
    print(f"Saving to {DB_PATH.name}. {_budget_text(budget)}. Press Ctrl+C to stop.")
    keep_awake(True)          # 24/7 while the logger runs; Windows lifts it when we exit
    import dashboard
    dashboard.start_in_background()  # http://localhost:8050
    pipeline.warm_up_hermes()  # load Hermes onto the GPU now so live picks are judged instantly

    state = None
    pause_until = 0.0
    waiting_note = False
    while True:
        now = time.time()
        et = now_et()
        if state is None or state.day != et.date():
            state = DayState(et.date())

        if (state.confirmed_open and not state.after_close_done
                and (state.closed or et.time() >= SESSION_CLOSE)):
            state.after_close_done = True
            day = state.day.isoformat()
            if not pipeline.after_close_done(db, day):
                try:
                    pipeline.run_after_close(db, make_budgeted_call(te, db, budget, (te_whale, _whale_budget)), day)
                except Exception as e:  # never let the nightly step kill the logger
                    print(f"{_stamp()} After-close run failed: {type(e).__name__}: {e}. "
                          f"Retry with: py flow_logger.py --after-close {day}")
                run_ibkr(db)  # free 1-minute prices from TWS (no Trade Echo credits)
                try:
                    import integrity
                    results = integrity.check_and_alert(db, now_et(), False, after_close_expected=True)
                    print(integrity.format_report(results))
                    pipeline.discord_send(integrity.summary_line(results), bot="auditor")
                    integrity.ai_audit(db, day)  # local Qwen review of today's picks
                except Exception as e:
                    print(f"{_stamp()} Integrity check failed to run: {type(e).__name__}: {e}")
                try:
                    import bots
                    pipeline.discord_send(bots.scout_hour(db, hours=8), bot="scout")
                except Exception as e:
                    print(f"{_stamp()} Scout summary failed: {type(e).__name__}: {e}")
                run_overnight_harvest(db, te, budget, state.day, alt=(te_whale, _whale_budget))
                run_ibkr(db)  # price the picks the harvest just added
                try:
                    import bots
                    bots.run_backtests(db, force_post=True)
                except Exception as e:
                    print(f"{_stamp()} Nightly bot reports failed: {type(e).__name__}: {e}")
            continue

        if state.closed or not in_session(et):
            nxt = next_open_et(et, skip_today=state.closed)
            print(f"{_stamp()} Outside market hours. Next session: {nxt:%a %Y-%m-%d %H:%M} ET.")
            sleep_until(nxt.timestamp())
            continue

        if state.jobs is None:
            state.jobs = build_jobs(now)
            print(f"{_stamp()} Session {state.day} started. Checking SPY first to confirm "
                  "the market is open.")

        if now < pause_until:
            time.sleep(min(pause_until - now, 30))
            continue

        if state.confirmed_open:
            run_periodic_bots(db, state, et, now)

        eligible = [j for j in state.jobs
                    if (state.confirmed_open and (j.kind != "whale" or state.whale_on))
                    or (j.kind == "flow" and j.ticker == "SPY")]
        due = sorted((j for j in eligible if j.next_due <= now),
                     key=lambda j: (j.priority, j.next_due))
        if not due:
            upcoming = min(j.next_due for j in eligible)
            time.sleep(min(max(upcoming - now, 0.5), 30))
            continue

        job = due[0]
        if job.kind == "whale":
            bud, cap = _whale_budget, WHALE_HOURLY_STOP
            if not bud.allows(job.cost, cap, now) and bud.hour_used + job.cost > cap:
                job.next_due = (now // 3600 + 1) * 3600 + 5   # its 50 are spent: wait for the new hour
                continue
        else:
            bud, cap = budget, (HOURLY_STOP if job.priority < 2 else DEALER_HOURLY_STOP)
        if not bud.allows(job.cost, cap, now):
            if not waiting_note:
                print(f"{_stamp()} Credit guard: holding calls ({_budget_text(budget)}).")
                waiting_note = True
            time.sleep(5)
            continue
        waiting_note = False
        pause_until = run_job(job, state, te, db, budget, whale=(te_whale, _whale_budget))


def harvest_sessions(last_day):
    """Weekday sessions still inside Trade Echo's ~7-day option price history, oldest first."""
    days, d = [], last_day
    while (last_day - d).days < pipeline.PRICE_HISTORY_DAYS:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(days)


def run_ibkr(db):
    """IBKR 1-minute after-print prices for still-listed picks. Skipped if TWS isn't open;
    never allowed to stop the logger. Stops 15 min before the open."""
    try:
        import ibkr
        deadline = next_open_et(now_et(), skip_today=now_et().time() >= SESSION_OPEN).timestamp() - 15 * 60
        ibkr.price_picks(db, deadline=deadline)
        ibkr.price_expiries(db, deadline=deadline)
        ibkr.price_holding_days(db, deadline=deadline)
        ibkr.price_spreads(db, deadline=deadline)
        ibkr.price_stock_iv(db, deadline=deadline)
        ibkr.price_underlyings(db, deadline=deadline)
        ibkr.refresh_greeks(db)
    except Exception as e:
        print(f"{_stamp()} IBKR pricing failed: {type(e).__name__}: {e}")
        pipeline.discord_send(f"🛑 **IBKR pricing failed** ({type(e).__name__}) - check TWS is open and unlocked.",
                              bot="auditor")


def run_overnight_harvest(db, te, budget, last_day, alt=None):
    """Spend idle overnight credits on extra REAL training data (both connections when `alt` is
    given: up to 150/hr). Stops at 9:00 ET so the credit hour containing the 9:30 open (credits
    reset on the UTC hour = 9:00 ET) is left untouched."""
    deadline = next_open_et(now_et(), skip_today=now_et().time() >= SESSION_OPEN).timestamp() - 30 * 60
    try:
        pipeline.discord_send("🌙 **Scout:** overnight harvest starting - pulling $100K+ noteworthy picks and "
                              "Algo Edge alerts (training only) with idle credits; stops at 9:00 AM ET so "
                              "the opening hour keeps its full credits.", bot="scout")
        pipeline.run_harvest(db, make_budgeted_call(te, db, budget, alt), harvest_sessions(last_day), deadline)
        total = db.execute("SELECT COUNT(*), SUM(day_stats_status = 'ok') FROM picks").fetchone()
        pipeline.discord_send(f"🌙 **Scout:** harvest done - training data now {total[1] or 0} priced picks "
                              f"({total[0]} stored).", bot="scout")
    except Exception as e:  # never let the harvest kill the logger
        print(f"{_stamp()} Overnight harvest failed: {type(e).__name__}: {e}")


def run_periodic_bots(db, state, et, now):
    """Scout's hourly report and the 30-minute Simulator/Analyst/Judges re-runs (CPU, seconds)."""
    import bots
    try:
        if et.minute < 5 and et.hour != state.scout_hour and et.hour > 9:
            state.scout_hour = et.hour
            pipeline.discord_send(bots.scout_hour(db), bot="scout")
        if now >= state.next_backtest:
            state.next_backtest = now + 1800
            bots.run_backtests(db)
    except Exception as e:  # reports must never stop data capture
        print(f"{_stamp()} Bot reports failed: {type(e).__name__}: {e}")


def run_integrity_check(db, send=True):
    """Run the integrity checks; returns 1 if anything FAILed (for the scheduled task's exit code)."""
    import integrity
    et = now_et()
    session = in_session(et)
    # prices/nightly-run checks only make sense once the nightly run should be done
    nightly_window = dtime(16, 15) <= et.time() < dtime(18, 0) and et.weekday() < 5
    results = integrity.check_and_alert(db, et, session, after_close_expected=not session and not nightly_window,
                                        send=send)
    print(integrity.format_report(results))
    print(integrity.summary_line(results))
    return 1 if any(s == integrity.FAIL for _, s, _ in results) else 0


def run_once(kind, ticker):
    db = open_db()
    budget = CreditBudget.from_db(db)
    cost, cap = {"flow": (1, HOURLY_STOP), "noteworthy": (2, HOURLY_STOP)}.get(
        kind, (2, DEALER_HOURLY_STOP))
    if not budget.allows(cost, cap, time.time()):
        sys.exit(f"Credit guard: not calling ({_budget_text(budget)}).")
    te = TradeEcho(load_token())
    job = Job(kind, ticker.upper(), 0, 0, cost, 0)
    state = DayState(now_et().date(), confirmed_open=True)
    run_job(job, state, te, db, budget)


def show_status():
    db = open_db()
    today = now_et().date().isoformat()
    print(f"flow.db status for {today} (ET)\n")
    print("Prints captured today:")
    for ticker, n, prem in db.execute(
            "SELECT ticker, COUNT(*), ROUND(SUM(premium)) FROM prints WHERE trade_date = ? "
            "GROUP BY ticker ORDER BY ticker", (today,)):
        print(f"  {ticker:<5} {n:>6} prints  ${prem:,.0f} premium")
    print("\nPossible gaps today:")
    gaps = db.execute(
        "SELECT started_utc, ticker FROM polls WHERE flag = 'possible_gap' AND started_utc >= ? "
        "ORDER BY started_utc", (today,)).fetchall()
    for at, ticker in gaps:
        print(f"  {at}  {ticker}")
    if not gaps:
        print("  none")
    budget = CreditBudget.from_db(db)
    print(f"\nThis UTC hour: {_budget_text(budget)}")
    last = db.execute("SELECT at_utc, retry_after_sec, used_burst, limit_burst, used_conn, "
                      "limit_conn FROM rate_limit_events ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        print(f"Last rate limit: {last[0]} retry={last[1]}s burst {last[2]}/{last[3]} "
              f"conn {last[4]}/{last[5]}")


def main():
    parser = argparse.ArgumentParser(description="Trade Echo options-flow capture logger")
    parser.add_argument("--once", metavar="TICKER", help="one flow poll, then exit")
    parser.add_argument("--once-dealer", metavar="TICKER", help="one Dealer Edge poll, then exit")
    parser.add_argument("--once-noteworthy", action="store_true",
                        help="one live noteworthy poll, then exit")
    parser.add_argument("--after-close", nargs="?", const="today", metavar="YYYY-MM-DD",
                        help="sweep noteworthy picks, label outcomes and retrain for a day")
    parser.add_argument("--backfill", type=int, metavar="DAYS",
                        help="pull, judge and label the last DAYS sessions of noteworthy picks")
    parser.add_argument("--rebuild", metavar="LAST_SESSION",
                        help="clear all derived results and recompute every pick (prices, Greeks, "
                             "Hermes, model) using price history up to LAST_SESSION")
    parser.add_argument("--expand", nargs=3, metavar=("FROM", "TO", "LAST_SESSION"),
                        help="re-fetch past sessions keeping all tickers' picks, record prices, "
                             "re-judge everything and retrain")
    parser.add_argument("--ai-audit", nargs="?", const="today", metavar="YYYY-MM-DD",
                        help="local Qwen audit of a day's picks (no cloud, no credits)")
    parser.add_argument("--harvest", metavar="LAST_SESSION",
                        help="overnight harvest of extra training data for sessions up to LAST_SESSION")
    parser.add_argument("--ibkr", action="store_true",
                        help="price still-listed picks from IBKR 1-minute bars (TWS must be open)")
    parser.add_argument("--ibkr-live", action="store_true",
                        help="during market hours, update today's picks' minute-by-minute Greeks from IBKR")
    parser.add_argument("--bots", action="store_true",
                        help="run Simulator, Analyst and Judges now and post their reports")
    parser.add_argument("--dashboard", action="store_true",
                        help="run only the dashboard (http://localhost:8050)")
    parser.add_argument("--check", action="store_true",
                        help="run the data-integrity checks (posts problems to Discord)")
    parser.add_argument("--rejudge", action="store_true",
                        help="re-run Hermes on all picks, oldest first (no API calls)")
    parser.add_argument("--train", action="store_true", help="retrain the model (no API calls)")
    parser.add_argument("--report", action="store_true", help="picks/model report (no API calls)")
    parser.add_argument("--test-discord", action="store_true", help="send one test message")
    parser.add_argument("--discord-history", nargs="*", metavar="YYYY-MM-DD",
                        help="post stored picks to Discord: no dates = all, or FROM [TO]")
    parser.add_argument("--fetch-picks", nargs=2, metavar=("FROM", "TO"),
                        help="pull picks for past sessions without price checks")
    parser.add_argument("--status", action="store_true", help="summary from flow.db")
    args = parser.parse_args()
    try:
        if args.status:
            show_status()
        elif args.report:
            print(pipeline.report(open_db()))
        elif args.expand:
            db = open_db()
            te = TradeEcho(load_token())
            pipeline.expand_training_data(db, make_budgeted_call(te, db, CreditBudget.from_db(db)),
                                          *args.expand)
        elif args.ai_audit:
            import integrity
            day = now_et().date().isoformat() if args.ai_audit == "today" else args.ai_audit
            integrity.ai_audit(open_db(), day)
        elif args.harvest:
            acquire_single_instance_lock_named("harvest")
            db = open_db()
            run_overnight_harvest(db, TradeEcho(load_token()), CreditBudget.from_db(db),
                                  date.fromisoformat(args.harvest))
        elif args.ibkr:
            acquire_single_instance_lock_named("ibkr")
            run_ibkr(open_db())
        elif args.ibkr_live:
            acquire_single_instance_lock_named("ibkr_live")
            import ibkr
            ibkr.run_live(open_db(), in_session, lambda et: next_open_et(et, skip_today=et.time() >= SESSION_OPEN),
                          pipeline.MY_TICKERS, log=lambda s: print(s, flush=True))
        elif args.bots:
            import bots
            bots.run_backtests(open_db(), force_post=True)
        elif args.dashboard:
            import dashboard
            dashboard.start_in_background()
            while True:
                time.sleep(3600)
        elif args.check:
            sys.exit(run_integrity_check(open_db()))
        elif args.rejudge:
            pipeline.rejudge(open_db())
        elif args.train:
            db = open_db()
            print(pipeline._describe(pipeline.train(db)))
            print(pipeline.report(db))
        elif args.test_discord:
            jobs = {"herald": "trade pings", "scout": "live capture and hourly updates",
                    "judges": "Hermes & Qwen accuracy", "arena": "nightly model contest",
                    "simulator": "backtest results", "analyst": "what predicts gains",
                    "auditor": "data integrity checks"}
            for bot, job in jobs.items():
                name, var = pipeline.BOTS[bot]
                own = pipeline._webhook_url(var) is not None
                ok = pipeline.discord_send(f"👋 {name} connected - this channel gets my {job}."
                                           + ("" if own else " (No channel of my own yet, so I'm "
                                              "posting in the main channel.)"), bot=bot)
                print(f"  {name:14s} {'sent' if ok else 'NOT sent'} -> "
                      f"{'its own channel' if own else 'main channel (no webhook yet)'}")
        elif args.discord_history is not None:
            dates = args.discord_history + [None, None]
            print(f"Sent {pipeline.send_history(open_db(), dates[0], dates[1])} message(s).")
        elif args.fetch_picks:
            db = open_db()
            te = TradeEcho(load_token())
            pipeline.fetch_picks_only(db, make_budgeted_call(te, db, CreditBudget.from_db(db)),
                                      *args.fetch_picks)
        elif args.once_noteworthy:
            run_once("noteworthy", "*")
        elif args.rebuild:
            db = open_db()
            te = TradeEcho(load_token())
            pipeline.rebuild(db, make_budgeted_call(te, db, CreditBudget.from_db(db)), args.rebuild)
        elif args.backfill:
            db = open_db()
            te = TradeEcho(load_token())
            pipeline.run_backfill(db, make_budgeted_call(te, db, CreditBudget.from_db(db)),
                                  args.backfill, now_et().date())
        elif args.after_close:
            day = now_et().date().isoformat() if args.after_close == "today" else args.after_close
            db = open_db()
            te = TradeEcho(load_token())
            pipeline.run_after_close(db, make_budgeted_call(te, db, CreditBudget.from_db(db)), day)
        elif args.once:
            run_once("flow", args.once)
        elif args.once_dealer:
            run_once("dealer_edge", args.once_dealer)
        else:
            run_logger()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
