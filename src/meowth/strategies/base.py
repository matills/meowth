"""Deterministic trading strategies shared identically between backtest, paper, and live."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from meowth.models import Candle, Signal


class Strategy(ABC):
    """Pure deterministic strategy interface — zero LLM calls, 100% backtestable."""

    name: str

    @abstractmethod
    def evaluate(
        self,
        mint: str,
        candles: Sequence[Candle],
        *,
        has_open_position: bool = False,
    ) -> Signal | None:
        """Return a BUY/SELL Signal with an explicit reason, or None if no action."""


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for val in values[1:]:
        result.append(val * k + result[-1] * (1.0 - k))
    return result


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(values) - period, len(values)):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
