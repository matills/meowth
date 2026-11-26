"""Tests for multi-asset providers (Binance crypto, Yahoo stocks), executor, and engine."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
import pytest

from meowth.config import Settings
from meowth.execution.multi_asset_paper import MultiAssetPaperExecutor
from meowth.markets.base import AssetClass, MarketQuote
from meowth.markets.binance import BinanceMarketProvider
from meowth.markets.stocks import YahooEquityProvider
from meowth.models import Candle, Side, Signal, Timeframe, TradeStatus
from meowth.multi_engine import MultiAssetEngine
from meowth.risk.manager import RiskManager
from meowth.storage.db import TradingDatabase
from meowth.strategies.base import Strategy


class _AlwaysBuyOnceStrategy(Strategy):
    name = "always_buy_multi"

    def evaluate(
        self,
        mint: str,
        candles: Sequence[Candle],
        *,
        has_open_position: bool = False,
    ) -> Signal | None:
        if not has_open_position and candles:
            return Signal(mint=mint, strategy_name=self.name, side=Side.BUY, reason="multi_buy")
        return None


async def test_binance_provider_parses_quote_and_klines() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ticker/24hr"):
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "lastPrice": "96500.0",
                    "bidPrice": "96495.0",
                    "askPrice": "96505.0",
                    "quoteVolume": "2500000000.0",
                },
            )
        if request.url.path.endswith("/klines"):
            return httpx.Response(
                200,
                json=[
                    [1732500000000, "96000", "96600", "95900", "96500", "10", 0, "965000"],
                ],
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        prov = BinanceMarketProvider(client=client)
        q = await prov.fetch_quote("BTCUSDT")
        candles = await prov.fetch_candles("BTCUSDT", timeframe=Timeframe.M5, limit=10)

    assert q.symbol == "BTCUSDT"
    assert q.asset_class == AssetClass.CRYPTO_CEX
    assert q.price_usd == pytest.approx(96500.0)
    assert len(candles) == 1
    assert candles[0].close == pytest.approx(96500.0)


async def test_yahoo_equity_provider_parses_nvda_quote_and_candles() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 145.5,
                                "regularMarketVolume": 50_000_000,
                            },
                            "timestamp": [1732500000, 1732500300],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [144.0, 145.0],
                                        "high": [145.2, 146.0],
                                        "low": [143.8, 144.9],
                                        "close": [145.0, 145.5],
                                        "volume": [1_000_000, 1_200_000],
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        prov = YahooEquityProvider(client=client)
        q = await prov.fetch_quote("NVDA")
        candles = await prov.fetch_candles("NVDA", timeframe=Timeframe.M5, limit=10)

    assert q.symbol == "NVDA"
    assert q.asset_class == AssetClass.EQUITY
    assert q.price_usd == pytest.approx(145.5)
    assert len(candles) == 2
    assert candles[-1].close == pytest.approx(145.5)


def test_multi_asset_paper_executor_tracks_usd_buy_sell_pnl(db: TradingDatabase) -> None:
    ex = MultiAssetPaperExecutor(db=db, initial_capital_usd=10_000.0)
    q_buy = MarketQuote(
        symbol="NVDA",
        asset_class=AssetClass.EQUITY,
        price_usd=100.0,
        bid_usd=99.95,
        ask_usd=100.05,
        source="yahoo_finance",
    )
    buy_rec = ex.execute_buy(
        quote=q_buy,
        notional_usd=200.0,
        strategy_name="breakout",
        reason="test_buy",
    )
    assert buy_rec.status == TradeStatus.OPEN
    assert ex.cash_usd == pytest.approx(9800.0)

    q_sell = MarketQuote(
        symbol="NVDA",
        asset_class=AssetClass.EQUITY,
        price_usd=110.0,
        bid_usd=109.95,
        ask_usd=110.05,
        source="yahoo_finance",
    )
    sell_rec = ex.execute_sell(quote=q_sell, strategy_name="breakout", reason="take_profit")
    assert sell_rec.status == TradeStatus.CLOSED
    assert sell_rec.pnl_sol is not None and sell_rec.pnl_sol > 15.0
    assert ex.cash_usd > 10_015.0


async def test_multi_asset_engine_runs_cycle_across_crypto_and_stocks(
    settings: Settings, db: TradingDatabase
) -> None:
    def binance_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ticker/24hr"):
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "lastPrice": "95000.0",
                    "bidPrice": "94995.0",
                    "askPrice": "95005.0",
                    "quoteVolume": "1000000000.0",
                },
            )
        return httpx.Response(
            200,
            json=[[1732500000000, "94800", "95100", "94700", "95000", "5", 0, "475000"]],
        )

    def yahoo_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 140.0,
                                "regularMarketVolume": 10_000_000,
                            },
                            "timestamp": [1732500000],
                            "indicators": {
                                "quote": [
                                    {
                                        "open": [139.0],
                                        "high": [140.5],
                                        "low": [138.8],
                                        "close": [140.0],
                                        "volume": [500_000],
                                    }
                                ]
                            },
                        }
                    ]
                }
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(binance_handler)) as b_http,
        httpx.AsyncClient(transport=httpx.MockTransport(yahoo_handler)) as y_http,
    ):
        engine = MultiAssetEngine(
            settings=settings,
            db=db,
            providers={
                BinanceMarketProvider(client=b_http): ["BTCUSDT"],
                YahooEquityProvider(client=y_http): ["NVDA"],
            },
            strategy=_AlwaysBuyOnceStrategy(),
            risk=RiskManager(settings),
            executor=MultiAssetPaperExecutor(db=db, initial_capital_usd=10_000.0),
        )
        summary = await engine.run_cycle(now=datetime.now(UTC))

    assert summary.symbols_updated == 2
    assert summary.trades_executed == 2
    assert db.count_rows("trades") == 2
    assert db.count_rows("candles") == 2
