import pandas as pd
from bot.scanner import MarketScanner, TechnicalFlags
from bot.regime import RegimeClassifier
from bot.data_feed import SyntheticFeed
from bot.models import Regime, RegimeSnapshot
from bot.fundamentals import FundamentalMetrics
from bot.config import SCANNER


def make_scanner():
    feed = SyntheticFeed()
    classifier = RegimeClassifier(feed)
    return MarketScanner(feed, classifier), feed


NEUTRAL_TECH = TechnicalFlags(atr_pct=2.0, macd_bullish=True, bb_squeeze=False, volume_spike=False)


def test_low_price_is_filtered():
    scanner, feed = make_scanner()
    df = feed.history("PENNY")
    df["Close"] = 2.0
    reason = scanner._passes_filters("PENNY", df, NEUTRAL_TECH, None)
    assert reason is not None and "price" in reason


def test_low_liquidity_is_filtered():
    scanner, feed = make_scanner()
    df = feed.history("THIN")
    df["Volume"] = 100
    reason = scanner._passes_filters("THIN", df, NEUTRAL_TECH, None)
    assert reason is not None and "volume" in reason


def test_low_atr_is_filtered():
    scanner, feed = make_scanner()
    df = feed.history("FLAT")
    flat_tech = TechnicalFlags(atr_pct=0.1, macd_bullish=True, bb_squeeze=False, volume_spike=False)
    reason = scanner._passes_filters("FLAT", df, flat_tech, None)
    assert reason is not None and "ATR" in reason


def test_earnings_blackout_is_filtered(monkeypatch):
    scanner, feed = make_scanner()
    df = feed.history("SOON")
    monkeypatch.setattr(feed, "days_to_next_earnings", lambda s: 2)
    monkeypatch.setattr(feed, "days_to_ex_dividend", lambda s: None)
    reason = scanner._passes_filters("SOON", df, NEUTRAL_TECH, None)
    assert reason is not None and "earnings" in reason


def test_ex_dividend_blackout_is_filtered(monkeypatch):
    scanner, feed = make_scanner()
    df = feed.history("DIVSOON")
    monkeypatch.setattr(feed, "days_to_next_earnings", lambda s: 45)
    monkeypatch.setattr(feed, "days_to_ex_dividend", lambda s: 1)
    reason = scanner._passes_filters("DIVSOON", df, NEUTRAL_TECH, None)
    assert reason is not None and "ex-dividend" in reason


def test_fundamental_gate_disabled_by_default():
    scanner, feed = make_scanner()
    df = feed.history("EXPENSIVE")
    rich_pe = FundamentalMetrics(pe_ratio=200.0)
    # SCANNER.max_pe_ratio is None by default -- should not reject
    assert scanner._fails_fundamentals(rich_pe) is None


def test_fundamental_gate_when_enabled():
    scanner, feed = make_scanner()
    original = SCANNER.max_pe_ratio
    try:
        SCANNER.max_pe_ratio = 40.0
        reason = scanner._fails_fundamentals(FundamentalMetrics(pe_ratio=80.0))
        assert reason is not None and "P/E" in reason
        assert scanner._fails_fundamentals(FundamentalMetrics(pe_ratio=20.0)) is None
    finally:
        SCANNER.max_pe_ratio = original


def test_clean_candidate_passes_filters(monkeypatch):
    scanner, feed = make_scanner()
    df = feed.history("CLEAN")
    df["Close"] = 100.0
    df["Volume"] = 5_000_000
    monkeypatch.setattr(feed, "days_to_next_earnings", lambda s: 45)
    monkeypatch.setattr(feed, "days_to_ex_dividend", lambda s: None)
    reason = scanner._passes_filters("CLEAN", df, NEUTRAL_TECH, None)
    assert reason is None


def test_scoring_prefers_higher_iv_rank_in_high_iv_regime():
    rich = RegimeSnapshot(symbol="A", regime=Regime.HIGH_IV_RANGE, iv_rank=90, adx=12,
                           ma_slope=0.0, vix_level=16, vix_jump_1d_pct=0.0)
    cheap = RegimeSnapshot(symbol="B", regime=Regime.HIGH_IV_RANGE, iv_rank=55, adx=12,
                            ma_slope=0.0, vix_level=16, vix_jump_1d_pct=0.0)
    assert MarketScanner._score(rich, NEUTRAL_TECH, None) > MarketScanner._score(cheap, NEUTRAL_TECH, None)


def test_bb_squeeze_penalized_in_high_iv_range():
    snap = RegimeSnapshot(symbol="A", regime=Regime.HIGH_IV_RANGE, iv_rank=80, adx=12,
                           ma_slope=0.0, vix_level=16, vix_jump_1d_pct=0.0)
    calm = TechnicalFlags(atr_pct=1.5, macd_bullish=True, bb_squeeze=False, volume_spike=False)
    squeezed = TechnicalFlags(atr_pct=1.5, macd_bullish=True, bb_squeeze=True, volume_spike=False)
    assert MarketScanner._score(snap, calm, None) > MarketScanner._score(snap, squeezed, None)


def test_trend_confirmation_bonus_in_trending_regime():
    up_trend = RegimeSnapshot(symbol="A", regime=Regime.TRENDING, iv_rank=50, adx=30,
                               ma_slope=0.03, vix_level=16, vix_jump_1d_pct=0.0)
    confirmed = TechnicalFlags(atr_pct=2.0, macd_bullish=True, bb_squeeze=False, volume_spike=False)
    contradicted = TechnicalFlags(atr_pct=2.0, macd_bullish=False, bb_squeeze=False, volume_spike=False)
    assert MarketScanner._score(up_trend, confirmed, None) > MarketScanner._score(up_trend, contradicted, None)


