"""The research bots. All run on the processor (CPU) from flow.db - no credits, no cloud.

  💰 Simulator - backtests picks as trades: buy at the fill, exit at the close / at +30% (else the
                close) / at expiry. For every pick, your 12 tickers, the model's replay picks
                (predicted using only earlier days) and the real live pings.
  🔬 Analyst   - what predicts a good trade: each input's rank agreement with the day's best gain,
                plus breakdowns by ticker, days to expiry, call/put, flags and time of day.
  ⚖️ Judges    - how accurate Hermes and Qwen are, overall and recently, and their bias.
  🛰️ Scout     - hourly capture summary while the market is open.

Each report is saved in `bot_reports` (the dashboard reads it) and posted to the bot's Discord
channel only when its underlying data changed, so channels aren't spammed with repeats.
"""

import json
from datetime import datetime, timedelta, timezone

import pipeline as pl

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_reports (
    id          INTEGER PRIMARY KEY,
    bot         TEXT NOT NULL,
    run_utc     TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    summary     TEXT NOT NULL,
    data_json   TEXT NOT NULL,
    posted      INTEGER NOT NULL DEFAULT 0
);
"""
TARGET = pl.PING_MIN_GAIN


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(db):
    return json.dumps(db.execute(
        "SELECT (SELECT COUNT(*) FROM picks WHERE day_stats_status = 'ok'), "
        "(SELECT COUNT(*) FROM picks WHERE exp_stats_status = 'ok'), "
        "(SELECT COUNT(*) FROM picks WHERE pinged_utc IS NOT NULL), "
        "(SELECT COUNT(*) FROM picks WHERE hermes_expected_gain IS NOT NULL AND qwen_expected_gain IS NOT NULL), "
        "(SELECT MAX(version) FROM replay_preds), "
        "(SELECT COUNT(*) FROM sqlite_master WHERE name = 'ib_stats')").fetchone()
        + (db.execute("SELECT COUNT(*) FROM ib_stats WHERE status = 'ok'").fetchone()
           if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_stats'").fetchone() else ()))


def _pct(v):
    return "n/a" if v is None else f"{v:+.0%}"


# ---------------------------------------------------------------- simulator --

def simulator(db):
    import numpy as np
    bundle = pl._load_model() or {}
    bar = bundle.get("ping_threshold", TARGET)
    select = ("SELECT p.id, p.trade_date, p.ticker, p.max_gain_pct, p.close_pct, p.exp_close_pct, "
              "p.exp_stats_status, p.fill_price, p.max_after_pct, p.trade_time_et, p.pinged_utc, r.pred, "
              "p.score, p.premium FROM picks p LEFT JOIN replay_preds r ON r.pick_id = p.id ")
    rows = db.execute(select + "WHERE p.day_stats_status = 'ok' ORDER BY p.trade_date, p.trade_time_et").fetchall()
    # the IBKR replay uses every IBKR-priced pick, with or without Trade Echo day prices
    ib_rows = (db.execute(select + "WHERE p.id IN (SELECT pick_id FROM ib_stats WHERE status = 'ok') "
                          "ORDER BY p.trade_date, p.trade_time_et").fetchall()
               if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_stats'").fetchone() else [])
    groups = {
        "every pick (incl. training extras)": lambda r: True,
        "core picks (noteworthy $350K+)": lambda r: r[12] is not None and (r[13] or 0) >= pl.NOTEWORTHY_MIN_PREMIUM,
        "your tickers (core)": lambda r: r[2] in pl.MY_TICKERS and r[12] is not None
                                            and (r[13] or 0) >= pl.NOTEWORTHY_MIN_PREMIUM,
        f"model picks (replay, bar +{bar:.0%})": lambda r: r[11] is not None and r[11] >= bar,
        "live pings": lambda r: r[10] is not None,
    }
    exits = {
        "close": lambda r: r[4],
        f"+{TARGET:.0%} target else close": lambda r: TARGET if r[3] >= TARGET else r[4],
        "hold to expiry": lambda r: r[5] if r[6] == "ok" else None,
    }
    data = {"groups": {}, "curves": {}, "bar": bar}
    lines = ["💰 **Simulator - backtest: buy each pick at its fill**",
             "_Equal size per trade. % = return per trade. This first part uses Trade Echo's daily "
             "prices, which include moves from BEFORE the print and overstate results - trust the "
             "IBKR 'realistic replay' and 'exit plan' sections below._"]
    for gname, gfilter in groups.items():
        picked = [r for r in rows if gfilter(r)]
        if not picked:
            data["groups"][gname] = None
            continue
        data["groups"][gname] = {}
        lines.append(f"\n**{gname}** ({len(picked)} trades)")
        for ename, exit_fn in exits.items():
            rets = np.array([v for v in (exit_fn(r) for r in picked) if v is not None], dtype=float)
            if not len(rets):
                continue
            dollars = np.array([exit_fn(r) * r[7] * 100 for r in picked if exit_fn(r) is not None])
            stats = {"n": int(len(rets)), "win_rate": float((rets > 0).mean()), "avg": float(rets.mean()),
                     "median": float(np.median(rets)), "best": float(rets.max()), "worst": float(rets.min()),
                     "total": float(rets.sum()), "avg_dollars_1_contract": float(dollars.mean())}
            data["groups"][gname][ename] = stats
            lines.append(f"  {ename}: n={stats['n']}, won {stats['win_rate']:.0%}, avg {_pct(stats['avg'])}, "
                         f"median {_pct(stats['median'])}, best {_pct(stats['best'])}, worst "
                         f"{_pct(stats['worst'])}, avg ${stats['avg_dollars_1_contract']:+,.0f} per 1 contract")
        if not gname.startswith("live pings"):
            curve, total = [], 0.0
            for r in picked:
                total += r[4]
                curve.append([r[1], round(total, 4)])
            data["curves"][gname] = curve
    _simulate_ibkr(db, groups, ib_rows, data, lines)
    return "\n".join(lines), data


def _ibkr_paths(db):
    """pick id -> (entry price, BID bars after the entry minute, in time order) for IBKR-priced
    picks. Entry = your real ask right after the ping when it was recorded (OPRA), else the
    whale's fill at the print minute."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_stats'").fetchone():
        return {}
    has_entries = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_entries'").fetchone()
    paths = {}
    for pid, fill, tdate, hhmm, ask, entry_min in db.execute(
            "SELECT p.id, p.fill_price, p.trade_date, p.trade_time_et, "
            + ("e.ask, e.minute_et FROM ib_stats s JOIN picks p ON p.id = s.pick_id "
               "LEFT JOIN ib_entries e ON e.pick_id = p.id AND e.status = 'ok' "
               if has_entries else "NULL, NULL FROM ib_stats s JOIN picks p ON p.id = s.pick_id ")
            + "WHERE s.status = 'ok' AND s.n_bid_bars > 0"):
        use_ask = ask and getattr(__import__("ibkr"), "ENTRY_MODE", "whale") == "ask"
        price, start = (ask, entry_min) if use_ask else (fill, f"{tdate} {hhmm}")
        bars = db.execute("SELECT high, low, close FROM ib_bars WHERE pick_id = ? AND kind = 'BID' "
                          "AND minute_et > ? AND high > 0 ORDER BY minute_et", (pid, start)).fetchall()
        if bars:
            paths[pid] = (price, bars)
    return paths


