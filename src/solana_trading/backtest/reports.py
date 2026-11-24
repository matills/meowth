"""QuantStats HTML tearsheet generation and Gate A/B summary reports."""

from __future__ import annotations

from pathlib import Path

from solana_trading.backtest.engine import BacktestResult


def format_metrics_summary(result: BacktestResult) -> str:
    m = result.metrics
    gate_label = "APROBADO" if m.passes_gate_a else "NO APROBADO"
    pf_str = f"{m.profit_factor:.2f}" if m.profit_factor != float("inf") else "inf"
    return (
        f"Estrategia: {m.strategy_name}\n"
        f"Trades: {m.total_trades} | Win rate: {m.win_rate:.1%}\n"
        f"Retorno neto: {m.net_return_pct:+.2%} | Profit factor: {pf_str}\n"
        f"Max drawdown: {m.max_drawdown_pct:.2%} | Sharpe anualizado: {m.sharpe_annualized:.2f}\n"
        f"Ganancia sin top 5% trades: {m.profit_without_top_5_pct:+.4f} SOL\n"
        f"Fees totales: {m.total_fees_sol:.4f} SOL | Equity final: {m.final_equity_sol:.4f} SOL\n"
        f"Gate A (backtest neto positivo con costos): {gate_label}"
    )


def generate_quantstats_tearsheet(
    result: BacktestResult,
    output_html: str | Path,
    *,
    title: str | None = None,
) -> Path:
    """Generate a QuantStats HTML tearsheet from a BacktestResult equity curve."""
    try:
        import pandas as pd
        import quantstats as qs
    except ImportError as exc:
        raise RuntimeError(
            "QuantStats y pandas son opcionales. Instala con: uv sync --extra backtest"
        ) from exc

    if len(result.equity_curve) < 2:
        raise ValueError("Se necesitan al menos 2 puntos en la curva de equity para el tearsheet")

    index = pd.DatetimeIndex([ts for ts, _ in result.equity_curve])
    series = pd.Series([eq for _, eq in result.equity_curve], index=index, dtype="float64")
    # Resample to daily returns if multi-day, otherwise use bar returns directly
    daily = series.resample("1D").last().dropna()
    returns = daily.pct_change().dropna() if len(daily) >= 3 else series.pct_change().dropna()

    out_path = Path(output_html)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    qs.reports.html(
        returns,
        output=str(out_path),
        title=title or f"Backtest — {result.metrics.strategy_name}",
    )
    return out_path
