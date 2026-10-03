"""Read-only data for FlowDesk's Learning and Predictions screens.

Learning: the nightly training runs, the model contest, the walk-forward replay day by day (each
day predicted by a model trained only on earlier days) and an interactive exit simulator that
replays IBKR's real 1-minute bids. Predictions: the model and both AI judges against what really
happened. All grading uses IBKR outcomes only (the best BID after the print), like the nightly run.
"""

import json
import time
from contextlib import closing

import numpy as np

from app_data import _db

HIT = 0.30                      # "a winner" = the bid reached +30% after the print
SIM_CACHE_SEC = 600
GRID_TARGETS = [0.10, 0.20, 0.30, 0.50, 0.75, 1.00, 1.50, None]
GRID_STOPS = [-0.10, -0.25, -0.50, -0.75, None]
DOLLARS_PER_TRADE = 1000

# what the model's inputs mean, in plain words (Analyst's rank agreements are shown with these)
FEATURE_WORDS = {
    "score": "Trade Echo's flow score", "log_premium": "Dollar size of the trade", "log_fill": "Option price",
    "dte": "Days to expiry", "hours_to_expiry": "Hours to expiry", "is_call": "Call (vs put)",
    "is_index": "Index ETF (SPY/QQQ)", "minutes_since_open": "Time of day", "otm_pct": "How far out of the money",
    "iv": "Implied volatility", "abs_delta": "Delta (how much it moves with the stock)", "theta_pct": "Time decay",
    "vega_pct": "Sensitivity to volatility", "gamma_x_spot": "Gamma", "gex_rating": "Dealer gamma rating",
    "flip_dist_pct": "Distance to the gamma flip", "charm_dist_pct": "Distance to the charm level",
    "atm_iv": "At-the-money IV", "n_setups": "Dealer setups active", "repeat_30m": "Repeat buys of the contract",
    "flow_call_share_30m": "Call share of recent flow", "hermes_est": "Hermes' guess", "qwen_est": "Qwen's guess",
    "is_my_ticker": "One of your 12 tickers", "from_algo": "Algo Edge alert", "stock_with_30m": "Stock moving its way (30 min)",
    "stock_with_day": "Stock moving its way (day)", "stock_range_30m": "Stock's recent range",
    "spy_with_30m": "Market moving its way (30 min)", "spy_with_day": "Market moving its way (day)",
    "stock_iv_prev": "Stock's IV yesterday", "stock_iv_rank_1y": "IV rank (1 year)", "iv_vs_stock_iv": "Option IV vs stock IV",
    "side_buy": "Whale bought (vs sold)", "is_multileg": "Part of a spread", "leg_sold": "Sold leg of a spread",
    "flag_sweep_or_block": "Sweep or block", "flag_aggressive_execution": "Paid at the ask", "flag_algo_edge": "Algo Edge flag",
    "other_flags": "Other flags",
}


def _latest_run(db):
    cols = {r[1] for r in db.execute("PRAGMA table_info(model_runs)")}
    profit = ("cv_profit_avg, base_profit_avg" if "cv_profit_avg" in cols else "NULL AS cv_profit_avg, NULL AS base_profit_avg")
    return db.execute("SELECT version, model_kind, ping_threshold, leaderboard, run_utc, n_rows, cv_spearman, "
                      f"cv_hit_rate, cv_n_hit, base_rate_hit, {profit} FROM model_runs WHERE status = 'trained' "
                      "ORDER BY id DESC LIMIT 1").fetchone()


def _is_profit(kind):
    return bool(kind) and kind.startswith("profit:")


def _rank_corr(a, b):
    import pandas as pd
    s = pd.DataFrame({"a": a, "b": b}).dropna()
    return float(s.a.corr(s.b, method="spearman")) if len(s) > 2 and s.a.nunique() > 1 else None


# ------------------------------------------------------------------ learning --

