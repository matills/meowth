"""Real-time 1m and 5m OHLCV candle aggregation from raw price ticks."""

from __future__ import annotations

from datetime import UTC, datetime

from meowth.models import Candle, Tick, Timeframe
from meowth.storage.db import TradingDatabase

DEFAULT_TIMEFRAMES: tuple[Timeframe, ...] = (Timeframe.M1, Timeframe.M5)


def floor_timestamp(ts: datetime, timeframe: Timeframe) -> datetime:
    """Floor a datetime to the start of its 1m or 5m UTC bucket."""
    utc_ts = ts.astimezone(UTC) if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    step = 1 if timeframe == Timeframe.M1 else 5
    minute = (utc_ts.minute // step) * step
    return utc_ts.replace(minute=minute, second=0, microsecond=0)


class CandleAggregator:
    """Aggregates raw ticks into 1m and 5m OHLCV candles and persists them to DuckDB."""

    def __init__(
        self,
        db: TradingDatabase,
        timeframes: tuple[Timeframe, ...] = DEFAULT_TIMEFRAMES,
    ) -> None:
        self._db = db
        self._timeframes = timeframes
        self._active: dict[tuple[str, Timeframe], Candle] = {}

    def process_tick(self, tick: Tick) -> list[Candle]:
        """Update all configured timeframe candles for a tick and persist to DuckDB."""
        updated: list[Candle] = []
        for tf in self._timeframes:
            bucket = floor_timestamp(tick.ts, tf)
            key = (tick.mint, tf)
            existing = self._active.get(key)

            if existing is None or existing.open_time != bucket:
                candle = Candle(
                    mint=tick.mint,
                    timeframe=tf,
                    open_time=bucket,
                    open=tick.price_sol,
                    high=tick.price_sol,
                    low=tick.price_sol,
                    close=tick.price_sol,
                    volume_usd=tick.volume_usd,
                    liquidity_usd=tick.liquidity_usd,
                )
            else:
                candle = Candle(
                    mint=tick.mint,
                    timeframe=tf,
                    open_time=bucket,
                    open=existing.open,
                    high=max(existing.high, tick.price_sol),
                    low=min(existing.low, tick.price_sol),
                    close=tick.price_sol,
                    volume_usd=existing.volume_usd + tick.volume_usd,
                    liquidity_usd=tick.liquidity_usd,
                )

            self._active[key] = candle
            self._db.upsert_candle(candle)
            updated.append(candle)

        return updated
