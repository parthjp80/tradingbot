# NextGen TradingBot — Regime-Aware Paper Trading Bot

Paper-trading bot for equity options + ES/MES futures that classifies market
regime, picks a strategy appropriate to that regime, sizes positions by
fixed-fractional risk, and manages exits dynamically. Built to slot into the
same TrueNAS SCALE / Docker pattern as `market-news` and
`earnings-reaction-backtest`.

**Status: paper trading only. No live orders are placed.** Extending this to
live execution is a deliberate, separate step — see "Path to live trading"
below.

## How the bot picks what to trade

**Universe → Scanner → Regime → Strategy**, in that order. Nothing is
hardcoded to a fixed watchlist anymore (that mode still exists as a
fallback — see below).

1. **Universe** (`bot/universe.py`): a candidate pool the scanner is
   allowed to consider. Ships with a curated list of ~30 liquid,
   optionable large/mid-caps + index ETFs. Override it entirely by creating
   `data/universe.txt` (one ticker per line) — useful if you want to feed
   it a real screener export instead of the curated default.

2. **Scanner** (`bot/scanner.py`): for every symbol in the universe, each
   cycle:
   - **Filters out** (hard rejects):
     - Price below $15, 20-day avg dollar volume below $25M (liquidity
       floor — no point ranking a name whose option spreads aren't
       fillable).
     - ATR below 1% of price (`min_atr_pct`) — screens out "dead" stocks
       with too little real movement to be worth the capital.
     - Inside a 7-day earnings blackout or 3-day ex-dividend blackout
       (yfinance calendar data, when available) — the ex-div check exists
       because it's early-assignment risk on short calls, not just an
       earnings-gap concern.
     - Optional fundamental gates (`max_pe_ratio`, `max_debt_to_equity`,
       `min_earnings_growth`) — **disabled by default**. A stock with no
       P/E or high leverage isn't automatically wrong to sell premium on;
       the mechanism is there, the opinion isn't forced. Turn any of these
       on in `ScannerConfig` if you want fundamentally-driven exclusion.
   - **Classifies regime**, then applies one regime-conditional filter:
     - **Price cap for premium-selling candidates**: `HIGH_IV_RANGE`
       (iron condor/strangle) names above `max_underlying_price_for_premium_selling`
       (default $300) are rejected. Condor/strangle wing width scales with
       the underlying's price, so a single contract on an expensive name
       (say, a $1,200 stock) can carry a max loss well beyond a sane
       per-trade risk budget even with narrow wings — this was discovered
       in practice: high-dollar names like LLY and WDC were consistently
       sizing to 0 contracts because their defined max loss exceeded 2% of
       equity outright. This filter only applies to `HIGH_IV_RANGE` —
       `TRENDING`-regime verticals on the same expensive names are
       unaffected, since their risk profile is different.
   - **Scores** survivors, regime-specific:
     - `HIGH_IV_RANGE`: IV rank (richer premium) + a chop bonus (lower
       ADX), **minus a penalty if Bollinger Bands are in a squeeze** —
       compressed range + already-elevated IV is exactly the setup that
       precedes a breakout, which is the tail risk that hurts short
       premium the most.
     - `TRENDING`: ADX + slope strength, **plus a bonus if MACD momentum
       agrees with the trend direction** (trend confirmation) and **a
       bigger bonus if a Bollinger squeeze is resolving on a volume
       spike** (a real breakout, not just noise).
     - Both regimes take a small penalty if recent news volume
       (`recent_news_count`, from yfinance) exceeds a threshold — not
       because news is bad, but because it signals active event risk on
       top of whatever the regime already implies.
   - Keeps the **top N** (`SCANNER.max_equity_candidates`, default 6).

3. From there it's the same pipeline as before: regime → strategy router →
   risk manager sizing/caps → paper broker fill.

Futures (MES) aren't screened — small, fixed universe you already trade —
and are always carried alongside whatever the scanner picks for equities.

**What the scanner is *not* doing**: predicting direction. It ranks setup
*quality* (how rich the premium is, how clean the trend is), which is a
different and more tractable problem than "which stock will go up."

**Toggle it off** (revert to the fixed 4-symbol watchlist from `config.py`)
by setting `BOT_SCANNER_ENABLED=false` in `.env` — useful for isolating
strategy/risk-logic behavior from scanner behavior when debugging.

Tunable in `config.py`'s `ScannerConfig`: `max_equity_candidates`,
`min_avg_dollar_volume`, `min_price`, `max_underlying_price_for_premium_selling`,
`min_atr_pct`, `earnings_blackout_days`,
`dividend_blackout_days`, `bb_squeeze_percentile`, `volume_spike_threshold`,
`max_recent_news_count`, and the (off-by-default) fundamental gates
`max_pe_ratio`, `max_debt_to_equity`, `min_earnings_growth`.