def learning():
    with closing(_db()) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(model_runs)")}
        profit = ("cv_profit_avg AS profit, base_profit_avg AS base_profit" if "cv_profit_avg" in cols
                  else "NULL AS profit, NULL AS base_profit")
        runs = [dict(r) for r in db.execute(
            "SELECT run_utc, version, model_kind, n_rows, cv_spearman AS rank, cv_hit_rate AS hit, cv_n_hit AS n_pings, "
            f"base_rate_hit AS base, ping_threshold AS bar, {profit} FROM model_runs WHERE status = 'trained' "
            "AND target IN ('after_print_gain', 'trade_profit') ORDER BY id")]
        run = _latest_run(db)
        board = json.loads(run["leaderboard"]) if run and run["leaderboard"] else []
        # walk-forward replay: each day predicted by a model that only saw earlier days
        rows = db.execute(
            "SELECT p.trade_date, r.pred, p.true_gain_pct FROM replay_preds r JOIN picks p ON p.id = r.pick_id "
            "WHERE r.version = ? AND r.kind = ? AND p.true_source = 'ibkr' AND p.true_gain_pct IS NOT NULL",
            (run["version"], run["model_kind"])).fetchall() if run else []
        analyst = db.execute("SELECT data_json FROM bot_reports WHERE bot = 'analyst' ORDER BY id DESC LIMIT 1").fetchone()
    bar = run["ping_threshold"] if run else HIT
    days = {}
    for d, pred, real in rows:
        x = days.setdefault(d, {"all": [], "model": []})
        x["all"].append(real)
        if pred is not None and pred >= bar:
            x["model"].append(real)
    replay = [{"day": d, "n_all": len(v["all"]), "hit_all": _share(v["all"]), "n_model": len(v["model"]),
               "hit_model": _share(v["model"]), "avg_model": _avg(v["model"]), "avg_all": _avg(v["all"])}
              for d, v in sorted(days.items())]
    corr = json.loads(analyst[0]).get("feature_corr", {}) if analyst else {}
    inputs = [{"name": FEATURE_WORDS.get(k, k.replace("_", " ")), "key": k, "corr": v[0], "n": v[1]}
              for k, v in list(corr.items())[:10]]
    return {"runs": runs, "board": board, "winner": run["model_kind"] if run else None, "bar": bar,
            "replay": replay, "inputs": inputs,
            "latest": {"version": run["version"], "n": run["n_rows"], "rank": run["cv_spearman"], "hit": run["cv_hit_rate"],
                       "n_pings": run["cv_n_hit"], "base": run["base_rate_hit"], "kind": run["model_kind"],
                       "profit": run["cv_profit_avg"], "base_profit": run["base_profit_avg"],
                       "profit_model": _is_profit(run["model_kind"])} if run else None}


def _share(xs):
    return sum(1 for x in xs if x >= HIT) / len(xs) if xs else None


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


# ----------------------------------------------------------- exit simulator --

_sim = {"at": 0.0, "data": None}


