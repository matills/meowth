"""Async Jupiter v6 quote client with price-impact parsing and honeypot sell check."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from meowth.models import Quote

WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


class JupiterQuoteError(RuntimeError):
    """Raised when Jupiter cannot return a valid swap quote."""


@dataclass(slots=True)
class SellabilityResult:
    sellable: bool
    reason: str
    buy_quote: Quote | None = None
    sell_quote: Quote | None = None
    roundtrip_loss_pct: float | None = None


class JupiterQuoteClient:
    """Fetches real-time swap quotes routed across Raydium, Orca, and other Solana DEXs."""

    def __init__(
        self,
        base_url: str = "https://lite-api.jup.ag/swap/v1",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        *,
        slippage_bps: int = 200,
        input_decimals: int = 9,
        output_decimals: int = 6,
    ) -> Quote:
        if amount_atomic <= 0:
            raise JupiterQuoteError("amount_atomic must be > 0")

        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_atomic),
            "slippageBps": str(slippage_bps),
        }

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.get(f"{self.base_url}/quote", params=params)
            if resp.status_code != 200:
                raise JupiterQuoteError(f"Jupiter HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
        except httpx.HTTPError as exc:
            raise JupiterQuoteError(f"Jupiter request failed: {exc}") from exc
        finally:
            if self._owns_client:
                await client.aclose()

        try:
            in_amount = int(data["inAmount"])
            out_amount = int(data["outAmount"])
            price_impact_pct = abs(float(data.get("priceImpactPct", 0.0)))
        except (KeyError, TypeError, ValueError) as exc:
            raise JupiterQuoteError(f"Invalid Jupiter quote payload: {exc}") from exc

        if in_amount <= 0 or out_amount <= 0:
            raise JupiterQuoteError("Jupiter returned zero in/out amount")

        in_human = in_amount / (10**input_decimals)
        out_human = out_amount / (10**output_decimals)

        if input_mint == WSOL_MINT:
            price_sol = in_human / out_human
        elif output_mint == WSOL_MINT:
            price_sol = out_human / in_human
        else:
            price_sol = in_human / out_human

        fee_lamports = 0
        for leg in data.get("routePlan", []):
            swap_info = leg.get("swapInfo", {})
            if swap_info.get("feeMint") == WSOL_MINT:
                try:
                    fee_lamports += int(swap_info.get("feeAmount", 0))
                except (TypeError, ValueError):
                    pass

        return Quote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=in_amount,
            out_amount=out_amount,
            price_sol=price_sol,
            price_impact_pct=price_impact_pct,
            fee_sol=fee_lamports / LAMPORTS_PER_SOL,
            raw=data,
        )

    async def check_sellable(
        self,
        token_mint: str,
        *,
        test_sol: float = 0.1,
        token_decimals: int = 6,
        max_roundtrip_loss_pct: float = 0.10,
        slippage_bps: int = 200,
    ) -> SellabilityResult:
        """Simulate a buy and immediate sell quote to detect honeypots or illiquid exits."""
        lamports = int(test_sol * LAMPORTS_PER_SOL)
        try:
            buy_quote = await self.get_quote(
                WSOL_MINT,
                token_mint,
                lamports,
                slippage_bps=slippage_bps,
                input_decimals=9,
                output_decimals=token_decimals,
            )
        except JupiterQuoteError as exc:
            return SellabilityResult(
                sellable=False,
                reason=f"RECHAZADA: sin cotización de compra en Jupiter ({exc})",
            )

        try:
            sell_quote = await self.get_quote(
                token_mint,
                WSOL_MINT,
                buy_quote.out_amount,
                slippage_bps=slippage_bps,
                input_decimals=token_decimals,
                output_decimals=9,
            )
        except JupiterQuoteError as exc:
            return SellabilityResult(
                sellable=False,
                reason=f"RECHAZADA: posible honeypot, sin ruta de venta en Jupiter ({exc})",
                buy_quote=buy_quote,
            )

        roundtrip_loss_pct = max(0.0, (lamports - sell_quote.out_amount) / lamports)
        if roundtrip_loss_pct > max_roundtrip_loss_pct:
            return SellabilityResult(
                sellable=False,
                reason=(
                    f"RECHAZADA: pérdida ida y vuelta {roundtrip_loss_pct:.1%}, "
                    f"límite {max_roundtrip_loss_pct:.1%} (posible honeypot o tax oculto)"
                ),
                buy_quote=buy_quote,
                sell_quote=sell_quote,
                roundtrip_loss_pct=roundtrip_loss_pct,
            )

        return SellabilityResult(
            sellable=True,
            reason="OK",
            buy_quote=buy_quote,
            sell_quote=sell_quote,
            roundtrip_loss_pct=roundtrip_loss_pct,
        )
