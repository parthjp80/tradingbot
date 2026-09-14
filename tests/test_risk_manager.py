from bot.risk_manager import RiskManager, ExpectancyStats, CircuitBreakerTripped, kelly_fraction
from bot.models import TradeSignal, StrategyType, Regime, Position
from bot.config import ACCOUNT
from datetime import datetime
import pytest


def test_expectancy_matches_manual_example():
    stats = ExpectancyStats()
    # 70% win rate, avg win 75, avg loss 300 -> negative expectancy (unmanaged tail)
    for _ in range(7):
        stats.record(75)
    for _ in range(3):
        stats.record(-300)
    assert stats.win_rate == pytest.approx(0.7)
    assert stats.expectancy_per_trade == pytest.approx((0.7 * 75) - (0.3 * 300), abs=0.01)
    assert stats.expectancy_per_trade < 0


def test_expectancy_flips_positive_with_tighter_stop():
    stats = ExpectancyStats()
    for _ in range(7):
        stats.record(75)
    for _ in range(3):
        stats.record(-150)  # cut losers earlier
    assert stats.expectancy_per_trade == pytest.approx((0.7 * 75) - (0.3 * 150), abs=0.01)
    assert stats.expectancy_per_trade > 0


def test_position_sizing_respects_risk_cap():
    rm = RiskManager(equity=100_000)
    signal = TradeSignal(
        symbol="TEST", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
        direction="neutral", est_credit_or_risk=200, est_max_loss=800, confidence=1.0,
        rationale="test",
    )
    contracts = rm.size_position(signal)
    max_allowed_risk = 100_000 * ACCOUNT.max_risk_per_trade_pct
    assert contracts * 800 <= max_allowed_risk + 800  # within one contract's tolerance


def test_daily_loss_limit_trips_breaker():
    rm = RiskManager(equity=100_000)
    rm.daily.starting_equity = 100_000
    rm.daily.realized_pnl_today = -3500  # exceeds 3% default limit
    with pytest.raises(CircuitBreakerTripped):
        rm.check_circuit_breakers()


def test_max_drawdown_pauses_trading_until_manual_resume():
    rm = RiskManager(equity=100_000)
    rm.peak_equity = 100_000
    rm.equity = 84_000  # 16% drawdown, exceeds 15% default
    with pytest.raises(CircuitBreakerTripped):
        rm.check_circuit_breakers()
    assert rm.trading_paused_for_drawdown is True
    # even if equity recovers, it stays paused until manually reset
    rm.equity = 99_000
    with pytest.raises(CircuitBreakerTripped):
        rm.check_circuit_breakers()


def test_crisis_regime_cuts_size():
    rm = RiskManager(equity=100_000)
    normal_signal = TradeSignal(
        symbol="TEST", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
        direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
    )
    crisis_signal = TradeSignal(
        symbol="TEST", strategy=StrategyType.IRON_CONDOR, regime=Regime.CRISIS,
        direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
    )
    assert rm.size_position(crisis_signal) < rm.size_position(normal_signal)


# ---- diversification / sector cap ----------------------------------------

def test_sector_cap_rejects_over_concentrated_sector():
    rm = RiskManager(equity=100_000)
    original_cap = ACCOUNT.max_positions_per_sector
    try:
        ACCOUNT.max_positions_per_sector = 2
        # AAPL, MSFT, NVDA are all "Technology" in bot/sectors.py
        for sym in ["AAPL", "MSFT"]:
            rm.open_positions.append(Position(
                id=sym, symbol=sym, strategy=StrategyType.IRON_CONDOR, opened_at=datetime.utcnow(),
                entry_credit_or_debit=100, max_loss=500, contracts=1, stop_loss_level=2.0, profit_target_pct=0.5,
            ))
        signal = TradeSignal(
            symbol="NVDA", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
            direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
        )
        assert rm._within_sector_cap(signal) is False
    finally:
        ACCOUNT.max_positions_per_sector = original_cap


def test_sector_cap_allows_different_sectors():
    rm = RiskManager(equity=100_000)
    rm.open_positions.append(Position(
        id="1", symbol="AAPL", strategy=StrategyType.IRON_CONDOR, opened_at=datetime.utcnow(),
        entry_credit_or_debit=100, max_loss=500, contracts=1, stop_loss_level=2.0, profit_target_pct=0.5,
    ))
    signal = TradeSignal(
        symbol="JPM", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,  # Financials, not Technology
        direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
    )
    assert rm._within_sector_cap(signal) is True


def test_unmapped_symbol_exempt_from_sector_cap():
    rm = RiskManager(equity=100_000)
    original_cap = ACCOUNT.max_positions_per_sector
    try:
        ACCOUNT.max_positions_per_sector = 1
        for sym in ["ZZZFAKE1", "ZZZFAKE2"]:
            rm.open_positions.append(Position(
                id=sym, symbol=sym, strategy=StrategyType.IRON_CONDOR, opened_at=datetime.utcnow(),
                entry_credit_or_debit=100, max_loss=500, contracts=1, stop_loss_level=2.0, profit_target_pct=0.5,
            ))
        signal = TradeSignal(
            symbol="ZZZFAKE3", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
            direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
        )
        assert rm._within_sector_cap(signal) is True  # UNKNOWN sector, not gated
    finally:
        ACCOUNT.max_positions_per_sector = original_cap