## Trade journal

Mapped to the standard 9-step journaling framework (`bot/journal.py`).
What doesn't translate literally for an automated system got the closest
algorithmic analog rather than being skipped:

| # | Framework step | What the bot does |
|---|---|---|
| 1 | Format/platform | CSV at `data/trade_journal.csv` — opens directly in Excel/Sheets/Notion |
| 2 | Basic trade details | symbol, direction, contracts, entry/exit price & time, strategy |
| 3 | Strategy & rationale | the actual `TradeSignal.rationale` string and regime are persisted verbatim, not just a P&L number |
| 4 | Financial metrics | position size, **planned** reward:risk ratio (computed at entry), realized P&L, R-multiple, % return on risk |
| 5 | Emotional/psych state | **substituted**: a bot has no emotions, but has the direct analog — which sizing method was active and whether this entry followed a losing streak (`sizing_method_at_entry`, `consecutive_losses_at_entry`) |
| 6 | Outcome analysis | auto-computed `hit_profit_target` / `hit_stop_loss` / `pct_of_target_captured`, plus a rule-based "lessons learned" one-liner per trade |
| 7 | Weekly/monthly review | `print_report(period="weekly"\|"monthly"\|"all")` |
| 8 | Visuals/charts | **substituted**: no price chart to screenshot for an automated entry, so `generate_charts()` plots an equity curve and a win/loss P&L distribution instead — the equivalent visual for "how is the system actually behaving" |
| 9 | Feedback/goals | **substituted**: no mentor to loop in, but `ACCOUNT.target_win_rate` / `target_expectancy_per_trade` / `target_avg_r_multiple` (`config.py`) get compared against actuals in every report, flagged ON TRACK / BELOW TARGET |

A performance report (all-time) prints automatically at the end of every
cycle. Run a periodic review manually:

```python
from bot.journal import print_report, generate_charts
print_report(period="weekly")     # or "monthly", "all"
generate_charts()                  # writes data/charts/equity_curve.png
                                    # and data/charts/pnl_distribution.png
```

Every row in the CSV also carries the full entry-time snapshot (ADX, IV
rank, ATR%, confidence, spot price) so you can later ask questions like
"do trades taken above 90 IV rank actually outperform" directly from the
CSV, without needing to re-derive it from logs.

## Risk management

Mapped directly to the standard 8-point risk management framework:

**1. Risk tolerance.** `AccountConfig.max_risk_per_trade_pct` (default 2%) caps
capital risked per trade. `max_drawdown_pause_pct` (15%) halts *all* trading
until you manually resume it — the account-level backstop.

**2. Stop-losses + position sizing together.** `exit_rules.py` sets a
stop-loss (as a multiple of credit received) and profit target per
strategy/regime at entry time. `RiskManager.size_position()` sizes the
trade so the *defined max loss* stays inside the risk budget — sizing and
stop-loss aren't independent decisions, one is computed from the other.

**3. Diversification.** New: `bot/sectors.py` maps the universe to sectors;
`AccountConfig.max_positions_per_sector` (default 2) rejects a signal if
the sector's already at cap, checked against currently *open* positions,
not just this cycle's picks. Unmapped symbols (e.g. a custom universe) are
exempt rather than silently blocked.

**4. Position sizing formulas.** Two methods, switchable via
`AccountConfig.position_sizing_method`:
  - `"fixed_fractional"` (default): constant % of equity per trade.
  - `"half_kelly"`: once `kelly_min_sample_size` (default 20) trades have
    closed, sizes using half of the Kelly-optimal fraction
    (`f* = win_rate − (1−win_rate)/R`) derived from realized win rate and
    avg win/loss — capped hard at `kelly_max_risk_per_trade_pct` regardless
    of what the math suggests. Below the sample threshold it falls back to
    fixed-fractional; sizing off Kelly math on 5 trades of history is a
    reliable way to blow up an account on noise, not an edge.
  - **Volatility adjustment**: any signal with ATR ≥ `high_vol_atr_pct_threshold`
    (default 4% of price) gets its risk budget cut by `high_vol_size_cut_pct`
    (25%) on top of whatever the strategy's own strike/stop selection
    already priced in.

**5. Monitor and adjust.** New: a trailing-stop ratchet in
`paper_broker.check_exits()`. Once a position captures
`trailing_stop_activation_pct` (default 50%) of its profit target, the
stop is tightened to `trailing_stop_lock_in_level` (default: breakeven) —
a winner that reverses gets closed near scratch instead of round-tripping
into a full loss. Every cycle re-evaluates every open position against
current conditions regardless of whether new entries fire.

