"""Market data ingestor: polls approved Solana tokens into ticks and 1m/5m candles."""

from __future__ import annotations

from datetime import UTC, datetime

from meowth.data.candles import CandleAggregator
from meowth.data.dexscreener import DexPairSnapshot, DexScreenerClient
from meowth.models import Tick
from meowth.storage.db import TradingDatabase


class MarketDataIngestor:
    """Ingests live market snapshots into DuckDB ticks, 1m/5m candles, and heartbeat state."""

    def __init__(
        self,
        *,
        db: TradingDatabase,
        dexscreener: DexScreenerClient,
        aggregator: CandleAggregator | None = None,
    ) -> None:
        self.db = db
        self.dexscreener = dexscreener
        self.aggregator = aggregator or CandleAggregator(db)

    async def ingest_mints(
        self,
        mints: list[str] | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        """Fetch latest snapshots for the given (or approved) mints and persist ticks + candles."""
        target_mints = mints if mints is not None else self.db.get_approved_mints()
        if not target_mints:
            return 0

        pairs = await self.dexscreener.fetch_token_pairs(target_mints)
        best_by_mint: dict[str, DexPairSnapshot] = {}
        for pair in pairs:
            prev = best_by_mint.get(pair.mint)
            if prev is None or pair.liquidity_usd > prev.liquidity_usd:
                best_by_mint[pair.mint] = pair

        ts = now or datetime.now(UTC)
        ingested = 0
        for pair in best_by_mint.values():
            if pair.price_sol <= 0:
                continue
            tick = Tick(
                mint=pair.mint,
                ts=ts,
                price_sol=pair.price_sol,
                price_usd=pair.price_usd,
                volume_usd=pair.volume_h1_usd / 60.0,
                liquidity_usd=pair.liquidity_usd,
                source="dexscreener",
            )
            self.db.insert_tick(tick)
            self.aggregator.process_tick(tick)
            ingested += 1

        if ingested > 0:
            self.db.set_state("last_data_ts", ts.isoformat())

        return ingested