def _sim_data():
    """Every IBKR-priced pick with its bid path after the print, the ask at the print minute and your
    ask at the ping. Cached for 10 minutes (the nightly run is what changes it)."""
    if _sim["data"] is not None and time.time() - _sim["at"] < SIM_CACHE_SEC:
        return _sim["data"]
    with closing(_db()) as db:
        run = _latest_run(db)
        bar = run["ping_threshold"] if run else HIT
        replay = dict(db.execute("SELECT pick_id, pred FROM replay_preds WHERE version = ? AND kind = ?",
                                 (run["version"], run["model_kind"]))) if run else {}
        picks = db.execute(
            "SELECT p.id, p.ticker, p.strike, p.put_call, p.expiration, p.trade_date, p.trade_time_et, p.fill_price, "
            "p.premium, p.score, p.pinged_utc, p.hermes_verdict, p.qwen_verdict, c.side, c.structure, c.our_leg FROM picks p "
            "JOIN ib_stats s ON s.pick_id = p.id LEFT JOIN pick_context c ON c.pick_id = p.id AND c.status = 'ok' "
            "WHERE s.status = 'ok' AND s.n_bid_bars > 0 ORDER BY p.trade_date, p.trade_time_et").fetchall()
        # one indexed range read per pick (a joined string comparison can't use the index: ~10x slower)
        bids, ask_print = {}, {}
        for p in picks:
            start = f"{p['trade_date']} {p['trade_time_et'][:5]}"
            bids[p["id"]] = db.execute(
                "SELECT minute_et, high, low, close FROM ib_bars WHERE pick_id = ? AND kind = 'BID' AND minute_et >= ? "
                "AND high > 0 ORDER BY minute_et", (p["id"], start)).fetchall()
            a = db.execute("SELECT close FROM ib_bars WHERE pick_id = ? AND kind = 'ASK' AND minute_et = ? AND close > 0",
                           (p["id"], start)).fetchone()
            if a:
                ask_print[p["id"]] = a[0]
        entries = {pid: (ask, m) for pid, ask, m in db.execute(
            "SELECT pick_id, ask, minute_et FROM ib_entries WHERE status = 'ok' AND ask > 0")}
    from pipeline import MY_TICKERS, NOTEWORTHY_MIN_PREMIUM
    out = []
    for p in picks:
        path = bids.get(p["id"])
        if not path:
            continue
        sold = p["our_leg"] == "sold" or (p["structure"] is None and p["side"] == "sell")
        core = p["score"] is not None and (p["premium"] or 0) >= NOTEWORTHY_MIN_PREMIUM
        out.append({
            "id": p["id"], "ticker": p["ticker"], "contract": f"{p['ticker']} ${p['strike']:g} {p['put_call'].title()} {p['expiration'][5:]}",
            "day": p["trade_date"], "start": f"{p['trade_date']} {p['trade_time_et'][:5]}",
            "fill": p["fill_price"], "ask_print": ask_print.get(p["id"]), "ping": entries.get(p["id"]),
            "minutes": [r[0] for r in path], "high": np.array([r[1] for r in path]), "low": np.array([r[2] for r in path]),
            "close": np.array([r[3] for r in path]),
            "side": p["side"] or "unknown", "sold": sold, "structure": p["structure"],
            "takes": {"hermes": p["hermes_verdict"] == "take", "qwen": p["qwen_verdict"] == "take"},
            "groups": {"all", *(["core"] if core else []), *(["mine"] if core and p["ticker"] in MY_TICKERS else []),
                       *(["model"] if replay.get(p["id"]) is not None and replay[p["id"]] >= bar else []),
                       *(["pings"] if p["pinged_utc"] else [])},
        })
    _sim["data"], _sim["at"] = {"trades": out, "bar": bar}, time.time()
    return _sim["data"]


def _entry(t, mode):
    """(entry price, first minute you could sell in) for a trade, or None if that entry is unknown."""
    if mode == "whale":
        return t["fill"], t["start"]
    if mode == "ask_print":
        return (t["ask_print"], t["start"]) if t["ask_print"] else None
    if mode == "ping":
        return (t["ping"][0], t["ping"][1]) if t["ping"] else None
    raise ValueError(mode)


def _exit(t, price, start, target, stop):
    """Same rule as the Simulator bot: walk the bid minute by minute after the entry minute; the stop
    is checked first inside a minute (cautious); else sell at the day's last bid."""
    i0 = next((i for i, m in enumerate(t["minutes"]) if m > start), None)
    if i0 is None:
        return None, None, None
    hi, lo = t["high"][i0:], t["low"][i0:]
    n = len(hi)
    s_idx = int(np.argmax(lo <= price * (1 + stop))) if stop is not None and (lo <= price * (1 + stop)).any() else n
    t_idx = int(np.argmax(hi >= price * (1 + target))) if target is not None and (hi >= price * (1 + target)).any() else n
    if s_idx <= t_idx and s_idx < n:
        return stop, "stop", t["minutes"][i0 + s_idx]
    if t_idx < n:
        return target, "target", t["minutes"][i0 + t_idx]
    return float(t["close"][-1] / price - 1), "close", t["minutes"][-1]


def _select(data, group, side):
    keep = {"any": lambda t: True, "buy": lambda t: t["side"] == "buy", "mid": lambda t: t["side"] == "mid",
            "sell": lambda t: t["sold"], "not_sold": lambda t: not t["sold"],
            "unknown": lambda t: t["side"] == "unknown"}[side]
    return [t for t in data["trades"] if group in t["groups"] and keep(t)]


