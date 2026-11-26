"""Tests for LLM providers, AnalystAgent, and SupervisorAgent."""

from __future__ import annotations

import httpx

from meowth.agents.analyst import AnalystAgent
from meowth.agents.supervisor import SupervisorAgent
from meowth.llm.client import BudgetedLLMClient
from meowth.llm.providers import GeminiLLMClient, OpenAICompatibleLLMClient
from meowth.models import TokenCandidate
from meowth.storage.db import TradingDatabase


async def test_analyst_approves_or_rejects_via_openai_compatible_client(
    db: TradingDatabase,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"score": 3, "rug_risk": "high", '
                                '"approved": false, "rationale": "wallet sospechosa"}'
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 50},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        raw = OpenAICompatibleLLMClient(
            provider="grok",
            model="grok-mini",
            api_key="test-key",
            client=http_client,
        )
        budgeted = BudgetedLLMClient(raw, db=db, daily_budget_usd=1.0)
        analyst = AnalystAgent(db=db, llm=budgeted)

        res = await analyst.evaluate_candidate(
            TokenCandidate(mint="MintSus1", symbol="SUS", liquidity_usd=70_000.0)
        )

    assert res.approved is False
    assert res.rug_risk == "high"
    assert db.count_rows("rejections") == 1
    assert db.count_rows("llm_calls") == 1


async def test_analyst_falls_back_to_rules_when_budget_exceeded(db: TradingDatabase) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"score": 8, "rug_risk": "low", '
                                        '"approved": true, "rationale": "sólido"}'
                                    )
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 40},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        raw = GeminiLLMClient(model="gemini-2.0-flash", api_key="k", client=http_client)
        # Budget = $0.00 -> immediately triggers fallback to deterministic rules
        budgeted = BudgetedLLMClient(raw, db=db, daily_budget_usd=0.0)
        analyst = AnalystAgent(db=db, llm=budgeted)

        res = await analyst.evaluate_candidate(
            TokenCandidate(mint="MintOK", symbol="OK", liquidity_usd=100_000.0)
        )

    assert res.approved is True
    assert res.used_llm is False


async def test_analyst_falls_back_on_transient_503_error(db: TradingDatabase) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        raw = GeminiLLMClient(model="gemini-3.5-flash-lite", api_key="k", client=http_client)
        budgeted = BudgetedLLMClient(raw, db=db, daily_budget_usd=1.0)
        analyst = AnalystAgent(db=db, llm=budgeted)

        res = await analyst.evaluate_candidate(
            TokenCandidate(mint="Mint503", symbol="ERR", liquidity_usd=100_000.0)
        )

    assert res.approved is True
    assert res.used_llm is False


async def test_supervisor_generates_review_without_mutating_config(db: TradingDatabase) -> None:
    sup = SupervisorAgent(db=db, llm=None)
    report = await sup.generate_review()
    assert report.used_llm is False
    assert "0 trades" in report.summary
