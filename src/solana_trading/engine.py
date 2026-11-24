"""24/7 TradingEngine orchestrating Scout -> Analyst -> Strategist -> Risk -> Executor."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from solana_trading.agents.analyst import AnalystAgent
from solana_trading.agents.scout import ScoutAgent
from solana_trading.alerts.telegram import TelegramNotifier
from solana_trading.config import Settings
from solana_trading.data.ingestor import MarketDataIngestor
from solana_trading.data.jupiter import LAMPORTS_PER_SOL, WSOL_MINT, JupiterQuoteError
from solana_trading.execution.paper import PaperExecutor
from solana_trading.models import (
    EquitySnapshot,
    OrderRequest,
    Side,
    Timeframe,
    TradeStatus,
)
from solana_trading.risk.manager import PortfolioState, RiskManager
from solana_trading.storage.db import TradingDatabase
from solana_trading.strategies.base import Strategy

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class OpenPositionMeta:
    mint: str
    entry_price_sol: float
    entry_liquidity_usd: float
    opened_at: datetime
    peak_price_sol: float
    strategy_name: str


@dataclass(slots=True)
class CycleSummary:
    ingested_tokens: int = 0
    signals_generated: int = 0
    trades_executed: int = 0
    rejections_logged: int = 0
    rug_exits: int = 0


class TradingEngine:
    """Single-writer 24/7 paper trading engine with deterministic Risk veto and rug monitoring."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: TradingDatabase,
        scout: ScoutAgent,
        analyst: AnalystAgent,
        ingestor: MarketDataIngestor,
        strategy: Strategy,
        risk: RiskManager,
        executor: PaperExecutor,
        notifier: TelegramNotifier | None = None,
        timeframe: Timeframe = Timeframe.M5,
        rug_liquidity_drop_pct: float = 0.40,
    ) -> None:
        self.settings = settings
        self.db = db
        self.scout = scout
        self.analyst = analyst
        self.ingestor = ingestor
        self.strategy = strategy
        self.risk = risk
        self.executor = executor
        self.notifier = notifier
        self.timeframe = timeframe
        self.rug_liquidity_drop_pct = rug_liquidity_drop_pct

        self._peak_equity_sol = settings.paper_initial_capital_sol
        self._daily_pnl_sol = 0.0
        self._consecutive_rpc_errors = 0
        self._open_meta: dict[str, OpenPositionMeta] = {}

    def _portfolio_state(self, positions_value_sol: float) -> PortfolioState:
        total_eq = self.executor.cash_sol + positions_value_sol
        self._peak_equity_sol = max(self._peak_equity_sol, total_eq)
        last_ts_raw = self.db.get_state("last_data_ts")
        last_ts = datetime.fromisoformat(last_ts_raw) if last_ts_raw else None
        paused = self.db.get_state("manual_pause", "false") == "true"

        return PortfolioState(
            total_equity_sol=total_eq,
            peak_equity_sol=self._peak_equity_sol,
            daily_pnl_sol=self._daily_pnl_sol,
            open_positions_count=len(self._open_meta),
            total_exposure_sol=positions_value_sol,
            last_data_ts=last_ts,
            consecutive_rpc_errors=self._consecutive_rpc_errors,
            manually_paused=paused,
        )

    async def _notify(self, text: str) -> None:
        if self.notifier is not None and self.notifier.enabled:
            try:
                await self.notifier.send_message(text)
            except Exception as exc:  # pragma: no cover
                logger.warning("telegram_send_failed", error=str(exc))

    async def run_cycle(
        self,
        *,
        discover_mints: list[str] | None = None,
        now: datetime | None = None,
    ) -> CycleSummary:
        current = now or datetime.now(UTC)
        summary = CycleSummary()

        # 1. Optional Scout discovery pass
        if discover_mints:
            approved, rejected = await self.scout.scan_mints(discover_mints, now=current)
            summary.rejections_logged += len(rejected)
            for token in approved:
                assessment = await self.analyst.evaluate_candidate(token)
                if not assessment.approved:
                    token.status = "rejected_analyst"
                    self.db.upsert_token(token)
                    summary.rejections_logged += 1

        # 2. Ingest latest ticks & 1m/5m candles for approved tokens
        approved_tokens = self.db.get_approved_tokens()
        mints = [t.mint for t in approved_tokens]
        summary.ingested_tokens = await self.ingestor.ingest_mints(mints, now=current)

        token_by_mint = {t.mint: t for t in approved_tokens}
        latest_prices: dict[str, float] = {}
        latest_liq: dict[str, float] = {}

        for mint in mints:
            candles = self.db.get_recent_candles(mint, timeframe=self.timeframe.value, limit=100)
            if candles:
                latest_prices[mint] = candles[-1].close
                latest_liq[mint] = candles[-1].liquidity_usd

        # 3. Monitor open positions: Rug detector + Stop Loss + Max Hold + Strategy SELL
        for mint in list(self._open_meta.keys()):
            meta = self._open_meta[mint]
            price = latest_prices.get(mint, meta.entry_price_sol)
            liq = latest_liq.get(mint, meta.entry_liquidity_usd)
            meta.peak_price_sol = max(meta.peak_price_sol, price)

            exit_reason: str | None = None
            is_rug = (
                meta.entry_liquidity_usd > 0
                and liq < meta.entry_liquidity_usd * (1.0 - self.rug_liquidity_drop_pct)
            )
            if is_rug:
                exit_reason = (
                    f"rug_alert_liquidity_drop ({liq:,.0f} < {meta.entry_liquidity_usd:,.0f})"
                )
                summary.rug_exits += 1
            elif price <= meta.entry_price_sol * (1.0 - self.settings.risk_stop_loss_pct):
                exit_reason = "stop_loss"
            elif (current - meta.opened_at) >= timedelta(hours=self.settings.risk_max_hold_hours):
                exit_reason = "max_hold_time"
            else:
                tf_val = self.timeframe.value
                candles = self.db.get_recent_candles(mint, timeframe=tf_val, limit=100)
                sig = self.strategy.evaluate(mint, candles, has_open_position=True)
                if sig is not None and sig.side == Side.SELL:
                    self.db.insert_signal(sig)
                    summary.signals_generated += 1
                    exit_reason = sig.reason

            if exit_reason is not None:
                rec = await self.executor.execute_order(
                    OrderRequest(
                        signal_id="exit",
                        mint=mint,
                        side=Side.SELL,
                        amount_sol=0.0,
                        strategy_name=meta.strategy_name,
                        reason=exit_reason,
                    )
                )
                summary.trades_executed += 1
                if rec.status in (TradeStatus.CLOSED, TradeStatus.TRAPPED):
                    self._open_meta.pop(mint, None)
                    if rec.pnl_sol is not None:
                        self._daily_pnl_sol += rec.pnl_sol
                    await self._notify(
                        f"SELL {mint[:8]} ({rec.status.value}) | {exit_reason} | "
                        f"PnL: {(rec.pnl_sol or 0.0):+.4f} SOL"
                    )

        # 4. Evaluate new BUY entries through Strategist -> RiskManager -> PaperExecutor
        for mint in mints:
            if mint in self._open_meta:
                continue
            candles = self.db.get_recent_candles(mint, timeframe=self.timeframe.value, limit=100)
            sig = self.strategy.evaluate(mint, candles, has_open_position=False)
            if sig is None or sig.side != Side.BUY:
                continue

            self.db.insert_signal(sig)
            summary.signals_generated += 1

            pos_val = sum(
                qty * latest_prices.get(m, 0.0)
                for m, qty in self.executor.positions_tokens.items()
            )
            portfolio = self._portfolio_state(pos_val)
            desired_sol = portfolio.total_equity_sol * self.settings.risk_max_position_pct

            try:
                quote = await self.executor.get_quote(
                    WSOL_MINT,
                    mint,
                    max(1, int(desired_sol * LAMPORTS_PER_SOL)),
                )
                self._consecutive_rpc_errors = 0
            except JupiterQuoteError as exc:
                self._consecutive_rpc_errors += 1
                logger.warning("jupiter_quote_error", mint=mint, error=str(exc))
                continue

            order = OrderRequest(
                signal_id=sig.signal_id,
                mint=mint,
                side=Side.BUY,
                amount_sol=desired_sol,
                strategy_name=sig.strategy_name,
                reason=sig.reason,
                quote=quote,
            )
            decision = self.risk.evaluate_entry(order, token_by_mint[mint], portfolio, now=current)
            if not decision.approved:
                self.db.insert_rejection(decision.to_rejection(mint))
                summary.rejections_logged += 1
                if decision.kill_switch_triggered:
                    await self._notify(f"🛑 KILL SWITCH: {decision.reason}")
                continue

            order.amount_sol = decision.approved_amount_sol
            rec = await self.executor.execute_order(order)
            summary.trades_executed += 1
            if rec.status == TradeStatus.OPEN:
                self._open_meta[mint] = OpenPositionMeta(
                    mint=mint,
                    entry_price_sol=rec.filled_price_sol,
                    entry_liquidity_usd=latest_liq.get(mint, token_by_mint[mint].liquidity_usd),
                    opened_at=current,
                    peak_price_sol=rec.filled_price_sol,
                    strategy_name=sig.strategy_name,
                )
                await self._notify(
                    f"BUY {token_by_mint[mint].symbol} | {rec.amount_sol:.3f} SOL | {sig.reason}"
                )

        # 5. Record equity curve snapshot
        positions_val = sum(
            qty * latest_prices.get(m, 0.0) for m, qty in self.executor.positions_tokens.items()
        )
        total_eq = self.executor.cash_sol + positions_val
        self._peak_equity_sol = max(self._peak_equity_sol, total_eq)
        dd = (
            (self._peak_equity_sol - total_eq) / self._peak_equity_sol
            if self._peak_equity_sol > 0
            else 0.0
        )
        self.db.insert_equity_snapshot(
            EquitySnapshot(
                ts=current,
                mode=self.settings.trading_mode,
                cash_sol=self.executor.cash_sol,
                positions_value_sol=positions_val,
                total_equity_sol=total_eq,
                drawdown_pct=dd,
                open_positions=len(self._open_meta),
            )
        )
        return summary

    async def run_forever(self, interval_seconds: int = 60) -> None:
        logger.info("trading_engine_started", mode=self.settings.trading_mode.value)
        while True:
            try:
                summary = await self.run_cycle()
                logger.info(
                    "engine_cycle_completed",
                    ingested=summary.ingested_tokens,
                    signals=summary.signals_generated,
                    trades=summary.trades_executed,
                    rejections=summary.rejections_logged,
                    rug_exits=summary.rug_exits,
                )
            except Exception as exc:  # pragma: no cover
                logger.exception("engine_cycle_error", error=str(exc))
            await asyncio.sleep(interval_seconds)
