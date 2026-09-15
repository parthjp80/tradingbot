from __future__ import annotations
from datetime import datetime
from typing import Optional
import pandas as pd

from bot.models import RegimeSnapshot, TradeSignal, Regime
from bot.strategies.premium_selling import IronCondorStrategy
from bot.strategies.directional import ShortVerticalStrategy, FuturesTrendStrategy
from bot.strategies.zero_dte import ZeroDteIronCondorStrategy
from bot.market_hours import EASTERN, ZERO_DTE_SYMBOLS, is_0dte_entry_window
from bot.logger_setup import get_logger

log = get_logger(__name__)


class StrategyRouter:
    """
    Central dispatch: no strategy trades outside its designated regime, and
    CRISIS never generates a new-risk signal from this router at all --
    that regime is handled purely by the risk manager (size cuts, and the
    orchestrator's flatten-on-crisis logic).
    """

    def __init__(self):
        self.iron_condor = IronCondorStrategy()
        self.short_vertical = ShortVerticalStrategy()
        self.futures_trend = FuturesTrendStrategy()
        self.zero_dte_condor = ZeroDteIronCondorStrategy()

    def route(
        self,
        snapshot: RegimeSnapshot,
        asset_class: str,
        price_history: pd.DataFrame,
        now: datetime | None = None,
    ) -> Optional[TradeSignal]:
        if snapshot.regime == Regime.CRISIS:
            log.info("%s in CRISIS regime: no new entries.", snapshot.symbol)
            return None

        if snapshot.regime == Regime.LOW_IV_RANGE:
            log.info("%s in LOW_IV_RANGE: premium too cheap to sell, no signal.", snapshot.symbol)
            return None

        if asset_class == "future":
            return self.futures_trend.generate_signal(snapshot, price_history)

        # equity_option asset class
        if snapshot.regime == Regime.HIGH_IV_RANGE:
            now = now or datetime.now(EASTERN)
            if snapshot.symbol in ZERO_DTE_SYMBOLS and is_0dte_entry_window(now):
                return self.zero_dte_condor.generate_signal(snapshot, price_history)
            return self.iron_condor.generate_signal(snapshot, price_history)
        if snapshot.regime == Regime.TRENDING:
            return self.short_vertical.generate_signal(snapshot, price_history)

        return None
