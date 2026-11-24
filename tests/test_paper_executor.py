"""Tests for pessimistic PaperExecutor: worse-of-two quotes, fees, failures, and trapped exits."""

from __future__ import annotations

import random

import httpx
import pytest

from solana_trading.config import Settings
from solana_trading.data.jupiter import WSOL_MINT, JupiterQuoteClient
from solana_trading.execution.paper import PaperExecutor
from solana_trading.models import OrderRequest, Side, TradeStatus
from solana_trading.storage.db import TradingDatabase


async def test_paper_executor_uses_worse_of_two_quotes_and_tracks_pnl(
    settings: Settings, db: TradingDatabase
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        in_mint = request.url.params["inputMint"]
        if in_mint == WSOL_MINT:
            # First buy quote gives 500 tokens, second (delayed) gives 490 (worse price!)
            out_amt = "500000000" if call_count == 1 else "490000000"
            return httpx.Response(
                200,
                json={"inAmount": "200000000", "outAmount": out_amt, "priceImpactPct": "0.005"},
            )
        # Sell quotes: return 0.25 SOL first, 0.24 SOL second (worse = 0.24)
        out_amt = "250000000" if call_count == 3 else "240000000"
        return httpx.Response(
            200,
            json={"inAmount": "487500000", "outAmount": out_amt, "priceImpactPct": "0.005"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        executor = PaperExecutor(
            settings=settings,
            db=db,
            jupiter=JupiterQuoteClient(client=http_client),
            confirm_delay_seconds=0.0,
            tx_failure_rate=0.0,
        )

        buy_rec = await executor.execute_order(
            OrderRequest(
                signal_id="s1",
                mint="TokenA",
                side=Side.BUY,
                amount_sol=0.2,
                strategy_name="momentum_breakout",
                reason="breakout",
            )
        )
        assert buy_rec.status == TradeStatus.OPEN
        assert buy_rec.filled_price_sol > buy_rec.quoted_price_sol
        assert buy_rec.slippage_bps > 0
        assert executor.cash_sol == pytest.approx(9.8)

        sell_rec = await executor.execute_order(
            OrderRequest(
                signal_id="s2",
                mint="TokenA",
                side=Side.SELL,
                amount_sol=0.0,
                strategy_name="momentum_breakout",
                reason="take_profit",
            )
        )
        assert sell_rec.status == TradeStatus.CLOSED
        assert sell_rec.filled_price_sol < sell_rec.quoted_price_sol
        assert sell_rec.pnl_sol is not None
        assert db.count_rows("trades") == 2


async def test_paper_executor_handles_simulated_tx_failure_and_trapped_liquidity(
    settings: Settings, db: TradingDatabase
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["inputMint"] == WSOL_MINT:
            return httpx.Response(
                200,
                json={"inAmount": "100000000", "outAmount": "100000000", "priceImpactPct": "0.01"},
            )
        # No exit liquidity on SELL
        return httpx.Response(404, text="No route found")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        # 100% tx failure rate -> FAILED trade, small network fee deducted, no open position
        failing_exec = PaperExecutor(
            settings=settings,
            db=db,
            jupiter=JupiterQuoteClient(client=http_client),
            confirm_delay_seconds=0.0,
            tx_failure_rate=1.0,
            rng=random.Random(42),
        )
        fail_rec = await failing_exec.execute_order(
            OrderRequest(
                signal_id="s-fail",
                mint="TokenRug",
                side=Side.BUY,
                amount_sol=0.1,
                strategy_name="test",
                reason="test",
            )
        )
        assert fail_rec.status == TradeStatus.FAILED
        assert "TokenRug" not in failing_exec.positions_tokens

        # Successful buy followed by illiquid sell -> TRAPPED
        exec_ok = PaperExecutor(
            settings=settings,
            db=db,
            jupiter=JupiterQuoteClient(client=http_client),
            confirm_delay_seconds=0.0,
            tx_failure_rate=0.0,
        )
        await exec_ok.execute_order(
            OrderRequest(
                signal_id="s-buy",
                mint="TokenRug",
                side=Side.BUY,
                amount_sol=0.1,
                strategy_name="test",
                reason="test",
            )
        )
        trapped_rec = await exec_ok.execute_order(
            OrderRequest(
                signal_id="s-sell",
                mint="TokenRug",
                side=Side.SELL,
                amount_sol=0.0,
                strategy_name="test",
                reason="stop_loss",
            )
        )
        assert trapped_rec.status == TradeStatus.TRAPPED
