from datetime import datetime, timedelta
from bot.paper_broker import PaperBroker
from bot.risk_manager import RiskManager
from bot.models import Position, StrategyType
from bot.config import ACCOUNT


def make_broker(tmp_data_dir=None):
    rm = RiskManager(equity=100_000)
    broker = PaperBroker(rm)
    return broker, rm


def test_trailing_stop_activates_after_profit_threshold():
    broker, rm = make_broker()
    pos = Position(
        id="1", symbol="TEST", strategy=StrategyType.IRON_CONDOR,
        opened_at=datetime.utcnow() - timedelta(days=10),
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        stop_loss_level=2.0, profit_target_pct=0.5,
        theta_estimate=-20.0,  # aggressive decay so profit_captured_pct crosses activation quickly
    )
    rm.open_positions.append(pos)

    broker.check_exits({"TEST": 100.0})

    # after 10 days at theta=-20/day, theta_captured=200, estimated_value=100,
    # profit_captured_pct = 1 - 100/300 = 0.667 >= activation (0.5*0.5=0.25)
    assert pos.trailing_stop_active is True
    assert pos.stop_loss_level <= ACCOUNT.trailing_stop_lock_in_level


def test_trailing_stop_not_activated_before_threshold():
    broker, rm = make_broker()
    pos = Position(
        id="1", symbol="TEST", strategy=StrategyType.IRON_CONDOR,
        opened_at=datetime.utcnow(),  # just opened, no theta captured yet
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        stop_loss_level=2.0, profit_target_pct=0.5,
        theta_estimate=-5.0,
    )
    rm.open_positions.append(pos)

    broker.check_exits({"TEST": 100.0})

    assert pos.trailing_stop_active is False
    assert pos.stop_loss_level == 2.0


def test_high_water_mark_tracks_best_profit_captured():
    broker, rm = make_broker()
    pos = Position(
        id="1", symbol="TEST", strategy=StrategyType.IRON_CONDOR,
        opened_at=datetime.utcnow() - timedelta(days=3),
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        stop_loss_level=2.0, profit_target_pct=0.9,  # high target so it doesn't close outright
        theta_estimate=-10.0,
    )
    rm.open_positions.append(pos)

    broker.check_exits({"TEST": 100.0})
    assert pos.high_water_mark_pct > 0
