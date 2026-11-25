"""End-to-end tests for TradingEngine cycle (buy + rug exit) and Dashboard API payload."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx

from meowth.agents.analyst import AnalystAgent
from meowth.agents.scout import ScoutAgent
from meowth.config import Settings
from meowth.dashboard.server import build_dashboard_payload
from meowth.data.dexscreener import DexScreenerClient
from meowth.data.ingestor import MarketDataIngestor
from meowth.data.jupiter import WSOL_MINT, JupiterQuoteClient
from meowth.data.solana_rpc import OnChainTokenInspector
from meowth.engine import TradingEngine
from meowth.execution.paper import PaperExecutor
from meowth.models import Candle, Side, Signal, TokenCandidate
from meowth.risk.manager import RiskManager
from meowth.storage.db import TradingDatabase
from meowth.strategies.base import Strategy


class AlwaysBuyStrategy(Strategy):
    name = "always_buy_test"

    def evaluate(
        self,
        mint: str,
        candles: Sequence[Candle],
        *,
        has_open_position: bool = False,
    ) -> Signal | None:
        if not has_open_position and candles:
            return Signal(mint=mint, strategy_name=self.name, side=Side.BUY, reason="test_buy")
        return None


async def test_engine_executes_buy_then_exits_on_rug_and_populates_dashboard(
    settings: Settings, db: TradingDatabase
) -> None:
    now = datetime.now(UTC)
    db.upsert_token(
        TokenCandidate(
            mint="MintEngine1",
            symbol="ENG",
            liquidity_usd=100_000.0,
            mint_authority_revoked=True,
            freeze_authority_revoked=True,
            created_at=now - timedelta(hours=5),
            status="approved",
        )
    )

    # Cycle 1: healthy liquidity $100k -> BUY; Cycle 2: liquidity crashes to $30k -> RUG EXIT!
    cycle_liq = {"usd": 100_000.0}

    def dex_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana",
                    "dexId": "raydium",
                    "pairAddress": "PoolE1",
                    "baseToken": {"address": "MintEngine1", "symbol": "ENG", "name": "Engine"},
                    "priceUsd": "1.0",
                    "priceNative": "0.005",
                    "liquidity": {"usd": cycle_liq["usd"]},
                    "volume": {"h1": 60_000.0},
                    "pairCreatedAt": int((now - timedelta(hours=5)).timestamp() * 1000),
                }
            ],
        )

    def jup_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["inputMint"] == WSOL_MINT:
            return httpx.Response(
                200,
                json={"inAmount": "200000000", "outAmount": "40000000", "priceImpactPct": "0.005"},
            )
        return httpx.Response(
            200,
            json={"inAmount": "40000000", "outAmount": "180000000", "priceImpactPct": "0.01"},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(dex_handler)) as dex_http,
        httpx.AsyncClient(transport=httpx.MockTransport(jup_handler)) as jup_http,
    ):
        dex = DexScreenerClient(client=dex_http)
        jup = JupiterQuoteClient(client=jup_http)
        engine = TradingEngine(
            settings=settings,
            db=db,
            scout=ScoutAgent(
                settings=settings,
                db=db,
                dexscreener=dex,
                inspector=OnChainTokenInspector("https://rpc.test"),
                jupiter=jup,
            ),
            analyst=AnalystAgent(db=db, llm=None),
            ingestor=MarketDataIngestor(db=db, dexscreener=dex),
            strategy=AlwaysBuyStrategy(),
            risk=RiskManager(settings),
            executor=PaperExecutor(
                settings=settings,
                db=db,
                jupiter=jup,
                confirm_delay_seconds=0.0,
                tx_failure_rate=0.0,
            ),
        )

        # Cycle 1 -> opens position
        s1 = await engine.run_cycle(now=now)
        assert s1.trades_executed == 1
        assert s1.rug_exits == 0

        # Cycle 2 -> liquidity drops 70% -> rug monitor forces immediate exit!
        cycle_liq["usd"] = 30_000.0
        s2 = await engine.run_cycle(now=now + timedelta(minutes=5))
        assert s2.rug_exits == 1

    # Verify Dashboard API payloads reflect the live DuckDB state
    code, status_payload = build_dashboard_payload(db, "/api/status", {})
    assert code == 200
    assert isinstance(status_payload, dict)
    assert status_payload["trades_count"] >= 2

    code_eq, eq_payload = build_dashboard_payload(db, "/api/equity", {})
    assert code_eq == 200
    assert len(eq_payload) == 2

    code_c, candles_payload = build_dashboard_payload(
        db, "/api/candles", {"mint": ["MintEngine1"], "timeframe": ["5m"]}
    )
    assert code_c == 200
    assert len(candles_payload) >= 1
