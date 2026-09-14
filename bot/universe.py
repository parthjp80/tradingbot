"""
The scanner needs a candidate universe to screen -- it doesn't discover
tickers out of thin air, it ranks a defined pool.

Default pool: a curated set of liquid, optionable large/mid-cap names
spanning sectors, biased toward names that reliably have tight option
spreads and real open interest (a scanner is only as good as its inputs --
ranking a stock with no real market for its options is a trap, not an edge).

To use your own list instead (e.g. names you already follow, or an
export from a real screener), create data/universe.txt with one ticker
per line -- it's picked up automatically and the curated list below is
ignored.
"""
from __future__ import annotations
from pathlib import Path
from bot.config import DATA_DIR

CURATED_UNIVERSE = [
    # mega-cap tech / semis
    "AAPL", "MSFT", "NVDA", "AMD", "AVGO", "GOOGL", "META", "AMZN",
    # your existing names
    "IBM", "WDC", "NVO", "PLTR",
    # financials
    "JPM", "BAC", "GS", "SCHW",
    # industrials / energy
    "CAT", "XOM", "CVX", "BA",
    # consumer
    "DIS", "NKE", "SBUX", "COST", "TGT",
    # healthcare
    "UNH", "PFE", "LLY",
    # ETFs (deep, liquid options markets, useful for iron condors specifically)
    "SPY", "QQQ", "IWM",
]

UNIVERSE_FILE = DATA_DIR / "universe.txt"


def get_universe() -> list[str]:
    if UNIVERSE_FILE.exists():
        lines = [l.strip().upper() for l in UNIVERSE_FILE.read_text().splitlines()]
        tickers = [l for l in lines if l and not l.startswith("#")]
        if tickers:
            return tickers
    return CURATED_UNIVERSE
