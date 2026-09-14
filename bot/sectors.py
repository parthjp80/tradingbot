"""
Minimal sector map for the curated universe (bot/universe.py). Used purely
for concentration limits (RiskManager won't let too many open positions
pile up in one sector) -- not for sector rotation strategy, which is a
different feature.

If you point the scanner at data/universe.txt with your own tickers,
anything not in this map falls back to "UNKNOWN" and is exempt from the
per-sector cap (better to under-diversify-check an unmapped name than to
silently block trading on it).
"""
from __future__ import annotations

SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
    "AMD": "Technology", "AVGO": "Technology", "GOOGL": "Technology",
    "META": "Technology", "AMZN": "Consumer Discretionary",
    "IBM": "Technology", "WDC": "Technology", "NVO": "Healthcare", "PLTR": "Technology",
    "JPM": "Financials", "BAC": "Financials", "GS": "Financials", "SCHW": "Financials",
    "CAT": "Industrials", "XOM": "Energy", "CVX": "Energy", "BA": "Industrials",
    "DIS": "Communication Services", "NKE": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary", "COST": "Consumer Staples", "TGT": "Consumer Discretionary",
    "UNH": "Healthcare", "PFE": "Healthcare", "LLY": "Healthcare",
    "SPY": "Broad Market ETF", "QQQ": "Broad Market ETF", "IWM": "Broad Market ETF",
    "MES": "Broad Market Futures",
}


def get_sector(symbol: str) -> str:
    return SECTOR_MAP.get(symbol.upper(), "UNKNOWN")
