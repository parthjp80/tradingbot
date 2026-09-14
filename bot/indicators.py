"""
Indicator math: ADX, moving-average slope, realized volatility, and an
IV-rank proxy. All functions take/return pandas Series or plain floats so
they're independent of the data source (yfinance live, or synthetic for
testing).

NOTE on IV rank: true IV rank should come from actual options-chain implied
vol (tastytrade DXLink gives you this directly, since you already have
OAuth2 wired up in trading-reporter). Until that feed is plugged in here,
`iv_rank_proxy()` approximates it using the percentile rank of 20-day
realized volatility over the trailing year, which correlates with but is
not identical to true IV rank. Swap `iv_rank_proxy` for a DXLink-backed
`iv_rank_from_chain()` before trusting this live -- see data_feed.py.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.rolling(period).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = true_range(high, low, close)
    atr_ = tr.rolling(period).mean()

    plus_di = 100 * pd.Series(plus_dm, index=high.index).rolling(period).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=high.index).rolling(period).mean() / atr_

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(period).mean()


def ma_slope(close: pd.Series, window: int = 20, lookback: int = 5) -> float:
    """Normalized slope of the moving average, as % change over `lookback` bars."""
    ma = close.rolling(window).mean()
    if len(ma.dropna()) < lookback + 1:
        return 0.0
    recent = ma.iloc[-1]
    past = ma.iloc[-1 - lookback]
    if past == 0 or np.isnan(past):
        return 0.0
    return (recent - past) / abs(past)


def realized_vol(close: pd.Series, window: int = 20, annualize: bool = True) -> pd.Series:
    log_ret = np.log(close / close.shift(1))
    rv = log_ret.rolling(window).std()
    if annualize:
        rv = rv * np.sqrt(252)
    return rv


def iv_rank_proxy(close: pd.Series, window: int = 20, lookback_days: int = 252) -> float:
    """
    Percentile rank (0-100) of current realized vol within its trailing-year
    range. A stand-in for true options IV rank -- see module docstring.
    """
    rv = realized_vol(close, window=window)
    rv = rv.dropna()
    if len(rv) < 20:
        return 50.0  # not enough history, assume neutral
    window_slice = rv.tail(lookback_days)
    current = window_slice.iloc[-1]
    rank = (window_slice < current).mean() * 100
    return float(rank)


def vix_jump_pct(vix_close: pd.Series) -> float:
    if len(vix_close) < 2:
        return 0.0
    prev, curr = vix_close.iloc[-2], vix_close.iloc[-1]
    if prev == 0:
        return 0.0
    return float((curr - prev) / prev)


# ---------------------------------------------------------------------------
# Trend / breakout indicators (added for scanner screening criteria)
# ---------------------------------------------------------------------------

def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(close: pd.Series, window: int = 20, num_std: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Returns (upper, mid, lower, width_pct). width_pct = band width as % of mid,
    the standard normalization for comparing squeeze tightness across stocks
    with very different price levels."""
    mid = close.rolling(window).mean()
    std = close.rolling(window).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    width_pct = (upper - lower) / mid.replace(0, np.nan) * 100
    return upper, mid, lower, width_pct


def is_bb_squeeze(width_pct: pd.Series, lookback: int = 120, percentile_threshold: float = 20.0) -> bool:
    """
    True when the current Bollinger Band width sits in the bottom
    `percentile_threshold` percent of its trailing `lookback`-bar range --
    i.e. volatility has compressed unusually tight, a classic setup that
    often precedes a breakout in either direction.
    """
    w = width_pct.dropna().tail(lookback)
    if len(w) < 20:
        return False
    current = w.iloc[-1]
    rank_pct = (w < current).mean() * 100
    return bool(rank_pct <= percentile_threshold)


def volume_spike_ratio(volume: pd.Series, window: int = 20) -> float:
    """Current volume / trailing average volume. >1.5 is a commonly used
    'something is happening' threshold; >2-3x often accompanies breakouts."""
    avg = volume.rolling(window).mean()
    if avg.empty or pd.isna(avg.iloc[-1]) or avg.iloc[-1] == 0:
        return 1.0
    return float(volume.iloc[-1] / avg.iloc[-1])


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    """ATR normalized as a percent of current price -- lets you compare
    'how much does this actually move' across stocks at very different
    price levels (a $500 stock and a $20 stock can have the same $ ATR
    but wildly different real volatility)."""
    atr_series = atr(high, low, close, period=period).dropna()
    if atr_series.empty or close.iloc[-1] == 0:
        return 0.0
    return float(atr_series.iloc[-1] / close.iloc[-1] * 100)
