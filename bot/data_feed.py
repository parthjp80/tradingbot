"""
Data feed abstraction.

- `YFinanceFeed`: live daily OHLC data via yfinance. This is what runs on
  your TrueNAS deployment where outbound internet is unrestricted.
- `SyntheticFeed`: deterministic random-walk data generator, used by the
  test suite and for dry-running the bot without any network access.

Both implement the same `.history(symbol, period, interval)` interface so
the rest of the bot (regime.py, strategies/*) never needs to know which
one it's talking to.

TODO (next integration step): add `TastytradeFeed` using the OAuth2 flow
from your trading-reporter repo + DXLink streaming for real option chains,
greeks, and true IV rank. Swap it in via `get_feed()` below once the
paper-trading loop is validated.
"""
from __future__ import annotations
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
import numpy as np
import pandas as pd
from dotenv import load_dotenv

from bot.fundamentals import FundamentalMetrics
from bot.config import FUTURES_YAHOO_SYMBOL_MAP

load_dotenv()


def _to_yahoo_symbol(symbol: str) -> str:
    """Translates the bot's internal symbol to whatever yfinance actually
    expects. Only futures need translation today (continuous contracts
    require a '=F' suffix on Yahoo) -- everything else passes through
    unchanged. See FUTURES_YAHOO_SYMBOL_MAP in config.py."""
    return FUTURES_YAHOO_SYMBOL_MAP.get(symbol, symbol)


class DataFeed(ABC):
    @abstractmethod
    def history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        """Return a DataFrame with columns: Open, High, Low, Close, Volume."""
        raise NotImplementedError

    def vix(self, period: str = "1y") -> pd.DataFrame:
        return self.history("^VIX", period=period)

    def days_to_next_earnings(self, symbol: str) -> Optional[int]:
        """
        Trading days until the next known earnings date, or None if unknown.
        Base implementation: unknown. Subclasses override where the data
        source actually supports it.
        """
        return None

    def days_to_ex_dividend(self, symbol: str) -> Optional[int]:
        """Days until the next ex-dividend date, or None if unknown/no dividend."""
        return None

    def fundamentals(self, symbol: str) -> Optional[FundamentalMetrics]:
        """P/E, earnings growth, debt levels, dividend yield. None if unavailable."""
        return None

    def recent_news_count(self, symbol: str, lookback_days: int = 3) -> Optional[int]:
        """Count of news items in the trailing `lookback_days`. None if unavailable.
        Used as a soft 'elevated event risk' flag, not a hard filter -- news
        volume alone doesn't tell you sentiment, just that something's active."""
        return None


class YFinanceFeed(DataFeed):
    def history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        import yfinance as yf  # imported lazily so SyntheticFeed works with no dep at all

        yahoo_symbol = _to_yahoo_symbol(symbol)
        df = yf.Ticker(yahoo_symbol).history(period=period, interval=interval)
        if df.empty:
            raise ValueError(f"No data returned for {symbol} (yahoo symbol: {yahoo_symbol})")
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def days_to_next_earnings(self, symbol: str) -> Optional[int]:
        import yfinance as yf

        try:
            dates_df = yf.Ticker(symbol).get_earnings_dates(limit=4)
            if dates_df is None or dates_df.empty:
                return None
            now = pd.Timestamp.now(tz=dates_df.index.tz)
            future = dates_df.index[dates_df.index >= now]
            if len(future) == 0:
                return None
            next_date = future.min()
            return max((next_date - now).days, 0)
        except Exception:
            # Earnings calendar isn't always available (index/ETF tickers,
            # yfinance hiccups) -- treat as unknown rather than failing the scan.
            return None

    def days_to_ex_dividend(self, symbol: str) -> Optional[int]:
        import yfinance as yf

        try:
            info = yf.Ticker(symbol).info
            ts = info.get("exDividendDate")
            if not ts:
                return None
            ex_date = pd.Timestamp.fromtimestamp(ts, tz="UTC")
            now = pd.Timestamp.now(tz="UTC")
            days = (ex_date - now).days
            return days if days >= 0 else None  # already passed / stale info field
        except Exception:
            return None

    def fundamentals(self, symbol: str) -> Optional[FundamentalMetrics]:
        from bot.fundamentals import fetch_fundamentals_yfinance
        return fetch_fundamentals_yfinance(symbol)

    def recent_news_count(self, symbol: str, lookback_days: int = 3) -> Optional[int]:
        import yfinance as yf

        try:
            items = yf.Ticker(symbol).news
            if not items:
                return 0
            cutoff = datetime.now().timestamp() - lookback_days * 86400
            count = 0
            for item in items:
                ts = item.get("providerPublishTime") or item.get("content", {}).get("pubDate")
                if ts and (ts if isinstance(ts, (int, float)) else 0) >= cutoff:
                    count += 1
            return count
        except Exception:
            return None


