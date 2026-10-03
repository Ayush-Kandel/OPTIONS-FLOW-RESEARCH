"""Trade context for a pick: was the whale BUYING (at the ask) or SELLING (at the bid), and was the
print one leg of a spread / hedge? Following a whale only makes sense when it bought.

Trade Echo's noteworthy rows are minute-level and can aggregate several fills, so each pick is
matched to its raw prints (second-level, with Trade Echo's sentiment) in two calls (1 credit each):
  1. the contract's prints around the pick minute -> the whale's exact print (the "anchor")
  2. every print on the ticker in the anchor's minute -> legs printed in the same second with a
     matching size (1:1 or 1:2) = a multi-leg order (vertical, calendar, straddle, collar...)
Side comes from Trade Echo's sentiment (a call marked BULLISH was bought at the ask, BEARISH sold at
the bid; NEUTRAL = between). Nightly, IBKR's own bid/ask at the print minute is stored next to it
(quote_pos: 0 = at the bid, 1 = at the ask) as an independent check.
Stock legs (a put bought against shares) never show in options flow, so those hedges can't be seen.
"""

import json
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS pick_context (
    pick_id      INTEGER PRIMARY KEY,
    status       TEXT NOT NULL,        -- 'ok', 'no_match' (no raw print found), 'error'
    print_time   TEXT,                 -- anchor print time, HH:MM:SS ET
    sentiment    TEXT,                 -- Trade Echo's: BULLISH / BEARISH / NEUTRAL
    activity     TEXT,                 -- SWEEP / TRADE
    side         TEXT,                 -- 'buy' (at the ask), 'sell' (at the bid), 'mid'
    structure    TEXT,                 -- NULL = single leg; else 'call vertical', 'calendar', 'collar'...
    our_leg      TEXT,                 -- in a structure: 'bought' / 'sold' / 'unclear'
    legs_json    TEXT,                 -- the raw rows used (anchor first), straight from Trade Echo
    quote_pos    REAL,                 -- IBKR: (fill - bid) / (ask - bid) at the print minute
    checked_utc  TEXT NOT NULL
)"""

LEG_SECONDS = 1          # legs of one order print within a second of each other
MIN_LEG_PREMIUM = 10000


def ensure_schema(db):
    db.execute(SCHEMA)
    db.commit()


def side_of(put_call, sentiment):
    s = (sentiment or "").upper()
    if s == "NEUTRAL" or s not in ("BULLISH", "BEARISH"):
        return "mid"
    bullish = s == "BULLISH"
    return "buy" if bullish == (put_call == "CALL") else "sell"


def _rows(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("rows") if isinstance(data, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


def _hhmm(t, minutes=0):
    d = datetime.strptime(t[:5], "%H:%M") + timedelta(minutes=minutes)
    return d.strftime("%H:%M")


def _structure(anchor, leg):
    a_pc, l_pc = anchor["put_call"], leg["put_call"]
    a_exp, l_exp = (anchor.get("date_expiration") or "")[:10], (leg.get("date_expiration") or "")[:10]
    a_k, l_k = anchor["strike_price"], leg["strike_price"]
    a_side, l_side = side_of(a_pc, anchor.get("sentiment")), side_of(l_pc, leg.get("sentiment"))
    opposite = {a_side, l_side} == {"buy", "sell"}
    if a_pc == l_pc:
        kind = "call" if a_pc == "CALL" else "put"
        if a_exp == l_exp:
            return f"{kind} vertical" if opposite else f"{kind} pair"
        return "calendar" if a_k == l_k else "diagonal"
    if a_exp != l_exp:
        return "call/put combo"
    if opposite:
        put_bought = (a_pc == "PUT" and a_side == "buy") or (l_pc == "PUT" and l_side == "buy")
        return "collar (hedge)" if put_bought else "risk reversal"
    return "straddle" if a_k == l_k else "strangle"


def check(db, call, pick_id):
    """Fetch and store the context of one pick. `call` = a budgeted Trade Echo call."""
    ensure_schema(db)
    tk, k, pc, exp, td, t, prem, size = db.execute(
        "SELECT ticker, strike, put_call, expiration, trade_date, trade_time_et, premium, size FROM picks "
        "WHERE id = ?", (pick_id,)).fetchone()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def save(status, **v):
        cols = ["pick_id", "status", "checked_utc", *v]
        db.execute(f"INSERT OR REPLACE INTO pick_context ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                   (pick_id, status, now, *v.values()))
        db.commit()
        return status

    # 1. the whale's own print(s) of this contract around the pick minute
    found = _rows(call("get_option_flow", {
        "ticker": tk, "date": td, "time_from": _hhmm(t, -1), "time_to": _hhmm(t, 2),
        "call_or_put": "Call" if pc == "CALL" else "Put", "min_premium": max(MIN_LEG_PREMIUM, (prem or 0) * 0.3),
        "limit": 50}, "context", tk))
    same = [r for r in found if r.get("strike_price") == k and (r.get("date_expiration") or "")[:10] == exp]
    if not same:
        return save("no_match")
    anchor = min(same, key=lambda r: abs((r.get("premium") or 0) - (prem or 0)))
    side = side_of(pc, anchor.get("sentiment"))

    # 2. everything on the ticker in the anchor's minute: legs printed in the same second
    minute = anchor["trade_time_et"][:5]
    around = _rows(call("get_option_flow", {
        "ticker": tk, "date": td, "time_from": minute, "time_to": minute,
        "min_premium": MIN_LEG_PREMIUM, "limit": 50}, "context", tk))
    a_sec = datetime.strptime(anchor["trade_time_et"], "%H:%M:%S")
    a_size = anchor.get("size") or 0
    legs = []
    for r in around:
        if (r.get("strike_price"), r.get("put_call"), (r.get("date_expiration") or "")[:10]) == (k, pc, exp):
            continue
        try:
            gap = abs((datetime.strptime(r["trade_time_et"], "%H:%M:%S") - a_sec).total_seconds())
        except (KeyError, ValueError):
            continue
        r_size = r.get("size") or 0
        ratio_ok = a_size and r_size and any(abs(r_size - a_size * x) <= max(1, 0.02 * a_size * x)
                                             for x in (1, 2, 0.5))
        if gap <= LEG_SECONDS and ratio_ok:
            legs.append((gap, abs(r_size - a_size), r))
    structure = our_leg = None
    if legs:
        leg = min(legs, key=lambda x: (x[0], x[1]))[2]
        structure = _structure(anchor, leg)
        our_leg = {"buy": "bought", "sell": "sold"}.get(side)
        if our_leg is None:   # anchor at mid: infer from the other leg if it's clearly one-sided
            other = side_of(leg["put_call"], leg.get("sentiment"))
            same_dir = structure in ("straddle", "strangle", "call pair", "put pair")
            our_leg = {"buy": "bought" if same_dir else "sold",
                       "sell": "sold" if same_dir else "bought"}.get(other, "unclear")
        rows = [anchor, leg]
    else:
        rows = [anchor]
    return save("ok", print_time=anchor["trade_time_et"], sentiment=anchor.get("sentiment"),
                activity=anchor.get("option_activity_type"), side=side, structure=structure,
                our_leg=our_leg, legs_json=json.dumps(rows))


def whale_sold(ctx):
    """True when following would mean taking the other side of the whale's trade."""
    return bool(ctx) and ctx["status"] == "ok" and (
        ctx["our_leg"] == "sold" or (ctx["structure"] is None and ctx["side"] == "sell"))


