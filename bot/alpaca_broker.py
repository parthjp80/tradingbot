"""
Broker backend that routes orders to your actual Alpaca PAPER trading
account instead of the internal simulator. Same public interface as
PaperBroker (open_position / close_position / check_exits), so the
orchestrator doesn't need to know which one it's talking to.

paper=True is HARDCODED below and is not controlled by any environment
variable or config value -- there is no way to point this class at a live
Alpaca account short of editing this file directly. That's intentional.

WHAT THIS DOES:
  - Takes the theoretical strikes your strategy layer wants (from
    TradeSignal.option_legs, computed via Black-Scholes in pricing.py) and
    resolves each one to a REAL listed option contract on Alpaca via
    GetOptionContractsRequest, matched by nearest strike within a window.
  - Places the whole spread as a single multi-leg (MLeg) limit order via
    Alpaca's Trading API (up to 4 legs -- covers strangles, verticals, and
    iron condors, which is everything this bot trades in options).
  - Marks positions to market each cycle using REAL option quotes
    (OptionHistoricalDataClient), not the theta-decay proxy PaperBroker
    uses -- this is a strict accuracy upgrade over the internal simulator
    for anything that actually reaches Alpaca.

WHAT THIS DOESN'T DO:
  - Futures. Alpaca doesn't offer futures trading. Any FUTURES_TREND
    signal is rejected here with a warning -- see orchestrator.py, which
    skips futures from the watchlist entirely when this backend is active
    rather than generating signals that can never fill.
  - Guarantee your account has options approved at Level 3 (needed for
    multi-leg spreads/iron condors). If your paper account isn't approved
    for that level, order submission will fail with an error from Alpaca;
    check your account's option trading level in the Alpaca dashboard.
  - Anything beyond best-effort fill-price reconciliation. Multi-leg
    orders can fill asynchronously; open_position() records the
    theoretical credit at submission time, and close_position() attempts
    one immediate poll for the actual fill before falling back to the
    last marked value. For anything you're relying on for real decisions,
    cross-check against the Alpaca dashboard directly.

THIS CODE IS UNTESTED AGAINST THE LIVE ALPACA API. It's built against the
documented alpaca-py request/response shapes (GetOptionContractsRequest,
OptionLegRequest, OrderClass.MLEG, PositionIntent) and the internal logic
is covered by mocked unit tests in tests/test_alpaca_broker.py, but no
real order has been placed against a real paper account from this
environment. Run a single small trade manually and check it in the Alpaca
dashboard before trusting this in the scheduled loop.
"""
from __future__ import annotations
import json
from datetime import datetime, date, timedelta
from typing import Optional

from bot.config import DATA_DIR, ALPACA, COMMISSION_PER_CONTRACT
from bot.models import Position, StrategyType, TradeSignal
from bot.risk_manager import RiskManager
from bot.logger_setup import get_logger

log = get_logger(__name__)
STATE_FILE = DATA_DIR / "alpaca_positions.json"