def test_breakout_combo_bonus_in_trending_regime():
    snap = RegimeSnapshot(symbol="A", regime=Regime.TRENDING, iv_rank=50, adx=30,
                           ma_slope=0.03, vix_level=16, vix_jump_1d_pct=0.0)
    no_breakout = TechnicalFlags(atr_pct=2.0, macd_bullish=True, bb_squeeze=False, volume_spike=False)
    breakout = TechnicalFlags(atr_pct=2.0, macd_bullish=True, bb_squeeze=True, volume_spike=True)
    assert MarketScanner._score(snap, breakout, None) > MarketScanner._score(snap, no_breakout, None)


def test_elevated_news_count_penalized():
    snap = RegimeSnapshot(symbol="A", regime=Regime.HIGH_IV_RANGE, iv_rank=80, adx=12,
                           ma_slope=0.0, vix_level=16, vix_jump_1d_pct=0.0)
    quiet_score = MarketScanner._score(snap, NEUTRAL_TECH, news_count=1)
    loud_score = MarketScanner._score(snap, NEUTRAL_TECH, news_count=SCANNER.max_recent_news_count + 5)
    assert quiet_score > loud_score


def test_untradeable_regimes_score_lowest():
    low_iv = RegimeSnapshot(symbol="A", regime=Regime.LOW_IV_RANGE, iv_rank=10, adx=10,
                             ma_slope=0.0, vix_level=16, vix_jump_1d_pct=0.0)
    trending = RegimeSnapshot(symbol="C", regime=Regime.TRENDING, iv_rank=50, adx=26,
                               ma_slope=0.02, vix_level=16, vix_jump_1d_pct=0.0)
    assert MarketScanner._score(low_iv, NEUTRAL_TECH, None) < MarketScanner._score(trending, NEUTRAL_TECH, None)


def test_top_candidates_respects_limit():
    scanner, feed = make_scanner()
    universe = [f"SYM{i}" for i in range(15)]
    picks = scanner.top_candidates(symbols=universe, limit=3)
    assert len(picks) <= 3
    for item in picks:
        assert item.asset_class == "equity_option"


def test_full_scan_end_to_end_runs_without_error():
    scanner, feed = make_scanner()
    results = scanner.scan(symbols=["A", "B", "C", "D", "E"])
    assert len(results) == 5
    for r in results:
        assert r.symbol in ("A", "B", "C", "D", "E")


# ---- premium-selling price cap ------------------------------------------

def _prep_scan_target(scanner, feed, symbol, close_price, regime):
    """Helper: force a symbol's last close price and regime for a
    deterministic scan() test, while leaving liquidity/earnings/dividend
    filters using safe pass-through values."""
    df = feed.history(symbol).copy()
    df.loc[df.index[-1], "Close"] = close_price
    scanner.feed.history = lambda s, **kw: df if s == symbol else feed.history(s, **kw)
    scanner.feed.days_to_next_earnings = lambda s: 45
    scanner.feed.days_to_ex_dividend = lambda s: None

    snapshot = RegimeSnapshot(
        symbol=symbol, regime=regime, iv_rank=80.0, adx=15.0,
        ma_slope=0.01, vix_level=15.0, vix_jump_1d_pct=0.0,
    )
    scanner.classifier.classify = lambda s: snapshot if s == symbol else scanner.classifier.classify(s)
    return df


def test_premium_selling_price_cap_rejects_expensive_underlying(monkeypatch):
    scanner, feed = make_scanner()
    original_cap = SCANNER.max_underlying_price_for_premium_selling
    try:
        SCANNER.max_underlying_price_for_premium_selling = 300.0
        _prep_scan_target(scanner, feed, "EXPENSIVE", close_price=500.0, regime=Regime.HIGH_IV_RANGE)

        results = scanner.scan(["EXPENSIVE"])
        assert results[0].reject_reason is not None
        assert "exceeds" in results[0].reject_reason
        assert "$300" in results[0].reject_reason
    finally:
        SCANNER.max_underlying_price_for_premium_selling = original_cap


def test_premium_selling_price_cap_allows_cheap_underlying(monkeypatch):
    scanner, feed = make_scanner()
    original_cap = SCANNER.max_underlying_price_for_premium_selling
    try:
        SCANNER.max_underlying_price_for_premium_selling = 300.0
        _prep_scan_target(scanner, feed, "CHEAP", close_price=100.0, regime=Regime.HIGH_IV_RANGE)

        results = scanner.scan(["CHEAP"])
        assert results[0].passed
    finally:
        SCANNER.max_underlying_price_for_premium_selling = original_cap


def test_premium_selling_price_cap_does_not_apply_to_trending_regime(monkeypatch):
    scanner, feed = make_scanner()
    original_cap = SCANNER.max_underlying_price_for_premium_selling
    try:
        SCANNER.max_underlying_price_for_premium_selling = 300.0
        # expensive underlying, but TRENDING regime (verticals) -- should NOT
        # be rejected on price, since only premium-selling (HIGH_IV_RANGE) is gated
        _prep_scan_target(scanner, feed, "EXPENSIVE_TREND", close_price=500.0, regime=Regime.TRENDING)

        results = scanner.scan(["EXPENSIVE_TREND"])
        assert results[0].passed
    finally:
        SCANNER.max_underlying_price_for_premium_selling = original_cap
