"""
Hard bounds for every parameter the optimizer (optimizer.py) is allowed to
tune, keyed on dotted "OBJECT.field" or "OBJECT.field.subkey" paths matching
config/tuned_overrides.json's structure.

Deliberately excluded, on purpose, never added here: starting_equity,
max_drawdown_pause_pct, daily_loss_limit_pct, max_concurrent_positions,
max_net_delta_shares_equiv, max_portfolio_theta_pct, max_portfolio_vega_pct,
every kelly_* field, max_positions_per_sector. These are account-level
circuit breakers and capital-at-risk knobs, not entry/exit signal
thresholds -- they stay human-only changes, the same scope boundary
lowfloat_trader's own optimizer already respects (it only tunes entry-filter
and exit-target params, never account-level risk caps). This is a safety
decision, not a style choice: don't add these here without a deliberate,
separate conversation about widening the optimizer's authority.
"""

PARAM_LIMITS = {
    "REGIME.iv_rank_high": (40.0, 65.0),
    "REGIME.iv_rank_low": (15.0, 40.0),
    "REGIME.adx_trend": (18.0, 35.0),
    "REGIME.adx_chop": (12.0, 22.0),

    "ACCOUNT.high_vol_size_cut_pct": (0.10, 0.50),
    "ACCOUNT.high_vol_atr_pct_threshold": (2.0, 8.0),
    "ACCOUNT.trailing_stop_activation_pct": (0.25, 0.75),
    "ACCOUNT.zero_dte_size_cut_pct": (0.15, 0.60),
    "ACCOUNT.aplus_score_threshold": (65.0, 92.0),
    "ACCOUNT.aplus_size_boost_pct": (0.10, 0.50),

    "ACCOUNT.min_reward_risk_ratio.short_strangle": (0.10, 0.40),
    "ACCOUNT.min_reward_risk_ratio.iron_condor": (0.10, 0.40),
    "ACCOUNT.min_reward_risk_ratio.short_put_vertical": (0.20, 0.70),
    "ACCOUNT.min_reward_risk_ratio.short_call_vertical": (0.20, 0.70),
    "ACCOUNT.min_reward_risk_ratio.futures_trend": (0.25, 0.90),
    "ACCOUNT.min_reward_risk_ratio.zero_dte_iron_condor": (0.10, 0.40),
}

# Minimum required gap between paired thresholds, enforced by
# optimizer.validate_invariants() after all individual parameter deltas are
# computed -- see that function's docstring for why this runs last.
MIN_IV_RANK_GAP = 10.0
MIN_ADX_GAP = 5.0
