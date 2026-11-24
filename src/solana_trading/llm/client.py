"""Abstract LLM client interface with daily budget cap, caching, and DuckDB logging."""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from solana_trading.models import LLMCallRecord
from solana_trading.storage.db import TradingDatabase


class LLMBudgetExceededError(RuntimeError):
    """Raised when the configured daily LLM spend limit has been reached."""


@dataclass(slots=True)
class LLMResponse:
    content: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    cached: bool = False


class LLMClient(ABC):
    """Provider-agnostic interface for Analyst and Supervisor LLM calls."""

    provider: str
    model: str

    @abstractmethod
    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send a prompt and return a structured JSON dict with token usage."""


class BudgetedLLMClient:
    """Wraps any LLMClient with DuckDB logging, prompt caching, and a hard daily USD cap."""

    def __init__(
        self,
        client: LLMClient,
        db: TradingDatabase,
        daily_budget_usd: float,
    ) -> None:
        self._client = client
        self._db = db
        self._daily_budget_usd = daily_budget_usd

    @staticmethod
    def _hash_prompt(provider: str, model: str, system_prompt: str, user_prompt: str) -> str:
        payload = f"{provider}|{model}|{system_prompt}|{user_prompt}".encode()
        return hashlib.sha256(payload).hexdigest()

    async def complete_json(
        self,
        *,
        role: str,
        system_prompt: str,
        user_prompt: str,
        mint: str | None = None,
        use_cache: bool = True,
        temperature: float = 0.0,
    ) -> LLMResponse:
        prompt_hash = self._hash_prompt(
            self._client.provider,
            self._client.model,
            system_prompt,
            user_prompt,
        )

        if use_cache:
            cached_raw = self._db.get_cached_llm_response(role=role, prompt_hash=prompt_hash)
            if cached_raw is not None:
                return LLMResponse(
                    content=json.loads(cached_raw),
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    latency_ms=0,
                    cached=True,
                )

        spent_today = self._db.get_daily_llm_spend_usd()
        if spent_today >= self._daily_budget_usd:
            raise LLMBudgetExceededError(
                f"Daily LLM budget reached: ${spent_today:.4f} >= ${self._daily_budget_usd:.4f}"
            )

        t0 = time.perf_counter()
        response = await self._client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
        )
        latency_ms = response.latency_ms or int((time.perf_counter() - t0) * 1000)

        self._db.insert_llm_call(
            LLMCallRecord(
                role=role,
                provider=self._client.provider,
                model=self._client.model,
                mint=mint,
                prompt_hash=prompt_hash,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                latency_ms=latency_ms,
                response_json=json.dumps(response.content),
                cached=False,
            )
        )
        return response
