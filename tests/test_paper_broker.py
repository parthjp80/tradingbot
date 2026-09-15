from datetime import datetime, timedelta
import pytest
from bot.paper_broker import PaperBroker
from bot.risk_manager import RiskManager
from bot.models import Position, StrategyType
from bot.config import ACCOUNT


@pytest.fixture
def make_broker(monkeypatch, tmp_path):
    # Isolate from the real STATE_FILE/journal -- these tests must never
    # touch production position state or trade history. See
    # tests/test_alpaca_broker.py's `broker` fixture for the same pattern.
    import bot.paper_broker as pb_module
    monkeypatch.setattr(pb_module, "STATE_FILE", tmp_path / "paper_positions.json")

    import bot.journal as journal_module
    monkeypatch.setattr(journal_module, "JOURNAL_FILE", tmp_path / "trade_journal.csv")

    def _make():
        rm = RiskManager(equity=100_000)
        broker = PaperBroker(rm)
        return broker, rm

    return _make


def test_trailing_stop_activates_after_profit_threshold(make_broker):
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


def test_trailing_stop_not_activated_before_threshold(make_broker):
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


def test_high_water_mark_tracks_best_profit_captured(make_broker):
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


# ---- 0DTE force-close-by-time ------------------------------------------

def test_force_close_by_time_closes_regardless_of_pnl(make_broker):
    broker, rm = make_broker()
    pos = Position(
        id="1", symbol="SPY", strategy=StrategyType.ZERO_DTE_IRON_CONDOR,
        opened_at=datetime.utcnow(),
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        # target set high and stop set high so neither the normal profit-target
        # nor stop-loss branch would fire on its own -- proves the force-close
        # check fires independently of P&L, not as a side effect of a target hit
        stop_loss_level=1000.0, profit_target_pct=2.0,
        theta_estimate=-1.0,
        force_close_by=datetime.utcnow() - timedelta(minutes=1),  # already in the past
    )
    rm.open_positions.append(pos)

    broker.check_exits({"SPY": 500.0})

    assert pos.status == "closed"
    assert pos.realized_pnl is not None


def test_force_close_by_time_does_not_fire_early(make_broker):
    broker, rm = make_broker()
    pos = Position(
        id="1", symbol="SPY", strategy=StrategyType.ZERO_DTE_IRON_CONDOR,
        opened_at=datetime.utcnow(),
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        stop_loss_level=1000.0, profit_target_pct=2.0,
        theta_estimate=-1.0,
        force_close_by=datetime.utcnow() + timedelta(hours=2),  # still in the future
    )
    rm.open_positions.append(pos)

    broker.check_exits({"SPY": 500.0})

    assert pos.status == "open"


def test_force_close_by_survives_save_and_reload(make_broker):
    # Regression test: _save_state()/_load_state() must serialize/deserialize
    # force_close_by (a datetime) the same way they already handle
    # opened_at/closed_at -- an untouched field here crashes json.dumps() the
    # first time a 0DTE position is still open when check_exits() saves state
    # for an unrelated reason (e.g. trailing-stop activation on another
    # position), not just when the 0DTE position itself is closed.
    broker, rm = make_broker()
    force_close_by = datetime.utcnow() + timedelta(hours=1)
    pos = Position(
        id="1", symbol="SPY", strategy=StrategyType.ZERO_DTE_IRON_CONDOR,
        opened_at=datetime.utcnow(),
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        stop_loss_level=1.3, profit_target_pct=0.4,
        force_close_by=force_close_by,
    )
    rm.open_positions.append(pos)

    broker._save_state()  # must not raise

    broker2, rm2 = make_broker()  # constructor calls _load_state() automatically
    reloaded = rm2.open_positions[0]
    assert reloaded.force_close_by == force_close_by


def test_non_0dte_positions_unaffected_by_force_close_check(make_broker):
    broker, rm = make_broker()
    pos = Position(
        id="1", symbol="TEST", strategy=StrategyType.IRON_CONDOR,
        opened_at=datetime.utcnow(),
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        stop_loss_level=100.0, profit_target_pct=0.99,
        theta_estimate=-1.0,
        # force_close_by defaults to None for every non-0DTE strategy
    )
    rm.open_positions.append(pos)

    broker.check_exits({"TEST": 500.0})

    assert pos.status == "open"
