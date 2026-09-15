"""
Trade journal, mapped to the standard 9-step journaling framework. What
doesn't translate literally for an automated system is substituted with
its closest algorithmic analog rather than skipped:

  1. Format/platform     -> CSV (data/trade_journal.csv), human-readable,
                             opens directly in Excel/Sheets/Notion if you
                             want to work with it outside the bot.
  2. Basic trade details  -> symbol, direction, contracts, entry/exit
                             price & time, strategy.
  3. Strategy & rationale -> the actual TradeSignal.rationale string and
                             regime are persisted verbatim, not just a P&L
                             number -- see Position.rationale / regime_at_entry.
  4. Financial metrics    -> position size, planned reward:risk ratio,
                             realized P&L, R-multiple, % return on risk.
  5. Emotional/psych state -> a bot has no emotions, but it has the direct
                             analog: what sizing regime was active and
                             whether this entry followed a losing streak.
                             That's what "system state at entry" captures --
                             the same kind of self-review, aimed at bias in
                             the *system* instead of bias in a person.
  6. Outcome analysis      -> auto-computed hit_profit_target/hit_stop_loss/
                             pct_of_target_captured + a short rule-based
                             "lessons learned" note per trade.
  7. Weekly/monthly review -> generate_report(period="weekly"|"monthly"|"all").
  8. Visuals/charts        -> generate_charts() plots an equity curve and a
                             win/loss distribution (matplotlib), since
                             there's no price chart to screenshot for an
                             automated entry -- the equivalent visual is
                             "how is the system's equity actually behaving."
  9. Feedback / goals      -> no mentor to loop in, but ACCOUNT.target_win_rate /
                             target_expectancy_per_trade / target_avg_r_multiple
                             (config.py) are compared against actuals in
                             every report, so drift shows up automatically.
"""
from __future__ import annotations
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from bot.config import DATA_DIR, ACCOUNT
from bot.models import Position
from bot.logger_setup import get_logger

log = get_logger(__name__)

JOURNAL_FILE = DATA_DIR / "trade_journal.csv"
CHARTS_DIR = DATA_DIR / "charts"

FIELDNAMES = [
    "trade_id", "symbol", "direction", "strategy", "sector", "contracts",
    "opened_at", "closed_at", "days_held", "entry_spot_price",
    "regime_at_entry", "rationale", "confidence",
    "adx_at_entry", "iv_rank_at_entry", "atr_pct_at_entry", "scanner_score_at_entry",
    "sizing_method_at_entry", "consecutive_losses_at_entry",
    "entry_credit_or_debit", "max_loss", "planned_stop_loss_level",
    "planned_profit_target_pct", "planned_reward_risk_ratio",
    "realized_pnl", "r_multiple", "return_pct_on_risk",
    "exit_reason", "hit_profit_target", "hit_stop_loss",
    "pct_of_target_captured", "trailing_stop_used", "lessons_learned",
]


def _ensure_file() -> None:
    if not JOURNAL_FILE.exists():
        JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def _lessons_learned(position: Position, exit_reason: str) -> str:
    """Deterministic, rule-based outcome note -- not a substitute for your
    own review, but gives every row a plain-English takeaway instead of
    just a P&L number, so a weekly scan of the CSV is actually readable."""
    pnl = position.realized_pnl or 0.0

    if "profit target" in exit_reason:
        return "Hit target as planned -- setup and exit rule worked as designed."
    if "trailing stop" in exit_reason:
        if pnl > 0:
            return (f"Trailing stop protected a gain after peaking at "
                    f"{position.high_water_mark_pct:.0%} of target; consider whether the "
                    f"activation threshold is capturing enough upside vs. exiting too early.")
        return "Trailing stop closed near breakeven after reversing from a partial gain."
    if "stop loss" in exit_reason:
        return (f"Stopped out at {position.stop_loss_level:.1f}x credit -- review whether "
                f"the regime read was wrong at entry or this was a normal, expected loser "
                f"within the strategy's win rate.")
    return f"Closed: {exit_reason}."


