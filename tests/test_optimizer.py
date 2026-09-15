import csv
from datetime import datetime, timedelta
from pathlib import Path

import optimizer
from bot.journal import FIELDNAMES
from bot.tuning_limits import PARAM_LIMITS


def write_journal(tmp_path, rows):
    path = tmp_path / "journal.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(rows)
    return path


def make_row(**overrides):
    now = datetime.utcnow()
    row = {f: "" for f in FIELDNAMES}
    row.update({
        "trade_id": "1", "symbol": "AAPL", "direction": "neutral", "strategy": "iron_condor",
        "sector": "Technology", "contracts": 1,
        "opened_at": (now - timedelta(days=2)).isoformat(),
        "closed_at": (now - timedelta(days=1)).isoformat(),
        "days_held": 1, "entry_spot_price": 100.0, "regime_at_entry": "high_iv_range",
        "confidence": 0.9, "adx_at_entry": 15.0, "iv_rank_at_entry": 60.0, "atr_pct_at_entry": 2.0,
        "scanner_score_at_entry": 0.0, "sizing_method_at_entry": "fixed_fractional",
        "consecutive_losses_at_entry": 0, "entry_credit_or_debit": 300.0, "max_loss": 1200.0,
        "planned_stop_loss_level": 2.0, "planned_profit_target_pct": 0.5, "planned_reward_risk_ratio": 0.2,
        "realized_pnl": -100.0, "r_multiple": -0.08, "return_pct_on_risk": -0.08,
        "exit_reason": "stop loss hit", "hit_profit_target": False, "hit_stop_loss": True,
        "pct_of_target_captured": 0, "trailing_stop_used": False,
    })
    row.update(overrides)
    return row


# ---- ported directly from lowfloat_trader's optimizer tests --------------

def test_clamp_bounds_value():
    assert optimizer.clamp(5.0, 1.0, 10.0) == 5.0
    assert optimizer.clamp(-5.0, 1.0, 10.0) == 1.0
    assert optimizer.clamp(50.0, 1.0, 10.0) == 10.0


def test_safe_change_rate_limits_large_swing():
    original = optimizer.MAX_CHANGE_PCT
    try:
        optimizer.MAX_CHANGE_PCT = 20
        # naive target (1.0) is a 900% jump from 0.1 -- must be capped to 20%
        result = optimizer.safe_change(0.1, 1.0, 0.0, 10.0)
        assert result == round(0.1 * 1.20, 4)
    finally:
        optimizer.MAX_CHANGE_PCT = original


def test_safe_change_hard_clamps_to_bounds():
    original = optimizer.MAX_CHANGE_PCT
    try:
        optimizer.MAX_CHANGE_PCT = 1000  # effectively no rate limit
        result = optimizer.safe_change(5.0, 50.0, 0.0, 10.0)
        assert result == 10.0  # clamped, even though the rate limit alone wouldn't stop it
    finally:
        optimizer.MAX_CHANGE_PCT = original


# ---- min-trades gate -------------------------------------------------

def test_min_trades_gate_skips_underpopulated_segment(tmp_path):
    original_min = optimizer.MIN_TRADES
    try:
        optimizer.MIN_TRADES = 5
        # only 3 losing trades -- below the gate, must not recommend a change
        rows = [make_row(trade_id=str(i), realized_pnl=-100.0) for i in range(3)]
        journal = write_journal(tmp_path, rows)
        changes, trades = optimizer.run(journal_file=journal)
        assert changes == []
    finally:
        optimizer.MIN_TRADES = original_min


def test_min_trades_gate_allows_at_threshold(tmp_path):
    original_min = optimizer.MIN_TRADES
    try:
        optimizer.MIN_TRADES = 5
        rows = [make_row(trade_id=str(i), realized_pnl=-100.0) for i in range(5)]
        journal = write_journal(tmp_path, rows)
        changes, trades = optimizer.run(journal_file=journal)
        assert any(c["path"] == "ACCOUNT.min_reward_risk_ratio.iron_condor" for c in changes)
    finally:
        optimizer.MIN_TRADES = original_min


# ---- rate limiting / hard bounds applied through the real tuners --------

def test_max_change_pct_rate_limits_large_recommended_swing(tmp_path):
    original_max = optimizer.MAX_CHANGE_PCT
    try:
        optimizer.MAX_CHANGE_PCT = 5  # very tight rate limit
        rows = [make_row(trade_id=str(i), realized_pnl=-100.0) for i in range(6)]
        journal = write_journal(tmp_path, rows)
        changes, trades = optimizer.run(journal_file=journal)
        change = next(c for c in changes if c["path"] == "ACCOUNT.min_reward_risk_ratio.iron_condor")
        # 5% of the current 0.20 value = 0.01 max delta
        assert abs(change["new"] - change["old"]) <= 0.01 + 1e-9
    finally:
        optimizer.MAX_CHANGE_PCT = original_max


