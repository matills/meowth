"""USD-denominated pessimistic PaperExecutor for Crypto CEX (Binance) and US Equities."""

from __future__ import annotations

from meowth.markets.base import AssetClass, MarketQuote
from meowth.models import Side, TradeRecord, TradeStatus, TradingMode
from meowth.storage.db import TradingDatabase


class MultiAssetPaperExecutor:
    """Simulates realistic bid/ask + fee + slippage fills in USD for CEX crypto and stocks."""

    def __init__(
        self,
        *,
        db: TradingDatabase,
        initial_capital_usd: float = 10_000.0,
        crypto_fee_bps: float = 10.0,
        equity_fee_bps: float = 0.0,
        extra_slippage_bps: float = 10.0,
    ) -> None:
        self.db = db
        self.cash_usd = initial_capital_usd
        self.crypto_fee_bps = crypto_fee_bps
        self.equity_fee_bps = equity_fee_bps
        self.extra_slippage_bps = extra_slippage_bps

        self.positions_qty: dict[str, float] = {}
        self.entry_cost_usd: dict[str, float] = {}

    def _fee_rate(self, asset_class: AssetClass) -> float:
        bps = self.crypto_fee_bps if asset_class == AssetClass.CRYPTO_CEX else self.equity_fee_bps
        return bps / 10_000.0

    def execute_buy(
        self,
        *,
        quote: MarketQuote,
        notional_usd: float,
        strategy_name: str,
        reason: str,
        signal_id: str | None = None,
    ) -> TradeRecord:
        if notional_usd <= 0 or notional_usd > self.cash_usd:
            raise ValueError(
                f"Saldo USD insuficiente: disponible={self.cash_usd:.2f}, pedido={notional_usd:.2f}"
            )

        slip = self.extra_slippage_bps / 10_000.0
        fee_rate = self._fee_rate(quote.asset_class)
        filled_price = quote.ask_usd * (1.0 + slip)
        fees_usd = notional_usd * fee_rate
        net_usd = max(0.0, notional_usd - fees_usd)
        qty = net_usd / filled_price if filled_price > 0 else 0.0

        self.cash_usd -= notional_usd
        self.positions_qty[quote.symbol] = self.positions_qty.get(quote.symbol, 0.0) + qty
        prev_cost = self.entry_cost_usd.get(quote.symbol, 0.0)
        self.entry_cost_usd[quote.symbol] = prev_cost + notional_usd

        slippage_bps = (
            ((filled_price - quote.price_usd) / quote.price_usd) * 10_000.0
            if quote.price_usd > 0
            else 0.0
        )

        record = TradeRecord(
            mode=TradingMode.PAPER,
            signal_id=signal_id,
            mint=quote.symbol,
            side=Side.BUY,
            status=TradeStatus.OPEN,
            strategy_name=strategy_name,
            reason=reason,
            qty_tokens=qty,
            amount_sol=notional_usd,
            quoted_price_sol=quote.price_usd,
            filled_price_sol=filled_price,
            slippage_bps=slippage_bps,
            price_impact_pct=quote.spread_pct,
            fees_sol=fees_usd,
        )
        self.db.upsert_trade(record)
        return record

    def execute_sell(
        self,
        *,
        quote: MarketQuote,
        strategy_name: str,
        reason: str,
        signal_id: str | None = None,
    ) -> TradeRecord:
        qty = self.positions_qty.get(quote.symbol, 0.0)
        if qty <= 0:
            raise ValueError(f"No hay posición abierta en {quote.symbol}")

        slip = self.extra_slippage_bps / 10_000.0
        fee_rate = self._fee_rate(quote.asset_class)
        filled_price = quote.bid_usd * (1.0 - slip)
        gross_usd = qty * filled_price
        fees_usd = gross_usd * fee_rate
        net_usd = max(0.0, gross_usd - fees_usd)

        cost_basis = self.entry_cost_usd.pop(quote.symbol, 0.0)
        self.positions_qty.pop(quote.symbol, None)
        self.cash_usd += net_usd

        pnl_usd = net_usd - cost_basis
        pnl_pct = pnl_usd / cost_basis if cost_basis > 0 else 0.0
        slippage_bps = (
            ((quote.price_usd - filled_price) / quote.price_usd) * 10_000.0
            if quote.price_usd > 0
            else 0.0
        )

        record = TradeRecord(
            mode=TradingMode.PAPER,
            signal_id=signal_id,
            mint=quote.symbol,
            side=Side.SELL,
            status=TradeStatus.CLOSED,
            strategy_name=strategy_name,
            reason=reason,
            qty_tokens=qty,
            amount_sol=net_usd,
            quoted_price_sol=quote.price_usd,
            filled_price_sol=filled_price,
            slippage_bps=slippage_bps,
            price_impact_pct=quote.spread_pct,
            fees_sol=fees_usd,
            pnl_sol=pnl_usd,
            pnl_pct=pnl_pct,
            exit_reason=reason,
        )
        self.db.upsert_trade(record)
        return record