class AlpacaBroker:
    def __init__(self, risk_manager: RiskManager):
        if not ALPACA.api_key or not ALPACA.secret_key:
            raise RuntimeError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY must be set in .env to use the Alpaca broker "
                "(BOT_BROKER_BACKEND=alpaca_paper)."
            )

        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.option import OptionHistoricalDataClient

        self.risk_manager = risk_manager
        # paper=True is not a variable. Do not change this to read from config/env.
        self.trade_client = TradingClient(ALPACA.api_key, ALPACA.secret_key, paper=True)
        self.data_client = OptionHistoricalDataClient(ALPACA.api_key, ALPACA.secret_key)
        self._load_state()

    # ---- persistence -----------------------------------------------------
    # Local bookkeeping only -- this is our record of what we believe is
    # open, not a substitute for the Alpaca dashboard as the source of
    # truth. If orders fail partially or get modified outside the bot,
    # reconcile manually.

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text())
            for p in raw.get("open_positions", []):
                p["opened_at"] = datetime.fromisoformat(p["opened_at"])
                p["strategy"] = StrategyType(p["strategy"])
                self.risk_manager.open_positions.append(Position(**p))
            self.risk_manager.equity = raw.get("equity", self.risk_manager.equity)
            self.risk_manager.peak_equity = raw.get("peak_equity", self.risk_manager.peak_equity)
            log.info("Restored %d open Alpaca-tracked positions from %s", len(self.risk_manager.open_positions), STATE_FILE)
        except Exception as e:
            log.error("Failed to load Alpaca broker state: %s", e)

    def _save_state(self) -> None:
        from dataclasses import asdict
        payload = {
            "equity": self.risk_manager.equity,
            "peak_equity": self.risk_manager.peak_equity,
            "open_positions": [
                {**asdict(p), "opened_at": p.opened_at.isoformat(),
                 "strategy": p.strategy.value,
                 "closed_at": p.closed_at.isoformat() if p.closed_at else None}
                for p in self.risk_manager.open_positions
            ],
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2))

    # ---- contract resolution ------------------------------------------------

    def find_contract(self, symbol: str, is_call: bool, target_strike: float, days_to_expiration: int,
                       exclude_symbols: Optional[set] = None):
        """Finds the real listed contract closest to a theoretical strike,
        within ALPACA.strike_match_window_pct and ALPACA.expiration_match_window_days.
        `exclude_symbols`, if given, rules out contracts already claimed by
        another leg of the same order -- see open_position(), which uses
        this to avoid two theoretical legs (e.g. a short call and long call
        spread narrower than the real strike increment) collapsing onto the
        same real contract. Returns the contract object, or None if nothing
        matched."""
        from alpaca.trading.requests import GetOptionContractsRequest
        from alpaca.trading.enums import AssetStatus, ContractType

        target_date = date.today() + timedelta(days=days_to_expiration)
        window = ALPACA.strike_match_window_pct
        dte_window = ALPACA.expiration_match_window_days

        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            status=AssetStatus.ACTIVE,
            type=ContractType.CALL if is_call else ContractType.PUT,
            strike_price_gte=str(round(target_strike * (1 - window), 2)),
            strike_price_lte=str(round(target_strike * (1 + window), 2)),
            expiration_date_gte=target_date - timedelta(days=dte_window),
            expiration_date_lte=target_date + timedelta(days=dte_window),
        )
        try:
            contracts = self.trade_client.get_option_contracts(req).option_contracts
        except Exception as e:
            log.error("AlpacaBroker: contract lookup failed for %s: %s", symbol, e)
            return None

        if exclude_symbols:
            contracts = [c for c in contracts if c.symbol not in exclude_symbols]

        if not contracts:
            log.warning(
                "AlpacaBroker: no listed %s contract found for %s near strike %.2f (+/-%.0f%%, DTE %d+/-%d)%s",
                "call" if is_call else "put", symbol, target_strike, window * 100, days_to_expiration, dte_window,
                " after excluding contracts already used by another leg" if exclude_symbols else "",
            )
            return None

        def sort_key(c):
            try:
                oi = int(c.open_interest) if c.open_interest not in (None, "") else 0
            except (ValueError, TypeError):
                oi = 0
            return (abs(float(c.strike_price) - target_strike), -oi)

        # nearest strike wins; ties broken by higher open interest (more liquid)
        return min(contracts, key=sort_key)

    # ---- order placement ----------------------------------------------------

    def open_position(
        self,
        symbol: str,
        strategy: StrategyType,
        credit_or_debit: float,
        max_loss: float,
        contracts: int,
        stop_loss_multiple: float,
        profit_target_pct: float,
        delta_estimate: float = 0.0,
        theta_estimate: float = 0.0,
        vega_estimate: float = 0.0,
        direction: str = "neutral",
        regime_at_entry: str = "",
        rationale: str = "",
        confidence: float = 0.0,
        entry_spot_price: float = 0.0,
        adx_at_entry: float = 0.0,
        iv_rank_at_entry: float = 0.0,
        atr_pct_at_entry: float = 0.0,
        sizing_method_at_entry: str = "",
        consecutive_losses_at_entry: int = 0,
        signal: Optional[TradeSignal] = None,
    ) -> Optional[Position]:
        if strategy == StrategyType.FUTURES_TREND:
            log.warning("AlpacaBroker: %s is a futures signal -- Alpaca doesn't support futures, skipping.", symbol)
            return None

        if signal is None or not signal.option_legs:
            log.error("AlpacaBroker: no option_legs on signal for %s, cannot resolve real contracts, skipping.", symbol)
            return None

        resolved = []
        used_symbols = set()
        for leg in signal.option_legs:
            contract = self.find_contract(
                symbol, leg.is_call, leg.strike, signal.days_to_expiration, exclude_symbols=used_symbols,
            )
            if contract is None:
                log.error("AlpacaBroker: aborting %s entry -- could not resolve all legs to distinct real contracts.", symbol)
                return None
            used_symbols.add(contract.symbol)
            resolved.append((contract, leg.side))

        # belt-and-suspenders: exclude_symbols above should already prevent
        # this, but never submit an order with duplicate leg symbols --
        # Alpaca's API rejects it outright (pydantic ValidationError), and
        # a silent partial fix here is worse than a clear abort.
        resolved_symbols = [c.symbol for c, _ in resolved]
        if len(resolved_symbols) != len(set(resolved_symbols)):
            log.error(
                "AlpacaBroker: aborting %s entry -- resolved legs still contain duplicate symbols %s "
                "after collision avoidance. Strikes may be too close together for this underlying's "
                "listed strike increments.", symbol, resolved_symbols,
            )
            return None

        from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, PositionIntent

        order_legs = [
            OptionLegRequest(
                symbol=contract.symbol,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                ratio_qty=1,
                position_intent=PositionIntent.BUY_TO_OPEN if side == "buy" else PositionIntent.SELL_TO_OPEN,
            )
            for contract, side in resolved
        ]

        # Alpaca's mleg sign convention: positive limit_price = net debit paid,
        # negative = net credit received. Our credit_or_debit is already a
        # positive dollar credit per contract (already x100), so flip sign
        # and convert back to a per-share limit.
        limit_price = round(-(credit_or_debit / 100), 2) if credit_or_debit else 0.0

        req = LimitOrderRequest(
            qty=contracts,
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            legs=order_legs,
            limit_price=limit_price,
        )

        try:
            order = self.trade_client.submit_order(req)
        except Exception as e:
            log.error("AlpacaBroker: order submission failed for %s: %s", symbol, e)
            return None

        # Re-mark entry to a REAL quote-derived value before storing it.
        # credit_or_debit is theoretical (Black-Scholes, pricing.py), but
        # check_exits() marks positions to market using real Alpaca quotes.
        # Keeping the theoretical value as entry_credit_or_debit means the
        # very next check_exits() call compares a real mark against a
        # theoretical basis -- any model/market gap reads as an immediate
        # profit-target or stop-loss hit and triggers a close attempt on a
        # position that was never even filled yet (Alpaca rejects it:
        # "position intent mismatch, inferred: buy_to_open, specified:
        # buy_to_close"). Pricing entry the same way exits are priced
        # makes the two comparable from the first cycle on.
        entry_credit_or_debit = credit_or_debit
        try:
            from alpaca.data.requests import OptionLatestQuoteRequest
            leg_symbols = [c.symbol for c, _ in resolved]
            quotes = self.data_client.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=leg_symbols)
            )
            marked = 0.0
            for contract, leg_side in resolved:
                q = quotes.get(contract.symbol)
                if q is None:
                    raise ValueError(f"no quote for {contract.symbol}")
                # same sign convention as credit_or_debit: selling a leg
                # to open contributes what we'd receive (bid), buying a
                # leg to open contributes what we'd pay (ask, negated).
                marked += float(q.bid_price) if leg_side == "sell" else -float(q.ask_price)
            entry_credit_or_debit = round(marked * 100, 2)  # per-contract dollars, matches credit_or_debit's *100 convention
        except Exception as e:
            log.warning(
                "AlpacaBroker: could not mark %s entry to real quotes, falling back to theoretical "
                "credit/debit (%.2f) -- exit checks this cycle may be less accurate: %s",
                symbol, credit_or_debit, e,
            )

        # Multi-leg orders can fill asynchronously (README), and options
        # orders don't fill at all outside market hours -- check_exits()
        # must not attempt to close a position until this is confirmed
        # True, or Alpaca rejects it ("position intent mismatch, inferred:
        # buy_to_open, specified: buy_to_close"), since there's no actual
        # position yet to close.
        fill_confirmed = False
        try:
            fetched = self.trade_client.get_order_by_id(order.id)
            fill_confirmed = getattr(fetched, "filled_avg_price", None) is not None
        except Exception as e:
            log.warning("AlpacaBroker: could not confirm fill status for %s (%s), assuming pending: %s", symbol, order.id, e)

        pos = Position(
            id=str(order.id),
            symbol=symbol,
            strategy=strategy,
            opened_at=datetime.utcnow(),
            entry_credit_or_debit=entry_credit_or_debit,  # real-quote-marked when available; see note above
            broker_fill_confirmed=fill_confirmed,
            max_loss=max_loss,
            contracts=contracts,
            stop_loss_level=stop_loss_multiple,
            profit_target_pct=profit_target_pct,
            delta_estimate=delta_estimate,
            theta_estimate=theta_estimate,
            vega_estimate=vega_estimate,
            direction=direction,
            regime_at_entry=regime_at_entry,
            rationale=rationale,
            confidence=confidence,
            entry_spot_price=entry_spot_price,
            adx_at_entry=adx_at_entry,
            iv_rank_at_entry=iv_rank_at_entry,
            atr_pct_at_entry=atr_pct_at_entry,
            planned_stop_loss_level=stop_loss_multiple,
            planned_profit_target_pct=profit_target_pct,
            sizing_method_at_entry=sizing_method_at_entry,
            consecutive_losses_at_entry=consecutive_losses_at_entry,
            broker_name="alpaca_paper",
            broker_order_id=str(order.id),
            broker_leg_symbols=[{"symbol": c.symbol, "side": side} for c, side in resolved],
        )
        self.risk_manager.register_open(pos)
        self._save_state()
        log.info(
            "ALPACA ORDER SUBMITTED: %s x%d %s legs=%s order_id=%s limit=%.2f",
            symbol, contracts, strategy.value, [c.symbol for c, _ in resolved], order.id, limit_price,
        )
        return pos

    # ---- closing --------------------------------------------------------

    def close_position(self, position: Position, current_value: float, reason: str) -> Optional[float]:
        from alpaca.trading.requests import MarketOrderRequest, OptionLegRequest
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, PositionIntent

        order_legs = []
        for leg in position.broker_leg_symbols:
            opening_side = leg["side"]
            closing_side = OrderSide.SELL if opening_side == "buy" else OrderSide.BUY
            closing_intent = PositionIntent.SELL_TO_CLOSE if opening_side == "buy" else PositionIntent.BUY_TO_CLOSE
            order_legs.append(OptionLegRequest(
                symbol=leg["symbol"], side=closing_side, ratio_qty=1, position_intent=closing_intent,
            ))

        req = MarketOrderRequest(
            qty=position.contracts, order_class=OrderClass.MLEG, time_in_force=TimeInForce.DAY, legs=order_legs,
        )

        try:
            order = self.trade_client.submit_order(req)
        except Exception as e:
            log.error("AlpacaBroker: close order failed for %s (%s): %s", position.symbol, position.id, e)
            return None

        # best-effort immediate reconciliation; multi-leg orders can fill
        # asynchronously, so this may still be provisional -- see docstring
        fill_value = current_value
        try:
            fetched = self.trade_client.get_order_by_id(order.id)
            if getattr(fetched, "filled_avg_price", None):
                fill_value = float(fetched.filled_avg_price)
        except Exception:
            log.warning(
                "AlpacaBroker: could not confirm fill for close of %s (%s) -- recorded P&L is provisional, "
                "verify against the Alpaca dashboard.", position.symbol, position.id,
            )

        commission = COMMISSION_PER_CONTRACT * position.contracts * (4 if "condor" in position.strategy.value else 2)
        pnl_per_contract = position.entry_credit_or_debit - fill_value
        realized_pnl = (pnl_per_contract * position.contracts) - commission

        position.status = "closed"
        position.closed_at = datetime.utcnow()
        position.realized_pnl = round(realized_pnl, 2)

        self.risk_manager.register_close(position, realized_pnl)
        self._save_state()

        from bot.journal import log_closed_trade
        log_closed_trade(position, reason)

        log.info("ALPACA CLOSE: %s (%s) pnl=%.2f reason=%s", position.symbol, position.id, realized_pnl, reason)
        return realized_pnl

    # ---- exit monitoring --------------------------------------------------

    def check_exits(self, current_prices: dict) -> None:
        """
        Marks each open position to market using REAL option quotes (not
        the theta-decay proxy PaperBroker uses), then applies the same
        stop-loss / profit-target / trailing-stop logic.
        """
        from alpaca.data.requests import OptionLatestQuoteRequest
        from bot.config import ACCOUNT

        for pos in list(self.risk_manager.open_positions):
            if not pos.broker_leg_symbols:
                continue

            if not pos.broker_fill_confirmed:
                # Re-check in case it filled since the opening cycle --
                # otherwise skip entirely rather than risk a close attempt
                # against a position Alpaca doesn't consider open yet.
                try:
                    fetched = self.trade_client.get_order_by_id(pos.broker_order_id)
                    pos.broker_fill_confirmed = getattr(fetched, "filled_avg_price", None) is not None
                except Exception as e:
                    log.warning("AlpacaBroker: could not re-check fill status for %s (%s): %s", pos.symbol, pos.broker_order_id, e)
                if not pos.broker_fill_confirmed:
                    log.info("AlpacaBroker: %s (%s) still has no confirmed fill, skipping exit check this cycle.", pos.symbol, pos.broker_order_id)
                    continue

            leg_symbols = [leg["symbol"] for leg in pos.broker_leg_symbols]
            try:
                quotes = self.data_client.get_option_latest_quote(
                    OptionLatestQuoteRequest(symbol_or_symbols=leg_symbols)
                )
            except Exception as e:
                log.warning("AlpacaBroker: quote fetch failed for %s, skipping this cycle's exit check: %s", pos.symbol, e)
                continue

            # cost to close: buy back sold legs at the ask, sell bought legs at the bid
            cost_to_close = 0.0
            missing_quote = False
            for leg in pos.broker_leg_symbols:
                q = quotes.get(leg["symbol"])
                if q is None:
                    missing_quote = True
                    break
                if leg["side"] == "sell":
                    cost_to_close += float(q.ask_price)
                else:
                    cost_to_close -= float(q.bid_price)

            if missing_quote:
                log.warning("AlpacaBroker: missing leg quote for %s, skipping this cycle's exit check.", pos.symbol)
                continue

            estimated_value = cost_to_close
            profit_captured_pct = 1 - (estimated_value / pos.entry_credit_or_debit) if pos.entry_credit_or_debit else 0
            loss_multiple = -estimated_value / pos.entry_credit_or_debit if pos.entry_credit_or_debit and estimated_value < 0 else 0

            pos.high_water_mark_pct = max(pos.high_water_mark_pct, profit_captured_pct)

            activation_threshold = ACCOUNT.trailing_stop_activation_pct * pos.profit_target_pct
            if not pos.trailing_stop_active and pos.high_water_mark_pct >= activation_threshold:
                pos.trailing_stop_active = True
                pos.stop_loss_level = min(pos.stop_loss_level, ACCOUNT.trailing_stop_lock_in_level)
                self._save_state()

            if profit_captured_pct >= pos.profit_target_pct:
                self.close_position(pos, estimated_value, reason=f"profit target {pos.profit_target_pct:.0%} hit")
            elif loss_multiple >= pos.stop_loss_level:
                reason = "trailing stop hit" if pos.trailing_stop_active else f"stop loss {pos.stop_loss_level:.1f}x credit hit"
                self.close_position(pos, estimated_value, reason=reason)
