from __future__ import annotations
import pandas as pd

from bot.config import REGIME
from bot.data_feed import DataFeed
from bot.indicators import adx, ma_slope, iv_rank_proxy, vix_jump_pct
from bot.models import Regime, RegimeSnapshot
from bot.logger_setup import get_logger

log = get_logger(__name__)


class RegimeClassifier:
    def __init__(self, feed: DataFeed):
        self.feed = feed

    def classify(self, symbol: str) -> RegimeSnapshot:
        df = self.feed.history(symbol, period="1y", interval="1d")
        vix_df = self.feed.vix(period="1y")

        adx_series = adx(df["High"], df["Low"], df["Close"])
        current_adx = float(adx_series.dropna().iloc[-1]) if not adx_series.dropna().empty else 0.0

        slope = ma_slope(df["Close"])
        ivr = iv_rank_proxy(df["Close"])
        vix_level = float(vix_df["Close"].iloc[-1])
        vix_jump = vix_jump_pct(vix_df["Close"])

        regime = self._classify_from_metrics(current_adx, ivr, vix_level, vix_jump)

        snap = RegimeSnapshot(
            symbol=symbol,
            regime=regime,
            iv_rank=round(ivr, 1),
            adx=round(current_adx, 1),
            ma_slope=round(slope, 4),
            vix_level=round(vix_level, 2),
            vix_jump_1d_pct=round(vix_jump, 4),
        )
        log.info(
            "Regime %s -> %s (ADX=%.1f IVR=%.1f VIX=%.1f slope=%.4f)",
            symbol, regime.value, current_adx, ivr, vix_level, slope,
        )
        return snap

    @staticmethod
    def _classify_from_metrics(adx_val: float, iv_rank: float, vix_level: float, vix_jump: float) -> Regime:
        # Crisis overrides everything -- capital preservation first.
        if vix_level >= REGIME.vix_crisis or vix_jump >= REGIME.vix_jump_pct_1d:
            return Regime.CRISIS

        if adx_val >= REGIME.adx_trend:
            return Regime.TRENDING

        # Range-bound (low ADX): split on IV rank to decide premium-selling appetite
        if iv_rank >= REGIME.iv_rank_high:
            return Regime.HIGH_IV_RANGE

        if iv_rank <= REGIME.iv_rank_low:
            return Regime.LOW_IV_RANGE

        # Middle IV rank, low ADX: default to the conservative bucket
        return Regime.LOW_IV_RANGE
