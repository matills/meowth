"""Single-writer DuckDB repository and Parquet backup utilities."""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb

from solana_trading.models import (
    Candle,
    EquitySnapshot,
    LLMCallRecord,
    Rejection,
    Signal,
    Tick,
    TokenCandidate,
    TradeRecord,
)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
TABLES_TO_BACKUP = (
    "tokens",
    "ticks",
    "candles",
    "signals",
    "rejections",
    "trades",
    "llm_calls",
    "equity_curve",
    "system_state",
)


class TradingDatabase:
    """Thread-safe single-writer wrapper around a local DuckDB database."""

    def __init__(self, db_path: str | Path, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        self._lock = threading.RLock()
        if not read_only and str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(db_path), read_only=read_only)

    def init_schema(self) -> None:
        if self.read_only:
            raise RuntimeError("Cannot initialize schema on a read-only connection")
        sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock:
            self._conn.execute(sql)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> TradingDatabase:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- Tokens ---

    def upsert_token(self, token: TokenCandidate) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tokens (
                    mint, symbol, name, pool_address, dex, created_at,
                    liquidity_usd, mint_authority_revoked, freeze_authority_revoked,
                    top_holder_pct, dev_wallet, status, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (mint) DO UPDATE SET
                    symbol = excluded.symbol,
                    name = excluded.name,
                    pool_address = excluded.pool_address,
                    dex = excluded.dex,
                    created_at = COALESCE(tokens.created_at, excluded.created_at),
                    liquidity_usd = excluded.liquidity_usd,
                    mint_authority_revoked = excluded.mint_authority_revoked,
                    freeze_authority_revoked = excluded.freeze_authority_revoked,
                    top_holder_pct = excluded.top_holder_pct,
                    dev_wallet = COALESCE(excluded.dev_wallet, tokens.dev_wallet),
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                [
                    token.mint,
                    token.symbol,
                    token.name,
                    token.pool_address,
                    token.dex,
                    token.created_at,
                    token.liquidity_usd,
                    token.mint_authority_revoked,
                    token.freeze_authority_revoked,
                    token.top_holder_pct,
                    token.dev_wallet,
                    token.status,
                    token.first_seen_at,
                    token.updated_at,
                ],
            )

    def get_approved_mints(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT mint FROM tokens WHERE status = 'approved' ORDER BY updated_at DESC"
            ).fetchall()
            return [str(r[0]) for r in rows]

    # --- Ticks & Candles ---

    def insert_tick(self, tick: Tick) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO ticks (
                    mint, ts, price_sol, price_usd, volume_usd, liquidity_usd, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    tick.mint,
                    tick.ts,
                    tick.price_sol,
                    tick.price_usd,
                    tick.volume_usd,
                    tick.liquidity_usd,
                    tick.source,
                ],
            )

    def upsert_candle(self, candle: Candle) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO candles (
                    mint, timeframe, open_time, open, high, low, close, volume_usd, liquidity_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (mint, timeframe, open_time) DO UPDATE SET
                    high = GREATEST(candles.high, excluded.high),
                    low = LEAST(candles.low, excluded.low),
                    close = excluded.close,
                    volume_usd = excluded.volume_usd,
                    liquidity_usd = excluded.liquidity_usd
                """,
                [
                    candle.mint,
                    candle.timeframe.value,
                    candle.open_time,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume_usd,
                    candle.liquidity_usd,
                ],
            )

    # --- Signals, Rejections, Trades ---

    def insert_signal(self, signal: Signal) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO signals (
                    signal_id, ts, mint, strategy_name, side, confidence, reason, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    signal.signal_id,
                    signal.ts,
                    signal.mint,
                    signal.strategy_name,
                    signal.side.value,
                    signal.confidence,
                    signal.reason,
                    json.dumps(signal.metadata),
                ],
            )

    def insert_rejection(self, rejection: Rejection) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO rejections (rejection_id, ts, mint, stage, reason, details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    rejection.rejection_id,
                    rejection.ts,
                    rejection.mint,
                    rejection.stage.value,
                    rejection.reason,
                    json.dumps(rejection.details),
                ],
            )

    def upsert_trade(self, trade: TradeRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO trades (
                    trade_id, mode, signal_id, mint, side, status, strategy_name, reason,
                    qty_tokens, amount_sol, quoted_price_sol, filled_price_sol,
                    slippage_bps, price_impact_pct, fees_sol, pnl_sol, pnl_pct,
                    opened_at, closed_at, exit_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (trade_id) DO UPDATE SET
                    status = excluded.status,
                    qty_tokens = excluded.qty_tokens,
                    filled_price_sol = excluded.filled_price_sol,
                    slippage_bps = excluded.slippage_bps,
                    fees_sol = excluded.fees_sol,
                    pnl_sol = excluded.pnl_sol,
                    pnl_pct = excluded.pnl_pct,
                    closed_at = excluded.closed_at,
                    exit_reason = excluded.exit_reason
                """,
                [
                    trade.trade_id,
                    trade.mode.value,
                    trade.signal_id,
                    trade.mint,
                    trade.side.value,
                    trade.status.value,
                    trade.strategy_name,
                    trade.reason,
                    trade.qty_tokens,
                    trade.amount_sol,
                    trade.quoted_price_sol,
                    trade.filled_price_sol,
                    trade.slippage_bps,
                    trade.price_impact_pct,
                    trade.fees_sol,
                    trade.pnl_sol,
                    trade.pnl_pct,
                    trade.opened_at,
                    trade.closed_at,
                    trade.exit_reason,
                ],
            )

    # --- LLM Calls & Budget Queries ---

    def insert_llm_call(self, record: LLMCallRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO llm_calls (
                    call_id, ts, role, provider, model, mint, prompt_hash,
                    input_tokens, output_tokens, cost_usd, latency_ms, response_json, cached
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    record.call_id,
                    record.ts,
                    record.role,
                    record.provider,
                    record.model,
                    record.mint,
                    record.prompt_hash,
                    record.input_tokens,
                    record.output_tokens,
                    record.cost_usd,
                    record.latency_ms,
                    record.response_json,
                    record.cached,
                ],
            )

    def get_daily_llm_spend_usd(self, day: date | None = None) -> float:
        target_day = day or datetime.now(UTC).date()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(cost_usd), 0.0)
                FROM llm_calls
                WHERE CAST(ts AT TIME ZONE 'UTC' AS DATE) = ?
                """,
                [target_day],
            ).fetchone()
            return float(row[0]) if row else 0.0

    def get_cached_llm_response(self, role: str, prompt_hash: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT response_json
                FROM llm_calls
                WHERE role = ? AND prompt_hash = ?
                ORDER BY ts DESC
                LIMIT 1
                """,
                [role, prompt_hash],
            ).fetchone()
            return str(row[0]) if row else None

    # --- Equity & System State ---

    def insert_equity_snapshot(self, snap: EquitySnapshot) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO equity_curve (
                    ts, mode, cash_sol, positions_value_sol, total_equity_sol,
                    sol_price_usd, drawdown_pct, open_positions
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    snap.ts,
                    snap.mode.value,
                    snap.cash_sol,
                    snap.positions_value_sol,
                    snap.total_equity_sol,
                    snap.sol_price_usd,
                    snap.drawdown_pct,
                    snap.open_positions,
                ],
            )

    def set_state(self, key: str, value: str) -> None:
        now = datetime.now(UTC)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [key, value, now],
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM system_state WHERE key = ?",
                [key],
            ).fetchone()
            return str(row[0]) if row else default

    # --- Queries & Parquet Backup ---

    def list_tables(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SHOW TABLES").fetchall()
            return sorted(str(r[0]) for r in rows)

    def count_rows(self, table: str) -> int:
        if table not in TABLES_TO_BACKUP:
            raise ValueError(f"Unknown table: {table}")
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            return int(row[0]) if row else 0

    def export_parquet_backup(self, output_dir: str | Path, tag: str | None = None) -> Path:
        """Export every table to Parquet files in a dated snapshot folder."""
        snapshot_tag = tag or datetime.now(UTC).strftime("%Y-%m-%d")
        target_dir = Path(output_dir) / snapshot_tag
        target_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            for table in TABLES_TO_BACKUP:
                dest = (target_dir / f"{table}.parquet").as_posix()
                self._conn.execute(f"COPY {table} TO '{dest}' (FORMAT PARQUET)")
        return target_dir
