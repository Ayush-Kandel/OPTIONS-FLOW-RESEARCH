"""Stock-tape math shared by FlowDesk's Market screen (app_market.py) and the model inputs
(pipeline.py), so both always mean the same thing by "volume vs. normal", RSI or VWAP. No I/O.

Bars are (minute 'YYYY-MM-DD HH:MM', open, high, low, close, volume, vwap) tuples, oldest first.
"""

MINUTES = [f"{9 + (30 + i) // 60:02d}:{(30 + i) % 60:02d}" for i in range(390)]   # 09:30 ... 15:59
FULL_DAY = 380            # a day with at least this many 1-minute bars counts as complete


def volume_curve(days):
    """Share of a normal day's volume traded in each minute, and by each minute (cumulative),
    averaged over complete days. `days` = iterable of {hhmm: volume} (one per ticker-day).
    Returns {hhmm: (share, cumulative)}, or {} when there is no complete day."""
    share, n = {}, 0
    for bars in days:
        if len(bars) < FULL_DAY:
            continue
        total = sum(bars.values())
        if total <= 0:
            continue
        n += 1
        for hm, v in bars.items():
            share[hm] = share.get(hm, 0) + v / total
    if not n:
        return {}
    out, cum = {}, 0.0
    for hm in MINUTES:
        s = share.get(hm, 0) / n
        cum += s
        out[hm] = (s, cum)
    return out


def ema(values, n):
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    """Wilder's RSI over the closes; None with fewer than n + 1 of them."""
    if len(closes) <= n:
        return None
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    g, l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        g, l = (g * (n - 1) + gains[i]) / n, (l * (n - 1) + losses[i]) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def vwap(bars):
    """Volume-weighted average price of the bars (each bar's own VWAP, else its typical price)."""
    num = den = 0.0
    for b in bars:
        v = b[5] or 0
        num += (b[6] if b[6] else (b[2] + b[3] + b[4]) / 3) * v
        den += v
    return num / den if den else None


def rvol(bars, curve, normal):
    """Volume so far vs. a normal day by the same minute (1.0 = normal). Needs 3+ bars."""
    if not curve or not normal or len(bars) < 3:
        return None
    expected = normal * curve.get(bars[-1][0][11:16], (0, 0))[1]
    return sum(b[5] or 0 for b in bars) / expected if expected else None


def volume_pace(bars, curve, recent=5, before=15):
    """The last `recent` minutes' volume vs. the `before` minutes ahead of them, each minute scaled
    by its normal share of the day (the open and close are always busy). >1 = picking up."""
    if not curve:
        return None
    u = [(b[5] or 0) / curve[b[0][11:16]][0] for b in bars if curve.get(b[0][11:16], (0,))[0]]
    if len(u) < recent + before:
        return None
    prev = sum(u[-(recent + before):-recent]) / before
    return (sum(u[-recent:]) / recent) / prev if prev else None
