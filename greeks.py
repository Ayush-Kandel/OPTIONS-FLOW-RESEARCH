"""Black-Scholes implied volatility and Greeks, computed locally (no API calls).

European-style approximation with no dividends. Close enough for short-dated
contracts; it is an estimate, not the exchange's official Greeks.
"""

import math

RISK_FREE = 0.04


def _cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(spot, strike, years, vol, is_call, r=RISK_FREE):
    if years <= 0 or vol <= 0:
        intrinsic = spot - strike if is_call else strike - spot
        return max(intrinsic, 0.0)
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (r + vol * vol / 2) * years) / sq
    d2 = d1 - sq
    if is_call:
        return spot * _cdf(d1) - strike * math.exp(-r * years) * _cdf(d2)
    return strike * math.exp(-r * years) * _cdf(-d2) - spot * _cdf(-d1)


def implied_vol(price, spot, strike, years, is_call, r=RISK_FREE):
    """Bisection on volatility. None if the price is below intrinsic value or out of range."""
    if not (price and spot and strike and years and years > 0):
        return None
    lo, hi = 0.005, 8.0
    if price < bs_price(spot, strike, years, lo, is_call, r) - 1e-6:
        return None
    if price > bs_price(spot, strike, years, hi, is_call, r):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, years, mid, is_call, r) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def greeks(spot, strike, years, vol, is_call, r=RISK_FREE):
    """delta, gamma, theta (per calendar day, per share) and vega (per 1 vol point, per share)."""
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (r + vol * vol / 2) * years) / sq
    d2 = d1 - sq
    delta = _cdf(d1) if is_call else _cdf(d1) - 1
    gamma = _pdf(d1) / (spot * sq)
    decay = -spot * _pdf(d1) * vol / (2 * math.sqrt(years))
    if is_call:
        theta = decay - r * strike * math.exp(-r * years) * _cdf(d2)
    else:
        theta = decay + r * strike * math.exp(-r * years) * _cdf(-d2)
    vega = spot * _pdf(d1) * math.sqrt(years) / 100
    return {"delta": delta, "gamma": gamma, "theta": theta / 365, "vega": vega}
