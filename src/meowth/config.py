"""Typed configuration loaded from environment variables / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from meowth.models import TradingMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Runtime
    trading_mode: TradingMode = TradingMode.PAPER
    log_level: str = "INFO"

    # Storage
    duckdb_path: Path = Path("data/meowth.duckdb")
    parquet_backup_dir: Path = Path("backups/parquet")

    # Solana RPC / WS
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_ws_url: str = "wss://api.mainnet-beta.solana.com"

    # Market data & quotes
    jupiter_api_url: str = "https://lite-api.jup.ag/swap/v1"
    dexscreener_api_url: str = "https://api.dexscreener.com"
    birdeye_api_key: SecretStr | None = None

    # Telegram alerts & kill switch
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # LLM Analyst & Supervisor
    llm_provider: str = "none"
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    llm_daily_budget_usd: float = Field(default=1.0, ge=0.0)
    llm_input_cost_per_1m_usd: float = Field(default=0.10, ge=0.0)
    llm_output_cost_per_1m_usd: float = Field(default=0.40, ge=0.0)

    # Paper account
    paper_initial_capital_sol: float = Field(default=10.0, gt=0.0)
    paper_extra_slippage_bps: int = Field(default=50, ge=0)
    paper_priority_fee_lamports: int = Field(default=5000, ge=0)

    # Deterministic risk limits & kill switches
    risk_max_position_pct: float = Field(default=0.02, gt=0.0, le=1.0)
    risk_max_open_positions: int = Field(default=5, ge=1)
    risk_max_total_exposure_pct: float = Field(default=0.30, gt=0.0, le=1.0)
    risk_stop_loss_pct: float = Field(default=0.20, gt=0.0, lt=1.0)
    risk_take_profit_partial_pct: float = Field(default=0.50, gt=0.0)
    risk_max_hold_hours: int = Field(default=24, ge=1)
    risk_min_liquidity_usd: float = Field(default=50_000.0, ge=0.0)
    risk_min_token_age_minutes: int = Field(default=60, ge=0)
    risk_max_price_impact_pct: float = Field(default=0.02, gt=0.0, lt=1.0)
    risk_max_daily_loss_pct: float = Field(default=0.05, gt=0.0, lt=1.0)
    risk_max_drawdown_pct: float = Field(default=0.20, gt=0.0, lt=1.0)
    risk_stale_data_seconds: int = Field(default=60, ge=5)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)
