"""Noteworthy-flow picks for your tickers: storage, price outcomes, Greeks, daily model,
Hermes second opinion and Discord pings.

A pick is a get_noteworthy_flow print on one of your tickers with score > 30,
0-14 DTE and premium >= $350K. For every pick we record what the option actually
did, from Trade Echo daily bars for the contract:
  - on the print day: high (max gain), low (max loss), average (VWAP) and close
  - on the expiry day: the same four numbers
Greeks (IV, delta, gamma, theta, vega) are computed locally with Black-Scholes from
the fill price and the stock price at the print (greeks.py) -- no credits.

The model learns to predict the honest after-print gain (true_gain_pct): the best bid after
the fill minute, from IBKR 1-minute bars. Hermes and Qwen estimate it too.

Called from flow_logger.py; nothing here talks to Trade Echo directly -- it uses
the budgeted `call(tool, args, kind, ticker)` function the logger passes in.
"""

import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import greeks as bs

# ---------------------------------------------------------------- settings --

MY_TICKERS = {"SPY", "QQQ", "NVDA", "INTC", "DRAM", "MU", "META", "CRWV", "HIMS",
              "TSLA", "GOOG", "PLTR"}
INDEX_TICKERS = {"SPY", "QQQ"}

NOTEWORTHY_MIN_SCORE = 30        # server keeps scores above this
NOTEWORTHY_MAX_DTE = 14
NOTEWORTHY_MIN_PREMIUM = 350000  # the Discovery page floor
NOTEWORTHY_LIMIT = 25            # server cap
LIVE_WINDOW_MIN = 20             # each live poll asks for the last 20 minutes
SWEEP_WINDOW_MIN = 45            # after-close sweep of the whole session

PRICE_HISTORY_DAYS = 7           # Trade Echo keeps ~7 calendar days of option bars
MAX_STATS_TRIES = 2

PINGS_START = date(2026, 10, 1)  # user turned pings on early to watch the model learn
PING_MIN_GAIN = 0.30             # ping when the model predicts a day max gain >= +30%
PING_MAX_AGE_MIN = 20            # never ping a stale pick
MODEL_MIN_ROWS = 25

# Two local AI judges. Both see the same facts and memory; each gets its own track record.
# They don't fit on the GPU together, so Ollama swaps them (~3-4 s); live picks run the judge
# the winning method needs first, and batches run one judge at a time.
JUDGES = {
    "hermes": {"model": "hermes3", "num_ctx": 4096, "extra": {}},
    "qwen": {"model": "qwen3:14b", "num_ctx": 4096, "extra": {"think": False}},
}
HERMES_MODEL = JUDGES["hermes"]["model"]
HERMES_EXAMPLES = 12             # past picks (earlier days only) shown to each judge
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
HERMES_TIMEOUT_SEC = 180
CONFIRMED_WEIGHT = 1.0           # training weight when the gain was seen AFTER the print
UNCONFIRMED_WEIGHT = 0.5         # day high only -- it may have happened before the print
# Honest training target (true_gain_pct): IBKR's best BID after the fill minute where we have it
# (full weight); else Trade Echo's day high only when it's known to come after the print
# (lower weight, it's a trade price not a bid); unconfirmed day highs are not used at all.
TRUE_WEIGHTS = {"ibkr": 1.0, "te_confirmed": 0.4}
TARGET_NAME = "after_print_gain"
# The trade we grade everything on: buy at the ask right after the whale, sell at +30% / -50% / 4 PM
TRADE_TARGET, TRADE_STOP = 0.30, -0.50
FEE_PER_CONTRACT = 0.65                 # IBKR options commission, each way

MODEL_DIR = Path(__file__).with_name("models")
KNOWN_FLAGS = ["algo_edge", "sweep_or_block", "aggressive_execution"]
FEATURES = ["score", "log_premium", "log_fill", "dte", "hours_to_expiry", "is_call", "is_index",
            "minutes_since_open", *[f"flag_{f}" for f in KNOWN_FLAGS], "other_flags",
            "otm_pct", "iv", "abs_delta", "theta_pct", "vega_pct", "gamma_x_spot",
            "gex_rating", "flip_dist_pct", "charm_dist_pct", "atm_iv", "n_setups",
            "repeat_30m", "flow_call_share_30m", "hermes_est", "qwen_est", "is_my_ticker", "from_algo", "from_whale",
            # trade context (trade_context.py): +1 whale bought at the ask, -1 sold at the bid, 0 mid;
            # one leg of a multi-leg order; this contract was the leg the whale SOLD
            "side_buy", "is_multileg", "leg_sold",
            # market context from IBKR 1-minute stock bars, BEFORE the print minute only; "with"
            # = signed in the trade's direction (stock up helps a call, down helps a put)
            "stock_with_30m", "stock_with_day", "stock_range_30m", "spy_with_30m", "spy_with_day",
            # stock implied volatility (IBKR, previous close only): level, 1-year rank, and how
            # expensive this contract is vs the stock's normal IV
            "stock_iv_prev", "stock_iv_rank_1y", "iv_vs_stock_iv"]
CONTEXT_MAX_STALE_MIN = 10       # latest bar must be this close to the print, else no context

SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    id                INTEGER PRIMARY KEY,
    trade_date        TEXT NOT NULL,
    trade_time_et     TEXT NOT NULL,          -- HH:MM (server gives minutes only)
    ticker            TEXT NOT NULL,
    contract          TEXT NOT NULL,          -- e.g. '1070C 2026-10-02'
    strike            REAL,
    put_call          TEXT,                   -- CALL / PUT
    expiration        TEXT,
    dte_days          INTEGER,
    size              INTEGER,
    fill_price        REAL,
    premium           REAL,
    score             REAL,                   -- score when first seen
    last_score        REAL,                   -- latest score seen
    flags             TEXT,                   -- JSON list
    line              TEXT,
    source            TEXT,                   -- live / sweep / backfill / history
    first_seen_utc    TEXT NOT NULL,
    raw_json          TEXT,
    pinged_utc        TEXT,
    UNIQUE (trade_date, ticker, contract, trade_time_et, premium, fill_price)
);

