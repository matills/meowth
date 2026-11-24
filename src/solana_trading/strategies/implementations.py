"""Concrete deterministic strategies for 1m/5m graduated Solana tokens."""

from __future__ import annotations

from collections.abc import Sequence

from solana_trading.models import Candle, Side, Signal
from solana_trading.strategies.base import Strategy, ema, rsi


class MomentumBreakoutStrategy(Strategy):
    """Buys breakouts above recent high with EMA trend alignment and volume expansion."""

    name = "momentum_breakout"

    def __init__(
        self,
        *,
        fast_ema: int = 5,
        slow_ema: int = 15,
        breakout_lookback: int = 10,
        volume_mult: float = 1.8,
    ) -> None:
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.breakout_lookback = breakout_lookback
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

        last = candles[-1]
        prev_window = candles[-self.breakout_lookback - 1 : -1]
        recent_high = max(c.high for c in prev_window)
        avg_vol = sum(volumes[-self.breakout_lookback - 1 : -1]) / self.breakout_lookback

        if not has_open_position:
            trend_up = fast[-1] > slow[-1]
            breakout = last.close > recent_high
            vol_spike = avg_vol > 0 and last.volume_usd >= avg_vol * self.volume_mult
            if trend_up and breakout and vol_spike:
                vol_ratio = last.volume_usd / avg_vol
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.BUY,
                    reason=(
                        f"Breakout sobre máximo {self.breakout_lookback} velas "
                        f"({last.close:.6f} > {recent_high:.6f}) con volumen {vol_ratio:.1f}x"
                    ),
                    metadata={"ema_fast": fast[-1], "ema_slow": slow[-1], "vol_ratio": vol_ratio},
                )
        else:
            if fast[-1] < slow[-1]:
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.SELL,
                    reason=(
                        f"Cruce bajista EMA({self.fast_ema}/{self.slow_ema}): "
                        f"{fast[-1]:.6f} < {slow[-1]:.6f}"
                    ),
                )
        return None


class MeanReversionStrategy(Strategy):
    """Buys oversold RSI pullbacks on green reversal candles; exits on RSI normalization."""

    name = "mean_reversion_rsi"

    def __init__(
        self,
        *,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 65.0,
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
        if len(candles) < self.rsi_period + 2:
            return None

        closes = [c.close for c in candles]
        prev_rsi = rsi(closes[:-1], self.rsi_period)
        curr_rsi = rsi(closes, self.rsi_period)
        if prev_rsi is None or curr_rsi is None:
            return None

        last = candles[-1]
        if not has_open_position:
            if prev_rsi <= self.oversold and last.close > last.open and curr_rsi > prev_rsi:
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.BUY,
                    reason=(
                        f"Rebote sobreventa RSI({self.rsi_period}): "
                        f"{prev_rsi:.1f} -> {curr_rsi:.1f} con vela alcista"
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
    """Buys when price reclaims rolling VWAP with volume confirmation; exits below VWAP."""

    name = "vwap_reclaim"

    def __init__(self, *, lookback: int = 20, min_volume_ratio: float = 1.3) -> None:
        self.lookback = lookback
        self.min_volume_ratio = min_volume_ratio

    def evaluate(
        self,
        mint: str,
        candles: Sequence[Candle],
        *,
        has_open_position: bool = False,
    ) -> Signal | None:
        if len(candles) < self.lookback + 1:
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
            reclaimed = prev.close <= vwap and last.close > vwap
            vol_ok = last.volume_usd >= avg_vol * self.min_volume_ratio
            if reclaimed and vol_ok:
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.BUY,
                    reason=(
                        f"Recuperación de VWAP({self.lookback}): "
                        f"{last.close:.6f} > {vwap:.6f}"
                    ),
                    metadata={"vwap": vwap, "vol_ratio": last.volume_usd / avg_vol},
                )
        else:
            if last.close < vwap * 0.99:
                return Signal(
                    mint=mint,
                    strategy_name=self.name,
                    side=Side.SELL,
                    reason=f"Pérdida de VWAP({self.lookback}): {last.close:.6f} < {vwap:.6f}",
                    metadata={"vwap": vwap},
                )
        return None
