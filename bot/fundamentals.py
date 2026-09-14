"""
Fundamental screening data. Kept separate from technical indicators.py
since it comes from a different source (company financials, not price
history) and updates on a completely different cadence (quarterly, not
daily).

These are used as SOFT screening signals by default -- see ScannerConfig
in config.py, where max_pe_ratio / max_debt_to_equity / min_earnings_growth
default to None (disabled). A stock with no P/E (unprofitable growth name)
or high debt isn't automatically wrong to trade options on; it's your call
whether "fundamentally strong" should gate premium-selling candidates at
all. The mechanism is here; the defaults don't force an opinion on it.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class FundamentalMetrics:
    pe_ratio: Optional[float] = None          # trailing P/E
    forward_pe: Optional[float] = None
    earnings_growth_yoy: Optional[float] = None  # e.g. 0.15 = +15% YoY
    debt_to_equity: Optional[float] = None      # e.g. 1.2 = 120%
    dividend_yield: Optional[float] = None       # e.g. 0.02 = 2%


def fetch_fundamentals_yfinance(symbol: str) -> Optional[FundamentalMetrics]:
    """Live path -- called from YFinanceFeed.fundamentals(). Kept as a
    free function so it's easy to unit test / swap independently."""
    import yfinance as yf

    try:
        info = yf.Ticker(symbol).info
        if not info:
            return None
        return FundamentalMetrics(
            pe_ratio=info.get("trailingPE"),
            forward_pe=info.get("forwardPE"),
            earnings_growth_yoy=info.get("earningsGrowth"),
            debt_to_equity=(info.get("debtToEquity") / 100.0 if info.get("debtToEquity") else None),
            dividend_yield=info.get("dividendYield"),
        )
    except Exception:
        # Fundamentals aren't always available (ETFs, delisted names,
        # yfinance rate limits) -- treat as unknown, never fail the scan.
        return None
