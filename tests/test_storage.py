"""Tests for DuckDB schema, CRUD operations, and Parquet backup."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from solana_trading.models import (
    Candle,
    EquitySnapshot,
    LLMCallRecord,
    PipelineStage,
    Rejection,
    Side,
    Signal,
    Tick,
    Timeframe,
    TokenCandidate,
    TradeRecord,
)
from solana_trading.storage.db import TABLES_TO_BACKUP, TradingDatabase


def test_schema_creates_all_tables(db: TradingDatabase) -> None:
    assert set(db.list_tables()) == set(TABLES_TO_BACKUP)


def test_upsert_and_backup_parquet(db: TradingDatabase, tmp_path: Path) -> None:
    now = datetime.now(UTC)

    db.upsert_token(
        TokenCandidate(
            mint="So11111111111111111111111111111111111111112",
            symbol="SOL",
            name="Wrapped SOL",
            liquidity_usd=500_000.0,
            mint_authority_revoked=True,
            freeze_authority_revoked=True,
        )
    )
    db.insert_tick(
        Tick(
            mint="So11111111111111111111111111111111111111112",
            price_sol=1.0,
            price_usd=180.0,
            volume_usd=1000.0,
            liquidity_usd=500_000.0,
        )
    )
    db.upsert_candle(
        Candle(
            mint="So11111111111111111111111111111111111111112",
            timeframe=Timeframe.M1,
            open_time=now.replace(second=0, microsecond=0),
            open=1.0,
            high=1.05,
            low=0.98,
            close=1.02,
            volume_usd=2500.0,
        )
    )
    sig = Signal(
        mint="MintABC",
        strategy_name="momentum_5m",
        side=Side.BUY,
        reason="Breakout volume > 3x",
    )
    db.insert_signal(sig)
    db.insert_rejection(
        Rejection(
            mint="MintXYZ",
            stage=PipelineStage.RISK,
            reason="RECHAZADA: slippage estimado 3,4%, límite 2,0%",
        )
    )
    db.upsert_trade(
        TradeRecord(
            signal_id=sig.signal_id,
            mint="MintABC",
            side=Side.BUY,
            strategy_name="momentum_5m",
            reason=sig.reason,
            qty_tokens=1000.0,
            amount_sol=0.2,
            quoted_price_sol=0.0002,
            filled_price_sol=0.000202,
            slippage_bps=100.0,
            fees_sol=0.0005,
        )
    )
    db.insert_llm_call(
        LLMCallRecord(
            role="analyst",
            provider="gemini",
            model="gemini-flash",
            mint="MintABC",
            prompt_hash="abc123",
            input_tokens=500,
            output_tokens=100,
            cost_usd=0.00025,
            latency_ms=320,
            response_json='{"score": 8}',
        )
    )
    db.insert_equity_snapshot(
        EquitySnapshot(
            cash_sol=9.8,
            positions_value_sol=0.2,
            total_equity_sol=10.0,
            sol_price_usd=180.0,
            open_positions=1,
        )
    )
    db.set_state("manual_pause", "false")

    assert db.count_rows("tokens") == 1
    assert db.count_rows("trades") == 1
    assert db.count_rows("rejections") == 1
    assert db.get_state("manual_pause") == "false"
    assert db.get_daily_llm_spend_usd() > 0.0

    backup_dir = db.export_parquet_backup(tmp_path / "parquet", tag="test-snap")
    for table in TABLES_TO_BACKUP:
        assert (backup_dir / f"{table}.parquet").exists()
