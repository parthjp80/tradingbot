"""
Simplified Black-Scholes pricing and greeks.

This is here so the paper-trading strategies can produce *plausible*
credit/debit, delta, theta, and vega numbers without a live options chain.
It is deliberately approximate:

  - Uses realized vol (scaled up ~15%, a common realized->implied
    convexity premium) as a stand-in for true implied vol.
  - Ignores dividends, early-assignment risk, bid/ask skew, and the
    volatility smile.

Before going live, replace `estimate_iv()` and the strike selection here
with real DXLink chain data (strikes, mid-price, and greeks tastytrade
already gives you) -- this module's job is only to make paper trading
directionally realistic enough to test the regime/risk logic end to end.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from scipy.stats import norm

from bot.indicators import realized_vol
import pandas as pd


@dataclass
class OptionLeg:
    strike: float
    is_call: bool
    price: float
    delta: float
    theta: float
    vega: float


def estimate_iv(close: pd.Series, realized_to_implied_premium: float = 1.15) -> float:
    rv = realized_vol(close, window=20).dropna()
    if rv.empty:
        return 0.30
    return float(rv.iloc[-1]) * realized_to_implied_premium


def _d1_d2(spot: float, strike: float, t_years: float, vol: float, r: float = 0.045):
    if t_years <= 0 or vol <= 0:
        return 0.0, 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * vol ** 2) * t_years) / (vol * math.sqrt(t_years))
    d2 = d1 - vol * math.sqrt(t_years)
    return d1, d2


def price_option(spot: float, strike: float, days_to_exp: int, vol: float, is_call: bool, r: float = 0.045) -> OptionLeg:
    t = max(days_to_exp, 1) / 365.0
    d1, d2 = _d1_d2(spot, strike, t, vol, r)

    if is_call:
        price = spot * norm.cdf(d1) - strike * math.exp(-r * t) * norm.cdf(d2)
        delta = norm.cdf(d1)
    else:
        price = strike * math.exp(-r * t) * norm.cdf(-d2) - spot * norm.cdf(-d1)
        delta = norm.cdf(d1) - 1

    theta = -(spot * norm.pdf(d1) * vol) / (2 * math.sqrt(t)) / 365.0
    vega = spot * norm.pdf(d1) * math.sqrt(t) / 100.0  # per 1 vol point

    return OptionLeg(strike=round(strike, 2), is_call=is_call, price=round(max(price, 0.01), 2),
                      delta=round(delta, 3), theta=round(theta, 4), vega=round(vega, 4))


def strike_at_std_dev(spot: float, vol: float, days_to_exp: int, std_devs: float, above: bool) -> float:
    t = max(days_to_exp, 1) / 365.0
    move = spot * vol * math.sqrt(t) * std_devs
    return spot + move if above else spot - move
