"""Pluggable LLM provider implementations behind the common LLMClient interface."""

from __future__ import annotations

import json
import time

import httpx

from meowth.config import Settings
from meowth.llm.client import BudgetedLLMClient, LLMClient, LLMResponse
from meowth.storage.db import TradingDatabase

PROVIDER_BASE_URLS: dict[str, str] = {
    "grok": "https://api.x.ai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
}


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    in_per_1m: float,
    out_per_1m: float,
) -> float:
    return (input_tokens * in_per_1m + output_tokens * out_per_1m) / 1_000_000.0


class OpenAICompatibleLLMClient(LLMClient):
    """Works with Grok (xAI), DeepSeek, Ollama, and any OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str | None = None,
        input_cost_per_1m: float = 0.10,
        output_cost_per_1m: float = 0.40,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self._api_key = api_key
        self.base_url = (base_url or PROVIDER_BASE_URLS.get(provider, "")).rstrip("/")
        self._in_cost = input_cost_per_1m
        self._out_cost = output_cost_per_1m
        self._client = client
        self._owns_client = client is None

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        payload = {
            "model": self.model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        finally:
            if self._owns_client:
                await client.aclose()

        raw_text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens", 0))
        out_tok = int(usage.get("completion_tokens", 0))
        cost = _estimate_cost(in_tok, out_tok, self._in_cost, self._out_cost)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        return LLMResponse(
            content=json.loads(raw_text),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            latency_ms=latency_ms,
        )


class GeminiLLMClient(LLMClient):
    """Google Gemini REST client requesting strict JSON output."""

    provider = "gemini"

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        input_cost_per_1m: float = 0.10,
        output_cost_per_1m: float = 0.40,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._in_cost = input_cost_per_1m
        self._out_cost = output_cost_per_1m
        self._client = client
        self._owns_client = client is None

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> LLMResponse:
        t0 = time.perf_counter()
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }

        client = self._client or httpx.AsyncClient(timeout=30.0)
        try:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        finally:
            if self._owns_client:
                await client.aclose()

        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        usage = data.get("usageMetadata") or {}
        in_tok = int(usage.get("promptTokenCount", 0))
        out_tok = int(usage.get("candidatesTokenCount", 0))
        cost = _estimate_cost(in_tok, out_tok, self._in_cost, self._out_cost)
        latency_ms = int((time.perf_counter() - t0) * 1000)

        return LLMResponse(
            content=json.loads(raw_text),
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
            latency_ms=latency_ms,
        )


def build_llm_client(settings: Settings, db: TradingDatabase) -> BudgetedLLMClient | None:
    """Instantiate the configured LLM provider wrapped with daily budget enforcement."""
    provider = settings.llm_provider.strip().lower()
    if provider in ("", "none", "off"):
        return None

    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    model = settings.llm_model
    if not model:
        return None

    if provider == "gemini":
        raw: LLMClient = GeminiLLMClient(
            model=model,
            api_key=api_key,
            input_cost_per_1m=settings.llm_input_cost_per_1m_usd,
            output_cost_per_1m=settings.llm_output_cost_per_1m_usd,
        )
    elif provider in PROVIDER_BASE_URLS:
        raw = OpenAICompatibleLLMClient(
            provider=provider,
            model=model,
            api_key=api_key,
            input_cost_per_1m=settings.llm_input_cost_per_1m_usd,
            output_cost_per_1m=settings.llm_output_cost_per_1m_usd,
        )
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")

    return BudgetedLLMClient(
        client=raw,
        db=db,
        daily_budget_usd=settings.llm_daily_budget_usd,
    )
