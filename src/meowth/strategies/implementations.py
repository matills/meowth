"""Concrete deterministic strategies for 1m/5m crypto and equity candles."""

from __future__ import annotations

from collections.abc import Sequence

from meowth.models import Candle, Side, Signal
from meowth.strategies.base import Strategy, ema, rsi


class MomentumBreakoutStrategy(Strategy):
    """Buys confirmed breakouts aligned with EMA(9/21/50) trend and volume expansion."""

    name = "momentum_breakout"

    def __init__(
        self,
        *,
        fast_ema: int = 9,
        slow_ema: int = 21,
        trend_ema: int = 50,
        breakout_lookback: int = 12,
        min_breakout_pct: float = 0.0015,
        exit_buffer_pct: float = 0.0015,
        volume_mult: float = 1.8,
    ) -> None:
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.trend_ema = trend_ema
        self.breakout_lookback = breakout_lookback
        self.min_breakout_pct = min_breakout_pct
        self.exit_buffer_pct = exit_buffer_pct
        self.volume_mult = volume_mult

    def evaluate(
        self,
        mint: str,
        candles: Sequence[Candle],
        *,
        has_open_position: bool = False,
    ) -> Signal | None:
        min_len = max(self.slow_ema, self.breakout_lookback) + 2
        if len(candles) < min_len:
            return None

        closes = [c.close for c in candles]
        volumes = [c.volume_usd for c in candles]
        fast = ema(closes, self.fast_ema)
        slow = ema(closes, self.slow_ema)
        trend_period = min(self.trend_ema, len(closes) - 1)
        trend = ema(closes, trend_period)

        last = candles[-1]
        prev_window = candles[-self.breakout_lookback - 1 : -1]
        recent_high = max(c.high for c in prev_window)
        avg_vol = sum(volumes[-self.breakout_lookback - 1 : -1]) / self.breakout_lookback

        if not has_open_position:
            trend_up = fast[-1] > slow[-1] and last.close > trend[-1]
            breakout = last.close > recent_high * (1.0 + self.min_breakout_pct)
            vol_spike = avg_vol > 0 and last.volume_usd >= avg_vol * self.volume_mult
            bullish_bar = last.close > last.open
            if trend_up and breakout and vol_spike and bullish_bar:
                vol_ratio = last.volume_usd / avg_vol
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.BUY,
                    reason=(
                        f"Breakout {self.breakout_lookback}v "
                        f"({last.close:.6f} > {recent_high:.6f}) vol {vol_ratio:.1f}x + EMA50"
                    ),
                    metadata={"ema_fast": fast[-1], "ema_slow": slow[-1], "vol_ratio": vol_ratio},
                )
        else:
            # Require a clear bearish cross (buffer avoids micro-whipsaws)
            if fast[-1] < slow[-1] * (1.0 - self.exit_buffer_pct):
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.SELL,
                    reason=(
                        f"Cruce bajista confirmado EMA({self.fast_ema}/{self.slow_ema}): "
                        f"{fast[-1]:.6f} < {slow[-1]:.6f}"
                    ),
                )
        return None


class MeanReversionStrategy(Strategy):
    """Buys confirmed oversold RSI pullbacks; avoids falling knives and exits on RSI strength."""

    name = "mean_reversion_rsi"

    def __init__(
        self,
        *,
        rsi_period: int = 14,
        oversold: float = 28.0,
        overbought: float = 68.0,
    ) -> None:
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought

    def evaluate(
        self,
        mint: str,
        candles: Sequence[Candle],
        *,
        has_open_position: bool = False,
    ) -> Signal | None:
        if len(candles) < self.rsi_period + 3:
            return None

        closes = [c.close for c in candles]
        prev_rsi = rsi(closes[:-1], self.rsi_period)
        curr_rsi = rsi(closes, self.rsi_period)
        if prev_rsi is None or curr_rsi is None:
            return None

        prev = candles[-2]
        last = candles[-1]
        if not has_open_position:
            # Require real reversal: prior bar oversold + current bar closes above prior close
            rebounded = (
                prev_rsi <= self.oversold
                and last.close > last.open
                and last.close > prev.close
                and curr_rsi > prev_rsi + 2.0
            )
            if rebounded:
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.BUY,
                    reason=(
                        f"Rebote confirmado RSI({self.rsi_period}): "
                        f"{prev_rsi:.1f} -> {curr_rsi:.1f} sobre cierre previo"
                    ),
                    metadata={"prev_rsi": prev_rsi, "curr_rsi": curr_rsi},
                )
        else:
            if curr_rsi >= self.overbought:
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.SELL,
                    reason=f"Reversión completada RSI={curr_rsi:.1f} >= {self.overbought:.1f}",
                    metadata={"curr_rsi": curr_rsi},
                )
        return None


class VwapTrendStrategy(Strategy):
    """Buys confirmed VWAP reclaim aligned with EMA trend; exits cleanly below VWAP."""

    name = "vwap_reclaim"

    def __init__(
        self,
        *,
        lookback: int = 24,
        min_volume_ratio: float = 1.5,
        reclaim_buffer_pct: float = 0.001,
    ) -> None:
        self.lookback = lookback
        self.min_volume_ratio = min_volume_ratio
        self.reclaim_buffer_pct = reclaim_buffer_pct

    def evaluate(
        self,
        mint: str,
        candles: Sequence[Candle],
        *,
        has_open_position: bool = False,
    ) -> Signal | None:
        if len(candles) < self.lookback + 2:
            return None

        window = candles[-self.lookback :]
        total_vol = sum(c.volume_usd for c in window)
        if total_vol <= 0:
            return None

        vwap = sum(((c.high + c.low + c.close) / 3.0) * c.volume_usd for c in window) / total_vol
        avg_vol = total_vol / self.lookback
        prev = candles[-2]
        last = candles[-1]

        if not has_open_position:
            reclaimed = (
                prev.close <= vwap
                and last.close > vwap * (1.0 + self.reclaim_buffer_pct)
                and last.close > last.open
            )
            vol_ok = last.volume_usd >= avg_vol * self.min_volume_ratio
            if reclaimed and vol_ok:
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.BUY,
                    reason=(
                        f"Recuperación confirmada VWAP({self.lookback}): "
                        f"{last.close:.6f} > {vwap:.6f}"
                    ),
                    metadata={"vwap": vwap, "vol_ratio": last.volume_usd / avg_vol},
                )
        else:
            if last.close < vwap * 0.992:
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.SELL,
                    reason=f"Pérdida de VWAP({self.lookback}): {last.close:.6f} < {vwap:.6f}",
                    metadata={"vwap": vwap},
                )
        return None
