"""
Automated pre-entry validation against the trader's own "A+ Setup
Checklist" (Premium-Selling half) for short strangles, iron condors, and
credit verticals. The Momentum/Low-Float half of the same paper checklist
is wired into lowfloat_trader's bot.py instead -- that bot only ever takes
low-float momentum longs/shorts and never touches options, exactly the
mirror image of this one, so each bot gets only the half that applies to
its own strategy family.

Every check below reads a field the scanner or strategy layer already
computes -- nothing here re-derives its own market data or hardcodes a
guess at what "good" looks like beyond the thresholds documented next to
each check. Two liquidity items ("tight bid/ask spreads" and "enough OI
to roll") collapse onto one proxy signal (20-day avg $ volume of the
underlying) at two strictness bars: this bot's data feed (yfinance) does
not carry a live options chain, so real spread/OI numbers don't exist to
check. A couple of items describe things this bot's architecture
guarantees unconditionally by construction (a management plan, a capped
per-trade risk budget) -- they're marked True with a note rather than
computing a signal that would always evaluate the same way regardless of
the candidate.

A signal needs a clean 13/13 to size up. The paper checklist allows "all
but one, with a good reason" -- there's no automated stand-in for judging
a "good reason", so this validator doesn't grant itself one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from bot.config import ACCOUNT, SCANNER
from bot.models import StrategyType, TradeSignal

if TYPE_CHECKING:
    from bot.scanner import ScanResult

# The paper checklist's own scope: "Strangles / Condors / Verticals /
# Covered Calls". This bot doesn't implement covered calls; futures-trend
# and 0DTE condors carry a different risk/DTE thesis entirely and are
# deliberately out of scope rather than forced through a mismatched check.
CHECKLIST_STRATEGIES = {
    StrategyType.SHORT_STRANGLE,
    StrategyType.IRON_CONDOR,
    StrategyType.SHORT_PUT_VERTICAL,
    StrategyType.SHORT_CALL_VERTICAL,
}

# A+-specific thresholds -- deliberately stricter than the scanner/strategy
# floors that just get a candidate onto the watchlist in the first place.
# Reusing those same floors here would make most of this checklist a
# no-op: anything that reached signal generation would trivially "pass".
IV_RANK_APLUS_MIN = 65.0                 # vs REGIME.iv_rank_high=50 to merely classify HIGH_IV_RANGE
STRANGLE_IV_RANK_APLUS_MIN = 75.0        # stranger's own richness proxy -- see p-richpremium below
RICHNESS_MULTIPLE = 1.5                  # x the strategy's configured min_reward_risk_ratio
LIQUIDITY_SPREAD_MULTIPLE = 1.5          # x SCANNER.min_avg_dollar_volume
LIQUIDITY_OI_MULTIPLE = 3.0              # x SCANNER.min_avg_dollar_volume -- stricter: "enough to roll/adjust"
NEWS_COUNT_APLUS_MAX = max(1, SCANNER.max_recent_news_count // 2)
CONFIDENCE_APLUS_MIN = 0.75
EXPIRATION_WINDOWS = {
    # (min, max) days-to-expiration that matches each strategy's own thesis.
    # Both bots hardcode a single DTE constant per strategy today, so this
    # mostly guards against a future config change drifting the DTE away
    # from the thesis it was built for.
    StrategyType.SHORT_STRANGLE: (30, 60),
    StrategyType.IRON_CONDOR: (30, 60),
    StrategyType.SHORT_PUT_VERTICAL: (20, 40),
    StrategyType.SHORT_CALL_VERTICAL: (20, 40),
}


@dataclass
class ChecklistItem:
    id: str
    group: str
    text: str


PREMIUM_CHECKLIST: list[ChecklistItem] = [
    ChecklistItem("p-ivrank", "Volatility", "IV Rank is elevated relative to this ticker's own history"),
    ChecklistItem("p-richpremium", "Volatility", "Selling rich premium, not cheap premium disguised as still decent"),
    ChecklistItem("p-spreads", "Liquidity", "Underlying liquidity implies a tight options bid/ask"),
    ChecklistItem("p-oi", "Liquidity", "Enough liquidity to roll or adjust without slippage"),
    ChecklistItem("p-noevent", "Event Risk", "No earnings or ex-dividend event inside the expiration"),
    ChecklistItem("p-nobinary", "Event Risk", "No elevated news-driven event risk"),
    ChecklistItem("p-strikesbacked", "Structure", "Strikes aren't sitting inside a coiled range about to break out"),
    ChecklistItem("p-mgmtplan", "Structure", "Defined management/adjustment plan exists before entry"),
    ChecklistItem("p-expmatches", "Structure", "Expiration timeframe matches the strategy's thesis"),
    ChecklistItem("p-sizingsmall", "Risk/Reward", "Position sizing keeps max loss to a small % of account"),
    ChecklistItem("p-comfortable", "Risk/Reward", "Volatility is in a normal range, not already elevated"),
    ChecklistItem("p-again", "Gut Check", "Conviction (confidence score) is high enough to take again tomorrow"),
    ChecklistItem("p-notbored", "Gut Check", "Setup quality clears the scanner's own A+ bar, not just barely qualifies"),
]

_LABELS = {item.id: item.text for item in PREMIUM_CHECKLIST}
_TOTAL_ITEMS = len(PREMIUM_CHECKLIST)


@dataclass
class ChecklistResult:
    checked: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.checked) + len(self.missed)

    @property
    def passed(self) -> bool:
        return self.total == _TOTAL_ITEMS and not self.missed

    def missed_labels(self) -> list[str]:
        return [_LABELS[item_id] for item_id in self.missed]


def evaluate_premium_checklist(scan_result: "ScanResult", signal: TradeSignal) -> ChecklistResult:
    """
    Grades one scanner candidate + its generated signal against
    PREMIUM_CHECKLIST. Only a clean 13/13 reports `passed` -- see the
    module docstring on why there's no automated stand-in for "one miss,
    with a good reason".
    """
    result = ChecklistResult()
    snapshot = scan_result.snapshot
    tech = scan_result.technicals

    def check(item_id: str, ok: bool) -> None:
        (result.checked if ok else result.missed).append(item_id)

    # -- Volatility --------------------------------------------------------
    check("p-ivrank", snapshot is not None and snapshot.iv_rank >= IV_RANK_APLUS_MIN)

    if signal.strategy == StrategyType.SHORT_STRANGLE:
        # ShortStrangleStrategy prices max_loss as a fixed multiple of its
        # own credit (see strategies/premium_selling.py) -- credit/max_loss
        # is tautologically constant for every strangle, so it carries no
        # richness signal for this one strategy. Fall back to a stricter
        # IV-rank bar, since IV rank is what actually drives how rich an
        # undefined-risk strangle's credit is.
        rich_ok = snapshot is not None and snapshot.iv_rank >= STRANGLE_IV_RANK_APLUS_MIN
    else:
        min_rr = ACCOUNT.min_reward_risk_ratio.get(signal.strategy.value)
        rich_ok = (
            min_rr is not None
            and signal.est_max_loss > 0
            and (signal.est_credit_or_risk / signal.est_max_loss) >= min_rr * RICHNESS_MULTIPLE
        )
    check("p-richpremium", rich_ok)

    # -- Liquidity -- (proxy: no live options chain in this data feed, so
    # equity $ volume stands in for spread/OI at two strictness bars)
    check("p-spreads", scan_result.avg_dollar_volume >= SCANNER.min_avg_dollar_volume * LIQUIDITY_SPREAD_MULTIPLE)
    check("p-oi", scan_result.avg_dollar_volume >= SCANNER.min_avg_dollar_volume * LIQUIDITY_OI_MULTIPLE)

    # -- Event risk -- (the scanner's own earnings/ex-div blackout is only
    # 7/3 days -- a 30-60 DTE trade can still have earnings land mid-trade
    # without tripping that filter at all. Require NO earnings before
    # expiration, not just outside the short blackout window.)
    no_earnings_in_trade = (
        scan_result.days_to_earnings is None
        or scan_result.days_to_earnings > signal.days_to_expiration
    )
    check("p-noevent", no_earnings_in_trade)
    check(
        "p-nobinary",
        scan_result.recent_news_count is None or scan_result.recent_news_count <= NEWS_COUNT_APLUS_MAX,
    )

    # -- Structure -----------------------------------------------------------
    check("p-strikesbacked", tech is not None and not tech.bb_squeeze)
    check("p-mgmtplan", True)  # every position gets get_exit_rules()'s stop/target unconditionally
    lo, hi = EXPIRATION_WINDOWS.get(signal.strategy, (0, 10_000))
    check("p-expmatches", lo <= signal.days_to_expiration <= hi)

    # -- Risk/Reward -----------------------------------------------------------
    check("p-sizingsmall", True)  # fixed-fractional/half-Kelly + aplus_max_risk_per_trade_pct hard cap, always
    check("p-comfortable", tech is not None and tech.atr_pct < ACCOUNT.high_vol_atr_pct_threshold)

    # -- Gut check -----------------------------------------------------------
    check("p-again", signal.confidence >= CONFIDENCE_APLUS_MIN)
    check("p-notbored", scan_result.score >= ACCOUNT.aplus_score_threshold)

    return result
