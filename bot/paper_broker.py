"""
Simulated broker for paper trading. Persists state to a JSON file so the
Docker container can restart (image rebuild, TrueNAS reboot, etc.) without
losing open positions -- same durability concern you handled for the
market-news and earnings-backtest containers.

Exit management lives here too: each cycle, `check_exits()` marks-to-market
every open position against current price/vol and closes it if it has hit
its stop-loss or profit-target level (both set at entry time by
position_builder.py based on regime -- this is the dynamic-exit half of
"manages risk based on market conditions").
"""
from __future__ import annotations
import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from bot.config import DATA_DIR, COMMISSION_PER_CONTRACT, SLIPPAGE_PCT_OF_MID, ACCOUNT
from bot.models import Position, StrategyType
from bot.risk_manager import RiskManager
from bot.logger_setup import get_logger

log = get_logger(__name__)
STATE_FILE = DATA_DIR / "paper_positions.json"


class PaperBroker:
    def __init__(self, risk_manager: RiskManager):
        self.risk_manager = risk_manager
        self._load_state()

    # ---- persistence -------------------------------------------------

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            raw = json.loads(STATE_FILE.read_text())
            for p in raw.get("open_positions", []):
                p["opened_at"] = datetime.fromisoformat(p["opened_at"])
                p["strategy"] = StrategyType(p["strategy"])
                pos = Position(**p)
                self.risk_manager.open_positions.append(pos)
            self.risk_manager.equity = raw.get("equity", self.risk_manager.equity)
            self.risk_manager.peak_equity = raw.get("peak_equity", self.risk_manager.peak_equity)
            log.info("Restored %d open positions from %s", len(self.risk_manager.open_positions), STATE_FILE)
        except Exception as e:
            log.error("Failed to load paper broker state: %s", e)

    def _save_state(self) -> None:
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

    # ---- order simulation -------------------------------------------------

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
        # journaling context -- captured once at entry, never recomputed later
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
        signal=None,  # unused here; accepted so PaperBroker/AlpacaBroker share one call signature
    ) -> Position:
        slippage = abs(credit_or_debit) * SLIPPAGE_PCT_OF_MID
        commission = COMMISSION_PER_CONTRACT * contracts * (4 if "condor" in strategy.value else 2)
        fill_credit = credit_or_debit - slippage - (commission / max(contracts, 1))

        pos = Position(
            id=str(uuid.uuid4())[:8],
            symbol=symbol,
            strategy=strategy,
            opened_at=datetime.utcnow(),
            entry_credit_or_debit=round(fill_credit, 2),
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
        )
        self.risk_manager.register_open(pos)
        self._save_state()
        log.info(
            "PAPER FILL: opened %s x%d %s @ credit %.2f (slippage %.2f, commission %.2f)",
            symbol, contracts, strategy.value, fill_credit, slippage, commission,
        )
        return pos

    def close_position(self, position: Position, current_value: float, reason: str) -> float:
        """
        current_value: current cost to close the position (what you'd pay to
        buy back a short premium position, per contract, in the same units
        as entry_credit_or_debit).
        """
        pnl_per_contract = position.entry_credit_or_debit - current_value
        commission = COMMISSION_PER_CONTRACT * position.contracts * (4 if "condor" in position.strategy.value else 2)
        realized_pnl = (pnl_per_contract * position.contracts) - commission

        position.status = "closed"
        position.closed_at = datetime.utcnow()
        position.realized_pnl = round(realized_pnl, 2)

        self.risk_manager.register_close(position, realized_pnl)
        self._save_state()

        from bot.journal import log_closed_trade
        log_closed_trade(position, reason)

        log.info("PAPER CLOSE: %s (%s) pnl=%.2f reason=%s", position.symbol, position.id, realized_pnl, reason)
        return realized_pnl

    def check_exits(self, current_prices: dict[str, float]) -> None:
        """
        Very simplified mark-to-market: assumes current_value scales with
        how far price has moved against the position, capped at max_loss.
        This is a paper-trading approximation -- a live version should
        re-price the actual option chain via DXLink instead of this proxy.

        Also implements a trailing-stop ratchet ("monitor and adjust", risk
        management item #5): once a position has captured
        ACCOUNT.trailing_stop_activation_pct of its profit target, its
        effective stop is tightened to ACCOUNT.trailing_stop_lock_in_level
        (default: breakeven) so a winner that reverses gets closed near
        scratch instead of round-tripping into a full loss.
        """
        for pos in list(self.risk_manager.open_positions):
            spot = current_prices.get(pos.symbol)
            if spot is None:
                continue

            # crude proxy: assume current cost-to-close scales linearly with
            # elapsed theta decay captured, floored at 0 and capped at max_loss
            days_open = max((datetime.utcnow() - pos.opened_at).days, 0)
            theta_captured = abs(pos.theta_estimate) * days_open
            estimated_value = max(pos.entry_credit_or_debit - theta_captured, -pos.max_loss / max(pos.contracts, 1))

            profit_captured_pct = 1 - (estimated_value / pos.entry_credit_or_debit) if pos.entry_credit_or_debit else 0
            loss_multiple = -estimated_value / pos.entry_credit_or_debit if pos.entry_credit_or_debit and estimated_value < 0 else 0

            pos.high_water_mark_pct = max(pos.high_water_mark_pct, profit_captured_pct)

            activation_threshold = ACCOUNT.trailing_stop_activation_pct * pos.profit_target_pct
            if not pos.trailing_stop_active and pos.high_water_mark_pct >= activation_threshold:
                pos.trailing_stop_active = True
                old_level = pos.stop_loss_level
                pos.stop_loss_level = min(pos.stop_loss_level, ACCOUNT.trailing_stop_lock_in_level)
                log.info(
                    "TRAILING STOP activated on %s (%s): captured %.0f%% of target, "
                    "stop ratcheted %.2fx -> %.2fx",
                    pos.symbol, pos.id, pos.high_water_mark_pct * 100, old_level, pos.stop_loss_level,
                )
                self._save_state()

            if profit_captured_pct >= pos.profit_target_pct:
                self.close_position(pos, estimated_value, reason=f"profit target {pos.profit_target_pct:.0%} hit")
            elif loss_multiple >= pos.stop_loss_level:
                reason = "trailing stop hit" if pos.trailing_stop_active else f"stop loss {pos.stop_loss_level:.1f}x credit hit"
                self.close_position(pos, estimated_value, reason=reason)
