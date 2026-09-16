from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Regime(str, Enum):
    LOW_IV_RANGE = "low_iv_range"          # cheap premium, chop -> avoid selling premium
    HIGH_IV_RANGE = "high_iv_range"        # rich premium, chop -> sell strangles/condors
    TRENDING = "trending"                  # strong directional move -> directional/verticals only
    CRISIS = "crisis"                      # vol spike / VIX pop -> flatten, reduce size, no new risk


@dataclass
class RegimeSnapshot:
    symbol: str
    regime: Regime
    iv_rank: float
    adx: float
    ma_slope: float
    vix_level: float
    vix_jump_1d_pct: float
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["regime"] = self.regime.value
        d["timestamp"] = self.timestamp.isoformat()
        return d


class StrategyType(str, Enum):
    SHORT_STRANGLE = "short_strangle"
    IRON_CONDOR = "iron_condor"
    SHORT_PUT_VERTICAL = "short_put_vertical"
    SHORT_CALL_VERTICAL = "short_call_vertical"
    FUTURES_TREND = "futures_trend"
    ZERO_DTE_IRON_CONDOR = "zero_dte_iron_condor"
    NO_TRADE = "no_trade"


@dataclass
class OptionLegSpec:
    """A theoretical leg from the strategy layer (strike/type/side) before
    it's matched to a real listed contract. AlpacaBroker resolves these to
    actual tradeable option symbols; PaperBroker ignores them (it prices
    off Black-Scholes directly and doesn't need a real contract to exist)."""
    strike: float
    is_call: bool
    side: str  # "buy" | "sell"


@dataclass
class TradeSignal:
    symbol: str
    strategy: StrategyType
    regime: Regime
    # For options: short strike distances expressed in standard deviations
    # from spot, derived from IV. For futures: direction only.
    direction: str  # "neutral" | "long" | "short"
    est_credit_or_risk: float  # estimated credit received (options) or $ risk unit (futures)
    est_max_loss: float
    confidence: float  # 0-1, used to scale size within the risk cap
    rationale: str
    atr_pct: float = 0.0  # ATR as % of price at signal time -- used for volatility-adjusted sizing
    option_legs: list = field(default_factory=list)  # list[OptionLegSpec], empty for futures signals
    days_to_expiration: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    # The scanner's own 0-100 ranking score for this candidate (bot/scanner.py),
    # threaded through so risk_manager.size_position() can size up on
    # exceptionally strong setups. 0.0 for signals that bypassed the scanner
    # (e.g. static_futures) -- never triggers the size boost, which is correct,
    # those never got the scanner's quality read in the first place.
    scanner_score: float = 0.0
    # Result of bot/aplus_checklist.py's PREMIUM_CHECKLIST, evaluated in
    # Orchestrator for premium-selling strategies only (see
    # aplus_checklist.CHECKLIST_STRATEGIES). False/empty for every other
    # signal -- never triggers the size boost, same reasoning as
    # scanner_score above. A clean 13/13 is required; there's no automated
    # stand-in for "one miss with a good reason".
    aplus_checklist_passed: bool = False
    aplus_checklist_missed: list = field(default_factory=list)


@dataclass
class Position:
    id: str
    symbol: str
    strategy: StrategyType
    opened_at: datetime
    entry_credit_or_debit: float
    max_loss: float
    contracts: int
    stop_loss_level: float     # expressed as multiple of credit received (e.g., 1.5x)
    profit_target_pct: float   # close at this % of max profit captured (e.g., 0.5 = 50%)
    delta_estimate: float = 0.0
    theta_estimate: float = 0.0
    vega_estimate: float = 0.0
    status: str = "open"
    closed_at: Optional[datetime] = None
    realized_pnl: Optional[float] = None
    high_water_mark_pct: float = 0.0     # best profit-captured % seen so far, for trailing-stop logic
    trailing_stop_active: bool = False   # once true, stop has been ratcheted to protect captured profit

    # --- journaling: strategy/rationale + technical snapshot at entry ---
    # ("Log basic trade details" + "Include strategy and rationale" +
    # mark-up-charts equivalent, captured as numbers since there's no chart
    # to screenshot for an automated system)
    direction: str = "neutral"
    regime_at_entry: str = ""
    rationale: str = ""
    confidence: float = 0.0
    entry_spot_price: float = 0.0
    adx_at_entry: float = 0.0
    iv_rank_at_entry: float = 0.0
    atr_pct_at_entry: float = 0.0
    scanner_score_at_entry: float = 0.0
    planned_stop_loss_level: float = 0.0
    planned_profit_target_pct: float = 0.0

    # --- journaling: system-state at entry ---
    # A bot has no emotional state, but it has an analogous set of
    # decision-time conditions worth reviewing for drift/bias the same way
    # a human reviews their psychological state: what sizing regime was
    # active, and was this entry taken right after a losing streak.
    sizing_method_at_entry: str = ""
    consecutive_losses_at_entry: int = 0

    # --- broker linkage (only populated when using a real broker backend) ---
    broker_name: str = "internal_simulator"
    broker_order_id: Optional[str] = None
    broker_leg_symbols: list = field(default_factory=list)  # real OCC option symbols, if applicable
    # AlpacaBroker only: whether the opening order has a confirmed fill.
    # Multi-leg orders can fill asynchronously (or not at all outside
    # market hours), and Alpaca rejects a close order against a position
    # that was never actually filled -- check_exits() must skip a position
    # until this is True. Always True for internal_simulator (fills are
    # synthetic/instant there, so the field is meaningless for that path).
    broker_fill_confirmed: bool = True

    # 0DTE only: naive-UTC wall-clock time by which this position must be
    # force-closed regardless of P&L (see bot/market_hours.py). None for
    # every other strategy -- no behavior change to them.
    force_close_by: Optional[datetime] = None
