from bot.strategies.zero_dte import ZeroDteIronCondorStrategy
from bot.strategies.premium_selling import IronCondorStrategy
from bot.data_feed import SyntheticFeed
from bot.models import Regime, RegimeSnapshot, StrategyType


def make_snapshot(symbol="SPY", regime=Regime.HIGH_IV_RANGE, iv_rank=70.0, adx=15.0):
    return RegimeSnapshot(
        symbol=symbol, regime=regime, iv_rank=iv_rank, adx=adx,
        ma_slope=0.0, vix_level=16.0, vix_jump_1d_pct=0.0,
    )


def test_generates_signal_for_spy_in_high_iv_range():
    strategy = ZeroDteIronCondorStrategy()
    feed = SyntheticFeed()
    snapshot = make_snapshot()
    signal = strategy.generate_signal(snapshot, feed.history("SPY"))

    assert signal is not None
    assert signal.strategy == StrategyType.ZERO_DTE_IRON_CONDOR
    assert signal.days_to_expiration == 0
    assert len(signal.option_legs) == 4
    assert "_greeks_per_contract" in signal.__dict__


def test_returns_none_outside_high_iv_range():
    strategy = ZeroDteIronCondorStrategy()
    feed = SyntheticFeed()
    snapshot = make_snapshot(regime=Regime.TRENDING)
    assert strategy.generate_signal(snapshot, feed.history("SPY")) is None


def test_returns_none_for_non_whitelisted_symbol():
    strategy = ZeroDteIronCondorStrategy()
    feed = SyntheticFeed()
    snapshot = make_snapshot(symbol="AAPL")
    assert strategy.generate_signal(snapshot, feed.history("AAPL")) is None


def test_short_strikes_tighter_than_45dte_condor():
    feed = SyntheticFeed()
    snapshot = make_snapshot()
    price_history = feed.history("SPY")
    spot = float(price_history["Close"].iloc[-1])

    zero_dte_signal = ZeroDteIronCondorStrategy().generate_signal(snapshot, price_history)
    regular_signal = IronCondorStrategy().generate_signal(snapshot, price_history)

    def short_call_strike(signal):
        return next(leg.strike for leg in signal.option_legs if leg.is_call and leg.side == "sell")

    zero_dte_distance = abs(short_call_strike(zero_dte_signal) - spot)
    regular_distance = abs(short_call_strike(regular_signal) - spot)
    assert zero_dte_distance < regular_distance