def simulate(group="all", side="any", entry="whale", target=None, stop=None):
    data = _sim_data()
    chosen = _select(data, group, side)
    results = []
    for t in chosen:
        e = _entry(t, entry)
        if not e or not e[0]:
            continue
        ret, why, _ = _exit(t, e[0], e[1], target, stop)
        if ret is not None:
            results.append({"id": t["id"], "day": t["day"], "start": t["start"], "contract": t["contract"],
                            "side": t["side"], "entry": e[0], "ret": ret, "why": why})
    rets = np.array([r["ret"] for r in results], dtype=float)
    curve, total = [], 0.0
    for r in sorted(results, key=lambda r: r["start"]):
        total += r["ret"] * DOLLARS_PER_TRADE
        curve.append([r["start"], round(total, 2)])
    edges = [-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0, 2.0, 1e9]
    labels = ["−100 to −75%", "−75 to −50%", "−50 to −25%", "−25 to 0%", "0 to +25%", "+25 to +50%",
              "+50 to +100%", "+100 to +200%", "+200%+"]
    hist = [int(((rets >= a) & (rets < b)).sum()) if len(rets) else 0 for a, b in zip(edges, edges[1:])]
    stats = None
    if len(rets):
        stats = {"n": int(len(rets)), "win_rate": float((rets > 0).mean()), "avg": float(rets.mean()),
                 "median": float(np.median(rets)), "best": float(rets.max()), "worst": float(rets.min()),
                 "total_dollars": float(rets.sum() * DOLLARS_PER_TRADE),
                 "exits": {k: sum(1 for r in results if r["why"] == k) for k in ("target", "stop", "close")}}
    return {"stats": stats, "curve": curve, "hist": {"labels": labels, "counts": hist},
            "trades": sorted(results, key=lambda r: r["start"], reverse=True)[:150],
            "available": len(chosen), "dollars_per_trade": DOLLARS_PER_TRADE, "model_bar": data["bar"]}


def exit_grid(group="all", side="any", entry="whale"):
    """Average return per trade for every take-profit x stop-loss pair (for the heat map)."""
    data = _sim_data()
    chosen = [(t, e) for t in _select(data, group, side) if (e := _entry(t, entry)) and e[0]]
    cells = []
    for tg in GRID_TARGETS:
        for st in GRID_STOPS:
            rets = [r for t, e in chosen if (r := _exit(t, e[0], e[1], tg, st)[0]) is not None]
            cells.append({"target": tg, "stop": st, "n": len(rets),
                          "avg": float(np.mean(rets)) if rets else None,
                          "win_rate": float(np.mean([r > 0 for r in rets])) if rets else None})
    return {"targets": GRID_TARGETS, "stops": GRID_STOPS, "cells": cells}


# ------------------------------------------------------------------- contest --

# Paper-money contest: each trader starts with $5,000 and buys the picks IT chose, under the same rules,
# so only the picking differs. A separate scoreboard: nothing here is fed back to the judges or the model.
CONTEST_CASH = 5000.0
CONTEST_SIZE = 0.10                    # of the account per trade (fractional contracts allowed)
CONTEST_TARGET, CONTEST_STOP = 0.30, -0.50
FEE_PER_CONTRACT = 0.65                # IBKR's options commission, each way
CONTESTANTS = [("hermes", "Hermes", "AI judge on this PC - buys when it says 'take'"),
               ("qwen", "Qwen", "AI judge on this PC - buys when it says 'take'"),
               ("model", "The model", "machine-learning model - buys the picks it would ping (tested blind)"),
               ("all", "Follow every whale", "baseline - buys every pick")]


def _chooses(key, t):
    return True if key == "all" else ("model" in t["groups"]) if key == "model" else t["takes"][key]


