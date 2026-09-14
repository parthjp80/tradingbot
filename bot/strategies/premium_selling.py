from __future__ import annotations
from typing import Optional
import pandas as pd

from bot.models import RegimeSnapshot, TradeSignal, StrategyType, Regime, OptionLegSpec
from bot.pricing import estimate_iv, price_option, strike_at_std_dev
from bot.indicators import atr_pct as compute_atr_pct
from bot.strategies.base import Strategy

DAYS_TO_EXP = 45           # typical premium-selling DTE target
SHORT_STRIKE_STD_DEVS = 1.0  # ~1 SD short strikes -> roughly 68% probability OTM
WING_WIDTH_STD_DEVS = 0.5   # extra distance for the long wings on an iron condor


class ShortStrangleStrategy(Strategy):
    """Naked short strangle: appropriate in HIGH_IV_RANGE only, and only
    when the account can absorb undefined-ish risk (RiskManager still caps
    size using est_max_loss, treated conservatively below)."""

    def generate_signal(self, snapshot: RegimeSnapshot, price_history: pd.DataFrame) -> Optional[TradeSignal]:
        if snapshot.regime != Regime.HIGH_IV_RANGE:
            return None

        spot = float(price_history["Close"].iloc[-1])
        vol = estimate_iv(price_history["Close"])

        call_strike = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS, above=True)
        put_strike = strike_at_std_dev(spot, vol, DAYS_TO_EXP, SHORT_STRIKE_STD_DEVS, above=False)

        call_leg = price_option(spot, call_strike, DAYS_TO_EXP, vol, is_call=True)
        put_leg = price_option(spot, put_strike, DAYS_TO_EXP, vol, is_call=False)

        credit = call_leg.price + put_leg.price
        # Undefined-risk strategy: treat max loss conservatively as a multiple
        # of credit for sizing purposes (this is intentionally punitive vs.
        # a defined-risk condor, which is the point -- see IBM expectancy note).
        conservative_max_loss = credit * 4 * 100  # per 100-share contract

        return TradeSignal(
            symbol=snapshot.symbol,
            strategy=StrategyType.SHORT_STRANGLE,
            regime=snapshot.regime,
            direction="neutral",
            est_credit_or_risk=round(credit * 100, 2),
            est_max_loss=round(conservative_max_loss, 2),
            confidence=min(0.9, snapshot.iv_rank / 100 + 0.2),
            atr_pct=compute_atr_pct(price_history["High"], price_history["Low"], price_history["Close"]),
            days_to_expiration=DAYS_TO_EXP,
            option_legs=[
                OptionLegSpec(strike=call_strike, is_call=True, side="sell"),
                OptionLegSpec(strike=put_strike, is_call=False, side="sell"),
            ],
            rationale=(
                f"IV rank {snapshot.iv_rank:.0f} in range-bound tape (ADX {snapshot.adx:.0f}); "
                f"selling {SHORT_STRIKE_STD_DEVS:.1f}SD strangle for {credit*100:.0f} credit/contract."
            ),
        )


class IronCondorStrategy(Strategy):
    """Defined-risk version of the same idea -- generally preferred over the
    naked strangle since it caps the tail loss that killed expectancy in
    the unmanaged-risk example."""

    def generate_signal(self, snapshot: RegimeSnapshot, price_history: pd.DataFrame) -> Optional[TradeSignal]:
        if snapshot.regime != Regime.HIGH_IV_RANGE:
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
        wing_width = (long_call_k - short_call_k)  # symmetric wing width in $
        max_loss = max((wing_width - credit) * 100, credit * 10)  # per contract, floor for sanity

        net_delta = (short_call.delta - long_call.delta) + (short_put.delta - long_put.delta)
        net_theta = (short_call.theta - long_call.theta) + (short_put.theta - long_put.theta)
        net_vega = (short_call.vega - long_call.vega) + (short_put.vega - long_put.vega)

        signal = TradeSignal(
            symbol=snapshot.symbol,
            strategy=StrategyType.IRON_CONDOR,
            regime=snapshot.regime,
            direction="neutral",
            est_credit_or_risk=round(credit * 100, 2),
            est_max_loss=round(max_loss, 2),
            confidence=min(0.95, snapshot.iv_rank / 100 + 0.3),
            atr_pct=compute_atr_pct(price_history["High"], price_history["Low"], price_history["Close"]),
            days_to_expiration=DAYS_TO_EXP,
            option_legs=[
                OptionLegSpec(strike=short_call_k, is_call=True, side="sell"),
                OptionLegSpec(strike=long_call_k, is_call=True, side="buy"),
                OptionLegSpec(strike=short_put_k, is_call=False, side="sell"),
                OptionLegSpec(strike=long_put_k, is_call=False, side="buy"),
            ],
            rationale=(
                f"IV rank {snapshot.iv_rank:.0f}, ADX {snapshot.adx:.0f}: defined-risk condor, "
                f"{credit*100:.0f} credit vs {max_loss:.0f} max loss/contract."
            ),
        )
        # stash greeks for the caller (portfolio/, risk_manager caps) without
        # overloading the TradeSignal schema -- see strategies/router.py usage
        signal.__dict__["_greeks_per_contract"] = (net_delta * 100, net_theta * 100, net_vega * 100)
        return signal
