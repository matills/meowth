"""Deterministic RiskManager and kill switches. Pure code — never uses an LLM."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from solana_trading.config import Settings
from solana_trading.models import OrderRequest, PipelineStage, Rejection, TokenCandidate


@dataclass(slots=True)
class PortfolioState:
    total_equity_sol: float
    peak_equity_sol: float
    daily_pnl_sol: float
    open_positions_count: int
    total_exposure_sol: float
    last_data_ts: datetime | None = None
    consecutive_rpc_errors: int = 0
    manually_paused: bool = False

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity_sol <= 0:
            return 0.0
        return max(0.0, (self.peak_equity_sol - self.total_equity_sol) / self.peak_equity_sol)

    @property
    def daily_loss_pct(self) -> float:
        if self.total_equity_sol <= 0 or self.daily_pnl_sol >= 0:
            return 0.0
        return abs(self.daily_pnl_sol) / self.total_equity_sol


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    approved_amount_sol: float = 0.0
    reason: str = "OK"
    kill_switch_triggered: bool = False

    def to_rejection(self, mint: str) -> Rejection:
        return Rejection(
            mint=mint,
            stage=PipelineStage.RISK,
            reason=self.reason,
            details={"kill_switch": self.kill_switch_triggered},
        )


class RiskManager:
    """Enforces hard risk limits and kill switches with final veto over every order."""

    def __init__(self, settings: Settings, max_consecutive_rpc_errors: int = 5) -> None:
        self.settings = settings
        self.max_consecutive_rpc_errors = max_consecutive_rpc_errors

    def check_kill_switches(
        self,
        portfolio: PortfolioState,
        now: datetime | None = None,
    ) -> RiskDecision | None:
        current = now or datetime.now(UTC)

        if portfolio.manually_paused:
            return RiskDecision(
                approved=False,
                reason="RECHAZADA: kill switch manual activo",
                kill_switch_triggered=True,
            )

        if portfolio.drawdown_pct >= self.settings.risk_max_drawdown_pct:
            return RiskDecision(
                approved=False,
                reason=(
                    f"RECHAZADA: drawdown {portfolio.drawdown_pct:.1%}, "
                    f"límite {self.settings.risk_max_drawdown_pct:.1%}"
                ),
                kill_switch_triggered=True,
            )

        if portfolio.daily_loss_pct >= self.settings.risk_max_daily_loss_pct:
            return RiskDecision(
                approved=False,
                reason=(
                    f"RECHAZADA: pérdida diaria {portfolio.daily_loss_pct:.1%}, "
                    f"límite {self.settings.risk_max_daily_loss_pct:.1%}"
                ),
                kill_switch_triggered=True,
            )

        if portfolio.consecutive_rpc_errors >= self.max_consecutive_rpc_errors:
            return RiskDecision(
                approved=False,
                reason=(
                    f"RECHAZADA: errores consecutivos de RPC/Jupiter "
                    f"({portfolio.consecutive_rpc_errors})"
                ),
                kill_switch_triggered=True,
            )

        if portfolio.last_data_ts is not None:
            age_seconds = (current - portfolio.last_data_ts).total_seconds()
            if age_seconds > self.settings.risk_stale_data_seconds:
                return RiskDecision(
                    approved=False,
                    reason=(
                        f"RECHAZADA: datos viejos ({age_seconds:.0f}s > "
                        f"{self.settings.risk_stale_data_seconds}s)"
                    ),
                    kill_switch_triggered=True,
                )

        return None

    def evaluate_entry(
        self,
        order: OrderRequest,
        token: TokenCandidate,
        portfolio: PortfolioState,
        now: datetime | None = None,
    ) -> RiskDecision:
        current = now or datetime.now(UTC)

        kill_decision = self.check_kill_switches(portfolio, now=current)
        if kill_decision is not None:
            return kill_decision

        # Hard token safety checks (defense-in-depth after Scout)
        if not token.mint_authority_revoked:
            return RiskDecision(approved=False, reason="RECHAZADA: mint authority activa")
        if not token.freeze_authority_revoked:
            return RiskDecision(approved=False, reason="RECHAZADA: freeze authority activa")
        if token.liquidity_usd < self.settings.risk_min_liquidity_usd:
            return RiskDecision(
                approved=False,
                reason=(
                    f"RECHAZADA: liquidez USD {token.liquidity_usd:,.0f}, "
                    f"mínimo USD {self.settings.risk_min_liquidity_usd:,.0f}"
                ),
            )
        if token.created_at is not None:
            age_minutes = (current - token.created_at).total_seconds() / 60.0
            if age_minutes < self.settings.risk_min_token_age_minutes:
                return RiskDecision(
                    approved=False,
                    reason=(
                        f"RECHAZADA: edad del token {age_minutes:.0f}m, "
                        f"mínimo {self.settings.risk_min_token_age_minutes}m"
                    ),
                )

        # Quote price impact check (max 2% by default)
        if order.quote is not None:
            if order.quote.price_impact_pct > self.settings.risk_max_price_impact_pct:
                return RiskDecision(
                    approved=False,
                    reason=(
                        f"RECHAZADA: slippage/impacto estimado "
                        f"{order.quote.price_impact_pct:.1%}, "
                        f"límite {self.settings.risk_max_price_impact_pct:.1%}"
                    ),
                )

        # Position count & total exposure checks
        if portfolio.open_positions_count >= self.settings.risk_max_open_positions:
            return RiskDecision(
                approved=False,
                reason=(
                    f"RECHAZADA: posiciones abiertas {portfolio.open_positions_count}, "
                    f"límite {self.settings.risk_max_open_positions}"
                ),
            )

        max_trade_sol = portfolio.total_equity_sol * self.settings.risk_max_position_pct
        max_total_exposure_sol = (
            portfolio.total_equity_sol * self.settings.risk_max_total_exposure_pct
        )
        remaining_exposure_sol = max(0.0, max_total_exposure_sol - portfolio.total_exposure_sol)

        if remaining_exposure_sol <= 0.0:
            return RiskDecision(
                approved=False,
                reason=(
                    f"RECHAZADA: exposición total alcanzada "
                    f"({self.settings.risk_max_total_exposure_pct:.0%})"
                ),
            )

        approved_sol = min(order.amount_sol, max_trade_sol, remaining_exposure_sol)
        if approved_sol <= 0.0:
            return RiskDecision(approved=False, reason="RECHAZADA: tamaño de orden <= 0")

        return RiskDecision(approved=True, approved_amount_sol=approved_sol, reason="OK")
