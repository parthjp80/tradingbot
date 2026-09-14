from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
import pytest

from bot.kalshi_client import KalshiClient
from bot.prediction_markets import MacroSentimentEngine
from bot.config import TrackedKalshiSeries, PREDICTION_MARKETS


def make_mock_response(json_data, status_ok=True):
    mock_resp = MagicMock()
    mock_resp.json.return_value = json_data
    if not status_ok:
        mock_resp.raise_for_status.side_effect = Exception("HTTP error")
    return mock_resp


def test_get_events_returns_list():
    client = KalshiClient()
    with patch("requests.get", return_value=make_mock_response({"events": [{"event_ticker": "KXRECSSNBER-26"}]})):
        events = client.get_events("KXRECSSNBER")
    assert len(events) == 1
    assert events[0]["event_ticker"] == "KXRECSSNBER-26"


def test_get_events_returns_empty_on_error():
    client = KalshiClient()
    with patch("requests.get", side_effect=Exception("network error")):
        events = client.get_events("KXRECSSNBER")
    assert events == []


def test_get_markets_passes_correct_params():
    client = KalshiClient()
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        return make_mock_response({"markets": []})

    with patch("requests.get", side_effect=fake_get):
        client.get_markets(event_ticker="KXRECSSNBER-26", status="open")

    assert captured["params"]["event_ticker"] == "KXRECSSNBER-26"
    assert captured["params"]["status"] == "open"


def test_get_markets_returns_empty_on_error():
    client = KalshiClient()
    with patch("requests.get", side_effect=Exception("network error")):
        markets = client.get_markets(event_ticker="X")
    assert markets == []


def make_engine_with_mock_client(events_by_series, markets_by_event):
    client = MagicMock()
    client.get_events.side_effect = lambda series_ticker, **kw: events_by_series.get(series_ticker, [])
    client.get_markets.side_effect = lambda event_ticker=None, **kw: markets_by_event.get(event_ticker, [])
    return MacroSentimentEngine(client=client)


def test_fetch_series_probability_from_yes_bid_ask():
    events = {"KXRECSSNBER": [{"event_ticker": "KXRECSSNBER-26"}]}
    markets = {"KXRECSSNBER-26": [{"yes_bid": 30, "yes_ask": 34}]}
    engine = make_engine_with_mock_client(events, markets)

    series = TrackedKalshiSeries("KXRECSSNBER", "US recession this year", bullish_when_yes=False, is_risk_flag=True)
    prob = engine._fetch_series_probability(series)
    assert prob == pytest.approx(0.32, abs=0.001)


def test_fetch_series_probability_none_when_no_events():
    engine = make_engine_with_mock_client({}, {})
    series = TrackedKalshiSeries("MISSING", "test", bullish_when_yes=True, is_risk_flag=False)
    assert engine._fetch_series_probability(series) is None


def test_get_signal_caches_within_ttl():
    events = {"KXRECSSNBER": [{"event_ticker": "KXRECSSNBER-26"}]}
    markets = {"KXRECSSNBER-26": [{"yes_bid": 20, "yes_ask": 22}]}
    engine = make_engine_with_mock_client(events, markets)
    series = TrackedKalshiSeries("KXRECSSNBER", "US recession this year", bullish_when_yes=False, is_risk_flag=True)

    sig1 = engine.get_signal(series)
    count_after_first = engine.client.get_events.call_count
    sig2 = engine.get_signal(series)
    count_after_second = engine.client.get_events.call_count

    assert sig1.yes_probability == sig2.yes_probability
    assert count_after_first == count_after_second


def test_get_signal_refetches_after_ttl_expires():
    events = {"KXRECSSNBER": [{"event_ticker": "KXRECSSNBER-26"}]}
    markets = {"KXRECSSNBER-26": [{"yes_bid": 20, "yes_ask": 22}]}
    engine = make_engine_with_mock_client(events, markets)
    series = TrackedKalshiSeries("KXRECSSNBER", "US recession this year", bullish_when_yes=False, is_risk_flag=True)

    engine.get_signal(series)
    engine._cache["KXRECSSNBER"].fetched_at = datetime.utcnow() - timedelta(hours=100)
    engine.get_signal(series)

    assert engine.client.get_events.call_count == 2


def test_macro_risk_off_true_above_threshold(monkeypatch):
    monkeypatch.setattr(PREDICTION_MARKETS, "recession_risk_off_threshold", 0.30)
    events = {"KXRECSSNBER": [{"event_ticker": "KXRECSSNBER-26"}]}
    markets = {"KXRECSSNBER-26": [{"yes_bid": 40, "yes_ask": 44}]}
    engine = make_engine_with_mock_client(events, markets)

    monkeypatch.setattr(PREDICTION_MARKETS, "tracked_series", [
        TrackedKalshiSeries("KXRECSSNBER", "US recession this year", bullish_when_yes=False, is_risk_flag=True),
    ])
    assert engine.macro_risk_off() is True


def test_macro_risk_off_false_below_threshold(monkeypatch):
    monkeypatch.setattr(PREDICTION_MARKETS, "recession_risk_off_threshold", 0.50)
    events = {"KXRECSSNBER": [{"event_ticker": "KXRECSSNBER-26"}]}
    markets = {"KXRECSSNBER-26": [{"yes_bid": 20, "yes_ask": 22}]}
    engine = make_engine_with_mock_client(events, markets)

    monkeypatch.setattr(PREDICTION_MARKETS, "tracked_series", [
        TrackedKalshiSeries("KXRECSSNBER", "US recession this year", bullish_when_yes=False, is_risk_flag=True),
    ])
    assert engine.macro_risk_off() is False


def test_directional_tilt_positive_for_long_when_bullish_series_high(monkeypatch):
    events = {"KXRATECUT": [{"event_ticker": "KXRATECUT-26DEC31"}]}
    markets = {"KXRATECUT-26DEC31": [{"yes_bid": 78, "yes_ask": 82}]}
    engine = make_engine_with_mock_client(events, markets)

    monkeypatch.setattr(PREDICTION_MARKETS, "tracked_series", [
        TrackedKalshiSeries("KXRATECUT", "Fed rate cut this year", bullish_when_yes=True, is_risk_flag=False),
    ])
    tilt_long = engine.directional_tilt("long")
    tilt_short = engine.directional_tilt("short")

    assert tilt_long > 0
    assert tilt_short < 0
    assert tilt_long == pytest.approx(-tilt_short, abs=0.001)
    assert abs(tilt_long) <= PREDICTION_MARKETS.directional_tilt_max_confidence_delta + 1e-9


def test_directional_tilt_zero_for_invalid_direction():
    engine = make_engine_with_mock_client({}, {})
    assert engine.directional_tilt("neutral") == 0.0


def test_directional_tilt_ignores_risk_flag_series(monkeypatch):
    events = {"KXRECSSNBER": [{"event_ticker": "KXRECSSNBER-26"}]}
    markets = {"KXRECSSNBER-26": [{"yes_bid": 90, "yes_ask": 92}]}
    engine = make_engine_with_mock_client(events, markets)

    monkeypatch.setattr(PREDICTION_MARKETS, "tracked_series", [
        TrackedKalshiSeries("KXRECSSNBER", "US recession this year", bullish_when_yes=False, is_risk_flag=True),
    ])
    assert engine.directional_tilt("long") == 0.0
