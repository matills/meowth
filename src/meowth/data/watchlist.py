"""Established high-liquidity Solana SPL token mints for baseline tracking."""

from __future__ import annotations

# Verified mainnet-beta SPL mint addresses (Raydium / Orca / Jupiter liquid tokens)
ESTABLISHED_SOLANA_TOKENS: dict[str, str] = {
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
}

ESTABLISHED_SOLANA_MINTS: list[str] = list(ESTABLISHED_SOLANA_TOKENS.values())
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
