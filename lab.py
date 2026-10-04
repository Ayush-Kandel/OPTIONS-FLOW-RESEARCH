"""Strategy Lab: builds its own ways to trade whale signals and backtests them nonstop - honestly.

A lab strategy = WHICH whales (1-3 conditions: call/put, days to expiry, time of day, size, whale bought
or sold, IV, trend, model/judge opinions, ...) x HOW to get in (right away, after a delay, only at the
whale's price or better, never chasing) x HOW to get out (take-profit x stop-loss x time limit,
trailing stops, overnight). Every combination is replayed on real IBKR minute bars (bid to sell, ask to
buy, after $0.65/contract each way); a strategy with no recorded prices for a pick has no trade.

Searching hundreds of thousands of strategies on a few weeks of data WILL find "profitable" ones by
luck, so the lab spends its computing power on not fooling itself:
  1. Blind test of the SEARCH itself (walk-forward): to trade day D it searches using only days before
     D, then trades its best strategy on D. Only those blind trades count.
  2. Luck test (permutation): the whole procedure is rerun on data whose outcomes were shuffled among
     each day's picks. If shuffled data does as well, the result is luck. p-value = share of shuffled
     runs that did at least as well.
  3. It only calls something profitable when the blind trades make money AND beat the luck test.
Runs as a background daemon at idle priority (only spare CPU), redoing everything when new graded data
arrives and adding luck tests in between. Writes lab_runs / lab_null / lab_trades in flow.db.
"""

import json
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import pipeline  # noqa: E402  (the condition vocabulary includes its MARKET_INPUTS)

DB = Path(os.environ.get("FLOW_DB") or Path(__file__).with_name("flow.db"))
FEE = 0.65                      # per contract, each way
OPEN_MIN = 9 * 60 + 30
N_MIN = 405                     # 09:30 .. 16:14
MIN_TRADES = 20                 # a strategy needs this many trades in its training days to count
MIN_DAYS = 3                    # ... spread over at least this many days
Z = 1.0                         # objective = average trade - Z x standard error (rewards consistency)
N_TRIPLES = 120_000             # random 3-condition filters tried per search (plus all singles and pairs)
CHUNK = 12_000
MIN_TRAIN_DAYS = 3              # blind-test days start once this many earlier days exist
SEED = 7

# ------------------------------------------------------------------ the parts strategies are built from

ENTRIES = [
    {"key": "now", "name": "buy at the ask right after the whale", "delay": 0},
    {"key": "d2", "name": "buy at the ask 2 minutes after the whale", "delay": 2},
    {"key": "d5", "name": "buy at the ask 5 minutes after the whale", "delay": 5},
    {"key": "d15", "name": "buy at the ask 15 minutes after the whale", "delay": 15},
    {"key": "limit", "name": "limit order at the whale's price (filled if the ask drops to it within 30 min)", "limit": 30},
    {"key": "nochase", "name": "buy right away, but only if the ask is within 10% of the whale's price", "delay": 0, "max_chase": 0.10},
]
TPS = [0.10, 0.15, 0.20, 0.30, 0.50, 0.75, 1.00, None]
SLS = [-0.10, -0.15, -0.25, -0.35, -0.50, None]
TIMES = [15, 30, 60, 120, "13:00", "14:00", "15:00", None]      # minutes after entry, a clock time, or the close
TRAILS = [(0.10, 0.10), (0.20, 0.10), (0.20, 0.20), (0.30, 0.15), (0.50, 0.20), (0.10, 0.05)]
OVERNIGHT_SLS = [-0.25, -0.50, None]


def _exit_list():
    out = [{"tp": tp, "sl": sl, "t": t} for tp in TPS for sl in SLS for t in TIMES]
    out += [{"trail": tr, "sl": -0.50} for tr in TRAILS]
    out += [{"overnight": True, "sl": sl} for sl in OVERNIGHT_SLS]
    return out


