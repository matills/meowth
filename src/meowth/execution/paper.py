"""Pessimistic PaperExecutor: real Jupiter quotes, confirmation delay, fees, and tx failures."""

from __future__ import annotations

import asyncio
import random

from meowth.config import Settings
from meowth.data.jupiter import (
    LAMPORTS_PER_SOL,
    WSOL_MINT,
    JupiterQuoteClient,
    JupiterQuoteError,
)
from meowth.execution.base import Executor
from meowth.models import (
    OrderRequest,
    Quote,
    Side,
    TradeRecord,
    TradeStatus,
    TradingMode,
)
from meowth.storage.db import TradingDatabase

BASE_NETWORK_FEE_LAMPORTS = 5_000


class PaperExecutor(Executor):
    """Simulates Solana swaps pessimistically against live Jupiter quotes."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: TradingDatabase,
        jupiter: JupiterQuoteClient,
        confirm_delay_seconds: float = 1.5,
        tx_failure_rate: float = 0.05,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.jupiter = jupiter
        self.confirm_delay_seconds = confirm_delay_seconds
        self.tx_failure_rate = tx_failure_rate
        self._rng = rng or random.Random()

        self.cash_sol: float = settings.paper_initial_capital_sol
        self.positions_tokens: dict[str, float] = {}
        self.entry_cost_sol: dict[str, float] = {}

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        *,
        input_decimals: int = 9,
        output_decimals: int = 6,
    ) -> Quote:
        return await self.jupiter.get_quote(
            input_mint,
            output_mint,
            amount_atomic,
            slippage_bps=int(self.settings.risk_max_price_impact_pct * 10_000),
            input_decimals=input_decimals,
            output_decimals=output_decimals,
        )

    def _network_and_priority_fee_sol(self) -> float:
        total_lamports = BASE_NETWORK_FEE_LAMPORTS + self.settings.paper_priority_fee_lamports
        return total_lamports / LAMPORTS_PER_SOL

    async def execute_order(
        self,
        order: OrderRequest,
        *,
        token_decimals: int = 6,
    ) -> TradeRecord:
        net_fee_sol = self._network_and_priority_fee_sol()
        extra_slip = self.settings.paper_extra_slippage_bps / 10_000.0

        if order.side == Side.BUY:
            return await self._execute_buy(order, token_decimals, net_fee_sol, extra_slip)
        return await self._execute_sell(order, token_decimals, net_fee_sol, extra_slip)

    async def _execute_buy(
        self,
        order: OrderRequest,
        token_decimals: int,
        net_fee_sol: float,
        extra_slip: float,
    ) -> TradeRecord:
        if order.amount_sol > self.cash_sol:
            raise ValueError(
                f"Saldo insuficiente en cuenta paper: {self.cash_sol:.4f} < {order.amount_sol:.4f}"
            )

        lamports = int(order.amount_sol * LAMPORTS_PER_SOL)
        quote_1 = order.quote or await self.get_quote(
            WSOL_MINT,
            order.mint,
            lamports,
            input_decimals=9,
            output_decimals=token_decimals,
        )

        if self.confirm_delay_seconds > 0:
            await asyncio.sleep(self.confirm_delay_seconds)

        # Re-quote after confirmation delay and take the WORSE (higher) price
        quote_2 = await self.get_quote(
            WSOL_MINT,
            order.mint,
            lamports,
            input_decimals=9,
            output_decimals=token_decimals,
        )
        worse_quote = quote_1 if quote_1.price_sol >= quote_2.price_sol else quote_2

        # Simulated on-chain tx failure (still pays network + priority fee)
        if self.tx_failure_rate > 0 and self._rng.random() < self.tx_failure_rate:
            self.cash_sol = max(0.0, self.cash_sol - net_fee_sol)
            failed = TradeRecord(
                mode=TradingMode.PAPER,
                signal_id=order.signal_id,
                mint=order.mint,
                side=Side.BUY,
                status=TradeStatus.FAILED,
                strategy_name=order.strategy_name,
                reason=order.reason,
                qty_tokens=0.0,
                amount_sol=order.amount_sol,
                quoted_price_sol=quote_1.price_sol,
                filled_price_sol=0.0,
                price_impact_pct=worse_quote.price_impact_pct,
                fees_sol=net_fee_sol,
                exit_reason="simulated_tx_failure",
            )
            self.db.upsert_trade(failed)
            return failed

        filled_price = worse_quote.price_sol * (1.0 + extra_slip)
        total_fees = net_fee_sol + worse_quote.fee_sol
        net_sol_spent = max(0.0, order.amount_sol - total_fees)
        qty_tokens = net_sol_spent / filled_price if filled_price > 0 else 0.0
        slippage_bps = (
            ((filled_price - quote_1.price_sol) / quote_1.price_sol) * 10_000.0
            if quote_1.price_sol > 0
            else 0.0
        )

        self.cash_sol -= order.amount_sol
        self.positions_tokens[order.mint] = self.positions_tokens.get(order.mint, 0.0) + qty_tokens
        prev_cost = self.entry_cost_sol.get(order.mint, 0.0)
        self.entry_cost_sol[order.mint] = prev_cost + order.amount_sol

        record = TradeRecord(
            mode=TradingMode.PAPER,
            signal_id=order.signal_id,
            mint=order.mint,
            side=Side.BUY,
            status=TradeStatus.OPEN,
            strategy_name=order.strategy_name,
            reason=order.reason,
            qty_tokens=qty_tokens,
            amount_sol=order.amount_sol,
            quoted_price_sol=quote_1.price_sol,
            filled_price_sol=filled_price,
            slippage_bps=slippage_bps,
            price_impact_pct=worse_quote.price_impact_pct,
            fees_sol=total_fees,
        )
        self.db.upsert_trade(record)
        return record

    async def _execute_sell(
        self,
        order: OrderRequest,
        token_decimals: int,
        net_fee_sol: float,
        extra_slip: float,
    ) -> TradeRecord:
        held_tokens = self.positions_tokens.get(order.mint, 0.0)
        if held_tokens <= 0:
            raise ValueError(f"No hay posición abierta en {order.mint} para vender")

        atomic_qty = max(1, int(held_tokens * (10**token_decimals)))
        try:
            quote_1 = order.quote or await self.get_quote(
                order.mint,
                WSOL_MINT,
                atomic_qty,
                input_decimals=token_decimals,
                output_decimals=9,
            )
            if self.confirm_delay_seconds > 0:
                await asyncio.sleep(self.confirm_delay_seconds)
            quote_2 = await self.get_quote(
                order.mint,
                WSOL_MINT,
                atomic_qty,
                input_decimals=token_decimals,
                output_decimals=9,
            )
        except JupiterQuoteError:
            # Illiquid or rugged pool: position is trapped, cannot sell at a fictional price
            trapped = TradeRecord(
                mode=TradingMode.PAPER,
                signal_id=order.signal_id,
                mint=order.mint,
                side=Side.SELL,
                status=TradeStatus.TRAPPED,
                strategy_name=order.strategy_name,
                reason=order.reason,
                qty_tokens=held_tokens,
                amount_sol=0.0,
                quoted_price_sol=0.0,
                filled_price_sol=0.0,
                fees_sol=0.0,
                exit_reason="no_exit_liquidity_trapped",
            )
            self.db.upsert_trade(trapped)
            return trapped

        # Worse price on SELL = lower price_sol
        worse_quote = quote_1 if quote_1.price_sol <= quote_2.price_sol else quote_2

        if self.tx_failure_rate > 0 and self._rng.random() < self.tx_failure_rate:
            self.cash_sol = max(0.0, self.cash_sol - net_fee_sol)
            failed = TradeRecord(
                mode=TradingMode.PAPER,
                signal_id=order.signal_id,
                mint=order.mint,
                side=Side.SELL,
                status=TradeStatus.FAILED,
                strategy_name=order.strategy_name,
                reason=order.reason,
                qty_tokens=0.0,
                amount_sol=0.0,
                quoted_price_sol=quote_1.price_sol,
                filled_price_sol=0.0,
                price_impact_pct=worse_quote.price_impact_pct,
                fees_sol=net_fee_sol,
                exit_reason="simulated_tx_failure",
            )
            self.db.upsert_trade(failed)
            return failed

        filled_price = worse_quote.price_sol * (1.0 - extra_slip)
        total_fees = net_fee_sol + worse_quote.fee_sol
        gross_sol = held_tokens * filled_price
        net_sol_received = max(0.0, gross_sol - total_fees)
        cost_basis = self.entry_cost_sol.pop(order.mint, 0.0)
        self.positions_tokens.pop(order.mint, None)
        self.cash_sol += net_sol_received

        pnl_sol = net_sol_received - cost_basis
        pnl_pct = pnl_sol / cost_basis if cost_basis > 0 else 0.0
        slippage_bps = (
            ((quote_1.price_sol - filled_price) / quote_1.price_sol) * 10_000.0
            if quote_1.price_sol > 0
            else 0.0
        )

        record = TradeRecord(
            mode=TradingMode.PAPER,
            signal_id=order.signal_id,
            mint=order.mint,
            side=Side.SELL,
            status=TradeStatus.CLOSED,
            strategy_name=order.strategy_name,
            reason=order.reason,
            qty_tokens=held_tokens,
            amount_sol=net_sol_received,
            quoted_price_sol=quote_1.price_sol,
            filled_price_sol=filled_price,
            slippage_bps=slippage_bps,
            price_impact_pct=worse_quote.price_impact_pct,
            fees_sol=total_fees,
            pnl_sol=pnl_sol,
            pnl_pct=pnl_pct,
            exit_reason=order.reason,
        )
        self.db.upsert_trade(record)
        return record
