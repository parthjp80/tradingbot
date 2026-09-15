#!/usr/bin/env python3
"""
Mines the trade journal for realized performance and recommends bounded,
rate-limited parameter adjustments, written to config/tuned_overrides.json
for PR review -- modeled on lowfloat_trader's optimizer.py/autotune.sh, but
adapted for tradingbot's dataclass config (via bot/tuned_overrides.py's
override-layer, not source patching) and its richer per-trade journal
schema (regime/strategy/scanner-score at entry, not just gap%/rel-vol
proxies).

Every change is gated on a minimum sample size (OPTIMIZER_MIN_TRADES),
rate-limited to a fraction of the current value per run (safe_change(),
OPTIMIZER_MAX_CHANGE_PCT), and hard-clamped to bot/tuning_limits.py's
PARAM_LIMITS -- which deliberately excludes every account-level circuit
breaker (drawdown pause, daily loss limit, Kelly ceilings, position caps).
See tuning_limits.py's module docstring for why.

This script only ever WRITES config/tuned_overrides.json locally -- it does
not touch git. See autotune.sh for the clone/branch/commit/PR wrapper that
runs this and opens a PR if the output actually changed.
"""
from __future__ import annotations
import json
import math
import os
import shutil
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from bot.tuning_limits import PARAM_LIMITS, MIN_IV_RANK_GAP, MIN_ADX_GAP
from bot.config import ACCOUNT, REGIME
from bot.tuned_overrides import DEFAULT_OVERRIDES_FILE

LOOKBACK_WEEKS = int(os.environ.get("OPTIMIZER_LOOKBACK_WEEKS", "4"))
MIN_TRADES = int(os.environ.get("OPTIMIZER_MIN_TRADES", "5"))
MAX_CHANGE_PCT = float(os.environ.get("OPTIMIZER_MAX_CHANGE_PCT", "20"))
OVERRIDES_FILE = Path(os.environ.get("OPTIMIZER_OVERRIDES_FILE", str(DEFAULT_OVERRIDES_FILE)))
PR_BODY_FILE = Path(os.environ.get("OPTIMIZER_PR_BODY_FILE", "pr_body.md"))


# ---- rate limiting / bounds, ported from lowfloat_trader/optimizer.py -----

def clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def safe_change(current: float, new: float, lo: float, hi: float) -> float:
    """Never move more than MAX_CHANGE_PCT of the current value in one run,
    then hard-clamp to (lo, hi)."""
    max_delta = abs(current) * (MAX_CHANGE_PCT / 100)
    delta = new - current
    if abs(delta) > max_delta:
        delta = math.copysign(max_delta, delta)
    return round(clamp(current + delta, lo, hi), 4)


# ---- journal loading --------------------------------------------------

def load_recent_trades(lookback_weeks: int = LOOKBACK_WEEKS, journal_file: Path | None = None) -> list[dict]:
    """Reuses bot.journal._read_rows for CSV parsing (don't reimplement it),
    temporarily pointed at journal_file if given, then applies our own
    lookback-window filter (journal.py's own _period_cutoff only knows
    "weekly"/"monthly"/"all", not an arbitrary week count -- extending it
    would touch the reporting module's tested period semantics for no
    reason)."""
    import bot.journal as journal_module

    original_file = journal_module.JOURNAL_FILE
    if journal_file is not None:
        journal_module.JOURNAL_FILE = journal_file
    try:
        rows = journal_module._read_rows("all")
    finally:
        journal_module.JOURNAL_FILE = original_file

    cutoff = datetime.utcnow() - timedelta(weeks=lookback_weeks)
    recent = []
    for r in rows:
        if not r.get("closed_at"):
            continue
        try:
            closed = datetime.fromisoformat(r["closed_at"])
        except ValueError:
            continue
        if closed >= cutoff:
            recent.append(r)
    return recent


# ---- trade-list helpers -------------------------------------------------

def _floats(trades: list[dict], field: str) -> list[float]:
    out = []
    for t in trades:
        try:
            out.append(float(t[field]))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def win_rate(trades: list[dict]) -> float:
    pnls = _floats(trades, "realized_pnl")
    if not pnls:
        return 0.0
    return len([p for p in pnls if p > 0]) / len(pnls)


def avg_pnl(trades: list[dict]) -> float:
    pnls = _floats(trades, "realized_pnl")
    return sum(pnls) / len(pnls) if pnls else 0.0


def segment_by(trades: list[dict], field: str) -> dict[str, list[dict]]:
    segments = defaultdict(list)
    for t in trades:
        segments[t.get(field, "")].append(t)
    return segments


# ---- path helpers for the (ACCOUNT/REGIME) targets + dotted PARAM_LIMITS keys

