"""Data-integrity checker for the flow logger pipeline.

Rule-based checks (exact, no AI guessing) over flow.db, the model file and the logger itself.
Each check returns OK / WARN / FAIL with a plain-English message. Problems are posted to
Discord (each distinct problem at most once per day); everything is saved in `integrity_checks`.

Run by:
  - the "Trade Echo integrity check" scheduled task every 30 min on weekdays (catches a dead logger)
  - the logger after each nightly run (included in the scorecard)
  - by hand: py flow_logger.py --check
"""

import json
import random
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pipeline as pl

OK, WARN, FAIL = "OK", "WARN", "FAIL"

SCHEMA = """
CREATE TABLE IF NOT EXISTS integrity_checks (
    id        INTEGER PRIMARY KEY,
    run_utc   TEXT NOT NULL,
    check_name TEXT NOT NULL,
    severity  TEXT NOT NULL,
    message   TEXT NOT NULL,
    alerted   INTEGER NOT NULL DEFAULT 0
);
"""


def _q(db, sql, args=()):
    return db.execute(sql, args).fetchall()


def _one(db, sql, args=()):
    return db.execute(sql, args).fetchone()[0]


def _count(msg):
    """The problem count in a check message: its first number, or 0 if it has none."""
    m = re.search(r"\d+", msg or "")
    return int(m.group()) if m else 0


