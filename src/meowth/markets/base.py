"""Multi-asset market data abstractions (Solana DEX, Crypto CEX, US Equities)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from meowth.models import Candle, Timeframe


class AssetClass(StrEnum):
    SOLANA_DEX = "solana_dex"
    CRYPTO_CEX = "crypto_cex"
    EQUITY = "equity"


@dataclass(slots=True)
class MarketQuote:
    symbol: str
    asset_class: AssetClass
    price_usd: float
    bid_usd: float
    ask_usd: float
    volume_24h_usd: float = 0.0
    source: str = ""
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def spread_pct(self) -> float:
        if self.price_usd <= 0:
            return 0.0
        return max(0.0, (self.ask_usd - self.bid_usd) / self.price_usd)


class MarketDataProvider(ABC):
    """Common interface for fetching real-time quotes and OHLCV candles from any market."""

    asset_class: AssetClass
    source_name: str

    @abstractmethod
    async def fetch_quote(self, symbol: str) -> MarketQuote:
        """Fetch latest bid/ask/last quote in USD for the symbol."""

    @abstractmethod
    async def fetch_candles(
        self,
        symbol: str,
        *,
        timeframe: Timeframe = Timeframe.M5,
        limit: int = 100,
    ) -> list[Candle]:
        """Fetch recent OHLCV candles in chronological order."""
