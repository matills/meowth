"""Abstract Executor contract shared by PaperExecutor and LiveExecutor."""

from __future__ import annotations

from abc import ABC, abstractmethod

from solana_trading.models import OrderRequest, Quote, TradeRecord


class Executor(ABC):
    """Strategy code talks only to this interface; paper and live share the exact same logic."""

    @abstractmethod
    async def get_quote(self, input_mint: str, output_mint: str, amount_atomic: int) -> Quote:
        """Fetch a real-time quote (e.g. from Jupiter)."""

    @abstractmethod
    async def execute_order(self, order: OrderRequest) -> TradeRecord:
        """Execute an approved order and return the resulting TradeRecord."""