def log_closed_trade(position: Position, exit_reason: str) -> None:
    _ensure_file()
    from bot.sectors import get_sector

    days_held = (position.closed_at - position.opened_at).days if position.closed_at else 0
    r_multiple = (position.realized_pnl / position.max_loss) if position.max_loss else 0.0
    return_pct_on_risk = r_multiple

    planned_reward_risk = None
    risk_to_stop = max(position.planned_stop_loss_level - 1, 1e-6)
    if position.planned_profit_target_pct:
        planned_reward_risk = round(position.planned_profit_target_pct / risk_to_stop, 3)

    hit_profit_target = "profit target" in exit_reason
    hit_stop_loss = "stop loss" in exit_reason or "trailing stop" in exit_reason
    pct_of_target_captured = (
        round(position.high_water_mark_pct / position.profit_target_pct, 3)
        if position.profit_target_pct else 0.0
    )

    row = {
        "trade_id": position.id,
        "symbol": position.symbol,
        "direction": position.direction,
        "strategy": position.strategy.value,
        "sector": get_sector(position.symbol),
        "contracts": position.contracts,
        "opened_at": position.opened_at.isoformat(),
        "closed_at": position.closed_at.isoformat() if position.closed_at else "",
        "days_held": days_held,
        "entry_spot_price": position.entry_spot_price,
        "regime_at_entry": position.regime_at_entry,
        "rationale": position.rationale,
        "confidence": position.confidence,
        "adx_at_entry": position.adx_at_entry,
        "iv_rank_at_entry": position.iv_rank_at_entry,
        "atr_pct_at_entry": position.atr_pct_at_entry,
        "scanner_score_at_entry": position.scanner_score_at_entry,
        "sizing_method_at_entry": position.sizing_method_at_entry,
        "consecutive_losses_at_entry": position.consecutive_losses_at_entry,
        "entry_credit_or_debit": position.entry_credit_or_debit,
        "max_loss": position.max_loss,
        "planned_stop_loss_level": position.planned_stop_loss_level,
        "planned_profit_target_pct": position.planned_profit_target_pct,
        "planned_reward_risk_ratio": planned_reward_risk,
        "realized_pnl": position.realized_pnl,
        "r_multiple": round(r_multiple, 3),
        "return_pct_on_risk": round(return_pct_on_risk, 3),
        "exit_reason": exit_reason,
        "hit_profit_target": hit_profit_target,
        "hit_stop_loss": hit_stop_loss,
        "pct_of_target_captured": pct_of_target_captured,
        "trailing_stop_used": position.trailing_stop_active,
        "lessons_learned": _lessons_learned(position, exit_reason),
    }
    with open(JOURNAL_FILE, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(row)


@dataclass
class GoalStatus:
    metric: str
    target: float
    actual: float
    on_track: bool


@dataclass
class PerformanceReport:
    period: str
    total_trades: int
    win_rate: float
    expectancy_per_trade: float
    avg_r_multiple: float
    by_strategy: dict
    by_sector: dict
    by_regime: dict
    worst_trade_pnl: float
    best_trade_pnl: float
    goals: list


def _period_cutoff(period: str) -> Optional[datetime]:
    now = datetime.utcnow()
    if period == "weekly":
        return now - timedelta(days=7)
    if period == "monthly":
        return now - timedelta(days=30)
    return None


def _read_rows(period: str = "all") -> list:
    if not JOURNAL_FILE.exists():
        return []
    with open(JOURNAL_FILE, newline="") as f:
        rows = list(csv.DictReader(f))

    cutoff = _period_cutoff(period)
    if cutoff is None:
        return rows

    filtered = []
    for r in rows:
        if not r.get("closed_at"):
            continue
        try:
            closed = datetime.fromisoformat(r["closed_at"])
        except ValueError:
            continue
        if closed >= cutoff:
            filtered.append(r)
    return filtered


def generate_report(period: str = "all") -> Optional[PerformanceReport]:
    """period: 'all' | 'weekly' (trailing 7d) | 'monthly' (trailing 30d)."""
    rows = _read_rows(period)
    if not rows:
        return None

    pnls = [float(r["realized_pnl"]) for r in rows if r["realized_pnl"]]
    r_multiples = [float(r["r_multiple"]) for r in rows if r["r_multiple"]]
    wins = [p for p in pnls if p >= 0]

    by_strategy = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    by_sector = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    by_regime = defaultdict(lambda: {"trades": 0, "pnl": 0.0})
    for r, pnl in zip(rows, pnls):
        by_strategy[r["strategy"]]["trades"] += 1
        by_strategy[r["strategy"]]["pnl"] += pnl
        by_sector[r["sector"]]["trades"] += 1
        by_sector[r["sector"]]["pnl"] += pnl
        by_regime[r["regime_at_entry"]]["trades"] += 1
        by_regime[r["regime_at_entry"]]["pnl"] += pnl

    win_rate = len(wins) / len(pnls) if pnls else 0.0
    expectancy = sum(pnls) / len(pnls) if pnls else 0.0
    avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0

    goals = [
        GoalStatus("Win rate", ACCOUNT.target_win_rate, win_rate, win_rate >= ACCOUNT.target_win_rate),
        GoalStatus("Expectancy/trade", ACCOUNT.target_expectancy_per_trade, expectancy,
                   expectancy >= ACCOUNT.target_expectancy_per_trade),
        GoalStatus("Avg R-multiple", ACCOUNT.target_avg_r_multiple, avg_r, avg_r >= ACCOUNT.target_avg_r_multiple),
    ]

    return PerformanceReport(
        period=period,
        total_trades=len(rows),
        win_rate=win_rate,
        expectancy_per_trade=expectancy,
        avg_r_multiple=avg_r,
        by_strategy=dict(by_strategy),
        by_sector=dict(by_sector),
        by_regime=dict(by_regime),
        worst_trade_pnl=min(pnls) if pnls else 0.0,
        best_trade_pnl=max(pnls) if pnls else 0.0,
        goals=goals,
    )


def print_report(period: str = "all") -> None:
    from tabulate import tabulate

    report = generate_report(period)
    if report is None:
        print(f"No closed trades in period '{period}' -- journal is empty.")
        return

    label = {"all": "All-time", "weekly": "Trailing 7 days", "monthly": "Trailing 30 days"}[period]
    print(f"\n=== Performance Report: {label} ({report.total_trades} closed trades) ===")
    print(f"Win rate: {report.win_rate:.1%}")
    print(f"Expectancy/trade: ${report.expectancy_per_trade:.2f}")
    print(f"Avg R-multiple: {report.avg_r_multiple:.2f}R")
    print(f"Best / worst trade: ${report.best_trade_pnl:.2f} / ${report.worst_trade_pnl:.2f}")

    goal_rows = [[g.metric, f"{g.target:.2f}", f"{g.actual:.2f}", "ON TRACK" if g.on_track else "BELOW TARGET"]
                 for g in report.goals]
    print("\nGoals:")
    print(tabulate(goal_rows, headers=["Metric", "Target", "Actual", "Status"], tablefmt="simple"))

    strat_rows = [[k, v["trades"], f"${v['pnl']:.2f}"] for k, v in report.by_strategy.items()]
    print("\nBy strategy:")
    print(tabulate(strat_rows, headers=["Strategy", "Trades", "Total P&L"], tablefmt="simple"))

    sector_rows = [[k, v["trades"], f"${v['pnl']:.2f}"] for k, v in report.by_sector.items()]
    print("\nBy sector:")
    print(tabulate(sector_rows, headers=["Sector", "Trades", "Total P&L"], tablefmt="simple"))

    regime_rows = [[k, v["trades"], f"${v['pnl']:.2f}"] for k, v in report.by_regime.items()]
    print("\nBy regime at entry:")
    print(tabulate(regime_rows, headers=["Regime", "Trades", "Total P&L"], tablefmt="simple"))


# ---------------------------------------------------------------------------
# Visuals ("mark up charts" analog): equity curve + win/loss distribution
# ---------------------------------------------------------------------------

def generate_charts(starting_equity: float = None) -> Optional[dict]:
    """
    Saves an equity-curve PNG and a win/loss-distribution PNG to
    data/charts/. Returns their paths, or None if there's no trade history
    yet. Regenerated fresh each call (not incremental) -- journal sizes for
    a bot like this stay small enough that this is cheap.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = _read_rows("all")
    if not rows:
        return None

    starting_equity = starting_equity if starting_equity is not None else ACCOUNT.starting_equity
    rows_sorted = sorted(rows, key=lambda r: r["closed_at"])
    pnls = [float(r["realized_pnl"]) for r in rows_sorted if r["realized_pnl"]]

    equity_curve = [starting_equity]
    for pnl in pnls:
        equity_curve.append(equity_curve[-1] + pnl)

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(range(len(equity_curve)), equity_curve, linewidth=1.5)
    ax.axhline(starting_equity, color="gray", linestyle="--", linewidth=0.8, alpha=0.6)
    ax.set_title("Equity Curve")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Equity ($)")
    fig.tight_layout()
    equity_path = CHARTS_DIR / "equity_curve.png"
    fig.savefig(equity_path, dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    colors = ["#2ca02c" if p >= 0 else "#d62728" for p in pnls]
    ax.bar(range(len(pnls)), pnls, color=colors)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title("Trade P&L Distribution")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("P&L ($)")
    fig.tight_layout()
    dist_path = CHARTS_DIR / "pnl_distribution.png"
    fig.savefig(dist_path, dpi=120)
    plt.close(fig)

    return {"equity_curve": str(equity_path), "pnl_distribution": str(dist_path)}