def _get_path(targets: dict, path: str):
    parts = path.split(".")
    obj = targets[parts[0]]
    value = getattr(obj, parts[1])
    for key in parts[2:]:
        value = value[key]
    return value


def _set_output_path(output: dict, path: str, value) -> None:
    parts = path.split(".")
    node = output.setdefault(parts[0], {})
    for key in parts[1:-1]:
        node = node.setdefault(key, {})
    node[parts[-1]] = value


# ---- per-parameter heuristics -------------------------------------------
# Each records (path, old, new, reason, sample_size) into `changes` only
# when a real change is computed; segments below MIN_TRADES are left alone.

def tune_reward_risk_ratios(trades: list[dict], changes: list[dict]) -> None:
    by_strategy = segment_by(trades, "strategy")
    for strategy, segment in by_strategy.items():
        path = f"ACCOUNT.min_reward_risk_ratio.{strategy}"
        if path not in PARAM_LIMITS or len(segment) < MIN_TRADES:
            continue
        current = ACCOUNT.min_reward_risk_ratio.get(strategy)
        if current is None:
            continue
        wr, ap = win_rate(segment), avg_pnl(segment)
        lo, hi = PARAM_LIMITS[path]
        if wr < 0.40 or ap < 0:
            new = safe_change(current, current * 1.15, lo, hi)
            reason = f"win_rate={wr:.0%}, avg_pnl={ap:.0f} over {len(segment)} trades -- raising bar"
        elif wr > 0.75 and ap > 0:
            new = safe_change(current, current * 0.90, lo, hi)
            reason = f"win_rate={wr:.0%}, avg_pnl={ap:.0f} over {len(segment)} trades -- room to admit more setups"
        else:
            continue
        if new != current:
            changes.append({"path": path, "old": current, "new": new, "reason": reason, "sample_size": len(segment)})


def tune_iv_rank_high(trades: list[dict], changes: list[dict]) -> None:
    path = "REGIME.iv_rank_high"
    high_iv_trades = [t for t in trades if t.get("regime_at_entry") == "high_iv_range"]
    ivs = [(t, float(t["iv_rank_at_entry"])) for t in high_iv_trades if t.get("iv_rank_at_entry")]
    current = REGIME.iv_rank_high
    near = [t for t, iv in ivs if current <= iv < current + 10]
    far = [t for t, iv in ivs if iv >= current + 10]
    if len(near) < MIN_TRADES or len(far) < MIN_TRADES:
        return
    near_wr, far_wr = win_rate(near), win_rate(far)
    lo, hi = PARAM_LIMITS[path]
    if far_wr - near_wr > 0.15:
        new = safe_change(current, current + 2.0, lo, hi)
        if new != current:
            changes.append({
                "path": path, "old": current, "new": new,
                "reason": f"trades entered near threshold win {near_wr:.0%} vs {far_wr:.0%} well above it "
                          f"({len(near)}/{len(far)} trades) -- raising the bar",
                "sample_size": len(near),
            })


def tune_adx_trend(trades: list[dict], changes: list[dict]) -> None:
    path = "REGIME.adx_trend"
    trending_trades = [t for t in trades if t.get("regime_at_entry") == "trending"]
    adxs = [(t, float(t["adx_at_entry"])) for t in trending_trades if t.get("adx_at_entry")]
    current = REGIME.adx_trend
    near = [t for t, adx in adxs if current <= adx < current + 8]
    far = [t for t, adx in adxs if adx >= current + 8]
    if len(near) < MIN_TRADES or len(far) < MIN_TRADES:
        return
    near_wr, far_wr = win_rate(near), win_rate(far)
    lo, hi = PARAM_LIMITS[path]
    if far_wr - near_wr > 0.15:
        new = safe_change(current, current + 2.0, lo, hi)
        if new != current:
            changes.append({
                "path": path, "old": current, "new": new,
                "reason": f"trending trades near threshold win {near_wr:.0%} vs {far_wr:.0%} well above it "
                          f"({len(near)}/{len(far)} trades) -- raising the bar",
                "sample_size": len(near),
            })


def tune_high_vol_size_cut(trades: list[dict], changes: list[dict]) -> None:
    path = "ACCOUNT.high_vol_size_cut_pct"
    threshold = ACCOUNT.high_vol_atr_pct_threshold
    high_vol = [t for t in trades if t.get("atr_pct_at_entry") and float(t["atr_pct_at_entry"]) >= threshold]
    low_vol = [t for t in trades if t.get("atr_pct_at_entry") and float(t["atr_pct_at_entry"]) < threshold]
    if len(high_vol) < MIN_TRADES or len(low_vol) < MIN_TRADES:
        return
    current = ACCOUNT.high_vol_size_cut_pct
    lo, hi = PARAM_LIMITS[path]
    high_ap, low_ap = avg_pnl(high_vol), avg_pnl(low_vol)
    if high_ap < low_ap * 0.5:  # high-vol trades meaningfully worse even after the existing cut
        new = safe_change(current, current * 1.10, lo, hi)
        reason = f"high-ATR trades avg_pnl={high_ap:.0f} vs {low_ap:.0f} for the rest -- cutting further"
    elif high_ap >= low_ap:  # cut may be excessive -- high-vol trades doing fine
        new = safe_change(current, current * 0.95, lo, hi)
        reason = f"high-ATR trades avg_pnl={high_ap:.0f} holding up vs {low_ap:.0f} -- easing the cut slightly"
    else:
        return
    if new != current:
        changes.append({"path": path, "old": current, "new": new, "reason": reason,
                         "sample_size": len(high_vol)})


