"""
Turns a handful of Kalshi prediction-market series into two things the
rest of the bot actually uses:

  1. A macro "risk off" flag (bot/risk_manager.py) -- if the market-implied
     probability of a recession this year crosses a threshold, cut position
     size further, independent of and in addition to the existing
     technical CRISIS regime (which reacts to realized VIX moves, not
     forward-looking event odds).

  2. A small directional confidence tilt (bot/scanner.py + orchestrator.py)
     -- nudges confidence/ranking for directional (TRENDING-regime) signals
     based on whether their direction agrees with the macro-implied bias
     (currently: Fed rate cut odds, treated as bullish for risk assets when
     elevated).

IMPORTANT CAVEAT ON INTERPRETATION: "rate cut odds = bullish" is a
simplifying assumption, not a fact -- rate cuts sometimes happen BECAUSE
of bad economic news, which is not bullish at all. This is exactly why
`bullish_when_yes` is a per-series config flag you can flip
(config.py -> PREDICTION_MARKETS.tracked_series), not a hardcoded belief.
Treat this whole module as one more noisy input, not a forecast.

SCOPE LIMIT: Kalshi does not have prediction markets for arbitrary
individual stocks (no "will AAPL beat earnings" contract for most names).
So despite the name, this can't give you a name-specific edge the way a
merger-arb or event contract sometimes can for a handful of large caps --
it's a market-wide macro signal, applied uniformly across whatever
directional signals the bot generates. Don't mistake "influences individual
stock selection" for "predicts individual stocks."
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from bot.config import PREDICTION_MARKETS, TrackedKalshiSeries
from bot.kalshi_client import KalshiClient
from bot.logger_setup import get_logger

log = get_logger(__name__)


@dataclass
class PredictionMarketSignal:
    series_ticker: str
    label: str
    yes_probability: float
    bullish_when_yes: bool
    is_risk_flag: bool
    fetched_at: datetime


class MacroSentimentEngine:
    def __init__(self, client: Optional[KalshiClient] = None):
        self.client = client or KalshiClient()
        self._cache: dict = {}

    def _is_cache_stale(self, series_ticker: str) -> bool:
        cached = self._cache.get(series_ticker)
        if cached is None:
            return True
        age = datetime.utcnow() - cached.fetched_at
        return age > timedelta(minutes=PREDICTION_MARKETS.cache_ttl_minutes)

    def _fetch_series_probability(self, series: TrackedKalshiSeries) -> Optional[float]:
        events = self.client.get_events(series.series_ticker, status="open", limit=5)
        if not events:
            log.warning("Kalshi: no open events found for series %s (%s)", series.series_ticker, series.label)
            return None

        event_ticker = events[0].get("event_ticker")
        markets = self.client.get_markets(event_ticker=event_ticker, status="open", limit=5)
        if not markets:
            log.warning("Kalshi: no open markets found for event %s", event_ticker)
            return None

        market = markets[0]
        yes_bid = market.get("yes_bid")
        yes_ask = market.get("yes_ask")
        if yes_bid is None or yes_ask is None:
            return None

        return ((yes_bid + yes_ask) / 2) / 100.0

    def get_signal(self, series: TrackedKalshiSeries) -> Optional[PredictionMarketSignal]:
        if not self._is_cache_stale(series.series_ticker):
            return self._cache[series.series_ticker]

        prob = self._fetch_series_probability(series)
        if prob is None:
            return self._cache.get(series.series_ticker)

        signal = PredictionMarketSignal(
            series_ticker=series.series_ticker,
            label=series.label,
            yes_probability=prob,
            bullish_when_yes=series.bullish_when_yes,
            is_risk_flag=series.is_risk_flag,
            fetched_at=datetime.utcnow(),
        )
        self._cache[series.series_ticker] = signal
        log.info("Kalshi: %s -> %.0f%% (YES)", series.label, prob * 100)
        return signal

    def get_all_signals(self) -> list:
        signals = []
        for series in PREDICTION_MARKETS.tracked_series:
            sig = self.get_signal(series)
            if sig is not None:
                signals.append(sig)
        return signals

    def macro_risk_off(self) -> bool:
        """True if any is_risk_flag series (e.g. recession odds) is above
        its configured threshold. Consumed by risk_manager.py to cut
        position size further, independent of the technical CRISIS regime."""
        for sig in self.get_all_signals():
            if sig.is_risk_flag and sig.yes_probability >= PREDICTION_MARKETS.recession_risk_off_threshold:
                log.warning(
                    "Macro risk-off: %s at %.0f%% (>= %.0f%% threshold)",
                    sig.label, sig.yes_probability * 100, PREDICTION_MARKETS.recession_risk_off_threshold * 100,
                )
                return True
        return False

    def directional_tilt(self, direction: str) -> float:
        """
        Returns a small confidence adjustment in
        [-directional_tilt_max_confidence_delta, +directional_tilt_max_confidence_delta]
        for a signal with the given direction ("long" or "short"), based on
        how far non-risk-flag series lean from a neutral 50%.
        """
        if direction not in ("long", "short"):
            return 0.0

        tilts = []
        for sig in self.get_all_signals():
            if sig.is_risk_flag:
                continue
            lean = (sig.yes_probability - 0.5) * 2
            bullish_lean = lean if sig.bullish_when_yes else -lean
            tilts.append(bullish_lean)

        if not tilts:
            return 0.0

        avg_bullish_lean = sum(tilts) / len(tilts)
        raw_tilt = avg_bullish_lean * PREDICTION_MARKETS.directional_tilt_max_confidence_delta
        signed_tilt = raw_tilt if direction == "long" else -raw_tilt
        return max(-PREDICTION_MARKETS.directional_tilt_max_confidence_delta,
                   min(PREDICTION_MARKETS.directional_tilt_max_confidence_delta, signed_tilt))
