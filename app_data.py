"""Read-only data for the FlowDesk desktop app (flow_app.py).

Never writes flow.db and never talks to IBKR or Trade Echo: it only shows what the logger and
the IBKR live tracker already saved. P&L is measured like the Discord follow-ups: from the
whale's fill at the print time to the best BID (the price you could actually sell at).
"""

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DB = Path(__file__).parent / "flow.db"
ALERT_ORDER = ["+30%", "+50%", "+100%", "-50%"]
SPARK_POINTS = 80


def _db():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _now():
    return datetime.now(ET)


def _market_open(now):
    return now.weekday() < 5 and "09:30" <= now.strftime("%H:%M") < "16:00"


def _minute_of(utc_iso):
    """'2026-10-02T14:04:05+00:00' -> '2026-10-02 10:04' (ET)."""
    return datetime.fromisoformat(utc_iso).astimezone(ET).strftime("%Y-%m-%d %H:%M")


def status():
    now = _now()
    with closing(_db()) as db:
        last_poll = db.execute("SELECT MAX(started_utc) FROM polls").fetchone()[0]
        last_quote = db.execute("SELECT MAX(minute_et) FROM ib_live_quotes").fetchone()[0]
        run = db.execute("SELECT run_utc, version, model_kind FROM model_runs WHERE status = 'trained' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    poll_min = (datetime.now(timezone.utc) - datetime.fromisoformat(last_poll)).total_seconds() / 60 if last_poll else None
    quote_min = ((now.replace(tzinfo=None) - datetime.fromisoformat(last_quote)).total_seconds() / 60
                 if last_quote else None)
    is_open = _market_open(now)
    return {
        "now_et": now.strftime("%a %b %d, %I:%M %p ET").replace(" 0", " "),
        "market_open": is_open,
        "flow": {"ok": poll_min is not None and poll_min < 6, "age_min": poll_min},
        "ibkr": {"ok": quote_min is not None and quote_min < 5, "idle": not is_open, "age_min": quote_min},
        "model": {"version": run["version"], "kind": run["model_kind"], "trained": _minute_of(run["run_utc"])}
        if run else None,
    }


def _path(db, p, kinds=("BID",)):
    """Minute -> price for one pick's contract, from the print minute through the last data we
    have: live streamed quotes, the print-day and expiry-day bars, and the holding-day bars."""
    start = f"{p['trade_date']} {p['trade_time_et']}"
    key = (p["ticker"], p["strike"], p["put_call"], p["expiration"])
    out = {}
    col = "bid" if kinds[0] == "BID" else "ask"
    for m, v in db.execute(f"SELECT minute_et, {col} FROM ib_live_quotes WHERE ticker = ? AND strike = ? AND "
                           f"put_call = ? AND expiration = ? AND minute_et >= ? AND {col} > 0", (*key, start)):
        out[m] = v
    # recorded 1-minute bars are the official record, so they win over the live snapshots
    for m, v in db.execute("SELECT minute_et, close FROM ib_contract_bars WHERE ticker = ? AND strike = ? AND "
                           "put_call = ? AND expiration = ? AND kind = ? AND minute_et >= ? AND close > 0",
                           (*key, kinds[0], start)):
        out[m] = v
    marks = ",".join("?" * len(kinds) * 2)
    for m, v in db.execute(f"SELECT minute_et, close FROM ib_bars WHERE pick_id = ? AND kind IN ({marks}) "
                           "AND minute_et >= ? AND close > 0",
                           (p["id"], *kinds, *[f"EXP_{k}" for k in kinds], start)):
        out[m] = v
    return sorted(out.items())


def _card(db, p, now):
    fill = p["fill_price"]
    start = f"{p['trade_date']} {p['trade_time_et']}"
    bids = _path(db, p)
    after = [(m, b) for m, b in bids if m > start]       # the fill minute itself includes pre-print prices
    last_m, last_bid = bids[-1] if bids else (None, None)
    best_m, best_bid = max(after, key=lambda r: r[1]) if after else (None, None)
    expired = p["expiration"] < now.date().isoformat() or (
        p["expiration"] == now.date().isoformat() and now.strftime("%H:%M") >= "16:00")
    live = (not expired and _market_open(now) and last_m is not None
            and (now.replace(tzinfo=None) - datetime.fromisoformat(last_m)).total_seconds() < 300)
    alerts = {r[0]: _minute_of(r[1]) for r in db.execute(
        "SELECT level, sent_utc FROM ib_ping_alerts WHERE pick_id = ?", (p["id"],))}
    step = max(1, len(bids) // SPARK_POINTS)
    spark = [b for _, b in bids[::step]] + ([last_bid] if bids and (len(bids) - 1) % step else [])
    dte = (date.fromisoformat(p["expiration"]) - now.date()).days
    pct = lambda x: None if x is None or not fill else x / fill - 1
    return {
        "id": p["id"], "ticker": p["ticker"], "strike": p["strike"], "put_call": p["put_call"],
        "expiration": p["expiration"], "dte": dte, "trade_date": p["trade_date"], "time": p["trade_time_et"],
        "fill": fill, "size": p["size"], "premium": p["premium"],
        "state": "expired" if expired else ("live" if live else "closed"),
        "bid": last_bid, "bid_at": last_m, "pnl": pct(last_bid),
        "best": best_bid, "best_at": best_m, "best_pnl": pct(best_bid),
        "model": p["pred_max_gain"], "hermes": p["hermes_expected_gain"], "qwen": p["qwen_expected_gain"],
        "alerts": [a for a in ALERT_ORDER if a in alerts], "spark": spark,
    }


def pings(scope="open"):
    """Every pinged contract: 'open' = not expired yet (incl. earlier days' pings), 'session' = the
    latest trading day that had pings, 'all'."""
    now = _now()
    with closing(_db()) as db:
        arg = (now.date().isoformat() if scope != "session" else
               db.execute("SELECT MAX(trade_date) FROM picks WHERE pinged_utc IS NOT NULL").fetchone()[0])
        where = {"open": "expiration >= ?", "session": "trade_date = ?", "all": "? IS NOT NULL"}[scope]
        rows = db.execute(f"SELECT * FROM picks WHERE pinged_utc IS NOT NULL AND {where} "
                          "ORDER BY trade_date DESC, trade_time_et DESC", (arg,)).fetchall()
        cards = [_card(db, p, now) for p in rows]
    if scope == "open":   # 'open' means still tradable: drop today's expiries once the close has passed
        cards = [c for c in cards if c["state"] != "expired"]
    return {"scope": scope, "cards": cards}


def detail(pick_id):
    now = _now()
    with closing(_db()) as db:
        p = db.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone()
        if not p:
            return None
        card = _card(db, p, now)
        bids = _path(db, p)
        asks = _path(db, p, kinds=("ASK",))
        entry = db.execute("SELECT minute_et, delay_sec, bid, ask, iv, delta, open_interest FROM ib_entries "
                           "WHERE pick_id = ? AND status = 'ok'", (pick_id,)).fetchone()
        stats = db.execute("SELECT spread_at_print, spread_pct_at_print, median_spread_pct FROM ib_stats "
                           "WHERE pick_id = ?", (pick_id,)).fetchone()
        # latency: the first whale-watch sighting of a whale-size print on this contract near the
        # trade time (the scored noteworthy row can be timed a minute or two differently)
        h, m = (int(x) for x in p["trade_time_et"][:5].split(":"))
        lo, hi = f"{max(h * 60 + m - 3, 0) // 60:02d}:{max(h * 60 + m - 3, 0) % 60:02d}:00", \
                 f"{(h * 60 + m + 3) // 60:02d}:{(h * 60 + m + 3) % 60:02d}:59"
        whale = db.execute(
            "SELECT p.first_seen_utc, p.trade_time_et, p.updated_utc FROM prints p JOIN polls q ON q.id = p.poll_id "
            "WHERE q.kind = 'whale' AND p.ticker = ? AND p.strike = ? AND p.put_call = ? AND p.expiration = ? "
            "AND p.trade_date = ? AND p.trade_time_et BETWEEN ? AND ? ORDER BY p.first_seen_utc LIMIT 1",
            (p["ticker"], p["strike"], p["put_call"], p["expiration"], p["trade_date"], lo, hi)).fetchone()
    flags = json.loads(p["flags"]) if p["flags"] else []
    alerts = []
    with closing(_db()) as db:
        for level, sent in db.execute("SELECT level, sent_utc FROM ib_ping_alerts WHERE pick_id = ?", (pick_id,)):
            alerts.append({"level": level, "minute": _minute_of(sent)})
    return {
        **card,
        "bids": bids, "asks": asks, "alerts_at": sorted(alerts, key=lambda a: a["minute"]),
        "line": p["line"], "score": p["score"], "flags": flags,
        # theta is per share per day; theta_dollars_day is the whole position (x 100 x size)
        "greeks": {"iv": p["iv"], "delta": p["delta"],
                   "theta_contract": p["theta"] * 100 if p["theta"] is not None else None,
                   "spot": p["spot_at_print"]},
        "pinged_at": _minute_of(p["pinged_utc"]),
        "listed_at": _minute_of(p["first_seen_utc"]) if p["first_seen_utc"] else None,
        "whale_watch": {"seen": datetime.fromisoformat(whale[0]).astimezone(ET).strftime("%Y-%m-%d %H:%M:%S"),
                        "trade_time": whale[1]} if whale else None,
        "ping_entry": dict(entry) if entry else None,
        "spread": dict(stats) if stats else None,
        "final": {"best": p["true_gain_pct"], "close": p["true_close_pct"], "source": p["true_source"],
                  "exp_close": p["true_exp_close_pct"]},
        "reasons": {"hermes": p["hermes_reason"], "qwen": p["qwen_reason"]},
    }