def tune_zero_dte_size_cut(trades: list[dict], changes: list[dict]) -> None:
    path = "ACCOUNT.zero_dte_size_cut_pct"
    segment = [t for t in trades if t.get("strategy") == "zero_dte_iron_condor"]
    if len(segment) < MIN_TRADES:
        return
    current = ACCOUNT.zero_dte_size_cut_pct
    lo, hi = PARAM_LIMITS[path]
    wr, ap = win_rate(segment), avg_pnl(segment)
    if wr < 0.40 or ap < 0:
        new = safe_change(current, current * 1.10, lo, hi)
        reason = f"0DTE win_rate={wr:.0%}, avg_pnl={ap:.0f} over {len(segment)} trades -- cutting further"
    elif wr > 0.75 and ap > 0:
        new = safe_change(current, current * 0.90, lo, hi)
        reason = f"0DTE win_rate={wr:.0%}, avg_pnl={ap:.0f} over {len(segment)} trades -- easing the cut"
    else:
        return
    if new != current:
        changes.append({"path": path, "old": current, "new": new, "reason": reason, "sample_size": len(segment)})


def tune_trailing_stop_activation(trades: list[dict], changes: list[dict]) -> None:
    path = "ACCOUNT.trailing_stop_activation_pct"
    used = [t for t in trades if t.get("trailing_stop_used") == "True"]
    unused = [t for t in trades if t.get("trailing_stop_used") == "False"]
    if len(used) < MIN_TRADES or len(unused) < MIN_TRADES:
        return
    current = ACCOUNT.trailing_stop_activation_pct
    lo, hi = PARAM_LIMITS[path]
    used_wr, unused_wr = win_rate(used), win_rate(unused)
    # If trades where the trailing stop activated win LESS often than trades
    # where it never activated, the ratchet may be firing too early and
    # truncating winners before they can develop -- activate later.
    if used_wr < unused_wr - 0.15:
        new = safe_change(current, current * 1.10, lo, hi)
        if new != current:
            changes.append({
                "path": path, "old": current, "new": new,
                "reason": f"trailing-stop-activated trades win {used_wr:.0%} vs {unused_wr:.0%} without -- "
                          f"activating later",
                "sample_size": len(used),
            })


def tune_aplus_params(trades: list[dict], changes: list[dict]) -> None:
    threshold_path = "ACCOUNT.aplus_score_threshold"
    boost_path = "ACCOUNT.aplus_size_boost_pct"
    scored = [t for t in trades if t.get("scanner_score_at_entry") and float(t["scanner_score_at_entry"]) > 0]
    current_threshold = ACCOUNT.aplus_score_threshold
    above = [t for t in scored if float(t["scanner_score_at_entry"]) >= current_threshold]
    below = [t for t in scored if float(t["scanner_score_at_entry"]) < current_threshold]
    if len(above) < MIN_TRADES or len(below) < MIN_TRADES:
        return
    above_wr, below_wr = win_rate(above), win_rate(below)
    above_ap, below_ap = avg_pnl(above), avg_pnl(below)
    if above_wr <= below_wr and above_ap <= below_ap:
        # the A+ designation isn't earning its keep -- be pickier about what counts
        lo, hi = PARAM_LIMITS[threshold_path]
        current = current_threshold
        new = safe_change(current, current + 3.0, lo, hi)
        if new != current:
            changes.append({
                "path": threshold_path, "old": current, "new": new,
                "reason": f"A+ trades (score>={current_threshold:.0f}) win {above_wr:.0%}/{above_ap:.0f} vs "
                          f"{below_wr:.0%}/{below_ap:.0f} below threshold -- not earning the boost, raising the bar",
                "sample_size": len(above),
            })
    elif above_wr > below_wr + 0.10 and above_ap > below_ap:
        # A+ setups clearly outperform -- lean in harder on the ones already identified
        lo, hi = PARAM_LIMITS[boost_path]
        current = ACCOUNT.aplus_size_boost_pct
        new = safe_change(current, current * 1.15, lo, hi)
        if new != current:
            changes.append({
                "path": boost_path, "old": current, "new": new,
                "reason": f"A+ trades win {above_wr:.0%}/{above_ap:.0f} vs {below_wr:.0%}/{below_ap:.0f} below "
                          f"threshold over {len(above)} trades -- validated, leaning in further",
                "sample_size": len(above),
            })