EXITS = _exit_list()


def describe_exit(x):
    pct = lambda v: f"{v:+.0%}"
    if x.get("overnight"):
        return "hold overnight, sell at the next morning's 10 AM bid" + (f" (stop {pct(x['sl'])})" if x["sl"] else "")
    if x.get("trail"):
        arm, tr = x["trail"]
        return f"once up {pct(arm)}, sell if the bid falls {tr:.0%} from its best; stop {pct(x['sl'])}; else 4 PM"
    parts = []
    if x["tp"] is not None:
        parts.append(f"take profit at {pct(x['tp'])}")
    if x["sl"] is not None:
        parts.append(f"stop at {pct(x['sl'])}")
    t = x["t"]
    parts.append("otherwise sell at the 4 PM bid" if t is None else
                 f"otherwise sell at {t}" if isinstance(t, str) else f"otherwise sell after {t} minutes")
    return ", ".join(parts)


# ------------------------------------------------------------------------------------------ data

def _minute_index(m):
    """'YYYY-MM-DD HH:MM' or 'HH:MM' -> index from 09:30."""
    hhmm = m[-5:]
    return int(hhmm[:2]) * 60 + int(hhmm[3:5]) - OPEN_MIN


def load(db):
    """Every IBKR-graded pick: its conditions' raw values and its real bid/ask paths."""
    sys.path.insert(0, str(Path(__file__).parent))
    import pipeline
    import trade_context
    run = db.execute("SELECT version, model_kind FROM model_runs WHERE status = 'trained' ORDER BY id DESC LIMIT 1").fetchone()
    replay = dict(db.execute("SELECT pick_id, pred FROM replay_preds WHERE version = ? AND kind = ?", run).fetchall()) if run else {}
    picks = db.execute(
        "SELECT p.id, p.trade_date, p.trade_time_et, p.ticker, p.put_call, p.dte_days, p.fill_price, p.premium, p.score, "
        "p.source, p.flags, p.hermes_verdict, p.qwen_verdict, p.strike, p.expiration FROM picks p "
        "WHERE p.true_source = 'ibkr' ORDER BY p.trade_date, p.trade_time_et").fetchall()
    rows = []
    for pid, td, t, tk, pc, dte, fill, prem, score, source, flags, hv, qv, k, exp in picks:
        i0 = _minute_index(t)
        if not (0 <= i0 < N_MIN) or not fill:
            continue
        bid = np.full((3, N_MIN), np.nan, dtype=np.float32)
        for m, hi, lo, cl in db.execute("SELECT minute_et, high, low, close FROM ib_bars WHERE pick_id = ? AND kind = 'BID' "
                                        "AND high > 0", (pid,)):
            j = _minute_index(m)
            if m[:10] == td and 0 <= j < N_MIN:
                bid[:, j] = (hi, lo, cl)
        ask = np.full(N_MIN, np.nan, dtype=np.float32)
        for m, cl in db.execute("SELECT minute_et, close FROM ib_bars WHERE pick_id = ? AND kind = 'ASK' AND close > 0", (pid,)):
            j = _minute_index(m)
            if m[:10] == td and 0 <= j < N_MIN:
                ask[j] = cl
        if np.isnan(ask[i0]) or np.isnan(bid[2]).all():
            continue
        nxt = None
        if exp > td:
            nd = db.execute("SELECT MIN(substr(minute_et, 1, 10)) FROM ib_contract_bars WHERE ticker = ? AND strike = ? AND "
                            "put_call = ? AND expiration = ? AND kind = 'BID' AND substr(minute_et, 1, 10) > ?",
                            (tk, k, pc, exp, td)).fetchone()[0]
            if nd:
                nb = db.execute("SELECT minute_et, low, close FROM ib_contract_bars WHERE ticker = ? AND strike = ? AND "
                                "put_call = ? AND expiration = ? AND kind = 'BID' AND minute_et LIKE ? AND high > 0 "
                                "ORDER BY minute_et", (tk, k, pc, exp, nd + "%")).fetchall()
                before = [r for r in nb if r[0][11:16] < "10:00"]
                at10 = next((r for r in nb if r[0][11:16] >= "10:00"), None)
                if at10:
                    nxt = (min([r[1] for r in before] + [at10[1]]), at10[2])
        f, _ = pipeline.features_for(db, pid)
        ctx = trade_context.get(db, pid)
        side = "unknown"
        if ctx and ctx["status"] in ("ok", "side_only"):
            side = "sold" if (ctx.get("our_leg") == "sold" or (not ctx.get("structure") and ctx["side"] == "sell")) else ctx["side"]
        fl = json.loads(flags or "[]")
        rows.append({
            "id": pid, "day": td, "i0": i0, "fill": fill, "bid": bid, "ask": ask, "next": nxt,
            "ticker": tk, "pc": pc, "dte": dte or 0, "mins": i0, "premium": prem, "score": score,
            "source": "noteworthy" if score is not None else source, "side": side,
            "multileg": bool(ctx and ctx["status"] == "ok" and ctx.get("structure")),
            "sweep": "sweep_or_block" in fl, "aggressive": "aggressive_execution" in fl,
            "hermes_take": hv == "take", "qwen_take": qv == "take", "model": replay.get(pid),
            "is_index": tk in pipeline.INDEX_TICKERS, "mine": tk in pipeline.MY_TICKERS,
            **{k2: f.get(k2) for k2 in NUMERIC if k2 not in ("model",)},
        })
    return rows


