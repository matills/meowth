"""Binance public market data provider for high-liquidity crypto pairs (BTC, ETH, SOL, etc.)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from meowth.markets.base import AssetClass, MarketDataProvider, MarketQuote
from meowth.models import Candle, Timeframe

DEFAULT_CRYPTO_SYMBOLS: list[str] = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "AVAXUSDT",
]


class BinanceMarketProvider(MarketDataProvider):
    """Fetches live book tickers and 1m/5m klines from Binance public market data API."""

    asset_class = AssetClass.CRYPTO_CEX
    source_name = "binance"

    def __init__(
        self,
        base_url: str = "https://data-api.binance.vision",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds

    async def _get_json(self, path: str, params: dict[str, str | int]) -> object:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        finally:
            if self._owns_client:
                await client.aclose()

    async def fetch_quote(self, symbol: str) -> MarketQuote:
        sym = symbol.upper()
        ticker = await self._get_json("/api/v3/ticker/24hr", {"symbol": sym})
        assert isinstance(ticker, dict)
        last = float(ticker["lastPrice"])
        bid = float(ticker.get("bidPrice") or last)
        ask = float(ticker.get("askPrice") or last)
        quote_vol = float(ticker.get("quoteVolume") or 0.0)
        return MarketQuote(
            symbol=sym,
            asset_class=self.asset_class,
            price_usd=last,
            bid_usd=bid if bid > 0 else last,
            ask_usd=ask if ask > 0 else last,
            volume_24h_usd=quote_vol,
            source=self.source_name,
        )

    async def fetch_candles(
        self,
        symbol: str,
        *,
        timeframe: Timeframe = Timeframe.M5,
        limit: int = 100,
    ) -> list[Candle]:
        sym = symbol.upper()
        interval = "1m" if timeframe == Timeframe.M1 else "5m"
        raw = await self._get_json(
            "/api/v3/klines",
            {"symbol": sym, "interval": interval, "limit": min(max(limit, 1), 1000)},
        )
        if not isinstance(raw, list):
            return []

        candles: list[Candle] = []
        for row in raw:
            if not isinstance(row, list) or len(row) < 8:
                continue
            open_time = datetime.fromtimestamp(int(row[0]) / 1000.0, tz=UTC)
            candles.append(
                Candle(
                    mint=sym,
                    timeframe=timeframe,
                    open_time=open_time,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume_usd=float(row[7]),
                    liquidity_usd=10_000_000.0,
                )
            )
        return candles
