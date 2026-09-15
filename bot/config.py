"""
Central configuration for the paper-trading bot.

Nothing in here is a live credential. Real broker keys (tastytrade OAuth2,
DXLink) belong in a .env file (see .env.example) and are loaded via
python-dotenv in data_feed.py, never committed to source control.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# Yahoo Finance requires a "=F" suffix for continuous futures contracts
# (e.g. "MES=F", not "MES") -- yfinance returns a 404/no-data error on the
# bare root symbol. This maps the bot's internal symbol (used everywhere:
# positions, journal, sector map, order routing) to the symbol actually
# passed to yfinance, so nothing outside data_feed.py needs to know about
# the distinction. Extend this if you add other futures to the watchlist.
FUTURES_YAHOO_SYMBOL_MAP = {
    "MES": "MES=F",
    "ES": "ES=F",
    "MNQ": "MNQ=F",
    "NQ": "NQ=F",
    "CL": "CL=F",
    "MCL": "MCL=F",
    "GC": "GC=F",
    "MGC": "MGC=F",
}


@dataclass
class AccountConfig:
    starting_equity: float = 100_000.0
    max_risk_per_trade_pct: float = 0.02      # 2% of equity risked per trade
    daily_loss_limit_pct: float = 0.03        # halt new entries for the day
    max_drawdown_pause_pct: float = 0.15      # halt ALL trading, needs manual resume
    max_concurrent_positions: int = 8
    max_portfolio_theta_pct: float = 0.005    # theta cap as % of equity per day
    max_portfolio_vega_pct: float = 0.01      # vega cap as % of equity per 1 vol pt
    max_net_delta_shares_equiv: float = 300.0  # net directional exposure ceiling

    # -- diversification --
    max_positions_per_sector: int = 2         # cap concentration (see bot/sectors.py)

    # -- position sizing method --
    # "fixed_fractional": constant % of equity risked per trade (default, simplest, safest starting point)
    # "half_kelly": once enough closed-trade history exists, size using half of the
    #   Kelly-optimal fraction derived from realized win rate / avg win / avg loss.
    #   Falls back to fixed_fractional until kelly_min_sample_size trades are closed --
    #   Kelly on a small sample is a great way to blow up an account on noise.
    position_sizing_method: str = "fixed_fractional"
    kelly_min_sample_size: int = 20
    kelly_safety_fraction: float = 0.5        # "half-Kelly" -- full Kelly is too aggressive for real capital
    kelly_max_risk_per_trade_pct: float = 0.05  # hard ceiling even if Kelly math suggests more

    # -- volatility-adjusted sizing --
    high_vol_atr_pct_threshold: float = 4.0   # ATR as % of price
    high_vol_size_cut_pct: float = 0.25       # cut risk budget by this much when ATR exceeds threshold

    # -- 0DTE sizing --
    # Same-day options carry sharply more gamma risk per dollar of premium
    # than the 45-DTE condor, even with tighter strikes -- cut risk budget
    # further on top of whatever the strategy's own strike selection already
    # priced in. See bot/strategies/zero_dte.py, bot/market_hours.py.
    zero_dte_size_cut_pct: float = 0.35

    # -- A+ setup sizing boost --
    # The scanner already computes a 0-100 quality score per candidate each
    # cycle (bot/scanner.py) but it's discarded after ranking. When a signal's
    # scanner_score clears aplus_score_threshold, size up -- but hard-capped
    # at aplus_max_risk_per_trade_pct of equity regardless of the multiplier,
    # the same way kelly_max_risk_per_trade_pct caps half-Kelly sizing
    # regardless of what the math suggests.
    aplus_score_threshold: float = 80.0
    aplus_size_boost_pct: float = 0.25
    aplus_max_risk_per_trade_pct: float = 0.03

    # -- revenge-trading / tilt prevention --
    consecutive_loss_cooldown_count: int = 3  # N losses in a row...
    consecutive_loss_cooldown_hours: int = 24  # ...triggers this many hours of no new entries

    # -- reward:risk gate --
    # Minimum acceptable (expected reward) / (risk taken to the stop-loss,
    # not the full defined max loss) before a signal is approved. Deliberately
    # different per strategy family: premium-selling strategies are high-win-rate/
    # asymmetric by design (you routinely risk more than you collect and still
    # come out ahead on expectancy), so their bar is lower than a directional
    # trader's classic "risk 1 to make 2" guideline. See risk_manager.py.
    min_reward_risk_ratio: dict = field(default_factory=lambda: {
        "short_strangle": 0.25,
        "iron_condor": 0.20,
        "short_put_vertical": 0.40,
        "short_call_vertical": 0.40,
        "futures_trend": 0.50,
        "zero_dte_iron_condor": 0.20,
    })

    # -- trailing stop (profit protection / "monitor and adjust") --
    # Once a position has captured this fraction of its profit target, ratchet
    # its stop-loss in to protect the gain instead of letting it round-trip
    # back to a full loss.
    trailing_stop_activation_pct: float = 0.5
    trailing_stop_lock_in_level: float = 1.0   # 1.0 = ratchet to breakeven (no worse than scratch)

    # -- journaling goals ("set goals", journaling step #9) --
    # Purely informational: the performance report flags actual vs. target,
    # nothing in the trading logic reads these.
    target_win_rate: float = 0.55
    target_expectancy_per_trade: float = 0.0   # any positive expectancy clears this bar
    target_avg_r_multiple: float = 0.15


@dataclass
class RegimeThresholds:
    iv_rank_high: float = 50.0
    iv_rank_low: float = 30.0
    adx_trend: float = 25.0
    adx_chop: float = 20.0
    vix_crisis: float = 30.0
    vix_jump_pct_1d: float = 0.12  # 12% single-day VIX pop triggers "crisis" flag


@dataclass
class WatchlistItem:
    symbol: str
    asset_class: str  # "equity_option" | "future"
    multiplier: float = 100.0  # 100 for equity options, contract multiplier for futures


@dataclass
class ScannerConfig:
    enabled: bool = True
    max_equity_candidates: int = 6          # total equity names carried into a cycle
    min_avg_dollar_volume: float = 25_000_000.0  # 20-day avg $ volume liquidity floor
    min_price: float = 15.0                 # avoid penny/illiquid-option names

    # Premium-selling strategies (iron condor / short strangle) size their
    # wings as a % move of the underlying, so max-loss-per-contract scales
    # directly with stock price -- a $1,200+ name can blow past a sane
    # per-trade risk budget on a single contract even with narrow wings.
    # This only gates the HIGH_IV_RANGE regime (premium-selling candidates);
    # TRENDING-regime verticals on the same expensive names are unaffected.
    max_underlying_price_for_premium_selling: float = 300.0

    # -- volatility --
    min_atr_pct: float = 1.0                # ATR must be >= 1.0% of price: filters "dead" stocks
                                             # with too little real movement to be worth trading

    # -- trend / breakout (used in scoring, not hard filters) --
    bb_squeeze_lookback: int = 120
    bb_squeeze_percentile: float = 20.0     # bottom 20% of trailing BB-width range = squeeze
    volume_spike_threshold: float = 1.5     # 1.5x avg volume counts as a breakout-confirming spike

    # -- events --
    earnings_blackout_days: int = 7         # skip new entries within N days of earnings
    dividend_blackout_days: int = 3         # skip new entries within N days of ex-dividend
                                             # (early-assignment risk on short calls)
    news_lookback_days: int = 3
    max_recent_news_count: int = 8          # more than this = elevated event-risk flag (soft)

    # -- fundamentals (soft filters, disabled by default -- see fundamentals.py) --
    max_pe_ratio: Optional[float] = None
    max_debt_to_equity: Optional[float] = None
    min_earnings_growth: Optional[float] = None

    # Futures aren't screened (small, fixed universe you already trade) --
    # always carried alongside whatever the scanner picks for equities.
    static_futures: list = field(default_factory=lambda: [
        WatchlistItem("MES", "future", multiplier=5.0),
    ])


DEFAULT_WATCHLIST = [
    WatchlistItem("IBM", "equity_option"),
    WatchlistItem("WDC", "equity_option"),
    WatchlistItem("NVO", "equity_option"),
    WatchlistItem("PLTR", "equity_option"),
    WatchlistItem("MES", "future", multiplier=5.0),   # micro ES, $5/pt
]

@dataclass
class AlpacaConfig:
    """
    Config for using an Alpaca paper trading account instead of the
    internal simulator. Alpaca does NOT support futures trading -- MES
    trend signals are skipped entirely when this backend is active (see
    orchestrator.py). paper=True is hardcoded in alpaca_broker.py
    regardless of what's set here; there is deliberately no config knob
    that can point this bot at a live Alpaca account.
    """
    enabled: bool = False
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    strike_match_window_pct: float = 0.15   # how far from the theoretical strike to search for a real listed one
    expiration_match_window_days: int = 5   # how many days of slack around the target DTE


@dataclass
class TrackedKalshiSeries:
    """
    One prediction-market series to pull as a macro signal. `series_ticker`
    values below are REAL, currently-listed Kalshi series (verified against
    kalshi.com at the time this was written) -- but Kalshi rotates/renames
    series periodically (e.g. yearly recession markets get a new event each
    year under the same series). If a series starts 404ing, check
    https://kalshi.com/category/economics and update the ticker here or via
    a custom list -- see bot/prediction_markets.py.
    """
    series_ticker: str
    label: str
    bullish_when_yes: bool   # does a higher YES probability lean bullish for equities?
    is_risk_flag: bool = False  # does high YES probability mean "get more conservative"?


@dataclass
class PredictionMarketConfig:
    enabled: bool = False
    cache_ttl_minutes: int = 60
    recession_risk_off_threshold: float = 0.35   # recession probability above this cuts position size further
    risk_off_size_cut_pct: float = 0.35           # how much to cut risk budget when the flag is tripped
    directional_tilt_max_confidence_delta: float = 0.08  # cap on how much this can move a signal's confidence
    tracked_series: list = field(default_factory=lambda: [
        TrackedKalshiSeries(
            series_ticker="KXRECSSNBER", label="US recession this year",
            bullish_when_yes=False, is_risk_flag=True,
        ),
        TrackedKalshiSeries(
            series_ticker="KXRATECUT", label="Fed rate cut this year",
            bullish_when_yes=True, is_risk_flag=False,
        ),
    ])


ACCOUNT = AccountConfig()
REGIME = RegimeThresholds()
SCANNER = ScannerConfig(enabled=os.getenv("BOT_SCANNER_ENABLED", "true").lower() != "false")
ALPACA = AlpacaConfig(enabled=os.getenv("BOT_BROKER_BACKEND", "internal_simulator").lower() == "alpaca_paper")
PREDICTION_MARKETS = PredictionMarketConfig(enabled=os.getenv("BOT_PREDICTION_MARKETS_ENABLED", "false").lower() == "true")

# How often the orchestrator loop runs during market hours
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "30"))

# Paper broker realism knobs
COMMISSION_PER_CONTRACT = 0.65 + 0.14  # tastytrade-style option commission + fees
SLIPPAGE_PCT_OF_MID = 0.03             # assume 3% of mid-price slippage on paper fills