NUMERIC = ["log_premium", "log_fill", "otm_pct", "iv", "abs_delta", "theta_pct", "stock_with_30m", "stock_with_day",
           "spy_with_30m", "repeat_30m", "flow_call_share_30m", "score", "iv_vs_stock_iv", "stock_iv_rank_1y",
           "hermes_est", "qwen_est", "model", *pipeline.MARKET_INPUTS]
NUMERIC_WORDS = {
    "log_premium": "trade size ($, log)", "log_fill": "option price (log)", "otm_pct": "% out of the money", "iv": "implied volatility",
    "abs_delta": "delta", "theta_pct": "time decay per day", "stock_with_30m": "stock move its way (30 min)",
    "stock_with_day": "stock move its way (day)", "spy_with_30m": "market move its way (30 min)", "repeat_30m": "repeat buys (30 min)",
    "flow_call_share_30m": "call share of recent flow", "score": "Trade Echo score", "iv_vs_stock_iv": "option IV vs stock IV",
    "stock_iv_rank_1y": "IV rank (1 yr)", "hermes_est": "Hermes' guess", "qwen_est": "Qwen's guess", "model": "model's blind guess",
    "rvol_at_print": "stock volume vs normal", "vol_pace_5m": "volume picking up (5 min)",
    "vwap_dist_with": "price vs VWAP its way", "rsi_with": "RSI its way", "range_pos_with": "spot in day's range its way",
    "flow_lean_30m_with": "other whales agree (30 min)"}