EXIT_TARGETS = [0.20, 0.30, 0.50, 1.00, None]      # None = no target, sell at the 4 PM bid
EXIT_STOPS = [-0.25, -0.50, None]                   # None = no stop
EXIT_MIN_TRADES = 20                                # fewer trades than this: not trustworthy


def exit_grid(paths, ids):
    """Every target x stop combination replayed minute by minute on the real bid; sorted by
    average return per trade."""
    import numpy as np
    out = []
    for t in EXIT_TARGETS:
        for s in EXIT_STOPS:
            rets = np.array([_ibkr_exit(*paths[i], target=t, stop=s) for i in ids if i in paths], dtype=float)
            if len(rets):
                out.append({"target": t, "stop": s, "n": int(len(rets)), "avg": float(rets.mean()),
                            "median": float(np.median(rets)), "win_rate": float((rets > 0).mean()),
                            "worst": float(rets.min())})
    return sorted(out, key=lambda r: -r["avg"])


def _exit_name(r):
    t = f"+{r['target']:.0%} target" if r["target"] is not None else "no target"
    s = f"{r['stop']:.0%} stop" if r["stop"] is not None else "no stop"
    return f"{t} / {s}"


def _ibkr_exit(fill, bars, target=None, stop=None):
    """Walk the bid minute by minute: sell at the target or stop when the BID touches it (stop is
    checked first inside a minute - the cautious assumption), else sell at the last bid of the day."""
    for high, low, _ in bars:
        if stop is not None and low <= fill * (1 + stop):
            return stop
        if target is not None and high >= fill * (1 + target):
            return target
    return bars[-1][2] / fill - 1


