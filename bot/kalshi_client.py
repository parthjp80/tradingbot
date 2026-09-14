"""
Minimal client for Kalshi's public market-data endpoints.

Per Kalshi's own docs: reading market data (prices, order books, market/
event/series details) is PUBLIC and requires no authentication. Only
placing orders and viewing portfolio/balance requires the RSA-signed
auth headers. This bot never trades on Kalshi -- it only reads prices as
a sentiment input -- so no API key is needed here at all.

Base URL and endpoint shapes verified against Kalshi's current API docs
and live market pages at the time this was written. If Kalshi changes
their schema, this is the one place that needs updating.
"""
from __future__ import annotations
from typing import Optional
import requests

from bot.logger_setup import get_logger

log = get_logger(__name__)

BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT_SECONDS = 10


class KalshiClient:
    def __init__(self, base_url: str = BASE_URL, timeout: int = REQUEST_TIMEOUT_SECONDS):
        self.base_url = base_url
        self.timeout = timeout

    def get_events(self, series_ticker: str, status: str = "open", limit: int = 20) -> list[dict]:
        """Returns events under a series (e.g. the yearly recession event
        under the KXRECSSNBER series)."""
        try:
            resp = requests.get(
                f"{self.base_url}/events",
                params={"series_ticker": series_ticker, "status": status, "limit": limit},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json().get("events", [])
        except Exception as e:
            log.warning("Kalshi: failed to fetch events for series %s: %s", series_ticker, e)
            return []

    def get_markets(self, event_ticker: Optional[str] = None, series_ticker: Optional[str] = None,
                     status: str = "open", limit: int = 20) -> list[dict]:
        """Returns markets, optionally filtered to a specific event or series.
        Each market includes yes_bid/yes_ask in cents (1-99), representing
        the market's implied probability of the YES outcome."""
        params = {"status": status, "limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        try:
            resp = requests.get(f"{self.base_url}/markets", params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json().get("markets", [])
        except Exception as e:
            log.warning("Kalshi: failed to fetch markets (event=%s series=%s): %s", event_ticker, series_ticker, e)
            return []
