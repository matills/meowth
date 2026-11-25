"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from meowth.config import Settings
from meowth.storage.db import TradingDatabase


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        duckdb_path=tmp_path / "test.duckdb",
        parquet_backup_dir=tmp_path / "backups",
        telegram_bot_token=None,
        telegram_chat_id=None,
    )


@pytest.fixture
def db(tmp_path: Path) -> Iterator[TradingDatabase]:
    database = TradingDatabase(tmp_path / "test.duckdb")
    database.init_schema()
    yield database
    database.close()
