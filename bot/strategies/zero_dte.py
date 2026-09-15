from __future__ import annotations
from typing import Optional
import pandas as pd

from bot.models import RegimeSnapshot, TradeSignal, StrategyType, Regime, OptionLegSpec
from bot.pricing import estimate_iv, price_option, strike_at_std_dev
from bot.indicators import atr_pct as compute_atr_pct
from bot.strategies.base import Strategy
from bot.market_hours import ZERO_DTE_SYMBOLS

DAYS_TO_EXP = 0
# Tighter than the 45-DTE condor's 1.0/0.5 -- there's far less time left for
# price to move, so strikes need to sit closer to spot to collect meaningful
# premium at all.
SHORT_STRIKE_STD_DEVS = 0.5
WING_WIDTH_STD_DEVS = 0.25


class ZeroDteIronCondorStrategy(Strategy):
    """Same-day-expiration iron condor for SPY/QQQ/IWM only (see
    bot.market_hours.ZERO_DTE_SYMBOLS), gated by the router to a narrow
    mid-morning entry window and closed unconditionally by
    bot.market_hours.ZERO_DTE_FORCE_CLOSE regardless of P&L -- see
    check_exits() in paper_broker.py/alpaca_broker.py.

    Known approximation, inherited from bot/pricing.py: price_option()
    floors days_to_expiration to t = max(days_to_exp, 1) / 365, so
    DAYS_TO_EXP = 0 is priced as a 1-day option, not literal same-day
    gamma. This matches the same documented-approximation pattern already
    used throughout the rest of the strategy layer -- not something to fix
    here, just something to know when reading the greeks/credit this
    strategy reports.
    """

    def generate_signal(self, snapshot: RegimeSnapshot, price_history: pd.DataFrame) -> Optional[TradeSignal]:
        if snapshot.regime != Regime.HIGH_IV_RANGE:
            return None
        if snapshot.symbol not in ZERO_DTE_SYMBOLS:
            return None

        spot = float(price_history["Close"].iloc[-1])
        vol = estimate_iv(price_history["Close"])

        short_call_k = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS, above=True)
        short_put_k = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS, above=False)
        long_call_k = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS + WING_WIDTH_STD_DEVS, above=True)
        long_put_k = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS + WING_WIDTH_STD_DEVS, above=False)

        short_call = price_option(spot, short_call_k, DAYS_TO_EXP, vol, is_call=True)
        long_call = price_option(spot, long_call_k, DAYS_TO_EXP, vol, is_call=True)
        short_put = price_option(spot, short_put_k, DAYS_TO_EXP, vol, is_call=False)
        long_put = price_option(spot, long_put_k, DAYS_TO_EXP, vol, is_call=False)

        credit = (short_call.price - long_call.price) + (short_put.price - long_put.price)
        wing_width = (long_call_k - short_call_k)
        max_loss = max((wing_width - credit) * 100, credit * 10)

        net_delta = (short_call.delta - long_call.delta) + (short_put.delta - long_put.delta)
        net_theta = (short_call.theta - long_call.theta) + (short_put.theta - long_put.theta)
        net_vega = (short_call.vega - long_call.vega) + (short_put.vega - long_put.vega)

        signal = TradeSignal(
            symbol=snapshot.symbol,
            strategy=StrategyType.ZERO_DTE_IRON_CONDOR,
            regime=snapshot.regime,
            direction="neutral",
            est_credit_or_risk=round(credit * 100, 2),
            est_max_loss=round(max_loss, 2),
            confidence=min(0.9, snapshot.iv_rank / 100 + 0.25),
            atr_pct=compute_atr_pct(price_history["High"], price_history["Low"], price_history["Close"]),
            days_to_expiration=DAYS_TO_EXP,
            option_legs=[
                OptionLegSpec(strike=short_call_k, is_call=True, side="sell"),
                OptionLegSpec(strike=long_call_k, is_call=True, side="buy"),
                OptionLegSpec(strike=short_put_k, is_call=False, side="sell"),
                OptionLegSpec(strike=long_put_k, is_call=False, side="buy"),
            ],
            rationale=(
                f"0DTE: IV rank {snapshot.iv_rank:.0f}, ADX {snapshot.adx:.0f}: same-day condor, "
                f"{credit*100:.0f} credit vs {max_loss:.0f} max loss/contract, forced close by 2:30pm ET."
            ),
        )
        signal.__dict__["_greeks_per_contract"] = (net_delta * 100, net_theta * 100, net_vega * 100)
        return signal
