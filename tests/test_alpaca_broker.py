"""
These tests mock TradingClient / OptionHistoricalDataClient so nothing
hits the network, but use the REAL alpaca-py request/enum classes
(OptionLegRequest, LimitOrderRequest, PositionIntent, etc.) so a wrong
field name or type fails loudly via pydantic validation instead of
silently passing a mock. That's the strongest correctness check available
without a live paper account.
"""
from types import SimpleNamespace
from datetime import datetime, timedelta
import pytest

from bot.config import ALPACA
from bot.models import Position, StrategyType, TradeSignal, Regime, OptionLegSpec


def make_fake_contract(symbol, strike, open_interest="100"):
    return SimpleNamespace(symbol=symbol, strike_price=strike, open_interest=open_interest)


@pytest.fixture(autouse=True)
def alpaca_creds(monkeypatch):
    monkeypatch.setattr(ALPACA, "api_key", "test_key")
    monkeypatch.setattr(ALPACA, "secret_key", "test_secret")
    yield


@pytest.fixture
def broker(monkeypatch, tmp_path):
    from bot.risk_manager import RiskManager
    import bot.alpaca_broker as ab_module

    monkeypatch.setattr(ab_module, "STATE_FILE", tmp_path / "alpaca_positions.json")

    import bot.journal as journal_module
    monkeypatch.setattr(journal_module, "JOURNAL_FILE", tmp_path / "trade_journal.csv")

    fake_trade_client = SimpleNamespace(
        get_option_contracts=lambda req: None,
        submit_order=lambda req: None,
        get_order_by_id=lambda oid: None,
    )
    fake_data_client = SimpleNamespace(get_option_latest_quote=lambda req: None)

    monkeypatch.setattr("alpaca.trading.client.TradingClient", lambda *a, **kw: fake_trade_client)
    monkeypatch.setattr("alpaca.data.historical.option.OptionHistoricalDataClient", lambda *a, **kw: fake_data_client)

    rm = RiskManager(equity=100_000)
    b = ab_module.AlpacaBroker(rm)
    b.trade_client = fake_trade_client
    b.data_client = fake_data_client
    return b, rm, fake_trade_client, fake_data_client


def make_iron_condor_signal(symbol="AAPL"):
    return TradeSignal(
        symbol=symbol, strategy=StrategyType.IRON_CONDOR, regime=Regime.HIGH_IV_RANGE,
        direction="neutral", est_credit_or_risk=300.0, est_max_loss=1200.0, confidence=0.9,
        rationale="test condor", atr_pct=2.0, days_to_expiration=45,
        option_legs=[
            OptionLegSpec(strike=110.0, is_call=True, side="sell"),
            OptionLegSpec(strike=115.0, is_call=True, side="buy"),
            OptionLegSpec(strike=90.0, is_call=False, side="sell"),
            OptionLegSpec(strike=85.0, is_call=False, side="buy"),
        ],
    )


def make_futures_signal():
    return TradeSignal(
        symbol="MES", strategy=StrategyType.FUTURES_TREND, regime=Regime.TRENDING,
        direction="long", est_credit_or_risk=0.0, est_max_loss=500.0, confidence=0.7,
        rationale="test futures", atr_pct=1.5, days_to_expiration=0, option_legs=[],
    )


def test_find_contract_picks_nearest_strike(broker):
    b, rm, trade_client, _ = broker
    contracts = [
        make_fake_contract("AAPL_C_108", 108.0),
        make_fake_contract("AAPL_C_110", 110.0),
        make_fake_contract("AAPL_C_120", 120.0),
    ]
    trade_client.get_option_contracts = lambda req: SimpleNamespace(option_contracts=contracts)

    result = b.find_contract("AAPL", is_call=True, target_strike=109.0, days_to_expiration=45)
    assert result.symbol == "AAPL_C_108"


def test_find_contract_returns_none_when_no_contracts(broker):
    b, rm, trade_client, _ = broker
    trade_client.get_option_contracts = lambda req: SimpleNamespace(option_contracts=[])
    result = b.find_contract("AAPL", is_call=True, target_strike=109.0, days_to_expiration=45)
    assert result is None