**6. Keep emotions in check.** New: `AccountConfig.consecutive_loss_cooldown_count`
(default 3) — after that many consecutive losing trades, a
`consecutive_loss_cooldown_hours` (24h) cooldown blocks new entries
entirely. This is "no revenge trading" enforced algorithmically: the bot
can't override it out of impatience, since impatience isn't a variable it has.

**7. Risk-reward ratio.** New: `RiskManager._within_reward_risk_minimum()`
gates every signal against `AccountConfig.min_reward_risk_ratio`, a
per-strategy dict. Deliberately **not** a flat "risk 1 to make 2" rule —
premium-selling strategies are high-win-rate/asymmetric by design (you
routinely risk more than you collect and still come out ahead on
expectancy, see the IBM walkthrough below), so iron condors get a lower
bar (0.20) than directional futures trend trades (0.50). The ratio is
computed against risk *to the stop*, not the full defined max loss, since
the stop is what you're actually exposed to under normal management.

**8. Learn and improve.** See the dedicated "Trade journal" section below —
this point grew into its own full framework mapping (9 sub-steps) rather
than a single paragraph here.

## Why win rate isn't the target
size of winners. What this bot actually controls is **expectancy**
(`win_rate × avg_win − loss_rate × avg_loss`), via two levers:

1. **Position sizing** (`risk_manager.py`) — fixed-fractional risk per trade,
   scaled by regime and signal confidence.
2. **Exit rules** (`exit_rules.py`) — stop-loss and profit-target levels set
   at entry, varying by regime and strategy, so losers are cut before they
   erase several winners' worth of profit.

`RiskManager.stats` tracks realized win rate and expectancy per trade live,
so you can watch whether the system is actually profitable, not just how
often it "wins."

## Architecture

```
main.py                    entry point: --once for cron, or continuous scheduler
bot/
  config.py                account risk limits, regime thresholds, scanner config, watchlist
  sectors.py                 sector map for diversification limits
  universe.py                candidate universe for the scanner (curated list, or data/universe.txt)
  scanner.py                  screens universe: liquidity/ATR/event/fundamental filters, regime+technical scoring
  fundamentals.py              P/E, earnings growth, debt-to-equity, dividend yield (yfinance-backed)
  data_feed.py              YFinanceFeed (live) / SyntheticFeed (offline testing)
  indicators.py              ADX, ATR/ATR%, MACD, Bollinger Bands + squeeze, volume spike, realized vol, IV-rank proxy
  regime.py                  classifies each symbol into a Regime each cycle
  pricing.py                  Black-Scholes pricing/greeks (paper-trading approximation)
  models.py                   Regime, TradeSignal, Position dataclasses
  strategies/
    premium_selling.py        iron condor / short strangle for HIGH_IV_RANGE
    directional.py             credit verticals + futures trend for TRENDING
    router.py                  dispatches regime+asset_class -> strategy
  exit_rules.py                regime/strategy-conditioned stop & target levels
  risk_manager.py              sizing (fixed-fractional / half-Kelly), sector caps, reward:risk gate, cooldown, circuit breakers, macro risk-off
  paper_broker.py              internal simulator: fills, P&L, trailing-stop exit monitoring, JSON persistence
  alpaca_broker.py              optional broker backend: real orders to your Alpaca PAPER account
  kalshi_client.py               minimal public REST client for Kalshi market data (no auth needed for reads)
  prediction_markets.py          macro sentiment engine: recession/rate-cut odds -> risk-off flag + directional tilt
  journal.py                    trade journal (CSV) + performance report by strategy/sector
  orchestrator.py              ties one full cycle together, selects broker backend
tests/                         pytest suite (regime, risk/expectancy, scanner, indicators, both brokers, journal, prediction markets)
```

### Regime states

| Regime | Trigger | What the bot does |
|---|---|---|
| `HIGH_IV_RANGE` | IV rank ≥ 50, ADX < 25 | Sell iron condors (defined risk) |
| `LOW_IV_RANGE` | IV rank ≤ 30, ADX < 25 | No premium-selling entries — premium too cheap |
| `TRENDING` | ADX ≥ 25 | Credit verticals *with* the trend (equities); ATR-based trend-following (futures) |
| `CRISIS` | VIX ≥ 30, or VIX up ≥12% in a day | No new entries at all; existing positions get tighter stops |

### Risk controls (`risk_manager.py`)

