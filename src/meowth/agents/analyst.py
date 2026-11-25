"""Analyst agent (LLM): scores candidate tokens that already passed Scout hard filters."""

from __future__ import annotations

import json
from dataclasses import dataclass

from meowth.llm.client import BudgetedLLMClient, LLMBudgetExceededError
from meowth.models import PipelineStage, Rejection, TokenCandidate
from meowth.storage.db import TradingDatabase

ANALYST_SYSTEM_PROMPT = (
    "Eres un analista cuantitativo de riesgo de tokens en Solana. "
    "Evalúa el candidato que ya pasó los filtros duros on-chain y responde SOLO JSON válido "
    'con las claves: {"score": int (0-10), "rug_risk": "low"|"medium"|"high", '
    '"approved": bool, "rationale": str breve}.'
)


@dataclass(slots=True)
class AnalystAssessment:
    approved: bool
    score: int
    rug_risk: str
    rationale: str
    used_llm: bool = True


class AnalystAgent:
    """Evaluates candidate quality/sentiment via LLM, falling back to rules on budget cap."""

    def __init__(
        self,
        *,
        db: TradingDatabase,
        llm: BudgetedLLMClient | None = None,
        min_score: int = 6,
    ) -> None:
        self.db = db
        self.llm = llm
        self.min_score = min_score

    async def evaluate_candidate(
        self,
        token: TokenCandidate,
        *,
        context_summary: str = "",
    ) -> AnalystAssessment:
        if self.llm is None:
            return AnalystAssessment(
                approved=True,
                score=7,
                rug_risk="unknown",
                rationale="LLM desactivado: operando con reglas deterministas.",
                used_llm=False,
            )

        user_payload = {
            "mint": token.mint,
            "symbol": token.symbol,
            "name": token.name,
            "dex": token.dex,
            "liquidity_usd": token.liquidity_usd,
            "top_holder_pct": token.top_holder_pct,
            "context": context_summary,
        }

        try:
            resp = await self.llm.complete_json(
                role="analyst",
                system_prompt=ANALYST_SYSTEM_PROMPT,
                user_prompt=json.dumps(user_payload, sort_keys=True),
                mint=token.mint,
            )
        except LLMBudgetExceededError:
            # Per plan: if daily LLM budget is exceeded, continue with pure deterministic rules
            return AnalystAssessment(
                approved=True,
                score=7,
                rug_risk="unknown",
                rationale="Tope diario de LLM alcanzado: continuando sin filtro IA.",
                used_llm=False,
            )

        data = resp.content
        score = int(data.get("score", 0))
        rug_risk = str(data.get("rug_risk", "high")).lower()
        rationale = str(data.get("rationale", "Sin justificación"))
        approved = (
            bool(data.get("approved", False))
            and score >= self.min_score
            and rug_risk != "high"
        )

        if not approved:
            msg = f"RECHAZADA: Analista LLM score={score}, riesgo={rug_risk} ({rationale})"
            self.db.insert_rejection(
                Rejection(
                    mint=token.mint,
                    stage=PipelineStage.ANALYST,
                    reason=msg,
                    details={"score": score, "rug_risk": rug_risk},
                )
            )

        return AnalystAssessment(
            approved=approved,
            score=score,
            rug_risk=rug_risk,
            rationale=rationale,
            used_llm=True,
        )