CREATE TABLE IF NOT EXISTS model_runs (
    id             INTEGER PRIMARY KEY,
    run_utc        TEXT NOT NULL,
    status         TEXT NOT NULL,             -- trained / waiting_for_data
    version        TEXT,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS harvest_runs (
    trade_date   TEXT PRIMARY KEY,           -- overnight training-data harvest finished for this session
    finished_utc TEXT NOT NULL,
    summary      TEXT
);

CREATE TABLE IF NOT EXISTS replay_preds (
    pick_id   INTEGER PRIMARY KEY,           -- the winner's prediction made using only earlier days
    kind      TEXT NOT NULL,
    pred      REAL NOT NULL,
    version   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_runs (
    trade_date     TEXT PRIMARY KEY,
    finished_utc   TEXT NOT NULL,
    summary        TEXT
);
"""

# Derived columns: everything here is recomputed by --rebuild.
DERIVED_COLUMNS = {
    "picks": {
        # print-day prices
        "day_open": "REAL", "day_high": "REAL", "day_low": "REAL", "day_vwap": "REAL",
        "day_close": "REAL", "day_volume": "REAL",
        "max_gain_pct": "REAL", "max_loss_pct": "REAL", "avg_pct": "REAL", "close_pct": "REAL",
        "day_stats_status": "TEXT", "day_stats_tries": "INTEGER NOT NULL DEFAULT 0",
        # expiry-day prices
        "exp_open": "REAL", "exp_high": "REAL", "exp_low": "REAL", "exp_vwap": "REAL",
        "exp_close": "REAL", "exp_high_pct": "REAL", "exp_low_pct": "REAL", "exp_avg_pct": "REAL",
        "exp_close_pct": "REAL",
        "exp_stats_status": "TEXT", "exp_stats_tries": "INTEGER NOT NULL DEFAULT 0",
        # Greeks at the print (computed locally)
        "spot_at_print": "REAL", "iv": "REAL", "delta": "REAL", "gamma": "REAL", "theta": "REAL",
        "vega": "REAL", "theta_dollars_day": "REAL",
        "spot_source": "TEXT",       # 'algo' (alert's own), 'ibkr_minute', 'flow_print' or 'dealer_edge'
        # opinions
        "pred_max_gain": "REAL", "model_version": "TEXT",
        "hermes_verdict": "TEXT", "hermes_expected_gain": "REAL", "hermes_confidence": "REAL",
        "hermes_reason": "TEXT", "hermes_error": "TEXT",
        "qwen_verdict": "TEXT", "qwen_expected_gain": "REAL", "qwen_confidence": "REAL",
        "qwen_reason": "TEXT", "qwen_error": "TEXT",
        # best price seen in captured prints AFTER the print time (confirms the gain was real)
        "max_after_pct": "REAL",
        # honest outcome (what a trader could really get AFTER the print); see update_true_outcomes
        "true_gain_pct": "REAL", "true_loss_pct": "REAL", "true_close_pct": "REAL", "true_source": "TEXT",
        # expiry day from IBKR: closing bid (what holding to expiry really returned) and best bid
        "true_exp_close_pct": "REAL", "true_exp_best_pct": "REAL",
        # the realistic TRADE result (see trade_result): buy at the ask right after the print, sell at
        # +30% / -50% / the 4 PM bid, after fees - what following the whale would really have earned
        "trade_ret": "REAL", "trade_exit": "TEXT",
        # which strategy (strategies.py) the model and each judge would trade this pick with
        "pred_strategy": "TEXT", "hermes_strategy": "TEXT", "qwen_strategy": "TEXT",
    },
    "replay_preds": {"strategy": "TEXT"},
    "model_runs": {
        "target": "TEXT", "n_rows": "INTEGER", "base_rate_hit": "REAL", "cv_mae": "REAL",
        "cv_spearman": "REAL", "cv_hit_rate": "REAL", "cv_n_hit": "INTEGER",
        "model_kind": "TEXT", "ping_threshold": "REAL", "leaderboard": "TEXT",
        # blind-test profit of the winner's picks (trade_ret): average per trade, total, how many
        "cv_profit_avg": "REAL", "cv_profit_total": "REAL", "cv_n_taken": "INTEGER", "base_profit_avg": "REAL",
    },
}
# Old +20% columns kept in existing databases but no longer used.
OLD_COLUMNS = ["label", "label_utc", "label_tries", "max_print_after", "close_return", "model_prob"]


def ensure_schema(db):
    db.executescript(SCHEMA)
    for table, cols in DERIVED_COLUMNS.items():
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in cols.items():
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    db.commit()


def _utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _et():
    from flow_logger import now_et  # lazy: flow_logger imports this module
    return now_et()


def _et_moment(trade_date, hhmm):
    from flow_logger import et_datetime
    h, m = (int(x) for x in hhmm.split(":")[:2])
    return et_datetime(date.fromisoformat(trade_date), dtime(h, m))


def _et_to_utc_iso(trade_date, hhmm):
    return _et_moment(trade_date, hhmm).astimezone(timezone.utc).isoformat(timespec="seconds")


def _score_txt(score):
    """Noteworthy picks have a Trade Echo score; Algo Edge alerts don't."""
    return f"score {score:g}" if score is not None else "Algo Edge alert"


def _pct(price, fill):
    return (price / fill - 1) if price is not None and fill else None


# ------------------------------------------------------------ noteworthy --

def noteworthy_args(session_date, time_from, time_to, min_premium=NOTEWORTHY_MIN_PREMIUM):
    return {
        "date": session_date,
        "min_score": NOTEWORTHY_MIN_SCORE,
        "max_dte_days": NOTEWORTHY_MAX_DTE,
        "min_premium": min_premium,
        "limit": NOTEWORTHY_LIMIT,
        "time_from": time_from,
        "time_to": time_to,
    }


def live_window(et_now):
    start = max(et_now - timedelta(minutes=LIVE_WINDOW_MIN),
                et_now.replace(hour=9, minute=30, second=0, microsecond=0))
    return start.strftime("%H:%M"), et_now.strftime("%H:%M")


def sweep_windows():
    start = datetime(2000, 1, 1, 9, 30)
    end = datetime(2000, 1, 1, 16, 15)
    while start < end:
        stop = min(start + timedelta(minutes=SWEEP_WINDOW_MIN), end)
        yield start.strftime("%H:%M"), stop.strftime("%H:%M")
        start = stop


def _parse_pick(p):
    m = re.match(r"\s*([\d.]+)\s*([CP])\s+(\d{4}-\d{2}-\d{2})", p.get("contract") or "")
    strike = float(m.group(1)) if m else None
    put_call = (p.get("callOrPut") or (m and ("Call" if m.group(2) == "C" else "Put")) or "").upper()
    expiration = p.get("expiration") or (m.group(3) if m else None)
    size_m = re.search(r"-\s*([\d,]+)\s*@", p.get("line") or "")
    try:
        hhmm = datetime.strptime((p.get("tradeTimeET") or "").strip(), "%I:%M %p").strftime("%H:%M")
    except ValueError:
        hhmm = None
    return {
        "ticker": (p.get("ticker") or "").upper(),
        "contract": p.get("contract") or "",
        "strike": strike,
        "put_call": put_call,
        "expiration": expiration,
        "dte_days": p.get("dteDays"),
        "size": int(size_m.group(1).replace(",", "")) if size_m else None,
        "fill_price": p.get("fillPrice"),
        "premium": round(p["premium"], 2) if isinstance(p.get("premium"), (int, float)) else None,
        "score": p.get("score"),
        "flags": p.get("flags") or [],
        "line": p.get("line"),
        "trade_time_et": hhmm,
    }


def store_picks(db, payload, source):
    """Save picks for ALL tickers (more training data; pings stay limited to MY_TICKERS).
    Returns (all_rows_returned, mine, new_ids, outside_window) -- new_ids covers all tickers."""
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    session = data.get("date")
    rows = _rows(payload, "picks")
    window = data.get("filtersApplied") if isinstance(data.get("filtersApplied"), dict) else {}
    t_from, t_to = window.get("timeFromET"), window.get("timeToET")
    mine, new_ids, outside = 0, [], 0
    for raw in rows:
        p = _parse_pick(raw)
        if p["trade_time_et"] and t_from and t_to:
            if not (_hhmm(t_from) <= p["trade_time_et"] <= _hhmm(t_to)):
                outside += 1
        if (not p["ticker"] or p["trade_time_et"] is None or session is None
                or (p["dte_days"] is not None and p["dte_days"] > NOTEWORTHY_MAX_DTE)
                or (p["score"] is not None and p["score"] <= NOTEWORTHY_MIN_SCORE)):
            continue
        mine += p["ticker"] in MY_TICKERS
        cur = db.execute(
            "INSERT OR IGNORE INTO picks (trade_date, trade_time_et, ticker, contract, strike, "
            "put_call, expiration, dte_days, size, fill_price, premium, score, last_score, flags, "
            "line, source, first_seen_utc, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session, p["trade_time_et"], p["ticker"], p["contract"], p["strike"], p["put_call"],
             p["expiration"], p["dte_days"], p["size"], p["fill_price"], p["premium"], p["score"],
             p["score"], json.dumps(p["flags"]), p["line"], source, _utc_iso(), json.dumps(raw)))
        if cur.rowcount:
            new_ids.append(cur.lastrowid)
        else:
            db.execute(
                "UPDATE picks SET last_score = ? WHERE trade_date = ? AND ticker = ? AND contract = ? "
                "AND trade_time_et = ? AND premium IS ? AND fill_price IS ?",
                (p["score"], session, p["ticker"], p["contract"], p["trade_time_et"],
                 p["premium"], p["fill_price"]))
    db.commit()
    return len(rows), mine, new_ids, outside


def mine_only(db, ids):
    """The subset of pick ids on your 12 tickers (the only ones judged live and pinged)."""
    return [i for i in ids if db.execute("SELECT ticker FROM picks WHERE id = ?",
                                         (i,)).fetchone()[0] in MY_TICKERS]


def _hhmm(text):
    """'9:35 AM', '09:35' or '09:35:00' -> 'HH:MM'."""
    text = str(text).strip()
    for fmt in ("%I:%M %p", "%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%H:%M")
        except ValueError:
            pass
    return text


# ---------------------------------------------------------- price outcomes --

def occ_symbol(ticker, expiration, put_call, strike):
    exp = expiration.replace("-", "")[2:]
    return f"O:{ticker}{exp}{put_call[0]}{round(strike * 1000):08d}"


def _rows(payload, key):
    """payload['data'][key] as a list - Trade Echo sometimes sends a text message instead of the
    usual table (e.g. 'no data for this contract'), which must count as no rows, not crash."""
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get(key) if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _bars_by_date(payload):
    from flow_logger import _et_tz
    out = {}
    for b in _rows(payload, "data"):
        if isinstance(b.get("t"), (int, float)):
            moment = datetime.fromtimestamp(b["t"] / 1000, timezone.utc)
            out[moment.astimezone(_et_tz(moment)).date().isoformat()] = b
    return out


def _save_day(db, pid, fill, bar):
    db.execute(
        "UPDATE picks SET day_open = ?, day_high = ?, day_low = ?, day_vwap = ?, day_close = ?, "
        "day_volume = ?, max_gain_pct = ?, max_loss_pct = ?, avg_pct = ?, close_pct = ?, "
        "day_stats_status = 'ok' WHERE id = ?",
        (bar.get("o"), bar.get("h"), bar.get("l"), bar.get("vw"), bar.get("c"), bar.get("v"),
         _pct(bar.get("h"), fill), _pct(bar.get("l"), fill), _pct(bar.get("vw"), fill),
         _pct(bar.get("c"), fill), pid))


def _save_exp(db, pid, fill, bar):
    db.execute(
        "UPDATE picks SET exp_open = ?, exp_high = ?, exp_low = ?, exp_vwap = ?, exp_close = ?, "
        "exp_high_pct = ?, exp_low_pct = ?, exp_avg_pct = ?, exp_close_pct = ?, "
        "exp_stats_status = 'ok' WHERE id = ?",
        (bar.get("o"), bar.get("h"), bar.get("l"), bar.get("vw"), bar.get("c"),
         _pct(bar.get("h"), fill), _pct(bar.get("l"), fill), _pct(bar.get("vw"), fill),
         _pct(bar.get("c"), fill), pid))


def _says_expired(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    inner = data.get("data") if isinstance(data, dict) else None
    return isinstance(inner, dict) and inner.get("expired") is True


def _mark_expired_no_data(db, pid, need_day, need_exp):
    if need_day:
        db.execute("UPDATE picks SET day_stats_status = 'no_data' WHERE id = ?", (pid,))
    if need_exp:
        db.execute("UPDATE picks SET exp_stats_status = 'no_data' WHERE id = ?", (pid,))
    db.commit()


def fetch_price_stats(db, call, session_date):
    """Fill print-day and expiry-day price stats for every pick that still needs them and is
    within Trade Echo's price history. One market-data call (2 credits) covers both days."""
    # Trade Echo keeps ~7 days counted back from TODAY (not from the session), so anything older
    # is marked too_old without spending credits on it.
    cutoff = (_et().date() - timedelta(days=PRICE_HISTORY_DAYS)).isoformat()
    db.execute("UPDATE picks SET day_stats_status = 'too_old' WHERE day_stats_status IS NULL "
               "AND trade_date < ?", (cutoff,))
    db.execute("UPDATE picks SET exp_stats_status = 'too_old' WHERE exp_stats_status IS NULL "
               "AND expiration < ?", (cutoff,))
    db.commit()
    rows = db.execute(
        "SELECT id, ticker, trade_date, trade_time_et, strike, put_call, expiration, fill_price, "
        "day_stats_status IS NULL AND trade_date <= ?, "
        "exp_stats_status IS NULL AND expiration <= ? "
        "FROM picks WHERE (day_stats_status IS NULL AND trade_date BETWEEN ? AND ?) "
        "OR (exp_stats_status IS NULL AND expiration BETWEEN ? AND ?) "
        "ORDER BY trade_date, trade_time_et",
        (session_date, session_date, cutoff, session_date, cutoff, session_date)).fetchall()
    counts = {"day": 0, "expiry": 0, "missing": 0, "expired_no_data": 0, "via_ibkr": 0}
    dead = set()   # contracts Trade Echo reported as expired with no bars (don't pay twice)
    # Contracts still listed are priced free (and honestly, after the print) by IBKR, so Trade
    # Echo credits are saved for them - but only while TWS is up to actually do it.
    today = _et().date().isoformat()
    try:
        import ibkr
        ib_up = ibkr.available()
    except Exception:
        ib_up = False
    for pid, ticker, tdate, hhmm, strike, pc, exp, fill, need_day, need_exp in rows:
        if not (strike and pc and exp and fill):
            db.execute("UPDATE picks SET day_stats_status = 'no_data', exp_stats_status = "
                       "'no_data' WHERE id = ?", (pid,))
            continue
        if ib_up and exp >= today and need_day:
            db.execute("UPDATE picks SET day_stats_status = 'via_ibkr' WHERE id = ?", (pid,))
            db.commit()
            counts["via_ibkr"] += 1
            need_day = False
            if not need_exp:
                continue
        symbol = occ_symbol(ticker, exp, pc, strike)
        # Trade Echo returns no bars for contracts that expired before today (0 of 56 tried on
        # Oct 2, at 2 credits each), nor for today's expiries by the evening harvest (0 of 128 on
        # Oct 2 after 21:00; the 16:15 after-close run did still get some), so don't pay to ask
        if symbol in dead or exp < today or (exp == today and _et().strftime("%H:%M") >= "20:00"):
            _mark_expired_no_data(db, pid, need_day, need_exp)
            counts["expired_no_data"] += 1
            continue
        payload = call("get_market_data", {"ticker": symbol, "endpoint_type": "aggregates"},
                       "market_data", ticker)
        bars = _bars_by_date(payload)
        if not bars and _says_expired(payload):
            # Trade Echo keeps no bars for this expired contract any more: a retry would cost 2
            # credits for nothing. IBKR prices are the honest outcome anyway.
            dead.add(symbol)
            _mark_expired_no_data(db, pid, need_day, need_exp)
            counts["expired_no_data"] += 1
            print(f"  prices {ticker} {strike:g}{pc[0]} {exp}: expired, Trade Echo has no bars - skipped for good")
            continue
        parts = []
        if need_day:
            if tdate in bars:
                _save_day(db, pid, fill, bars[tdate])
                counts["day"] += 1
                parts.append(f"day high {_pct(bars[tdate].get('h'), fill):+.0%} "
                             f"close {_pct(bars[tdate].get('c'), fill):+.0%}")
            else:
                _bump(db, pid, "day")
                counts["missing"] += 1
                parts.append("day: no bar")
        if need_exp:
            if exp in bars:
                _save_exp(db, pid, fill, bars[exp])
                counts["expiry"] += 1
                parts.append(f"expiry close {_pct(bars[exp].get('c'), fill):+.0%}")
            else:
                _bump(db, pid, "exp")
                parts.append("expiry: no bar")
        db.commit()
        print(f"  prices {ticker} {strike:g}{pc[0]} {exp} @ {fill} ({tdate} {hhmm}): {'; '.join(parts)}")
    return counts


def update_confirmed_gains(db):
    """max_after_pct = best fill of the same contract in captured prints AFTER the print time.
    A day high close to this was really reachable after the print; otherwise it's uncertain."""
    db.execute(
        "UPDATE picks SET max_after_pct = (SELECT MAX(pr.fill_price) / picks.fill_price - 1 FROM prints pr "
        "WHERE pr.ticker = picks.ticker AND pr.strike = picks.strike AND pr.put_call = picks.put_call "
        "AND pr.expiration = picks.expiration AND pr.trade_date = picks.trade_date "
        "AND pr.trade_time_et > picks.trade_time_et || ':59') "
        "WHERE day_stats_status = 'ok' AND fill_price > 0")
    db.commit()


def update_true_outcomes(db):
    """Fill the honest outcome columns. 'ibkr': best bid / worst trade / last bid after the fill
    minute from IBKR 1-minute bars. 'te_confirmed': Trade Echo's day stats, only when the day's high
    is known to come after the print. 'te_unconfirmed': the high may predate the print - unused."""
    update_confirmed_gains(db)
    db.execute("UPDATE picks SET true_gain_pct = NULL, true_loss_pct = NULL, true_close_pct = NULL, "
               "true_source = NULL, true_exp_close_pct = NULL, true_exp_best_pct = NULL")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_exp_stats'").fetchone():
        db.execute("UPDATE picks SET true_exp_close_pct = x.bid_close_pct, true_exp_best_pct = x.bid_high_pct "
                   "FROM ib_exp_stats x WHERE x.pick_id = picks.id AND x.status = 'ok'")
    has_ib = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_stats'").fetchone()
    if has_ib:
        db.execute("UPDATE picks SET true_gain_pct = s.bid_max_gain_pct, true_loss_pct = s.max_loss_pct, "
                   "true_close_pct = s.bid_close_pct, true_source = 'ibkr' FROM ib_stats s "
                   "WHERE s.pick_id = picks.id AND s.status = 'ok' AND s.bid_max_gain_pct IS NOT NULL")
    for pid, gain, after, hhmm, loss, close in db.execute(
            "SELECT id, max_gain_pct, max_after_pct, trade_time_et, max_loss_pct, close_pct FROM picks "
            "WHERE true_source IS NULL AND day_stats_status = 'ok' AND max_gain_pct IS NOT NULL").fetchall():
        if _training_weight(gain, after, hhmm) == CONFIRMED_WEIGHT:
            db.execute("UPDATE picks SET true_gain_pct = ?, true_loss_pct = ?, true_close_pct = ?, "
                       "true_source = 'te_confirmed' WHERE id = ?", (gain, loss, close, pid))
        else:
            db.execute("UPDATE picks SET true_source = 'te_unconfirmed' WHERE id = ?", (pid,))
    db.commit()
    update_trade_results(db)
    import strategies
    strategies.compute_all(db)   # every graded pick replayed under every strategy, on real IBKR prices
    return dict(db.execute("SELECT true_source, COUNT(*) FROM picks WHERE true_source IS NOT NULL "
                           "GROUP BY 1").fetchall())


def _walk_exit(entry, bars, target=None, stop=None):
    """Walk the bid minute by minute: sell when it touches the stop or the target (the stop is checked
    first inside a minute - the cautious assumption), else at the day's last bid."""
    target = TRADE_TARGET if target is None else target
    stop = TRADE_STOP if stop is None else stop
    for high, low, _ in bars:
        if low <= entry * (1 + stop):
            return stop, "stop"
        if high >= entry * (1 + target):
            return target, "target"
    return bars[-1][2] / entry - 1, "close"


def update_trade_results(db):
    """trade_ret for every IBKR-priced pick: what following the whale would really have earned. Buy at
    the ask right after the print (the print minute's closing ask), sell at +30% / -50% / the 4 PM bid,
    minus $0.65 per contract each way. Same rule as the Simulator bot and FlowDesk's simulator."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_bars'").fetchone():
        return 0
    db.execute("UPDATE picks SET trade_ret = NULL, trade_exit = NULL")
    n = 0
    for pid, td, t in db.execute("SELECT id, trade_date, trade_time_et FROM picks WHERE true_source = 'ibkr'").fetchall():
        start = f"{td} {t[:5]}"
        ask = db.execute("SELECT close FROM ib_bars WHERE pick_id = ? AND kind = 'ASK' AND minute_et = ? AND close > 0",
                         (pid, start)).fetchone()
        bars = db.execute("SELECT high, low, close FROM ib_bars WHERE pick_id = ? AND kind = 'BID' AND minute_et > ? "
                          "AND high > 0 ORDER BY minute_et", (pid, start)).fetchall()
        if not ask or not bars:
            continue
        ret, why = _walk_exit(ask[0], bars)
        db.execute("UPDATE picks SET trade_ret = ?, trade_exit = ? WHERE id = ?",
                   (ret - 2 * FEE_PER_CONTRACT / (ask[0] * 100), why, pid))
        n += 1
    db.commit()
    return n


def _training_weight(max_gain, max_after, hhmm):
    """Full weight when the day's best gain is confirmed after the print (or the print was in the
    first 10 minutes, leaving little room for an earlier high); half weight otherwise."""
    if hhmm <= "09:40":
        return CONFIRMED_WEIGHT
    if max_after is not None and max_gain is not None and max_after >= max_gain - 0.10:
        return CONFIRMED_WEIGHT
    return UNCONFIRMED_WEIGHT


def _bump(db, pid, which):
    db.execute(f"UPDATE picks SET {which}_stats_tries = {which}_stats_tries + 1, "
               f"{which}_stats_status = CASE WHEN {which}_stats_tries + 1 >= ? THEN 'no_data' "
               f"ELSE NULL END WHERE id = ?", (MAX_STATS_TRIES, pid))


# ------------------------------------------------------------------ greeks --

def compute_greeks(db, pick_id):
    """IV and Greeks at the print from fill, stock price, strike and time to expiry."""
    ticker, tdate, hhmm, strike, pc, exp, fill, size, given_spot, source = db.execute(
        "SELECT ticker, trade_date, trade_time_et, strike, put_call, expiration, fill_price, size, "
        "spot_at_print, source FROM picks WHERE id = ?", (pick_id,)).fetchone()
    # Stock price at the print: an Algo Edge alert's own price, else IBKR's 1-minute stock bar for
    # the print minute (its average price), else the nearest captured print / Dealer Edge snapshot
    if source == "algo" and given_spot:
        spot, spot_src = given_spot, "algo"
    else:
        spot, spot_src = _ibkr_spot(db, ticker, tdate, hhmm), "ibkr_minute"
        if not spot:
            spot, spot_src = _spot_at(db, ticker, tdate, hhmm)
    if not (spot and strike and exp and fill):
        return None
    from flow_logger import et_datetime
    expiry_close = et_datetime(date.fromisoformat(exp), dtime(16, 0))
    years = max((expiry_close - _et_moment(tdate, hhmm)).total_seconds(), 600) / (365 * 86400)
    is_call = pc == "CALL"
    iv = bs.implied_vol(fill, spot, strike, years, is_call)
    if iv is None:
        db.execute("UPDATE picks SET spot_at_print = ?, spot_source = ?, iv = NULL, delta = NULL, gamma = NULL, "
                   "theta = NULL, vega = NULL, theta_dollars_day = NULL WHERE id = ?", (spot, spot_src, pick_id))
        db.commit()
        return None
    g = bs.greeks(spot, strike, years, iv, is_call)
    db.execute("UPDATE picks SET spot_at_print = ?, spot_source = ?, iv = ?, delta = ?, gamma = ?, theta = ?, "
               "vega = ?, theta_dollars_day = ? WHERE id = ?",
               (spot, spot_src, iv, g["delta"], g["gamma"], g["theta"], g["vega"],
                g["theta"] * 100 * size if size else None, pick_id))
    db.commit()
    return g


SPOT_MAX_GAP_SEC = 1800          # stock price must come from within 30 min of the print


def _ibkr_spot(db, ticker, tdate, hhmm):
    """IBKR's average stock price during the print minute, or None if not downloaded."""
    try:
        row = db.execute("SELECT COALESCE(vwap, close) FROM ib_stock_bars WHERE ticker = ? AND minute_et = ?",
                         (ticker, f"{tdate} {hhmm}")).fetchone()
    except sqlite3.OperationalError:  # IBKR tables not created yet
        return None
    return row[0] if row and row[0] and row[0] > 0 else None


def _spot_at(db, ticker, tdate, hhmm):
    """(stock price, source) from the captured print nearest in time (within 30 min), else the
    same day's Dealer Edge snapshot. (None, None) rather than a guess."""
    row = db.execute(
        "SELECT spot, ABS(strftime('%s', '2000-01-01 ' || trade_time_et) - "
        "strftime('%s', '2000-01-01 ' || ?)) AS gap FROM prints WHERE ticker = ? AND trade_date = ? "
        "AND spot IS NOT NULL ORDER BY gap LIMIT 1",
        (f"{hhmm}:30", ticker, tdate)).fetchone()
    if row and row[1] is not None and row[1] <= SPOT_MAX_GAP_SEC:
        return row[0], "flow_print"
    moment = _et_moment(tdate, hhmm)
    row = db.execute(
        "SELECT spot FROM dealer_edge_snapshots WHERE ticker = ? AND fetched_utc BETWEEN ? AND ? "
        "AND spot IS NOT NULL ORDER BY ABS(julianday(fetched_utc) - julianday(?)) LIMIT 1",
        (ticker, (moment - timedelta(seconds=SPOT_MAX_GAP_SEC)).astimezone(timezone.utc).isoformat(timespec="seconds"),
         (moment + timedelta(seconds=SPOT_MAX_GAP_SEC)).astimezone(timezone.utc).isoformat(timespec="seconds"),
         moment.astimezone(timezone.utc).isoformat(timespec="seconds"))).fetchone()
    return (row[0], "dealer_edge") if row else (None, None)


# -------------------------------------------------------------- features --

def features_for(db, pick_id):
    (ticker, trade_date, hhmm, strike, put_call, dte, fill, premium, score, flags_json, exp,
     iv, delta, gamma, theta, vega, spot, source) = db.execute(
        "SELECT ticker, trade_date, trade_time_et, strike, put_call, dte_days, fill_price, premium, "
        "score, flags, expiration, iv, delta, gamma, theta, vega, spot_at_print, source FROM picks "
        "WHERE id = ?", (pick_id,)).fetchone()
    flags = json.loads(flags_json or "[]")
    t_sec = f"{hhmm}:59"
    h, m = (int(x) for x in hhmm.split(":"))
    is_call = 1.0 if put_call == "CALL" else 0.0
    spot = spot or _spot_at(db, ticker, trade_date, hhmm)[0]
    hours = None
    if exp:
        from flow_logger import et_datetime
        hours = (et_datetime(date.fromisoformat(exp), dtime(16, 0))
                 - _et_moment(trade_date, hhmm)).total_seconds() / 3600
    f = {
        "score": score,
        "log_premium": math.log10(premium) if premium else None,
        "log_fill": math.log10(fill) if fill else None,
        "dte": dte,
        "hours_to_expiry": hours,
        "is_call": is_call,
        "is_index": 1.0 if ticker in INDEX_TICKERS else 0.0,
        "minutes_since_open": (h * 60 + m) - (9 * 60 + 30),
        "other_flags": float(len([x for x in flags if x not in KNOWN_FLAGS])),
        "otm_pct": ((strike / spot - 1) * 100 * (1 if is_call else -1)) if spot and strike else None,
        "iv": iv,
        "abs_delta": abs(delta) if delta is not None else None,
        "theta_pct": (theta / fill) if theta is not None and fill else None,
        "vega_pct": (vega / fill) if vega is not None and fill else None,
        "gamma_x_spot": (gamma * spot) if gamma is not None and spot else None,
    }
    for flag in KNOWN_FLAGS:
        f[f"flag_{flag}"] = 1.0 if flag in flags else 0.0

    snap = db.execute(
        "SELECT id, spot, gex_rating, flip, charm_anchor, atm_iv_nearest FROM dealer_edge_snapshots "
        "WHERE ticker = ? AND fetched_utc <= ? AND fetched_utc >= ? ORDER BY id DESC LIMIT 1",
        (ticker, _et_to_utc_iso(trade_date, hhmm), _et_to_utc_iso(trade_date, "00:00"))).fetchone()
    if snap:
        _, _, gex, flip, charm, atm = snap
        f["gex_rating"] = gex
        f["flip_dist_pct"] = (spot - flip) / spot * 100 if spot and flip else None
        f["charm_dist_pct"] = (strike - charm) / spot * 100 if spot and charm and strike else None
        f["atm_iv"] = atm
        f["n_setups"] = db.execute("SELECT COUNT(*) FROM setup_observations WHERE snapshot_id = ?",
                                   (snap[0],)).fetchone()[0]
    else:
        f.update(gex_rating=None, flip_dist_pct=None, charm_dist_pct=None, atm_iv=None, n_setups=None)

    start = (datetime(2000, 1, 1, h, m) - timedelta(minutes=30)).strftime("%H:%M:%S")
    f["repeat_30m"] = db.execute(
        "SELECT COUNT(*) FROM prints WHERE ticker = ? AND strike = ? AND put_call = ? AND "
        "trade_date = ? AND trade_time_et BETWEEN ? AND ?",
        (ticker, strike, put_call, trade_date, start, t_sec)).fetchone()[0]
    call_prem, total_prem = db.execute(
        "SELECT SUM(CASE WHEN put_call = 'CALL' THEN premium ELSE 0 END), SUM(premium) FROM prints "
        "WHERE ticker = ? AND trade_date = ? AND trade_time_et BETWEEN ? AND ?",
        (ticker, trade_date, start, t_sec)).fetchone()
    f["flow_call_share_30m"] = (call_prem / total_prem) if total_prem else None
    f["hermes_est"], f["qwen_est"] = db.execute(
        "SELECT hermes_expected_gain, qwen_expected_gain FROM picks WHERE id = ?", (pick_id,)).fetchone()
    f["is_my_ticker"] = 1.0 if ticker in MY_TICKERS else 0.0
    f["from_algo"] = 1.0 if source == "algo" else 0.0      # Algo Edge alert rather than a noteworthy pick
    f["from_whale"] = 1.0 if source == "whale" else 0.0    # a raw whale-size print (training only)
    import trade_context
    ctx = trade_context.get(db, pick_id)
    f["side_buy"] = f["is_multileg"] = f["leg_sold"] = None
    if ctx and ctx["status"] in ("ok", "side_only"):
        f["side_buy"] = {"buy": 1.0, "sell": -1.0, "mid": 0.0}[ctx["side"]]
    if ctx and ctx["status"] == "ok":   # 'side_only' (raw whale prints): legs weren't checked
        f["is_multileg"] = 1.0 if ctx["structure"] else 0.0
        f["leg_sold"] = 1.0 if ctx["our_leg"] == "sold" else 0.0
    sign = 1.0 if is_call else -1.0
    s30, sday, srange = _stock_context(db, ticker, trade_date, hhmm)
    p30, pday, _ = _stock_context(db, "SPY", trade_date, hhmm)
    f["stock_with_30m"] = None if s30 is None else sign * s30
    f["stock_with_day"] = None if sday is None else sign * sday
    f["stock_range_30m"] = srange
    f["spy_with_30m"] = None if p30 is None else sign * p30
    f["spy_with_day"] = None if pday is None else sign * pday
    f["stock_iv_prev"], f["stock_iv_rank_1y"] = _stock_iv(db, ticker, trade_date)
    f["iv_vs_stock_iv"] = iv / f["stock_iv_prev"] if iv and f["stock_iv_prev"] else None
    return f, spot


def _stock_iv(db, ticker, trade_date):
    """(IV at the last close BEFORE the trade date, its percentile rank over the prior year)."""
    try:
        rows = [r[0] for r in db.execute(
            "SELECT iv FROM ib_stock_iv WHERE ticker = ? AND day < ? AND day >= date(?, '-1 year') ORDER BY day",
            (ticker, trade_date, trade_date))]
    except sqlite3.OperationalError:
        return None, None
    if not rows:
        return None, None
    last = rows[-1]
    rank = sum(1 for v in rows if v <= last) / len(rows) if len(rows) >= 60 else None
    return last, rank


def _stock_context(db, ticker, trade_date, hhmm):
    """(return over the 30 min before the print, return since the open, 30-min high-low range as a
    share of price) from IBKR 1-minute bars that END before the print minute. Nones if missing."""
    try:
        rows = db.execute("SELECT minute_et, open, high, low, close FROM ib_stock_bars WHERE ticker = ? "
                          "AND minute_et >= ? AND minute_et < ? ORDER BY minute_et",
                          (ticker, f"{trade_date} 09:30", f"{trade_date} {hhmm}")).fetchall()
    except sqlite3.OperationalError:
        return None, None, None
    if not rows:
        return None, None, None
    h, m = (int(x) for x in hhmm.split(":"))
    lh, lm = (int(x) for x in rows[-1][0][11:].split(":"))
    if (h * 60 + m) - (lh * 60 + lm) > CONTEXT_MAX_STALE_MIN:
        return None, None, None
    last = rows[-1][4]
    t30 = f"{trade_date} {(datetime(2000, 1, 1, h, m) - timedelta(minutes=30)):%H:%M}"
    before = [r for r in rows if r[0] <= t30]
    ref30 = before[-1][4] if before else rows[0][1]
    window = [r for r in rows if r[0] > t30]
    rng = (max(r[2] for r in window) - min(r[3] for r in window)) / last if window and last else None
    return last / ref30 - 1, last / rows[0][1] - 1, rng


def _frame(db, ids, columns=None):
    """Feature table; `columns` = a saved model's own input list, so a model trained before new
    inputs were added still scores live picks until the next retrain."""
    import pandas as pd
    return pd.DataFrame([features_for(db, i)[0] for i in ids], columns=columns or FEATURES, dtype="float64")


# ----------------------------------------------------------------- model --

WF_MIN_TRAIN = 12                       # picks needed before a replay day can be predicted
PING_THRESHOLDS = [0.20, 0.30, 0.40, 0.50]     # for spike guesses (expected best gain)
PROFIT_PREFIX = "profit:"                       # models trained on the real trade result (trade_ret)
PICKER_PREFIX = "picker:"                       # one model per strategy; trades each pick with its best one
PICKER_MAKERS = ["linear", "boosting", "similar_trades"]
PROFIT_THRESHOLDS = [0.0, 0.03, 0.06, 0.10, 0.15, 0.20]   # profit guesses: take when expected profit >= this
MIN_TAKEN_SHARE, MIN_TAKEN = 0.05, 8           # a rule must take enough replay trades to be trusted


def _candidates():
    """Methods that compete every night. Each returns a fresh, unfitted estimator."""
    from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
    from sklearn.feature_selection import VarianceThreshold
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.neighbors import KNeighborsRegressor
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    def imp():
        # add_indicator: the model also sees WHICH inputs were missing (e.g. no Greeks)
        return SimpleImputer(strategy="median", keep_empty_features=True, add_indicator=True)

    return {
        "linear": lambda n: make_pipeline(imp(), StandardScaler(), Ridge(alpha=3.0)),
        # constant columns are dropped first: this sklearn version crashes on them
        "boosting": lambda n: make_pipeline(imp(), VarianceThreshold(0.0), HistGradientBoostingRegressor(
            max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=max(3, n // 10),
            random_state=0)),
        "forest": lambda n: make_pipeline(imp(), RandomForestRegressor(
            n_estimators=300, min_samples_leaf=3, random_state=0)),
        "similar_trades": lambda n: make_pipeline(imp(), StandardScaler(), KNeighborsRegressor(
            n_neighbors=max(2, min(7, n // 3)), weights="distance")),
    }


LLM_SOURCES = ["hermes", "qwen", "judges"]   # "judges" = average of Hermes and Qwen


def _llm_values(name, llm, fill):
    import warnings
    import numpy as np
    if name == "judges":
        with warnings.catch_warnings():  # all-missing columns give NaN, filled below
            warnings.simplefilter("ignore", RuntimeWarning)
            vals = np.nanmean(np.vstack([llm["hermes"], llm["qwen"]]), axis=0)
    else:
        vals = llm[name]
    return np.where(np.isnan(vals), fill, vals)


def _predict_with(kind, estimator, X, llm, fill):
    """Per row: predicted best gain (a trained model, a judge - Hermes, Qwen or both averaged - or a
    50/50 blend of a model with a judge), or for 'profit:' models the predicted TRADE result.
    `llm` maps judge name -> estimates array."""
    import numpy as np
    if kind.startswith(PROFIT_PREFIX):
        return estimator.predict(X)
    base, _, judge = kind.partition("+")
    if base in LLM_SOURCES:
        return _llm_values(base, llm, fill)
    model = np.expm1(estimator.predict(X))
    return (model + _llm_values(judge, llm, fill)) / 2 if judge else model


def _fit_weighted(est, X, y, w):
    """Fit with training weights when the final step supports them (k-NN doesn't)."""
    import inspect
    step_name, step = est.steps[-1]
    if "sample_weight" in inspect.signature(step.fit).parameters:
        return est.fit(X, y, **{f"{step_name}__sample_weight": w})
    return est.fit(X, y)


def _score_predictions(kind, pred, gain, profit):
    """How a method did in the blind replay. The contest is decided by TRADING PROFIT: for each
    'take it when the guess is >= X' rule, the average trade result (trade_ret) of the trades it would
    have taken; the method keeps its most profitable rule (it must take enough trades to count -
    averaging per trade, rather than summing, stops a rule from 'winning' a losing contest just by
    taking the fewest trades). Rank agreement with the spike (gain) and with the profit are reported."""
    import numpy as np
    import pandas as pd
    corr = lambda a, b: float(pd.Series(a).corr(pd.Series(b), method="spearman")) if len(b) > 2 else float("nan")
    out = {"n": int(len(gain)), "mae": float(np.abs(pred - (profit if kind.startswith(PROFIT_PREFIX) else gain)).mean()),
           "spearman": corr(pred, gain), "spearman_profit": corr(pred, profit)}
    need = max(MIN_TAKEN, int(MIN_TAKEN_SHARE * len(gain)))
    best = None
    for t in (PROFIT_THRESHOLDS if kind.startswith(PROFIT_PREFIX) else PING_THRESHOLDS):
        chosen = pred >= t
        k = int(chosen.sum())
        if k >= need:
            total = float(profit[chosen].sum())
            if best is None or total / k > best["profit_avg"] + 1e-9:
                best = {"threshold": t, "n_pings": k, "profit_total": total, "profit_avg": total / k,
                        "hit": float((gain[chosen] >= PING_MIN_GAIN).mean()),
                        "win_rate": float((profit[chosen] > 0).mean())}
    out.update(best or {"threshold": (PROFIT_THRESHOLDS if kind.startswith(PROFIT_PREFIX) else PING_THRESHOLDS)[-1],
                        "n_pings": 0, "profit_total": None, "profit_avg": None, "hit": None, "win_rate": None})
    return out


def train(db):
    """Nightly contest: replay past days as if live (train on earlier days only, predict the next).
    Every method competes twice - trained on the spike (the best bid after the print) and trained on
    the real trade result ('profit:' methods) - and ALL are judged on the same thing: the trading
    profit of the trades they would have taken, each with its most profitable 'take when >= X' rule.
    The winner is refit on all data. Graded only on IBKR-verified picks."""
    import joblib
    import numpy as np

    sources = update_true_outcomes(db)
    rows = db.execute("SELECT id, trade_date, true_gain_pct, hermes_expected_gain, qwen_expected_gain, "
                      "true_source, trade_ret FROM picks WHERE true_gain_pct IS NOT NULL "
                      "AND true_source IN ('ibkr', 'te_confirmed') "
                      "ORDER BY trade_date, trade_time_et").fetchall()
    n = len(rows)
    actual = np.array([r[2] for r in rows], dtype=float)
    profit = np.array([np.nan if r[6] is None else r[6] for r in rows], dtype=float)
    has_trade = ~np.isnan(profit)
    # Replay is graded ONLY on IBKR-verified outcomes with a trade result; Trade Echo-confirmed rows
    # just help the spike models train
    graded = np.array([r[5] == "ibkr" for r in rows]) & has_trade
    base = float((actual[graded] >= PING_MIN_GAIN).mean()) if graded.any() else None
    if n < MODEL_MIN_ROWS or graded.sum() < MODEL_MIN_ROWS:
        note = (f"need {MODEL_MIN_ROWS}+ picks with honest after-print prices; have {n} "
                f"({int(graded.sum())} IBKR-verified)")
        db.execute("INSERT INTO model_runs (run_utc, status, target, n_rows, base_rate_hit, note) "
                   "VALUES (?, 'waiting_for_data', ?, ?, ?, ?)", (_utc_iso(), TARGET_NAME, n, base, note))
        db.commit()
        return {"status": "waiting_for_data", "n": n, "base": base, "note": note}

    X = _frame(db, [r[0] for r in rows])
    y = np.log1p(np.clip(actual, -0.99, None))
    w = np.array([TRUE_WEIGHTS[r[5]] for r in rows])
    llm_all = {"hermes": np.array([np.nan if r[3] is None else r[3] for r in rows], dtype=float),
               "qwen": np.array([np.nan if r[4] is None else r[4] for r in rows], dtype=float)}
    dates = np.array([r[1] for r in rows])
    makers = _candidates()
    kinds = (list(makers) + [f"{k}+{j}" for k in makers for j in LLM_SOURCES] + LLM_SOURCES
             + [PROFIT_PREFIX + k for k in makers])
    # strategy library: every pick's real result under every strategy (NaN = not recorded / not possible)
    import strategies
    strat_keys = list(strategies.STRATEGIES)
    by_pick = strategies.results(db)
    R = np.array([[by_pick.get(r[0], {}).get(s, (np.nan, ""))[0] for s in strat_keys] for r in rows], dtype=float)
    # when each result became KNOWN (its exit date): training for day D may only use results closed before D
    E = np.array([[by_pick.get(r[0], {}).get(s, (np.nan, "9999"))[1] or "9999" for s in strat_keys] for r in rows])
    pickers = [m for m in PICKER_MAKERS if m in makers]
    picked = {m: {"pred": [], "real": [], "choice": []} for m in pickers}

    # Replay: for each day with enough history, train on earlier days only and predict it.
    preds = {k: [] for k in kinds}
    truth, truth_profit, replay_days, replay_ids = [], [], [], []
    row_ids = np.array([r[0] for r in rows])
    for day in sorted(set(dates)):
        train_mask, test_mask = dates < day, (dates == day) & graded
        p_train = train_mask & has_trade
        if train_mask.sum() < WF_MIN_TRAIN or p_train.sum() < WF_MIN_TRAIN or not test_mask.any():
            continue
        replay_days.append(day)
        fill = float(np.median(actual[train_mask]))
        fitted = {}
        for k in makers:
            for name, mask, target, weights in ((k, train_mask, y, w),
                                                (PROFIT_PREFIX + k, p_train, profit, np.ones(n))):
                try:
                    fitted[name] = _fit_weighted(makers[k](int(mask.sum())), X[mask], target[mask], weights[mask])
                except Exception as e:  # one broken method must not stop the contest
                    print(f"  ({name} failed on {day}: {type(e).__name__}; skipped)")
        test_llm = {j: v[test_mask] for j, v in llm_all.items()}
        for kind in kinds:
            base_kind = kind if kind.startswith(PROFIT_PREFIX) else kind.split("+")[0]
            if base_kind not in LLM_SOURCES and base_kind not in fitted:
                preds[kind] = None  # disqualified: couldn't predict every replay day
                continue
            if preds[kind] is not None:
                preds[kind].extend(_predict_with(kind, fitted.get(base_kind), X[test_mask],
                                                 test_llm, fill))
        # strategy pickers: one model per strategy predicts its result; each pick goes to its best
        # strategy among those recorded for it (0DTE can't be held overnight, expiries not yet reached
        # have no result) and is taken when that expected result clears the rule
        real_s = R[test_mask]
        for m in pickers:
            est_s = np.full(real_s.shape, np.nan)
            for j in range(len(strat_keys)):
                tr = train_mask & ~np.isnan(R[:, j]) & (E[:, j] < day)   # no peeking at later exits
                if tr.sum() < WF_MIN_TRAIN:
                    continue
                try:
                    est = _fit_weighted(makers[m](int(tr.sum())), X[tr], R[tr, j], np.ones(int(tr.sum())))
                    est_s[:, j] = est.predict(X[test_mask])
                except Exception as e:
                    print(f"  ({PICKER_PREFIX}{m}/{strat_keys[j]} failed on {day}: {type(e).__name__}; skipped)")
            usable = np.where(~np.isnan(real_s) & ~np.isnan(est_s), est_s, -np.inf)
            choice = usable.argmax(axis=1)
            best = usable.max(axis=1)
            picked[m]["pred"].extend(np.where(np.isfinite(best), best, -1.0))
            picked[m]["real"].extend(np.where(np.isfinite(best), real_s[np.arange(len(choice)), choice], profit[test_mask]))
            picked[m]["choice"].extend(strat_keys[c] if np.isfinite(b) else "standard" for c, b in zip(choice, best))
        truth.extend(actual[test_mask])
        truth_profit.extend(profit[test_mask])
        replay_ids.extend(int(i) for i in row_ids[test_mask])
    truth, truth_profit = np.array(truth), np.array(truth_profit)
    # live pings never follow a whale that SOLD (a bid-side print or the sold leg of a spread), so the
    # blind test can't take those trades either
    import trade_context
    sold_ids = {r[0] for r in db.execute("SELECT pick_id FROM pick_context WHERE status = 'ok' AND "
                                         "(our_leg = 'sold' OR (structure IS NULL AND side = 'sell'))")}
    sold = np.array([i in sold_ids for i in replay_ids], dtype=bool)
    never = lambda p: np.where(sold, -9.0, np.array(p, dtype=float))
    board = [{"kind": k, **_score_predictions(k, never(v), truth, truth_profit)}
             for k, v in preds.items() if v]
    for m in pickers:
        if len(picked[m]["pred"]) == len(truth):
            picked[m]["pred"] = list(never(picked[m]["pred"]))
            row = {"kind": PICKER_PREFIX + m, **_score_predictions(PROFIT_PREFIX, np.array(picked[m]["pred"]), truth,
                                                                  np.array(picked[m]["real"]))}
            chosen = np.array(picked[m]["pred"]) >= row["threshold"]
            row["strategies"] = {s: int(sum(1 for c, k in zip(picked[m]["choice"], chosen) if k and c == s))
                                 for s in strat_keys}
            board.append(row)
    # decided by blind-test trading profit; a method that never took enough trades ranks last
    board.sort(key=lambda s: -(s["profit_avg"] if s["profit_avg"] is not None else -1e9))
    winner = board[0] if board else {"kind": "linear", "threshold": PING_MIN_GAIN, "hit": None, "n_pings": 0,
                                     "spearman": float("nan"), "n": 0, "mae": float("nan"),
                                     "profit_avg": None, "profit_total": None}
    base_profit = float(truth_profit.mean()) if len(truth_profit) else None

    kind = winner["kind"]
    strategy_estimators = None
    if kind.startswith(PICKER_PREFIX):
        m = kind[len(PICKER_PREFIX):]
        est, strategy_estimators = None, {}
        for j, s in enumerate(strat_keys):
            tr = ~np.isnan(R[:, j])
            if tr.sum() >= WF_MIN_TRAIN:
                strategy_estimators[s] = _fit_weighted(makers[m](int(tr.sum())), X[tr], R[tr, j], np.ones(int(tr.sum())))
    elif kind.startswith(PROFIT_PREFIX):
        est = _fit_weighted(makers[kind[len(PROFIT_PREFIX):]](int(has_trade.sum())), X[has_trade],
                            profit[has_trade], np.ones(int(has_trade.sum())))
    else:
        base_kind = kind.split("+")[0]
        est = _fit_weighted(makers[base_kind](n), X, y, w) if base_kind not in LLM_SOURCES else None
    target_name = "trade_profit" if kind.startswith((PROFIT_PREFIX, PICKER_PREFIX)) else TARGET_NAME
    version = datetime.now().strftime("%Y%m%d-%H%M")
    MODEL_DIR.mkdir(exist_ok=True)
    bundle = {"kind": kind, "estimator": est, "strategy_estimators": strategy_estimators, "features": FEATURES,
              "version": version, "n": n, "target": target_name, "hermes_fill": float(np.median(actual)),
              "ping_threshold": winner["threshold"], "base_rate_hit": base, "replay": winner,
              "replay_days": replay_days, "leaderboard": board, "sources": sources,
              "n_graded": int(graded.sum()), "base_profit": base_profit}
    joblib.dump(bundle, MODEL_DIR / f"model_{version}.joblib")
    joblib.dump(bundle, MODEL_DIR / "latest.joblib")
    db.execute("DELETE FROM replay_preds")
    if kind.startswith(PICKER_PREFIX):
        p = picked[kind[len(PICKER_PREFIX):]]
        db.executemany("INSERT INTO replay_preds (pick_id, kind, pred, version, strategy) VALUES (?, ?, ?, ?, ?)",
                       [(pid, kind, float(v), version, s) for pid, v, s in zip(replay_ids, p["pred"], p["choice"])])
    elif preds.get(kind):
        db.executemany("INSERT INTO replay_preds (pick_id, kind, pred, version, strategy) VALUES (?, ?, ?, ?, ?)",
                       [(pid, kind, float(p), version, "standard") for pid, p in zip(replay_ids, preds[kind])])
    db.execute(
        "INSERT INTO model_runs (run_utc, status, version, target, n_rows, base_rate_hit, cv_mae, "
        "cv_spearman, cv_hit_rate, cv_n_hit, model_kind, ping_threshold, leaderboard, cv_profit_avg, "
        "cv_profit_total, cv_n_taken, base_profit_avg) VALUES (?, 'trained', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_utc_iso(), version, target_name, n, base, winner["mae"], winner["spearman"], winner["hit"],
         winner["n_pings"], kind, winner["threshold"], json.dumps(board), winner.get("profit_avg"),
         winner.get("profit_total"), winner["n_pings"], base_profit))
    db.commit()
    return {"status": "trained", "version": version, "n": n, "base": base, "kind": kind,
            "n_graded": int(graded.sum()), "sources": sources,
            "replay_days": len(replay_days), "replay_n": winner["n"], "spearman": winner["spearman"],
            "mae": winner["mae"], "hit": winner["hit"], "n_hit": winner["n_pings"],
            "threshold": winner["threshold"], "board": board, "profit_avg": winner.get("profit_avg"),
            "profit_total": winner.get("profit_total"), "base_profit": base_profit}


_model_cache = {"mtime": None, "bundle": None}


def judges_needed():
    """Which judges tonight's winning method uses (run these first so pings aren't delayed).
    Trained models use both judges' estimates as inputs, so they need both."""
    bundle = _load_model() or {}
    base, _, judge = bundle.get("kind", "hermes").partition("+")
    if base in ("hermes", "qwen"):
        return [base]
    if base == "judges" or base not in LLM_SOURCES:
        return ["hermes", "qwen"]
    return [judge]


def _load_model():
    path = MODEL_DIR / "latest.joblib"
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    if _model_cache["mtime"] != mtime:
        import joblib
        bundle = joblib.load(path)
        if bundle.get("target") not in (TARGET_NAME, "trade_profit", "max_gain_pct") or "kind" not in bundle:  # old formats
            return None
        _model_cache.update(mtime=mtime, bundle=bundle)
    return _model_cache["bundle"]


def score_pick(db, pick_id):
    """Run tonight's winning method on a new pick (call after the judges it needs have run)."""
    bundle = _load_model()
    if bundle is None:
        return None
    cols = bundle.get("features", FEATURES)
    if set(cols) - set(FEATURES) and (bundle.get("estimator") is not None or bundle.get("strategy_estimators")):
        print(f"Model {bundle.get('version')} uses inputs that no longer exist - not scoring; it is "
              "replaced at the next nightly retrain (or run: py flow_logger.py --train)")
        return None
    import numpy as np
    h, q, dte = db.execute("SELECT hermes_expected_gain, qwen_expected_gain, dte_days FROM picks WHERE id = ?",
                           (pick_id,)).fetchone()
    strategy = "standard"
    if bundle["kind"].startswith(PICKER_PREFIX):
        # the strategy picker: each strategy's own model predicts its result; trade the best one
        import strategies
        X = _frame(db, [pick_id], cols)
        guesses = {s: float(e.predict(X)[0]) for s, e in (bundle.get("strategy_estimators") or {}).items()
                   if s in strategies.applicable(dte)}
        if not guesses:
            return None
        strategy, pred = max(guesses.items(), key=lambda kv: kv[1])
    else:
        llm = {"hermes": np.array([np.nan if h is None else h]), "qwen": np.array([np.nan if q is None else q])}
        pred = float(_predict_with(bundle["kind"], bundle["estimator"], _frame(db, [pick_id], cols), llm,
                                   bundle["hermes_fill"])[0])
    db.execute("UPDATE picks SET pred_max_gain = ?, model_version = ?, pred_strategy = ? WHERE id = ?",
               (pred, bundle["version"], strategy, pick_id))
    db.commit()
    return pred


# ---------------------------------------------------------------- hermes --

def _signed(v):
    return "n/a" if v is None else f"{v:+.0%}"


def _hermes_memory(db, trade_date, ticker=None, put_call=None, dte=None, judge="hermes"):
    """What a judge learns from: outcomes from EARLIER days only (same-day results were not
    known at the time), its OWN track record, per-ticker stats, and the most similar past picks."""
    assert judge in JUDGES  # used in column names below
    # Honest outcomes only: the best price you could have SOLD at after the print (true_gain_pct)
    past = "true_gain_pct IS NOT NULL AND trade_date < ?"
    n, avg_gain, hits = db.execute(
        f"SELECT COUNT(*), AVG(true_gain_pct), SUM(true_gain_pct >= ?) FROM picks WHERE {past}",
        (PING_MIN_GAIN, trade_date)).fetchone()
    if not n:
        return "PAST OUTCOMES: none yet.\n"
    lines = [f"PAST OUTCOMES from earlier days: {n} picks, average best sellable gain AFTER the print "
             f"{avg_gain:+.0%}, {hits or 0} of {n} reached +{PING_MIN_GAIN:.0%}."]

    # The judge's own track record -> feedback it can correct itself with
    k, est, act = db.execute(
        f"SELECT COUNT(*), AVG({judge}_expected_gain), AVG(true_gain_pct) FROM picks WHERE {past} "
        f"AND {judge}_expected_gain IS NOT NULL", (trade_date,)).fetchone()
    if k:
        lines.append(f"YOUR TRACK RECORD: on {k} past picks your estimates averaged {est:+.0%}; "
                     f"the real best gain averaged {act:+.0%}.")
        for verdict in ("take", "skip"):
            vk, vh = db.execute(f"SELECT COUNT(*), SUM(true_gain_pct >= ?) FROM picks WHERE {past} "
                                f"AND {judge}_verdict = ?", (PING_MIN_GAIN, trade_date, verdict)).fetchone()
            if vk:
                lines.append(f"  your '{verdict}' calls: {vh or 0} of {vk} reached +{PING_MIN_GAIN:.0%}")
        import strategies
        strategies.ensure_schema(db)
        tk_n, tk_avg, tk_won = db.execute(
            f"SELECT COUNT(*), AVG(r.ret), AVG(r.ret > 0) FROM picks p JOIN strategy_results r ON r.pick_id = p.id "
            f"AND r.strategy = COALESCE(p.{judge}_strategy, 'standard') WHERE substr(r.exit_minute, 1, 10) < ? "
            f"AND p.{judge}_verdict = 'take'", (trade_date,)).fetchone()   # only trades closed before that day
        if tk_n:
            lines.append(f"  YOUR TRADING RECORD: your {tk_n} 'take' calls, traded with the strategy you chose, "
                         f"averaged {tk_avg:+.0%} per trade after costs and made money {tk_won:.0%} of the time.")
        lines.append("  Use this to calibrate: spread your estimates out and say 'skip' when a pick "
                     "looks like past losers.")

    if ticker:
        tk_n, tk_g, tk_h = db.execute(
            f"SELECT COUNT(*), AVG(true_gain_pct), SUM(true_gain_pct >= ?) FROM picks WHERE {past} "
            "AND ticker = ?", (PING_MIN_GAIN, trade_date, ticker)).fetchone()
        if tk_n:
            lines.append(f"{ticker} picks so far: {tk_n}, average best {tk_g:+.0%}, "
                         f"{tk_h or 0} reached +{PING_MIN_GAIN:.0%}.")

    cols = ("trade_date, trade_time_et, ticker, contract, dte_days, fill_price, size, score, iv, "
            "true_gain_pct, true_loss_pct, true_close_pct, expiration, exp_close_pct")
    similar = db.execute(
        f"SELECT {cols} FROM picks WHERE {past} "
        "ORDER BY (ticker = ?) DESC, (put_call = ?) DESC, ABS(COALESCE(dte_days, 0) - ?) ASC, "
        "trade_date DESC, trade_time_et DESC LIMIT ?",
        (trade_date, ticker or "", put_call or "", dte or 0, HERMES_EXAMPLES)).fetchall()
    recent = db.execute(
        f"SELECT {cols} FROM picks WHERE {past} ORDER BY trade_date DESC, trade_time_et DESC LIMIT 6",
        (trade_date,)).fetchall()

    def fmt(r):
        d, t, tk, contract, dte_, fill, size, score, iv, gain, loss, close, exp, exp_close = r
        expiry = (f", at expiry {exp_close:+.0%}" if exp_close is not None and exp and exp < trade_date
                  else "")
        iv_txt = f", IV {iv:.0%}" if iv else ""
        return (f"- {d} {t} {tk} {contract}, {dte_} DTE, {size} @ {fill}, {_score_txt(score)}{iv_txt}: "
                f"best after print {gain:+.0%}, worst {_signed(loss)}, close {_signed(close)}{expiry}")

    lines.append("MOST SIMILAR PAST PICKS (same ticker / type / days to expiry first):")
    lines += [fmt(r) for r in similar]
    extra = [r for r in recent if r not in similar]
    if extra:
        lines.append("OTHER RECENT PICKS:")
        lines += [fmt(r) for r in extra]
    return "\n".join(lines) + "\n"


def warm_up_hermes():
    """Load the judge tonight's winning method needs first onto the GPU and keep it there."""
    name = judges_needed()[0]
    cfg = JUDGES[name]
    body = json.dumps({"model": cfg["model"], "keep_alive": -1, "stream": False, **cfg["extra"],
                       "options": {"num_ctx": cfg["num_ctx"]},
                       "messages": [{"role": "user", "content": "ready"}]}).encode("utf-8")
    start = time.time()
    while True:  # Ollama can start a few seconds after the logger at login - wait for it
        try:
            req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=HERMES_TIMEOUT_SEC):
                pass
            print(f"{name} loaded on the GPU and kept in memory ({time.time() - start:.1f}s).")
            return
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if time.time() - start > 120:
                print(f"{name} warm-up failed after 2 min ({e}); it will load on the first pick.")
                return
            time.sleep(5)


def judge_with_hermes(db, pick_id):
    return judge(db, pick_id, "hermes")


def judge(db, pick_id, name):
    """Ask one judge (hermes or qwen) for its estimate; stores it in <name>_* columns."""
    cfg = JUDGES[name]
    f, spot = features_for(db, pick_id)
    (ticker, trade_date, hhmm, contract, put_call, dte, size, fill, premium, score, flags, iv, delta,
     gamma, theta, vega, theta_usd) = db.execute(
        "SELECT ticker, trade_date, trade_time_et, contract, put_call, dte_days, size, fill_price, "
        "premium, score, flags, iv, delta, gamma, theta, vega, theta_dollars_day FROM picks "
        "WHERE id = ?", (pick_id,)).fetchone()
    setups = [r[0] for r in db.execute(
        "SELECT so.name FROM setup_observations so JOIN dealer_edge_snapshots s ON s.id = so.snapshot_id "
        "WHERE s.ticker = ? AND s.fetched_utc <= ? AND s.fetched_utc >= ? ORDER BY s.id DESC LIMIT 5",
        (ticker, _et_to_utc_iso(trade_date, hhmm), _et_to_utc_iso(trade_date, "00:00")))]

    def fmt(v, spec=".2f"):
        return "unknown" if v is None else format(v, spec)

    greeks_txt = (f"IV {iv:.0%}, delta {delta:.2f}, gamma {gamma:.4f}, theta ${theta:.2f}/day per share "
                  f"({theta / fill:.1%} of the price per day), vega {vega:.2f}"
                  if iv is not None else "unknown (no stock price at the print)")
    import strategies
    import trade_context
    ctx = trade_context.get(db, pick_id)
    side_txt = ("unknown" if not ctx or ctx["status"] not in ("ok", "side_only") else
                {"buy": "the whale BOUGHT at the ask", "sell": "the whale SOLD at the bid",
                 "mid": "traded between bid and ask (unclear)"}[ctx["side"]]
                + (f"; part of a {ctx['structure']} - this contract was the leg {ctx['our_leg']}"
                   if ctx.get("structure") else ""))
    facts = (
        _hermes_memory(db, trade_date, ticker, put_call, dte, judge=name)
        + strategies.memory(db, trade_date, ticker, dte) + "\nNEW PICK TO JUDGE\n"
        f"Whale side: {side_txt}\n"
        f"Ticker: {ticker}\nContract: {contract} ({put_call}), {dte} days to expiry\n"
        f"Print: {size} contracts @ ${fill} = ${premium:,.0f} premium at {hhmm} ET on {trade_date}\n"
        f"Trade Echo noteworthy score: {score}; flags: {', '.join(json.loads(flags or '[]')) or 'none'}\n"
        f"Stock price: {fmt(spot)}; strike is {fmt(f['otm_pct'])}% out of the money\n"
        f"Greeks at the print: {greeks_txt}\n"
        f"Dealer Edge: GEX rating {fmt(f['gex_rating'], 'g')}, spot vs gamma flip "
        f"{fmt(f['flip_dist_pct'])}%, strike vs charm anchor {fmt(f['charm_dist_pct'])}%, "
        f"ATM IV {fmt(f['atm_iv'], '.3f')}, active setups: {', '.join(setups) or 'none'}\n"
        f"Same-contract prints in the last 30 min: {f['repeat_30m']}; call share of this "
        f"ticker's premium in the last 30 min: {fmt(f['flow_call_share_30m'])}\n")
    question = (
        f"1) Estimate the highest BID (the best price you could sell at) this option will reach after "
        f"{hhmm} ET and before today's close, as a % gain over the ${fill} fill. 2) You would buy at the ask "
        "right after the whale's trade: choose the ONE strategy from the playbook most likely to make money "
        "on this trade, and say 'take' only if you expect it to make money after costs - otherwise 'skip' "
        "(most whale trades lose money, so skip freely). Reply with JSON only: "
        '{"expected_max_gain_pct": number (e.g. 25 for +25%), "verdict": "take" or "skip", '
        f'"strategy": one of {list(strategies.STRATEGIES)}, '
        '"confidence": 0-100, "reason": "one short sentence"}')
    body = json.dumps({
        "model": cfg["model"],
        "stream": False,
        "format": "json",
        "keep_alive": -1,  # stay loaded on the GPU -> no reload delay while it's the active judge
        "options": {"temperature": 0.2, "num_ctx": cfg["num_ctx"]},
        **cfg["extra"],
        "messages": [
            {"role": "system", "content": "You are a careful options-flow trader. Judge only from the facts "
             "given, and learn from the past outcomes listed. Never follow a whale that SOLD the contract.\n"
             "STRATEGY PLAYBOOK (every strategy buys this contract at the ask right after the whale's trade):\n"
             + strategies.playbook()},
            {"role": "user", "content": facts + "\n" + question},
        ],
    }).encode("utf-8")
    try:
        req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=HERMES_TIMEOUT_SEC) as resp:
            reply = json.loads(json.loads(resp.read().decode("utf-8"))["message"]["content"])
        verdict = str(reply.get("verdict", "")).lower()
        verdict = verdict if verdict in ("take", "skip") else None
        est = reply.get("expected_max_gain_pct")
        est = float(est) / 100 if isinstance(est, (int, float)) else None
        conf = reply.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else None
        reason = str(reply.get("reason") or "")[:300]
        strategy = str(reply.get("strategy") or "").strip().lower()
        strategy = strategy if strategy in strategies.STRATEGIES else None
        db.execute(f"UPDATE picks SET {name}_verdict = ?, {name}_expected_gain = ?, {name}_confidence = ?, "
                   f"{name}_reason = ?, {name}_strategy = ?, {name}_error = NULL WHERE id = ?",
                   (verdict, est, conf, reason, strategy, pick_id))
        db.commit()
        return verdict, est, reason
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
        db.execute(f"UPDATE picks SET {name}_error = ? WHERE id = ?", (str(e)[:300], pick_id))
        db.commit()
        return None, None, f"{name} unavailable: {e}"


def judge_batch(db, ids, deadline=None):
    """Judge many picks oldest first, one judge at a time (no GPU model swapping)."""
    ids = [r[0] for r in db.execute(
        f"SELECT id FROM picks WHERE id IN ({','.join('?' * len(ids))}) ORDER BY trade_date, trade_time_et",
        ids)] if ids else []
    for name in JUDGES:
        for i, pid in enumerate(ids, 1):
            if deadline and time.time() > deadline:
                print(f"  {name}: stopped at {i - 1}/{len(ids)} for the market open")
                return
            judge(db, pid, name)
            if i % 25 == 0 or i == len(ids):
                print(f"  {name}: judged {i}/{len(ids)}")


# --------------------------------------------------------------- discord --

# Each bot posts under its own name to its own channel's webhook (falls back to the main one).
BOTS = {
    "herald": ("Herald 📣", "DISCORD_WEBHOOK_URL"),
    "scout": ("Scout 🛰️", "DISCORD_WEBHOOK_SCOUT"),
    "judges": ("Judges ⚖️", "DISCORD_WEBHOOK_JUDGES"),
    "arena": ("Arena 🏆", "DISCORD_WEBHOOK_ARENA"),
    "simulator": ("Simulator 💰", "DISCORD_WEBHOOK_SIMULATOR"),
    "analyst": ("Analyst 🔬", "DISCORD_WEBHOOK_ANALYST"),
    "auditor": ("Auditor 🛡️", "DISCORD_WEBHOOK_AUDITOR"),
}


def _webhook_url(var="DISCORD_WEBHOOK_URL"):
    url = os.environ.get(var)
    if not url and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                url, _ = winreg.QueryValueEx(key, var)
        except OSError:
            url = None
    return (url or "").strip() or None


def discord_send(text, bot="herald"):
    """Post as `bot` to its channel's webhook (or the main one). URLs are never printed or stored."""
    name, var = BOTS.get(bot, BOTS["herald"])
    url = _webhook_url(var) or _webhook_url()
    if not url:
        print("  (Discord not set up: DISCORD_WEBHOOK_URL is missing)")
        return False
    body = json.dumps({"content": text[:1900], "username": name}).encode("utf-8")
    for _ in range(3):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "flow-logger (local, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=15):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode("utf-8")).get("retry_after", 2))
                except ValueError:
                    wait = 2
                time.sleep(min(wait, 30))
                continue
            print(f"  Discord error: HTTP {e.code}")
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  Discord error: {type(e).__name__}")
            return False
    return False


def _money(v):
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def _stats_line(label, status, fill, o_high, o_low, o_avg, o_close, pending_text):
    if status == "ok":
        def one(name, price):
            return f"{name} ${price:,.2f} ({price / fill - 1:+.0%})" if price is not None else f"{name} -"
        return (f"{label}: {one('high', o_high)} · {one('low', o_low)} · {one('avg', o_avg)} · "
                f"{one('close', o_close)}")
    if status == "too_old":
        return f"{label}: no price data (older than Trade Echo's ~7-day history)"
    if status == "no_data":
        return f"{label}: no price data"
    return f"{label}: {pending_text}"


def pick_block(db, pick_id, with_model=False):
    (tdate, hhmm, ticker, contract, pc, dte, size, fill, premium, score, verdict, h_est, reason,
     iv, delta, gamma, theta, vega, theta_usd, exp, pred,
     d_status, d_high, d_low, d_avg, d_close,
     e_status, e_high, e_low, e_avg, e_close, q_verdict, q_est) = db.execute(
        "SELECT trade_date, trade_time_et, ticker, contract, put_call, dte_days, size, fill_price, "
        "premium, score, hermes_verdict, hermes_expected_gain, hermes_reason, iv, delta, gamma, theta, "
        "vega, theta_dollars_day, expiration, pred_max_gain, "
        "day_stats_status, day_high, day_low, day_vwap, day_close, "
        "exp_stats_status, exp_high, exp_low, exp_vwap, exp_close, qwen_verdict, qwen_expected_gain "
        "FROM picks WHERE id = ?", (pick_id,)).fetchone()
    icon = "🟢" if pc == "CALL" else "🔴"

    def opinion(v, e):
        return (v or "-").upper() + (f" (est {e:+.0%})" if e is not None else "")

    strike = db.execute("SELECT strike FROM picks WHERE id = ?", (pick_id,)).fetchone()[0]
    exp_txt = f"{datetime.strptime(exp, '%Y-%m-%d'):%b %d}" if exp else "?"
    lines = [f"{icon} `{hhmm}` **{ticker} ${strike:g} {pc}** exp {exp_txt} ({dte} DTE) | "
             f"{_score_txt(score)} | Hermes {opinion(verdict, h_est)} | Qwen {opinion(q_verdict, q_est)}",
             f"Fill ${fill:,.2f}/share = **${fill * 100:,.0f} per contract** × {size or '?'} contracts "
             f"= {_money(premium or 0)}"]
    if iv is not None:
        theta_txt = f"Θ ${theta * 100:.2f}/day" + (f" ({_money(abs(theta_usd))}/day on this print)"
                                                   if theta_usd else "")
        lines.append(f"At print: IV {iv:.0%} · Δ {delta:.2f} · Γ {gamma:.4f} · {theta_txt} · "
                     f"Vega ${vega * 100:.2f}")
    if with_model:
        import trade_context
        lines.append(trade_context.describe(trade_context.get(db, pick_id)))
    if with_model and pred is not None:
        import strategies
        kind = (_load_model() or {}).get("kind", "")
        play = db.execute("SELECT pred_strategy FROM picks WHERE id = ?", (pick_id,)).fetchone()[0] or "standard"
        s = strategies.STRATEGIES.get(play, strategies.STRATEGIES["standard"])
        if kind.startswith(PICKER_PREFIX):
            lines.append(f"🎯 **Play: {s['name']}** - {s['about']} Model expects **{pred:+.0%}** (buy at the ask now).")
        elif kind.startswith(PROFIT_PREFIX):
            lines.append(f"Model: expected trade result **{pred:+.0%}** (buy at the ask now, sell at +30%/-50%/4 PM)")
        else:
            lines.append(f"Model: expected best gain today **{pred:+.0%}**")
    d = datetime.strptime(tdate, "%Y-%m-%d")
    lines.append(_stats_line(f"Day {d:%m/%d}", d_status, fill, d_high, d_low, d_avg, d_close,
                             "after today's close"))
    if exp and exp == tdate:
        lines.append("Expiry: same day (0DTE) - the day's close is where it finished")
    elif exp:
        e = datetime.strptime(exp, "%Y-%m-%d")
        lines.append(_stats_line(f"Expiry {e:%m/%d}", e_status, fill, e_high, e_low, e_avg, e_close,
                                 "pending"))
    return "\n".join(lines)


def send_history(db, since=None, until=None):
    """Post stored picks to Discord, one message block per session date."""
    rows = db.execute(
        f"SELECT id, trade_date FROM picks WHERE trade_date >= COALESCE(?, '') "
        f"AND trade_date <= COALESCE(?, '9999') AND ticker IN ({','.join('?' * len(MY_TICKERS))}) "
        "ORDER BY trade_date, trade_time_et", (since, until, *sorted(MY_TICKERS))).fetchall()
    if not rows:
        print("No picks to send.")
        return 0
    discord_send(f"📜 **Past noteworthy picks** (your 12 tickers, score > 30, 0-14 DTE, $350K+) - "
                 f"{len(rows)} picks. % = change vs the fill price. Greeks are calculated at the "
                 "print (Black-Scholes estimate). History for checking, not live alerts.")
    by_date = {}
    for pid, d in rows:
        by_date.setdefault(d, []).append(pid)
    sent = 0
    for day, ids in by_date.items():
        header = f"**{datetime.strptime(day, '%Y-%m-%d'):%a %b %d, %Y}** - {len(ids)} picks"
        chunk = header
        for pid in ids:  # stay under Discord's 2000-character limit
            block = pick_block(db, pid)
            if len(chunk) + len(block) + 2 > 1900:
                sent += discord_send(chunk)
                time.sleep(1)
                chunk = header + " (cont.)"
            chunk += "\n\n" + block
        sent += discord_send(chunk)
        time.sleep(1)
    return sent


def maybe_ping(db, pick_id, et_now):
    if et_now.date() < PINGS_START:
        return False
    trade_date, hhmm, pred, pinged, ticker, premium, score = db.execute(
        "SELECT trade_date, trade_time_et, pred_max_gain, pinged_utc, ticker, premium, score FROM picks "
        "WHERE id = ?", (pick_id,)).fetchone()
    bundle = _load_model() or {}
    bar = bundle.get("ping_threshold", PING_MIN_GAIN)
    # Pings: only your tickers' noteworthy picks at the $350K+ floor (extra data is for training)
    if (ticker not in MY_TICKERS or score is None or (premium or 0) < NOTEWORTHY_MIN_PREMIUM
            or pinged or pred is None or pred < bar
            or trade_date != et_now.date().isoformat()):
        return False
    h, m = (int(x) for x in hhmm.split(":"))
    if (et_now.hour * 60 + et_now.minute) - (h * 60 + m) > PING_MAX_AGE_MIN:
        return False
    import trade_context
    ctx = trade_context.get(db, pick_id)
    if trade_context.whale_sold(ctx):
        # following would mean buying what the whale sold (a bid-side print, or the sold leg of a spread)
        print(f"{et_now:%H:%M:%S} NO PING {ticker}: the whale SOLD this contract "
              f"({ctx['structure'] or 'at the bid'})")
        return False
    replay = bundle.get("replay") or {}
    hit, pa = replay.get("hit"), replay.get("profit_avg")
    footer = (f"_Method: {bundle.get('kind', '?')}, trained on {bundle.get('n', '?')} picks"
              + (f"; in blind tests its {replay.get('n_pings')} trades averaged {pa:+.0%} each after fees"
                 if pa is not None else "")
              + (f", {hit:.0%} reached +{PING_MIN_GAIN:.0%}" if hit is not None else "")
              + ". Model output, not advice._")
    if discord_send(pick_block(db, pick_id, with_model=True) + "\n" + footer):
        db.execute("UPDATE picks SET pinged_utc = ? WHERE id = ?", (_utc_iso(), pick_id))
        db.commit()
        return True
    return False


# ------------------------------------------------------------- workflows --

def handle_new_picks(db, pick_ids, live, call=None):
    """Live: picks on your tickers get their trade context (bought/sold, spread legs - via `call`,
    which never waits for credits), the judges the winning method needs, a model estimate and
    maybe a ping right away; the other judge runs after. Other tickers' picks are stored for
    training and judged after the close. Not live: everything is judged in one batch."""
    et_now = _et()
    for pid in pick_ids:
        compute_greeks(db, pid)
    if not live:
        judge_batch(db, pick_ids)
        for pid in pick_ids:
            score_pick(db, pid)
        return
    mine = mine_only(db, pick_ids)
    needed = judges_needed()
    import trade_context
    for pid in mine:
        if call is not None:
            try:
                trade_context.check(db, call, pid)
            except Exception as e:   # no credit room right now or a server error: checked tonight
                print(f"{et_now:%H:%M:%S} trade context skipped for pick {pid}: {type(e).__name__}: {e}")
        for name in needed:
            judge(db, pid, name)
        pred = score_pick(db, pid)
        pinged = maybe_ping(db, pid, et_now)
        ticker, contract, hhmm, fill, score, iv, h, q = db.execute(
            "SELECT ticker, contract, trade_time_et, fill_price, score, iv, hermes_expected_gain, "
            "qwen_expected_gain FROM picks WHERE id = ?", (pid,)).fetchone()
        ctx = trade_context.get(db, pid) or {}
        print(f"{et_now:%H:%M:%S} PICK   {ticker} {contract} @ {fill} ({hhmm} ET, {_score_txt(score)}) "
              f"IV={f'{iv:.0%}' if iv else '-'} model={f'{pred:+.0%}' if pred is not None else '-'} "
              f"hermes={f'{h:+.0%}' if h is not None else '-'} qwen={f'{q:+.0%}' if q is not None else '-'} "
              f"side={ctx.get('side') or '?'}{' ' + ctx['structure'] if ctx.get('structure') else ''}"
              f"{' PINGED' if pinged else ''}")
    for pid in mine:  # the remaining judge, after any pings have gone out
        for name in JUDGES:
            if name not in needed:
                judge(db, pid, name)
    others = len(pick_ids) - len(mine)
    if others:
        print(f"{et_now:%H:%M:%S} stored {others} pick(s) on other tickers for training")


WHALE_PICKS_PER_DAY = 250     # raw whale prints turned into training picks per day (largest first)
WHALE_DEDUPE_MIN = 3          # a print this close to an existing pick on the same contract IS that pick
WHALE_REPEAT_MIN = 5          # repeat prints of one contract within 5 min = one whale (largest kept)


def add_whale_picks(db, trade_date):
    """More training data: the day's whale-size raw prints ($350K+, 0-14 DTE, from the whale watch and
    the per-ticker flow polls) that Trade Echo's lists didn't already give us become training picks
    (source 'whale'). They get IBKR prices, Greeks, the judges and the model's inputs like any pick, but
    are never pinged (pings need Trade Echo's score). Their side (bought/sold) comes from the print's
    own sentiment; spread legs aren't checked for them."""
    import trade_context
    trade_context.ensure_schema(db)
    has_raw = "raw_json" in {r[1] for r in db.execute("PRAGMA table_info(prints)")}
    prints = db.execute(
        "SELECT id, ticker, strike, expiration, put_call, trade_time_et, size, fill_price, premium, spot, dte_days, "
        f"sentiment, activity_type, updated_utc, first_seen_utc, {'raw_json' if has_raw else 'NULL'} FROM prints "
        "WHERE trade_date = ? AND premium >= ? AND dte_days <= ? AND strike IS NOT NULL AND fill_price > 0 "
        "AND expiration != '' ORDER BY premium DESC", (trade_date, NOTEWORTHY_MIN_PREMIUM, NOTEWORTHY_MAX_DTE)).fetchall()
    minutes = lambda t: int(t[:2]) * 60 + int(t[3:5])
    taken = [(r[0], r[1], r[2], r[3], minutes(r[4])) for r in db.execute(
        "SELECT ticker, strike, put_call, expiration, trade_time_et FROM picks WHERE trade_date = ?", (trade_date,))]
    kept, new_ids = [], []
    for p in prints:
        pid, tk, k, exp, pc, t, size, fill, prem, spot, dte, sent, act, upd, seen, raw = p
        key, m = (tk, k, pc, exp), minutes(t)
        if any(x[:4] == key and abs(x[4] - m) <= WHALE_DEDUPE_MIN for x in taken):
            continue
        if any(x[:4] == key and abs(x[4] - m) <= WHALE_REPEAT_MIN for x in kept):
            continue
        kept.append((*key, m))
        row = json.loads(raw) if raw else {   # older prints were stored field by field: rebuild the row
            "ticker": tk, "strike_price": k, "date_expiration": exp, "put_call": pc, "trade_date": trade_date,
            "trade_time_et": t, "size": size, "fill_price": fill, "premium": prem, "spot": spot, "dteDays": dte,
            "sentiment": sent, "option_activity_type": act, "updated": upd, "rebuilt_from_prints_id": pid}
        pcw = "Call" if pc == "CALL" else "Put"
        cur = db.execute(
            # priced by IBKR only ('ibkr_only'): Trade Echo's paid price calls skip them
            "INSERT OR IGNORE INTO picks (trade_date, trade_time_et, ticker, contract, strike, put_call, expiration, "
            "dte_days, size, fill_price, premium, score, last_score, flags, line, source, first_seen_utc, raw_json, "
            "day_stats_status, exp_stats_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '[]', ?, 'whale', ?, ?, 'ibkr_only', 'ibkr_only')",
            (trade_date, t[:5], tk, f"{k:g}{pc[0]} {exp}", k, pc, exp, dte, size, fill, prem,
             f"${tk} {k:g} {pcw} ({exp[5:7]}/{exp[8:]}) - {size:,} @ {fill} for ${prem / 1e3:,.0f}K "
             f"(raw {act or 'print'}, {(sent or 'unknown').lower()})", seen, json.dumps(row)))
        if cur.rowcount:
            new_ids.append(cur.lastrowid)
            db.execute("INSERT OR REPLACE INTO pick_context (pick_id, status, print_time, sentiment, activity, side, "
                       "legs_json, checked_utc) VALUES (?, 'side_only', ?, ?, ?, ?, ?, ?)",
                       (cur.lastrowid, t, sent, act, trade_context.side_of(pc, sent), json.dumps([row]), _utc_iso()))
        if len(kept) >= WHALE_PICKS_PER_DAY:
            break
    db.commit()
    return new_ids


def after_close_done(db, trade_date):
    return db.execute("SELECT 1 FROM daily_runs WHERE trade_date = ?", (trade_date,)).fetchone() is not None


def run_after_close(db, call, trade_date):
    """End-of-day: sweep the session's noteworthy picks, record prices, retrain."""
    print(f"\n=== After-close run for {trade_date} ===")
    outside_total, swept = 0, []
    for t_from, t_to in sweep_windows():
        payload = call("get_noteworthy_flow", noteworthy_args(trade_date, t_from, t_to),
                       "noteworthy", "*")
        total, mine, new_ids, outside = store_picks(db, payload, "sweep")
        swept += new_ids
        outside_total += outside
        print(f"  sweep {t_from}-{t_to}: {total} picks, {mine} yours, {len(new_ids)} new")
    found = len(swept)
    print(f"Sweep: {found} picks not seen live.")
    if outside_total:
        print(f"WARNING: {outside_total} rows were outside their time window - the server may be "
              "ignoring time_from/time_to, so some picks could be missed.")
    whales = add_whale_picks(db, trade_date)
    print(f"Whale-size raw prints added as training picks: {len(whales)}")
    # Judge everything still missing an opinion (sweep picks + other tickers seen live), in batches
    unjudged = [r[0] for r in db.execute(
        "SELECT id FROM picks WHERE trade_date <= ? AND (hermes_expected_gain IS NULL OR "
        "qwen_expected_gain IS NULL) AND hermes_error IS NULL AND qwen_error IS NULL",
        (trade_date,))]
    print(f"Judging {len(unjudged)} picks with both judges...")
    handle_new_picks(db, unjudged, live=False)
    counts = fetch_price_stats(db, call, trade_date)
    print(f"Prices: {counts}")
    try:  # honest after-print prices from IBKR BEFORE training (skipped if TWS is closed)
        import ibkr
        ibkr.price_picks(db)
        ibkr.price_expiries(db)
        ibkr.price_underlyings(db)
        ibkr.price_stock_iv(db)
        ibkr.refresh_greeks(db)
        import trade_context
        trade_context.update_quote_positions(db)   # IBKR bid/ask cross-check of each whale's side
    except Exception as e:
        print(f"IBKR pricing failed: {type(e).__name__}: {e} - training on what's there")
        discord_send(f"🛑 **Nightly IBKR pricing failed** ({type(e).__name__}). Today's picks have no honest "
                     "prices yet - check TWS is open and unlocked; 0DTE contracts vanish from IBKR within hours.",
                     bot="auditor")
    result = train(db)
    print(f"Model: {_describe(result)}")
    db.execute("INSERT OR REPLACE INTO daily_runs (trade_date, finished_utc, summary) VALUES (?, ?, ?)",
               (trade_date, _utc_iso(), json.dumps({"swept_new": found, "prices": counts,
                                                    "model": {k: v for k, v in result.items()
                                                              if k != "board"}})))
    db.commit()
    print(report(db))
    card = scorecard(db, trade_date, result)
    print(card)
    discord_send(card, bot="arena")


def scorecard(db, trade_date, result):
    """Nightly Discord summary: today's picks and pings, and tonight's model contest."""
    d = datetime.strptime(trade_date, "%Y-%m-%d")
    lines = [f"📊 **Nightly scorecard - {d:%a %b %d}**"]
    n_today, n_mine = db.execute(
        f"SELECT COUNT(*), SUM(ticker IN ({','.join('?' * len(MY_TICKERS))})) FROM picks WHERE trade_date = ?",
        (*sorted(MY_TICKERS), trade_date)).fetchone()
    lines.append(f"Picks today: {n_mine or 0} on your tickers, {n_today} in total (all used for training)")
    pings = db.execute(
        "SELECT ticker, strike, put_call, expiration, pred_max_gain, true_gain_pct, true_close_pct, true_source "
        "FROM picks WHERE trade_date = ? AND pinged_utc IS NOT NULL ORDER BY trade_time_et", (trade_date,)).fetchall()
    lines.append("_Grades use the best BID after the print (what you could really sell at)._")
    if pings:
        hits = sum(1 for p in pings if p[5] is not None and p[5] >= PING_MIN_GAIN)
        lines.append(f"Pings today: {len(pings)}, {hits} reached +{PING_MIN_GAIN:.0%}")
        for tk, k, pc, exp, pred, best, close, src in pings:
            outcome = (f"best {best:+.0%}, close {_signed(close)}" if best is not None
                       else "no after-print price yet")
            lines.append(f"  {'✅' if best is not None and best >= PING_MIN_GAIN else '❌'} {tk} ${k:g} "
                         f"{pc} {exp}: predicted {pred:+.0%} -> {outcome}")
    else:
        lines.append("Pings today: none")
    all_p, all_h = db.execute("SELECT COUNT(*), SUM(true_gain_pct >= ?) FROM picks WHERE pinged_utc IS NOT NULL "
                              "AND true_gain_pct IS NOT NULL", (PING_MIN_GAIN,)).fetchone()
    if all_p:
        lines.append(f"All pings so far: {all_h or 0} of {all_p} reached +{PING_MIN_GAIN:.0%}")
    if result.get("status") != "trained":
        lines.append(f"Model: {result.get('note')}")
        return "\n".join(lines)
    lines.append(f"Training data: {result['n']} picks with honest after-print prices, "
                 f"{result['n_graded']} IBKR-verified ({result['base']:.0%} of those reached "
                 f"+{PING_MIN_GAIN:.0%})")
    bp = result.get("base_profit")
    lines.append(f"Replay over {result['replay_days']} past days ({result['replay_n']} picks), each predicted using "
                 f"only earlier days. Judged on trading profit: buy at the ask right after the whale, sell at "
                 f"+30%/-50%/4 PM, after fees (following every whale: {'n/a' if bp is None else f'{bp:+.1%}'} per trade):")
    for row in result["board"][:4]:
        res = (f"{row['n_pings']} trades, avg {row['profit_avg']:+.1%}, won {row['win_rate']:.0%}"
               if row.get("profit_avg") is not None else "too few trades")
        lines.append(f"  {'🏆' if row['kind'] == result['kind'] else '•'} {row['kind']}: {res} "
                     f"(takes >= {row['threshold']:+.0%})")
    unit = "expected trade profit" if result["kind"].startswith((PROFIT_PREFIX, PICKER_PREFIX)) else "expected best gain"
    lines.append(f"Tomorrow: **{result['kind']}** pings when its {unit} is {result['threshold']:+.0%} or more.")
    return "\n".join(lines)


def rebuild(db, call, session_date):
    """Clear every derived result and recompute all picks the same way, oldest first."""
    cols = [c for c in DERIVED_COLUMNS["picks"] if not c.endswith("_tries")]
    db.execute("UPDATE picks SET " + ", ".join(f"{c} = NULL" for c in cols)
               + ", day_stats_tries = 0, exp_stats_tries = 0")
    have = {r[1] for r in db.execute("PRAGMA table_info(picks)")}
    old = [c for c in OLD_COLUMNS if c in have and c != "label_tries"]
    if old:
        db.execute("UPDATE picks SET " + ", ".join(f"{c} = NULL" for c in old))
    db.commit()
    print(f"Cleared results for {db.execute('SELECT COUNT(*) FROM picks').fetchone()[0]} picks.")

    print("\n1) Prices (print day and expiry day)")
    print(f"   {fetch_price_stats(db, call, session_date)}")
    ids = [r[0] for r in db.execute("SELECT id FROM picks ORDER BY trade_date, trade_time_et")]
    print(f"\n2) Greeks, then both judges oldest first ({len(ids)} picks)")
    for pid in ids:
        compute_greeks(db, pid)
    judge_batch(db, ids)
    print("\n3) Model")
    print(f"   {_describe(train(db))}")
    print(report(db))


def rejudge(db):
    """Re-run both judges on every pick, oldest first, with the current memory format (free)."""
    for name in JUDGES:
        db.execute(f"UPDATE picks SET {name}_verdict = NULL, {name}_expected_gain = NULL, "
                   f"{name}_confidence = NULL, {name}_reason = NULL, {name}_error = NULL")
    db.commit()
    judge_batch(db, [r[0] for r in db.execute("SELECT id FROM picks")])
    print(report(db))


def expand_training_data(db, call, first, last, last_session):
    """Re-fetch past sessions keeping ALL tickers' picks, record prices for those still in Trade
    Echo's history, then re-judge everything oldest first and retrain (consistent data)."""
    d = date.fromisoformat(first)
    while d <= date.fromisoformat(last):
        if d.weekday() < 5:
            _fetch_session(db, call, d.isoformat(), "backfill", judge_now=False)
        d += timedelta(days=1)
    print(f"Prices: {fetch_price_stats(db, call, last_session)}")
    for (pid,) in db.execute("SELECT id FROM picks WHERE iv IS NULL").fetchall():
        compute_greeks(db, pid)
    rejudge(db)
    print(f"\nModel: {_describe(train(db))}")


def run_backfill(db, call, days, today):
    """Pull the last `days` weekday sessions before `today`, then record prices and retrain."""
    sessions, d = [], today
    while len(sessions) < days:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            sessions.append(d.isoformat())
    for session in reversed(sessions):
        print(f"\n=== Backfill {session} ===")
        _fetch_session(db, call, session, "backfill")
    print(f"Prices: {fetch_price_stats(db, call, sessions[0])}")
    print(f"\nModel: {_describe(train(db))}")
    print(report(db))


def fetch_picks_only(db, call, first, last):
    """Pull picks for weekday sessions first..last. Prices are fetched later only if they are
    still inside Trade Echo's history; older picks are marked too_old without spending credits."""
    d = date.fromisoformat(first)
    while d <= date.fromisoformat(last):
        if d.weekday() < 5:
            _fetch_session(db, call, d.isoformat(), "history")
        d += timedelta(days=1)


def _fetch_session(db, call, session, source, judge_now=True):
    payload = call("get_noteworthy_flow", noteworthy_args(session, "09:30", "16:15"), "noteworthy", "*")
    payloads = [payload]
    if len(_rows(payload, "picks")) >= NOTEWORTHY_LIMIT:
        payloads = [call("get_noteworthy_flow", noteworthy_args(session, a, b), "noteworthy", "*")
                    for a, b in sweep_windows()]
    collected = []
    for p in payloads:
        if isinstance(p.get("data"), dict) and p["data"].get("date") not in (None, session):
            print(f"  {session}: server returned {p['data']['date']} instead - skipped (holiday?)")
            continue
        total, mine, new_ids, _ = store_picks(db, p, source)
        print(f"  {session}: {total} picks, {mine} yours, {len(new_ids)} new")
        collected += new_ids
    if judge_now:
        handle_new_picks(db, collected, live=False)


# ------------------------------------------------- overnight data harvest --
# Idle overnight credits pull MORE REAL training data from Trade Echo: lower-premium noteworthy
# picks ($100K+) and Algo Edge alerts. Training only - pings keep the $350K noteworthy rule.

HARVEST_MIN_PREMIUM = 100000
ALGO_CHANNELS = ["momentum_trades", "large_trades", "high_value_0dte_trades"]
ALGO_LIMIT = 50


class HarvestStopped(Exception):
    pass


def _windows(start="09:30", end="16:15", minutes=30):
    t = datetime.strptime(start, "%H:%M")
    stop = datetime.strptime(end, "%H:%M")
    while t < stop:
        nxt = min(t + timedelta(minutes=minutes), stop)
        yield t.strftime("%H:%M"), nxt.strftime("%H:%M")
        t = nxt


def _fetch_split(call, make_args, tool, rows_of, limit, a, b, deadline, out):
    """Fetch one time window; if it comes back full, split it in half so nothing is cut off."""
    if time.time() > deadline:
        raise HarvestStopped()
    payload = call(tool, make_args(a, b), "harvest", "*")
    rows = rows_of(payload)
    span = (datetime.strptime(b, "%H:%M") - datetime.strptime(a, "%H:%M")).seconds // 60
    if len(rows) >= limit and span > 4:
        mid = (datetime.strptime(a, "%H:%M") + timedelta(minutes=span // 2)).strftime("%H:%M")
        _fetch_split(call, make_args, tool, rows_of, limit, a, mid, deadline, out)
        _fetch_split(call, make_args, tool, rows_of, limit, mid, b, deadline, out)
    else:
        out.append(payload)


def store_algo_rows(db, payload, session, channel):
    """Save Algo Edge alerts as training picks (score = NULL marks them as Algo Edge)."""
    new_ids = []
    for r in _rows(payload, "rows"):
        try:
            strike, fill = float(r["strikePrice"]), float(r["fillPrice"])
            pc, exp, tk = str(r["callOrPut"]).upper(), str(r["expiration"])[:10], str(r["ticker"]).upper()
            hhmm = datetime.strptime(str(r["timeET"]).strip(), "%I:%M %p").strftime("%H:%M")
            size = int(r["size"])
        except (KeyError, TypeError, ValueError):
            continue
        dte = (date.fromisoformat(exp) - date.fromisoformat(session)).days
        if pc not in ("CALL", "PUT") or dte < 0 or dte > NOTEWORTHY_MAX_DTE:
            continue
        contract = f"{strike:g}{pc[0]} {exp}"
        # the same trade may already be stored as a noteworthy pick -> don't count it twice
        if db.execute("SELECT 1 FROM picks WHERE trade_date = ? AND ticker = ? AND contract = ? AND "
                      "trade_time_et = ? AND size = ? AND ABS(fill_price - ?) < 0.01",
                      (session, tk, contract, hhmm, size, fill)).fetchone():
            continue
        cur = db.execute(
            "INSERT OR IGNORE INTO picks (trade_date, trade_time_et, ticker, contract, strike, put_call, "
            "expiration, dte_days, size, fill_price, premium, score, last_score, flags, line, source, "
            "first_seen_utc, raw_json, spot_at_print) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, "
            "'algo', ?, ?, ?)",
            (session, hhmm, tk, contract, strike, pc, exp, dte, size, fill, r.get("value"),
             json.dumps([f"algo:{channel}"]), r.get("messageContent"), _utc_iso(), json.dumps(r),
             r.get("underlyingPrice")))
        if cur.rowcount:
            new_ids.append(cur.lastrowid)
    db.commit()
    return new_ids


def harvest_session(db, call, session, deadline):
    """Pull one session's extra training data. Raises HarvestStopped at the deadline."""
    found = {"noteworthy": 0, "algo": 0}
    pages = []
    _fetch_split(call, lambda a, b: noteworthy_args(session, a, b, HARVEST_MIN_PREMIUM), "get_noteworthy_flow",
                 lambda p: _rows(p, "picks"), NOTEWORTHY_LIMIT, "09:30", "16:15",
                 deadline, pages)
    for p in pages:
        if isinstance(p.get("data"), dict) and p["data"].get("date") == session:
            found["noteworthy"] += len(store_picks(db, p, "harvest")[2])
    for channel in ALGO_CHANNELS:
        for a, b in _windows(minutes=60):
            pages = []
            _fetch_split(call, lambda x, y, ch=channel: {
                "channel": ch, "date": session, "time_from": x, "time_to": y,
                "max_dte_days": NOTEWORTHY_MAX_DTE, "limit": ALGO_LIMIT}, "get_algo_edge_signals",
                lambda p: _rows(p, "rows"), ALGO_LIMIT, a, b, deadline, pages)
            for p in pages:
                found["algo"] += len(store_algo_rows(db, p, session, channel))
    return found


def run_harvest(db, call, sessions, deadline):
    """Harvest each not-yet-harvested session (oldest first, so data about to expire from Trade
    Echo's 7-day price history goes first), price everything, judge it, retrain. Stops cleanly at
    `deadline` (epoch) and resumes next night."""
    print(f"\n=== Overnight harvest: {', '.join(sessions)} ===")
    try:
        for session in sessions:
            if db.execute("SELECT 1 FROM harvest_runs WHERE trade_date = ?", (session,)).fetchone():
                continue
            found = harvest_session(db, call, session, deadline)
            print(f"  {session}: +{found['noteworthy']} noteworthy ($100K+), +{found['algo']} Algo Edge")
            if time.time() > deadline:
                raise HarvestStopped()
            print(f"  prices: {fetch_price_stats(db, call, sessions[-1])}")
            db.execute("INSERT OR REPLACE INTO harvest_runs (trade_date, finished_utc, summary) VALUES (?, ?, ?)",
                       (session, _utc_iso(), json.dumps(found)))
            db.commit()
    except HarvestStopped:
        print("  stopped for the market open - the rest continues tonight")
    new = [r[0] for r in db.execute("SELECT id FROM picks WHERE hermes_expected_gain IS NULL OR "
                                    "qwen_expected_gain IS NULL")]
    for pid in [r[0] for r in db.execute("SELECT id FROM picks WHERE iv IS NULL AND spot_at_print IS NOT NULL")]:
        compute_greeks(db, pid)
    if new and time.time() < deadline:
        print(f"  judging {len(new)} new picks with both judges...")
        judge_batch(db, new, deadline=deadline)
    if time.time() < deadline:
        print(f"  model: {_describe(train(db))}")


def _describe(result):
    if result["status"] != "trained":
        return f"waiting for data - {result['note']}"
    hit = f"{result['hit']:.0%}" if result["hit"] is not None else "n/a"
    pa, bp = result.get("profit_avg"), result.get("base_profit")
    board = ", ".join(f"{r['kind']} {r['profit_avg']:+.1%}/trade x{r['n_pings']}" if r.get("profit_avg") is not None
                      else f"{r['kind']} (too few trades)" for r in result.get("board", [])[:5])
    unit = "expected profit" if result["kind"].startswith((PROFIT_PREFIX, PICKER_PREFIX)) else "expected best gain"
    return (f"winner '{result['kind']}' on {result['n']} picks; replay of {result['replay_days']} days "
            f"({result['replay_n']} picks): takes trades when its {unit} >= {result['threshold']:+.0%} -> "
            f"{result['n_hit']} trades, avg {'n/a' if pa is None else f'{pa:+.1%}'} per trade after fees "
            f"(following every whale: {'n/a' if bp is None else f'{bp:+.1%}'}); {hit} reached +{PING_MIN_GAIN:.0%}; "
            f"rank agreement with the spike {result['spearman']:+.2f}. Leaderboard (blind profit): {board}")


def report(db):
    lines = ["--- Picks report ---"]
    total, mine = db.execute(f"SELECT COUNT(*), SUM(ticker IN ({','.join('?' * len(MY_TICKERS))})) "
                             "FROM picks", tuple(sorted(MY_TICKERS))).fetchone()
    status = dict(db.execute("SELECT COALESCE(day_stats_status, 'pending'), COUNT(*) FROM picks GROUP BY 1"))
    lines.append(f"Picks: {total} ({mine or 0} on your tickers); print-day prices: {status}")
    n, avg_g, avg_l, avg_c, hits = db.execute(
        "SELECT COUNT(*), AVG(max_gain_pct), AVG(max_loss_pct), AVG(close_pct), SUM(max_gain_pct >= ?) "
        "FROM picks WHERE day_stats_status = 'ok'", (PING_MIN_GAIN,)).fetchone()
    if n:
        lines.append(f"Print day (n={n}): avg best {avg_g:+.0%}, avg worst {avg_l:+.0%}, "
                     f"avg close {avg_c:+.0%}; {hits} of {n} reached +{PING_MIN_GAIN:.0%}")
    ne, avg_e = db.execute("SELECT COUNT(*), AVG(exp_close_pct) FROM picks "
                           "WHERE exp_stats_status = 'ok'").fetchone()
    if ne:
        lines.append(f"Expiry day (n={ne}): avg close vs fill {avg_e:+.0%}")
    if n:
        lines.append("  (Trade Echo daily prices include moves from BEFORE the print - overstated)")
    for src, label in (("ibkr", "IBKR best bid after the print"), ("te_confirmed", "Trade Echo, high after the print")):
        tn, tg, tc, th = db.execute("SELECT COUNT(*), AVG(true_gain_pct), AVG(true_close_pct), "
                                    "SUM(true_gain_pct >= ?) FROM picks WHERE true_source = ?",
                                    (PING_MIN_GAIN, src)).fetchone()
        if tn:
            lines.append(f"Honest outcome - {label} (n={tn}): avg best {tg:+.0%}, avg close "
                         f"{_signed(tc)}; {th} of {tn} reached +{PING_MIN_GAIN:.0%}")
    try:
        import pandas as pd
        for name in JUDGES:
            rows = db.execute(f"SELECT {name}_expected_gain, true_gain_pct FROM picks WHERE true_source "
                              f"= 'ibkr' AND {name}_expected_gain IS NOT NULL").fetchall()
            if len(rows) > 2:
                df = pd.DataFrame(rows, columns=["est", "act"])
                lines.append(f"{name.capitalize()} (n={len(df)}): rank agreement with honest best gain "
                             f"{df.est.corr(df.act, method='spearman'):+.2f}, avg estimate {df.est.mean():+.0%} "
                             f"vs real {df.act.mean():+.0%}")
    except ImportError:
        pass
    run = db.execute("SELECT status, version, n_rows, cv_mae, cv_spearman, cv_hit_rate, cv_n_hit, note, "
                     "model_kind, ping_threshold FROM model_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run:
        if run[0] == "trained":
            hit = f"{run[5]:.0%}" if run[5] is not None else "n/a"
            lines.append(f"Model {run[1]} ({run[8] or 'linear'}): {run[2]} picks; replay rank agreement "
                         f"{run[4]:+.2f}; ping bar +{(run[9] or PING_MIN_GAIN):.0%} -> {hit} of {run[6]} "
                         f"pings reached +{PING_MIN_GAIN:.0%}")
        else:
            lines.append(f"Model: {run[7]}")
    p, ph = db.execute("SELECT COUNT(*), SUM(true_gain_pct >= ?) FROM picks WHERE pinged_utc IS NOT NULL "
                       "AND true_gain_pct IS NOT NULL", (PING_MIN_GAIN,)).fetchone()
    pinged = db.execute("SELECT COUNT(*) FROM picks WHERE pinged_utc IS NOT NULL").fetchone()[0]
    lines.append(f"Pings sent: {pinged} ({ph or 0} of {p} with results reached +{PING_MIN_GAIN:.0%}); "
                 f"pings since {PINGS_START}")
    return "\n".join(lines)