- Max 2% of equity risked per trade (scaled down further by signal confidence, and cut ~60% in CRISIS)
- Daily loss limit: 3% of equity halts new entries for the day
- Max drawdown breaker: 15% drawdown halts **all** trading until manually resumed (`trading_paused_for_drawdown = False`)
- Portfolio-level net delta / theta / vega caps, so multiple positions can't silently stack correlated risk
- Duplicate-position guard: won't open a second position in a symbol that already has one open

## Broker backends

Two interchangeable backends, selected by `BOT_BROKER_BACKEND` in `.env`:

### `internal_simulator` (default)

The built-in engine described throughout this README — Black-Scholes
pricing, simulated fills, no external account needed. Good for testing
regime/risk/scanner logic quickly, including fully offline with
`BOT_DATA_FEED=synthetic`.

### `alpaca_paper`

Routes real orders to **your actual Alpaca paper trading account**
(`bot/alpaca_broker.py`), instead of simulating fills internally.

```env
BOT_BROKER_BACKEND=alpaca_paper
ALPACA_API_KEY=your_paper_key
ALPACA_SECRET_KEY=your_paper_secret
```

**What it does differently from the simulator:**
- Resolves the theoretical strikes your strategy layer wants (from
  Black-Scholes, same as always) to **real listed contracts** on Alpaca
  via `GetOptionContractsRequest`, matched to the nearest actual strike
  within a configurable window (`ALPACA.strike_match_window_pct`, default
  ±15%) and expiration (`ALPACA.expiration_match_window_days`, default
  ±5 days).
- Places the whole spread as a single multi-leg limit order
  (`OrderClass.MLEG`) — up to 4 legs, covering every options strategy this
  bot trades (strangles, verticals, iron condors).
- Marks positions to market each cycle using **real bid/ask option
  quotes**, not the theta-decay proxy the internal simulator uses — this
  is a genuine accuracy improvement for anything that reaches Alpaca.

**Hard requirements and limits:**
- `paper=True` is **hardcoded** in `alpaca_broker.py`, not read from any
  config or env var. There's no way to point this at a live account
  without directly editing that line.
- Your Alpaca paper account needs **Level 3 options approval** (required
  for multi-leg spreads/iron condors). If it's not enabled, order
  submission fails with an error from Alpaca — check the dashboard.
- **Alpaca does not support futures.** MES/trend-following signals are
  skipped entirely when this backend is active — the orchestrator drops
  futures from the watchlist and logs why, rather than generating signals
  that could never fill. If you want futures trading, that still only
  works through the internal simulator (or, per the "path to live
  trading" section below, a real futures-capable broker).
- If a signal's strikes can't be matched to a real listed contract within
  the window, the entry is rejected outright (not partially filled) —
  you'll see `FAILED` in the cycle's summary table with a reason logged.
- Multi-leg orders can fill asynchronously. `close_position()` attempts
  one immediate poll for the confirmed fill price; if that's not yet
  available it falls back to the last marked value and logs a warning
  that the recorded P&L is provisional — cross-check the Alpaca dashboard
  for anything you're relying on.

**This integration has not been tested against a live paper account** —
this sandbox has no network access to Alpaca's API. It's built against
the documented `alpaca-py` request/response shapes and validated with
mocked-client unit tests that use the *real* `alpaca-py` request classes
(so a wrong field name fails loudly via pydantic validation, not
silently). Before trusting it in the scheduled loop: run `python main.py
--once --force` with `BOT_BROKER_BACKEND=alpaca_paper` set, place one
small trade, and confirm it in the Alpaca dashboard.

## Prediction-market macro signal (Kalshi)

Optional, off by default. Pulls a small set of Kalshi prediction-market
series each cycle (`bot/prediction_markets.py`, `bot/kalshi_client.py`)
and uses them for two things:

```env
BOT_PREDICTION_MARKETS_ENABLED=true
```

No API key needed — Kalshi's market-data reads (prices, order books,
events, series) are public; only placing orders requires auth, and this
bot never trades on Kalshi, only reads prices as a sentiment input.

**What it tracks by default** (`PredictionMarketConfig.tracked_series` in
`config.py`), both real, currently-listed series verified against
kalshi.com at the time this was built:
- `KXRECSSNBER` — "will there be a US recession this year" (`is_risk_flag=True`)
- `KXRATECUT` — "will the Fed cut rates this year" (`bullish_when_yes=True`)

**What it influences:**
1. **Position sizing** (`risk_manager.py`) — if recession odds cross
   `recession_risk_off_threshold` (35% default), every trade's risk budget
   gets cut an additional `risk_off_size_cut_pct` (35% default) that
   cycle. This is independent of and stacks with the existing CRISIS
   regime — CRISIS reacts to realized VIX moves, this reacts to
   forward-looking event odds, which can diverge.
