"""On-chain Solana SPL token security inspector via JSON-RPC and solders."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from solders.pubkey import Pubkey


class OnChainInspectionError(RuntimeError):
    """Raised when on-chain token metadata cannot be inspected."""


@dataclass(slots=True)
class TokenSecurityReport:
    mint: str
    decimals: int
    mint_authority_revoked: bool
    freeze_authority_revoked: bool
    top_holder_pct: float | None = None
    top10_holders_pct: float | None = None


class OnChainTokenInspector:
    """Checks SPL mint/freeze authorities and top-holder concentration via Solana RPC."""

    def __init__(
        self,
        rpc_url: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.rpc_url = rpc_url
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds

    async def _rpc_call(self, method: str, params: list[object]) -> dict:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            resp.raise_for_status()
            payload = resp.json()
        except httpx.HTTPError as exc:
            raise OnChainInspectionError(f"RPC {method} failed: {exc}") from exc
        finally:
            if self._owns_client:
                await client.aclose()

        if "error" in payload:
            raise OnChainInspectionError(f"RPC {method} error: {payload['error']}")
        return payload.get("result") or {}

    async def inspect_mint(self, mint: str) -> TokenSecurityReport:
        # Validate base58 Solana pubkey using solders
        try:
            Pubkey.from_string(mint)
        except ValueError as exc:
            raise OnChainInspectionError(f"Invalid Solana mint address: {mint}") from exc

        account_res = await self._rpc_call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        value = account_res.get("value")
        if not isinstance(value, dict):
            raise OnChainInspectionError(f"Mint account not found on-chain: {mint}")

        info = ((value.get("data") or {}).get("parsed") or {}).get("info")
        if not isinstance(info, dict):
            raise OnChainInspectionError(f"Account is not a parsed SPL mint: {mint}")

        mint_auth = info.get("mintAuthority")
        freeze_auth = info.get("freezeAuthority")
        decimals = int(info.get("decimals", 6))
        total_supply = float(info.get("supply") or 0.0)

        top_holder_pct: float | None = None
        top10_holders_pct: float | None = None

        if total_supply > 0:
            largest_res = await self._rpc_call("getTokenLargestAccounts", [mint])
            accounts = largest_res.get("value") or []
            if isinstance(accounts, list) and accounts:
                amounts = [float(a.get("amount") or 0.0) for a in accounts if isinstance(a, dict)]
                if amounts:
                    top_holder_pct = amounts[0] / total_supply
                    top10_holders_pct = sum(amounts[:10]) / total_supply

        return TokenSecurityReport(
            mint=mint,
            decimals=decimals,
            mint_authority_revoked=mint_auth is None,
            freeze_authority_revoked=freeze_auth is None,
            top_holder_pct=top_holder_pct,
            top10_holders_pct=top10_holders_pct,
        )