class SyntheticFeed(DataFeed):
    """
    Deterministic synthetic OHLC generator (seeded per-symbol) so tests and
    offline dry-runs are reproducible without hitting the network.
    """

    def __init__(self, seed_base: int = 42, days: int = 300):
        self.seed_base = seed_base
        self.days = days

    def _seed_for(self, symbol: str) -> int:
        return self.seed_base + sum(ord(c) for c in symbol)

    def history(self, symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
        rng = np.random.default_rng(self._seed_for(symbol))
        n = self.days

        if symbol == "^VIX":
            # mean-reverting process around a realistic 13-22 band, with an
            # occasional spike, instead of a stock-like random walk
            close = np.zeros(n)
            close[0] = 16.0
            for i in range(1, n):
                mean_reversion = (16.0 - close[i - 1]) * 0.05
                shock = rng.normal(0, 0.6)
                # rare spike day
                if rng.random() < 0.01:
                    shock += rng.uniform(6, 12)
                close[i] = max(close[i - 1] + mean_reversion + shock, 9.0)
        else:
            drift = rng.normal(0.0002, 0.0001)
            vol = rng.uniform(0.012, 0.03)

            # inject a regime shift partway through so the classifier has
            # something interesting to detect during dry-runs
            returns = rng.normal(drift, vol, n)
            shift_point = n * 2 // 3
            returns[shift_point:] += rng.normal(0.0, vol * 1.8, n - shift_point)

            close = 100 * np.exp(np.cumsum(returns))
        high = close * (1 + rng.uniform(0.001, 0.01, n))
        low = close * (1 - rng.uniform(0.001, 0.01, n))
        open_ = close * (1 + rng.normal(0, 0.003, n))
        volume = rng.integers(1_000_000, 5_000_000, n)

        idx = pd.date_range(end=pd.Timestamp.today(), periods=n, freq="B")
        return pd.DataFrame(
            {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
            index=idx,
        )

    def days_to_next_earnings(self, symbol: str) -> Optional[int]:
        # deterministic per symbol so tests are reproducible: cycles through
        # 0-59 days out, giving a mix of in-blackout and clear names
        rng = np.random.default_rng(self._seed_for(symbol) + 999)
        return int(rng.integers(0, 60))

    def days_to_ex_dividend(self, symbol: str) -> Optional[int]:
        rng = np.random.default_rng(self._seed_for(symbol) + 1999)
        # ~30% of synthetic names pay no dividend (return None), rest get a
        # deterministic days-out value so blackout logic is exercisable in tests
        if rng.random() < 0.3:
            return None
        return int(rng.integers(0, 45))

    def fundamentals(self, symbol: str) -> Optional[FundamentalMetrics]:
        rng = np.random.default_rng(self._seed_for(symbol) + 2999)
        return FundamentalMetrics(
            pe_ratio=float(rng.uniform(8, 55)),
            forward_pe=float(rng.uniform(7, 50)),
            earnings_growth_yoy=float(rng.normal(0.08, 0.15)),
            debt_to_equity=float(rng.uniform(0.1, 2.5)),
            dividend_yield=float(rng.uniform(0, 0.04)) if rng.random() > 0.3 else None,
        )

    def recent_news_count(self, symbol: str, lookback_days: int = 3) -> Optional[int]:
        rng = np.random.default_rng(self._seed_for(symbol) + 3999)
        # most names quiet (0-3 items), occasional name running hot (event risk)
        return int(rng.choice([0, 1, 2, 3, 4, 10, 15], p=[0.25, 0.25, 0.2, 0.1, 0.1, 0.06, 0.04]))


def get_feed() -> DataFeed:
    """
    Feed selection: set BOT_DATA_FEED=synthetic in .env for offline dry-runs
    (default when yfinance/network isn't available), or =live for real data.
    """
    mode = os.getenv("BOT_DATA_FEED", "live").lower()
    if mode == "synthetic":
        return SyntheticFeed()
    return YFinanceFeed()
