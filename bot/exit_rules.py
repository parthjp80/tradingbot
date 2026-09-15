"""
Exit rules are set at entry time and vary by regime -- this is the other
half of "adapts to market conditions" beyond just entry selection. The
IBM-strangle expectancy math showed the whole edge lived in cutting losers
before they ran to 3x credit; these defaults encode that lesson.
"""
from __future__ import annotations
from bot.models import Regime, StrategyType


def get_exit_rules(regime: Regime, strategy: StrategyType) -> tuple[float, float]:
    """
    Returns (stop_loss_multiple_of_credit, profit_target_pct_of_max_profit).
    """
    if strategy == StrategyType.SHORT_STRANGLE:
        # undefined risk -> tightest stop
        return (1.5, 0.50)

    if strategy == StrategyType.IRON_CONDOR:
        # defined risk cushions the tail, can let it run a bit more
        base_stop = 2.0
        if regime == Regime.CRISIS:
            base_stop = 1.25  # shouldn't fire in crisis (router blocks new entries),
            # but existing positions get tightened if regime flips post-entry
        return (base_stop, 0.50)

    if strategy in (StrategyType.SHORT_PUT_VERTICAL, StrategyType.SHORT_CALL_VERTICAL):
        return (1.75, 0.60)  # trending trades: take profit sooner, trend can reverse

    if strategy == StrategyType.ZERO_DTE_IRON_CONDOR:
        # tighter than the 45-DTE condor on both sides: theta decay is much
        # faster same-day, so there's less reason to let a loser run to 2x
        # credit or hold out for the full 50% target. The force-close-by-time
        # check in check_exits() is the real backstop regardless of these.
        return (1.3, 0.40)

    if strategy == StrategyType.FUTURES_TREND:
        # ATR-based stop is already baked into est_max_loss; target is a
        # 2:1 reward:risk on the same ATR unit
        return (1.0, 1.0)  # interpreted specially by the futures execution path

    return (2.0, 0.50)