def test_find_contract_handles_string_open_interest(broker):
    """Regression test: Alpaca's real OptionContract.open_interest is
    typed Optional[str], not int -- ties on strike distance must sort
    correctly without crashing on the string-to-negative-int coercion."""
    b, rm, trade_client, _ = broker
    contracts = [
        make_fake_contract("AAPL_C_110_LOW_OI", 110.0, open_interest="5"),
        make_fake_contract("AAPL_C_110_HIGH_OI", 110.0, open_interest="500"),
    ]
    trade_client.get_option_contracts = lambda req: SimpleNamespace(option_contracts=contracts)

    result = b.find_contract("AAPL", is_call=True, target_strike=110.0, days_to_expiration=45)
    assert result.symbol == "AAPL_C_110_HIGH_OI"  # same strike distance, higher OI wins


def test_find_contract_handles_none_and_empty_open_interest(broker):
    b, rm, trade_client, _ = broker
    contracts = [
        make_fake_contract("AAPL_C_110_NONE_OI", 110.0, open_interest=None),
        make_fake_contract("AAPL_C_112_EMPTY_OI", 112.0, open_interest=""),
    ]
    trade_client.get_option_contracts = lambda req: SimpleNamespace(option_contracts=contracts)

    # neither should crash; nearest-strike (110.0) should still win despite
    # having a None open_interest rather than a parseable number
    result = b.find_contract("AAPL", is_call=True, target_strike=110.0, days_to_expiration=45)
    assert result.symbol == "AAPL_C_110_NONE_OI"


def test_find_contract_uses_real_request_class(broker):
    b, rm, trade_client, _ = broker
    captured = {}

    def fake_get_contracts(req):
        captured["req"] = req
        return SimpleNamespace(option_contracts=[])

    trade_client.get_option_contracts = fake_get_contracts
    b.find_contract("AAPL", is_call=True, target_strike=100.0, days_to_expiration=30)

    from alpaca.trading.requests import GetOptionContractsRequest
    assert isinstance(captured["req"], GetOptionContractsRequest)
    assert captured["req"].underlying_symbols == ["AAPL"]


def test_open_position_rejects_futures(broker):
    b, rm, trade_client, _ = broker
    submitted = []
    trade_client.submit_order = lambda req: submitted.append(req) or None

    signal = make_futures_signal()
    result = b.open_position(
        symbol="MES", strategy=StrategyType.FUTURES_TREND, credit_or_debit=0.0, max_loss=500.0,
        contracts=1, stop_loss_multiple=2.0, profit_target_pct=0.5, signal=signal,
    )
    assert result is None
    assert submitted == []


def test_open_position_rejects_missing_option_legs(broker):
    b, rm, trade_client, _ = broker
    signal = make_iron_condor_signal()
    signal.option_legs = []
    result = b.open_position(
        symbol="AAPL", strategy=StrategyType.IRON_CONDOR, credit_or_debit=300.0, max_loss=1200.0,
        contracts=1, stop_loss_multiple=2.0, profit_target_pct=0.5, signal=signal,
    )
    assert result is None


def test_open_position_builds_correct_mleg_request(broker):
    b, rm, trade_client, _ = broker

    def fake_get_contracts(req):
        mid_strike = (float(req.strike_price_gte) + float(req.strike_price_lte)) / 2
        return SimpleNamespace(option_contracts=[make_fake_contract(f"AAPL_{mid_strike:.0f}", mid_strike)])

    captured_orders = []

    def fake_submit(req):
        captured_orders.append(req)
        return SimpleNamespace(id="order123", filled_avg_price=None)

    trade_client.get_option_contracts = fake_get_contracts
    trade_client.submit_order = fake_submit

    signal = make_iron_condor_signal()
    pos = b.open_position(
        symbol="AAPL", strategy=StrategyType.IRON_CONDOR, credit_or_debit=300.0, max_loss=1200.0,
        contracts=2, stop_loss_multiple=2.0, profit_target_pct=0.5,
        direction="neutral", regime_at_entry="high_iv_range", rationale="test", confidence=0.9,
        entry_spot_price=100.0, signal=signal,
    )

    assert pos is not None
    assert len(captured_orders) == 1
    req = captured_orders[0]

    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderClass
    assert isinstance(req, LimitOrderRequest)
    assert req.order_class == OrderClass.MLEG
    assert len(req.legs) == 4
    assert req.qty == 2
    assert req.limit_price < 0

    assert pos.broker_order_id == "order123"
    assert len(pos.broker_leg_symbols) == 4
    assert pos.strategy == StrategyType.IRON_CONDOR


