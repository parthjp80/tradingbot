from dataclasses import dataclass, field
from pathlib import Path

from bot.tuned_overrides import apply_overrides


@dataclass
class FakeAccount:
    max_risk_per_trade_pct: float = 0.02
    min_reward_risk_ratio: dict = field(default_factory=lambda: {
        "iron_condor": 0.20,
        "short_strangle": 0.25,
    })


@dataclass
class FakeRegime:
    iv_rank_high: float = 50.0
    iv_rank_low: float = 30.0


def test_no_override_file_means_defaults_unchanged(tmp_path):
    account, regime = FakeAccount(), FakeRegime()
    apply_overrides({"ACCOUNT": account, "REGIME": regime}, overrides_file=tmp_path / "does_not_exist.json")
    assert account.max_risk_per_trade_pct == 0.02
    assert regime.iv_rank_high == 50.0


def test_scalar_override_applied(tmp_path):
    override_file = tmp_path / "overrides.json"
    override_file.write_text('{"REGIME": {"iv_rank_high": 55.0}}')

    account, regime = FakeAccount(), FakeRegime()
    apply_overrides({"ACCOUNT": account, "REGIME": regime}, overrides_file=override_file)
    assert regime.iv_rank_high == 55.0
    assert regime.iv_rank_low == 30.0  # untouched


def test_dict_field_deep_merges_not_replaces(tmp_path):
    override_file = tmp_path / "overrides.json"
    override_file.write_text('{"ACCOUNT": {"min_reward_risk_ratio": {"iron_condor": 0.30}}}')

    account, regime = FakeAccount(), FakeRegime()
    apply_overrides({"ACCOUNT": account, "REGIME": regime}, overrides_file=override_file)
    assert account.min_reward_risk_ratio["iron_condor"] == 0.30
    assert account.min_reward_risk_ratio["short_strangle"] == 0.25  # not dropped


def test_unknown_object_key_logs_warning_and_is_ignored(tmp_path):
    override_file = tmp_path / "overrides.json"
    override_file.write_text('{"NONEXISTENT_OBJECT": {"foo": 1}}')

    account, regime = FakeAccount(), FakeRegime()
    apply_overrides({"ACCOUNT": account, "REGIME": regime}, overrides_file=override_file)  # must not raise
    assert account.max_risk_per_trade_pct == 0.02


def test_unknown_field_logs_warning_and_is_ignored(tmp_path):
    override_file = tmp_path / "overrides.json"
    override_file.write_text('{"ACCOUNT": {"totally_made_up_field": 999}}')

    account, regime = FakeAccount(), FakeRegime()
    apply_overrides({"ACCOUNT": account, "REGIME": regime}, overrides_file=override_file)
    assert not hasattr(account, "totally_made_up_field")


def test_malformed_json_does_not_crash_import(tmp_path):
    override_file = tmp_path / "overrides.json"
    override_file.write_text("{not valid json")

    account, regime = FakeAccount(), FakeRegime()
    apply_overrides({"ACCOUNT": account, "REGIME": regime}, overrides_file=override_file)  # must not raise
    assert account.max_risk_per_trade_pct == 0.02  # falls back to defaults


def test_meta_key_is_never_applied(tmp_path):
    override_file = tmp_path / "overrides.json"
    override_file.write_text('{"_meta": {"changes": []}, "REGIME": {"iv_rank_high": 52.0}}')

    account, regime = FakeAccount(), FakeRegime()
    apply_overrides({"ACCOUNT": account, "REGIME": regime}, overrides_file=override_file)
    assert regime.iv_rank_high == 52.0
    assert not hasattr(regime, "_meta")