2. **Stock selection and directional confidence** (`scanner.py` scoring +
   `orchestrator.py`) — for `TRENDING`-regime signals only, a small
   confidence/ranking tilt (capped at `directional_tilt_max_confidence_delta`,
   0.08 default) applied when a signal's direction agrees or disagrees
   with the macro-implied bias. This does shift which candidates the
   scanner ranks highest, which is the literal "influences stock
   selection" behavior — but see the scope limit below before reading too
   much into it.

**Scope limit, worth being direct about:** Kalshi doesn't have prediction
markets for arbitrary individual stocks — there's no "will AAPL beat
earnings" contract for most names in the universe. This is a **market-wide
macro signal**, applied uniformly across whatever directional signals the
bot already generates from price/technicals. It's not picking stocks based
on stock-specific prediction markets, because those largely don't exist.

**Interpretation caveat, also worth being direct about:** `bullish_when_yes`
on the rate-cut series is a simplifying assumption baked into the default
config, not a fact — rate cuts sometimes happen *because* of bad economic
news, which isn't bullish at all. That's exactly why it's a per-series
config flag you can flip, not hardcoded logic. Treat this whole feature as
one more noisy input to weigh, not a forecast.

**Caching:** each series is cached for `cache_ttl_minutes` (60 default) so
the bot isn't hammering Kalshi's API every cycle for data that only
meaningfully moves a few times a day.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit as needed
```

### Run a single cycle (good for testing, or cron-style scheduling)
```bash
python main.py --once --force   # --force ignores the market-hours check
```

### Run continuously (polls every POLL_INTERVAL_MINUTES during market hours)
```bash
python main.py
```

### Run offline with synthetic data (no network required — useful for dev/CI)
```bash
BOT_DATA_FEED=synthetic python main.py --once --force
```

### Run tests
```bash
pytest tests/ -v
```

## Deploying on TrueNAS SCALE

Same pattern as your other Docker-based repos:

```bash
# on TrueNAS, in a dataset e.g. /mnt/<pool>/tradingbot
git clone <this repo>
cp .env.example .env   # fill in real values
mkdir -p data logs
docker compose up -d --build
```

Edit `docker-compose.yml` and replace `/mnt/<pool>/tradingbot` with your
actual dataset path — this bind-mounts `data/` (open positions, survives
restarts) and `logs/` outside the container, matching how you've set up
`market-news` and `earnings-reaction-backtest`.

## Known approximations (read before trusting this with real capital)

- **IV rank proxy**: `indicators.iv_rank_proxy()` uses realized-vol
  percentile as a stand-in for true options IV rank. It correlates with but
  is not identical to real IV rank from an option chain.
- **Options pricing**: `pricing.py` uses simplified Black-Scholes with
  realized-vol-derived IV, ignoring the vol smile, dividends, and early
  assignment. Fine for testing regime/risk logic; not a substitute for real
  quotes.
- **Exit mark-to-market**: `paper_broker.check_exits()` approximates
  current position value via theta decay elapsed, not a real re-price of
  the option chain.
- **Market hours check**: doesn't account for holidays or early closes.

None of these affect the regime classification or risk-management logic —
they only affect how realistic the *simulated* fills are. All of them are
called out inline in the code with a note on what to swap in.

## Path to live trading (not done here, on purpose)

`AlpacaBroker` gets you real fills against a real (paper) order book, but
`paper=True` is hardcoded — going live means a genuinely separate step,
not a config flip:

1. Run the paper bot (either backend) for a meaningful sample size first
   (weeks, not days) and check `risk_manager.stats.expectancy_per_trade`
   is consistently positive before touching real capital.
2. For live equity options via Alpaca: this is the smallest step, since
   `AlpacaBroker` already speaks the real API — the only change is
   removing the hardcoded `paper=True` and pointing at live keys, which
   is a deliberate one-line edit, not something this bot will do via a
   config toggle.
3. For live futures (Alpaca doesn't support them): replace `pricing.py`
   and the IV-rank proxy with real tastytrade DXLink chain data (you
   already have OAuth2 working in `trading-reporter`), add a
   `TastytradeFeed` implementing the same `DataFeed` interface as
   `YFinanceFeed`, and a `TastytradeBroker` implementing the same
   interface as `PaperBroker`/`AlpacaBroker`.
4. Start live with a small fraction of `ACCOUNT.starting_equity` and the
   tightest circuit-breaker settings, then relax gradually.

This is deliberately not a recommendation to go live on any particular
timeline — that's a risk decision only you should make, informed by paper
results, not by this document.
