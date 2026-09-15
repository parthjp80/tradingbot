from __future__ import annotations
from tabulate import tabulate

from bot.config import DEFAULT_WATCHLIST, SCANNER, WatchlistItem, ACCOUNT, ALPACA, PREDICTION_MARKETS
from bot.data_feed import get_feed
from bot.regime import RegimeClassifier
from bot.scanner import MarketScanner
from bot.universe import get_universe
from bot.strategies.router import StrategyRouter
from bot.risk_manager import RiskManager, CircuitBreakerTripped
from bot.exit_rules import get_exit_rules
from bot.models import RegimeSnapshot, StrategyType
from bot.market_hours import zero_dte_force_close_utc
from bot.logger_setup import get_logger

log = get_logger(__name__)


class Orchestrator:
    def __init__(self):
        self.feed = get_feed()
        self.classifier = RegimeClassifier(self.feed)

        self.macro_engine = None
        if PREDICTION_MARKETS.enabled:
            from bot.prediction_markets import MacroSentimentEngine
            self.macro_engine = MacroSentimentEngine()
            log.info("Prediction-market macro signals: enabled (Kalshi).")

        self.scanner = MarketScanner(self.feed, self.classifier, macro_engine=self.macro_engine)
        self.router = StrategyRouter()
        self.risk_manager = RiskManager()

        if ALPACA.enabled:
            from bot.alpaca_broker import AlpacaBroker
            self.broker = AlpacaBroker(self.risk_manager)
            log.info("Broker backend: Alpaca paper trading account.")
        else:
            from bot.paper_broker import PaperBroker
            self.broker = PaperBroker(self.risk_manager)
            log.info("Broker backend: internal simulator.")

    def _build_cycle_watchlist(self) -> list[tuple[WatchlistItem, RegimeSnapshot | None, float]]:
        """
        Returns (WatchlistItem, RegimeSnapshot, scanner_score) triples for
        this cycle. Snapshot is pre-computed for scanner-sourced equity picks
        (the scan already classified them, no point doing it twice); it's
        None for static-watchlist mode, where the main loop classifies as it
        goes. scanner_score is the scanner's own 0-100 ranking score, 0.0 for
        anything that bypassed the scanner (static watchlist, static
        futures) -- see TradeSignal.scanner_score / the A+ sizing boost in
        risk_manager.size_position().
        """
        if not SCANNER.enabled:
            watchlist = DEFAULT_WATCHLIST
            if ALPACA.enabled:
                watchlist = [item for item in watchlist if item.asset_class != "future"]
            return [(item, None, 0.0) for item in watchlist]

        scan_results = self.scanner.scan(get_universe())
        tradeable = sorted([r for r in scan_results if r.passed], key=lambda r: r.score, reverse=True)
        top = tradeable[: SCANNER.max_equity_candidates]

        log.info(
            "Scanner selected %d/%d candidates: %s",
            len(top), len(scan_results),
            ", ".join(f"{r.symbol}({r.score:.0f},{r.snapshot.regime.value})" for r in top) or "none",
        )

        triples = [(WatchlistItem(r.symbol, "equity_option"), r.snapshot, r.score) for r in top]

        if ALPACA.enabled:
            log.info("Alpaca broker active: skipping futures (%s) -- not supported by Alpaca.",
                      ", ".join(f.symbol for f in SCANNER.static_futures))
        else:
            triples += [(item, None, 0.0) for item in SCANNER.static_futures]
        return triples

    def run_cycle(self) -> None:
        log.info("=" * 70)
        log.info("Starting cycle | equity=%.2f | open positions=%d | scanner=%s",
                  self.risk_manager.equity, len(self.risk_manager.open_positions),
                  "on" if SCANNER.enabled else "off (static watchlist)")

        macro_risk_off = False
        if self.macro_engine is not None:
            macro_risk_off = self.macro_engine.macro_risk_off()
            if macro_risk_off:
                log.warning("Macro risk-off flag is ACTIVE this cycle -- position sizing cut further.")

        current_prices = {}
        rows = []

        for item, precomputed_snapshot, scanner_score in self._build_cycle_watchlist():
            try:
                price_history = self.feed.history(item.symbol, period="1y", interval="1d")
                current_prices[item.symbol] = float(price_history["Close"].iloc[-1])

                snapshot = precomputed_snapshot or self.classifier.classify(item.symbol)

                already_open = any(
                    p.symbol == item.symbol and p.status == "open"
                    for p in self.risk_manager.open_positions
                )
                if already_open:
                    rows.append([item.symbol, snapshot.regime.value, "-", "skipped", "position already open"])
                    continue

                signal = self.router.route(snapshot, item.asset_class, price_history)

                if signal is None:
                    rows.append([item.symbol, snapshot.regime.value, "-", "no signal", "-"])
                    continue

                signal.scanner_score = scanner_score

                if self.macro_engine is not None and signal.direction in ("long", "short"):
                    tilt = self.macro_engine.directional_tilt(signal.direction)
                    if tilt != 0.0:
                        signal.confidence = max(0.0, min(1.0, signal.confidence + tilt))

                delta, theta, vega = signal.__dict__.get("_greeks_per_contract", (0.0, 0.0, 0.0))
                stop_mult, target_pct = get_exit_rules(signal.regime, signal.strategy)

                try:
                    contracts = self.risk_manager.evaluate_signal(
                        signal, stop_mult, target_pct, delta, theta, vega, macro_risk_off=macro_risk_off,
                    )
                except CircuitBreakerTripped as e:
                    log.warning("Circuit breaker: %s", e)
                    rows.append([item.symbol, snapshot.regime.value, signal.strategy.value, "BLOCKED", str(e)])
                    continue

                if contracts is None:
                    rows.append([item.symbol, snapshot.regime.value, signal.strategy.value, "rejected", "-"])
                    continue

                force_close_by = (
                    zero_dte_force_close_utc()
                    if signal.strategy == StrategyType.ZERO_DTE_IRON_CONDOR
                    else None
                )

                # est_credit_or_risk and est_max_loss from the strategy layer are
                # already expressed per contract (in dollars); max_loss stored on
                # the Position is the TOTAL across all contracts (paper_broker
                # divides back down by contracts when marking to market).
                opened_position = self.broker.open_position(
                    symbol=item.symbol,
                    strategy=signal.strategy,
                    credit_or_debit=signal.est_credit_or_risk if item.asset_class == "equity_option" else 0.0,
                    max_loss=signal.est_max_loss * contracts,
                    contracts=contracts,
                    stop_loss_multiple=stop_mult,
                    profit_target_pct=target_pct,
                    delta_estimate=delta,
                    theta_estimate=theta,
                    vega_estimate=vega,
                    direction=signal.direction,
                    regime_at_entry=snapshot.regime.value,
                    rationale=signal.rationale,
                    confidence=signal.confidence,
                    entry_spot_price=current_prices[item.symbol],
                    adx_at_entry=snapshot.adx,
                    iv_rank_at_entry=snapshot.iv_rank,
                    atr_pct_at_entry=signal.atr_pct,
                    sizing_method_at_entry=ACCOUNT.position_sizing_method,
                    consecutive_losses_at_entry=self.risk_manager.consecutive_losses,
                    force_close_by=force_close_by,
                    signal=signal,
                )

                if opened_position is None:
                    rows.append([item.symbol, snapshot.regime.value, signal.strategy.value,
                                 "FAILED", "broker rejected the order -- see logs"])
                else:
                    rows.append([item.symbol, snapshot.regime.value, signal.strategy.value,
                                 f"OPENED x{contracts}", signal.rationale])

            except Exception as e:
                log.exception("Error processing %s: %s", item.symbol, e)
                rows.append([item.symbol, "ERROR", "-", str(e), "-"])

        # exit management runs every cycle regardless of new signals
        self.broker.check_exits(current_prices)

        print(tabulate(rows, headers=["Symbol", "Regime", "Strategy", "Action", "Detail"], tablefmt="simple"))
        log.info(
            "Cycle complete | equity=%.2f | expectancy/trade=%.2f | win_rate=%.1f%% | open=%d | "
            "consecutive_losses=%d | sizing=%s",
            self.risk_manager.equity, self.risk_manager.stats.expectancy_per_trade,
            self.risk_manager.stats.win_rate * 100, len(self.risk_manager.open_positions),
            self.risk_manager.consecutive_losses, ACCOUNT.position_sizing_method,
        )

        from bot.journal import print_report
        print_report()
