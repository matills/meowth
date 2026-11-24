"""Supervisor agent (LLM): audits closed trades and proposes human-approved parameter tweaks."""

from __future__ import annotations

import json
from dataclasses import dataclass

from solana_trading.llm.client import BudgetedLLMClient, LLMBudgetExceededError
from solana_trading.storage.db import TradingDatabase

SUPERVISOR_SYSTEM_PROMPT = (
    "Eres el Supervisor de un sistema de trading en Solana. "
    "Audita las estadísticas recientes de trades y rechazos y propón ajustes prudentes. "
    "NUNCA apliques cambios automáticamente: solo propón para aprobación humana. "
    'Responde SOLO JSON válido: {"summary": str, "loss_patterns": [str], '
    '"proposed_adjustments": [{"param": str, "current": str, "suggested": str, "reason": str}]}.'
)


@dataclass(slots=True)
class SupervisorReport:
    summary: str
    loss_patterns: list[str]
    proposed_adjustments: list[dict[str, str]]
    used_llm: bool = True


class SupervisorAgent:
    """Reads DuckDB performance history and proposes parameter adjustments for human review."""

    def __init__(self, *, db: TradingDatabase, llm: BudgetedLLMClient | None = None) -> None:
        self.db = db
        self.llm = llm

    async def generate_review(self) -> SupervisorReport:
        stats = {
            "total_trades": self.db.count_rows("trades"),
            "total_rejections": self.db.count_rows("rejections"),
            "daily_llm_spend_usd": self.db.get_daily_llm_spend_usd(),
            "recent_trades": self.db.get_recent_trades(limit=25),
            "recent_rejections": self.db.get_recent_rejections(limit=25),
        }

        if self.llm is None:
            return SupervisorReport(
                summary=(
                    f"Resumen sin LLM: {stats['total_trades']} trades, "
                    f"{stats['total_rejections']} rechazos, "
                    f"gasto LLM hoy ${stats['daily_llm_spend_usd']:.4f}."
                ),
                loss_patterns=[],
                proposed_adjustments=[],
                used_llm=False,
            )

        try:
            resp = await self.llm.complete_json(
                role="supervisor",
                system_prompt=SUPERVISOR_SYSTEM_PROMPT,
                user_prompt=json.dumps(stats, default=str),
                use_cache=False,
            )
        except LLMBudgetExceededError:
            return SupervisorReport(
                summary="Tope diario de LLM alcanzado: revisión pospuesta.",
                loss_patterns=[],
                proposed_adjustments=[],
                used_llm=False,
            )

        data = resp.content
        return SupervisorReport(
            summary=str(data.get("summary", "")),
            loss_patterns=[str(x) for x in data.get("loss_patterns", [])],
            proposed_adjustments=list(data.get("proposed_adjustments", [])),
            used_llm=True,
        )
