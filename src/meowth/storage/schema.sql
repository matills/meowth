-- DuckDB analytical schema for Solana 24/7 trading system.
-- Single-writer pattern: one writer process mutates, readers query snapshots or Parquet exports.

CREATE TABLE IF NOT EXISTS tokens (
    mint VARCHAR PRIMARY KEY,
    symbol VARCHAR NOT NULL,
    name VARCHAR DEFAULT '',
    decimals INTEGER DEFAULT 6,
    pool_address VARCHAR DEFAULT '',
    dex VARCHAR DEFAULT '',
    created_at TIMESTAMPTZ,
    liquidity_usd DOUBLE DEFAULT 0.0,
    mint_authority_revoked BOOLEAN DEFAULT FALSE,
    freeze_authority_revoked BOOLEAN DEFAULT FALSE,
    top_holder_pct DOUBLE,
    dev_wallet VARCHAR,
    status VARCHAR DEFAULT 'candidate',
    first_seen_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS ticks (
    mint VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    price_sol DOUBLE NOT NULL,
    price_usd DOUBLE NOT NULL,
    volume_usd DOUBLE DEFAULT 0.0,
    liquidity_usd DOUBLE DEFAULT 0.0,
    source VARCHAR DEFAULT 'dexscreener'
);

CREATE TABLE IF NOT EXISTS candles (
    mint VARCHAR NOT NULL,
    timeframe VARCHAR NOT NULL,
    open_time TIMESTAMPTZ NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume_usd DOUBLE DEFAULT 0.0,
    liquidity_usd DOUBLE DEFAULT 0.0,
    PRIMARY KEY (mint, timeframe, open_time)
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id VARCHAR PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    mint VARCHAR NOT NULL,
    strategy_name VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    confidence DOUBLE DEFAULT 1.0,
    reason VARCHAR NOT NULL,
    metadata_json VARCHAR DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS rejections (
    rejection_id VARCHAR PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    mint VARCHAR NOT NULL,
    stage VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    details_json VARCHAR DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id VARCHAR PRIMARY KEY,
    mode VARCHAR NOT NULL,
    signal_id VARCHAR,
    mint VARCHAR NOT NULL,
    side VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    strategy_name VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    qty_tokens DOUBLE NOT NULL,
    amount_sol DOUBLE NOT NULL,
    quoted_price_sol DOUBLE NOT NULL,
    filled_price_sol DOUBLE NOT NULL,
    slippage_bps DOUBLE DEFAULT 0.0,
    price_impact_pct DOUBLE DEFAULT 0.0,
    fees_sol DOUBLE DEFAULT 0.0,
    pnl_sol DOUBLE,
    pnl_pct DOUBLE,
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ,
    exit_reason VARCHAR
);

CREATE TABLE IF NOT EXISTS llm_calls (
    call_id VARCHAR PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    role VARCHAR NOT NULL,
    provider VARCHAR NOT NULL,
    model VARCHAR NOT NULL,
    mint VARCHAR,
    prompt_hash VARCHAR NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd DOUBLE DEFAULT 0.0,
    latency_ms INTEGER DEFAULT 0,
    response_json VARCHAR DEFAULT '{}',
    cached BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts TIMESTAMPTZ NOT NULL,
    mode VARCHAR NOT NULL,
    cash_sol DOUBLE NOT NULL,
    positions_value_sol DOUBLE NOT NULL,
    total_equity_sol DOUBLE NOT NULL,
    sol_price_usd DOUBLE DEFAULT 0.0,
    drawdown_pct DOUBLE DEFAULT 0.0,
    open_positions INTEGER DEFAULT 0,
    PRIMARY KEY (ts, mode)
);

CREATE TABLE IF NOT EXISTS system_state (
    key VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
