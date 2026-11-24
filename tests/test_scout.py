"""Tests for ScoutAgent hard filters, DexScreener parsing, and on-chain RPC inspection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from solana_trading.agents.scout import ScoutAgent
from solana_trading.config import Settings
from solana_trading.data.dexscreener import DexPairSnapshot, DexScreenerClient
from solana_trading.data.jupiter import WSOL_MINT, JupiterQuoteClient
from solana_trading.data.solana_rpc import OnChainTokenInspector
from solana_trading.storage.db import TradingDatabase

VALID_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # Valid Solana base58 pubkey (USDC)


def _make_rpc_client(
    *,
    mint_auth: str | None = None,
    freeze_auth: str | None = None,
    top_holder_amount: str = "100000000",
    total_supply: str = "1000000000",
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        method = body["method"]
        if method == "getAccountInfo":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "value": {
                            "data": {
                                "parsed": {
                                    "info": {
                                        "decimals": 6,
                                        "mintAuthority": mint_auth,
                                        "freezeAuthority": freeze_auth,
                                        "supply": total_supply,
                                    }
                                }
                            }
                        }
                    },
                },
            )
        if method == "getTokenLargestAccounts":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"value": [{"amount": top_holder_amount}, {"amount": "50000000"}]},
                },
            )
        return httpx.Response(400)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_jup_client(impact: str = "0.008", back_lamports: str = "99000000") -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["inputMint"] == WSOL_MINT:
            return httpx.Response(
                200,
                json={"inAmount": "100000000", "outAmount": "50000000", "priceImpactPct": impact},
            )
        return httpx.Response(
            200,
            json={"inAmount": "50000000", "outAmount": back_lamports, "priceImpactPct": impact},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _healthy_pair(now: datetime) -> DexPairSnapshot:
    return DexPairSnapshot(
        mint=VALID_MINT,
        symbol="TEST",
        name="Test Graduated",
        pool_address="Pool111",
        dex="raydium",
        price_usd=1.0,
        price_sol=0.005,
        liquidity_usd=150_000.0,
        volume_h1_usd=40_000.0,
        created_at=now - timedelta(hours=4),
    )


async def test_scout_approves_healthy_graduated_token(
    settings: Settings, db: TradingDatabase
) -> None:
    now = datetime.now(UTC)
    async with _make_rpc_client() as rpc_http, _make_jup_client() as jup_http:
        scout = ScoutAgent(
            settings=settings,
            db=db,
            dexscreener=DexScreenerClient(),
            inspector=OnChainTokenInspector("https://rpc.test", client=rpc_http),
            jupiter=JupiterQuoteClient(client=jup_http),
        )
        cand, rej = await scout.evaluate_pair(_healthy_pair(now), now=now)

    assert rej is None
    assert cand is not None
    assert cand.status == "approved"
    assert db.count_rows("tokens") == 1
    assert db.count_rows("rejections") == 0


async def test_scout_rejects_and_logs_reasons_in_duckdb(
    settings: Settings, db: TradingDatabase
) -> None:
    now = datetime.now(UTC)

    # 1. Ungraduated DEX (pumpfun)
    async with _make_rpc_client() as rpc_http, _make_jup_client() as jup_http:
        scout = ScoutAgent(
            settings=settings,
            db=db,
            dexscreener=DexScreenerClient(),
            inspector=OnChainTokenInspector("https://rpc.test", client=rpc_http),
            jupiter=JupiterQuoteClient(client=jup_http),
        )
        pump = _healthy_pair(now)
        pump.dex = "pumpfun"
        cand, rej = await scout.evaluate_pair(pump, now=now)
        assert cand is None
        assert rej is not None
        assert "DEX no permitido" in rej.reason

    # 2. Active freeze authority
    async with (
        _make_rpc_client(freeze_auth="DevWallet111") as rpc_http,
        _make_jup_client() as jup_http,
    ):
        scout = ScoutAgent(
            settings=settings,
            db=db,
            dexscreener=DexScreenerClient(),
            inspector=OnChainTokenInspector("https://rpc.test", client=rpc_http),
            jupiter=JupiterQuoteClient(client=jup_http),
        )
        cand, rej = await scout.evaluate_pair(_healthy_pair(now), now=now)
        assert cand is None
        assert rej is not None
        assert "freeze authority" in rej.reason

    # 3. High price impact (> 2%)
    async with _make_rpc_client() as rpc_http, _make_jup_client(impact="0.034") as jup_http:
        scout = ScoutAgent(
            settings=settings,
            db=db,
            dexscreener=DexScreenerClient(),
            inspector=OnChainTokenInspector("https://rpc.test", client=rpc_http),
            jupiter=JupiterQuoteClient(client=jup_http),
        )
        cand, rej = await scout.evaluate_pair(_healthy_pair(now), now=now)
        assert cand is None
        assert rej is not None
        assert "3.4%" in rej.reason

    assert db.count_rows("rejections") == 3
