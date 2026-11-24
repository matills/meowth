"""Tests for 1m/5m candle aggregation and MarketDataIngestor."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from solana_trading.data.candles import CandleAggregator, floor_timestamp
from solana_trading.data.dexscreener import DexScreenerClient
from solana_trading.data.ingestor import MarketDataIngestor
from solana_trading.models import Tick, Timeframe, TokenCandidate
from solana_trading.storage.db import TradingDatabase


def test_floor_timestamp_1m_and_5m() -> None:
    ts = datetime(2026, 11, 24, 10, 17, 42, tzinfo=UTC)
    assert floor_timestamp(ts, Timeframe.M1) == datetime(2026, 11, 24, 10, 17, 0, tzinfo=UTC)
    assert floor_timestamp(ts, Timeframe.M5) == datetime(2026, 11, 24, 10, 15, 0, tzinfo=UTC)


def test_candle_aggregator_builds_ohlcv_across_buckets(db: TradingDatabase) -> None:
    agg = CandleAggregator(db)
    t1 = datetime(2026, 11, 24, 10, 16, 10, tzinfo=UTC)
    t2 = datetime(2026, 11, 24, 10, 16, 50, tzinfo=UTC)
    t3 = datetime(2026, 11, 24, 10, 17, 5, tzinfo=UTC)

    liq = 60_000.0
    agg.process_tick(
        Tick(
            mint="MintA", ts=t1, price_sol=1.0, price_usd=180.0, volume_usd=100.0, liquidity_usd=liq
        )
    )
    agg.process_tick(
        Tick(
            mint="MintA", ts=t2, price_sol=1.2, price_usd=216.0, volume_usd=150.0, liquidity_usd=liq
        )
    )
    c_final = agg.process_tick(
        Tick(
            mint="MintA", ts=t3, price_sol=0.9, price_usd=162.0, volume_usd=80.0, liquidity_usd=liq
        )
    )

    m1, m5 = c_final[0], c_final[1]
    assert m1.timeframe == Timeframe.M1
    assert m1.open == pytest.approx(0.9)
    assert m1.close == pytest.approx(0.9)

    # All three ticks fall inside the 10:15 5m bucket
    assert m5.timeframe == Timeframe.M5
    assert m5.open == pytest.approx(1.0)
    assert m5.high == pytest.approx(1.2)
    assert m5.low == pytest.approx(0.9)
    assert m5.close == pytest.approx(0.9)
    assert m5.volume_usd == pytest.approx(330.0)

    # 2 buckets for 1m (10:16, 10:17) + 1 bucket for 5m (10:15) = 3 rows
    assert db.count_rows("candles") == 3


async def test_market_data_ingestor_persists_ticks_candles_and_heartbeat(
    db: TradingDatabase,
) -> None:
    db.upsert_token(
        TokenCandidate(
            mint="MintApproved1",
            symbol="APP",
            liquidity_usd=100_000.0,
            mint_authority_revoked=True,
            freeze_authority_revoked=True,
            status="approved",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "chainId": "solana",
                    "dexId": "raydium",
                    "pairAddress": "Pool1",
                    "baseToken": {"address": "MintApproved1", "symbol": "APP", "name": "Approved"},
                    "priceUsd": "0.50",
                    "priceNative": "0.0025",
                    "liquidity": {"usd": 100_000.0},
                    "volume": {"h1": 60_000.0},
                    "pairCreatedAt": 1_700_000_000_000,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        ingestor = MarketDataIngestor(
            db=db,
            dexscreener=DexScreenerClient(client=client),
        )
        count = await ingestor.ingest_mints()

    assert count == 1
    assert db.count_rows("ticks") == 1
    assert db.count_rows("candles") == 2  # 1m + 5m
    assert db.get_state("last_data_ts") is not None
