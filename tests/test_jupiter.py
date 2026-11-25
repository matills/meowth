"""Tests for JupiterQuoteClient quote parsing and honeypot sellability check."""

from __future__ import annotations

import httpx
import pytest

from meowth.data.jupiter import (
    LAMPORTS_PER_SOL,
    WSOL_MINT,
    JupiterQuoteClient,
    JupiterQuoteError,
)


async def test_get_quote_parses_price_and_impact() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/quote")
        return httpx.Response(
            200,
            json={
                "inputMint": WSOL_MINT,
                "outputMint": "TokenMint111",
                "inAmount": str(LAMPORTS_PER_SOL),
                "outAmount": str(500 * 10**6),
                "priceImpactPct": "0.012",
                "routePlan": [
                    {"swapInfo": {"feeMint": WSOL_MINT, "feeAmount": "2500000"}},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jup = JupiterQuoteClient(client=client)
        quote = await jup.get_quote(WSOL_MINT, "TokenMint111", LAMPORTS_PER_SOL)

    assert quote.price_sol == pytest.approx(0.002)
    assert quote.price_impact_pct == pytest.approx(0.012)
    assert quote.fee_sol == pytest.approx(0.0025)


async def test_check_sellable_detects_honeypot_and_passes_healthy_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        in_mint = request.url.params["inputMint"]
        out_mint = request.url.params["outputMint"]
        if out_mint == "HealthyMint":
            return httpx.Response(
                200,
                json={"inAmount": "100000000", "outAmount": "50000000", "priceImpactPct": "0.005"},
            )
        if in_mint == "HealthyMint":
            return httpx.Response(
                200,
                json={"inAmount": "50000000", "outAmount": "98500000", "priceImpactPct": "0.005"},
            )
        if out_mint == "HoneypotMint":
            return httpx.Response(
                200,
                json={"inAmount": "100000000", "outAmount": "50000000", "priceImpactPct": "0.01"},
            )
        if in_mint == "HoneypotMint":
            return httpx.Response(
                200,
                json={"inAmount": "50000000", "outAmount": "40000000", "priceImpactPct": "0.50"},
            )
        return httpx.Response(404, text="No route")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jup = JupiterQuoteClient(client=client)

        ok = await jup.check_sellable("HealthyMint", test_sol=0.1)
        assert ok.sellable is True
        assert ok.roundtrip_loss_pct == pytest.approx(0.015)

        bad = await jup.check_sellable("HoneypotMint", test_sol=0.1)
        assert bad.sellable is False
        assert "honeypot" in bad.reason.lower()

        no_route = await jup.check_sellable("DeadMint", test_sol=0.1)
        assert no_route.sellable is False
        assert "RECHAZADA:" in no_route.reason


async def test_get_quote_raises_on_invalid_amount() -> None:
    jup = JupiterQuoteClient()
    with pytest.raises(JupiterQuoteError):
        await jup.get_quote(WSOL_MINT, "TokenMint111", 0)
