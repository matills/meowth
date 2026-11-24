"""Deterministic walk-forward backtester reusing live Strategy implementations."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from solana_trading.models import Candle, Side
from solana_trading.strategies.base import Strategy


@dataclass(slots=True)
class BacktestConfig:
    initial_capital_sol: float = 10.0
    position_size_pct: float = 0.02
    pool_fee_bps: float = 30.0
    slippage_bps: float = 75.0
    network_fee_sol: float = 0.00001
    stop_loss_pct: float = 0.20
    take_profit_partial_pct: float = 0.50
    trailing_stop_pct: float = 0.15
    max_hold_hours: int = 24


@dataclass(slots=True)
class SimulatedTrade:
    mint: str
    strategy_name: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    size_sol: float
    pnl_sol: float
    pnl_pct: float
    fees_sol: float
    exit_reason: str


@dataclass(slots=True)
class BacktestMetrics:
    strategy_name: str
    total_trades: int
    win_rate: float
    net_return_pct: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_annualized: float
    profit_without_top_5_pct: float
    total_fees_sol: float
    final_equity_sol: float
    passes_gate_a: bool


@dataclass(slots=True)
class BacktestResult:
    metrics: BacktestMetrics
    trades: list[SimulatedTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)


def _compute_metrics(
    strategy_name: str,
    initial_capital: float,
    final_equity: float,
    trades: list[SimulatedTrade],
    equity_curve: list[tuple[datetime, float]],
) -> BacktestMetrics:
    total_trades = len(trades)
    wins = [t.pnl_sol for t in trades if t.pnl_sol > 0]
    losses = [abs(t.pnl_sol) for t in trades if t.pnl_sol < 0]

    win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
    net_return_pct = (
        (final_equity - initial_capital) / initial_capital if initial_capital > 0 else 0.0
    )

    gross_profit = sum(wins)
    gross_loss = sum(losses)
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    # Max drawdown from equity curve
    peak = initial_capital
    max_dd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

    # Profit excluding best 5% of trades (Gate B robustness metric)
    if total_trades > 0:
        sorted_pnls = sorted((t.pnl_sol for t in trades), reverse=True)
        drop_n = max(1, math.ceil(total_trades * 0.05)) if total_trades >= 2 else 0
        profit_without_top_5 = sum(sorted_pnls[drop_n:])
    else:
        profit_without_top_5 = 0.0

    # Simple annualized Sharpe from per-step equity returns (assuming ~288 5m bars/day)
    returns: list[float] = []
    for i in range(1, len(equity_curve)):
        prev_eq = equity_curve[i - 1][1]
        curr_eq = equity_curve[i][1]
        if prev_eq > 0:
            returns.append((curr_eq - prev_eq) / prev_eq)

    sharpe = 0.0
    if len(returns) >= 2:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var)
        if std > 0:
            # Annualize using 365 days; approximate bars per year from timestamps if possible
            span_seconds = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds()
            bars_per_year = (
                len(returns) * (365 * 86400 / span_seconds) if span_seconds > 0 else 365.0
            )
            sharpe = (mean_r / std) * math.sqrt(bars_per_year)

    total_fees = sum(t.fees_sol for t in trades)
    passes_gate_a = net_return_pct > 0.0 and profit_factor > 1.0 and total_trades > 0

    return BacktestMetrics(
        strategy_name=strategy_name,
        total_trades=total_trades,
        win_rate=win_rate,
        net_return_pct=net_return_pct,
        profit_factor=profit_factor,
        max_drawdown_pct=max_dd,
        sharpe_annualized=sharpe,
        profit_without_top_5_pct=profit_without_top_5,
        total_fees_sol=total_fees,
        final_equity_sol=final_equity,
        passes_gate_a=passes_gate_a,
    )


class BacktestEngine:
    """Runs a deterministic Strategy over historical candles with realistic DEX costs."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def _cost_rate(self) -> float:
        return (self.config.pool_fee_bps + self.config.slippage_bps) / 10_000.0

    def run(self, strategy: Strategy, mint: str, candles: Sequence[Candle]) -> BacktestResult:
        cfg = self.config
        cash = cfg.initial_capital_sol
        trades: list[SimulatedTrade] = []
        equity_curve: list[tuple[datetime, float]] = []

        if not candles:
            metrics = _compute_metrics(strategy.name, cfg.initial_capital_sol, cash, trades, [])
            return BacktestResult(metrics=metrics, trades=trades, equity_curve=equity_curve)

        cost_rate = self._cost_rate()

        # Open position state
        in_pos = False
        entry_time: datetime | None = None
        entry_price = 0.0
        qty_tokens = 0.0
        cost_basis_sol = 0.0
        entry_fee_sol = 0.0
        partial_taken = False
        peak_price = 0.0

        for idx in range(len(candles)):
            candle = candles[idx]
            window = candles[: idx + 1]

            if in_pos:
                peak_price = max(peak_price, candle.high)
                exit_price: float | None = None
                exit_reason = ""

                # 1. Stop loss check
                stop_price = entry_price * (1.0 - cfg.stop_loss_pct)
                if candle.low <= stop_price:
                    exit_price = stop_price
                    exit_reason = "stop_loss"

                # 2. Partial take profit (sell 50% at +50%)
                elif (
                    not partial_taken
                    and candle.high >= entry_price * (1.0 + cfg.take_profit_partial_pct)
                ):
                    tp_price = entry_price * (1.0 + cfg.take_profit_partial_pct)
                    sell_qty = qty_tokens * 0.5
                    exec_price = tp_price * (1.0 - cost_rate)
                    proceeds = sell_qty * exec_price - cfg.network_fee_sol
                    part_cost = cost_basis_sol * 0.5
                    part_fee = (sell_qty * tp_price * cost_rate) + cfg.network_fee_sol
                    pnl_sol = proceeds - part_cost

                    cash += proceeds
                    qty_tokens -= sell_qty
                    cost_basis_sol -= part_cost
                    partial_taken = True

                    trades.append(
                        SimulatedTrade(
                            mint=mint,
                            strategy_name=strategy.name,
                            entry_time=entry_time or candle.open_time,
                            exit_time=candle.open_time,
                            entry_price=entry_price,
                            exit_price=exec_price,
                            size_sol=part_cost,
                            pnl_sol=pnl_sol,
                            pnl_pct=pnl_sol / part_cost if part_cost > 0 else 0.0,
                            fees_sol=entry_fee_sol * 0.5 + part_fee,
                            exit_reason="take_profit_partial_50pct",
                        )
                    )
                    entry_fee_sol *= 0.5

                # 3. Trailing stop after partial take profit
                if in_pos and exit_price is None and partial_taken:
                    trail_price = peak_price * (1.0 - cfg.trailing_stop_pct)
                    if candle.low <= trail_price:
                        exit_price = trail_price
                        exit_reason = "trailing_stop"

                # 4. Max hold duration
                if (
                    in_pos
                    and exit_price is None
                    and entry_time is not None
                    and (candle.open_time - entry_time) >= timedelta(hours=cfg.max_hold_hours)
                ):
                    exit_price = candle.close
                    exit_reason = "max_hold_time"

                # 5. Strategy SELL signal
                if in_pos and exit_price is None:
                    sig = strategy.evaluate(mint, window, has_open_position=True)
                    if sig is not None and sig.side == Side.SELL:
                        exit_price = candle.close
                        exit_reason = sig.reason

                if in_pos and exit_price is not None:
                    exec_price = exit_price * (1.0 - cost_rate)
                    proceeds = qty_tokens * exec_price - cfg.network_fee_sol
                    exit_fee = (qty_tokens * exit_price * cost_rate) + cfg.network_fee_sol
                    pnl_sol = proceeds - cost_basis_sol
                    cash += proceeds

                    trades.append(
                        SimulatedTrade(
                            mint=mint,
                            strategy_name=strategy.name,
                            entry_time=entry_time or candle.open_time,
                            exit_time=candle.open_time,
                            entry_price=entry_price,
                            exit_price=exec_price,
                            size_sol=cost_basis_sol,
                            pnl_sol=pnl_sol,
                            pnl_pct=pnl_sol / cost_basis_sol if cost_basis_sol > 0 else 0.0,
                            fees_sol=entry_fee_sol + exit_fee,
                            exit_reason=exit_reason,
                        )
                    )
                    in_pos = False
                    qty_tokens = 0.0
                    cost_basis_sol = 0.0
                    partial_taken = False

            else:
                sig = strategy.evaluate(mint, window, has_open_position=False)
                if sig is not None and sig.side == Side.BUY and candle.close > 0:
                    size_sol = cash * cfg.position_size_pct
                    if size_sol > cfg.network_fee_sol * 2:
                        exec_price = candle.close * (1.0 + cost_rate)
                        net_sol = size_sol - cfg.network_fee_sol
                        qty_tokens = net_sol / exec_price
                        cash -= size_sol
                        entry_time = candle.open_time
                        entry_price = exec_price
                        cost_basis_sol = size_sol
                        entry_fee_sol = (net_sol * cost_rate) + cfg.network_fee_sol
                        peak_price = candle.close
                        partial_taken = False
                        in_pos = True

            mark_value = qty_tokens * candle.close if in_pos else 0.0
            equity_curve.append((candle.open_time, cash + mark_value))

        # Mark-to-market close at end of sample for clean accounting
        final_equity = equity_curve[-1][1] if equity_curve else cash
        metrics = _compute_metrics(
            strategy.name,
            cfg.initial_capital_sol,
            final_equity,
            trades,
            equity_curve,
        )
        return BacktestResult(metrics=metrics, trades=trades, equity_curve=equity_curve)

    def run_walk_forward(
        self,
        strategy: Strategy,
        mint: str,
        candles: Sequence[Candle],
        *,
        folds: int = 3,
        train_ratio: float = 0.7,
    ) -> list[BacktestResult]:
        """Evaluate strategy across sequential out-of-sample (OOS) walk-forward folds."""
        n = len(candles)
        if folds <= 0 or n < 20:
            return []

        fold_size = n // folds
        results: list[BacktestResult] = []
        for i in range(folds):
            start = i * fold_size
            end = n if i == folds - 1 else (i + 1) * fold_size
            split = start + int((end - start) * train_ratio)
            oos_slice = candles[split:end]
            if len(oos_slice) >= 5:
                results.append(self.run(strategy, mint, oos_slice))
        return results
