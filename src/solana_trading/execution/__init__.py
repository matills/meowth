"""Execution interfaces and PaperExecutor simulator."""

from solana_trading.execution.base import Executor
from solana_trading.execution.paper import PaperExecutor

__all__ = ["Executor", "PaperExecutor"]