def test_param_limits_hard_bound_enforced(tmp_path):
    original_max = optimizer.MAX_CHANGE_PCT
    try:
        optimizer.MAX_CHANGE_PCT = 100_000  # effectively no rate limit, to isolate the hard bound
        rows = [make_row(trade_id=str(i), realized_pnl=-100.0) for i in range(20)]
        journal = write_journal(tmp_path, rows)
        changes, trades = optimizer.run(journal_file=journal)
        change = next(c for c in changes if c["path"] == "ACCOUNT.min_reward_risk_ratio.iron_condor")
        lo, hi = PARAM_LIMITS["ACCOUNT.min_reward_risk_ratio.iron_condor"]
        assert lo <= change["new"] <= hi
    finally:
        optimizer.MAX_CHANGE_PCT = original_max


# ---- cross-parameter validation ------------------------------------------

def test_validate_invariants_rejects_bad_combination():
    class FakeRegime:
        iv_rank_low = 30.0
        iv_rank_high = 50.0
        adx_chop = 20.0
        adx_trend = 25.0

    targets = {"ACCOUNT": optimizer.ACCOUNT, "REGIME": FakeRegime()}
    # a naive change that would push iv_rank_high down to within the
    # required gap of iv_rank_low
    changes = [{"path": "REGIME.iv_rank_high", "old": 50.0, "new": 35.0, "reason": "test", "sample_size": 10}]
    error = optimizer.validate_invariants(targets, changes)
    assert error is not None
    assert "iv_rank" in error


def test_validate_invariants_accepts_good_combination():
    class FakeRegime:
        iv_rank_low = 30.0
        iv_rank_high = 50.0
        adx_chop = 20.0
        adx_trend = 25.0

    # real ACCOUNT singleton, which already has every PARAM_LIMITS reward:risk
    # key populated -- this test only exercises the REGIME gap check
    targets = {"ACCOUNT": optimizer.ACCOUNT, "REGIME": FakeRegime()}
    changes = [{"path": "REGIME.iv_rank_high", "old": 50.0, "new": 52.0, "reason": "test", "sample_size": 10}]
    assert optimizer.validate_invariants(targets, changes) is None


def test_aborting_run_writes_no_file(tmp_path, monkeypatch):
    overrides_file = tmp_path / "overrides.json"
    monkeypatch.setattr(optimizer, "OVERRIDES_FILE", overrides_file)

    class FakeRegime:
        iv_rank_low = 30.0
        iv_rank_high = 50.0
        adx_chop = 20.0
        adx_trend = 25.0

    monkeypatch.setattr(optimizer, "REGIME", FakeRegime())
    monkeypatch.setattr(optimizer, "run", lambda journal_file=None: (
        [{"path": "REGIME.iv_rank_high", "old": 50.0, "new": 35.0, "reason": "test", "sample_size": 10}], [1] * 10
    ))

    optimizer.main()
    assert not overrides_file.exists()


# ---- output shape ---------------------------------------------------

def test_output_json_is_complete_self_consistent_snapshot(tmp_path, monkeypatch):
    overrides_file = tmp_path / "overrides.json"
    pr_body_file = tmp_path / "pr_body.md"
    monkeypatch.setattr(optimizer, "OVERRIDES_FILE", overrides_file)
    monkeypatch.setattr(optimizer, "PR_BODY_FILE", pr_body_file)

    rows = [make_row(trade_id=str(i), realized_pnl=-100.0) for i in range(6)]
    journal = write_journal(tmp_path, rows)
    changes, trades = optimizer.run(journal_file=journal)
    targets = {"ACCOUNT": optimizer.ACCOUNT, "REGIME": optimizer.REGIME}
    optimizer.write_overrides(changes, targets, len(trades))

    import json
    output = json.loads(overrides_file.read_text())
    # every PARAM_LIMITS-covered top-level field is present, not just the
    # one that actually changed this run
    assert "high_vol_size_cut_pct" in output["ACCOUNT"]
    assert "min_reward_risk_ratio" in output["ACCOUNT"]
    assert "short_strangle" in output["ACCOUNT"]["min_reward_risk_ratio"]  # untouched key still present
    assert "iv_rank_high" in output["REGIME"]
    assert "adx_trend" in output["REGIME"]
    assert "_meta" in output and "changes" in output["_meta"]


def test_second_run_produces_fresh_snapshot_not_accumulating_diff(tmp_path, monkeypatch):
    overrides_file = tmp_path / "overrides.json"
    monkeypatch.setattr(optimizer, "OVERRIDES_FILE", overrides_file)

    rows = [make_row(trade_id=str(i), realized_pnl=-100.0) for i in range(6)]
    journal = write_journal(tmp_path, rows)
    targets = {"ACCOUNT": optimizer.ACCOUNT, "REGIME": optimizer.REGIME}

    changes1, trades1 = optimizer.run(journal_file=journal)
    optimizer.write_overrides(changes1, targets, len(trades1))

    changes2, trades2 = optimizer.run(journal_file=journal)
    optimizer.write_overrides(changes2, targets, len(trades2))

    import json
    output = json.loads(overrides_file.read_text())
    # exactly one meta.changes entry per run's worth of data, not two runs' worth accumulated
    assert len(output["_meta"]["changes"]) == len(changes2)
