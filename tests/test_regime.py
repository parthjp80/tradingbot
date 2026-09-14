from bot.regime import RegimeClassifier
from bot.models import Regime
from bot.config import REGIME


def test_crisis_overrides_everything():
    r = RegimeClassifier._classify_from_metrics(adx_val=10, iv_rank=20, vix_level=35, vix_jump=0.01)
    assert r == Regime.CRISIS

    r2 = RegimeClassifier._classify_from_metrics(adx_val=10, iv_rank=20, vix_level=18, vix_jump=0.15)
    assert r2 == Regime.CRISIS


def test_trending_detected_before_iv_check():
    r = RegimeClassifier._classify_from_metrics(adx_val=30, iv_rank=80, vix_level=18, vix_jump=0.0)
    assert r == Regime.TRENDING


def test_high_iv_range_when_choppy_and_rich():
    r = RegimeClassifier._classify_from_metrics(adx_val=15, iv_rank=65, vix_level=18, vix_jump=0.0)
    assert r == Regime.HIGH_IV_RANGE


def test_low_iv_range_when_choppy_and_cheap():
    r = RegimeClassifier._classify_from_metrics(adx_val=15, iv_rank=15, vix_level=18, vix_jump=0.0)
    assert r == Regime.LOW_IV_RANGE


def test_synthetic_feed_end_to_end():
    from bot.data_feed import SyntheticFeed
    feed = SyntheticFeed()
    classifier = RegimeClassifier(feed)
    snapshot = classifier.classify("TESTSYM")
    assert snapshot.regime in list(Regime)
    assert 0 <= snapshot.iv_rank <= 100
