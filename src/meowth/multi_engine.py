"""Multi-asset 24/7 paper trading engine for Crypto CEX (Binance) and US Equities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from meowth.alerts.telegram import TelegramNotifier
from meowth.config import Settings
from meowth.execution.multi_asset_paper import MultiAssetPaperExecutor
from meowth.markets.base import MarketDataProvider, MarketQuote
from meowth.models import (
    EquitySnapshot,
    OrderRequest,
    Quote,
    Side,
    Timeframe,
    TokenCandidate,
    TradeStatus,
)
from meowth.risk.manager import PortfolioState, RiskManager
from meowth.storage.db import TradingDatabase
from meowth.strategies.base import Strategy

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class MultiAssetCycleSummary:
    symbols_updated: int = 0
    signals_generated: int = 0
    trades_executed: int = 0
    rejections_logged: int = 0


@dataclass(slots=True)
class _OpenPos:
    symbol: str
    entry_price_usd: float
    opened_at: datetime
    strategy_name: str


class MultiAssetEngine:
    """Runs deterministic strategies + RiskManager across high-liquidity crypto and US stocks."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: TradingDatabase,
        providers: dict[MarketDataProvider, list[str]],
        strategy: Strategy,
        risk: RiskManager,
        executor: MultiAssetPaperExecutor,
        notifier: TelegramNotifier | None = None,
        timeframe: Timeframe = Timeframe.M5,
    ) -> None:
        self.settings = settings
        self.db = db
        self.providers = providers
        self.strategy = strategy
        self.risk = risk
        self.executor = executor
        self.notifier = notifier
        self.timeframe = timeframe

        self._peak_equity_usd = executor.cash_usd
        self._daily_pnl_usd = 0.0
        self._open_pos: dict[str, _OpenPos] = {}

    async def _notify(self, text: str) -> None:
        if self.notifier is not None and self.notifier.enabled:
            try:
                await self.notifier.send_message(text)
            except Exception as exc:  # pragma: no cover
                logger.warning("telegram_send_failed", error=str(exc))

    def _portfolio_state(self, positions_val_usd: float, last_ts: datetime) -> PortfolioState:
        total_eq = self.executor.cash_usd + positions_val_usd
        self._peak_equity_usd = max(self._peak_equity_usd, total_eq)
        paused = self.db.get_state("manual_pause", "false") == "true"
        return PortfolioState(
            total_equity_sol=total_eq,
            peak_equity_sol=self._peak_equity_usd,
            daily_pnl_sol=self._daily_pnl_usd,
            open_positions_count=len(self._open_pos),
            total_exposure_sol=positions_val_usd,
            last_data_ts=last_ts,
            manually_paused=paused,
        )

    async def run_cycle(self, *, now: datetime | None = None) -> MultiAssetCycleSummary:
        current = now or datetime.now(UTC)
        summary = MultiAssetCycleSummary()
        latest_quotes: dict[str, MarketQuote] = {}

        # 1. Fetch quotes + 5m candles from every configured market provider
        for provider, symbols in self.providers.items():
            for symbol in symbols:
                try:
                    quote = await provider.fetch_quote(symbol)
                    candles = await provider.fetch_candles(
                        symbol, timeframe=self.timeframe, limit=100
                    )
                except Exception as exc:
                    logger.warning(
                        "market_fetch_error",
                        source=provider.source_name,
                        symbol=symbol,
                        error=str(exc),
                    )
                    continue

                latest_quotes[quote.symbol] = quote
                self.db.upsert_token(
                    TokenCandidate(
                        mint=quote.symbol,
                        symbol=quote.symbol,
                        name=f"{quote.symbol} ({provider.source_name})",
                        decimals=8,
                        dex=provider.source_name,
                        created_at=current - timedelta(days=365),
                        liquidity_usd=max(quote.volume_24h_usd, 1_000_000.0),
                        mint_authority_revoked=True,
                        freeze_authority_revoked=True,
                        status="approved",
                        updated_at=current,
                    )
                )
                for c in candles:
                    self.db.upsert_candle(c)
                summary.symbols_updated += 1

        if summary.symbols_updated > 0:
            self.db.set_state("last_data_ts", current.isoformat())

        # 2. Check exits on open positions (stop loss, max hold, strategy SELL)
        for symbol in list(self._open_pos.keys()):
            pos = self._open_pos[symbol]
            quote = latest_quotes.get(symbol)
            if quote is None:
                continue

            exit_reason: str | None = None
            if quote.price_usd <= pos.entry_price_usd * (1.0 - self.settings.risk_stop_loss_pct):
                exit_reason = "stop_loss"
            elif (current - pos.opened_at) >= timedelta(hours=self.settings.risk_max_hold_hours):
                exit_reason = "max_hold_time"
            else:
                candles = self.db.get_recent_candles(
                    symbol, timeframe=self.timeframe.value, limit=100
                )
                sig = self.strategy.evaluate(symbol, candles, has_open_position=True)
                if sig is not None and sig.side == Side.SELL:
                    self.db.insert_signal(sig)
                    summary.signals_generated += 1
                    exit_reason = sig.reason

            if exit_reason is not None:
                rec = self.executor.execute_sell(
                    quote=quote,
                    strategy_name=pos.strategy_name,
                    reason=exit_reason,
                )
                summary.trades_executed += 1
                if rec.status == TradeStatus.CLOSED:
                    self._open_pos.pop(symbol, None)
                    if rec.pnl_sol is not None:
                        self._daily_pnl_usd += rec.pnl_sol
                    await self._notify(
                        f"SELL {symbol} ({quote.source}) | {exit_reason} | "
                        f"PnL: ${(rec.pnl_sol or 0.0):+.2f}"
                    )

        # 3. Evaluate BUY signals across all updated symbols
        for symbol, quote in latest_quotes.items():
            if symbol in self._open_pos:
                continue
            candles = self.db.get_recent_candles(symbol, timeframe=self.timeframe.value, limit=100)
            sig = self.strategy.evaluate(symbol, candles, has_open_position=False)
            if sig is None or sig.side != Side.BUY:
                continue

            self.db.insert_signal(sig)
            summary.signals_generated += 1

            pos_val = sum(
                qty * latest_quotes[s].price_usd
                for s, qty in self.executor.positions_qty.items()
                if s in latest_quotes
            )
            portfolio = self._portfolio_state(pos_val, current)
            desired_usd = portfolio.total_equity_sol * self.settings.risk_max_position_pct

            order = OrderRequest(
                signal_id=sig.signal_id,
                mint=symbol,
                side=Side.BUY,
                amount_sol=desired_usd,
                strategy_name=sig.strategy_name,
                reason=sig.reason,
                quote=Quote(
                    input_mint="USD",
                    output_mint=symbol,
                    in_amount=int(desired_usd * 100),
                    out_amount=max(1, int((desired_usd / max(quote.price_usd, 1e-9)) * 100)),
                    price_sol=quote.price_usd,
                    price_impact_pct=quote.spread_pct,
                ),
            )
            token_meta = TokenCandidate(
                mint=symbol,
                symbol=symbol,
                liquidity_usd=max(quote.volume_24h_usd, 1_000_000.0),
                mint_authority_revoked=True,
                freeze_authority_revoked=True,
                created_at=current - timedelta(days=365),
            )
            decision = self.risk.evaluate_entry(order, token_meta, portfolio, now=current)
            if not decision.approved:
                self.db.insert_rejection(decision.to_rejection(symbol))
                summary.rejections_logged += 1
                continue

            rec = self.executor.execute_buy(
                quote=quote,
                notional_usd=decision.approved_amount_sol,
                strategy_name=sig.strategy_name,
                reason=sig.reason,
                signal_id=sig.signal_id,
            )
            summary.trades_executed += 1
            if rec.status == TradeStatus.OPEN:
                self._open_pos[symbol] = _OpenPos(
                    symbol=symbol,
                    entry_price_usd=rec.filled_price_sol,
                    opened_at=current,
                    strategy_name=sig.strategy_name,
                )
                await self._notify(
                    f"BUY {symbol} ({quote.source}) | ${rec.amount_sol:.2f} | {sig.reason}"
                )

        # 4. Record equity curve snapshot (in USD)
        positions_val = sum(
            qty * latest_quotes[s].price_usd
            for s, qty in self.executor.positions_qty.items()
            if s in latest_quotes
        )
        total_eq = self.executor.cash_usd + positions_val
        self._peak_equity_usd = max(self._peak_equity_usd, total_eq)
        dd = (
            (self._peak_equity_usd - total_eq) / self._peak_equity_usd
            if self._peak_equity_usd > 0
            else 0.0
        )
        self.db.insert_equity_snapshot(
            EquitySnapshot(
                ts=current,
                mode=self.settings.trading_mode,
                cash_sol=self.executor.cash_usd,
                positions_value_sol=positions_val,
                total_equity_sol=total_eq,
                drawdown_pct=dd,
                open_positions=len(self._open_pos),
            )
        )
        return summary

    async def run_forever(self, interval_seconds: int = 60) -> None:
        logger.info("multi_asset_engine_started", mode=self.settings.trading_mode.value)
        cycle_idx = 0
        while True:
            try:
                summary = await self.run_cycle()
                logger.info(
                    "multi_cycle_completed",
                    cycle=cycle_idx,
                    symbols=summary.symbols_updated,
                    signals=summary.signals_generated,
                    trades=summary.trades_executed,
                    rejections=summary.rejections_logged,
                )
            except Exception as exc:  # pragma: no cover
                logger.exception("multi_cycle_error", error=str(exc))
            cycle_idx += 1
            await asyncio.sleep(interval_seconds)