def run_checks(db, now_et, in_session, after_close_expected):
    """Return a list of (check_name, severity, message)."""
    results = []

    def add(name, severity, message):
        results.append((name, severity, message))

    # 1. Database file itself
    status = _one(db, "PRAGMA quick_check")
    add("database", OK if status == "ok" else FAIL,
        "database file is healthy" if status == "ok" else f"database corruption check failed: {status}")

    # 2. Logger heartbeat (only meaningful while the market is open)
    last = _one(db, "SELECT MAX(started_utc) FROM polls")
    if in_session:
        age_min = ((datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 60
                   if last else 1e9)
        if age_min > 15:
            add("logger_alive", FAIL, f"market is open but the logger hasn't polled for {age_min:.0f} min "
                "- it may be stopped or the PC asleep")
        elif age_min > 6:
            add("logger_alive", WARN, f"no poll for {age_min:.0f} min (normally every 1-2 min)")
        else:
            add("logger_alive", OK, f"last poll {age_min:.1f} min ago")

    # 2b. IBKR live tracker heartbeat (streams save a quote row about once a minute)
    if in_session and now_et.strftime("%H:%M") >= "09:45":
        try:
            live = _one(db, "SELECT MAX(minute_et) FROM ib_live_quotes")
        except sqlite3.OperationalError:
            live = None
        age_min = ((now_et.replace(tzinfo=None) - datetime.fromisoformat(live)).total_seconds() / 60
                   if live else 1e9)
        if age_min > 10:
            add("ibkr_live_alive", FAIL, f"market is open but IBKR live tracking hasn't saved a quote for "
                f"{min(age_min, 999):.0f} min - check TWS is logged in and the 'IBKR live Greeks' task is running")
        else:
            add("ibkr_live_alive", OK, f"IBKR live quotes {age_min:.1f} min old")

    # 3. Credits, refusals, errors in the last 24 h
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    # Trade Echo caps 100/hr per connection and 150/hr per user; the whale watch is connection 2
    over = _q(db, "SELECT substr(started_utc, 1, 13), SUM(CASE WHEN COALESCE(conn, 1) = 1 THEN credits_charged "
                  "ELSE 0 END) AS main, SUM(credits_charged) AS total FROM polls WHERE started_utc >= ? "
                  "GROUP BY 1 HAVING main > 100 OR total > 150", (since,))
    add("credits", FAIL if over else OK,
        f"hours over the credit caps (hour, main connection, total): {over}" if over
        else "every hour stayed within 100 credits per connection and 150 in total")
    refusals = _one(db, "SELECT COUNT(*) FROM rate_limit_events WHERE at_utc >= ?", (since,))
    add("rate_limits", WARN if refusals else OK,
        f"Trade Echo refused {refusals} call(s) in 24 h" if refusals else "no refused calls in 24 h")
    total, errs = _q(db, "SELECT COUNT(*), SUM(error IS NOT NULL) FROM polls WHERE started_utc >= ?", (since,))[0]
    rate = (errs or 0) / total if total else 0
    add("poll_errors", FAIL if rate > 0.10 else WARN if errs else OK,
        f"{errs or 0} of {total} calls failed in 24 h ({rate:.0%})")
    gaps = _one(db, "SELECT COUNT(*) FROM polls WHERE flag = 'possible_gap' AND started_utc >= ?", (since,))
    add("flow_gaps", WARN if gaps >= 3 else OK,
        f"{gaps} possible gaps in raw flow in 24 h" + (" - some tickers need faster polling" if gaps >= 3 else ""))

    # 4. Picks: required fields and internal consistency
    bad_fields = _one(db, "SELECT COUNT(*) FROM picks WHERE strike IS NULL OR fill_price IS NULL OR "
                          "fill_price <= 0 OR expiration IS NULL OR put_call NOT IN ('CALL', 'PUT') OR "
                          "trade_time_et IS NULL")
    add("pick_fields", FAIL if bad_fields else OK,
        f"{bad_fields} picks missing strike/fill/expiry/type" if bad_fields else "all picks have strike, fill, expiry, type")
    dte_bad = [r for r in _q(db, "SELECT id, trade_date, expiration, dte_days FROM picks WHERE dte_days IS NOT NULL")
               if abs((date.fromisoformat(r[2]) - date.fromisoformat(r[1])).days - r[3]) > 1]
    add("pick_dte", WARN if dte_bad else OK,
        f"{len(dte_bad)} picks where days-to-expiry doesn't match the dates" if dte_bad else "days-to-expiry matches dates")
    # Trade Echo's own numbers sometimes disagree (multi-price fills shown at one rounded price).
    # Only NEW cases are a warning; known ones are listed for information.
    mismatch = "size > 0 AND premium > 0 AND ABS(fill_price * size * 100 - premium) / premium > 0.10"
    prem_all = _one(db, f"SELECT COUNT(*) FROM picks WHERE {mismatch}")
    prem_new = _one(db, f"SELECT COUNT(*) FROM picks WHERE {mismatch} AND first_seen_utc >= ?", (since,))
    add("source_premium", WARN if prem_new else OK,
        f"{prem_new} new pick(s) where Trade Echo's fill x size x 100 is >10% off its own premium "
        "(source data, kept as reported)" if prem_new
        else f"no new source mismatches ({prem_all} known Trade Echo fill/premium mismatches, kept as reported)")
    # The stored fields must match Trade Echo's own contract text and summary line exactly
    text_bad = []
    for pid, contract, strike, pc, exp, line, size, fill in _q(
            db, "SELECT id, contract, strike, put_call, expiration, line, size, fill_price FROM picks"):
        m = re.match(r"\s*([\d.]+)\s*([CP])\s+(\d{4}-\d{2}-\d{2})", contract or "")
        if not m or abs(float(m.group(1)) - (strike or -1)) > 1e-9 or m.group(2) != (pc or " ")[0] \
                or m.group(3) != exp:
            text_bad.append(pid)
            continue
        lm = re.search(r"-\s*([\d,]+)\s*@\s*([\d.]+)", line or "")
        if lm and (int(lm.group(1).replace(",", "")) != size or abs(float(lm.group(2)) - fill) > 0.005):
            text_bad.append(pid)
    add("pick_text_match", FAIL if text_bad else OK,
        f"{len(text_bad)} picks whose stored strike/type/expiry/size/fill differ from Trade Echo's text "
        f"(ids {text_bad[:10]})" if text_bad else "stored fields match Trade Echo's contract text and line exactly")
    dupes = _one(db, "SELECT COUNT(*) FROM (SELECT 1 FROM picks GROUP BY trade_date, ticker, contract, "
                     "trade_time_et, premium, fill_price HAVING COUNT(*) > 1)")
    add("pick_duplicates", FAIL if dupes else OK, f"{dupes} duplicated picks" if dupes else "no duplicate picks")
    rule_bad = _one(db, "SELECT COUNT(*) FROM picks WHERE dte_days > ? OR score <= ?",
                    (pl.NOTEWORTHY_MAX_DTE, pl.NOTEWORTHY_MIN_SCORE))
    add("pick_rules", FAIL if rule_bad else OK,
        f"{rule_bad} picks break the score > {pl.NOTEWORTHY_MIN_SCORE} / 0-{pl.NOTEWORTHY_MAX_DTE} DTE rules"
        if rule_bad else "every pick meets the score and DTE rules")

    # 4b. Provenance: every record must trace back to a real Trade Echo response (no made-up data)
    allowed = ("live", "sweep", "backfill", "history", "harvest", "algo")
    bad_source = _one(db, f"SELECT COUNT(*) FROM picks WHERE source NOT IN ({','.join('?' * len(allowed))}) "
                          "OR source IS NULL", allowed)
    raw_bad = 0
    for raw, tk, contract, fill, source, strike, pc, exp in _q(
            db, "SELECT raw_json, ticker, contract, fill_price, source, strike, put_call, expiration FROM picks"):
        try:
            r = json.loads(raw or "")
            if source == "algo":  # Algo Edge alert: check its own fields
                raw_bad += not (str(r.get("ticker", "")).upper() == tk
                                and abs(float(r.get("strikePrice")) - strike) < 1e-9
                                and str(r.get("callOrPut", "")).upper() == pc
                                and str(r.get("expiration", ""))[:10] == exp
                                and abs(float(r.get("fillPrice")) - fill) < 1e-9)
            else:
                raw_bad += not (str(r.get("ticker", "")).upper() == tk and r.get("contract") == contract
                                and abs(float(r.get("fillPrice")) - fill) < 1e-9)
        except (ValueError, TypeError):
            raw_bad += 1
    add("provenance_picks", FAIL if (bad_source or raw_bad) else OK,
        f"{bad_source} picks with an unknown source and {raw_bad} that don't match their stored Trade Echo "
        "response - possible made-up or altered data" if (bad_source or raw_bad)
        else "every pick matches the Trade Echo response it came from")
    orphan_prints = _one(db, "SELECT COUNT(*) FROM prints p LEFT JOIN polls q ON q.id = p.poll_id "
                             "WHERE q.id IS NULL OR q.error IS NOT NULL")
    unbacked_prices = _one(db, "SELECT COUNT(*) FROM picks p WHERE day_stats_status = 'ok' AND NOT EXISTS ("
                               "SELECT 1 FROM polls q WHERE q.kind = 'market_data' AND q.ticker = p.ticker "
                               "AND q.error IS NULL AND substr(q.started_utc, 1, 10) >= p.trade_date)")
    add("provenance_data", FAIL if (orphan_prints or unbacked_prices) else OK,
        f"{orphan_prints} flow prints and {unbacked_prices} price records with no real Trade Echo call "
        "behind them" if (orphan_prints or unbacked_prices)
        else "every flow print and price record traces back to a real Trade Echo call")

    # 5. Price stats: bars must make sense and percentages must match the prices
    bars_bad = _one(db, "SELECT COUNT(*) FROM picks WHERE day_stats_status = 'ok' AND (day_high < day_low OR "
                        "day_close > day_high + 1e-9 OR day_close < day_low - 1e-9 OR "
                        "(day_vwap IS NOT NULL AND (day_vwap > day_high + 0.01 OR day_vwap < day_low - 0.01)))")
    add("price_bars", FAIL if bars_bad else OK,
        f"{bars_bad} picks with impossible prices (e.g. close above high)" if bars_bad
        else "all day prices are consistent (low <= avg/close <= high)")
    pct_bad = _one(db, "SELECT COUNT(*) FROM picks WHERE day_stats_status = 'ok' AND "
                       "ABS((day_high / fill_price - 1) - max_gain_pct) > 1e-6")
    add("price_pcts", FAIL if pct_bad else OK,
        f"{pct_bad} picks whose best-gain % doesn't match their prices" if pct_bad else "gain % match the prices")
    status_bad = _one(db, "SELECT COUNT(*) FROM picks WHERE (day_stats_status = 'ok' AND day_high IS NULL) "
                          "OR (day_stats_status != 'ok' AND max_gain_pct IS NOT NULL)")
    add("price_status", FAIL if status_bad else OK,
        f"{status_bad} picks whose price status disagrees with their data" if status_bad else "price statuses consistent")
    if after_close_expected:
        window = (now_et.date() - timedelta(days=pl.PRICE_HISTORY_DAYS - 1)).isoformat()
        backlog = _one(db, "SELECT COUNT(*) FROM picks WHERE day_stats_status IS NULL AND trade_date < ? "
                           "AND trade_date >= ?", (now_et.date().isoformat(), window))
        add("price_backlog", WARN if backlog else OK,
            f"{backlog} picks from earlier days still have no prices - they may be lost after 7 days"
            if backlog else "no unpriced picks from earlier days")

    # 6. Greeks sanity
    greeks_bad = _one(db, "SELECT COUNT(*) FROM picks WHERE iv IS NOT NULL AND (iv < 0.005 OR iv > 8 OR "
                          "ABS(delta) > 1.0001 OR gamma < 0)")
    add("greeks", FAIL if greeks_bad else OK,
        f"{greeks_bad} picks with impossible Greeks" if greeks_bad else "Greeks within valid ranges")

    # 6b. IBKR 1-minute prices: every stat must be recomputable from its stored raw bars, and the
    # after-print high can't be above Trade Echo's whole-day high (beyond small feed differences)
    if _one(db, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'ib_stats'"):
        ib_bad = []
        for pid, hi, lo, close, n_t, tdate, hhmm in _q(
                db, "SELECT s.pick_id, s.after_high, s.after_low, s.day_close, s.n_trade_bars, p.trade_date, "
                    "p.trade_time_et FROM ib_stats s JOIN picks p ON p.id = s.pick_id WHERE s.status = 'ok'"):
            bars = _q(db, "SELECT minute_et, high, low, close FROM ib_bars WHERE pick_id = ? AND kind = 'TRADES' "
                          "ORDER BY minute_et", (pid,))
            after = [b for b in bars if b[0] > f"{tdate} {hhmm}"]
            if (len(bars) != n_t or not after or abs(max(b[1] for b in after) - hi) > 1e-9
                    or abs(min(b[2] for b in after) - lo) > 1e-9 or abs(bars[-1][3] - close) > 1e-9):
                ib_bad.append(pid)
        n_ok = _one(db, "SELECT COUNT(*) FROM ib_stats WHERE status = 'ok'")
        add("ibkr_bars", FAIL if ib_bad else OK,
            f"{len(ib_bad)} IBKR price records don't match their stored 1-minute bars (ids {ib_bad[:10]})"
            if ib_bad else f"all {n_ok} IBKR price records match their stored 1-minute bars")
        if _one(db, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'ib_greeks_path'"):
            path_bad = _one(db, "SELECT COUNT(*) FROM ib_greeks_path g LEFT JOIN ib_bars o ON o.pick_id = g.pick_id "
                                "AND o.kind = 'TRADES' AND o.minute_et = g.minute_et WHERE o.close IS NULL "
                                "OR ABS(o.close - g.opt_price) > 1e-9 OR g.iv < 0.005 OR g.iv > 8 "
                                "OR ABS(g.delta) > 1.0001 OR g.gamma < 0")
            n_path = _one(db, "SELECT COUNT(*) FROM ib_greeks_path")
            add("ibkr_greeks_path", FAIL if path_bad else OK,
                f"{path_bad} minute-by-minute Greeks points with no matching IBKR option trade or impossible values"
                if path_bad else f"all {n_path} minute-by-minute Greeks points trace to real IBKR trades")
        handed = _one(db, "SELECT COUNT(*) FROM picks p LEFT JOIN ib_stats s ON s.pick_id = p.id "
                          "WHERE p.day_stats_status = 'via_ibkr' AND p.trade_date < ? "
                          "AND COALESCE(s.status, '') NOT IN ('ok', 'no_trades', 'no_bars')",
                      (now_et.date().isoformat(),))
        add("ibkr_handoff", WARN if handed else OK,
            f"{handed} picks were left to IBKR for pricing but IBKR hasn't priced them yet - keep TWS "
            "logged in (they're lost once the contract expires)" if handed
            else "every pick handed to IBKR has been priced")
        cols = {r[1] for r in db.execute("PRAGMA table_info(picks)")}
        if "true_source" in cols:
            true_bad = _one(db, "SELECT COUNT(*) FROM picks p LEFT JOIN ib_stats s ON s.pick_id = p.id "
                                "WHERE p.true_source = 'ibkr' AND (s.status IS NOT 'ok' "
                                "OR ABS(p.true_gain_pct - s.bid_max_gain_pct) > 1e-9 "
                                "OR ABS(COALESCE(p.true_close_pct, 9) - COALESCE(s.bid_close_pct, 9)) > 1e-9)")
            te_bad = _one(db, "SELECT COUNT(*) FROM picks WHERE true_source = 'te_confirmed' "
                              "AND ABS(true_gain_pct - max_gain_pct) > 1e-9")
            n_true = dict(_q(db, "SELECT true_source, COUNT(*) FROM picks WHERE true_source IS NOT NULL GROUP BY 1"))
            add("true_outcomes", FAIL if (true_bad or te_bad) else OK,
                f"{true_bad + te_bad} honest training outcomes don't match their IBKR / Trade Echo source"
                if (true_bad or te_bad) else f"honest training outcomes match their sources {n_true}")
        disagree = _one(db, "SELECT COUNT(*) FROM ib_stats s JOIN picks p ON p.id = s.pick_id WHERE s.status = 'ok' "
                            "AND p.day_stats_status = 'ok' AND s.after_high > p.day_high * 1.05 + 0.05")
        add("ibkr_vs_tradeecho", WARN if disagree else OK,
            f"{disagree} picks where IBKR's after-print high is >5% above Trade Echo's whole-day high "
            "(the two feeds disagree)" if disagree else "IBKR and Trade Echo prices agree")

    # 7. Judges: coverage, errors, and the no-peeking rule
    for name in pl.JUDGES:
        missing = _one(db, f"SELECT COUNT(*) FROM picks WHERE {name}_expected_gain IS NULL AND trade_date < ?",
                       (now_et.date().isoformat(),))
        errors = _one(db, f"SELECT COUNT(*) FROM picks WHERE {name}_error IS NOT NULL")
        sev = WARN if (missing or errors) else OK
        add(f"judge_{name}", sev, f"{name}: {missing} earlier picks without an opinion, {errors} errors"
            if sev != OK else f"{name} has judged every earlier pick")
    leak = 0
    for pid, tdate, ticker, pc, dte in _q(db, "SELECT id, trade_date, ticker, put_call, dte_days FROM picks "
                                              "ORDER BY RANDOM() LIMIT 20"):
        memory = pl._hermes_memory(db, tdate, ticker, pc, dte)
        leak += sum(1 for line in memory.splitlines()
                    if line.startswith("- ") and line[2:12] >= tdate)
    add("no_peeking", FAIL if leak else OK,
        f"judges were shown {leak} same-day/future outcomes (look-ahead!)" if leak
        else "judges only see outcomes from earlier days (checked 20 random picks)")

    # 8. Model: exists, loads, matches the features, trained recently
    bundle = pl._load_model()
    if bundle is None:
        add("model", WARN, "no current model file - pings are off until the next nightly run")
    else:
        feat_ok = bundle.get("features") == pl.FEATURES
        add("model", OK if feat_ok else FAIL,
            f"model '{bundle['kind']}' ({bundle['version']}, {bundle['n']} picks) loads and matches the inputs"
            if feat_ok else "model was trained on a different input list - retrain needed")
    if after_close_expected:
        # last trading day whose nightly run should be finished by now
        last_day = now_et.date() if now_et.time() >= datetime.strptime("18:00", "%H:%M").time() \
            else now_et.date() - timedelta(days=1)
        while last_day.weekday() >= 5:
            last_day -= timedelta(days=1)
        done = _one(db, "SELECT COUNT(*) FROM daily_runs WHERE trade_date = ?", (last_day.isoformat(),))
        add("nightly_run", OK if done else WARN,
            f"nightly run for {last_day} finished" if done else f"no nightly run recorded for {last_day}")
    return results


def check_and_alert(db, now_et, in_session, after_close_expected, send=True):
    db.executescript(SCHEMA)
    results = run_checks(db, now_et, in_session, after_close_expected)
    run = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = now_et.date().isoformat()
    new_problems = []
    for name, sev, msg in results:
        # Alert once per check per day. The message text changes between runs (e.g. "4 of 632 calls"
        # -> "4 of 605"), so compare the problem count (first number), not the text: re-alert only
        # if the problem got bigger.
        sent = [m for (m,) in _q(db, "SELECT message FROM integrity_checks WHERE check_name = ? AND severity = ? "
                                     "AND alerted = 1 AND run_utc >= ?", (name, sev, today))]
        alert = sev != OK and (not sent or _count(msg) > max(_count(m) for m in sent))
        db.execute("INSERT INTO integrity_checks (run_utc, check_name, severity, message, alerted) "
                   "VALUES (?, ?, ?, ?, ?)", (run, name, sev, msg, int(alert)))
        if alert:
            new_problems.append((name, sev, msg))
    db.commit()
    if send and new_problems:
        icon = {WARN: "⚠️", FAIL: "🛑"}
        pl.discord_send("🔎 **Data integrity check**\n" + "\n".join(
            f"{icon[s]} {s} {n}: {m}" for n, s, m in new_problems), bot="auditor")
    return results


AUDIT_MODEL = "qwen3:14b"
AUDIT_FIELDS = ["id", "trade_date", "trade_time_et", "ticker", "contract", "strike", "put_call", "expiration",
                "dte_days", "size", "fill_price", "premium", "score", "line", "day_low", "day_vwap",
                "day_high", "day_close", "max_gain_pct", "close_pct", "exp_close", "exp_close_pct",
                "spot_at_print", "iv", "delta", "hermes_expected_gain", "qwen_expected_gain"]


def ai_audit(db, trade_date, sample_older=10, send=True):
    """Local AI audit with Qwen (on your GPU, no cloud): reviews the day's picks plus a random
    sample of older ones and lists anything that looks wrong. Every finding is verified to name
    a real pick and field before it's reported, so it can't invent rows."""
    db.executescript(SCHEMA)
    cols = ", ".join(AUDIT_FIELDS)
    rows = _q(db, f"SELECT {cols} FROM picks WHERE trade_date = ?", (trade_date,))
    older = _q(db, f"SELECT {cols} FROM picks WHERE trade_date < ?", (trade_date,))
    rows += random.sample(older, min(sample_older, len(older)))
    if not rows:
        print("AI audit: no picks to review.")
        return []
    table = [dict(zip(AUDIT_FIELDS, r)) for r in rows]
    prompt = (
        "You are a strict data auditor for an options-flow database. Each row is one options trade.\n"
        "Rules that must hold: contract text '<strike><C|P> <expiry>' matches strike, put_call and "
        "expiration; line text '- <size> @ <fill>' matches size and fill_price; premium is about "
        "fill_price*size*100; dte_days equals the days from trade_date to expiration; "
        "day_low <= day_vwap <= day_high and day_low <= day_close <= day_high; "
        "max_gain_pct = day_high/fill_price - 1 and close_pct = day_close/fill_price - 1; "
        "IV is between 0.01 and 8; delta is between -1 and 1 (calls positive, puts negative); "
        "estimates are fractions (0.25 = +25%). Missing values (null) are allowed.\n"
        "Check EVERY row strictly and list only real violations or clearly implausible values. "
        'Reply with JSON only: {"issues": [{"id": <row id>, "field": "<column>", "problem": "<short>"}]} '
        'or {"issues": []} if everything is consistent.\n\nROWS:\n'
        + "\n".join(json.dumps(r, default=str) for r in table))
    body = json.dumps({"model": AUDIT_MODEL, "stream": False, "format": "json", "think": False,
                       "keep_alive": -1, "options": {"temperature": 0, "num_ctx": 16384},
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    try:
        req = urllib.request.Request(pl.OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            reply = json.loads(json.loads(resp.read().decode("utf-8"))["message"]["content"])
        raw_issues = reply.get("issues") or []
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
        print(f"AI audit could not run: {e}")
        return []

    by_id = {r["id"]: r for r in table}
    confirmed, unverifiable, false_alarms, invalid = [], [], 0, 0
    for issue in raw_issues if isinstance(raw_issues, list) else []:
        pid, field = issue.get("id"), str(issue.get("field", ""))
        if pid not in by_id or field not in AUDIT_FIELDS:
            invalid += 1  # names a row or field that doesn't exist
            continue
        finding = (pid, field, str(issue.get("problem", ""))[:160], by_id[pid].get(field))
        verdict = _verify_finding(by_id[pid], field)
        if verdict is True:
            confirmed.append(finding)
        elif verdict is None:
            unverifiable.append(finding)
        else:
            false_alarms += 1  # code shows the value is actually fine
    run = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for kind, items in (("ai_confirmed", confirmed), ("ai_unverified", unverifiable)):
        for pid, field, problem, value in items:
            db.execute("INSERT INTO integrity_checks (run_utc, check_name, severity, message, alerted) "
                       "VALUES (?, ?, 'WARN', ?, ?)",
                       (run, kind, f"pick {pid} {field}={value}: {problem}", int(send and kind == "ai_confirmed")))
    db.commit()
    print(f"AI audit reviewed {len(table)} picks: {len(confirmed)} confirmed by code, "
          f"{len(unverifiable)} unverifiable, {false_alarms} false alarms discarded, {invalid} invalid.")
    for label, items in (("CONFIRMED", confirmed), ("worth a look", unverifiable)):
        for pid, field, problem, value in items:
            print(f"  {label}: pick {pid} {field}={value}: {problem}")
    if send:
        lines = [f"🤖 **Local AI audit** (Qwen on your PC): reviewed {len(table)} picks - "
                 f"{len(confirmed)} confirmed problem(s), {false_alarms} false alarm(s) filtered out"]
        lines += [f"🛑 pick {pid} ({by_id[pid]['ticker']} {by_id[pid]['contract']}) {field}={value}: {problem}"
                  for pid, field, problem, value in confirmed[:10]]
        lines += [f"❔ worth a look: pick {pid} ({by_id[pid]['ticker']}) {field}: {problem}"
                  for pid, field, problem, value in unverifiable[:5]]
        pl.discord_send("\n".join(lines), bot="auditor")
    return confirmed


def _verify_finding(r, field):
    """Check an AI finding with exact code. True = real problem, False = the value is fine,
    None = no exact rule exists for this field."""
    def num(k):
        return r.get(k) if isinstance(r.get(k), (int, float)) else None

    fill, size = num("fill_price"), num("size")
    if field in ("contract", "strike", "put_call", "expiration"):
        m = re.match(r"\s*([\d.]+)\s*([CP])\s+(\d{4}-\d{2}-\d{2})", r.get("contract") or "")
        return not (m and num("strike") is not None and abs(float(m.group(1)) - r["strike"]) < 1e-9
                    and m.group(2) == (r.get("put_call") or " ")[0] and m.group(3) == r.get("expiration"))
    if field in ("line", "size", "fill_price"):
        lm = re.search(r"-\s*([\d,]+)\s*@\s*([\d.]+)", r.get("line") or "")
        if not lm or size is None or fill is None:
            return None
        return int(lm.group(1).replace(",", "")) != size or abs(float(lm.group(2)) - fill) > 0.005
    if field == "premium":
        if not (fill and size and num("premium")):
            return None
        return abs(fill * size * 100 - r["premium"]) / r["premium"] > 0.10
    if field == "dte_days":
        if num("dte_days") is None or not r.get("expiration"):
            return None
        days = (date.fromisoformat(r["expiration"]) - date.fromisoformat(r["trade_date"])).days
        return abs(days - r["dte_days"]) > 1
    if field in ("day_low", "day_vwap", "day_high", "day_close"):
        lo, hi, vw, cl = num("day_low"), num("day_high"), num("day_vwap"), num("day_close")
        if lo is None or hi is None:
            return None
        return (hi < lo or (cl is not None and not lo - 1e-9 <= cl <= hi + 1e-9)
                or (vw is not None and not lo - 0.01 <= vw <= hi + 0.01))
    if field in ("max_gain_pct", "close_pct", "exp_close_pct"):
        price = {"max_gain_pct": num("day_high"), "close_pct": num("day_close"),
                 "exp_close_pct": num("exp_close")}[field]
        if price is None or not fill or num(field) is None:
            return None
        return abs((price / fill - 1) - r[field]) > 1e-6
    if field == "iv":
        return None if num("iv") is None else not 0.005 <= r["iv"] <= 8
    if field == "delta":
        d = num("delta")
        if d is None:
            return None
        return abs(d) > 1.0001 or (r.get("put_call") == "CALL" and d < 0) or (r.get("put_call") == "PUT" and d > 0)
    return None


def summary_line(results):
    fails = sum(1 for _, s, _ in results if s == FAIL)
    warns = sum(1 for _, s, _ in results if s == WARN)
    if not fails and not warns:
        return f"🔎 Data integrity: all {len(results)} checks passed"
    return f"🔎 Data integrity: {fails} FAIL, {warns} WARN of {len(results)} checks (details posted separately)"


def format_report(results):
    width = max(len(n) for n, _, _ in results)
    return "\n".join(f"{s:4s}  {n:<{width}}  {m}" for n, s, m in results)
