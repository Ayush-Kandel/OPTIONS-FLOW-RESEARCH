"""Strategy library: ways to trade a whale signal, every one replayed on REAL IBKR prices.

Each strategy buys the whale's contract at the ask right after the print (the print minute's closing
ask - what a follower really pays) and differs in how it gets out. Results are after $0.65 per contract
each way. Nothing is priced by a formula: a strategy whose prices weren't recorded (no next-day bars,
no expiry-day close) simply has no result for that pick.

The nightly model learns, per pick, which strategy to use - or to skip it - and the AI judges get the
same playbook plus how each strategy did on similar past trades (earlier days only).
"""

from datetime import datetime, timezone

FEE_PER_CONTRACT = 0.65

STRATEGIES = {
    "standard": {"name": "Standard", "target": 0.30, "stop": -0.50,
                 "about": "Sell when the bid is up +30% or down -50%, otherwise at the 4 PM bid."},
    "scalp": {"name": "Quick scalp", "target": 0.15, "stop": -0.15, "max_min": 30,
              "about": "Take a quick +15%, cut at -15%, and get out within 30 minutes either way."},
    "runner": {"name": "Runner", "target": 1.00, "stop": -0.40,
               "about": "Let a winner run: sell at +100% or -40%, otherwise at the 4 PM bid."},
    "trailing": {"name": "Trailing stop", "arm": 0.20, "trail": 0.15, "stop": -0.40,
                 "about": "Once up +20%, sell if the bid falls 15% from its best; -40% stop; otherwise 4 PM."},
    "hour": {"name": "One-hour hold", "stop": -0.50, "max_min": 60,
             "about": "Sell 60 minutes after buying (-50% stop) - the flow's first push, no afternoon decay."},
    "by2pm": {"name": "Out by 2 PM", "target": 0.30, "stop": -0.50, "out_by": "14:00",
              "about": "+30% / -50%, but always out by 2 PM, before the afternoon time decay."},
    "overnight": {"name": "Hold overnight", "stop": -0.50, "overnight": True,
                  "about": "-50% stop, otherwise hold through the close and sell at the next morning's 10 AM bid."},
    "expiry": {"name": "Hold to expiry", "expiry": True,
               "about": "No stop: hold until the expiry day's closing bid (what the whale may really be playing for)."},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_results (
    pick_id      INTEGER NOT NULL,
    strategy     TEXT NOT NULL,
    ret          REAL NOT NULL,         -- after fees, from the ask right after the print
    exit         TEXT NOT NULL,         -- target / stop / trail / time / close / next_day / expiry
    exit_minute  TEXT,                  -- when the trade closed (the money is free again)
    computed_utc TEXT NOT NULL,
    PRIMARY KEY (pick_id, strategy)
)"""


def ensure_schema(db):
    have = {r[1] for r in db.execute("PRAGMA table_info(strategy_results)")}
    if have and "exit_minute" not in have:   # derived data, recomputed every night: just rebuild it
        db.execute("DROP TABLE strategy_results")
    db.execute(SCHEMA)
    db.commit()


def _mins(m):
    return int(m[11:13]) * 60 + int(m[14:16])


def intraday(entry, start, bars, target=None, stop=None, max_min=None, out_by=None, arm=None, trail=None):
    """Walk the print day's bid bars (minute, high, low, close) after the entry minute. Inside a minute
    the stop is checked first (the cautious assumption), then the trailing stop against the best bid so
    far, then the target; time exits sell at that minute's closing bid. Else the day's last bid."""
    peak, last, last_m = None, None, None
    for m, hi, lo, cl in bars:
        if m <= start:
            continue
        last, last_m = cl, m
        if out_by and m[11:16] >= out_by:
            return cl / entry - 1, "time", m
        if stop is not None and lo <= entry * (1 + stop):
            return stop, "stop", m
        if peak is not None and lo <= peak * (1 - trail):
            return peak * (1 - trail) / entry - 1, "trail", m
        if target is not None and hi >= entry * (1 + target):
            return target, "target", m
        if arm is not None and (peak is not None or hi >= entry * (1 + arm)):
            peak = max(peak or hi, hi)
        if max_min is not None and _mins(m) - _mins(start) >= max_min:
            return cl / entry - 1, "time", m
    return (None, None, None) if last is None else (last / entry - 1, "close", last_m)


def _overnight(entry, start, bars, next_bars, stop):
    """Stop on the print day, then the next trading day's bars until the 10 AM bid."""
    for m, hi, lo, cl in bars:
        if m > start and lo <= entry * (1 + stop):
            return stop, "stop", m
    for m, hi, lo, cl in next_bars:
        if lo <= entry * (1 + stop):
            return stop, "stop", m
        if m[11:16] >= "10:00":
            return cl / entry - 1, "next_day", m
    return None, None, None


def results_for(db, pick):
    """{strategy: (ret after fees, exit)} for one pick, from recorded IBKR bars only."""
    pid, tk, k, pc, exp, td, t = pick
    start = f"{td} {t[:5]}"
    ask = db.execute("SELECT close FROM ib_bars WHERE pick_id = ? AND kind = 'ASK' AND minute_et = ? AND close > 0",
                     (pid, start)).fetchone()
    bars = db.execute("SELECT minute_et, high, low, close FROM ib_bars WHERE pick_id = ? AND kind = 'BID' "
                      "AND minute_et >= ? AND high > 0 ORDER BY minute_et", (pid, start)).fetchall()
    if not ask or not bars:
        return {}
    entry = ask[0]
    fees = 2 * FEE_PER_CONTRACT / (entry * 100)
    out = {}
    for key, s in STRATEGIES.items():
        if s.get("overnight"):
            if exp <= td:
                continue          # a 0DTE contract can't be held overnight
            next_day = db.execute(
                "SELECT MIN(substr(minute_et, 1, 10)) FROM ib_contract_bars WHERE ticker = ? AND strike = ? "
                "AND put_call = ? AND expiration = ? AND kind = 'BID' AND substr(minute_et, 1, 10) > ?",
                (tk, k, pc, exp, td)).fetchone()[0]
            if not next_day:
                continue
            nb = db.execute("SELECT minute_et, high, low, close FROM ib_contract_bars WHERE ticker = ? AND strike = ? "
                            "AND put_call = ? AND expiration = ? AND kind = 'BID' AND minute_et LIKE ? AND high > 0 "
                            "ORDER BY minute_et", (tk, k, pc, exp, next_day + "%")).fetchall()
            ret, why, when = _overnight(entry, start, bars, nb, s["stop"])
        elif s.get("expiry"):
            if exp <= td:
                ret, why, when = bars[-1][3] / entry - 1, "expiry", bars[-1][0]
            else:
                row = db.execute("SELECT bid_close FROM ib_exp_stats WHERE pick_id = ? AND status = 'ok' "
                                 "AND bid_close IS NOT NULL", (pid,)).fetchone()
                if not row:
                    continue
                ret, why, when = row[0] / entry - 1, "expiry", f"{exp} 16:00"
        else:
            ret, why, when = intraday(entry, start, bars, target=s.get("target"), stop=s.get("stop"),
                                max_min=s.get("max_min"), out_by=s.get("out_by"), arm=s.get("arm"),
                                trail=s.get("trail"))
        if ret is not None:
            out[key] = (ret - fees, why, when)
    return out


def compute_all(db):
    """Nightly: every IBKR-graded pick replayed under every strategy."""
    ensure_schema(db)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_exp_stats'").fetchone():
        return 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute("DELETE FROM strategy_results")
    n = 0
    for pick in db.execute("SELECT id, ticker, strike, put_call, expiration, trade_date, trade_time_et FROM picks "
                           "WHERE true_source = 'ibkr'").fetchall():
        for key, (ret, why, when) in results_for(db, pick).items():
            db.execute("INSERT INTO strategy_results (pick_id, strategy, ret, exit, exit_minute, computed_utc) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (pick[0], key, ret, why, when, now))
            n += 1
    db.commit()
    return n


def applicable(dte):
    """Strategies a pick can use, known at decision time (0DTE can't be held overnight)."""
    return [k for k, s in STRATEGIES.items() if not (s.get("overnight") and (dte or 0) <= 0)]


def results(db):
    """{pick_id: {strategy: (ret, exit date)}} for training and grading. The exit date matters: a result
    is only KNOWN once its trade has closed (holding to expiry can take days), so training for a given
    day may only use results that closed before it."""
    ensure_schema(db)
    out = {}
    for pid, key, ret, when in db.execute("SELECT pick_id, strategy, ret, exit_minute FROM strategy_results"):
        out.setdefault(pid, {})[key] = (ret, (when or "")[:10])
    return out


def playbook():
    """The strategy list as the AI judges see it."""
    return "\n".join(f"- {k}: {s['name']} - {s['about']}" for k, s in STRATEGIES.items())


def memory(db, trade_date, ticker=None, dte=None, min_n=5):
    """How each strategy did on picks from EARLIER days (never the same day): all picks, this ticker,
    and picks with similar days to expiry. Each line: average result per trade and how often it won."""
    ensure_schema(db)
    groups = [("all earlier picks", "", ())]
    if ticker:
        groups.append((f"{ticker} picks", "AND p.ticker = ?", (ticker,)))
    if dte is not None:
        groups.append(("same-day expiries (0DTE)" if dte <= 0 else "1-14 days to expiry",
                       "AND p.dte_days <= 0" if dte <= 0 else "AND p.dte_days > 0", ()))
    lines = []
    for label, where, args in groups:
        # only trades that had CLOSED before the judged day (a hold-to-expiry result can arrive days later)
        rows = db.execute(f"SELECT r.strategy, COUNT(*), AVG(r.ret), AVG(r.ret > 0) FROM strategy_results r "
                          f"JOIN picks p ON p.id = r.pick_id WHERE substr(r.exit_minute, 1, 10) < ? {where} "
                          f"GROUP BY r.strategy", (trade_date, *args)).fetchall()
        rows = [r for r in rows if r[1] >= min_n]
        if rows:
            rows.sort(key=lambda r: -r[2])
            lines.append(f"  {label}: " + "; ".join(f"{k} {avg:+.0%} (won {won:.0%}, n={n})" for k, n, avg, won in rows))
    return ("STRATEGY RESULTS on earlier picks (buy at the ask right after the whale, after fees):\n"
            + "\n".join(lines) + "\n") if lines else ""
