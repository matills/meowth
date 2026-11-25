"""Tests for BudgetedLLMClient caching, DuckDB logging, and daily cost cap."""

from __future__ import annotations

import pytest

from meowth.llm.client import (
    BudgetedLLMClient,
    LLMBudgetExceededError,
    LLMClient,
    LLMResponse,
)
from meowth.storage.db import TradingDatabase


class DummyLLMClient(LLMClient):
    provider = "dummy"
    model = "dummy-mini"

    def __init__(self, cost_per_call: float = 0.05) -> None:
        self.calls = 0
        self.cost_per_call = cost_per_call

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content={"rug_risk": "low", "score": 8},
            input_tokens=200,
            output_tokens=50,
            cost_usd=self.cost_per_call,
            latency_ms=42,
        )


async def test_budgeted_llm_caches_and_enforces_daily_cap(db: TradingDatabase) -> None:
    dummy = DummyLLMClient(cost_per_call=0.06)
    client = BudgetedLLMClient(client=dummy, db=db, daily_budget_usd=0.10)

    r1 = await client.complete_json(
        role="analyst",
        system_prompt="sys",
        user_prompt="token A",
        mint="MintA",
    )
    assert r1.cached is False
    assert dummy.calls == 1

    # Identical prompt hits DuckDB cache without incurring cost
    r2 = await client.complete_json(
        role="analyst",
        system_prompt="sys",
        user_prompt="token A",
        mint="MintA",
    )
    assert r2.cached is True
    assert dummy.calls == 1

    # Second distinct call brings spend to $0.12 >= $0.10
    await client.complete_json(
        role="analyst",
        system_prompt="sys",
        user_prompt="token B",
        mint="MintB",
    )
    assert dummy.calls == 2

    # Third distinct call is blocked by the daily cap
    with pytest.raises(LLMBudgetExceededError):
        await client.complete_json(
            role="analyst",
            system_prompt="sys",
            user_prompt="token C",
            mint="MintC",
        )