def conditions(rows):
    """The vocabulary filters are built from: (name, boolean array over picks)."""
    out = []
    add = lambda name, arr: out.append((name, np.array(arr, dtype=bool)))
    add("calls", [r["pc"] == "CALL" for r in rows])
    add("puts", [r["pc"] == "PUT" for r in rows])
    for lo, hi, name in ((0, 0, "0DTE"), (1, 2, "1-2 days to expiry"), (3, 7, "3-7 days to expiry"), (8, 14, "8-14 days to expiry"),
                         (0, 2, "0-2 days to expiry"), (1, 14, "1-14 days to expiry")):
        add(name, [lo <= r["dte"] <= hi for r in rows])
    for lo, hi, name in ((0, 30, "9:30-10:00"), (30, 90, "10:00-11:00"), (90, 210, "11:00-13:00"), (210, 330, "13:00-15:00"),
                         (330, 405, "15:00-close"), (0, 90, "before 11:00"), (90, 405, "after 11:00")):
        add(name, [lo <= r["mins"] < hi for r in rows])
    for s in ("buy", "mid", "sold", "unknown"):
        add(f"whale side: {s}", [r["side"] == s for r in rows])
    add("whale didn't sell", [r["side"] != "sold" for r in rows])
    add("one leg of a spread", [r["multileg"] for r in rows])
    for s in ("noteworthy", "algo", "whale"):
        add(f"source: {s}", [r["source"] == s for r in rows])
    for k, name in (("sweep", "sweep or block"), ("aggressive", "paid at the ask (flag)"), ("hermes_take", "Hermes says take"),
                    ("qwen_take", "Qwen says take"), ("is_index", "index ETF"), ("mine", "your 12 tickers")):
        add(name, [r[k] for r in rows])
    add("both judges say take", [r["hermes_take"] and r["qwen_take"] for r in rows])
    tickers = {}
    for r in rows:
        tickers[r["ticker"]] = tickers.get(r["ticker"], 0) + 1
    for tk, n in tickers.items():
        if n >= 25:
            add(f"ticker {tk}", [r["ticker"] == tk for r in rows])
    for k in NUMERIC:
        vals = np.array([np.nan if r.get(k) is None else r[k] for r in rows], dtype=float)
        ok = ~np.isnan(vals)
        if ok.sum() < 40 or len(set(vals[ok])) < 4:
            continue
        for q in (25, 50, 75):
            cut = float(np.nanpercentile(vals, q))
            add(f"{NUMERIC_WORDS[k]} >= {cut:.3g}", ok & (vals >= cut))
            add(f"{NUMERIC_WORDS[k]} <= {cut:.3g}", ok & (vals <= cut))
    names = [o[0] for o in out]
    C = np.stack([o[1] for o in out], axis=1) if out else np.zeros((len(rows), 0), bool)
    keep = C.sum(axis=0) >= MIN_TRADES
    return [n for n, k in zip(names, keep) if k], C[:, keep]


# ------------------------------------------------------------------------------- the replay engine

