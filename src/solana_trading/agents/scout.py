"""Scout agent: discovers graduated Solana pairs and applies deterministic hard filters."""

from __future__ import annotations

from datetime import UTC, datetime

from solana_trading.config import Settings
from solana_trading.data.dexscreener import ALLOWED_DEXS, DexPairSnapshot, DexScreenerClient
from solana_trading.data.jupiter import JupiterQuoteClient
from solana_trading.data.solana_rpc import OnChainInspectionError, OnChainTokenInspector
from solana_trading.models import PipelineStage, Rejection, TokenCandidate
from solana_trading.storage.db import TradingDatabase


class ScoutAgent:
    """Filters candidate Solana tokens with hard on-chain & liquidity rules before LLM/Strategy."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: TradingDatabase,
        dexscreener: DexScreenerClient,
        inspector: OnChainTokenInspector,
        jupiter: JupiterQuoteClient,
        max_top_holder_pct: float = 0.25,
    ) -> None:
        self.settings = settings
        self.db = db
        self.dexscreener = dexscreener
        self.inspector = inspector
        self.jupiter = jupiter
        self.max_top_holder_pct = max_top_holder_pct

    def _reject(self, mint: str, reason: str, details: dict | None = None) -> Rejection:
        rejection = Rejection(
            mint=mint,
            stage=PipelineStage.SCOUT,
            reason=reason,
            details=details or {},
        )
        self.db.insert_rejection(rejection)
        return rejection

    async def evaluate_pair(
        self,
        pair: DexPairSnapshot,
        *,
        now: datetime | None = None,
    ) -> tuple[TokenCandidate | None, Rejection | None]:
        current = now or datetime.now(UTC)

        # 1. Graduated DEX only (Raydium, Orca, Meteora)
        if pair.dex not in ALLOWED_DEXS:
            return None, self._reject(
                pair.mint,
                f"RECHAZADA: DEX no permitido ({pair.dex})",
                {"dex": pair.dex},
            )

        # 2. Minimum liquidity
        if pair.liquidity_usd < self.settings.risk_min_liquidity_usd:
            return None, self._reject(
                pair.mint,
                (
                    f"RECHAZADA: liquidez USD {pair.liquidity_usd:,.0f}, "
                    f"mínimo USD {self.settings.risk_min_liquidity_usd:,.0f}"
                ),
                {"liquidity_usd": pair.liquidity_usd},
            )

        # 3. Minimum age (>= 60 min by default)
        if pair.created_at is None:
            return None, self._reject(pair.mint, "RECHAZADA: fecha de creación desconocida")

        age_minutes = (current - pair.created_at).total_seconds() / 60.0
        if age_minutes < self.settings.risk_min_token_age_minutes:
            return None, self._reject(
                pair.mint,
                (
                    f"RECHAZADA: edad del token {age_minutes:.0f}m, "
                    f"mínimo {self.settings.risk_min_token_age_minutes}m"
                ),
                {"age_minutes": age_minutes},
            )

        # 4. On-chain security checks (mint/freeze authority & top holder concentration)
        try:
            security = await self.inspector.inspect_mint(pair.mint)
        except OnChainInspectionError as exc:
            return None, self._reject(
                pair.mint,
                f"RECHAZADA: error en inspección on-chain ({exc})",
            )

        if not security.mint_authority_revoked:
            return None, self._reject(pair.mint, "RECHAZADA: mint authority activa")

        if not security.freeze_authority_revoked:
            return None, self._reject(pair.mint, "RECHAZADA: freeze authority activa")

        if (
            security.top_holder_pct is not None
            and security.top_holder_pct > self.max_top_holder_pct
        ):
            return None, self._reject(
                pair.mint,
                (
                    f"RECHAZADA: top holder concentra {security.top_holder_pct:.1%}, "
                    f"límite {self.max_top_holder_pct:.1%}"
                ),
                {"top_holder_pct": security.top_holder_pct},
            )

        # 5. Jupiter sellability (honeypot) & buy price impact check
        sell_check = await self.jupiter.check_sellable(
            pair.mint,
            token_decimals=security.decimals,
            slippage_bps=int(self.settings.risk_max_price_impact_pct * 10_000),
        )
        if not sell_check.sellable:
            return None, self._reject(
                pair.mint,
                sell_check.reason,
                {"roundtrip_loss_pct": sell_check.roundtrip_loss_pct},
            )

        if (
            sell_check.buy_quote is not None
            and sell_check.buy_quote.price_impact_pct > self.settings.risk_max_price_impact_pct
        ):
            impact = sell_check.buy_quote.price_impact_pct
            return None, self._reject(
                pair.mint,
                (
                    f"RECHAZADA: slippage estimado {impact:.1%}, "
                    f"límite {self.settings.risk_max_price_impact_pct:.1%}"
                ),
                {"price_impact_pct": impact},
            )

        candidate = TokenCandidate(
            mint=pair.mint,
            symbol=pair.symbol,
            name=pair.name,
            pool_address=pair.pool_address,
            dex=pair.dex,
            created_at=pair.created_at,
            liquidity_usd=pair.liquidity_usd,
            mint_authority_revoked=True,
            freeze_authority_revoked=True,
            top_holder_pct=security.top_holder_pct,
            status="approved",
            updated_at=current,
        )
        self.db.upsert_token(candidate)
        return candidate, None

    async def scan_mints(
        self,
        mints: list[str],
        *,
        now: datetime | None = None,
    ) -> tuple[list[TokenCandidate], list[Rejection]]:
        """Fetch pairs for the given mints, pick highest-liquidity pair per mint, and filter."""
        pairs = await self.dexscreener.fetch_token_pairs(mints)
        best_by_mint: dict[str, DexPairSnapshot] = {}
        for pair in pairs:
            prev = best_by_mint.get(pair.mint)
            if prev is None or pair.liquidity_usd > prev.liquidity_usd:
                best_by_mint[pair.mint] = pair

        approved: list[TokenCandidate] = []
        rejected: list[Rejection] = []
        for pair in best_by_mint.values():
            cand, rej = await self.evaluate_pair(pair, now=now)
            if cand is not None:
                approved.append(cand)
            if rej is not None:
                rejected.append(rej)
        return approved, rejected
