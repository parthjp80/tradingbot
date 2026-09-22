"""
This is the module that actually makes the bot "manage risk and reward
based on market conditions" -- regime and strategy signals just propose
trades; RiskManager decides whether they're allowed and how big they can be.

Design principle (see the IBM strangle expectancy walkthrough): win rate is
an output, not a target. What we control directly are (a) how much capital
touches each trade, (b) how concentrated that capital gets, and (c) how big
losers are allowed to run before they're cut. All three are enforced here,
not left to the strategy layer.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from bot.config import ACCOUNT
from bot.models import TradeSignal, Position, Regime, StrategyType
from bot.sectors import get_sector
from bot.logger_setup import get_logger

log = get_logger(__name__)


@dataclass
class ExpectancyStats:
    wins: int = 0
    losses: int = 0
    total_win_pnl: float = 0.0
    total_loss_pnl: float = 0.0  # stored as positive magnitude

    @property
    def total_trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        n = self.total_trades
        return self.wins / n if n else 0.0

    @property
    def avg_win(self) -> float:
        return self.total_win_pnl / self.wins if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return self.total_loss_pnl / self.losses if self.losses else 0.0

    @property
    def expectancy_per_trade(self) -> float:
        return (self.win_rate * self.avg_win) - ((1 - self.win_rate) * self.avg_loss)

    def record(self, pnl: float) -> None:
        if pnl >= 0:
            self.wins += 1
            self.total_win_pnl += pnl
        else:
            self.losses += 1
            self.total_loss_pnl += abs(pnl)


def kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Standard Kelly formula: f* = W - (1-W)/R, where R = avg_win / avg_loss.
    Returns the fraction of capital to risk per trade. Can be negative
    (meaning the system has negative expectancy and shouldn't be sized up
    at all -- caller should floor at 0).
    """
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    r = avg_win / avg_loss
    f_star = win_rate - (1 - win_rate) / r
    return f_star


@dataclass
class DailyState:
    trading_day: date
    starting_equity: float
    realized_pnl_today: float = 0.0
    trades_today: int = 0


class CircuitBreakerTripped(Exception):
    pass


