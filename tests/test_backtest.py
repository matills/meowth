"""Tests for deterministic strategies, walk-forward backtester, and reports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from meowth.backtest.engine import BacktestConfig, BacktestEngine
from meowth.backtest.reports import format_metrics_summary
from meowth.models import Candle, Timeframe
from meowth.strategies.implementations import (
    MeanReversionStrategy,
    MomentumBreakoutStrategy,
    VwapTrendStrategy,
)


def _make_candles(prices: list[float], volumes: list[float] | None = None) -> list[Candle]:
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    vols = volumes or [1000.0] * len(prices)
    out: list[Candle] = []
    for i, p in enumerate(prices):
        prev = prices[i - 1] if i > 0 else p
        out.append(
            Candle(
                mint="MintBT1",
                timeframe=Timeframe.M5,
                open_time=start + timedelta(minutes=5 * i),
                open=prev,
                high=max(prev, p) * 1.01,
                low=min(prev, p) * 0.99,
                close=p,
                volume_usd=vols[i],
                liquidity_usd=100_000.0,
            )
        )
    return out


def test_momentum_breakout_backtest_takes_partial_tp_and_computes_metrics() -> None:
    # Flat base for 20 bars, then breakout with volume spike (+70% rally), then pullback
    prices = [1.0] * 20 + [1.05, 1.15, 1.35, 1.65, 1.75, 1.40, 1.25, 1.10]
    vols = [1000.0] * 20 + [3500.0, 3000.0, 2800.0, 2500.0, 2000.0, 1800.0, 1500.0, 1200.0]
    candles = _make_candles(prices, vols)

    engine = BacktestEngine(BacktestConfig(initial_capital_sol=10.0, position_size_pct=0.05))
    result = engine.run(MomentumBreakoutStrategy(), "MintBT1", candles)

    assert result.metrics.total_trades >= 2  # partial TP + trailing stop / EMA exit
    assert result.metrics.total_fees_sol > 0.0
    assert result.metrics.net_return_pct > 0.0
    assert result.metrics.passes_gate_a is True

    summary = format_metrics_summary(result)
    assert "Gate A" in summary
    assert "APROBADO" in summary


def test_mean_reversion_and_vwap_strategies_run_walk_forward() -> None:
    # Oscillating synthetic market across 90 bars for 3 walk-forward folds
    pattern = [1.0, 0.95, 0.90, 0.85, 0.80, 0.78, 0.82, 0.88, 0.96, 1.04, 1.10, 1.02]
    prices = (pattern * 8)[:96]
    vols = [1000.0 if i % 6 != 0 else 2800.0 for i in range(len(prices))]
    candles = _make_candles(prices, vols)

    engine = BacktestEngine()
    wf_rsi = engine.run_walk_forward(MeanReversionStrategy(), "MintBT1", candles, folds=3)
    wf_vwap = engine.run_walk_forward(VwapTrendStrategy(), "MintBT1", candles, folds=3)

    assert len(wf_rsi) == 3
    assert len(wf_vwap) == 3
    for fold_res in wf_rsi + wf_vwap:
        assert len(fold_res.equity_curve) > 0
