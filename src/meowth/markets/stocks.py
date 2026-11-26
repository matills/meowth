"""US Equities market data provider via Yahoo Finance public chart API (NVDA, SPY, QQQ, etc.)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from meowth.markets.base import AssetClass, MarketDataProvider, MarketQuote
from meowth.models import Candle, Timeframe

DEFAULT_EQUITY_SYMBOLS: list[str] = [
    "NVDA",
    "SPY",
    "QQQ",
    "AAPL",
    "MSFT",
]


class YahooEquityProvider(MarketDataProvider):
    """Fetches US stock quotes and 1m/5m intraday OHLCV bars from Yahoo Finance."""

    asset_class = AssetClass.EQUITY
    source_name = "yahoo_finance"

    def __init__(
        self,
        base_url: str = "https://query1.finance.yahoo.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds

    async def _fetch_chart(self, symbol: str, interval: str, range_str: str) -> dict:
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            resp = await client.get(
                f"{self.base_url}/v8/finance/chart/{symbol.upper()}",
                params={"interval": interval, "range": range_str},
            )
            resp.raise_for_status()
            payload = resp.json()
        finally:
            if self._owns_client:
                await client.aclose()

        results = (payload.get("chart") or {}).get("result") or []
        if not results:
            raise RuntimeError(f"No Yahoo Finance data for {symbol}")
        return results[0]

    async def fetch_quote(self, symbol: str) -> MarketQuote:
        sym = symbol.upper()
        chart = await self._fetch_chart(sym, interval="5m", range_str="1d")
        meta = chart.get("meta") or {}
        price = float(meta.get("regularMarketPrice") or 0.0)
        vol = float(meta.get("regularMarketVolume") or 0.0)
        # Estimate conservative 5 bps spread when L1 book is not provided
        half_spread = price * 0.00025
        return MarketQuote(
            symbol=sym,
            asset_class=self.asset_class,
            price_usd=price,
            bid_usd=max(0.0, price - half_spread),
            ask_usd=price + half_spread,
            volume_24h_usd=vol * price,
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
        range_str = "1d" if timeframe == Timeframe.M1 else "5d"
        chart = await self._fetch_chart(sym, interval=interval, range_str=range_str)

        timestamps = chart.get("timestamp") or []
        quote_block = ((chart.get("indicators") or {}).get("quote") or [{}])[0]
        opens = quote_block.get("open") or []
        highs = quote_block.get("high") or []
        lows = quote_block.get("low") or []
        closes = quote_block.get("close") or []
        volumes = quote_block.get("volume") or []

        candles: list[Candle] = []
        rows = zip(timestamps, opens, highs, lows, closes, volumes, strict=False)
        for ts, o, h, low_p, c, v in rows:
            if o is None or h is None or low_p is None or c is None:
                continue
            close_f = float(c)
            vol_usd = float(v or 0.0) * close_f
            candles.append(
                Candle(
                    mint=sym,
                    timeframe=timeframe,
                    open_time=datetime.fromtimestamp(int(ts), tz=UTC),
                    open=float(o),
                    high=float(h),
                    low=float(low_p),
                    close=close_f,
                    volume_usd=vol_usd,
                    liquidity_usd=50_000_000.0,
                )
            )
        return candles[-limit:]