def _run_account(trades):
    """trades: (entry minute, exit minute, entry price, return, trade). Cash is tied up until each exit;
    each new trade gets 10% of the account (cash + money in open trades), if the cash is there."""
    import heapq
    cash, heap, seq, log, by_day, skipped = CONTEST_CASH, [], 0, [], {}, 0

    def settle(until):
        nonlocal cash
        while heap and heap[0][0] <= until:
            ex_m, _, stake, contracts, ret, rec = heapq.heappop(heap)
            fee_out = FEE_PER_CONTRACT * contracts
            cash += stake * (1 + ret) - fee_out
            rec["pnl"] = stake * ret - rec["fee_in"] - fee_out
            by_day[ex_m[:10]] = cash + sum(h[2] for h in heap)

    for entry_m, exit_m, price, ret, t in sorted(trades, key=lambda x: x[0]):
        settle(entry_m)
        equity = cash + sum(h[2] for h in heap)
        per_dollar_fee = FEE_PER_CONTRACT / (price * 100)
        stake = min(equity * CONTEST_SIZE, cash / (1 + per_dollar_fee))
        if stake < 25:                      # no cash left right now
            skipped += 1
            continue
        contracts = stake / (price * 100)
        rec = {"start": entry_m, "contract": t["contract"], "entry": price, "stake": stake, "ret": ret,
               "fee_in": FEE_PER_CONTRACT * contracts, "why": None, "id": t["id"]}
        cash -= stake + rec["fee_in"]
        heapq.heappush(heap, (exit_m, seq, stake, contracts, ret, rec))
        seq += 1
        log.append(rec)
    settle("9999")
    return cash, log, by_day, skipped


def contest(universe="all"):
    """universe: 'all' picks the judges judged, 'core' = big noteworthy picks ($350K+), 'mine' = your tickers."""
    data = _sim_data()
    days = sorted({t["day"] for t in data["trades"]})
    out = []
    for key, name, about in CONTESTANTS:
        trades = []
        for t in data["trades"]:
            if universe not in t["groups"] or not _chooses(key, t) or not t["ask_print"]:
                continue
            ret, why, exit_m = _exit(t, t["ask_print"], t["start"], CONTEST_TARGET, CONTEST_STOP)
            if ret is not None:
                trades.append((t["start"], exit_m, t["ask_print"], ret, t))
                t.setdefault("_why", {})[key] = why
        final, log, by_day, skipped = _run_account(trades)
        curve, value = [], CONTEST_CASH
        for d in days:                      # end-of-day account value, carried over quiet days
            value = by_day.get(d, value)
            curve.append(round(value, 2))
        peak, max_dd = CONTEST_CASH, 0.0
        for v in [CONTEST_CASH] + curve:
            peak = max(peak, v)
            max_dd = min(max_dd, v / peak - 1)
        pnls = [r["pnl"] for r in log]
        by_id = {t["id"]: t for t in data["trades"]}
        out.append({
            "key": key, "name": name, "about": about, "final": final, "ret": final / CONTEST_CASH - 1,
            "trades": len(log), "wins": sum(p > 0 for p in pnls), "skipped": skipped, "max_dd": max_dd,
            "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None, "curve": curve,
            "log": [{"start": r["start"], "contract": r["contract"], "entry": r["entry"], "stake": round(r["stake"], 2),
                     "ret": r["ret"], "pnl": round(r["pnl"], 2), "id": r["id"],
                     "why": by_id[r["id"]].get("_why", {}).get(key)} for r in sorted(log, key=lambda r: r["start"], reverse=True)[:80]],
        })
    return {"universe": universe, "days": days, "traders": out, "rules": {"cash": CONTEST_CASH, "size": CONTEST_SIZE, "target": CONTEST_TARGET,
                                                    "stop": CONTEST_STOP, "fee": FEE_PER_CONTRACT}}


# --------------------------------------------------------------- predictions --

