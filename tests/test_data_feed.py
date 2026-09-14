from unittest.mock import MagicMock, patch
import pandas as pd
from bot.data_feed import YFinanceFeed, _to_yahoo_symbol
from bot.config import FUTURES_YAHOO_SYMBOL_MAP


def test_futures_symbol_translated():
    assert _to_yahoo_symbol("MES") == "MES=F"
    assert _to_yahoo_symbol("ES") == "ES=F"


def test_equity_symbol_passes_through_unchanged():
    assert _to_yahoo_symbol("AAPL") == "AAPL"
    assert _to_yahoo_symbol("SPY") == "SPY"


def test_all_configured_futures_have_f_suffix():
    for internal, yahoo in FUTURES_YAHOO_SYMBOL_MAP.items():
        assert yahoo.endswith("=F")
        assert yahoo.startswith(internal)


def test_yfinance_feed_calls_translated_symbol_for_futures():
    fake_df = pd.DataFrame({
        "Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [100],
    })

    mock_ticker_instance = MagicMock()
    mock_ticker_instance.history.return_value = fake_df

    with patch("yfinance.Ticker", return_value=mock_ticker_instance) as mock_ticker_cls:
        feed = YFinanceFeed()
        feed.history("MES", period="1y", interval="1d")

    mock_ticker_cls.assert_called_once_with("MES=F")


def test_yfinance_feed_calls_unmodified_symbol_for_equities():
    fake_df = pd.DataFrame({
        "Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [100],
    })
    mock_ticker_instance = MagicMock()
    mock_ticker_instance.history.return_value = fake_df

    with patch("yfinance.Ticker", return_value=mock_ticker_instance) as mock_ticker_cls:
        feed = YFinanceFeed()
        feed.history("AAPL", period="1y", interval="1d")

    mock_ticker_cls.assert_called_once_with("AAPL")
