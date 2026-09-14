from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
import pandas as pd

from bot.models import RegimeSnapshot, TradeSignal


class Strategy(ABC):
    """
    A strategy consumes a regime snapshot + recent price history and either
    proposes a TradeSignal or returns None (no edge / not applicable to this
    regime). RiskManager is the one that decides sizing and final approval --
    strategies should NOT size positions themselves.
    """

    @abstractmethod
    def generate_signal(self, snapshot: RegimeSnapshot, price_history: pd.DataFrame) -> Optional[TradeSignal]:
        raise NotImplementedError
