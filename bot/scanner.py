"""
The scanner turns "trade these fixed names" into "trade whatever the
market is actually offering an edge in right now." Each cycle, per symbol
in the universe:

  1. FILTER  -- hard rejects: too illiquid, too price-cheap, too flat
     (ATR floor), inside an earnings or ex-dividend blackout, or fails an
     (optional, off by default) fundamental gate.
  2. CLASSIFY -- run the existing RegimeClassifier.
  3. SCORE   -- rank survivors by setup quality *for their regime*, with
     trend/breakout/event-risk signals adjusting the score up or down.
  4. RANK    -- keep the top N.

This ranks setup QUALITY, not direction. It's not trying to predict which
stock moves next -- it's trying to avoid wasting size on a stale, dead, or
event-landmine name when better setups exist in the same universe.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pandas as pd

from bot.config import SCANNER, WatchlistItem
from bot.data_feed import DataFeed
from bot.regime import RegimeClassifier
from bot.models import Regime, RegimeSnapshot
from bot.universe import get_universe
from bot.fundamentals import FundamentalMetrics
from bot.indicators import (
    macd, bollinger_bands, is_bb_squeeze, volume_spike_ratio, atr_pct,
)
from bot.logger_setup import get_logger

log = get_logger(__name__)


@dataclass
class TechnicalFlags:
    atr_pct: float = 0.0
    macd_bullish: bool = False       # MACD histogram > 0 (momentum currently up)
    bb_squeeze: bool = False         # compressed volatility, breakout setup
    volume_spike: bool = False       # volume >= threshold x its 20d average


@dataclass
class ScanResult:
    symbol: str
    snapshot: Optional[RegimeSnapshot]
    avg_dollar_volume: float
    days_to_earnings: Optional[int]
    days_to_ex_dividend: Optional[int]
    recent_news_count: Optional[int]
    fundamentals: Optional[FundamentalMetrics]
    technicals: Optional[TechnicalFlags]
    score: float
    reject_reason: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.reject_reason is None


class MarketScanner:
    def __init__(self, feed: DataFeed, classifier: RegimeClassifier, macro_engine=None):
        self.feed = feed
        self.classifier = classifier
        self.macro_engine = macro_engine  # optional MacroSentimentEngine; see prediction_markets.py

    # ---- technical flags -----------------------------------------------------

    @staticmethod
    def _compute_technicals(price_history: pd.DataFrame) -> TechnicalFlags:
        close, high, low, volume = (
            price_history["Close"], price_history["High"], price_history["Low"], price_history["Volume"],
        )

        _, _, hist = macd(close)
        macd_bullish = bool(not hist.dropna().empty and hist.dropna().iloc[-1] > 0)

        _, _, _, width_pct = bollinger_bands(close)
        squeeze = is_bb_squeeze(width_pct, SCANNER.bb_squeeze_lookback, SCANNER.bb_squeeze_percentile)

        vol_spike = volume_spike_ratio(volume) >= SCANNER.volume_spike_threshold

        return TechnicalFlags(
            atr_pct=atr_pct(high, low, close),
            macd_bullish=macd_bullish,
            bb_squeeze=squeeze,
            volume_spike=vol_spike,
        )

    # ---- fundamental gate (soft by default -- see ScannerConfig) -----------

    @staticmethod
    def _fails_fundamentals(fnd: Optional[FundamentalMetrics]) -> Optional[str]:
        if fnd is None:
            return None  # unknown fundamentals never block a trade

        if SCANNER.max_pe_ratio is not None and fnd.pe_ratio is not None:
            if fnd.pe_ratio > SCANNER.max_pe_ratio:
                return f"P/E {fnd.pe_ratio:.0f} exceeds max {SCANNER.max_pe_ratio:.0f}"

        if SCANNER.max_debt_to_equity is not None and fnd.debt_to_equity is not None:
            if fnd.debt_to_equity > SCANNER.max_debt_to_equity:
                return f"debt/equity {fnd.debt_to_equity:.2f} exceeds max {SCANNER.max_debt_to_equity:.2f}"

        if SCANNER.min_earnings_growth is not None and fnd.earnings_growth_yoy is not None:
            if fnd.earnings_growth_yoy < SCANNER.min_earnings_growth:
                return f"earnings growth {fnd.earnings_growth_yoy:.1%} below min {SCANNER.min_earnings_growth:.1%}"

        return None

    # ---- scoring --------------------------------------------------------------

    @staticmethod
    def _score(snapshot: RegimeSnapshot, technicals: TechnicalFlags, news_count: Optional[int],
               macro_tilt: float = 0.0) -> float:
        """
        Higher is a better setup, scored per-regime:

        HIGH_IV_RANGE (premium selling): rewards IV rank (richer premium)
        and low ADX (genuinely range-bound). PENALIZES a Bollinger squeeze --
        compressed price range + a squeeze is exactly the setup that
        precedes a breakout, which is the tail risk that hurts short
        strangles/condors most.

        TRENDING (directional): rewards ADX + slope strength, and BONUSES
        trend confirmation (MACD agrees with the trend direction) and a
        breakout combo (squeeze + volume spike together -- compressed
        range that's now expanding on real volume is a stronger signal
        than either alone). Also takes a small macro_tilt adjustment when
        prediction markets are enabled (see bot/prediction_markets.py) --
        a directional candidate whose direction agrees with the
        macro-implied bias ranks slightly higher, disagrees ranks slightly
        lower. This is deliberately small (see
        PREDICTION_MARKETS.directional_tilt_max_confidence_delta) -- it's a
        tiebreaker among otherwise-similar setups, not a override of the
        technical read.

        Both regimes apply a small penalty for elevated recent news volume
        -- not because news is bad, but because it means something is
        actively being priced in, which adds event risk on top of whatever
        the regime already implies.
        """
        if snapshot.regime == Regime.HIGH_IV_RANGE:
            richness = snapshot.iv_rank
            choppiness_bonus = max(0.0, 20 - snapshot.adx)
            squeeze_penalty = 15.0 if technicals.bb_squeeze else 0.0
            score = richness + choppiness_bonus - squeeze_penalty

        elif snapshot.regime == Regime.TRENDING:
            trend_strength = snapshot.adx + min(abs(snapshot.ma_slope) * 200, 20)
            trend_up = snapshot.ma_slope > 0
            confirmation_bonus = 10.0 if (technicals.macd_bullish == trend_up) else -10.0
            breakout_bonus = 15.0 if (technicals.bb_squeeze and technicals.volume_spike) else (
                5.0 if technicals.volume_spike else 0.0
            )
            # macro_tilt is already signed relative to "long"/"short"; scale
            # it up into scanner-score units (it arrives as a small
            # confidence-scale delta, e.g. +/-0.08)
            macro_bonus = macro_tilt * 100 if trend_up else -macro_tilt * 100
            score = trend_strength + confirmation_bonus + breakout_bonus + macro_bonus

        else:
            return -1.0  # LOW_IV_RANGE / CRISIS: not tradeable, sort to the bottom

        if news_count is not None and news_count > SCANNER.max_recent_news_count:
            score -= 10.0

        return score

    # ---- filters ----------------------------------------------------------

    def _passes_filters(
        self, symbol: str, price_history: pd.DataFrame,
        technicals: TechnicalFlags, fundamentals: Optional[FundamentalMetrics],
    ) -> Optional[str]:
        last_price = float(price_history["Close"].iloc[-1])
        if last_price < SCANNER.min_price:
            return f"price ${last_price:.2f} below ${SCANNER.min_price:.0f} floor"

        avg_dollar_vol = float((price_history["Close"] * price_history["Volume"]).tail(20).mean())
        if avg_dollar_vol < SCANNER.min_avg_dollar_volume:
            return f"avg $ volume {avg_dollar_vol/1e6:.1f}M below {SCANNER.min_avg_dollar_volume/1e6:.0f}M floor"

        if technicals.atr_pct < SCANNER.min_atr_pct:
            return f"ATR {technicals.atr_pct:.2f}% of price below {SCANNER.min_atr_pct:.1f}% floor (too flat)"

        days_to_earn = self.feed.days_to_next_earnings(symbol)
        if days_to_earn is not None and days_to_earn <= SCANNER.earnings_blackout_days:
            return f"earnings in {days_to_earn}d, inside {SCANNER.earnings_blackout_days}d blackout"

        days_to_div = self.feed.days_to_ex_dividend(symbol)
        if days_to_div is not None and days_to_div <= SCANNER.dividend_blackout_days:
            return f"ex-dividend in {days_to_div}d, inside {SCANNER.dividend_blackout_days}d blackout"

        fnd_reject = self._fails_fundamentals(fundamentals)
        if fnd_reject:
            return fnd_reject

        return None

    # ---- main entry point ---------------------------------------------------

    def scan(self, symbols: Optional[list[str]] = None) -> list[ScanResult]:
        symbols = symbols or get_universe()
        results: list[ScanResult] = []

        for symbol in symbols:
            try:
                price_history = self.feed.history(symbol, period="1y", interval="1d")
            except Exception as e:
                log.warning("Scanner: skipping %s, data fetch failed: %s", symbol, e)
                continue

            avg_dollar_vol = float((price_history["Close"] * price_history["Volume"]).tail(20).mean())
            days_to_earn = self.feed.days_to_next_earnings(symbol)
            days_to_div = self.feed.days_to_ex_dividend(symbol)
            news_count = self.feed.recent_news_count(symbol, SCANNER.news_lookback_days)
            fnd = self.feed.fundamentals(symbol)
            technicals = self._compute_technicals(price_history)

            reject_reason = self._passes_filters(symbol, price_history, technicals, fnd)

            if reject_reason:
                results.append(ScanResult(
                    symbol=symbol, snapshot=None, avg_dollar_volume=avg_dollar_vol,
                    days_to_earnings=days_to_earn, days_to_ex_dividend=days_to_div,
                    recent_news_count=news_count, fundamentals=fnd, technicals=technicals,
                    score=-1.0, reject_reason=reject_reason,
                ))
                continue

            snapshot = self.classifier.classify(symbol)

            last_price = float(price_history["Close"].iloc[-1])
            if (snapshot.regime == Regime.HIGH_IV_RANGE
                    and last_price > SCANNER.max_underlying_price_for_premium_selling):
                results.append(ScanResult(
                    symbol=symbol, snapshot=snapshot, avg_dollar_volume=avg_dollar_vol,
                    days_to_earnings=days_to_earn, days_to_ex_dividend=days_to_div,
                    recent_news_count=news_count, fundamentals=fnd, technicals=technicals,
                    score=-1.0,
                    reject_reason=(
                        f"price ${last_price:.2f} exceeds ${SCANNER.max_underlying_price_for_premium_selling:.0f} "
                        f"cap for premium-selling candidates -- condor/strangle wings on this name would risk "
                        f"more per contract than the account's risk budget allows"
                    ),
                ))
                continue

            macro_tilt = 0.0
            if self.macro_engine is not None and snapshot.regime == Regime.TRENDING:
                direction = "long" if snapshot.ma_slope > 0 else "short"
                macro_tilt = self.macro_engine.directional_tilt(direction)

            score = self._score(snapshot, technicals, news_count, macro_tilt)
            results.append(ScanResult(
                symbol=symbol, snapshot=snapshot, avg_dollar_volume=avg_dollar_vol,
                days_to_earnings=days_to_earn, days_to_ex_dividend=days_to_div,
                recent_news_count=news_count, fundamentals=fnd, technicals=technicals,
                score=score,
                reject_reason=None if snapshot.regime in (Regime.HIGH_IV_RANGE, Regime.TRENDING)
                else f"regime {snapshot.regime.value} not tradeable",
            ))

        return results

    def top_candidates(self, symbols: Optional[list[str]] = None, limit: int = None) -> list[WatchlistItem]:
        limit = limit or SCANNER.max_equity_candidates
        results = self.scan(symbols)
        tradeable = sorted([r for r in results if r.passed], key=lambda r: r.score, reverse=True)

        rejected = [r for r in results if not r.passed]
        log.info(
            "Scan complete: %d/%d symbols tradeable. Top picks: %s",
            len(tradeable), len(results),
            ", ".join(f"{r.symbol}({r.score:.0f})" for r in tradeable[:limit]) or "none",
        )
        for r in rejected:
            log.debug("Scan rejected %s: %s", r.symbol, r.reject_reason)

        return [WatchlistItem(r.symbol, "equity_option") for r in tradeable[:limit]]
