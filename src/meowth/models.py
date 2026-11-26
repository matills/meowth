"""Core domain models shared across Scout, Analyst, Strategist, Risk, and Executor."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid4().hex


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"


class PipelineStage(StrEnum):
    SCOUT = "scout"
    ANALYST = "analyst"
    STRATEGIST = "strategist"
    RISK = "risk"
    EXECUTOR = "executor"


class TradeStatus(StrEnum):
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
    TRAPPED = "TRAPPED"


class TokenCandidate(BaseModel):
    mint: str
    symbol: str
    name: str = ""
    decimals: int = 6
    pool_address: str = ""
    dex: str = ""
    created_at: datetime | None = None
    liquidity_usd: float = 0.0
    mint_authority_revoked: bool = False
    freeze_authority_revoked: bool = False
    top_holder_pct: float | None = None
    dev_wallet: str | None = None
    status: str = "candidate"
    first_seen_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Tick(BaseModel):
    mint: str
    ts: datetime = Field(default_factory=utc_now)
    price_sol: float
    price_usd: float
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0
    source: str = "dexscreener"


class Candle(BaseModel):
    mint: str
    timeframe: Timeframe
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0


class Signal(BaseModel):
    signal_id: str = Field(default_factory=new_id)
    ts: datetime = Field(default_factory=utc_now)
    mint: str
    strategy_name: str
    side: Side
    confidence: float = 1.0
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class Rejection(BaseModel):
    rejection_id: str = Field(default_factory=new_id)
    ts: datetime = Field(default_factory=utc_now)
    mint: str
    stage: PipelineStage
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)


class Quote(BaseModel):
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_sol: float
    price_impact_pct: float
    fee_sol: float = 0.0
    ts: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class OrderRequest(BaseModel):
    signal_id: str
    mint: str
    side: Side
    amount_sol: float
    strategy_name: str
    reason: str
    max_slippage_bps: int = 200
    quote: Quote | None = None


class TradeRecord(BaseModel):
    trade_id: str = Field(default_factory=new_id)
    mode: TradingMode = TradingMode.PAPER
    signal_id: str | None = None
    mint: str
    side: Side
    status: TradeStatus = TradeStatus.OPEN
    strategy_name: str
    reason: str
    qty_tokens: float
    amount_sol: float
    quoted_price_sol: float
    filled_price_sol: float
    slippage_bps: float = 0.0
    price_impact_pct: float = 0.0
    fees_sol: float = 0.0
    pnl_sol: float | None = None
    pnl_pct: float | None = None
    opened_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None
    exit_reason: str | None = None


class LLMCallRecord(BaseModel):
    call_id: str = Field(default_factory=new_id)
    ts: datetime = Field(default_factory=utc_now)
    role: str
    provider: str
    model: str
    mint: str | None = None
    prompt_hash: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    response_json: str = "{}"
    cached: bool = False


class EquitySnapshot(BaseModel):
    ts: datetime = Field(default_factory=utc_now)
    mode: TradingMode = TradingMode.PAPER
    cash_sol: float
    positions_value_sol: float
    total_equity_sol: float
    sol_price_usd: float = 0.0
    drawdown_pct: float = 0.0
    open_positions: int = 0
