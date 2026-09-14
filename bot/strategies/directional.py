from __future__ import annotations
from typing import Optional
import pandas as pd

from bot.models import RegimeSnapshot, TradeSignal, StrategyType, Regime, OptionLegSpec
from bot.pricing import estimate_iv, price_option, strike_at_std_dev
from bot.indicators import atr_pct as compute_atr_pct
from bot.strategies.base import Strategy

DAYS_TO_EXP = 30
SHORT_STRIKE_STD_DEVS = 0.75
WING_WIDTH_STD_DEVS = 0.5


class ShortVerticalStrategy(Strategy):
    """
    In a TRENDING regime we don't sell naked premium against the trend --
    instead sell a defined-risk vertical WITH the trend direction (credit
    spread), which still benefits from elevated IV but caps risk tightly
    and doesn't fight the tape.
    """

    def generate_signal(self, snapshot: RegimeSnapshot, price_history: pd.DataFrame) -> Optional[TradeSignal]:
        if snapshot.regime != Regime.TRENDING:
            return None

        spot = float(price_history["Close"].iloc[-1])
        vol = estimate_iv(price_history["Close"])
        trend_up = snapshot.ma_slope > 0

        if trend_up:
            # sell a put credit spread (bullish, defined risk)
            short_k = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS, above=False)
            long_k = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS + WING_WIDTH_STD_DEVS, above=False)
            short_leg = price_option(spot, short_k, DAYS_TO_EXP, vol, is_call=False)
            long_leg = price_option(spot, long_k, DAYS_TO_EXP, vol, is_call=False)
            strategy_type = StrategyType.SHORT_PUT_VERTICAL
            width = short_k - long_k
        else:
            short_k = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS, above=True)
            long_k = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS + WING_WIDTH_STD_DEVS, above=True)
            short_leg = price_option(spot, short_k, DAYS_TO_EXP, vol, is_call=True)
            long_leg = price_option(spot, long_k, DAYS_TO_EXP, vol, is_call=True)
            strategy_type = StrategyType.SHORT_CALL_VERTICAL
            width = long_k - short_k

        credit = short_leg.price - long_leg.price
        max_loss = max((width - credit) * 100, credit * 10)
        is_call_leg = not trend_up

        signal = TradeSignal(
            symbol=snapshot.symbol,
            strategy=strategy_type,
            regime=snapshot.regime,
            direction="long" if trend_up else "short",
            est_credit_or_risk=round(credit * 100, 2),
            est_max_loss=round(max_loss, 2),
            confidence=min(0.85, abs(snapshot.ma_slope) * 5 + 0.3),
            atr_pct=compute_atr_pct(price_history["High"], price_history["Low"], price_history["Close"]),
            days_to_expiration=DAYS_TO_EXP,
            option_legs=[
                OptionLegSpec(strike=short_k, is_call=is_call_leg, side="sell"),
                OptionLegSpec(strike=long_k, is_call=is_call_leg, side="buy"),
            ],
            rationale=(
                f"ADX {snapshot.adx:.0f} confirms trend ({'up' if trend_up else 'down'}, "
                f"slope {snapshot.ma_slope:.3f}); selling with-trend vertical, "
                f"{credit*100:.0f} credit vs {max_loss:.0f} max loss."
            ),
        )
        net_delta = (short_leg.delta - long_leg.delta) * 100
        net_theta = (short_leg.theta - long_leg.theta) * 100
        net_vega = (short_leg.vega - long_leg.vega) * 100
        signal.__dict__["_greeks_per_contract"] = (net_delta, net_theta, net_vega)
        return signal


class FuturesTrendStrategy(Strategy):
    """
    Simple trend-following overlay for ES/MES: trade WITH an ADX-confirmed
    trend, sized in $ risk per contract via ATR (not options greeks, since
    futures carry none). Stays flat outside TRENDING regime and goes flat
    entirely in CRISIS (handled by the router, not here).
    """

    ATR_STOP_MULTIPLE = 2.0

    def generate_signal(self, snapshot: RegimeSnapshot, price_history: pd.DataFrame) -> Optional[TradeSignal]:
        if snapshot.regime != Regime.TRENDING:
            return None

        from bot.indicators import atr as atr_fn
        atr_series = atr_fn(price_history["High"], price_history["Low"], price_history["Close"])
        current_atr = float(atr_series.dropna().iloc[-1]) if not atr_series.dropna().empty else 0.0
        if current_atr <= 0:
            return None

        direction = "long" if snapshot.ma_slope > 0 else "short"
        # $ risk per contract if stopped out at ATR_STOP_MULTIPLE * ATR away,
        # multiplier applied by the caller (config.WatchlistItem.multiplier)
        risk_per_contract_points = current_atr * self.ATR_STOP_MULTIPLE

        return TradeSignal(
            symbol=snapshot.symbol,
            strategy=StrategyType.FUTURES_TREND,
            regime=snapshot.regime,
            direction=direction,
            est_credit_or_risk=0.0,
            est_max_loss=risk_per_contract_points,  # caller multiplies by contract multiplier
            confidence=min(0.85, snapshot.adx / 50),
            atr_pct=compute_atr_pct(price_history["High"], price_history["Low"], price_history["Close"]),
            rationale=(
                f"ADX {snapshot.adx:.0f} trend confirmed ({direction}); "
                f"ATR stop at {self.ATR_STOP_MULTIPLE}x ATR ({risk_per_contract_points:.2f} pts)."
            ),
        )