class RiskManager:
    def __init__(self, equity: float = ACCOUNT.starting_equity):
        self.equity = equity
        self.peak_equity = equity
        self.open_positions: list[Position] = []
        self.stats = ExpectancyStats()
        self.daily = DailyState(trading_day=date.today(), starting_equity=equity)
        self.trading_paused_for_drawdown = False
        self.consecutive_losses = 0
        self.cooldown_until: Optional[datetime] = None

    # ---- circuit breakers -------------------------------------------------

    def _roll_day_if_needed(self) -> None:
        today = date.today()
        if self.daily.trading_day != today:
            self.daily = DailyState(trading_day=today, starting_equity=self.equity)

    def check_circuit_breakers(self) -> None:
        self._roll_day_if_needed()

        drawdown = (self.peak_equity - self.equity) / self.peak_equity if self.peak_equity else 0.0
        if drawdown >= ACCOUNT.max_drawdown_pause_pct:
            self.trading_paused_for_drawdown = True

        if self.trading_paused_for_drawdown:
            raise CircuitBreakerTripped(
                f"Max drawdown breaker tripped ({drawdown:.1%} >= "
                f"{ACCOUNT.max_drawdown_pause_pct:.1%}). Trading halted until manually resumed."
            )

        daily_loss_pct = -self.daily.realized_pnl_today / self.daily.starting_equity
        if daily_loss_pct >= ACCOUNT.daily_loss_limit_pct:
            raise CircuitBreakerTripped(
                f"Daily loss limit tripped ({daily_loss_pct:.1%} >= "
                f"{ACCOUNT.daily_loss_limit_pct:.1%}). No new entries until tomorrow."
            )

        if len(self.open_positions) >= ACCOUNT.max_concurrent_positions:
            raise CircuitBreakerTripped(
                f"Max concurrent positions reached ({len(self.open_positions)})."
            )

        if self.cooldown_until and datetime.utcnow() < self.cooldown_until:
            raise CircuitBreakerTripped(
                f"Cooldown active after {ACCOUNT.consecutive_loss_cooldown_count} consecutive "
                f"losses -- no new entries until {self.cooldown_until.isoformat()} (prevents "
                f"revenge-trading into a losing streak)."
            )

    # ---- diversification / concentration -------------------------------------

    def _sector_counts(self) -> Counter:
        # 0DTE positions are excluded entirely -- they neither count toward
        # nor are blocked by the sector cap (see _within_sector_cap). SPY/
        # QQQ/IWM all map to the same "Broad Market ETF" sector, and with
        # the default cap of 2, two ordinary condors/verticals on any two of
        # them would otherwise starve 0DTE of the third. Portfolio-level
        # delta/theta/vega caps are the real backstop against correlated
        # concentration across these three, not this per-sector count.
        return Counter(
            get_sector(p.symbol) for p in self.open_positions
            if p.strategy != StrategyType.ZERO_DTE_IRON_CONDOR
        )

    def _within_sector_cap(self, signal: TradeSignal) -> bool:
        if signal.strategy == StrategyType.ZERO_DTE_IRON_CONDOR:
            return True
        sector = get_sector(signal.symbol)
        if sector == "UNKNOWN":
            return True  # unmapped names (e.g. custom universe) aren't gated
        current = self._sector_counts().get(sector, 0)
        if current >= ACCOUNT.max_positions_per_sector:
            log.warning(
                "Rejecting %s: sector '%s' already at cap (%d/%d positions)",
                signal.symbol, sector, current, ACCOUNT.max_positions_per_sector,
            )
            return False
        return True

    # ---- portfolio-level exposure caps -------------------------------------

    def _projected_greeks(self, extra_delta=0.0, extra_theta=0.0, extra_vega=0.0):
        net_delta = sum(p.delta_estimate for p in self.open_positions) + extra_delta
        net_theta = sum(p.theta_estimate for p in self.open_positions) + extra_theta
        net_vega = sum(p.vega_estimate for p in self.open_positions) + extra_vega
        return net_delta, net_theta, net_vega

    def _within_portfolio_caps(self, signal: TradeSignal, est_delta: float, est_theta: float, est_vega: float) -> bool:
        net_delta, net_theta, net_vega = self._projected_greeks(est_delta, est_theta, est_vega)

        if abs(net_delta) > ACCOUNT.max_net_delta_shares_equiv:
            log.warning("Rejecting %s: net delta %.1f exceeds cap", signal.symbol, net_delta)
            return False
        if abs(net_theta) > ACCOUNT.max_portfolio_theta_pct * self.equity:
            log.warning("Rejecting %s: net theta exceeds cap", signal.symbol)
            return False
        if abs(net_vega) > ACCOUNT.max_portfolio_vega_pct * self.equity:
            log.warning("Rejecting %s: net vega exceeds cap", signal.symbol)
            return False
        return True

    # ---- reward:risk gate ----------------------------------------------------

    def _within_reward_risk_minimum(self, signal: TradeSignal, stop_loss_multiple: float, profit_target_pct: float) -> bool:
        """
        Compares expected reward to the risk actually taken to the STOP-LOSS
        (not the full defined max loss -- the stop is what you're actually
        exposed to under normal management). For a credit strategy, risk to
        stop = (stop_loss_multiple - 1) * credit; reward = profit_target_pct * credit.
        """
        min_ratio = ACCOUNT.min_reward_risk_ratio.get(signal.strategy.value)
        if min_ratio is None:
            return True  # no configured minimum for this strategy type

        risk_to_stop = max(stop_loss_multiple - 1, 1e-6)
        ratio = profit_target_pct / risk_to_stop

        if ratio < min_ratio:
            log.warning(
                "Rejecting %s: reward:risk %.2f below minimum %.2f for %s",
                signal.symbol, ratio, min_ratio, signal.strategy.value,
            )
            return False
        return True

    # ---- position sizing ----------------------------------------------------

    def _risk_budget_fixed_fractional(self, signal: TradeSignal) -> float:
        return self.equity * ACCOUNT.max_risk_per_trade_pct * signal.confidence

    def _risk_budget_half_kelly(self, signal: TradeSignal) -> float:
        """
        Falls back to fixed-fractional until there's enough closed-trade
        history to estimate win rate / avg win / avg loss reliably --
        sizing off Kelly math with 5 trades of history is how you find out
        the hard way that Kelly assumes you actually know your edge.
        """
        if self.stats.total_trades < ACCOUNT.kelly_min_sample_size:
            return self._risk_budget_fixed_fractional(signal)

        f_star = kelly_fraction(self.stats.win_rate, self.stats.avg_win, self.stats.avg_loss)
        f_star = max(f_star, 0.0) * ACCOUNT.kelly_safety_fraction  # half-Kelly, floor at 0
        f_star = min(f_star, ACCOUNT.kelly_max_risk_per_trade_pct)  # hard ceiling regardless of math

        return self.equity * f_star * signal.confidence

    def size_position(self, signal: TradeSignal, macro_risk_off: bool = False) -> int:
        """
        Determines contract count from a risk budget (fixed-fractional or
        half-Kelly, see config) divided by per-contract defined risk, then
        applies further cuts:
          - CRISIS regime: cut sharply regardless of sizing method.
          - Elevated ATR%: cut further when the underlying is unusually
            volatile right now, on top of whatever the strategy's own
            strike/stop selection already priced in.
          - Macro risk-off (optional, prediction-market-derived): cut
            further when forward-looking event odds (e.g. recession
            probability) are elevated -- independent of and in addition to
            CRISIS, which only reacts to realized VIX moves. See
            bot/prediction_markets.py.
          - 0DTE: cut further regardless of the above, same-day gamma risk
            on top of whatever the strategy's own tighter strikes priced in.
          - A+ setup: boost size, hard-capped at aplus_max_risk_per_trade_pct
            of equity regardless of the multiplier -- applied last, after
            every cut above. Requires BOTH scanner_score >= aplus_score_threshold
            AND a clean 13/13 on bot/aplus_checklist.py's PREMIUM_CHECKLIST
            (aplus_checklist_passed) -- a hot scanner score alone no longer
            sizes up if the checklist itself misses something the score
            doesn't capture (event risk inside expiration, a coiled range,
            thin liquidity, ...).
        """
        if ACCOUNT.position_sizing_method == "half_kelly":
            risk_budget = self._risk_budget_half_kelly(signal)
        else:
            risk_budget = self._risk_budget_fixed_fractional(signal)

        if signal.regime == Regime.CRISIS:
            risk_budget *= 0.4  # cut size sharply in crisis regime

        if signal.atr_pct >= ACCOUNT.high_vol_atr_pct_threshold:
            risk_budget *= (1 - ACCOUNT.high_vol_size_cut_pct)

        if macro_risk_off:
            from bot.config import PREDICTION_MARKETS
            risk_budget *= (1 - PREDICTION_MARKETS.risk_off_size_cut_pct)

        if signal.strategy == StrategyType.ZERO_DTE_IRON_CONDOR:
            risk_budget *= (1 - ACCOUNT.zero_dte_size_cut_pct)

        if signal.scanner_score >= ACCOUNT.aplus_score_threshold and signal.aplus_checklist_passed:
            risk_budget *= (1 + ACCOUNT.aplus_size_boost_pct)
            risk_budget = min(risk_budget, self.equity * ACCOUNT.aplus_max_risk_per_trade_pct)

        per_contract_risk = max(signal.est_max_loss, 1e-6)
        contracts = int(risk_budget // per_contract_risk)
        return max(contracts, 0)

    # ---- entry gate ----------------------------------------------------------

    def evaluate_signal(
        self,
        signal: TradeSignal,
        stop_loss_multiple: float,
        profit_target_pct: float,
        est_delta_per_contract: float = 0.0,
        est_theta_per_contract: float = 0.0,
        est_vega_per_contract: float = 0.0,
        macro_risk_off: bool = False,
    ) -> Optional[int]:
        """
        Returns the approved contract count, or None if the trade is rejected.
        Raises CircuitBreakerTripped if the account-level breakers are hit
        (caller should stop generating new entries for the cycle).
        """
        self.check_circuit_breakers()

        if not self._within_sector_cap(signal):
            return None

        if not self._within_reward_risk_minimum(signal, stop_loss_multiple, profit_target_pct):
            return None

        contracts = self.size_position(signal, macro_risk_off=macro_risk_off)
        if contracts <= 0:
            log.info("Signal for %s sized to 0 contracts, skipping.", signal.symbol)
            return None

        if not self._within_portfolio_caps(
            signal,
            est_delta_per_contract * contracts,
            est_theta_per_contract * contracts,
            est_vega_per_contract * contracts,
        ):
            return None

        log.info(
            "Approved %s x%d contracts (%s, regime=%s, confidence=%.2f, sizing=%s)",
            signal.symbol, contracts, signal.strategy.value, signal.regime.value,
            signal.confidence, ACCOUNT.position_sizing_method,
        )
        return contracts

    # ---- lifecycle hooks -------------------------------------------------

    def register_open(self, position: Position) -> None:
        self.open_positions.append(position)
        self.daily.trades_today += 1

    def discard_unfilled(self, position: Position) -> None:
        """
        Drops a position whose entry order Alpaca has terminally rejected,
        expired, or canceled without ever filling -- no equity/P&L/win-rate
        impact since no real trade happened, just freeing the concurrent-
        position slot it was wrongly holding.
        """
        if position in self.open_positions:
            self.open_positions.remove(position)

    def register_close(self, position: Position, realized_pnl: float) -> None:
        if position in self.open_positions:
            self.open_positions.remove(position)
        self.equity += realized_pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        self.daily.realized_pnl_today += realized_pnl
        self.stats.record(realized_pnl)

        if realized_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= ACCOUNT.consecutive_loss_cooldown_count:
                self.cooldown_until = datetime.utcnow() + timedelta(hours=ACCOUNT.consecutive_loss_cooldown_hours)
                log.warning(
                    "%d consecutive losses -- cooldown active until %s (no new entries)",
                    self.consecutive_losses, self.cooldown_until.isoformat(),
                )
        else:
            self.consecutive_losses = 0
            self.cooldown_until = None

        log.info(
            "Closed %s pnl=%.2f | equity=%.2f | expectancy/trade=%.2f (win_rate=%.1f%%) | consecutive_losses=%d",
            position.symbol, realized_pnl, self.equity,
            self.stats.expectancy_per_trade, self.stats.win_rate * 100, self.consecutive_losses,
        )