# ---- cross-parameter validation, run last --------------------------------

def validate_invariants(targets: dict, changes: list[dict]) -> str | None:
    """Re-derives the full effective value of every changed param and checks
    invariants that span multiple of them. Returns an error string (and the
    caller aborts -- no file written, no PR) if any invariant would be
    violated; None if everything checks out. Mirrors lowfloat_trader's
    validate_rr(), which runs its own cross-check only after every
    individual parameter change is already computed."""
    effective = {c["path"]: c["new"] for c in changes}

    def eff(path, targets=targets):
        return effective.get(path, _get_path(targets, path))

    iv_low, iv_high = eff("REGIME.iv_rank_low"), eff("REGIME.iv_rank_high")
    if iv_high - iv_low < MIN_IV_RANK_GAP:
        return f"REGIME.iv_rank_high ({iv_high}) - iv_rank_low ({iv_low}) < required gap {MIN_IV_RANK_GAP}"

    adx_chop, adx_trend = eff("REGIME.adx_chop"), eff("REGIME.adx_trend")
    if adx_trend - adx_chop < MIN_ADX_GAP:
        return f"REGIME.adx_trend ({adx_trend}) - adx_chop ({adx_chop}) < required gap {MIN_ADX_GAP}"

    for path, (lo, hi) in PARAM_LIMITS.items():
        if path.startswith("ACCOUNT.min_reward_risk_ratio."):
            value = eff(path)
            if not (lo <= value <= hi):
                return f"{path} ({value}) outside bounds ({lo}, {hi}) after merge"

    return None


# ---- main -----------------------------------------------------------------

TUNERS = [
    tune_reward_risk_ratios,
    tune_iv_rank_high,
    tune_adx_trend,
    tune_high_vol_size_cut,
    tune_zero_dte_size_cut,
    tune_trailing_stop_activation,
    tune_aplus_params,
]


def run(journal_file: Path | None = None) -> list[dict]:
    """Returns the list of changes computed (possibly empty). Does not
    write anything -- see main() for that. Exposed separately for tests."""
    trades = load_recent_trades(journal_file=journal_file)
    changes: list[dict] = []
    for tuner in TUNERS:
        tuner(trades, changes)
    return changes, trades


def write_overrides(changes: list[dict], targets: dict, trade_count: int) -> None:
    if OVERRIDES_FILE.exists():
        backup = OVERRIDES_FILE.with_suffix(f".bak.{datetime.utcnow().strftime('%Y%m%d%H%M%S')}")
        shutil.copy(OVERRIDES_FILE, backup)

    # Complete snapshot of every tunable field (not just this run's deltas),
    # so the file never becomes an accumulating diff-of-diffs.
    output: dict = {}
    for path in PARAM_LIMITS:
        value = _get_path(targets, path)
        for c in changes:
            if c["path"] == path:
                value = c["new"]
        _set_output_path(output, path, value)

    output["_meta"] = {
        "generated_at": datetime.utcnow().isoformat(),
        "lookback_weeks": LOOKBACK_WEEKS,
        "trade_count": trade_count,
        "changes": changes,
    }

    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(json.dumps(output, indent=2) + "\n")


def write_pr_body(changes: list[dict], trade_count: int) -> None:
    lines = [
        f"Auto-tuned from {trade_count} trades closed in the last {LOOKBACK_WEEKS} weeks.",
        "",
        "| Parameter | Old | New | Sample | Reason |",
        "|---|---|---|---|---|",
    ]
    for c in changes:
        lines.append(f"| `{c['path']}` | {c['old']} | {c['new']} | {c['sample_size']} | {c['reason']} |")
    lines.append("")
    lines.append("Review against `bot/tuning_limits.py`'s bounds before merging.")
    PR_BODY_FILE.write_text("\n".join(lines) + "\n")


def main() -> None:
    targets = {"ACCOUNT": ACCOUNT, "REGIME": REGIME}
    changes, trades = run()

    if not changes:
        print("optimizer: no changes recommended this run.")
        return

    error = validate_invariants(targets, changes)
    if error:
        print(f"optimizer: aborting, invariant check failed: {error}")
        return

    write_overrides(changes, targets, len(trades))
    write_pr_body(changes, len(trades))

    print(f"optimizer: wrote {len(changes)} change(s) to {OVERRIDES_FILE}")
    for c in changes:
        print(f"  {c['path']}: {c['old']} -> {c['new']} ({c['reason']})")


if __name__ == "__main__":
    main()