def test_open_position_aborts_if_any_leg_unresolvable(broker):
    b, rm, trade_client, _ = broker
    call_count = {"n": 0}

    def fake_get_contracts(req):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return SimpleNamespace(option_contracts=[make_fake_contract("AAPL_110", 110.0)])
        return SimpleNamespace(option_contracts=[])

    submitted = []
    trade_client.get_option_contracts = fake_get_contracts
    trade_client.submit_order = lambda req: submitted.append(req)

    signal = make_iron_condor_signal()
    result = b.open_position(
        symbol="AAPL", strategy=StrategyType.IRON_CONDOR, credit_or_debit=300.0, max_loss=1200.0,
        contracts=1, stop_loss_multiple=2.0, profit_target_pct=0.5, signal=signal,
    )
    assert result is None
    assert submitted == []


def test_open_position_avoids_duplicate_contract_across_legs(broker):
    """Regression test: two theoretical legs (short call, long call) closer
    together than the real listed strike increment must not collapse onto
    the same contract -- find_contract should pick the next-nearest
    distinct strike for the second leg instead."""
    b, rm, trade_client, _ = broker

    # Only ONE real call strike (105) exists near both theoretical call legs
    # (110 short, 115 long) on the first lookup; a second, further-out call
    # strike (120) exists too. Both calls should resolve to distinct
    # symbols even though 105 is nearest to both theoretical strikes.
    call_contracts = [
        make_fake_contract("AAPL_C_105", 105.0),
        make_fake_contract("AAPL_C_120", 120.0),
    ]
    put_contracts = [
        make_fake_contract("AAPL_P_95", 95.0),
        make_fake_contract("AAPL_P_80", 80.0),
    ]

    def fake_get_contracts(req):
        if req.type.value == "call":
            return SimpleNamespace(option_contracts=call_contracts)
        return SimpleNamespace(option_contracts=put_contracts)

    captured_orders = []

    def fake_submit(req):
        captured_orders.append(req)
        return SimpleNamespace(id="order999", filled_avg_price=None)

    trade_client.get_option_contracts = fake_get_contracts
    trade_client.submit_order = fake_submit

    signal = make_iron_condor_signal()  # legs at 110(short call), 115(long call), 90(short put), 85(long put)
    pos = b.open_position(
        symbol="AAPL", strategy=StrategyType.IRON_CONDOR, credit_or_debit=300.0, max_loss=1200.0,
        contracts=1, stop_loss_multiple=2.0, profit_target_pct=0.5, signal=signal,
    )

    assert pos is not None
    leg_symbols = [leg["symbol"] for leg in pos.broker_leg_symbols]
    assert len(leg_symbols) == len(set(leg_symbols))  # no duplicates
    # both call legs should be present but pointing at different real contracts
    call_legs_used = [s for s in leg_symbols if "_C_" in s]
    assert len(call_legs_used) == 2
    assert call_legs_used[0] != call_legs_used[1]


def test_open_position_aborts_when_no_distinct_strike_available(broker):
    """If only one real strike exists near a group of theoretical legs (no
    second candidate to fall back to), the entry should abort cleanly
    rather than submit a broken duplicate-symbol order."""
    b, rm, trade_client, _ = broker

    call_contracts = [make_fake_contract("AAPL_C_105", 105.0)]  # only one, ever
    put_contracts = [make_fake_contract("AAPL_P_95", 95.0), make_fake_contract("AAPL_P_80", 80.0)]

    def fake_get_contracts(req):
        if req.type.value == "call":
            return SimpleNamespace(option_contracts=call_contracts)
        return SimpleNamespace(option_contracts=put_contracts)

    submitted = []
    trade_client.get_option_contracts = fake_get_contracts
    trade_client.submit_order = lambda req: submitted.append(req)

    signal = make_iron_condor_signal()
    result = b.open_position(
        symbol="AAPL", strategy=StrategyType.IRON_CONDOR, credit_or_debit=300.0, max_loss=1200.0,
        contracts=1, stop_loss_multiple=2.0, profit_target_pct=0.5, signal=signal,
    )
    assert result is None
    assert submitted == []