def _ffill(a):
    """Carry the last known bid forward over minutes without a quote."""
    missing = np.isnan(a)
    idx = np.where(~missing, np.arange(len(a)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = a[idx]
    out[np.cumsum(~missing) == 0] = np.nan      # nothing known yet
    return out


def _first(cond):
    """Index of the first True along the last axis, or a large number."""
    any_ = cond.any(axis=-1)
    return np.where(any_, cond.argmax(axis=-1), 10_000)


def _entry(r, e):
    """(entry index, entry price) or None."""
    i0, ask = r["i0"], r["ask"]
    if "limit" in e:
        win = ask[i0:min(N_MIN, i0 + e["limit"] + 1)]
        hit = np.where(~np.isnan(win) & (win <= r["fill"]))[0]
        return (i0 + int(hit[0]), float(win[hit[0]])) if len(hit) else None
    j = i0 + e["delay"]
    for jj in range(j, min(N_MIN, j + 4)):          # the first quote at or just after the planned minute
        if not np.isnan(ask[jj]):
            if e.get("max_chase") is not None and ask[jj] > r["fill"] * (1 + e["max_chase"]):
                return None
            return jj, float(ask[jj])
    return None


def replay_pick(r):
    """Return (after fees) for every entry x exit; NaN where the trade can't happen."""
    out = np.full((len(ENTRIES), len(EXITS)), np.nan, dtype=np.float32)
    for ei, e in enumerate(ENTRIES):
        got = _entry(r, e)
        if not got:
            continue
        j, price = got
        hi, lo, cl = r["bid"][0, j + 1:], r["bid"][1, j + 1:], r["bid"][2, j + 1:]
        if len(cl) == 0 or np.isnan(cl).all():
            continue
        valid = ~np.isnan(cl)
        last = int(np.where(valid)[0][-1])
        clf = _ffill(cl)
        hi_ = np.where(np.isnan(hi), -np.inf, hi)
        lo_ = np.where(np.isnan(lo), np.inf, lo)
        fee = 2 * FEE / (price * 100)
        tp_vals = np.array([np.inf if tp is None else tp for tp in TPS])
        sl_vals = np.array([-np.inf if sl is None else sl for sl in SLS])
        tp_first = _first(hi_[None, :] >= price * (1 + tp_vals[:, None]))
        sl_first = _first(lo_[None, :] <= price * (1 + sl_vals[:, None]))
        t_idx = []
        for t in TIMES:
            if t is None:
                t_idx.append(last)
            elif isinstance(t, str):
                k = _minute_index(t) - (j + 1)
                t_idx.append(min(k, last) if k >= 0 else -1)    # -1: bought after that time, no trade
            else:
                t_idx.append(min(t - 1, last))
        t_idx = np.array(t_idx)
        xi = 0
        for a, tp in enumerate(TPS):
            for b, sl in enumerate(SLS):
                for c, t in enumerate(TIMES):
                    s_i, t_i, lim = sl_first[b], tp_first[a], t_idx[c]
                    if lim < 0:
                        xi += 1
                        continue
                    if s_i <= t_i and s_i <= lim:
                        ret = sl
                    elif t_i <= lim:
                        ret = tp
                    else:
                        v = clf[lim]
                        ret = (v / price - 1) if not np.isnan(v) else np.nan
                    out[ei, xi] = ret - fee if ret is not None and not np.isnan(ret) else np.nan
                    xi += 1
        for arm, tr in TRAILS:                     # trailing stops (stop -50%)
            peak, ret = None, None
            for m in range(len(cl)):
                if np.isnan(cl[m]):
                    continue
                if lo[m] <= price * 0.5:
                    ret = -0.5
                    break
                if peak is not None and lo[m] <= peak * (1 - tr):
                    ret = peak * (1 - tr) / price - 1
                    break
                if peak is not None or hi[m] >= price * (1 + arm):
                    peak = max(peak or hi[m], hi[m])
            if ret is None:
                ret = cl[last] / price - 1
            out[ei, xi] = ret - fee
            xi += 1
        for sl in OVERNIGHT_SLS:                    # hold overnight
            if r["next"] is None:
                xi += 1
                continue
            day_low = np.nanmin(lo) if not np.isnan(lo).all() else np.inf
            next_low, at10 = r["next"]
            if sl is not None and min(day_low, next_low) <= price * (1 + sl):
                ret = sl
            else:
                ret = at10 / price - 1
            out[ei, xi] = ret - fee
            xi += 1
    return out.reshape(-1)


def replay_all(rows):
    """The full outcome table R: picks x (entry, exit) columns. Uses every core."""
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as ex:
        return np.stack(list(ex.map(replay_pick, rows, chunksize=16))).astype(np.float32)


def column_name(col):
    e, x = divmod(col, len(EXITS))
    return ENTRIES[e]["name"], describe_exit(EXITS[x])


# --------------------------------------------------------------------------------------- the search

def _combos(n_cond, rng):
    singles = [(i,) for i in range(n_cond)]
    pairs = [(i, j) for i in range(n_cond) for j in range(i + 1, n_cond)]
    triples = set()
    if n_cond >= 3:
        while len(triples) < min(N_TRIPLES, n_cond * (n_cond - 1) * (n_cond - 2) // 6):
            triples.add(tuple(sorted(rng.choice(n_cond, 3, replace=False).tolist())))
    return singles + pairs + sorted(triples)


RERANK = 400                     # stage-2 candidates re-scored day by day


def search(R, C, days, train, combos, top=1):
    """Best strategies (filter combo x entry/exit column) on the training picks only, in two stages:
    1) every combination scored by average trade - Z x standard error (enough trades over enough days);
    2) the best few hundred re-scored DAY BY DAY - the average of their daily results minus Z x the
       spread between days - so a strategy has to work on most days, not just one lucky big day."""
    stage1 = _search_stage1(R, C, days, train, combos, top=RERANK)
    rescored = []
    for s1, combo, col, mean, n in stage1:
        sel = train & C[:, list(combo)].all(axis=1) & ~np.isnan(R[:, col])
        daily = [float(R[sel & (days == d), col].mean()) for d in np.unique(days[sel])]
        if len(daily) < MIN_DAYS:
            continue
        score = float(np.mean(daily) - Z * np.std(daily, ddof=1) / math.sqrt(len(daily)))
        rescored.append((score, combo, col, mean, n))
    rescored.sort(key=lambda b: -b[0])
    return rescored[:top]


def _search_stage1(R, C, days, train, combos, top=1):
    """Stage 1: average trade - Z x standard error for every combination, fully vectorized."""
    A = (~np.isnan(R)) & train[:, None]
    Rf = np.where(A, R, 0).astype(np.float32)
    Af = A.astype(np.float32)
    Q = Rf * Rf
    day_ids = np.unique(days[train])
    D = np.stack([(days == d) & train for d in day_ids], axis=1).astype(np.float32)
    best = []
    for s in range(0, len(combos), CHUNK):
        chunk = combos[s:s + CHUNK]
        M = np.ones((len(chunk), len(days)), dtype=bool)
        for k in range(3):
            idx = np.array([c[k] if len(c) > k else -1 for c in chunk])
            has = idx >= 0
            M[has] &= C[:, idx[has]].T
        M = M.astype(np.float32)
        n = M @ Af
        S = M @ Rf
        SS = M @ Q
        ndays = ((M @ D) > 0).sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            mean = S / n
            var = (SS - S * mean) / np.maximum(n - 1, 1)
            score = mean - Z * np.sqrt(np.maximum(var, 0) / n)
        score[(n < MIN_TRADES) | ~np.isfinite(score)] = -np.inf
        score[ndays < MIN_DAYS] = -np.inf
        k = min(top, score.size)
        flat = np.argpartition(score.ravel(), -k)[-k:]
        for f in flat:
            ci, col = divmod(int(f), score.shape[1])
            if np.isfinite(score.ravel()[f]):
                best.append((float(score.ravel()[f]), chunk[ci], col, float(mean.ravel()[f]), int(n.ravel()[f])))
    best.sort(key=lambda b: -b[0])
    return best[:top]


def walk_forward(R, C, days, combos):
    """The blind test of the whole procedure: for each day D (after a few training days), search on
    days before D, then trade the winner on D. Returns the blind trades."""
    trades = []
    uniq = sorted(set(days))
    for d in uniq[MIN_TRAIN_DAYS:]:
        train = days < d
        best = search(R, C, days, train, combos, top=1)
        if not best:
            continue
        _, combo, col, _, _ = best[0]
        test = (days == d) & C[:, list(combo)].all(axis=1) & ~np.isnan(R[:, col])
        for i in np.where(test)[0]:
            trades.append((d, int(i), combo, col, float(R[i, col])))
    return trades


def _summary(trades):
    rets = np.array([t[4] for t in trades], dtype=float)
    if not len(rets):
        return {"n": 0, "avg": None, "total": 0.0, "win": None, "days": 0}
    return {"n": int(len(rets)), "avg": float(rets.mean()), "total": float(rets.sum()), "win": float((rets > 0).mean()),
            "days": len({t[0] for t in trades})}


def shuffled(R, days, rng):
    """Outcomes shuffled among each day's picks: same days, same market, but no link to the conditions."""
    out = R.copy()
    for d in set(days):
        idx = np.where(days == d)[0]
        out[idx] = R[rng.permutation(idx)]
    return out


# ------------------------------------------------------------------------------------- storage + loop

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_runs (
    id INTEGER PRIMARY KEY, started_utc TEXT, finished_utc TEXT, data_key TEXT, n_picks INTEGER, n_days INTEGER,
    n_conditions INTEGER, n_columns INTEGER, n_strategies INTEGER,
    blind_n INTEGER, blind_avg REAL, blind_total REAL, blind_win REAL, blind_days INTEGER,
    best_json TEXT, top_json TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS lab_trades (run_id INTEGER, day TEXT, pick_id INTEGER, strategy TEXT, ret REAL);
CREATE TABLE IF NOT EXISTS lab_null (run_id INTEGER, at_utc TEXT, blind_avg REAL, blind_total REAL, blind_n INTEGER);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _data_key(db):
    """Changes whenever graded data, the strategy replays, the model's blind guesses or the inputs the
    conditions are built from change."""
    has_sr = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'strategy_results'").fetchone()
    return "|".join(str(x) for x in db.execute(
        "SELECT COUNT(*), MAX(id), "
        + ("(SELECT MAX(computed_utc) FROM strategy_results)" if has_sr else "NULL")
        + ", (SELECT MAX(version) FROM replay_preds) FROM picks WHERE true_source = 'ibkr'").fetchone()) \
        + f"|inputs{len(NUMERIC)}"


def strategy_text(names, combo, col):
    entry, exit_ = column_name(col)
    return {"filters": [names[i] for i in combo], "entry": entry, "exit": exit_, "column": int(col), "combo": list(combo)}


def status_of(blind, p, n_null):
    if not blind["n"]:
        return "searching"
    if blind["avg"] > 0 and blind["n"] >= 30 and p is not None and p < 0.05 and n_null >= 100:
        return "validated"
    if blind["avg"] > 0 and p is not None and p < 0.20:
        return "promising"
    return "not profitable yet"


def run_once(log=print):
    """A full lab run on the current data; returns the run id."""
    con = sqlite3.connect(str(DB), timeout=60)
    con.executescript(SCHEMA)
    ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    t0 = time.time()
    key = _data_key(ro)
    rows = load(ro)
    names, C = conditions(rows)
    days = np.array([r["day"] for r in rows])
    R = replay_all(rows)
    rng = np.random.default_rng(SEED)
    combos = _combos(C.shape[1], rng)
    log(f"lab: {len(rows)} picks over {len(set(days))} days, {C.shape[1]} conditions x {R.shape[1]} entry/exit columns "
        f"= {len(combos) * R.shape[1]:,} strategies per search ({time.time() - t0:.0f}s to load)")
    blind_trades = walk_forward(R, C, days, combos)
    blind = _summary(blind_trades)
    best_all = search(R, C, days, np.ones(len(days), bool), combos, top=15)
    top = [{**strategy_text(names, b[1], b[2]), "score": b[0], "avg_in_sample": b[3], "n_in_sample": b[4]} for b in best_all]
    cur = con.execute(
        "INSERT INTO lab_runs (started_utc, data_key, n_picks, n_days, n_conditions, n_columns, n_strategies, blind_n, "
        "blind_avg, blind_total, blind_win, blind_days, best_json, top_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_now(), key, len(rows), len(set(days)), C.shape[1], R.shape[1], len(combos) * R.shape[1], blind["n"], blind["avg"],
         blind["total"], blind["win"], blind["days"], json.dumps(top[0] if top else None), json.dumps(top), "searching"))
    run_id = cur.lastrowid
    con.executemany("INSERT INTO lab_trades VALUES (?, ?, ?, ?, ?)",
                    [(run_id, d, rows[i]["id"], json.dumps(strategy_text(names, combo, col)), ret)
                     for d, i, combo, col, ret in blind_trades])
    con.execute("UPDATE lab_runs SET finished_utc = ?, status = ? WHERE id = ?",
                (_now(), status_of(blind, None, 0), run_id))
    con.commit()
    avg = "n/a" if blind["avg"] is None else f"{blind['avg']:+.1%}"
    log(f"lab run {run_id}: blind test {blind['n']} trades over {blind['days']} days, avg {avg} per trade "
        f"({time.time() - t0:.0f}s)")
    return run_id, (R, C, days, combos, rows, names)


def luck_test(run_id, state, n=1, log=print):
    """n more shuffled reruns of the whole blind-test procedure for this run."""
    R, C, days, combos, rows, names = state
    con = sqlite3.connect(str(DB), timeout=60)
    rng = np.random.default_rng()
    for _ in range(n):
        s = _summary(walk_forward(shuffled(R, days, rng), C, days, combos))
        con.execute("INSERT INTO lab_null VALUES (?, ?, ?, ?, ?)", (run_id, _now(), s["avg"], s["total"], s["n"]))
        con.commit()
    real = con.execute("SELECT blind_n, blind_avg, blind_total, blind_win, blind_days FROM lab_runs WHERE id = ?", (run_id,)).fetchone()
    nulls = [r[0] for r in con.execute("SELECT blind_avg FROM lab_null WHERE run_id = ? AND blind_avg IS NOT NULL", (run_id,))]
    blind = {"n": real[0], "avg": real[1], "total": real[2], "win": real[3], "days": real[4]}
    p = (1 + sum(v >= real[1] for v in nulls)) / (1 + len(nulls)) if real[1] is not None and nulls else None
    status = status_of(blind, p, len(nulls))
    con.execute("UPDATE lab_runs SET status = ? WHERE id = ?", (status, run_id))
    con.commit()
    return p, len(nulls), status


def _idle_priority():
    """Only use CPU nothing else wants (the logger, IBKR tracker and judges always come first)."""
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p          # a 64-bit handle: don't let ctypes cut it to 32 bits
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x40)        # IDLE_PRIORITY_CLASS (workers inherit it)


def daemon():
    """Run forever: a full run whenever graded data changes, luck tests in between."""
    _idle_priority()
    run_id, state, key, last_status = None, None, None, None
    while True:
        try:
            ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
            new_key = _data_key(ro)
            ro.close()
            if new_key != key:
                run_id, state = run_once()
                key = new_key
            p, n_null, status = luck_test(run_id, state, n=1)
            if n_null % 10 == 0:
                print(f"{datetime.now():%H:%M:%S} lab run {run_id}: {n_null} luck tests, p = {p}, status = {status}", flush=True)
            if status != last_status and status in ("promising", "validated"):
                _announce(run_id, status, p, n_null)
            last_status = status
        except Exception as e:                       # never die: log, wait, retry
            print(f"{datetime.now():%H:%M:%S} lab error: {type(e).__name__}: {e}", flush=True)
            time.sleep(60)


def _announce(run_id, status, p, n_null):
    try:
        import pipeline
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        n, avg, best = con.execute("SELECT blind_n, blind_avg, best_json FROM lab_runs WHERE id = ?", (run_id,)).fetchone()
        b = json.loads(best or "null") or {}
        pipeline.discord_send(
            f"🧪 **Strategy Lab: {status.upper()}** - the search's blind trades averaged {avg:+.1%} over {n} trades "
            f"(luck test p = {p:.3f} after {n_null} shuffled reruns).\nCurrent best: {' + '.join(b.get('filters', []))} -> "
            f"{b.get('entry')}; {b.get('exit')}. Paper only - see FlowDesk's Lab tab.", bot="arena")
    except Exception:
        pass


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        daemon()
    else:
        rid, st = run_once()
        print(luck_test(rid, st, n=int(sys.argv[sys.argv.index("--luck") + 1]) if "--luck" in sys.argv else 3))
