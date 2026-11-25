"""Async DexScreener client for discovering graduated Solana pairs and live snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

ALLOWED_DEXS = frozenset({"raydium", "orca", "meteora", "pumpswap"})


@dataclass(slots=True)
class DexPairSnapshot:
    mint: str
    symbol: str
    name: str
    pool_address: str
    dex: str
    price_usd: float
    price_sol: float
    liquidity_usd: float
    volume_h1_usd: float
    created_at: datetime | None


def _parse_pair(item: dict) -> DexPairSnapshot | None:
    if item.get("chainId") != "solana":
        return None
    base = item.get("baseToken") or {}
    mint = str(base.get("address") or "").strip()
    if not mint:
        return None

    try:
        price_usd = float(item.get("priceUsd") or 0.0)
        price_sol = float(item.get("priceNative") or 0.0)
        liquidity_usd = float((item.get("liquidity") or {}).get("usd") or 0.0)
        volume_h1_usd = float((item.get("volume") or {}).get("h1") or 0.0)
    except (TypeError, ValueError):
        return None

    created_ms = item.get("pairCreatedAt")
    created_at = (
        datetime.fromtimestamp(int(created_ms) / 1000.0, tz=UTC)
        if created_ms is not None
        else None
    )

    return DexPairSnapshot(
        mint=mint,
        symbol=str(base.get("symbol") or "UNKNOWN"),
        name=str(base.get("name") or ""),
        pool_address=str(item.get("pairAddress") or ""),
        dex=str(item.get("dexId") or "").lower(),
        price_usd=price_usd,
        price_sol=price_sol,
        liquidity_usd=liquidity_usd,
        volume_h1_usd=volume_h1_usd,
        created_at=created_at,
    )


class DexScreenerClient:
    """Fetches Solana token pairs and live price/liquidity snapshots from DexScreener."""

    def __init__(
        self,
        base_url: str = "https://api.dexscreener.com",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout_seconds

    async def _get_json(self, path: str, params: dict[str, str] | None = None) -> object:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            resp = await client.get(f"{self.base_url}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        finally:
            if self._owns_client:
                await client.aclose()

    async def fetch_token_pairs(self, mints: list[str]) -> list[DexPairSnapshot]:
        """Fetch pairs for up to 30 Solana token mints in a single request."""
        if not mints:
            return []
        csv = ",".join(mints[:30])
        data = await self._get_json(f"/tokens/v1/solana/{csv}")
        items = data if isinstance(data, list) else (data.get("pairs") or [])
        snapshots: list[DexPairSnapshot] = []
        for item in items:
            if isinstance(item, dict):
                snap = _parse_pair(item)
                if snap is not None:
                    snapshots.append(snap)
        return snapshots

    async def fetch_latest_boosted_solana_mints(self) -> list[str]:
        """Return unique Solana token mints from DexScreener's latest boosted feed."""
        data = await self._get_json("/token-boosts/latest/v1")
        if not isinstance(data, list):
            return []
        seen: set[str] = set()
        mints: list[str] = []
        for item in data:
            if isinstance(item, dict) and item.get("chainId") == "solana":
                addr = str(item.get("tokenAddress") or "").strip()
                if addr and addr not in seen:
                    seen.add(addr)
                    mints.append(addr)
        return mints

    async def search_solana_pairs(self, query: str) -> list[DexPairSnapshot]:
        data = await self._get_json("/latest/dex/search", params={"q": query})
        pairs = data.get("pairs", []) if isinstance(data, dict) else []
        out: list[DexPairSnapshot] = []
        for item in pairs:
            if isinstance(item, dict):
                snap = _parse_pair(item)
                if snap is not None:
                    out.append(snap)
        return out