def make_open_position():
    return Position(
        id="order123", symbol="AAPL", strategy=StrategyType.IRON_CONDOR,
        opened_at=datetime.utcnow() - timedelta(days=5),
        entry_credit_or_debit=300.0, max_loss=1200.0, contracts=1,
        stop_loss_level=2.0, profit_target_pct=0.5,
        broker_name="alpaca_paper", broker_order_id="order123",
        broker_leg_symbols=[
            {"symbol": "AAPL_C_110", "side": "sell"},
            {"symbol": "AAPL_C_115", "side": "buy"},
            {"symbol": "AAPL_P_90", "side": "sell"},
            {"symbol": "AAPL_P_85", "side": "buy"},
        ],
    )


def test_close_position_reverses_leg_sides(broker):
    b, rm, trade_client, _ = broker
    rm.open_positions.append(make_open_position())
    captured = []

    def fake_submit(req):
        captured.append(req)
        return SimpleNamespace(id="close1", filled_avg_price=None)

    trade_client.submit_order = fake_submit
    trade_client.get_order_by_id = lambda oid: SimpleNamespace(filled_avg_price=None)

    pos = rm.open_positions[0]
    b.close_position(pos, current_value=100.0, reason="profit target 50% hit")

    from alpaca.trading.enums import OrderSide, PositionIntent
    req = captured[0]
    legs_by_symbol = {leg.symbol: leg for leg in req.legs}

    assert legs_by_symbol["AAPL_C_110"].side == OrderSide.BUY
    assert legs_by_symbol["AAPL_C_110"].position_intent == PositionIntent.BUY_TO_CLOSE
    assert legs_by_symbol["AAPL_C_115"].side == OrderSide.SELL
    assert legs_by_symbol["AAPL_C_115"].position_intent == PositionIntent.SELL_TO_CLOSE


def test_close_position_uses_confirmed_fill_price_when_available(broker):
    b, rm, trade_client, _ = broker
    rm.open_positions.append(make_open_position())
    pos = rm.open_positions[0]

    trade_client.submit_order = lambda req: SimpleNamespace(id="close1", filled_avg_price=None)
    trade_client.get_order_by_id = lambda oid: SimpleNamespace(filled_avg_price=120.0)

    realized = b.close_position(pos, current_value=999.0, reason="stop loss 2.0x credit hit")
    expected_pnl_per_contract = 300.0 - 120.0
    assert realized == pytest.approx(expected_pnl_per_contract - (0.65 + 0.14) * 4, abs=0.01)


def make_fake_quote(bid, ask):
    return SimpleNamespace(bid_price=bid, ask_price=ask)


def test_check_exits_computes_cost_to_close_from_real_quotes(broker):
    b, rm, trade_client, data_client = broker
    rm.open_positions.append(make_open_position())
    pos = rm.open_positions[0]
    pos.entry_credit_or_debit = 300.0
    pos.profit_target_pct = 0.9
    pos.stop_loss_level = 5.0

    quotes = {
        "AAPL_C_110": make_fake_quote(bid=1.0, ask=1.1),
        "AAPL_C_115": make_fake_quote(bid=0.4, ask=0.5),
        "AAPL_P_90": make_fake_quote(bid=0.8, ask=0.9),
        "AAPL_P_85": make_fake_quote(bid=0.3, ask=0.4),
    }
    data_client.get_option_latest_quote = lambda req: quotes

    b.check_exits({"AAPL": 100.0})
    assert pos.high_water_mark_pct == pytest.approx(1 - (1.3 / 300.0), abs=0.001)


def test_check_exits_closes_on_profit_target(broker):
    b, rm, trade_client, data_client = broker
    rm.open_positions.append(make_open_position())
    pos = rm.open_positions[0]
    pos.entry_credit_or_debit = 300.0
    pos.profit_target_pct = 0.1

    quotes = {
        "AAPL_C_110": make_fake_quote(bid=0.5, ask=0.6),
        "AAPL_C_115": make_fake_quote(bid=0.1, ask=0.2),
        "AAPL_P_90": make_fake_quote(bid=0.4, ask=0.5),
        "AAPL_P_85": make_fake_quote(bid=0.05, ask=0.1),
    }
    data_client.get_option_latest_quote = lambda req: quotes
    trade_client.submit_order = lambda req: SimpleNamespace(id="close1", filled_avg_price=None)
    trade_client.get_order_by_id = lambda oid: SimpleNamespace(filled_avg_price=None)

    b.check_exits({"AAPL": 100.0})
    assert pos.status == "closed"
