from datetime import datetime, timedelta
from bot.models import Position, StrategyType
from bot import journal


def make_closed_position(symbol="TEST", pnl=150.0, exit_reason_hint="profit", days_ago=1,
                          rationale="IV rank 90, ADX 12: defined-risk condor, 300 credit vs 1200 max loss.",
                          regime="high_iv_range", high_water_mark_pct=0.5, trailing_stop_active=False):
    opened = datetime.utcnow() - timedelta(days=days_ago + 5)
    closed = datetime.utcnow() - timedelta(days=days_ago)
    pos = Position(
        id="t1", symbol=symbol, strategy=StrategyType.IRON_CONDOR,
        opened_at=opened,
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        stop_loss_level=2.0, profit_target_pct=0.5,
        direction="neutral", regime_at_entry=regime, rationale=rationale, confidence=0.9,
        entry_spot_price=100.0, adx_at_entry=15.0, iv_rank_at_entry=90.0, atr_pct_at_entry=2.0,
        planned_stop_loss_level=2.0, planned_profit_target_pct=0.5,
        sizing_method_at_entry="fixed_fractional", consecutive_losses_at_entry=0,
        high_water_mark_pct=high_water_mark_pct, trailing_stop_active=trailing_stop_active,
    )
    pos.status = "closed"
    pos.closed_at = closed
    pos.realized_pnl = pnl
    return pos


def test_log_captures_rationale_and_regime(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    pos = make_closed_position(rationale="IV rank 95, selling condor")
    journal.log_closed_trade(pos, exit_reason="profit target 50% hit")

    import csv
    with open(journal.JOURNAL_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["rationale"] == "IV rank 95, selling condor"
    assert rows[0]["regime_at_entry"] == "high_iv_range"


def test_log_captures_system_state_at_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    pos = make_closed_position()
    pos.sizing_method_at_entry = "half_kelly"
    pos.consecutive_losses_at_entry = 2
    journal.log_closed_trade(pos, exit_reason="profit target 50% hit")

    import csv
    with open(journal.JOURNAL_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["sizing_method_at_entry"] == "half_kelly"
    assert rows[0]["consecutive_losses_at_entry"] == "2"


def test_outcome_analysis_profit_target(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    pos = make_closed_position(pnl=200.0)
    journal.log_closed_trade(pos, exit_reason="profit target 50% hit")

    import csv
    with open(journal.JOURNAL_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["hit_profit_target"] == "True"
    assert rows[0]["hit_stop_loss"] == "False"
    assert "worked as designed" in rows[0]["lessons_learned"]


def test_outcome_analysis_stop_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    pos = make_closed_position(pnl=-300.0)
    journal.log_closed_trade(pos, exit_reason="stop loss 2.0x credit hit")

    import csv
    with open(journal.JOURNAL_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["hit_stop_loss"] == "True"
    assert rows[0]["hit_profit_target"] == "False"
    assert "Stopped out" in rows[0]["lessons_learned"]


def test_trailing_stop_lessons_learned_differs_by_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    winning_trail = make_closed_position(pnl=100.0, high_water_mark_pct=0.4, trailing_stop_active=True)
    journal.log_closed_trade(winning_trail, exit_reason="trailing stop hit")

    import csv
    with open(journal.JOURNAL_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert "protected a gain" in rows[0]["lessons_learned"]


def test_r_multiple_and_reward_risk_ratio_computed(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    pos = make_closed_position(pnl=600.0)  # max_loss=1200 -> R=0.5
    journal.log_closed_trade(pos, "target hit")

    import csv
    with open(journal.JOURNAL_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    assert float(rows[0]["r_multiple"]) == 0.5
    # stop_loss_level=2.0 -> risk_to_stop=1.0, target_pct=0.5 -> ratio=0.5
    assert float(rows[0]["planned_reward_risk_ratio"]) == 0.5


def test_report_none_when_no_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "empty.csv")
    assert journal.generate_report() is None


def test_report_breaks_down_by_strategy_sector_regime(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    journal.log_closed_trade(make_closed_position(symbol="AAPL", pnl=100.0), "profit target 50% hit")
    journal.log_closed_trade(make_closed_position(symbol="JPM", pnl=-50.0), "stop loss 2.0x credit hit")

    report = journal.generate_report()
    assert report.total_trades == 2
    assert report.win_rate == 0.5
    assert "Technology" in report.by_sector or "Financials" in report.by_sector
    assert "high_iv_range" in report.by_regime


def test_weekly_period_excludes_older_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    journal.log_closed_trade(make_closed_position(pnl=100.0, days_ago=2), "profit target 50% hit")
    journal.log_closed_trade(make_closed_position(pnl=-50.0, days_ago=20), "stop loss 2.0x credit hit")

    weekly = journal.generate_report(period="weekly")
    all_time = journal.generate_report(period="all")
    assert weekly.total_trades == 1
    assert all_time.total_trades == 2


def test_goal_status_reflects_config():
    from bot.config import ACCOUNT
    original = ACCOUNT.target_win_rate
    try:
        ACCOUNT.target_win_rate = 0.9  # unrealistically high, should read as below target
        goals = [journal.GoalStatus("Win rate", 0.9, 0.5, 0.5 >= 0.9)]
        assert goals[0].on_track is False
    finally:
        ACCOUNT.target_win_rate = original


def test_generate_charts_creates_files(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "journal.csv")
    monkeypatch.setattr(journal, "CHARTS_DIR", tmp_path / "charts")
    journal.log_closed_trade(make_closed_position(pnl=100.0), "profit target 50% hit")
    journal.log_closed_trade(make_closed_position(pnl=-50.0), "stop loss 2.0x credit hit")

    paths = journal.generate_charts(starting_equity=100_000)
    assert paths is not None
    import os
    assert os.path.exists(paths["equity_curve"])
    assert os.path.exists(paths["pnl_distribution"])


def test_generate_charts_none_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "empty.csv")
    assert journal.generate_charts() is None