def predictions(scope="mine"):
    from pipeline import MY_TICKERS
    with closing(_db()) as db:
        run = _latest_run(db)
        has_trade = "trade_ret" in {r[1] for r in db.execute("PRAGMA table_info(picks)")}
        graded = db.execute(
            "SELECT p.id, p.ticker, r.pred, p.hermes_expected_gain, p.qwen_expected_gain, p.true_gain_pct, "
            f"{'p.trade_ret' if has_trade else 'NULL'} FROM picks p LEFT JOIN replay_preds r ON r.pick_id = p.id "
            "AND r.version = ? AND r.kind = ? WHERE p.true_source = 'ibkr' AND p.true_gain_pct IS NOT NULL",
            (run["version"] if run else "", run["model_kind"] if run else "")).fetchall()
        recent = db.execute(
            "SELECT p.id, p.trade_date, p.trade_time_et, p.ticker, p.strike, p.put_call, p.expiration, p.fill_price, "
            "p.premium, p.pred_max_gain, r.pred AS replay, p.hermes_expected_gain AS hermes, p.qwen_expected_gain AS qwen, "
            "p.true_gain_pct AS real, p.true_close_pct AS close, p.pinged_utc IS NOT NULL AS pinged, c.side, c.structure, "
            f"{'p.trade_ret' if has_trade else 'NULL'} AS trade, "
            "c.our_leg FROM picks p LEFT JOIN replay_preds r ON r.pick_id = p.id AND r.version = ? AND r.kind = ? "
            "LEFT JOIN pick_context c ON c.pick_id = p.id AND c.status = 'ok' WHERE p.score IS NOT NULL "
            + (f"AND p.ticker IN ({','.join('?' * len(MY_TICKERS))}) " if scope == "mine" else "")
            + "ORDER BY p.trade_date DESC, p.trade_time_et DESC LIMIT 200",
            (run["version"] if run else "", run["model_kind"] if run else "",
             *(sorted(MY_TICKERS) if scope == "mine" else []))).fetchall()
    real = [r[5] for r in graded]
    trades = [r[6] for r in graded if r[6] is not None]
    scorecard = []
    for name, idx in (("Model (walk-forward)", 2), ("Hermes (AI judge)", 3), ("Qwen (AI judge)", 4)):
        rows = [r for r in graded if r[idx] is not None]
        if len(rows) < 5:
            continue
        rows.sort(key=lambda r: -r[idx])
        top = rows[:max(1, len(rows) // 5)]
        top_trades = [r[6] for r in top if r[6] is not None]
        scorecard.append({"name": name, "n": len(rows),
                          "rank": _rank_corr([r[idx] for r in rows], [r[5] for r in rows]),
                          "rank_profit": _rank_corr([r[idx] for r in rows if r[6] is not None],
                                                    [r[6] for r in rows if r[6] is not None]),
                          "top_hit": _share([r[5] for r in top]), "n_top": len(top),
                          "top_profit": _avg(top_trades), "top_win": (sum(t > 0 for t in top_trades) / len(top_trades)) if top_trades else None,
                          "avg_guess": _avg([r[idx] for r in rows]), "avg_real": _avg([r[5] for r in rows])})
    # calibration: what you'd really have earned per trade when the model said X (blind replay only);
    # profit models guess the trade result itself, spike models the best gain
    profit_model = bool(run) and _is_profit(run["model_kind"])
    buckets = ([(-9, -0.10, "below −10%"), (-0.10, 0.0, "−10 to 0%"), (0.0, 0.05, "0 to +5%"), (0.05, 0.10, "+5 to +10%"),
                (0.10, 0.20, "+10 to +20%"), (0.20, 9, "+20% or more")] if profit_model else
               [(-9, 0.0, "below 0%"), (0.0, 0.1, "0 to +10%"), (0.1, 0.2, "+10 to +20%"), (0.2, 0.3, "+20 to +30%"),
                (0.3, 0.5, "+30 to +50%"), (0.5, 9, "+50% or more")])
    calib = []
    for lo, hi, label in buckets:
        sel = [r for r in graded if r[2] is not None and lo <= r[2] < hi]
        ts = [r[6] for r in sel if r[6] is not None]
        calib.append({"label": label, "n": len(sel), "avg_real": _avg([r[5] for r in sel]), "hit": _share([r[5] for r in sel]),
                      "avg_trade": _avg(ts), "win": (sum(t > 0 for t in ts) / len(ts)) if ts else None})
    scatter = [[round(r[2], 3), round(max(min(r[6] if profit_model else r[5], 3.0), -1.0), 3)]
               for r in graded if r[2] is not None and (r[6] is not None or not profit_model)]
    return {"scope": scope, "base_hit": _share(real), "base_trade": _avg(trades), "n_graded": len(graded),
            "scorecard": scorecard, "calibration": calib, "scatter": scatter, "profit_model": profit_model,
            "bar": run["ping_threshold"] if run else HIT, "recent": [dict(r) for r in recent]}
