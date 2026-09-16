from bot.aplus_checklist import (
    PREMIUM_CHECKLIST,
    evaluate_premium_checklist,
)
from bot.config import ACCOUNT, SCANNER
from bot.models import Regime, RegimeSnapshot, StrategyType, TradeSignal
from bot.scanner import ScanResult, TechnicalFlags


def _snapshot(**overrides):
    defaults = dict(
        symbol="TEST", regime=Regime.HIGH_IV_RANGE, iv_rank=80.0,
        adx=15.0, ma_slope=0.0, vix_level=16.0, vix_jump_1d_pct=0.0,
    )
    defaults.update(overrides)
    return RegimeSnapshot(**defaults)


def _scan_result(**overrides):
    defaults = dict(
        symbol="TEST",
        snapshot=_snapshot(),
        avg_dollar_volume=SCANNER.min_avg_dollar_volume * 5,
        days_to_earnings=90,
        days_to_ex_dividend=90,
        recent_news_count=0,
        fundamentals=None,
        technicals=TechnicalFlags(atr_pct=1.5, macd_bullish=True, bb_squeeze=False, volume_spike=False),
        score=ACCOUNT.aplus_score_threshold + 5,
    )
    defaults.update(overrides)
    return ScanResult(**defaults)


def _iron_condor_signal(**overrides):
    defaults = dict(
        symbol="TEST", strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
        direction="neutral", est_credit_or_risk=140.0, est_max_loss=350.0,
        confidence=0.9, rationale="", days_to_expiration=45,
    )
    defaults.update(overrides)
    return TradeSignal(**defaults)


def test_checklist_has_thirteen_items():
    assert len(PREMIUM_CHECKLIST) == 13


def test_clean_iron_condor_passes_all_thirteen():
    result = evaluate_premium_checklist(_scan_result(), _iron_condor_signal())
    assert result.passed
    assert result.missed == []
    assert result.total == 13


def test_earnings_inside_expiration_blocks_even_outside_short_blackout():
    # 20 days out clears the scanner's own 7-day earnings blackout but
    # still lands well inside a 45 DTE trade -- this is exactly the gap
    # the checklist's event-risk item exists to catch.
    scan = _scan_result(days_to_earnings=20)
    result = evaluate_premium_checklist(scan, _iron_condor_signal())
    assert not result.passed
    assert "p-noevent" in result.missed


def test_bb_squeeze_blocks_structure_item():
    scan = _scan_result(technicals=TechnicalFlags(atr_pct=1.5, macd_bullish=True, bb_squeeze=True, volume_spike=False))
    result = evaluate_premium_checklist(scan, _iron_condor_signal())
    assert "p-strikesbacked" in result.missed


def test_thin_liquidity_blocks_both_liquidity_items():
    scan = _scan_result(avg_dollar_volume=SCANNER.min_avg_dollar_volume * 1.1)
    result = evaluate_premium_checklist(scan, _iron_condor_signal())
    assert "p-spreads" in result.missed
    assert "p-oi" in result.missed


def test_low_iv_rank_blocks_volatility_item():
    scan = _scan_result(snapshot=_snapshot(iv_rank=40.0))
    result = evaluate_premium_checklist(scan, _iron_condor_signal())
    assert "p-ivrank" in result.missed


def test_short_strangle_richness_uses_iv_rank_not_credit_ratio():
    # ShortStrangleStrategy defines max_loss as credit*4 -- a constant
    # 0.25 ratio regardless of setup quality -- so richness must fall
    # back to a stricter IV-rank bar instead of the credit/max_loss test
    # used for defined-risk strategies.
    strangle_signal = TradeSignal(
        symbol="TEST", strategy=StrategyType.SHORT_STRANGLE, regime=Regime.HIGH_IV_RANGE,
        direction="neutral", est_credit_or_risk=100.0, est_max_loss=400.0,
        confidence=0.9, rationale="", days_to_expiration=45,
    )
    rich_scan = _scan_result(snapshot=_snapshot(iv_rank=80.0))
    lean_scan = _scan_result(snapshot=_snapshot(iv_rank=68.0))

    assert "p-richpremium" not in evaluate_premium_checklist(rich_scan, strangle_signal).missed
    assert "p-richpremium" in evaluate_premium_checklist(lean_scan, strangle_signal).missed


def test_missing_snapshot_never_crashes_and_fails_closed():
    scan = _scan_result(snapshot=None)
    result = evaluate_premium_checklist(scan, _iron_condor_signal())
    assert not result.passed
    assert "p-ivrank" in result.missed
