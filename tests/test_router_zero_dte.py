from datetime import datetime

from bot.strategies.router import StrategyRouter
from bot.data_feed import SyntheticFeed
from bot.models import Regime, RegimeSnapshot, StrategyType
from bot.market_hours import EASTERN

INSIDE_WINDOW = datetime(2026, 9, 14, 10, 30, tzinfo=EASTERN)
OUTSIDE_WINDOW = datetime(2026, 9, 14, 13, 0, tzinfo=EASTERN)


def make_snapshot(symbol, regime, iv_rank=70.0, adx=15.0, ma_slope=0.0):
    return RegimeSnapshot(
        symbol=symbol, regime=regime, iv_rank=iv_rank, adx=adx,
        ma_slope=ma_slope, vix_level=16.0, vix_jump_1d_pct=0.0,
    )


def test_spy_routes_to_0dte_in_entry_window_high_iv():
    router = StrategyRouter()
    feed = SyntheticFeed()
    snapshot = make_snapshot("SPY", Regime.HIGH_IV_RANGE)
    signal = router.route(snapshot, "equity_option", feed.history("SPY"), now=INSIDE_WINDOW)
    assert signal is not None
    assert signal.strategy == StrategyType.ZERO_DTE_IRON_CONDOR


def test_spy_routes_to_regular_iron_condor_outside_entry_window():
    router = StrategyRouter()
    feed = SyntheticFeed()
    snapshot = make_snapshot("SPY", Regime.HIGH_IV_RANGE)
    signal = router.route(snapshot, "equity_option", feed.history("SPY"), now=OUTSIDE_WINDOW)
    assert signal is not None
    assert signal.strategy == StrategyType.IRON_CONDOR


def test_spy_routes_to_short_vertical_when_trending():
    router = StrategyRouter()
    feed = SyntheticFeed()
    # strong ADX so ShortVerticalStrategy's own confidence floor doesn't reject it
    snapshot = make_snapshot("SPY", Regime.TRENDING, adx=40.0, ma_slope=0.02)
    signal = router.route(snapshot, "equity_option", feed.history("SPY"), now=INSIDE_WINDOW)
    assert signal is not None
    assert signal.strategy in (StrategyType.SHORT_PUT_VERTICAL, StrategyType.SHORT_CALL_VERTICAL)


def test_non_whitelisted_symbol_never_gets_0dte():
    router = StrategyRouter()
    feed = SyntheticFeed()
    snapshot = make_snapshot("AAPL", Regime.HIGH_IV_RANGE)
    signal = router.route(snapshot, "equity_option", feed.history("AAPL"), now=INSIDE_WINDOW)
    assert signal is not None
    assert signal.strategy == StrategyType.IRON_CONDOR