def _simulate_ibkr(db, groups, rows, data, lines):
    """Same groups, replayed on IBKR's 1-minute BID prices after the print (what you could sell at)."""
    import numpy as np
    paths = _ibkr_paths(db)
    if not paths:
        return
    cols = {r[1] for r in db.execute("PRAGMA table_info(picks)")}
    exp_close = (dict(db.execute("SELECT id, true_exp_close_pct FROM picks WHERE true_exp_close_pct IS NOT NULL"))
                 if "true_exp_close_pct" in cols else {})
    exits = {
        "sell at the 4 PM bid": dict(),
        f"+{TARGET:.0%} target (bid) else 4 PM bid": dict(target=TARGET),
        f"+{TARGET:.0%} target / -{TARGET:.0%} stop (bid)": dict(target=TARGET, stop=-TARGET),
    }
    data["ibkr"] = {}
    n_entry = (db.execute("SELECT COUNT(*) FROM ib_entries WHERE status = 'ok'").fetchone()[0]
               if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ib_entries'").fetchone() else 0)
    lines.append(f"\n📈 **Realistic replay - IBKR 1-minute bid prices after the print** ({len(paths)} picks so far; "
                 f"{n_entry} from your real entry ask, the rest from the whale's fill)")
    for gname, gfilter in groups.items():
        picked = [r for r in rows if gfilter(r) and r[0] in paths]
        if not picked:
            continue
        data["ibkr"][gname] = {}
        lines.append(f"**{gname}** ({len(picked)} trades)")
        held = [exp_close[r[0]] for r in picked if exp_close.get(r[0]) is not None]
        if held:
            held = np.array(held, dtype=float)
            data["ibkr"][gname]["hold to expiry (closing bid)"] = {
                "n": int(len(held)), "avg": float(held.mean()), "win_rate": float((held > 0).mean())}
            lines.append(f"  hold to expiry, sell at the expiry-day closing bid: {len(held)} trades, "
                         f"won {(held > 0).mean():.0%}, avg {_pct(float(held.mean()))}")
        for ename, kw in exits.items():
            rets = np.array([_ibkr_exit(*paths[r[0]], **kw) for r in picked], dtype=float)
            stats = {"n": int(len(rets)), "win_rate": float((rets > 0).mean()), "avg": float(rets.mean()),
                     "median": float(np.median(rets)), "worst": float(rets.min()), "best": float(rets.max())}
            data["ibkr"][gname][ename] = stats
            lines.append(f"  {ename}: won {stats['win_rate']:.0%}, avg {_pct(stats['avg'])}, "
                         f"median {_pct(stats['median'])}, worst {_pct(stats['worst'])}, best {_pct(stats['best'])}")

    # Exit plan: which target/stop would have worked best, per group
    data["exit_plans"] = {}
    lines.append(f"\n🎯 **Exit plan test** - every target x stop on real minute-by-minute bids "
                 f"(fewer than {EXIT_MIN_TRADES} trades = not trustworthy yet)")
    for gname, gfilter in groups.items():
        ids = [r[0] for r in rows if gfilter(r) and r[0] in paths]
        grid = exit_grid(paths, ids)
        if not grid:
            continue
        data["exit_plans"][gname] = grid
        best = grid[0]
        hold = next((g for g in grid if g["target"] is None and g["stop"] is None), None)
        warn = " ⚠️ too few trades" if best["n"] < EXIT_MIN_TRADES else ""
        lines.append(f"**{gname}** ({best['n']} trades){warn}: best = {_exit_name(best)}: avg "
                     f"{_pct(best['avg'])}, won {best['win_rate']:.0%}, worst {_pct(best['worst'])}"
                     + (f"; vs sell at 4 PM: avg {_pct(hold['avg'])}" if hold else ""))


# ------------------------------------------------------------------ analyst --

def analyst(db):
    import numpy as np
    import pandas as pd
    rows = db.execute("SELECT id, ticker, put_call, dte_days, trade_time_et, flags, true_gain_pct, "
                      "true_close_pct FROM picks WHERE true_gain_pct IS NOT NULL").fetchall()
    if len(rows) < 10:
        return "🔬 **Analyst** - not enough picks with honest after-print prices yet.", {}
    df = pd.DataFrame(rows, columns=["id", "ticker", "pc", "dte", "time", "flag_list", "best", "close"])
    feats = pl._frame(db, list(df.id))
    corr = {}
    for col in pl.FEATURES:
        s = feats[col]
        if s.notna().sum() >= 10 and s.nunique() > 1:
            corr[col] = (float(s.corr(df.best.reset_index(drop=True), method="spearman")), int(s.notna().sum()))
    ranked = sorted(corr.items(), key=lambda kv: -abs(kv[1][0]))
    data = {"n": len(df), "feature_corr": {k: v for k, v in ranked}}
    lines = [f"🔬 **Analyst - what predicts the best sellable gain after the print** ({len(df)} picks)",
             "_Rank agreement: +1 = higher value -> bigger gains, -1 = the opposite, 0 = no link. "
             "Small samples - treat |value| < 0.2 as noise._", "**Strongest inputs:**"]
    lines += [f"  {name}: {c:+.2f} (n={n})" for name, (c, n) in ranked[:8]]

    def table(title, key):
        g = df.groupby(key).agg(n=("best", "size"), avg_best=("best", "mean"),
                                hit=("best", lambda s: float((s >= TARGET).mean())), avg_close=("close", "mean"))
        g = g[g.n >= 3].sort_values("avg_best", ascending=False)
        data[title] = {str(k): {c: float(v) for c, v in row.items()} for k, row in g.iterrows()}
        lines.append(f"**{title}:**")
        lines.extend(f"  {k}: n={int(r.n)}, avg best {_pct(r.avg_best)}, {r.hit:.0%} reached +{TARGET:.0%}, "
                     f"avg close {_pct(r.avg_close)}" for k, r in g.head(8).iterrows())

    df["dte_bucket"] = pd.cut(df.dte, [-1, 1, 5, 14], labels=["0-1 DTE", "2-5 DTE", "6-14 DTE"])
    df["session"] = np.select([df.time <= "10:30", df.time >= "14:30"], ["open (to 10:30)", "late (14:30+)"],
                              "midday")
    df["sweep"] = df["flag_list"].fillna("").str.contains("sweep_or_block").map(
        {True: "sweep/block", False: "other"})
    table("By ticker", "ticker")
    table("By days to expiry", "dte_bucket")
    table("Calls vs puts", "pc")
    table("By time of day", "session")
    table("Sweep/block flag", "sweep")
    return "\n".join(lines), data


# ------------------------------------------------------------------- judges --

def judges_report(db):
    import pandas as pd
    lines = ["⚖️ **Judges - how accurate are Hermes and Qwen?**",
             "_Rank agreement between each judge's estimate and the honest best gain after the print "
             "(best bid; 0 = random)._"]
    data = {}
    days = [r[0] for r in db.execute("SELECT DISTINCT trade_date FROM picks WHERE true_gain_pct IS NOT NULL "
                                     "ORDER BY trade_date DESC LIMIT 3")]
    for name in pl.JUDGES:
        df = pd.DataFrame(db.execute(
            f"SELECT trade_date, {name}_expected_gain, true_gain_pct, {name}_verdict FROM picks "
            f"WHERE true_gain_pct IS NOT NULL AND {name}_expected_gain IS NOT NULL").fetchall(),
            columns=["d", "est", "act", "verdict"])
        if len(df) < 5:
            continue
        recent = df[df.d.isin(days)]
        d = {"n": len(df), "rank_all": float(df.est.corr(df.act, method="spearman")),
             "rank_recent": float(recent.est.corr(recent.act, method="spearman")) if len(recent) > 4 else None,
             "avg_est": float(df.est.mean()), "avg_act": float(df.act.mean()),
             "take_hit": float((df[df.verdict == "take"].act >= TARGET).mean()) if (df.verdict == "take").any() else None,
             "skip_hit": float((df[df.verdict == "skip"].act >= TARGET).mean()) if (df.verdict == "skip").any() else None}
        data[name] = d
        recent_txt = f"{d['rank_recent']:+.2f}" if d["rank_recent"] is not None else "n/a"
        lines.append(f"**{name.capitalize()}** (n={d['n']}): rank agreement {d['rank_all']:+.2f} overall, "
                     f"{recent_txt} on the last 3 days; estimates average {_pct(d['avg_est'])} vs real "
                     f"{_pct(d['avg_act'])}; 'take' calls reached +{TARGET:.0%} "
                     f"{'n/a' if d['take_hit'] is None else f'{d['take_hit']:.0%}'}, 'skip' calls "
                     f"{'n/a' if d['skip_hit'] is None else f'{d['skip_hit']:.0%}'}")
    return "\n".join(lines), data


# -------------------------------------------------------------------- scout --

def scout_hour(db, hours=1):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    calls = dict(db.execute("SELECT kind, COUNT(*) FROM polls WHERE started_utc >= ? GROUP BY kind", (since,)))
    credits = db.execute("SELECT COALESCE(SUM(credits_charged), 0) FROM polls WHERE started_utc >= ?",
                         (since,)).fetchone()[0]
    prints = db.execute("SELECT COUNT(*) FROM prints WHERE first_seen_utc >= ?", (since,)).fetchone()[0]
    mine, total = db.execute(
        f"SELECT SUM(ticker IN ({','.join('?' * len(pl.MY_TICKERS))})), COUNT(*) FROM picks "
        "WHERE first_seen_utc >= ?", (*sorted(pl.MY_TICKERS), since)).fetchone()
    errors = db.execute("SELECT COUNT(*) FROM polls WHERE started_utc >= ? AND error IS NOT NULL", (since,)).fetchone()[0]
    gaps = db.execute("SELECT GROUP_CONCAT(ticker) FROM polls WHERE started_utc >= ? AND flag = 'possible_gap'",
                      (since,)).fetchone()[0]
    pings = db.execute("SELECT COUNT(*) FROM picks WHERE pinged_utc >= ?", (since,)).fetchone()[0]
    used = f"{credits}/150 credits" if hours == 1 else f"{credits} credits"
    label = "last hour" if hours == 1 else f"last {hours} hours"
    return (f"🛰️ **Scout - {label}:** {used}, calls {calls}, {prints} new flow prints, "
            f"{mine or 0} new picks on your tickers ({total} total), {pings} ping(s), {errors} errors"
            + (f", possible gaps: {gaps}" if gaps else ""))


# ------------------------------------------------------------------ runner --

def run_backtests(db, force_post=False):
    """Run Simulator, Analyst and Judges; save every run; post a bot's report only if its data
    changed since its last post (or force_post, e.g. after the nightly run)."""
    db.executescript(SCHEMA)
    fp = _fingerprint(db)
    for bot, fn in (("simulator", simulator), ("analyst", analyst), ("judges", judges_report)):
        try:
            text, data = fn(db)
        except Exception as e:  # one broken bot must not stop the others
            print(f"  {bot} failed: {type(e).__name__}: {e}")
            continue
        last = db.execute("SELECT fingerprint FROM bot_reports WHERE bot = ? AND posted = 1 "
                          "ORDER BY id DESC LIMIT 1", (bot,)).fetchone()
        post = force_post or last is None or last[0] != fp
        if post:
            post = pl.discord_send(text, bot=bot)
        db.execute("INSERT INTO bot_reports (bot, run_utc, fingerprint, summary, data_json, posted) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (bot, _utc(), fp, text, json.dumps(data, default=str), int(post)))
        db.commit()
        print(f"  {bot}: {'posted' if post else 'saved (no new data)'}")