# ---- Kelly sizing -----------------------------------------------------

def test_kelly_fraction_matches_formula():
    # win_rate=0.6, avg_win=100, avg_loss=50 -> R=2, f*=0.6-0.4/2=0.4
    f = kelly_fraction(win_rate=0.6, avg_win=100, avg_loss=50)
    assert f == pytest.approx(0.4, abs=0.001)


def test_kelly_fraction_negative_expectancy_returns_negative():
    # win_rate=0.3, avg_win=50, avg_loss=100 -> clearly negative edge
    f = kelly_fraction(win_rate=0.3, avg_win=50, avg_loss=100)
    assert f < 0


def test_half_kelly_falls_back_to_fixed_fractional_with_small_sample():
    rm = RiskManager(equity=100_000)
    original_method = ACCOUNT.position_sizing_method
    try:
        ACCOUNT.position_sizing_method = "half_kelly"
        # fewer than kelly_min_sample_size closed trades
        signal = TradeSignal(
            symbol="TEST", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
            direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
        )
        budget_kelly_mode = rm._risk_budget_half_kelly(signal)
        budget_fixed = rm._risk_budget_fixed_fractional(signal)
        assert budget_kelly_mode == pytest.approx(budget_fixed)
    finally:
        ACCOUNT.position_sizing_method = original_method


def test_half_kelly_respects_hard_ceiling_once_enough_samples():
    rm = RiskManager(equity=100_000)
    # simulate a very high win rate / big win-loss ratio history that would
    # otherwise push Kelly sizing very high
    for _ in range(30):
        rm.stats.record(500)  # all wins
    for _ in range(5):
        rm.stats.record(-50)
    signal = TradeSignal(
        symbol="TEST", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
        direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
    )
    budget = rm._risk_budget_half_kelly(signal)
    assert budget <= rm.equity * ACCOUNT.kelly_max_risk_per_trade_pct + 1e-6


# ---- reward:risk gate ----------------------------------------------------

def test_reward_risk_gate_rejects_below_minimum():
    rm = RiskManager(equity=100_000)
    signal = TradeSignal(
        symbol="TEST", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
        direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
    )
    # stop_loss_multiple=3.0 -> risk_to_stop=2.0, target_pct=0.1 -> ratio=0.05, below iron_condor min (0.20)
    assert rm._within_reward_risk_minimum(signal, stop_loss_multiple=3.0, profit_target_pct=0.1) is False


def test_reward_risk_gate_accepts_above_minimum():
    rm = RiskManager(equity=100_000)
    signal = TradeSignal(
        symbol="TEST", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
        direction="neutral", est_credit_or_risk=200, est_max_loss=500, confidence=1.0, rationale="",
    )
    # stop_loss_multiple=2.0 -> risk_to_stop=1.0, target_pct=0.5 -> ratio=0.5, above min (0.20)
    assert rm._within_reward_risk_minimum(signal, stop_loss_multiple=2.0, profit_target_pct=0.5) is True


# ---- consecutive-loss cooldown (revenge-trading prevention) ---------------

def test_consecutive_losses_trigger_cooldown():
    rm = RiskManager(equity=100_000)
    original_count = ACCOUNT.consecutive_loss_cooldown_count
    try:
        ACCOUNT.consecutive_loss_cooldown_count = 3
        pos = Position(
            id="1", symbol="TEST", strategy=StrategyType.IRON_CONDOR, opened_at=datetime.utcnow(),
            entry_credit_or_debit=100, max_loss=500, contracts=1, stop_loss_level=2.0, profit_target_pct=0.5,
        )
        rm.open_positions.append(pos)
        for _ in range(3):
            rm.open_positions.append(Position(
                id="x", symbol="TEST", strategy=StrategyType.IRON_CONDOR, opened_at=datetime.utcnow(),
                entry_credit_or_debit=100, max_loss=500, contracts=1, stop_loss_level=2.0, profit_target_pct=0.5,
            ))
            rm.register_close(rm.open_positions[-1], -100)
        assert rm.consecutive_losses == 3
        assert rm.cooldown_until is not None
        with pytest.raises(CircuitBreakerTripped):
            rm.check_circuit_breakers()
    finally:
        ACCOUNT.consecutive_loss_cooldown_count = original_count


def test_win_resets_consecutive_loss_counter():
    rm = RiskManager(equity=100_000)
    for _ in range(2):
        pos = Position(
            id="x", symbol="TEST", strategy=StrategyType.IRON_CONDOR, opened_at=datetime.utcnow(),
            entry_credit_or_debit=100, max_loss=500, contracts=1, stop_loss_level=2.0, profit_target_pct=0.5,
        )
        rm.open_positions.append(pos)
        rm.register_close(pos, -100)
    assert rm.consecutive_losses == 2

    win_pos = Position(
        id="w", symbol="TEST", strategy=StrategyType.IRON_CONDOR, opened_at=datetime.utcnow(),
        entry_credit_or_debit=100, max_loss=500, contracts=1, stop_loss_level=2.0, profit_target_pct=0.5,
    )
    rm.open_positions.append(win_pos)
    rm.register_close(win_pos, 150)
    assert rm.consecutive_losses == 0
    assert rm.cooldown_until is None