def get(db, pick_id):
    ensure_schema(db)
    cur = db.execute("SELECT * FROM pick_context WHERE pick_id = ?", (pick_id,))
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row)) if row else None


def describe(ctx):
    """One plain-English line for pings."""
    if not ctx or ctx["status"] != "ok":
        return "Side: not checked" if not ctx else "Side: no matching raw print found"
    side = {"buy": "🟢 **bought at the ask** (aggressive buyer)", "sell": "🔴 **sold at the bid** (the whale was SELLING)",
            "mid": "🟡 traded between bid and ask (side unclear)"}[ctx["side"]]
    text = f"Whale {side} · {ctx['activity'] or ''} {ctx['print_time']}"
    if ctx["structure"]:
        legs = json.loads(ctx["legs_json"] or "[]")
        other = legs[1] if len(legs) > 1 else {}
        text += (f"\n🧩 **Part of a {ctx['structure']}**: also traded {other.get('size')} × "
                 f"${other.get('strike_price')} {other.get('put_call', '').title()} "
                 f"{(other.get('date_expiration') or '')[5:10]} ({(other.get('sentiment') or '').lower()}) "
                 f"in the same second - this contract was the leg **{ctx['our_leg']}**")
    return text


def backfill(db, call, deadline=None, limit=None, log=print):
    """Context for picks that don't have it yet (2 credits each): IBKR-graded picks and your
    tickers first, newest first. Stops at `deadline` (epoch). Algo Edge alerts are skipped: their
    trades aren't in Trade Echo's raw flow feed (0 of 6 matched on Oct 2), so asking costs credits
    for nothing."""
    import time
    import pipeline
    ensure_schema(db)
    marks = ",".join("?" * len(pipeline.MY_TICKERS))
    todo = [r[0] for r in db.execute(
        f"SELECT p.id FROM picks p LEFT JOIN pick_context c ON c.pick_id = p.id "
        f"WHERE (c.pick_id IS NULL OR c.status = 'error') AND p.strike IS NOT NULL AND p.source != 'algo' "
        f"ORDER BY p.true_source = 'ibkr' DESC, p.ticker IN ({marks}) DESC, p.trade_date DESC, p.trade_time_et DESC",
        tuple(sorted(pipeline.MY_TICKERS))).fetchall()][:limit]
    log(f"Trade context: {len(todo)} picks to check (~{2 * len(todo)} credits)")
    counts = {}
    for i, pid in enumerate(todo, 1):
        if deadline and time.time() > deadline:
            log(f"Trade context: stopped at {i - 1}/{len(todo)} (deadline)")
            break
        try:
            st = check(db, call, pid)
        except Exception as e:
            db.execute("INSERT OR REPLACE INTO pick_context (pick_id, status, checked_utc) VALUES (?, 'error', ?)",
                       (pid, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            db.commit()
            st = "error"
            log(f"  context pick {pid}: {type(e).__name__}: {str(e)[:150]}")
        counts[st] = counts.get(st, 0) + 1
        if i % 25 == 0:
            log(f"  context {i}/{len(todo)}: {counts}")
    log(f"Trade context: {counts}; IBKR quote cross-checks added: {update_quote_positions(db)}")
    return counts


def update_quote_positions(db):
    """IBKR cross-check: where the whale's fill sat inside the bid/ask at the print minute."""
    ensure_schema(db)
    n = 0
    for pid, fill, minute in db.execute(
            "SELECT c.pick_id, p.fill_price, p.trade_date || ' ' || substr(p.trade_time_et, 1, 5) FROM pick_context c "
            "JOIN picks p ON p.id = c.pick_id WHERE c.quote_pos IS NULL").fetchall():
        bars = dict(db.execute("SELECT kind, MIN(low) FROM ib_bars WHERE pick_id = ? AND minute_et = ? AND kind = 'BID' "
                               "UNION ALL SELECT kind, MAX(high) FROM ib_bars WHERE pick_id = ? AND minute_et = ? "
                               "AND kind = 'ASK'", (pid, minute, pid, minute)).fetchall())
        bid, ask = bars.get("BID"), bars.get("ASK")
        if bid and ask and ask > bid > 0 and fill:
            db.execute("UPDATE pick_context SET quote_pos = ? WHERE pick_id = ?", ((fill - bid) / (ask - bid), pid))
            n += 1
    db.commit()
    return n
